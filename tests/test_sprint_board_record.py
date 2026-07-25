#!/usr/bin/env python3
"""The sprint board as a record — migration 0098 + /api/sprint-units + the
`sc sprint unit|board` verbs (spec doc 58 "The board becomes a record",
feature 27, sprint doc 59 U1).

Every test here pins a decision that a plausible tidy-up would undo, and the
two that matter most are not about happy-path CRUD:

- THE REVIEWER COLUMN IS THE UNIT. `spec_tasks` carries one `shell_id` and no
  reviewer, which is why a dead reviewer (flag #185 instance 4) is invisible
  to any comparator built on today's data. A board that stored only the dev
  would pass every CRUD test ever written and still ship the original defect.
- WORKERS DO NOT WRITE THE BOARD. Not a permission nicety: a worker that
  could mark its own unit done would make the board agree with reality BY
  CONSTRUCTION, and the reconciler's entire value is the disagreement. So the
  refusal is asserted together with the row being UNMOVED — a 403 that still
  wrote would satisfy a status-code-only test.

The rest pin the edges where belief gets corrupted quietly rather than
loudly: an edit that creates a phantom unit, a declaration that overwrites a
live one, a typo'd shortname that leaves a role silently empty (which the
reconciler reads as "nobody is expected here" — a wrong answer that looks
like a correct one), a state re-assert that hands a stalled worker a fresh
detection window, and a state move smuggled in beside a branch rename.

Run:
    python3 tests/test_sprint_board_record.py
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import interface_routes as routes  # noqa: E402
import interface_wake  # noqa: E402
import sprint as sprint_cli  # noqa: E402

OP = "Authorization: Bearer optok"
PLANNER = "Authorization: Bearer plntok"      # shell 9, flavor planner
PLANNER2 = "Authorization: Bearer pln2tok"    # shell 10, flavor planner
DEV = "Authorization: Bearer devtok"          # shell 11, flavor dev
REVIEWER = "Authorization: Bearer revtok"     # shell 8, flavor reviewer

DOC_BODY = "# SPRINT: test\nstatus: ACTIVE\n\nprose the record never touches\n"


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    for sid, short, flavor, key in (
            (8, "REV2", "reviewer", "revtok"),
            (9, "PLN1", "planner", "plntok"),
            (10, "PLN2", "planner", "pln2tok"),
            (11, "DEV5", "dev", "devtok")):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
            "mandate, system_prompt, user_id, api_key, is_shared, "
            "has_identity, bootstrapped) "
            "VALUES (?,?,?,?,'test','sp',1,?,0,1,1)",
            (sid, f"S{sid}", short, flavor, key))
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body) "
        "VALUES (1,'doc','SPRINT: test',?)", (DOC_BODY,))
    con.commit()
    con.close()


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


class _NoCoordinator:
    def notify_binding(self, binding_id):
        pass

    def notify_message(self, message_id):
        pass

    def notify_session(self, session_id):
        pass


class BoardRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "shell_db.db"
        build_engine_db(self.db_path)
        run_dir = self.tmp / "run" / "interface"
        run_dir.mkdir(parents=True)
        self.patches = [
            mock.patch.object(routes, "DB_PATH", self.db_path),
            mock.patch.object(routes, "RUN_DIR", run_dir),
            mock.patch.object(routes, "OPERATOR_TOKEN_PATH",
                              run_dir / "operator.token"),
        ]
        for p in self.patches:
            p.start()
        (run_dir / "operator.token").write_text("optok")
        interface_wake.bind(_NoCoordinator())
        self._keys = 0

    def tearDown(self):
        interface_wake.bind(None)
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    def call(self, method, path, header_lines=(), body=None):
        self._keys += 1
        lines = list(header_lines)
        if method != "GET":
            lines.append(f"Idempotency-Key: k{self._keys}")
        payload = json.dumps(body).encode() if body is not None else b""
        status, _h, resp = routes.handle(method, path, hdrs(*lines), payload)
        return status, json.loads(resp or b"{}")

    def add(self, who=(OP,), **body):
        body.setdefault("sprint_doc_id", 1)
        body.setdefault("seq", "U1")
        body.setdefault("unit_title", "board record")
        return self.call("POST", "/api/sprint-units", who, body)

    def patch(self, who=(OP,), **body):
        body.setdefault("sprint_doc_id", 1)
        body.setdefault("seq", "U1")
        return self.call("PATCH", "/api/sprint-units", who, body)

    def row(self, seq="U1"):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM sprint_units WHERE seq=?",
                            (seq,)).fetchone()
            return dict(r) if r is not None else None
        finally:
            con.close()

    def sql(self, stmt, params=()):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(stmt, params)
            con.commit()
        finally:
            con.close()

    def arm_binding(self, planner_shell_id=9):
        """A live binding for that planner. `session_id` is NOT NULL, so the
        binding needs a generation + session behind it — written directly
        rather than through the arm route, which additionally demands a
        hook-capable harness this test has no opinion about."""
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO interface_generations (shell_id, generation) "
                "VALUES (?,1)", (planner_shell_id,))
            sid = con.execute(
                "INSERT INTO interface_sessions (shell_id, generation, "
                "occupancy, lifecycle, harness, cli_version) "
                "VALUES (?,1,'occupied','idle','kimi','kimi-code 0.27.0')",
                (planner_shell_id,)).lastrowid
            con.execute(
                "INSERT INTO sprint_planner_bindings (sprint_doc_id, "
                "planner_shell_id, session_id, shell_id, generation) "
                "VALUES (1,?,?,?,1)",
                (planner_shell_id, sid, planner_shell_id))
            con.commit()
        finally:
            con.close()

    # -- the reviewer column: the whole point of the unit ---------------------

    def test_both_roles_are_records_and_resolve_to_shells(self):
        """The gap that blocked everything: a reviewer assignment must be
        REPRESENTABLE and readable. A board carrying only the dev passes
        ordinary CRUD tests and still leaves a dead reviewer invisible."""
        status, unit = self.add(dev="DEV5", reviewer="REV2")
        self.assertEqual(status, 201, unit)
        self.assertEqual(unit["dev_shell_id"], 11)
        self.assertEqual(unit["reviewer_shell_id"], 8)
        self.assertEqual(unit["dev_shortname"], "DEV5")
        self.assertEqual(unit["reviewer_shortname"], "REV2")
        # and durably, not just in the response projection
        self.assertEqual(self.row()["reviewer_shell_id"], 8)

    def test_reviewer_is_queryable_as_an_expected_role(self):
        """What U4 will actually run: 'who is expected on this sprint'. The
        reviewer must come back from a query over the RECORD, which is
        impossible against spec_tasks' single shell_id."""
        self.add(seq="U1", dev="DEV5", reviewer="REV2")
        self.add(seq="U2", dev="DEV5", reviewer="REV2")
        con = sqlite3.connect(self.db_path)
        try:
            found = con.execute(
                "SELECT COUNT(*) FROM sprint_units "
                "WHERE reviewer_shell_id=8 AND state NOT IN "
                "('merged','cancelled')").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(found, 2)

    # -- workers never write the board ---------------------------------------

    def test_dev_cannot_declare_a_unit_and_nothing_is_written(self):
        self.arm_binding()
        status, err = self.add((DEV,), seq="U9")
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "not_the_planner")
        self.assertIsNone(self.row("U9"), "the refused write still landed")

    def test_dev_cannot_move_its_own_unit_to_merged(self):
        """The failure this whole service exists to prevent: a worker marking
        its own unit done makes the board agree with reality by construction.
        The refusal is worthless unless the state is genuinely unmoved."""
        self.arm_binding()
        self.add(dev="DEV5", reviewer="REV2")
        status, err = self.patch((DEV,), state="merged")
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "not_the_planner")
        self.assertEqual(self.row()["state"], "pending")

    def test_reviewer_cannot_write_the_board_either(self):
        self.arm_binding()
        self.add(dev="DEV5", reviewer="REV2")
        status, _ = self.patch((REVIEWER,), state="in_review")
        self.assertEqual(status, 403)
        self.assertEqual(self.row()["state"], "pending")

    def test_workers_read_the_board_freely(self):
        """Read is not the fence. Every participant works from this board;
        blocking their reads would just push them back to parsing prose."""
        self.add(dev="DEV5", reviewer="REV2")
        for who in (DEV, REVIEWER):
            status, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1",
                                    (who,))
            self.assertEqual(status, 200)
            self.assertEqual(len(out["units"]), 1)
            self.assertEqual(out["units"][0]["reviewer_shortname"], "REV2")

    def test_bound_planner_writes_and_another_planner_does_not(self):
        """The binding names ONE planner. A second planner shell — running its
        own sprint next door — is not an authority on this board."""
        self.arm_binding(planner_shell_id=9)
        status, _ = self.add((PLANNER,), dev="DEV5")
        self.assertEqual(status, 201)
        status, err = self.patch((PLANNER2,), state="working")
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "not_the_planner")
        self.assertEqual(self.row()["state"], "pending")

    def test_unbound_sprint_falls_back_to_planner_flavor_not_to_anyone(self):
        """The spec's fallback is 'the sprint doc's author', which `documents`
        has no column for. Flavor is the stand-in — and it must still exclude
        workers, or the fence is decorative before a binding is armed."""
        status, _ = self.add((PLANNER2,), dev="DEV5")
        self.assertEqual(status, 201)
        status, err = self.patch((DEV,), branch="feat/x")
        self.assertEqual(status, 403)
        self.assertIsNone(self.row()["branch"])

    # -- belief must not be corrupted quietly --------------------------------

    def test_patch_never_creates_a_phantom_unit(self):
        """A typo'd --seq on an edit must fail loudly. A created phantom is
        worse than an error: the reconciler would expect a shell to be working
        on a unit that was never declared."""
        self.add(seq="U1")
        status, err = self.patch(seq="U7", state="working")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "no_such_unit")
        self.assertIsNone(self.row("U7"))

    def test_add_is_not_an_upsert_and_leaves_the_live_row_intact(self):
        self.add(dev="DEV5", reviewer="REV2", branch="feat/real")
        self.patch(state="working")
        status, err = self.add(unit_title="typo redeclare", dev="PLN2")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "unit_exists")
        live = self.row()
        self.assertEqual(live["state"], "working")
        self.assertEqual(live["branch"], "feat/real")
        self.assertEqual(live["dev_shell_id"], 11)
        self.assertEqual(live["unit_title"], "board record")

    def test_unknown_shortname_is_refused_not_silently_unassigned(self):
        """An empty role column reads as 'nobody is expected here'. A typo
        that clears a role therefore produces a CONFIDENTLY WRONG answer
        rather than a visible failure — the worst outcome for a monitor."""
        self.add(dev="DEV5", reviewer="REV2")
        status, err = self.patch(reviewer="REV9")
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "no_such_shell")
        self.assertEqual(self.row()["reviewer_shell_id"], 8)

    def test_a_role_is_cleared_only_when_said_explicitly(self):
        """Omitted field = leave alone; explicit null = clear. Conflating the
        two makes every partial edit a silent de-assignment."""
        self.add(dev="DEV5", reviewer="REV2")
        self.patch(branch="feat/x")
        self.assertEqual(self.row()["reviewer_shell_id"], 8)
        self.patch(reviewer=None)
        self.assertIsNone(self.row()["reviewer_shell_id"])

    # -- state is the expectation surface ------------------------------------

    def test_a_state_move_refuses_to_carry_other_edits(self):
        """State is the only column role expectation derives from. Bundled
        with a branch rename, a planner moves what a worker is expected to be
        doing as a side effect of clerical tidying."""
        self.add(dev="DEV5")
        status, err = self.patch(state="working", branch="feat/x")
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "state_moves_alone")
        live = self.row()
        self.assertEqual(live["state"], "pending")
        self.assertIsNone(live["branch"])

    def test_state_changed_at_moves_only_on_a_real_change(self):
        """The no-progress window resets on state change. If re-asserting the
        same state restamped the clock, a planner refreshing its board would
        hand a stalled worker a fresh detection window every tick — the
        monitor would go quiet exactly when it should fire."""
        self.add(dev="DEV5")
        self.patch(state="working")
        self.sql("UPDATE sprint_units SET state_changed_at='2020-01-01 00:00:00'"
                 " WHERE seq='U1'")
        self.patch(state="working")
        self.assertEqual(self.row()["state_changed_at"], "2020-01-01 00:00:00",
                         "re-asserting the same state reset the window")
        self.patch(state="in_review")
        self.assertNotEqual(self.row()["state_changed_at"],
                            "2020-01-01 00:00:00")

    def test_assigned_at_moves_when_a_role_changes(self):
        """U7's trigger surface: an assignment change is what it notifies on,
        and a field edit is not one."""
        self.add(dev="DEV5", reviewer="REV2")
        self.sql("UPDATE sprint_units SET assigned_at='2020-01-01 00:00:00' "
                 "WHERE seq='U1'")
        self.patch(branch="feat/x")
        self.assertEqual(self.row()["assigned_at"], "2020-01-01 00:00:00",
                         "a branch edit counted as a reassignment")
        self.patch(reviewer="PLN2")
        self.assertNotEqual(self.row()["assigned_at"], "2020-01-01 00:00:00")

    def test_the_db_refuses_a_state_outside_the_declared_set(self):
        """The API validates, but the CHECK is the backstop for anything that
        reaches the table another way."""
        self.add()
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE sprint_units SET state='done' WHERE seq='U1'")

    def test_who_moved_belief_is_recorded(self):
        self.arm_binding()
        self.add((PLANNER,), dev="DEV5")
        self.assertEqual(self.row()["updated_by_shell_id"], 9)

    # -- the record is the source; the table is a view -----------------------

    def test_board_writes_never_touch_the_document_body(self):
        """The document keeps its prose and the record holds the units. A
        write-back would state the board twice — the exact drift this table
        exists to delete."""
        self.add(dev="DEV5", reviewer="REV2")
        self.patch(state="working")
        con = sqlite3.connect(self.db_path)
        try:
            body = con.execute(
                "SELECT body FROM documents WHERE document_id=1").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(body, DOC_BODY)

    def test_the_merge_surface_annotation_survives_and_renders(self):
        """The markdown board's "depends on" cell carries prose beside the
        dependency — "shares SKILL.md with U8 — MUST rebase onto merged U2" —
        and the merge protocol turns on it. A record storing only the comma
        list would hold LESS than the markdown it replaces, which makes this
        unit a lossy migration rather than a fix."""
        note = "shares schema.sql + scripts/sprint.py with U5 — MUST rebase"
        self.add(seq="U5", unit_title="alerts + delivery", depends_on="U4",
                 overlap=note)
        self.assertEqual(self.row("U5")["overlap"], note)
        _s, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1", (OP,))
        buf = io.StringIO()
        with mock.patch.object(sprint_cli, "_api", return_value=out), \
                redirect_stdout(buf):
            sprint_cli.cmd_board(mock.Mock(sprint=1))
        self.assertIn(f"| U4 · {note} |", buf.getvalue())

    def test_an_annotation_without_a_dependency_still_renders(self):
        """Wave 1's units depend on nothing and carry the annotation anyway —
        doc 59's own U1 row reads "— · owns migration 0098". An empty
        dependency must not swallow the note."""
        self.add(seq="U1", overlap="owns migration 0098; schema.sql")
        _s, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1", (OP,))
        buf = io.StringIO()
        with mock.patch.object(sprint_cli, "_api", return_value=out), \
                redirect_stdout(buf):
            sprint_cli.cmd_board(mock.Mock(sprint=1))
        self.assertIn("| — · owns migration 0098; schema.sql |",
                      buf.getvalue())

    def test_rendered_board_reports_the_record_including_the_reviewer(self):
        """`sc sprint board` is a projection of the record. It renders what
        the record says — both roles — not what a document body remembers."""
        self.add(seq="U1", unit_title="board record", dev="DEV5",
                 reviewer="REV2", depends_on="U0", branch="feat/b", pr_number=7)
        self.patch(seq="U1", state="in_review")
        _s, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1", (OP,))
        buf = io.StringIO()
        with mock.patch.object(sprint_cli, "_api", return_value=out), \
                redirect_stdout(buf):
            sprint_cli.cmd_board(mock.Mock(sprint=1))
        text = buf.getvalue()
        self.assertIn("| U1 | board record | DEV5 | REV2 | U0 | feat/b | #7 "
                      "| in_review |", text)

    def test_an_unassigned_role_renders_as_empty_not_as_a_shell(self):
        self.add(seq="U3", unit_title="activity readers")
        _s, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1", (OP,))
        buf = io.StringIO()
        with mock.patch.object(sprint_cli, "_api", return_value=out), \
                redirect_stdout(buf):
            sprint_cli.cmd_board(mock.Mock(sprint=1))
        self.assertIn("| U3 | activity readers | — | — |", buf.getvalue())

    # -- the board survives a rebuild ----------------------------------------

    def test_the_board_is_snapshot_content_not_a_rebuildable_cache(self):
        """Nothing re-derives a sprint board. Left out of the snapshot
        allowlist, a mid-sprint rebuild drops every unit and the reconciler
        has nothing left to compare belief against."""
        import snapshot
        self.assertIn("sprint_units", snapshot.PER_INSTANCE_TABLES)
        self.assertGreater(
            snapshot.PER_INSTANCE_TABLES.index("sprint_units"),
            snapshot.PER_INSTANCE_TABLES.index("documents"),
            "sprint_units must load after its FK target `documents`")
        self.assertGreater(
            snapshot.PER_INSTANCE_TABLES.index("sprint_units"),
            snapshot.PER_INSTANCE_TABLES.index("shells"),
            "sprint_units must load after its FK target `shells`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
