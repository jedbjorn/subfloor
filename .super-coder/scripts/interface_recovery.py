#!/usr/bin/env python3
"""Unified stranded-shell recovery (spec #30 req 24 / task #95).

One API-owned preview/execute workflow shared by browser and CLI:

- **Preview** gathers the durable + process evidence for one shell, derives
  ONE server-side classification (available / stale durable lock / exact idle
  orphan / verified live / indeterminate) and the legal actions, and stores
  them as an opaque observation row fingerprinted against that evidence.
  The client never infers safety from raw fields.
- **Execute** requires a fresh observation. The evidence is re-gathered as the
  LAST precondition — after the legality, confirmation and unpushed gates, and
  immediately before anything is signalled or closed — and fingerprinted
  against the preview: durable state (a concurrent recovery, a new
  generation, an archive hand-off) AND the volatile safety evidence the
  operator actually saw — exact process identity + liveness, pane/tmux
  membership, and the worktree's dirty/untracked/unpushed facts. Any
  difference refuses with 409 recovery_observation_stale before a signal is
  sent, a row is closed, or a file is touched. Process identity is then
  re-verified once more at signal time (PID + /proc start ticks) — a PID
  reuse or unreadable /proc at that instant performs no signal and returns
  an indeterminate result. A confirmed discard re-reads the worktree ONE more
  time immediately before `reset --hard`/`clean -fd`, because a shell can
  write while it shuts down, i.e. after that fence has already passed; that
  refusal deletes nothing but cannot unwind the signal or the closure. See
  `_assert_worktree_unchanged` for the windows that remain open — a worktree
  is not transactional and no claim of atomicity is made here.

Signaling discipline (spec Shell Recovery): SIGTERM to the exact verified
process group, the bounded existing grace period, SIGKILL only while the
same PID/start ticks still identify the process. Never a broad match.

Closure discipline: on proven absence ONE transaction ends the Interface
session + generation (via interface_broker.close_session), closes the
matching archive, clears shells.active_archive_id only while it still
points there, resolves session alerts, and releases only generation-bound
sprint bindings (unambiguous ownership); ambiguous wake/binding state is
parked with a named next action. Unread inbox messages stay unread.

Worktree discipline: files are preserved by default. discard_worktree is an
independently confirmed escalation (typed shell shortname) that refuses
when unpushed commits exist and never deletes the worktree or branch.

Evidence discipline, both directions:
- the freshness digest binds every attribute a discard REWRITES — content,
  type, symlink target, permissions, ownership, timestamps — so that any
  post-preview change to state the operator confirmed erasing moves it;
- an observation that cannot be gathered WHOLE refuses. Git facts are a
  complete observation, an explicit gap, or "there is no repository here";
  a gap never degrades to absent facts, because a gap is deterministic — the
  same undecodable output or unreadable entry at preview and at execute would
  fingerprint EQUAL and let a discard run as though nothing had changed.
  Absence of evidence is not evidence of safety.

This module is stdlib-only: recovery must work HTTP-only, without the
websockets-dependent Interface runtime (spec Restricted Admin).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess
import time

import interface_broker
from interface_runtime import GRACEFUL_TERMINATE_S

OBSERVATION_TTL_S = 120

CLASSIFICATIONS = ("available", "stale_durable_lock", "exact_idle_orphan",
                   "verified_live", "indeterminate")


class RecoveryError(Exception):
    """A refusal the routes layer maps straight to an HTTP error."""

    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


# ------------------------------------------------------------------ processes

def _read_stat(pid: int) -> tuple[int, str]:
    """(start_ticks, state) from /proc/<pid>/stat. FileNotFoundError means
    the pid is gone; PermissionError etc. mean present-but-unreadable —
    callers distinguish 'dead' from 'unknown'."""
    with open(f"/proc/{pid}/stat") as fh:
        text = fh.read()
    rest = text[text.rindex(")") + 2:]
    fields = rest.split()
    return int(fields[19]), fields[0]  # field 22 starttime, field 3 state


def _proc_state(pid: int, start_ticks: int) -> str:
    """Exact-identity liveness: 'alive' (pid present, ticks match, not a
    zombie), 'dead' (pid gone, recycled, or reaped), 'unreadable' (present
    but /proc refuses us — fail closed, never 'dead')."""
    try:
        ticks, state = _read_stat(pid)
    except FileNotFoundError:
        return "dead"
    except (PermissionError, ProcessLookupError):
        return "unreadable"
    except OSError:
        return "unreadable"
    if ticks != start_ticks:
        return "dead"  # recycled pid: a different process, not ours
    if state == "Z":
        return "dead"
    return "alive"


def _pane_present(sock: str | None, pane_id: str) -> bool | None:
    """Is the exact pane in the session's own tmux server? Membership is
    answered by list-panes, so a server that answers proves BOTH ways:
    False = the pane is gone (reachable classification), True = it lives.
    None ONLY when tmux can't answer (binary missing, socket unreachable,
    garbled output) — unknown is not gone."""
    if not sock:
        return None
    try:
        out = subprocess.run(
            ["tmux", "-S", sock, "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=10, check=False)
    except Exception:  # noqa: BLE001 — any tmux failure means "unknown"
        return None
    if out.returncode != 0:
        return None  # server unreachable — unknown, NOT proof of absence
    return any(line.strip() == pane_id for line in out.stdout.splitlines())


def _wait_dead(pid: int, start_ticks: int, grace_s: float) -> str:
    """Poll exact-identity liveness for up to grace_s. Returns 'dead' the
    moment /proc proves absence; otherwise the last observed state at the
    deadline ('alive' or 'unreadable') — NEITHER is proof of absence, so
    neither may satisfy closure."""
    deadline = time.monotonic() + grace_s
    state = _proc_state(pid, start_ticks)
    while state != "dead" and time.monotonic() < deadline:
        time.sleep(0.1)
        state = _proc_state(pid, start_ticks)
    return state


def terminate_process_group(pid: int, start_ticks: int,
                            grace_s: float = GRACEFUL_TERMINATE_S) -> dict:
    """SIGTERM the exact verified process group, bounded grace, SIGKILL only
    while the same PID/start ticks still identify the process. Identity
    mismatch or unreadable state performs NO signal and reports
    indeterminate — the caller maps that to a refusal, never a closure.
    `dead` is True ONLY on /proc-proven absence: a signal is not proof of
    death, and 'unreadable' or a SIGKILL survivor (D-state) leaves the
    caller to refuse closure with a named next action."""
    state = _proc_state(pid, start_ticks)
    if state != "alive":
        return {"signaled": False, "dead": False, "reason": "indeterminate",
                "detail": f"process state {state} at signal time"}
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return {"signaled": False, "dead": False, "reason": "indeterminate",
                "detail": "process group unreadable at signal time"}
    os.killpg(pgid, signal.SIGTERM)
    state = _wait_dead(pid, start_ticks, grace_s)
    if state == "dead":
        return {"signaled": True, "dead": True, "escalated": False,
                "pid": pid, "pgid": pgid}
    if state == "unreadable":
        return {"signaled": True, "dead": False, "escalated": False,
                "pid": pid, "pgid": pgid, "reason": "absence_unproven",
                "detail": "SIGTERM sent but /proc turned unreadable during "
                          "the grace — absence not proven"}
    # Grace expired with the process alive. Re-verify the EXACT identity
    # before SIGKILL — the window is long enough for exit + PID reuse, and
    # the rule is never signal an uncertain process.
    state = _proc_state(pid, start_ticks)
    if state != "alive":
        return {"signaled": True, "dead": state == "dead",
                "escalated": False, "pid": pid, "pgid": pgid,
                "note": "identity changed during grace — no SIGKILL sent"}
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return {"signaled": True, "dead": False, "escalated": False,
                "pid": pid, "pgid": pgid,
                "note": "process exited during grace — no SIGKILL sent"}
    os.killpg(pgid, signal.SIGKILL)
    state = _wait_dead(pid, start_ticks, grace_s)
    if state == "dead":
        return {"signaled": True, "dead": True, "escalated": True,
                "pid": pid, "pgid": pgid}
    return {"signaled": True, "dead": False, "escalated": True,
            "pid": pid, "pgid": pgid, "reason": "absence_unproven",
            "detail": f"process state {state} after SIGKILL — absence not "
                      "proven (an unkillable D-state process survives)"}


# ------------------------------------------------------------------ git facts

class _GitEvidenceUnavailable(Exception):
    """A repository is there but its evidence could not be gathered whole.

    Distinct from "there is no repository": one is a gap, the other is a
    complete observation. A gap must never reach the fence as absent facts —
    absence of evidence is not evidence of safety.
    """


def _git_out(worktree: str, *args, timeout: int = 15) -> str:
    """Git stdout, decoded losslessly; any failure raises.

    surrogateescape, NEVER strict: a valid non-UTF-8 filename is real working
    -tree state, and decoding it strictly raises — which is exactly how this
    guard used to collapse to "no facts" (SC-087). Surrogates keep such names
    distinct in the digest and hand os.* calls back the original bytes.
    Non-zero exit, timeout and spawn failure all become a refusal, never a
    partial answer.
    """
    try:
        out = subprocess.run(["git", "-C", worktree, *args],
                             capture_output=True, timeout=timeout,
                             check=False)
    except Exception as exc:  # spawn failure / timeout: a gap, not a fact
        raise _GitEvidenceUnavailable(
            f"git {args[0]}: {type(exc).__name__}") from exc
    if out.returncode != 0:
        raise _GitEvidenceUnavailable(f"git {args[0]}: exit {out.returncode}")
    return out.stdout.decode("utf-8", "surrogateescape")


def _head_exists(worktree: str) -> bool:
    """True when HEAD resolves; False for an unborn HEAD — a repo with no
    commits is a COMPLETE observation (nothing committed to diff against,
    nothing that can be unpushed), not a gap. Any other exit is a gap."""
    try:
        out = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True, timeout=15, check=False)
    except Exception as exc:  # spawn failure / timeout: a gap, not a fact
        raise _GitEvidenceUnavailable(
            f"git rev-parse: {type(exc).__name__}") from exc
    if out.returncode == 0:
        return True
    if out.returncode == 1 and not out.stdout.strip():
        return False
    raise _GitEvidenceUnavailable(f"git rev-parse: exit {out.returncode}")


def _path_identity(path: str) -> str:
    """Identity of one working-tree path: its TYPE and lstat metadata first,
    then what that type carries. Classified with lstat — NO-FOLLOW, always.

    The metadata prefix binds every attribute a discard REWRITES: the type,
    the FULL permission bits, owner and group, size, and the mtime/ctime
    `git checkout` replaces. `reset --hard` does not restore a dirty file in
    place — it recreates it from the index, so its mode comes back as
    umask-derived 0644/0666 and its owner as the recovering process's
    (reproduced: 0640 -> reset -> 0666). Permissions are work; discard
    destroys them; the digest therefore binds them.

    On top of the prefix:
    - regular file -> its bytes (never size or mtime alone: a same-size
      overwrite must move the hash).
    - symlink -> the readlink TARGET STRING itself. Never the bytes behind
      it: resolving would miss a retarget onto a byte-identical file, and it
      would let the digest wander outside the worktree entirely. Link and
      target are distinct entities and stay distinguished.
    - directory / fifo / socket / device -> the prefix alone.
    - genuinely absent (ENOENT/ENOTDIR) -> a marker; that IS the state.
    - unreadable for any other reason -> a GAP: raise, never a marker. A
      marker is deterministic, so it would read equal at preview and execute
      and let a discard erase whatever changed behind it.

    Only st_atime is excluded, and by reproduction rather than by argument:
    this function lstats a path and then READS it, and on a file the shell
    just wrote (every dirty file) that read moves atime — measured moving
    ~1ms under relatime. The value recorded is the one observed BEFORE our
    own read, so binding it would make the preview stale against itself and
    refuse every discard forever.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        # Paths and contents never enter the payload — errno only.
        raise _GitEvidenceUnavailable(f"lstat: errno {exc.errno}") from exc
    mode = st.st_mode
    meta = (f"{stat.S_IFMT(mode):o}:{stat.S_IMODE(mode):04o}:{st.st_uid}:"
            f"{st.st_gid}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}")
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise _GitEvidenceUnavailable(
                f"readlink: errno {exc.errno}") from exc
        return f"link:{meta}:" + hashlib.sha256(
            target.encode("utf-8", "surrogateescape")).hexdigest()
    if stat.S_ISDIR(mode):
        return f"dir:{meta}"
    if not stat.S_ISREG(mode):
        return f"special:{meta}"
    h = hashlib.sha256()
    try:
        # O_NOFOLLOW: the path was a regular file at lstat; if it became a
        # symlink in between, refuse to read through it rather than hash
        # whatever it now points at.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            while chunk := os.read(fd, 1 << 16):
                h.update(chunk)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _GitEvidenceUnavailable(f"read: errno {exc.errno}") from exc
    return f"file:{meta}:{h.hexdigest()}"


