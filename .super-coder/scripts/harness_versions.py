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
import socket
import subprocess
import sys

# Probe order = the order the harness picker lists them.
HARNESSES = ("claude", "codex", "opencode", "vibe", "kimi")
TIMEOUT = 8
SEMVER_TOKEN = re.compile(
    r"(?:^|(?<=\s))v?((\d+\.\d+\.\d+)(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?)(?=$|\s|\()"
)
# Only these complete observed outputs earned the maintained-version canary.
# Parsing a matching semantic core is useful diagnostic evidence, but it must
# not promote an arbitrary wrapper or custom build to the tested assurance.
MAINTAINED_OBSERVED_VERSIONS = {
    "claude": "2.1.223 (Claude Code)",
    "codex": "codex-cli 0.147.0",
    "opencode": "1.18.9",
    "vibe": "vibe 2.22.0",
    "kimi": "0.33.0",
}


def runtime_scope(*, env=None, hostname: str | None = None) -> dict[str, str]:
    """Identify the execution seat whose binaries this process can probe."""
    env = os.environ if env is None else env
    runtime = "sandbox" if env.get("SC_SANDBOX") else "host"
    hostname = socket.gethostname() if hostname is None else hostname
    return {
        "runtime": runtime,
        "runtime_identity": f"{runtime}:{hostname}",
    }


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
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    return out[0].strip() if out else None


def versions() -> dict[str, str | None]:
    return {name: probe(name) for name in HARNESSES}


def compatibility_status(
    harnesses: tuple[str, ...] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Report runtime binaries through the adapters' shared range contract."""
    from conversation_adapters import ADAPTER_TYPES, AdapterError
    from conversation_adapters.base import (
        ADAPTERS,
        checked_version_compatibility,
        load_manifest,
    )

    harnesses = HARNESSES if harnesses is None else harnesses
    scope = runtime_scope()
    found: dict[str, dict[str, str | None]] = {}
    for name in harnesses:
        identity = {"harness": name, **scope}
        raw_version = probe(name)
        if raw_version is None:
            found[name] = {
                **identity,
                "version": None,
                "observed_version": None,
                "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_UNAVAILABLE",
            }
            continue
        match = SEMVER_TOKEN.search(raw_version)
        version = match.group(1) if match else None
        manifest = {}
        try:
            if name in ADAPTER_TYPES:
                manifest = load_manifest(name)
                compatibility = manifest.get("conversation") or {}
            else:
                adapter = json.loads(
                    (ADAPTERS / name / "adapter.json").read_text()
                )
                compatibility = adapter.get("runtime_compatibility") or {}
                manifest = {"conversation": compatibility}
            validation_version = (match.group(2) if match else None) or compatibility.get("verified_cli_version")
            result = checked_version_compatibility(
                harness=name,
                compatibility=compatibility,
                version=validation_version,
            )
        except (AdapterError, OSError, json.JSONDecodeError) as exc:
            conversation = manifest.get("conversation", {})
            if isinstance(exc, AdapterError) and exc.code == "HARNESS_VERSION_UNSUPPORTED":
                found[name] = {
                    **identity,
                    "version": version,
                    "observed_version": raw_version,
                    "compatibility": "older-unverified",
                    "minimum_version": conversation.get("minimum_cli_version"),
                    "maximum_version_exclusive": conversation.get(
                        "maximum_cli_version_exclusive"
                    ),
                    "verified_version": conversation.get("verified_cli_version"),
                    "error": None,
                }
                continue
            found[name] = {
                **identity,
                "version": version,
                "observed_version": raw_version,
                "compatibility": None,
                "minimum_version": conversation.get("minimum_cli_version"),
                "maximum_version_exclusive": conversation.get(
                    "maximum_cli_version_exclusive"
                ),
                "verified_version": conversation.get("verified_cli_version"),
                "error": exc.code if isinstance(exc, AdapterError)
                else "HARNESS_MANIFEST_INVALID",
            }
            continue
        if version is None:
            found[name] = {
                **identity,
                "version": None,
                "observed_version": raw_version,
                "compatibility": "non-semver",
                "minimum_version": result.minimum_version,
                "maximum_version_exclusive": result.maximum_version_exclusive,
                "verified_version": result.verified_version,
                "error": None,
            }
            continue
        compatibility_state = result.compatibility
        if version != match.group(2) and compatibility_state == "verified":
            compatibility_state = "prerelease-unverified"
        elif (
            compatibility_state == "verified"
            and raw_version != MAINTAINED_OBSERVED_VERSIONS.get(name)
        ):
            compatibility_state = "custom-unverified"
        found[name] = {
            **identity,
            "version": version,
            "observed_version": raw_version,
            "compatibility": compatibility_state,
            "minimum_version": result.minimum_version,
            "maximum_version_exclusive": result.maximum_version_exclusive,
            "verified_version": result.verified_version,
            "error": None,
        }
    return found


def main(argv: list[str]) -> int:
    provenance = runtime_scope()["runtime"]
    found = compatibility_status()
    if "--json" in argv:
        print(json.dumps({"runtime": provenance, "harnesses": found}, indent=2))
        return 0
    print(f"  runtime:   {provenance}")
    for name, status in found.items():
        version = status["version"]
        observed_version = status.get("observed_version") or version
        compatibility = status["compatibility"]
        if status["error"]:
            detail = f"{observed_version or '—'} · {status['error']}"
        elif observed_version and compatibility == "verified":
            detail = (
                f"{observed_version} · {compatibility} · tested "
                f"[{status['minimum_version']}, "
                f"{status['maximum_version_exclusive']})"
            )
        elif observed_version and compatibility:
            detail = (
                f"{observed_version} · {compatibility} · best-effort"
            )
        elif observed_version:
            detail = observed_version
        else:
            detail = "— not installed"
        print(f"  {name:9} {detail}")
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
