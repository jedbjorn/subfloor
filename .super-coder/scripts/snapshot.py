#!/usr/bin/env python3
"""Serialize this fork's per-instance content + memory to text.

Dumps the per-instance tables of the live `shell_db.db` to the gitignored local
content path as a deterministic, idempotent SQL script:
each table is `DELETE`d then re-`INSERT`ed in primary-key order, so re-running
produces a byte-identical file (clean git diffs) and loading it is repeatable.

This is the *per-instance* serialization — it rebuilds THIS repo's content and
stays local. It never propagates to forks (that is migrations' job). Engine
skills are seeded from assets/ via migrations. Project-local skills are dumped
here so a fork can author its own skills without upstreaming them.

The snapshot wraps its body in PRAGMA foreign_keys=OFF/ON (outside the
transaction — SQLite ignores the pragma inside BEGIN/COMMIT) so tables can
be dumped in readability order rather than strict FK dependency order.
Needed because db_driver.connect() sets PRAGMA foreign_keys=ON on the
connection that rebuild.py uses to load the snapshot.

Usage:
    python3 .super-coder/scripts/snapshot.py
"""
from __future__ import annotations

import sqlite3  # kept for map.db (which stays SQLite)
from pathlib import Path

import artifact_policy
import db_driver
import map_db
import seed_skills
from _serialize_guard import require_admin

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
# Generated instance state is always local and gitignored.
OUT_PATH = artifact_policy.content_path()
# One-release cleanup: if a not-yet-migrated fork still carries the old in-engine
# copy, remove it once we write the new one so it can't shadow or drift.
LEGACY_PATH = ENGINE / "snapshot" / "content.sql"

# Every durable Sprints v2 table.  Keep this as the one snapshot authority for
# the domain: tests compare it to the migrated schema so a future sprint_*
# table cannot silently fall out of rebuilds.  Generic conversations are
# listed before this group in PER_INSTANCE_TABLES because participant links,
# pointers, and wake attempts reference them.
SPRINT_INSTANCE_TABLES = [
    "sprint_spec_approvals",
    "sprints",
    "sprint_specs",
    "sprint_participants",
    "sprint_participant_conversations",
    "sprint_cleanup_targets",
    "sprint_cleanup_requests",
    "sprint_work_units",
    "sprint_work_unit_tasks",
    "sprint_work_unit_dependencies",
    "wake_message",
    "sprint_wake_outbox",
    "sprint_wake_messages",
    "sprint_wake_attempts",
    "sprint_liveness_expectations",
    "sprint_registered_prs",
    "pr_subscriptions",
    "sprint_pr_work_units",
    "sprint_pr_transitions",
    "pr_subscription_transitions",
    "pr_subscription_poll_failures",
    "sprint_judgments",
    "sprint_reports",
    "sprint_followups",
    "sprint_events",
    "sprint_wake_recovery_messages",
]


# Per-instance tables, parents-before-children for readability.
# `schema_migrations` is excluded. Engine-authored skills are system content
# seeded from migrations; project-local skills are serialized by the special
# `skills` dumper below. Grant tables load after `skills`, so grants to local
# skill names resolve on rebuild.
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
    "flavor_skills",
    "shell_skills",
    # shell_messages is per-instance memory (the inbox between this fork's
    # shells), so it survives a rebuild like flags/decisions — not a derived
    # cache. Loads after `shells` (its FK target). read_at is preserved, so an
    # unread message stays unread across a rebuild.
    "shell_messages",
    # flavor_defaults is operator-tuned launch config (the Default Models GUI:
    # model per harness + starred default harness, per flavor). Migrations seed
    # the engine's baseline; content.sql loads AFTER migrations on rebuild, so
    # the fork's edits win — without this the GUI's changes vanish on rebuild.
    "flavor_defaults",
    # Browser-native conversations are durable instance truth: exact harness
    # refs, message queues, recovery evidence, replay events, and pending
    # outbox work must all survive update/rebuild. Parents precede
    # children so a snapshot stays readable and foreign-key-valid when loaded.
    "conversations",
    "active_shell_chats",
    "conversation_git_targets",
    "conversation_messages",
    "conversation_runs",
    "conversation_events",
    "conversation_outbox",
    # Sprints are durable orchestration truth, not a derived runtime cache.
    # This includes terminal rows and append-only evidence: numeric allocators,
    # exact approvals, active routes, retries, reports, and history must all
    # survive update/rebuild together.
    *SPRINT_INSTANCE_TABLES,
    # NOTE: dr_section is authored navigation but lives in the MAP DB now
    # (.sc-state/map.db), not shell_db.db — it is serialized separately to
    # .sc-state/local/map/content.sql by snapshot_map() below, not here.
]

