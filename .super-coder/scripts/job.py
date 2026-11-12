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
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import project_root
from runs import (
    AGENT_DEFAULT_TIMEOUT,
    AGENT_MAX_TIMEOUT,
    PROBE_EXCERPT,
    TERMINAL,
    WATCH_ARGV,
    boot_id,
    terminal_payload,
    watch_form,
)

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
PROBE_TIMEOUT = 3600        # probes always have a finite overall deadline
WATCH_PR_EVERY = 60         # `watch pr` default delay between reads (seconds)
AGENT_START_WAIT = 60       # how long `agent` waits for its harness to start
SETTLED_PR_STATES = frozenset({"green", "red", "merged", "closed"})
REPOSITORY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*")


def die(msg: str) -> NoReturn:
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
    if meta.get('kind') == 'probe':
        # A probe has many short-lived children. Its stable, cancellable
        # incarnation is the supervisor, including between attempts.
        meta.update(pid=os.getpid(), start_ticks=meta['supervisor_start_ticks'])
    write_meta(jobdir, meta)
    if meta.get('run_id'):
        try:
            _api('POST', f"/_sc/runs/{meta['run_id']}/running", {
                k: meta.get(k) for k in ('pid', 'start_ticks', 'supervisor_pid', 'supervisor_start_ticks',
                                        'boot_id', 'started_at')})
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 409 \
                    and 'terminal result' in _refused(exc):
                # The run ended before this supervisor arrived (for example it
                # was recorded lost). Nothing is launched and nothing is
                # submitted: the ledger already holds the outcome.
                meta.update(finished_at=_now(), exit_code=127, not_launched=True,
                            spawn_error='the run had already ended when its supervisor '
                                        'checked in; nothing launched')
                write_meta(jobdir, meta)
                return 127
            meta.update(finished_at=_now(), exit_code=127,
                        spawn_error='registration check-in unavailable; command not launched')
            write_meta(jobdir, meta)
            notify(meta)
            return 127

    if meta.get('kind') == 'probe':
        meta['probe_ready'] = True
        write_meta(jobdir, meta)
        if meta.get('watch') == 'timer':
            return supervise_timer(jobdir, meta, notify)
        return supervise_probe(jobdir, meta, notify)
    if meta.get('kind') == 'agent':
        return supervise_agent(jobdir, meta, notify)

    log = open(jobdir / "log", "ab", buffering=0)
    child_environment = project_root.scrubbed()
    child_environment.pop('SC_DEVKIT_RUN_ID', None)
    if meta.get('kind') == 'devkit':
        child_environment['SC_DEVKIT_RUN_ID'] = str(meta['run_id'])
    try:
        child = subprocess.Popen(
            meta["cmd"], cwd=meta.get("cwd") or None,
            env=child_environment,
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


def supervise_agent(jobdir: Path, meta: dict, notify) -> int:
    """Run one subagent: worktree, brief, harness, result, facts, terminal.

    The worktree is created here, under a supervisor that has already checked
    in, so a slow creation is a running run and never a registered row with no
    process. Ending the run, by exit, time limit, stop or a signal to this
    supervisor, signals the harness's whole process group, so no harness
    process outlives its run.
    """
    import job_agent as agent

    plan = meta['agent']
    run_id = meta['run_id']
    worktree = Path(meta['cwd'])
    root = worktree.parents[1]
    base = plan['base_commit']
    continues = plan.get('continues_run_id') is not None

    def not_started(payload: dict) -> None:
        meta.update(finished_at=_now(), exit_code=payload['exit_code'],
                    spawn_error=payload['spawn_error'])
        write_meta(jobdir, meta)
        notify(meta)

    # A continue works in the earlier run's worktree; one removed as unchanged
    # is recreated at the same path, where the harness session was first run.
    if not (continues and worktree.is_dir()):
        created = agent.create_worktree_or_fail(
            run_id, base, plan.get('branch'), submit=not_started,
            root=root, path=worktree, reuse_branch=continues)
        if created is None:
            return 127

    stopped_by: list[int] = []
    previous = {}
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            previous[number] = signal.signal(
                number, lambda received, _frame: stopped_by.append(received))
        except ValueError:
            pass  # not the main thread: the caller keeps its own handlers
    child = None
    rc = 127
    timed_out = False
    spawn_error = None
    log = open(jobdir / 'log', 'ab', buffering=0)
    errors = open(jobdir / agent.STDERR_FILE, 'ab', buffering=0)
    try:
        try:
            argv, brief = agent.materialize_argv(meta['cmd'], jobdir, plan)
            with (open(brief, 'rb') if brief else open(os.devnull, 'rb')) as stdin:
                # The brief is attached before the process starts. Standard
                # output is the harness's report and goes to the log alone, so
                # it can be parsed; standard error is appended when the run ends.
                child = subprocess.Popen(
                    argv, cwd=str(worktree),
                    env=agent.child_environment(run_id, plan=plan),
                    stdout=log, stderr=errors, stdin=stdin, start_new_session=True)
        except (OSError, ValueError) as exc:
            spawn_error = f'subagent not started: {exc}'[:256]
        if child is not None:
            meta['pid'] = child.pid
            meta['start_ticks'] = _start_ticks(child.pid)
            write_meta(jobdir, meta)
            try:
                _api('POST', f"/_sc/runs/{run_id}/running", {
                    k: meta.get(k) for k in ('pid', 'start_ticks', 'supervisor_pid',
                                            'supervisor_start_ticks', 'boot_id', 'started_at')})
            except (urllib.error.URLError, OSError):
                pass  # local identity is durable; the runtime pulse recovers it
            timeout = meta.get('timeout')
            deadline = time.monotonic() + timeout if timeout else None
            while True:
                try:
                    rc = child.wait(timeout=POLL)
                    break
                except subprocess.TimeoutExpired:
                    timed_out = bool(deadline and time.monotonic() >= deadline)
                    if timed_out or stopped_by or (jobdir / 'kill_requested').exists():
                        _kill_group(child.pid, child)
                        rc = child.wait()
                        break
            # The leader is gone; whatever it left in its group goes with it.
            _kill_group(child.pid)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        errors.close()

    message, reported = agent.read_report(
        plan['result'], plan['session'], jobdir / 'log', jobdir, plan.get('session_ref'))
    # A reference that could not be an argument of a later resume is not
    # reported; the run then has none, and a continue is refused.
    session = agent.usable_session(reported or plan.get('session_ref'))
    result_bytes = agent.write_result(jobdir, message)
    stderr_path = jobdir / agent.STDERR_FILE
    if stderr_path.stat().st_size:
        with stderr_path.open('rb') as handle:
            handle.seek(max(0, stderr_path.stat().st_size - agent.STDERR_TAIL))
            log.write(b'\n--- harness standard error ---\n' + handle.read())
    log.close()

    state = agent.worktree_state(worktree, base)
    removed_at = None
    if state['unchanged']:
        try:
            agent.remove_worktree(worktree, root)
            removed_at = _now()
        except RuntimeError:
            pass  # kept; `sc job clean` removes it
    facts = {k: v for k, v in {
        'session_ref': session, 'result_bytes': result_bytes,
        'worktree_removed_at': removed_at}.items() if v is not None}
    # Persisted first: the terminal route recovers them if this post is lost.
    agent.write_facts(jobdir, facts)
    try:
        _api('POST', f"/_sc/runs/{run_id}/facts", facts)
    except (urllib.error.URLError, OSError):
        pass
    outcome = {k: v for k, v in {
        'head_commit': state['head_commit'], 'uncommitted': state['uncommitted'],
    }.items() if v is not None}

    # Re-read before the final write, as for every job: never clobber a marker.
    meta = read_meta(jobdir) or meta
    meta.update(finished_at=_now(), exit_code=rc, timed_out=timed_out,
                agent_outcome=outcome)
    if (jobdir / 'kill_requested').exists():
        meta['killed'] = True
    if spawn_error:
        meta['spawn_error'] = spawn_error
    elif stopped_by:
        meta['spawn_error'] = (f'supervisor stopped by signal {stopped_by[0]}; '
                               'the subagent was stopped with it')
    write_meta(jobdir, meta)
    notify(meta)
    return rc


def supervise_timer(jobdir: Path, meta: dict, notify) -> int:
    """`watch timer`: end `done` when the delay has passed, `killed` when stopped."""
    deadline = time.monotonic() + meta['after']
    stopped = False
    while not stopped and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        stopped = (jobdir / 'kill_requested').exists()
    meta.update(finished_at=_now(), exit_code=None if stopped else 0,
                timed_out=False, killed=stopped)
    write_meta(jobdir, meta)
    notify(meta)
    return 1 if stopped else 0


def probe_stop(jobdir: Path, deadline: float) -> str | None:
    if (jobdir / 'kill_requested').exists():
        return 'killed'
    return 'timeout' if time.monotonic() >= deadline else None


def probe_attempt(jobdir: Path, meta: dict, deadline: float) -> tuple[int, str, str | None]:
    """Drain output without accumulating it; check cancellation even on floods.

    The pipe is nonblocking so a silent probe or a descendant retaining stdout
    cannot hold the supervisor beyond its deadline.
    """
    try:
        child = subprocess.Popen(
            meta['cmd'], cwd=meta['cwd'], env=project_root.scrubbed(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return 127, str(exc)[:PROBE_EXCERPT], 'failed'
    output = bytearray()
    stop = None
    assert child.stdout is not None  # PIPE above always supplies it
    with child.stdout, selectors.DefaultSelector() as selector:
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            stop = probe_stop(jobdir, deadline)
            if stop:
                _kill_group(child.pid, child)
                break
            for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 65536)
                output.extend(chunk[:max(0, PROBE_EXCERPT * 4 - len(output))])
                if not chunk:
                    selector.unregister(key.fileobj)
            if child.poll() is not None:
                # Capture the final buffered prefix, without waiting for EOF
                # from descendants or growing memory with the output volume.
                chunk = child.stdout.read(PROBE_EXCERPT * 4) or b''
                output.extend(chunk[:max(0, PROBE_EXCERPT * 4 - len(output))])
                break
        # Reap the leader before group cleanup, avoiding zombie grace waits.
        rc = child.wait()
        if not stop:
            _kill_group(child.pid)
    return rc, output.decode(errors='replace')[:PROBE_EXCERPT], stop


def supervise_probe(jobdir: Path, meta: dict, notify) -> int:
    deadline = time.monotonic() + meta['timeout']
    attempt = 0
    rc = None
    stop = probe_stop(jobdir, deadline)
    with (jobdir / 'log').open('a', buffering=1) as log:
        while not stop:
            attempt += 1
            rc, excerpt, stop = probe_attempt(jobdir, meta, deadline)
            stop = stop or probe_stop(jobdir, deadline)
            meta['probe_attempt'] = {'number': attempt, 'exit_code': rc, 'excerpt': excerpt}
            # JSON escaping guarantees one physical line, even for binary or
            # multiline output. Raw probe output is never retained elsewhere.
            log.write(json.dumps(meta['probe_attempt']) + '\n')
            log.flush()
            os.fsync(log.fileno())
            write_meta(jobdir, meta)
            if stop or rc == 0:
                break
            next_attempt = time.monotonic() + meta['every']
            while not stop and time.monotonic() < next_attempt:
                time.sleep(min(0.1, max(0, min(next_attempt, deadline) - time.monotonic())))
                stop = probe_stop(jobdir, deadline)
    meta.update(finished_at=_now(), exit_code=rc,
                timed_out=stop == 'timeout', killed=stop == 'killed')
    write_meta(jobdir, meta)
    notify(meta)
    return rc if rc is not None else 1


def _kill_group(pid: int, child: subprocess.Popen | None = None) -> None:
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
            if child is not None:
                child.poll()  # reap our leader so a zombie cannot consume the grace
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def cmd_supervise(args) -> int:
    return supervise(Path(args.jobdir))


# ── verbs ─────────────────────────────────────────────────────────────────────

def cmd_start(args) -> int:
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else list(args.cmd)
    until = getattr(args, 'until', None)
    every = getattr(args, 'every', None)
    if until is not None:
        if getattr(args, 'kind', 'job') == 'devkit':
            die('--kind devkit cannot be combined with --until')
        if not until.strip() or '\0' in until or cmd:
            die('--until requires one nonempty quoted shell command and no positional command')
        cmd = ['/bin/sh', '-c', until]
        if every is not None and not 5 <= every <= sys.maxsize:
            die(f'--every must be between 5 and {sys.maxsize} seconds')
        every = every if every is not None else 30
        if args.timeout is None:
            args.timeout = PROBE_TIMEOUT
    elif every is not None:
        die('--every requires --until')
    if not cmd:
        die('nothing to run — use -- <command and args> or --until "<probe command>"')
    if args.timeout is not None and not 0 < args.timeout <= sys.maxsize:
        die(f'--timeout must be positive seconds (maximum {sys.maxsize})')
    if not (SC_API_TOKEN and SC_API_BASE):
        die('authenticated shell API is required; no command launched')
    cwd = str(project_root.invocation_cwd())
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                            capture_output=True, text=True, check=False)
    try:
        registered = _api('POST', '/_sc/runs', {
            'registration_key': uuid.uuid4().hex, 'kind': 'probe' if until is not None else getattr(args, 'kind', 'job'), 'label': args.label,
            'argv': cmd, 'cwd': cwd, 'commit': commit.stdout.strip() or None,
            'timeout_seconds': args.timeout})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            die(f'refused ({_refused(exc)}); no command launched')
        die(f'registration failed ({type(exc).__name__}); no command launched')
    job_id = str(registered['run_id'])
    jobdir = Path(registered['evidence_path']).parent
    jobdir.mkdir(parents=True, exist_ok=False)
    (jobdir / 'log').touch()
    write_meta(jobdir, {
        'run_id': registered['run_id'], 'job_id': job_id,
        'kind': registered['kind'],
        'label': args.label, 'cmd': cmd, 'cwd': cwd,
        'every': every,
        'timeout': args.timeout, 'started_at': _now(), 'log': str(jobdir / 'log'),
    })
    sup = _detach_supervisor(jobdir)
    # Only the supervisor writes meta after this point (no lost-update races);
    # wait for its first write so an immediate `status`/`wait` never reads a
    # pre-supervisor meta and mis-calls the job 'lost'.
    end = time.monotonic() + 5
    while time.monotonic() < end:
        started = read_meta(jobdir)
        ready = started.get('probe_ready') if until is not None else started.get('pid')
        if ready or started.get('finished_at'):
            break
        time.sleep(0.05)
    else:
        die(f'supervisor startup unconfirmed; registered run {job_id} will reconcile; '
            f'inspect `sc job status {job_id}` before starting dependent work')
    print(f"job: {job_id} started (supervisor pid {sup.pid}) — "
          f"end your turn; completion wakes you "
          f"(TUI delivery waits for the CLI slot). `sc job status {job_id}`")
    return 0


def _refused(exc: Exception) -> str:
    """The engine's own words for a refusal, when it sent any."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            reason = json.loads(exc.read()).get('error')
        except (OSError, ValueError, AttributeError):
            reason = None
        return f'HTTP {exc.code}: {reason}' if reason else f'HTTP {exc.code}'
    return type(exc).__name__


def _detach_supervisor(jobdir: Path) -> subprocess.Popen:
    # Detach: the supervisor gets its own session so it survives this process,
    # the harness turn, and the harness session itself.
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_supervise", str(jobdir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)


def _brief_text(args) -> str:
    if args.brief is not None:
        return args.brief
    try:
        return project_root.read_text(args.brief_file)
    except (OSError, UnicodeDecodeError) as exc:
        die(f'cannot read the brief file: {exc}; nothing was registered or started')


def _version_notice(harness: str) -> str | None:
    """One line when the installed harness is outside its adapter's tested range."""
    try:
        import harness_versions
        status = harness_versions.compatibility_status((harness,)).get(harness) or {}
    except Exception:  # noqa: BLE001 - a notice never blocks a launch
        return None
    found = status.get('compatibility')
    if not found or found == 'verified':
        return None
    return (f"note: {harness} {status.get('observed_version') or '?'} is outside the "
            f"version its adapter was tested with ({found}); launching anyway")


def _launch_agent(args, *, harness, model, effort, base_commit, branch, label,
                  continues=None, resume_session=None) -> int:
    """Register, then detach the supervisor that creates the worktree and runs the harness."""
    import job_agent as agent

    if not (SC_API_TOKEN and SC_API_BASE):
        die('authenticated shell API is required; nothing was registered or started')
    if args.timeout is not None and args.timeout <= 0:
        die('--timeout must be positive seconds; nothing was registered or started')
    try:
        brief = agent.validate_brief(_brief_text(args))
        command = agent.build_command(harness, model, effort, resume_session=resume_session)
    except agent.AgentRefusal as exc:
        die(f'refused: {exc}; nothing was registered or started')
    try:
        registered = _api('POST', '/_sc/runs', {
            'registration_key': uuid.uuid4().hex, 'kind': 'agent', 'label': label,
            'argv': command.argv, 'commit': base_commit, 'timeout_seconds': args.timeout,
            'agent': {
                'harness': harness, 'requested_model': model, 'requested_effort': effort,
                'effective_effort': command.effective_effort, 'branch': branch,
                'adapter_version': (command.cli_version or '')[:128] or None,
                'brief_bytes': len(brief),
                'launched_from_run': agent.launched_from_run(),
                'continues_run_id': continues,
            }})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'refused ({_refused(exc)}); nothing was registered or started')
    job_id = str(registered['run_id'])
    facts = registered['agent']
    jobdir = Path(registered['evidence_path']).parent
    jobdir.mkdir(parents=True, exist_ok=False)
    (jobdir / 'log').touch()
    (jobdir / agent.BRIEF_FILE).write_bytes(brief)
    write_meta(jobdir, {
        'run_id': registered['run_id'], 'job_id': job_id, 'kind': 'agent',
        'label': registered['label'], 'cmd': command.argv, 'cwd': registered['cwd'],
        'timeout': registered['timeout_seconds'], 'started_at': _now(),
        'log': str(jobdir / 'log'),
        # The supervisor is another process: the positions of the engine's
        # tokens travel with the command, so it never searches argv for them.
        'agent': {**agent.spawn_plan(command), 'base_commit': facts['base_commit'],
                  'branch': facts['branch'], 'continues_run_id': continues},
    })
    _detach_supervisor(jobdir)
    end = time.monotonic() + AGENT_START_WAIT
    started: dict = {}
    while time.monotonic() < end:
        started = read_meta(jobdir)
        if started.get('pid') or started.get('finished_at'):
            break
        time.sleep(0.05)
    else:
        die(f'subagent startup unconfirmed after {AGENT_START_WAIT}s; registered run {job_id} '
            f'will reconcile; inspect `sc job status {job_id}` before starting dependent work')
    if started.get('finished_at') and started.get('spawn_error'):
        die(f"run {job_id} ended before the subagent started: {started['spawn_error']}. "
            f"Its wake states the same; `sc job status {job_id}`")
    notice = _version_notice(harness)
    if notice:
        print(f'job: {notice}')
    where = facts['branch'] or f"detached at {facts['base_commit'][:12]}"
    print(f"job: {job_id} started — subagent {harness} · {facts['requested_model']} · "
          f"effort {facts['effective_effort'] or 'default'}; time limit "
          f"{registered['timeout_seconds']}s\n"
          f"  worktree: {registered['cwd']} ({where})"
          + (f"\n  continues run {continues}, same harness session" if continues else '')
          + f"\n  end your turn; completion wakes you. `sc job status {job_id}` · "
          f"`sc job result {job_id}`")
    return 0


