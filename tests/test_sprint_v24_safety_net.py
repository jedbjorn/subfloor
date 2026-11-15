"""Safety net for Sprints v2.4 (Feature #92, spec #269).

Nothing here describes the behavior spec #269 asks for.  Every expectation
pins what the engine does *today*, so each later task in the delivery shows up
as a deliberate edit to one of the tables below rather than as a silent drift.

Part A characterises one whole lane and the individual behaviors the spec
changes.  Part B is the scaffold for acceptance criterion 16: a Sprint stored
mid-lane at a fixed prior migration level is carried through the real
migration runner and finished with the old-style commands.

The comment on each expectation names the spec #269 section that governs it.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sprint_v24_support import (
    DEVELOPER,
    MIDLANE_FIXTURE,
    MIGRATIONS,
    PLANNER,
    PRIOR_MIGRATION,
    REVIEWER,
    LaneRig,
    build_database,
    load_rows,
    migration_ledger,
    upgrade_database,
)

DEV, PLN, REV = DEVELOPER, PLANNER, REVIEWER

# ---------------------------------------------------------------------------
# Part A: one lane, as it behaves today
# ---------------------------------------------------------------------------

# Every message the engine creates across one two-unit Sprint, in order:
#   (after this step, recipient, reason, chat placement, wake outbox effect)
# "own wake" opens a wake outbox row; "joins pending wake" rides a row already
# queued for that shell and still runs as its own turn.  No message today is
# stored without a wake.
LANE_WAKES_TODAY = [
    # "One wake per next action": Arm wakes the Planner to read the route list.
    ("arm", PLN, "route list", "new", "own wake"),
    # "One wake per next action", Wakes that stay: assignment.
    ("arm", DEV, "assignment", "force-new", "own wake"),
    # "One wake per next action": Checks green, lane active.
    ("checks green", DEV, "PR green", "re-enter", "own wake"),
    # Wakes that stay: review request.
    ("request-review", REV, "review request", "force-new", "own wake"),
    # Wakes that stay: verdict.
    ("record-review approved", DEV, "approval notice", "force-new", "own wake"),
    # "One wake per next action": Merge by the grant, the merged notice.
    ("merge observed", DEV, "PR merged", "re-enter", "own wake"),
    # Wakes that stay: the next assignment, dispatched by the merge itself.
    ("merge observed", DEV, "assignment", "force-new", "joins pending wake"),
    # "One wake per next action": Merge by the grant, the Developer's handoff.
    ("handoff send", PLN, "handoff relay", "re-enter", "own wake"),
    # "One wake per next action": Checks green (already green at registration).
    ("register-pr, second lane", DEV, "PR green", "re-enter", "own wake"),
    ("request-review, second lane", REV, "review request", "force-new", "own wake"),
    ("record-review approved, second lane", DEV, "approval notice", "force-new",
     "own wake"),
    # "One wake per next action": Merge by the grant, the merged notice.
    ("merge observed, second lane", DEV, "PR merged", "re-enter", "own wake"),
    # Wakes that stay: delivery terminal.
    ("merge observed, second lane", REV, "delivery terminal", "new", "own wake"),
    # "One wake per next action": Merge by the grant, the Developer's handoff.
    ("handoff send, second lane", PLN, "handoff relay", "re-enter", "own wake"),
    # Wakes that stay: close receipt.
    ("record-conformance", PLN, "close receipt", "re-enter", "own wake"),
]

# Every wake the delivery worker hands to a shell, in order:
#   (after this step, recipient, turns started by that one wake)
LANE_TURNS_TODAY = [
    ("arm", PLN, 1),
    ("arm", DEV, 1),
    ("checks green", DEV, 1),
    ("request-review", REV, 1),
    ("record-review approved", DEV, 1),
    # "One wake per next action": Several messages pending for one shell are
    # one wake and one turn per message: the merged notice, then the
    # assignment.
    ("merge observed", DEV, 2),
    ("handoff send", PLN, 1),
    ("register-pr, second lane", DEV, 1),
    ("request-review, second lane", REV, 1),
    ("record-review approved, second lane", DEV, 1),
    ("merge observed, second lane", DEV, 1),
    ("merge observed, second lane", REV, 1),
    ("handoff send, second lane", PLN, 1),
    ("record-conformance", PLN, 1),
]

# The Sprint after each step:
#   (step, lifecycle, (first unit, second unit), Sprint events it emitted)
# Each row is read after the wakes that step queued were delivered and their
# recipients ran the old-style `accept` over their Sprint inbox.
LANE_STATE_TODAY = [
    ("declare", "prepared", ("planned", "planned"),
     ("sprint.declared", "work_unit.created", "work_unit.created")),
    # "Pickup without ceremony", Read receipts: the Developer's `accept` is
    # what moves the unit from ready to active and emits work_unit.accepted.
    ("arm", "armed", ("active", "planned"),
     ("participant.route_bound", "participant.route_bound",
      "participant.route_bound", "conformance_owner.changed",
      "work_unit.ready", "lifecycle.armed", "work_unit.accepted")),
    # "Pickup without ceremony", PR registration.
    ("register-pr", "armed", ("active", "planned"),
     ("pr.registered", "pr.transition", "pr.no_checks_observed")),
    ("checks green", "armed", ("active", "planned"), ("pr.transition",)),
    ("request-review", "armed", ("in_review", "planned"), ("review.requested",)),
    ("record-review approved", "armed", ("merge_ready", "planned"),
     ("review.approved",)),
    ("authorize-merge", "armed", ("merge_ready", "planned"),
     ("merge.authorized",)),
    # "One wake per next action": merge observation completes the unit and
    # releases the dependent lane in one step; `accept` then activates it.
    ("merge observed", "armed", ("completed", "active"),
     ("work_unit.completed", "work_unit.ready", "pr.transition",
      "work_unit.accepted")),
    ("handoff send", "armed", ("completed", "active"), ()),
    ("register-pr, second lane", "armed", ("completed", "active"),
     ("pr.registered", "pr.transition")),
    ("request-review, second lane", "armed", ("completed", "in_review"),
     ("review.requested",)),
    ("record-review approved, second lane", "armed",
     ("completed", "merge_ready"), ("review.approved",)),
    ("authorize-merge, second lane", "armed", ("completed", "merge_ready"),
     ("merge.authorized",)),
    ("merge observed, second lane", "armed", ("completed", "completed"),
     ("work_unit.completed", "sprint.delivery_terminal", "pr.transition")),
    ("handoff send, second lane", "armed", ("completed", "completed"), ()),
    ("record-conformance", "completed", ("completed", "completed"),
     ("conformance.recorded", "sprint.cleanup_scheduled",
      "lifecycle.completed")),
]

# Messages still unread once every shell has run `accept` on its whole Sprint
# inbox, after each step where that set changes:
#   (step, ((recipient, reason), ...))
# "Pickup without ceremony", Read receipts: PR event notices are addressed to
# the shell, not the Sprint, so `sc sprint inbox` never lists them and
# `sc sprint accept` cannot read them.  Every Sprint-scoped message is read.
LANE_UNREAD_AFTER_PICKUP_TODAY = [
    ("declare", ()),
    ("checks green", ((DEV, "PR green"),)),
    ("merge observed", ((DEV, "PR green"), (DEV, "PR merged"))),
    ("register-pr, second lane",
     ((DEV, "PR green"), (DEV, "PR merged"), (DEV, "PR green"))),
    ("merge observed, second lane",
     ((DEV, "PR green"), (DEV, "PR merged"), (DEV, "PR green"),
      (DEV, "PR merged"))),
    # The close receipt is addressed to the Planner shell the same way.
    ("record-conformance",
     ((DEV, "PR green"), (DEV, "PR merged"), (DEV, "PR green"),
      (DEV, "PR merged"), (PLN, "close receipt"))),
]

# What `sc sprint monitor` reports when it reconciles pickup and finds nothing
# to repair.
PICKUP_QUIET = {"action": "none", "pause_reason": None, "requeued_wake_ids": []}


class SafetyNetCase(unittest.TestCase):
    maxDiff = None

    def rig(self, *, through: str | None = None) -> LaneRig:
        """A started rig over a fresh engine database, torn down with the test."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        db_path = Path(scratch.name) / "engine.db"
        build_database(db_path, through=through)
        return self.start(db_path, Path(scratch.name))

    def start(self, db_path: Path, scratch: Path) -> LaneRig:
        rig = LaneRig(db_path, scratch)
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def monitor(self, rig: LaneRig, sprint_id: int) -> dict:
        return rig.cli(PLN, "monitor", "--sprint", sprint_id)["pickup"]

    def relay(
        self,
        rig: LaneRig,
        sprint_id: int,
        key: str,
        *,
        sender: str = PLN,
        to: str = DEV,
        intent: str = "information",
        body: str = "For your information.",
    ) -> dict:
        return rig.cli(
            sender,
            "send",
            "--sprint", sprint_id,
            "--to", to,
            "--intent", intent,
            "--sprint-level",
            "--body-file", rig.write(body),
            "--key", key,
        )

    def request_review(self, rig: LaneRig, sprint_id: int, registered: int,
                       key: str = "review:1") -> dict:
        return rig.cli(
            DEV,
            "request-review",
            "--sprint", sprint_id,
            "--registered-pr", registered,
            "--intent", "submit",
            "--key", key,
        )

    def record_review(self, rig: LaneRig, sprint_id: int, registered: int,
                      key: str = "verdict:1", body: str = "No findings.") -> dict:
        return rig.cli(
            REV,
            "record-review",
            "--sprint", sprint_id,
            "--registered-pr", registered,
            "--verdict", "approved",
            "--body-file", rig.write(body),
            "--key", key,
        )

    def lane_in_review(self, rig: LaneRig) -> tuple[int, int]:
        """First lane accepted, PR green, review requested and picked up."""
        sprint_id, _first, _second, registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        self.request_review(rig, sprint_id, registered)
        rig.deliver()
        rig.accept_all(REV, sprint_id)
        return sprint_id, registered


