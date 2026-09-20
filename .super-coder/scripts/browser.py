#!/usr/bin/env python3
"""Opt-in host browser lifecycle. Private packages, two processes, no credentials."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import browser_launch_guard as guard
import instance_state
import ports

ENGINE = Path(__file__).resolve().parents[1]
PACKAGES = ("@playwright/mcp", "playwright", "playwright-core")
REQUIRED_FLAGS = {
    "--extension",
    "--profile-dir-name",
    "--user-data-dir",
    "--executable-path",
    "--host",
    "--allowed-hosts",
    "--port",
    "--output-dir",
    "--save-session",
    "--timeout-action",
    "--timeout-navigation",
    "--blocked-origins",
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
    # Old installations carry version receipts. They are not admission gates.
    for field in ("server_version", "playwright_version"):
        result.pop(field, None)
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


def inspect_package(node: str, target: Path) -> dict:
    """Exercise the installed CLI and report its actual dependency resolution."""
    script = """const { createRequire } = require('module');
const root = createRequire(process.argv[1]);
const mcp = root.resolve('@playwright/mcp/package.json');
const fromMcp = createRequire(mcp);
const pw = fromMcp.resolve('playwright/package.json');
const core = fromMcp.resolve('playwright-core/package.json');
console.log(JSON.stringify(Object.fromEntries([mcp, pw, core].map(p => {
  const v = require(p); return [v.name, v.version];
}))));"""
    resolved = {}
    try:
        probe = subprocess.run(
            [node, "-e", script, str(target / "package.json")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if probe.returncode:
            raise ValueError("browser package dependencies are missing or cannot load")
        resolved = json.loads(probe.stdout)
        if not all(resolved.get(name) for name in PACKAGES):
            raise ValueError("browser package dependencies are incomplete")
        help_result = subprocess.run(
            [node, str(target / "node_modules/@playwright/mcp/cli.js"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if help_result.returncode:
            raise ValueError(
                "browser MCP CLI cannot run: " + help_result.stderr[-1000:]
            )
        missing = sorted(
            REQUIRED_FLAGS - set(re.findall(r"--[a-z][a-z-]*", help_result.stdout))
        )
        if missing:
            raise ValueError(
                "browser MCP lacks required capabilities: " + ", ".join(missing)
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"ok": False, "versions": resolved, "error": str(exc)}
    return {"ok": True, "versions": resolved, "error": None}


def package_check(*, install: bool = False) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "Node >=20 and npm are required", "versions": {}}
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=5, check=True
    ).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 20:
        return {
            "ok": False,
            "error": "Node >=20 is required",
            "node": version,
            "versions": {},
        }
    target = private() / "package"
    receipt = inspect_package(node, target)
    if receipt["ok"] or not install:
        return {**receipt, "node": version}
    npm = shutil.which("npm")
    if not npm:
        return {
            **receipt,
            "node": version,
            "error": "npm is required to bootstrap browser packages",
        }
    # Prepare and verify a fresh tree before replacing anything. An interrupted
    # or incompatible install never destroys the previous installation.
    with tempfile.TemporaryDirectory(
        prefix="package-install-", dir=private()
    ) as staging:
        candidate = Path(staging) / "package"
        candidate.mkdir(mode=0o700)
        shutil.copyfile(ENGINE / "browser/package.json", candidate / "package.json")
        try:
            completed = subprocess.run(
                [
                    npm,
                    "install",
                    "--prefix",
                    str(candidate),
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--package-lock=false",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode:
                raise ValueError(
                    "browser package installation failed: " + completed.stderr[-2000:]
                )
            receipt = inspect_package(node, candidate)
            if not receipt["ok"]:
                return {**receipt, "node": version}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"ok": False, "node": version, "versions": {}, "error": str(exc)}
        stop_process("proxy")
        stop_process("server")
        previous = Path(staging) / "previous"
        if target.exists():
            target.rename(previous)
        try:
            candidate.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    return {**receipt, "node": version, "repaired": True}


def extension_installed(profile: Path) -> bool:
    # Match upstream detection, including externally installed/unpacked
    # extensions recorded in Preferences rather than the Extensions directory.
    if (profile / "Extensions" / EXTENSION_ID).is_dir():
        return True
    for name in ("Preferences", "Secure Preferences"):
        try:
            prefs = json.loads((profile / name).read_text())
            record = prefs.get("extensions", {}).get("settings", {}).get(EXTENSION_ID)
            if isinstance(record, dict) and record:
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def profile_checks(config: dict) -> dict:
    config = validate(config)
    path = Path(config["user_data_dir"]) / config["profile_dir_name"]
    result: dict = {
        "profile": False,
        "extension": extension_installed(path),
    }
    try:
        guard.check(config)
        result["profile"] = True
    except guard.GuardRefusal as exc:
        result["error"] = str(exc)
    if result["profile"] and not result["extension"]:
        result["error"] = (
            "Open Subfloor and install the Playwright Extension there, then approve the connection"
        )
    return result


def installations() -> list[dict]:
    """Standard Linux Chromium-family roots paired with their launchers."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    choices = [
        (root / "chromium", ("chromium", "chromium-browser")),
        (root / "google-chrome", ("google-chrome", "google-chrome-stable")),
        (
            Path.home() / "snap/chromium/common/chromium",
            ("chromium", "chromium-browser"),
        ),
    ]
    found = []
    for directory, commands in choices:
        executable = next(
            (path for name in commands if (path := shutil.which(name))), None
        )
        if executable and (directory / "Local State").is_file():
            found.append({"user_data_dir": str(directory), "executable": executable})
    return found