SNAPSHOT_ROW_FILTERS = {}


def quote(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def engine_skill_names() -> list[str]:
    """Names the ENGINE SEED owns (seed_skills.seeded_skill_names — i.e.
    migrations/0001, upstream-materialized in a fork).

    Any live skill whose name is not in this set is project-local and belongs in
    local `content.sql`. Keyed off the seed, NOT asset-file presence (#253):
    a fork-authored skill keeps its SKILL.md under assets/skills/ as authoring
    source, and classifying by asset presence would silently drop it from
    content.sql — losing it on the next update's materialize.
    """
    return seed_skills.seeded_skill_names()


def dump_shell_skills(con) -> list[str]:
    """Bespoke grants resolved by skill NAME, not raw skill_id."""
    tombstones = seed_skills.tombstoned_skill_names()
    placeholders = ",".join("?" for _ in tombstones)
    rows = con.execute(
        "SELECT ss.shell_id, s.name FROM shell_skills ss "
        "JOIN skills s ON s.skill_id = ss.skill_id "
        "JOIN shells sh ON sh.shell_id = ss.shell_id "
        f"WHERE sh.flavor IS NULL AND s.name NOT IN ({placeholders}) "
        "ORDER BY ss.shell_id, s.name",
        tombstones,
    ).fetchall()
    lines = ["DELETE FROM shell_skills;"]
    for shell_id, name in rows:
        lines.append(
            f"INSERT INTO shell_skills (shell_id, skill_id) "
            f"SELECT {shell_id}, skill_id FROM skills WHERE name={quote(name)};")
    lines.append("")
    return lines


def dump_flavor_skills(con) -> list[str]:
    """Flavor packs resolved by skill NAME so catalogue id churn is harmless."""
    tombstones = seed_skills.tombstoned_skill_names()
    placeholders = ",".join("?" for _ in tombstones)
    rows = con.execute(
        "SELECT fs.flavor, s.name FROM flavor_skills fs "
        "JOIN skills s ON s.skill_id = fs.skill_id "
        f"WHERE s.name NOT IN ({placeholders}) ORDER BY fs.flavor, s.name",
        tombstones,
    ).fetchall()
    lines = ["DELETE FROM flavor_skills;"]
    for flavor, name in rows:
        lines.append(
            f"INSERT INTO flavor_skills (flavor, skill_id) "
            f"SELECT {quote(flavor)}, skill_id FROM skills WHERE name={quote(name)};")
    lines.append("")
    return lines


def dump_local_skills(con) -> list[str]:
    """Serialize project-local skills only, keyed by name.

    The engine seed owns active rows; the tombstone registry owns retired names.
    Everything outside both sets is fork-local content and must survive
    rebuild/update from snapshot.
    """
    engine_names = engine_skill_names()
    excluded_names = sorted(
        set(engine_names) | set(seed_skills.tombstoned_skill_names())
    )
    if engine_names:
        delete_line = (
            "DELETE FROM skills WHERE name NOT IN ("
            + ", ".join(quote(n) for n in engine_names)
            + ");"
        )
    else:
        delete_line = "DELETE FROM skills;"

    cols = [r[1] for r in con.execute("PRAGMA table_info(skills)")]
    mutable_cols = [c for c in cols if c != "skill_id"]
    insert_cols = ", ".join(mutable_cols)
    update_cols = [c for c in mutable_cols if c != "name"]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    excluded_placeholders = ", ".join("?" for _ in excluded_names)
    local_where = (
        f"name NOT IN ({excluded_placeholders})" if excluded_names else "1=1"
    )
    rows = con.execute(
        f"SELECT {insert_cols} FROM skills WHERE {local_where} ORDER BY name",
        excluded_names,
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


# Columns that must NEVER be serialized to content.sql — these are live
# credentials managed at runtime, not memory to
# preserve across a rebuild. `api_key` is (re)provisioned at rebuild time
# (rebuild.py's final backfill step) and again at server startup; `password_*`
# are launcher auth fields. Omitting them from the INSERT means they load as NULL
# on rebuild, which is correct: the key is re-minted by rebuild itself (so a
# rebuilt DB is never NULL-keyed, even under an already-running server) and they
# never enter the portable rebuild snapshot. This defense remains necessary
# even though the snapshot itself is ignored.
SENSITIVE_COLUMNS = {
    "shells": {"api_key", "api_key_rotated_at"},
    "users": {"password_hash", "password_salt"},
}


def _table_columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _insert_line(table: str, cols: list[str], row) -> str:
    vals = ", ".join(quote(v) for v in row)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({vals});"


def _dependency_ordered_rows(
    rows,
    cols: list[str],
    *,
    table: str,
    identity: str,
    parent: str,
    scope: tuple[str, ...],
):
    """Order immutable self-references parent-first for trigger-safe replay."""
    identity_index = cols.index(identity)
    parent_index = cols.index(parent)
    scope_indexes = tuple(cols.index(column) for column in scope)

    def row_key(row) -> tuple:
        return tuple(row[index] for index in scope_indexes) + (row[identity_index],)

    by_key = {row_key(row): row for row in rows}
    if len(by_key) != len(rows):
        raise RuntimeError(f"snapshot: duplicate dependency identity in {table}")

    keys = [row_key(row) for row in rows]
    children: dict[tuple, list[tuple]] = {key: [] for key in keys}
    indegree = {key: 0 for key in keys}
    for key, row in zip(keys, rows):
        parent_identity = row[parent_index]
        if parent_identity is not None:
            parent_key = tuple(row[index] for index in scope_indexes) + (
                parent_identity,
            )
            if parent_key not in by_key:
                raise RuntimeError(
                    f"snapshot: missing dependency in {table}: {parent_key!r}"
                )
            children[parent_key].append(key)
            indegree[key] += 1

    ready = [key for key in keys if indegree[key] == 0]
    ordered = []
    cursor = 0
    while cursor < len(ready):
        key = ready[cursor]
        cursor += 1
        ordered.append(by_key[key])
        for child_key in children[key]:
            indegree[child_key] -= 1
            if indegree[child_key] == 0:
                ready.append(child_key)
    if len(ordered) != len(rows):
        raise RuntimeError(f"snapshot: dependency cycle in {table}")
    return ordered


def dump_dependency_ordered_table(
    con,
    table: str,
    *,
    identity: str,
    parent: str,
    scope: tuple[str, ...],
) -> list[str]:
    """Dump a self-referential table in legal immutable-insert order."""
    cols = _table_columns(con, table)
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"
    ).fetchall()
    ordered = _dependency_ordered_rows(
        rows,
        cols,
        table=table,
        identity=identity,
        parent=parent,
        scope=scope,
    )
    lines = [f"DELETE FROM {table};"]
    lines.extend(_insert_line(table, cols, row) for row in ordered)
    lines.append("")
    return lines


def dump_sprints(con) -> list[str]:
    """Serialize lifecycle rows through their legal transition path.

    The schema correctly refuses inserting an armed/terminal Sprint directly.
    A rebuild therefore inserts the exact row as prepared (with no terminal
    outcome), then replays only the lifecycle edges needed to restore its
    projection.  All timestamps, generation identity, version, and plan fields
    are inserted at their original values and remain byte-for-byte unchanged.
    """
    table = "sprints"
    cols = _table_columns(con, table)
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"
    ).fetchall()
    lifecycle_index = cols.index("lifecycle")
    outcome_index = cols.index("terminal_outcome")
    lines = [f"DELETE FROM {table};"]
    restores: list[tuple[str, list[str]]] = []
    for original in rows:
        row = list(original)
        lifecycle = str(row[lifecycle_index])
        outcome = row[outcome_index]
        sprint_id = row[cols.index("sprint_id")]
        if lifecycle != "prepared":
            row[lifecycle_index] = "prepared"
            row[outcome_index] = None
        lines.append(_insert_line(table, cols, row))
        transitions: list[str] = []
        if lifecycle in {"armed", "paused", "completed"}:
            transitions.append(
                f"UPDATE sprints SET lifecycle='armed' WHERE sprint_id={quote(sprint_id)};"
            )
        if lifecycle == "paused":
            transitions.append(
                f"UPDATE sprints SET lifecycle='paused' WHERE sprint_id={quote(sprint_id)};"
            )
        elif lifecycle == "completed":
            transitions.append(
                "UPDATE sprints SET lifecycle='completed', terminal_outcome="
                f"{quote(outcome)} WHERE sprint_id={quote(sprint_id)};"
            )
        elif lifecycle == "aborted":
            transitions.append(
                "UPDATE sprints SET lifecycle='aborted', terminal_outcome="
                f"{quote(outcome)} WHERE sprint_id={quote(sprint_id)};"
            )
        restores.append((lifecycle, transitions))
    # The unique partial index permits only one armed Sprint.  Restore every
    # row that finishes non-armed first, then the current armed row.  This is
    # independent of creation order (an older paused Sprint can be resumed
    # after a newer Sprint is paused).
    for lifecycle, transitions in restores:
        if lifecycle != "armed":
            lines.extend(transitions)
    for lifecycle, transitions in restores:
        if lifecycle == "armed":
            lines.extend(transitions)
    lines.append("")
    return lines