def cmd_agent(args) -> int:
    import job_agent as agent

    cwd = project_root.invocation_cwd()
    try:
        base = agent.resolve_base(args.base or 'HEAD', cwd)
        branch = agent.validate_branch(args.branch, cwd) if args.branch else None
    except agent.AgentRefusal as exc:
        die(f'refused: {exc}; nothing was registered or started')
    return _launch_agent(args, harness=args.harness, model=args.model, effort=args.effort,
                         base_commit=base, branch=branch, label=args.label)


def _agent_row(job_id: str) -> dict:
    if not job_id.isdecimal():
        die(f"'{job_id}' is not a run id")
    row = ledger_run(job_id)
    if row is None:
        die(f"no run '{job_id}' in the ledger (see `sc job list --kind agent --all`)")
    if row['kind'] != 'agent' or not row.get('agent'):
        die(f"run {job_id} is a {row['kind']} run, not a subagent")
    return row


def cmd_continue(args) -> int:
    row = _agent_row(args.id)
    facts = row['agent']
    if row['state'] not in TERMINAL:
        die(f"refused: run {args.id} is still live ({row['state']}); wait for it to end or "
            f"stop it with `sc job kill {args.id}`; nothing was registered or started")
    if not facts['session_ref']:
        die(f'refused: run {args.id} captured no session reference, so there is no harness '
            'session to resume; launch a fresh subagent. Nothing was registered or started')
    return _launch_agent(
        args, harness=facts['harness'], model=facts['requested_model'],
        effort=facts['requested_effort'], base_commit=facts['base_commit'], branch=None,
        label=args.label or row['label'], continues=row['run_id'],
        resume_session=facts['session_ref'])


