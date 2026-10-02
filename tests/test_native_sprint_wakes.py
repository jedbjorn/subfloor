"""Exact controller wire plus synthetic canonical wake placement; no native launch."""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import conversation_native_chats
import sprint_message_delivery
import sprint_native_wakes
import test_conversation_runtime_foundation as foundation
import test_native_chat_ownership as ownership
from conversation_native_chats import NativeChatsService
from conversation_runtime import RuntimeClient
from conversation_runtime_contract import (
    NativeReference,
    RuntimeContractError,
    RuntimeEvent,
)

controller, control, wire, command = (
    foundation.controller,
    foundation.control,
    foundation.wire,
    foundation.command,
)
database = ownership.database


@pytest.fixture
def native(database, controller, monkeypatch):
    path, con = database
    ctl, driver = controller
    # Rebuild only the synthetic seed row with a selected route; production
    # route immutability remains enabled throughout the exercised operations.
    con.execute("DELETE FROM conversation_runtime_generations")
    con.execute("DELETE FROM active_shell_chats")
    con.execute("DELETE FROM conversations")
    con.execute(
        "INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash) VALUES('cv',1,1,'codex','selected-model','high',?,'key','hash')",
        (str(path.parent),),
    )
    con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'cv')")
    con.execute(
        "INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g','cv',1,1,'codex','{}','ready',1,1)"
    )
    con.execute(
        "UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",
        (json.dumps({"role": "ordinary", "state": "ready", "generation_id": "g"}),),
    )
    con.commit()
    supervisor = SimpleNamespace(stop=lambda g: {"os_cleanup": {"complete": True}})
    service = NativeChatsService(path, path.parent, supervisor)
    service.consumer = "api"
    lease = service.store.attach("g", 1, 1, service.consumer)

    class Client(RuntimeClient):
        def __init__(self):
            super().__init__(
                Path("/unused"),
                "g",
                consumer="api",
                controller_pid=1,
                controller_start_ticks=1,
            )
            self.lease = lease

        def request(self, op, **fields):
            return ctl.handle(wire(op, **fields))

    client = Client()
    service.attach = lambda generation: (client, 1, 1)
    monkeypatch.setattr(conversation_native_chats, "_SERVICE", service)
    cleanups = []
    original = driver.cleanup

    def cleanup(**fields):
        cleanups.append(True)
        return original(**fields)

    driver.cleanup = cleanup
    yield SimpleNamespace(
        con=con,
        service=service,
        client=client,
        ctl=ctl,
        driver=driver,
        cleanups=cleanups,
    )
    service.starts.shutdown(wait=False, cancel_futures=True)


def conditional(owner, *, epoch=None, seconds=0, ordinal=1, cid="wake-close"):
    epoch = owner.status()["quiet"]["epoch"] if epoch is None else epoch
    return control(
        cid,
        ordinal,
        action="close",
        options={"wake_quiet_epoch": epoch, "wake_quiet_seconds": seconds},
    )


def test_conditional_close_primary_race_proves_no_write_without_close_fence(controller):
    owner, _driver = controller
    intent = conditional(owner)
    owner.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference("root", thread_id="root", activity_id="autonomous"),
        )
    )
    assert owner.handle(wire("close", command=intent))["state"] == "not_written"
    assert not owner.journal.get("close") and owner.ready
    owner.emit(
        RuntimeEvent(
            "activity.terminal",
            NativeReference("root", thread_id="root", activity_id="autonomous"),
        )
    )
    # Old epoch cannot be silently rebound or replayed as a fresh intent.
    assert owner.handle(wire("close", command=intent))["state"] == "not_written"
    with pytest.raises(RuntimeContractError, match="identity changed"):
        owner.handle(wire("close", command=conditional(owner, cid="wake-close")))


@pytest.mark.parametrize("state", ["accepted", "written", "unknown"])
def test_outstanding_submission_prevents_quiet_close(controller, state):
    owner, _ = controller
    send = command("send", 1, text="one")
    if state == "accepted":
        owner.journal.reserve_command("send", 1, send["payload_digest"], "submit")
    else:
        owner.handle(wire("submit", command=send))
        if state == "unknown":
            from conversation_runtime_contract import WriteReceipt

            owner.journal.receipt("send", WriteReceipt("unknown"))
    assert (
        owner.handle(wire("close", command=conditional(owner, ordinal=2)))["state"]
        == "not_written"
    )
    assert not owner.journal.get("close")


