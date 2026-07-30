"""Browser turns use the canonical shell launch path before native dispatch."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import run as run_mod  # noqa: E402
from conversation_broker import BrokerRun  # noqa: E402
from conversation_launch import (  # noqa: E402
    ConversationLaunchError,
    ConversationLaunchPreparer,
)


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())


def make_run(
    worktree: Path,
    *,
    conversation_id: str = "cv_" + "a" * 32,
    harness: str = "codex",
    model: str = "gpt-test",
    session_before: str | None = None,
) -> BrokerRun:
    return BrokerRun(
        run_id=7,
        conversation_id=conversation_id,
        message_id=8,
        shell_id=1,
        harness=harness,
        provider="openai",
        model=model,
        effort="high",
        worktree=worktree,
        title="A chat",
        body="Do the work",
        session_before=session_before,
        session_after=None,
        runner_ref=None,
        state="leased",
    )


@pytest.fixture
def launch_case():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "shell.db"
        con = sqlite3.connect(db_path)
        apply_schema(con)
        con.execute(
            "INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev','dev','prompt',1)"
        )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,mode,owner_user_id,harness,provider,"
            "model,effort,worktree,state,creation_idempotency_key,"
            "creation_request_hash) "
            "VALUES (?,1,'normal',1,'codex','openai','gpt-test','high',"
            "?,'idle','normal-create','normal-hash')",
            ("cv_" + "a" * 32, str(root / ".sc-worktrees" / "dev")),
        )
        con.commit()
        con.close()
        worktree = root / ".sc-worktrees" / "dev"
        worktree.mkdir(parents=True)
        yield db_path, worktree


def bind_sprint_assignment(
    db_path: Path,
    worktree: Path,
    *,
    role: str = "developer",
    state: str = "active",
    session_ref: str | None = None,
) -> BrokerRun:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    conversation_id = "cv_" + "s" * 32
    try:
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            ("cv_" + "a" * 32,),
        )
        con.execute(
            "INSERT INTO documents (document_id,kind,title,body) "
            "VALUES (24,'doc','SPRINT: launch contract','sprint')"
        )
        con.execute(
            "INSERT INTO documents (document_id,kind,title,body) "
            "VALUES (25,'spec','Browser Sprint','spec')"
        )
        con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,spec_doc_id,planner_route,dev_route,"
            "reviewer_route,state,legacy) "
            "VALUES (24,25,'codex/gpt-test','codex/gpt-test',"
            "'codex/gpt-test','active',1)"
        )
        if role == "conductor":
            con.execute(
                "UPDATE shells SET shortname='con',flavor='conductor' "
                "WHERE shell_id=1"
            )
            harness, model = "opencode", "openai/gpt-5.6-luna"
            unit_id = source_directive_id = required_result = None
        else:
            harness, model = "codex", "gpt-test"
            unit_id = con.execute(
                "INSERT INTO sprint_units "
                "(sprint_doc_id,seq,unit_title,dev_shell_id,state,branch) "
                "VALUES (24,'U1','Launch contract',1,'working',"
                "'feat/launch-contract')"
            ).lastrowid
            source_directive_id = con.execute(
                "INSERT INTO directives "
                "(issuer_flavor,kind,target,sprint_doc_id,unit_id) "
                "VALUES ('system','stall','conductor',24,?)",
                (unit_id,),
            ).lastrowid
            required_result = "unit-report"
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,mode,sprint_doc_id,harness,provider,"
            "model,effort,worktree,harness_session_ref,state,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (?,1,'sprint',24,?,'openai',?,'high',?,?,"
            "'idle','sprint-create','sprint-hash')",
            (
                conversation_id,
                harness,
                model,
                str(worktree),
                session_ref,
            ),
        )
        con.execute(
            "INSERT INTO sprint_conversation_bindings "
            "(conversation_id,sprint_doc_id,role,lifecycle,slot,unit_id,"
            "source_directive_id,required_result_kind,state,started_at) "
            "VALUES (?,24,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                role,
                "persistent" if role == "conductor" else "one_shot",
                "con" if role == "conductor" else "dev",
                unit_id,
                source_directive_id,
                required_result,
                state,
                "2026-07-30 00:00:00" if state == "active" else None,
            ),
        )
        con.commit()
    finally:
        con.close()
    return make_run(
        worktree,
        conversation_id=conversation_id,
        harness=harness,
        model=model,
        session_before=session_ref,
    )


def test_preparer_returns_canonical_environment_and_archive(launch_case):
    db_path, worktree = launch_case
    called = []

    def prepare(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(
            cwd=str(worktree),
            archive_id=42,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={"SC_API_TOKEN": "shell-token", "MARKER": "prepared"},
        )

    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: {"supported": True, "processes": []},
    )
    context, archive_id = preparer(make_run(worktree))

    assert archive_id == 42
    assert context.worktree == worktree
    assert context.env["SC_API_TOKEN"] == "shell-token"
    assert context.permission_mode == "unrestricted"
    assert called == [{
        "shell_id": 1,
        "harness": "codex",
        "model": "gpt-test",
        "effort": "high",
        "headless_prompt": "Do the work",
    }]


def test_sprint_worker_launch_injects_validated_assignment_context(launch_case):
    db_path, worktree = launch_case
    run = bind_sprint_assignment(db_path, worktree)
    called = []

    def prepare(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(
            cwd=str(worktree),
            archive_id=43,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={
                "SC_API_TOKEN": "shell-token",
                **run_mod.sprint_launch_env(kwargs["slot_context"]),
            },
        )

    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: {"supported": True, "processes": []},
    )
    context, archive_id = preparer(run)

    sprint = called[0]["slot_context"]
    assert archive_id == 43
    assert sprint["role"] == "developer"
    assert sprint["lifecycle"] == "one_shot"
    assert sprint["slot"] == "dev"
    assert sprint["spec_doc_id"] == 25
    assert sprint["source_directive_id"]
    assert sprint["required_result_kind"] == "unit-report"
    assert sprint["result_target_slot"] == "dev"
    assert sprint["units"] == [{
        "unit_id": sprint["units"][0]["unit_id"],
        "seq": "U1",
        "unit_title": "Launch contract",
        "state": "working",
        "depends_on": None,
        "overlap": None,
        "branch": "feat/launch-contract",
        "pr_number": None,
    }]
    assert context.env["SC_SPRINT_REF"] == "24"
    assert context.env["SC_SPRINT_ROLE"] == "developer"
    assert context.env["SC_SPRINT_SLOT"] == "dev"
    assert context.env["SC_SPRINT_UNIT"] == "U1"
    assert context.env["SC_SPRINT_REQUIRED_RESULT_KIND"] == "unit-report"
    assert context.env["SC_SPRINT_RESULT_TARGET"] == "dev"


def test_persistent_conductor_preserves_exact_session_resume(launch_case):
    db_path, worktree = launch_case
    run = bind_sprint_assignment(
        db_path,
        worktree,
        role="conductor",
        session_ref="ses_exact",
    )
    called = []

    def prepare(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(
            cwd=str(worktree),
            archive_id=44,
            harness="opencode",
            model="openai/gpt-5.6-luna",
            effort="high",
            env=run_mod.sprint_launch_env(kwargs["slot_context"]),
        )

    context, archive_id = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: {"supported": True, "processes": []},
    )(run)

    assert archive_id == 44
    assert run.session_before == "ses_exact"
    assert called[0]["slot_context"]["role"] == "conductor"
    assert called[0]["slot_context"]["lifecycle"] == "persistent"
    assert context.env["SC_SPRINT_ROLE"] == "conductor"


def test_one_shot_assignment_refuses_native_session_reuse(launch_case):
    db_path, worktree = launch_case
    run = bind_sprint_assignment(
        db_path,
        worktree,
        session_ref="ses_must_not_resume",
    )
    called = False

    def prepare(**_kwargs):
        nonlocal called
        called = True

    with pytest.raises(ConversationLaunchError) as caught:
        ConversationLaunchPreparer(
            db_path,
            prepare_launch=prepare,
            liveness=lambda: {"supported": True, "processes": []},
        )(run)

    assert caught.value.code == "SPRINT_ONE_SHOT_ALREADY_STARTED"
    assert not called


def test_sprint_launch_rejects_missing_binding_and_inactive_sprint(launch_case):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    conversation_id = "cv_" + "m" * 32
    con.execute(
        "UPDATE conversations SET state='closed',closed_at=datetime('now')"
    )
    con.execute(
        "INSERT INTO documents (document_id,kind,title,body) "
        "VALUES (24,'doc','SPRINT: missing binding','sprint')"
    )
    con.execute(
        "INSERT INTO sprints (sprint_doc_id,state,legacy) "
        "VALUES (24,'declared',1)"
    )
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,mode,sprint_doc_id,harness,model,effort,"
        "worktree,state,creation_idempotency_key,creation_request_hash) "
        "VALUES (?,1,'sprint',24,'codex','gpt-test','high',?,'idle',"
        "'missing-binding','hash')",
        (conversation_id, str(worktree)),
    )
    con.commit()
    con.close()
    run = make_run(worktree, conversation_id=conversation_id)

    with pytest.raises(ConversationLaunchError) as missing:
        ConversationLaunchPreparer(
            db_path,
            prepare_launch=lambda **_: None,
            liveness=lambda: {"supported": True, "processes": []},
        )(run)
    assert missing.value.code == "SPRINT_BINDING_REQUIRED"

    con = sqlite3.connect(db_path)
    unit_id = con.execute(
        "INSERT INTO sprint_units "
        "(sprint_doc_id,seq,unit_title,dev_shell_id,state) "
        "VALUES (24,'U1','Unit',1,'working')"
    ).lastrowid
    directive_id = con.execute(
        "INSERT INTO directives "
        "(issuer_flavor,kind,target,sprint_doc_id,unit_id) "
        "VALUES ('system','stall','conductor',24,?)",
        (unit_id,),
    ).lastrowid
    con.execute(
        "INSERT INTO sprint_conversation_bindings "
        "(conversation_id,sprint_doc_id,role,lifecycle,slot,unit_id,"
        "source_directive_id,required_result_kind) "
        "VALUES (?,24,'developer','one_shot','dev',?,?,'unit-report')",
        (conversation_id, unit_id, directive_id),
    )
    con.commit()
    con.close()

    with pytest.raises(ConversationLaunchError) as inactive:
        ConversationLaunchPreparer(
            db_path,
            prepare_launch=lambda **_: None,
            liveness=lambda: {"supported": True, "processes": []},
        )(run)
    assert inactive.value.code == "SPRINT_NOT_LAUNCHABLE"


def test_conductor_policy_rejects_non_opencode_before_preparation(launch_case):
    db_path, worktree = launch_case
    run = bind_sprint_assignment(db_path, worktree, role="conductor")
    run = make_run(
        worktree,
        conversation_id=run.conversation_id,
        harness="codex",
        model="gpt-test",
    )
    called = False

    def prepare(**_kwargs):
        nonlocal called
        called = True

    with pytest.raises(ConversationLaunchError) as caught:
        ConversationLaunchPreparer(
            db_path,
            prepare_launch=prepare,
            liveness=lambda: {"supported": True, "processes": []},
        )(run)

    assert caught.value.code == "SPRINT_ROUTE_MISMATCH"
    assert not called


def test_preparer_refuses_a_cli_process_holding_the_shell(launch_case):
    db_path, worktree = launch_case
    called = False

    def prepare(**_kwargs):
        nonlocal called
        called = True

    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: {
            "supported": True,
            "processes": [{
                "pid": 99,
                "shortname": "dev",
                "orphaned": False,
                "claimed": False,
            }],
        },
        liveness_retries=0,
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))

    assert caught.value.code == "SHELL_BUSY"
    assert not called


def test_preparer_refuses_an_admin_cli_process_at_the_repo_root(launch_case):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=1")
    con.commit()
    con.close()
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **_: None,
        liveness=lambda: {
            "supported": True,
            "processes": [],
            "admin_root_pids": [99],
        },
        liveness_retries=0,
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))
    assert caught.value.code == "SHELL_BUSY"


def test_preparer_rejects_route_or_worktree_drift(launch_case):
    db_path, worktree = launch_case
    other = worktree.parent / "other"
    other.mkdir()
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **_: SimpleNamespace(
            cwd=str(other),
            archive_id=42,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={},
        ),
        liveness=lambda: {"supported": True, "processes": []},
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))
    assert caught.value.code == "HARNESS_WORKTREE_MISMATCH"


def test_preparer_rechecks_liveness_after_canonical_preparation(launch_case):
    db_path, worktree = launch_case
    snapshots = iter((
        {"supported": True, "processes": []},
        {
            "supported": True,
            "processes": [{
                "pid": 100,
                "shortname": "dev",
                "orphaned": False,
                "claimed": False,
            }],
        },
    ))
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **_: SimpleNamespace(
            cwd=str(worktree),
            archive_id=42,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={},
        ),
        liveness=lambda: next(snapshots),
        liveness_retries=0,
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))
    assert caught.value.code == "SHELL_BUSY"


def test_preparer_waits_for_a_prior_browser_process_to_exit(launch_case):
    db_path, worktree = launch_case
    busy = {
        "supported": True,
        "processes": [{
            "pid": 100,
            "shortname": "dev",
            "orphaned": False,
            "claimed": True,
        }],
    }
    clear = {"supported": True, "processes": []}
    snapshots = iter((busy, clear, clear))
    sleeps = []
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **_: SimpleNamespace(
            cwd=str(worktree),
            archive_id=42,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={},
        ),
        liveness=lambda: next(snapshots),
        liveness_retries=1,
        liveness_delay=0.05,
        sleep=sleeps.append,
    )

    context, archive_id = preparer(make_run(worktree))

    assert context.worktree == worktree
    assert archive_id == 42
    assert sleeps == [0.05]


def test_cli_launch_is_reserved_until_the_browser_chat_is_ended(launch_case):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        assert run_mod.browser_conversation_active(con, 1)
        assert not run_mod.browser_conversation_active(con, 999)
        assert [
            (shell["shortname"], shell["browser_active"])
            for shell in run_mod.list_shells(con, 1)
        ] == [("dev", True)]
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE shell_id=1"
        )
        con.commit()
        assert not run_mod.browser_conversation_active(con, 1)
        assert [
            (shell["shortname"], shell["browser_active"])
            for shell in run_mod.list_shells(con, 1)
        ] == [("dev", False)]
    finally:
        con.close()
