"""Private credential lifecycle, with no operator profile or service access."""

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
import browser


@pytest.fixture
def seat(tmp_path, monkeypatch):
    config = {
        "user_data_dir": str(tmp_path / "chromium"),
        "profile_dir_name": "Profile 1",
        "armed": True,
    }
    events = []
    monkeypatch.delenv("SC_API_TOKEN", raising=False)
    monkeypatch.delenv("SC_SANDBOX", raising=False)
    monkeypatch.setattr(browser, "private", lambda: tmp_path)
    monkeypatch.setattr(browser, "read", lambda: config)
    monkeypatch.setattr(browser.guard, "check", lambda config: None)
    monkeypatch.setattr(
        browser, "profile_checks", lambda config: {"profile": True, "extension": True}
    )
    monkeypatch.setattr(browser, "process_receipt", lambda name: None)
    monkeypatch.setattr(
        browser, "stop_process", lambda name: events.append(("stop", name))
    )
    monkeypatch.setattr(
        browser,
        "proxy_status",
        lambda config: {"active_shells": [], "extension": "not connected"},
    )
    return config, tmp_path / browser.TOKEN_FILE, events


def test_private_profile_bound_storage_and_status(seat):
    config, path, _ = seat
    assert browser.status()["approval_mode"] == "manual"
    receipt = browser.set_extension_token("fixture-private-token")
    assert path.stat().st_mode & 0o777 == 0o600
    assert browser.extension_token(config) == ("configured", "fixture-private-token")
    assert receipt["approval_mode"] == "token"
    assert not receipt["connection_ready"]
    assert "fixture-private-token" not in json.dumps(receipt)
    config["profile_dir_name"] = "Profile 2"
    assert browser.extension_token(config) == ("profile_mismatch", None)
    assert browser.status()["state"] == "failed"
    assert browser.status()["credential_state"] == "profile_mismatch"


@pytest.mark.parametrize(
    "damage",
    [
        "permissions",
        "symlink",
        "hardlink",
        "directory",
        "fifo",
        "json",
        "encoding",
        "oversized",
        "token",
    ],
)
def test_unsafe_credentials_fail_closed_without_disclosing_values(seat, damage):
    config, path, _ = seat
    browser.set_extension_token("fixture-secret")
    if damage == "permissions":
        path.chmod(0o644)
    elif damage in ("symlink", "hardlink"):
        other = path.with_suffix(".original")
        path.rename(other)
        path.symlink_to(other) if damage == "symlink" else os.link(other, path)
    elif damage in ("directory", "fifo"):
        path.unlink()
        path.mkdir() if damage == "directory" else os.mkfifo(path)
    elif damage == "json":
        path.write_text("fixture-secret-invalid-json")
    elif damage == "encoding":
        path.write_bytes(b"\xff")
    elif damage == "oversized":
        path.write_text("x" * 16385)
    else:
        path.write_text(json.dumps({"token": ["fixture-secret"]}))
    assert browser.extension_token(config) == ("invalid", None)
    receipt = browser.status()
    assert receipt["state"] == "failed"
    assert "fixture-secret" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "token", ["", "has spaces", "line\nbreak", "nul\0", "x" * 4097, "é"]
)
def test_invalid_input_does_not_stop_services_or_replace_credential(seat, token):
    config, path, events = seat
    browser.set_extension_token("previous")
    original = path.read_bytes()
    events.clear()
    with pytest.raises(ValueError, match="printable ASCII"):
        browser.set_extension_token(token)
    assert not events
    assert path.read_bytes() == original
    assert browser.extension_token(config)[1] == "previous"


@pytest.mark.parametrize(
    "running,armed", [(False, True), (True, True), (True, False), (False, False)]
)
def test_replace_and_remove_end_connections_without_arming(
    seat, monkeypatch, running, armed
):
    config, path, events = seat
    config["armed"] = armed
    monkeypatch.setattr(
        browser, "process_receipt", lambda name: {"pid": 1} if running else None
    )
    monkeypatch.setattr(
        browser, "operate", lambda action: events.append(("operate", action)) or {}
    )
    for token in ("first", "replacement", None, None):
        events.clear()
        browser.set_extension_token(token)
        assert events == [("stop", "proxy"), ("stop", "server")] + (
            [("operate", "up")] if running and armed else []
        )
        assert path.exists() == (token is not None)
        assert config["armed"] is armed


def test_disarm_retains_credential_disable_removes_it(seat, monkeypatch):
    config, path, _ = seat
    browser.set_extension_token("fixture-secret")
    monkeypatch.setattr(browser.ports, "update", lambda *args, **kwargs: None)
    monkeypatch.setattr(browser, "start_process", lambda *args: None)
    browser.operate("disarm")
    assert path.exists() and not config["armed"]
    browser.operate("disable")
    assert not path.exists()