def test_quiet_interval_and_background_scope(controller):
    owner, _ = controller
    status = owner.status()["quiet"]
    owner.emit(
        RuntimeEvent(
            "work.observed",
            NativeReference(
                "root",
                thread_id="child",
                parent_thread_id="root",
                activity_id="child-turn",
            ),
            data={"kind": "child", "state": "running"},
        )
    )
    assert owner.status()["quiet"] == status
    assert (
        owner.handle(wire("close", command=conditional(owner, seconds=10)))["state"]
        == "not_written"
    )
    owner.journal.set("idle_since", time.time() - 11)
    # Retrying the exact proved no-write intent is safe; one Close fence owns BG.
    assert (
        owner.handle(wire("close", command=conditional(owner, seconds=10)))["outcome"]
        == "complete"
    )
    assert owner.journal.get("close")


def test_quiet_no_write_keeps_generation_and_root_owned(native):
    n = native
    service, observation = sprint_native_wakes.observe(n.con, "cv")
    service.request_close(n.con, "cv", 1, 1, wake_id=9, observation=observation)
    n.ctl.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference("root", thread_id="root", activity_id="raced"),
        )
    )
    service.close_generation("g", "cv", n.client, 1, 1)
    row = n.con.execute("SELECT * FROM conversation_runtime_generations").fetchone()
    assert row["state"] == "ready" and not row["close_intent"] and not n.cleanups
    assert (
        n.con.execute("SELECT state FROM conversation_runtime_commands").fetchone()[0]
        == "not_written"
    )
    assert n.con.execute("SELECT state FROM conversations").fetchone()[0] != "closed"


def test_operator_close_cannot_be_unfenced_by_delayed_wake_refusal(native):
    n = native
    _, observed = sprint_native_wakes.observe(n.con, "cv")
    n.service.request_close(n.con, "cv", 1, 1, wake_id=10, observation=observed)
    request = n.client.request

    def replace(op, **fields):
        if op == "close":
            version = n.con.execute("SELECT version FROM conversations").fetchone()[0]
            n.service.request_close(n.con, "cv", 1, version)
            return {"state": "not_written", "detail": "WAKE_NOT_QUIET"}
        return request(op, **fields)

    n.client.request = replace
    n.service.close_generation("g", "cv", n.client, 1, 1)
    assert n.service.close_id("g", "cv") == "close:g"
    assert (
        n.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 1
    )
    assert (
        json.loads(
            n.con.execute("SELECT runtime_projection FROM conversations").fetchone()[0]
        )["state"]
        == "closing"
    )


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "owner",
        "queued",
        "unknown",
        "stale",
        "service",
        "role",
        "lease",
        "shell_owner",
    ],
)
def test_final_close_gate_refuses_changed_canonical_or_observed_ownership(
    native, monkeypatch, change
):
    n = native
    _, observed = sprint_native_wakes.observe(n.con, "cv")
    if change == "generation":
        n.con.execute(
            "UPDATE conversations SET runtime_projection=?",
            (
                json.dumps(
                    {
                        "generation_id": "replacement",
                        "state": "ready",
                        "role": "ordinary",
                    }
                ),
            ),
        )
    elif change == "owner":
        n.con.execute("INSERT INTO users(user_id,username) VALUES(2,'other')")
        n.con.execute("UPDATE conversation_runtime_generations SET owner_user_id=2")
    elif change == "queued":
        n.con.execute(
            "INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash,state) VALUES('cv','user','1','prompt','q','q','h','queued')"
        )
    elif change == "unknown":
        n.con.execute(
            "INSERT INTO conversation_runtime_commands VALUES('g','unknown',1,'submit','digest','{}','unknown','{}')"
        )
    elif change == "stale":
        observed["observed_at"] = time.time() - 3
    elif change == "role":
        n.con.execute(
            "UPDATE conversations SET runtime_projection=?",
            (json.dumps({"generation_id": "g", "state": "ready", "role": "probe"}),),
        )
    elif change == "lease":
        n.con.execute(
            "UPDATE conversation_runtime_generations SET consumer_fence=consumer_fence+1"
        )
    elif change == "shell_owner":
        n.con.execute("UPDATE shells SET user_id=NULL")
    else:
        monkeypatch.setattr(conversation_native_chats, "_SERVICE", None)
    n.con.commit()
    with pytest.raises(RuntimeContractError):
        n.service.request_close(n.con, "cv", 1, 1, wake_id=11, observation=observed)
    assert (
        n.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 0
    )
    assert not n.cleanups


