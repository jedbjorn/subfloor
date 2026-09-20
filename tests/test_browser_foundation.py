"""The browser gate never needs a real profile/browser for its local proof."""

import json
import os
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


def profile(config, tmp_path, active=None):
    (tmp_path / "Profile 1").mkdir()
    (tmp_path / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {"Profile 1": {"name": "Subfloor"}},
                    "last_active_profiles": active or [],
                    "last_used": "Default",
                }
            }
        )
    )


@pytest.mark.parametrize("active", [[], ["Default"], ["Profile 1"]])
def test_guard_existing_profile_needs_no_window_or_process_access(
    config, tmp_path, active
):
    profile(config, tmp_path, active)
    guard.check(config)


@pytest.mark.parametrize(
    "failure",
    [
        "missing-profile",
        "missing-executable",
        "missing-state",
        "malformed-state",
        "wrong-profile",
    ],
)
def test_guard_refuses(config, tmp_path, failure):
    profile(config, tmp_path)
    if failure == "missing-profile":
        (tmp_path / "Profile 1").rmdir()
    elif failure == "missing-executable":
        Path(config["executable"]).unlink()
    elif failure == "missing-state":
        (tmp_path / "Local State").unlink()
    elif failure == "malformed-state":
        (tmp_path / "Local State").write_text("[]")
    else:
        (tmp_path / "Local State").write_text(
            '{"profile":{"info_cache":{"Profile 1":{"name":"Personal"}}}}'
        )
    with pytest.raises(guard.GuardRefusal, match="browser profile unavailable"):
        guard.check(config)


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
    assert called == [
        (
            config["executable"],
            [config["executable"], "--user-data-dir=" + config["user_data_dir"], *args],
        )
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("armed", "true"),
        ("profile_dir_name", "../Default"),
        ("proxy_port", 8870),
        ("extra", "value"),
    ],
)
def test_block_rejects_unsafe_fields(config, field, value):
    config[field] = value
    with pytest.raises(ValueError):
        browser.validate(config)


@pytest.mark.parametrize("field", ["executable", "user_data_dir"])
@pytest.mark.parametrize("value", [None, 12, "", "relative/path", "/tmp/bad\0path"])
def test_link_rejects_invalid_paths_before_filesystem_access(field, value, monkeypatch):
    def unexpected_read(*args, **kwargs):
        pytest.fail("invalid setup reached the filesystem")

    monkeypatch.delenv("SC_SANDBOX", raising=False)
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    with pytest.raises(ValueError, match="absolute paths"):
        browser.link({field: value}, save=False)


@pytest.mark.parametrize("save", [False, True])
def test_link_refuses_container_seat_before_reading_host_paths(monkeypatch, save):
    """A sandboxed API server sees neither the profile nor the browser process,
    so setup names the seat instead of calling a live host path missing."""

    def unexpected_read(*args, **kwargs):
        pytest.fail("container seat reached the host profile")

    monkeypatch.setenv("SC_SANDBOX", "1")
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    with pytest.raises(ValueError, match="unsupported seat"):
        browser.link(
            {"executable": "/usr/bin/chromium", "user_data_dir": "/home/op/chromium"},
            save=save,
        )


def test_profile_probe_rejects_traversal_before_filesystem_access(config, monkeypatch):
    config["profile_dir_name"] = "../other-profile"

    def unexpected_probe(*args, **kwargs):
        pytest.fail("invalid profile reached the filesystem")

    monkeypatch.setattr(Path, "is_dir", unexpected_probe)
    with pytest.raises(ValueError, match="one directory name"):
        browser.profile_checks(config)


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
        browser,
        "package_check",
        lambda **kwargs: {"ok": False, "error": "capability missing"},
    )
    monkeypatch.setattr(
        browser, "start_process", lambda *args: pytest.fail("must not start")
    )
    with pytest.raises(ValueError, match="capability missing"):
        browser.operate("up")


def test_legacy_version_receipts_do_not_pin(config):
    config.update(server_version="old", playwright_version="old")
    assert "server_version" not in browser.validate(config)


def test_launcher_forces_linked_profile(config):
    result = guard.command(
        config,
        [
            "--profile-directory",
            "Default",
            "--user-data-dir=/wrong",
            "https://example.org",
        ],
    )
    assert result == [
        config["executable"],
        "--user-data-dir=" + config["user_data_dir"],
        "--profile-directory=Profile 1",
        "https://example.org",
    ]


