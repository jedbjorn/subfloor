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

import json
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
import sprint_routes  # noqa: E402
import sprint_units  # noqa: E402


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
    bespoke_sid = con.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname) "
        "VALUES ('Custom', 'x', NULL, 'custom')").lastrowid
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
    con.execute("INSERT INTO flavor_skills (flavor, skill_id) VALUES ('dev', ?)",
                (kid,))
    con.execute("INSERT INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
                (bespoke_sid, kid))
    con.commit()
    return {"shell_id": sid, "bespoke_shell_id": bespoke_sid,
            "feature_id": fid, "skill_id": kid}


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

    def test_health_exposes_local_artifact_capabilities(self) -> None:
        with mock.patch.object(server.ports_mod, "resolve",
                               return_value={"repo": "source", "port": 17171}), \
             mock.patch.object(server.artifact_policy, "mode", return_value="local"):
            out = server.health_payload()
        self.assertEqual(out["artifact_mode"], "local")
        self.assertFalse(out["git_publication"])
        self.assertEqual(out["repo"], "source")

    def test_health_keeps_tracked_forks_publishable(self) -> None:
        with mock.patch.object(server.ports_mod, "resolve",
                               return_value={"repo": "fork", "port": 17172}), \
             mock.patch.object(server.artifact_policy, "mode", return_value="tracked"):
            out = server.health_payload()
        self.assertEqual(out["artifact_mode"], "tracked")
        self.assertTrue(out["git_publication"])

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

    def test_get_active_sprints_empty(self) -> None:
        self.assertEqual(
            server.get_active_sprints(self.con),
            {"active_count": 0, "sprints": []})

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
        self.assertEqual(fixture["granted_flavors"], ["dev"])
        self.assertEqual(
            fixture["granted_shells"], [self.ids["bespoke_shell_id"]])
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


class ActiveSprintsProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = build_db()
        self._next_pr = 100
        self.planner_old = self._shell("PLN-OLD")
        self.planner_new = self._shell("PLN-NEW")
        self.dev = self._shell("DEV")
        self.reviewer = self._shell("REV")
        self.feature = self.con.execute(
            "INSERT INTO roadmap (title, roadmap_status) "
            "VALUES ('Flow Board', 'in_progress')").lastrowid
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()

    def _shell(self, shortname: str) -> int:
        return self.con.execute(
            "INSERT INTO shells "
            "(display_name, shortname, system_prompt, flavor) "
            "VALUES (?, ?, 'x', 'dev')",
            (shortname, shortname)).lastrowid

    def _doc(self, title: str, *, frozen=0, body="status: ACTIVE",
             created_at="2026-07-26 12:00:00", feature_id=None,
             kind="doc") -> int:
        seq = self.con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM documents "
            "WHERE feature_id IS ? AND kind=?",
            (feature_id, kind)).fetchone()[0]
        return self.con.execute(
            "INSERT INTO documents "
            "(feature_id, kind, seq, title, frozen, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feature_id, kind, seq, title, frozen, body, created_at)).lastrowid

    def _unit(self, doc_id: int, seq: str, *, state="pending") -> int:
        # A distinct PR per unit: one PR belongs to one unit (0109's partial
        # unique index), so a fixture reusing 42 for every row now describes a
        # board no planner could have declared.
        self._next_pr += 1
        return self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id, seq, unit_title, dev_shell_id, "
            "reviewer_shell_id, state, depends_on, overlap, branch, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, 'U0', 'server.py', 'feat/unit', ?)",
            (doc_id, seq, f"Unit {seq}", self.dev, self.reviewer,
             state, self._next_pr)).lastrowid

    def _binding(self, doc_id: int, planner_id: int, *,
                 released=False) -> int:
        generation = self.con.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM interface_generations "
            "WHERE shell_id=?", (planner_id,)).fetchone()[0]
        self.con.execute(
            "INSERT INTO interface_generations (shell_id, generation) "
            "VALUES (?, ?)", (planner_id, generation))
        session_id = self.con.execute(
            "INSERT INTO interface_sessions (shell_id, generation) "
            "VALUES (?, ?)", (planner_id, generation)).lastrowid
        return self.con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation, "
            "released_at) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, planner_id, session_id, planner_id, generation,
             "2026-07-26 12:30:00" if released else None)).lastrowid

    def _projected_ids(self) -> list[int]:
        return [
            sprint["document_id"]
            for sprint in server.get_active_sprints(self.con)["sprints"]
        ]

    def test_unfrozen_closed_body_still_projects(self) -> None:
        doc_id = self._doc(
            "SPRINT: Closed prose, live record", body="status: CLOSED")
        self._unit(doc_id, "U1")
        self.con.commit()

        out = server.get_active_sprints(self.con)

        self.assertEqual(out["active_count"], 1)
        self.assertEqual(out["sprints"][0]["document_id"], doc_id)

    def test_frozen_sprint_excluded_while_unfrozen_control_projects(self) -> None:
        active_id = self._doc("SPRINT: Active control")
        frozen_id = self._doc("SPRINT: Frozen", frozen=1)
        self._unit(active_id, "U1")
        self._unit(frozen_id, "U1")
        self.con.commit()

        ids = self._projected_ids()

        self.assertIn(active_id, ids)
        self.assertNotIn(frozen_id, ids)

    def test_zero_unit_sprint_still_projects(self) -> None:
        doc_id = self._doc("SPRINT: No units")
        self.con.commit()

        out = server.get_active_sprints(self.con)

        self.assertEqual(out["sprints"], [{
            "document_id": doc_id,
            "title": "SPRINT: No units",
            "started_at": "2026-07-26T12:00:00Z",
            "planner": None,
            "feature": None,
            "units": [],
        }])

    def test_empty_title_remainder_uses_document_id_fallback(self) -> None:
        doc_id = self._doc("SPRINT:")
        self._unit(doc_id, "U1")
        self.con.commit()

        sprint = server.get_active_sprints(self.con)["sprints"][0]

        self.assertEqual(sprint["title"], f"Sprint #{doc_id}")

    def test_whitespace_title_remainder_uses_document_id_fallback(self) -> None:
        doc_id = self._doc("SPRINT:   ")
        self._unit(doc_id, "U1")
        self.con.commit()

        sprint = server.get_active_sprints(self.con)["sprints"][0]

        self.assertEqual(sprint["title"], f"Sprint #{doc_id}")

    def test_kind_is_an_independent_structural_predicate_operand(self) -> None:
        active_id = self._doc("SPRINT: Active")
        self._unit(active_id, "U1")
        self._doc("SPRINT: Spec is not a sprint doc", kind="spec")
        self.con.commit()

        self.assertEqual(self._projected_ids(), [active_id])

    def test_title_is_an_independent_structural_predicate_operand(self) -> None:
        active_id = self._doc("SPRINT: Active")
        self._unit(active_id, "U1")
        self._doc("Ordinary document")
        self.con.commit()

        self.assertEqual(self._projected_ids(), [active_id])

    def test_lowercase_sprint_prefix_projects_and_renders_verbatim(self) -> None:
        doc_id = self._doc("sprint: lowercase declaration")
        self._unit(doc_id, "U1")
        self.con.commit()

        sprint = server.get_active_sprints(self.con)["sprints"][0]

        self.assertEqual(sprint["document_id"], doc_id)
        self.assertEqual(sprint["title"], "sprint: lowercase declaration")

    def test_sprint_unit_columns_match_board_route_projection(self) -> None:
        self.assertIs(server._SPRINT_UNIT_COLUMNS, sprint_units.UNIT_COLUMNS)
        self.assertEqual(
            sprint_units.UNIT_COLUMNS,
            sprint_routes._UNIT_COLS)

    def test_orders_sprints_and_units_and_projects_full_unit_shape(self) -> None:
        later = self._doc(
            "SPRINT: Later", created_at="2026-07-26 13:00:00")
        earlier = self._doc(
            "SPRINT: Earlier", created_at="2026-07-26 11:00:00",
            feature_id=self.feature)
        self._unit(later, "U1")
        for seq in ("U10", "U-H", "U2", "U1"):
            self._unit(earlier, seq, state="working")
        self._binding(earlier, self.planner_old, released=True)
        self._binding(earlier, self.planner_new, released=True)
        self.con.commit()

        out = server.get_active_sprints(self.con)

        self.assertEqual(out["active_count"], 2)
        self.assertEqual(
            [s["document_id"] for s in out["sprints"]], [earlier, later])
        sprint = out["sprints"][0]
        self.assertEqual(sprint["started_at"], "2026-07-26T11:00:00Z")
        self.assertIsNone(
            sprint["planner"],
            "retired bindings must not project as live planner truth",
        )
        self.assertEqual(sprint["feature"], {
            "feature_id": self.feature, "title": "Flow Board"})
        self.assertEqual(
            [u["seq"] for u in sprint["units"]],
            ["U1", "U2", "U-H", "U10"])
        self.assertEqual(set(sprint["units"][0]), {
            *server._SPRINT_UNIT_COLUMNS,
            "state_recognized", "dev_shortname", "reviewer_shortname",
        })
        self.assertTrue(sprint["units"][0]["state_recognized"])
        self.assertEqual(sprint["units"][0]["dev_shortname"], "DEV")
        self.assertEqual(sprint["units"][0]["reviewer_shortname"], "REV")

    def test_missing_metadata_is_explicit(self) -> None:
        doc_id = self._doc("SPRINT: Missing metadata")
        self._unit(doc_id, "U1")
        self.con.commit()

        sprint = server.get_active_sprints(self.con)["sprints"][0]

        self.assertIsNone(sprint["planner"])
        self.assertIsNone(sprint["feature"])

    def test_time_shape_gate_rejects_sqlite_relative_clock(self) -> None:
        valid_id = self._doc(
            "SPRINT: Valid time", created_at="2026-07-26 13:00:00")
        self._unit(valid_id, "U1")
        corrupt_id = self._doc(
            "SPRINT: Relative clock", created_at="now")
        self._unit(corrupt_id, "U1")
        self.con.commit()

        out = server.get_active_sprints(self.con)

        self.assertEqual(
            [s["document_id"] for s in out["sprints"]],
            [valid_id, corrupt_id])
        self.assertIsNone(out["sprints"][1]["started_at"])

    def test_time_parser_gate_rejects_shaped_invalid_timestamp(self) -> None:
        valid_id = self._doc(
            "SPRINT: Valid time", created_at="2026-07-26 13:00:00")
        self._unit(valid_id, "U1")
        corrupt_id = self._doc(
            "SPRINT: Invalid calendar", created_at="2026-99-99")
        self._unit(corrupt_id, "U1")
        self.con.commit()

        out = server.get_active_sprints(self.con)

        self.assertEqual(
            [s["document_id"] for s in out["sprints"]],
            [valid_id, corrupt_id])
        self.assertIsNone(out["sprints"][1]["started_at"])

    def test_unassigned_unit_roles_remain_explicit(self) -> None:
        doc_id = self._doc("SPRINT: Unassigned")
        self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id, seq, unit_title, state) "
            "VALUES (?, 'U1', 'Unassigned unit', 'pending')", (doc_id,))
        self.con.commit()

        unit = server.get_active_sprints(self.con)["sprints"][0]["units"][0]

        self.assertIsNone(unit["dev_shell_id"])
        self.assertIsNone(unit["dev_shortname"])
        self.assertIsNone(unit["reviewer_shell_id"])
        self.assertIsNone(unit["reviewer_shortname"])

    def test_unknown_unit_state_degrades_only_its_unit(self) -> None:
        corrupt_doc_id = self._doc("SPRINT: Corrupt state")
        healthy_doc_id = self._doc("SPRINT: Healthy board")
        self._unit(corrupt_doc_id, "U1", state="working")
        self._unit(healthy_doc_id, "U1", state="in_review")
        self.con.execute("PRAGMA ignore_check_constraints=ON")
        self.con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id, seq, unit_title, state) "
            "VALUES (?, 'U2', 'Corrupt unit', 'mystery')",
            (corrupt_doc_id,))
        self.con.commit()
        self.con.execute("PRAGMA ignore_check_constraints=OFF")
        proxy = mock.Mock(wraps=self.con)
        proxy.close.return_value = None

        with mock.patch.object(server, "db", return_value=proxy):
            status, _headers, body = server.dispatch_http(
                "GET", "/api/sprints?status=active", "", b"")
        out = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(out["active_count"], 2)
        self.assertEqual(
            [sprint["document_id"] for sprint in out["sprints"]],
            [corrupt_doc_id, healthy_doc_id])
        self.assertEqual(
            [(unit["state"], unit["state_recognized"])
             for unit in out["sprints"][0]["units"]],
            [("working", True), ("mystery", False)])
        self.assertEqual(
            [(unit["state"], unit["state_recognized"])
             for unit in out["sprints"][1]["units"]],
            [("in_review", True)])

    def test_projection_is_one_snapshot_and_never_reads_document_body(self) -> None:
        doc_id = self._doc("SPRINT: Snapshot", body="status: CLOSED")
        self._unit(doc_id, "U1")
        self.con.commit()
        statements = []
        self.con.set_trace_callback(statements.append)

        def authorizer(action, table, column, _db_name, _source):
            if action == sqlite3.SQLITE_READ \
                    and table == "documents" and column == "body":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.con.set_authorizer(authorizer)
        try:
            out = server.get_active_sprints(self.con)
        finally:
            self.con.set_authorizer(None)
            self.con.set_trace_callback(None)

        reads = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        self.assertEqual(out["active_count"], 1)
        self.assertEqual(len(reads), 1, reads)

    def test_get_route_requires_active_status(self) -> None:
        doc_id = self._doc("SPRINT: Routed")
        self._unit(doc_id, "U1")
        self.con.commit()
        proxy = mock.Mock(wraps=self.con)
        proxy.close.return_value = None
        with mock.patch.object(server, "db", return_value=proxy):
            status, _headers, body = server.dispatch_http(
                "GET", "/api/sprints?status=active", "", b"")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["active_count"], 1)

        with mock.patch.object(server, "db", return_value=proxy):
            status, _headers, body = server.dispatch_http(
                "GET", "/api/sprints", "", b"")
        self.assertEqual(status, 400)
        self.assertIn("status=active", json.loads(body)["error"])


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