def _change_digest(worktree: str, porcelain: list[str], head: bool) -> str:
    """Fingerprint of what a discard would erase.

    INVARIANT: this digest MUST change if ANY safety-relevant aspect of the
    worktree state a discard would destroy has changed since the preview.
    Porcelain lines are far coarser than that — they stay byte-identical
    while the work underneath them is rewritten, retargeted, re-permissioned
    or changes type — so the line set is only the outer layer. Held against
    the invariant, the state a discard destroys is: the SET of affected paths
    (the lines), and for each path its ENTITY IDENTITY (`_path_identity`) —
    type, permissions, ownership, timestamps, and regular-file bytes or
    symlink target.

    The path set is what `reset --hard` + `clean -fd` would act on: the paths
    differing from HEAD, plus untracked files, plus untracked DIRECTORIES —
    `clean -fd` removes an empty untracked directory that `ls-files -o` (a
    file listing) never names. Ignored files stay out: `clean -fd` without
    -x does not touch them, so they are not state the confirmation is about.
    """
    def paths(*args) -> list[str]:
        # -z: paths verbatim, no C-quoting to unescape.
        return [p for p in _git_out(worktree, *args, timeout=30).split("\0")
                if p]

    h = hashlib.sha256()
    for line in sorted(porcelain):
        h.update(line.encode("utf-8", "surrogateescape") + b"\n")
    # ls-files -o: untracked FILES individually, never collapsed to a dir;
    # --directory: the untracked DIRECTORIES themselves, empty ones included.
    rels = set(paths("ls-files", "-o", "--exclude-standard", "-z")) \
        | set(paths("ls-files", "-o", "--directory", "--exclude-standard",
                    "-z"))
    if head:
        # staged + unstaged + deletions, one row per path.
        rels |= set(paths("diff", "HEAD", "--name-only", "-z"))
    for rel in sorted(rels):
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0"
                 + _path_identity(os.path.join(worktree, rel)).encode()
                 + b"\0")
    return h.hexdigest()


