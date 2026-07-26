#!/usr/bin/env python3
"""The assignment change notice — the retired-feature-#29 emitter (spec doc
58 "Assignment change notice", feature 27, sprint doc 59 U7).

The unit's whole claim is a SET: when a role on a sprint unit changes, the
shells told are exactly the shell newly named, the shell it replaced, and the
counterpart role on that unit — read off the record, with no subscription and
no reader log, because the audit that retired #29 found those are exactly the
shells who need telling. So the test that matters asserts the RECIPIENT SET,
not a row count: "three rows" is satisfied just as happily by the wrong three,
and any rule broader than the record's own columns is the defect this emitter
exists to avoid rather than a generalisation of it. The board here therefore
carries a SECOND unit whose shells must stay silent — a single-unit fixture
cannot tell "notify the record's parties" from "notify the board".

The other half is what must NOT emit. A field edit, a state move, a role
re-asserted to the shell already in it, a closed sprint, and every refused or
rejected write tell nobody — the sprint-52 incident was a column that CHANGED
under two live workers, and noise on the paths that did not change is exactly
the cost that retired #29 in the first place.

Run:
    python3 tests/test_sprint_assignment_notice.py
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import interface_wake  # noqa: E402
from test_sprint_board_record import (  # noqa: E402
    DEV, OP, PLANNER, _BoardCase)

REV1, REV2, DEV5, DEV6, PLN1, PLN2 = 7, 8, 11, 12, 9, 10


class AssignmentNoticeTest(_BoardCase):
    """One ACTIVE sprint, two units. U1 is the unit under test; U2 exists so
    that a rule reading the BOARD instead of the RECORD has somewhere to leak
    to."""

    def setUp(self):
        super().setUp()
        for sid, short, flavor, key in ((REV1, "REV1", "reviewer", "rev1tok"),
                                        (DEV6, "DEV6", "dev", "dev6tok")):
            self.sql(
                "INSERT INTO shells (shell_id, display_name, shortname, "
                "flavor, mandate, system_prompt, user_id, api_key, is_shared, "
                "has_identity, bootstrapped) VALUES (?,?,?,?,'test','sp',1,?,"
                "0,1,1)", (sid, f"S{sid}", short, flavor, key))
        self.add(seq="U1", dev=DEV5, reviewer=REV1)
        self.add(seq="U2", dev=DEV6, reviewer=PLN2)
        self.clear_messages()

    # -- helpers -------------------------------------------------------------

    def clear_messages(self):
        self.sql("DELETE FROM shell_messages")

    def notices(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM shell_messages ORDER BY message_id")]
        finally:
            con.close()

    def told(self):
        return sorted(m["to_shell_id"] for m in self.notices())

    # -- the spec's verification row -----------------------------------------

    def test_reviewer_reassignment_notifies_exactly_three(self):
        """spec doc 58: "assignment change notifies exactly three | reassign a
        reviewer | notify by any broader rule -> extra rows, red"."""
        status, _ = self.patch(seq="U1", reviewer=REV2)
        self.assertEqual(status, 200)
        # The newly named, the one it replaced, and the counterpart on THIS
        # unit. U2's dev and reviewer are on the same board and hear nothing.
        self.assertEqual(self.told(), [REV1, REV2, DEV5])
        self.assertEqual(len(self.notices()), 3)

    def test_the_notice_names_the_change_and_the_new_roster(self):
        self.patch(seq="U1", reviewer=REV2)
        bodies = {m["body"] for m in self.notices()}
        # ONE body, delivered to each party — three phrasings of one fact is
        # the drift this spec keeps closing.
        self.assertEqual(len(bodies), 1)
        body = bodies.pop()
        self.assertIn("unit U1", body)
        self.assertIn("reviewer REV1 -> REV2", body)
        self.assertIn("Now: dev DEV5, reviewer REV2", body)

    # -- what must not emit ---------------------------------------------------

    def test_a_role_reasserted_to_the_same_shell_tells_nobody(self):
        """A planner re-typing the value already in the column is not the
        sprint-52 incident, and must not spend the noise budget on it."""
        status, _ = self.patch(seq="U1", reviewer=REV1)
        self.assertEqual(status, 200)
        self.assertEqual(self.notices(), [])

    def test_a_field_edit_tells_nobody(self):
        status, _ = self.patch(seq="U1", branch="feat/x", pr_number=616)
        self.assertEqual(status, 200)
        self.assertEqual(self.notices(), [])

    def test_a_state_move_tells_nobody(self):
        status, _ = self.patch(seq="U1", state="working")
        self.assertEqual(status, 200)
        self.assertEqual(self.notices(), [])

    def test_a_closed_sprint_emits_nothing_but_still_writes_the_board(self):
        """The ACTIVE gate is on the NOTICE, never on the record. A planner
        must still be able to correct a closed sprint's board."""
        self.sql("UPDATE documents SET body=? WHERE document_id=1",
                 ("# SPRINT: test\nstatus: CLOSED\n",))
        status, _ = self.patch(seq="U1", reviewer=REV2)
        self.assertEqual(status, 200)
        self.assertEqual(self.row("U1")["reviewer_shell_id"], REV2)
        self.assertEqual(self.notices(), [])

    def test_a_refused_write_tells_nobody(self):
        """A worker cannot move the board (U1's rule), so it cannot make the
        board TALK either — a 403 that still emitted would tell three shells
        about a change that did not happen."""
        status, _ = self.patch((DEV,), seq="U1", reviewer=REV2)
        self.assertEqual(status, 403)
        self.assertEqual(self.notices(), [])

    def test_an_edit_to_a_unit_that_does_not_exist_tells_nobody(self):
        status, _ = self.patch(seq="U9", reviewer=REV2)
        self.assertEqual(status, 404)
        self.assertEqual(self.notices(), [])

    def test_a_redeclared_unit_tells_nobody(self):
        """POST is not an upsert: the 409 leaves the board unchanged, so the
        notice must not ride the request that was rejected."""
        status, _ = self.add(seq="U1", dev=DEV6, reviewer=REV2)
        self.assertEqual(status, 409)
        self.assertEqual(self.notices(), [])

    # -- the rest of the set ---------------------------------------------------

    def test_declaring_a_unit_notifies_the_roles_it_names(self):
        """An INSERT that names a role IS an assignment change; the two named
        shells are each other's counterpart, so it is two rows, not three."""
        self.add(seq="U3", dev=DEV5, reviewer=REV2)
        self.assertEqual(self.told(), [REV2, DEV5])

    def test_declaring_an_unassigned_unit_tells_nobody(self):
        self.add(seq="U3")
        self.assertEqual(self.notices(), [])

    def test_clearing_a_role_notifies_the_departing_shell(self):
        """The shell newly named is nobody, which is a real board state — the
        one it replaced still has to hear that it is off the unit."""
        status, _ = self.patch(seq="U1", reviewer=None)
        self.assertEqual(status, 200)
        self.assertEqual(self.told(), [REV1, DEV5])
        self.assertIn("reviewer REV1 -> unassigned",
                      self.notices()[0]["body"])

    def test_both_roles_at_once_tells_each_shell_exactly_once(self):
        """AMBIGUITY CALL (reported pre-build): the spec's "at most three" is
        per CHANGED ROLE, and a request that moves both roles is two changes.
        The union is deduped by recipient, so four shells hear once each and
        no shell is ever told twice about one write."""
        status, _ = self.patch(seq="U1", dev=DEV6, reviewer=REV2)
        self.assertEqual(status, 200)
        self.assertEqual(self.told(), [REV1, REV2, DEV5, DEV6])

    def test_a_dev_holding_both_roles_hears_once(self):
        self.patch(seq="U1", reviewer=DEV5)
        self.assertEqual(self.told(), [REV1, DEV5])

    def test_the_writing_planner_is_not_told_what_it_just_wrote(self):
        """The board's writer knows what it wrote. Excluding it can only take
        the count BELOW the ceiling, never above."""
        self.arm_binding(PLN1)
        self.patch(seq="U1", reviewer=PLN1)
        self.clear_messages()
        status, _ = self.patch((PLANNER,), seq="U1", reviewer=REV2)
        self.assertEqual(status, 200)
        self.assertEqual(self.told(), [REV2, DEV5])

    def test_a_soft_deleted_shell_is_not_a_party(self):
        self.sql("UPDATE shells SET is_deleted=1 WHERE shell_id=?", (REV1,))
        self.patch(seq="U1", reviewer=REV2)
        self.assertEqual(self.told(), [REV2, DEV5])

    # -- the row has to be writable at all ------------------------------------

    def test_an_operator_write_on_an_unbound_sprint_still_emits(self):
        """`from_shell_id` is NOT NULL and the operator token carries no
        shell, so a sender that resolved to NULL would turn every operator
        board edit into a 500. With no binding to fall back to, the row is
        self-addressed — pr_poller.py's convention for daemon-emitted rows."""
        self.patch(seq="U1", reviewer=REV2)
        rows = self.notices()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(m["from_shell_id"] == m["to_shell_id"]
                            for m in rows), rows)

    def test_a_planner_written_notice_is_sent_from_the_planner(self):
        self.arm_binding(PLN1)
        status, _ = self.patch((PLANNER,), seq="U1", reviewer=REV2)
        self.assertEqual(status, 200)
        self.assertEqual({m["from_shell_id"] for m in self.notices()},
                         {PLN1})

    def test_the_notice_can_never_boot_a_shell(self):
        """`kind` is the whole safety argument: task / result / pr_event are
        exactly the wake-eligible kinds, so any of them would let a planner's
        board edit turn into a BOOT of whichever party is a bound planner.
        This is a notice, not work."""
        self.patch(seq="U1", reviewer=REV2)
        kinds = {m["kind"] for m in self.notices()}
        self.assertEqual(kinds, {"shell"})
        for kind in kinds:
            self.assertNotIn(kind, interface_wake.ELIGIBLE_KINDS)

    def test_the_notice_is_scoped_to_its_sprint(self):
        self.patch(seq="U1", reviewer=REV2)
        self.assertEqual({m["sprint_doc_id"] for m in self.notices()}, {1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