class LaneCharacterizationTest(SafetyNetCase):
    def test_one_lane_from_declare_to_close_as_it_runs_today(self):
        rig = self.rig()
        seed = rig.seed_roster()
        lane: dict[str, int] = {}

        def declare() -> None:
            lane["sprint"], lane["first"], lane["second"] = rig.declare_and_plan(
                seed
            )

        def register_first() -> None:
            rig.github.set(41, checks=None)
            lane["pr_1"] = rig.register_pr(lane["sprint"], lane["first"], 41)

        def checks_green() -> None:
            rig.github.set(41)
            rig.poll_github()

        def merge(number: int):
            def observed() -> None:
                rig.github.set(number, state="MERGED")
                rig.poll_github()
            return observed

        def authorize(pr: str):
            return lambda: self.assertEqual(
                "a" * 40,
                rig.cli(
                    DEV,
                    "authorize-merge",
                    "--sprint", lane["sprint"],
                    "--registered-pr", lane[pr],
                )["head_sha"],
            )

        def handoff(unit: str, key: str):
            return lambda: rig.cli(
                DEV,
                "send",
                "--sprint", lane["sprint"],
                "--to", PLN,
                "--intent", "handoff",
                "--work-unit", lane[unit],
                "--body-file", rig.write("Merged and cleaned up."),
                "--key", key,
            )

        def register_second() -> None:
            rig.github.set(42)
            lane["pr_2"] = rig.register_pr(lane["sprint"], lane["second"], 42)

        def conformance() -> None:
            receipt = rig.cli(
                REV,
                "record-conformance",
                "--sprint", lane["sprint"],
                "--body-file", rig.write("Integrated conformance is clean."),
                "--final-report-file", rig.write("Final Sprint report."),
                "--reason", "conformance complete",
                "--outcome", "accepted",
                "--key", "conformance:1",
            )
            self.assertTrue(receipt["completed"])

        # (step, the command or engine event, shells that then run the
        # old-style `accept` over their Sprint inbox)
        steps = [
            ("declare", declare, ()),
            ("arm", lambda: rig.arm(lane["sprint"]), (PLN, DEV)),
            ("register-pr", register_first, ()),
            ("checks green", checks_green, ()),
            ("request-review",
             lambda: self.request_review(rig, lane["sprint"], lane["pr_1"]),
             (REV,)),
            ("record-review approved",
             lambda: self.record_review(rig, lane["sprint"], lane["pr_1"]),
             (DEV,)),
            ("authorize-merge", authorize("pr_1"), ()),
            ("merge observed", merge(41), (DEV,)),
            ("handoff send", handoff("first", "handoff:1"), (PLN,)),
            ("register-pr, second lane", register_second, ()),
            ("request-review, second lane",
             lambda: self.request_review(
                 rig, lane["sprint"], lane["pr_2"], "review:2"),
             (REV,)),
            ("record-review approved, second lane",
             lambda: self.record_review(
                 rig, lane["sprint"], lane["pr_2"], "verdict:2"),
             (DEV,)),
            ("authorize-merge, second lane", authorize("pr_2"), ()),
            ("merge observed, second lane", merge(42), (REV,)),
            ("handoff send, second lane", handoff("second", "handoff:2"), (PLN,)),
            ("record-conformance", conformance, (PLN,)),
        ]

        wakes, turns, state, unread = [], [], [], []
        for step, act, pickup in steps:
            act()
            sprint_id = lane["sprint"]
            wakes += [(step, *message) for message in rig.new_messages()]
            turns += [
                (step, delivery.recipient, delivery.turns)
                for delivery in rig.deliver()
            ]
            for shell in pickup:
                rig.accept_all(shell, sprint_id)
            state.append(
                (
                    step,
                    rig.lifecycle(sprint_id),
                    tuple(rig.units(sprint_id)),
                    tuple(rig.new_events()),
                )
            )
            still_unread = tuple(rig.unread())
            if not unread or unread[-1][1] != still_unread:
                unread.append((step, still_unread))
            if rig.lifecycle(sprint_id) == "armed":
                # Pickup recovery finds nothing to repair at any point.
                self.assertEqual(PICKUP_QUIET, self.monitor(rig, sprint_id), step)
            self.assertEqual([], rig.new_messages(), step)

        self.assertEqual(LANE_WAKES_TODAY, wakes)
        self.assertEqual(LANE_TURNS_TODAY, turns)
        self.assertEqual(LANE_STATE_TODAY, state)
        self.assertEqual(LANE_UNREAD_AFTER_PICKUP_TODAY, unread)

        # Every message opened or joined a wake, and every wake was delivered
        # on its first attempt: one outbox row per "own wake" line above.
        own_wakes = [row for row in LANE_WAKES_TODAY if row[4] == "own wake"]
        self.assertEqual(
            [(row[1], "delivered", 1) for row in own_wakes],
            [row[:3] for row in rig.wake_rows()],
        )
        # No pause of any cause on the whole path.
        self.assertEqual([], rig.pauses(lane["sprint"]))
        self.assertEqual(
            ("completed", "accepted"),
            tuple(
                rig.con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints "
                    "WHERE sprint_id=?",
                    (lane["sprint"],),
                ).fetchone()
            ),
        )