def _git_facts(worktree: str | None) -> dict | None:
    """Worktree facts for the freshness fence. Three outcomes, kept apart on
    purpose:

    - `None` — there is no repository to observe (no worktree, or no `.git`).
      Complete evidence: there is no git-managed state here to erase.
    - a facts dict — the complete observation.
    - `{"indeterminate": <reason>}` — a repository IS there and its evidence
      could not be gathered whole. NOT "no facts": execute refuses on it, so
      an unobservable worktree can never read as a safe one (SC-087).
    """
    if not worktree:
        return None
    dotgit = os.path.join(worktree, ".git")
    if not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
        return None
    try:
        head = _head_exists(worktree)
        branch = _git_out(worktree, "rev-parse", "--abbrev-ref", "HEAD") \
            if head else _git_out(worktree, "branch", "--show-current")
        porcelain = _git_out(worktree, "status", "--porcelain").splitlines()
        untracked = sum(1 for ln in porcelain if ln.startswith("??"))
        dirty = len(porcelain) - untracked
        unpushed = int(_git_out(worktree, "rev-list", "HEAD", "--not",
                                "--remotes", "--count").strip() or 0) \
            if head else 0
        return {"worktree": worktree, "branch": branch.strip(),
                "dirty_tracked": dirty, "untracked": untracked,
                "unpushed_commits": unpushed,
                # WHICH paths changed and WHAT each one now IS — not just how
                # many: equal-count churn (one file cleaned while another is
                # dirtied), a rewrite of an already-listed path, a symlink
                # retarget, a chmod, and a type transition all move the
                # freshness fingerprint. Paths and contents stay out of the
                # payload.
                "change_digest": _change_digest(worktree, porcelain, head)}
    except (_GitEvidenceUnavailable, ValueError) as exc:
        return {"indeterminate": str(exc)}


def _unpushed_count(worktree: str) -> int:
    """The discard gate — exact and fail-closed: any error is a refusal,
    never an assumption of clean.

    An unborn HEAD is NOT an error, for the same reason `_git_facts` reads it
    as complete evidence: a repo with no commits has nothing that can be
    unpushed. Reading it as a failure here made the two disagree — the preview
    showed `0 unpushed` and this gate refused the discard it had just
    authorised.
    """
    def refuse(detail: str):
        return RecoveryError(
            409, "worktree_state_unknown",
            f"cannot enumerate unpushed commits in {worktree} — discard "
            "refused (fail closed)", {"stderr": detail[-200:]})

    try:
        if not _head_exists(worktree):
            return 0
    except _GitEvidenceUnavailable as exc:
        raise refuse(str(exc)) from exc
    try:
        out = subprocess.run(
            ["git", "-C", worktree, "rev-list", "HEAD", "--not", "--remotes",
             "--count"], capture_output=True, timeout=15, check=False)
    except Exception as exc:  # timeout / spawn failure: refuse, never guess
        raise refuse(f"{type(exc).__name__}: {exc}") from exc
    if out.returncode != 0:
        raise refuse(out.stderr.decode("utf-8", "replace").strip())
    try:
        return int(out.stdout.decode("utf-8", "replace").strip() or 0)
    except ValueError as exc:
        raise refuse("rev-list --count gave a non-numeric answer") from exc