def test_force_new_waits_current_quiet_and_cleanup_before_replacement(native):
    n = native
    store = sprint_message_delivery.SprintMessageStore(n.con)
    receipt = store.send_to_shell(
        1,
        message_kind="notification",
        body="synthetic",
        idempotency_key="wake-key",
        declared_type="force-new",
    )
    now = datetime.now(timezone.utc)
    delivery = sprint_message_delivery.SprintWakeDeliveryService(n.con, now=lambda: now)
    assert delivery.claim_next("worker") is None
    now += timedelta(seconds=11)
    lease = delivery.claim_next("worker")
    assert lease.wake_id == receipt.wake_id
    with pytest.raises(sprint_message_delivery.ForceNewDeferred):
        delivery._resolve_conversation(lease)
    assert (
        n.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 1
    )
    assert n.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    # The 10-second controller interval is independent of API test time.
    n.ctl.journal.set("idle_since", time.time() - 11)
    n.service.close_generation("g", "cv", n.client, 1, 1)
    assert sprint_native_wakes.released(n.con, "cv")


@pytest.mark.parametrize("declared", ["re-enter", "new"])
def test_busy_wake_uses_same_native_chat_and_stable_engine_delivery(native, declared):
    n = native
    n.ctl.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference("root", thread_id="root", activity_id="busy"),
        )
    )
    receipt = sprint_message_delivery.SprintMessageStore(n.con).send_to_shell(
        1,
        message_kind="notification",
        body="synthetic",
        idempotency_key="wake-key",
        declared_type=declared,
    )
    calls = []
    result = sprint_message_delivery.SprintWakeDeliveryService(n.con).deliver_once(
        "worker", lambda cid, text, key: calls.append((cid, key)) or "engine-run"
    )
    assert result.state == "delivered" and calls == [
        ("cv", "receiver:1:wake-for-message:" + str(receipt.message_id))
    ]
    assert (
        not n.cleanups
        and n.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    )


def test_expired_consumer_cannot_unfence_but_fresh_consumer_recovers_proved_no_write(
    native, monkeypatch
):
    n = native
    _, observation = sprint_native_wakes.observe(n.con, "cv")
    n.service.request_close(n.con, "cv", 1, 1, wake_id=12, observation=observation)
    n.ctl.emit(
        RuntimeEvent(
            "activity.started",
            NativeReference("root", thread_id="root", activity_id="raced"),
        )
    )
    request = n.client.request

    def expire(op, **fields):
        result = request(op, **fields)
        if op == "close":
            n.con.execute(
                "UPDATE conversation_runtime_generations SET consumer_expires=0"
            )
            n.con.commit()
        return result

    n.client.request = expire
    # The old result is durable, but its expired consumer cannot clear the fence.
    n.service.close_generation("g", "cv", n.client, 1, 1)
    assert (
        n.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 1
    )
    replacement = NativeChatsService(
        n.service.database, n.service.root, n.service.supervisor
    )
    lease = replacement.store.attach("g", 1, 1, replacement.consumer)
    replacement.release_wake_close(
        "g", "cv", 1, 1, replacement.close_id("g", "cv"), lease["fence"]
    )
    assert (
        n.con.execute(
            "SELECT close_intent FROM conversation_runtime_generations"
        ).fetchone()[0]
        == 0
    )
    assert not n.cleanups and n.ctl.ready
    replacement.starts.shutdown(wait=False, cancel_futures=True)


def test_unknown_close_retains_ownership_after_os_exit_and_never_becomes_quiet(native):
    n = native
    _, observation = sprint_native_wakes.observe(n.con, "cv")
    n.service.request_close(n.con, "cv", 1, 1, wake_id=13, observation=observation)
    request = n.client.request

    def lose_ack(op, **fields):
        if op == "close":
            raise OSError("synthetic lost ack")
        return request(op, **fields)

    n.client.request = lose_ack
    n.service.close_generation("g", "cv", n.client, 1, 1)
    row = n.con.execute("SELECT * FROM conversation_runtime_generations").fetchone()
    assert row["state"] == "lost" and row["close_intent"] == 1
    assert json.loads(row["cleanup_json"])["unit_verified_exited"] is True
    assert not sprint_native_wakes.released(n.con, "cv")
    assert (
        n.con.execute("SELECT state FROM conversation_runtime_commands").fetchone()[0]
        == "unknown"
    )


