"""Browser turns use the canonical shell launch path before native dispatch."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import instance_state  # noqa: E402
import route_bindings  # noqa: E402
import run as run_mod  # noqa: E402
import shell_liveness
from conversation_boot import BootDirective  # noqa: E402
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
    model: str | None = "gpt-test",
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


def harness_default_binding(harness: str = "kimi") -> dict:
    binding = {
        "contract_version": 2,
        "control_state": "harness-default",
        "harness": harness,
        "requested_model": None,
        "provider_model": None,
        "requested_effort": None,
        "effective_effort": None,
        "native_variant_id": None,
        "transport": "native-default",
        "catalogue_generation": None,
        "evidence_digest": None,
        "selector_binding": None,
        "adapter_metadata": {},
    }
    route_bindings.validate_v2_binding(binding)
    return binding


def controlled_opencode_binding() -> dict:
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "opencode",
        "requested_model": "openai/gpt-test",
        "provider_model": "openai/gpt-test",
        "requested_effort": "high",
        "effective_effort": "high",
        "native_variant_id": "high",
        "transport": "opencode-route-agent",
        "catalogue_generation": "a" * 32,
        "evidence_digest": "b" * 64,
        "selector_binding": {"kind": "exact-model"},
        "adapter_metadata": {"variant_options": {"reasoningEffort": "high"}},
    }
    route_bindings.validate_v2_binding(binding)
    return binding


def test_bound_headless_route_preserves_harness_default_nulls() -> None:
    binding = harness_default_binding()
    route = run_mod.resolve_bound_headless_route(
        harness="kimi",
        model=None,
        effort=None,
        binding=binding,
        binding_digest=route_bindings.digest_json(binding),
    )

    assert route.harness == "kimi"
    assert route.model is None
    assert route.effort is None


def test_bound_headless_route_uses_exact_controlled_native_variant() -> None:
    binding = controlled_opencode_binding()
    route = run_mod.resolve_bound_headless_route(
        harness="opencode",
        model="openai/gpt-test",
        effort="high",
        binding=binding,
        binding_digest=route_bindings.digest_json(binding),
    )

    assert route.model == "openai/gpt-test"
    assert route.effort == binding["native_variant_id"] == "high"


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
            boot_content="immutable boot bytes",
            execution_view=SimpleNamespace(prefix=("view-helper", "--")),
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
    assert context.env["SC_CONVERSATION_SURFACE"] == "browser"
    assert context.conversation_id == "cv_" + "a" * 32
    assert context.boot_content == "immutable boot bytes"
    assert context.execution_prefix == ("view-helper", "--")
    assert context.permission_mode == "unrestricted"
    assert called == [{
        "shell_id": 1,
        "harness": "codex",
        "model": "gpt-test",
        "effort": "high",
        "headless_prompt": "Do the work",
        "conversation_owned": True,
        "current_leased_run_id": 7,
        "boot": BootDirective(
            conversation_id="cv_" + "a" * 32,
            phase="start",
        ),
    }]


@pytest.mark.parametrize("surface", ["browser", "sprint"])
def test_recovery_rebuilds_and_preflights_canonical_restricted_view(
    launch_case, monkeypatch, surface
):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    con.execute("UPDATE shells SET api_key='canonical-token' WHERE shell_id=1")
    con.commit()
    con.close()
    build_calls = []
    preflights = []

    def project_environment(env):
        projected = dict(env)
        projected.pop("SC_ROOT", None)
        projected.pop("SC_ENGINE_DIR", None)
        projected["SC_EXECUTION_VIEW"] = "restricted-source"
        return projected

    view = SimpleNamespace(
        prefix=("view-helper", "--"),
        preflight=lambda: preflights.append(True),
        environment=project_environment,
    )
    monkeypatch.setattr(run_mod, "shell_work_dir", lambda *_: worktree)
    monkeypatch.setattr(run_mod.ports_mod, "resolve", lambda: {"port": 8837})
    monkeypatch.setattr(run_mod.install, "is_source_repo", lambda: True)
    monkeypatch.setattr(
        run_mod.execution_view,
        "build",
        lambda **kwargs: build_calls.append(kwargs) or view,
    )
    monkeypatch.setenv("SC_ROOT", "/spoofed/root")
    monkeypatch.setenv("SC_ENGINE_DIR", "/spoofed/engine")
    preparer = ConversationLaunchPreparer(db_path)
    monkeypatch.setattr(preparer, "_conversation_surface", lambda _: surface)

    context = preparer.recovery(make_run(worktree))

    assert build_calls == [{
        "engine": run_mod.ENGINE,
        "repo_root": run_mod.REPO_ROOT,
        "flavor": "dev",
        "source_mode": True,
    }]
    assert preflights == [True]
    assert context.execution_prefix == ("view-helper", "--")
    assert context.env["SC_EXECUTION_VIEW"] == "restricted-source"
    assert context.env["SC_CONVERSATION_SURFACE"] == surface
    assert "SC_ROOT" not in context.env
    assert "SC_ENGINE_DIR" not in context.env


def test_recovery_admin_remains_unwrapped(launch_case, monkeypatch):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE shells SET flavor='admin',api_key='canonical-token' "
        "WHERE shell_id=1"
    )
    con.commit()
    con.close()
    view = SimpleNamespace(
        prefix=(),
        preflight=lambda: None,
        environment=lambda env: dict(env),
    )
    monkeypatch.setattr(run_mod, "shell_work_dir", lambda *_: worktree)
    monkeypatch.setattr(run_mod.ports_mod, "resolve", lambda: {"port": 8837})
    monkeypatch.setattr(run_mod.execution_view, "build", lambda **_: view)

    context = ConversationLaunchPreparer(db_path).recovery(make_run(worktree))

    assert context.execution_prefix == ()
    assert context.env["SC_ROOT"] == str(run_mod.REPO_ROOT)
    assert context.env["SC_ENGINE_DIR"] == str(run_mod.ENGINE)


def test_recovery_failed_preflight_refuses_before_context(
    launch_case, monkeypatch
):
    db_path, worktree = launch_case
    con = sqlite3.connect(db_path)
    con.execute("UPDATE shells SET api_key='canonical-token' WHERE shell_id=1")
    con.commit()
    con.close()

    def refuse():
        raise run_mod.execution_view.ExecutionViewError(
            run_mod.execution_view.RESTRICTED_VIEW_ERROR
        )

    view = SimpleNamespace(prefix=("view-helper", "--"), preflight=refuse)
    monkeypatch.setattr(run_mod, "shell_work_dir", lambda *_: worktree)
    monkeypatch.setattr(run_mod.ports_mod, "resolve", lambda: {"port": 8837})
    monkeypatch.setattr(run_mod.execution_view, "build", lambda **_: view)

    with pytest.raises(ConversationLaunchError) as caught:
        ConversationLaunchPreparer(db_path).recovery(make_run(worktree))

    assert caught.value.code == "CONVERSATION_LAUNCH_REFUSED"
    assert caught.value.detail == run_mod.execution_view.RESTRICTED_VIEW_ERROR


def test_recovery_view_masks_private_state_and_parent_root_alias(
    launch_case, monkeypatch
):
    db_path, worktree = launch_case
    root = db_path.parent
    repo = root / "fork"
    engine = repo / ".super-coder"
    engine.mkdir(parents=True)
    (engine / "schema.sql").write_text("schema secret\n")
    (engine / "migrations").mkdir()
    (engine / "migrations" / "0001.sql").write_text("migration secret\n")
    home = root / "home"
    home.mkdir(mode=0o700)
    state_home = root / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("SC_DB_BACKUP_DIR", "backups")
    private = instance_state.resolve(
        instance_config=engine / "instance.json",
        environ=os.environ,
        id_factory=lambda: "0123456789abcdef0123456789abcdef",
    )
    private.database.write_text("private secret\n")
    backup = repo / "backups" / "shell_db.preboundary.db"
    backup.parent.mkdir()
    backup.write_text("backup secret\n")
    (worktree / "backups").symlink_to(backup.parent, target_is_directory=True)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE shells SET api_key='canonical-token' WHERE shell_id=1")
    con.commit()
    con.close()
    monkeypatch.setattr(run_mod, "ENGINE", engine)
    monkeypatch.setattr(run_mod, "REPO_ROOT", repo)
    monkeypatch.setattr(run_mod, "shell_work_dir", lambda *_: worktree)
    monkeypatch.setattr(run_mod.ports_mod, "resolve", lambda: {"port": 8837})
    monkeypatch.setattr(run_mod.install, "is_source_repo", lambda: False)

    context = ConversationLaunchPreparer(db_path).recovery(make_run(worktree))
    secret = private.database
    probe = subprocess.run(
        context.execution_argv([
            "/bin/sh",
            "-c",
            f"! cat {secret} >/dev/null 2>&1 && "
            f"! cat /proc/{os.getpid()}/root{secret} >/dev/null 2>&1 && "
            f"! cat {engine / 'schema.sql'} >/dev/null 2>&1 && "
            f"! cat {backup} >/dev/null 2>&1 && "
            "! cat backups/shell_db.preboundary.db >/dev/null 2>&1 && "
            "! /bin/sh -c 'cat backups/shell_db.preboundary.db' "
            ">/dev/null 2>&1",
        ]),
        cwd=context.worktree,
        env=context.env,
        check=False,
    )

    assert probe.returncode == 0
    assert context.env["SC_EXECUTION_VIEW"] == "restricted-downstream"


def test_preparer_marks_a_resume_turn_with_the_resume_phase(launch_case):
    db_path, worktree = launch_case
    called = []
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=lambda **kwargs: called.append(kwargs) or SimpleNamespace(
            cwd=str(worktree),
            archive_id=42,
            harness="codex",
            model="gpt-test",
            effort="high",
            env={},
        ),
        liveness=lambda: {"supported": True, "processes": []},
    )
    preparer(make_run(worktree, session_before="native-session-1"))
    assert called[0]["boot"] == BootDirective(
        conversation_id="cv_" + "a" * 32,
        phase="resume",
    )


def test_preexisting_null_model_chat_refuses_turn_and_preserves_row(launch_case):
    db_path, worktree = launch_case
    conversation_id = "cv_" + "a" * 32
    con = sqlite3.connect(db_path)
    con.execute(
        "DELETE FROM conversations WHERE conversation_id=?", (conversation_id,)
    )
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
        "effort,worktree,state,creation_idempotency_key,creation_request_hash) "
        "VALUES (?,1,1,'kimi','kimi',NULL,'high',?,'idle',"
        "'legacy-null-create','legacy-null-hash')",
        (conversation_id, str(worktree)),
    )
    con.commit()
    con.close()

    adapter = {
        "harness": "kimi",
        "headless": {
            "launch": ["kimi", "--prompt", "{prompt}"],
            "model_flag": "--model",
            "effort": {"flag": "--effort"},
        },
    }

    def prepare(**kwargs):
        try:
            run_mod.resolve_headless_route(
                harness=kwargs["harness"],
                adapter=adapter,
                flavor_model=None,
                model=kwargs["model"],
                effort=kwargs["effort"],
            )
        except ValueError as exc:
            raise run_mod.LaunchError(str(exc)) from exc
        raise AssertionError("a legacy NULL route must not become dispatchable")

    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: {"supported": True, "processes": []},
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree, harness="kimi", model=None))

    assert caught.value.code == "CONVERSATION_LAUNCH_REFUSED"
    assert caught.value.detail == (
        "harness 'kimi' cannot resolve a model: no model was supplied and no "
        "flavor default exists for it; supply an explicit model"
    )
    con = sqlite3.connect(db_path)
    try:
        assert con.execute(
            "SELECT harness,provider,model,state FROM conversations "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone() == ("kimi", "kimi", None, "idle")
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_runs WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0] == 0
    finally:
        con.close()


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


def browser_snapshot(*sessions: dict) -> dict:
    # Unit B's shape, built by hand: one browser-owned pid per entry.
    return {
        "supported": True,
        "processes": [
            {
                "pid": session["pid"],
                "shortname": "dev",
                "orphaned": False,
                "claimed": True,
                "browser_conversation": session["conversation_id"],
                "lingering": session.get("lingering", True),
            }
            for session in sessions
        ],
        "browser_sessions": {"dev": list(sessions)},
    }


def test_preparer_dispatches_over_its_own_lingering_browser_process(
    launch_case, monkeypatch
):
    db_path, worktree = launch_case
    monkeypatch.setattr(
        shell_liveness, "session_state", lambda shortname, snap: "browser"
    )
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
        liveness=lambda: browser_snapshot(
            {"pid": 4242, "conversation_id": "cv_" + "a" * 32, "lingering": True}
        ),
        liveness_retries=0,
    )
    _context, archive_id = preparer(make_run(worktree))

    assert archive_id == 42


def test_preparer_names_a_foreign_browser_chat_holding_the_shell(
    launch_case, monkeypatch
):
    db_path, worktree = launch_case
    called = False

    def prepare(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        shell_liveness, "session_state", lambda shortname, snap: "browser"
    )
    holder = "cv_" + "b" * 32
    preparer = ConversationLaunchPreparer(
        db_path,
        prepare_launch=prepare,
        liveness=lambda: browser_snapshot(
            {"pid": 4242, "conversation_id": holder, "lingering": True}
        ),
        liveness_retries=0,
    )
    with pytest.raises(ConversationLaunchError) as caught:
        preparer(make_run(worktree))

    assert caught.value.code == "SHELL_BUSY"
    assert holder in caught.value.detail
    assert "pid 4242" in caught.value.detail
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
