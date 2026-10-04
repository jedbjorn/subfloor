"""Launch policy after decision #427 retired the Landlock execution view.

Every flavor launches its harness argv unchanged, in source and downstream
repositories alike. The one surviving environment guard withholds the main
checkout's engine and root paths (``SC_ENGINE_DIR``/``SC_ROOT``) from every
seat except Admin, so an ordinary shell is never handed a path to ``cd`` into
the stale default-branch tree.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import execution_view  # noqa: E402

FLAVORS = ("admin", "planner", "dev", "reviewer", "devops", "cartographer", None)
ARGV = ["claude", "--model", "opus", "--resume", "session id with spaces"]
SOURCE = {
    "PATH": "/usr/bin",
    "SC_ENGINE_DIR": "/main/.super-coder",
    "SC_ROOT": "/main",
    "SC_API_TOKEN": "token",
}


@pytest.mark.parametrize("flavor", FLAVORS)
def test_every_flavor_launches_the_harness_argv_unchanged(flavor):
    view = execution_view.build(flavor=flavor)
    assert view.command(ARGV) == ARGV
    assert view.command([]) == []


@pytest.mark.parametrize("flavor", FLAVORS)
def test_only_admin_receives_the_maintenance_paths(flavor):
    env = execution_view.build(flavor=flavor).environment(SOURCE)
    assert "SC_EXECUTION_VIEW" not in env
    if flavor == "admin":
        assert env == SOURCE
    else:
        assert "SC_ENGINE_DIR" not in env
        assert "SC_ROOT" not in env
        assert env == {"PATH": "/usr/bin", "SC_API_TOKEN": "token"}


def test_labels_are_admin_shell_and_render_only():
    assert execution_view.build(flavor="admin").mode == "admin"
    assert execution_view.build(flavor="dev").mode == "shell"
    assert execution_view.build(flavor=None).mode == "shell"
    render = execution_view.build(flavor="admin", render_only=True)
    assert render.mode == "render-only"
    assert render.command(ARGV) == ARGV
    assert not render.maintenance_environment
    assert execution_view.build(flavor="admin").maintenance_environment
    assert not execution_view.build(flavor="planner").maintenance_environment


def test_repository_mode_is_not_an_input_to_the_launch_policy():
    # Source and downstream repositories launch every flavor identically:
    # the builder cannot see the repository mode at all.
    assert set(inspect.signature(execution_view.build).parameters) == {
        "flavor", "render_only",
    }


def test_the_kernel_view_and_its_refusal_surface_are_gone():
    assert not (ENGINE / "scripts" / "execution_view_exec.py").exists()
    for name in ("ExecutionViewError", "RESTRICTED_VIEW_ERROR", "build_masks"):
        assert not hasattr(execution_view, name)
    view = execution_view.build(flavor="dev")
    for name in ("preflight", "prefix", "masked_paths", "restricted"):
        assert not hasattr(view, name)
    retired = (
        "ExecutionViewError", "execution_view_exec", "SC_EXECUTION_VIEW",
        "execution_prefix", "execution_argv", "RESTRICTED_SHELL_VIEW_MISMATCH",
    )
    for tree in (ENGINE / "scripts", ENGINE / "api", ENGINE / "render"):
        for source in tree.rglob("*.py"):
            text = source.read_text()
            for symbol in retired:
                assert symbol not in text, f"{symbol} in {source}"
