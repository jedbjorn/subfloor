#!/usr/bin/env python3
"""Isolated DeepSeek SDK/runtime carrier contracts."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import deepseek_runtime  # noqa: E402


def load_tests(_loader, _standard_tests, _pattern):
    """Run the same contract under the stdlib-only Python 3.9 CI lane."""
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            suite.addTest(unittest.FunctionTestCase(function, description=name))
    return suite


def completed(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def carrier_evidence(
    python: Path,
    *,
    version: tuple[int, int, int] = (3, 14, 7),
    sdk: str = "0.1.0rc7",
    runtime: str = "0.1.0rc7",
    isolated: bool = True,
) -> str:
    return json.dumps(
        {
            "python": list(version),
            "prefix": str(python.parent.parent),
            "base_prefix": "/usr" if isolated else str(python.parent.parent),
            "sdk": sdk,
            "runtime": runtime,
        }
    )


def test_runtime_manifest_pins_official_pair_and_all_published_artifacts() -> None:
    manifest = deepseek_runtime.load_runtime_manifest()

    assert manifest["python_minimum"] == "3.10"
    assert manifest["source"] == {
        "repository": "https://github.com/deepseek-ai/deepseek-harness",
        "commit": "bb4ca698d63714e753f5621b07400e6ebb0b5d97",
        "license": "MIT",
    }
    assert manifest["sdk"] == {
        "distribution": "deepseek-harness-sdk",
        "version": "0.1.0rc7",
        "wheel_sha256": "5327d60659d8802442d2c589c89c3528cf18eb07b5d698059c833eb6b853b7d4",
    }
    assert manifest["runtime"] == {
        "distribution": "deepseek-harness-runtime-bin",
        "version": "0.1.0rc7",
        "wheel_sha256": {
            "macos-arm64": "39ae51dec905c496ede69250d51b02a424a6c96829feb2ad7e01b3cec97a3255",
            "linux-arm64": "de4e2571d3cd1c521ee22c754460dd251b45bd91a033876f554d0eab9b2843a1",
            "linux-x64": "bf26f76a8cab7bedcce3a060b81d19ec1cabd8bd261c40b47381d630742797a7",
        },
    }


def test_composition_is_exact_and_contains_only_the_reviewed_plugin_allowlist() -> None:
    manifest = deepseek_runtime.load_runtime_manifest()
    composition = ROOT / ".super-coder" / manifest["composition"]["path"]
    names = tuple(
        line.split("name:", 1)[1].strip().strip("'")
        for line in composition.read_text().splitlines()
        if line.strip().startswith("name:")
    )

    assert names == (
        "@deepseek-ai/dsh-sdk-jsonrpc-server",
        "@deepseek-ai/dsh-llm-deepseek",
        "@deepseek-ai/dsh-subprocess-local",
        "@deepseek-ai/dsh-bash-local",
        "@deepseek-ai/dsh-fs-local",
        "@deepseek-ai/dsh-fs-observation-policy",
        "@deepseek-ai/dsh-tool-fs",
        "@deepseek-ai/dsh-agent-spine-demo",
        "@deepseek-ai/dsh-session-persistence-jsonl",
        "@deepseek-ai/dsh-session-checkpoint-policy",
        "@deepseek-ai/dsh-token-meter",
    )
    assert not any(
        forbidden in name
        for name in names
        for forbidden in ("approval", "question", "plugin", "web", "terminal")
    )


def test_composition_digest_drift_fails_closed() -> None:
    with mock.patch.object(deepseek_runtime, "_sha256", return_value="0" * 64):
        try:
            deepseek_runtime.load_runtime_manifest()
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            assert exc.code == "HARNESS_COMPOSITION_DRIFT"
            assert "176f00285a2bf8f7b01b69136b250183122d6a01a06d93c3f4dac056b3a0460c" in exc.detail
        else:
            raise AssertionError("composition drift was accepted")


def test_missing_carrier_is_a_deepseek_only_unavailable_status() -> None:
    with tempfile.TemporaryDirectory() as raw:
        engine = Path(raw) / "engine"
        runner = mock.Mock(side_effect=AssertionError("missing carrier must not execute"))

        status = deepseek_runtime.runtime_status(env={}, engine=engine, runner=runner)

        assert status.available is False
        assert status.enabled is True
        assert status.error == "HARNESS_RUNTIME_MISSING"
        assert status.sdk_version is None
        assert status.runtime_version is None
        assert not engine.exists()
        runner.assert_not_called()


def test_python39_carrier_projects_incompatible_without_importing_the_sdk() -> None:
    with tempfile.TemporaryDirectory() as raw:
        python = executable(Path(raw) / "venv" / "bin" / "python")
        runner = mock.Mock(
            return_value=completed(
                stdout=carrier_evidence(python, version=(3, 9, 19))
            )
        )

        status = deepseek_runtime.probe_carrier(python, runner=runner)

        assert status.available is False
        assert status.error == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert status.python_version == "3.9.19"
        assert status.sdk_version == "0.1.0rc7"
        assert status.runtime_version == "0.1.0rc7"
        assert runner.call_args.args[0][1:3] == ["-I", "-c"]


def test_exact_isolated_pair_reports_available_and_mismatch_does_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        python = executable(Path(raw) / "venv" / "bin" / "python")
        good = deepseek_runtime.probe_carrier(
            python,
            runner=mock.Mock(return_value=completed(stdout=carrier_evidence(python))),
        )
        bad = deepseek_runtime.probe_carrier(
            python,
            runner=mock.Mock(
                return_value=completed(
                    stdout=carrier_evidence(python, runtime="0.1.0rc6")
                )
            ),
        )

        assert good.as_dict() == {
            "available": True,
            "enabled": True,
            "error": None,
            "detail": None,
            "carrier_python": str(python),
            "python_version": "3.14.7",
            "sdk_version": "0.1.0rc7",
            "runtime_version": "0.1.0rc7",
            "composition_sha256": "176f00285a2bf8f7b01b69136b250183122d6a01a06d93c3f4dac056b3a0460c",
        }
        assert bad.available is False
        assert bad.error == "HARNESS_RUNTIME_VERSION_MISMATCH"
        assert "0.1.0rc7/0.1.0rc7" in bad.detail


def test_bare_metal_install_uses_a_separate_venv_and_writes_version_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        engine = root / "engine"
        bootstrap = executable(root / "python3.14")
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            if argv[-1] == deepseek_runtime._PYTHON_VERSION_PROBE:
                return completed(stdout="[3, 14, 7]\n")
            if argv[1:3] == ["-m", "venv"]:
                executable(Path(argv[3]) / "bin" / "python")
                return completed()
            if argv[1:4] == ["-m", "pip", "install"]:
                return completed()
            if argv[-1] == deepseek_runtime._CARRIER_PROBE:
                python = Path(argv[0])
                return completed(stdout=carrier_evidence(python))
            raise AssertionError(f"unexpected command: {argv}")

        status = deepseek_runtime.ensure_carrier(
            env={"SC_DEEPSEEK_BOOTSTRAP_PYTHON": str(bootstrap)},
            engine=engine,
            runner=runner,
        )

        expected_python = (
            engine / "run" / "deepseek" / "carriers" / "0.1.0rc7" / "bin" / "python"
        )
        assert status.available is True
        assert status.carrier_python == str(expected_python)
        assert expected_python != bootstrap
        assert calls[2][1:4] == ["-m", "pip", "install"]
        assert calls[2][-1] == "deepseek-harness-sdk==0.1.0rc7"
        evidence = json.loads(
            (expected_python.parent.parent / "install-evidence.json").read_text()
        )
        assert evidence["sdk_version"] == "0.1.0rc7"
        assert evidence["runtime_version"] == "0.1.0rc7"
        assert evidence["source"]["commit"] == "bb4ca698d63714e753f5621b07400e6ebb0b5d97"
        assert stat.S_IMODE(
            (expected_python.parent.parent / "install-evidence.json").stat().st_mode
        ) == 0o600


def test_absent_compatible_bootstrap_does_not_create_a_partial_carrier() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        engine = root / "engine"
        bootstrap = executable(root / "python3.9")
        runner = mock.Mock(return_value=completed(stdout="[3, 9, 19]\n"))

        status = deepseek_runtime.ensure_carrier(
            env={"SC_DEEPSEEK_BOOTSTRAP_PYTHON": str(bootstrap)},
            engine=engine,
            runner=runner,
        )

        assert status.available is False
        assert status.error == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert not engine.exists()
        assert runner.call_count == 1


def test_conversations_receive_distinct_private_roots_and_process_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = Path(raw)
        first = deepseek_runtime.conversation_layout("conversation-A", state_root=state)
        second = deepseek_runtime.conversation_layout("conversation-B", state_root=state)
        secret_argv = ["runtime", "--token", "sk-secret-value"]

        deepseek_runtime.provision_conversation(first)
        evidence = deepseek_runtime.record_process_identity(
            first, pid=123, start_ticks=456, argv=secret_argv
        )

        assert first.root != second.root
        assert first.session_root != second.session_root
        assert not second.root.exists()
        assert evidence["pid"] == 123
        assert evidence["start_ticks"] == 456
        assert "sk-secret-value" not in first.process_identity.read_text()
        assert stat.S_IMODE(first.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.session_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.process_identity.stat().st_mode) == 0o600


def test_launch_environment_replaces_personal_state_and_redacts_credentials() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        layout = deepseek_runtime.conversation_layout(41, state_root=root / "state")

        child = deepseek_runtime.launch_environment(
            layout,
            worktree=worktree,
            system_prompt="immutable boot bytes",
            api_key="sk-private-credential",
            base_url="https://api.deepseek.example/v1",
            base_env={
                "PATH": "/usr/bin",
                "DSH_HOME": "/home/operator/.dsh",
                "DSH_SESSION_ROOT": "/tmp/shared",
                "DSH_PROFILE": "mutable-personal-profile",
                "DEEPSEEK_API_KEY": "old-secret",
            },
        )
        redacted = deepseek_runtime.redacted_environment(child)

        assert child["PATH"] == "/usr/bin"
        assert child["DSH_HOME"] == str(layout.home)
        assert child["DSH_SESSION_ROOT"] == str(layout.session_root)
        assert child["DSH_CWD"] == str(worktree)
        assert child["DSH_SYSTEM_PROMPT"] == "immutable boot bytes"
        assert child["DEEPSEEK_API_KEY"] == "sk-private-credential"
        assert child["DEEPSEEK_BASE_URL"] == "https://api.deepseek.example/v1"
        assert "DSH_PROFILE" not in child
        assert "/home/operator/.dsh" not in child.values()
        assert "old-secret" not in child.values()
        assert redacted["DEEPSEEK_API_KEY"] == "[REDACTED]"
        assert "sk-private-credential" not in json.dumps(redacted)


def test_disable_is_non_destructive_and_short_circuits_runtime_probe() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = Path(raw) / "state"
        layout = deepseek_runtime.conversation_layout(7, state_root=state)
        deepseek_runtime.provision_conversation(layout)
        marker = layout.session_root / "native-session.jsonl"
        marker.write_text("durable history\n")
        runner = mock.Mock(side_effect=AssertionError("disabled carrier must not run"))

        status = deepseek_runtime.runtime_status(
            env={"SC_DISABLED_HARNESSES": "opencode, deepseek"},
            engine=Path(raw) / "engine",
            runner=runner,
        )

        assert status.available is False
        assert status.enabled is False
        assert status.error == "HARNESS_DISABLED"
        assert marker.read_text() == "durable history\n"
        assert tuple(layout.session_root.iterdir()) == (marker,)
        runner.assert_not_called()


def test_diagnostics_redact_known_and_explicit_secrets_before_bounding() -> None:
    secret = "private-token-value"
    raw = (
        "DEEPSEEK_API_KEY=sk-1234567890 Authorization: Bearer bearer-secret "
        + secret
        + " x" * 5000
    )

    diagnostic = deepseek_runtime.sanitize_diagnostic(
        raw, secrets=(secret,), limit=256
    )

    assert len(diagnostic) == 254
    assert diagnostic.endswith("…[truncated]")
    assert "sk-1234567890" not in diagnostic
    assert "bearer-secret" not in diagnostic
    assert secret not in diagnostic
    assert diagnostic.count("[REDACTED]") == 3


def test_linux_process_identity_reads_start_ticks_after_a_spaced_comm() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        pid_dir = proc / "321"
        pid_dir.mkdir()
        fields = ["S", *("0" for _ in range(18)), "987654", "0"]
        (pid_dir / "stat").write_text(
            "321 (runtime name with spaces) " + " ".join(fields) + "\n"
        )

        assert deepseek_runtime.process_start_ticks(321, proc_root=proc) == 987654