def cmd_result(args) -> int:
    _agent_row(args.id)
    try:
        result = _api('GET', f'/_sc/runs/{args.id}/result')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'result unavailable ({_refused(exc)})')
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result['available'] else 1
    facts = result['agent'] or {}
    print(f"run {result['run_id']}: {result['state']} exit={result['exit_code']} — "
          f"{facts.get('harness')} · {facts.get('requested_model')} · "
          f"{facts.get('effective_effort') or 'default'}")
    for name in ('session_ref', 'worktree', 'worktree_removed_at', 'worktree_discarded_at',
                 'base_commit', 'branch', 'continues_run_id', 'usage_json'):
        if facts.get(name) is not None:
            print(f"  {name}: {json.dumps(facts[name]) if name == 'usage_json' else facts[name]}")
    if not result['available']:
        print('  no result is recorded yet')
        return 1
    print(f"  result_bytes: {result['result_bytes']}")
    print('---')
    print(result['text'])
    if result['truncated']:
        print("--- truncated: the first 256 KiB are shown; the full text is result.md "
              "beside the run's log (`sc job status` names the path)")
    return 0


def cmd_clean(args) -> int:
    import job_agent as agent

    row = _agent_row(args.id)
    facts = row['agent']
    path = Path(facts['worktree'])
    state = agent.worktree_state(path, facts['base_commit'])
    discarding = False
    if state['exists']:
        unpushed = agent.unpushed_commits(path, facts['base_commit'])
        discarding = state['uncommitted'] is not False or unpushed is None or unpushed > 0
        if discarding and not args.discard:
            kept = (f" (local branch {facts['branch']} is kept and still holds its commits)"
                    if facts['branch'] and unpushed else '')
            die(f"refused: {path} holds work that is on no remote branch — uncommitted changes: "
                f"{'unknown' if state['uncommitted'] is None else 'yes' if state['uncommitted'] else 'no'}; "
                f"commits on no remote branch: {'unknown' if unpushed is None else unpushed}{kept}. "
                f"Push or copy what you need, or remove the worktree with "
                f"`sc job clean {args.id} --discard`")
    try:
        _api('POST', f'/_sc/runs/{args.id}/clean', {'discard': discarding})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'refused ({_refused(exc)}); nothing was removed')
    if not state['exists']:
        print(f'job: {args.id} worktree {path} is already removed')
        return 0
    try:
        agent.remove_worktree(path)
    except RuntimeError as exc:
        die(f'{exc}; the ledger records the removal — remove the directory by hand')
    print(f'job: {args.id} worktree {path} removed'
          + (' with its uncommitted or unpushed work (--discard); it cannot be continued'
             if discarding else '')
          + (f". Branch {facts['branch']} is kept" if facts['branch'] else ''))
    return 0


