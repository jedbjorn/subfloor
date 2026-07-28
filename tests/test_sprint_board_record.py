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
import urllib.error
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
MIGRATION = MIGRATIONS / "0098_sprint_units.sql"
# 0098 declared the board; later deltas reshape it. A fork upgrading runs the
# CHAIN, so the fixture below must skip and then re-apply all of it — pinning
# only 0098 would compare "the board as first declared" against "the baseline
# plus every delta" and fail on the first ALTER anyone ever adds, while
# proving nothing about whether the upgrade path reaches the same table.
# Append to this tuple whenever a migration touches sprint_units.
BOARD_MIGRATIONS = (
    MIGRATION,
    MIGRATIONS / "0108_sprint_unit_transitions.sql",
    MIGRATIONS / "0109_sprint_pr_unit_linkage.sql",
)

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import migrate  # noqa: E402
import sprint as sprint_cli  # noqa: E402
import sprint_routes as routes  # noqa: E402
import sprint_units  # noqa: E402

# Post-Interface auth model (sprint_routes): no Authorization header on the
# localhost-fenced server IS the operator — there is no operator token file.
# Any non-Authorization header keeps the call-site shape below.
OP = "X-Actor: operator"
PLANNER = "Authorization: Bearer plntok"      # shell 9, flavor planner
PLANNER2 = "Authorization: Bearer pln2tok"    # shell 10, flavor planner
DEV = "Authorization: Bearer devtok"          # shell 11, flavor dev
REVIEWER = "Authorization: Bearer revtok"     # shell 8, flavor reviewer

DOC_BODY = "# SPRINT: test\nstatus: ACTIVE\n\nprose the record never touches\n"


class SharedStateVocabularyTest(unittest.TestCase):
    def test_api_uses_the_dependency_free_shared_vocabulary(self):
        self.assertIs(routes._UNIT_STATES, sprint_units.UNIT_STATES)
        self.assertEqual(
            set(sprint_units.TERMINAL_UNIT_STATES),
            {"merged", "cancelled"},
        )


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    seed_fixtures(con)
    con.close()


def build_pre_migration_db(path: Path) -> None:
    """The DB this migration actually meets in the field: a fork's live engine
    DB the moment before it pulls 0098 — everything else in place, no board.

    `schema.sql` is the CURRENT baseline and therefore already carries
    `sprint_units`, so the fixture drops it. That drop is the whole point:
    build from the baseline and 0098 could be DELETED with every behavioural
    test still green, because a fresh install would keep working while every
    fork that UPGRADES would be left with no board at all.
    """
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        if p not in BOARD_MIGRATIONS:
            con.executescript(p.read_text())
    con.execute("DROP TABLE IF EXISTS sprint_units")   # drops its index too
    seed_fixtures(con)
    con.close()


def seed_fixtures(con) -> None:
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


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


def _unit_args(**over):
    """A parsed argv for the `unit` verbs — every flag the parser defines,
    defaulted the way argparse leaves an absent one, since the CLI reads the
    difference between "absent" and "explicitly cleared"."""
    args = dict(sprint=1, seq="U1", title="board record", dev=None,
                reviewer=None, depends_on=None, overlap=None, branch=None,
                pr=None, review_head=None, state=None)
    args.update(over)
    return mock.Mock(**args)


class _FakeResponse:
    """What `urlopen` hands `_api` on success: a context manager whose read()
    returns the body."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _BoardCase(unittest.TestCase):
    """Scaffolding shared by the record tests and the live-upgrade tests: one
    temp DB with the routes bound to it. `build` is the hook the upgrade class
    swaps — it is the ONLY difference between a board that was installed and a
    board that was migrated into place."""

    build = staticmethod(build_engine_db)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "shell_db.db"
        self.build(self.db_path)
        self.patches = [
            mock.patch.object(routes, "DB_PATH", self.db_path),
        ]
        for p in self.patches:
            p.start()
        self._keys = 0

    def tearDown(self):
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

    def sql_one(self, stmt, params=()):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(stmt, params).fetchone()[0]
        finally:
            con.close()

    def sql(self, stmt, params=()):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(stmt, params)
            con.commit()
        finally:
            con.close()

    def arm_binding(self, planner_shell_id=9, sprint_doc_id=1) -> int:
        """A binding for that planner. `session_id` is NOT NULL, so the
        binding needs a generation + session behind it — written directly:
        the Interface arm route is retired (conductor Step 1) and the tables
        remain as the board_writer authority record. Returns the
        binding_id."""
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
            binding_id = con.execute(
                "INSERT INTO sprint_planner_bindings (sprint_doc_id, "
                "planner_shell_id, session_id, shell_id, generation) "
                "VALUES (?,?,?,?,1)",
                (sprint_doc_id, planner_shell_id, sid,
                 planner_shell_id)).lastrowid
            con.commit()
            return binding_id
        finally:
            con.close()

    def release(self, binding_id, reason="shell_recovery") -> None:
        """Stamp the binding released. The engine's release path
        (interface_broker.release_binding) retired with the wake machine
        (conductor Step 1); what release MEANS to the surviving board_writer
        fence is exactly `released_at` being set — which is what these tests
        pin: a released binding still names the writer."""
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "UPDATE sprint_planner_bindings "
                "SET released_at=datetime('now'), release_reason=? "
                "WHERE binding_id=?", (reason, binding_id))
            con.commit()
        finally:
            con.close()

    @contextmanager
    def cli(self, token="plntok"):
        """Drive the SHIPPED cli verbs against the real in-process routes.

        The route tests call `handle` directly and mint a fresh
        Idempotency-Key per call, so nothing in them can see the CLI's own key
        strategy — which is exactly where flag 221 lived. So this patches
        `urlopen` UNDERNEATH `_api` rather than replacing `_api` with a spy:
        the token, the headers and the key that reach the route are the ones
        the shipped code assembles, and the CLI's own error handling runs.
        Yields the list collecting each request's Idempotency-Key."""
        keys = []

        def fake_urlopen(req, timeout=None):
            keys.append(req.headers.get("Idempotency-key"))
            u = urlparse(req.full_url)
            status, _h, resp = routes.handle(
                req.get_method(), u.path + (f"?{u.query}" if u.query else ""),
                hdrs(*(f"{k}: {v}" for k, v in req.headers.items())),
                req.data or b"")
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "error", {}, io.BytesIO(resp))
            return _FakeResponse(resp)

        with mock.patch.object(sprint_cli, "SC_API_TOKEN", token), \
                mock.patch.object(sprint_cli.urllib.request, "urlopen",
                                  fake_urlopen):
            yield keys


