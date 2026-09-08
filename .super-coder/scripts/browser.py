#!/usr/bin/env python3
"""Opt-in host browser lifecycle. Private packages, two processes, no credentials."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import browser_launch_guard as guard
import instance_state
import ports

ENGINE = Path(__file__).resolve().parents[1]
VERSIONS = {
    "@playwright/mcp": "0.0.80",
    "playwright": "1.63.0",
    "playwright-core": "1.63.0",
}
EXTENSION_ID = "mmlmfjhmonkocbjadbfplnigmagldckm"
TIMEOUT = 30
UNSUPPORTED_SEAT = "unsupported seat: browser requires bare metal"
SUPPORTED = ("claude", "codex", "opencode")
FIELDS = {
    "executable",
    "channel",
    "user_data_dir",
    "profile_dir_name",
    "server_version",
    "playwright_version",
    "browser_port",
    "proxy_port",
    "blocked_origins",
    "armed",
}


def require_bare_metal() -> None:
    """Setup and lifecycle need the host's Chromium profile and process table.

    On the docker runtime the API server itself runs in `sc-<repo>`, which
    mounts no browser profile and has its own PID namespace, so the profile and
    window probes would read the container instead of the operator's host.
    Refuse by seat rather than reporting a host path as missing (issue #1556)."""
    if os.environ.get("SC_SANDBOX"):
        raise ValueError(UNSUPPORTED_SEAT)


def read() -> dict | None:
    value = ports.resolve().get("browser")
    return validate(value) if value is not None else None


def validate(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError(
            "browser must be an object containing only documented configuration fields"
        )
    result = dict(value)
    for field in ("executable", "user_data_dir"):
        guard.operator_path(result.get(field))
    profile = result.get("profile_dir_name")
    if (
        not isinstance(profile, str)
        or profile in ("", ".", "..")
        or "/" in profile
        or "\\" in profile
    ):
        raise ValueError("browser profile_dir_name must be one directory name")
    if result.get("channel") != "chromium":
        raise ValueError("browser channel must be chromium")
    if (
        result.get("server_version") != VERSIONS["@playwright/mcp"]
        or result.get("playwright_version") != VERSIONS["playwright"]
    ):
        raise ValueError("browser package versions differ from the engine lock")
    for field in ("browser_port", "proxy_port"):
        if type(result.get(field)) is not int or not 8800 <= result[field] < 8900:
            raise ValueError(f"browser {field} must be in 8800..8899")
    if result["browser_port"] == result["proxy_port"]:
        raise ValueError("browser server and proxy ports must be distinct")
    if type(result.get("armed")) is not bool:
        raise ValueError("browser armed must be a boolean")
    origins = result.setdefault("blocked_origins", [])
    if not isinstance(origins, list) or not all(
        isinstance(x, str) and x and ";" not in x for x in origins
    ):
        raise ValueError("browser blocked_origins must be a list of origins")
    return result


def private() -> Path:
    root = instance_state.maintenance_state(ENGINE).root / "browser"
    if root.is_symlink():
        raise ValueError("unsafe browser private directory")
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    return root


def write_private(path: Path, value: dict) -> None:
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def lifecycle_lock():
    fd = os.open(
        private() / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def package_check(*, install: bool = False) -> dict:
    node = shutil.which("node")
    if not node:
        return {
            "ok": False,
            "state": "unavailable",
            "error": "Node >=20 is required",
            "versions": {},
        }
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=5, check=True
    ).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 20:
        return {
            "ok": False,
            "state": "unavailable",
            "error": "Node >=20 is required",
            "node": version,
            "versions": {},
        }
    source = ENGINE / "browser"
    target = private() / "package"
    expected = json.loads((source / "package.json").read_text())
    if expected.get("dependencies") != {
        "@playwright/mcp": VERSIONS["@playwright/mcp"]
    } or expected.get("overrides") != {
        k: VERSIONS[k] for k in ("playwright", "playwright-core")
    }:
        raise ValueError("engine browser manifest drift")
    lock_bytes = (source / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    for name, wanted in VERSIONS.items():
        if lock["packages"].get("node_modules/" + name, {}).get("version") != wanted:
            raise ValueError(f"engine browser lock drift: {name}")
    if install and not (target / "node_modules").exists():
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("npm is required to install the locked browser package")
        target.mkdir(mode=0o700, exist_ok=True)
        for filename in ("package.json", "package-lock.json"):
            shutil.copyfile(source / filename, target / filename)
        completed = subprocess.run(
            [
                npm,
                "ci",
                "--prefix",
                str(target),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode:
            raise ValueError(
                "locked browser installation failed: " + completed.stderr[-2000:]
            )
    resolved = {}
    for name in VERSIONS:
        path = target / "node_modules" / name / "package.json"
        resolved[name] = (
            json.loads(path.read_text()).get("version") if path.is_file() else None
        )
    # Check the dependency resolution used by the CLI, including shadowing by
    # nested node_modules (top-level package versions alone do not prove it).
    resolution_ok = False
    if resolved == VERSIONS:
        script = """const { createRequire } = require('module');
const root = createRequire(process.argv[1]);
const mcp = root.resolve('@playwright/mcp/package.json');
const fromMcp = createRequire(mcp);
const pw = fromMcp.resolve('playwright/package.json');
const core = fromMcp.resolve('playwright-core/package.json');
const fromPw = createRequire(pw);
console.log(JSON.stringify([mcp, pw, core, fromPw.resolve('playwright-core/package.json')]));"""
        checked = subprocess.run(
            [node, "-e", script, str(target / "package.json")],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        expected_paths = [
            str(target / "node_modules" / name / "package.json") for name in VERSIONS
        ]
        resolution_ok = checked.returncode == 0 and json.loads(checked.stdout) == [
            *expected_paths,
            expected_paths[-1],
        ]
    same_lock = (target / "package-lock.json").is_file() and (
        target / "package-lock.json"
    ).read_bytes() == lock_bytes
    same_manifest = (target / "package.json").is_file() and json.loads(
        (target / "package.json").read_text()
    ) == expected
    ok = resolved == VERSIONS and same_lock and same_manifest and resolution_ok
    if ok:
        help_result = subprocess.run(
            [node, str(target / "node_modules/@playwright/mcp/cli.js"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        ok = help_result.returncode == 0 and "--profile-dir-name" in help_result.stdout
    return {
        "ok": ok,
        "node": version,
        "versions": resolved,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "error": None
        if ok
        else "browser packages missing or drifted; doctor installs only an absent package tree",
    }


def profile_checks(config: dict) -> dict:
    config = validate(config)
    path = Path(config["user_data_dir"]) / config["profile_dir_name"]
    # The host operator chooses this root; validate confines the profile name
    # to one component. These probes intentionally inspect that chosen profile.
    result = {
        # codeql[py/path-injection]
        "profile": path.is_dir(),
        # codeql[py/path-injection]
        "extension": (path / "Extensions" / EXTENSION_ID).is_dir(),
    }
    try:
        guard.check(config)
        result["window"] = True
    except guard.GuardRefusal as exc:
        result.update(window=False, error=str(exc))
    return result


def link(value: dict, *, save: bool = True) -> dict:
    """Resolve the human display name; save only a validated, nonsecret block."""
    require_bare_metal()
    if set(value) - {"executable", "user_data_dir", "profile_name", "blocked_origins"}:
        raise ValueError("unknown browser link field")
    directory = guard.operator_path(
        value.get("user_data_dir", str(Path.home() / ".config/chromium"))
    )
    executable = guard.operator_path(
        value.get("executable", "/usr/lib/chromium/chromium")
    )
    # Host-operator setup accepts an arbitrary Chromium installation/profile
    # root. The API gates shell credentials and cross-origin requests before
    # this function. Never use it for paths supplied by MCP tools.
    # codeql[py/path-injection]
    state = json.loads((directory / "Local State").read_text())
    display = value.get("profile_name", "Subfloor")
    if display != "Subfloor":
        raise ValueError("use the dedicated profile named Subfloor")
    matches = [
        key
        for key, info in state.get("profile", {}).get("info_cache", {}).items()
        if info.get("name") == display
    ]
    if len(matches) != 1:
        raise ValueError("create exactly one Chromium profile named Subfloor first")
    allocation = ports.ensure_browser_ports()
    # codeql[py/path-injection]
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("set the absolute path to the native Chromium executable")
    config = validate(
        {
            "executable": str(executable),
            "channel": "chromium",
            "user_data_dir": str(directory),
            "profile_dir_name": matches[0],
            "server_version": VERSIONS["@playwright/mcp"],
            "playwright_version": VERSIONS["playwright"],
            "browser_port": allocation["browser_port"],
            "proxy_port": allocation["browser_proxy_port"],
            "armed": True,
            "blocked_origins": value.get("blocked_origins", []),
        }
    )
    checks = profile_checks(config)
    if not checks["profile"] or not checks["extension"]:
        raise ValueError(
            "create the Subfloor profile and install the Playwright Extension first"
        )
    with lifecycle_lock():
        packages = package_check(install=True)
    if not packages["ok"]:
        raise ValueError(packages["error"])
    if not save:
        return {"ok": True, "checks": checks, "packages": packages}
    with lifecycle_lock():
        stop_process("proxy")
        stop_process("server")
        ports.update({"browser": config})
    return operate("up")


def process_identity(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else stat[19]
    except (OSError, IndexError):
        return None


def process_receipt(name: str) -> dict | None:
    path = private() / (name + ".json")
    try:
        receipt = json.loads(path.read_text())
        if type(receipt.get("pid")) is not int or not receipt.get("start"):
            return None
        return receipt if process_identity(receipt["pid"]) == receipt["start"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stop_process(name: str) -> None:
    receipt = process_receipt(name)
    if receipt:
        # pidfd binds the signal to this process, not a later recycled pid.
        try:
            fd = os.pidfd_open(receipt["pid"])
        except ProcessLookupError:
            (private() / (name + ".json")).unlink(missing_ok=True)
            return
        try:
            if process_identity(receipt["pid"]) == receipt["start"]:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                if not select.select([fd], [], [], 5)[0]:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)
    (private() / (name + ".json")).unlink(missing_ok=True)


def server_command(config: dict) -> list[str]:
    cmd = [
        shutil.which("node") or "node",
        str(private() / "package/node_modules/@playwright/mcp/cli.js"),
        "--extension",
        "--profile-dir-name",
        config["profile_dir_name"],
        "--user-data-dir",
        config["user_data_dir"],
        "--executable-path",
        str(ENGINE / "scripts/browser_launch_guard.py"),
        "--host",
        "127.0.0.1",
        "--allowed-hosts",
        f"127.0.0.1:{config['browser_port']}",
        "--port",
        str(config["browser_port"]),
        "--output-dir",
        str(private() / "sessions"),
        "--save-session",
        "--timeout-action",
        str(TIMEOUT * 1000),
        "--timeout-navigation",
        str(TIMEOUT * 1000),
    ]
    if config["blocked_origins"]:
        cmd += ["--blocked-origins", ";".join(config["blocked_origins"])]
    return cmd


def start_process(name: str, command: list[str], config: dict) -> None:
    if process_receipt(name):
        return
    # Do not inherit upstream configuration, credentials, or debug logging.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "LANG",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "XDG_STATE_HOME",
            "DBUS_SESSION_BUS_ADDRESS",
        }
    }
    env["SC_BROWSER_GUARD_CONFIG"] = str(private() / "guard.json")
    write_private(private() / "guard.json", config)
    sessions = private() / "sessions"
    sessions.mkdir(mode=0o700, exist_ok=True)
    sessions.chmod(0o700)
    ready_read, ready_write = os.pipe()
    env["SC_BROWSER_READY_FD"] = str(ready_write)
    log_fd = os.open(
        private() / (name + ".log"),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            env=env,
            cwd=ENGINE.parent,
            start_new_session=True,
            pass_fds=(ready_write,) if name == "proxy" else (),
        )
    except OSError:
        os.close(ready_read)
        raise
    finally:
        os.close(log_fd)
        os.close(ready_write)
    try:
        write_private(
            private() / (name + ".json"),
            {"pid": child.pid, "start": process_identity(child.pid)},
        )
        if name == "proxy" and (
            not select.select([ready_read], [], [], 5)[0]
            or os.read(ready_read, 1) != b"1"
        ):
            stop_process(name)
            raise ValueError("browser proxy failed to bind; inspect its private log")
    finally:
        os.close(ready_read)


def proxy_status(config: dict) -> dict:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{config['proxy_port']}/status", timeout=1
        ) as response:
            return json.load(response)
    except (OSError, ValueError):
        return {"active_shells": [], "extension": "not connected"}


def status(*, harness: str | None = None, sandbox: bool = False) -> dict:
    if sandbox or os.environ.get("SC_SANDBOX"):
        return {
            "state": "failed",
            "supported": False,
            "detail": UNSUPPORTED_SEAT,
        }
    if harness and harness not in SUPPORTED:
        return {
            "state": "failed",
            "supported": False,
            "detail": f"{harness}: managed browser MCP is unsupported",
        }
    config = read()
    if config is None:
        return {"state": "absent", "supported": True, "armed": False, "server": False}
    server = bool(process_receipt("server"))
    proxy = bool(process_receipt("proxy"))
    checks = profile_checks(config)
    live = (
        proxy_status(config)
        if proxy
        else {"active_shells": [], "extension": "not connected"}
    )
    state = (
        "disarmed"
        if not config["armed"]
        else ("ready" if server and proxy and checks["window"] else "declared")
    )
    if config["armed"] and not server and (private() / "server.json").exists():
        state = "failed"
    if live.get("protocol_errors"):
        state = "failed"
    if not config["armed"] or not server or not checks["window"]:
        live = {"active_shells": [], "extension": "not connected"}
    return {
        "state": state,
        "supported": True,
        "armed": config["armed"],
        "server": server,
        "proxy": proxy,
        "checks": checks,
        "approval": "FnB approval required for every new connection",
        **live,
    }


def operate(action: str) -> dict:
    require_bare_metal()
    if action == "status":
        return status()
    if action == "doctor":
        with lifecycle_lock():
            packages = package_check(install=True)
        st = status()
        return {
            **st,
            "packages": packages,
            "ok": packages["ok"]
            and not st.get("protocol_errors")
            and st.get("checks", {}).get("profile", False)
            and st.get("checks", {}).get("extension", False),
        }
    with lifecycle_lock():
        config = read()
        if action in ("down", "disable"):
            stop_process("proxy")
            stop_process("server")
            if action == "disable":
                ports.update({}, remove=("browser",))
            return status()
        if not config:
            raise ValueError("browser is absent; link Scripts → Browser first")
        if action in ("arm", "disarm"):
            config["armed"] = action == "arm"
            ports.update({"browser": config})
        if not config["armed"]:
            stop_process("server")
        elif action in ("up", "arm"):
            packages = package_check()
            if not packages["ok"]:
                raise ValueError(packages["error"])
            start_process("server", server_command(config), config)
        try:
            start_process(
                "proxy",
                [sys.executable, str(ENGINE / "scripts/browser_proxy.py")],
                config,
            )
        except (OSError, ValueError):
            stop_process("server")
            raise
    return status()


def set_grants(con, enabled: bool) -> None:
    """Feature-owned grants remain opt-in across ordinary skill reseeding."""
    import skill
    import skill_projection

    row = con.execute(
        "SELECT skill_id FROM skills WHERE name='drive_browser' AND is_deleted=0"
    ).fetchone()
    if row is None:
        raise ValueError("drive_browser is not seeded; reconcile the engine first")
    flavors = ("dev", "reviewer", "planner", "admin")
    for flavor in flavors:
        if enabled:
            con.execute(
                "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) VALUES (?,?)",
                (flavor, row[0]),
            )
        else:
            con.execute(
                "DELETE FROM flavor_skills WHERE flavor=? AND skill_id=?",
                (flavor, row[0]),
            )
    con.commit()
    skill._persist_mutation(
        con,
        "browser feature grants",
        lambda: skill_projection.reconcile_flavors(con, flavors),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sc browser")
    parser.add_argument(
        "action", choices=["status", "up", "down", "doctor", "arm", "disarm"]
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("SC_API_TOKEN"):
            import mem

            mem._require_api()
            result = mem._api(
                "POST",
                "/_sc/browser",
                {
                    "action": args.action,
                    "sandbox": bool(os.environ.get("SC_SANDBOX")),
                    "harness": os.environ.get("SC_HARNESS"),
                },
            )
        else:
            result = operate(args.action)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"state": "failed", "ok": False, "error": str(exc)}
    print(json.dumps(result, indent=2) if args.json else json.dumps(result))
    return 1 if result.get("ok") is False or result.get("state") == "failed" else 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