def dump_plain_table(con, table: str) -> list[str]:
    cols = _table_columns(con, table)
    cols = [c for c in cols if c not in SENSITIVE_COLUMNS.get(table, ())]
    if not cols:
        return []
    collist = ", ".join(cols)
    where = SNAPSHOT_ROW_FILTERS.get(table, "")
    rows = con.execute(
        f"SELECT {collist} FROM {table} {where} ORDER BY rowid"
    ).fetchall()
    lines = [f"DELETE FROM {table};"]
    for row in rows:
        lines.append(_insert_line(table, cols, row))
    lines.append("")
    return lines


def dump_table(con, table: str) -> list[str]:
    if table == "skills":
        return dump_local_skills(con)
    if table == "flavor_skills":
        return dump_flavor_skills(con)
    if table == "shell_skills":
        return dump_shell_skills(con)
    if table == "conversation_messages":
        return dump_dependency_ordered_table(
            con,
            table,
            identity="message_id",
            parent="caused_by_message_id",
            scope=("conversation_id",),
        )
    if table == "sprints":
        return dump_sprints(con)
    return dump_plain_table(con, table)


def snapshot_map() -> None:
    """Serialize the map's authored layer under the active artifact policy.

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
        artifact_policy.atomic_write_text(map_db.MAP_CONTENT, "\n".join(out) + "\n")
        print(f"snapshot: wrote {map_db.MAP_CONTENT.relative_to(REPO_ROOT)}")
    finally:
        con.close()


def serialize_instance(con) -> str:
    """Render one coherent read view of every per-instance table."""
    con.execute("BEGIN")
    try:
        out = [
            "-- super-coder per-instance snapshot — GENERATED by scripts/snapshot.py.",
            "-- Idempotent: DELETE-then-INSERT per table, PK order. Loaded by rebuild.py.",
            "-- This file rebuilds THIS repo's content + memory; it stays local (never",
            "-- propagates to forks). Do not hand-edit — author via the shell or GUI, then",
            "-- `./sc snapshot`.",
            "",
            "PRAGMA foreign_keys=OFF;",
            "BEGIN;",
            "",
        ]
        for table in PER_INSTANCE_TABLES:
            if table_exists(con, table):
                out.extend(dump_table(con, table))
        out.extend(["COMMIT;", "PRAGMA foreign_keys=ON;"])
        return "\n".join(out) + "\n"
    finally:
        # End the fixed read view without taking ownership of any live writes.
        con.rollback()


def persist_instance(con) -> Path:
    """Persist one coherent instance snapshot without changing DB state.

    Callers that already own an authorized DB mutation use this after commit.
    The standalone CLI keeps its Admin guard in ``main``; persistence itself is
    deliberately reusable by narrower first-class mutation surfaces.
    """
    artifact_policy.prepare_local_state()
    content = serialize_instance(con)
    artifact_policy.atomic_write_text(OUT_PATH, content)
    return OUT_PATH


def main() -> int:
    require_admin("snapshot")
    copied = artifact_policy.prepare_local_state()
    if copied:
        print(f"snapshot: localized {len(copied)} existing artifact(s)")
    if not DB_PATH.exists():
        raise SystemExit(f"snapshot: no live DB at {DB_PATH} — run `./sc rebuild` first.")
    con = db_driver.connect(DB_PATH)
    try:
        reconciled = seed_skills.reconcile_tombstoned_skills(con)
        persist_instance(con)
    finally:
        con.close()
    if reconciled.changed_names:
        print(
            "snapshot: removed tombstoned skills "
            f"({', '.join(reconciled.changed_names)}; "
            f"{reconciled.grant_count} grant(s))"
        )
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
    from cli_entry import run_cli

    raise SystemExit(run_cli(main))