def named_profiles(directory: Path) -> list[str]:
    try:
        state = json.loads((directory / "Local State").read_text())
        return [
            key
            for key, info in state.get("profile", {}).get("info_cache", {}).items()
            if info.get("name") == "Subfloor"
        ]
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"Cannot read Chromium profiles in {directory}; check its user data directory"
        ) from exc


def defaults() -> dict:
    """Only suggest an unambiguous existing profile; blank fields mean detect."""
    if os.environ.get("SC_SANDBOX"):
        return {"user_data_dir": "", "executable": ""}
    candidates = []
    for item in installations():
        try:
            if named_profiles(Path(item["user_data_dir"])):
                candidates.append(item)
        except (OSError, ValueError, AttributeError):
            continue
    if len(candidates) == 1:
        return candidates[0]
    return {"user_data_dir": "", "executable": ""}


def resolve_paths(value: dict) -> tuple[Path, Path]:
    # Validate all explicit inputs before touching the filesystem.
    explicit = {
        key: guard.operator_path(value[key])
        for key in ("user_data_dir", "executable")
        if key in value
    }
    detected = defaults()
    directory = explicit.get("user_data_dir")
    if directory is None:
        if not detected["user_data_dir"]:
            raise ValueError(
                "Could not select one Subfloor profile. Create it in Chromium, or set its user data directory in Scripts → Browser"
            )
        directory = Path(detected["user_data_dir"])
    executable = explicit.get("executable")
    if executable is None:
        matching = [
            item for item in installations() if Path(item["user_data_dir"]) == directory
        ]
        path = (
            matching[0]["executable"]
            if matching
            else shutil.which("chromium") or shutil.which("chromium-browser")
        )
        if not path:
            raise ValueError(
                "Chromium was not found on PATH; install it or set its executable in Scripts → Browser"
            )
        executable = Path(path)
    return directory, executable


