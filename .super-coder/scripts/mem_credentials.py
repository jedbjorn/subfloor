#!/usr/bin/env python3
"""Runtime Admin credential artifacts (spec doc #30 req 11, issue #516).

The supervised engine API provisions one owner-only credential artifact per
Admin shell at every boot, under `.super-coder/run/mem/<shortname>.json` —
the same local trust-boundary pattern as the Interface operator capability
(`api/interface_routes.py:ensure_operator_capability`). A host Admin seat that
was NOT booted through run.py has no `SC_API_BASE`/`SC_API_TOKEN`; `sc mem`
discovers the unique Admin artifact instead of dying unwired (see
`mem.py:_discover_runtime_credential`).

Properties the spec pins:

- mode 0600, parent dir 0700, regular files owned by the service user —
  written as a fresh temp inode and renamed into place, so a symlink at the
  artifact path is replaced, never followed;
- refreshed on every boot — an api_key rotation (startup backfill or rebuild
  re-key) is picked up the next time the service starts;
- never snapshotted or rendered: `.super-coder/run/` is gitignored and the
  snapshot serializes DB tables only, so nothing to exclude elsewhere;
- the token IS the shell's existing `shells.api_key` — no second credential
  is minted (a rotation would 401 every live session's injected SC_API_TOKEN;
  see tests/test_rebuild_keys.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
RUN_DIR = ENGINE / "run" / "mem"


def provision(db_path: str, api_base: str, run_dir: Path = RUN_DIR) -> list[str]:
    """(Re)write one mode-0600 artifact per live, keyed Admin shell and remove
    artifacts whose shell is gone, deleted, demoted, or unkeyed. Idempotent.
    Returns the provisioned shortnames."""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT shell_id, shortname, api_key FROM shells "
            "WHERE flavor='admin' AND COALESCE(is_deleted,0)=0").fetchall()
    finally:
        con.close()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    live: set[str] = set()
    for shell_id, shortname, api_key in rows:
        # An unkeyed shell can't authenticate (pre-backfill DB) — skip it, and
        # let the stale-artifact sweep below drop any file it left behind.
        if not api_key or "/" in shortname or shortname.startswith("."):
            continue
        live.add(shortname)
        payload = json.dumps({
            "shell_id": shell_id,
            "shortname": shortname,
            "api_base": api_base,
            "token": api_key,
        })
        path = run_dir / f"{shortname}.json"
        # Never open the artifact path for writing: a symlink planted there
        # would be followed and its target truncated and overwritten with a
        # bearer token. Write a freshly created 0600 regular file (mkstemp is
        # O_EXCL and ignores the umask) and rename it over the name — rename
        # replaces the link itself, never what the link points at. This also
        # repairs a previously weakened artifact: every boot writes a new inode.
        fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=f".{shortname}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload.encode())
            os.replace(tmp, path)
        except OSError:
            os.unlink(tmp)
            raise
    for stale in run_dir.glob("*.json"):
        if stale.stem not in live:
            stale.unlink()
    return sorted(live)