def test_only_managed_server_receives_provisioned_token(seat, monkeypatch):
    config, path, _ = seat
    browser.set_extension_token("provisioned-secret")
    monkeypatch.setenv(browser.TOKEN_ENV, "ambient-secret")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://untrusted")
    monkeypatch.setattr(browser, "process_identity", lambda pid: "123")
    seen = []

    def spawn(command, **kwargs):
        seen.append((command, kwargs["env"]))
        if browser.TOKEN_ENV in kwargs["env"]:
            os.write(kwargs["stdout"], b"provisioned-secret")
        for fd in kwargs["pass_fds"]:
            os.write(fd, b"1")
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(browser.subprocess, "Popen", spawn)
    browser.start_process("server", ["fixture-server"], config)
    browser.start_process("proxy", ["fixture-proxy"], config)
    assert seen[0][1][browser.TOKEN_ENV] == "provisioned-secret"
    assert browser.TOKEN_ENV not in seen[1][1]
    assert "ambient-secret" not in json.dumps(seen)
    assert all("PLAYWRIGHT_MCP_CDP_ENDPOINT" not in env for _, env in seen)
    for file in path.parent.iterdir():
        if file.is_file() and file != path:
            assert b"provisioned-secret" not in file.read_bytes()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unusable"):
        browser.start_process("server", ["fixture-server"], config)
    assert len(seen) == 2


@pytest.mark.parametrize("action", ["token-set", "token-remove"])
def test_shell_refused_before_reading_secret(seat, monkeypatch, capsys, action):
    _, path, events = seat
    monkeypatch.setenv("SC_API_TOKEN", "shell-token")
    monkeypatch.setattr(
        browser.getpass, "getpass", lambda *args: pytest.fail("must not prompt")
    )
    assert browser.main([action, "--json"]) == 1
    assert "operator's host terminal" in capsys.readouterr().out
    assert not path.exists() and not events


def test_container_and_missing_profile_refuse_before_mutation(seat, monkeypatch):
    _, path, events = seat
    monkeypatch.setenv("SC_SANDBOX", "1")
    with pytest.raises(ValueError, match="bare metal"):
        browser.set_extension_token("fixture-secret")
    monkeypatch.delenv("SC_SANDBOX")
    monkeypatch.setattr(browser, "read", lambda: None)
    with pytest.raises(ValueError, match="Link the Subfloor profile"):
        browser.set_extension_token("fixture-secret")
    assert not path.exists() and not events


def test_hidden_input_never_falls_back_to_echo(seat, monkeypatch, capsys):
    import warnings

    _, path, events = seat
    stdin = io.StringIO()
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)

    def unavailable(prompt):
        warnings.warn("fixture echo fallback", browser.getpass.GetPassWarning)
        pytest.fail("must refuse before reading with echo enabled")

    monkeypatch.setattr(browser.getpass, "getpass", unavailable)
    assert browser.main(["token-set"]) == 1
    assert "Hidden terminal input is unavailable" in capsys.readouterr().out
    assert not path.exists() and not events


def test_cli_hidden_prompt_and_bounded_stdin(seat, monkeypatch, capsys):
    config, _, _ = seat
    stdin = io.StringIO()
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(browser.getpass, "getpass", lambda prompt: "hidden-secret")
    assert browser.main(["token-set", "--json"]) == 0
    assert browser.extension_token(config)[1] == "hidden-secret"
    assert "hidden-secret" not in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-secret\r\n"))
    assert browser.main(["token-set", "--token-stdin"]) == 0
    assert browser.extension_token(config)[1] == "stdin-secret"
    assert "stdin-secret" not in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 5000))
    assert browser.main(["token-set", "--token-stdin"]) == 1
    assert browser.extension_token(config)[1] == "stdin-secret"
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert browser.main(["token-set"]) == 1
    assert "never put the token in argv" in capsys.readouterr().out


def test_failed_atomic_write_keeps_previous_credential_with_connections_stopped(
    seat, monkeypatch
):
    _, path, events = seat
    browser.set_extension_token("previous")
    original = path.read_bytes()
    events.clear()
    monkeypatch.setattr(
        browser.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("fixture disk full")),
    )
    with pytest.raises(OSError):
        browser.set_extension_token("replacement")
    assert events == [("stop", "proxy"), ("stop", "server")]
    assert path.read_bytes() == original
    assert not list(path.parent.glob("." + browser.TOKEN_FILE + "*"))


def test_relink_to_other_profile_discards_token(seat, monkeypatch):
    config, path, _ = seat
    browser.set_extension_token("previous")
    monkeypatch.setattr(
        browser,
        "resolve_paths",
        lambda value: (Path(config["user_data_dir"]), Path(sys.executable)),
    )
    monkeypatch.setattr(browser, "named_profiles", lambda directory: ["Profile 2"])
    monkeypatch.setattr(
        browser.ports,
        "ensure_browser_ports",
        lambda: {"browser_port": 8870, "browser_proxy_port": 8871},
    )
    monkeypatch.setattr(browser, "package_check", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(browser.ports, "update", lambda *args: None)
    monkeypatch.setattr(browser, "operate", lambda action: {})
    browser.link({})
    assert not path.exists()
