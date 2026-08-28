"""Reusable dirty downstream fixture for skill-catalogue convergence tests.

The fixture is deliberately pinned to the engine state immediately before
feature 33.  Later migration tests can therefore build a legacy database,
apply the feature's trailing migrations, and separately replay the stale
snapshot that would otherwise resurrect retired upstream authority.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ENGINE = SOURCE_ROOT / ".super-coder"

BASELINE_SHA = "446dc39641fc4a146951824fb86a19fb13700ddd"
BASELINE_LAST_MIGRATION = "0153_harden_sprint_handoff_skills.sql"

TOMBSTONE_SKILLS = (
    "dev_sprint",
    "plan_sprint",
    "rev_sprint",
    "sprint",
    "sprint_cond",
    "sprint_onboarding",
    "sprint_orchestration",
    "sprint_orchestration_close",
    "sprint_orchestration_recover",
    "sprint_review",
    "engine_surgery",
    "agents",
    "api-design",
    "app_deploy_setup",
    "authoring_syntax",
    "blueprint",
    "configure_winbox",
    "database-migrations",
    "local_skill_management",
    "migration_management",
    "pm2",
    "query_authoring_pg",
    "tailscale",
    "test_authoring",
    "test_authoring_pg",
    "test_authoring_sqlite",
    "windows_devkit",
    "windows_vm_gui",
)
LOCAL_SKILL_NAME = "dos_arch_testing"
LOCAL_SKILL_DESCRIPTION = "Fork-owned dos-arch testing procedure"
LOCAL_SKILL_CONTENT = b"# dos_arch_testing\n\nFork-owned testing procedure.\n"
LOCAL_SKILL_ASSET = (
    b"---\n"
    b"name: dos_arch_testing\n"
    b"description: Fork-owned dos-arch testing procedure\n"
    b"common: false\n"
    b"---\n\n"
    + LOCAL_SKILL_CONTENT
)
CONTROL_FILE_BODY = b"operator-owned control; never delete\n"
NATIVE_SKILL_DIRS = (
    Path(".claude/skills"),
    Path(".agents/skills"),
    Path(".opencode/skills"),
)

FIXTURE_USER_ID = 9100
BESPOKE_SHELL_ID = 9101
FLAVORED_SHELL_ID = 9102


@dataclass(frozen=True)
class DirtySkillFork:
    root: Path
    engine: Path
    database: Path
    snapshot: Path
    dormant_worktree: Path
    checkouts: tuple[Path, Path]
    native_skill_roots: tuple[Path, ...]
    legacy_skill_roots: tuple[Path, Path]
    catalogue_root: Path
    local_asset: Path
    control_files: tuple[Path, Path]
    bespoke_shell_id: int = BESPOKE_SHELL_ID
    flavored_shell_id: int = FLAVORED_SHELL_ID
    expected_local_content: bytes = LOCAL_SKILL_CONTENT
    expected_local_asset: bytes = LOCAL_SKILL_ASSET
    expected_control_file: bytes = CONTROL_FILE_BODY


def _run_git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Skill Convergence Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Skill Convergence Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _create_downstream_checkout(root: Path, source_root: Path) -> Path:
    root.mkdir(parents=True)
    _run_git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(
        "/.super-coder/\n"
        "/.sc-state/\n"
        "/.sc-worktrees/\n"
        "/.claude/skills/\n"
        "/.agents/skills/\n"
        "/.opencode/skills/\n"
        "/skills_sc/\n"
    )
    shutil.copy2(source_root / "sc", root / "sc")
    (root / "host-owned.txt").write_text("dos-arch host content\n")
    _run_git(root, "add", ".gitignore", "host-owned.txt", "sc")
    _run_git(root, "commit", "-qm", "fixture downstream baseline")

    worktree = root / ".sc-worktrees" / "dev9"
    _run_git(root, "worktree", "add", "-q", "-b", "shell/dev9", str(worktree))
    return worktree


def _apply_baseline(engine: Path, database: Path) -> None:
    con = sqlite3.connect(database)
    try:
        con.executescript((engine / "schema.sql").read_text())
        migrations = sorted((engine / "migrations").glob("*.sql"))
        baseline = [p for p in migrations if p.name <= BASELINE_LAST_MIGRATION]
        if not baseline or baseline[-1].name != BASELINE_LAST_MIGRATION:
            raise AssertionError(
                f"fixture baseline migration {BASELINE_LAST_MIGRATION} is missing"
            )
        for migration in baseline:
            con.executescript(migration.read_text())
            con.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration.name,),
            )
            con.commit()

        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "INSERT INTO users (user_id, username, email, initials, is_active) "
            "VALUES (?, 'fixture-operator', 'fixture@example.test', 'FO', 1)",
            (FIXTURE_USER_ID,),
        )
        con.executemany(
            "INSERT INTO shells "
            "(shell_id, display_name, shortname, flavor, mandate, system_prompt, "
            "user_id, has_identity, bootstrapped) "
            "VALUES (?, ?, ?, ?, 'fixture', 'fixture', ?, 1, 1)",
            (
                (
                    BESPOKE_SHELL_ID,
                    "Fixture Bespoke",
                    "BSP9",
                    None,
                    FIXTURE_USER_ID,
                ),
                (
                    FLAVORED_SHELL_ID,
                    "Fixture Dev",
                    "DEV9",
                    "dev",
                    FIXTURE_USER_ID,
                ),
            ),
        )
        _dirty_skill_rows(con)
        con.commit()
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise AssertionError(f"dirty fixture has foreign-key violations: {violations}")
    finally:
        con.close()


def _dirty_skill_rows(con: sqlite3.Connection) -> None:
    for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
        body = (
            LOCAL_SKILL_CONTENT.decode()
            if name == LOCAL_SKILL_NAME
            else f"# {name}\n\nRetired upstream fixture authority.\n"
        )
        category = "fork" if name == LOCAL_SKILL_NAME else "legacy"
        description = (
            LOCAL_SKILL_DESCRIPTION
            if name == LOCAL_SKILL_NAME
            else f"{name} fixture"
        )
        con.execute(
            "INSERT INTO skills "
            "(name, description, category, content, command, common, is_deleted) "
            "VALUES (?, ?, ?, ?, NULL, 0, 0) "
            "ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description, category=excluded.category, "
            "content=excluded.content, command=NULL, common=0, is_deleted=0",
            (name, description, category, body),
        )
        skill_id = con.execute(
            "SELECT skill_id FROM skills WHERE name=?", (name,)
        ).fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) VALUES (?, ?)",
            (BESPOKE_SHELL_ID, skill_id),
        )
        con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) VALUES ('dev', ?)",
            (skill_id,),
        )


def _snapshot_sql() -> str:
    lines = [
        "-- stale downstream snapshot pinned before skill-catalogue convergence",
        "PRAGMA foreign_keys=OFF;",
        "BEGIN;",
        (
            "INSERT OR IGNORE INTO users "
            "(user_id, username, email, initials, is_active) VALUES "
            f"({FIXTURE_USER_ID}, 'fixture-operator', "
            "'fixture@example.test', 'FO', 1);"
        ),
        (
            "INSERT OR IGNORE INTO shells "
            "(shell_id, display_name, shortname, flavor, mandate, system_prompt, "
            "user_id, has_identity, bootstrapped) VALUES "
            f"({BESPOKE_SHELL_ID}, 'Fixture Bespoke', 'BSP9', NULL, "
            f"'fixture', 'fixture', {FIXTURE_USER_ID}, 1, 1), "
            f"({FLAVORED_SHELL_ID}, 'Fixture Dev', 'DEV9', 'dev', "
            f"'fixture', 'fixture', {FIXTURE_USER_ID}, 1, 1);"
        ),
    ]
    for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
        body = (
            LOCAL_SKILL_CONTENT.decode()
            if name == LOCAL_SKILL_NAME
            else f"# {name}\n\nRetired upstream fixture authority.\n"
        )
        category = "fork" if name == LOCAL_SKILL_NAME else "legacy"
        description = (
            LOCAL_SKILL_DESCRIPTION
            if name == LOCAL_SKILL_NAME
            else f"{name} fixture"
        )
        quoted_body = body.replace("'", "''")
        lines.extend(
            [
                (
                    "INSERT INTO skills "
                    "(name, description, category, content, command, common, "
                    "is_deleted) "
                    f"VALUES ('{name}', '{description}', '{category}', "
                    f"'{quoted_body}', NULL, 0, 0) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "description=excluded.description, category=excluded.category, "
                    "content=excluded.content, command=NULL, common=0, is_deleted=0;"
                ),
                (
                    "INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
                    f"SELECT {BESPOKE_SHELL_ID}, skill_id FROM skills "
                    f"WHERE name='{name}';"
                ),
                (
                    "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) "
                    f"SELECT 'dev', skill_id FROM skills WHERE name='{name}';"
                ),
            ]
        )
    lines.extend(["COMMIT;", "PRAGMA foreign_keys=ON;", ""])
    return "\n".join(lines)


def _managed_skill_body(name: str) -> str:
    if name == LOCAL_SKILL_NAME:
        description = LOCAL_SKILL_DESCRIPTION
        content = LOCAL_SKILL_CONTENT.decode().rstrip()
        return (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"{content}\n"
        )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: stale fixture projection for {name}\n"
        "---\n\n"
        f"# {name}\n\nStale managed projection.\n"
    )


def _banner_owned_catalogue_body(name: str) -> str:
    return (
        "---\n"
        "rendered_by: super-coder\n"
        "source: db\n"
        "edit: changes here are overwritten — author via the shell or localhost GUI\n"
        "---\n\n"
        f"# {name}\n\nStale catalogue projection.\n"
    )


def _populate_projections(checkouts: tuple[Path, Path], root: Path) -> tuple:
    native_roots = []
    legacy_roots = []
    controls = []
    for checkout in checkouts:
        for relative in NATIVE_SKILL_DIRS:
            skills_root = checkout / relative
            native_roots.append(skills_root)
            for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
                path = skills_root / name / "SKILL.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(_managed_skill_body(name))

        legacy = checkout / "skills_sc"
        legacy.mkdir(parents=True, exist_ok=True)
        legacy_roots.append(legacy)
        for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
            (legacy / f"{name}.md").write_text(_banner_owned_catalogue_body(name))
        control = legacy / "operator-notes.md"
        control.write_bytes(CONTROL_FILE_BODY)
        controls.append(control)

    catalogue = root / ".sc-state" / "local" / "renders" / "skills_sc"
    catalogue.mkdir(parents=True, exist_ok=True)
    for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
        (catalogue / f"{name}.md").write_text(_banner_owned_catalogue_body(name))
    return tuple(native_roots), tuple(legacy_roots), catalogue, tuple(controls)


def build_dirty_skill_fork(
    root: Path,
    *,
    source_engine: Path = SOURCE_ENGINE,
) -> DirtySkillFork:
    """Build one realistic, disposable downstream fork at ``root``.

    ``root`` must be absent or empty.  The caller owns cleanup; tests should use
    ``TemporaryDirectory`` or ``addCleanup`` so a failing assertion cannot leak
    the registered dormant worktree.
    """
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"dirty skill fixture root is not empty: {root}")

    source_engine = Path(source_engine)
    dormant = _create_downstream_checkout(root, source_engine.parent)
    engine = root / ".super-coder"
    engine.mkdir(parents=True)
    database = engine / "shell_db.db"
    _apply_baseline(source_engine, database)

    state = root / ".sc-state"
    snapshot = state / "local" / "content.sql"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(_snapshot_sql())
    (state / "engine.ref").write_text(BASELINE_SHA + "\n")

    local_asset = engine / "assets" / "skills" / LOCAL_SKILL_NAME / "SKILL.md"
    local_asset.parent.mkdir(parents=True)
    local_asset.write_bytes(LOCAL_SKILL_ASSET)

    checkouts = (root, dormant)
    native, legacy, catalogue, controls = _populate_projections(checkouts, root)
    return DirtySkillFork(
        root=root,
        engine=engine,
        database=database,
        snapshot=snapshot,
        dormant_worktree=dormant,
        checkouts=checkouts,
        native_skill_roots=native,
        legacy_skill_roots=legacy,
        catalogue_root=catalogue,
        local_asset=local_asset,
        control_files=controls,
    )
