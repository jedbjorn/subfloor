#!/usr/bin/env python3
"""Backfill api_key + api_key_hash + api_key_rotated_at for shells that have none.

Run once after migration 0026 on an existing fork. New shells get keys at
creation time via shell_factory.py. The admin shell (flavor='admin') is
included — it interacts with the API too.

Usage:
    python3 .super-coder/scripts/backfill_shell_api_keys.py <path-to-db>
"""
from __future__ import annotations

import hashlib
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402


def backfill(db_path: str) -> int:
    con = db_driver.connect(None if db_driver.is_postgres() else db_path)
    try:
        shells = con.execute(
            "SELECT shell_id FROM shells "
            "WHERE api_key_hash IS NULL AND COALESCE(is_deleted,0)=0"
        ).fetchall()
        if not shells:
            print("backfill_shell_api_keys: nothing to do — all shells already keyed.")
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for row in shells:
            sid = row[0]
            key = secrets.token_urlsafe(32)
            khash = hashlib.sha256(key.encode()).hexdigest()
            con.execute(
                "UPDATE shells SET api_key=?, api_key_hash=?, api_key_rotated_at=? "
                "WHERE shell_id=?",
                (key, khash, now, sid),
            )
            print(f"  keyed shell_id={sid}")
        con.commit()
        print(f"backfill_shell_api_keys: {len(shells)} shell(s) keyed.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <path-to-db>")
    sys.exit(backfill(sys.argv[1]))