def test_discovery_and_explicit_paths(config, tmp_path, monkeypatch):
    profile(config, tmp_path)
    monkeypatch.setattr(
        browser,
        "installations",
        lambda: [{"user_data_dir": str(tmp_path), "executable": config["executable"]}],
    )
    assert browser.resolve_paths({}) == (tmp_path, Path(config["executable"]))
    assert browser.resolve_paths({"user_data_dir": str(tmp_path)}) == (
        tmp_path,
        Path(config["executable"]),
    )
    assert browser.defaults()["user_data_dir"] == str(tmp_path)
    monkeypatch.setattr(
        browser,
        "installations",
        lambda: (
            [{"user_data_dir": str(tmp_path), "executable": config["executable"]}] * 2
        ),
    )
    with pytest.raises(ValueError, match="Could not select one"):
        browser.resolve_paths({})


def test_open_only_existing_armed_profile(config, tmp_path, monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock

    profile(config, tmp_path)
    monkeypatch.setattr(browser, "read", lambda: config)
    monkeypatch.setattr(browser, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(browser, "status", lambda **kwargs: {"state": "declared"})
    spawn = Mock(return_value=Mock(wait=Mock(return_value=0)))
    monkeypatch.setattr(browser.subprocess, "Popen", spawn)
    assert browser.open_profile()["launch_requested"] is True
    assert spawn.call_args.args[0] == guard.command(config, [])
    spawn.reset_mock()
    config["armed"] = False
    with pytest.raises(ValueError, match="disarmed"):
        browser.open_profile()
    config["armed"] = True
    (tmp_path / "Profile 1").rmdir()
    with pytest.raises(ValueError, match="missing"):
        browser.open_profile()
    spawn.assert_not_called()


@pytest.mark.parametrize("sandbox,harness", [(True, "codex"), (False, "kimi")])
def test_open_refuses_unsupported_seats(monkeypatch, sandbox, harness):
    monkeypatch.setattr(
        browser, "read", lambda: pytest.fail("unsupported open read profile")
    )
    with pytest.raises(ValueError, match="unsupported"):
        browser.open_profile(sandbox=sandbox, harness=harness)


def test_doctor_distinguishes_setup_and_connection(config, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(browser, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(browser, "package_check", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(browser, "read", lambda: config)
    st = {
        "state": "ready",
        "checks": {"profile": True, "extension": True},
        "connection_ready": False,
    }
    monkeypatch.setattr(browser, "status", lambda: st)
    receipt = browser.operate("doctor")
    assert receipt["setup_ready"] is True
    assert receipt["ok"] is False
    st["connection_ready"] = True
    assert browser.operate("doctor")["ok"] is True


@pytest.mark.parametrize("name", ["Preferences", "Secure Preferences"])
def test_extension_detected_from_preferences(config, tmp_path, name):
    profile(config, tmp_path)
    (tmp_path / "Profile 1" / name).write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {browser.EXTENSION_ID: {"path": "/operator/extension"}}
                }
            }
        )
    )
    assert browser.profile_checks(config)["extension"] is True


def test_default_discovery_pairs_xdg_root_and_path_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "chromium").mkdir()
    (tmp_path / "chromium/Local State").write_text("{}")
    monkeypatch.setattr(
        browser.shutil,
        "which",
        lambda name: "/bin/chromium" if name == "chromium" else None,
    )
    assert {
        "user_data_dir": str(tmp_path / "chromium"),
        "executable": "/bin/chromium",
    } in browser.installations()


def test_real_launcher_exec_targets_existing_profile(config, tmp_path):
    import subprocess

    profile(config, tmp_path)
    receipt = tmp_path / "argv.json"
    Path(config["executable"]).write_text(
        "#!"
        + sys.executable
        + "\nimport json, sys\nfrom pathlib import Path\nPath("
        + repr(str(receipt))
        + ").write_text(json.dumps(sys.argv[1:]))\n"
    )
    path = tmp_path / "guard.json"
    path.write_text(json.dumps(config))
    env = dict(os.environ, SC_BROWSER_GUARD_CONFIG=str(path))
    result = subprocess.run(
        [sys.executable, guard.__file__, "chrome-extension://fixture/connect.html"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(receipt.read_text()) == [
        "--user-data-dir=" + str(tmp_path),
        "--profile-directory=Profile 1",
        "chrome-extension://fixture/connect.html",
    ]
