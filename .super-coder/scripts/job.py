#!/usr/bin/env python3
"""sc job — session-surviving local job runner (specs_sc/job-runner.md).

A harness background task is session-scoped: in a headless (-p) boot it dies
with the session, silently. `sc job` runs a long local command — a suite, a
bench, a build — as a detached, supervised process that survives the session
that started it, and posts ONE completion message (`result` row) to the
starting shell's own inbox, so the existing eventing loop (inbox watcher,
headless boots on message rows) covers local long jobs the way it already
covers PR transitions.

    ./sc job start [--label <slug>] [--timeout <sec>] -- <cmd ...>
    ./sc job list [--all]
    ./sc job status <id>
    ./sc job tail <id> [-n N]
    ./sc job wait <id> [--for <sec>]     bounded foreground wait (wait-slice)
    ./sc job kill <id>

State: <engine>/run/jobs/<id>/ — meta.json + log. No DB surface except the
completion message, sent through the API with the token the environment
carried at `start` (the `sc mem` doctrine: shell-side writes go through the
API, stamped with a dedupe_key so a retry never double-sends). If the API is
unreachable at completion the supervisor retries briefly and gives up —
meta.json still holds the result; the row is the fast path, never the only
path.

The supervisor (`job.py _supervise <dir>`, spawned with start_new_session)
is the job's parent: it survives the harness session, streams the child's
output to the log, enforces --timeout on the whole process group, records
the exit, and sends the wake-up.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
JOBS = ENGINE / "run" / "jobs"

# API proxy — run.py injects these at boot; the supervisor inherits them.
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE = os.environ.get("SC_API_BASE", "")

WAIT_DEFAULT = 300          # `job wait` default slice (seconds)
WAIT_CAP = 550              # hard cap — under harness foreground-timeout limits
KILL_GRACE = 10             # SIGTERM → SIGKILL grace (seconds)
POLL = 2                    # supervisor/wait poll interval (seconds)


def die(msg: str) -> "NoReturn":  # noqa: F821
    sys.exit(f"job: {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dur(started: str, finished: str) -> str:
    """Human duration between two _now() stamps — best-effort."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        secs = int((datetime.strptime(finished, fmt)
                    - datetime.strptime(started, fmt)).total_seconds())
    except ValueError:
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


# ── meta.json — the job's one record ─────────────────────────────────────────

def _meta_path(jobdir: Path) -> Path:
    return jobdir / "meta.json"