def _register_watch(args, *, argv, cmd, every, timeout, label, extra) -> int:
    if not (SC_API_TOKEN and SC_API_BASE):
        die('authenticated shell API is required; no watch started')
    cwd = str(project_root.invocation_cwd())
    try:
        registered = _api('POST', '/_sc/runs', {
            'registration_key': uuid.uuid4().hex, 'kind': 'probe', 'label': label,
            'argv': argv, 'cwd': cwd, 'commit': None, 'timeout_seconds': timeout})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'refused ({_refused(exc)}); no watch started')
    job_id = str(registered['run_id'])
    jobdir = Path(registered['evidence_path']).parent
    jobdir.mkdir(parents=True, exist_ok=False)
    (jobdir / 'log').touch()
    write_meta(jobdir, {
        'run_id': registered['run_id'], 'job_id': job_id, 'kind': 'probe',
        'label': label, 'cmd': cmd, 'cwd': cwd, 'every': every, 'timeout': timeout,
        'started_at': _now(), 'log': str(jobdir / 'log'), **extra})
    _detach_supervisor(jobdir)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        started = read_meta(jobdir)
        if started.get('probe_ready') or started.get('finished_at'):
            break
        time.sleep(0.05)
    else:
        die(f'supervisor startup unconfirmed; registered run {job_id} will reconcile; '
            f'inspect `sc job status {job_id}`')
    print(f"job: {job_id} watching — {watch_form(registered)[1]}; ends within {timeout}s. "
          f"End your turn; it wakes you once. This watch grants no merge authority. "
          f"`sc job status {job_id}`")
    return 0


