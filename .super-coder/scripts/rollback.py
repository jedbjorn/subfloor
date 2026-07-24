#!/usr/bin/env python3
"""Sound rollback of a bad super-coder update — restore the (DB + engine) PAIR.

Engine code is read live every session, and a migration exists *because new code
expects the new schema*. Restoring only the DB would leave new engine code
running against the old schema. So a restore point is a **pair** and rollback
restores both:

    1. back up the CURRENT (post-bad-update) DB first — so rollback is itself
       reversible; you can never lose state by rolling back.
    2. restore the DB from the most recent shell_db.prerebuild.<ts>.db.
    3. re-materialize the engine at .sc-state/engine.ref.prev; restore engine.ref.
    4. clear the -wal/-shm sidecars (the restored .db is a complete snapshot).

Whole-restore, not down-migration (B7 settled this): zero reverse-SQL to author,
never surprisingly lossy, always works. The only data lost is anything written
*between* the bad update and this rollback — a seconds-wide window in practice
("migrated, it broke, rolled back").

Usage:
    ./sc rollback
    ./sc rollback --engine-only  # new-engine / unchanged-old-DB half floor
    python3 .super-coder/scripts/rollback.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
STATE_DIR = REPO_ROOT / ".sc-state"
ENGINE_REF = STATE_DIR / "engine.ref"
ENGINE_REF_PREV = STATE_DIR / "engine.ref.prev"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_backup as db_backup_mod  # noqa: E402
import engine_manifest  # noqa: E402
import rebuild as rebuild_mod  # noqa: E402  (BACKUP_DIR, backup_db, prune_backups, KEEP_BACKUPS)
import update as update_mod    # noqa: E402  (materialize_engine, super_coder_remote, git)

# Compatibility alias for callers that inspect the preferred location. Runtime
# reads/writes use rebuild_mod.backup_dir() so restricted seats share the same
# fallback selection as rebuild and restart.
BACKUP_DIR = rebuild_mod.BACKUP_DIR


def latest_db_restore_point() -> Path | None:
    return db_backup_mod.latest_backup(
        REPO_ROOT, "shell_db.prerebuild.*.db"
    )


def backup_current_db() -> None:
    """Safety copy of the post-bad-update DB under a DISTINCT prefix, so it is
    never mistaken for a pre-update restore point on a later rollback."""
    if not DB_PATH.exists():
        return
    target = rebuild_mod.backup_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = target / f"shell_db.prerollback.{ts}.db"
    rebuild_mod.backup_db(dst)
    rebuild_mod.prune_backups("shell_db.prerollback", target)
    print(f"→ backed up current DB -> {dst}")


def restore_db(src: Path) -> None:
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(src, DB_PATH)
    print(f"→ restored DB from {src.name}")


def restore_engine(prev_sha: str) -> None:
    """Re-materialize the engine at the prior pin. The SHA should already be in
    the object store (it is the ref we updated *from*); fetch as a fallback."""
    previous_files = set(update_mod._engine_files_at(prev_sha))
    current_sha = ENGINE_REF.read_text().strip() if ENGINE_REF.exists() else ""
    current_files = (set(update_mod._engine_files_at(current_sha))
                     if current_sha else set())
    # materialize_engine overlays an archive and deliberately leaves upstream-
    # retired files alone during a forward update. Rollback is different: a
    # target-only migration left behind would make the restored old server
    # demand the new schema again. Remove only files proved upstream-owned by
    # the current pin and absent from the previous pin; fork-local files remain.
    for rel in sorted(current_files - previous_files):
        path = REPO_ROOT / rel
        if path.is_file() or path.is_symlink():
            path.unlink()
    try:
        update_mod.materialize_engine(prev_sha)
    except SystemExit:
        print("  ref not present locally — fetching super-coder, then retrying")
        remote = update_mod.super_coder_remote()
        update_mod.git("fetch", remote)
        update_mod.materialize_engine(prev_sha)
    ENGINE_REF.write_text(prev_sha + "\n")
    # Re-baseline the hash manifest at the restored engine — without this, the
    # next update would read every rolled-back file as a "local edit" and block.
    engine_manifest.write_manifest(update_mod._engine_paths_at(prev_sha),
                                   files=update_mod._engine_files_at(prev_sha))
    print(f"→ engine re-materialized at {prev_sha[:12]} (engine.ref restored)")


def verify_engine_only_floor(prev_sha: str) -> None:
    """Prove the DB is still on the previous engine migration floor."""
    current_sha = ENGINE_REF.read_text().strip() if ENGINE_REF.exists() else ""
    if not current_sha or current_sha == prev_sha:
        sys.exit(
            "rollback: --engine-only is safe only for a newer materialized "
            "engine over an unchanged older DB; the two engine pins do not "
            "prove that state.")
    if not DB_PATH.exists():
        sys.exit("rollback: --engine-only cannot verify a missing DB.")

    previous = {
        Path(line).name
        for line in update_mod.git(
            "ls-tree", "-r", "--name-only", prev_sha, "--",
            ".super-coder/migrations"
        ).stdout.splitlines()
        if line.endswith(".sql")
    }
    current = {
        path.name for path in (ENGINE / "migrations").glob("*.sql")
    }
    con = sqlite3.connect(DB_PATH)
    try:
        table = con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            sys.exit(
                "rollback: --engine-only cannot prove the DB migration floor "
                "(schema_migrations is absent).")
        applied = {
            row[0] for row in con.execute(
                "SELECT filename FROM schema_migrations")
        }
    finally:
        con.close()

    introduced = current - previous
    if not introduced:
        sys.exit(
            "rollback: --engine-only refused — the current engine/DB pair is "
            "not a proved new-engine/old-schema half floor.")
    missing = previous - applied
    unexpected = applied - previous
    if missing or unexpected:
        mismatch = (
            f"missing={sorted(missing)!r}, "
            f"unexpected={sorted(unexpected)!r}"
        )
        sys.exit(
            "rollback: --engine-only refused — the DB does not exactly retain "
            f"the previous engine migration floor ({mismatch}). Use a verified "
            "paired backup or operator-directed recovery instead.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    unknown = [arg for arg in argv if arg != "--engine-only"]
    if unknown:
        sys.exit(f"rollback: unknown argument(s): {' '.join(unknown)}")
    engine_only = "--engine-only" in argv

    if update_mod.EJECTED_MARKER.exists() and not update_mod.is_source_repo():
        sys.exit("rollback: this fork has EJECTED (.sc-state/ejected) — the "
                 "engine is fork source now, so update/rollback no longer apply. "
                 "Use plain git (revert/reset) on the tracked .super-coder/.")
    prev_sha = ENGINE_REF_PREV.read_text().strip() if ENGINE_REF_PREV.exists() else ""
    if engine_only:
        if not prev_sha:
            sys.exit(
                "rollback: --engine-only requires .sc-state/engine.ref.prev; "
                "the previous installed engine cannot be identified.")
        verify_engine_only_floor(prev_sha)
        print("→ repairing a new-engine / unchanged-DB half floor")
        backup_current_db()
        restore_engine(prev_sha)
        ENGINE_REF_PREV.unlink(missing_ok=True)
        print("\nrollback: done — restored the previous engine and preserved "
              "the current DB byte-for-byte.")
        print("  Restart your session to boot onto the restored state.")
        return 0

    src = latest_db_restore_point()
    if src is None:
        sys.exit("rollback: no shell_db.prerebuild.*.db restore point found in "
                 f"{rebuild_mod.backup_dir()} — nothing to roll back to.")

    print("→ rolling back the last update (DB + engine pair-restore)")
    backup_current_db()
    restore_db(src)

    if prev_sha:
        restore_engine(prev_sha)
        ENGINE_REF_PREV.unlink(missing_ok=True)  # consumed — no double-rollback
    else:
        print("⚠ no .sc-state/engine.ref.prev — DB restored, but the engine could "
              "not be reverted (first update post-B7, or prev pin already consumed).")
        print("  If a schema mismatch surfaces, re-`./sc update` to the matching "
              "engine, or set engine.ref by hand.")

    print("\nrollback: done — restored to the pre-update floor.")
    print("  Restart your session to boot onto the restored state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
