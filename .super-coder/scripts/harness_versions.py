#!/usr/bin/env python3
"""Report the harness CLI versions of the runtime this runs in.

Nothing else surfaces these, and that is how a regression hid: the model picker
and the catalogue show ALIASES (`opus`, `sonnet`), never the CLI build behind
them, so a sandbox baked with claude 2.1.218 — one release before Opus 5 landed
in 2.1.219 — silently offered an `opus` that could not resolve to Opus 5. Nobody
could see the version that decided it.

Run inside the sandbox (`./sc harness-status` docker-execs it there) so the
answer is the version SHELLS run, not the host's own copy. The host's CLIs are
irrelevant on the docker path: the container mounts creds, never binaries.

Each probe is capped by a timeout — a harness that hangs on `--version` must not
be able to hang a launch banner — and a missing harness is reported, not fatal.
"""
from __future__ import annotations

import json
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


def main(argv: list[str]) -> int:
    found = versions()
    if "--json" in argv:
        print(json.dumps(found, indent=2))
        return 0
    for name, version in found.items():
        print(f"  {name:9} {version or '— not installed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