def cmd_watch(args) -> int:
    script = [sys.executable, str(Path(__file__).resolve()), '_watch']
    if args.form == 'timer':
        if not 0 < args.after <= sys.maxsize:
            die('--after must be positive seconds')
        options = ['timer', '--after', str(args.after)]
        return _register_watch(
            args, argv=[*WATCH_ARGV, *options], cmd=[*script, *options], every=None,
            timeout=args.after, label=args.label or f'timer {args.after}s',
            extra={'watch': 'timer', 'after': args.after})
    if args.pr < 1:
        die('--pr must be a pull request number')
    if args.repository is not None and not REPOSITORY.fullmatch(args.repository):
        die('--repository must be owner/name')
    every = WATCH_PR_EVERY if args.every is None else args.every
    timeout = PROBE_TIMEOUT if args.timeout is None else args.timeout
    if not 5 <= every <= sys.maxsize:
        die(f'--every must be between 5 and {sys.maxsize} seconds')
    if not 0 < timeout <= sys.maxsize:
        die(f'--timeout must be positive seconds (maximum {sys.maxsize})')
    options = ['pr', '--pr', str(args.pr)]
    if args.repository:
        options += ['--repository', args.repository]
    # One read in the foreground: a PR that cannot be read is refused now,
    # not reported an hour later as a timeout.
    state, line = read_pr_state(args.pr, args.repository)
    if state is None:
        die(f'cannot read PR #{args.pr}: {line}; no watch started')
    return _register_watch(
        args, argv=[*WATCH_ARGV, *options], cmd=[*script, *options], every=every,
        timeout=timeout, label=args.label or f'PR #{args.pr}', extra={'watch': 'pr'})