class BoardRecordTest(_BoardCase):

    # -- localhost operator boundary -----------------------------------------

    def test_cross_site_browser_cannot_mutate_as_the_operator(self):
        """Host=localhost is not provenance: a hostile page can target a
        loopback URL while the browser still supplies the destination Host.
        Origin / Fetch Metadata keep that request outside operator authority."""
        for browser_headers in (
                ("Origin: https://hostile.example",
                 "Sec-Fetch-Site: cross-site"),
                ("Sec-Fetch-Site: cross-site",)):
            with self.subTest(browser_headers=browser_headers):
                status, error = self.add(
                    who=browser_headers,
                    seq=f"U{self._keys + 1}",
                )
                self.assertEqual(status, 403, error)
                self.assertEqual(error["error"]["code"], "not_same_origin")
        self.assertEqual(
            self.sql_one("SELECT COUNT(*) FROM sprint_units"), 0)

    def test_same_origin_browser_keeps_operator_authority(self):
        status, unit = self.add(
            who=("Origin: http://127.0.0.1:8800",
                 "Sec-Fetch-Site: same-origin"),
        )
        self.assertEqual(status, 201, unit)

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

    def test_planner_flavor_writes_without_interface_binding_truth(self):
        """Step 3 retires bindings as an authority source. Until the explicit
        Conductor contract lands, planner flavor is the complete write fence;
        both planner shells may write and workers still may not."""
        binding_id = self.arm_binding(planner_shell_id=9)
        self.release(binding_id)
        status, _ = self.add((PLANNER,), dev="DEV5")
        self.assertEqual(status, 201)
        status, _ = self.patch((PLANNER2,), state="working")
        self.assertEqual(status, 200)
        self.assertEqual(self.row()["state"], "working")

    def test_unbound_sprint_falls_back_to_planner_flavor_not_to_anyone(self):
        """The spec's fallback is 'the sprint doc's author', which `documents`
        has no column for. Flavor is the stand-in — and it must still exclude
        workers, or the fence is decorative before a binding is armed."""
        status, _ = self.add((PLANNER2,), dev="DEV5")
        self.assertEqual(status, 201)
        status, _err = self.patch((DEV,), branch="feat/x")
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

    def test_every_editable_field_round_trips_through_set(self):
        """Enumerated as a SET rather than one field at a time, because a
        mutation round trip found the gap: dropping `overlap` from the
        editable map reddened nothing — every test that touched a field
        asserted some OTHER property (a refusal, a timestamp) and none
        asserted the value landed. The ruling requires both add and set to
        carry the annotation; the same hole covered branch, depends_on,
        pr_number and unit_title, so all five are pinned here."""
        self.add()
        edits = {"unit_title": "board record v2", "depends_on": "U0,U3",
                 "overlap": "shares scripts/sprint.py with U5",
                 "branch": "feat/sprint-board-record", "pr_number": 613}
        for field, value in edits.items():
            status, unit = self.patch(**{field: value})
            self.assertEqual(status, 200, unit)
            self.assertEqual(self.row()[field], value,
                             f"{field} did not survive `set`")

    def test_a_clearable_field_is_cleared_only_when_said_explicitly(self):
        """Same distinction the roles get: omitted leaves it, explicit null
        clears it. `sc sprint unit set --overlap none` must be able to retract
        a stale merge-surface note, and an unrelated edit must not."""
        self.add(depends_on="U0", overlap="MUST rebase onto merged U2")
        self.patch(branch="feat/x")
        self.assertEqual(self.row()["overlap"], "MUST rebase onto merged U2")
        self.patch(overlap=None)
        self.assertIsNone(self.row()["overlap"])
        self.assertEqual(self.row()["depends_on"], "U0",
                         "clearing one field cleared its neighbour")

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
        # in_review is reached THROUGH working — the board is a machine, and
        # pending -> in_review is not one of its edges (0108).
        self.patch(seq="U1", state="working")
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

    # -- the CLI reaches the routes it claims to ------------------------------

    def test_each_verb_calls_the_method_and_path_it_claims(self):
        """The route tests prove the handlers; this proves the verbs actually
        REACH them. A wrong method or path in the CLI would ship invisibly —
        the handler tests stay green either way, and the live server is on the
        stale engine floor this sprint, so no end-to-end call can catch it.

        `set` and `state` deliberately share one route: state is refused
        server-side when it arrives beside other edits, so the separation is
        enforced where it cannot be bypassed by calling the API directly."""
        seen = []

        def spy(method, path, payload=None, idem=None):
            seen.append((method, path, payload))
            return {"units": [], "seq": "U1", "sprint_doc_id": 1,
                    "unit_title": "t", "state": "pending"}

        args = mock.Mock(sprint=1, seq="U1", title="t", dev="DEV5",
                         reviewer="REV2", depends_on="U0", overlap="note",
                         branch="b", pr=7, state="working")
        with mock.patch.object(sprint_cli, "_api", spy):
            sprint_cli.cmd_unit_add(args)
            sprint_cli.cmd_unit_set(args)
            sprint_cli.cmd_unit_state(args)
            sprint_cli.cmd_unit_list(args)
            sprint_cli.cmd_board(args)

        self.assertEqual([m for m, _p, _b in seen],
                         ["POST", "PATCH", "PATCH", "GET", "GET"])
        for _m, path, _b in seen:
            self.assertTrue(path.startswith("/api/sprint-units"), path)
        # the state verb sends state ALONE — the CLI must not smuggle the
        # other parsed arguments along and earn a 422 on every call
        self.assertEqual(seen[2][2],
                         {"sprint_doc_id": 1, "seq": "U1", "state": "working"})
        # and `set` must not send state, which would make every field edit a
        # refusal
        self.assertNotIn("state", seen[1][2])
        self.assertEqual(seen[1][2]["overlap"], "note")

    def test_the_cli_clears_a_slot_only_on_the_literal_none(self):
        """`--dev none` clears; `--dev DEV5` assigns. Mapping a missing flag
        to a clear would de-assign a role on every unrelated edit."""
        seen = []
        with mock.patch.object(
                sprint_cli, "_api",
                lambda m, p, payload=None, idem=None: (
                    seen.append(payload) or {"seq": "U1", "sprint_doc_id": 1,
                                             "unit_title": "t",
                                             "state": "pending"})):
            sprint_cli.cmd_unit_set(mock.Mock(
                sprint=1, seq="U1", title=None, dev="none", reviewer=None,
                depends_on=None, overlap=None, branch=None, pr=-1))
        self.assertIsNone(seen[0]["dev"], "'none' did not clear the dev slot")
        self.assertIsNone(seen[0]["pr_number"], "--pr -1 did not clear the PR")
        self.assertNotIn("reviewer", seen[0],
                         "an omitted --reviewer was sent as a change")

    # -- the key the CLI mints is part of the verb -----------------------------

    def test_a_failed_declaration_can_be_corrected(self):
        """flag 221, and the reason it reached main: the route tests mint a
        fresh key per call, so NO test drove the CLI's own key strategy.

        `unit-add|{sprint}|{seq}` is deterministic, and `_idempotent` stores
        whatever produce() returns INCLUDING its error statuses. So a typo'd
        --dev cached a 422 against that (sprint, seq), and the corrected retry
        — the one the error message asks for — came back 409
        idempotency_conflict for good: nothing reads expires_at, nothing
        sweeps the table, and it is snapshot content. The unit could never be
        declared again under its own seq.

        Driven through the shipped verb end to end, because that is the only
        vantage the defect is visible from."""
        with self.cli() as keys:
            with self.assertRaises(SystemExit) as typo:
                sprint_cli.cmd_unit_add(_unit_args(dev="DEV55"))
            self.assertIn("422", str(typo.exception))
            self.assertIn("no such shell", str(typo.exception))
            self.assertIsNone(self.row("U1"), "a refused declare wrote a row")
            with redirect_stdout(io.StringIO()):
                rc = sprint_cli.cmd_unit_add(_unit_args(dev="DEV5"))

        self.assertEqual(rc, 0)
        row = self.row("U1")
        self.assertIsNotNone(
            row, "the corrected declaration never landed — the failed "
                 "attempt still owns the key")
        self.assertEqual(row["dev_shell_id"], 11)
        self.assertNotEqual(keys[0], keys[1],
                            "the retry reused the failed attempt's key")

    def test_every_mutating_verb_mints_a_fresh_key_and_reads_carry_none(self):
        """The key shape, per verb, asserted on the header that actually
        leaves the CLI — `test_each_verb_calls_the_method_and_path_it_claims`
        spies `_api` and drops the key argument, which is why a deterministic
        key on ONE of three verbs shipped unnoticed.

        The prefix stays human-readable in the store; the uuid is what makes a
        failed attempt cost one attempt instead of the (sprint, seq) forever.
        Declaring twice is refused by the route's natural key instead — see
        below — so nothing is lost by not keying on it here."""
        with self.cli() as keys, redirect_stdout(io.StringIO()):
            sprint_cli.cmd_unit_add(_unit_args())
            with self.assertRaises(SystemExit):
                sprint_cli.cmd_unit_add(_unit_args())
            sprint_cli.cmd_unit_set(_unit_args(branch="feat/one"))
            sprint_cli.cmd_unit_set(_unit_args(branch="feat/two"))
            sprint_cli.cmd_unit_state(_unit_args(state="working"))
            sprint_cli.cmd_unit_state(_unit_args(state="working"))
            sprint_cli.cmd_unit_list(_unit_args())

        expected = ("unit-add|1|U1|", "unit-set|1|U1|",
                    "unit-state|1|U1|working|")
        for prefix, (first, second) in zip(expected,
                                           (keys[0:2], keys[2:4], keys[4:6])):
            for key in (first, second):
                self.assertTrue(key.startswith(prefix),
                                f"{key!r} is not a {prefix!r} key")
                suffix = key[len(prefix):]
                self.assertEqual(uuid.UUID(suffix).version, 4,
                                 f"{prefix!r} key has no uuid4 suffix")
            self.assertNotEqual(
                first, second,
                f"{prefix!r} repeats its key — one failed call locks it out")
        self.assertIsNone(keys[6], "a read sent an Idempotency-Key")

    def test_a_redeclaration_reaches_the_routes_refusal_not_the_key_cache(self):
        """The Low the same fix closes. `unit_exists` — "edit it with PATCH
        rather than declaring it twice" — is what the migration comment, the
        route docstring and the CLI all present as the answer to a double
        declare, and on the shipped path it was UNREACHABLE: the deterministic
        key answered first, with a replay of the original 201 or a 409 about
        request bodies. The code was right; nothing could get to it.

        Asserted on what the planner is TOLD, not just the status: a body
        mismatch and a duplicate unit are different mistakes with different
        repairs, and the operator acts on the sentence."""
        with self.cli(), redirect_stdout(io.StringIO()):
            sprint_cli.cmd_unit_add(_unit_args(dev="DEV5", branch="feat/real"))
            with self.assertRaises(SystemExit) as dup:
                sprint_cli.cmd_unit_add(
                    _unit_args(title="typo redeclare", dev="PLN2"))

        told = str(dup.exception)
        self.assertIn("409", told)
        self.assertIn("already has unit U1", told)
        self.assertIn("edit it with PATCH", told)
        self.assertNotIn("Idempotency-Key", told)
        live = self.row("U1")
        self.assertEqual(live["unit_title"], "board record")
        self.assertEqual(live["dev_shell_id"], 11)
        self.assertEqual(live["branch"], "feat/real")

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

    # -- malformed input is refused at the edge -------------------------------

    def test_a_field_of_the_wrong_type_is_refused_not_stored(self):
        """The board is a record of what a planner DECLARED, and the reconciler
        acts on what it is handed. A field holding something no planner could
        have typed — a PR number that is text, a branch that is a number — is
        belief corrupted quietly; SQLite's affinity will happily keep 'seven'
        in an INTEGER column. Enumerated as a SET, because a boundary that
        covers the field a bug was reported on and not its neighbours is the
        same gap one column over."""
        self.add()
        malformed = {"unit_title": 7, "depends_on": ["U1"], "overlap": 3.5,
                     "branch": 42, "pr_number": "seven"}
        for field, value in malformed.items():
            before = self.row()[field]
            status, err = self.patch(**{field: value})
            self.assertEqual(status, 422, f"{field}={value!r} was accepted")
            self.assertEqual(err["error"]["code"], "validation")
            self.assertEqual(self.row()[field], before,
                             f"{field} moved on a refused write")

    def test_a_malformed_declaration_is_refused_without_a_500(self):
        """Same set on the create path. `add` and `set` are two doors to one
        record: a numeric title reached .strip() here and returned a sanitized
        500, which tells the planner nothing about what it typed wrong."""
        for field, value in (("unit_title", 7), ("pr_number", "seven"),
                             ("branch", 42), ("sprint_doc_id", "1"),
                             ("sprint_doc_id", True), ("seq", 1)):
            status, err = self.add(**{"seq": "U8", field: value})
            self.assertEqual(status, 422, f"{field}={value!r} was accepted")
            self.assertEqual(err["error"]["code"], "validation")
            self.assertIsNone(self.row("U8"))

    def test_a_blank_is_not_a_value_and_null_is_the_way_to_clear(self):
        """An empty `branch` reads as a unit whose declared branch is the empty
        string — and U3/U4 compare that declaration against what the worktree
        holds. Retraction has a spelling already (explicit null); a blank must
        not become a second one that means something subtly different."""
        self.add(branch="feat/real")
        status, _ = self.patch(branch="   ")
        self.assertEqual(status, 422)
        self.assertEqual(self.row()["branch"], "feat/real")
        self.patch(branch=None)
        self.assertIsNone(self.row()["branch"])

    def test_a_board_cannot_be_minted_on_a_document_that_is_not_a_sprint(self):
        """flag 223. `sprint_doc_id` was validated as "a document that exists"
        and the FK carries no kind constraint, so a board could be declared on
        a SPEC — and a sprint doc and its spec are consecutive ids (59 and 58
        for this very sprint), which is a typo a planner makes at the same
        rate as any other.

        Worse than a wrong row: the board is invisible where anyone would look
        for it — participants read the sprint doc — while `sprint_units` rows
        exist and the reconciler watches them. The seq axis of this same typo
        class is already defended (PATCH never creates); this is the doc axis.

        The frozen case is a SEPARATE refusal now — see
        test_a_frozen_board_takes_no_writes. This route used to omit the
        `frozen=0` clause deliberately, with a comment parking "whether a
        frozen board stays mutable"; H-1 answers it (a frozen sprint doc is
        not live, so it is closed) and the parked comment went with the
        clause."""
        self.sql("INSERT INTO documents (document_id, kind, title, body) "
                 "VALUES (2,'spec','Worker expectation reconciler','x')")
        status, err = self.call("POST", "/api/sprint-units", (OP,),
                                {"sprint_doc_id": 2, "seq": "U1",
                                 "unit_title": "phantom board"})
        self.assertEqual(status, 422, "a board was minted on a spec")
        self.assertEqual(err["error"]["code"], "not_a_sprint_doc")
        # and it says WHICH document, because the planner's next move is to
        # retype the id and it needs to see what it hit
        self.assertIn("Worker expectation reconciler",
                      err["error"]["message"])
        self.assertIsNone(self.row("U1"))
        # a doc that is not there at all is still the OTHER refusal — the two
        # mistakes have different repairs
        status, err = self.call("POST", "/api/sprint-units", (OP,),
                                {"sprint_doc_id": 99, "seq": "U1",
                                 "unit_title": "no doc"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "no_such_sprint")
        self.assertEqual(self.add()[0], 201, "the real sprint doc was refused")

    def test_a_frozen_board_takes_no_writes(self):
        """The parked question, answered (H-1 + H-12). Freezing the sprint doc
        IS closing the sprint, so its board stops being mutable — the unit
        route and the shared liveness predicate agree instead of the route
        carrying its own carve-out.

        The refusal is asserted with the row UNWRITTEN: a 409 that still
        inserted would satisfy a status-code-only test.
        """
        self.sql("UPDATE documents SET frozen=1 WHERE document_id=1")
        status, err = self.call("POST", "/api/sprint-units", (OP,),
                                {"sprint_doc_id": 1, "seq": "U1",
                                 "unit_title": "posthumous unit"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "sprint_frozen")
        self.assertIsNone(self.row("U1"))
        # and thawing it makes the same call succeed — so the refusal is the
        # freeze and nothing else about this fixture
        self.sql("UPDATE documents SET frozen=0 WHERE document_id=1")
        self.assertEqual(self.add()[0], 201)

    def test_declaring_the_first_unit_is_not_gated_on_liveness(self):
        """The ordering H-1 creates, from the other side. A sprint is live only
        once it holds a unit, so gating THIS route on liveness would make the
        first unit — and therefore every board — undeclarable. Board first,
        then arm the binding.
        """
        self.assertEqual(
            self.sql_one("SELECT COUNT(*) FROM sprint_units"), 0,
            "the fixture starts with no board")
        self.assertEqual(self.add()[0], 201)

    def test_the_board_reads_back_in_work_order_not_lexicographic_order(self):
        """`seq` is TEXT, so `ORDER BY seq` puts U10 and U11 between U1 and
        U2. The board is a work order read top to bottom, and no test covered
        ordering at all — this sprint stops at U9, so the first sprint to
        reach ten units would have been the one to find it.

        Asserted on the RENDERED board as well as the API: the render is what
        a planner actually reads, and a projection can re-sort."""
        for seq in ("U10", "U2", "U1", "U11", "U9", "U3"):
            self.add(seq=seq, unit_title=f"unit {seq}")
        _s, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1", (OP,))
        self.assertEqual([u["seq"] for u in out["units"]],
                         ["U1", "U2", "U3", "U9", "U10", "U11"])
        buf = io.StringIO()
        with mock.patch.object(sprint_cli, "_api", return_value=out), \
                redirect_stdout(buf):
            sprint_cli.cmd_board(mock.Mock(sprint=1))
        rendered = [line.split("|")[1].strip()
                    for line in buf.getvalue().splitlines()
                    if line.startswith("| U")]
        self.assertEqual(rendered, ["U1", "U2", "U3", "U9", "U10", "U11"])

    def test_the_cli_refuses_to_title_a_unit_none(self):
        """`none` is the CLI's spelling of retraction on every role and field,
        and its help says so — but a unit cannot be untitled, so `--title
        none` stored the four letters as the title and the board read "none".
        Refusing names which of the two things the planner meant; storing it
        silently produces a board that is wrong and looks deliberate."""
        with self.cli(), redirect_stdout(io.StringIO()):
            for verb in (sprint_cli.cmd_unit_add, sprint_cli.cmd_unit_set):
                with self.assertRaises(SystemExit) as refused:
                    verb(_unit_args(title="none"))
                self.assertIn("cannot be cleared", str(refused.exception))
            self.assertIsNone(self.row("U1"), "a refused title wrote a row")
            # the guard is about the WORD, not about titles it dislikes
            sprint_cli.cmd_unit_add(_unit_args(title="none of the above"))
        self.assertEqual(self.row("U1")["unit_title"], "none of the above")

    def test_a_malformed_filter_refuses_rather_than_widening(self):
        """The failure that matters most on the read path: a filter that fails
        to parse must not fall open to EVERY board. The caller believes it
        asked about one sprint, and the reconciler acts on what it is handed —
        so silent widening hands it units from a sprint nobody asked about."""
        self.add(seq="U1")
        self.sql("INSERT INTO documents (document_id, kind, title, body) "
                 "VALUES (2,'doc','SPRINT: other','x')")
        self.sql("INSERT INTO sprint_units (sprint_doc_id, seq, unit_title) "
                 "VALUES (2,'U1','another sprint entirely')")
        status, err = self.call("GET", "/api/sprint-units?sprint_doc_id=abc",
                                (OP,))
        self.assertEqual(status, 422, err)
        self.assertEqual(err["error"]["code"], "validation")
        # and the honest filter still answers with ONLY that sprint
        status, out = self.call("GET", "/api/sprint-units?sprint_doc_id=1",
                                (OP,))
        self.assertEqual(status, 200)
        self.assertEqual([u["sprint_doc_id"] for u in out["units"]], [1])


class BoardTransitionRouteTest(_BoardCase):
    """The transition machine AS THE ROUTE SERVES IT (spec #76 H-11).

    The matrix walk against both enforcement layers lived in the retired
    Interface suite (test_interface_transitions.py, gone with the wake
    machine). What only THIS vantage sees is the layer a planner actually
    meets: whether the API answers a refused move with a status and a sentence
    it can act on, or with a 500 carrying an IntegrityError — and whether the
    refused move left the row alone.
    """

    def test_every_legal_edge_is_served(self):
        """The permissive half. A machine tested only by what it refuses
        passes just as well when it refuses everything, and a board that
        cannot reach in_review is worse than one that can be walked back."""
        for path in (("working", "in_review", "working", "blocked", "working",
                      "merged"),
                     ("working", "in_review", "blocked", "working",
                      "in_review", "merged"),
                     ("working", "blocked", "cancelled"),
                     ("cancelled",),
                     ("working", "in_review", "cancelled")):
            with self.subTest(path=path):
                seq = "U" + "".join(s[0] for s in path)
                self.add(seq=seq)
                for state in path:
                    status, out = self.patch(seq=seq, state=state)
                    self.assertEqual(status, 200, out)
                    self.assertEqual(out["state"], state)
                self.assertEqual(self.row(seq)["state"], path[-1])

    def test_an_illegal_move_is_refused_by_name_and_changes_nothing(self):
        self.add(seq="U1")
        before = self.row("U1")
        status, err = self.patch(seq="U1", state="in_review")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "illegal_unit_transition")
        # The message has to name the move AND what IS reachable — "illegal
        # transition" alone sends the planner back to the migration to find
        # out what to type instead.
        self.assertIn("pending -> in_review", err["error"]["message"])
        self.assertIn("working", err["error"]["message"])
        after = self.row("U1")
        self.assertEqual(after["state"], "pending")
        self.assertEqual(after["state_changed_at"], before["state_changed_at"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_terminal_is_terminal_through_the_route(self):
        """Both terminals, every exit, through the API — and the refusal has
        to teach the remedy, because "no" without "declare a successor unit"
        is what makes an operator go looking for a --force flag."""
        for terminal in ("merged", "cancelled"):
            seq = f"U{terminal[:3]}"
            self.add(seq=seq)
            self.patch(seq=seq, state="working")
            self.assertEqual(
                self.patch(seq=seq, state=terminal)[0], 200)
            for target in ("pending", "working", "in_review", "blocked",
                           "merged", "cancelled"):
                if target == terminal:
                    continue      # a same-state re-assert is a legal no-op
                with self.subTest(edge=f"{terminal}->{target}"):
                    status, err = self.patch(seq=seq, state=target)
                    self.assertEqual(status, 409, err)
                    self.assertEqual(err["error"]["code"],
                                     "illegal_unit_transition")
                    self.assertIn("SUCCESSOR UNIT", err["error"]["message"])
                    self.assertEqual(self.row(seq)["state"], terminal)

    def test_a_terminal_unit_still_accepts_its_own_state(self):
        """A re-assert is how an idempotent retry lands after an ambiguous
        timeout. Refusing it would turn "the response was lost" into "the
        sprint cannot be closed"."""
        self.add(seq="U1")
        self.patch(seq="U1", state="working")
        self.patch(seq="U1", state="merged")
        moved = self.row("U1")["state_changed_at"]
        status, out = self.patch(seq="U1", state="merged")
        self.assertEqual(status, 200, out)
        self.assertEqual(self.row("U1")["state"], "merged")
        self.assertEqual(self.row("U1")["state_changed_at"], moved,
                         "a no-op re-assert restamped the clock")

    def test_one_pr_belongs_to_one_unit(self):
        """H-13's board half. Two units claiming one PR makes every structured
        answer to "which unit is this event about" depend on read order."""
        self.add(seq="U1", pr_number=658)
        self.add(seq="U2")
        status, err = self.patch(seq="U2", pr_number=658)
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "pr_already_claimed")
        self.assertIn("U1", err["error"]["message"])
        self.assertIsNone(self.row("U2")["pr_number"])
        # Declaring the collision is refused the same way an edit is — the
        # constraint is the board's, not one route's.
        status, err = self.add(seq="U3", pr_number=658)
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "pr_already_claimed")
        self.assertIsNone(self.row("U3"))
        # and the claim is releasable, so a mis-typed PR is not permanent
        self.assertEqual(self.patch(seq="U1", pr_number=None)[0], 200)
        self.assertEqual(self.patch(seq="U2", pr_number=658)[0], 200)
        self.assertEqual(self.row("U2")["pr_number"], 658)

    def test_two_units_may_both_have_no_pr(self):
        """The index is PARTIAL. A board's normal state is many units with no
        PR yet, and a plain UNIQUE would have refused the second one."""
        self.assertEqual(self.add(seq="U1")[0], 201)
        self.assertEqual(self.add(seq="U2")[0], 201)
        self.assertEqual(self.add(seq="U3")[0], 201)

    def test_review_head_is_recorded_and_projected(self):
        """H-14: the reviewer's verdict head becomes a column the record
        carries, not a sentence in a message body."""
        self.add(seq="U1")
        self.patch(seq="U1", state="working")
        self.patch(seq="U1", state="in_review")
        status, out = self.patch(seq="U1", review_head="963dc8c3")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["review_head"], "963dc8c3")
        self.assertEqual(self.row("U1")["review_head"], "963dc8c3")
        # PRESENCE, not correctness — the route stores what the planner read
        # off the verdict and judges none of it (decision #76).
        self.assertEqual(
            self.patch(seq="U1", review_head="not-a-sha")[0], 200)
        # blank is not a value; null retracts
        self.assertEqual(self.patch(seq="U1", review_head="  ")[0], 422)
        self.assertEqual(self.patch(seq="U1", review_head=None)[0], 200)
        self.assertIsNone(self.row("U1")["review_head"])

    def test_review_head_cannot_ride_a_state_move(self):
        """`state_changed_at` has exactly one writer, and H-14 does not buy a
        second door into the state column."""
        self.add(seq="U1")
        status, err = self.patch(seq="U1", state="working",
                                 review_head="963dc8c3")
        self.assertEqual(status, 422, err)
        self.assertEqual(err["error"]["code"], "state_moves_alone")
        self.assertEqual(self.row("U1")["state"], "pending")


