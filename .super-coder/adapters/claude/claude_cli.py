#!/usr/bin/env python3
"""Claude Code capability probe used by the launcher and dormant adapter."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable


# Validated version floor — newer CLIs are accepted on feature-probe evidence
# (the flag detection below), older ones fail closed. Support-latest policy:
# a version bump alone never disables session control.
MIN_TESTED_VERSION = (2, 1)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=5, check=False
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def probe_claude(
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
) -> dict:
    """Probe real CLI flags; unknown versions fail active delivery closed."""
    try:
        version_result = runner(["claude", "--version"])
        help_result = runner(["claude", "--help"])
    except (OSError, subprocess.SubprocessError):
        return {
            "cli_version": None,
            "create": False,
            "deliver": False,
            "resume": False,
            "active_delivery": False,
            "normal_steer": False,
        }

    version_text = _output(version_result).strip()
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", version_text)
    version = tuple(map(int, match.groups())) if match else None
    help_text = _output(help_result)
    known = bool(version and version[:2] >= MIN_TESTED_VERSION)
    create = (
        known
        and help_result.returncode == 0
        and "--session-id" in help_text
        and "--permission-mode" in help_text
    )
    resume = (
        version_result.returncode == 0
        and help_result.returncode == 0
        and "--resume" in help_text
        and "--print" in help_text
        and "--model" in help_text
        and "--effort" in help_text
    )
    return {
        "cli_version": ".".join(map(str, version)) if version else None,
        "create": create,
        "deliver": create,
        "resume": resume,
        "active_delivery": create,
        "normal_steer": False,
    }