def _discard_worktree_files(worktree: str) -> dict:
    """Remove tracked + untracked file changes in the exact worktree. Never
    deletes the worktree, its branch, or ignored files. Runs AFTER the
    durable closure is committed, so a git failure here must never escape
    as a 500 that hides what happened: each step's outcome is recorded and
    a failure returns exactly what completed and where it stopped."""
    result: dict = {"worktree": worktree, "discarded": False,
                    "completed": [], "failed": None}
    for step, args in (("reset", ["reset", "--hard", "HEAD"]),
                       ("clean", ["clean", "-fd"])):
        try:
            out = subprocess.run(["git", "-C", worktree, *args],
                                 capture_output=True, text=True, timeout=60,
                                 check=False)
        except Exception as exc:  # noqa: BLE001 — timeout etc: report it
            result["failed"] = {"step": step, "error": str(exc)[:200]}
            return result
        if out.returncode != 0:
            result["failed"] = {"step": step,
                                "error": out.stderr.strip()[-200:]}
            return result
        result["completed"].append(step)
    result["discarded"] = True
    return result


# ------------------------------------------------------------------ evidence

def _shell(con, shell_id: int):
    row = con.execute(
        "SELECT shell_id, shortname, active_archive_id, is_deleted "
        "FROM shells WHERE shell_id=?", (shell_id,)).fetchone()
    if row is None:
        raise RecoveryError(404, "no_such_shell",
                            f"shell {shell_id} not found")
    return row


def _live_session(con, shell_id: int):
    return con.execute(
        "SELECT session_id, generation, occupancy, lifecycle, harness, "
        " worktree, archive_id, tmux_socket, tmux_session, tmux_window, "
        " tmux_pane_id, pane_pid, pane_start_ticks, created_at "
        "FROM interface_sessions "
        "WHERE shell_id=? AND occupancy <> 'ended' "
        "ORDER BY session_id DESC LIMIT 1", (shell_id,)).fetchone()


def _last_session(con, shell_id: int):
    return con.execute(
        "SELECT session_id, generation, occupancy, lifecycle, harness, "
        " worktree, archive_id, tmux_socket, tmux_session, tmux_window, "
        " tmux_pane_id, pane_pid, pane_start_ticks, created_at "
        "FROM interface_sessions WHERE shell_id=? "
        "ORDER BY session_id DESC LIMIT 1", (shell_id,)).fetchone()


_SESSION_COLS = ("session_id", "generation", "occupancy", "lifecycle",
                 "harness", "worktree", "archive_id", "tmux_socket",
                 "tmux_session", "tmux_window", "tmux_pane_id", "pane_pid",
                 "pane_start_ticks", "created_at")


def gather(con, shell_id: int, default_worktree: str | None) -> dict:
    """Assemble the full evidence picture. Pure read — never mutates, never
    signals. Secrets and terminal content are never included."""
    shell_id, shortname, active_archive_id, _deleted = _shell(con, shell_id)
    live = _live_session(con, shell_id)
    sess_row = live or _last_session(con, shell_id)
    sess = dict(zip(_SESSION_COLS, sess_row)) if sess_row else None

    process: dict = {"pane_id": None, "pane_pid": None,
                     "pane_start_ticks": None, "pane_present": None,
                     "pid_state": "none", "pgid": None}
    if sess and sess["pane_pid"] is not None \
            and sess["pane_start_ticks"] is not None:
        pid, ticks = sess["pane_pid"], sess["pane_start_ticks"]
        process.update({
            "pane_id": sess["tmux_pane_id"], "pane_pid": pid,
            "pane_start_ticks": ticks,
            "pid_state": _proc_state(pid, ticks)})
        if sess["tmux_pane_id"]:
            process["pane_present"] = _pane_present(sess["tmux_socket"],
                                                    sess["tmux_pane_id"])
        if process["pid_state"] == "alive":
            try:
                process["pgid"] = os.getpgid(pid)
            except OSError:
                process["pgid"] = None

    generation = None
    if sess:
        grow = con.execute(
            "SELECT generation, ended_at, last_hook_seq "
            "FROM interface_generations WHERE shell_id=? AND generation=?",
            (shell_id, sess["generation"])).fetchone()
        if grow:
            generation = {"generation": grow[0], "ended_at": grow[1],
                          "last_hook_seq": grow[2]}

    archive = None
    archive_id = (sess or {}).get("archive_id") or active_archive_id
    if archive_id is not None:
        arow = con.execute(
            "SELECT archive_id, ended_at FROM shell_memory_archives "
            "WHERE archive_id=?", (archive_id,)).fetchone()
        if arow:
            archive = {"archive_id": arow[0], "ended_at": arow[1],
                       "active": active_archive_id == arow[0]}

    binding = None
    if sess:
        brow = con.execute(
            "SELECT binding_id, sprint_doc_id FROM sprint_planner_bindings "
            "WHERE shell_id=? AND generation=? AND released_at IS NULL",
            (shell_id, sess["generation"])).fetchone()
        if brow:
            binding = {"binding_id": brow[0], "sprint_doc_id": brow[1]}

    unread = con.execute(
        "SELECT COUNT(*) FROM shell_messages "
        "WHERE to_shell_id=? AND read_at IS NULL", (shell_id,)).fetchone()[0]

    worktree = (sess or {}).get("worktree") or default_worktree
    evidence = {
        "shell": {"shell_id": shell_id, "shortname": shortname,
                  "active_archive_id": active_archive_id},
        "session": ({k: sess[k] for k in
                     ("session_id", "generation", "occupancy", "lifecycle",
                      "harness", "worktree", "archive_id", "created_at")}
                    if sess else None),
        "generation": generation,
        "archive": archive,
        "sprint_binding": binding,
        "process": process,
        "tmux": ({"socket": sess["tmux_socket"],
                  "session": sess["tmux_session"],
                  "window": sess["tmux_window"],
                  "pane_id": sess["tmux_pane_id"]} if sess else None),
        "unread_messages": unread,
        "git": _git_facts(worktree),
    }
    evidence["live_session"] = live is not None
    return evidence


