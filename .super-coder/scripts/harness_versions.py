#!/usr/bin/env python3
"""Report the harness CLI versions of the runtime this runs in.

Nothing else surfaces these, and that is how a regression hid: the model picker
and the catalogue show ALIASES (`opus`, `sonnet`), never the CLI build behind
them, so a sandbox baked with claude 2.1.218 — one release before Opus 5 landed
in 2.1.219 — silently offered an `opus` that could not resolve to Opus 5. Nobody
could see the version that decided it.

Run inside the sandbox (`./sc harness-status` docker-execs it there) so the
answer is the version SHELLS run, not the host's own copy. The host's CLIs are
irrelevant on the docker path: state homes are mounted, but launchers must
resolve image-owned executables.

Each probe is capped by a timeout — a harness that hangs on `--version` must not
be able to hang a launch banner — and a missing harness is reported, not fatal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# Probe order = the order the harness picker lists them.
HARNESSES = ("claude", "codex", "opencode", "vibe", "kimi")
TIMEOUT = 8


def probe(name: str) -> str | None:
    """First line of `<harness> --version`, or None when absent/unusable.
    Absent, hung, and crashed all collapse to None on purpose: the caller is a
    status line, and every one of those means "cannot tell you a version"."""
    if not shutil.which(name):
        return None
    try:
        proc = subprocess.run([name, "--version"], capture_output=True, text=True,
                              timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    return out[0].strip() if out else None


def versions() -> dict[str, str | None]:
    return {name: probe(name) for name in HARNESSES}


def compatibility_status() -> dict[str, dict[str, str | None]]:
    """Report runtime binaries through the adapters' shared range contract."""
    from conversation_adapters import ADAPTER_TYPES, AdapterError
    from conversation_adapters.base import (
        AdapterCapabilities,
        checked_probe_result,
        load_manifest,
    )

    found: dict[str, dict[str, str | None]] = {}
    for name in HARNESSES:
        raw_version = probe(name)
        match = re.search(r"\d+\.\d+\.\d+", raw_version or "")
        version = match.group(0) if match else None
        if name not in ADAPTER_TYPES:
            found[name] = {
                "version": raw_version,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": None,
            }
            continue
        if version is None:
            found[name] = {
                "version": None,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_UNAVAILABLE",
            }
            continue
        manifest = {}
        try:
            manifest = load_manifest(name)
            result = checked_probe_result(
                harness=name,
                manifest=manifest,
                capabilities=AdapterCapabilities.from_manifest(manifest),
                version=version,
            )
        except AdapterError as exc:
            conversation = manifest.get("conversation", {})
            found[name] = {
                "version": version,
                "compatibility": None,
                "minimum_version": conversation.get("minimum_cli_version"),
                "maximum_version_exclusive": conversation.get(
                    "maximum_cli_version_exclusive"
                ),
                "verified_version": conversation.get("verified_cli_version"),
                "error": exc.code,
            }
            continue
        found[name] = {
            "version": result.version,
            "compatibility": result.compatibility,
            "minimum_version": result.minimum_version,
            "maximum_version_exclusive": result.maximum_version_exclusive,
            "verified_version": result.verified_version,
            "error": None,
        }
    return found


def main(argv: list[str]) -> int:
    provenance = "sandbox" if os.environ.get("SC_SANDBOX") else "host"
    found = compatibility_status()
    if "--json" in argv:
        print(json.dumps({"runtime": provenance, "harnesses": found}, indent=2))
        return 0
    print(f"  runtime:   {provenance}")
    for name, status in found.items():
        version = status["version"]
        compatibility = status["compatibility"]
        if status["error"]:
            detail = f"{version or '—'} · {status['error']}"
        elif version and compatibility:
            detail = (
                f"{version} · {compatibility} · supported "
                f"[{status['minimum_version']}, "
                f"{status['maximum_version_exclusive']})"
            )
        elif version:
            detail = version
        else:
            detail = "— not installed"
        print(f"  {name:9} {detail}")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
