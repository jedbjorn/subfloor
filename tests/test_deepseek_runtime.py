#!/usr/bin/env python3
"""Isolated DeepSeek SDK/runtime carrier contracts."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import deepseek_runtime  # noqa: E402
import build_deepseek_carrier  # noqa: E402


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
            "carrier": {
                "protocol": "super-coder-deepseek-lifecycle-v1",
                "sourceCommit": "bb4ca698d63714e753f5621b07400e6ebb0b5d97",
            },
        }
    )


def write_artifact_evidence(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = deepseek_runtime.load_runtime_manifest()
    records = []
    for filename, payload in (
        ("deepseek_harness_runtime_bin-0.1.0rc7-py3-none-test.whl", b"runtime"),
        ("deepseek_harness_sdk-0.1.0rc7-py3-none-any.whl", b"sdk"),
    ):
        path = directory / filename
        path.write_bytes(payload)
        records.append(
            {"filename": filename, "sha256": deepseek_runtime._sha256(path), "size": len(payload)}
        )
    (directory / "deepseek-carrier-artifacts.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": deepseek_runtime.carrier_runtime_platform(),
                "source_commit": manifest["source"]["commit"],
                "source_archive_sha256": manifest["source"]["archive_sha256"],
                "patch_sha256": manifest["patch"]["sha256"],
                "build_recipe_sha256": manifest["build"]["sha256"],
                "build_tools": {
                    "node": manifest["build"]["node_version"],
                    "pnpm": manifest["build"]["pnpm_version"],
                    "uv": manifest["build"]["uv_version"],
                    "source_date_epoch": manifest["build"]["source_date_epoch"],
                },
                "artifacts": records,
            }
        )
    )


def test_runtime_manifest_pins_source_patch_recipe_and_supported_platforms() -> None:
    manifest = deepseek_runtime.load_runtime_manifest()

    assert manifest["python_minimum"] == "3.10"
    assert manifest["carrier"] == {
        "protocol": "super-coder-deepseek-lifecycle-v1",
        "acquisition": "verified-source-build",
    }
    assert manifest["source"]["commit"] == "bb4ca698d63714e753f5621b07400e6ebb0b5d97"
    assert manifest["source"]["archive_sha256"] == "d5a78fb623d1c14846812e8e18042134a1127ab86dea259f79c2c8358e8481bc"
    assert manifest["patch"]["upstream_issue_url"] == "https://github.com/deepseek-ai/deepseek-harness/issues/new"
    assert manifest["build"]["pnpm_version"] == "11.7.0"
    assert manifest["runtime"]["platforms"] == ["macos-arm64", "linux-arm64", "linux-x64"]


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
            assert "9f29580d44ff78363f32585e926f384038de1f7f51dafde473375d69e73e69de" in exc.detail
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
            "composition_sha256": "9f29580d44ff78363f32585e926f384038de1f7f51dafde473375d69e73e69de",
        }
        assert bad.available is False
        assert bad.error == "HARNESS_RUNTIME_VERSION_MISMATCH"
        assert "0.1.0rc7/0.1.0rc7" in bad.detail


def test_stock_runtime_identity_is_rejected_even_when_versions_match() -> None:
    with tempfile.TemporaryDirectory() as raw:
        python = executable(Path(raw) / "venv" / "bin" / "python")
        evidence = json.loads(carrier_evidence(python))
        evidence["carrier"] = None

        status = deepseek_runtime.probe_carrier(
            python, runner=mock.Mock(return_value=completed(stdout=json.dumps(evidence)))
        )

        assert status.available is False
        assert status.error == "HARNESS_RUNTIME_ARTIFACT_DRIFT"


def test_built_artifact_digest_drift_fails_before_install() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_artifact_evidence(directory)
        next(directory.glob("deepseek_harness_sdk-*.whl")).write_bytes(b"changed")

        try:
            deepseek_runtime._load_built_artifacts(
                directory, manifest=deepseek_runtime.load_runtime_manifest()
            )
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            assert exc.code == "HARNESS_RUNTIME_ARTIFACT_DRIFT"
        else:
            raise AssertionError("changed built artifact was accepted")


def test_provider_request_patch_is_callable_exact_and_fail_closed() -> None:
    assert deepseek_runtime.provider_request_options(
        thinking="omit", reasoning_effort="omit"
    ) == {"thinking": "omit", "reasoningEffort": "omit"}
    assert deepseek_runtime.provider_request_options(
        thinking="enabled", reasoning_effort="max"
    ) == {"thinking": "enabled", "reasoningEffort": "max"}
    assert deepseek_runtime.LIFECYCLE_METHODS == {
        "session/start", "session/cancel", "session/inspect", "session/reconcile", "shutdown"
    }
    try:
        deepseek_runtime.provider_request_options(
            thinking="default", reasoning_effort="high"
        )
    except deepseek_runtime.DeepSeekRuntimeError as exc:
        assert exc.code == "HARNESS_PROVIDER_OPTION_INVALID"
    else:
        raise AssertionError("unknown provider option was accepted")


def test_provider_wire_evidence_captures_default_omission_and_named_mapping() -> None:
    status = deepseek_runtime.RuntimeStatus(
        available=True,
        enabled=True,
        error=None,
        detail=None,
        carrier_python="/carrier/bin/python",
        python_version="3.14.7",
        sdk_version="0.1.0rc7",
        runtime_version="0.1.0rc7",
        composition_sha256=deepseek_runtime.load_runtime_manifest()["composition"]["sha256"],
    )

    def runner(argv, **kwargs):
        options = json.loads(argv[-2])
        body = {"model": argv[-3], "messages": [], "stream": True}
        if options["thinking"] != "omit":
            body["thinking"] = {"type": options["thinking"]}
        if options["reasoningEffort"] != "omit":
            body["reasoning_effort"] = options["reasoningEffort"]
        request = urllib.request.Request(
            kwargs["env"]["DEEPSEEK_BASE_URL"] + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {kwargs['env']['DEEPSEEK_API_KEY']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        return completed()

    evidence = deepseek_runtime.provider_wire_evidence(
        "deepseek-v4-pro",
        {
            "default": {"thinking": "omit", "reasoningEffort": "omit"},
            "high": {"thinking": "enabled", "reasoningEffort": "high"},
        },
        env={},
        runner=runner,
        status=status,
    )

    assert evidence["contract"] == deepseek_runtime.PROVIDER_WIRE_CONTRACT
    assert evidence["proofs"]["default"]["wire_options"] == {}
    assert evidence["proofs"]["high"]["wire_options"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert len(evidence["proofs"]["default"]["digest"]) == 64


def test_provider_wire_evidence_rejects_materialized_default() -> None:
    status = deepseek_runtime.RuntimeStatus(
        available=True,
        enabled=True,
        error=None,
        detail=None,
        carrier_python="/carrier/bin/python",
        python_version="3.14.7",
        sdk_version="0.1.0rc7",
        runtime_version="0.1.0rc7",
        composition_sha256=deepseek_runtime.load_runtime_manifest()["composition"]["sha256"],
    )

    def runner(argv, **kwargs):
        body = {
            "model": argv[-3],
            "messages": [],
            "stream": True,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        request = urllib.request.Request(
            kwargs["env"]["DEEPSEEK_BASE_URL"] + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {kwargs['env']['DEEPSEEK_API_KEY']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        return completed()

    try:
        deepseek_runtime.provider_wire_evidence(
            "deepseek-v4-pro",
            {"default": {"thinking": "omit", "reasoningEffort": "omit"}},
            env={},
            runner=runner,
            status=status,
        )
    except deepseek_runtime.DeepSeekRuntimeError as exc:
        assert exc.code == "HARNESS_PROVIDER_WIRE_MISMATCH"
    else:
        raise AssertionError("materialized default entered controlled evidence")


def test_carrier_build_normalizes_only_the_pkg_sea_temp_token() -> None:
    with tempfile.TemporaryDirectory() as raw:
        executable_path = Path(raw) / "carrier"
        executable_path.write_bytes(
            b"prefix/tmp/pkg-sea-Ab9Z0q/sea-main.js/suffix"
        )

        build_deepseek_carrier.normalize_sea_executable(executable_path)

        assert executable_path.read_bytes() == (
            b"prefix/tmp/pkg-sea-SC0000/sea-main.js/suffix"
        )


def test_carrier_build_rejects_missing_or_malformed_pkg_sea_temp_path() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        missing = root / "missing"
        malformed = root / "malformed"
        missing.write_bytes(b"no SEA marker")
        malformed.write_bytes(b"/tmp/pkg-sea-ABC123/not-main.js")

        for path, expected in (
            (missing, "carrier executable does not contain a pkg SEA temp path"),
            (malformed, "carrier executable has an unexpected pkg SEA temp path"),
        ):
            before = path.read_bytes()
            try:
                build_deepseek_carrier.normalize_sea_executable(path)
            except RuntimeError as exc:
                assert str(exc) == expected
            else:
                raise AssertionError(f"invalid SEA path was accepted: {path.name}")
            assert path.read_bytes() == before


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
            if len(argv) > 1 and Path(argv[1]).name == "build_deepseek_carrier.py":
                write_artifact_evidence(Path(argv[argv.index("--output-dir") + 1]))
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
        assert Path(calls[2][1]).name == "build_deepseek_carrier.py"
        assert calls[3][-1] == "pydantic>=2.12,<3"
        assert calls[4][5:7] == ["--no-index", "--no-deps"]
        evidence = json.loads(
            (expected_python.parent.parent / "install-evidence.json").read_text()
        )
        assert evidence["sdk_version"] == "0.1.0rc7"
        assert evidence["runtime_version"] == "0.1.0rc7"
        assert evidence["source"]["commit"] == "bb4ca698d63714e753f5621b07400e6ebb0b5d97"
        assert evidence["built_artifacts"]["patch_sha256"] == deepseek_runtime.load_runtime_manifest()["patch"]["sha256"]
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


def test_unsupported_container_architecture_degrades_without_partial_carrier() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "deepseek-runtime"
        runner = mock.Mock(
            side_effect=AssertionError("unsupported architecture must not install")
        )

        status = deepseek_runtime.prepare_container_carrier(
            target,
            architecture="s390x",
            runner=runner,
        )
        projected = deepseek_runtime.runtime_status(
            env={
                "SC_DEEPSEEK_CARRIER_PYTHON": str(target / "bin" / "python")
            },
            runner=runner,
        )

        marker = target.with_suffix(".unavailable.json")
        assert status.available is False
        assert status.error == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert status.carrier_python == str(target / "bin" / "python")
        assert not target.exists()
        assert json.loads(marker.read_text()) == {
            "architecture": "s390x",
            "detail": "pinned DeepSeek carrier has no build target for s390x",
            "error": "HARNESS_RUNTIME_INCOMPATIBLE",
            "sdk_version": "0.1.0rc7",
        }
        assert projected.error == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert "architecture: s390x" in projected.detail
        runner.assert_not_called()


def test_unsupported_container_cli_exits_successfully_for_global_image_build() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "deepseek-runtime"
        output = io.StringIO()

        with redirect_stdout(output):
            returncode = deepseek_runtime.main(
                [
                    "--install-container-carrier",
                    str(target),
                    "s390x",
                    "--json",
                ]
            )

        payload = json.loads(output.getvalue())
        assert returncode == 0
        assert payload["available"] is False
        assert payload["error"] == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert not target.exists()
        assert target.with_suffix(".unavailable.json").is_file()


def test_python39_container_degrades_on_supported_architecture_without_install() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        target = root / "deepseek-runtime"
        bootstrap = executable(root / "python3.9")
        runner = mock.Mock(return_value=completed(stdout="[3, 9, 19]\n"))

        status = deepseek_runtime.prepare_container_carrier(
            target,
            architecture="x86_64",
            bootstrap_python=bootstrap,
            runner=runner,
        )

        assert status.available is False
        assert status.error == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert "no Python 3.10+ carrier interpreter" in status.detail
        assert not target.exists()
        assert target.with_suffix(".unavailable.json").is_file()
        assert runner.call_count == 1
        assert runner.call_args.args[0] == [
            str(bootstrap.resolve()),
            "-I",
            "-c",
            deepseek_runtime._PYTHON_VERSION_PROBE,
        ]


def test_container_platform_map_matches_every_published_linux_wheel_family() -> None:
    assert {
        architecture: deepseek_runtime.container_runtime_platform(architecture)
        for architecture in ("amd64", "x86_64", "arm64", "aarch64", "s390x")
    } == {
        "amd64": "linux-x64",
        "x86_64": "linux-x64",
        "arm64": "linux-arm64",
        "aarch64": "linux-arm64",
        "s390x": None,
    }


def test_dockerfile_delegates_container_acquisition_to_tested_optional_helper() -> None:
    dockerfile = (ROOT / ".super-coder" / "Dockerfile").read_text()
    folded = dockerfile.replace("\\\n", " ")

    assert (
        "COPY .super-coder/scripts/deepseek_runtime.py "
        "/opt/super-coder/deepseek-bootstrap/scripts/deepseek_runtime.py"
    ) in dockerfile
    assert (
        "COPY .super-coder/scripts/build_deepseek_carrier.py "
        "/opt/super-coder/deepseek-bootstrap/scripts/build_deepseek_carrier.py"
    ) in dockerfile
    assert (
        "--install-container-carrier /opt/super-coder/deepseek-runtime "
        '"$(uname -m)"'
    ) in folded
    assert "RUN python -m venv /opt/super-coder/deepseek-runtime" not in folded


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
