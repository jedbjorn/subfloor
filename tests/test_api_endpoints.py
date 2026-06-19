#!/usr/bin/env python3
"""Smoke tests for the review-layer data-assembly functions (api/server.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and
the sibling tests. Each test builds a throwaway DB the way the engine ships it
(schema.sql + every migration in filename order), seeds REPRESENTATIVE data,
then calls each `get_*(con)` assembler and asserts it returns without raising.

Why this file exists: a `get_roadmap()` `KeyError: 'feature_id'` shipped
because nothing exercised the endpoints, and the bug was data-dependent — it
only fired once an open flag was linked to a feature. `./sc verify` does
rebuild→render→boot and never touches the API; `./sc test` had no endpoint
coverage. So the seed below deliberately includes the trigger combinations:
  - a flag that is open + linked to a feature   (the exact KeyError trigger)
  - a document linked to a feature
  - a roadmap feature with an owning shell
Any future SELECT that omits a column the code reads by key will raise here,
on a developer's machine, instead of as a cryptic 500 in front of the FnB.

Run:
    python3 tests/test_api_endpoints.py
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402  (server.py adds scripts/ to the path on import)


def build_db() -> sqlite3.Connection:
    """Fresh in-memory DB: schema.sql + every migration, FK enforcement on."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for path in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(path.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed(con: sqlite3.Connection) -> dict:
    """Minimal but trigger-complete fixture. Returns the ids it created."""
    cur = con.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname) "
        "VALUES ('Dev', 'x', 'dev', 'dev')")
    sid = cur.lastrowid
    fid = con.execute(
        "INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary) "
        "VALUES ('Feature A', 'next', 1, ?, 'a summary')", (sid,)).lastrowid
    con.execute(
        "INSERT INTO documents (feature_id, kind, seq, title, render_path) "
        "VALUES (?, 'spec', 1, 'Spec A', 'specs_sc/a.md')", (fid,))
    con.execute(
        "INSERT INTO documents (feature_id, kind, seq, title, render_path) "
        "VALUES (?, 'doc', 1, 'Doc A', 'docs_sc/a.md')", (fid,))
    # The exact KeyError trigger: an OPEN, non-deleted flag linked to a feature.
    con.execute(
        "INSERT INTO flags (display_name, description, resolved, is_deleted, "
        "feature_id, shell_id) VALUES ('CC-001', 'blocker', 0, 0, ?, ?)",
        (fid, sid))
    # A repo-local skill (name not under assets/skills/) + a grant, so the
    # Skills-tab assembler exercises both origins and the grant aggregation.
    kid = con.execute(
        "INSERT INTO skills (name, description, category, common, is_deleted) "
        "VALUES ('local_only_skill', 'fixture repo skill', 'craft', 0, 0)").lastrowid
    con.execute("INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
                (sid, kid))
    con.commit()
    return {"shell_id": sid, "feature_id": fid, "skill_id": kid}


class AssemblerSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self.ids = seed(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def test_get_shells(self) -> None:
        out = server.get_shells(self.con)
        self.assertTrue(any(s["shell_id"] == self.ids["shell_id"] for s in out))

    def test_get_shell(self) -> None:
        out = server.get_shell(self.con, self.ids["shell_id"])
        self.assertIsNotNone(out)
        for key in ("seed", "lns", "skills", "decisions"):
            self.assertIn(key, out)

    def test_get_shell_missing_returns_none(self) -> None:
        self.assertIsNone(server.get_shell(self.con, 999999))

    def test_get_roadmap_with_linked_flag_and_doc(self) -> None:
        # The regression: this path raised KeyError('feature_id') when a flag
        # was linked to a feature. Assert it assembles and carries the links.
        out = server.get_roadmap(self.con)
        feats = [f for b in out["buckets"] for f in b["features"]]
        feat = next(f for f in feats if f["feature_id"] == self.ids["feature_id"])
        self.assertEqual(len(feat["open_flags"]), 1)
        self.assertTrue(len(feat["documents"]) >= 1)

    def test_get_docs(self) -> None:
        out = server.get_docs(self.con)
        self.assertTrue(any(d["feature_id"] == self.ids["feature_id"]
                            for d in out["docs"]))

    def test_get_flags(self) -> None:
        out = server.get_flags(self.con)
        self.assertTrue(out["flags"])
        self.assertTrue(any(f["feature_title"] == "Feature A"
                            for f in out["flags"]))

    def test_get_skills_origin_and_grants(self) -> None:
        out = server.get_skills(self.con)
        self.assertTrue(out["shells"])
        by_name = {s["name"]: s for s in out["skills"]}
        # the fixture skill has no assets/skills/ dir → repo origin, granted once
        fixture = by_name["local_only_skill"]
        self.assertEqual(fixture["origin"], "repo")
        self.assertEqual(fixture["granted_shells"], [self.ids["shell_id"]])
        # an engine-seeded skill derives as engine
        self.assertEqual(by_name["db_map"]["origin"], "engine")

    def test_get_shell_skills_carry_origin(self) -> None:
        out = server.get_shell(self.con, self.ids["shell_id"])
        self.assertTrue(all("origin" in k and "category" in k for k in out["skills"]))

    def test_get_map_unmapped_degrades_to_empty(self) -> None:
        # get_map() reads the SEPARATE map.db via map_db.open_ro() — it takes no
        # args and ignores shell_db. When the fork isn't mapped, open_ro() returns
        # None and get_map must degrade to the empty shape, never crash.
        with mock.patch.object(server.map_db, "open_ro", return_value=None):
            out = server.get_map()
        self.assertEqual(out["total_files"], 0)
        self.assertIsNone(out["repo"])

    def test_get_roadmap_includes_blockers_key(self) -> None:
        # Every feature dict must carry a `blockers` list (empty when none),
        # so the UI can read f.blockers unconditionally.
        out = server.get_roadmap(self.con)
        feats = [f for b in out["buckets"] for f in b["features"]]
        self.assertTrue(all(isinstance(f.get("blockers"), list) for f in feats))


class FeatureBlockerTest(unittest.TestCase):
    """server.set_blockers — replace-set semantics + the validations that keep
    the blocker graph a DAG (self, unknown id, cycle)."""

    def setUp(self) -> None:
        self.con = build_db()
        # three features in real (sequencing) stages
        self.A = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('A','in_progress')").lastrowid
        self.B = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('B','next')").lastrowid
        self.C = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) VALUES ('C','near_term')").lastrowid
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()

    def _blockers_of(self, fid):
        out = server.get_roadmap(self.con)
        feats = {f["feature_id"]: f for b in out["buckets"] for f in b["features"]}
        return sorted(feats[fid]["blockers"])

    def test_replace_set(self) -> None:
        ok, err = server.set_blockers(self.con, self.B, [self.A])
        self.assertTrue(ok, err)
        self.assertEqual(self._blockers_of(self.B), [self.A])
        # replace (not append): C then A,C
        ok, _ = server.set_blockers(self.con, self.C, [self.A])
        self.assertTrue(ok)
        ok, _ = server.set_blockers(self.con, self.C, [self.A, self.B])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), sorted([self.A, self.B]))
        # empty list clears
        ok, _ = server.set_blockers(self.con, self.C, [])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), [])

    def test_dedup(self) -> None:
        ok, _ = server.set_blockers(self.con, self.C, [self.A, self.A, self.B])
        self.assertTrue(ok)
        self.assertEqual(self._blockers_of(self.C), sorted([self.A, self.B]))

    def test_self_block_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, self.A, [self.A])
        self.assertFalse(ok)
        self.assertIn("itself", err)
        self.assertEqual(self._blockers_of(self.A), [])

    def test_unknown_id_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, self.A, [999999])
        self.assertFalse(ok)
        self.assertIn("no such feature", err)
        self.assertEqual(self._blockers_of(self.A), [])

    def test_missing_feature_rejected(self) -> None:
        ok, err = server.set_blockers(self.con, 999999, [self.A])
        self.assertFalse(ok)
        self.assertEqual(err, "no such feature")

    def test_cycle_rejected_and_no_write(self) -> None:
        ok, _ = server.set_blockers(self.con, self.B, [self.A])   # B ← A
        self.assertTrue(ok)
        ok, err = server.set_blockers(self.con, self.A, [self.B])  # A ← B would cycle
        self.assertFalse(ok)
        self.assertIn("cycle", err)
        # the rejected set wrote nothing; the original edge stands
        self.assertEqual(self._blockers_of(self.A), [])
        self.assertEqual(self._blockers_of(self.B), [self.A])

    def test_transitive_cycle_rejected(self) -> None:
        self.assertTrue(server.set_blockers(self.con, self.B, [self.A])[0])  # B ← A
        self.assertTrue(server.set_blockers(self.con, self.C, [self.B])[0])  # C ← B
        ok, err = server.set_blockers(self.con, self.A, [self.C])  # A ← C closes A→B→C→A
        self.assertFalse(ok)
        self.assertIn("cycle", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
