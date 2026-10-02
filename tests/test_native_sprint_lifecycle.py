"""Canonical Sprint transactions and private test transport; no native launches."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import active_chat_registry
import db_driver
import sprint_cleanup
import sprint_domain
import sprint_message_delivery
import sprint_native_lifecycle as lifecycle
import test_native_sprint_wakes as wakes
from conversation_runtime_contract import NativeReference, RuntimeEvent
from test_sprint_v2_domain import SprintDomainCase

controller, database, native = wakes.controller, wakes.database, wakes.native


@pytest.fixture
def sprint(native):
    n = native
    con = n.con
    # Create the synthetic initial row with its immutable Sprint scope.
    chat = dict(
        con.execute("SELECT * FROM conversations WHERE conversation_id='cv'").fetchone()
    )
    generation = dict(
        con.execute(
            "SELECT * FROM conversation_runtime_generations WHERE generation_id='g'"
        ).fetchone()
    )
    con.execute("DELETE FROM active_shell_chats")
    con.execute("DELETE FROM conversation_runtime_generations")
    con.execute("DELETE FROM conversations")
    chat["conversation_scope"] = "sprint"
    for table, values in [
        ("conversations", chat),
        ("conversation_runtime_generations", generation),
    ]:
        con.execute(
            f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
    con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'cv')")
    for sid, name, flavor in [(2, "REV1", "reviewer"), (3, "PLN1", "planner")]:
        con.execute(
            "INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(?,?,?,?,?,1)",
            (sid, name, name, flavor, "Synthetic native lifecycle"),
        )
    state = SimpleNamespace(con=con, serial=0)
    sid, unit = SprintDomainCase.create_sprint(state)
    con.execute(
        "UPDATE sprint_participants SET runtime_mode='native_experiment' WHERE sprint_id=? AND role='developer'",
        (sid,),
    )
    con.execute(
        "UPDATE sprints SET conformance_reviewer_shell_id=2,conformance_owner_generation=1 WHERE sprint_id=?",
        (sid,),
    )
    con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?", (sid,))
    participant = con.execute(
        "SELECT participant_id FROM sprint_participants WHERE sprint_id=? AND role='developer'",
        (sid,),
    ).fetchone()[0]
    con.execute(
        "INSERT INTO sprint_participant_conversations(sprint_participant_id,conversation_id) VALUES(?,'cv')",
        (participant,),
    )
    ref = {"root_id": "root", "thread_id": "root", "activity_id": "A"}
    runtime = {
        "role": "ordinary",
        "state": "ready",
        "generation_id": "g",
        "primary": ref,
        "primary_observation": {"root_id":"root","activity_id":"A","freshness":"current",
                                "partial":False,"grade":"compatible","active":True,"observed_at":time.time()},
        "freshness": "current",
        "partial": False,
        "observed_at": time.time(),
        "capabilities": {"stop_reply": "compatible", "submission": "compatible"},
    }
    con.execute(
        "UPDATE conversations SET runtime_projection=? WHERE conversation_id='cv'",
        (json.dumps(runtime),),
    )
    con.commit()
    n.ctl.emit(RuntimeEvent("activity.started", NativeReference(**ref)))
    store = sprint_domain.SprintLifecycleStore(
        con,
        interrupt_run=lambda _: pytest.fail("legacy interrupt on native participant"),
        cleanup_store=sprint_cleanup.SprintCleanupTargetStore(
            con, identity_provider=lambda: (n.service.root, n.service.root / ".git")
        ),
    )
    yield SimpleNamespace(
        n=n, con=con, sprint=sid, unit=unit, participant=participant, store=store
    )


def intents(s):
    return s.con.execute(
        "SELECT * FROM sprint_native_lifecycle_intents WHERE sprint_id=? ORDER BY created_at",
        (s.sprint,),
    ).fetchall()


def pause(s):
    return s.store.pause(
        s.sprint, sprint_domain.LifecycleActor("planner", 3), reason="Synthetic Pause"
    )


def test_pause_without_legacy_run_persists_exact_primary_and_retains_background(sprint):
    s = sprint
    s.n.ctl.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference(
                "root",
                thread_id="child",
                parent_thread_id="root",
                activity_id="child-turn",
            ),
        )
    )
    receipt = pause(s)
    assert receipt.changed and not receipt.interrupt_run_ids
    row = intents(s)[0]
    assert (
        row["generation_id"] == "g"
        and json.loads(row["primary_json"])["activity_id"] == "A"
    )
    assert (
        not s.n.driver.controls and not s.n.cleanups
    )  # DB-only lifecycle transaction.
    lifecycle.consume(s.n.service, "g")
    assert s.n.driver.controls == [row["command_id"]]
    assert not s.n.ctl.journal.get("close") and not s.n.cleanups
    assert (
        s.con.execute(
            "SELECT state FROM conversations WHERE conversation_id='cv'"
        ).fetchone()[0]
        != "closed"
    )
    lifecycle.consume(s.n.service, "g")
    assert s.n.driver.controls == [row["command_id"]]


def test_lifecycle_rollback_persists_no_command_or_intent(sprint):
    s = sprint
    with (
        pytest.raises(RuntimeError, match="rollback"),
        db_driver.write_transaction(s.con, "synthetic.rollback"),
    ):
        s.store._pause_in_transaction(
            s.store._sprint(s.sprint),
            sprint_domain.LifecycleActor("planner", 3),
            reason="Rollback",
            detail={},
        )
        raise RuntimeError("rollback")
    assert not intents(s)
    assert (
        s.con.execute("SELECT COUNT(*) FROM conversation_runtime_commands").fetchone()[
            0
        ]
        == 0
    )
    assert s.con.execute("SELECT lifecycle FROM sprints").fetchone()[0] == "armed"
    assert not s.n.driver.controls


def test_replaced_native_primary_refuses_old_pause_without_close(sprint):
    s = sprint
    pause(s)
    s.n.ctl.emit(
        RuntimeEvent(
            "activity.terminal",
            NativeReference("root", thread_id="root", activity_id="A"),
        )
    )
    s.n.ctl.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference("root", thread_id="root", activity_id="B"),
        )
    )
    lifecycle.consume(s.n.service, "g")
    assert not s.n.driver.controls and not s.n.cleanups
    assert intents(s)[0]["state"] == "rejected"
    assert s.n.ctl.journal.get("primary") == "B" and not s.n.ctl.journal.get("close")


@pytest.mark.parametrize(
    "change",
    [
        {"primary": None},
        {"primary_observation": {"freshness": "stale"}},
        {"primary_observation": {"partial": True}},
        {"capabilities": {}},
    ],
)
def test_missing_or_unqualified_primary_is_named_pending(sprint, change):
    s = sprint
    runtime = json.loads(
        s.con.execute("SELECT runtime_projection FROM conversations").fetchone()[0]
    )
    runtime.update(change)
    s.con.execute(
        "UPDATE conversations SET runtime_projection=?", (json.dumps(runtime),)
    )
    s.con.commit()
    pause(s)
    lifecycle.consume(s.n.service, "g")
    assert intents(s)[0]["state"] == "pending" and intents(s)[0]["detail"]
    assert not s.n.driver.controls and not s.n.cleanups


def ingest_events(s):
    import conversation_native_chats as chats

    replay = s.n.client.request("subscribe", after=0)
    activity = replay.get("primary")
    primary = {"root_id":"root","thread_id":"root","activity_id":activity} if activity else None
    s.n.service.store.ingest("g",1,1,s.n.client.lease,replay,
        project=lambda con,cid,seq,event: chats.project_event(con,cid,seq,event,primary=primary))
    return json.loads(s.con.execute("SELECT runtime_projection FROM conversations WHERE conversation_id='cv'").fetchone()[0])


def test_actual_root_processing_after_partial_child_keeps_pause_eligible(sprint):
    s = sprint
    s.n.ctl.emit(RuntimeEvent("work.observed", NativeReference("root",thread_id="child",parent_thread_id="root",work_id="bg"),
        data={"kind":"task","state":"unknown"},partial=True))
    s.n.ctl.emit(RuntimeEvent("activity.processed",NativeReference("root",thread_id="root",activity_id="A")))
    runtime = ingest_events(s)
    assert runtime["partial"] is True  # Work controls still see uncertainty.
    pause(s)
    assert intents(s)[0]["command_id"] is not None
    lifecycle.consume(s.n.service,"g")
    assert s.n.driver.controls == [intents(s)[0]["command_id"]]
    assert not s.n.cleanups


@pytest.mark.parametrize("fields", [
    {"partial":True}, {"freshness":"stale"}, {"grade":"inconclusive"},
])
def test_actual_unqualified_root_remains_pending_despite_prior_known_primary(sprint, fields):
    s = sprint
    s.n.ctl.emit(RuntimeEvent("activity.processed",NativeReference("root",thread_id="root",activity_id="A"),**fields))
    ingest_events(s)
    pause(s)
    assert intents(s)[0]["command_id"] is None
    assert not s.n.driver.controls


def test_missing_root_activity_does_not_borrow_prior_primary_evidence(sprint):
    s = sprint
    s.n.ctl.emit(RuntimeEvent("activity.processed",NativeReference("root",thread_id="root")))
    ingest_events(s)
    pause(s)
    assert intents(s)[0]["command_id"] is None


def test_primary_observation_must_match_current_journal_primary(sprint):
    s = sprint
    runtime = json.loads(s.con.execute("SELECT runtime_projection FROM conversations").fetchone()[0])
    runtime["primary"]["activity_id"]="B"
    s.con.execute("UPDATE conversations SET runtime_projection=?",(json.dumps(runtime),))
    s.con.commit()
    pause(s)
    assert intents(s)[0]["command_id"] is None


def test_postcommit_transport_crash_retains_unknown_without_another_write(sprint):
    s = sprint
    pause(s)
    original = s.n.client.request
    attempts = []

    def crash(op, **fields):
        if op == "control":
            assert (
                s.con.execute(
                    "SELECT state FROM conversation_runtime_commands"
                ).fetchone()[0]
                == "unknown"
            )
            attempts.append(True)
            raise OSError("synthetic lost acknowledgement")
        return original(op, **fields)

    s.n.client.request = crash
    lifecycle.consume(s.n.service, "g")
    lifecycle.consume(s.n.service, "g")
    assert attempts == [True] and intents(s)[0]["state"] == "unknown"
    assert not s.n.cleanups and not s.n.ctl.journal.get("close")


def test_abort_interrupts_foreground_but_retains_chat_generation(sprint):
    s = sprint
    receipt = s.store.abort(
        s.sprint,
        sprint_domain.LifecycleActor("planner", 3),
        reason="Synthetic abort",
        terminal_outcome="cancelled",
    )
    lifecycle.consume(s.n.service, "g")
    assert receipt.changed and intents(s)[0]["lifecycle"] == "aborted"
    assert not s.n.cleanups and not s.n.ctl.journal.get("close")
    assert (
        s.con.execute("SELECT state FROM conversation_runtime_generations").fetchone()[
            0
        ]
        == "ready"
    )


def test_resume_native_lane_queues_distinct_notice_once_without_reaccept(sprint):
    s = sprint
    pause(s)
    s.con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?", (s.sprint,))
    s.con.commit()
    with db_driver.write_transaction(s.con, "synthetic.resume"):
        first = lifecycle.reenter(s.con, s.sprint)
        second = lifecycle.reenter(s.con, s.sprint)
    assert len(first) == 1 and not second
    message = s.con.execute(
        "SELECT message_kind,body FROM wake_message WHERE idempotency_key LIKE 'sprint-resume-native:%'"
    ).fetchone()
    assert (
        message["message_kind"] == "notification"
        and "do not re-accept or replay" in message["body"]
    )
    assert (
        s.con.execute(
            "SELECT generation_id FROM conversation_runtime_generations"
        ).fetchone()[0]
        == "g"
    )


def test_completed_developer_close_is_pending_before_artifact_and_slot_release(sprint):
    s = sprint
    s.con.execute(
        "UPDATE sprint_work_units SET disposition='completed',completed_at=datetime('now') WHERE sprint_id=?",
        (s.sprint,),
    )
    s.con.commit()
    with db_driver.write_transaction(s.con, "synthetic.completed"):
        closed = s.store.complete_in_transaction(
            s.sprint,
            sprint_domain.LifecycleActor("planner", 3),
            reason="Synthetic accepted delivery",
            terminal_outcome="accepted",
            cleanup_targets=(
                sprint_cleanup.CleanupTargetDraft(
                    "artifact_dir",
                    str(s.n.service.root / "shared/sprints" / f"sprint-{s.sprint}"),
                    str(s.n.service.root),
                    str(s.n.service.root / ".git"),
                ),
            ),
        )
    assert closed == () and not s.n.cleanups
    assert (
        s.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 1
    )
    assert not lifecycle.developer_cleanup_complete(s.con, s.sprint)
    assert active_chat_registry.native_cleanup_pending(s.con, 1)
    executor = sprint_cleanup.SprintCleanupExecutor.__new__(
        sprint_cleanup.SprintCleanupExecutor
    )
    executor.con = s.con
    executor.store = SimpleNamespace(claim_is_current=lambda _: True)
    executor._validate_repository_identity = lambda _: pytest.fail(
        "artifact mutation before native cleanup"
    )
    executor._validate_artifact_identity = lambda _: pytest.fail(
        "artifact mutation before native cleanup"
    )
    with pytest.raises(
        sprint_cleanup.SprintCleanupSafetyError, match="native ownership"
    ) as error:
        executor._validate_under_lock(SimpleNamespace(sprint_id=s.sprint))
    assert error.value.code == "native_cleanup_pending"
    s.n.service.close_generation("g", "cv", s.n.client, 1, 1)
    lifecycle.reconcile(s.con)
    assert lifecycle.developer_cleanup_complete(s.con, s.sprint)
    assert intents(s)[0]["state"] == "terminal"
    assert active_chat_registry.native_cleanup_pending(s.con, 1) is None


@pytest.mark.parametrize("obligation", ["unresolved_work", "unresolved_definitions"])
def test_exited_unit_does_not_release_native_definition_or_work_obligation(
    sprint, obligation
):
    s = sprint
    cleanup = {
        "outcome": "complete",
        "unit_verified_exited": True,
        obligation: ["retained-owned-id"],
    }
    s.con.execute(
        "UPDATE conversations SET state='closed',closed_at=datetime('now') WHERE conversation_id='cv'"
    )
    s.con.execute(
        "UPDATE conversation_runtime_generations SET state='closed',cleanup_json=?",
        (json.dumps(cleanup),),
    )
    s.con.commit()
    assert not lifecycle.developer_cleanup_complete(s.con, s.sprint)


def test_preparer_without_native_generation_retains_completed_close(sprint):
    s = sprint
    s.con.execute("DELETE FROM conversation_runtime_generations")
    runtime = {
        "role": "ordinary",
        "state": "preparing",
        "generation_id": "g",
        "preparation_owner": {"api_pid": 123},
    }
    s.con.execute(
        "UPDATE conversations SET runtime_projection=?", (json.dumps(runtime),)
    )
    s.con.commit()
    with db_driver.write_transaction(s.con, "synthetic.completed.preparer"):
        lifecycle.persist_developer_close(s.con, s.sprint)
    assert not lifecycle.developer_cleanup_complete(s.con, s.sprint)
    assert intents(s)[0][
        "state"
    ] == "pending" and active_chat_registry.native_cleanup_pending(s.con, 1)


def test_missing_generation_is_not_never_launched_proof(sprint):
    s = sprint
    s.con.execute("DELETE FROM conversation_runtime_generations")
    s.con.commit()
    with db_driver.write_transaction(s.con, "synthetic.completed.missing"):
        lifecycle.persist_developer_close(s.con, s.sprint)
    lifecycle.reconcile(s.con)
    assert not lifecycle.developer_cleanup_complete(s.con, s.sprint)
    assert intents(s)[0]["state"] == "pending"
    assert active_chat_registry.native_cleanup_pending(s.con, 1)


def test_completed_developer_leaves_planner_and_reviewer_chats_alive(sprint):
    s = sprint
    original = dict(
        s.con.execute(
            "SELECT * FROM conversations WHERE conversation_id='cv'"
        ).fetchone()
    )
    for shell, role in [(2, "reviewer"), (3, "planner")]:
        chat = original | {
            "conversation_id": role,
            "shell_id": shell,
            "creation_idempotency_key": role,
        }
        chat["runtime_projection"] = json.dumps(
            {"role": "ordinary", "state": "ready", "generation_id": role}
        )
        s.con.execute(
            f"INSERT INTO conversations({','.join(chat)}) VALUES({','.join('?' for _ in chat)})",
            tuple(chat.values()),
        )
        s.con.execute(
            "INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES(?,?,?,1,'codex','{}','ready',1,1)",
            (role, role, shell),
        )
        participant = s.con.execute(
            "SELECT participant_id FROM sprint_participants WHERE sprint_id=? AND role=?",
            (s.sprint, role),
        ).fetchone()[0]
        s.con.execute(
            "INSERT INTO sprint_participant_conversations(sprint_participant_id,conversation_id) VALUES(?,?)",
            (participant, role),
        )
    s.con.commit()
    with db_driver.write_transaction(s.con, "synthetic.complete.retain"):
        assert lifecycle.persist_developer_close(s.con, s.sprint) == ("cv",)
    assert [
        tuple(row)
        for row in s.con.execute(
            "SELECT generation_id,state,close_intent FROM conversation_runtime_generations WHERE generation_id!='g' ORDER BY generation_id"
        )
    ] == [("planner", "ready", 0), ("reviewer", "ready", 0)]
    assert len(intents(s)) == 1 and not s.n.cleanups


def test_paused_engine_wake_keeps_same_intent_then_resumes_without_reaccept(sprint):
    s = sprint
    with db_driver.write_transaction(s.con, "synthetic.wake"):
        wake = sprint_message_delivery.SprintMessageStore(s.con).send_in_transaction(
            s.sprint,
            to_participant_id=s.participant,
            message_kind="notification",
            body="Continue inbox",
            idempotency_key="synthetic-relay",
            work_unit_id=s.unit,
        )
    key = s.con.execute(
        "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
        (wake.wake_id,),
    ).fetchone()[0]
    mid = s.con.execute(
        "INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash) VALUES('cv','engine','sprint-runtime','prompt','Synthetic wake',?,'hash')",
        (key,),
    ).lastrowid
    s.con.execute(
        "INSERT INTO conversation_outbox(conversation_id,message_id) VALUES('cv',?)",
        (mid,),
    )
    s.con.commit()
    pause(s)
    s.n.ctl.emit(
        RuntimeEvent(
            "activity.terminal",
            NativeReference("root", thread_id="root", activity_id="A"),
        )
    )
    runtime = json.loads(
        s.con.execute("SELECT runtime_projection FROM conversations").fetchone()[0]
    )
    runtime["primary"] = None
    s.con.execute(
        "UPDATE conversations SET runtime_projection=?", (json.dumps(runtime),)
    )
    s.con.commit()
    s.n.service.dispatch_queued("g", "cv", s.n.client, 1, 1)
    assert (
        not s.n.driver.writes
        and s.con.execute("SELECT COUNT(*) FROM conversation_runs").fetchone()[0] == 0
    )
    s.con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?", (s.sprint,))
    s.con.commit()
    s.n.service.dispatch_queued("g", "cv", s.n.client, 1, 1)
    s.n.service.dispatch_queued("g", "cv", s.n.client, 1, 1)
    assert s.n.driver.writes == ["gui-message:" + str(mid)]
    assert (
        s.con.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
    )
    assert (
        s.con.execute(
            "SELECT generation_id FROM conversation_runtime_generations"
        ).fetchone()[0]
        == "g"
    )


def test_missing_link_cannot_complete_retained_developer_intent(sprint):
    assert not lifecycle.developer_cleanup_complete(
        sprint.con, sprint.sprint, conversation_id="unbound"
    )


@pytest.mark.parametrize("fields", [{"partial":True},{"freshness":"stale"}])
def test_root_uncertainty_after_pause_intent_is_refused_at_actual_control_edge(sprint, fields):
    s=sprint
    pause(s)
    original=s.n.ctl.call
    def race(function, *, deadline):
        s.n.ctl.emit(RuntimeEvent("activity.processed",NativeReference("root",thread_id="root",activity_id="A"),**fields))
        return original(function,deadline=deadline)
    s.n.ctl.call=race
    lifecycle.consume(s.n.service,"g")
    assert not s.n.driver.controls and not s.n.cleanups
    assert intents(s)[0]["state"]=="not_written"
    assert "inconclusive" in intents(s)[0]["detail"]



def test_later_uncertain_child_does_not_withdraw_known_root_eligibility(sprint):
    s=sprint
    s.n.ctl.emit(RuntimeEvent("activity.processed",NativeReference("root",thread_id="root",activity_id="A")))
    s.n.ctl.emit(RuntimeEvent("work.observed",NativeReference("root",thread_id="child",parent_thread_id="root",work_id="bg"),
        data={"kind":"task","state":"unknown"},partial=True,freshness="stale"))
    runtime=ingest_events(s)
    assert runtime["partial"] is True and runtime["freshness"]=="stale"
    pause(s)
    assert intents(s)[0]["command_id"] is not None
    lifecycle.consume(s.n.service,"g")
    assert s.n.driver.controls==[intents(s)[0]["command_id"]]


def test_unknown_root_occupancy_after_pause_never_dispatches_interrupt(sprint):
    s=sprint
    pause(s)
    s.n.ctl.observe_root_occupancy()
    lifecycle.consume(s.n.service,"g")
    assert not s.n.driver.controls and not s.n.cleanups
    assert intents(s)[0]["state"]=="not_written"
