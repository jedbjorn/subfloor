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
            "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
            "effort,worktree,state,creation_idempotency_key,"
            "creation_request_hash) "
            "VALUES (?,1,1,'codex','openai','gpt-test','high',"
            "?,'idle','normal-create','normal-hash')",
            ("cv_" + "a" * 32, str(root / ".sc-worktrees" / "dev")),
        )
        con.commit()
        con.close()
        worktree = root / ".sc-worktrees" / "dev"
        worktree.mkdir(parents=True)
        yield db_path, worktree


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