def open_profile(*, harness: str | None = None, sandbox: bool = False) -> dict:
    require_bare_metal()
    if sandbox or (harness and harness not in SUPPORTED):
        raise ValueError("unsupported seat or harness for browser driving")
    with lifecycle_lock():
        config = read()
        if not config:
            raise ValueError(
                "browser is absent; link the existing Subfloor profile in Scripts → Browser"
            )
        if not config["armed"]:
            raise ValueError(
                "Browser is disarmed; ask the FnB to arm it in Scripts → Browser"
            )
        guard.check(config)
        child = subprocess.Popen(
            guard.command(config, []),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            code = None
        if code not in (None, 0):
            raise ValueError(
                f"Chromium launch failed (exit {code}); check the host graphical session"
            )
    return {
        **status(harness=harness, sandbox=sandbox),
        "launch_requested": True,
        "detail": "Subfloor launch requested; approve the Playwright Extension connection when prompted",
    }


def link(value: dict, *, save: bool = True) -> dict:
    """Resolve the human display name; save only a validated, nonsecret block."""
    require_bare_metal()
    if set(value) - {"executable", "user_data_dir", "profile_name", "blocked_origins"}:
        raise ValueError("unknown browser link field")
    directory, executable = resolve_paths(value)
    display = value.get("profile_name", "Subfloor")
    if display != "Subfloor":
        raise ValueError("use the dedicated profile named Subfloor")
    matches = named_profiles(directory)
    if len(matches) != 1:
        raise ValueError("create exactly one Chromium profile named Subfloor first")
    allocation = ports.ensure_browser_ports()
    # codeql[py/path-injection]
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("set the absolute path to the Chromium executable or launcher")
    config = validate(
        {
            "executable": str(executable),
            "channel": "chromium",
            "user_data_dir": str(directory),
            "profile_dir_name": matches[0],
            "browser_port": allocation["browser_port"],
            "proxy_port": allocation["browser_proxy_port"],
            "armed": True,
            "blocked_origins": value.get("blocked_origins", []),
        }
    )
    checks = profile_checks(config)
    if not checks["profile"]:
        raise ValueError(checks["error"])
    with lifecycle_lock():
        packages = package_check(install=save)
    if not packages["ok"]:
        raise ValueError(
            packages["error"]
            + "; use link profile / sc browser setup to bootstrap, or doctor to repair"
        )
    if not save:
        return {
            "ok": checks["profile"] and checks["extension"],
            "checks": checks,
            "packages": packages,
        }
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
        else (
            "ready"
            if server and proxy and checks["profile"] and checks["extension"]
            else "declared"
        )
    )
    if config["armed"] and not server and (private() / "server.json").exists():
        state = "failed"
    if live.get("protocol_errors"):
        state = "failed"
    if not config["armed"] or not server or not checks["profile"]:
        live = {"active_shells": [], "extension": "not connected"}
    return {
        "state": state,
        "supported": True,
        "armed": config["armed"],
        "server": server,
        "proxy": proxy,
        "checks": checks,
        "connection_ready": bool(live.get("active_shells")),
        "approval": "FnB approval required for every new connection",
        **live,
    }


def operate(action: str) -> dict:
    require_bare_metal()
    if action == "status":
        return status()
    if action == "open":
        return open_profile()
    if action == "doctor":
        with lifecycle_lock():
            packages = package_check(install=True)
        config = read()
        if packages.get("repaired") and config and config["armed"]:
            operate("up")
        st = status()
        setup_ok = packages["ok"] and all(
            st.get("checks", {}).get(key, False) for key in ("profile", "extension")
        )
        return {
            **st,
            "packages": packages,
            "setup_ready": setup_ok,
            "ok": setup_ok
            and st.get("state") == "ready"
            and st.get("connection_ready", False),
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
            packages = package_check(install=True)
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
        "action",
        choices=["status", "open", "up", "down", "doctor", "arm", "disarm", "setup"],
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--executable")
    parser.add_argument("--user-data-dir")
    args = parser.parse_args(argv)
    try:
        config = {
            key: value
            for key in ("executable", "user_data_dir")
            if (value := getattr(args, key)) is not None
        }
        if config and args.action != "setup":
            raise ValueError("path options are only valid with browser setup")
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
        elif args.action == "setup":
            result = link(config)
        else:
            result = operate(args.action)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"state": "failed", "ok": False, "error": str(exc)}
    print(json.dumps(result, indent=2) if args.json else json.dumps(result))
    return 1 if result.get("ok") is False or result.get("state") == "failed" else 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