def read_meta(jobdir: Path) -> dict:
    try:
        return json.loads(_meta_path(jobdir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(jobdir: Path, meta: dict) -> None:
    tmp = _meta_path(jobdir).with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(tmp, _meta_path(jobdir))


def job_dir(job_id: str) -> Path:
    d = JOBS / job_id
    if not d.is_dir():
        die(f"no such job '{job_id}' (see `sc job list --all`)")
    return d


def next_job_id(label: "str | None") -> str:
    """Sequential id, readable: '7' or '7-pytest'. The number never repeats
    (max over existing dirs + 1), the label is display sugar."""
    JOBS.mkdir(parents=True, exist_ok=True)
    seqs = [0]
    for d in JOBS.iterdir():
        head = d.name.split("-", 1)[0]
        if head.isdigit():
            seqs.append(int(head))
    n = max(seqs) + 1
    return f"{n}-{label}" if label else str(n)


def is_finished(meta: dict) -> bool:
    return meta.get("finished_at") is not None


def is_running(meta: dict) -> bool:
    """Running = not finished AND the supervisor is still alive. A dead
    supervisor with no finish record is 'lost' (host reboot, SIGKILL) —
    reported, never silently running forever."""
    if is_finished(meta):
        return False
    spid = meta.get("supervisor_pid")
    if not spid:
        return False
    try:
        os.kill(int(spid), 0)
        return True
    except (OSError, ValueError):
        return False


def state_of(meta: dict) -> str:
    if is_finished(meta):
        if meta.get("timed_out"):
            return "timeout"
        if meta.get("killed"):
            return "killed"
        return "done" if meta.get("exit_code") == 0 else "failed"
    return "running" if is_running(meta) else "lost"


# ── completion message (supervisor-side) ─────────────────────────────────────

def _api(method: str, path: str, payload: "dict | None" = None) -> dict:
    url = SC_API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict = {"Authorization": f"Bearer {SC_API_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def completion_body(meta: dict) -> str:
    state = state_of(meta)
    exit_code = meta.get("exit_code")
    dur = _dur(meta.get("started_at", ""), meta.get("finished_at", ""))
    label = meta.get("label") or meta.get("cmd", ["?"])[0]
    return (f"job {meta.get('job_id')} ({label}) {state}"
            f" exit={exit_code} after {dur} — `sc job status"
            f" {meta.get('job_id')}` · log: {meta.get('log')}")


def send_completion(meta: dict, retries: int = 5, delay: float = 3.0) -> bool:
    """One result row to the starting shell's OWN inbox — the wake-up. The
    dedupe_key makes retries safe (#333 doctrine); API down after `retries`
    attempts → give up quietly (meta.json still has the result)."""
    if not (SC_API_TOKEN and SC_API_BASE):
        return False
    for attempt in range(retries):
        try:
            me = _api("GET", "/_sc/mem/whoami")
            _api("POST", "/_sc/mem/messages", {
                "to_shell_id": me["shell_id"],
                "body": completion_body(meta),
                "kind": "result",
                "dedupe_key": f"job-{meta.get('job_id')}-completion",
            })
            return True
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
            time.sleep(delay * (attempt + 1))
    return False


# ── the supervisor (detached; the part that survives the session) ────────────

def supervise(jobdir: Path, notify=send_completion) -> int:
    """Run the job to completion: spawn the command as its own process group,
    stream output to log, enforce the timeout, record the exit, send the
    wake-up. `notify` is injectable for tests."""
    meta = read_meta(jobdir)
    meta["supervisor_pid"] = os.getpid()
    write_meta(jobdir, meta)

    log = open(jobdir / "log", "ab", buffering=0)
    try:
        child = subprocess.Popen(
            meta["cmd"], cwd=meta.get("cwd") or None,
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        meta.update(finished_at=_now(), exit_code=127, spawn_error=str(e))
        write_meta(jobdir, meta)
        notify(meta)
        return 127

    meta["pid"] = child.pid
    write_meta(jobdir, meta)

    timeout = meta.get("timeout")
    deadline = time.monotonic() + timeout if timeout else None
    timed_out = False
    while True:
        try:
            rc = child.wait(timeout=POLL)
            break
        except subprocess.TimeoutExpired:
            if deadline and time.monotonic() >= deadline:
                timed_out = True
                _kill_group(child.pid)
                rc = child.wait()
                break

    # Re-read before the final write: `kill` may have stamped killed=True on
    # disk while we held a stale copy — never clobber it.
    meta = read_meta(jobdir) or meta
    meta.update(finished_at=_now(), exit_code=rc, timed_out=timed_out)
    write_meta(jobdir, meta)
    log.close()
    notify(meta)
    return rc


def _kill_group(pid: int) -> None:
    """SIGTERM the job's process group; SIGKILL what remains after the grace
    period. The group is the child's own session (start_new_session at spawn),
    so a suite's worker processes die with it — no half-dead pytest trees."""
    for sig, wait_s in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def cmd_supervise(args) -> int:
    return supervise(Path(args.jobdir))


# ── verbs ─────────────────────────────────────────────────────────────────────

def cmd_start(args) -> int:
    if not args.cmd:
        die("nothing to run — usage: sc job start [--label x] [--timeout N] -- <cmd ...>")
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else list(args.cmd)
    if not cmd:
        die("nothing to run after --")
    job_id = next_job_id(args.label)
    jobdir = JOBS / job_id
    jobdir.mkdir(parents=True)
    (jobdir / "log").touch()
    write_meta(jobdir, {
        "job_id": job_id,
        "label": args.label,
        "cmd": cmd,
        "cwd": os.getcwd(),
        "timeout": args.timeout,
        "started_at": _now(),
        "log": str(jobdir / "log"),
    })
    # Detach: the supervisor gets its own session so it survives this process,
    # the harness turn, and the harness session itself.
    sup = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_supervise", str(jobdir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    # Only the supervisor writes meta after this point (no lost-update races);
    # wait for its first write so an immediate `status`/`wait` never reads a
    # pre-supervisor meta and mis-calls the job 'lost'.
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if read_meta(jobdir).get("supervisor_pid"):
            break
        time.sleep(0.05)
    else:
        print(f"job: WARNING — supervisor (pid {sup.pid}) has not checked in "
              f"after 5s; `sc job status {job_id}` before trusting it")
    print(f"job: {job_id} started (supervisor pid {sup.pid}) — "
          f"`sc job wait {job_id}` or end the turn; completion lands in "
          f"your inbox as a result row")
    return 0


def cmd_list(args) -> int:
    if not JOBS.is_dir():
        print("job: none")
        return 0
    rows = []
    for d in sorted(JOBS.iterdir(), key=lambda p: p.name):
        meta = read_meta(d)
        if not meta:
            continue
        st = state_of(meta)
        if not args.all and st not in ("running", "lost"):
            continue
        rows.append((d.name, st, meta))
    if not rows:
        print("job: none live" + ("" if args.all else " (--all includes finished)"))
        return 0
    for name, st, meta in rows:
        dur = (_dur(meta.get("started_at", ""), meta.get("finished_at") or _now()))
        print(f"  {name:<20} {st:<8} {dur:>8}  {' '.join(meta.get('cmd', []))[:60]}")
    return 0


def cmd_status(args) -> int:
    meta = read_meta(job_dir(args.id))
    st = state_of(meta)
    print(f"job {meta.get('job_id')}: {st}")
    for k in ("label", "cmd", "cwd", "pid", "supervisor_pid", "started_at",
              "finished_at", "exit_code", "timed_out", "killed", "timeout",
              "spawn_error", "log"):
        v = meta.get(k)
        if v is not None and v is not False:   # `v not in (None, False)` hides exit_code=0
            print(f"  {k}: {v if not isinstance(v, list) else ' '.join(v)}")
    if st == "lost":
        print("  ! supervisor died without recording an exit (reboot/SIGKILL) —"
              " check the log; the job may or may not have finished its work.")
    return 0 if st != "lost" else 1


def cmd_tail(args) -> int:
    log = job_dir(args.id) / "log"
    if not log.exists():
        die(f"no log for job '{args.id}'")
    lines = log.read_bytes().decode(errors="replace").splitlines()
    for line in lines[-args.n:]:
        print(line)
    return 0


def cmd_wait(args) -> int:
    """Bounded foreground wait — THE wait-slice primitive. Exit 0 = finished
    (status line printed) · 2 = still running after the slice (drain your
    inbox, then slice again) · 1 = no such job / lost."""
    jobdir = job_dir(args.id)
    slice_s = min(args.for_seconds or WAIT_DEFAULT, WAIT_CAP)
    deadline = time.monotonic() + slice_s
    while True:
        meta = read_meta(jobdir)
        if is_finished(meta):
            print(f"job: {completion_body(meta)}")
            return 0
        if not is_running(meta):
            print(f"job {args.id}: LOST — supervisor died without recording an "
                  f"exit; check `sc job status {args.id}` and the log.")
            return 1
        if time.monotonic() >= deadline:
            print(f"job {args.id}: still running after {slice_s}s slice — "
                  f"drain your inbox, then `sc job wait {args.id}` again "
                  f"(or end the turn; the completion row wakes you).")
            return 2
        time.sleep(POLL)


def cmd_kill(args) -> int:
    jobdir = job_dir(args.id)
    meta = read_meta(jobdir)
    if is_finished(meta):
        die(f"job '{args.id}' already finished ({state_of(meta)})")
    pid = meta.get("pid")
    if not pid:
        die(f"job '{args.id}' has no recorded pid yet — try again in a moment")
    meta["killed"] = True
    write_meta(jobdir, meta)
    _kill_group(int(pid))
    print(f"job: {args.id} killed (SIGTERM→SIGKILL on the process group) — "
          f"the supervisor records the exit and sends the completion row")
    return 0


# ── arg parsing ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sc job",
        description="session-surviving local job runner (specs_sc/job-runner.md)")
    sub = p.add_subparsers(dest="cmd_name", required=True)

    sp = sub.add_parser("start", help="run a command detached; completion lands in your inbox")
    sp.add_argument("--label", help="short slug for the id + the completion row")
    sp.add_argument("--timeout", type=int,
                    help="kill the whole process group after N seconds")
    sp.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- <command and args>")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("list", help="live jobs (--all includes finished)")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("status", help="one job's state, exit, paths")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("tail", help="last N log lines")
    sp.add_argument("id")
    sp.add_argument("-n", type=int, default=50)
    sp.set_defaults(fn=cmd_tail)

    sp = sub.add_parser("wait", help="bounded foreground wait — exit 0 done · 2 still running")
    sp.add_argument("id")
    sp.add_argument("--for", dest="for_seconds", type=int, default=WAIT_DEFAULT,
                    help=f"slice seconds (default {WAIT_DEFAULT}, cap {WAIT_CAP})")
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser("kill", help="SIGTERM→SIGKILL the job's process group")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_kill)

    sp = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    sp.add_argument("jobdir")
    sp.set_defaults(fn=cmd_supervise)
    return p


def main(argv: "list[str]") -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