class FlavorDefaultsTest(unittest.TestCase):
    """The Default Models matrix: get_flavor_defaults / set_flavor_default.

    flavor_defaults is migration-seeded launch config the GUI now edits — the
    contract is upsert-on-write (template flavors / harnesses may lack seeded
    rows), a transactional star (exactly one is_default per flavor after), and
    loud validation for unknown names."""

    def setUp(self) -> None:
        self.con = build_db()

    def _row(self, flavor, harness):
        return self.con.execute(
            "SELECT model, is_default FROM flavor_defaults "
            "WHERE flavor=? AND harness=?", (flavor, harness)).fetchone()

    def _route(self, harness, selector, *, availability="available", stale=0):
        self.con.execute(
            "INSERT INTO model_routes "
            "(harness, selector, source, availability, last_seen_at, stale) "
            "VALUES (?, ?, 'test', ?, datetime('now'), ?)",
            (harness, selector, availability, stale))
        self.con.commit()

    def test_matrix_includes_template_flavors_and_harnesses(self) -> None:
        got = server.get_flavor_defaults(self.con)
        self.assertIn("planner", got["flavors"])
        self.assertIn("admin", got["flavors"], "template flavors appear even unseeded")
        for h in ("claude", "codex", "opencode", "vibe"):
            self.assertIn(h, got["harnesses"])

    def test_set_model(self) -> None:
        self._route("claude", "opus")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": "opus"})
        self.assertTrue(ok, err)
        self.assertEqual(self._row("planner", "claude")["model"], "opus")

    def test_star_is_transactional_across_the_flavor(self) -> None:
        self.assertTrue(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "codex",
                       "is_default": True})[0])
        rows = self.con.execute(
            "SELECT harness, is_default FROM flavor_defaults "
            "WHERE flavor='planner'").fetchall()
        stars = {r["harness"]: r["is_default"] for r in rows}
        self.assertEqual(sum(stars.values()), 1)
        self.assertEqual(stars["codex"], 1)

    def test_upsert_missing_cell(self) -> None:
        # 'vibe' has no seeded row for planner — a write must create it
        self.assertIsNone(self._row("planner", "vibe"))
        self._route("vibe", "devstral-latest")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "vibe",
                       "model": "devstral-latest", "is_default": True})
        self.assertTrue(ok, err)
        row = self._row("planner", "vibe")
        self.assertEqual(row["model"], "devstral-latest")
        self.assertEqual(row["is_default"], 1)

    def test_empty_model_clears_to_null(self) -> None:
        self._route("claude", "opus")
        server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": "opus"})
        server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": None})
        self.assertIsNone(self._row("planner", "claude")["model"])

    def test_empty_string_is_not_harness_default(self) -> None:
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude", "model": ""})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)

    def test_invalid_model_does_not_create_missing_cell(self) -> None:
        self.assertIsNone(self._row("planner", "vibe"))
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "vibe",
                       "model": "not-local"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)
        self.assertIsNone(self._row("planner", "vibe"))

    def test_model_requires_exact_available_route_for_harness(self) -> None:
        self._route("codex", "gpt-5.6-sol")
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "gpt-5.6-sol"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)
        self.assertNotEqual(self._row("planner", "claude")["model"],
                            "gpt-5.6-sol")

    def test_stale_route_is_not_settable(self) -> None:
        self._route("claude", "opus-next", stale=1)
        ok, err = server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude",
                       "model": "opus-next"})
        self.assertFalse(ok)
        self.assertIn("invalid_model_route", err)

    def test_unknown_names_and_empty_writes_are_loud(self) -> None:
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "emacs", "model": "x"})[0])
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "nope", "harness": "claude", "model": "x"})[0])
        self.assertFalse(server.set_flavor_default(
            self.con, {"flavor": "planner", "harness": "claude"})[0])


