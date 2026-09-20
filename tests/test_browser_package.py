"""Exercise installed upstream capabilities; mandatory in the PR test job."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import browser


@pytest.fixture
def package(tmp_path, monkeypatch):
    source = ROOT / ".super-coder/browser"
    if not (source / "node_modules").exists():
        if os.environ.get("SC_REQUIRE_BROWSER_PACKAGE_CANARY"):
            pytest.fail(
                "CI requires npm install --prefix .super-coder/browser --ignore-scripts --package-lock=false"
            )
        pytest.skip("browser package is not installed in this test seat")
    shutil.copytree(source, tmp_path / "package", symlinks=True)
    monkeypatch.setattr(browser, "private", lambda: tmp_path)
    return tmp_path / "package"


def test_installed_resolution_and_profile_flag(package):
    receipt = browser.package_check()
    assert receipt["ok"], receipt
    assert set(receipt["versions"]) == set(browser.PACKAGES)


def test_package_versions_and_lock_are_not_admission_gates(package):
    for name in browser.PACKAGES:
        file = package / "node_modules" / name / "package.json"
        value = json.loads(file.read_text())
        value["version"] = "999.0.0"
        file.write_text(json.dumps(value))
    (package / "package-lock.json").write_text("not a lock")
    assert browser.package_check()["ok"]


def test_missing_cli_capability_is_reported(package):
    (package / "node_modules/@playwright/mcp/cli.js").write_text(
        "console.log('--extension')"
    )
    receipt = browser.package_check()
    assert receipt["ok"] is False
    assert "--profile-dir-name" in receipt["error"]


def test_failed_repair_keeps_previous_tree(package, monkeypatch):
    (package / "node_modules/@playwright/mcp/cli.js").write_text("process.exit(1)")
    original = browser.subprocess.run

    def run(args, **kwargs):
        if "install" in args:
            return browser.subprocess.CompletedProcess(
                args, 1, "", "fixture registry offline"
            )
        return original(args, **kwargs)

    monkeypatch.setattr(browser.subprocess, "run", run)
    receipt = browser.package_check(install=True)
    assert not receipt["ok"] and "registry offline" in receipt["error"]
    assert (
        package / "node_modules/@playwright/mcp/cli.js"
    ).read_text() == "process.exit(1)"


def test_repair_replaces_incomplete_tree_after_validation(package, monkeypatch):
    original = browser.subprocess.run
    events = []

    def run(args, **kwargs):
        if "install" in args:
            candidate = Path(args[args.index("--prefix") + 1])
            shutil.copytree(package / "node_modules", candidate / "node_modules")
            return browser.subprocess.CompletedProcess(args, 0, "", "")
        return original(args, **kwargs)

    monkeypatch.setattr(browser.subprocess, "run", run)
    inspect = browser.inspect_package
    monkeypatch.setattr(
        browser,
        "inspect_package",
        lambda node, target: (
            {"ok": False, "error": "incomplete", "versions": {}}
            if target == package
            else inspect(node, target)
        ),
    )
    monkeypatch.setattr(browser, "stop_process", lambda name: events.append(name))
    receipt = browser.package_check(install=True)
    assert receipt["ok"] and receipt["repaired"]
    assert events == ["proxy", "server"]
    assert inspect(shutil.which("node"), package)["ok"]


def test_server_argv_and_environment_are_not_upstream_configurable(
    package, monkeypatch, tmp_path
):
    config = {
        "profile_dir_name": "Profile 1",
        "user_data_dir": "/fixture/chromium",
        "browser_port": 8870,
        "blocked_origins": [],
    }
    command = browser.server_command(config)
    assert "--extension" in command and "--profile-dir-name" in command
    assert "--allow-unrestricted-file-access" not in command
    assert "--headless" not in command and "--cdp-endpoint" not in command
    assert "browser_launch_guard.py" in command[command.index("--executable-path") + 1]
    monkeypatch.setenv("PLAYWRIGHT_MCP_EXTENSION_TOKEN", "fixture-secret-never-forward")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://wrong")
    monkeypatch.setattr(browser, "process_receipt", lambda name: None)
    monkeypatch.setattr(browser, "process_identity", lambda pid: "123")
    seen = []

    class Child:
        pid = 123

    def spawn(*args, **kwargs):
        seen.append(kwargs)
        return Child()

    monkeypatch.setattr(browser.subprocess, "Popen", spawn)
    browser.start_process("server", command, config)
    assert not any(key.startswith("PLAYWRIGHT_") for key in seen[0]["env"])
    assert "fixture-secret-never-forward" not in json.dumps(seen, default=str)


def test_real_locked_server_discovery_through_gate_without_browser(
    package, monkeypatch
):
    import select
    import socket
    import subprocess
    import threading

    import browser_proxy

    from tests.test_browser_proxy import request

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = {
        "profile_dir_name": "Profile 1",
        "user_data_dir": "/unused/fixture",
        "browser_port": port,
        "blocked_origins": [],
        "armed": True,
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG"}
    }
    child = subprocess.Popen(
        browser.server_command(config),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    gate = None
    try:
        assert select.select([child.stderr], [], [], 5)[0], (
            "server did not announce readiness"
        )
        assert b"Listening on" in child.stderr.readline()
        gate = browser_proxy.Gate(
            ("127.0.0.1", 0), config, state=lambda: config
        )
        monkeypatch.setattr(
            browser, "process_receipt", lambda name: {"start": "fixture"}
        )
        threading.Thread(target=gate.serve_forever, daemon=True).start()
        sid, initialized, _ = request(
            gate,
            "DEV5",
            "initialize",
            params={"protocolVersion": "2025-03-26", "capabilities": {}},
        )
        assert "result" in initialized
        _, listed, _ = request(gate, "DEV5", "tools/list", sid)
        assert "result" in listed, listed
        assert "browser_snapshot" in {t["name"] for t in listed["result"]["tools"]}
        config["armed"] = False
        _, listed, _ = request(gate, "DEV5", "tools/list", sid)
        assert "result" in listed, listed
        assert "browser_snapshot" in {t["name"] for t in listed["result"]["tools"]}
        _, refused, _ = request(
            gate,
            "DEV5",
            "tools/call",
            sid,
            {"name": "browser_snapshot", "arguments": {}},
        )
        assert "FnB" in refused["error"]["message"]
    finally:
        if gate:
            gate.shutdown()
            gate.server_close()
        child.terminate()
        child.wait(timeout=5)
        child.stderr.close()


def test_managed_lifecycle_two_processes_and_cleanup(package, tmp_path, monkeypatch):
    import ports

    repo = tmp_path / "fixture-repo"
    engine = repo / ".super-coder"
    scripts = engine / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "browser.py",
        "browser_proxy.py",
        "browser_launch_guard.py",
        "instance_state.py",
        "ports.py",
        "cli_entry.py",
    ):
        shutil.copyfile(ROOT / ".super-coder/scripts" / name, scripts / name)
    shutil.copytree(
        ROOT / ".super-coder/browser",
        engine / "browser",
        ignore=shutil.ignore_patterns("node_modules"),
    )
    private = repo / ".sc-state/local/browser"
    private.mkdir(parents=True)
    shutil.move(str(package), str(private / "package"))
    monkeypatch.setattr(browser, "private", lambda: private)
    monkeypatch.setattr(browser, "ENGINE", engine)
    monkeypatch.setattr(ports, "CONFIG", engine / "instance.json")
    monkeypatch.setattr(ports, "REPO_ROOT", repo)
    ports.CONFIG.write_text('{"port":8800,"dev_port":8801}')
    allocation = ports.ensure_browser_ports()
    config = {
        "executable": "/unused/chromium",
        "channel": "chromium",
        "profile_dir_name": "Profile 1",
        "user_data_dir": "/unused/fixture",
        "browser_port": allocation["browser_port"],
        "proxy_port": allocation["browser_proxy_port"],
        "server_version": "0.0.80",
        "playwright_version": "1.63.0",
        "blocked_origins": [],
        "armed": True,
    }
    ports.update({"browser": config})
    try:
        up = browser.operate("up")
        assert up["server"] and up["proxy"], up
        stopped = browser.operate("disarm")
        assert not stopped["server"] and stopped["proxy"]
        assert browser.operate("arm")["server"]
        down = browser.operate("down")
        assert not down["server"] and not down["proxy"]
        assert ports.resolve()["browser"]["armed"] is True
        browser.operate("disable")
        assert "browser" not in ports.resolve()
    finally:
        browser.stop_process("proxy")
        browser.stop_process("server")


@pytest.mark.parametrize("source_mode", [False, True])
def test_installed_browser_package_allows_restricted_launch(
    package, tmp_path, monkeypatch, source_mode,
):
    import execution_view

    from tests.test_execution_view import installation, run_in

    repo, engine, env, private_root = installation(tmp_path)
    private = private_root / "browser"
    private.mkdir()
    installed = private / "package"
    shutil.move(str(package), str(installed))
    monkeypatch.setattr(browser, "private", lambda: private)
    assert browser.package_check()["ok"]
    assert (installed / "node_modules/.bin/playwright-mcp").is_symlink()
    view = execution_view.build(
        engine=engine, repo_root=repo, flavor="planner",
        source_mode=source_mode, environ=env,
    )
    view.preflight()
    result = run_in(
        view,
        f"! cat {installed / 'node_modules/.bin/playwright-mcp'} >/dev/null 2>&1 && "
        f"! cat {private_root / 'shell_db.db'} >/dev/null 2>&1",
    )
    assert result.returncode == 0, result.stderr