def classify(evidence: dict) -> tuple[str, list[str]]:
    """The ONE server-side verdict. Clients render it; they never derive
    their own."""
    proc = evidence["process"]
    pid_state = proc["pid_state"]
    pane = proc["pane_present"]

    if evidence["live_session"]:
        if pid_state == "none":
            # No process identity was ever recorded (a reservation that
            # never spawned, or a legacy row): nothing live to disprove the
            # lock — safe to close.
            return "stale_durable_lock", ["recover"]
        if pid_state == "unreadable" or pane is None:
            return "indeterminate", []
        if pane and pid_state == "alive":
            return "verified_live", ["force"]
        if not pane and pid_state == "alive":
            # The pane is gone from our tmux server but the exact process
            # lives on — a leaked orphan, exactly identified.
            return "exact_idle_orphan", ["recover"]
        if not pane and pid_state == "dead":
            return "stale_durable_lock", ["recover"]
        # pane present but its pid/ticks no longer match the record —
        # something else owns that pane now.
        return "indeterminate", []

    # No live session: a residual exact process from the last generation is
    # an orphan; an open active archive is a stale lock; otherwise the shell
    # is simply available.
    if pid_state == "alive":
        return "exact_idle_orphan", ["recover"]
    if pid_state == "unreadable":
        return "indeterminate", []
    archive = evidence["archive"]
    if archive and archive["active"] and archive["ended_at"] is None:
        return "stale_durable_lock", ["recover"]
    return "available", []


def evidence_projection(evidence: dict, classification: str,
                        legal_actions: list[str]) -> list[dict[str, str]]:
    """Canonical client-visible recovery evidence.

    Browser and CLI render these exact rows.  Keeping the field selection and
    absence wording here prevents either client from presenting a safer-looking
    subset than the other for the same observation.
    """
    shell = evidence.get("shell") or {}
    session = evidence.get("session")
    generation = evidence.get("generation")
    archive = evidence.get("archive")
    binding = evidence.get("sprint_binding")
    process = evidence.get("process") or {}
    tmux = evidence.get("tmux")
    git = evidence.get("git")

    shortname = shell.get("shortname") or "unknown"
    shell_id = shell.get("shell_id")
    shell_value = f"{shortname} · id {shell_id if shell_id is not None else '—'}"

    if session:
        session_value = (
            f"session #{session.get('session_id', '—')} · generation "
            f"{session.get('generation', '—')} · "
            f"{session.get('occupancy', '—')}/{session.get('lifecycle', '—')}"
            f" · harness {session.get('harness') or '—'}")
    else:
        session_value = "no Interface session"

    if generation:
        ended = generation.get("ended_at")
        generation_value = (
            f"generation {generation.get('generation', '—')} · "
            f"{'open' if ended is None else f'ended {ended}'} · "
            f"last hook {generation.get('last_hook_seq', '—')}")
    else:
        generation_value = "no generation record"

    if archive:
        ended = archive.get("ended_at")
        archive_value = (
            f"archive #{archive.get('archive_id', '—')} · "
            f"{'open' if ended is None else f'closed {ended}'}"
            f"{' · active' if archive.get('active') else ''}")
    else:
        archive_value = "no archive relation"

    if binding:
        binding_value = (
            f"binding #{binding.get('binding_id', '—')} · sprint doc "
            f"#{binding.get('sprint_doc_id', '—')}")
    else:
        binding_value = "no armed sprint binding"

    if process.get("pane_pid") is None or \
            process.get("pane_start_ticks") is None:
        process_value = "no recorded process identity"
    else:
        presence = process.get("pane_present")
        presence_value = "presence unknown" if presence is None else \
            ("present" if presence else "gone")
        process_value = (
            f"PID {process['pane_pid']} · start ticks "
            f"{process['pane_start_ticks']} · PGID "
            f"{process.get('pgid') if process.get('pgid') is not None else '—'}"
            f" · {process.get('pid_state') or 'unknown'} · pane "
            f"{process.get('pane_id') or '—'} ({presence_value})")

    if tmux:
        tmux_value = (
            f"socket {tmux.get('socket') or '—'} · "
            f"session {tmux.get('session') or '—'} · "
            f"window {tmux.get('window') or '—'} · "
            f"pane {tmux.get('pane_id') or '—'}")
    else:
        tmux_value = "no tmux relation"

    unread = evidence.get("unread_messages")
    unread_value = (
        f"{unread} · left unread" if isinstance(unread, int)
        else "unknown · left unread")

    if git and git.get("indeterminate"):
        worktree_value = (
            f"state could not be observed completely ({git['indeterminate']})"
            " · recovery refused until it can be")
    elif git:
        tracked = git.get("dirty_tracked")
        untracked = git.get("untracked")
        if isinstance(tracked, int) and isinstance(untracked, int):
            cleanliness = "clean" if tracked == 0 and untracked == 0 \
                else "not clean"
            worktree_value = (
                f"{cleanliness} · {tracked} tracked · {untracked} untracked · "
                f"{git.get('unpushed_commits', '—')} unpushed commit(s) · "
                f"branch {git.get('branch') or '—'} · "
                f"{git.get('worktree') or 'worktree path unavailable'}")
        else:
            worktree_value = (
                f"unknown cleanliness · branch {git.get('branch') or '—'} · "
                f"{git.get('worktree') or 'worktree path unavailable'}")
    else:
        worktree_value = "worktree facts unavailable"

    values = (
        ("shell", "shell", shell_value),
        ("classification", "classification", classification),
        ("legal_actions", "legal actions",
         ", ".join(legal_actions) if legal_actions else "none"),
        ("session", "session", session_value),
        ("generation", "generation", generation_value),
        ("archive", "archive", archive_value),
        ("sprint_binding", "sprint binding", binding_value),
        ("process", "process", process_value),
        ("tmux", "tmux", tmux_value),
        ("unread_messages", "unread messages", unread_value),
        ("worktree", "worktree", worktree_value),
    )
    return [{"key": key, "label": label, "value": value}
            for key, label, value in values]