class PatchShellTest(unittest.TestCase):
    """server.patch_shell — display_name rename + the strictly-guarded
    system_prompt H1 re-stamp (creation-time render only, never curation)."""

    def setUp(self) -> None:
        self.con = build_db()

    def tearDown(self) -> None:
        self.con.close()

    def _mk(self, name, prompt) -> int:
        sid = self.con.execute(
            "INSERT INTO shells (display_name, system_prompt) VALUES (?, ?)",
            (name, prompt)).lastrowid
        self.con.commit()
        return sid

    def _shell(self, sid):
        return self.con.execute(
            "SELECT display_name, system_prompt, current_state FROM shells "
            "WHERE shell_id=?", (sid,)).fetchone()

    def test_rename_restamps_pristine_h1(self) -> None:
        sid = self._mk("DEV1", "# DEV1 — dev shell, working repo\n\nfocus")
        ok, err = server.patch_shell(self.con, sid, {"display_name": "Forge"})
        self.assertTrue(ok, err)
        row = self._shell(sid)
        self.assertEqual(row["display_name"], "Forge")
        self.assertEqual(row["system_prompt"],
                         "# Forge — dev shell, working repo\n\nfocus")

    def test_rename_never_touches_curated_prompt(self) -> None:
        # H1 no longer carries the creation-time name → shell curation, no door
        sid = self._mk("DEV1", "# The Floorwright\n\nmy own words")
        ok, _ = server.patch_shell(self.con, sid, {"display_name": "Forge"})
        self.assertTrue(ok)
        row = self._shell(sid)
        self.assertEqual(row["display_name"], "Forge")
        self.assertEqual(row["system_prompt"], "# The Floorwright\n\nmy own words")

    def test_rename_trims_whitespace(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, _ = server.patch_shell(self.con, sid, {"display_name": "  Forge  "})
        self.assertTrue(ok)
        self.assertEqual(self._shell(sid)["display_name"], "Forge")

    def test_empty_and_nonstring_names_rejected(self) -> None:
        sid = self._mk("DEV1", "x")
        for bad in ("", "   ", None, 7):
            ok, err = server.patch_shell(self.con, sid, {"display_name": bad})
            self.assertFalse(ok)
            self.assertIn("non-empty", err)
        self.assertEqual(self._shell(sid)["display_name"], "DEV1")

    def test_missing_shell_is_not_found(self) -> None:
        ok, err = server.patch_shell(self.con, 999999, {"display_name": "X"})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_current_state_path_unchanged(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, _ = server.patch_shell(self.con, sid, {"current_state": "building"})
        self.assertTrue(ok)
        self.assertEqual(self._shell(sid)["current_state"], "building")

    def test_system_prompt_stays_doorless(self) -> None:
        sid = self._mk("DEV1", "x")
        ok, err = server.patch_shell(self.con, sid, {"system_prompt": "hijack"})
        self.assertFalse(ok)
        self.assertEqual(self._shell(sid)["system_prompt"], "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
