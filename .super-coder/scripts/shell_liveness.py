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
fork's harness binaries (adapters/*/adapter.json `launch[0]`) and whose cwd is
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
`indeterminate > 0` the admin must NOT assume all-clear — surface instead.

Orphans: closing a terminal window does not reliably kill the harness on every
host — the session survives, holds its shell's one-session slot, and blocks
every headless boot of that shell until someone kills it by hand. Each process
is therefore classified (`orphaned`): 'tty-gone' (had a controlling terminal;
the pty vanished — the window closed under it), 'detached' (no controlling
TTY and reparented to init — its spawning session is gone), or None (normal).
Classification is reporting only — an orphan may still be mid-work (a merge,
a suite), so nothing here kills anything. The consumer (`sc run`'s guard, the
operator) verifies idleness first: `ps -o etime=,stat= -p <pid>`, no child
processes doing work, then `kill <pid>`.

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

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
ADAPTERS = ENGINE / "adapters"
DB_PATH = ENGINE / "shell_db.db"
PROC = Path("/proc")

_FALLBACK_BINS = {"claude", "codex", "opencode", "vibe", "kimi"}


def harness_binaries() -> set[str]:
    """The fork's harness launch binaries, from adapters/*/adapter.json `launch[0]`.
    /proc/<pid>/comm is truncated to 15 chars — harness names are short, but we
    truncate the expected set to match so a long name would still compare."""
    bins: set[str] = set()
    if ADAPTERS.is_dir():
        for d in ADAPTERS.iterdir():
            cfg = d / "adapter.json"
            if not cfg.is_file():
                continue
            try:
                launch = json.loads(cfg.read_text()).get("launch") or []
            except (json.JSONDecodeError, OSError):
                continue
            if launch:
                bins.add(Path(launch[0]).name)
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


def _tty_nr(pid: int) -> int | None:
    rest = _stat_fields(pid)
    try:
        return int(rest[4])              # rest[4]=tty_nr; 0 = no controlling TTY
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
        tty_exists = os.path.exists(tty_fd)
    return None if tty_exists else "tty-gone"


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

    # First pass: every harness process and its raw pid/comm (cwd resolved next).
    harness_pids: set[int] = set()
    raw: list[tuple[int, str]] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        comm = _read(entry / "comm").strip()
        if comm and comm in bins:
            pid = int(entry.name)
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
            "orphaned": (None if pid == self_pid
                         else classify_orphan(_tty_nr(pid), _ppid(pid), _tty_fd(pid))),
        })

    active_other = sorted(worktree_sessions)
    indeterminate = len(indeterminate_pids)
    orphaned_pids = [p["pid"] for p in processes if p["orphaned"]]
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
    survivors of closed terminals / dead parents, not by a working session."""
    procs = [p for p in snap.get("processes", [])
             if (p.get("shortname") or "").lower() == shortname.lower()]
    return ([p["pid"] for p in procs],
            [p["pid"] for p in procs if p.get("orphaned")])


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
        if p["orphaned"]:
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
    raise SystemExit(main(sys.argv[1:]))
