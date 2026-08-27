#!/usr/bin/env python3
"""Runtime provenance and adapter compatibility status contracts."""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import harness_versions  # noqa: E402
import model_catalog  # noqa: E402


STATUS = {
    "codex": {
        "version": "0.147.0",
        "compatibility": "verified",
        "minimum_version": "0.145.0",
        "maximum_version_exclusive": "0.148.0",
        "verified_version": "0.147.0",
        "error": None,
    }
}


def test_runtime_binary_version_is_checked_against_adapter_range() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("claude",)),
        mock.patch.object(
            harness_versions,
            "probe",
            return_value="2.1.223 (Claude Code)",
        ),
    ):
        assert harness_versions.compatibility_status() == {
            "claude": {
                "harness": "claude",
                **harness_versions.runtime_scope(),
                "version": "2.1.223",
                "observed_version": "2.1.223 (Claude Code)",
                "compatibility": "verified",
                "minimum_version": "2.1.220",
                "maximum_version_exclusive": "2.2.0",
                "verified_version": "2.1.223",
                "error": None,
            }
        }


def test_newer_runtime_is_reported_without_becoming_an_error() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("codex",)),
        mock.patch.object(
            harness_versions,
            "probe",
            return_value="codex-cli 0.148.0",
        ),
    ):
        assert harness_versions.compatibility_status() == {
            "codex": {
                "harness": "codex",
                **harness_versions.runtime_scope(),
                "version": "0.148.0",
                "observed_version": "codex-cli 0.148.0",
                "compatibility": "newer-unverified",
                "minimum_version": "0.145.0",
                "maximum_version_exclusive": "0.148.0",
                "verified_version": "0.147.0",
                "error": None,
            }
        }


def test_older_runtime_is_reported_as_best_effort_not_an_error() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("codex",)),
        mock.patch.object(
            harness_versions,
            "probe",
            return_value="codex-cli 0.144.0",
        ),
    ):
        assert harness_versions.compatibility_status() == {
            "codex": {
                "harness": "codex",
                **harness_versions.runtime_scope(),
                "version": "0.144.0",
                "observed_version": "codex-cli 0.144.0",
                "compatibility": "older-unverified",
                "minimum_version": "0.145.0",
                "maximum_version_exclusive": "0.148.0",
                "verified_version": "0.147.0",
                "error": None,
            }
        }


def test_spec_current_harness_versions_all_report_tested() -> None:
    observed = {
        "claude": "2.1.223 (Claude Code)",
        "codex": "codex-cli 0.147.0",
        "opencode": "1.18.9",
        "vibe": "vibe 2.22.0",
        "kimi": "0.33.0",
    }
    with mock.patch.object(
        harness_versions, "probe", side_effect=observed.__getitem__
    ):
        statuses = harness_versions.compatibility_status(tuple(observed))

    assert {
        harness: (status["compatibility"], status["error"])
        for harness, status in statuses.items()
    } == {
        "claude": ("verified", None),
        "codex": ("verified", None),
        "opencode": ("verified", None),
        "vibe": ("verified", None),
        "kimi": ("verified", None),
    }


def test_removed_deepseek_is_absent_from_default_status_roster() -> None:
    with mock.patch.object(
        harness_versions, "probe", return_value="unused"
    ) as probe:
        result = harness_versions.compatibility_status()

    assert "deepseek" not in harness_versions.HARNESSES
    assert "deepseek" not in result
    assert "dsh" not in harness_versions.PROBE_COMMANDS.values()
    assert all(call.args != ("deepseek",) for call in probe.call_args_list)


def test_current_core_prerelease_is_best_effort_not_tested() -> None:
    with mock.patch.object(
        harness_versions, "probe", return_value="codex-cli 0.147.0-dev"
    ):
        status = harness_versions.compatibility_status(("codex",))["codex"]

    assert status["version"] == "0.147.0-dev"
    assert status["compatibility"] == "prerelease-unverified"
    assert status["error"] is None


def test_same_core_non_tokens_are_best_effort_not_tested() -> None:
    for observed in (
        "codex-cli 0.147.0dev", "codex-cli 0.147.0.1",
        "codex-cli 0.147.0_dev", "codex-cli 0.147.0~dev",
        "codex-cli 0.147.0/dev", "codex-cli 0.147.0:dev",
    ):
        with mock.patch.object(harness_versions, "probe", return_value=observed):
            status = harness_versions.compatibility_status(("codex",))["codex"]

        assert status["version"] is None
        assert status["observed_version"] == observed
        assert status["compatibility"] == "non-semver"
        assert status["error"] is None
        assert model_catalog._support_state(status) == "best-effort"


