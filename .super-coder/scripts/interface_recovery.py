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
  time immediately before it deletes, because a shell can write while it shuts
  down, i.e. after that fence has already passed; that refusal deletes nothing
  but cannot unwind the signal or the closure, and it says which of the two
  actually happened. Every worktree observation is STABLE — each path read
  self-consistently, the whole observation repeated identically — so a write
  landing during a read cannot produce an answer that was never true (SC-092).
  Freshness alone cannot make a discard safe, because no read sees work that
  does not exist yet: the discard is therefore BOUNDED BY THE OBSERVATION
  rather than by the tree's later state — it restores exactly the enumerated
  tracked paths and removes exactly the enumerated untracked ones, so a file
  written after the observation is not in the delete set and survives by
  construction (SC-100). See `_assert_worktree_unchanged` for what remains
  open — a worktree is not transactional and no claim of atomicity is made
  here.

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

import errno
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
    but /proc will not give us a usable answer — fail closed, never 'dead').

    'Unreadable' covers a refused read AND an unusable one. A short or
    malformed `/proc/<pid>/stat` — the file is a live snapshot and can be read
    mid-teardown — makes `_read_stat` raise ValueError or IndexError, which
    escaped every caller: this one is called from inside the signalling
    sequence, where an escape after SIGTERM became an opaque 500 over an
    already-delivered signal (SC-131). Answering 'unreadable' here is both the
    honest reading and the fail-closed one, and it is fixed at the definition
    so no caller has to guard it separately.
    """
    try:
        ticks, state = _read_stat(pid)
    except FileNotFoundError:
        return "dead"
    except (PermissionError, ProcessLookupError):
        return "unreadable"
    except OSError:
        return "unreadable"
    except (ValueError, IndexError):
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
    caller to refuse closure with a named next action.

    ALWAYS returns a result; never raises. The delivery of the first signal is
    a seam exactly like the durable commit: before it, a failure is a refusal
    and nothing has happened; after it, an irreversible act has been performed
    and an exception escaping to the route as an opaque 500 tells the operator
    neither that their process was signalled nor that nothing was closed
    (SC-131). So the boundary sits AT the seam, and the failure modes are
    enumerated by where they fall relative to it rather than by which of them
    a probe has already found:

    - before delivery — a stale identity, an unreadable process group, and a
      SIGTERM that cannot be delivered at all (EPERM on a group we may not
      signal, ESRCH on one that died since the identity check). All report
      `signaled: False`, which the caller maps to a refusal;
    - after delivery — everything else, whatever it is. The grace poll, the
      identity re-check, the SIGKILL itself and its poll all run under one
      boundary that returns `signaled: True` with the phase it broke in, so
      the caller can state what was done to the process and what was not.
    """
    state = _proc_state(pid, start_ticks)
    if state != "alive":
        return {"signaled": False, "dead": False, "reason": "indeterminate",
                "detail": f"process state {state} at signal time"}
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return {"signaled": False, "dead": False, "reason": "indeterminate",
                "detail": "process group unreadable at signal time"}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError as exc:
        # Nothing was delivered, so this is a refusal like the two above and
        # not a partial act — the distinction the caller acts on.
        return {"signaled": False, "dead": False, "reason": "indeterminate",
                "detail": f"SIGTERM could not be delivered to PGID {pgid}: "
                          f"{type(exc).__name__}: {str(exc)[:200]}"}
    # -- the signal is delivered: NOTHING below may raise -------------------
    phase, escalated = "grace_wait", False
    try:
        state = _wait_dead(pid, start_ticks, grace_s)
        if state == "dead":
            return {"signaled": True, "dead": True, "escalated": False,
                    "pid": pid, "pgid": pgid}
        if state == "unreadable":
            return {"signaled": True, "dead": False, "escalated": False,
                    "pid": pid, "pgid": pgid, "reason": "absence_unproven",
                    "detail": "SIGTERM sent but /proc turned unreadable "
                              "during the grace — absence not proven"}
        # Grace expired with the process alive. Re-verify the EXACT identity
        # before SIGKILL — the window is long enough for exit + PID reuse, and
        # the rule is never signal an uncertain process.
        phase = "escalation_recheck"
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
        phase = "sigkill"
        os.killpg(pgid, signal.SIGKILL)
        escalated, phase = True, "kill_wait"
        state = _wait_dead(pid, start_ticks, grace_s)
        if state == "dead":
            return {"signaled": True, "dead": True, "escalated": True,
                    "pid": pid, "pgid": pgid}
        return {"signaled": True, "dead": False, "escalated": True,
                "pid": pid, "pgid": pgid, "reason": "absence_unproven",
                "detail": f"process state {state} after SIGKILL — absence not "
                          "proven (an unkillable D-state process survives)"}
    except Exception as exc:  # noqa: BLE001 — post-signal: report, never raise
        return {"signaled": True, "dead": False, "escalated": escalated,
                "pid": pid, "pgid": pgid, "reason": "signal_failed",
                "phase": phase,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "detail": f"SIGTERM was delivered to PGID {pgid} and cannot "
                          f"be unwound; the sequence then failed in {phase} "
                          f"({type(exc).__name__}: {str(exc)[:200]}) — "
                          "absence not proven"}


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


def _stamp(st) -> tuple:
    """The comparable identity of one inode-as-seen-at-a-path. Everything the
    metadata prefix binds, PLUS device/inode so a path replaced by a rename is
    a different answer — and NOT st_atime, which our own read moves (see
    `_path_identity`). Used only to prove an observation held still; the digest
    records `_meta`."""
    return (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid,
            st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_dev, st.st_ino)


def _meta(st) -> str:
    return (f"{stat.S_IFMT(st.st_mode):o}:{stat.S_IMODE(st.st_mode):04o}:"
            f"{st.st_uid}:{st.st_gid}:{st.st_size}:{st.st_mtime_ns}:"
            f"{st.st_ctime_ns}")


# A torn observation is retried this many times before it becomes a gap.
# Small on purpose: a path that will not hold still across three passes is a
# tree still being written, and that is a refusal, not something to wait out.
_STABILITY_TRIES = 3

# How many kept-back entries the discard result names before it only counts.
_KEPT_REPORTED = 20

# errnos that mean "the entry changed under us", not "we cannot read it":
# gone, replaced by a symlink, or a parent component replaced by a file.
_TORN_ERRNOS = (errno.ENOENT, errno.ELOOP, errno.ENOTDIR)


