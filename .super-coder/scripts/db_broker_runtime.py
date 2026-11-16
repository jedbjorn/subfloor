#!/usr/bin/env python3
"""Provision the host DB broker's driver without modifying the engine Python."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import venv

import dbq

PSYCOPG_VERSION = "3.3.5"
REQUIREMENT = f"psycopg[binary]=={PSYCOPG_VERSION}"


def ready(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-c", "import psycopg; print(psycopg.__version__)"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == PSYCOPG_VERSION
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure() -> Path:
    if os.environ.get("SC_SANDBOX"):
        raise RuntimeError("db-broker interpreter provisioning is host-only")
    configured = (dbq.read() or {}).get("broker_python")
    override = os.environ.get("SC_DB_BROKER_PYTHON") or configured
    if override:
        python = Path(override).expanduser().absolute()
        if not ready(python):
            raise RuntimeError(f"configured broker Python requires {REQUIREMENT}: {python}")
        return python

    # Outside the repo and sandbox mounts; versioned so an update never changes
    # a running broker's environment. A failed install is retried on next use.
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").absolute()
    version = ".".join(map(str, sys.version_info[:3]))
    folder = data / "subfloor/db-broker-venvs" / f"python-{version}-psycopg-{PSYCOPG_VERSION}"
    python = folder / "bin/python"
    folder.parent.mkdir(parents=True, exist_ok=True)
    with (folder.parent / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not ready(python):
            venv.EnvBuilder(with_pip=True).create(folder)
            subprocess.run(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check", REQUIREMENT],
                check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=300,
            )
            if not ready(python):
                raise RuntimeError("db-broker driver import failed after provisioning")
    return python


def main(argv: list[str]) -> int:
    if argv:
        raise SystemExit("usage: db_broker_runtime.py")
    try:
        print(ensure())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"db-broker runtime: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
