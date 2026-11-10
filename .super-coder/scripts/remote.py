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
import project_root
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


def do_push(name: object, src: object, dest: object) -> dict:
    entry, error = _connection(name)
    if error:
        return error
    if not isinstance(src, str) or not isinstance(dest, str) or not dest:
        return _error("remote_push_invalid", "push requires source and destination")
    # One helper for both seats: the same roots and the same carved-out
    # sub-trees `./sc vm push` applies (vm.transfer_path).
    source, path_error = vm.transfer_path(src, "push")
    if path_error:
        return path_error
    assert source is not None
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
    if not dest:
        return _error("remote_pull_invalid", "pull requires source and destination")
    destination, path_error = vm.transfer_path(dest, "pull")
    if path_error:
        return path_error
    assert destination is not None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _error("remote_pull_invalid", f"pull destination unusable: {exc}")
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


LINK_NEXT = {
    "remote_key_invalid": (
        "point --key-path at an absolute, host-owned private key and "
        "`chmod 600` it, then re-run ./sc remote add"
    ),
    "remote_known_hosts_invalid": (
        "pass an absolute --known-hosts-path, or omit it to let the broker "
        "pin the host key"
    ),
    "remote_config_invalid": "re-run ./sc remote add with --host, --user and --key-path",
    "remote_unreachable": (
        "check --host/--user/--port and that the key is authorized on the "
        "remote, then ./sc remote status <name>"
    ),
    "broker_unreachable": "./sc vm-broker-up on the host, then ./sc remote status <name>",
}


def verify_remote(name: str) -> dict:
    """Test the SAVED remote once over SSH; pass/fail and the next step.

    On the host the check runs in this process (the key is here). A sandboxed
    caller holds no key, so it asks the broker.
    """
    if os.environ.get("SC_SANDBOX"):
        try:
            response = vm.broker_call(
                "GET", f"/remote/{name}/status", None, timeout=30
            )
        except (vm.BrokerConnectionError, vm.BrokerTimeoutError,
                vm.BrokerResponseError):
            response = {
                "ok": False, "error": "broker_unreachable",
                "output": "not run: the VM broker is not reachable from here",
            }
    else:
        response = do_status(name)
    ok = response.get("ok") is True
    code = str(response.get("error") or "remote_unreachable")
    check = {
        "check": "ssh", "ok": ok,
        "skipped": not ok and code == "broker_unreachable",
        "output": str(response.get("output") or "")[:300],
        "next": None if ok else LINK_NEXT.get(code, LINK_NEXT["remote_unreachable"]),
    }
    return {"passed": ok, "checks": [check]}


def link_remote(name: object, entry: dict, verify: bool = True) -> dict:
    """`./sc remote add`: save, start the broker, and test the link.

    The block is durable before the check runs, so a failure reports
    `remote_verify_failed` and says the remote IS saved. `verify=False` is the
    save-only form for offline setup.
    """
    value = add_remote(name, entry)
    if not value["ok"] or not verify:
        return value
    assert isinstance(name, str)
    broker = vm.ensure_broker()
    verified = verify_remote(name)
    public = value["result"]["remote"]
    if verified["passed"]:
        return vm.operation_success(
            "add", {"remote": public, "broker": broker, "verify": verified}
        )
    return vm.operation_error(
        "add", "remote_verify_failed",
        f"remote '{name}' IS saved, but the link did not verify. Fix what the "
        "check names and re-run ./sc remote add, or pass --no-verify to save "
        "without testing.",
        {"saved": True, "remote": public, "checks": verified["checks"],
         "broker": broker},
    )


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
        # `./sc vm-broker-up` starts nothing while no block exists, so say
        # which case this is instead of always pointing at the broker.
        if name not in read_all():
            return vm.operation_error(
                operation, "remote_not_found",
                f"remote '{name}' is not declared on this install - add it "
                "with ./sc remote add (./sc remote list shows what is linked)",
            )
        return vm.operation_error(
            operation, "broker_unreachable",
            f"remote '{name}' is declared but the VM broker is not running - "
            "start it on the host with ./sc vm-broker-up",
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
        head = f"✗ remote {value['operation']} [{error['code']}]: {error['message']}"
        details = error.get("details") or {}
        if error["code"] == "remote_verify_failed":
            return "\n".join([
                head, *vm.link_lines(details.get("checks"), details.get("broker"))
            ])
        return head
    operation = value["operation"]
    result = value["result"]
    if operation == "add":
        if "verify" in result:
            return "\n".join([
                f"Remote '{result['remote']['name']}' saved · link verified",
                *vm.link_lines(result["verify"]["checks"], result["broker"]),
            ])
        state = "ready" if result["broker"]["ready"] else (
            "not running; start: ./sc vm-broker-up"
        )
        return (
            f"Remote '{result['remote']['name']}' saved (not verified) · "
            f"broker {state}"
        )
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


def build_client_parser() -> argparse.ArgumentParser:
    """The `./sc remote` parser. Every subcommand carries a one-line help so
    `./sc remote --help` describes the verb list instead of only naming it."""
    parser = argparse.ArgumentParser(
        prog="./sc remote",
        description="Use broker-served SSH against named remotes.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    add = commands.add_parser(
        "add",
        help="add or replace a named remote, start the broker and test the link",
    )
    add.add_argument("name")
    add.add_argument("--host", required=True)
    add.add_argument("--user", required=True)
    add.add_argument("--port", type=int, default=22)
    add.add_argument("--key-path", required=True)
    add.add_argument("--known-hosts-path")
    add.add_argument(
        "--no-verify", action="store_true",
        help="save the remote without testing the link or starting the broker "
             "(offline setup)",
    )
    add.add_argument("--json", action="store_true")
    for operation, description in (
        ("remove", "forget a named remote"),
        ("status", "check that a named remote answers over the broker"),
    ):
        command_parser = commands.add_parser(operation, help=description)
        command_parser.add_argument("name")
        command_parser.add_argument("--json", action="store_true")
    list_parser = commands.add_parser("list", help="list the configured remotes")
    list_parser.add_argument("--json", action="store_true")
    execute = commands.add_parser(
        "exec", help="run a command on a named remote over the broker"
    )
    execute.add_argument("name")
    execute.add_argument("--command-file")
    execute.add_argument("--json", action="store_true")
    execute.add_argument("command", nargs="*")
    for operation, description in (
        ("push", "copy a file from this repo to a named remote"),
        ("pull", "copy a file from a named remote into this repo "
                 "or .sc-state/local"),
    ):
        command_parser = commands.add_parser(operation, help=description)
        command_parser.add_argument("name")
        command_parser.add_argument("src")
        command_parser.add_argument("dest")
        command_parser.add_argument("--json", action="store_true")
    return parser


def client_main(argv: list[str]) -> int:
    args = build_client_parser().parse_args(argv)
    if args.operation == "add":
        entry = {
            "host": args.host,
            "user": args.user,
            "port": args.port,
            "key_path": args.key_path,
        }
        if args.known_hosts_path:
            entry["known_hosts_path"] = args.known_hosts_path
        value = link_remote(args.name, entry, verify=not args.no_verify)
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
                command = project_root.invocation_path(
                    args.command_file).read_text(encoding="utf-8")
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
