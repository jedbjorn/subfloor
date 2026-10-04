"""Bounded, once-per-binary Claude hook verification beside model evidence."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import harness_versions

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "logs" / "seat_conversion"
OUTPUT_LIMIT = 256 * 1024
CLEANUP_TIMEOUT = 1.0
TIERS = {"rewrite", "deny-only", "disarmed"}
PROMPT = (
    "Hook compatibility test. Use Bash exactly twice in this order: first command "
    "'printf SC_PROBE_ORIGINAL' with run_in_background true; then command "
    "'printf SC_PROBE_DENIED'. Do not retry refused calls or run other commands. "
    "Report the tool results and stop."
)
PROBE_HOOK = '''import json,sys,time
from pathlib import Path
event=json.load(sys.stdin)
command=event.get("tool_input",{}).get("command","")
event["observed_monotonic"]=time.monotonic()
with Path("hook-calls.jsonl").open("a") as out:
    line=json.dumps(event)+"\\n"
    if out.tell()+len(line.encode()) <= 262144:
        out.write(line)
result={"hookEventName":"PreToolUse"}
if command == "printf SC_PROBE_ORIGINAL":
    result.update(permissionDecision="allow", updatedInput={"command":"printf SC_PROBE_REWRITTEN","run_in_background":False})
elif command == "printf SC_PROBE_DENIED":
    result.update(permissionDecision="deny",permissionDecisionReason="SC_PROBE_DENY_HONORED: use sc job start")
print(json.dumps({"hookSpecificOutput":result}))
'''


def binary_identity(executable: str) -> dict:
    path = Path(executable).resolve(strict=True)
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            **harness_versions.runtime_scope()}


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Callers hold the identity lock. Never expose half-written evidence.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def _key(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def current() -> dict:
    """Status projection: file/stat reads only, never an inference request."""
    executable = shutil.which("claude")
    if not executable:
        return {"tier": "disarmed", "error": "Claude is unavailable"}
    try:
        identity = binary_identity(executable)
    except OSError:
        return {"tier": "disarmed", "error": "Claude binary is unreadable"}
    pointer = _read(EVIDENCE_DIR / (_key(identity) + ".current.json"))
    evidence = _read(Path(pointer["evidence_path"])) if pointer.get("evidence_path") else {}
    if evidence.get("binary_identity") != identity or evidence.get("tier") not in TIERS:
        return {"tier": "disarmed", "error": "Conversion not verified for this installation"}
    return evidence


def _tool_results(transcript: str) -> list[str]:
    results = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        for block in (event.get("message") or {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    results.append(content)
                elif isinstance(content, list):
                    results.extend(part.get("text", "") for part in content if isinstance(part, dict))
    return results


def _process_identity(pid: int) -> dict | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return {"pid": pid, "start_ticks": int(fields[19]),
            "process_group": int(fields[2]), "state": fields[0]}


def _group_snapshot(pgid: int) -> dict:
    members, unreadable = [], 0
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            identity = _process_identity(int(path.name))
        except (OSError, ValueError, IndexError):
            unreadable += 1
            continue
        if identity and identity["process_group"] == pgid:
            members.append(identity)
    return {"members": members, "unreadable_count": unreadable}


def _capture(process: subprocess.Popen, root: Path, deadline: float, started: float) -> dict:
    """Drain both pipes until EOF/deadline; retain at most OUTPUT_LIMIT each."""
    observed: dict = {"bytes": {}, "first_output_seconds": {}}
    with selectors.DefaultSelector() as selector:
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
            observed["bytes"][name] = 0
        with (root / "stdout.jsonl").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
            outputs = {"stdout": stdout, "stderr": stderr}
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    observed["timed_out"] = True
                    return observed
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    observed["first_output_seconds"].setdefault(name, time.monotonic() - started)
                    retained = observed["bytes"][name]
                    outputs[name].write(chunk[:max(0, OUTPUT_LIMIT - retained)])
                    observed["bytes"][name] += len(chunk)
    observed["timed_out"] = False
    return observed


def _cleanup_probe(process: subprocess.Popen, owned_members: list[dict]) -> dict:
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    before = _group_snapshot(process.pid)
    cleanup: dict = {"before": before, "signal": None}
    # Our unreaped child pins its PID. After reaping, require a surviving member
    # with the same incarnation so a recycled group can never be signalled.
    try:
        owned = process.returncode is None
        for member in owned_members:
            current = _process_identity(member["pid"])
            owned = owned or bool(current and current["start_ticks"] == member["start_ticks"]
                                  and current["process_group"] == process.pid)
        if owned:
            os.killpg(process.pid, signal.SIGKILL)
            cleanup["signal"] = "SIGKILL"
    except ProcessLookupError:
        pass
    except (OSError, ValueError, IndexError) as exc:
        cleanup["error"] = type(exc).__name__
    try:
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        cleanup["error"] = "Probe leader did not reap within cleanup bound"
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    after = _group_snapshot(process.pid)
    while any(member["state"] != "Z" for member in after["members"]) and time.monotonic() < deadline:
        time.sleep(min(.01, max(0, deadline - time.monotonic())))
        after = _group_snapshot(process.pid)
    cleanup["after"] = after
    cleanup["status"] = "indeterminate" if after["unreadable_count"] else (
        "remaining" if any(member["state"] != "Z" for member in after["members"]) else (
            "zombies-only" if after["members"] else "gone"))
    return cleanup


def native_probe(executable: str, *, timeout: float) -> dict:
    # Retain private diagnostic artifacts; never include their contents in wakes.
    started = time.monotonic()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SC_", "CLAUDECODE", "CLAUDE_CODE_"))}
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="probe-", dir=EVIDENCE_DIR))
    hook = root / "probe.py"
    hook.write_text(PROBE_HOOK)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash", "hooks": [{"type": "command",
        "command": shlex.join([sys.executable, str(hook)])}]}]}}))
    argv = [executable, "-p", PROMPT, "--settings", str(settings),
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--dangerously-skip-permissions",
            "--model", "haiku", "--verbose", "--output-format", "stream-json", "--max-turns", "4"]
    record: dict = {"argv": argv, "cwd": str(root), "budget_seconds": timeout,
              "cleanup_timeout_seconds": CLEANUP_TIMEOUT,
              "output_limit_bytes": OUTPUT_LIMIT, "timings": {"setup_seconds": time.monotonic() - started}}
    result: dict = {"hook_fired": None, "rewrite_honored": None, "deny_honored": None,
              "artifact_directory": str(root), "error": None}
    process = None
    owned_members: list[dict] = []
    try:
        process = subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        record["timings"]["spawn_seconds"] = time.monotonic() - started
        record["process"] = {"pid": process.pid, "process_group": process.pid, "start_ticks": None}
        try:
            record["process"] = _process_identity(process.pid) or record["process"]
        except (OSError, ValueError, IndexError) as exc:
            record["process"]["identity_error"] = type(exc).__name__
        record["capture"] = _capture(process, root, started + timeout, started)
        record["timings"]["capture_seconds"] = time.monotonic() - started
        if record["capture"]["timed_out"]:
            result["error"] = f"Conversion probe exceeded {timeout:g}s"
        else:
            owned_members = _group_snapshot(process.pid)["members"]
            process.wait(timeout=max(.001, started + timeout - time.monotonic()))
            if process.returncode:
                result["error"] = f"Probe exited {process.returncode}"
    except subprocess.TimeoutExpired:
        result["error"] = f"Conversion probe exceeded {timeout:g}s"
    except (OSError, ValueError, IndexError) as exc:
        result["error"] = f"Conversion probe failed: {type(exc).__name__}"
    finally:
        if process is not None:
            cleanup_started = time.monotonic()
            record["cleanup"] = _cleanup_probe(process, owned_members)
            record["timings"]["cleanup_seconds"] = time.monotonic() - cleanup_started
            if record["cleanup"]["status"] == "remaining" or record["cleanup"].get("error"):
                result["error"] = result["error"] or "Probe cleanup incomplete; inspect retained receipt"
            record["returncode"] = process.returncode
        record["timings"]["total_seconds"] = time.monotonic() - started
    calls = []
    audit = root / "hook-calls.jsonl"
    if audit.exists():
        for line in audit.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                calls.append(event)
    transcript = root / "stdout.jsonl"
    results = _tool_results(transcript.read_text(errors="replace")) if transcript.exists() else []
    background = any(call.get("tool_input", {}).get("run_in_background") is True
                     and call.get("tool_input", {}).get("command") == "printf SC_PROBE_ORIGINAL"
                     for call in calls)
    denied_call = any(call.get("tool_input", {}).get("command") == "printf SC_PROBE_DENIED" for call in calls)
    if calls:
        result["hook_fired"] = True
    if background:
        if any("SC_PROBE_ORIGINAL" == value.strip() for value in results):
            result["rewrite_honored"] = False
        elif any("SC_PROBE_REWRITTEN" == value.strip() for value in results):
            result["rewrite_honored"] = True
    if denied_call:
        if any("SC_PROBE_DENIED" == value.strip() for value in results):
            result["deny_honored"] = False
        elif any("SC_PROBE_DENY_HONORED" in value for value in results):
            result["deny_honored"] = True
    record["timings"]["hook_seconds"] = [call["observed_monotonic"] - started
        for call in calls if isinstance(call.get("observed_monotonic"), (int, float))]
    record["observations"] = result.copy()
    _write(root / "receipt.json", record)
    return result


def ensure(*, force: bool = False) -> dict:
    executable = shutil.which("claude")
    if not executable:
        return {"tier": "disarmed", "error": "Claude is unavailable"}
    try:
        identity = binary_identity(executable)
        version = harness_versions.probe("claude")
        key = _key({"harness": "claude", "version": version, "binary_identity": identity})
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_DIR / (key + ".json")
        with (EVIDENCE_DIR / (key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"tier": "disarmed", "error": "Conversion verification is already running"}
            stored = _read(path)
            if stored.get("tier") in TIERS and not force:
                return stored
            try:
                result = native_probe(executable, timeout=harness_versions.TIMEOUT) if version else {
                    "error": "Cannot observe Claude version"}
            except (OSError, subprocess.SubprocessError) as exc:
                result = {"error": f"Conversion probe failed: {type(exc).__name__}"}
            tier = "disarmed"
            if not result.get("error") and result.get("hook_fired") and result.get("deny_honored"):
                tier = "rewrite" if result.get("rewrite_honored") else "deny-only"
            if tier == "disarmed" and not result.get("error"):
                result["error"] = "Native hook behavior was not verified"
            result.update(harness="claude", version=version, binary_identity=identity, tier=tier,
                          verified_at=datetime.now(timezone.utc).isoformat(), evidence_path=str(path))
            _write(path, result)
            _write(EVIDENCE_DIR / (_key(identity) + ".current.json"), {"evidence_path": str(path)})
            return result
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tier": "disarmed", "error": f"Conversion verification unavailable: {type(exc).__name__}"}


def main(argv: list[str]) -> int:
    print(json.dumps(ensure(force="--force" in argv), indent=2))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