def _observe_path(path: str) -> str | None:
    """ONE self-consistent attempt at `_path_identity`. Returns the identity,
    or None when the path moved while it was being observed (the caller
    retries, then fails closed).

    Self-consistency is the whole point: metadata is read, the content behind
    it is read, and the metadata is read AGAIN — via the OPEN FD (same inode,
    whatever the path now names) and via the path (same inode still named) —
    and the answer is returned only when every read agrees. Reading the two
    halves separately is a torn read: a chmod landing between the lstat and
    the open produced an identity that was never true at any instant — old
    mode, new bytes — which then compared EQUAL to the preview and let the
    discard erase the change (SC-092).
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        # Paths and contents never enter the payload — errno only.
        raise _GitEvidenceUnavailable(f"lstat: errno {exc.errno}") from exc
    stamp, meta, mode = _stamp(st), _meta(st), st.st_mode

    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            if exc.errno in _TORN_ERRNOS:
                return None
            raise _GitEvidenceUnavailable(
                f"readlink: errno {exc.errno}") from exc
        identity = f"link:{meta}:" + hashlib.sha256(
            target.encode("utf-8", "surrogateescape")).hexdigest()
    elif stat.S_ISDIR(mode):
        identity = f"dir:{meta}"
    elif not stat.S_ISREG(mode):
        identity = f"special:{meta}"
    else:
        h = hashlib.sha256()
        try:
            # O_NOFOLLOW: the path was a regular file at lstat; if it became a
            # symlink in between, refuse to read through it rather than hash
            # whatever it now points at.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in _TORN_ERRNOS:
                return None
            raise _GitEvidenceUnavailable(f"open: errno {exc.errno}") from exc
        try:
            if _stamp(os.fstat(fd)) != stamp:
                return None          # already moved between lstat and open
            while chunk := os.read(fd, 1 << 16):
                h.update(chunk)
            if _stamp(os.fstat(fd)) != stamp:
                return None          # rewritten or re-permissioned as we read
        except OSError as exc:
            raise _GitEvidenceUnavailable(f"read: errno {exc.errno}") from exc
        finally:
            os.close(fd)
        identity = f"file:{meta}:{h.hexdigest()}"

    # ...and the path must still name that same inode: an fstat cannot see a
    # rename landing a different file on the path we are reporting about.
    try:
        after = os.lstat(path)
    except OSError:
        return None
    return identity if _stamp(after) == stamp else None


def _path_identity(path: str) -> str:
    """Identity of one working-tree path: its TYPE and lstat metadata first,
    then what that type carries. Classified with lstat — NO-FOLLOW, always.

    Observed self-consistently and retried while it tears; a path that will
    not hold still becomes a GAP (`_GitEvidenceUnavailable`), which refuses the
    recovery. See `_observe_path`.

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
    for _ in range(_STABILITY_TRIES):
        identity = _observe_path(path)
        if identity is not None:
            return identity
    raise _GitEvidenceUnavailable(
        "torn read: an entry kept changing while it was observed")


# What `_entry_identity` records for a path the index holds nothing for.
# A value, not an absence: a path GAINING or LOSING an index entry is itself
# a change the digest must move on.
_INDEX_ABSENT = "index:none"


def _index_flags(identity: str) -> str:
    """The durable-flag token out of one index identity — `--` for an entry
    the index does not hold."""
    return identity.rsplit(" ", 1)[-1] if " " in identity else "--"


def _index_identities(worktree: str) -> dict[str, str]:
    """What the INDEX holds, per path: mode, object id, merge stage, and the
    durable per-entry FLAGS.

    Read in ONE pass over the whole index rather than per path — every entry
    then comes from the same instant, the same way the path set does.

    The index is a store in its own right, not a blob table, so what is bound
    here is everything an entry durably carries that a discard can rewrite.
    Enumerated from the on-disk index format rather than from memory, the
    per-entry fields are: the stat cache (ctime/mtime/dev/ino/uid/gid/size),
    mode, object id, and the flag word — assume-valid, merge stage, and the
    v3 extended bits skip-worktree and intent-to-add. Of those:

    - mode, object id and merge stage are the content a discard throws back to
      HEAD, and are recorded.
    - skip-worktree and assume-unchanged are recorded (SC-125). They are
      durable index state that `restore --staged` CLEARS, and setting one
      moves nothing else: on a staged-only entry the working file, its lstat,
      the porcelain line and the entry's own mode/object/stage are all
      byte-identical either side of the bit, so an unbound flag rode straight
      through the fence.
    - intent-to-add needs no separate field: it can only be set on a path the
      index does not otherwise hold, and both transitions move the PORCELAIN
      line the digest already binds (`??` <-> ` A`, which is distinct from a
      real staged add's `A `). Recorded by the outer layer, not this one.
    - the STAT CACHE is excluded, and by reproduction rather than by argument:
      `git status` REFRESHES it as a side effect of the very observation this
      fences, so binding it would make a preview stale against itself and
      refuse every discard forever (the same trap st_atime is excluded from
      `_path_identity` for). It is a cache of the worktree, and the worktree
      half of `_entry_identity` binds the thing itself.

    Index EXTENSIONS carry no per-entry work a discard destroys: cache-tree,
    untracked-cache, fsmonitor and split-index are all caches git rebuilds,
    and resolve-undo (REUC) survives the discard untouched — reproduced, not
    assumed. `MERGE_HEAD` and an in-progress merge likewise survive it.

    The flags are read via `ls-files -v`, whose tag letter is the only
    interface to them. Its letter set also spans WORKTREE-derived states (`C`
    modified, `R` removed, `K` to-be-killed) — none of which this invocation
    can emit, since those require the matching listing option — so rather than
    record the letter, the two durable bits are decoded out of it. A letter
    this function does not expect therefore decodes to "no flags", exactly as
    a plain `H` does, and can never move the digest on a volatile fact.

    An unmerged path carries several stage entries; all of them are recorded,
    sorted, because a conflict resolution rewrites exactly that set.
    """
    entries: dict[str, list[str]] = {}
    for record in _git_out(worktree, "ls-files", "-v", "--stage", "-z",
                           timeout=30).split("\0"):
        if not record:
            continue
        meta, _tab, rel = record.partition("\t")
        tag, mode, obj, stage = meta.split()
        flags = ("S" if tag.upper() == "S" else "-") \
            + ("A" if tag.islower() else "-")
        entries.setdefault(rel, []).append(f"{mode} {obj} {stage} {flags}")
    return {rel: "+".join(sorted(stages)) for rel, stages in entries.items()}


def _entry_identity(worktree: str, rel: str, index: dict[str, str]) -> str:
    """One enumerated entry's COMPLETE identity — what the filesystem holds at
    that path, and what the index holds for it.

    Staged work does not live in the worktree, and a discard destroys it all
    the same: `git restore --source=HEAD --staged` throws the index back to
    HEAD, and on an unborn HEAD `git rm --cached` drops the entry outright. So
    an operator who stages a change AFTER the preview had, until SC-123, work
    inside the confirmed blast radius that no gate could see — the working
    file, its lstat and the porcelain line all stay byte-identical while the
    blob underneath is replaced (`git add -p` produces exactly that shape).

    Same rule as every attribute already bound: if the discard would create,
    delete or rewrite it, the digest binds it. The index is simply the one
    place that state is not a file.
    """
    return (_path_identity(os.path.join(worktree, rel)) + "\0"
            + index.get(rel, _INDEX_ABSENT))


