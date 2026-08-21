#!/usr/bin/env python3
"""Isolated DeepSeek SDK/runtime carrier contracts."""
from __future__ import annotations

import io
import importlib.util
import json
import os
import shutil
import stat
import subprocess
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


def native_request_metadata(model: str, efforts) -> dict:
    return {
        effort: {
            purpose: {
                "event_type": "provider.request",
                "provider": "deepseek-official",
                "model": model,
                "reasoning_effort": None if effort == "default" else effort,
                "reserved_default_omitted": effort == "default",
                "shell_tool_declared": purpose == "conversation",
                "purpose": purpose,
            }
            for purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES
        }
        for effort in efforts
    }


def emit_provider_requests(argv, kwargs) -> dict:
    options_by_effort = json.loads(argv[-1])
    for options in options_by_effort.values():
        for purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES:
            body = {"model": argv[-2], "messages": [], "stream": True}
            if purpose == "conversation":
                body["tools"] = [deepseek_runtime.PROVIDER_WIRE_SHELL_TOOL]
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
    return options_by_effort


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
                "canary": {
                    "schema_version": 1,
                    "contract": "deepseek-production-skill-resume-v1",
                    "source_commit": manifest["source"]["commit"],
                    "composition_sha256": manifest["composition"]["sha256"],
                    "initial_catalog": ["changed", "current", "revoked"],
                    "resumed_catalog": ["changed", "current", "new"],
                    "changed_body_refreshed": True,
                    "new_grant_loadable": True,
                    "revoked_grant_absent": True,
                    "boot_digest_preserved": True,
                    "native_session_preserved": True,
                    "fresh_carrier_process": True,
                    "initial_terminal": "run.completed",
                    "resumed_terminal": "run.completed",
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
        "worker_path": "scripts/deepseek_carrier_worker.py",
        "worker_sha256": "c75ad8ff34dd659a5d2d1c4727f619c599a0c18924269be82a1b5ca605d39c34",
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
        "@deepseek-ai/dsh-llm-deepseek",
    )
    assert not any(
        forbidden in name
        for name in names
        for forbidden in ("approval", "question", "plugin", "web", "terminal")
    )


def test_provider_compositions_activate_only_the_selected_adapter() -> None:
    expected = {
        "deepseek-official": (
            "@deepseek-ai/dsh-llm-deepseek",
            "@deepseek-ai/dsh-llm-pi-ai",
        ),
        "ollama-cloud": (
            "@deepseek-ai/dsh-llm-pi-ai",
            "@deepseek-ai/dsh-llm-deepseek",
        ),
    }

    for provider, (selected, excluded) in expected.items():
        composition = deepseek_runtime.provider_composition(provider)
        path = Path(composition["path"])
        body = path.read_text()
        names = tuple(
            line.split("name:", 1)[1].strip().strip("'")
            for line in body.splitlines()
            if line.strip().startswith("name:")
        )
        adapter = deepseek_runtime.provider_adapter(provider)

        assert composition["sha256"] == adapter["composition_sha256"]
        assert deepseek_runtime._sha256(path) == composition["sha256"]
        assert names.count(selected) == 1
        assert excluded not in names


def test_composition_enables_native_skills_only_from_the_rendered_grant_root() -> None:
    manifest = deepseek_runtime.load_runtime_manifest()
    composition = ROOT / ".super-coder" / manifest["composition"]["path"]
    body = composition.read_text()

    assert "skills:\n      enabled: true" in body
    assert "includeDefaultRoots: false" in body
    assert "- !!js process.env.DSH_SKILL_ROOT" in body
    assert "watch: false" in body
    assert "DSH_AGENTS_HOME" not in body
    assert "DSH_BUNDLED_SKILL_DIR" not in body


def test_composition_digest_drift_fails_closed() -> None:
    manifest = deepseek_runtime.load_runtime_manifest()
    composition = ROOT / ".super-coder" / manifest["composition"]["path"]
    real_sha256 = deepseek_runtime._sha256

    def changed_digest(path: Path) -> str:
        return "0" * 64 if path == composition else real_sha256(path)

    with mock.patch.object(deepseek_runtime, "_sha256", side_effect=changed_digest):
        try:
            deepseek_runtime.load_runtime_manifest()
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            assert exc.code == "HARNESS_COMPOSITION_DRIFT"
            assert manifest["composition"]["sha256"] in exc.detail
        else:
            raise AssertionError("composition drift was accepted")