_VOLATILE_PROCESS_KEYS = ("pane_id", "pane_pid", "pane_start_ticks",
                          "pane_present", "pid_state", "pgid")
_VOLATILE_GIT_KEYS = ("worktree", "branch", "dirty_tracked", "untracked",
                      "unpushed_commits", "change_digest", "indeterminate")


def _volatile_git(git: dict | None) -> dict | None:
    """The worktree facts the fence binds — every key, so a gap
    (`indeterminate`) is a value like any other and can never read as absence."""
    return {k: git.get(k) for k in _VOLATILE_GIT_KEYS} if git is not None \
        else None


def _volatile_evidence(evidence: dict) -> dict:
    """The safety-relevant facts that live OUTSIDE the database: the exact
    process identity and its liveness, pane/tmux membership, and the working
    tree a discard would erase. The preview showed these to the operator, so
    the operator's decision is only valid while they still hold — a changed
    pid_state, a vanished pane, or a file written after the preview must
    force a fresh preview, not ride the old one into a signal or a
    `git clean`."""
    process = evidence.get("process") or {}
    return {"process": {k: process.get(k) for k in _VOLATILE_PROCESS_KEYS},
            "tmux": evidence.get("tmux"),
            "git": _volatile_git(evidence.get("git"))}


def _fingerprint(con, shell_id: int, evidence: dict) -> str:
    """sha256 over everything an observation depends on: the durable state
    (closure, a new generation, a binding release, an archive hand-off) and
    the volatile process/tmux/worktree evidence above. Any change
    invalidates every outstanding observation."""
    parts = [json.dumps(_volatile_evidence(evidence), sort_keys=True,
                        default=str)]
    live = _live_session(con, shell_id)
    parts.append(json.dumps(list(live) if live is not None else None,
                            default=str))
    row = con.execute(
        "SELECT active_archive_id FROM shells WHERE shell_id=?",
        (shell_id,)).fetchone()
    parts.append(str(row[0] if row else None))
    rows = con.execute(
        "SELECT binding_id FROM sprint_planner_bindings "
        "WHERE shell_id=? AND released_at IS NULL ORDER BY binding_id",
        (shell_id,)).fetchall()
    parts.append(json.dumps([r[0] for r in rows]))
    rows = con.execute(
        "SELECT archive_id FROM shell_memory_archives "
        "WHERE shell_id=? AND ended_at IS NULL ORDER BY archive_id",
        (shell_id,)).fetchall()
    parts.append(json.dumps([r[0] for r in rows]))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ------------------------------------------------------------------ preview

def preview(con, shell_id: int, default_worktree: str | None) -> dict:
    """Build the evidence, classify, store the observation, return the
    client payload. Read-only against every non-observation table."""
    evidence = gather(con, shell_id, default_worktree)
    classification, legal_actions = classify(evidence)
    observation_id = secrets.token_hex(16)
    con.execute(
        "DELETE FROM interface_recovery_observations "
        "WHERE shell_id=? AND expires_at < datetime('now')", (shell_id,))
    con.execute(
        "INSERT INTO interface_recovery_observations "
        "(observation_id, shell_id, classification, legal_actions, evidence,"
        " fingerprint, expires_at) "
        "VALUES (?,?,?,?,?,?, datetime('now', ?))",
        (observation_id, shell_id, classification, json.dumps(legal_actions),
         json.dumps(evidence, default=str),
         _fingerprint(con, shell_id, evidence),
         f"+{OBSERVATION_TTL_S} seconds"))
    con.commit()
    return {"observation_id": observation_id,
            "expires_in_s": OBSERVATION_TTL_S,
            "classification": classification,
            "legal_actions": legal_actions,
            "evidence": evidence,
            "evidence_projection": evidence_projection(
                evidence, classification, legal_actions)}


# ------------------------------------------------------------------ execute

def _load_observation(con, shell_id: int, observation_id: str):
    """Fetch the observation and reject an unknown or expired one.

    Freshness is deliberately NOT judged here: the fence has to be the LAST
    thing that happens before the destructive sequence, not the first thing
    after the request is parsed (SC-091). This only loads what the
    preconditions need to argue about.
    """
    row = con.execute(
        "SELECT classification, legal_actions, evidence, fingerprint, "
        " expires_at FROM interface_recovery_observations "
        "WHERE observation_id=? AND shell_id=?",
        (observation_id, shell_id)).fetchone()
    if row is None:
        raise RecoveryError(404, "no_such_observation",
                            f"recovery observation {observation_id} not "
                            f"found for shell {shell_id}")
    classification, legal_actions, evidence, fingerprint, expires_at = row
    now = con.execute("SELECT datetime('now')").fetchone()[0]
    if expires_at < now:
        raise RecoveryError(
            409, "recovery_observation_stale",
            "the observation has expired — preview again",
            {"observation_id": observation_id})
    return (classification, json.loads(legal_actions), json.loads(evidence),
            fingerprint)


def _assert_no_gap(observation_id: str, evidence: dict, when: str) -> None:
    """Fail closed on incomplete evidence. A gap is deterministic — the same
    unreadable path or undecodable git output yields the same absent facts at
    preview and at execute — so it would fingerprint EQUAL and ride through as
    "nothing changed" while the work behind it was rewritten (SC-087). Absence
    of evidence is never evidence of safety."""
    reason = (evidence.get("git") or {}).get("indeterminate")
    if not reason:
        return
    raise RecoveryError(
        409, "recovery_observation_stale",
        f"the worktree could not be observed completely at {when} ({reason}) "
        "— recovery refused before any signal, closure or file removal; "
        "repair the repository and preview again",
        {"observation_id": observation_id, "detail": reason})


