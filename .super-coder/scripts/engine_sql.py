#!/usr/bin/env python3
"""Admin-only engine SQL passthrough.

Authorization comes from the launched shell bearer token, resolved by
``engine_identity`` (the one identity check shared with the serialization
gate). The API is the preferred authority; when it is unavailable, the host
Admin recovery seat may resolve the same token against the canonical active
database. Unlike serialization, engine SQL requires a positive Admin identity:
a caller without a token must adopt the host Admin runtime credential.
Caller-supplied flavor and path environment variables are deliberately
ignored.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import engine_identity
import instance_state
import mem

ENGINE = Path(__file__).resolve().parents[1]
ERROR_CODE = "admin_only_engine_state"
MAINTENANCE_ERROR_CODE = "maintenance_cutover_required"


def refuse() -> NoReturn:
    sys.exit(
        f"{ERROR_CODE}: general engine SQL is available only to Admin — an "
        "ordinary shell reads and writes engine state through `sc mem`, so "
        "raw SQL would bypass the API that keeps the shared instance coherent."
    )


def refuse_write() -> NoReturn:
    sys.exit(
        f"{MAINTENANCE_ERROR_CODE}: engine SQL writes require the Spec #133 "
        "maintenance contract"
    )


def _discovered_admin_credential() -> tuple[str, str]:
    """Adopt the host Admin's runtime credential, or refuse.

    A host Admin seat booted outside run.py carries no token; the supervised
    API provisions an owner-only credential it may adopt (mem.py).
    """
    mem._PROG = "sc sql"
    if not mem._discover_runtime_credential():
        refuse()
    return mem.SC_API_TOKEN, mem.SC_API_BASE


def _admin_token() -> str:
    token = os.environ.get("SC_API_TOKEN", "")
    base = os.environ.get("SC_API_BASE", "")
    if not token:
        token, base = _discovered_admin_credential()
    # The same identity resolution the serialization gate uses. The canonical
    # DB is consulted below in main(), after the API has had its say, so a
    # refused non-Admin never resolves (or learns) the database path.
    caller = engine_identity.resolve(token=token, base=base, db_path=lambda: None)
    if caller.flavor is not None and not caller.admin:
        refuse()
    return token


def _require_local_admin(token: str, db_path: Path) -> None:
    # An unavailable database is not a reason to disclose its path or fall
    # back to caller-controlled identity hints: local_flavor answers None.
    if engine_identity.local_flavor(token, db_path) != engine_identity.ADMIN:
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
