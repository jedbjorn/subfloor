#!/usr/bin/env python3
"""Launch only the operator-linked, existing Subfloor profile."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


class GuardRefusal(ValueError):
    pass


def operator_path(value: object) -> Path:
    """Host setup intentionally accepts arbitrary absolute Chromium paths."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("browser paths must be absolute paths")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("browser paths must be absolute paths")
    return path


def check(config: dict) -> None:
    """Local State identifies a profile, never a live window or connection."""
    try:
        directory = operator_path(config["user_data_dir"])
        executable = operator_path(config["executable"])
        profile = config["profile_dir_name"]
        if (
            not isinstance(profile, str)
            or profile in ("", ".", "..")
            or "/" in profile
            or "\\" in profile
        ):
            raise GuardRefusal("invalid profile directory")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise GuardRefusal(
                "Chromium executable is unavailable; relink in Scripts → Browser"
            )
        state = json.loads((directory / "Local State").read_text())
        if (
            state.get("profile", {}).get("info_cache", {}).get(profile, {}).get("name")
            != "Subfloor"
        ):
            raise GuardRefusal(
                "linked profile is no longer named Subfloor; relink in Scripts → Browser"
            )
        if not (directory / profile).is_dir():
            raise GuardRefusal(
                "Subfloor profile directory is missing; create it and relink"
            )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, GuardRefusal)
            else "cannot read the linked Subfloor profile; check setup in Scripts → Browser"
        )
        raise GuardRefusal(f"browser profile unavailable: {reason}") from exc


def command(config: dict, argv: list[str]) -> list[str]:
    # The configured profile wins even if an upstream release changes how it
    # supplies Chromium flags. Never accept another profile through argv.
    args = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("--user-data-dir", "--profile-directory"):
            skip = True
        elif not arg.startswith(("--user-data-dir=", "--profile-directory=")):
            args.append(arg)
    return [
        config["executable"],
        f"--user-data-dir={config['user_data_dir']}",
        f"--profile-directory={config['profile_dir_name']}",
        *args,
    ]


def main(argv: list[str]) -> int:
    try:
        config = json.loads(Path(os.environ["SC_BROWSER_GUARD_CONFIG"]).read_text())
        check(config)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    os.execv(config["executable"], command(config, argv))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
