"""Seat-aware boot and skills (Feature #97, spec #276), AC1-AC13.

Every boot here is composed from a DB built from schema + every migration, so
the seat column, the cli_orchestration tag and the role packs are the shipped
ones. Liveness is a stubbed snapshot over a fake /proc.
"""
from __future__ import annotations

import contextlib
import io
import re
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
for sub in ("scripts", "render"):
    sys.path.insert(0, str(ENGINE / sub))
sys.path.insert(0, str(ROOT / "tests"))
import compose
import run_seat
import seed_skills
import shell_liveness
import skill as skill_mod
import skill_projection

SEAT_MIGRATION = "0291_skill_seat.sql"
GUI_TOPICS = ("**Background work.**", "**Wakes.**", "**Watching.**", "**Orchestration.**")
ADMIN_FALLBACK = (
    "A shell with no SEAT section (Admin) goes by how it was launched: a\n"
    "session the FnB types into is a terminal seat; a chat the engine opened is a\n"
    "headless one."
)


def build_db(path: Path | str = ":memory:", *, stop_before: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        if stop_before and migration.name >= stop_before:
            break
        con.executescript(migration.read_text())
    return con


def add_shell(con, shortname: str, flavor: str | None) -> int:
    if con.execute("SELECT 1 FROM users WHERE user_id=1").fetchone() is None:
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
    shell_id = con.execute(
        "INSERT INTO shells (display_name, shortname, flavor, mandate, system_prompt, "
        "user_id, is_shared, has_identity, bootstrapped) "
        "VALUES (?, ?, ?, 'test', 'sp', 1, 0, 1, 1)",
        (shortname, shortname.upper(), flavor),
    ).lastrowid
    seed_skills.reconcile_standard_flavor_packs(con)
    return shell_id


def boot(con, shell_id: int, seat: str | None, conversion: dict | None = None) -> str:
    shell = con.execute("SELECT * FROM shells WHERE shell_id=?", (shell_id,)).fetchone()
    user = con.execute("SELECT * FROM users WHERE user_id=1").fetchone()
    with mock.patch.object(compose, "open_map_ro", return_value=None):
        return compose.compose_boot(
            con, shell, user, "0001", 1, launch_mode="host",
            seat=seat, conversion=conversion,
        )


def section(text: str, heading: str) -> str:
    """One `## ` section's body, up to the next `## ` heading."""
    body = text.split(heading, 1)[1]
    return body.split("\n## ", 1)[0]


def skills_list(text: str) -> set[str]:
    return set(re.findall(r"^- \*\*([a-z_]+)\*\*", section(text, "## SKILLS\n"), re.MULTILINE))


@pytest.fixture
def con():
    connection = build_db()
    yield connection
    connection.close()


@pytest.fixture
def planner(con):
    # The planner pack grants cli_orchestration (tui) and delegation (untagged).
    return add_shell(con, "pln1", "planner")


# ── AC1-AC5: one named SEAT section before WAKES ────────────────────────────


def test_ac1_gui_boot_names_its_seat_and_holds_only_gui_guidance(con, planner):
    text = boot(con, planner, "gui")
    assert "## SEAT — GUI\n" in text
    seat = section(text, "## SEAT — GUI\n")
    positions = [seat.index(topic) for topic in GUI_TOPICS]
    assert positions == sorted(positions)
    for absent in ("SEAT — CLI", "You hold a terminal", "terminal seat"):
        assert absent not in text


def test_ac2_cli_boot_names_its_seat_and_holds_only_cli_guidance(con, planner):
    text = boot(con, planner, "tui")
    assert "## SEAT — CLI\n" in text
    seat = section(text, "## SEAT — CLI\n")
    positions = [seat.index(topic) for topic in GUI_TOPICS]
    assert positions == sorted(positions)
    for absent in ("SEAT — GUI", "die when your turn ends", "in-session timers"):
        assert absent not in text


@pytest.mark.parametrize("seat,name", [("gui", "GUI"), ("tui", "CLI")])
def test_ac3_seat_directly_precedes_a_seat_neutral_wakes(con, planner, seat, name):
    text = boot(con, planner, seat)
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    assert headings[headings.index("## WAKES") - 1] == f"## SEAT — {name}"
    assert headings.count(f"## SEAT — {name}") == 1
    wakes = section(text, "## WAKES\n")
    assert "headless seat" not in wakes and "terminal seat" not in wakes
    assert "When and how wakes reach you: SEAT." in wakes
    # Exactly one separator between the two sections.
    between = text.split(f"## SEAT — {name}\n", 1)[1].split("## WAKES", 1)[0]
    assert between.rstrip().endswith("---")
    assert between.count("\n---\n") == 1


def test_ac4_admin_boot_has_no_seat_and_keeps_the_launch_fallback(con):
    admin = add_shell(con, "admin", "admin")
    text = boot(con, admin, None)
    assert "## SEAT" not in text
    assert "{{seat}}" not in text and "{{wake_seat}}" not in text
    assert "---\n\n---" not in text
    assert "---\n\n## WAKES" in text
    assert ADMIN_FALLBACK in section(text, "## WAKES\n")
    # Even an explicit seat never reaches an Admin boot.
    assert boot(con, admin, "gui") == text


def test_ac5_conversion_disarmed_note_renders_inside_seat_gui(con, planner):
    text = boot(con, planner, "gui", {"tier": "disarmed", "version": "2.1", "error": "fixture"})
    seat = section(text, "## SEAT — GUI\n")
    assert "Claude background conversion is disarmed for 2.1: fixture." in seat
    assert "conversion is disarmed" not in boot(con, planner, "tui")


# ── AC6-AC9: seat-filtered skill surfaces ───────────────────────────────────


def test_ac6_boot_skills_list_follows_the_launch_seat(con, planner):
    gui, tui = skills_list(boot(con, planner, "gui")), skills_list(boot(con, planner, "tui"))
    assert "cli_orchestration" not in gui
    assert "cli_orchestration" in tui
    assert "delegation" in gui and "delegation" in tui
    # Filtering never touches grants.
    granted = {row[0] for row in con.execute(
        "SELECT s.name FROM skills s JOIN resolved_shell_skills r USING(skill_id) "
        "WHERE r.shell_id=?", (planner,))}
    assert {"cli_orchestration", "delegation"} <= granted


def project(con, shell_id: int, checkout: Path, seats) -> dict:
    return skill_projection.reconcile_shell(
        con, shell_id, checkout,
        ensure_dirs=skill_projection.managed_skill_dirs(), seats=seats,
    )


@pytest.mark.parametrize("seats,present", [
    ({"gui"}, False), ({"tui"}, True), ({"gui", "tui"}, True), (None, True),
])
def test_ac7_projected_roots_follow_the_seat_set(con, planner, tmp_path, seats, present):
    # Start from a full projection so S={gui} must remove, not merely skip.
    project(con, planner, tmp_path, None)
    project(con, planner, tmp_path, seats)
    for root in skill_projection.managed_skill_dirs():
        assert (tmp_path / root / "cli_orchestration").is_dir() is present
        assert (tmp_path / root / "delegation" / "SKILL.md").is_file()


@pytest.fixture
def liveness(tmp_path, monkeypatch):
    """Fake /proc: `set(seat, ...)` makes live processes of PLN1 with those seats."""
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(shell_liveness, "PROC", proc)
    monkeypatch.setattr(shell_liveness, "_start_ticks", lambda pid: 7)
    snapshot = {"supported": True, "processes": []}
    monkeypatch.setattr(shell_liveness, "compute", lambda: snapshot)

    def live(*seats, shortname="pln1"):
        for seat in seats:
            pid = 100 + len(snapshot["processes"])
            (proc / str(pid)).mkdir()
            marker = b"" if seat is None else b"SC_SEAT=" + seat.encode() + b"\0"
            (proc / str(pid) / "environ").write_bytes(b"HOME=/x\0" + marker)
            snapshot["processes"].append(
                {"pid": pid, "start_ticks": 7, "shortname": shortname})
        return snapshot

    return live


def test_live_seats_know_nothing_live_from_unknown(liveness):
    assert run_seat.live_seats("PLN1", "planner") == frozenset()
    liveness("tui")
    assert run_seat.live_seats("PLN1", "planner") == {"tui"}
    assert run_seat.live_seats("PLN1", "admin") is None
    assert run_seat.launch_seats("PLN1", "planner", "gui") == {"gui", "tui"}
    assert run_seat.launch_seats("PLN1", "planner", None) is None
    liveness(None)  # a pre-marker process: unknown, so no filter
    assert run_seat.live_seats("PLN1", "planner") is None
    assert run_seat.launch_seats("PLN1", "planner", "gui") is None


def test_live_seats_unsupported_liveness_is_unknown(monkeypatch):
    monkeypatch.setattr(shell_liveness, "compute", lambda: {"supported": False})
    assert run_seat.live_seats("PLN1", "planner") is None
    assert run_seat.launch_seats("PLN1", "planner", "gui") is None


def test_ac8_launch_union_protects_a_live_cli_session(con, planner, tmp_path, liveness):
    liveness("tui")
    # Nothing else live would narrow a GUI launch to its own seat.
    project(con, planner, tmp_path, None)
    seats = run_seat.launch_seats("PLN1", "planner", "gui")
    assert seats == {"gui", "tui"}
    project(con, planner, tmp_path, seats)
    for root in skill_projection.managed_skill_dirs():
        assert (tmp_path / root / "cli_orchestration" / "SKILL.md").is_file()
    assert "cli_orchestration" not in skills_list(boot(con, planner, "gui"))


def test_ac8_both_launch_paths_project_the_launch_seat_set():
    source = (ENGINE / "scripts" / "run.py").read_text()
    assert source.count(
        'seats=run_seat.launch_seats(chosen["shortname"], chosen["flavor"], seat)') == 2


@pytest.mark.parametrize("holders,present", [(("gui",), False), ((), True)])
def test_ac9_non_launch_reconciles_use_live_seats(con, planner, tmp_path, liveness, holders, present):
    checkout = tmp_path / ".sc-worktrees" / "pln1"
    project(con, planner, checkout, None)
    liveness(*holders)
    for reconcile in (
        lambda: skill_projection.reconcile_assignment_targets(con, [planner], repo_root=tmp_path),
        lambda: skill_projection.reconcile_flavors(con, ["planner"], repo_root=tmp_path),
        lambda: skill_projection.reconcile_existing_checkouts(con, repo_root=tmp_path),
    ):
        project(con, planner, checkout, None)
        reconcile()
        for root in skill_projection.managed_skill_dirs():
            assert (checkout / root / "cli_orchestration").is_dir() is present
            assert (checkout / root / "delegation").is_dir()


# ── AC10-AC13: the seat tag ─────────────────────────────────────────────────


def test_ac10_engine_asset_seat_is_ingested_and_validated(tmp_path):
    assert seed_skills.parse_skill(
        ENGINE / "assets" / "skills" / "cli_orchestration" / "SKILL.md")["seat"] == "tui"
    assert seed_skills.parse_skill(
        ENGINE / "assets" / "skills" / "delegation" / "SKILL.md")["seat"] is None
    bad = tmp_path / "SKILL.md"
    bad.write_text("---\nname: odd\ndescription: x\nseat: bogus\n---\nbody\n")
    with pytest.raises(SystemExit, match="gui or tui"):
        seed_skills.parse_skill(bad)


def test_ac10_heal_owns_the_engine_seat(con):
    con.execute("UPDATE skills SET seat=NULL WHERE name='cli_orchestration'")
    assert "cli_orchestration" in seed_skills.stale_engine_skills(con)
    seed_skills.sync_engine_skills(con)
    assert con.execute(
        "SELECT seat FROM skills WHERE name='cli_orchestration'").fetchone()[0] == "tui"


class TestSkillPut:
    @pytest.fixture(autouse=True)
    def lane(self, tmp_path, monkeypatch):
        self.db = tmp_path / "engine.db"
        build_db(self.db).close()
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
            con.execute(
                "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
                "system_prompt, user_id, api_key) "
                "VALUES (9, 'Planner', 'PLN1', 'planner', 'sp', 1, 'planner-token')")
        self.tmp = tmp_path
        monkeypatch.setattr(skill_mod, "DB_PATH", self.db)
        monkeypatch.setattr(skill_mod.mem, "SC_API_TOKEN", "planner-token")
        monkeypatch.setattr(skill_mod, "_shell_api_enabled", lambda: False)
        monkeypatch.setattr(skill_mod, "_persist_snapshot", lambda con: None)
        monkeypatch.setattr(skill_mod, "_persist_render", lambda con: None)
        monkeypatch.setattr(
            skill_mod.skill_projection, "reconcile_existing_checkouts", lambda con: None)

    def put(self, seat: str | None) -> None:
        draft = self.tmp / "draft.md"
        seat_line = "" if seat is None else f"seat: {seat}\n"
        draft.write_text(
            "---\nname: local_tool\ndescription: a fork tool\n"
            f"common: false\n{seat_line}---\n\nDo the thing.\n")
        with contextlib.redirect_stdout(io.StringIO()):
            assert skill_mod.main(["put", "--file", str(draft)]) == 0

    def stored(self):
        with sqlite3.connect(self.db) as con:
            return con.execute("SELECT seat FROM skills WHERE name='local_tool'").fetchone()[0]

    def test_ac10_put_sets_refuses_and_clears_the_seat(self):
        self.put("gui")
        assert self.stored() == "gui"
        self.put("tui")
        assert self.stored() == "tui"
        with pytest.raises(SystemExit, match=r"`seat` must be gui or tui.*'bogus'"):
            self.put("bogus")
        assert self.stored() == "tui"
        self.put(None)
        assert self.stored() is None

    def test_ac10_api_lane_shares_the_draft_contract(self):
        with pytest.raises(skill_mod.DraftValidationError, match="gui or tui"):
            skill_mod.parse_local_skill_spec(
                "---\nname: x\ndescription: y\nseat: both\n---\nbody\n")
        spec = skill_mod.parse_local_skill_spec(
            "---\nname: x\ndescription: y\nseat: tui\n---\nbody\n")
        assert spec["seat"] == "tui"

    def test_ac13_skill_list_shows_each_seat(self):
        self.put("gui")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert skill_mod.main(["list"]) == 0
        rows = {line.split()[0]: line.split()[3] for line in out.getvalue().splitlines()}
        assert rows["cli_orchestration"] == "tui"
        assert rows["local_tool"] == "gui"
        assert rows["delegation"] == "both"


def test_ac11_fresh_rebuild_carries_the_seat_column_and_tag(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(skills)")}
    assert "seat" in columns
    assert con.execute(
        "SELECT seat FROM skills WHERE name='cli_orchestration'").fetchone()[0] == "tui"
    assert con.execute("SELECT COUNT(*) FROM skills WHERE seat IS NOT NULL").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE skills SET seat='both' WHERE name='delegation'")
    # schema.sql only names the owner; the column lives in one place.
    assert SEAT_MIGRATION in (ENGINE / "schema.sql").read_text()


def test_ac11_upgrade_keeps_ids_and_grants():
    old = build_db(stop_before=SEAT_MIGRATION)
    try:
        add_shell(old, "pln1", "planner")
        before = old.execute(
            "SELECT skill_id FROM skills WHERE name='cli_orchestration'").fetchone()[0]
        grants = [tuple(r) for r in old.execute("SELECT * FROM flavor_skills ORDER BY 1, 2")]
        old.executescript((ENGINE / "migrations" / SEAT_MIGRATION).read_text())
        assert tuple(old.execute(
            "SELECT skill_id, seat FROM skills WHERE name='cli_orchestration'"
        ).fetchone()) == (before, "tui")
        assert [tuple(r) for r in old.execute(
            "SELECT * FROM flavor_skills ORDER BY 1, 2")] == grants
        for name in ("cli_orchestration", "delegation", "spec_execution"):
            spec = seed_skills.parse_skill(ENGINE / "assets" / "skills" / name / "SKILL.md")
            assert tuple(old.execute(
                "SELECT description, content, seat FROM skills WHERE name=?", (name,)
            ).fetchone()) == (spec["description"], spec["content"], spec["seat"])
    finally:
        old.close()


# ── AC12: seat-split prose is gone ──────────────────────────────────────────


def test_ac12_no_surface_detects_seat_by_prose_or_explains_both_seats():
    surfaces = [
        *sorted((ENGINE / "templates").rglob("*.md")),
        *sorted((ENGINE / "assets" / "skills").rglob("*.md")),
        ENGINE / "scripts" / "run_seat.py",
    ]
    for path in surfaces:
        assert "You hold a terminal" not in path.read_text(), path
    for path in (
        ENGINE / "templates" / "boot.md",
        ENGINE / "templates" / "shells" / "dev.md",
        ENGINE / "assets" / "skills" / "spec_execution" / "SKILL.md",
        ENGINE / "assets" / "skills" / "delegation" / "SKILL.md",
    ):
        text = " ".join(path.read_text().lower().split())
        gui = any(word in text for word in ("headless seat", "gui seat"))
        cli = "terminal seat" in text
        assert not (gui and cli), path
