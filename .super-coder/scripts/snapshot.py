#!/usr/bin/env python3
"""Serialize this fork's per-instance content + memory to text.

Dumps the per-instance tables of the live `shell_db.db` to
`.sc-state/content.sql` as a deterministic, idempotent SQL script:
each table is `DELETE`d then re-`INSERT`ed in primary-key order, so re-running
produces a byte-identical file (clean git diffs) and loading it is repeatable.

This is the *per-instance* serialization — it rebuilds THIS repo's content and
stays local. It never propagates to forks (that is migrations' job). Engine
skills are seeded from assets/ via migrations. Project-local skills are dumped
here so a fork can author its own skills without upstreaming them.

FK checks are off during load (SQLite default), so dump order is for readability
only, not correctness.

Usage:
    python3 .super-coder/scripts/snapshot.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import map_db  # noqa: E402 — sibling module in scripts/

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
# Per-fork memory lives OUTSIDE the gitignored engine dir (B7) — see rebuild.py.
OUT_PATH = REPO_ROOT / ".sc-state" / "content.sql"
# One-release cleanup: if a not-yet-migrated fork still carries the old in-engine
# copy, remove it once we write the new one so it can't shadow or drift.
LEGACY_PATH = ENGINE / "snapshot" / "content.sql"

# Per-instance tables, parents-before-children for readability.
# `schema_migrations` is excluded. Engine-authored skills are system content
# seeded from migrations; project-local skills are serialized by the special
# `skills` dumper below. `shell_skills` loads after `skills`, so grants to
# local skill names resolve on rebuild.
PER_INSTANCE_TABLES = [
    "users",
    "shells",
    "shell_identity_entries",
    "shell_decisions",
    "shell_memory_archives",
    "roadmap",
    "documents",
    "flags",
    "spec_tasks",
    # feature_blockers is per-instance roadmap content (the blocking edges
    # between this fork's features), like roadmap/flags. Loads after `roadmap`
    # (both its FK targets), so the edges resolve on rebuild.
    "feature_blockers",
    "projects",
    "project_shells",
    "skills",
    "shell_skills",
    # shell_messages is per-instance memory (the inbox between this fork's
    # shells), so it survives a rebuild like flags/decisions — not a derived
    # cache. Loads after `shells` (its FK target). read_at is preserved, so an
    # unread message stays unread across a rebuild.
    "shell_messages",
    # NOTE: dr_section is authored navigation but lives in the MAP DB now
    # (.sc-state/map.db), not shell_db.db — it is serialized separately to
    # .sc-state/map_content.sql by snapshot_map() below, not here.
]


def quote(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def engine_skill_names() -> list[str]:
    """Names authored by the engine seed assets.

    Any live skill whose name is not in this set is project-local and belongs in
    `.sc-state/content.sql`. This keeps local skills durable while engine
    updates can still UPSERT their own catalogue rows.
    """
    skills_dir = ENGINE / "assets" / "skills"
    names: list[str] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        try:
            _, fm, _ = text.split("---", 2)
        except ValueError:
            continue
        for line in fm.strip().splitlines():
            if line.startswith("name:"):
                names.append(line.split(":", 1)[1].strip())
                break
    return names


def dump_shell_skills(con: sqlite3.Connection) -> list[str]:
    """Grants resolved by skill NAME, not raw skill_id. Skill ids are positional
    (they shift when the catalogue grows), so a raw-id dump would bind a fork's
    grants to the wrong skills after an update. Resolving by name at load time
    makes grants id-churn-proof."""
    rows = con.execute(
        "SELECT ss.shell_id, s.name FROM shell_skills ss "
        "JOIN skills s ON s.skill_id = ss.skill_id ORDER BY ss.shell_id, s.name"
    ).fetchall()
    lines = ["DELETE FROM shell_skills;"]
    for shell_id, name in rows:
        lines.append(
            f"INSERT INTO shell_skills (shell_id, skill_id) "
            f"SELECT {shell_id}, skill_id FROM skills WHERE name={quote(name)};")
    lines.append("")
    return lines


def dump_local_skills(con: sqlite3.Connection) -> list[str]:
    """Serialize project-local skills only, keyed by name.

    The engine seed owns rows whose names exist under assets/skills. Everything
    else is fork-local content and must survive rebuild/update from snapshot.
    """
    engine_names = engine_skill_names()
    if engine_names:
        placeholders = ", ".join("?" for _ in engine_names)
        where = f"name NOT IN ({placeholders})"
        params = engine_names
        delete_line = (
            "DELETE FROM skills WHERE name NOT IN ("
            + ", ".join(quote(n) for n in engine_names)
            + ");"
        )
    else:
        where = "1=1"
        params = []
        delete_line = "DELETE FROM skills;"

    cols = [r[1] for r in con.execute("PRAGMA table_info(skills)")]
    collist = ", ".join(cols)
    mutable_cols = [c for c in cols if c != "skill_id"]
    insert_cols = ", ".join(mutable_cols)
    update_cols = [c for c in mutable_cols if c != "name"]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    rows = con.execute(
        f"SELECT {insert_cols} FROM skills WHERE {where} ORDER BY name", params
    ).fetchall()

    lines = [
        "-- Project-local skills only. Engine-seeded skills come from migrations.",
        delete_line,
    ]
    for row in rows:
        vals = ", ".join(quote(v) for v in row)
        lines.append(
            f"INSERT INTO skills ({insert_cols}) VALUES ({vals}) "
            f"ON CONFLICT(name) DO UPDATE SET {update_clause};"
        )
    lines.append("")
    return lines


def dump_table(con: sqlite3.Connection, table: str) -> list[str]:
    if table == "skills":
        return dump_local_skills(con)
    if table == "shell_skills":
        return dump_shell_skills(con)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if not cols:
        return []
    collist = ", ".join(cols)
    rows = con.execute(f"SELECT {collist} FROM {table} ORDER BY rowid").fetchall()
    lines = [f"DELETE FROM {table};"]
    for row in rows:
        vals = ", ".join(quote(v) for v in row)
        lines.append(f"INSERT INTO {table} ({collist}) VALUES ({vals});")
    lines.append("")
    return lines


def snapshot_map() -> None:
    """Serialize the map's AUTHORED layer (dr_section) to .sc-state/map_content.sql.

    The map DB (.sc-state/map.db) is a derived cache — its files/deps/env are
    re-mapped, not snapshotted. Only the cartographer-curated sections must
    survive a fresh map DB, so this is the map's equivalent of content.sql,
    reloaded by map_db.seed_authored(). Skipped if the map DB has no sections
    yet (a fork that hasn't mapped/curated) so we never write an empty file."""
    if not map_db.MAP_DB_PATH.exists():
        return
    con = sqlite3.connect(map_db.MAP_DB_PATH)
    try:
        if not table_exists(con, "dr_section"):
            return
        if not con.execute("SELECT COUNT(*) FROM dr_section").fetchone()[0]:
            return
        out = [
            "-- super-coder MAP authored layer — GENERATED by scripts/snapshot.py.",
            "-- The cartographer-curated sections of the map DB (.sc-state/map.db).",
            "-- Idempotent; reloaded into a fresh map DB by map_db.seed_authored().",
            "-- The rest of the map (files/deps/env) is a derived cache — re-mapped,",
            "-- not snapshotted. Do not hand-edit — curate via the shell, then snapshot.",
            "",
            "BEGIN;",
            "",
            *dump_table(con, "dr_section"),
            "COMMIT;",
        ]
        map_db.MAP_CONTENT.parent.mkdir(parents=True, exist_ok=True)
        map_db.MAP_CONTENT.write_text("\n".join(out) + "\n")
        print(f"snapshot: wrote {map_db.MAP_CONTENT.relative_to(REPO_ROOT)}")
    finally:
        con.close()


def main() -> int:
    if not DB_PATH.exists():
        raise SystemExit(f"snapshot: no live DB at {DB_PATH} — run `./sc rebuild` first.")
    con = sqlite3.connect(DB_PATH)
    try:
        out = [
            "-- super-coder per-instance snapshot — GENERATED by scripts/snapshot.py.",
            "-- Idempotent: DELETE-then-INSERT per table, PK order. Loaded by rebuild.py.",
            "-- This file rebuilds THIS repo's content + memory; it stays local (never",
            "-- propagates to forks). Do not hand-edit — author via the shell or GUI, then",
            "-- `./sc snapshot`.",
            "",
            "BEGIN;",
            "",
        ]
        for table in PER_INSTANCE_TABLES:
            if table_exists(con, table):
                out.extend(dump_table(con, table))
        out.append("COMMIT;")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text("\n".join(out) + "\n")
    finally:
        con.close()
    # Relocate-on-write: drop a stale legacy copy so the new .sc-state/ path is
    # the single source after the first snapshot post-B7.
    if LEGACY_PATH.exists():
        LEGACY_PATH.unlink()
        try:
            LEGACY_PATH.parent.rmdir()  # remove empty snapshot/ dir
        except OSError:
            pass
        print(f"snapshot: removed legacy {LEGACY_PATH.relative_to(REPO_ROOT)}")
    print(f"snapshot: wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    snapshot_map()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