def _enumerate(worktree: str, head: bool) -> tuple[set, set, set, set]:
    """`(tracked, index_only, untracked_files, untracked_dirs)` — the entries a
    discard would have to undo, as of now, classified by what undoing one does.

    THE definition of the destructive set, and the only one. It has three
    consumers and states itself to none of them: the plan the operator
    confirms and the destruction it bounds (`_discard_plan`), the check that
    decides whether the discard actually worked (`_unrestored`), and the
    preview the operator reads (`evidence_projection`, through
    `_observe_worktree`). Each restatement was a place the contract could
    drift from itself, and each one drifted: the verification asked a proxy
    question (SC-129), and the preview counted porcelain lines instead —
    which is why it described a staged blob's destruction in the same words as
    an ordinary file deletion (SC-130).

    `index_only` is a SUBSET of `tracked`, and it is the class the operator
    cannot otherwise see: the entry's content lives in the index and the
    working tree does not show it, so nothing on disk changes appearance when
    the discard destroys it. On a born HEAD that is exactly "differs from HEAD
    in the index while the working tree agrees with HEAD" — the `AD` staged-
    then-deleted path, and equally a staged edit whose file was written back
    to HEAD's content. On an unborn HEAD, where there is no HEAD to agree
    with, it is an index entry with no file on disk at all: the same class,
    reached by the same reasoning rather than by the one case a probe found.
    """
    def paths(*args) -> list[str]:
        # -z: paths verbatim, no C-quoting to unescape.
        return [p for p in _git_out(worktree, *args, timeout=30).split("\0")
                if p]

    listed = set(paths("ls-files", "-o", "--exclude-standard", "-z"))
    untracked_dirs = {p for p in
                      paths("ls-files", "-o", "--directory",
                            "--exclude-standard", "-z") if p.endswith("/")}
    # A trailing slash in the FILE listing is a nested repository — git names
    # it once and never descends. It is a directory: unlinking it would fail
    # the whole discard, and `clean -fd` leaves it alone too.
    untracked_dirs |= {p for p in listed if p.endswith("/")}
    untracked_files = {p for p in listed if not p.endswith("/")}
    if not head:
        tracked = set(paths("ls-files", "-z"))
        index_only = {rel for rel in tracked
                      if not os.path.lexists(os.path.join(worktree, rel))}
        return tracked, index_only, untracked_files, untracked_dirs
    # BOTH diffs, because neither alone is the set. `diff HEAD` compares HEAD
    # to the WORKING TREE, so a path staged and then deleted (`AD`) reads as
    # no change — absent in HEAD, absent on disk — while the index still holds
    # its blob. It was therefore in no delete set and no preview, survived the
    # discard, and `discarded=true` was returned over staged work still
    # sitting in the index. `reset --hard`, the operation this replaces, threw
    # that entry away; found by asking the SC-129 question of the enumeration
    # itself rather than of the check that reads it.
    in_worktree = set(paths("diff", "HEAD", "--name-only", "-z"))
    in_index = set(paths("diff", "--cached", "HEAD", "--name-only", "-z"))
    return in_worktree | in_index, in_index - in_worktree, \
        untracked_files, untracked_dirs


def _discard_plan(worktree: str, porcelain: list[str], head: bool) -> dict:
    """WHAT a discard would destroy, named entry by entry: the exact SET of
    worktree entries, each entry's identity, and the digest over the whole
    thing.

    This set is not merely described to the operator — it BOUNDS the
    destructive step (`_discard_worktree_files`). `reset --hard` and
    `clean -fd` act on whatever is in the tree when they run, so an ordinary
    file save landing after the observation was erased even though no
    observation ever saw it, and no repetition of the read can close that:
    two passes detect changes BETWEEN them, and a path created after each
    pass's own enumeration is missed by both, so the digests agree (SC-100).
    Discarding this enumerated set instead makes the race stop existing —
    a path that is not in the set cannot be removed, whenever it appeared.

    INVARIANT for the digest: it MUST change if ANY safety-relevant aspect of
    the state a discard would destroy has changed since the preview. Porcelain
    lines are far coarser than that — they stay byte-identical while the work
    underneath them is rewritten, retargeted, re-permissioned or changes type —
    so the line set is only the outer layer. Held against the invariant, that
    state is: the SET of affected entries, and for each its ENTITY IDENTITY
    (`_entry_identity`) — type, permissions, ownership, timestamps, and
    regular-file bytes or symlink target, PLUS the index entry (mode, object,
    stage) standing behind it. The last is not a filesystem fact at all and
    was missed for exactly that reason (SC-123): `git restore --staged`
    destroys staged content the worktree never shows.

    The set is what the discard must undo:
    - untracked FILES individually (`ls-files -o`), never collapsed to a dir;
    - untracked DIRECTORIES in their own right (`--directory`, trailing `/`),
      empty ones included: `clean -fd` removes a directory that a file listing
      never names;
    - born HEAD -> every path differing from HEAD in the working tree OR in
      the index (`diff HEAD` and `diff --cached HEAD`): staged, unstaged and
      deletions, one row each. Both, because a path staged and then deleted
      differs from HEAD only in the index — see `_enumerate`;
    - unborn HEAD -> every INDEX entry (`ls-files`). Nothing has been
      committed, so a discard drops the whole index and the files with it —
      and `ls-files -o` cannot see a staged path, which left staged work
      destroyed by a discard the digest never covered.

    Ignored files stay out: `clean -fd` without -x does not touch them, so
    they are not state the confirmation is about.
    """
    tracked, index_only, untracked_files, untracked_dirs = _enumerate(
        worktree, head)
    index = _index_identities(worktree)

    h = hashlib.sha256()
    for line in sorted(porcelain):
        h.update(line.encode("utf-8", "surrogateescape") + b"\n")
    identities, index_of = {}, {}
    for rel in sorted(tracked | untracked_files | untracked_dirs):
        index_of[rel] = index.get(rel, _INDEX_ABSENT)
        identities[rel] = (_path_identity(os.path.join(worktree, rel)) + "\0"
                           + index_of[rel])
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0"
                 + identities[rel].encode() + b"\0")
    # The INDEX half is kept separately as well as composed, because the
    # discard has to re-verify it ALONE at a point where the filesystem half
    # has legitimately moved: immediately before the destructive call, when the
    # removal may already have unlinked the file (SC-124).
    return {"head": head, "tracked": sorted(tracked),
            "index_only": sorted(index_only),
            "untracked_files": sorted(untracked_files),
            "untracked_dirs": sorted(untracked_dirs),
            "identities": identities, "index": index_of,
            "digest": h.hexdigest()}


def _observe_worktree(worktree: str) -> tuple[dict, dict]:
    """ONE complete pass over the worktree facts, plus the discard plan that
    pass enumerated. Every git read the fence depends on happens here, so the
    whole answer can be repeated and compared — the entry SET is enumerated at
    a different instant from each entry's identity, and a pass is only
    trustworthy if the composite holds still."""
    head = _head_exists(worktree)
    branch = _git_out(worktree, "rev-parse", "--abbrev-ref", "HEAD") \
        if head else _git_out(worktree, "branch", "--show-current")
    porcelain = _git_out(worktree, "status", "--porcelain").splitlines()
    unpushed = int(_git_out(worktree, "rev-list", "HEAD", "--not",
                            "--remotes", "--count").strip() or 0) \
        if head else 0
    plan = _discard_plan(worktree, porcelain, head)
    # COUNTED OFF THE PLAN, never off porcelain. What the operator is shown
    # has to be generated by the same thing that decides what happens, or the
    # two can describe different worlds — and did: porcelain renders an `AD`
    # staged-then-deleted path as one dirty line, indistinguishable from an
    # ordinary deletion, while the plan destroys a staged blob nothing on disk
    # shows (SC-130). Porcelain stays an INPUT to the digest, where its
    # coarseness is harmless; it is no longer a second statement of the set.
    return {"worktree": worktree, "branch": branch.strip(),
            "dirty_tracked": len(plan["tracked"]),
            "index_only": len(plan["index_only"]),
            "untracked": len(plan["untracked_files"]),
            "untracked_dirs": len(plan["untracked_dirs"]),
            "unpushed_commits": unpushed,
            # WHICH paths changed and WHAT each one now IS — not just how
            # many: equal-count churn (one file cleaned while another is
            # dirtied), a rewrite of an already-listed path, a symlink
            # retarget, a chmod, and a type transition all move the
            # freshness fingerprint. Paths and contents stay out of the
            # payload.
            "change_digest": plan["digest"]}, plan


