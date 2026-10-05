#!/usr/bin/env python3
"""Durable local commands: register first, detach, retain outcome and owner wake.

Computation survives harness exit. Service teardown, reboot or container loss
may interrupt it: the ledger and local evidence recover a truthful terminal or
lost result, never rerun the command. No general shell token is written to disk.
Legacy run/jobs status and logs remain readable without retrospective wakes.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

import project_root
from runs import boot_id, terminal_payload

ENGINE = Path(__file__).resolve().parents[1]
JOBS = ENGINE / "run" / "jobs"
RUNS = ENGINE / "run" / "runs"

# API proxy — run.py injects these at boot; the supervisor inherits them.
SC_API_TOKEN = os.environ.get("SC_API_TOKEN", "")
SC_API_BASE = os.environ.get("SC_API_BASE", "")

WAIT_DEFAULT = 300          # `job wait` default slice (seconds)
WAIT_CAP = 550              # hard cap — under harness foreground-timeout limits
KILL_GRACE = 10             # SIGTERM → SIGKILL grace (seconds)
POLL = 2                    # supervisor/wait poll interval (seconds)


def die(msg: str) -> NoReturn:  # noqa: F821
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
    tmp = _meta_path(jobdir).with_suffix(f".{os.getpid()}.tmp")
    with tmp.open('w') as handle:
        handle.write(json.dumps(meta, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, _meta_path(jobdir))


def job_dir(job_id: str) -> Path:
    if Path(job_id).name != job_id or job_id in {".", ".."}:
        die("invalid job id")
    d = RUNS / job_id if job_id.isdecimal() and (RUNS / job_id).is_dir() else JOBS / job_id
    if not d.is_dir():
        die(f"no such job '{job_id}' (see `sc job list --all`)")
    return d


def next_job_id(label: str | None) -> str:
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


def _start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat — the process's start time in clock ticks
    since boot. Together with the pid it names one process incarnation: a
    recycled pid carries a different value. None when the pid is gone or the
    host has no procfs."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _has_procfs() -> bool:
    return Path("/proc/self/stat").exists()


def kill_refusal(meta: dict, pid: int) -> str | None:
    """Why `kill` must not signal `pid`, or None when it is still the job's own
    process. A recorded pid is only an identity while its incarnation matches;
    once the OS recycles it, killpg would hit a stranger's process group (#954
    — a foreign install's watchers died this way). Mismatch, gone, or
    unverifiable all count as 'not ours to kill'."""
    if meta.get("boot_id") and meta["boot_id"] != boot_id():
        return "host boot identity changed; refusing to signal"
    recorded = meta.get("start_ticks")
    live = _start_ticks(pid)
    if live is None:
        if _has_procfs():
            return f"pid {pid} is gone — nothing to kill; the supervisor records the exit"
        return None   # no procfs: nothing to verify against; legacy behavior
    if recorded is None:
        return (f"pid {pid} has no recorded identity (job started before this "
                f"engine version) — refusing to signal it blind; verify and kill it by hand")
    if int(recorded) != live:
        return (f"pid {pid} was recycled by the OS (recorded start {recorded}, live "
                f"{live}) — it is no longer this job's process; nothing killed")
    return None


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

def _api(method: str, path: str, payload: dict | None = None) -> dict:
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


def send_completion(meta: dict, retries: int | None = None, delay: float = 3.0) -> bool:
    """Submit persisted evidence; retry transport outages at a five-minute cap.

    A stopped supervisor needs no token recovery: the runtime reconciler reads
    the registered evidence path and commits the same terminal payload.
    """
    if not (SC_API_TOKEN and SC_API_BASE and meta.get('run_id')):
        return False
    attempt = 0
    jobdir = Path(meta['log']).parent
    while retries is None or attempt < retries:
        attempt += 1
        meta.update(submission_attempt=attempt, submission_attempted_at=_now())
        try:
            receipt = _api('POST', f"/_sc/runs/{meta['run_id']}/terminal", terminal_payload(meta))
            meta.update(wake_id=receipt['wake_id'], message_id=receipt['message_id'],
                        wake_state=receipt['wake_state'], submission_error=None)
            write_meta(jobdir, meta)
            return True
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
            # Do not persist response bodies, headers or credentials.
            meta['submission_error'] = f'{type(exc).__name__}: submission unavailable'
            write_meta(jobdir, meta)
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                return False  # reconciler owns recovery after token expiry/conflict
            if retries is None or attempt < retries:
                time.sleep(min(300, delay * 2 ** min(attempt - 1, 10)))
    return False


# ── the supervisor (detached; the part that survives the session) ────────────

def supervise(jobdir: Path, notify=send_completion) -> int:
    """Run the job to completion: spawn the command as its own process group,
    stream output to log, enforce the timeout, record the exit, send the
    wake-up. `notify` is injectable for tests."""
    meta = read_meta(jobdir)
    meta["supervisor_pid"] = os.getpid()
    meta["supervisor_start_ticks"] = _start_ticks(os.getpid())
    meta["boot_id"] = boot_id()
    write_meta(jobdir, meta)
    if meta.get('run_id'):
        try:
            _api('POST', f"/_sc/runs/{meta['run_id']}/running", {
                k: meta.get(k) for k in ('supervisor_pid', 'supervisor_start_ticks',
                                        'boot_id', 'started_at')})
        except (urllib.error.URLError, OSError):
            meta.update(finished_at=_now(), exit_code=127,
                        spawn_error='registration check-in unavailable; command not launched')
            write_meta(jobdir, meta)
            notify(meta)
            return 127

    log = open(jobdir / "log", "ab", buffering=0)
    try:
        child = subprocess.Popen(
            meta["cmd"], cwd=meta.get("cwd") or None,
            env=project_root.scrubbed(),
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        meta.update(finished_at=_now(), exit_code=127, spawn_error=str(e))
        write_meta(jobdir, meta)
        log.close()
        notify(meta)
        return 127

    meta["pid"] = child.pid
    meta["start_ticks"] = _start_ticks(child.pid)
    write_meta(jobdir, meta)
    if meta.get('run_id'):
        try:
            _api('POST', f"/_sc/runs/{meta['run_id']}/running", {
                k: meta.get(k) for k in ('pid', 'start_ticks', 'supervisor_pid',
                                        'supervisor_start_ticks', 'boot_id', 'started_at')})
        except (urllib.error.URLError, OSError):
            pass  # local identity is durable; the runtime pulse recovers it

    timeout = meta.get("timeout")
    deadline = time.monotonic() + timeout if timeout else None
    timed_out = False
    while True:
        try:
            rc = child.wait(timeout=POLL)
            break
        except subprocess.TimeoutExpired:
            if (jobdir / 'kill_requested').exists():
                _kill_group(child.pid)
                rc = child.wait()
                break
            if deadline and time.monotonic() >= deadline:
                timed_out = True
                _kill_group(child.pid)
                rc = child.wait()
                break

    # Re-read before the final write: `kill` may have stamped killed=True on
    # disk while we held a stale copy — never clobber it.
    meta = read_meta(jobdir) or meta
    meta.update(finished_at=_now(), exit_code=rc, timed_out=timed_out)
    if (jobdir / 'kill_requested').exists():
        meta['killed'] = True
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
    if args.timeout is not None and args.timeout <= 0:
        die('--timeout must be positive seconds')
    if not (SC_API_TOKEN and SC_API_BASE):
        die('authenticated shell API is required; no command launched')
    cwd = str(project_root.invocation_cwd())
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                            capture_output=True, text=True, check=False)
    try:
        registered = _api('POST', '/_sc/runs', {
            'registration_key': uuid.uuid4().hex, 'kind': 'job', 'label': args.label,
            'argv': cmd, 'cwd': cwd, 'commit': commit.stdout.strip() or None})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'registration failed ({type(exc).__name__}); no command launched')
    job_id = str(registered['run_id'])
    jobdir = Path(registered['evidence_path']).parent
    jobdir.mkdir(parents=True, exist_ok=False)
    (jobdir / 'log').touch()
    write_meta(jobdir, {
        'run_id': registered['run_id'], 'job_id': job_id,
        'label': args.label, 'cmd': cmd, 'cwd': cwd,
        'timeout': args.timeout, 'started_at': _now(), 'log': str(jobdir / 'log'),
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
        started = read_meta(jobdir)
        if started.get('pid') or started.get('finished_at'):
            break
        time.sleep(0.05)
    else:
        die(f'supervisor startup unconfirmed; registered run {job_id} will reconcile; '
            f'inspect `sc job status {job_id}` before starting dependent work')
    print(f"job: {job_id} started (supervisor pid {sup.pid}) — "
          f"end your turn; completion wakes you "
          f"(TUI delivery waits for the CLI slot). `sc job status {job_id}`")
    return 0


def read_api(path: str) -> dict | None:
    """Read remotely when reachable; authorization failures never fall back."""
    if not (SC_API_TOKEN and SC_API_BASE):
        return None
    try:
        return _api('GET', path)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        die(f'run access refused (HTTP {exc.code})')
    except (urllib.error.URLError, OSError):
        print('job: API unreachable; using local evidence (unauthoritative)', file=sys.stderr)
        return None


def local_notice():
    print('job: local evidence (unauthoritative; wake delivery unconfirmed)', file=sys.stderr)


def cmd_list(args) -> int:
    emitted = False
    result = read_api('/_sc/runs')
    if result is not None:
        for row in result['runs']:
            if args.all or row['state'] in {'registered', 'running'} or row['wake_state'] == 'blocked':
                emitted = True
                print(f"  {row['run_id']:<20} {row['state']:<10} wake={row['wake_state']} {row['label']}")
    roots = [JOBS] if result is not None else [RUNS, JOBS]
    for root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda p: p.name):
            meta = read_meta(directory)
            if not meta:
                continue
            st = state_of(meta)
            if not args.all and st not in ('running', 'lost') and not meta.get('submission_error'):
                continue
            emitted = True
            dur = _dur(meta.get('started_at', ''), meta.get('finished_at') or _now())
            print(f"  {directory.name:<20} {st:<8} {dur:>8}  local evidence (unauthoritative) "
                  f"{' '.join(meta.get('cmd', []))[:60]}")
            for key in ('submission_error', 'submission_attempted_at', 'submission_attempt'):
                if meta.get(key) is not None:
                    print(f"    {key}: {meta[key]}")
    if not emitted:
        print('job: none live' + ('' if args.all else ' (--all includes finished)'))
    return 0


def ledger_run(job_id: str) -> dict | None:
    return read_api(f'/_sc/runs/{job_id}') if job_id.isdecimal() else None


def cmd_status(args) -> int:
    row = ledger_run(args.id)
    if row is not None:
        print(json.dumps(row, indent=2))
        return 1 if row['state'] == 'lost' else 0
    meta = read_meta(job_dir(args.id))
    local_notice()
    st = state_of(meta)
    print(f"job {meta.get('job_id')}: {st}")
    for k in ("label", "cmd", "cwd", "pid", "supervisor_pid", "started_at",
              "finished_at", "exit_code", "timed_out", "killed", "timeout",
              "spawn_error", "log", "submission_error", "submission_attempted_at",
              "submission_attempt"):
        v = meta.get(k)
        if v is not None and v is not False:   # `v not in (None, False)` hides exit_code=0
            print(f"  {k}: {v if not isinstance(v, list) else ' '.join(v)}")
    if st == "lost":
        print("  ! supervisor died without recording an exit (reboot/SIGKILL) —"
              " check the log; the job may or may not have finished its work.")
    return 0 if st != "lost" else 1


def cmd_tail(args) -> int:
    result = read_api(f'/_sc/runs/{args.id}/tail') if args.id.isdecimal() else None
    if result is not None:
        print('\n'.join(result['text'].splitlines()[-args.n:]))
        return 0
    log = job_dir(args.id) / "log"
    if not log.exists():
        die(f"no log for job '{args.id}'")
    local_notice()
    lines = log.read_bytes().decode(errors="replace").splitlines()
    for line in lines[-args.n:]:
        print(line)
    return 0


def cmd_wait(args) -> int:
    """Bounded foreground wait — THE wait-slice primitive. Exit 0 = finished
    (status line printed) · 2 = still running after the slice (drain your
    inbox, then slice again) · 1 = no such job / lost."""
    slice_s = min(args.for_seconds or WAIT_DEFAULT, WAIT_CAP)
    deadline = time.monotonic() + slice_s
    row = ledger_run(args.id)
    if row is not None:
        while True:
            row = ledger_run(args.id)
            if row is None:
                break  # API went away while waiting; inspect persisted evidence.
            if row['state'] not in {'registered', 'running'}:
                print(json.dumps(row, indent=2))
                return 1 if row['state'] == 'lost' else 0
            if time.monotonic() >= deadline:
                print(f"job {args.id}: still running; end the turn and await its owner wake")
                return 2
            time.sleep(POLL)
    jobdir = job_dir(args.id)
    local_notice()
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
    if ledger_run(args.id) is not None:
        try:
            print(json.dumps(_api('POST', f'/_sc/runs/{args.id}/kill', {})))
        except urllib.error.HTTPError as exc:
            die(f'kill refused (HTTP {exc.code})')
        except (urllib.error.URLError, OSError):
            die('API unreachable; cancellation unconfirmed; inspect status before retrying')
        return 0
    jobdir = job_dir(args.id)
    if jobdir.parent == RUNS:
        die('ledger cancellation requires the API; inspect local status until it recovers')
    meta = read_meta(jobdir)
    if is_finished(meta):
        die(f"job '{args.id}' already finished ({state_of(meta)})")
    pid = meta.get("pid")
    if not pid:
        die(f"job '{args.id}' has no recorded pid yet — try again in a moment")
    refusal = kill_refusal(meta, int(pid))
    if refusal:
        die(f"job '{args.id}': {refusal}")
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
        description="Registered jobs survive harness exit and durably wake their owner.",
        epilog="Service teardown/reboot may interrupt computation; recovery records lost "
               "when no exit evidence survives. Commands are never automatically rerun. "
               "TUI wakes wait out the CLI lock. Legacy job status/logs remain readable.")
    sub = p.add_subparsers(dest="cmd_name", required=True)

    sp = sub.add_parser("start", help="register and detach; completion durably wakes the owner")
    sp.add_argument("--label", help="display label (never used in a filesystem path)")
    sp.add_argument("--timeout", type=int,
                    help="kill the whole process group after N seconds")
    sp.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- <command and args>")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("list", help="live jobs (--all includes finished)")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("status", help="state, exit, paths; local evidence when API unreachable")
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


def main(argv: list[str]) -> int:
    # Early-closed stdout is handled at the entrypoint (cli_entry.run_cli, #384).
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    from cli_entry import run_cli

    sys.exit(run_cli(main, sys.argv[1:]))