def test_ollama_composition_drift_fails_only_when_that_route_is_selected() -> None:
    official = deepseek_runtime.provider_composition("deepseek-official")
    ollama = deepseek_runtime.provider_composition("ollama-cloud")
    real_sha256 = deepseek_runtime._sha256

    def changed_digest(path: Path) -> str:
        return "0" * 64 if path == Path(ollama["path"]) else real_sha256(path)

    with mock.patch.object(deepseek_runtime, "_sha256", side_effect=changed_digest):
        assert deepseek_runtime.provider_composition("deepseek-official") == official
        try:
            deepseek_runtime.provider_composition("ollama-cloud")
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            assert exc.code == "HARNESS_COMPOSITION_DRIFT"
            assert ollama["sha256"] in exc.detail
        else:
            raise AssertionError("changed Ollama composition was accepted")


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
            "composition_sha256": deepseek_runtime.load_runtime_manifest()["composition"]["sha256"],
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


def test_built_artifact_without_production_skill_canary_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_artifact_evidence(directory)
        evidence_path = directory / "deepseek-carrier-artifacts.json"
        evidence = json.loads(evidence_path.read_text())
        del evidence["canary"]
        evidence_path.write_text(json.dumps(evidence))

        try:
            deepseek_runtime._load_built_artifacts(
                directory, manifest=deepseek_runtime.load_runtime_manifest()
            )
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            assert exc.code == "HARNESS_RUNTIME_ARTIFACT_DRIFT"
            assert "canary" in exc.detail
        else:
            raise AssertionError("artifact without production skill canary was accepted")


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

    runner_calls = []

    def runner(argv, **kwargs):
        runner_calls.append((argv, kwargs))
        options_by_effort = emit_provider_requests(argv, kwargs)
        return completed(stdout=json.dumps(
            native_request_metadata(argv[-2], options_by_effort)
        ))

    evidence = deepseek_runtime.provider_wire_evidence(
        "deepseek-official",
        "deepseek-v4-pro",
        {
            "default": {"thinking": "omit", "reasoningEffort": "omit"},
            "low": {"thinking": "enabled", "reasoningEffort": "low"},
            "high": {"thinking": "enabled", "reasoningEffort": "high"},
            "max": {"thinking": "enabled", "reasoningEffort": "max"},
        },
        env={},
        runner=runner,
        status=status,
    )

    assert evidence["contract"] == deepseek_runtime.PROVIDER_WIRE_CONTRACT
    assert evidence["proofs"]["default"]["wire_options"] == {}
    assert evidence["proofs"]["default"]["native_request"] == {
        "event_type": "provider.request",
        "provider": "deepseek-official",
        "model": "deepseek-v4-pro",
        "reasoning_effort": None,
        "reserved_default_omitted": True,
        "shell_tool_declared": True,
        "purpose": "conversation",
    }
    assert set(evidence["proofs"]["default"]["purpose_proofs"]) == set(
        deepseek_runtime.PROVIDER_WIRE_PURPOSES
    )
    for purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES:
        purpose_proof = evidence["proofs"]["default"]["purpose_proofs"][purpose]
        assert purpose_proof["wire_options"] == {}
        assert purpose_proof["native_request"]["purpose"] == purpose
        assert purpose_proof["shell_tool"] == (
            [deepseek_runtime.PROVIDER_WIRE_SHELL_TOOL]
            if purpose == "conversation"
            else None
        )
    for effort in ("low", "high", "max"):
        assert evidence["proofs"][effort]["wire_options"] == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": effort,
        }
        assert evidence["proofs"][effort]["native_request"] == {
            "event_type": "provider.request",
            "provider": "deepseek-official",
            "model": "deepseek-v4-pro",
            "reasoning_effort": effort,
            "reserved_default_omitted": False,
            "shell_tool_declared": True,
            "purpose": "conversation",
        }
    assert len(evidence["proofs"]["default"]["digest"]) == 64
    assert len(runner_calls) == 1
    assert runner_calls[0][1]["timeout"] == 30


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
        options_by_effort = json.loads(argv[-1])
        assert list(options_by_effort) == ["default"]
        body = {
            "model": argv[-2],
            "messages": [],
            "stream": True,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        for _purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES:
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
        return completed(stdout=json.dumps(
            native_request_metadata(argv[-2], options_by_effort)
        ))

    try:
        deepseek_runtime.provider_wire_evidence(
            "deepseek-official",
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


def test_provider_wire_evidence_rejects_missing_native_request_metadata() -> None:
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
        emit_provider_requests(argv, kwargs)
        return completed(stdout="")

    try:
        deepseek_runtime.provider_wire_evidence(
            "deepseek-official",
            "deepseek-v4-pro",
            {"default": {"thinking": "omit", "reasoningEffort": "omit"}},
            env={},
            runner=runner,
            status=status,
        )
    except deepseek_runtime.DeepSeekRuntimeError as exc:
        assert exc.code == "HARNESS_PROVIDER_WIRE_INVALID"
        assert exc.detail == "carrier returned no valid native request metadata"
    else:
        raise AssertionError("wire-only receipt entered controlled evidence")


def test_provider_wire_evidence_rejects_missing_egress_purpose() -> None:
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
        options_by_effort = emit_provider_requests(argv, kwargs)
        metadata = native_request_metadata(argv[-2], options_by_effort)
        del metadata["default"]["session-title"]
        return completed(stdout=json.dumps(metadata))

    try:
        deepseek_runtime.provider_wire_evidence(
            "deepseek-official",
            "deepseek-v4-pro",
            {"default": {"thinking": "omit", "reasoningEffort": "omit"}},
            env={},
            runner=runner,
            status=status,
        )
    except deepseek_runtime.DeepSeekRuntimeError as exc:
        assert exc.code == "HARNESS_PROVIDER_WIRE_INVALID"
        assert exc.detail == "native request purposes are incomplete for effort default"
    else:
        raise AssertionError("incomplete purpose proof entered controlled evidence")


def test_provider_wire_evidence_rejects_mismatched_native_effort() -> None:
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
        options_by_effort = emit_provider_requests(argv, kwargs)
        metadata = native_request_metadata(argv[-2], options_by_effort)
        metadata["low"]["conversation"]["reasoning_effort"] = "high"
        return completed(stdout=json.dumps(metadata))

    try:
        deepseek_runtime.provider_wire_evidence(
            "deepseek-official",
            "deepseek-v4-pro",
            {"low": {"thinking": "enabled", "reasoningEffort": "low"},
             "default": {"thinking": "omit", "reasoningEffort": "omit"}},
            env={},
            runner=runner,
            status=status,
        )
    except deepseek_runtime.DeepSeekRuntimeError as exc:
        assert exc.code == "HARNESS_PROVIDER_WIRE_MISMATCH"
        assert exc.detail == (
            "native request metadata does not match effort low purpose conversation"
        )
    else:
        raise AssertionError("mismatched native effort entered controlled evidence")


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


def test_dockerfile_stages_a_runnable_deepseek_container_entrypoint() -> None:
    dockerfile = (ROOT / ".super-coder" / "Dockerfile").read_text()
    container_root = Path("/opt/super-coder/deepseek-bootstrap")

    with tempfile.TemporaryDirectory() as raw:
        staging_root = Path(raw) / "deepseek-bootstrap"
        for line in dockerfile.splitlines():
            fields = line.split()
            if (
                len(fields) != 3
                or fields[0] != "COPY"
                or not fields[2].startswith(f"{container_root}/")
            ):
                continue
            source = ROOT / fields[1]
            destination = staging_root / Path(fields[2]).relative_to(container_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)

        target = Path(raw) / "deepseek-runtime"
        completed = subprocess.run(
            [
                sys.executable,
                str(staging_root / "scripts" / "deepseek_runtime.py"),
                "--install-container-carrier",
                str(target),
                "s390x",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        payload = json.loads(completed.stdout)
        assert payload["available"] is False
        assert payload["error"] == "HARNESS_RUNTIME_INCOMPATIBLE"
        assert target.with_suffix(".unavailable.json").is_file()


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
                "DSH_SKILL_ROOT": "/home/operator/.agents/skills",
                "DSH_PROFILE": "mutable-personal-profile",
                "DEEPSEEK_API_KEY": "old-secret",
                "ANTHROPIC_API_KEY": "ambient-anthropic-secret",
                "KIMI_API_KEY": "ambient-kimi-secret",
                "MISTRAL_API_KEY": "ambient-mistral-secret",
                "OLLAMA_API_KEY": "ambient-ollama-secret",
                "OPENAI_API_KEY": "ambient-openai-secret",
            },
        )
        redacted = deepseek_runtime.redacted_environment(child)

        assert child["PATH"] == "/usr/bin"
        assert child["DSH_HOME"] == str(layout.home)
        assert child["DSH_SESSION_ROOT"] == str(layout.session_root)
        assert child["DSH_CWD"] == str(worktree)
        assert child["DSH_SKILL_ROOT"] == str(worktree / ".agents" / "skills")
        assert child["DSH_SYSTEM_PROMPT"] == "immutable boot bytes"
        assert child["DEEPSEEK_API_KEY"] == "sk-private-credential"
        assert child["DEEPSEEK_BASE_URL"] == "https://api.deepseek.example/v1"
        assert child["DSH_CORDIS_CONFIG"].endswith("/assets/deepseek/cordis.yml")
        assert "DSH_PROFILE" not in child
        assert "/home/operator/.dsh" not in child.values()
        assert "old-secret" not in child.values()
        assert {
            name
            for name in child
            if name in {
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "KIMI_API_KEY",
                "MISTRAL_API_KEY",
                "OLLAMA_API_KEY",
                "OPENAI_API_KEY",
            }
        } == {"DEEPSEEK_API_KEY"}
        for leaked in (
            "ambient-anthropic-secret",
            "ambient-kimi-secret",
            "ambient-mistral-secret",
            "ambient-ollama-secret",
            "ambient-openai-secret",
        ):
            assert leaked not in child.values()
        assert redacted["DEEPSEEK_API_KEY"] == "[REDACTED]"
        assert "sk-private-credential" not in json.dumps(redacted)


def test_ollama_launch_projects_only_its_fixed_provider_credential() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree = root / "worktree"
        worktree.mkdir()
        layout = deepseek_runtime.conversation_layout(42, state_root=root / "state")

        child = deepseek_runtime.launch_environment(
            layout,
            worktree=worktree,
            system_prompt="immutable boot bytes",
            provider="ollama-cloud",
            api_key="ollama-private-credential",
            base_env={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "ambient-anthropic-secret",
                "DEEPSEEK_API_KEY": "ambient-deepseek-secret",
                "DEEPSEEK_BASE_URL": "https://attacker.example/v1",
                "KIMI_API_KEY": "ambient-kimi-secret",
                "MISTRAL_API_KEY": "ambient-mistral-secret",
                "OLLAMA_API_KEY": "old-ollama-secret",
                "OPENAI_API_KEY": "ambient-openai-secret",
                "SC_DEEPSEEK_PROVIDER": "deepseek-official",
            },
        )

        assert child["OLLAMA_API_KEY"] == "ollama-private-credential"
        assert child["SC_DEEPSEEK_PROVIDER_BASE_URL"] == "https://ollama.com/v1"
        assert child["DSH_CORDIS_CONFIG"].endswith(
            "/assets/deepseek/cordis-ollama-cloud.yml"
        )
        assert "DEEPSEEK_API_KEY" not in child
        assert "DEEPSEEK_BASE_URL" not in child
        assert "SC_DEEPSEEK_PROVIDER" not in child
        assert {
            name
            for name in child
            if name in {
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "KIMI_API_KEY",
                "MISTRAL_API_KEY",
                "OLLAMA_API_KEY",
                "OPENAI_API_KEY",
            }
        } == {"OLLAMA_API_KEY"}
        for leaked in (
            "ambient-anthropic-secret",
            "ambient-deepseek-secret",
            "ambient-kimi-secret",
            "ambient-mistral-secret",
            "ambient-openai-secret",
            "old-ollama-secret",
        ):
            assert leaked not in child.values()


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


def test_failed_command_diagnostic_retains_redacted_terminal_cause() -> None:
    secret = "private-token-value"
    completed_process = subprocess.CompletedProcess(
        ["builder"],
        -9,
        stdout="carrier build started\n" + "ordinary output " * 200,
        stderr=(
            f"Authorization: Bearer {secret}\n"
            + "warning output " * 300
            + "\nterminal failure cause"
        ),
    )

    diagnostic = deepseek_runtime.failed_command_diagnostic(
        completed_process, secrets=(secret,), limit=256
    )

    assert len(diagnostic) == 256
    assert diagnostic.startswith("exit code -9: carrier build started")
    assert "…[truncated middle]…" in diagnostic
    assert diagnostic.endswith("terminal failure cause")
    assert secret not in diagnostic
    assert "Authorization: Bearer" not in diagnostic


def test_carrier_worker_redacts_ollama_credential_from_errors() -> None:
    worker_path = ROOT / ".super-coder" / "scripts" / "deepseek_carrier_worker.py"
    spec = importlib.util.spec_from_file_location("deepseek_carrier_worker_test", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    stub = SimpleNamespace(HarnessClient=object, HarnessConfig=object)
    with mock.patch.dict(sys.modules, {"deepseek_harness": stub}):
        spec.loader.exec_module(module)

    secret = "ollama-private-credential-value"
    detail = module._detail(ValueError(f"OLLAMA_API_KEY={secret}"))

    assert secret not in detail
    assert detail == "OLLAMA_API_KEY=[REDACTED]"


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