def _assert_fresh(con, shell_id: int, observation_id: str, stored: dict,
                  fingerprint: str, default_worktree: str | None) -> None:
    """Re-gather the whole evidence picture (a pure read) and refuse unless it
    still matches the preview. Nothing has been signalled, closed or removed
    when this runs — it is the last precondition, deliberately placed after
    every other one so the check-then-act gap is as small as the sequence can
    make it (SC-091)."""
    fresh_evidence = gather(con, shell_id, default_worktree)
    _assert_no_gap(observation_id, fresh_evidence, "now")
    if fingerprint != _fingerprint(con, shell_id, fresh_evidence):
        raise RecoveryError(
            409, "recovery_observation_stale",
            "the shell's state changed since the preview — its durable rows, "
            "process/pane identity or worktree contents no longer match what "
            "the preview showed; preview again",
            {"observation_id": observation_id})


def _assert_worktree_unchanged(observation_id: str, stored: dict,
                               worktree: str, signal_result) -> None:
    """The last gate before `git reset --hard && git clean -fd`.

    Why a SECOND gate exists at all: `_assert_fresh` is the last thing before
    the signal, but the signal is what makes a shell shut down, and a shell can
    WRITE while it shuts down — the file appears after the fence passed and the
    clean erases it (SC-091). Every other fact the fence binds (pid state, pane
    membership, the durable rows) this recovery has by now deliberately
    changed, so re-checking them is meaningless. The worktree is the one piece
    of evidence a recovery must NOT change — and the only piece a discard
    destroys. So that is what is re-read here, immediately before the delete.

    NOT ATOMIC, and not claimed to be. A git worktree is not transactional:
    unlike a row, `verify` and `reset --hard && clean -fd` cannot be made
    indivisible. Two windows remain and are irreducible at this layer:

      1. between this read and `git reset` actually opening a file — one
         subprocess spawn, the smallest gap this ordering can reach;
      2. anything writing DURING the reset/clean themselves.

    What bounds them is not a lock but the sequence: the exact process this
    recovery targeted was proven dead via /proc before we got here, so the
    writer this protects against is already gone. A DIFFERENT process writing
    into the worktree was never inside the observation's scope, and no check
    here can serialise against it.

    The refusal is honest about what already happened: the signal was sent and
    the durable closure committed, and neither can be unwound. Only the
    escalation is refused — nothing is reset, nothing is cleaned.
    """
    fresh = _volatile_git(_git_facts(worktree))
    if fresh == _volatile_git(stored.get("git")):
        return
    raise RecoveryError(
        409, "recovery_observation_stale",
        "the worktree changed after the freshness fence — the discard is "
        "refused and NOTHING was reset or cleaned. The exact process was "
        "signalled and the durable state closed before this point (that is "
        "the recovery itself, and it cannot be unwound); the files are "
        "untouched. Preview again to see the new state and confirm the "
        "discard against it.",
        {"observation_id": observation_id, "worktree": worktree,
         "signaled": signal_result, "closed": True, "discarded": False})


def _close_durable_state(con, shell_id: int, evidence: dict,
                         end_reason: str) -> dict:
    """The atomic closure (caller's transaction): session+generation+leases+
    input/wake parking via the ONE closure helper, then the archive, the
    alerts, and the generation-bound sprint binding. Ambiguous leftovers are
    parked with a named next action — never force-closed."""
    changed: dict = {"session": None, "archive": None, "alerts_resolved": 0,
                     "binding": None, "parked": []}
    sess = evidence["session"]
    if evidence["live_session"] and sess:
        alerts_before = con.execute(
            "SELECT COUNT(*) FROM planner_alerts "
            "WHERE session_id=? AND resolved_at IS NULL",
            (sess["session_id"],)).fetchone()[0]
        result = interface_broker.close_session(con, sess["session_id"],
                                                end_reason)
        alerts_after = con.execute(
            "SELECT COUNT(*) FROM planner_alerts "
            "WHERE session_id=? AND resolved_at IS NULL",
            (sess["session_id"],)).fetchone()[0]
        changed["alerts_resolved"] += alerts_before - alerts_after
        changed["session"] = {"session_id": sess["session_id"],
                              "end_reason": result["end_reason"],
                              "already_ended": result["already_ended"]}

    archive = evidence["archive"]
    if archive and archive["ended_at"] is None:
        con.execute(
            "UPDATE shell_memory_archives SET ended_at=datetime('now') "
            "WHERE archive_id=? AND ended_at IS NULL",
            (archive["archive_id"],))
        # Clear the shell's pointer ONLY while it still names this archive —
        # a newer session may already have handed over.
        con.execute(
            "UPDATE shells SET active_archive_id=NULL "
            "WHERE shell_id=? AND active_archive_id=?",
            (shell_id, archive["archive_id"]))
        changed["archive"] = {"archive_id": archive["archive_id"],
                              "closed": True}

    if sess:
        cur = con.execute(
            "UPDATE planner_alerts SET resolved_at=datetime('now') "
            "WHERE session_id=? AND resolved_at IS NULL",
            (sess["session_id"],))
        changed["alerts_resolved"] += cur.rowcount

    # Generation-bound bindings are unambiguously owned by the ended
    # generation — release them. Any OTHER unreleased binding for this shell
    # is ambiguous: leave it, park it with a named next action.
    generation = (sess or {}).get("generation")
    rows = con.execute(
        "SELECT binding_id, generation FROM sprint_planner_bindings "
        "WHERE shell_id=? AND released_at IS NULL", (shell_id,)).fetchall()
    for binding_id, bound_generation in rows:
        if generation is not None and bound_generation == generation:
            interface_broker.release_binding(con, binding_id,
                                             "shell_recovery")
            cur = con.execute(
                "UPDATE planner_alerts SET resolved_at=datetime('now') "
                "WHERE binding_id=? AND resolved_at IS NULL", (binding_id,))
            changed["alerts_resolved"] += cur.rowcount
            changed["binding"] = {"binding_id": binding_id,
                                  "released": True}
        else:
            interface_broker._alert(
                con, severity="warning",
                reason="recovery_ambiguous_binding: generation not owned "
                       "by this recovery — release via sprint close or "
                       "DELETE /api/interface/sprint-bindings/"
                       f"{binding_id}",
                binding_id=binding_id)
            changed["parked"].append({"binding_id": binding_id,
                                      "next_action": "release via sprint "
                                                     "close or explicit "
                                                     "binding DELETE"})
    return changed


