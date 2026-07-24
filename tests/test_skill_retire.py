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
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import seed_skills  # noqa: E402
import skill as skill_cli  # noqa: E402

ENGINE_SKILLS = ("test_authoring", "review", "git")

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
    con.executescript(SEED_SQL)
    con.execute("INSERT INTO skills (name, description, content) "
                "VALUES ('test_authoring_dosarch', 'fork skill', 'body')")
    # one grant on the skill we retire, to prove grants stay put
    con.execute("INSERT INTO shell_skills (shell_id, skill_id) "
                "SELECT 1, skill_id FROM skills WHERE name='test_authoring'")
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
        self.con = make_db()

    def tearDown(self):
        seed_skills.RETIRED_FILE, seed_skills.OUT = self._orig
        self.con.close()
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
        self._retire_file({"retire": ["test_authoring"]})
        with self.assertRaises(ValueError):
            seed_skills.retired_skill_names()

    # ── apply_retired ───────────────────────────────────────────────────────
    def test_retire_flips_engine_skill(self):
        self._retire_file(["test_authoring"])
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, ["test_authoring"])
        self.assertEqual(self._deleted("test_authoring"), 1)
        self.assertEqual(self._deleted("review"), 0)

    def test_apply_is_idempotent(self):
        self._retire_file(["test_authoring"])
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
        self._retire_file(["test_authoring"])
        seed_skills.apply_retired(self.con)
        self._retire_file([])
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, ["test_authoring"])
        self.assertEqual(self._deleted("test_authoring"), 0)

    def test_survives_full_seed_rerun(self):
        # update.sync_skills re-executes the whole seed → is_deleted=0; the
        # re-apply must retire the skill again.
        self._retire_file(["test_authoring"])
        seed_skills.apply_retired(self.con)
        self.con.executescript(SEED_SQL)      # the resurrect
        self.assertEqual(self._deleted("test_authoring"), 0)
        seed_skills.apply_retired(self.con)
        self.assertEqual(self._deleted("test_authoring"), 1)

    def test_sync_engine_skills_reapplies(self):
        self._retire_file(["test_authoring"])
        seed_skills.apply_retired(self.con)
        self.con.executescript(SEED_SQL)      # simulate an upstream resurrect
        seed_skills.sync_engine_skills(self.con, specs=[])
        self.assertEqual(self._deleted("test_authoring"), 1)

    def test_local_skill_never_touched(self):
        self._retire_file(["test_authoring_dosarch"])   # local name — ignored
        flipped = seed_skills.apply_retired(self.con)
        self.assertEqual(flipped, [])
        self.assertEqual(self._deleted("test_authoring_dosarch"), 0)

    def test_grants_stay_dormant(self):
        self._retire_file(["test_authoring"])
        seed_skills.apply_retired(self.con)
        n = self.con.execute(
            "SELECT COUNT(*) FROM shell_skills ss "
            "JOIN skills s ON s.skill_id=ss.skill_id "
            "WHERE s.name='test_authoring'").fetchone()[0]
        self.assertEqual(n, 1, "retire must not delete grant rows")

    # ── CLI (skill.py) ──────────────────────────────────────────────────────
    def test_cmd_retire_writes_file_and_flips(self):
        skill_cli.cmd_retire(self.con, "test_authoring")
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()),
                         ["test_authoring"])
        self.assertEqual(self._deleted("test_authoring"), 1)

    def test_cmd_retire_refuses_local_skill(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_retire(self.con, "test_authoring_dosarch")

    def test_cmd_retire_refuses_unknown(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_retire(self.con, "no_such_skill")

    def test_cmd_unretire_restores(self):
        skill_cli.cmd_retire(self.con, "test_authoring")
        skill_cli.cmd_unretire(self.con, "test_authoring")
        self.assertEqual(json.loads(seed_skills.RETIRED_FILE.read_text()), [])
        self.assertEqual(self._deleted("test_authoring"), 0)

    def test_cmd_unretire_unlisted_is_loud(self):
        with self.assertRaises(SystemExit):
            skill_cli.cmd_unretire(self.con, "review")

    def test_grant_of_retired_skill_is_loud(self):
        skill_cli.cmd_retire(self.con, "test_authoring")
        with self.assertRaises(SystemExit):
            skill_cli.resolve_skill(self.con, "test_authoring")


if __name__ == "__main__":
    unittest.main()