class TodayBehaviorTest(SafetyNetCase):
    """One small pin per behavior spec #269 changes."""

    # 1 -- "Pickup without ceremony", Read receipts
    def test_succeeded_turn_with_unaccepted_assignment_pauses_at_once(self):
        rig = self.rig()
        sprint_id, first, _second = rig.declare_and_plan(rig.seed_roster())
        rig.arm(sprint_id)
        rig.deliver()
        rig.accept_all(PLN, sprint_id)
        rig.mark()

        # The Developer's turn ended without `accept`.  The first pickup
        # reconciliation pauses: no re-wake is attempted for an assignment.
        # The monitor receipt that performed the pause still reads "none".
        self.assertEqual(
            {"action": "none", "pause_reason": None, "requeued_wake_ids": []},
            self.monitor(rig, sprint_id),
        )
        self.assertEqual("paused", rig.lifecycle(sprint_id))
        self.assertEqual(
            ["assignment_execution_already_started"], rig.pauses(sprint_id)
        )
        self.assertEqual(
            ["wake.pickup_exhausted", "lifecycle.paused"], rig.new_events()
        )
        exhausted = rig.last_event(sprint_id, "wake.pickup_exhausted")
        self.assertEqual(
            ("ASSIGNMENT_TERMINAL_UNREAD", "terminal_unread", "succeeded", 1,
             DEV, first),
            (
                exhausted["error_code"],
                exhausted["failure_class"],
                exhausted["run_state"],
                exhausted["attempt_count"],
                exhausted["shell"],
                exhausted["work_unit_id"],
            ),
        )
        self.assertEqual(
            [(PLN, "pause notice", "re-enter", "own wake")], rig.new_messages()
        )
        self.assertEqual(["ready", "planned"], rig.units(sprint_id))
        self.assertEqual(
            [(DEV, "assignment"), (PLN, "pause notice")], rig.unread()
        )
        # The assignment is never queued again.
        self.assertEqual([(PLN, 1)], [(d.recipient, d.turns) for d in rig.deliver()])

    # 2 -- "Pickup without ceremony", Read receipts
    def test_unread_no_action_message_gets_one_rewake_then_pauses(self):
        def information_relay(rig: LaneRig) -> int:
            sprint_id, _first, _second, _registered = rig.start_first_lane(
                rig.seed_roster()
            )
            rig.deliver()
            self.relay(rig, sprint_id, "note:1")
            return sprint_id

        def approval_notice(rig: LaneRig) -> int:
            sprint_id, registered = self.lane_in_review(rig)
            self.record_review(rig, sprint_id, registered)
            return sprint_id

        for name, arrange, reason in (
            ("information relay", information_relay, "information relay"),
            ("approval notice", approval_notice, "approval notice"),
        ):
            with self.subTest(message=name):
                rig = self.rig()
                sprint_id = arrange(rig)
                rig.mark()
                # First turn succeeds and leaves the message unread.
                self.assertEqual([(DEV, 1)],
                                 [(d.recipient, d.turns) for d in rig.deliver()])
                first = self.monitor(rig, sprint_id)
                self.assertEqual(("requeued", None), (first["action"],
                                                      first["pause_reason"]))
                self.assertEqual(1, len(first["requeued_wake_ids"]))
                self.assertEqual(["wake.requeued"], rig.new_events())
                self.assertEqual("armed", rig.lifecycle(sprint_id))
                self.assertEqual(
                    (DEV, "pending", 0, "sprint-recovery:N:delivered-unread:N"),
                    rig.wake_rows()[-1],
                )
                # The one re-wake is delivered, and also ends unread.
                self.assertEqual([(DEV, 1)],
                                 [(d.recipient, d.turns) for d in rig.deliver()])
                self.assertEqual(
                    {"action": "paused", "pause_reason": "wake_pickup_unread",
                     "requeued_wake_ids": []},
                    self.monitor(rig, sprint_id),
                )
                self.assertEqual("paused", rig.lifecycle(sprint_id))
                self.assertEqual(["wake_pickup_unread"], rig.pauses(sprint_id))
                self.assertEqual(
                    ["wake.pickup_exhausted", "lifecycle.paused"], rig.new_events()
                )
                exhausted = rig.last_event(sprint_id, "wake.pickup_exhausted")
                self.assertEqual(
                    ("WAKE_PICKUP_UNREAD", "terminal_unread", 2),
                    (exhausted["error_code"], exhausted["failure_class"],
                     exhausted["attempt_count"]),
                )
                self.assertIn((DEV, reason), rig.unread())
                self.assertEqual((PLN, "pause notice", "re-enter", "own wake"),
                                 rig.new_messages()[-1])

    # 2, continued -- "Pickup without ceremony", Read receipts
    def test_pr_event_notice_is_outside_sprint_pickup_recovery(self):
        rig = self.rig()
        sprint_id, _first, _second, _registered = rig.start_first_lane(
            rig.seed_roster()
        )
        self.assertEqual([(DEV, 1)], [(d.recipient, d.turns) for d in rig.deliver()])
        self.assertEqual([(DEV, "PR green")], rig.unread())

        # The green notice is addressed to the shell, not the Sprint: it is
        # not in the Sprint inbox, `accept` cannot read it, and leaving it
        # unread after a succeeded turn causes neither a re-wake nor a pause.
        self.assertEqual([], rig.inbox_ids(DEV, sprint_id))
        notice = rig.message_id(DEV, "notification")
        self.assertEqual(
            (None,),
            tuple(
                rig.con.execute(
                    "SELECT sprint_id FROM wake_message WHERE message_id=?",
                    (notice,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/inbox-read → HTTP 404: "
            f"Sprint message {notice} is not addressed to shell",
            rig.cli_refusal(DEV, "accept", "--sprint", sprint_id,
                            "--message", notice),
        )
        for _ in range(2):
            self.assertEqual(PICKUP_QUIET, self.monitor(rig, sprint_id))
        self.assertEqual("armed", rig.lifecycle(sprint_id))
        self.assertEqual([], rig.deliver())
        self.assertEqual([(DEV, "PR green")], rig.unread())

    # 3 -- "Pickup without ceremony", Keys and intent
    def test_send_and_review_writes_refuse_a_missing_key(self):
        rig = self.rig()
        sprint_id, _first, _second, registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        rig.mark()
        body = rig.write("A relay without a key.")

        self.assertEqual(
            "exit 2: sc sprint send: error: the following arguments are "
            "required: --key",
            rig.cli_refusal(DEV, "send", "--sprint", sprint_id, "--to", PLN,
                            "--body-file", body),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/send → HTTP 400: "
            "Sprint message idempotency key is empty",
            rig.api_refusal(DEV, "/_sc/sprint/send",
                            {"sprint_id": sprint_id, "to": PLN, "body": "x"}),
        )
        self.assertEqual(
            "exit 2: sc sprint request-review: error: the following arguments "
            "are required: --key",
            rig.cli_refusal(DEV, "request-review", "--sprint", sprint_id,
                            "--registered-pr", registered, "--intent", "submit"),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/review-request → HTTP 400: "
            "idempotency key is empty",
            rig.api_refusal(
                DEV, "/_sc/sprint/review-request",
                {"sprint_id": sprint_id, "registered_pr_id": registered,
                 "intent": "submit"},
            ),
        )
        self.assertEqual(["active", "planned"], rig.units(sprint_id))

        self.request_review(rig, sprint_id, registered)
        rig.deliver()
        rig.accept_all(REV, sprint_id)
        rig.mark()
        self.assertEqual(
            "exit 2: sc sprint record-review: error: the following arguments "
            "are required: --key",
            rig.cli_refusal(REV, "record-review", "--sprint", sprint_id,
                            "--registered-pr", registered, "--verdict",
                            "approved", "--body-file", body),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/review-record → HTTP 400: "
            "idempotency key is empty",
            rig.api_refusal(
                REV, "/_sc/sprint/review-record",
                {"sprint_id": sprint_id, "registered_pr_id": registered,
                 "verdict": "approved", "body": "x"},
            ),
        )
        # Nothing was written by any refusal.
        self.assertEqual([], rig.new_messages())
        self.assertEqual([], rig.new_events())
        self.assertEqual(["in_review", "planned"], rig.units(sprint_id))

    # 4 -- "Pickup without ceremony", PR registration
    def test_request_review_needs_a_registered_pr(self):
        rig = self.rig()
        sprint_id, _first, _second = rig.declare_and_plan(rig.seed_roster())
        rig.arm(sprint_id)
        rig.deliver()
        rig.accept_all(DEV, sprint_id)
        rig.mark()

        # The lane is accepted and active, and no PR is registered to it.
        self.assertEqual(
            "exit 2: sc sprint request-review: error: the following arguments "
            "are required: --registered-pr",
            rig.cli_refusal(DEV, "request-review", "--sprint", sprint_id,
                            "--intent", "submit", "--key", "review:1"),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/review-request → HTTP 400: "
            "registered_pr_id must be a positive integer",
            rig.api_refusal(
                DEV, "/_sc/sprint/review-request",
                {"sprint_id": sprint_id, "intent": "submit",
                 "idempotency_key": "review:1"},
            ),
        )
        self.assertEqual(
            "sprint: API POST /_sc/sprint/review-request → HTTP 404: "
            "unknown registered PR: 1",
            rig.cli_refusal(DEV, "request-review", "--sprint", sprint_id,
                            "--registered-pr", 1, "--intent", "submit",
                            "--key", "review:1"),
        )
        self.assertEqual([], rig.new_messages())
        self.assertEqual(["active", "planned"], rig.units(sprint_id))

    # 5 -- "Pickup without ceremony", Stored size
    def test_verdict_body_over_8000_characters_is_refused(self):
        rig = self.rig()
        sprint_id, registered = self.lane_in_review(rig)
        rig.mark()

        self.assertEqual(
            "sprint: API POST /_sc/sprint/review-record → HTTP 400: "
            "review body is 8001 characters; maximum is 8000",
            rig.cli_refusal(
                REV, "record-review", "--sprint", sprint_id, "--registered-pr",
                registered, "--verdict", "approved", "--body-file",
                rig.write("x" * 8001), "--key", "verdict:oversize",
            ),
        )
        self.assertEqual([], rig.new_messages())
        self.assertEqual([], rig.new_events())
        self.assertEqual(["in_review", "planned"], rig.units(sprint_id))
        self.assertEqual(
            0,
            rig.con.execute(
                "SELECT COUNT(*) FROM sprint_judgments WHERE sprint_id=? "
                "AND length(body)>8000",
                (sprint_id,),
            ).fetchone()[0],
        )
        # The limit is exact: 8,000 characters is stored.
        self.assertEqual(
            "merge_ready",
            self.record_review(rig, sprint_id, registered, "verdict:limit",
                               "x" * 8000)["disposition"],
        )

    # 6 -- "Pickup without ceremony", Merge grant
    def test_declare_refuses_without_merge_grant(self):
        rig = self.rig()
        seed = rig.seed_roster()
        participants = rig.participants_file()

        self.assertEqual(
            "exit 2: sc sprint declare: error: the following arguments are "
            "required: --merge-grant",
            rig.cli_refusal(PLN, "declare", "--feature", seed["feature_id"],
                            "--spec", seed["document_id"],
                            "--participants-file", participants),
        )
        payload = {
            "feature_id": seed["feature_id"],
            "spec_document_ids": [seed["document_id"]],
            "participants": json.loads(Path(participants).read_text()),
        }
        for grant in ({}, {"merge_grant_enabled": False}):
            self.assertEqual(
                "sprint: API POST /_sc/sprint/declare → HTTP 409: "
                "declaration requires an explicit Sprint merge grant",
                rig.api_refusal(PLN, "/_sc/sprint/declare", {**payload, **grant}),
            )
        self.assertEqual(
            0, rig.con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0]
        )

    # 7 -- "One wake per next action": Checks green; Checks red while paused;
    # Pause by the Planner
    def test_green_check_wakes_in_review_when_merge_ready_and_while_paused(self):
        rig = self.rig()
        sprint_id, registered = self.lane_in_review(rig)
        rig.mark()

        def new_head(head: str, **checks) -> tuple[list, list, str]:
            rig.github.set(41, head_sha=head * 40, **checks)
            rig.poll_github()
            messages = rig.new_messages()
            turns = [(d.recipient, d.state, d.turns) for d in rig.deliver()]
            instruction = rig.prompts[-1].rsplit("event=", 1)[1].split(". ", 1)[1]
            return messages, turns, instruction

        # In review.
        self.assertEqual(
            (
                [(DEV, "PR green", "re-enter", "own wake")],
                [(DEV, "delivered", 1)],
                (
                    "Your active Sprint PR is green; judge readiness and pass the "
                    "baton to review when ready."
                ),
            ),
            new_head("d"),
        )
        self.assertEqual(["in_review", "planned"], rig.units(sprint_id))

        # Merge-ready.
        self.record_review(rig, sprint_id, registered)
        rig.deliver()
        rig.accept_all(DEV, sprint_id)
        rig.mark()
        self.assertEqual(["merge_ready", "planned"], rig.units(sprint_id))
        self.assertEqual(
            (
                [(DEV, "PR green", "re-enter", "own wake")],
                [(DEV, "delivered", 1)],
                (
                    "Your active Sprint PR is green; judge readiness and pass the "
                    "baton to review when ready."
                ),
            ),
            new_head("e"),
        )

        # The Planner pauses and is woken about its own action.
        self.assertTrue(
            rig.cli(PLN, "pause", "--sprint", sprint_id, "--reason",
                    "planner pause")["changed"]
        )
        self.assertEqual("paused", rig.lifecycle(sprint_id))
        self.assertEqual(
            [(PLN, "pause notice", "re-enter", "own wake")], rig.new_messages()
        )
        self.assertEqual(
            [(PLN, "delivered", 1)],
            [(d.recipient, d.state, d.turns) for d in rig.deliver()],
        )

        # Paused: green and red both still wake the owner, and are delivered.
        self.assertEqual(
            (
                [(DEV, "PR green", "re-enter", "own wake")],
                [(DEV, "delivered", 1)],
                (
                    "Your paused Sprint PR is green; judge readiness and wait for "
                    "the Sprint to resume."
                ),
            ),
            new_head("f"),
        )
        self.assertEqual(
            (
                [(DEV, "PR red", "re-enter", "own wake")],
                [(DEV, "delivered", 1)],
                (
                    "Your paused Sprint PR went red; fix the failing checks now; "
                    "do not wait for the Sprint to resume."
                ),
            ),
            new_head("0", checks="FAILURE", checks_failed=True),
        )
        self.assertEqual("paused", rig.lifecycle(sprint_id))
        self.assertEqual(["planner pause"], rig.pauses(sprint_id))

    # 8 -- "One wake per next action": Relay with intent `information`
    def test_information_relay_wakes_the_recipient(self):
        rig = self.rig()
        sprint_id, _first, _second, _registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        rig.mark()

        receipt = self.relay(rig, sprint_id, "note:1")
        self.assertEqual((True, "pending"),
                         (receipt["message_created"], receipt["wake_state"]))
        self.assertEqual(
            [(DEV, "information relay", "re-enter", "own wake")],
            rig.new_messages(),
        )
        self.assertEqual(
            [(DEV, "delivered", 1)],
            [(d.recipient, d.state, d.turns) for d in rig.deliver()],
        )
        self.assertTrue(rig.prompts[0].endswith("For your information."))

    # 9 -- "One wake per next action": Several messages pending for one shell
    def test_several_pending_messages_are_one_wake_and_one_turn_each(self):
        rig = self.rig()
        sprint_id, _first, _second, _registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        rig.mark()
        wakes_before = len(rig.wake_rows())

        for number in (1, 2, 3):
            self.relay(rig, sprint_id, f"note:{number}", body=f"Note {number}.")
        self.assertEqual(
            [
                (DEV, "information relay", "re-enter", "own wake"),
                (DEV, "information relay", "re-enter", "joins pending wake"),
                (DEV, "information relay", "re-enter", "joins pending wake"),
            ],
            rig.new_messages(),
        )
        self.assertEqual(wakes_before + 1, len(rig.wake_rows()))
        # One wake outbox row, three separate turns, one body in each prompt.
        self.assertEqual(
            [(DEV, "delivered", 3)],
            [(d.recipient, d.state, d.turns) for d in rig.deliver()],
        )
        self.assertEqual(
            ["Note 1.", "Note 2.", "Note 3."],
            [prompt.rsplit("\n\n", 1)[1] for prompt in rig.prompts],
        )
        for number, prompt in enumerate(rig.prompts, start=1):
            self.assertEqual(1, prompt.count("## wake_message #"), number)

    # 10 -- "Adjusting a running Sprint", A failing route
    def test_route_that_exhausts_wake_attempts_pauses_the_whole_sprint(self):
        rig = self.rig()
        sprint_id, _first, _second, _registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        self.relay(rig, sprint_id, "note:1")
        rig.mark()

        attempts = rig.deliver(unreachable=(DEV,))
        rig.advance_clock(hours=1)  # step past the retry backoff
        attempts += rig.deliver(unreachable=(DEV,))
        self.assertEqual(
            [
                (DEV, "pending", 1, 0),
                (DEV, "pending", 2, 0),
                (DEV, "failed", 3, 0),
                # The pause notice is delivered while the Sprint is paused.
                (PLN, "delivered", 1, 1),
            ],
            [(d.recipient, d.state, d.attempt, d.turns) for d in attempts],
        )
        self.assertEqual("paused", rig.lifecycle(sprint_id))
        self.assertEqual(["wake_delivery_exhausted"], rig.pauses(sprint_id))
        self.assertEqual(["lifecycle.paused"], rig.new_events())
        paused = rig.last_event(sprint_id, "lifecycle.paused")
        self.assertEqual(
            (3, "simulated harness launch failure", "armed"),
            (paused["attempts"], paused["last_error"], paused["from"]),
        )
        self.assertEqual(
            [(PLN, "pause notice", "re-enter", "own wake")], rig.new_messages()
        )
        # The whole Sprint stops: the unaffected lane state is frozen as it was.
        self.assertEqual(["active", "planned"], rig.units(sprint_id))
        self.assertEqual((DEV, "failed", 3), rig.wake_rows()[-2][:3])

    # 11 -- "Adjusting a running Sprint", Lane-scoped quiesce
    def test_reroute_participant_is_refused_while_armed(self):
        rig = self.rig()
        sprint_id, _first, _second, _registered = rig.start_first_lane(
            rig.seed_roster()
        )
        rig.deliver()
        rig.mark()

        for shell_id in (1, 2, 3):  # Developer, Reviewer, Planner
            self.assertEqual(
                "sprint: API POST /_sc/sprint/reroute-participant → HTTP 409: "
                "participant routes may change only while a Sprint is "
                "prepared or paused",
                rig.cli_refusal(
                    PLN, "reroute-participant", "--sprint", sprint_id,
                    "--participant-shell", shell_id, "--harness", "claude",
                ),
            )
        self.assertEqual("armed", rig.lifecycle(sprint_id))
        self.assertEqual([], rig.new_events())
        self.assertEqual(
            [1, 1, 1],
            [
                int(row[0])
                for row in rig.con.execute(
                    "SELECT binding.route_revision FROM sprint_participants p "
                    "JOIN sprint_participant_route_bindings binding "
                    "ON binding.binding_id=p.active_route_binding_id "
                    "WHERE p.sprint_id=? ORDER BY p.participant_id",
                    (sprint_id,),
                )
            ],
        )


# ---------------------------------------------------------------------------
# Part B: a Sprint in flight across the update, finished with old commands
# ---------------------------------------------------------------------------


class UpgradeCompatibilityTest(SafetyNetCase):
    """Acceptance criterion 16 scaffold.

    The Sprint under test was written by the engine at ``PRIOR_MIGRATION`` and
    is restored from a frozen fixture, so this keeps running unchanged when a
    later task adds a migration above that level or changes how a new Sprint
    is declared.
    """

    def test_prior_level_is_a_fixed_migration_in_this_tree(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertIn(PRIOR_MIGRATION, names)
        self.assertEqual("0284_reseed_sprint_consistency.sql", PRIOR_MIGRATION)
        self.assertIn(PRIOR_MIGRATION, MIDLANE_FIXTURE.read_text())

    def test_sprint_in_flight_survives_update_and_old_style_commands(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        db_path = Path(scratch.name) / "engine.db"
        every_migration = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        newer = [name for name in every_migration if name > PRIOR_MIGRATION]

        # 1. An engine at the prior level, holding a Sprint mid-lane.
        build_database(db_path, through=PRIOR_MIGRATION)
        self.assertEqual(PRIOR_MIGRATION, migration_ledger(db_path)[-1])
        load_rows(db_path, MIDLANE_FIXTURE)
        con = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                [(1, "armed", 1)],
                con.execute(
                    "SELECT sprint_id,lifecycle,merge_grant_enabled FROM sprints"
                ).fetchall(),
            )
            self.assertEqual(
                [("active",), ("planned",)],
                con.execute(
                    "SELECT disposition FROM sprint_work_units "
                    "ORDER BY work_unit_id"
                ).fetchall(),
            )
            self.assertEqual(
                [("work_assignment", "accepted", 1, 1)],
                con.execute(
                    "SELECT message_kind,disposition,delivered_at IS NOT NULL,"
                    "read_at IS NOT NULL FROM wake_message "
                    "WHERE message_kind='work_assignment'"
                ).fetchall(),
            )
            self.assertEqual(
                [("acme/repo", 41, "created")],
                con.execute(
                    "SELECT pr.repository,pr.pr_number,t.normalized_state "
                    "FROM sprint_registered_prs pr JOIN sprint_pr_transitions t "
                    "USING (registered_pr_id)"
                ).fetchall(),
            )
        finally:
            con.close()

        # 2. The update: every migration the tree holds beyond that level,
        #    through the real runner.
        self.assertEqual(newer, upgrade_database(db_path))
        self.assertEqual(every_migration, migration_ledger(db_path))
        self.assertEqual([], upgrade_database(db_path))
        con = sqlite3.connect(db_path)
        try:
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            con.close()

        # 3. The lane continues on the updated engine with old-style commands.
        #    Only what an un-updated fork relies on is asserted here: each
        #    command succeeds and the lane moves.  How many wakes that takes
        #    is Part A's business.
        rig = self.start(db_path, Path(scratch.name))
        sprint_id, first, second, registered = 1, 1, 2, 1
        self.assertEqual("armed", rig.lifecycle(sprint_id))
        self.assertEqual(PICKUP_QUIET, self.monitor(rig, sprint_id))

        def accept(shell: str, message_id: int) -> dict:
            receipt = rig.cli(
                shell, "accept", "--sprint", sprint_id, "--message", message_id
            )
            self.assertEqual((message_id, True),
                             (receipt["message_id"], receipt["read"]))
            return receipt

        rig.github.set(41)
        rig.poll_github()
        rig.deliver()

        # request-review with an explicit --intent and --key; the key replays
        review = self.request_review(rig, sprint_id, registered, "old:review:1")
        self.assertTrue(review["created"])
        replay = self.request_review(rig, sprint_id, registered, "old:review:1")
        self.assertEqual((False, review["message_id"]),
                         (replay["created"], replay["message_id"]))
        self.assertEqual(["in_review", "planned"], rig.units(sprint_id))
        rig.deliver()
        # accept, on the review request
        self.assertEqual(
            "accepted", accept(REV, review["message_id"])["disposition"]
        )

        # record-review with an explicit --key; the key replays
        verdict = self.record_review(rig, sprint_id, registered, "old:verdict:1")
        self.assertEqual((True, "merge_ready"),
                         (verdict["created"], verdict["disposition"]))
        self.assertFalse(
            self.record_review(rig, sprint_id, registered, "old:verdict:1")[
                "created"
            ]
        )
        rig.deliver()
        # accept, on a message with no action to take; repeating it is harmless
        for _ in range(2):
            accept(DEV, verdict["message_id"])

        self.assertEqual(
            41,
            rig.cli(DEV, "authorize-merge", "--sprint", sprint_id,
                    "--registered-pr", registered)["pr_number"],
        )
        rig.github.set(41, state="MERGED")
        rig.poll_github()
        self.assertEqual(["completed", "ready"], rig.units(sprint_id))
        rig.deliver()

        # accept, on the next assignment
        self.assertEqual(
            "accepted",
            accept(DEV, rig.message_id(DEV, "work_assignment"))["disposition"],
        )
        self.assertEqual(["completed", "active"], rig.units(sprint_id))

        # the Developer's handoff send, with --intent handoff and --key
        handoff = rig.cli(
            DEV, "send", "--sprint", sprint_id, "--to", PLN, "--intent",
            "handoff", "--work-unit", first, "--body-file",
            rig.write("Merged and cleaned up."), "--key", "old:handoff:1",
        )
        self.assertTrue(handoff["message_created"])
        rig.deliver()
        accept(PLN, handoff["message_id"])

        # register-pr on the next lane
        rig.github.set(42)
        self.assertEqual(2, rig.register_pr(sprint_id, second, 42))

        # declare with --merge-grant
        declared = rig.cli(
            PLN, "declare", "--feature", 1, "--spec", 1,
            "--participants-file", rig.participants_file(), "--merge-grant",
        )
        self.assertEqual("prepared", rig.lifecycle(declared["sprint_id"]))

        rig.deliver()
        self.assertEqual("armed", rig.lifecycle(sprint_id))
        self.assertEqual(["completed", "active"], rig.units(sprint_id))
        self.assertEqual([], rig.pauses(sprint_id))
        self.assertEqual(PICKUP_QUIET, self.monitor(rig, sprint_id))


if __name__ == "__main__":
    unittest.main()