def _observe_stable(worktree: str | None) -> tuple[dict | None, dict | None]:
    """`(facts, plan)` — the facts the fence compares, and the enumerated set
    the discard is bounded by. They come from the SAME pass on purpose: the
    digest equality proves that set IS the set the operator consented to, so
    the discard needs no second enumeration to race against.

    `plan` is None whenever there is nothing to discard against — no
    repository, or an observation that could not be completed.

    Three outcomes for the facts, kept apart on purpose:

    - `None` — there is no repository to observe (no worktree, or no `.git`).
      Complete evidence: there is no git-managed state here to erase.
    - a facts dict — the complete observation, STABLE: two consecutive passes
      returned exactly the same answer.
    - `{"indeterminate": <reason>}` — a repository IS there and its evidence
      could not be gathered whole, or it would not hold still. NOT "no facts":
      execute refuses on it, so an unobservable worktree can never read as a
      safe one (SC-087).

    Stability is a correctness requirement, not politeness. A single pass reads
    the path set at one instant and each path's identity at another, so a write
    landing between them yields an answer that was never true as a whole — and
    a torn answer can compare EQUAL to the preview and let a discard erase what
    moved (SC-092). Requiring two consecutive identical passes means the answer
    was true across a window, not merely at assorted instants inside one; a tree
    that keeps moving is refused rather than approximated.

    Stability does NOT make the answer current, and is not relied on for that:
    a path created after each pass's own enumeration is invisible to both
    passes, so equal digests prove only that nothing the passes SAW moved
    (SC-100). What protects an unseen path is that the discard cannot touch a
    path outside `plan` at all.
    """
    if not worktree:
        return None, None
    dotgit = os.path.join(worktree, ".git")
    if not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
        return None, None
    try:
        previous = None
        for _ in range(_STABILITY_TRIES):
            facts, plan = _observe_worktree(worktree)
            if facts == previous:
                return facts, plan
            previous = facts
        raise _GitEvidenceUnavailable(
            "the worktree did not hold still across consecutive observations")
    except (_GitEvidenceUnavailable, ValueError) as exc:
        return {"indeterminate": str(exc)}, None


def _git_facts(worktree: str | None) -> dict | None:
    """The fence's half of `_observe_stable` — the facts alone, for the
    evidence payload. See `_observe_stable` for the three outcomes."""
    return _observe_stable(worktree)[0]


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


def _open_parent(worktree: str, rel: str) -> int:
    """A directory fd for `rel`'s parent, walked from the worktree root ONE
    COMPONENT AT A TIME with O_NOFOLLOW. Raises OSError if any component is a
    symlink or has stopped being a directory. Caller closes the fd.

    Every destructive call resolves through this instead of through a joined
    path, because O_NOFOLLOW on the final component says nothing about the
    ANCESTORS (SC-105): move `d` out of the worktree and drop a symlink to it
    at `d`, and `d/u.txt` still stats as the very same inode the observation
    recorded — the identity check passes, and a path-based `unlink` follows the
    symlink and deletes the file at its new home OUTSIDE the worktree. A
    recovery may only ever destroy inside the exact worktree it was confirmed
    against, so an ancestor that moved is a refusal, never a redirect.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    fd = os.open(worktree, flags)
    try:
        for part in os.path.dirname(rel).split("/"):
            if not part or part == ".":
                continue
            nxt = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


def _is_dir(path: str) -> bool:
    """A REAL directory at this path — lstat, so a symlink to one is not it.
    `git restore` replaces a symlink outright; only a directory makes it
    recurse."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _prune_dirs(worktree: str, plan: dict) -> list[str]:
    """Remove the enumerated untracked directories once their enumerated files
    are gone. Returns the enumerated roots that are still there.

    Two bounds, and both matter:

    - only directories the observation covers are candidates: an enumerated
      directory, or an ancestor of an enumerated entry (which is how a
      directory emptied by the restore — the one holding a staged-new file —
      still goes, as it did under `clean -fd`). Walking the tree instead would
      rmdir an EMPTY directory created after the observation, which is the
      SC-100 case one entity type over.
    - rmdir, never a recursive delete. Emptiness is the whole guarantee: a
      file created inside after the observation keeps its directory non-empty,
      so the directory survives carrying it, and so does every parent. Ignored
      files and a nested repository's contents have exactly the same effect —
      as they did under `clean -fd`, which leaves both behind.
    """
    prunable = {rel.rstrip("/") for rel in plan["untracked_dirs"]}
    for rel in plan["untracked_files"] + plan["tracked"]:
        parent = os.path.dirname(rel)
        while parent:                       # stops at the worktree root
            prunable.add(parent)
            parent = os.path.dirname(parent)
    for rel in sorted(prunable, key=len, reverse=True):  # deepest first
        try:
            fd = _open_parent(worktree, rel)
        except OSError:
            continue                    # moved or symlinked ancestor: leave it
        try:
            os.rmdir(os.path.basename(rel), dir_fd=fd)
        except OSError:
            pass                        # not empty, or gone: both fine
        finally:
            os.close(fd)
    return [rel for rel in plan["untracked_dirs"]
            if os.path.lexists(os.path.join(worktree, rel.rstrip("/")))]


def _unrestored(worktree: str, plan: dict, restored: list[str]) -> list[str]:
    """Of the entries the restore reported success for, the ones it did NOT
    actually undo. Never raises.

    An exit code is not an outcome. `git restore` can exit 0 without having
    put the entry where it promised, and the result then claims `discarded`
    over work still sitting on disk — misreporting, which decision #45 ranks
    with destruction. So the outcome is VERIFIED rather than inferred.

    Verified against the CONTRACT, and that distinction is the whole of
    SC-129. The first version of this check asked whether the entry had
    changed from the identity the operator consented to — a PROXY: the
    contract implies it, so the right answer satisfies it, but so does a
    restore that writes some THIRD state, which is neither the old content nor
    HEAD. It passed and the discard was reported complete. (Same shape as
    F7's SC-121 one unit over: asserting a floor the correct implementation
    also clears.)

    The contract is "this entry is no longer one a discard would have to
    undo", so that is what is asked — by re-running the enumeration that
    DEFINED the set (`_enumerate`) and requiring the entry to be absent from
    it. Nothing is restated in a second form that could drift: a third state
    still differs from HEAD and is named; a staged-new path whose file
    survived its de-staging is named as untracked; an entry the restore left
    alone is named exactly as it was at plan time.

    That enumeration reads the worktree half of a flagged entry THROUGH the
    index — `git diff` trusts skip-worktree and assume-unchanged and will not
    look at the file — so an entry still carrying a durable bit here cannot be
    verified by it and is reported kept. Reachable enumerated entries do not
    hit this: a bit only exists on an index entry, and an index entry the plan
    holds differs from HEAD, so the restore rewrites it and drops the bit with
    it (reproduced). An entry whose bit survived is one where something other
    than that happened, which is not a state to report success from.

    Unreadable now -> unverifiable, and an unverifiable outcome is never
    reported as a success.

    On an unborn HEAD the same enumeration is the same contract: the entry
    must be gone from the index (`ls-files`) and gone from disk, where it
    would otherwise reappear in the untracked listing.
    """
    if not restored:
        return []
    try:
        tracked, _index_only, untracked_files, untracked_dirs = _enumerate(
            worktree, plan["head"])
        index = _index_identities(worktree)
    except _GitEvidenceUnavailable:
        return sorted(restored)
    still = tracked | untracked_files | untracked_dirs
    return sorted(
        rel for rel in restored
        if rel in still
        or _index_flags(index.get(rel, _INDEX_ABSENT)) != "--")


