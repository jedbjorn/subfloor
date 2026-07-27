#!/usr/bin/env python3
"""The sprint directive is STAMPED AT BOOT (spec doc 58 U9, feature 27).

The boot render selects the role-specific participant skill from the structured
record, independent of task wording.

WHY MOST OF THIS FILE ASSERTS ABSENCE. A test that only checks the directive
APPEARS passes for a renderer that emits the section unconditionally — which
would be a worse bug than the one being fixed, since it would tell every shell
in the fork it was in a sprint. So the absences are the real subject here:
no non-terminal row, only-merged rows, a frozen doc, a CLOSED doc, and the
tables missing entirely.

AND EVERY ABSENCE CARRIES A POSITIVE CONTROL. An absence asserted against an
artifact you have not proven populated is trivially true — no-op the emitter
and every absence test in a naive version of this file still passes. So each
one flips exactly the fact under test on the SAME db and proves the section
comes back. That pairing is the point; do not drop the control half.

Run:
    python3 tests/test_boot_sprint_directive.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"

sys.path.insert(0, str(ENGINE / "render"))
import compose  # noqa: E402
import sprint_units  # noqa: E402

DEV_SHELL = 11
REVIEWER_SHELL = 8
PLANNER_SHELL = 9
BYSTANDER_SHELL = 6          # holds no sprint role in any test

ACTIVE_BODY = "# SPRINT: test\nstatus: ACTIVE\n\nprose the record never touches\n"
CLOSED_BODY = "# SPRINT: test\nstatus: CLOSED\n\nprose the record never touches\n"


def build_db(path: Path) -> sqlite3.Connection:
    """Engine db from the tracked baseline: schema.sql THEN every migration.

    `sprint_units` and `sprint_planner_bindings` are both in schema.sql, so the
    shapes this renderer reads were once available from the baseline alone —
    but "the sources" for a render are schema + migrations (what `./sc rebuild`
    and the hermetic render-check both build), and compose_boot legitimately
    reads migration-added columns (0100's `shells.lns_curated_at`). Replaying
    them keeps this fixture the same floor the real render runs on.
    """
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for m in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(m.read_text())
    # A migration turns FK enforcement on for its own replay; this fixture was
    # written against the schema-only default (off) and seeds rows in an order
    # that leans on it. Replaying migrations is meant to add COLUMNS here, not
    # to change which constraints this fixture is checked against.
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO users (user_id, username) VALUES (1, 'Jed')")
    for shell_id, shortname, flavor in (
            (DEV_SHELL, "DEV5", "dev"),
            (REVIEWER_SHELL, "REV2", "reviewer"),
            (PLANNER_SHELL, "PLN1", "planner"),
            (BYSTANDER_SHELL, "DEV4", "dev")):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
            "role, mandate, system_prompt, current_state) "
            "VALUES (?,?,?,?,'r','m','sp','cs')",
            (shell_id, shortname, shortname, flavor))
    con.commit()
    return con


def add_doc(con, doc_id: int, title: str = "SPRINT: watchdog",
            body: str = ACTIVE_BODY, frozen: int = 0) -> None:
    con.execute(
        "INSERT INTO documents (document_id, title, body, kind, frozen) "
        "VALUES (?,?,?,'doc',?)", (doc_id, title, body, frozen))
    con.commit()


def add_unit(con, doc_id: int, seq: str, *, dev=None, reviewer=None,
             state: str = "working", title: str = "the unit") -> None:
    con.execute(
        "INSERT INTO sprint_units (sprint_doc_id, seq, unit_title, "
        "dev_shell_id, reviewer_shell_id, state) VALUES (?,?,?,?,?,?)",
        (doc_id, seq, title, dev, reviewer, state))
    con.commit()


def arm_binding(con, doc_id: int, planner: int, *, released: bool = False,
                session_id: int = 1, generation: int = 1) -> int:
    """A binding row. The FKs to interface_sessions/interface_generations are
    satisfied by seeding those first; `released` is what the spec's original
    wording would have filtered on and this unit deliberately does not."""
    con.execute(
        "INSERT OR IGNORE INTO interface_generations (shell_id, generation) "
        "VALUES (?,?)", (planner, generation))
    con.execute(
        "INSERT OR IGNORE INTO interface_sessions (session_id, shell_id, "
        "generation, occupancy, lifecycle) VALUES (?,?,?,'occupied','idle')",
        (session_id, planner, generation))
    cur = con.execute(
        "INSERT INTO sprint_planner_bindings (sprint_doc_id, planner_shell_id, "
        "session_id, shell_id, generation, released_at) "
        "VALUES (?,?,?,?,?,?)",
        (doc_id, planner, session_id, planner, generation,
         "2026-07-26 01:00:00" if released else None))
    con.commit()
    return cur.lastrowid


class SprintDirectiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "shell_db.db"
        self.con = build_db(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.con.close)

    def render(self, shell_id: int) -> str:
        return compose.render_sprint_directive(self.con, shell_id)

    # ── the roles resolve, and each names its own skill and slot ────────────

    def test_dev_gets_the_dev_slot_and_its_unit(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL, reviewer=REVIEWER_SHELL,
                 title="boot-stamped directive")
        out = self.render(DEV_SHELL)
        self.assertIn("DEV", out)
        self.assertIn("`sprint_dev` skill", out)
        self.assertIn("`U9`", out)
        self.assertIn("boot-stamped directive", out)
        self.assertIn("doc 59", out)

    def test_reviewer_gets_the_reviewer_slot(self):
        # PLN1's named mutation targets exactly this: make the resolution
        # ignore reviewer_shell_id and this test must go red. That column's
        # absence from spec_tasks is what made a dead reviewer undetectable
        # and justified the whole feature, so it is asserted on its own and
        # not folded into the dev case.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U3", dev=DEV_SHELL, reviewer=REVIEWER_SHELL)
        out = self.render(REVIEWER_SHELL)
        self.assertIn("REVIEWER", out)
        self.assertIn("`sprint_review` skill", out)
        self.assertIn("`U3`", out)

    def test_planner_gets_the_orchestration_skill(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL)
        out = self.render(PLANNER_SHELL)
        self.assertIn("PLANNER", out)
        self.assertIn("`sprint_orchestration` skill", out)

    def test_planner_gets_nothing_before_the_board_has_rows(self):
        # A sprint's very first boot: the binding exists, no unit rows do.
        # Correct — the planner is the shell about to CREATE those rows and
        # does not need to be told what it is doing. Decided, not discovered
        # (PLN1 #2263).
        add_doc(self.con, 59)
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertEqual(self.render(PLANNER_SHELL), "")
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)          # control
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))

    def test_planner_resolves_from_a_RELEASED_binding_too(self):
        # The load-bearing departure from the spec's wording. Arming requires
        # an already-`occupied` session, so the generation being booted cannot
        # have a binding yet and the previous one is released when its
        # generation ends. Filtering on armed would render the planner's
        # directive only when recovery lagged — the sprint doc is what says
        # the sprint is live, not the binding.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL, released=True)
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))

    def test_only_the_latest_binding_names_the_planner(self):
        # A sprint re-bound to a different planner must not keep telling the
        # old one it is the planner.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL, released=True,
                    session_id=1, generation=1)
        arm_binding(self.con, 59, 10, session_id=2, generation=1)
        self.assertEqual(self.render(PLANNER_SHELL), "")
        self.assertIn("PLANNER", self.render(10))   # positive control

    def test_reviewer_of_several_units_gets_one_line_naming_all(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U3", reviewer=REVIEWER_SHELL)
        add_unit(self.con, 59, "U4", reviewer=REVIEWER_SHELL)
        out = self.render(REVIEWER_SHELL)
        self.assertIn("`U3`", out)
        self.assertIn("`U4`", out)
        self.assertEqual(out.count("`sprint_review` skill"), 1)

    # ── two roles / two sprints — every one named, never a silent pick ──────

    def test_both_roles_are_named_and_neither_is_picked(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        add_unit(self.con, 59, "U3", reviewer=DEV_SHELL)
        arm_binding(self.con, 59, DEV_SHELL)
        out = self.render(DEV_SHELL)
        self.assertIn("`sprint_dev` skill", out)
        self.assertIn("`sprint_review` skill", out)
        self.assertIn("`sprint_orchestration` skill", out)
        self.assertIn("Act on every role listed above", out)

    def test_a_shell_in_two_sprints_sees_both(self):
        add_doc(self.con, 59, title="SPRINT: watchdog")
        add_doc(self.con, 61, title="SPRINT: the other one")
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        add_unit(self.con, 61, "U2", dev=DEV_SHELL)
        out = self.render(DEV_SHELL)
        self.assertIn("doc 59", out)
        self.assertIn("doc 61", out)
        self.assertIn("SPRINT: the other one", out)
        self.assertIn("Act on every role listed above", out)

    def test_the_four_participant_rules_are_in_the_section_itself(self):
        # They render as prose because the failure mode IS a skill body that
        # never loads — a pointer to the skill would reproduce the bug.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        out = self.render(DEV_SHELL)
        self.assertIn("File findings, flags, verdicts", out)
        self.assertIn("--kind result --sprint <doc-id>", out)
        self.assertIn("send a partial", out)
        self.assertIn("Nothing found is a result. Send it.", out)

    # ── the absences, each with its positive control ────────────────────────

    def test_no_sprint_row_renders_nothing(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL, reviewer=REVIEWER_SHELL)
        self.assertEqual(self.render(BYSTANDER_SHELL), "")
        self.assertNotEqual(self.render(DEV_SHELL), "")     # control

    def test_terminal_units_retire_the_WORKERS_but_not_the_planner(self):
        # The predicate split (flag_id 231, spec doc 58 "The boot directive").
        # A dev whose every unit is merged or cancelled is done. THE PLANNER IS
        # NOT: every step of close-out — the conformance pass, `status: CLOSED`,
        # the freeze, the participant messages, the watch sweep, the sprint
        # report, the flag and roadmap bookkeeping — happens in exactly this
        # state, all units merged and the doc not yet frozen. The first cut of
        # this test asserted the planner rendered "" here and called the sprint
        # "over"; it is not over, and the assertion pinned the defect.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL, state="merged")
        add_unit(self.con, 59, "U8", dev=DEV_SHELL, state="cancelled")
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertEqual(self.render(DEV_SHELL), "")
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))
        # control: the SAME rows, one moved off a terminal state — the dev's
        # directive comes back, so the absence above was about the state and
        # not about an empty board
        self.con.execute("UPDATE sprint_units SET state='working' WHERE seq='U9'")
        self.con.commit()
        self.assertNotEqual(self.render(DEV_SHELL), "")
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))

    def test_the_freeze_alone_retires_the_planner(self):
        # The other half of the split: what DOES end the planner's directive is
        # the freeze, which is the participant skills' revocation predicate.
        # Terminal units + frozen doc, one field apart from the test above.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL, state="merged")
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))   # control first
        self.con.execute("UPDATE documents SET frozen=1 WHERE document_id=59")
        self.con.commit()
        self.assertEqual(self.render(PLANNER_SHELL), "")

    def test_frozen_doc_renders_nothing(self):
        add_doc(self.con, 59, frozen=1)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertEqual(self.render(DEV_SHELL), "")
        self.assertEqual(self.render(PLANNER_SHELL), "")
        # control: same rows, doc unfrozen
        self.con.execute("UPDATE documents SET frozen=0 WHERE document_id=59")
        self.con.commit()
        self.assertNotEqual(self.render(DEV_SHELL), "")
        self.assertNotEqual(self.render(PLANNER_SHELL), "")

    def test_liveness_is_structured_and_the_body_prose_is_never_read(self):
        # The inverse of an absence test, and it pins a ruling rather than a
        # behaviour: liveness is `frozen = 0` + a non-terminal unit row, NOT
        # the body's `status:` line. Regex-matching ACTIVE-ness out of prose is
        # part of the structural gap this feature closes (flag_id 213 removed
        # exactly that from the reconciler's trigger), and reintroducing it
        # here would let the boot render and the reconciler disagree about
        # whether a sprint is live the moment someone reformats a line.
        # So: prose says CLOSED, the record says otherwise, and the RECORD wins.
        add_doc(self.con, 59, body=CLOSED_BODY)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertIn("DEV", self.render(DEV_SHELL))
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))
        # and a body with no status line at all changes nothing either
        self.con.execute("UPDATE documents SET body='' WHERE document_id=59")
        self.con.commit()
        self.assertIn("DEV", self.render(DEV_SHELL))
        self.assertIn("PLANNER", self.render(PLANNER_SHELL))

    def test_missing_tables_render_nothing_instead_of_breaking_the_boot(self):
        # Not hypothetical: as this unit was written the repo's own running
        # floor had applied migrations only through 0096, so the live db had
        # `sprint_planner_bindings` and NO `sprint_units` at all. compose reads
        # that db at every launch of every shell, so an unguarded SELECT here
        # is a broken boot for the whole fork, not a missing section.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        arm_binding(self.con, 59, PLANNER_SHELL)
        self.assertNotEqual(self.render(DEV_SHELL), "")       # control first
        self.assertNotEqual(self.render(PLANNER_SHELL), "")

        self.con.execute("DROP TABLE sprint_units")
        self.con.commit()
        self.assertEqual(self.render(DEV_SHELL), "")
        # the planner goes quiet too — its predicate needs a unit ROW, and the
        # table those rows live in is the one that just vanished
        self.assertEqual(self.render(PLANNER_SHELL), "")

        self.con.execute("DROP TABLE sprint_planner_bindings")
        self.con.commit()
        self.assertEqual(self.render(PLANNER_SHELL), "")
        self.assertEqual(self.render(DEV_SHELL), "")

    def test_a_BROKEN_query_raises_where_a_missing_table_degrades(self):
        # flag_id 233. The degrade above is load-bearing but its justification
        # is TEMPORARY — it evaporates the moment the floor applies 0098 —
        # whereas a bare `except OperationalError` would be PERMANENT. Past
        # that point the only thing such a catch could still swallow is a
        # genuine fault: a later migration renaming a column, a typo. It would
        # revert the whole fleet to pre-U9 behaviour on a section the render
        # itself labels MANDATORY, with no error, no stderr and no failing
        # test — which is the same shape as the bug this unit exists to close,
        # one layer down.
        #
        # No test above this one can tell the two cases apart: DROPping the
        # table satisfies "absent" and "broken" alike. This one separates them.
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        self.assertNotEqual(self.render(DEV_SHELL), "")        # control first
        # the table is PRESENT and a column the query reads is gone — exactly
        # what a later migration or a typo leaves behind
        self.con.execute(
            "ALTER TABLE sprint_units RENAME COLUMN state TO unit_state")
        self.con.commit()
        with self.assertRaises(sqlite3.OperationalError):
            self.render(DEV_SHELL)

    # ── the terminal set is the schema's, not a private opinion ─────────────

    def test_the_state_vocabulary_is_pinned_to_the_schema_check_exactly(self):
        # A state ADDED to sprint_units that this renderer does not know about
        # is the silent failure: an unknown state reads as non-terminal, which
        # errs toward telling a shell it is still in a sprint.
        #
        # So this asserts EQUALITY, both directions (flag_id 232). Its first
        # cut asserted TERMINAL <= allowed — a subset, which can only fail on a
        # REMOVAL, i.e. the one direction the comment above says is not the
        # risk. REV2's M10 mutation (adding 'archived' to the CHECK) survived
        # it with 19/19 green: one leg of eleven lived, and it was this one.
        ddl = self.con.execute(
            "SELECT sql FROM sqlite_master WHERE name='sprint_units'"
        ).fetchone()[0]
        # Bounded to the clause. Splitting on "CHECK (state IN" and taking the
        # REST of the DDL swept up the rest of the column list, so `allowed`
        # actually contained 'now' — from DEFAULT (datetime('now')).
        clause = re.search(r"CHECK\s*\(state IN\s*\(([^)]*)\)", ddl)
        self.assertIsNotNone(clause, "the state CHECK clause moved — re-anchor")
        allowed = set(re.findall(r"'(\w+)'", clause.group(1)))
        self.assertNotIn("now", allowed)        # the parse stayed in the clause
        self.assertEqual(set(sprint_units.UNIT_STATES), allowed)
        self.assertEqual(set(compose.TERMINAL_UNIT_STATES),
                         {"merged", "cancelled"})
        # the complement is the live half, named in full: a new schema state
        # lands HERE and turns this red, which is the addition case.
        self.assertEqual(allowed - set(compose.TERMINAL_UNIT_STATES),
                         {"pending", "working", "in_review", "blocked"})
        self.assertIs(
            compose.TERMINAL_UNIT_STATES,
            sprint_units.TERMINAL_UNIT_STATES,
        )


class BootDocIntegrationTest(unittest.TestCase):
    """The section has to reach the rendered document, in the right place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "shell_db.db"
        self.con = build_db(self.path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.con.close)

    def boot(self, shell_id: int) -> str:
        shell = self.con.execute(
            "SELECT * FROM shells WHERE shell_id=?", (shell_id,)).fetchone()
        user = self.con.execute(
            "SELECT * FROM users WHERE user_id=1").fetchone()
        return compose.compose_boot(self.con, shell, user, "0001", 1)

    def test_the_section_lands_in_the_boot_doc_under_active_session(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        doc = self.boot(DEV_SHELL)
        self.assertIn("## SPRINT DIRECTIVE — MANDATORY", doc)
        # ahead of everything the render appends after ACTIVE SESSION — this
        # is standing orders, not an appendix
        self.assertLess(doc.index("## ACTIVE SESSION"),
                        doc.index("## SPRINT DIRECTIVE"))
        self.assertLess(doc.index("## SPRINT DIRECTIVE"),
                        doc.index("## IDENTITY"))

    def test_a_shell_with_no_sprint_work_gets_no_section_at_all(self):
        add_doc(self.con, 59)
        add_unit(self.con, 59, "U9", dev=DEV_SHELL)
        doc = self.boot(BYSTANDER_SHELL)
        self.assertNotIn("SPRINT DIRECTIVE", doc)
        self.assertIn("## IDENTITY", doc)                  # control: it rendered
        self.assertIn("## SPRINT DIRECTIVE", self.boot(DEV_SHELL))

    def test_a_boot_still_renders_when_the_board_table_is_absent(self):
        self.con.execute("DROP TABLE sprint_units")
        self.con.commit()
        doc = self.boot(DEV_SHELL)
        self.assertNotIn("SPRINT DIRECTIVE", doc)
        self.assertIn("## IDENTITY", doc)
        self.assertIn("## STATUS", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