def test_custom_current_core_is_best_effort_not_tested() -> None:
    for observed in (
        "codex-cli 0.147.0(dev)",
        "codex-cli 0.147.0 custom-build",
        "wrapper 0.147.0 (not-the-canary)",
    ):
        with mock.patch.object(harness_versions, "probe", return_value=observed):
            status = harness_versions.compatibility_status(("codex",))["codex"]

        assert status["version"] == "0.147.0"
        assert status["observed_version"] == observed
        assert status["compatibility"] == "custom-unverified"
        assert status["error"] is None
        assert model_catalog._support_state(status) == "best-effort"
        assert model_catalog._parsed_version(observed) is None


def test_non_semver_runtime_is_present_as_best_effort_evidence() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("codex",)),
        mock.patch.object(harness_versions, "probe", return_value="codex dev-build"),
    ):
        status = harness_versions.compatibility_status()["codex"]

    assert status["version"] is None
    assert status["observed_version"] == "codex dev-build"
    assert status["compatibility"] == "non-semver"
    assert status["error"] is None


def test_non_semver_runtime_still_requires_a_valid_adapter_contract() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("codex",)),
        mock.patch.object(harness_versions, "probe", return_value="codex dev-build"),
        mock.patch(
            "conversation_adapters.base.load_manifest",
            side_effect=OSError("missing manifest"),
        ),
    ):
        status = harness_versions.compatibility_status()["codex"]

    assert status["observed_version"] == "codex dev-build"
    assert status["error"] == "HARNESS_MANIFEST_INVALID"


def test_failed_version_command_is_unavailable_not_non_semver_evidence() -> None:
    with (
        mock.patch.object(harness_versions.shutil, "which", return_value="/bin/codex"),
        mock.patch.object(
            harness_versions.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="codex dev-build"),
        ),
    ):
        assert harness_versions.probe("codex") is None


def test_missing_non_conversation_harness_is_reported_unavailable() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("vibe",)),
        mock.patch.object(harness_versions, "probe", return_value=None),
    ):
        assert harness_versions.compatibility_status() == {
            "vibe": {
                "harness": "vibe",
                **harness_versions.runtime_scope(),
                "version": None,
                "observed_version": None,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_UNAVAILABLE",
            }
        }


def test_vibe_runtime_uses_its_non_conversation_compatibility_manifest() -> None:
    with mock.patch.object(
        harness_versions, "probe", return_value="vibe 2.22.0"
    ) as probe:
        status = harness_versions.compatibility_status(("vibe",))

    assert status == {
        "vibe": {
            "harness": "vibe",
            **harness_versions.runtime_scope(),
                "version": "2.22.0",
                "observed_version": "vibe 2.22.0",
            "compatibility": "verified",
            "minimum_version": "2.22.0",
            "maximum_version_exclusive": "2.23.0",
            "verified_version": "2.22.0",
            "error": None,
        }
    }
    probe.assert_called_once_with("vibe")


def test_text_status_couples_host_provenance_and_compatibility() -> None:
    output = io.StringIO()
    with (
        mock.patch.object(harness_versions, "compatibility_status", return_value=STATUS),
        mock.patch.dict(os.environ, {}, clear=True),
        redirect_stdout(output),
    ):
        assert harness_versions.main([]) == 0

    assert output.getvalue().splitlines() == [
        "  runtime:   host",
        "  codex     0.147.0 · verified · tested [0.145.0, 0.148.0)",
    ]


def test_text_status_marks_newer_runtime_as_unverified() -> None:
    status = {
        "codex": {
            **STATUS["codex"],
            "version": "0.148.0",
            "compatibility": "newer-unverified",
        }
    }
    output = io.StringIO()
    with (
        mock.patch.object(harness_versions, "compatibility_status", return_value=status),
        mock.patch.dict(os.environ, {}, clear=True),
        redirect_stdout(output),
    ):
        assert harness_versions.main([]) == 0

    assert output.getvalue().splitlines() == [
        "  runtime:   host",
        "  codex     0.148.0 · newer-unverified · best-effort",
    ]


def test_text_status_keeps_non_semver_runtime_visible_as_best_effort() -> None:
    status = {
        "codex": {
            **STATUS["codex"],
            "version": None,
            "observed_version": "codex dev-build",
            "compatibility": "non-semver",
        }
    }
    output = io.StringIO()
    with (
        mock.patch.object(harness_versions, "compatibility_status", return_value=status),
        mock.patch.dict(os.environ, {}, clear=True),
        redirect_stdout(output),
    ):
        assert harness_versions.main([]) == 0

    assert output.getvalue().splitlines() == [
        "  runtime:   host",
        "  codex     codex dev-build · non-semver · best-effort",
    ]


def test_json_status_couples_sandbox_provenance_and_compatibility() -> None:
    output = io.StringIO()
    with (
        mock.patch.object(harness_versions, "compatibility_status", return_value=STATUS),
        mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}, clear=True),
        redirect_stdout(output),
    ):
        assert harness_versions.main(["--json"]) == 0

    assert json.loads(output.getvalue()) == {
        "runtime": "sandbox",
        "harnesses": STATUS,
    }
