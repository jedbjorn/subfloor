#!/usr/bin/env python3
"""Runtime provenance and adapter compatibility status contracts."""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import harness_versions  # noqa: E402


STATUS = {
    "codex": {
        "version": "0.145.0",
        "compatibility": "verified",
        "minimum_version": "0.145.0",
        "maximum_version_exclusive": "0.147.0",
        "verified_version": "0.145.0",
        "error": None,
    }
}


def test_runtime_binary_version_is_checked_against_adapter_range() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("claude",)),
        mock.patch.object(
            harness_versions,
            "probe",
            return_value="2.1.222 (Claude Code)",
        ),
    ):
        assert harness_versions.compatibility_status() == {
            "claude": {
                "version": "2.1.222",
                "compatibility": "verified",
                "minimum_version": "2.1.220",
                "maximum_version_exclusive": "2.2.0",
                "verified_version": "2.1.222",
                "error": None,
            }
        }


def test_newer_runtime_is_reported_without_becoming_an_error() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("codex",)),
        mock.patch.object(
            harness_versions,
            "probe",
            return_value="codex-cli 0.147.0",
        ),
    ):
        assert harness_versions.compatibility_status() == {
            "codex": {
                "version": "0.147.0",
                "compatibility": "newer-unverified",
                "minimum_version": "0.145.0",
                "maximum_version_exclusive": "0.147.0",
                "verified_version": "0.145.0",
                "error": None,
            }
        }


def test_missing_non_conversation_harness_is_reported_unavailable() -> None:
    with (
        mock.patch.object(harness_versions, "HARNESSES", ("vibe",)),
        mock.patch.object(harness_versions, "probe", return_value=None),
    ):
        assert harness_versions.compatibility_status() == {
            "vibe": {
                "version": None,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_UNAVAILABLE",
            }
        }


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
        "  codex     0.145.0 · verified · supported [0.145.0, 0.147.0)",
    ]


def test_text_status_marks_newer_runtime_as_unverified() -> None:
    status = {
        "codex": {
            **STATUS["codex"],
            "version": "0.147.0",
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
        "  codex     0.147.0 · newer-unverified · tested [0.145.0, 0.147.0)",
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