def read_pr_state(number: int, repository: str | None) -> tuple[str | None, str]:
    """One read of a PR through the PR watcher's reader and normalization.

    Returns the state and the line a watch attempt reports, or (None, reason)
    when GitHub could not be read.
    """
    from github_pull_requests import GitHubPullRequestReader, GitHubReadError, normalize_state

    try:
        pull_request = GitHubPullRequestReader(
            project_root.invocation_cwd(), repository=repository).get(number)
    except (GitHubReadError, ValueError) as exc:
        return None, str(exc)[:120]
    state = normalize_state(pull_request)
    shown = 'no checks reported' if state == 'created' else state
    return state, f"{shown} at head {pull_request.head_sha or 'unknown'}"


def cmd_watch_attempt(args) -> int:
    """One attempt of a watch, run by the probe supervisor: exit 0 = settled."""
    if args.form == 'timer':
        time.sleep(args.after)
        return 0
    state, line = read_pr_state(args.pr, args.repository)
    if state is None:
        print(f'read failed: {line}')
        return 2
    print(line)
    return 0 if state in SETTLED_PR_STATES else 1


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


def _seconds_since(stamp: str | None) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _remaining(row: dict) -> str:
    """Time left against the run's recorded limit, for a live run."""
    elapsed = _seconds_since(row.get('started_at') or row.get('created_at'))
    limit = row.get('timeout_seconds')
    if row['state'] in TERMINAL or elapsed is None or not limit:
        return f"limit {_span(limit)}" if limit else 'no limit'
    return f"{_span(limit - elapsed)} left of {_span(limit)}"


