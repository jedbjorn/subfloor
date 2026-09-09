#!/usr/bin/env python3
"""Named remote client and host-side SSH verbs served by vm-broker."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import ports
import vm

RESOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
REMOTE_STATE_ROOT = ports.ENGINE.parent / ".sc-state" / "local" / "remotes"
SSH_TIMEOUT = 120
TRANSFER_TIMEOUT = 300


def read_all() -> dict:
    value = ports.resolve(persist=False).get("remotes") or {}
    return value if isinstance(value, dict) else {}


def write_all(remotes: dict) -> dict:
    if remotes:
        ports.update({"remotes": remotes})
    else:
        ports.update({}, remove=("remotes",))
    return remotes


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(RESOURCE_NAME.fullmatch(name))


def _error(code: str, output: str, **details: object) -> dict:
    return {"ok": False, "error": code, "output": output, **details}


def _public_entry(name: str, entry: dict) -> dict:
    return {
        "name": name,
        "host": str(entry.get("host", "")),
        "user": str(entry.get("user", "")),
        "port": entry.get("port", 22),
        "known_hosts": "configured" if entry.get("known_hosts_path") else "managed",
    }


def _entry(name: object) -> tuple[dict | None, dict | None]:
    if not _valid_name(name):
        return None, _error(
            "remote_name_invalid",
            "remote name must match [a-z0-9][a-z0-9-]{0,31}",
        )
    entry = read_all().get(name)
    if not isinstance(entry, dict):
        return None, _error("remote_not_found", f"remote '{name}' is not declared")
    return entry, None


def _connection(name: object) -> tuple[dict | None, dict | None]:
    entry, error = _entry(name)
    if error:
        return None, error
    assert entry is not None
    missing = [
        field for field in ("host", "user", "key_path")
        if not str(entry.get(field, "")).strip()
    ]
    if missing:
        return None, _error(
            "remote_config_invalid",
            "missing required remote field(s): " + ", ".join(missing),
        )
    port = entry.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None, _error("remote_config_invalid", "remote port must be 1..65535")

    raw_key = str(entry["key_path"])
    key_path = Path(raw_key)
    if not key_path.is_absolute():
        return None, _error(
            "remote_key_invalid", "remote key_path must be an absolute host path"
        )
    try:
        key_stat = key_path.stat()
    except OSError:
        return None, _error(
            "remote_key_invalid", "remote key_path is not a readable host file"
        )
    if not stat.S_ISREG(key_stat.st_mode) or stat.S_IMODE(key_stat.st_mode) != 0o600:
        return None, _error(
            "remote_key_invalid", "remote key_path must be a regular mode-0600 file"
        )

    configured_known_hosts = entry.get("known_hosts_path")
    if configured_known_hosts:
        known_hosts = Path(str(configured_known_hosts))
        if not known_hosts.is_absolute():
            return None, _error(
                "remote_known_hosts_invalid",
                "known_hosts_path must be an absolute host path",
            )
    else:
        REMOTE_STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(REMOTE_STATE_ROOT, 0o700)
        known_hosts = REMOTE_STATE_ROOT / f"{name}.known_hosts"

    return {
        **entry,
        "port": port,
        "key_path": str(key_path),
        "known_hosts_path": str(known_hosts),
    }, None


def _ssh_options(entry: dict) -> list[str]:
    return [
        "-i", entry["key_path"],
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={entry['known_hosts_path']}",
    ]


def _ssh_argv(entry: dict, command: str) -> list[str]:
    return [
        "ssh",
        *_ssh_options(entry),
        "-p", str(entry["port"]),
        f"{entry['user']}@{entry['host']}",
        command,
    ]


def _scp_argv(entry: dict, source: str, destination: str) -> list[str]:
    return [
        "scp",
        *_ssh_options(entry),
        "-P", str(entry["port"]),
        "--",
        source,
        destination,
    ]


def _run(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        process = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return process.returncode, process.stdout, process.stderr
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc.filename}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out (>{timeout}s)"


def do_status(name: object) -> dict:
    entry, error = _connection(name)
    if error:
        return error
    assert entry is not None and isinstance(name, str)
    exit_code, stdout, stderr = _run(_ssh_argv(entry, "echo ok"), timeout=20)
    return {
        "ok": exit_code == 0,
        "error": None if exit_code == 0 else "remote_unreachable",
        "output": stdout.strip() if exit_code == 0 else (stderr.strip() or "SSH failed"),
        "remote": _public_entry(name, entry),
        "reachable": exit_code == 0,
        "exit": exit_code,
    }


def do_exec(name: object, command: object, timeout: int = SSH_TIMEOUT) -> dict:
    entry, error = _connection(name)
    if error:
        return error
    if not isinstance(command, str) or not command.strip():
        return _error("remote_exec_invalid", "remote exec command is empty")
    assert entry is not None and isinstance(name, str)
    exit_code, stdout, stderr = _run(_ssh_argv(entry, command), timeout=timeout)
    return {
        "ok": exit_code == 0,
        "error": None if exit_code == 0 else "remote_exec_failed",
        "remote": name,
        "exit": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def do_push(name: object, src: object, dest: object) -> dict:
    entry, error = _connection(name)
    if error:
        return error
    if not isinstance(src, str) or not isinstance(dest, str) or not dest:
        return _error("remote_push_invalid", "push requires source and destination")
    repo_root = ports.ENGINE.parent.resolve()
    source = Path(src)
    if not source.is_absolute():
        source = repo_root / source
    source = source.resolve()
    if not _inside(source, (repo_root,)):
        return _error("remote_path_not_allowed", "push source must be inside the repo")
    if not source.is_file():
        return _error("remote_push_invalid", "push source is not a file")
    assert entry is not None and isinstance(name, str)
    target = f"{entry['user']}@{entry['host']}:{dest}"
    exit_code, stdout, stderr = _run(
        _scp_argv(entry, str(source), target), timeout=TRANSFER_TIMEOUT
    )
    return {
        "ok": exit_code == 0,
        "error": None if exit_code == 0 else "remote_push_failed",
        "remote": name,
        "source": str(source),
        "destination": dest,
        "exit": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def do_pull(name: object, src: object, dest: object) -> dict:
    entry, error = _connection(name)
    if error:
        return error
    if not isinstance(src, str) or not src or not isinstance(dest, str):
        return _error("remote_pull_invalid", "pull requires source and destination")
    repo_root = ports.ENGINE.parent.resolve()
    local_root = (repo_root / ".sc-state" / "local").resolve()
    destination = Path(dest)
    if not destination.is_absolute():
        destination = repo_root / destination
    destination = destination.resolve()
    if not _inside(destination, (repo_root, local_root)):
        return _error(
            "remote_path_not_allowed",
            "pull destination must be inside the repo or .sc-state/local",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert entry is not None and isinstance(name, str)
    source = f"{entry['user']}@{entry['host']}:{src}"
    exit_code, stdout, stderr = _run(
        _scp_argv(entry, source, str(destination)), timeout=TRANSFER_TIMEOUT
    )
    return {
        "ok": exit_code == 0,
        "error": None if exit_code == 0 else "remote_pull_failed",
        "remote": name,
        "source": src,
        "destination": str(destination),
        "exit": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _broker_health() -> dict:
    try:
        response = vm.broker_call("GET", "/health", None, timeout=5)
        ready = response.get("ok") is True
    except (vm.BrokerConnectionError, vm.BrokerTimeoutError, vm.BrokerResponseError):
        ready = False
    return {
        "ready": ready,
        "start_command": "./sc vm-broker-up",
    }


def add_remote(name: object, entry: dict) -> dict:
    if not _valid_name(name):
        return vm.operation_error(
            "add", "remote_name_invalid",
            "remote name must match [a-z0-9][a-z0-9-]{0,31}",
        )
    for field in ("host", "user", "key_path"):
        if not str(entry.get(field, "")).strip():
            return vm.operation_error(
                "add", "remote_config_invalid", f"remote {field} is required"
            )
    key_path = Path(str(entry["key_path"]))
    if not key_path.is_absolute():
        return vm.operation_error(
            "add", "remote_key_invalid", "key_path must be an absolute host path"
        )
    known_hosts = entry.get("known_hosts_path")
    if known_hosts and not Path(str(known_hosts)).is_absolute():
        return vm.operation_error(
            "add", "remote_known_hosts_invalid",
            "known_hosts_path must be an absolute host path",
        )
    port = entry.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return vm.operation_error(
            "add", "remote_config_invalid", "remote port must be 1..65535"
        )
    assert isinstance(name, str)
    remotes = read_all()
    remotes[name] = entry
    try:
        write_all(remotes)
    except (OSError, RuntimeError, ValueError):
        return vm.operation_error(
            "add", "remote_write_failed", "the remotes block could not be saved"
        )
    return vm.operation_success("add", {
        "remote": _public_entry(name, entry),
        "broker": _broker_health(),
    })


def remove_remote(name: object) -> dict:
    if not _valid_name(name):
        return vm.operation_error(
            "remove", "remote_name_invalid",
            "remote name must match [a-z0-9][a-z0-9-]{0,31}",
        )
    remotes = read_all()
    if name not in remotes:
        return vm.operation_error(
            "remove", "remote_not_found", f"remote '{name}' is not declared"
        )
    del remotes[name]
    try:
        write_all(remotes)
    except (OSError, RuntimeError, ValueError):
        return vm.operation_error(
            "remove", "remote_write_failed", "the remotes block could not be saved"
        )
    return vm.operation_success("remove", {"name": name})


def list_remotes() -> dict:
    entries = [
        _public_entry(name, entry)
        for name, entry in sorted(read_all().items())
        if isinstance(entry, dict)
    ]
    return vm.operation_success("list", {"remotes": entries})


def run_broker_operation(
    operation: str,
    name: str,
    *,
    command: str | None = None,
    src: str | None = None,
    dest: str | None = None,
) -> dict:
    if not _valid_name(name):
        return vm.operation_error(
            operation, "remote_name_invalid",
            "remote name must match [a-z0-9][a-z0-9-]{0,31}",
        )
    calls = {
        "status": ("GET", f"/remote/{name}/status", None, 30),
        "exec": (
            "POST", f"/remote/{name}/exec", {"command": command}, SSH_TIMEOUT + 15
        ),
        "push": (
            "POST", f"/remote/{name}/push", {"src": src, "dest": dest},
            TRANSFER_TIMEOUT + 15,
        ),
        "pull": (
            "POST", f"/remote/{name}/pull", {"src": src, "dest": dest},
            TRANSFER_TIMEOUT + 15,
        ),
    }
    if operation not in calls:
        return vm.operation_error(
            operation, "remote_operation_unknown", "unknown remote operation"
        )
    method, path, body, timeout = calls[operation]
    try:
        response = vm.broker_call(method, path, body, timeout=timeout)
    except (vm.BrokerConnectionError, vm.BrokerTimeoutError):
        return vm.operation_error(
            operation, "broker_unreachable", "the VM broker is not reachable"
        )
    except vm.BrokerResponseError:
        return vm.operation_error(
            operation, "broker_response_invalid",
            "the VM broker did not return a complete response",
        )
    if not response.get("ok"):
        return vm.operation_error(
            operation,
            str(response.get("error") or f"remote_{operation}_failed"),
            str(response.get("output") or f"remote {operation} failed"),
            {"remote": name, "exit_code": response.get("exit")},
        )
    if operation == "status":
        return vm.operation_success(operation, {
            "remote": response["remote"], "reachable": bool(response["reachable"])
        })
    if operation == "exec":
        return vm.operation_success(operation, {
            "remote": name,
            "exit_code": int(response["exit"]),
            "stdout": str(response["stdout"]),
            "stderr": str(response["stderr"]),
        })
    return vm.operation_success(operation, {
        "remote": name,
        "source": response["source"],
        "destination": response["destination"],
    })


def _human_result(value: dict) -> str:
    if not value["ok"]:
        error = value["error"]
        return f"✗ remote {value['operation']} [{error['code']}]: {error['message']}"
    operation = value["operation"]
    result = value["result"]
    if operation == "add":
        state = "ready" if result["broker"]["ready"] else (
            "not running; start: ./sc vm-broker-up"
        )
        return f"Remote '{result['remote']['name']}' saved · broker {state}"
    if operation == "remove":
        return f"Remote '{result['name']}' removed"
    if operation == "list":
        return "\n".join(item["name"] for item in result["remotes"]) or "No remotes"
    if operation == "status":
        return f"Remote '{result['remote']['name']}' reachable"
    if operation == "exec":
        lines = [f"Remote command exited {result['exit_code']}"]
        if result["stdout"]:
            lines.append(f"stdout:\n{result['stdout'].rstrip()}")
        if result["stderr"]:
            lines.append(f"stderr:\n{result['stderr'].rstrip()}")
        return "\n".join(lines)
    return f"Remote {operation}: {result['source']} -> {result['destination']}"


def client_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="./sc remote",
        description="Use broker-served SSH against named remotes.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    add = commands.add_parser("add", help="add or replace a named remote")
    add.add_argument("name")
    add.add_argument("--host", required=True)
    add.add_argument("--user", required=True)
    add.add_argument("--port", type=int, default=22)
    add.add_argument("--key-path", required=True)
    add.add_argument("--known-hosts-path")
    add.add_argument("--json", action="store_true")
    for operation in ("remove", "status"):
        command_parser = commands.add_parser(operation)
        command_parser.add_argument("name")
        command_parser.add_argument("--json", action="store_true")
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    execute = commands.add_parser("exec")
    execute.add_argument("name")
    execute.add_argument("--command-file")
    execute.add_argument("--json", action="store_true")
    execute.add_argument("command", nargs="*")
    for operation in ("push", "pull"):
        command_parser = commands.add_parser(operation)
        command_parser.add_argument("name")
        command_parser.add_argument("src")
        command_parser.add_argument("dest")
        command_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.operation == "add":
        entry = {
            "host": args.host,
            "user": args.user,
            "port": args.port,
            "key_path": args.key_path,
        }
        if args.known_hosts_path:
            entry["known_hosts_path"] = args.known_hosts_path
        value = add_remote(args.name, entry)
    elif args.operation == "remove":
        value = remove_remote(args.name)
    elif args.operation == "list":
        value = list_remotes()
    elif args.operation == "exec":
        command_parts = args.command[1:] if args.command[:1] == ["--"] else args.command
        if args.command_file and command_parts:
            value = vm.operation_error(
                "exec", "remote_exec_invalid",
                "use either arguments after -- or --command-file, not both",
            )
        elif args.command_file:
            try:
                command = Path(args.command_file).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                value = vm.operation_error(
                    "exec", "remote_exec_invalid",
                    "the command file could not be read as UTF-8",
                )
            else:
                value = run_broker_operation("exec", args.name, command=command)
        else:
            value = run_broker_operation(
                "exec", args.name, command=" ".join(command_parts)
            )
    elif args.operation in {"push", "pull"}:
        value = run_broker_operation(
            args.operation, args.name, src=args.src, dest=args.dest
        )
    else:
        value = run_broker_operation("status", args.name)
    if args.json:
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(_human_result(value), file=sys.stdout if value["ok"] else sys.stderr)
    return 0 if value["ok"] else 1


def main(argv: list[str]) -> int:
    return client_main(argv)


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
