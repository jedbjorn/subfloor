"""The browser gate never needs a real profile/browser for its local proof."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
import browser
import browser_launch_guard as guard
import ports


@pytest.fixture
def config(tmp_path):
    executable = tmp_path / "chromium"
    executable.touch(mode=0o700)
    return {
        "executable": str(executable),
        "channel": "chromium",
        "user_data_dir": str(tmp_path),
        "profile_dir_name": "Profile 1",
        "server_version": "0.0.80",
        "playwright_version": "1.63.0",
        "browser_port": 8870,
        "proxy_port": 8871,
        "armed": True,
    }


def window(config, tmp_path, active=None):
    (tmp_path / "SingletonLock").symlink_to(socket.gethostname() + "-123")
    (tmp_path / "Local State").write_text(
        json.dumps({"profile": {"last_active_profiles": active or ["Profile 1"]}})
    )
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123/exe").symlink_to(config["executable"])
    return proc


def test_guard_active_profile(config, tmp_path):
    guard.check(config, proc=window(config, tmp_path))


@pytest.mark.parametrize(
    "failure",
    ["missing-lock", "stale-lock", "missing-state", "malformed-state", "wrong-profile"],
)
def test_guard_refuses(config, tmp_path, failure):
    proc = window(config, tmp_path)
    if failure == "missing-lock":
        (tmp_path / "SingletonLock").unlink()
    elif failure == "stale-lock":
        (proc / "123/exe").unlink()
    elif failure == "missing-state":
        (tmp_path / "Local State").unlink()
    elif failure == "malformed-state":
        (tmp_path / "Local State").write_text("[]")
    else:
        (tmp_path / "Local State").write_text(
            '{"profile":{"last_active_profiles":["Default"]}}'
        )
    with pytest.raises(guard.GuardRefusal, match="extension not connected"):
        guard.check(config, proc=proc)


def test_guard_exec_preserves_argv(config, tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("SC_BROWSER_GUARD_CONFIG", str(path))
    monkeypatch.setattr(guard, "check", lambda config: None)
    called = []
    monkeypatch.setattr(os, "execv", lambda *args: called.append(args))
    args = [
        "--profile-directory=Profile 1",
        "chrome-extension://extension/connect.html?a=b",
    ]
    assert guard.main(args) == 0
    assert called == [(config["executable"], [config["executable"], *args])]


@pytest.mark.parametrize(
    "field,value",
    [
        ("armed", "true"),
        ("profile_dir_name", "../Default"),
        ("proxy_port", 8870),
        ("server_version", "latest"),
        ("extra", "value"),
    ],
)
def test_block_rejects_unsafe_fields(config, field, value):
    config[field] = value
    with pytest.raises(ValueError):
        browser.validate(config)


def test_private_modes(tmp_path):
    p = tmp_path / "receipt"
    browser.write_private(p, {"pid": 123})
    assert p.stat().st_mode & 0o777 == 0o600


def test_browser_ports_stable_and_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(ports, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ports, "CONFIG", tmp_path / "instance.json")
    ports.CONFIG.write_text('{"port":8800,"dev_port":8801}')
    first = ports.ensure_browser_ports()
    second = ports.ensure_browser_ports()
    assert first == second
    assert (
        len(
            {
                first[k]
                for k in ("port", "dev_port", "browser_port", "browser_proxy_port")
            }
        )
        == 4
    )


def test_seats():
    assert browser.status(sandbox=True)["detail"].startswith("unsupported seat")
    for harness in ("kimi", "vibe"):
        assert browser.status(harness=harness)["supported"] is False


def test_lifecycle_disarm_stops_server_keeps_gate(config, monkeypatch):
    from contextlib import nullcontext

    events = []
    monkeypatch.setattr(browser, "read", lambda: config)
    monkeypatch.setattr(browser, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(
        browser.ports,
        "update",
        lambda changes: events.append(("update", changes["browser"]["armed"])),
    )
    monkeypatch.setattr(
        browser, "stop_process", lambda name: events.append(("stop", name))
    )
    monkeypatch.setattr(
        browser, "start_process", lambda name, *args: events.append(("start", name))
    )
    monkeypatch.setattr(browser, "status", lambda: {"state": "disarmed"})
    assert browser.operate("disarm")["state"] == "disarmed"
    assert events == [("update", False), ("stop", "server"), ("start", "proxy")]


def test_drift_prevents_start(config, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(browser, "read", lambda: config)
    monkeypatch.setattr(browser, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(
        browser, "package_check", lambda: {"ok": False, "error": "drift"}
    )
    monkeypatch.setattr(
        browser, "start_process", lambda *args: pytest.fail("must not start")
    )
    with pytest.raises(ValueError, match="drift"):
        browser.operate("up")
