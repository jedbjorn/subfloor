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
# PER-FORK backup dir, keyed by the host repo's dir name. This was a fixed
# "super-coder" for every fork, which pooled all forks' pre-update dumps in one
# dir — and rollback restores the MOST RECENT dump, so a multi-fork update
# sweep could roll one fork back onto ANOTHER FORK'S DB. In the source repo the
# name IS super-coder, so its path is unchanged. Old pooled dumps stay where
# they are: they cannot be attributed to a fork after the fact.
BACKUP_DIR = Path.home() / "db_backups" / REPO_ROOT.name

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver    # noqa: E402
import migrate as migrate_mod  # noqa: E402
import map_repo     # noqa: E402
import backfill_shell_api_keys  # noqa: E402  (re-provision api_keys post-rebuild)


def snapshot_path() -> Path:
    return SNAPSHOT if SNAPSHOT.exists() else SNAPSHOT_LEGACY


def read_existing_keys() -> dict:
    """shell_id -> (api_key, api_key_rotated_at) from the outgoing DB.

    content.sql never serializes api_key (secret in a git-tracked file), so
    without a carry-over every rebuild re-minted all keys — orphaning the
    SC_API_TOKEN run.py injected into each live session at boot and 401-ing
    every mem call engine-wide until the sessions re-entered (#265). Read the
    keys before the old DB is deleted; restore_keys() puts them back after the
    content load. A missing DB or a pre-0027 one (no api_key column) yields
    empty — every shell gets minted fresh, same as a first build."""
    if not DB_PATH.exists():
        return {}
    con = db_driver.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT shell_id, api_key, api_key_rotated_at FROM shells "
            "WHERE api_key IS NOT NULL"
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    except db_driver.OperationalError:
        return {}
    finally:
        con.close()


def restore_keys(keys: dict) -> int:
    """Re-attach carried keys to the rebuilt shells. Guarded on api_key IS
    NULL so a loaded value (should content.sql ever carry one) is never
    clobbered; a shell_id absent from the new content matches nothing and its
    key is dropped with it."""
    if not keys:
        return 0
    con = db_driver.connect(DB_PATH)
    try:
        restored = 0
        for sid, (key, rotated) in keys.items():
            cur = con.execute(
                "UPDATE shells SET api_key=?, api_key_rotated_at=? "
                "WHERE shell_id=? AND api_key IS NULL",
                (key, rotated, sid),
            )
            restored += cur.rowcount
        con.commit()
        return restored
    finally:
        con.close()


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
    keys = read_existing_keys()
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

    # Re-attach the keys carried over from the outgoing DB (content.sql never
    # carries api_key — it is a secret and content.sql is git-tracked, see
    # snapshot.py's no-serialize set), then mint only what is still missing:
    # new shells from the content load, or everything on a fresh/pre-key build.
    # Rotation is never a rebuild side effect — live sessions keep the
    # SC_API_TOKEN they booted with (#265). Rotate deliberately, not here.
    restored = restore_keys(keys)
    if restored:
        print(f"rebuild: preserved {restored} shell api_key(s)")
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