_INDEX_FLAG_OPTS = {"S": "--skip-worktree", "A": "--assume-unchanged"}


def _restore_index_flags(worktree: str, plan: dict,
                         restored: list[str]) -> list[str]:
    """Put back the durable index flags the restore cleared. Returns the
    entries whose flags could NOT be put back.

    The bits are not part of HEAD, so "throw the entry back to HEAD" does not
    say what should happen to them — clearing them is a SIDE EFFECT of the
    command, not a change the operator confirmed discarding. The content goes;
    the operator's standing instruction about the path stays (SC-125, and
    decision #45's preserve-and-report rather than silent clearing).

    Where the store leaves no room for the flag it is REPORTED, never quietly
    dropped: a staged-new path is gone from the index once the discard has
    run, and on an unborn HEAD every entry is, so there is no entry left to
    carry a bit. Outcomes are verified by re-reading the index rather than
    trusted from an exit code — `update-index` is silent about a path it could
    not mark.
    """
    want = {rel: _index_flags(plan["index"][rel]) for rel in restored}
    want = {rel: flags for rel, flags in want.items() if flags != "--"}
    if not want:
        return []
    for bit, opt in _INDEX_FLAG_OPTS.items():
        paths = [rel for rel, flags in want.items() if bit in flags]
        if not paths:
            continue
        try:
            subprocess.run(
                ["git", "-C", worktree, "update-index", "-z", opt, "--stdin"],
                input=b"".join(rel.encode("utf-8", "surrogateescape") + b"\0"
                               for rel in paths),
                capture_output=True, timeout=60, check=False)
        except Exception:  # noqa: BLE001, S110 — timeout or spawn failure;
            pass           # the verification below reports what actually
                           # stuck, which is the truth an exit code is not
    try:
        after = _index_identities(worktree)
    except _GitEvidenceUnavailable:
        return sorted(want)
    return sorted(rel for rel, flags in want.items()
                  if _index_flags(after.get(rel, _INDEX_ABSENT)) != flags)


def _discard_result(worktree: str) -> dict:
    """The shape EVERY discard outcome is reported in, whether the sequence
    ran to the end, stopped at a named step, or never started because the gate
    ahead of it broke. One builder so those cannot drift into three answers to
    the same question."""
    return {"worktree": worktree, "discarded": False, "completed": [],
            "failed": None, "kept": [], "kept_count": 0, "flags_lost": []}


