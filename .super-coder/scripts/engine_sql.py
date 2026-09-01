#!/usr/bin/env python3
"""Admin-only engine SQL passthrough.

Authorization comes from the launched shell bearer token. The API is the
preferred authority; when it is unavailable, the host Admin recovery seat may
resolve the same token against the canonical active database. Caller-supplied
flavor and path environment variables are deliberately ignored.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

import instance_state
import mem

ENGINE = Path(__file__).resolve().parents[1]
ERROR_CODE = "admin_only_engine_state"
MAINTENANCE_ERROR_CODE = "maintenance_cutover_required"


def refuse() -> NoReturn:
    sys.exit(f"{ERROR_CODE}: general engine SQL is available only to Admin")


def refuse_write() -> NoReturn:
    sys.exit(
        f"{MAINTENANCE_ERROR_CODE}: engine SQL writes require the Spec #133 "
        "maintenance contract"
    )


def _api_flavor(token: str, base: str) -> str | None:
    if not token or not base:
        return None
    request = urllib.request.Request(
        base.rstrip("/") + "/_sc/mem/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read()).get("flavor")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _admin_token() -> str:
    token = os.environ.get("SC_API_TOKEN", "")
    base = os.environ.get("SC_API_BASE", "")
    flavor = _api_flavor(token, base)
    if flavor is not None:
        if flavor != "admin":
            refuse()
        return token

    if not token:
        mem._PROG = "sc sql"
        if not mem._discover_runtime_credential():
            refuse()
        token = mem.SC_API_TOKEN
    return token


def _require_local_admin(token: str, db_path: Path) -> None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT flavor FROM shells WHERE api_key=? "
                "AND COALESCE(is_deleted,0)=0",
                (token,),
            ).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        # A masked/unavailable database is not a reason to disclose its path or
        # fall back to caller-controlled identity hints.
        refuse()
    if row is None or row[0] != "admin":
        refuse()


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("read-only", "read-write"):
        sys.exit("usage: engine_sql.py <read-only|read-write> [sqlite3 args]")
    mode, sqlite_args = argv[0], argv[1:]
    token = _admin_token()
    db_path = instance_state.active_database_path(ENGINE)
    _require_local_admin(token, db_path)

    # Spec #133 owns the stopped-runtime proof, exclusive maintenance lease,
    # WAL-safe backup, verification, and recovery contract. Until that
    # contract exists, arbitrary writes must fail closed before sqlite parses
    # or executes caller input.
    if mode == "read-write":
        refuse_write()

    sqlite = shutil.which("sqlite3")
    if not sqlite:
        sys.exit("engine SQL: sqlite3 is unavailable")
    command = [sqlite]
    command.append("-readonly")
    command.extend((str(db_path), *sqlite_args))
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    from cli_entry import run_cli

    sys.exit(run_cli(main, sys.argv[1:]))
