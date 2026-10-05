"""Claude GUI conversion and private, data-only argv bridge (Runs L2)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
INSTRUCTION = "Use sc job start for long work; end your turn and completion wakes you."
SCHEDULERS = {"Monitor", "CronCreate", "ScheduleWakeup"}


def timeout_seconds(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Bash timeout must be a positive integer in milliseconds; omit it for the job default")
    return (value + 999) // 1000


def label(command: str) -> str:
    token = command.split(maxsplit=1)[0] if command.strip() else "bash"
    token = re.sub(r"[^a-zA-Z0-9_-]", "-", token)[:32].strip("-") or "bash"
    return token + "-" + hashlib.sha256(command.encode()).hexdigest()[:10]


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def evidence_tier() -> str:
    try:
        evidence = json.loads(Path(os.environ["SC_SEAT_CONVERSION_EVIDENCE"]).read_text())
    except (KeyError, OSError, ValueError):
        return "disarmed"
    return evidence.get("tier", "disarmed") if isinstance(evidence, dict) else "disarmed"


def validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 1:
        raise ValueError("invalid conversion payload version")
    for name in ("command", "cwd", "bash"):
        value = payload.get(name)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError(f"invalid conversion {name}")
    if not Path(payload["cwd"]).is_absolute() or not Path(payload["cwd"]).is_dir():
        raise ValueError("conversion cwd must be the existing absolute tool cwd")
    if not Path(payload["bash"]).is_absolute():
        raise ValueError("conversion requires the session's absolute Bash executable")
    if "timeout" in payload:
        timeout_seconds(payload["timeout"])


def convert(event: dict, tier: str) -> dict | None:
    if os.environ.get("SC_SEAT") != "gui" or os.environ.get("SC_SHELL_FLAVOR") == "admin":
        return None
    if tier not in {"rewrite", "deny-only"}:
        return None
    name, tool_input = event.get("tool_name"), event.get("tool_input")
    if name in SCHEDULERS:
        return deny("Use sc job start --until for bounded polling; Sprint and PR wakes already deliver their outcomes.")
    if name not in {"Bash", "Agent"} or not isinstance(tool_input, dict) or tool_input.get("run_in_background") is not True:
        return None
    if tier == "deny-only":
        return deny(INSTRUCTION if name == "Bash" else "Run Agent in the foreground; use Sprint lanes for background agents.")
    updated = dict(tool_input)
    updated["run_in_background"] = False
    if name == "Bash":
        shell = os.environ.get("SHELL", "")
        bash = shell if Path(shell).name == "bash" and Path(shell).is_absolute() else shutil.which("bash")
        payload = {"v": 1, "command": tool_input.get("command"),
                   "cwd": event.get("cwd"), "bash": bash}
        if "timeout" in tool_input:
            payload["timeout"] = tool_input["timeout"]
        try:
            validate_payload(payload)
        except ValueError as exc:
            return deny(f"Cannot convert background Bash: {exc}. {INSTRUCTION}")
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode("ascii")
        # Only fixed executable paths and encoded data enter native shell syntax.
        updated["command"] = shlex.join([sys.executable, str(Path(__file__).resolve()), "bridge", encoded])
        # Registration budget, independent of the child's requested deadline.
        updated["timeout"] = 120000
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "updatedInput": updated,
    }}


def bridge(encoded: str) -> int:
    try:
        payload = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
        validate_payload(payload)
    except (ValueError, TypeError) as exc:
        print(f"seat conversion: {exc}", file=sys.stderr)
        return 2
    argv = [str(ENGINE.parent / "sc"), "job", "start", "--label", label(payload["command"])]
    if "timeout" in payload:
        argv += ["--timeout", str(timeout_seconds(payload["timeout"]))]
    argv += ["--", payload["bash"], "-c", payload["command"]]
    try:
        result = subprocess.run(argv, cwd=payload["cwd"], shell=False, check=False)
    except OSError as exc:
        print(f"seat conversion: job startup failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode == 0:
        print("End your turn; the completion wakes you.")
    return result.returncode


def main(argv: list[str]) -> int:
    if argv == ["hook"]:
        if os.environ.get("SC_SEAT") != "gui" or os.environ.get("SC_SHELL_FLAVOR") == "admin":
            return 0
        result = convert(json.load(sys.stdin), evidence_tier())
        if result is not None:
            print(json.dumps(result))
        return 0
    if len(argv) == 2 and argv[0] == "bridge":
        return bridge(argv[1])
    return 2


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
