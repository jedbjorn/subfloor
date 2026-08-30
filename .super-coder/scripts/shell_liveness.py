#!/usr/bin/env python3
"""Shell-liveness snapshot — which shells have a LIVE harness session right now,
read straight from the OS in one pass from a single vantage. The read-side
companion to git_cleanup: before the admin touches another shell's worktree it
must know that shell is dormant.

Why the OS and not the DB: there is no liveness flag in shell_db.db
(shell_memory_archives carries only a date), and run.py ends in
`os.chdir(work_dir); os.execvpe(...)` — the launcher BECOMES the harness, cwd
pinned to the shell's worktree, leaving no exit hook to clear a bool. A persisted
flag would go stale on `kill -9` or reboot. That same exec hands us a clean,
self-cleaning signal instead: a live harness process is one whose cwd sits inside
a worktree. The process dies → the signal vanishes. No cron, no persistence, no
staleness window. Reporting only, by design — like git_hygiene.py, it surfaces
state and never mutates.

Mechanism (Linux): scan /proc/<pid>/{comm,cwd}. A process whose comm is one of the
fork's harness comm values (adapters/*/adapter.json `launch[0]`,
`headless.launch[0]`, and `comm_aliases` — see harness_binaries) and whose cwd is
under THIS repo is a live shell session:
  • cwd == repo root            → the admin itself (the one shell that boots in
                                  root, not a worktree)
  • cwd under .sc-worktrees/<n> → the shell whose shortname.lower() == <n>

The admin runs this (directly or as a child of its own harness), and its OWN
session is positively identified only when the PPID walk finds a harness
ancestor whose cwd is the repo root. Host launchers may hide that ancestor; in
that case admin presence is indeterminate and cleanup fails closed. When Admin
is positively present, the gate is about OTHER shells.

Permissions: /proc/<pid>/cwd is readable only for same-user processes. A harness
owned by another OS user is counted but unreadable → `indeterminate`. When
`indeterminate > 0` the admin must NOT assume all-clear — surface instead. A
ZOMBIE harness is excluded before that question is asked: it kept its comm but
has already exited, so counting it would pin `indeterminate` — and with it the
cleanup gate — on a process that is gone.

Orphans: closing a terminal window does not reliably kill the harness on every
host — the session survives, holds its shell's one-session slot, and blocks
every headless boot of that shell until someone kills it by hand. Each process
is therefore classified (`orphaned`): 'tty-gone' (had a controlling terminal;
the pty vanished — the window closed under it), 'detached' (no controlling
TTY and reparented to init — its spawning session is gone), or None (normal).
"Vanished" is asked in the PROCESS's mount namespace (/proc/<pid>/root<tty>),
never the scanner's: a container-hosted session's pty lives in the container's
devpts, so testing the bare path from the host answers about a different device
and turns live work into a false orphan.
Classification is reporting only — an orphan may still be mid-work (a merge,
a suite), so nothing here kills anything. The consumer (`sc run`'s guard, the
operator) verifies idleness first: `ps -o etime=,stat= -p <pid>`, no child
processes doing work, then `kill <pid>`.

Launch claims (spec #76 H-25): lineage cannot answer for a generic headless
shell. It is detached from birth — no controlling TTY, launcher gone — so
'detached' describes how it was STARTED, not whether it is working, and between
relaunches no process holds the worktree at all. `run.record_launch` therefore
records the pid each headless boot is about to become (`shell_launch_records`),
and this scan tests THAT pid — identity by pid + start ticks, plus its worktree
hold — never its parentage. A claimed pid is never an orphan; `orphan` now means
a pid holding a worktree that no launch record claims, i.e. a true remnant. A
claim whose process is gone is `expected_absent`, which is not `available`:
the difference between "nobody launched anything here" and "a launch claimed
this shell and its worker is missing".

Non-Linux: /proc is absent; compute() returns supported=False. Fall back to
`lsof -a +D <worktree>` / `ps`. The substrate host is Linux.

Run standalone:
    python3 .super-coder/scripts/shell_liveness.py            # JSON
    python3 .super-coder/scripts/shell_liveness.py --text     # human table
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import instance_state

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
ADAPTERS = ENGINE / "adapters"
DB_PATH = instance_state.active_database_path(ENGINE)
PROC = Path("/proc")

_FALLBACK_BINS = {"claude", "codex", "opencode", "vibe", "kimi"}


def harness_binaries() -> set[str]:
    """The comm values a live harness of this fork can present, from
    adapters/*/adapter.json: `launch[0]`, `headless.launch[0]`, and the optional
    `comm_aliases` list.

    Why aliases: comm is the RUNTIME name, which a harness may set for itself
    (PR_SET_NAME) rather than inherit from its launch binary. `kimi` execs
    /usr/local/bin/kimi and then renames itself `kimi-code`, so matching launch
    names alone made a live kimi worker invisible — and an invisible worker
    projects as "available", the wrong failure direction for a badge whose job
    is to stop the operator double-booking a shell.

    /proc/<pid>/comm is truncated to 15 chars, so the expected set is truncated
    to match: a longer declared alias still compares against what the kernel
    actually reports. Duplicate aliases across adapters are harmless — this is a
    set. An adapter dir with no adapter.json contributes nothing, leaving the
    fallback set as the floor exactly as before."""
    bins: set[str] = set()
    if ADAPTERS.is_dir():
        for d in ADAPTERS.iterdir():
            cfg = d / "adapter.json"
            if not cfg.is_file():
                continue
            try:
                spec = json.loads(cfg.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            launches = [spec.get("launch") or [],
                        (spec.get("headless") or {}).get("launch") or []]
            for launch in launches:
                if launch:
                    bins.add(Path(launch[0]).name)
            for alias in spec.get("comm_aliases") or []:
                if alias:
                    bins.add(Path(alias).name)
    bins |= _FALLBACK_BINS
    return {b[:15] for b in bins}


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _stat_fields(pid: int) -> "list[str]":
    """Fields of /proc/<pid>/stat after comm. comm (field 2) may contain spaces
    and parens, so split after the final ')': state, ppid, ... follow."""
    data = _read(PROC / str(pid) / "stat")
    rp = data.rfind(")")
    if rp == -1:
        return []
    return data[rp + 2:].split()


def _ppid(pid: int) -> int | None:
    rest = _stat_fields(pid)
    try:
        return int(rest[1])              # rest[0]=state, rest[1]=ppid
    except (IndexError, ValueError):
        return None


def _is_zombie(pid: int) -> bool:
    """Has this process already exited, waiting only on a reaper?

    A zombie keeps its comm — so a finished `claude`/`kimi-code` worker still
    LOOKS like a harness — but it holds no worktree, runs no code, and its cwd
    link is empty. Counted, it lands in `indeterminate` and pins
    `safe_to_clean_all` False forever on the word of a process that ended. The
    unreadable-cwd honesty gap this scan does own is about LIVE processes; a
    zombie is not that case. Container PID 1 is often not a reaper (here it is
    the engine server), so these accumulate and never clear on their own."""
    rest = _stat_fields(pid)
    return bool(rest) and rest[0] == "Z"


def _tty_nr(pid: int) -> int | None:
    rest = _stat_fields(pid)
    try:
        return int(rest[4])              # rest[4]=tty_nr; 0 = no controlling TTY
    except (IndexError, ValueError):
        return None


def _start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat — when this process started. Pairs with the
    pid to form an identity a recycled pid number cannot inherit. None for a
    pid that is gone, which is the answer a stale claim needs."""
    rest = _stat_fields(pid)
    try:
        return int(rest[19])             # rest[0]=state (field 3) → rest[19]=field 22
    except (IndexError, ValueError):
        return None


def _tty_fd(pid: int) -> str | None:
    """The terminal device behind the process's stdio, from /proc/<pid>/fd/0..2 —
    the first fd whose link target is a tty device. A vanished pty keeps the
    link but readlink may append ' (deleted)'. None when no stdio fd is a tty
    (fully redirected) or the fds are unreadable (foreign user)."""
    for n in ("0", "1", "2"):
        try:
            target = os.readlink(PROC / str(pid) / "fd" / n)
        except OSError:
            continue
        base = target.removesuffix(" (deleted)")
        if base.startswith("/dev/pts/") or base.startswith("/dev/tty"):
            return target
    return None


def _tty_exists(pid: int, tty_fd: "str | None") -> "bool | None":
    """Does this process's terminal device still exist — asked in the process's
    OWN mount namespace, via /proc/<pid>/root<tty_fd>.

    Testing the bare path instead asks the SCANNER's namespace, which is a
    different question whenever the two disagree. A containerized session holds
    a pty from the container's devpts; scanned from the host, `/dev/pts/3` names the
    HOST's pts 3 — a device that is absent, or worse, belongs to some unrelated
    terminal. The live session then read as `tty-gone`, the rail projected
    `unreconciled` with a recovery affordance instead of `working`, and the
    scan's own advice invited killing work in progress — the exact inversion
    decision #45 exists to prevent.

    None when the answer cannot be obtained (no tty_fd, or /proc/<pid>/root is
    unreadable — a foreign user, or the process exited mid-scan). The caller
    turns that into no verdict: never call an orphan on missing data.

    Deliberately os.stat and not os.path.exists: exists() folds PermissionError
    into False, so an unreadable namespace would come back as "the pty is gone"
    — a confident orphan verdict derived from our own lack of access. Only a
    FileNotFoundError, reached THROUGH a root we could stat, means gone."""
    if not tty_fd:
        return None
    root = PROC / str(pid) / "root"
    try:
        os.stat(root)                    # can we see this pid's namespace at all?
    except OSError:
        return None                      # foreign user, or the process is gone
    try:
        os.stat(root / tty_fd.lstrip("/"))
    except FileNotFoundError:
        return False                     # the device really is not there
    except OSError:
        return None                      # unreadable → no verdict, not an orphan
    return True


def classify_orphan(tty_nr: "int | None", ppid: "int | None",
                    tty_fd: "str | None",
                    tty_exists: "bool | None" = None) -> "str | None":
    """Orphan verdict for one harness process — pure, injectable for tests.

    'tty-gone'  — has (had) a controlling TTY but the pty device is gone: the
                  terminal window closed under the session.
    'detached'  — no controlling TTY and reparented to init: whatever spawned
                  it (a headless boot's parent, a dead terminal's shell) is
                  gone. A NORMAL headless session still has a live parent, so
                  ppid==1 is the discriminator.
    None        — attached and normal, or not enough signal to say otherwise
                  (conservative: never call an orphan on missing data).

    `tty_exists` is supplied by the caller (see _tty_exists) because only the
    caller knows the pid, and the question is namespace-scoped to that pid.
    Unknown (None) is therefore a real answer here — no verdict — not an
    invitation to go and test the path in whatever namespace we happen to
    occupy, which is the bug this signature now forecloses.
    """
    if tty_nr is None:
        return None
    if tty_nr == 0:
        return "detached" if ppid == 1 else None
    if tty_fd is None:
        return None
    if tty_fd.endswith(" (deleted)"):
        return "tty-gone"
    if tty_exists is None:
        return None
    return None if tty_exists else "tty-gone"


def _orphan_verdict(pid: int) -> "str | None":
    """One process's orphan state, reading every input from /proc for that pid.
    The seam that keeps classify_orphan pure and namespace-blind."""
    tty_fd = _tty_fd(pid)
    return classify_orphan(_tty_nr(pid), _ppid(pid), tty_fd,
                           _tty_exists(pid, tty_fd))


def _self_harness_pid(harness_pids: set[int]) -> int | None:
    """Walk the PPID chain from this process up to the first harness ancestor —
    that is the admin's own session driving this scan."""
    pid: int | None = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen and pid > 1:
        seen.add(pid)
        if pid in harness_pids:
            return pid
        pid = _ppid(pid)
    return None


def _shell_labels() -> dict[str, dict]:
    """shortname.lower() → {shortname, flavor, display_name} from the DB, for
    friendlier output. Best-effort: missing/locked DB just means no labels."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return {}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT shortname, flavor, display_name FROM shells "
            "WHERE COALESCE(is_deleted,0)=0 AND shortname IS NOT NULL").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {r["shortname"].lower(): dict(r) for r in rows}


def _launch_claims() -> dict[str, dict]:
    """shortname.lower() → this shell's current launch claim (spec #76 H-25).

    `run.record_launch` writes one row per shell at every HEADLESS boot, naming
    the pid it is about to become. Best-effort like _shell_labels: no DB, a
    locked DB, or an un-migrated fork with no such table all mean "no claims",
    and the scan falls back to lineage exactly as it did before the record
    existed."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return {}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT s.shortname, r.pid, r.start_ticks, r.worktree, r.launched_at "
            "FROM shell_launch_records r JOIN shells s ON s.shell_id=r.shell_id "
            "WHERE s.shortname IS NOT NULL "
            "AND COALESCE(s.is_deleted,0)=0").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {r["shortname"].lower(): dict(r) for r in rows}


def claim_live(claim: dict) -> bool:
    """Is the process a launch claim names still the one that was launched, and
    does it still hold that shell's worktree?

    PARENTAGE IS NEVER ASKED — that is the whole of H-25. A headless shell is
    reparented to init the moment its launcher exits, so every
    lineage-shaped question ('detached', 'a NORMAL headless session still has a
    live parent') answers about the launch style rather than about the work.

    Identity is pid + start ticks, so a recycled pid number cannot inherit a
    dead worker's claim, and a zombie — which keeps both — is excluded before
    the question is asked, exactly as the main scan does.

    The worktree hold only REFUTES on a positive mismatch. An unreadable cwd is
    missing data (foreign user, or the process exiting mid-scan), and turning
    missing data into "the worker is gone" is the failure direction this
    requirement exists to remove."""
    pid = claim.get("pid")
    if not pid or _is_zombie(pid):
        return False
    if _start_ticks(pid) != claim.get("start_ticks"):
        return False
    worktree = claim.get("worktree")
    if not worktree:
        return True
    try:
        cwd = Path(os.readlink(PROC / str(pid) / "cwd")).resolve()
    except OSError:
        return True                      # unreadable → no refutation
    try:
        cwd.relative_to(Path(worktree).resolve())
    except ValueError:
        return False
    return True


def compute() -> dict:
    """Live shell-liveness snapshot. Pure read — never mutates."""
    if not PROC.is_dir():
        return {
            "supported": False,
            "note": "/proc unavailable (non-Linux) — fall back to "
                    "`lsof -a +D <worktree>` / `ps`.",
            "repo": {"name": REPO_ROOT.name, "root": str(REPO_ROOT)},
        }

    bins = harness_binaries()
    root = REPO_ROOT.resolve()
    labels = _shell_labels()
    claims = _launch_claims()
    live_claims = {name: c for name, c in claims.items() if claim_live(c)}

    # First pass: every harness process and its raw pid/comm (cwd resolved next).
    harness_pids: set[int] = set()
    raw: list[tuple[int, str]] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        comm = _read(entry / "comm").strip()
        if not comm or comm not in bins:
            continue
        pid = int(entry.name)
        if _is_zombie(pid):
            continue                 # already exited — holds no shell, no slot
        harness_pids.add(pid)
        raw.append((pid, comm))

    self_pid = _self_harness_pid(harness_pids)

    processes: list[dict] = []
    worktree_sessions: dict[str, list[int]] = {}
    indeterminate_pids: list[int] = []
    admin_root_pids: list[int] = []

    for pid, comm in raw:
        try:
            cwd = os.readlink(PROC / str(pid) / "cwd")        # absolute target
        except (PermissionError, FileNotFoundError, OSError):
            indeterminate_pids.append(pid)                    # foreign user / gone
            continue
        cwdp = Path(cwd).resolve()
        try:
            rel = cwdp.relative_to(root)                      # in THIS repo?
        except ValueError:
            continue                                          # another repo — ignore
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == ".sc-worktrees":
            shortname = parts[1]                              # worktree dir = shortname.lower()
            region = "worktree"
            worktree_sessions.setdefault(shortname, []).append(pid)
        else:
            shortname = None                                  # repo root (or a subdir of it)
            region = "root"
            admin_root_pids.append(pid)
        processes.append({
            "pid": pid,
            "comm": comm,
            "cwd": str(cwdp),
            "region": region,
            "shortname": shortname,
            "display_name": (labels.get(shortname or "", {}).get("display_name")),
            "is_self": pid == self_pid,
            # H-25: a pid a live launch record claims is not a remnant, whatever
            # its lineage says. `orphaned` keeps reporting the raw lineage read;
            # `claimed` is what every verdict below consults alongside it.
            "claimed": live_claims.get(shortname or "", {}).get("pid") == pid,
            "orphaned": (None if pid == self_pid
                         else _orphan_verdict(pid)),
        })

    active_other = sorted(worktree_sessions)
    indeterminate = len(indeterminate_pids)
    orphaned_pids = [p["pid"] for p in processes
                     if p["orphaned"] and not p["claimed"]]
    admin_present = self_pid is not None and self_pid in admin_root_pids
    admin_presence = "present" if admin_present else "indeterminate"
    return {
        "supported": True,
        "repo": {"name": REPO_ROOT.name, "root": str(REPO_ROOT)},
        "harness_binaries": sorted(bins),
        "self_pid": self_pid,
        "admin_presence": admin_presence,
        "processes": processes,
        "worktree_sessions": worktree_sessions,
        "active_other_shells": active_other,
        "admin_root_pids": admin_root_pids,
        "indeterminate": indeterminate,
        "indeterminate_pids": indeterminate_pids,
        "orphaned_pids": orphaned_pids,
        # The launch record's two answers (H-25), for consumers that must
        # distinguish "nobody launched anything here" from "a launch claimed
        # this shell and its process is gone".
        "claimed_pids": {name: c["pid"] for name, c in live_claims.items()},
        "claimed_absent": sorted(set(claims) - set(live_claims)),
        # The gate: Admin is positively present, no other shell is live, and
        # every harness cwd was readable.
        "safe_to_clean_all": admin_present and not active_other and indeterminate == 0,
    }


def is_active(shortname: str, snap: dict | None = None) -> bool:
    """Convenience for the admin gate: is THIS shell's worktree live right now?"""
    snap = snap or compute()
    return shortname.lower() in {s.lower() for s in snap.get("active_other_shells", [])}


def orphan_split(shortname: str, snap: dict) -> "tuple[list[int], list[int]]":
    """(all pids, orphaned pids) for one shell's worktree sessions — the shape
    the `sc run` guard needs: every-session-orphaned means the slot is held by
    survivors of closed terminals / dead parents, not by a working session.

    A CLAIMED pid is never in the orphan half (H-25). The consumer of this list
    is advice to kill, and a detached headless shell is the one process that
    reads as a remnant by lineage while being the healthiest thing on the box."""
    procs = [p for p in snap.get("processes", [])
             if (p.get("shortname") or "").lower() == shortname.lower()]
    return ([p["pid"] for p in procs],
            [p["pid"] for p in procs
             if p.get("orphaned") and not p.get("claimed")])


def session_state(shortname: str, snap: dict) -> "str | None":
    """One shell's slot verdict, the shape the picker annotation needs:
    'busy' (a working session holds the worktree), 'orphan' (EVERY session pid
    is an orphan — closed terminal / dead parent still holding the slot), or
    None (dormant, or liveness unsupported). A single live session among
    orphans wins: someone is working there → 'busy'."""
    if not snap.get("supported"):
        return None
    pids, orphans = orphan_split(shortname, snap)
    if not pids:
        return None
    return "orphan" if len(orphans) == len(pids) else "busy"


def record_state(shortname: str, snap: dict) -> "str | None":
    """The launch RECORD's verdict for one shell (spec #76 H-25) — asked only
    after session_state() found no process holding the worktree.

    'working'         — the claimed pid is live and still holds the worktree.
                        Reachable even when the process scan saw nothing: the
                        claim is comm-blind, so a harness that renames itself
                        out of the expected set stays visible.
    'expected_absent' — a launch claimed this shell and that process is gone.
                        The distinction the rail could not previously draw:
                        between headless launches a shell held no
                        process at all and read as a bare 'available'.
    None              — no claim was ever made, or liveness is unsupported."""
    if not snap.get("supported"):
        return None
    name = shortname.lower()
    if name in snap.get("claimed_pids", {}):
        return "working"
    if name in snap.get("claimed_absent", []):
        return "expected_absent"
    return None


def _print_text(d: dict) -> None:
    if not d.get("supported"):
        print(f"{d['repo']['name']}: liveness unsupported — {d.get('note','')}")
        return
    print(f"{d['repo']['name']}   harnesses={','.join(d['harness_binaries'])}"
          f"   self_pid={d['self_pid']}"
          f"   admin_presence={d['admin_presence']}")
    print("\nLIVE HARNESS SESSIONS")
    if not d["processes"]:
        print("  (none — no harness cwd'd inside this repo)")
    for p in d["processes"]:
        who = (f"{p['display_name']} ({p['shortname']})" if p["shortname"]
               else "admin / repo root")
        tags = []
        if p["is_self"]:
            tags.append("SELF")
        if p.get("claimed"):
            tags.append("CLAIMED")
        if p["orphaned"] and not p.get("claimed"):
            tags.append(f"ORPHAN:{p['orphaned']}")
        tag = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  pid {p['pid']:<7} {p['comm']:<9} {p['region']:<9} {who}{tag}")
        print(f"            {p['cwd']}")
    if d["indeterminate"]:
        print(f"\n⚠ {d['indeterminate']} harness process(es) with unreadable cwd "
              f"(other OS user?): pids {d['indeterminate_pids']} — liveness "
              f"INDETERMINATE; do not assume all-clear.")
    if d.get("orphaned_pids"):
        print(f"\n⚠ {len(d['orphaned_pids'])} orphaned session(s): pids "
              f"{d['orphaned_pids']} — terminal closed / parent gone. Each "
              f"holds its shell's one-session slot. Verify idle "
              f"(`ps -o etime=,stat= -p <pid>`; no busy children), then "
              f"`kill <pid>`. An orphan can still be mid-work — never kill "
              f"unverified.")
    if d.get("claimed_absent"):
        print(f"\n⚠ {len(d['claimed_absent'])} shell(s) EXPECTED BUT ABSENT: "
              f"{', '.join(d['claimed_absent'])} — a headless launch claimed "
              f"each one and that process is gone. Not a remnant to kill; the "
              f"opposite. Whether an absence is a fault belongs to the "
              f"caller, never this scan.")
    print("\nVERDICT")
    if d["active_other_shells"]:
        print(f"  Live OTHER shells: {', '.join(d['active_other_shells'])}"
              f"  → surface those worktrees; do NOT act on them.")
        if not d["indeterminate"]:
            print("  All other worktrees are dormant → safe to clean.")
    elif d["admin_presence"] != "present":
        print("  Admin presence is indeterminate — current harness identity "
              "was not positively matched to the repo root; cleanup remains unsafe.")
    elif d["indeterminate"]:
        print("  No live other shells seen, but indeterminate>0 → surface, "
              "do not assume safe.")
    else:
        print("  Admin is the only live shell → safe to clean ALL worktrees.")


def main(argv: list[str]) -> int:
    d = compute()
    if "--text" in argv:
        _print_text(d)
    else:
        print(json.dumps(d, indent=2))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