def execute(con, shell_id: int, body: dict,
            default_worktree: str | None,
            grace_s: float = GRACEFUL_TERMINATE_S,
            abandon=None) -> dict:
    """Run one recovery against a fresh observation.

    `abandon` — optional callable(session_id) dropping the live runtime
    generation after closure (routes passes the runtime bridge when the
    Interface runtime is up; HTTP-only operation passes None).
    """
    observation_id = body.get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise RecoveryError(422, "validation",
                            "observation_id (string) required")
    mode = body.get("mode", "recover")
    if mode not in ("recover", "force"):
        raise RecoveryError(422, "validation", "mode is recover|force")
    preserve = body.get("preserve_worktree", True)
    discard = bool(body.get("discard_worktree", False))
    if discard and preserve:
        raise RecoveryError(422, "validation",
                            "discard_worktree requires "
                            "preserve_worktree=false — discard is never "
                            "implied by recover or force")

    classification, legal_actions, evidence, fingerprint = _load_observation(
        con, shell_id, observation_id)
    # The stored half of the fail-closed check needs no live read, so it runs
    # first: an observation that could not be gathered WHOLE is unusable no
    # matter what the live gates below would say, and the operator needs that
    # reason, not whichever live git call the same broken repo trips next.
    _assert_no_gap(observation_id, evidence, "the preview")

    if mode == "recover" and "recover" not in legal_actions:
        raise RecoveryError(
            409, "recovery_action_not_legal",
            f"recover is not legal for a {classification} shell — the "
            "preview lists the legal actions",
            {"classification": classification,
             "legal_actions": legal_actions})
    if mode == "force":
        if classification != "verified_live":
            raise RecoveryError(
                409, "recovery_action_not_legal",
                "force is legal only against a verified-live exact process "
                f"identity — this preview classified {classification}",
                {"classification": classification})
        if body.get("confirm_force") is not True:
            raise RecoveryError(
                409, "force_confirmation_required",
                "force requires confirm_force=true after naming the exact "
                "process identity to the operator",
                {"process": evidence["process"]})

    shortname = evidence["shell"]["shortname"]
    worktree: str | None = None
    if discard:
        if body.get("confirm_shortname") != shortname:
            raise RecoveryError(
                409, "discard_confirmation_required",
                "discard_worktree requires confirm_shortname naming the "
                "exact shell — it is an independent escalation, never "
                "implied", {"shell": shortname})
        worktree = (evidence["git"] or {}).get("worktree") \
            or (evidence["session"] or {}).get("worktree") \
            or default_worktree
        if not worktree or not os.path.isdir(worktree):
            raise RecoveryError(409, "no_such_worktree",
                                "no exact shell worktree to discard in")
        unpushed = _unpushed_count(worktree)
        if unpushed:
            raise RecoveryError(
                409, "unpushed_commits",
                f"{worktree} has {unpushed} commit(s) not on any remote — "
                "discard refused; push or abandon them explicitly first",
                {"worktree": worktree, "unpushed_commits": unpushed})

    # -- the freshness fence: LAST precondition, nothing destructive yet ----
    # Deliberately here and not at entry: every gate above is a pure read or a
    # body check, and each one costs wall-clock (`_unpushed_count` shells out
    # to git) during which the worktree can move. Validating at entry and
    # destroying afterwards left exactly that gap — a file written while the
    # preconditions ran was deleted by the clean (SC-091). A refusal from here
    # performs NO signal, NO closure, NO reset and NO clean.
    _assert_fresh(con, shell_id, observation_id, evidence, fingerprint,
                  default_worktree)

    # -- signal (exact process-group, re-verified at signal time) ----------
    proc = evidence["process"]
    signal_result = None
    if classification in ("exact_idle_orphan", "verified_live") \
            and proc["pid_state"] == "alive":
        signal_result = terminate_process_group(
            proc["pane_pid"], proc["pane_start_ticks"], grace_s)
        if not signal_result["signaled"]:
            raise RecoveryError(
                409, "recovery_indeterminate",
                "the exact process identity no longer verifies — no signal "
                "sent, no state closed; preview again",
                {"detail": signal_result.get("detail")})
        if not signal_result.get("dead"):
            raise RecoveryError(
                409, "recovery_absence_unproven",
                "a signal was sent but /proc never proved the process gone "
                "— durable closure refused (closure only on proven "
                "absence). Next action: preview again; if the process "
                "persists, inspect /proc/"
                f"{proc['pane_pid']} and resolve it at the OS level first",
                {"pid": proc["pane_pid"],
                 "detail": signal_result.get("detail")})

    # -- atomic durable closure on proven absence ---------------------------
    end_reason = "operator_recovery_force" if mode == "force" \
        else "operator_recovery"
    try:
        changed = _close_durable_state(con, shell_id, evidence, end_reason)
        con.execute(
            "UPDATE interface_recovery_observations "
            "SET acted_at=datetime('now') WHERE observation_id=?",
            (observation_id,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    if abandon is not None and evidence["live_session"] and evidence["session"]:
        try:
            abandon(evidence["session"]["session_id"])
        except Exception:  # noqa: BLE001, S110 — runtime cleanup is
            pass         # best-effort; durable state is already closed

    discarded = None
    if discard:
        assert worktree is not None  # proven by the discard gate above
        # Re-read the worktree immediately before the delete: the shell may
        # have written during its own SIGTERM shutdown, after the fence above
        # passed. See the docstring for the windows this does NOT close.
        _assert_worktree_unchanged(observation_id, evidence, worktree,
                                   signal_result)
        discarded = _discard_worktree_files(worktree)

    return {"shell_id": shell_id, "shortname": shortname,
            "classification": classification, "mode": mode,
            "signaled": signal_result,
            "closed": changed,
            "worktree": discarded or {"preserved": True},
            "unread_messages": evidence["unread_messages"],
            "availability": "available"}