def _row_detail(row: dict, kind: str | None) -> str:
    """What `list` adds for a subagent, and for a watcher under `--kind probe`."""
    agent = row.get('agent')
    if agent:
        usage = agent.get('usage_json') or {}
        tokens = (f" · {usage.get('input_tokens', 0) + usage.get('output_tokens', 0)} tokens"
                  if usage else '')
        return (f" [{agent['harness']} · {agent['requested_model']} · "
                f"{agent['effective_effort'] or 'default'} · {_remaining(row)}{tokens}]")
    if kind == 'probe':
        form, condition = watch_form(row)
        return f" [{form}: {condition[:80]} · {_remaining(row)}]"
    return ''


def cmd_list(args) -> int:
    emitted = False
    kind = getattr(args, 'kind', None)
    result = read_api('/_sc/runs' + (f'?kind={kind}' if kind else ''))
    if result is not None:
        for row in result['runs']:
            if args.all or row['state'] in {'registered', 'running'} or row['wake_state'] == 'blocked':
                emitted = True
                print(f"  {row['run_id']:<20} {row['state']:<10} wake={row['wake_state']} "
                      f"{row['label']}{_row_detail(row, kind)}")
    # Legacy job directories predate kinds: they are listed only without a filter.
    roots = ([] if kind else [JOBS]) if result is not None else [RUNS, JOBS]
    for root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda p: p.name):
            meta = read_meta(directory)
            if not meta or (kind and meta.get('kind', 'job') != kind):
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


def cmd_attach(args) -> int:
    payload = {'pr': args.pr} if args.pr is not None else {'work_unit': args.work_unit}
    if args.repository is not None:
        payload['repository'] = args.repository
    try:
        row = _api('POST', f'/_sc/runs/{args.id}/attach', payload)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f'attach failed: {exc}')
    print(json.dumps(row))
    return 0


# ── arg parsing ───────────────────────────────────────────────────────────────

PUBLIC_VERBS = ("start", "agent", "continue", "result", "clean", "watch",
                "list", "status", "tail", "wait", "kill", "attach")

