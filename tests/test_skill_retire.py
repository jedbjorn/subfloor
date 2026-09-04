#!/usr/bin/env python3
"""Tests for the fork skill retire list (seed_skills.apply_retired + `./sc
skill retire|unretire`, #238).

The engine seed resurrects every engine skill (is_deleted=0) on each
update/sync/rebuild, so a fork could not durably take a superseded engine
skill out of service. The retire list (`.sc-state/skills_retired.json`,
tracked, fork-owned) must: flip listed engine names to is_deleted=1 and
unlisted ones back to 0 (converge, both directions), survive a full-seed
re-run, never touch local skills or grant rows, and fail loud on a malformed
file rather than silently resurrecting a retired skill.

Run:
    python3 tests/test_skill_retire.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
import seed_skills
import skill as skill_cli
import render_check
import server

ENGINE_SKILLS = ("redline_review", "review", "git")

SEED_SQL = "\n".join(
    f"INSERT INTO skills (name, description, common, content, is_deleted) "
    f"VALUES ('{n}', 'engine skill', 1, 'body', 0) "
    f"ON CONFLICT(name) DO UPDATE SET is_deleted=0;"
    for n in ENGINE_SKILLS)


def make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, description TEXT, category TEXT, "
        "content TEXT, command TEXT, common INTEGER NOT NULL DEFAULT 1, "
        "is_deleted INTEGER NOT NULL DEFAULT 0)")
    con.execute(
        "CREATE TABLE shell_skills (shell_skill_id INTEGER PRIMARY KEY, "
        "shell_id INTEGER, skill_id INTEGER, UNIQUE(shell_id, skill_id))")
    con.execute(
        "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
        "UNIQUE(flavor, skill_id))")
    con.execute(
        "CREATE TABLE shells (shell_id INTEGER PRIMARY KEY, flavor TEXT, "
        "shortname TEXT, display_name TEXT, is_deleted INTEGER DEFAULT 0)")
    con.execute(
        "INSERT INTO shells (shell_id, flavor, shortname, display_name) "
        "VALUES (1, NULL, 'BSP1', 'Bespoke')")
    con.executescript(SEED_SQL)
    con.execute("INSERT INTO skills (name, description, content) "
                "VALUES ('test_authoring_dosarch', 'fork skill', 'body')")
    # one grant on the skill we retire, to prove grants stay put
    con.execute("INSERT INTO shell_skills (shell_id, skill_id) "
                "SELECT 1, skill_id FROM skills WHERE name='redline_review'")
    con.commit()
    return con


class RetireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        seed = root / "0001_seed_skills.sql"
        seed.write_text(SEED_SQL)
        self._orig = (seed_skills.RETIRED_FILE, seed_skills.OUT)
        seed_skills.RETIRED_FILE = root / ".sc-state" / "skills_retired.json"
        seed_skills.OUT = seed
        self.projection = mock.patch.object(
            skill_cli.skill_projection,
            "reconcile_existing_checkouts",
            return_value={
                "written": [], "skipped": [], "deleted": [], "checkouts": []
            },
        )
        self.reconcile_existing = self.projection.start()
        self.target_projection = mock.patch.object(
            skill_cli.skill_projection,
            "reconcile_assignment_targets",
            return_value={
                "written": [], "skipped": [], "deleted": [], "checkouts": []
            },
        )
        self.reconcile_targets = self.target_projection.start()
        self.persist_snapshot = mock.patch.object(
            skill_cli, "_persist_snapshot"
        ).start()
        self.persist_render = mock.patch.object(
            skill_cli, "_persist_render"
        ).start()
        self.con = make_db()

    def tearDown(self):
        seed_skills.RETIRED_FILE, seed_skills.OUT = self._orig
        self.con.close()
        self.target_projection.stop()
        self.projection.stop()
        self.persist_render.stop()
        self.persist_snapshot.stop()
        self.tmp.cleanup()

    def _retire_file(self, names) -> None:
        seed_skills.RETIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        seed_skills.RETIRED_FILE.write_text(json.dumps(names))

    def _deleted(self, name) -> int:
        return self.con.execute(
            "SELECT is_deleted FROM skills WHERE name=?", (name,)).fetchone()[0]

    # ── retired_skill_names ─────────────────────────────────────────────────
    def test_no_file_is_empty_list(self):
        self.assertEqual(seed_skills.retired_skill_names(), [])

    def test_bad_json_fails_loud(self):
        seed_skills.RETIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        seed_skills.RETIRED_FILE.write_text("{broken")
        with self.assertRaises(ValueError):
            seed_skills.retired_skill_names()

    def test_non_list_fails_loud(self):
        self._retire_file({"retire": ["redline_review"]})
        with self.assertRaises(ValueError):
            seed_skills.retired_skill_names()

    # ── apply_retired ───────────────────────────────────────────────────────
    def test_retire_flips_engine_skill(self):
        self._retire_file(["redline_review"])
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, ["redline_review"])
        self.assertEqual(self._deleted("redline_review"), 1)
        self.assertEqual(self._deleted("review"), 0)

    def test_apply_is_idempotent(self):
        self._retire_file(["redline_review"])
        seed_skills.apply_retired(self.con)
        self.assertEqual(seed_skills.apply_retired(self.con), [],
                         "second apply must flip nothing")

    def test_noop_apply_does_not_open_write_transaction(self):
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(seed_skills.apply_retired(self.con), [])
        self.assertFalse(
            self.con.in_transaction,
            "fresh skill retirement convergence must stay read-only",
        )

    def test_unlisting_restores(self):
        self._retire_file(["redline_review"])
        seed_skills.apply_retired(self.con)
        self._retire_file([])
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, ["redline_review"])
        self.assertEqual(self._deleted("redline_review"), 0)

    def test_survives_full_seed_rerun(self):
        # update.sync_skills re-executes the whole seed → is_deleted=0; the
        # re-apply must retire the skill again.
        self._retire_file(["redline_review"])
        seed_skills.apply_retired(self.con)
        self.con.executescript(SEED_SQL)      # the resurrect
        self.assertEqual(self._deleted("redline_review"), 0)
        seed_skills.apply_retired(self.con)
        self.assertEqual(self._deleted("redline_review"), 1)

    def test_sync_engine_skills_reapplies(self):
        self._retire_file(["redline_review"])
        seed_skills.apply_retired(self.con)
        self.con.executescript(SEED_SQL)      # simulate an upstream resurrect
        seed_skills.sync_engine_skills(self.con, specs=[])
        self.assertEqual(self._deleted("redline_review"), 1)

    def test_local_skill_never_touched(self):
        self._retire_file(["test_authoring_dosarch"])   # local name — ignored
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, [])
        self.assertEqual(self._deleted("test_authoring_dosarch"), 0)

    def test_grants_stay_dormant(self):
        self._retire_file(["redline_review"])
        seed_skills.apply_retired(self.con)
        n = self.con.execute(
            "SELECT COUNT(*) FROM shell_skills ss "
            "JOIN skills s ON s.skill_id=ss.skill_id "
            "WHERE s.name='redline_review'").fetchone()[0]
        self.assertEqual(n, 1, "retire must not delete grant rows")

    # ── CLI (skill.py) ──────────────────────────────────────────────────────
    def test_cmd_retire_writes_file_and_flips(self):
        skill_cli.cmd_retire(self.con, "redline_review")
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()),
                         ["redline_review"])
        self.assertEqual(self._deleted("redline_review"), 1)
        self.reconcile_existing.assert_called_once_with(self.con)

    def test_cmd_retire_refuses_local_skill(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_retire(self.con, "test_authoring_dosarch")

    def test_cmd_retire_refuses_unknown(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_retire(self.con, "no_such_skill")

    def test_cmd_unretire_restores(self):
        skill_cli.cmd_retire(self.con, "redline_review")
        self.reconcile_existing.reset_mock()
        skill_cli.cmd_unretire(self.con, "redline_review")
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()), [])
        self.assertEqual(self._deleted("redline_review"), 0)
        self.reconcile_existing.assert_called_once_with(self.con)

    def test_cmd_grant_reconciles_the_target_shell(self):
        skill_cli.cmd_grant(self.con, "review", ["BSP1"])
        self.reconcile_targets.assert_called_once_with(self.con, [1])

    def test_cmd_revoke_reconciles_the_target_shell(self):
        skill_cli.cmd_grant(self.con, "review", ["BSP1"])
        self.reconcile_targets.reset_mock()
        skill_cli.cmd_revoke(self.con, "review", ["BSP1"])
        self.reconcile_targets.assert_called_once_with(self.con, [1])

    def test_cmd_rm_reconciles_every_existing_checkout(self):
        skill_cli.cmd_rm(self.con, "test_authoring_dosarch")
        self.reconcile_existing.assert_called_once_with(self.con)
        self.assertEqual(self._deleted("test_authoring_dosarch"), 1)

    def test_projection_failure_reports_committed_grant(self):
        self.reconcile_targets.side_effect = (
            skill_cli.skill_projection.ProjectionError("managed root is a symlink")
        )
        with self.assertRaisesRegex(
            SystemExit,
            "grant review committed in the DB, snapshot, and flat render, "
            "but skill projection failed: "
            "managed root is a symlink",
        ):
            skill_cli.cmd_grant(self.con, "review", ["BSP1"])
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM shell_skills ss "
                "JOIN skills s ON s.skill_id=ss.skill_id "
                "WHERE ss.shell_id=1 AND s.name='review'"
            ).fetchone()[0],
            1,
        )

    def test_cmd_unretire_unlisted_is_loud(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_unretire(self.con, "review")

    # ── the Planner API lane shares the spec helpers ─────────────────────────
    def test_spec_helpers_raise_conflicts_instead_of_exiting(self):
        with self.assertRaisesRegex(skill_cli.SkillConflictError, "LOCAL skill"):
            skill_cli._retire_spec(self.con, "test_authoring_dosarch")
        with self.assertRaisesRegex(skill_cli.SkillConflictError, "no engine skill"):
            skill_cli._retire_spec(self.con, "no_such_skill")
        with self.assertRaisesRegex(skill_cli.SkillConflictError, "not on the retire list"):
            skill_cli._unretire_spec(self.con, "review")

    def test_api_retire_and_unretire_round_trip(self):
        status, body = server._skills_mutation_report(
            server.api_skill_retire, self.con, "redline_review")
        self.assertEqual((status, body), (200, {
            "ok": True, "action": "retire", "name": "redline_review",
            "already_listed": False, "dormant_grants": 1,
        }))
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()),
                         ["redline_review"])
        self.assertEqual(self._deleted("redline_review"), 1)
        self.reconcile_existing.assert_called_once_with(self.con)

        status, body = server._skills_mutation_report(
            server.api_skill_retire, self.con, "redline_review")
        self.assertEqual(status, 200)
        self.assertTrue(body["already_listed"])

        self.reconcile_existing.reset_mock()
        status, body = server._skills_mutation_report(
            server.api_skill_unretire, self.con, "redline_review")
        self.assertEqual((status, body), (200, {
            "ok": True, "action": "unretire", "name": "redline_review", "grants": 1,
        }))
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()), [])
        self.assertEqual(self._deleted("redline_review"), 0)
        self.reconcile_existing.assert_called_once_with(self.con)

    def test_api_retire_refusals_are_structured(self):
        status, body = server._skills_mutation_report(
            server.api_skill_retire, self.con, "test_authoring_dosarch")
        self.assertEqual(status, 409)
        self.assertIn("LOCAL skill", body["error"])
        status, body = server._skills_mutation_report(
            server.api_skill_unretire, self.con, "review")
        self.assertEqual(status, 409)
        self.assertIn("not on the retire list", body["error"])
        with self.assertRaises(server.SkillApiError):
            server.api_skill_retire(self.con, "")

    def test_grant_of_retired_skill_is_loud(self):
        skill_cli.cmd_retire(self.con, "redline_review")
        with self.assertRaises(SystemExit):
            skill_cli.resolve_skill(self.con, "redline_review")


class RenderCheckRetirementTest(unittest.TestCase):
    def test_hermetic_build_applies_retire_list_after_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schema = root / "schema.sql"
            content = root / "content.sql"
            retired = root / "skills_retired.json"
            db = root / "hermetic.db"
            schema.write_text(
                "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL UNIQUE, description TEXT, category TEXT, "
                "content TEXT, command TEXT, common INTEGER NOT NULL DEFAULT 1, "
                "is_deleted INTEGER NOT NULL DEFAULT 0);"
            )
            content.write_text(SEED_SQL)
            retired.write_text(json.dumps(["redline_review"]))

            with (
                mock.patch.object(render_check, "SCHEMA", schema),
                mock.patch.object(render_check, "CONTENT", content),
                mock.patch.object(
                    render_check, "CONTENT_LEGACY", root / "missing.sql"
                ),
                mock.patch.object(render_check.migrate_mod, "migrate"),
                mock.patch.object(seed_skills, "RETIRED_FILE", retired),
                mock.patch.object(seed_skills, "OUT", content),
            ):
                render_check._build_tracked_db(db)

            with closing(sqlite3.connect(db)) as con:
                rows = con.execute(
                    "SELECT name, is_deleted FROM skills "
                    "WHERE name IN ('redline_review', 'review') ORDER BY name"
                ).fetchall()
            self.assertEqual(rows, [("redline_review", 1), ("review", 0)])


class SkillCliConnectionTest(unittest.TestCase):
    def test_exact_list_uses_shell_api_without_opening_database(self):
        payload = {"skills": [{
            "skill_id": 1, "name": "api_skill", "common": 0,
            "is_deleted": 0, "grant_scopes": ["flavor:dev"],
        }]}
        with (
            mock.patch.object(skill_cli.mem, "SC_API_TOKEN", "shell-token"),
            mock.patch.object(skill_cli.mem, "SC_API_BASE", "http://engine"),
            mock.patch.object(skill_cli.mem, "_api", return_value=payload) as api,
            mock.patch.object(
                skill_cli, "connect", side_effect=AssertionError("opened DB")
            ),
        ):
            self.assertEqual(skill_cli.main(["list"]), 0)
        api.assert_called_once_with("GET", "/_sc/skills")

    def test_no_token_root_list_keeps_normal_database_connection(self):
        con = mock.Mock()
        with (
            mock.patch.object(skill_cli.mem, "SC_API_TOKEN", ""),
            mock.patch.object(skill_cli, "connect", return_value=con) as opened,
            mock.patch.object(skill_cli, "cmd_list", return_value=0),
        ):
            self.assertEqual(skill_cli.main(["list"]), 0)
        opened.assert_called_once_with()

    def test_mutation_keeps_the_wal_enabled_write_connection(self):
        con = mock.Mock()
        with (
            mock.patch.object(skill_cli, "connect", return_value=con) as opened,
            mock.patch.object(skill_cli, "cmd_grant", return_value=0),
        ):
            self.assertEqual(skill_cli.main(["grant", "review", "DEV1"]), 0)
        opened.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
