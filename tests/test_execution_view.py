"""Restricted role/repository-mode harness execution view."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import execution_view
import instance_state


def installation(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    repo = tmp_path / "fork"
    engine = repo / ".super-coder"
    engine.mkdir(parents=True)
    (engine / "schema.sql").write_text("engine schema secret\n")
    (engine / "migrations").mkdir()
    (engine / "migrations" / "0001.sql").write_text("migration secret\n")
    (engine / "shell_db.db").write_text("legacy db secret\n")
    config = engine / "instance.json"
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": os.environ.get("PATH", ""),
        "SC_ROOT": "/spoofed/root",
        "SC_ENGINE_DIR": "/spoofed/engine",
        "SC_SHELL_FLAVOR": "admin",
    }
    Path(env["HOME"]).mkdir(mode=0o700)
    private = instance_state.resolve(
        instance_config=config,
        environ=env,
        id_factory=lambda: "0123456789abcdef0123456789abcdef",
    )
    private.database.write_text("private db secret\n")
    return repo, engine, env, private.root


def run_in(view: execution_view.ExecutionView, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        view.command(["/bin/sh", "-c", script]),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_downstream_view_masks_state_schema_and_process_root_alias(tmp_path: Path) -> None:
    repo, engine, env, private_root = installation(tmp_path)
    view = execution_view.build(
        engine=engine,
        repo_root=repo,
        flavor="developer",
        source_mode=False,
        environ=env,
    )
    view.preflight()
    probe = run_in(
        view,
        " && ".join(
            (
                f"! cat {engine / 'shell_db.db'} >/dev/null 2>&1",
                f"! cat {private_root / 'shell_db.db'} >/dev/null 2>&1",
                (
                    f"! /bin/sh -c 'echo mutate >> \"$1\"' child "
                    f"{private_root / 'shell_db.db'}"
                ),
                f"! rm {private_root / 'shell_db.db'} >/dev/null 2>&1",
                f"! cat {engine / 'schema.sql'} >/dev/null 2>&1",
                f"! cat {engine / 'migrations' / '0001.sql'} >/dev/null 2>&1",
                (
                    f"! cat /proc/{os.getpid()}/root"
                    f"{private_root / 'shell_db.db'} >/dev/null 2>&1"
                ),
                (
                    f"/bin/sh -c '! cat \"$1\" >/dev/null 2>&1' child "
                    f"{private_root / 'shell_db.db'}"
                ),
            )
        ),
    )
    assert probe.returncode == 0, probe.stderr

    restricted_env = view.environment(env)
    assert restricted_env["SC_EXECUTION_VIEW"] == "restricted-downstream"
    assert "SC_ROOT" not in restricted_env
    assert "SC_ENGINE_DIR" not in restricted_env
    assert restricted_env["SC_SHELL_FLAVOR"] == "admin"  # diagnostic only


def test_source_view_keeps_tracked_engine_source_but_masks_live_state(
    tmp_path: Path,
) -> None:
    repo, engine, env, private_root = installation(tmp_path)
    view = execution_view.build(
        engine=engine,
        repo_root=repo,
        flavor="dev",
        source_mode=True,
        environ=env,
    )
    probe = run_in(
        view,
        f"grep -q secret {engine / 'schema.sql'} && "
        f"grep -q secret {engine / 'migrations' / '0001.sql'} && "
        f"! cat {private_root / 'shell_db.db'} >/dev/null 2>&1",
    )
    assert probe.returncode == 0, probe.stderr


def test_sandbox_landlock_view_masks_direct_and_process_root_aliases(
    tmp_path: Path,
) -> None:
    repo, engine, env, private_root = installation(tmp_path)
    env["SC_SANDBOX"] = "1"
    view = execution_view.build(
        engine=engine,
        repo_root=repo,
        flavor="reviewer",
        source_mode=False,
        environ=env,
    )
    view.preflight()
    secret = private_root / "shell_db.db"
    probe = run_in(
        view,
        f"! cat {secret} >/dev/null 2>&1 && "
        f"! cat /proc/{os.getpid()}/root{secret} >/dev/null 2>&1 && "
        f"! cat {engine / 'schema.sql'} >/dev/null 2>&1",
    )
    assert probe.returncode == 0, probe.stderr


def test_detached_descendant_survives_wrapper_and_keeps_restriction(
    tmp_path: Path,
) -> None:
    repo, engine, env, private_root = installation(tmp_path)
    product = repo / "product"
    product.mkdir()
    result = product / "job-result"
    view = execution_view.build(
        engine=engine,
        repo_root=repo,
        flavor="dev",
        source_mode=False,
        environ=env,
    )
    secret = private_root / "shell_db.db"
    launched = run_in(
        view,
        f"setsid /bin/sh -c 'sleep 0.05; "
        f"if cat {secret} >/dev/null 2>&1; then echo exposed; "
        f"else echo restricted; fi > {result}' >/dev/null 2>&1 &",
    )
    assert launched.returncode == 0, launched.stderr
    deadline = time.monotonic() + 2
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert result.read_text().strip() == "restricted"


def test_admin_is_unwrapped_and_keeps_maintenance_environment(tmp_path: Path) -> None:
    view = execution_view.build(
        engine=tmp_path / "missing-engine",
        repo_root=tmp_path / "repo",
        flavor="admin",
        source_mode=False,
        environ={},
    )
    assert view.command(["tool", "arg"]) == ["tool", "arg"]
    assert view.environment({"SC_ROOT": "/repo"})["SC_ROOT"] == "/repo"


def test_missing_private_identity_fails_closed_without_path_disclosure(
    tmp_path: Path,
) -> None:
    engine = tmp_path / "repo" / ".super-coder"
    engine.mkdir(parents=True)
    with pytest.raises(execution_view.ExecutionViewError) as caught:
        execution_view.build(
            engine=engine,
            repo_root=engine.parent,
            flavor="dev",
            source_mode=False,
            environ={"HOME": str(tmp_path)},
        )
    assert str(caught.value) == execution_view.RESTRICTED_VIEW_ERROR
    assert str(tmp_path) not in str(caught.value)


def test_masked_file_alias_fails_closed(tmp_path: Path) -> None:
    repo, engine, env, private_root = installation(tmp_path)
    alias = repo / "product-db-alias"
    os.link(private_root / "shell_db.db", alias)
    with pytest.raises(execution_view.ExecutionViewError) as caught:
        execution_view.build(
            engine=engine,
            repo_root=repo,
            flavor="dev",
            source_mode=False,
            environ=env,
        )
    assert str(caught.value) == execution_view.RESTRICTED_VIEW_ERROR
    assert str(private_root) not in str(caught.value)
