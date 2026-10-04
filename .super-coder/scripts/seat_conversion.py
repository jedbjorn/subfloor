"""Bounded, once-per-binary Claude hook verification beside model evidence."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import harness_versions

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "logs" / "seat_conversion"
TIERS = {"rewrite", "deny-only", "disarmed"}
PROMPT = (
    "Hook compatibility test. Use Bash exactly twice in this order: first command "
    "'printf SC_PROBE_ORIGINAL' with run_in_background true; then command "
    "'printf SC_PROBE_DENIED'. Do not retry refused calls or run other commands. "
    "Report the tool results and stop."
)
PROBE_HOOK = '''import json,sys
from pathlib import Path
event=json.load(sys.stdin)
command=event.get("tool_input",{}).get("command","")
with Path("hook-calls.jsonl").open("a") as out:
    out.write(json.dumps(event)+"\\n")
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
        if event.get("type") != "user":
            continue
        for block in (event.get("message") or {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    results.append(content)
                elif isinstance(content, list):
                    results.extend(part.get("text", "") for part in content if isinstance(part, dict))
    return results


def native_probe(executable: str, *, timeout: float) -> dict:
    # Isolated settings and cwd; preserve credentials, remove nesting markers and
    # engine credentials so the synthetic invocation cannot target this engine.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))
           and k not in {"SC_API_TOKEN", "SC_API_BASE", "SC_API_URL"}}
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=EVIDENCE_DIR) as directory:
        root = Path(directory)
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
        process = subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {"hook_fired": False, "rewrite_honored": False, "deny_honored": False,
                    "error": f"Conversion probe exceeded {timeout:g}s"}
        calls = []
        audit = root / "hook-calls.jsonl"
        if audit.exists():
            calls = [json.loads(line) for line in audit.read_text().splitlines()]
        results = _tool_results(stdout)
        background = any(call.get("tool_input", {}).get("run_in_background") is True
                         and call.get("tool_input", {}).get("command") == "printf SC_PROBE_ORIGINAL"
                         for call in calls)
        denied_call = any(call.get("tool_input", {}).get("command") == "printf SC_PROBE_DENIED" for call in calls)
        return {
            "hook_fired": bool(calls),
            "rewrite_honored": background and any("SC_PROBE_REWRITTEN" in result for result in results)
                and not any("SC_PROBE_ORIGINAL" == result.strip() for result in results),
            "deny_honored": denied_call and any("SC_PROBE_DENY_HONORED" in result for result in results)
                and not any("SC_PROBE_DENIED" == result.strip() for result in results),
            "error": None if process.returncode == 0 else f"Probe exited {process.returncode}",
        }


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


if __name__ == "__main__":
    print(json.dumps(ensure(force="--force" in sys.argv), indent=2))
