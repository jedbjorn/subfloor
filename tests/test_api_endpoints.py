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
    con.commit()
    return {"shell_id": sid, "feature_id": fid}


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

    def test_get_map_empty_catalogue(self) -> None:
        # dr_* catalogue is empty in a fresh DB — must not crash.
        out = server.get_map(self.con)
        self.assertEqual(out["total_files"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
