#!/usr/bin/env python3
"""Rebuild the super-coder DB from git-tracked text.

  1. apply schema.sql            (the v1 baseline)
  2. apply migrations/*.sql      (ordered deltas, ledger-tracked)
  3. load .sc-state/content.sql  (per-instance content + memory)

The .db file is gitignored and disposable. Text serializations are the source
of truth; the DB is a cache.

Usage:
    python3 .super-coder/scripts/rebuild.py [--no-backup]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
SCHEMA_SQLITE = ENGINE / "schema.sql"
SNAPSHOT       = REPO_ROOT / ".sc-state" / "content.sql"
SNAPSHOT_LEGACY = ENGINE / "snapshot" / "content.sql"
BACKUP_DIR = Path.home() / "db_backups" / "super-coder"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver    # noqa: E402
import migrate as migrate_mod  # noqa: E402
import map_repo     # noqa: E402
import backfill_shell_api_keys  # noqa: E402  (re-provision api_keys post-rebuild)


def snapshot_path() -> Path:
    return SNAPSHOT if SNAPSHOT.exists() else SNAPSHOT_LEGACY


KEEP_BACKUPS = 5


def prune_backups(prefix: str) -> None:
    backups = sorted(BACKUP_DIR.glob(f"{prefix}.*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def backup_existing() -> None:
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"shell_db.prerebuild.{ts}.db"
    shutil.copy2(DB_PATH, dst)
    prune_backups("shell_db.prerebuild")
    print(f"rebuild: backed up existing DB -> {dst}")


def main(argv: list[str]) -> int:
    schema = SCHEMA_SQLITE
    if not schema.exists():
        sys.exit(f"rebuild: missing {schema}")

    if "--no-backup" not in argv:
        backup_existing()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()

    con = db_driver.connect(DB_PATH)
    try:
        con.executescript(schema.read_text())
        con.commit()
        print("rebuild: schema.sql applied")
    finally:
        con.close()

    migrate_mod.migrate(str(DB_PATH))

    snap = snapshot_path()
    if snap.exists():
        con = db_driver.connect(DB_PATH)
        try:
            con.executescript(snap.read_text())
            con.commit()
            print(f"rebuild: loaded {snap.relative_to(REPO_ROOT)}")
        finally:
            con.close()
    else:
        print("rebuild: no .sc-state/content.sql — built empty (no per-instance content).")

    # Re-provision api_keys. content.sql never carries api_key (it is a secret and
    # content.sql is git-tracked — see snapshot.py's no-serialize set), so every
    # shell comes back NULL-keyed from the load above. The server backfills NULL
    # keys, but only at startup — a rebuild under an already-running server would
    # otherwise leave the live DB NULL-keyed and 401 every shell's mem write until
    # the API is bounced. Minting here makes a rebuilt DB self-sufficient.
    backfill_shell_api_keys.backfill(str(DB_PATH))

    try:
        map_repo.main()
    except SystemExit as e:
        print(f"rebuild: map skipped ({e}) — run `./sc map` once the repo is ready")
    except Exception as e:  # noqa: BLE001
        print(f"rebuild: map failed ({e}) — run `./sc map`")

    size_kb = DB_PATH.stat().st_size / 1024
    print(f"rebuild: done -> {DB_PATH.relative_to(ENGINE.parent)} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
