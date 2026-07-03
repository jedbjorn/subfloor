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
    python3 .super-coder/scripts/rollback.py
"""
from __future__ import annotations

import shutil
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
import engine_manifest  # noqa: E402
import rebuild as rebuild_mod  # noqa: E402  (BACKUP_DIR, prune_backups, KEEP_BACKUPS)
import update as update_mod    # noqa: E402  (materialize_engine, super_coder_remote, git)

# One source of truth with rebuild.py — the PER-FORK dir. A restore point must
# come from THIS fork's backups: a private copy of the path is exactly how the
# pooled-dir hazard happened (rollback restoring another fork's dump).
BACKUP_DIR = rebuild_mod.BACKUP_DIR


def latest_db_restore_point() -> Path | None:
    backups = sorted(BACKUP_DIR.glob("shell_db.prerebuild.*.db"))
    return backups[-1] if backups else None


def backup_current_db() -> None:
    """Safety copy of the post-bad-update DB under a DISTINCT prefix, so it is
    never mistaken for a pre-update restore point on a later rollback."""
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"shell_db.prerollback.{ts}.db"
    shutil.copy2(DB_PATH, dst)
    rebuild_mod.prune_backups("shell_db.prerollback")
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


def main() -> int:
    if update_mod.EJECTED_MARKER.exists() and not update_mod.is_source_repo():
        sys.exit("rollback: this fork has EJECTED (.sc-state/ejected) — the "
                 "engine is fork source now, so update/rollback no longer apply. "
                 "Use plain git (revert/reset) on the tracked .super-coder/.")
    src = latest_db_restore_point()
    if src is None:
        sys.exit("rollback: no shell_db.prerebuild.*.db restore point found in "
                 f"{BACKUP_DIR} — nothing to roll back to.")
    prev_sha = ENGINE_REF_PREV.read_text().strip() if ENGINE_REF_PREV.exists() else ""

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
