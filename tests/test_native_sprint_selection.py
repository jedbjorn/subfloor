"""Synthetic source admission/race proof; no native behavior certification."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import active_chat_registry
import conversation_native_chats
import migrate
import pytest
import server
import sprint_board
import sprint_domain
import sprint_native_selection
import sprint_participant_chats as chats
from conversation_runtime_contract import RuntimeContractError
from sprint_route_binding_support import candidate
from test_sprint_v2_domain import ENGINE, SprintDomainCase, apply_schema


@pytest.fixture
def selection(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    apply_schema(con)
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'synthetic')")
    for shell, name, flavor in (
        (1, "DEV1", "dev"),
        (2, "REV1", "reviewer"),
        (3, "PLN1", "planner"),
    ):
        con.execute(
            "INSERT INTO shells(shell_id,display_name,shortname,flavor,system_prompt,user_id) VALUES(?,?,?,?,?,1)",
            (shell, name, name, flavor, "Synthetic selection fixture"),
        )
    state = SimpleNamespace(con=con, serial=0)
    sprint, _ = SprintDomainCase.create_sprint(state)
    con.execute(
        "UPDATE sprint_participants SET harness='codex',model='selected-model',effort='high',runtime_mode='native_experiment' WHERE sprint_id=?",
        (sprint,),
    )
    con.commit()
    service = SimpleNamespace(database=database, calls=0, callback=None, failure=None)

    def resolve(harness, model, effort):
        service.calls += 1
        if service.failure:
            raise service.failure
        binding = candidate(
            con,
            {"participant_id": 1, "harness": harness, "model": model, "effort": effort},
        ).binding
        binding["selector_binding"].update(
            proof_state="checked_native_selection",
            native_fingerprint="a" * 64,
            native_executable_version="0.159.1-test",
        )
        if service.callback:
            service.callback()
        return binding, chats.route_bindings.digest_json(binding)

    service.resolve_route = resolve
    monkeypatch.setattr(conversation_native_chats, "_SERVICE", service)
    monkeypatch.setattr(
        chats.run_mod, "shell_work_dir", lambda short, flavor: tmp_path / short.lower()
    )
    store = sprint_domain.SprintLifecycleStore(
        con, probe_harness=lambda _: pytest.fail("legacy probe on native mode")
    )
    try:
        yield SimpleNamespace(
            con=con, sprint=sprint, service=service, store=store, database=database
        )
    finally:
        con.close()


def developer(f):
    return f.con.execute(
        "SELECT participant_id FROM sprint_participants WHERE sprint_id=? AND role='developer'",
        (f.sprint,),
    ).fetchone()[0]


def arm(f):
    return f.store.arm(f.sprint, 3)


def test_native_arm_binding_creation_and_board_use_real_writer(selection):
    f = selection
    arm(f)
    participant = developer(f)
    row = f.con.execute(
        "SELECT * FROM sprint_participant_route_bindings WHERE participant_id=?",
        (participant,),
    ).fetchone()
    assert row["contract_version"] == 2
    assert row["harness_evidence_format"] == "raw-observed-v1"
    assert row["source_fingerprint"] == "a" * 64
    assert row["harness_version"] == "0.159.1-test"
    route = chats.prepare_wake_conversation(
        f.con, sprint_id=f.sprint, participant_id=participant
    )
    with f.con:
        f.con.execute("BEGIN")
        cid = chats.create_prepared_wake_conversation(f.con, wake_id=101, route=route)
    chat = f.con.execute(
        "SELECT * FROM conversations WHERE conversation_id=?", (cid,)
    ).fetchone()
    assert chat["runtime_mode"] == "native_experiment"
    runtime = json.loads(chat["runtime_projection"])
    assert runtime == {
        "role": "ordinary",
        "state": "starting",
        "source": "sprint",
        "sprint_id": f.sprint,
        "participant_id": participant,
        "wake_id": 101,
    }
    assert chat["route_binding"] == chats.route_bindings.canonical_json(route.binding)
    board = sprint_board.SprintBoardProjection(f.con).board(f.sprint)
    assert {p["runtime_mode"] for p in board["participants"]} == {"native_experiment"}


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeContractError("CAPABILITY_INCONCLUSIVE", "missing"),
        RuntimeContractError("CAPABILITY_INCOMPATIBLE", "changed"),
    ],
)
def test_native_arm_fails_without_writing_bindings_or_wakes(selection, failure):
    f = selection
    f.service.failure = failure
    with pytest.raises(sprint_domain.SprintPreflightError):
        arm(f)
    assert (
        f.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?", (f.sprint,)
        ).fetchone()[0]
        == "prepared"
    )
    assert (
        f.con.execute(
            "SELECT COUNT(*) FROM sprint_participant_route_bindings"
        ).fetchone()[0]
        == 0
    )
    assert f.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["owner", "mode", "intent"])
def test_native_resolver_rechecks_owner_mode_and_intent_before_admission(
    selection, change
):
    f = selection
    participant = developer(f)

    def mutate():
        if change == "owner":
            f.con.execute("UPDATE shells SET user_id=NULL WHERE shell_id=1")
        elif change == "mode":
            f.con.execute(
                "UPDATE sprint_participants SET runtime_mode='ephemeral' WHERE participant_id=?",
                (participant,),
            )
        else:
            f.con.execute(
                "UPDATE sprint_participants SET model='changed' WHERE participant_id=?",
                (participant,),
            )

    f.service.callback = mutate
    with pytest.raises(ValueError, match="changed during observation"):
        sprint_native_selection.checked_binding(
            f.con, participant, "codex", "selected-model", "high"
        )
    assert (
        f.con.execute(
            "SELECT COUNT(*) FROM sprint_participant_route_bindings"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("change", ["owner", "proof", "intent"])
def test_prepared_wake_rechecks_selection_before_creation(selection, change):
    f = selection
    arm(f)
    participant = developer(f)
    route = chats.prepare_wake_conversation(
        f.con, sprint_id=f.sprint, participant_id=participant
    )
    if change == "owner":
        f.con.execute("UPDATE shells SET user_id=NULL WHERE shell_id=1")
    elif change == "proof":
        f.con.execute("BEGIN")
        f.service.failure = RuntimeContractError(
            "CAPABILITY_INCONCLUSIVE", "replacement observed"
        )
    else:
        # Intent updates without touching an immutable captured binding cannot
        # reinterpret that binding as the newly requested route.
        f.con.execute(
            "UPDATE sprint_participants SET model='changed' WHERE participant_id=?",
            (participant,),
        )
    with pytest.raises(chats.SprintConversationError):
        chats.create_prepared_wake_conversation(f.con, wake_id=101, route=route)
    assert f.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_retained_native_cleanup_blocks_creation_even_without_registry(
    selection, monkeypatch
):
    f = selection
    arm(f)
    route = chats.prepare_wake_conversation(
        f.con, sprint_id=f.sprint, participant_id=developer(f)
    )
    f.con.execute("BEGIN")
    monkeypatch.setattr(
        active_chat_registry,
        "native_cleanup_pending",
        lambda con, shell: "retained-generation",
    )
    with pytest.raises(active_chat_registry.ActiveChatBusy, match="pending cleanup"):
        chats.create_prepared_wake_conversation(f.con, wake_id=101, route=route)
    assert f.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_foreign_database_or_pending_binding_cannot_be_native_admission(selection):
    f = selection
    participant = developer(f)
    f.service.database = f.database.with_name("foreign.db")
    with pytest.raises(ValueError, match="matching owned"):
        sprint_native_selection.checked_binding(
            f.con, participant, "codex", "selected-model", "high"
        )
    assert f.service.calls == 0
    f.service.database = f.database
    original = f.service.resolve_route

    def pending(*args):
        binding, _ = original(*args)
        binding["selector_binding"]["proof_state"] = "pending_finite_probe"
        return binding, chats.route_bindings.digest_json(binding)

    f.service.resolve_route = pending
    with pytest.raises(ValueError, match="exact checked"):
        sprint_native_selection.checked_binding(
            f.con, participant, "codex", "selected-model", "high"
        )


def test_native_runtime_mode_cannot_change_after_arm(selection):
    f = selection
    arm(f)
    with pytest.raises(sqlite3.IntegrityError, match="only while prepared"):
        f.con.execute(
            "UPDATE sprint_participants SET runtime_mode='ephemeral' WHERE participant_id=?",
            (developer(f),),
        )


def test_mode_is_in_arm_snapshot_and_reroute_preserves_it(selection):
    f = selection
    before = f.store._participant_intent_fingerprint(f.sprint)
    f.con.execute(
        "UPDATE sprint_participants SET runtime_mode='ephemeral' WHERE participant_id=?",
        (developer(f),),
    )
    assert f.store._participant_intent_fingerprint(f.sprint) != before
    f.con.execute(
        "UPDATE sprint_participants SET runtime_mode='native_experiment' WHERE participant_id=?",
        (developer(f),),
    )
    f.con.commit()
    route_store = sprint_domain.SprintParticipantStore(
        f.con, probe_harness=lambda _: pytest.fail("legacy probe")
    )
    route_store.reroute(
        f.sprint,
        3,
        participant_shell_id=1,
        harness="codex",
        model="new-selection",
        effort="high",
    )
    assert (
        f.con.execute(
            "SELECT runtime_mode FROM sprint_participants WHERE participant_id=?",
            (developer(f),),
        ).fetchone()[0]
        == "native_experiment"
    )


@pytest.mark.parametrize("runtime_mode", ["ephemeral", "native_experiment"])
def test_actual_declaration_handler_persists_explicit_mode(selection, runtime_mode):
    f = selection
    document = f.con.execute(
        "SELECT document_id FROM sprint_specs WHERE sprint_id=?", (f.sprint,)
    ).fetchone()[0]
    feature = f.con.execute(
        "SELECT feature_id FROM sprints WHERE sprint_id=?", (f.sprint,)
    ).fetchone()[0]
    participants = [
        {
            "shell_id": shell,
            "role": role,
            "harness": "codex",
            "model": "selected-model",
            "effort": "high",
            **(
                {"runtime_mode": runtime_mode}
                if runtime_mode == "native_experiment"
                else {}
            ),
        }
        for shell, role in ((3, "planner"), (1, "developer"), (2, "reviewer"))
    ]
    handler = object.__new__(server.Handler)
    sid = handler._declare_sprint(
        f.con,
        3,
        {
            "feature_id": feature,
            "spec_document_ids": [document],
            "merge_grant_enabled": True,
            "participants": participants,
        },
    )
    assert {
        row[0]
        for row in f.con.execute(
            "SELECT runtime_mode FROM sprint_participants WHERE sprint_id=?", (sid,)
        )
    } == {runtime_mode}


def test_dirty_migration_preserves_rows_and_ledger_retry_skips_delta(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.row_factory = sqlite3.Row
    apply_schema(con, through="0274_native_chats_experiment.sql")
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'synthetic')")
    con.execute(
        "INSERT INTO shells(shell_id,display_name,shortname,flavor,system_prompt,user_id,current_state) VALUES(1,'Planner','PLN1','planner','prompt',1,'retain')"
    )
    feature = con.execute(
        "INSERT INTO roadmap(title) VALUES('migration fixture')"
    ).lastrowid
    sprint = con.execute(
        "INSERT INTO sprints(feature_id,originating_planner_shell_id,merge_grant_enabled) VALUES(?,1,1)",
        (feature,),
    ).lastrowid
    con.execute(
        "INSERT INTO sprint_participants(sprint_id,shell_id,role,harness) VALUES(?,1,'planner','codex')",
        (sprint,),
    )
    con.commit()
    migration = ENGINE / "migrations/0275_native_sprint_selection.sql"
    migrate.applied_set(con)
    migrate.apply(con, migration)
    assert migration.name in migrate.applied_set(con)
    assert migration not in migrate.pending(con)
    assert (
        con.execute("SELECT runtime_mode FROM sprint_participants").fetchone()[0]
        == "ephemeral"
    )
    assert (
        con.execute("SELECT current_state FROM shells WHERE shell_id=1").fetchone()[0]
        == "retain"
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename=?", (migration.name,)
        ).fetchone()[0]
        == 1
    )
    con.close()
