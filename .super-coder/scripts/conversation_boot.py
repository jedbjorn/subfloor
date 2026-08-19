#!/usr/bin/env python3
"""Conversation boot snapshots — one immutable boot document per browser chat.

Spec #163 / decision #224: a durable browser conversation binds exactly one
boot-document snapshot before its first native harness session starts. Every
later turn and every closed-chat reopen restores those exact bytes; only a new
conversation composes fresh boot content. This module is the seam that owns
binding, validation, and restoration; ``run.prepare_launch`` keeps every other
per-turn preparation (liveness, worktree, route, environment, archive, skills,
adapter configuration, permissions) on its current cadence.

Ownership rules:
- Composition and filesystem work happen outside conversation write
  transactions; binding is one short compare-and-set transaction. A concurrent
  winner is authoritative — the loser discards its candidate.
- A stored row whose digest or byte count disagrees is an invariant failure:
  fail closed before native dispatch, never repair in place.
- Conversations that predate the 0224 migration bind once on first
  post-upgrade dispatch with origin ``legacy_first_resume``; a missing snapshot
  on a newer conversation is ``BOOT_SNAPSHOT_MISSING``, not a legacy case.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BOOT_FILES = ("CLAUDE.md", "AGENTS.md")
FORMAT_VERSION = 1
MAX_CONTENT_BYTES = 1048576
MIGRATION_FILENAME = "0224_conversation_boot_snapshots.sql"

PHASE_START = "start"
PHASE_RESUME = "resume"


class BootSnapshotError(RuntimeError):
    """Stable pre-dispatch refusal owned by boot snapshot integrity."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class BootDirective:
    """Explicit launch mode: which conversation this launch belongs to and
    whether it starts a new native session or resumes an existing one."""

    conversation_id: str
    phase: str

    def __post_init__(self) -> None:
        if self.phase not in (PHASE_START, PHASE_RESUME):
            raise ValueError(f"unknown boot phase {self.phase!r}")
        if not self.conversation_id:
            raise ValueError("boot directive requires a conversation id")


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_row(row: sqlite3.Row, conversation_id: str) -> dict:
    snapshot = dict(row)
    content = snapshot["content"]
    data = content.encode("utf-8")
    if (
        not data
        or len(data) > MAX_CONTENT_BYTES
        or snapshot["content_bytes"] != len(data)
        or snapshot["content_sha256"] != hashlib.sha256(data).hexdigest()
        or int(snapshot["format_version"]) <= 0
    ):
        raise BootSnapshotError(
            "BOOT_SNAPSHOT_CORRUPT",
            f"stored boot snapshot for conversation {conversation_id} "
            "fails digest/byte validation; refusing native dispatch",
        )
    return snapshot


def load_snapshot(
    con: sqlite3.Connection, conversation_id: str
) -> "dict | None":
    """Read and validate the committed snapshot, or None when unbound."""
    row = con.execute(
        "SELECT conversation_id,content,content_sha256,content_bytes,"
        "format_version,binding_origin,bound_at "
        "FROM conversation_boot_snapshots WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return _validate_row(row, conversation_id)


def _snapshot_cutover(con: sqlite3.Connection) -> "str | None":
    """When the 0224 migration was applied, per the migration ledger.

    A missing ledger cannot disprove pre-migration provenance, so it degrades
    to legacy; a real engine DB always stamps the ledger when applying."""
    try:
        row = con.execute(
            "SELECT applied_at FROM schema_migrations WHERE filename=?",
            (MIGRATION_FILENAME,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _is_legacy(con: sqlite3.Connection, conversation_id: str) -> bool:
    cutover = _snapshot_cutover(con)
    if cutover is None:
        return True
    created_at = con.execute(
        "SELECT created_at FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()[0]
    return str(created_at) < cutover


def bind_snapshot(
    con: sqlite3.Connection,
    conversation_id: str,
    content: str,
    origin: str,
) -> dict:
    """Commit one snapshot compare-and-set; return the authoritative row.

    Exactly one contender wins the insert; every other caller reads back the
    committed winner and validates it instead of overwriting it. The caller's
    candidate is discarded whenever a row already exists."""
    data = content.encode("utf-8")
    if not data or len(data) > MAX_CONTENT_BYTES:
        raise BootSnapshotError(
            "BOOT_SNAPSHOT_UNBINDABLE",
            "composed boot content is empty or exceeds the 1 MiB bound",
        )
    own_transaction = not con.in_transaction
    if own_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT OR IGNORE INTO conversation_boot_snapshots "
            "(conversation_id,content,content_sha256,content_bytes,"
            "format_version,binding_origin) VALUES (?,?,?,?,?,?)",
            (
                conversation_id,
                content,
                hashlib.sha256(data).hexdigest(),
                len(data),
                FORMAT_VERSION,
                origin,
            ),
        )
        row = con.execute(
            "SELECT conversation_id,content,content_sha256,content_bytes,"
            "format_version,binding_origin,bound_at "
            "FROM conversation_boot_snapshots WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if own_transaction:
            con.commit()
    except Exception:
        if own_transaction:
            con.rollback()
        raise
    if row is None:
        raise BootSnapshotError(
            "BOOT_SNAPSHOT_BIND_FAILED",
            f"no boot snapshot committed for conversation {conversation_id}",
        )
    return _validate_row(row, conversation_id)


def resolve_boot(
    con: sqlite3.Connection,
    directive: "BootDirective | None",
    compose: Callable[[], str],
) -> str:
    """Resolve the exact boot bytes for this launch.

    No directive (interactive CLI, legacy callers): compose fresh, exactly the
    historical behavior. ``start``: reuse an already-committed snapshot (retry
    and race losers), else compose once and bind. ``resume``: never compose
    while a snapshot is stored; an unbound pre-migration conversation binds
    once as ``legacy_first_resume``, anything newer fails closed."""
    if directive is None:
        return compose()

    existing = load_snapshot(con, directive.conversation_id)
    if existing is not None:
        return str(existing["content"])

    if directive.phase == PHASE_RESUME and not _is_legacy(
        con, directive.conversation_id
    ):
        raise BootSnapshotError(
            "BOOT_SNAPSHOT_MISSING",
            f"conversation {directive.conversation_id} was created after the "
            "boot snapshot migration but has no stored snapshot",
        )

    origin = (
        "legacy_first_resume"
        if _is_legacy(con, directive.conversation_id)
        else "new_conversation"
    )
    candidate = compose()
    winner = bind_snapshot(con, directive.conversation_id, candidate, origin)
    return str(winner["content"])


def write_boot_files(work_dir: Path, content: str) -> None:
    """Materialize the resolved bytes into CLAUDE.md and AGENTS.md.

    When both files already hold exactly these bytes, skip the writes entirely
    so a resumed conversation's artifacts stay untouched (mtime included).
    When either differs — a different chat or an external lifecycle replaced
    them — restore both atomically."""
    data = content.encode("utf-8")
    targets = [work_dir / name for name in BOOT_FILES]
    if all(t.is_file() and t.read_bytes() == data for t in targets):
        return
    for target in targets:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
