"""Spec #163 conversation_boot seam: bind-once, exact reuse, restore, legacy."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import conversation_boot  # noqa: E402
from conversation_boot import (  # noqa: E402
    BootDirective,
    BootSnapshotError,
)


MIGRATION = "0224_conversation_boot_snapshots.sql"


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    apply_schema(connection)
    connection.execute(
        "INSERT INTO users (user_id,username) VALUES (1,'operator')"
    )
    connection.execute(
        "INSERT INTO shells "
        "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
        "VALUES (1,'Dev','dev1','dev','prompt',1)"
    )
    yield connection
    connection.close()


def add_conversation(
    con: sqlite3.Connection,
    conversation_id: str,
    *,
    created_at: str | None = None,
) -> str:
    columns = (
        "conversation_id,shell_id,owner_user_id,harness,worktree,"
        "creation_idempotency_key,creation_request_hash"
    )
    values: list = [
        conversation_id, 1, 1, "codex", "/tmp/worktree-1",
        f"key-{conversation_id}", f"hash-{conversation_id}",
    ]
    if created_at is not None:
        columns += ",created_at"
        values.append(created_at)
    con.execute(
        f"INSERT INTO conversations ({columns}) "
        f"VALUES ({','.join('?' for _ in values)})",
        values,
    )
    con.commit()
    return conversation_id


def stamp_migration(con: sqlite3.Connection, applied_at: str) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    con.execute(
        "INSERT OR REPLACE INTO schema_migrations (filename,applied_at) "
        "VALUES (?,?)",
        (MIGRATION, applied_at),
    )
    con.commit()


def compose_factory(content: str, calls: list) -> object:
    def compose() -> str:
        calls.append(content)
        return content
    return compose


def test_directive_requires_a_known_phase_and_conversation() -> None:
    with pytest.raises(ValueError):
        BootDirective(conversation_id="cv_x", phase="reopen")
    with pytest.raises(ValueError):
        BootDirective(conversation_id="", phase="start")


def test_start_binds_one_new_conversation_snapshot(con) -> None:
    stamp_migration(con, "2020-01-01 00:00:00")
    conversation = add_conversation(con, "cv_start")
    calls: list = []

    content = conversation_boot.resolve_boot(
        con,
        BootDirective(conversation_id=conversation, phase="start"),
        compose_factory("boot A", calls),
    )

    assert content == "boot A"
    assert calls == ["boot A"]
    row = con.execute(
        "SELECT content_sha256,content_bytes,format_version,binding_origin "
        "FROM conversation_boot_snapshots WHERE conversation_id=?",
        (conversation,),
    ).fetchone()
    assert tuple(row) == (
        hashlib.sha256(b"boot A").hexdigest(), 6, 1, "new_conversation",
    )


def test_start_retry_reuses_the_committed_snapshot_without_composing(
    con,
) -> None:
    stamp_migration(con, "2020-01-01 00:00:00")
    conversation = add_conversation(con, "cv_retry")
    directive = BootDirective(conversation_id=conversation, phase="start")
    first_calls: list = []
    conversation_boot.resolve_boot(con, directive, compose_factory("boot A", first_calls))

    retry_calls: list = []
    content = conversation_boot.resolve_boot(
        con, directive, compose_factory("boot B", retry_calls)
    )

    assert content == "boot A"
    assert retry_calls == []


def test_a_bind_race_keeps_the_first_committed_snapshot(con) -> None:
    stamp_migration(con, "2020-01-01 00:00:00")
    conversation = add_conversation(con, "cv_race")

    winner = conversation_boot.bind_snapshot(con, conversation, "boot A", "new_conversation")
    loser = conversation_boot.bind_snapshot(con, conversation, "boot B", "new_conversation")

    assert winner["content"] == loser["content"] == "boot A"
    assert winner["binding_origin"] == loser["binding_origin"] == "new_conversation"


def test_resume_reuses_stored_bytes_with_zero_compositions(con) -> None:
    stamp_migration(con, "2020-01-01 00:00:00")
    conversation = add_conversation(con, "cv_resume")
    conversation_boot.bind_snapshot(con, conversation, "boot A", "new_conversation")
    calls: list = []

    content = conversation_boot.resolve_boot(
        con,
        BootDirective(conversation_id=conversation, phase="resume"),
        compose_factory("boot B", calls),
    )

    assert content == "boot A"
    assert calls == []


def test_resume_adopts_an_unbound_legacy_conversation_once(con) -> None:
    stamp_migration(con, "2030-01-01 00:00:00")  # cutover after created_at
    conversation = add_conversation(con, "cv_legacy", created_at="2020-01-01 00:00:00")
    calls: list = []

    content = conversation_boot.resolve_boot(
        con,
        BootDirective(conversation_id=conversation, phase="resume"),
        compose_factory("legacy boot", calls),
    )

    assert content == "legacy boot"
    assert calls == ["legacy boot"]
    origin = con.execute(
        "SELECT binding_origin FROM conversation_boot_snapshots "
        "WHERE conversation_id=?",
        (conversation,),
    ).fetchone()[0]
    assert origin == "legacy_first_resume"

    # Later turns reuse the adopted snapshot without composing again.
    more_calls: list = []
    again = conversation_boot.resolve_boot(
        con,
        BootDirective(conversation_id=conversation, phase="resume"),
        compose_factory("other boot", more_calls),
    )
    assert again == "legacy boot"
    assert more_calls == []


def test_resume_refuses_an_unbound_post_migration_conversation(con) -> None:
    stamp_migration(con, "2020-01-01 00:00:00")
    conversation = add_conversation(con, "cv_missing")

    with pytest.raises(BootSnapshotError) as caught:
        conversation_boot.resolve_boot(
            con,
            BootDirective(conversation_id=conversation, phase="resume"),
            compose_factory("boot", []),
        )
    assert caught.value.code == "BOOT_SNAPSHOT_MISSING"


def test_a_stored_row_that_fails_validation_refuses_dispatch(con) -> None:
    conversation = add_conversation(con, "cv_corrupt")
    with pytest.raises(BootSnapshotError) as caught:
        conversation_boot._validate_row(
            {
                "conversation_id": conversation,
                "content": "tampered",
                "content_sha256": hashlib.sha256(b"original").hexdigest(),
                "content_bytes": 8,
                "format_version": 1,
                "binding_origin": "new_conversation",
                "bound_at": "2026-08-19 00:00:00",
            },
            conversation,
        )
    assert caught.value.code == "BOOT_SNAPSHOT_CORRUPT"


def test_write_boot_files_skips_matching_and_restores_drift(tmp_path) -> None:
    content = "immutable boot"
    conversation_boot.write_boot_files(tmp_path, content)
    first = {
        name: (tmp_path / name).stat().st_mtime_ns
        for name in conversation_boot.BOOT_FILES
    }

    conversation_boot.write_boot_files(tmp_path, content)
    assert {
        name: (tmp_path / name).stat().st_mtime_ns
        for name in conversation_boot.BOOT_FILES
    } == first

    # A different chat replaced one file: both restore the exact bytes.
    (tmp_path / "CLAUDE.md").write_text("other chat")
    conversation_boot.write_boot_files(tmp_path, content)
    for name in conversation_boot.BOOT_FILES:
        assert (tmp_path / name).read_bytes() == content.encode("utf-8")

    # A missing file is restored as well.
    (tmp_path / "AGENTS.md").unlink()
    conversation_boot.write_boot_files(tmp_path, content)
    for name in conversation_boot.BOOT_FILES:
        assert (tmp_path / name).read_bytes() == content.encode("utf-8")