class LiveUpgradeTest(_BoardCase):
    """Migration 0098 as the thing under test, rather than as a file that
    happens to run on the way to a fixture.

    Every test above builds its DB from `schema.sql` — the current baseline,
    which already carries `sprint_units`. Under that fixture the migration
    could be DELETED and all of them would stay green: fresh installs would
    keep working, and every existing fork — including this repo's own live
    DB — would upgrade into a board that is not there. So these start from the
    DB the migration actually meets and drive the REAL runner across it,
    ledger and outer-transaction stripping included.
    """

    build = staticmethod(build_pre_migration_db)

    def upgrade(self):
        con = sqlite3.connect(self.db_path)
        try:
            for migration in BOARD_MIGRATIONS:
                migrate.apply(con, migration)
        finally:
            con.close()

    def shape(self, db_path):
        """A table's structure as the DB itself reports it — columns with
        their types, defaults and nullability, the FK targets, and the
        indexes with their columns and uniqueness."""
        con = sqlite3.connect(db_path)
        try:
            cols = con.execute("PRAGMA table_info(sprint_units)").fetchall()
            fks = sorted(con.execute(
                "PRAGMA foreign_key_list(sprint_units)").fetchall())
            idx = []
            for _s, name, unique, _o, _p in con.execute(
                    "PRAGMA index_list(sprint_units)").fetchall():
                cols_of = [r[2] for r in con.execute(
                    f"PRAGMA index_info({name})").fetchall()]
                idx.append((name, unique, cols_of))
            return {"columns": cols, "fks": fks, "indexes": sorted(idx)}
        finally:
            con.close()

    def test_the_migration_lands_the_board_on_a_db_that_had_none(self):
        """Absence first: a fixture that already held the table would make
        every assertion below vacuous — which is exactly the defect this test
        exists to close, one layer down."""
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM sqlite_master WHERE "
                            "type='table' AND name='sprint_units'"
                            ).fetchone()[0], 0,
                "the fixture already had a board — the upgrade proves nothing")
        finally:
            con.close()

        self.upgrade()

        shape = self.shape(self.db_path)
        names = [c[1] for c in shape["columns"]]
        # BOTH roles: the reviewer column is the gap that blocked everything,
        # and an upgrade that landed only the dev would ship the original
        # defect to every fork while a fresh install stayed correct.
        self.assertIn("dev_shell_id", names)
        self.assertIn("reviewer_shell_id", names)
        self.assertIn("overlap", names)
        # the reconciler's per-tick read is (sprint_doc_id, state)
        self.assertIn(("idx_sprint_units_live", 0, ["sprint_doc_id", "state"]),
                      shape["indexes"])
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM schema_migrations WHERE "
                            "filename=?", (MIGRATION.name,)).fetchone()[0], 1,
                "the runner did not stamp the ledger — it would re-run")
        finally:
            con.close()

    def test_the_migrated_board_enforces_the_constraints_it_promises(self):
        """Structure is not behaviour: a table can arrive with the right
        columns and no CHECK, and the first thing that notices is a board
        holding a state nothing can interpret."""
        self.upgrade()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("INSERT INTO sprint_units (sprint_doc_id, seq, "
                        "unit_title) VALUES (1,'U1','board record')")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE sprint_units SET state='done'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO sprint_units (sprint_doc_id, seq, "
                            "unit_title) VALUES (1,'U1','declared twice')")
            self.assertEqual(
                con.execute("SELECT state FROM sprint_units").fetchone()[0],
                "pending")
        finally:
            con.close()

    def test_the_migrated_board_serves_the_api_and_its_fence(self):
        """The end the operator sees: after the upgrade the board takes a
        planner's declaration and refuses a worker's — through the same routes,
        against a table no `schema.sql` in this DB ever created."""
        self.upgrade()
        self.arm_binding()
        status, unit = self.add((PLANNER,), dev="DEV5", reviewer="REV2")
        self.assertEqual(status, 201, unit)
        self.assertEqual(unit["reviewer_shortname"], "REV2")
        status, _ = self.patch((DEV,), state="merged")
        self.assertEqual(status, 403)
        self.assertEqual(self.row()["state"], "pending")

    def test_the_upgrade_and_the_baseline_build_the_same_table(self):
        """Two populations run this code: forks that install from `schema.sql`
        and forks that upgrade through the migration. If the two definitions
        drift, the same query answers differently in each — and the drift is
        invisible to both, because each only ever builds its own way."""
        self.upgrade()
        installed = self.tmp / "installed.db"
        build_engine_db(installed)
        self.assertEqual(self.shape(self.db_path), self.shape(installed))

    def test_the_migration_is_inert_when_the_board_is_already_there(self):
        """A fresh build runs `schema.sql` and THEN every migration, so 0098
        always meets a table that already exists — and `./sc update` can re-run
        an unstamped file against a DB whose ledger was rebuilt. Neither may
        error, and neither may disturb what the board already holds."""
        installed = self.tmp / "installed.db"
        build_engine_db(installed)
        con = sqlite3.connect(installed)
        try:
            con.execute("INSERT INTO sprint_units (sprint_doc_id, seq, "
                        "unit_title, state) VALUES (1,'U1','live',"
                        "'in_review')")
            con.commit()
            migrate.apply(con, MIGRATION)
            con.executescript(MIGRATION.read_text())      # and again, bare
            self.assertEqual(
                con.execute("SELECT seq, state FROM sprint_units"
                            ).fetchall(), [("U1", "in_review")])
        finally:
            con.close()


class RemovedWakeVerbsTest(unittest.TestCase):
    def test_removed_verbs_are_absent_from_help_and_fail_with_conductor_bridge(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            sprint_cli.main(["--help"])
        help_text = out.getvalue()
        for verb in ("action", "arm", "disarm", "status", "alerts", "retry"):
            self.assertNotIn(f"{{{verb}", help_text)
            with self.subTest(verb=verb):
                with self.assertRaises(SystemExit) as raised:
                    sprint_cli.main([verb])
                self.assertIn("arrives with Conductor", str(raised.exception))
                self.assertIn("./sc run <shortname>", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