def _discard_worktree_files(worktree: str, plan: dict) -> dict:
    """Undo EXACTLY the entries `plan` enumerated — restore its tracked paths
    from HEAD, remove its untracked ones — and nothing else. Never deletes the
    worktree, its branch, or ignored files.

    Bounded by the observation, not by the filesystem (SC-100). `reset --hard`
    and `clean -fd` operate on whatever the tree holds at delete time, so an
    ordinary editor save landing after the last observation was erased without
    ever having been seen or consented to. Here the delete set is fixed by the
    observation the operator confirmed: a path that appeared later is simply
    not in it and survives BY CONSTRUCTION — the race is not won, it stops
    existing. The blast radius is exactly what the preview showed, which is
    what decision #41 requires of a consented destructive path.

    Bounding the SET is not enough on its own, because one git command's own
    footprint can exceed the path it is given: `git restore` on a path the
    worktree has turned into a directory deletes that directory whole,
    ignored files and all (SC-103). So the removal runs FIRST — clearing the
    enumerated entries out of such a directory — and a path still standing as
    a directory afterwards is left alone rather than restored over.

    Each entry is also re-verified against the identity the fence observed,
    and left alone when it no longer matches — so a path is only ever removed
    while it still IS what the operator was shown. BOTH halves of that
    identity are re-read immediately before the command that destroys them:
    the filesystem half before each `unlink`, the index half before the final
    `restore`/`rm --cached`. Sampling the index ONCE at the top instead was
    SC-124 — nothing HERE writes to the index before the restore, but the
    operator does, and a `git add` landing while the removal loop runs was
    then erased by a restore that had checked an older read. That check
    narrows, and does not close, the window between the check and the
    `unlink`/`restore` itself: no filesystem offers "remove only if
    unchanged", and the index is not transactional against us either.

    Durable index flags the restore clears are put back afterwards, and named
    in `flags_lost` where the store leaves nowhere to put them (SC-125).

    Runs AFTER the durable closure is committed, so a failure here must never
    escape as a 500 that hides what happened: each step's outcome is recorded
    and a failure returns exactly what completed and where it stopped.
    `discarded` is true only when every enumerated FILE was undone; anything
    left behind — files and directories alike — is named in `kept`, never
    silently dropped.

    That promise is now STRUCTURAL rather than a claim about the steps below
    (SC-126). Every known failure mode is handled where it happens, but the
    operator's files have already been touched by the time anything here can
    go wrong, and a 500 tells them nothing about what state those files are
    in — the worst report there is. So the whole sequence returns its
    part-filled result rather than raising, and an unexpected failure is NAMED
    in `failed` rather than swallowed.
    """
    result = _discard_result(worktree)
    try:
        _discard_steps(worktree, plan, result)
    except Exception as exc:  # noqa: BLE001 — post-commit: report, never raise
        result["discarded"] = False
        result["failed"] = result["failed"] or {
            "step": "unexpected",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return result


def _discard_steps(worktree: str, plan: dict, result: dict) -> None:
    """The discard itself, filling `result` as it goes. Split out so that
    whatever happens, the caller still has what completed (see above)."""
    identities, restore, kept_files = plan["identities"], [], []
    # One index read for the whole pass: nothing below writes to the index
    # until the final `restore`/`rm --cached`, so this snapshot stays true for
    # every re-check. Unreadable -> `index` is None and NOTHING verifies,
    # which keeps every entry rather than deleting on an unchecked identity.
    try:
        index = _index_identities(worktree)
    except _GitEvidenceUnavailable:
        index = None

    def unchanged(rel: str) -> bool:
        if index is None:
            return False
        try:
            return _entry_identity(worktree, rel, index) == identities[rel]
        except _GitEvidenceUnavailable:
            return False   # unreadable now: never a licence to delete

    def keep(rel: str, *, is_file: bool = True) -> None:
        result["kept_count"] += 1
        if is_file:
            kept_files.append(rel)
        if len(result["kept"]) < _KEPT_REPORTED:
            result["kept"].append(rel)

    # Decided BEFORE anything moves, because the removal below is what clears
    # a directory sitting on a tracked path — after it, "absent" is our own
    # doing and would read as a change.
    for rel in plan["tracked"]:
        (restore.append if unchanged(rel) else keep)(rel)

    # -- remove the enumerated untracked entries ----------------------------
    # FIRST, and that ordering is load-bearing (SC-103). `git restore` on a
    # path the worktree has turned into a DIRECTORY deletes that directory
    # whole — everything under it, enumerated or not. Removing the enumerated
    # entries first empties such a directory so the restore has nothing to
    # recurse through; whatever is left afterwards was never in the delete set,
    # and the restore below refuses to run over it.
    removable = list(plan["untracked_files"])
    if not plan["head"]:
        # Unborn: `git rm --cached` never touches the worktree, so these files
        # are removed here with the untracked ones and unstaged below.
        removable += restore
    removed = []
    for rel in sorted(removable):
        if not unchanged(rel):
            keep(rel)
            continue
        try:
            fd = _open_parent(worktree, rel)
        except OSError:
            # An ancestor is no longer the directory it was, or is now a
            # symlink. The entry may still stat as the observed inode through
            # it — that is exactly the trap — so refuse and report rather than
            # delete whatever is on the other side (SC-105).
            keep(rel)
            continue
        try:
            os.unlink(os.path.basename(rel), dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            result["failed"] = {"step": "remove",
                                "error": f"{rel[-120:]}: errno {exc.errno}"}
            return
        finally:
            os.close(fd)
        removed.append(rel)
    for rel in _prune_dirs(worktree, plan):
        keep(rel, is_file=False)
    result["completed"].append("remove")

    # -- restore the enumerated tracked paths -------------------------------
    # Born HEAD: index + worktree back to HEAD. Unborn: HEAD holds nothing, so
    # "back to HEAD" is just dropping the index entry — and only for entries
    # the removal above actually handled.
    if plan["head"]:
        standing = [rel for rel in restore
                    if _is_dir(os.path.join(worktree, rel))]
        for rel in standing:
            # Still a directory, so it holds something the discard was not
            # allowed to remove — an ignored file, a nested repository, work
            # written later. Restoring would erase it as collateral, so the
            # path is left exactly as it is and reported.
            keep(rel)
        restore = [rel for rel in restore if rel not in set(standing)]
    else:
        restore = [rel for rel in restore if rel in set(removed)]

    # -- revalidate the INDEX immediately before the destructive call -------
    # The snapshot above is taken at the top of this function and the removal
    # loop then runs for as many entries as the plan holds. A `git add`
    # landing in that window is invisible to a read taken before it, so the
    # restore threw a blob away that no gate had seen — a check-then-act gap
    # of exactly the shape already closed twice on the worktree (SC-124). The
    # index therefore gets its own re-read HERE, at the same point the
    # worktree half is re-read: the last instant before the command that
    # destroys it.
    #
    # Only the INDEX half is re-checked, deliberately. The filesystem half
    # cannot be: on an unborn HEAD the removal above unlinked these very
    # files, so re-reading it would compare "absent" against the observed
    # content and keep everything the discard just did the work for.
    # Unreadable now -> nothing is verifiable, so nothing is restored over.
    try:
        fresh_index = _index_identities(worktree)
    except _GitEvidenceUnavailable:
        fresh_index = None
    still = []
    for rel in restore:
        if fresh_index is not None \
                and fresh_index.get(rel, _INDEX_ABSENT) == plan["index"][rel]:
            still.append(rel)
        else:
            keep(rel)
    restore = still

    if restore:
        # --no-recurse-submodules is PINNED, not left to config. A submodule is
        # a third store — its own worktree, index and refs — and none of it is
        # inside this fence: the host sees a gitlink and a directory, neither
        # of which moves when work is committed inside the submodule. With
        # `submodule.recurse` set, `git restore` follows the gitlink and
        # resets that store too (reproduced: an inner commit and its files
        # erased). The SC-103 `standing` guard happens to keep a checked-out
        # submodule as well, since it is always a directory — but that guard is
        # about collateral, not about submodules, and a consented blast radius
        # must not depend on a coincidence or on the operator's config.
        #
        # --ignore-skip-worktree-bits is the other half of taking those bits
        # seriously. Without it git filters our OWN pathspec down to
        # non-sparse entries, and a staged-new path carrying skip-worktree
        # matches neither HEAD nor the filtered index — so the command fails
        # `pathspec did not match`, taking the restore of every other
        # consented entry down with it. It cannot widen anything: the pathspec
        # is the enumerated set, read from a file, and this only stops git
        # discarding members of it. (A sparse checkout's excluded paths never
        # reach here at all — they do not differ from HEAD, so nothing
        # enumerates them.)
        args = ["restore", "--source=HEAD", "--no-recurse-submodules",
                "--ignore-skip-worktree-bits", "--staged", "--worktree"] \
            if plan["head"] else ["rm", "--cached", "--force", "--quiet"]
        try:
            out = subprocess.run(
                ["git", "-C", worktree, *args, "--pathspec-from-file=-",
                 "--pathspec-file-nul"],
                input=b"".join(rel.encode("utf-8", "surrogateescape") + b"\0"
                               for rel in restore),
                capture_output=True, timeout=60, check=False)
        except Exception as exc:  # noqa: BLE001 — timeout etc: report it
            result["failed"] = {"step": "restore", "error": str(exc)[:200]}
            return
        if out.returncode != 0:
            result["failed"] = {
                "step": "restore",
                "error": out.stderr.decode("utf-8", "replace").strip()[-200:]}
            return
    result["completed"].append("restore")
    # The restore's exit code says it ran, not that it worked (SC-127), and
    # "it moved" is not the promise either (SC-129). Verify each entry against
    # the contract before any of it is reported as discarded.
    skipped = _unrestored(worktree, plan, restore)
    for rel in skipped:
        keep(rel)
    # Every entry the restore RAN OVER gets its bits put back, including the
    # ones just reported kept: the command clears them whether or not it went
    # on to do what it promised, and an entry reported as spared whose durable
    # bit was silently dropped is spared in name only (SC-125's rule, applied
    # to the set the command touched rather than the set it satisfied).
    result["flags_lost"] = _restore_index_flags(worktree, plan, restore)
    # A surviving DIRECTORY does not make the discard incomplete. It stands
    # because it holds something outside the delete set — an ignored file, a
    # nested repository, work written after the confirmation — and `clean -fd`
    # left exactly those standing too. Reporting it as a failure would cry wolf
    # on a very common shape (a build directory, `__pycache__`) and bury the
    # signal that matters: a FILE the discard was confirmed to erase and did
    # not, which is the only way consented work outlives a discard. Both are
    # named in `kept`; only the second moves `discarded`.
    result["discarded"] = not kept_files
    return


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
        # Says what the system DOES, which stopped being "refuse everything"
        # (SC-107). An operator told recovery is impossible does not attempt
        # it, so the shell stays stranded on the strength of the sentence
        # rather than the code — the objective failing through the projection.
        # Canonical, so browser and CLI both inherit the correction.
        worktree_value = (
            f"state could not be observed completely ({git['indeterminate']})"
            " · discard declined · recover with files preserved (the default)"
            " to free the shell — every file is left untouched")
    elif git:
        tracked = git.get("dirty_tracked")
        untracked = git.get("untracked")
        untracked_dirs = git.get("untracked_dirs")
        index_only = git.get("index_only")
        if all(isinstance(n, int)
               for n in (tracked, untracked, untracked_dirs, index_only)):
            cleanliness = "clean" if not (tracked or untracked
                                          or untracked_dirs) else "not clean"
            # The enumerated entries are rendered BY EFFECT, because the
            # operator's consent is what makes destroying them legitimate and
            # they cannot consent to what they cannot see. An index-only entry
            # is the class with no appearance on disk at all — it read as an
            # ordinary dirty file, so a discard destroying staged work looked
            # byte-identical to one deleting a file the operator could see
            # (SC-130). Named here, in the one projection both clients render.
            staged = (f" ({index_only} of them staged-only: content held in "
                      "the git index that the working tree does not show — a "
                      "discard destroys it)") if index_only else ""
            worktree_value = (
                f"{cleanliness} · {tracked} tracked{staged} · "
                f"{untracked} untracked file(s) · "
                f"{untracked_dirs} untracked dir(s) · "
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
_VOLATILE_GIT_KEYS = ("worktree", "branch", "dirty_tracked", "index_only",
                      "untracked", "untracked_dirs", "unpushed_commits",
                      "change_digest", "indeterminate")


def _volatile_git(git: dict | None) -> dict | None:
    """The worktree facts the fence binds — every key, so a gap
    (`indeterminate`) is a value like any other and can never read as absence."""
    return {k: git.get(k) for k in _VOLATILE_GIT_KEYS} if git is not None \
        else None


def _volatile_evidence(evidence: dict) -> dict:
    """The safety-relevant facts that live OUTSIDE the database and govern
    EVERY recovery: the exact process identity and its liveness, and pane/tmux
    membership. The preview showed these to the operator, so the decision is
    only valid while they still hold — a changed pid_state or a vanished pane
    must force a fresh preview rather than ride the old one into a signal.

    The WORKTREE is deliberately not here (SC-106). It governs the optional
    discard escalation and nothing else, so it is compared separately, only
    when a discard was asked for (`_assert_fresh`) and again at the destructive
    gate (`_assert_worktree_unchanged`). Binding it here instead coupled plain
    recovery — which touches no file at all — to the readability of a worktree
    the operator asked us to LEAVE ALONE, and left a shell whose lock was
    PROVEN absent stranded because a file had moved or `.git` was corrupt.
    Failing closed is right for destruction; for availability it is the bug.
    """
    process = evidence.get("process") or {}
    return {"process": {k: process.get(k) for k in _VOLATILE_PROCESS_KEYS},
            "tmux": evidence.get("tmux")}


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
    """Fail closed on incomplete worktree evidence — for a DISCARD only.

    A gap is deterministic — the same unreadable path or undecodable git output
    yields the same absent facts at preview and at execute — so it would
    compare EQUAL and ride through as "nothing changed" while the work behind
    it was rewritten (SC-087). Absence of evidence is never evidence of safety
    when something is about to be destroyed.

    It is not evidence of danger either. Callers must not reach here for a
    plain recovery: that touches no file, so an unreadable worktree tells it
    nothing, and refusing on one strands a shell whose lock is proven absent
    (SC-106).
    """
    reason = (evidence.get("git") or {}).get("indeterminate")
    if not reason:
        return
    raise RecoveryError(
        409, "recovery_observation_stale",
        f"the worktree could not be observed completely at {when} ({reason}) "
        "— the DISCARD is refused before any signal, closure or file removal. "
        "Recover without discard_worktree to free the shell and leave every "
        "file untouched, or repair the repository and preview again",
        {"observation_id": observation_id, "detail": reason})


def _assert_fresh(con, shell_id: int, observation_id: str, stored: dict,
                  fingerprint: str, default_worktree: str | None, *,
                  discard: bool) -> None:
    """Re-gather the evidence (a pure read) and refuse unless what this
    recovery actually depends on still matches the preview. Nothing has been
    signalled, closed or removed when this runs — it is the last precondition,
    deliberately placed after every other one so the check-then-act gap is as
    small as the sequence can make it (SC-091).

    Two tiers, and keeping them apart is the point (SC-106). The durable rows
    and the process/pane identity gate EVERY recovery: they are what a signal
    and a closure act on. The WORKTREE gates only the discard — it is the one
    thing a discard destroys and the one thing a plain recovery never touches.
    Checking it for both meant a corrupt `.git` or a moved file refused to free
    a shell whose lock was already proven absent, which is the opposite of what
    a recovery is for.
    """
    fresh_evidence = gather(con, shell_id, default_worktree)
    if fingerprint != _fingerprint(con, shell_id, fresh_evidence):
        raise RecoveryError(
            409, "recovery_observation_stale",
            "the shell's state changed since the preview — its durable rows or "
            "its process/pane identity no longer match what the preview "
            "showed; preview again",
            {"observation_id": observation_id})
    if not discard:
        return
    _assert_no_gap(observation_id, fresh_evidence, "now")
    if _volatile_git(fresh_evidence.get("git")) \
            != _volatile_git(stored.get("git")):
        raise RecoveryError(
            409, "recovery_observation_stale",
            "the worktree changed since the preview — the discard is refused "
            "before any signal, closure or file removal; preview again to see "
            "the new state and confirm the discard against it",
            {"observation_id": observation_id})


def _assert_worktree_unchanged(observation_id: str, stored: dict,
                               worktree: str, signal_result) -> dict:
    """The last gate before the discard — and the source of the set the
    discard is allowed to touch.

    Why a SECOND gate exists at all: `_assert_fresh` is the last thing before
    the signal, but the signal is what makes a shell shut down, and a shell can
    WRITE while it shuts down — the file appears after the fence passed and the
    clean erases it (SC-091). Every other fact the fence binds (pid state, pane
    membership, the durable rows) this recovery has by now deliberately
    changed, so re-checking them is meaningless. The worktree is the one piece
    of evidence a recovery must NOT change — and the only piece a discard
    destroys. So that is what is re-read here, immediately before the delete.

    The read itself is stable: each path is observed self-consistently and the
    whole observation must repeat identically before it counts
    (`_observe_stable`), so a write landing DURING this gate's own read no
    longer produces a torn answer that compares equal to the preview (SC-092).

    Stability is NOT what protects work this gate never saw, and was wrongly
    described as if it were. Two observations detect only what changed BETWEEN
    them: an ordinary file save landing after each pass's own `ls-files` is
    missed by both passes, so the digests agree and the gate concludes
    "unchanged" (SC-100). Reading more times narrows that window; nothing
    closes it, because a read cannot see what does not exist yet.

    So the returned PLAN, not this comparison, is what makes the discard safe.
    The digest matching proves the freshly enumerated set is the set the
    operator consented to; `_discard_worktree_files` then touches that set and
    only that set, so a path created at any point after the observation — during
    this gate, during the `git restore` spawn, during the removal — is not in
    it and survives. The race is bounded away rather than won.

    NOT ATOMIC, and still not claimed to be. A git worktree is not
    transactional and no filesystem offers "remove only if unchanged", so what
    remains open is narrower and different in kind:

      an entry the observation DID enumerate — a path the operator was shown
      and confirmed erasing — can be rewritten in the moment between its
      identity being re-checked and its removal, and that rewrite is lost.
      Everything outside the enumerated set is unaffected.

    What bounds that is not a lock but the sequence: the exact process this
    recovery targeted was proven dead via /proc before we got here, so the
    writer this protects against is already gone. A DIFFERENT process writing
    into the worktree was never inside the observation's scope. Operator
    remedy, and the only one: nothing else may be writing to the worktree while
    a discard runs — if something is (an editor, a build, a second agent), stop
    it first, or recover with the worktree preserved and clean up by hand.

    The refusal is honest about what already happened — and about what did
    NOT: it names the signal only when a signal was actually sent, because a
    stale durable lock has no process to signal and reporting one anyway is
    the same dishonesty in the opposite direction.
    """
    fresh, plan = _observe_stable(worktree)
    if plan is not None \
            and _volatile_git(fresh) == _volatile_git(stored.get("git")):
        return plan
    if signal_result:
        performed = (f"The exact process (PID {signal_result.get('pid')}) was "
                     "signalled and the durable state was closed before this "
                     "point")
    else:
        performed = ("NO process was signalled — there was none to signal, "
                     "the durable lock was stale; the durable state was "
                     "closed before this point")
    raise RecoveryError(
        409, "recovery_observation_stale",
        "the worktree changed after the freshness fence — the discard is "
        f"refused and NOTHING was reset or cleaned. {performed} (that is the "
        "recovery itself, and it cannot be unwound); the files are untouched. "
        "Preview again to see the new state and confirm the discard against "
        "it.",
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


def _abandon_runtime(abandon, evidence: dict) -> dict | None:
    """Drop the live runtime generation now that the durable state is closed.
    `None` when there was nothing to abandon.

    Best-effort by design — the closure is already committed and a runtime
    that will not let go is not something to fail a recovery over — but
    REPORTED, which it was not. A swallowed failure here is post-commit
    misreporting in the one shape that never produces a 500: the response says
    the shell is available while a generation may still be attached to it, and
    the operator has nothing to act on. Same rule as everywhere else in this
    unit — handle it where something can be done about it, and name it either
    way (SC-128).
    """
    if abandon is None or not evidence["live_session"] \
            or not evidence["session"]:
        return None
    try:
        abandon(evidence["session"]["session_id"])
    except Exception as exc:  # noqa: BLE001 — post-commit: report, never raise
        return {"abandoned": False,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"abandoned": True}


def _discard_after_closure(observation_id: str, evidence: dict, worktree: str,
                           signal_result) -> dict:
    """The late gate AND the discard, under one result-producing boundary.

    `_discard_worktree_files` promises never to raise (SC-126), and that
    promise was read as covering the post-commit path. It covered the sequence
    inside it: `_assert_worktree_unchanged` runs after the same durable commit,
    can throw for reasons that are not its refusal — an unreadable worktree,
    a git that will not spawn — and threw straight past the structure into a
    500 with the session already ended (SC-128).

    So the boundary belongs at the seam between "the durable state is closed"
    and "the files are dealt with", not around one of the two helpers on the
    far side of it. The gate's own `RecoveryError` still leaves: it is the
    honest refusal, and states both the closure it performed and that nothing
    was discarded. Anything else becomes a discard result naming the step that
    broke, which at this point is all the operator can act on.
    """
    try:
        plan = _assert_worktree_unchanged(observation_id, evidence, worktree,
                                          signal_result)
    except RecoveryError:
        raise
    except Exception as exc:  # noqa: BLE001 — post-commit: report, never raise
        result = _discard_result(worktree)
        result["failed"] = {"step": "worktree_gate",
                            "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return result
    return _discard_worktree_files(worktree, plan)


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
    # first: an observation whose WORKTREE could not be gathered whole cannot
    # authorise a discard no matter what the live gates below would say, and
    # the operator needs that reason rather than whichever live git call the
    # same broken repo trips next. Only for a discard, though — a recovery that
    # touches no file has no business asking whether the files were readable
    # (SC-106).
    if discard:
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
                  default_worktree, discard=discard)

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
            # Two ways to reach here and the operator acts on them
            # differently: the process outlived the signals, or the sequence
            # itself broke after delivering one. Both are refusals — nothing
            # closed, nothing discarded — and both say so rather than letting
            # the second escape as an opaque 500 over an irreversible
            # signal (SC-131).
            failed = signal_result.get("reason") == "signal_failed"
            raise RecoveryError(
                409, "recovery_absence_unproven",
                ("a signal was delivered and the termination sequence then "
                 f"failed in {signal_result.get('phase')} "
                 f"({signal_result.get('error')}) — absence never proven, so "
                 "no session, archive, alert or binding was closed and "
                 "nothing was reset or cleaned. "
                 if failed else
                 "a signal was sent but /proc never proved the process gone "
                 "— durable closure refused (closure only on proven "
                 "absence). ") +
                "Next action: preview again; if the process persists, "
                f"inspect /proc/{proc['pane_pid']} and resolve it at the OS "
                "level first",
                {"pid": proc["pane_pid"], "signaled": True,
                 "escalated": signal_result.get("escalated"),
                 "phase": signal_result.get("phase"),
                 "error": signal_result.get("error"),
                 "closed": False, "discarded": False,
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
    except Exception as exc:
        # The rollback makes the DURABLE half a clean no-op — and says nothing
        # about the half that cannot be rolled back. By here the exact process
        # has been signalled and proven dead, so a bare 500 leaves the operator
        # with a shell whose rows still read live and whose process is gone,
        # and no way to tell that from a recovery that never started. Same rule
        # as the post-commit paths, one step earlier: an irreversible action
        # already performed is NAMED, whichever direction the failure goes
        # (SC-128's category, taken to the whole sequence rather than to the
        # side of the commit it was found on).
        con.rollback()
        raise RecoveryError(
            500, "recovery_closure_failed",
            "the durable closure failed and was rolled back — no session, "
            "archive, alert or binding was changed, and NOTHING was reset or "
            "cleaned. " + (
                f"The exact process (PID {signal_result.get('pid')}) was "
                "already signalled and proven dead (that cannot be unwound), "
                "so the shell's rows still describe a process that is gone; "
                "recover it again to close them."
                if signal_result else
                "No process was signalled. Recover again."),
            {"observation_id": observation_id, "signaled": signal_result,
             "closed": False, "discarded": False,
             "error": f"{type(exc).__name__}: {str(exc)[:200]}"}) from exc
    # -- past the point of no return: NOTHING below may become a 500 --------
    # The durable state is committed and cannot be unwound, and for a discard
    # the operator's files are about to be — or by now already have been —
    # touched. An exception escaping from here reaches the route as an opaque
    # internal error whose body says which of those happened: nothing. That is
    # the worst report there is, and decision #45 ranks it with the
    # destruction itself. SC-126 gave that guarantee to the discard sequence;
    # SC-128 was the identical defect one call EARLIER, in the late gate,
    # which sat outside the structure built for it. So the boundary is drawn
    # around the whole tail rather than around another helper: the response is
    # assembled HERE, out of values already in hand, and every step after it
    # only fills one of its fields in. Three things can still go wrong past
    # this line and each returns rather than raises — the runtime abandon, the
    # late gate, and the discard — the single exception being the gate's own
    # refusal, which is not opaque: it names the closure it performed and that
    # nothing was discarded.
    result = {"shell_id": shell_id, "shortname": shortname,
              "classification": classification, "mode": mode,
              "signaled": signal_result,
              "closed": changed,
              "worktree": {"preserved": True},
              "unread_messages": evidence["unread_messages"],
              "availability": "available"}
    changed["runtime"] = _abandon_runtime(abandon, evidence)
    if discard:
        result["worktree"] = _discard_after_closure(
            observation_id, evidence, worktree, signal_result)
    return result