def test_verified_rotation_creates_one_checked_native_chat_without_old_generation(
    native, monkeypatch
):
    import sprint_participant_chats
    from sprint_route_binding_support import candidate

    n = native

    def resolve(harness, model, effort):
        binding = candidate(
            n.con,
            {"participant_id": 1, "harness": harness, "model": model, "effort": effort},
        ).binding
        binding["selector_binding"].update(
            proof_state="checked_native_selection",
            native_fingerprint="a" * 64,
            native_executable_version="fixture",
        )
        return binding, sprint_participant_chats.route_bindings.digest_json(binding)

    n.service.route_resolver = resolve
    monkeypatch.setattr(
        sprint_participant_chats.run_mod, "shell_work_dir", lambda *_: n.service.root
    )
    receipt = sprint_message_delivery.SprintMessageStore(n.con).send_to_shell(
        1,
        message_kind="notification",
        body="synthetic",
        idempotency_key="rotation",
        declared_type="force-new",
    )
    delivery = sprint_message_delivery.SprintWakeDeliveryService(
        n.con, force_new_quiet_seconds=0
    )
    lease = delivery.claim_next("worker")
    with pytest.raises(sprint_message_delivery.ForceNewDeferred):
        delivery._resolve_conversation(lease)
    n.service.close_generation("g", "cv", n.client, 1, 1)
    cid = delivery._resolve_conversation(lease)
    chat = n.con.execute(
        "SELECT * FROM conversations WHERE conversation_id=?", (cid,)
    ).fetchone()
    assert (
        chat["runtime_mode"] == "native_experiment"
        and chat["route_contract_version"] == 2
    )
    assert json.loads(chat["runtime_projection"]) == {
        "role": "ordinary",
        "state": "starting",
        "source": "wake",
        "wake_id": receipt.wake_id,
    }
    assert (
        n.con.execute(
            "SELECT COUNT(*) FROM conversation_runtime_generations WHERE conversation_id=?",
            (cid,),
        ).fetchone()[0]
        == 0
    )
    assert delivery._resolve_conversation(lease) == cid
    assert n.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2


@pytest.mark.parametrize(
    "flags", [{"partial": True}, {"freshness": "stale"}, {"grade": "inconclusive"}]
)
def test_uncertain_primary_terminal_is_not_quiet_until_current_reconciliation(
    controller, flags
):
    owner, _ = controller
    ref = NativeReference("root", thread_id="root", activity_id="turn")
    owner.emit(RuntimeEvent("activity.started", ref))
    owner.emit(RuntimeEvent("activity.terminal", ref, **flags))
    assert owner.status()["quiet"]["idle"] is False
    assert (
        owner.handle(wire("close", command=conditional(owner)))["state"]
        == "not_written"
    )
    owner.emit(RuntimeEvent("activity.terminal", ref))
    assert owner.status()["quiet"]["idle"] is True


def test_refused_wake_commands_cannot_consume_unconditional_close_reserve(tmp_path):
    from conversation_runtime_controller import Journal

    journal = Journal(tmp_path / "bounded.sqlite", max_commands=4, reserve=2)
    quiet = {"wake_quiet_epoch": 99, "wake_quiet_seconds": 0}
    for ordinal in (1, 2):
        assert (
            journal.reserve_command(
                str(ordinal),
                ordinal,
                str(ordinal),
                "control",
                closing=True,
                quiet=quiet,
            )["state"]
            == "not_written"
        )
    with pytest.raises(RuntimeContractError, match="dispatch refused"):
        journal.reserve_command("3", 3, "3", "control", closing=True, quiet=quiet)
    assert (
        journal.reserve_command("operator-close", 3, "close", "control", closing=True)
        is None
    )
    assert journal.get("close")


def test_lost_controller_no_write_does_not_restore_false_ready(native):
    n = native
    _, observation = sprint_native_wakes.observe(n.con, "cv")
    n.service.request_close(n.con, "cv", 1, 1, wake_id=14, observation=observation)
    with pytest.raises(RuntimeContractError):
        n.ctl.emit(
            RuntimeEvent(
                "activity.started",
                NativeReference("foreign", thread_id="foreign", activity_id="foreign"),
            )
        )
    n.service.close_generation("g", "cv", n.client, 1, 1)
    row = n.con.execute(
        "SELECT state,close_intent FROM conversation_runtime_generations"
    ).fetchone()
    assert tuple(row) == ("lost", 0)
    assert (
        json.loads(
            n.con.execute("SELECT runtime_projection FROM conversations").fetchone()[0]
        )["state"]
        == "lost"
    )
    assert not n.cleanups and not sprint_native_wakes.released(n.con, "cv")
