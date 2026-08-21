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
sys.path.insert(0, str(ENGINE / "api"))

import route_bindings  # noqa: E402
import run as run_mod  # noqa: E402
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


def unsupported_deepseek_binding() -> dict:
    return {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "deepseek",
        "requested_model": "deepseek-v4-pro",
        "provider_model": "deepseek-v4-pro",
        "requested_effort": "medium",
        "effective_effort": "medium",
        "native_variant_id": None,
        "transport": "deepseek-provider-options-v1",
        "catalogue_generation": "a" * 32,
        "evidence_digest": "b" * 64,
        "selector_binding": {
            "kind": "authenticated-provider-model",
            "selector": "deepseek-v4-pro",
        },
        "adapter_metadata": {
            "provider_route": "deepseek-official",
            "provider_adapter_id": "deepseek-native-v1",
            "provider_adapter_digest": "1" * 64,
            "provider_registry_sha256": "2" * 64,
            "credential_kind": "deepseek-api-key",
            "endpoint_identity": "https://api.deepseek.com",
            "discovery_evidence_digest": "3" * 64,
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "c" * 64,
            "runtime_version": "0.1.0rc7",
            "source_commit": "b" * 40,
            "patch_sha256": "4" * 64,
            "composition_sha256": "5" * 64,
            "provider_options": {
                "omit": [],
                "set": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "medium",
                },
            },
        },
    }


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


def test_bound_headless_route_rejects_unsupported_deepseek_effort() -> None:
    binding = unsupported_deepseek_binding()

    with pytest.raises(route_bindings.RouteResolutionError) as refused:
        run_mod.resolve_bound_headless_route(
            harness="deepseek",
            model="deepseek-v4-pro",
            effort="medium",
            binding=binding,
            binding_digest=route_bindings.digest_json(binding),
        )

    assert refused.value.code == "thinking_evidence_missing"
    assert refused.value.details == {
        "reason": "DeepSeek effort is outside the carrier contract"
    }


def test_native_deepseek_conversation_does_not_enable_cli_headless() -> None:
    adapter = run_mod.load_adapter("deepseek")

    assert run_mod.headless_command(adapter, "prompt") is None
    assert run_mod.headless_command(
        adapter, "prompt", conversation_owned=True
    ) == []
    assert adapter["surfaces"]["one_shot"] is False


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
    assert context.conversation_id == "cv_" + "a" * 32
    assert context.boot_content == "immutable boot bytes"
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
