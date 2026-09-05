from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills
import windows_test_controller as controller_mod


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def now(self) -> float:
        return self.value

    def monotonic(self) -> float:
        self.value += 1
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeRun:
    def __init__(self) -> None:
        self.state = "running"
        self.snapshots = {"recovery", "working-1", "named"}
        self.calls: list[list[str]] = []
        self.fail_snapshot_create = False
        self.ssh_timeout = False
        self.shutdown_sticks = False

    @staticmethod
    def _done(argv, rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            if self.ssh_timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
            encoded = argv[-1].rsplit(" ", 1)[-1]
            script = base64.b64decode(encoded).decode("utf-16-le")
            if "(Get-Item -LiteralPath $target).Length" in script:
                return self._done(argv, stdout='{"size":4}\n')
            if "$PSVersionTable.PSVersion" in script:
                return self._done(
                    argv,
                    stdout='{"administrator":true,"powershell":"5.1"}\n',
                )
            return self._done(
                argv,
                stdout='{"exit_code":0,"stdout":"ok\\n","stderr":"","timed_out":false}\n',
            )

        assert argv[:3] == ["virsh", "--connect", "qemu:///system"]
        command = argv[3]
        assert controller_mod.DOMAIN in argv or command == "snapshot-list"
        if command == "domuuid":
            return self._done(argv, stdout=controller_mod.DOMAIN_UUID + "\n")
        if command == "domstate":
            raw = "shut off" if self.state == "powered_off" else self.state
            return self._done(argv, stdout=raw + "\n")
        if command == "start":
            self.state = "running"
            return self._done(argv)
        if command == "shutdown":
            if not self.shutdown_sticks:
                self.state = "powered_off"
            return self._done(argv)
        if command == "destroy":
            self.state = "powered_off"
            return self._done(argv)
        if command == "snapshot-list":
            return self._done(argv, stdout="\n".join(sorted(self.snapshots)) + "\n")
        if command == "snapshot-create-as":
            if self.fail_snapshot_create:
                return self._done(argv, rc=1, stderr="create failed")
            self.snapshots.add(argv[5])
            return self._done(argv)
        if command == "snapshot-info":
            return self._done(argv, rc=0 if argv[6] in self.snapshots else 1)
        if command == "snapshot-delete":
            self.snapshots.discard(argv[6])
            return self._done(argv)
        if command == "snapshot-revert":
            if argv[6] not in self.snapshots:
                return self._done(argv, rc=1)
            self.state = "powered_off"
            return self._done(argv)
        raise AssertionError(argv)


class KeepBytesIO(io.BytesIO):
    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(self, output=b"", response=b"", rc=0) -> None:
        self.stdin = KeepBytesIO()
        self.stdout = io.BytesIO(response or output)
        self.stderr = io.BytesIO()
        self.killed = False
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.killed = True


class FakePopen:
    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        output = b"data" if kwargs.get("stdin") is subprocess.DEVNULL else b""
        process = FakeProcess(output)
        self.processes.append(process)
        return process


@pytest.fixture
def subject(tmp_path):
    key = tmp_path / "guest-key"
    key.write_text("not-a-real-key")
    key.chmod(0o600)
    config = tmp_path / "windows-test-controller.json"
    config.write_text(
        json.dumps(
            {
                "guest": {
                    "host": "192.0.2.10",
                    "port": 22,
                    "user": "tester",
                    "key_path": str(key),
                    "workspace": "C:\\SubfloorTest",
                },
                "recovery_snapshot": "recovery",
                "initial_working_baseline": "working-1",
                "lease_seconds": 3600,
            }
        )
    )
    config.chmod(0o600)
    runner = FakeRun()
    popen = FakePopen()
    clock = Clock()
    controller = controller_mod.Controller(
        config,
        run=runner,
        popen=popen,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return controller, runner, popen, clock


def acquire(subject, owner="dev-shell"):
    return subject[0].acquire(owner)["token"]


def test_status_is_fixed_to_testing_domain_and_hides_lease_token(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)

    result = controller.status()

    assert result["domain"] == "W10C-Testing"
    assert result["uuid"] == controller_mod.DOMAIN_UUID
    assert result["lease"] == {
        "active": True,
        "owner": "dev-shell",
        "expires_at": 4600.0,
    }
    assert token not in json.dumps(result)
    assert all(
        "W10C" not in arg or arg == "W10C-Testing"
        for call in runner.calls
        for arg in call
    )


def test_lease_rejects_competing_session_and_mutation(subject):
    controller, _runner, _popen, _clock = subject
    token = acquire(subject, "first")

    with pytest.raises(controller_mod.ControllerError, match="already leased"):
        controller.acquire("second")
    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.start("wrong-token", None)
    assert caught.value.code == "lease_conflict"

    assert controller.release(token) == {"released": True}


def test_start_waits_for_administrator_powershell_readiness(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)
    runner.state = "powered_off"

    result = controller.start(token, 20)

    assert result == {
        "domain": "W10C-Testing",
        "state": "running",
        "changed": True,
        "guest": {
            "ready": True,
            "administrator": True,
            "powershell": "5.1",
        },
    }


def test_forced_stop_requires_explicit_flag(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)
    runner.shutdown_sticks = True

    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.stop(token, 2, False)
    assert caught.value.code == "stop_timeout"
    assert runner.state == "running"

    result = controller.stop(token, 2, True)
    assert result["forced"] is True
    assert runner.state == "powered_off"


def test_exec_is_encoded_admin_powershell_and_never_host_shell(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)
    command = "Write-Output ok; Remove-Item 'C:\\guest-only'"

    result = controller.exec(token, command, "run", 10)

    assert result == {
        "exit_code": 0,
        "stdout": "ok\n",
        "stderr": "",
        "timed_out": False,
    }
    ssh_call = next(call for call in runner.calls if call[0] == "ssh")
    assert command not in " ".join(ssh_call)
    decoded = base64.b64decode(ssh_call[-1].rsplit(" ", 1)[-1]).decode("utf-16-le")
    assert "WindowsBuiltInRole]::Administrator" in decoded
    assert "taskkill.exe /PID $p.Id /T /F" in decoded
    assert "-WorkingDirectory $target" in decoded


def test_uncertain_transport_timeout_blocks_reset_until_explicit_stop(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)
    runner.ssh_timeout = True

    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.exec(token, "Get-Date", "run", 10)
    assert caught.value.code == "host_command_timeout"
    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.reset(token, "working")
    assert caught.value.code == "exec_outcome_uncertain"

    runner.ssh_timeout = False
    assert controller.stop(token, 10, False)["state"] == "powered_off"
    assert controller.reset(token, "working")["snapshot"] == "working-1"


def test_push_and_pull_stream_without_halo_paths(subject):
    controller, _runner, popen, _clock = subject
    token = acquire(subject)

    pushed = controller.push(token, "artifacts\\test.zip", io.BytesIO(b"data"), 4, 10)
    plan = controller.pull_plan(token, "results\\result.json", 10)
    output = io.BytesIO()
    controller.stream_pull(plan, output)

    assert pushed == {"path": "artifacts\\test.zip", "bytes": 4}
    assert popen.processes[0].stdin.getvalue() == b"data"
    assert output.getvalue() == b"data"
    assert not hasattr(plan, "host_path")
    push_script = base64.b64decode(popen.calls[0][-1].rsplit(" ", 1)[-1]).decode(
        "utf-16-le"
    )
    pull_script = base64.b64decode(popen.calls[1][-1].rsplit(" ", 1)[-1]).decode(
        "utf-16-le"
    )
    assert "ReparsePoint" in push_script
    assert "ReparsePoint" in pull_script


@pytest.mark.parametrize(
    "path",
    ("../secret", "C:\\Windows\\secret", "\\\\host\\share\\secret", "dir\\..\\secret"),
)
def test_transfer_path_validation_rejects_escape(subject, path):
    controller, _runner, _popen, _clock = subject
    token = acquire(subject)
    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.push(token, path, io.BytesIO(), 0, 10)
    assert caught.value.code == "guest_path_invalid"


def test_snapshot_guards_and_named_lifecycle(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)

    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.snapshot_delete(token, "recovery")
    assert caught.value.code == "snapshot_protected"
    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.snapshot_delete(token, "working-1")
    assert caught.value.code == "snapshot_protected"

    assert controller.snapshot_create(token, "trial")["snapshot"] == "trial"
    assert "trial" in controller.snapshot_list()["snapshots"]
    assert controller.reset(token, "trial") == {
        "snapshot": "trial",
        "state": "powered_off",
    }
    assert controller.snapshot_delete(token, "trial") == {
        "snapshot": "trial",
        "deleted": True,
    }
    assert "trial" not in runner.snapshots


def test_failed_promotion_preserves_previous_baseline(subject):
    controller, runner, _popen, _clock = subject
    token = acquire(subject)
    runner.fail_snapshot_create = True

    with pytest.raises(controller_mod.ControllerError) as caught:
        controller.promote(token, "working-2")
    assert caught.value.code == "snapshot_create_failed"
    assert controller._state()["working_baseline"] == "working-1"

    runner.fail_snapshot_create = False
    result = controller.promote(token, "working-2")
    assert result["working_baseline"] == "working-2"
    assert result["previous_baseline"] == "working-1"
    assert result["previous_preserved"] is True
    assert "working-1" in runner.snapshots


def test_protocol_returns_structured_error(subject):
    controller, _runner, _popen, _clock = subject
    stdin = io.BytesIO(b'{"operation":"reset","name":"working"}\n')
    stdout = io.BytesIO()

    assert controller_mod.serve_once(stdin, stdout, controller) == 1
    response = json.loads(stdout.getvalue())
    assert response["schema_version"] == 1
    assert response["ok"] is False
    assert response["error"]["code"] == "lease_required"


def test_exec_nonzero_is_a_structured_operation_failure(subject, monkeypatch):
    controller = subject[0]
    monkeypatch.setattr(
        controller,
        "exec",
        lambda *_args: {
            "exit_code": 7,
            "stdout": "partial",
            "stderr": "failed",
            "timed_out": False,
        },
    )

    response = controller.dispatch(
        {"operation": "exec", "command": "ignored", "cwd": "run"},
        io.BytesIO(),
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "exec_failed"
    assert response["result"]["exit_code"] == 7


def test_transport_initialization_changes_only_process_transport(monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(controller_mod.subprocess, "Popen", fake_popen)

    controller_mod._client_process({"transport": "local"})
    controller_mod._client_process({"transport": "ssh", "host": "halo-windows-test"})

    assert calls[0][0][-1] == "serve"
    assert calls[1][0] == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "halo-windows-test",
    ]
    assert calls[0][1] == calls[1][1]


def test_client_uses_same_frame_and_saves_lease_outside_argv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client_path = tmp_path / ".sc-state/local/windows-test-client.json"
    controller_mod._atomic_json(
        client_path, {"transport": "ssh", "host": "halo-windows-test"}
    )
    raw_response = (
        json.dumps(
            controller_mod._success(
                "acquire",
                {
                    "token": "opaque-token",
                    "owner": "dev",
                    "acquired_at": 1,
                    "expires_at": 2,
                },
            ),
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    process = FakeProcess(response=raw_response)
    monkeypatch.setattr(controller_mod, "_client_process", lambda _cfg: process)

    result = controller_mod.client_request({"operation": "acquire", "owner": "dev"})

    assert json.loads(process.stdin.getvalue().splitlines()[0]) == {
        "operation": "acquire",
        "owner": "dev",
    }
    assert result["result"]["lease_saved"] is True
    assert "token" not in result["result"]
    saved = json.loads(client_path.read_text())
    assert saved["lease"] == "opaque-token"
    assert client_path.stat().st_mode & 0o077 == 0


def test_failed_pull_transport_does_not_replace_existing_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    controller_mod._atomic_json(
        tmp_path / ".sc-state/local/windows-test-client.json",
        {"transport": "local", "lease": "opaque"},
    )
    response = controller_mod._success("pull", {"bytes": 4, "payload_bytes": 4})
    process = FakeProcess(
        response=json.dumps(response, separators=(",", ":")).encode() + b"\n" + b"data",
        rc=1,
    )
    monkeypatch.setattr(controller_mod, "_client_process", lambda _cfg: process)
    target = tmp_path / "result.json"
    target.write_bytes(b"old")

    with pytest.raises(controller_mod.ControllerError) as caught:
        controller_mod.client_request(
            {"operation": "pull", "path": "results\\result.json"},
            output_path=target,
        )

    assert caught.value.code == "transport_failed"
    assert target.read_bytes() == b"old"


def test_fork_local_skill_template_is_ready_for_supported_import():
    skill_path = ENGINE / "docs/skills/windows_testing/SKILL.md"
    skill = seed_skills.parse_skill(skill_path)
    assert skill["name"] == "windows_testing"
    assert skill["category"] == "substrate"
    assert skill["common"] == 0
    assert "no daemon and opens no\nlistener" in skill["content"]
    assert "sc skill put --file" in skill["content"]
    assert "./sc vm test" not in skill["content"]
    assert not (ENGINE / "assets/skills/windows_testing/SKILL.md").exists()


def test_controller_refuses_non_private_configuration_directory(subject):
    controller = subject[0]
    controller.config_path.parent.chmod(0o755)
    try:
        with pytest.raises(controller_mod.ControllerError) as caught:
            controller_mod.Controller(controller.config_path)
        assert caught.value.code == "configuration_permissions"
    finally:
        controller.config_path.parent.chmod(0o700)
