"""GUI/TUI policy at launch, including nested Admin exemption."""
from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mem
import run
import run_seat
import shell_liveness
from test_mem import build_engine_db


@pytest.mark.parametrize("flavor,headless,expected", [
    ("dev", False, "tui"), ("dev", True, "gui"),
    ("planner", True, "gui"), ("admin", False, None),
    ("admin", True, None),
])
def test_explicit_launch_seat_overrides_inherited_seat(flavor, headless, expected):
    seat = run_seat.select(flavor, headless=headless)
    env = {"SC_SEAT": "tui" if headless else "gui", "SC_HARNESS": "claude"}
    run_seat.inject(env, seat)
    assert env.get("SC_SEAT") == expected
    assert env["SC_HARNESS"] == "claude"
    block = run_seat.boot_block(flavor, seat)
    assert ("## SEAT" in block) == (expected is not None)
    if expected == "tui":
        assert "native background tools freely" in block
        assert "next boot" in block
    if expected == "gui":
        assert "Long work goes through `sc job`" in block
        assert "Do not schedule in-session timers" in block


def test_admin_render_ignores_even_explicit_gui_seat():
    assert run_seat.boot_block("admin", "gui") == ""


@pytest.mark.parametrize("flavor,seat,shown", [
    ("dev", "gui", True), ("dev", "tui", True),
    ("admin", "gui", False), ("dev", "invalid", False),
])
def test_which_reports_invoking_seat_only_for_nonadmin(flavor, seat, shown):
    out = io.StringIO()
    with mock.patch.object(mem, "_api", return_value={"flavor": flavor}), \
            mock.patch.dict(mem.os.environ, {"SC_SEAT": seat}), redirect_stdout(out):
        assert mem.cmd_which(None) == 0
    assert ("seat       :" in out.getvalue()) == shown


def test_live_seat_requires_instance_shell_and_process_incarnation(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_liveness, "PROC", tmp_path)
    process = tmp_path / "42"
    process.mkdir()
    (process / "environ").write_bytes(b"SECRET=not-projected\0SC_SEAT=gui\0")
    snapshot = {"processes": [{"pid": 42, "start_ticks": 123, "shortname": "dev4"}]}
    with mock.patch.object(shell_liveness, "_start_ticks", return_value=123):
        assert run_seat.live_seat("DEV4", "dev", snapshot=snapshot) == "gui"
        assert run_seat.live_seat("DEV3", "dev", snapshot=snapshot) is None
        assert run_seat.live_seat("DEV4", "admin", snapshot=snapshot) is None
    with mock.patch.object(shell_liveness, "_start_ticks", side_effect=[123, 124]):
        assert run_seat.live_seat("DEV4", "dev", snapshot=snapshot) is None


@pytest.mark.parametrize("mode,flavor,expected", [
    ("enter", "dev", "tui"), ("headless", "dev", "gui"),
    ("browser", "dev", "gui"), ("admin", "admin", None),
])
def test_render_only_launch_plan_matches_boot(mode, flavor, expected, tmp_path, monkeypatch):
    database = tmp_path / "engine.db"
    build_engine_db(database)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE shells SET flavor=? WHERE shell_id=1", (flavor,))

    def connect():
        con = sqlite3.connect(database)
        con.row_factory = sqlite3.Row
        return con

    monkeypatch.setenv("RENDER_ONLY", "1")
    monkeypatch.setenv("SC_SEAT", "wrong-inherited-seat")
    monkeypatch.setattr(run, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run, "open_db", connect)
    monkeypatch.setattr(run, "authenticate", lambda *a, **k: {"user_id": 1, "username": "T"})
    monkeypatch.setattr(run, "shell_work_dir", lambda *a: tmp_path)
    monkeypatch.setattr(run, "open_session", lambda *a, **k: ("0001", 1))
    monkeypatch.setattr(run, "main_checkout_note", lambda *a: "fixture")
    monkeypatch.setattr(run, "declared_work_repo_note", lambda *a: None)
    monkeypatch.setattr(run, "collect_dev_tools", lambda *a, **k: {})
    monkeypatch.setattr(run, "_cli_version", lambda *a: "fixture")
    monkeypatch.setattr(run, "resolve_headless_route", lambda **k: SimpleNamespace(model=None, effort=None))
    monkeypatch.setattr(run.ports_mod, "resolve", lambda **k: {})
    monkeypatch.setattr(run.install, "is_source_repo", lambda: False)
    monkeypatch.setattr(run.run_seat, "conversion_status", lambda *a, **k: {"tier": "disarmed", "error": "fixture"})
    plan = run.prepare_launch(shell_id=1, harness="claude",
                              headless_prompt="test" if mode in {"headless", "browser"} else None,
                              conversation_owned=mode == "browser")
    assert plan.env.get("SC_SEAT") == expected
    assert ("## SEAT" in plan.boot_content) == (expected is not None)
    assert (tmp_path / "AGENTS.md").read_text() == plan.boot_content
    if expected is not None:
        assert plan.boot_content.index("## EXECUTION CONTEXT") < plan.boot_content.index("## SEAT") < plan.boot_content.index("## ACTIVE SESSION")
    if expected == "gui":
        assert "conversion is disarmed" in plan.boot_content
