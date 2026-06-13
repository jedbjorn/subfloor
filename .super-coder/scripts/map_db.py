#!/usr/bin/env python3
"""Resolve, open, and seed the map DB — the repo catalogue (dr_*).

The map is a derived cache of the host repo, owned by the cartographer. It lives
in its OWN sqlite file (`.sc-state/map.db`), separate from the engine memory DB
(`shell_db.db`), so an engine schema migration or a memory rebuild never touches
it. This module is the single place that knows where the map DB is and how to
bring a fresh one up to a usable state.

Layers:
- DERIVED  (dr_repo / dr_filepath / dr_dependency / dr_env) — repopulated by
  map_repo on every map; nothing to persist.
- AUTHORED (dr_section, and dr_filepath.desc) — cartographer-curated. dr_section
  is serialized to `.sc-state/map_content.sql` (tracked) by snapshot.py and
  reloaded here when a fresh map DB has no sections yet.

Transition shim: a fork created before the split has its authored dr_section in
`shell_db.db`. Until it re-snapshots (which moves that to map_content.sql), a
fresh map DB with no map_content.sql falls back to copying dr_section out of the
old engine DB. Harmless once map_content.sql exists.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
MAP_DB_PATH = REPO_ROOT / ".sc-state" / "map.db"
MAP_SCHEMA = ENGINE / "map_schema.sql"
MAP_CONTENT = REPO_ROOT / ".sc-state" / "map_content.sql"
ENGINE_DB = ENGINE / "shell_db.db"  # legacy source of pre-split dr_section


def ensure_schema(con: sqlite3.Connection) -> None:
    """Apply map_schema.sql to the map DB (idempotent — CREATE IF NOT EXISTS).

    Fail loudly if the schema file is absent: it ships with the engine, so a
    missing one means an incomplete materialize (e.g. a fork updated by an engine
    that predates map_schema.sql being in ENGINE_PATHS). Better a clear error here
    than a cryptic 'no such table: dr_section' downstream in seed_authored."""
    if not MAP_SCHEMA.exists():
        raise SystemExit(
            f"map_db: {MAP_SCHEMA} missing — engine materialize is incomplete. "
            "Re-run `./sc update` with an engine that materializes map_schema.sql.")
    con.executescript(MAP_SCHEMA.read_text())
    con.commit()


def seed_authored(con: sqlite3.Connection) -> None:
    """Populate the authored layer of a fresh map DB. Only acts when dr_section is
    empty, so a curated set already in the live map DB is never overwritten.

    Source of truth, in order: the tracked snapshot (`map_content.sql`); else the
    legacy pre-split engine DB (one-time transition). A genuinely fresh repo with
    neither falls through to map_repo's dir-seeding."""
    if con.execute("SELECT COUNT(*) FROM dr_section").fetchone()[0]:
        return
    if MAP_CONTENT.exists():
        con.executescript(MAP_CONTENT.read_text())
        con.commit()
        return
    # Transition: lift dr_section out of the pre-split engine DB if it still has it.
    if not ENGINE_DB.exists():
        return
    try:
        eng = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return
    try:
        has = eng.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dr_section'"
        ).fetchone()
        rows = eng.execute(
            "SELECT name, path_prefix, description, sort_order FROM dr_section"
        ).fetchall() if has else []
    finally:
        eng.close()
    for r in rows:
        con.execute(
            "INSERT OR IGNORE INTO dr_section (name, path_prefix, description, sort_order) "
            "VALUES (?, ?, ?, ?)", r)
    if rows:
        con.commit()


def connect(*, seed: bool = True) -> sqlite3.Connection:
    """Open the map DB (creating + schema-applying if absent), row_factory set.
    When `seed`, also load the authored layer into a fresh DB."""
    MAP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(MAP_DB_PATH)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    if seed:
        seed_authored(con)
    return con


def open_ro() -> "sqlite3.Connection | None":
    """Open the map DB read-only for rendering. None if it doesn't exist yet
    (a fork that hasn't mapped) — callers degrade to 'not mapped'."""
    if not MAP_DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{MAP_DB_PATH}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError:
        return None