HELP_EPILOG = """\
forms:
  sc job start [--label L] [--timeout S] -- <command and args>
      run one command detached; its completion wakes you
  sc job start [--label L] [--timeout S] --until "<probe command>" [--every S]
      repeat a quoted /bin/sh probe until it exits 0, then wake you; --every
      is the delay between attempts (default 30, minimum 5) and needs --until;
      probes time out after 3600s unless --timeout says otherwise
  sc job agent --harness H --model M [--effort E] (--brief-file P | --brief "T")
               [--base REF] [--branch NAME] [--label L] [--timeout S]
      run one subagent: a headless harness run in its own worktree
      (.sc-worktrees/agent-<id>), acting as you under your brief; its
      completion wakes you. Model is required. Effort is optional: omitted,
      no effort setting is sent and the harness's own default for that model
      applies (`sc run` instead falls back to a flavor effort, then high).
      Time limit 7200s by default, 28800s at most. At most 3 live subagents
      for one shell and 6 on the install; a subagent cannot launch one.
  sc job result <id> [--json]
      a subagent's final message (at most 256 KiB) and its recorded facts
  sc job continue <id> (--brief-file P | --brief "T") [--label L] [--timeout S]
      a new run that resumes the same harness session in the same worktree
  sc job clean <id> [--discard]
      remove a kept subagent worktree; --discard removes one that holds
      uncommitted changes or commits on no remote branch
  sc job watch pr --pr N [--repository owner/name] [--every S] [--timeout S]
      wake you once when that PR's checks settle (green or red) or it is
      merged or closed; read every 60s, for at most 3600s, unless told otherwise
  sc job watch timer --after S
      wake you once after S seconds (at most 86400, 24 hours)
  sc job attach <id> (--pr N [--repository owner/name] | --work-unit N)
      link a finished run to a PR or a Sprint work unit as supporting evidence
  sc job list [--kind agent|probe|job] [--all] · status <id> · tail <id> [-n N]
  sc job wait <id> [--for S] · kill <id>

A watch is the engine reading on your behalf, bounded, with one outcome; it
grants no merge authority. At most 10 live watchers (--until included) for
one shell.

Service teardown/reboot may interrupt computation; recovery records lost when
no exit evidence survives. Interrupted runs are never automatically restarted.
TUI wakes wait out the CLI lock. Legacy job status/logs remain readable.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sc job",
        description="Registered jobs survive harness exit and durably wake their owner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG)
    sub = p.add_subparsers(dest="cmd_name", required=True)

    sp = sub.add_parser("start", help="register and detach; completion durably wakes the owner")
    sp.add_argument("--label", help="display label (never used in a filesystem path)")
    sp.add_argument("--kind", choices=('job', 'devkit'), default='job', help=argparse.SUPPRESS)
    sp.add_argument("--timeout", type=int,
                    help="positive overall seconds; probes default to 3600, ordinary jobs have no default")
    sp.add_argument("--until", metavar="COMMAND",
                    help="repeat a quoted /bin/sh command until exit 0; bounded attempt excerpts and owner wake")
    sp.add_argument("--every", type=int, metavar="SECONDS",
                    help="delay between probe attempts (default 30, minimum 5); requires --until")
    sp.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- <command and args>")
    sp.set_defaults(fn=cmd_start)

    def brief_arguments(parser) -> None:
        brief = parser.add_mutually_exclusive_group(required=True)
        brief.add_argument("--brief-file", metavar="PATH",
                           help="file holding the brief (at most 64 KiB); - reads stdin")
        brief.add_argument("--brief", metavar="TEXT", help="the brief as text")
        parser.add_argument("--label", help="display label")
        parser.add_argument("--timeout", type=int,
                            help=f"run-time limit in seconds (default {AGENT_DEFAULT_TIMEOUT}, "
                                 f"maximum {AGENT_MAX_TIMEOUT})")

    sp = sub.add_parser("agent", help="run one subagent in its own worktree; completion wakes you")
    sp.add_argument("--harness", help="harness to run it on (required)")
    sp.add_argument("--model", help="model selector for that harness (required)")
    sp.add_argument("--effort", help="effort level; omitted, the harness's default for the "
                                     "model applies and no effort setting is sent")
    sp.add_argument("--base", metavar="REF", help="commit to start from (default: your HEAD)")
    sp.add_argument("--branch", metavar="NAME",
                    help="create this branch at the base; omitted, the worktree is detached")
    brief_arguments(sp)
    sp.set_defaults(fn=cmd_agent)

    sp = sub.add_parser("continue", help="resume a subagent's session in its worktree, as a new run")
    sp.add_argument("id")
    brief_arguments(sp)
    sp.set_defaults(fn=cmd_continue)

    sp = sub.add_parser("result", help="a subagent's final message and recorded facts")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true", help="the structured form")
    sp.set_defaults(fn=cmd_result)

    sp = sub.add_parser("clean", help="remove a kept subagent worktree")
    sp.add_argument("id")
    sp.add_argument("--discard", action="store_true",
                    help="remove it even with uncommitted changes or unpushed commits")
    sp.set_defaults(fn=cmd_clean)

    sp = sub.add_parser("watch", help="one bounded watch that wakes you once")
    forms = sp.add_subparsers(dest="form", required=True)
    wp = forms.add_parser("pr", help="a PR's checks settle, or it is merged or closed")
    wp.add_argument("--pr", type=int, required=True)
    wp.add_argument("--repository", help="owner/name; default: this checkout's repository")
    wp.add_argument("--every", type=int, metavar="SECONDS",
                    help=f"delay between reads (default {WATCH_PR_EVERY}, minimum 5)")
    wp.add_argument("--timeout", type=int, help=f"overall seconds (default {PROBE_TIMEOUT})")
    wp.add_argument("--label", help="display label")
    wp = forms.add_parser("timer", help="a number of seconds passes")
    wp.add_argument("--after", type=int, required=True, metavar="SECONDS")
    wp.add_argument("--label", help="display label")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("list", help="live jobs (--all includes finished)")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--kind", choices=("agent", "probe", "job"),
                    help="only runs of this kind; agents show harness, model, effort "
                         "and time remaining")
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

    sp = sub.add_parser("attach", help="link a terminal run by id; receipts supplement, never replace CI")
    sp.add_argument("id", type=int)
    target = sp.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--work-unit", type=int)
    sp.add_argument("--repository", help="owner/name when a PR number is ambiguous")
    sp.set_defaults(fn=cmd_attach)

    return p


def supervisor_parser() -> argparse.ArgumentParser:
    """The detached supervisor's own re-entry point (`job.py _supervise
    <jobdir>`). Not a subcommand of the public parser: registered there it
    surfaced in --help and in every invalid-choice message."""
    p = argparse.ArgumentParser(prog="sc job _supervise")
    p.add_argument("jobdir")
    p.set_defaults(fn=cmd_supervise)
    return p


def watch_attempt_parser() -> argparse.ArgumentParser:
    """One attempt of a named watch (`job.py _watch <form>`), run by the probe
    supervisor. Internal, like `_supervise`."""
    p = argparse.ArgumentParser(prog="sc job _watch")
    forms = p.add_subparsers(dest="form", required=True)
    wp = forms.add_parser("pr")
    wp.add_argument("--pr", type=int, required=True)
    wp.add_argument("--repository")
    wp = forms.add_parser("timer")
    wp.add_argument("--after", type=int, required=True)
    p.set_defaults(fn=cmd_watch_attempt)
    return p


def main(argv: list[str]) -> int:
    # Early-closed stdout is handled at the entrypoint (cli_entry.run_cli, #384).
    if argv[:1] == ["_supervise"]:
        args = supervisor_parser().parse_args(argv[1:])
    elif argv[:1] == ["_watch"]:
        args = watch_attempt_parser().parse_args(argv[1:])
    else:
        args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    from cli_entry import run_cli

    sys.exit(run_cli(main, sys.argv[1:]))
