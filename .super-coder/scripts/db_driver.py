#!/usr/bin/env python3
"""Thin database accessor for super-coder — SQLite only.

A fork needs only python3 + sqlite3, which the install already requires.
Every script and route opens the engine DB through this one seam, so the
connection PRAGMAs live in a single place.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import random
import sqlite3
import time


DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_WRITE_WAIT_SECONDS = 5.0
DEFAULT_BEGIN_ATTEMPT_TIMEOUT_MS = 100
DEFAULT_RETRY_BASE_SECONDS = 0.01
DEFAULT_RETRY_MAX_SECONDS = 0.25
SLOW_WRITE_WAIT_MS = 250.0
SLOW_WRITE_HOLD_MS = 100.0

_LOG = logging.getLogger("super_coder.db")


@dataclass(frozen=True)
class WriteTransactionTiming:
    """Contention evidence for one short SQLite write transaction."""

    operation: str
    attempts: int
    wait_ms: float
    hold_ms: float
    acquired: bool
    committed: bool


def is_busy_error(exc: BaseException) -> bool:
    """Return whether SQLite rejected work because another writer owns it."""
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


def _report_timing(
    timing: WriteTransactionTiming,
    observer: Callable[[WriteTransactionTiming], None] | None,
) -> None:
    if observer is not None:
        try:
            observer(timing)
        except Exception:
            _LOG.exception(
                "SQLite write timing observer failed for %s",
                timing.operation,
            )
    log = (
        _LOG.warning
        if (
            timing.wait_ms >= SLOW_WRITE_WAIT_MS
            or timing.hold_ms >= SLOW_WRITE_HOLD_MS
        )
        else _LOG.debug
    )
    log(
        "SQLite write operation=%s attempts=%d wait_ms=%.1f hold_ms=%.1f "
        "acquired=%s committed=%s",
        timing.operation,
        timing.attempts,
        timing.wait_ms,
        timing.hold_ms,
        timing.acquired,
        timing.committed,
    )


def _enable_wal(
    con: sqlite3.Connection,
    *,
    max_wait_seconds: float = DEFAULT_WRITE_WAIT_SECONDS,
    attempt_timeout_ms: int = DEFAULT_BEGIN_ATTEMPT_TIMEOUT_MS,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    random_fraction: Callable[[], float] = random.random,
) -> None:
    """Enable WAL within the shared bounded SQLite acquisition policy."""
    deadline = monotonic() + max_wait_seconds
    attempts = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError(
                "timed out configuring SQLite journal_mode=WAL"
            )
        attempts += 1
        per_attempt_ms = max(
            1,
            min(attempt_timeout_ms, int(remaining * 1000)),
        )
        con.execute(f"PRAGMA busy_timeout={per_attempt_ms}")
        try:
            con.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            ceiling = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** min(attempts - 1, 8)),
                remaining,
            )
            if ceiling > 0:
                sleep(ceiling * (0.75 + 0.5 * random_fraction()))


def connect(path):
    """Open the engine SQLite DB at `path` with the standard PRAGMAs."""
    con = sqlite3.connect(str(path), timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        _enable_wal(con)
        con.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        return con
    except BaseException:
        con.close()
        raise


def connect_readonly(path):
    """Open an existing engine DB without requiring a writable DB directory.

    Read surfaces used from linked shell worktrees deliberately target the
    canonical main checkout's live database.  That checkout can be outside the
    worktree's writable seat, so the normal connector cannot be used: enabling
    WAL is itself a write and may need to create SQLite sidecars.  URI
    ``mode=ro`` is the hard write boundary; ``query_only`` is a second guard
    against accidental mutation by a caller that receives this connection.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(
        uri,
        uri=True,
        timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA query_only=ON")
        con.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        return con
    except BaseException:
        con.close()
        raise


@contextmanager
def write_transaction(
    con: sqlite3.Connection,
    operation: str,
    *,
    max_wait_seconds: float = DEFAULT_WRITE_WAIT_SECONDS,
    attempt_timeout_ms: int = DEFAULT_BEGIN_ATTEMPT_TIMEOUT_MS,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    random_fraction: Callable[[], float] = random.random,
    observer: Callable[[WriteTransactionTiming], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Acquire, time, commit, or roll back one short DB-only write.

    Only ``BEGIN IMMEDIATE`` acquisition is retried: no transaction body is
    replayed, so an ambiguous commit can never duplicate a mutation. Callers
    must perform sleeps, subprocess work, filesystem probes, and network calls
    before entering this context, then re-read authoritative DB state inside.
    """
    if con.in_transaction:
        raise RuntimeError(
            f"{operation}: write_transaction cannot nest inside a transaction"
        )
    if max_wait_seconds <= 0:
        raise ValueError("max_wait_seconds must be positive")
    if attempt_timeout_ms <= 0:
        raise ValueError("attempt_timeout_ms must be positive")
    if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
        raise ValueError("invalid retry delay bounds")

    original_timeout_ms = int(con.execute("PRAGMA busy_timeout").fetchone()[0])
    started = monotonic()
    deadline = started + max_wait_seconds
    attempts = 0
    acquired_at: float | None = None
    committed = False
    last_busy: sqlite3.OperationalError | None = None
    try:
        while acquired_at is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                if last_busy is not None:
                    raise last_busy
                raise sqlite3.OperationalError(
                    f"{operation}: timed out acquiring SQLite write transaction"
                )
            attempts += 1
            per_attempt_ms = max(
                1,
                min(attempt_timeout_ms, int(remaining * 1000)),
            )
            con.execute(f"PRAGMA busy_timeout={per_attempt_ms}")
            try:
                con.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not is_busy_error(exc):
                    raise
                last_busy = exc
                con.rollback()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise
                ceiling = min(
                    retry_max_seconds,
                    retry_base_seconds * (2 ** min(attempts - 1, 8)),
                    remaining,
                )
                if ceiling > 0:
                    # 0.75x–1.25x jitter avoids synchronized shell retries.
                    sleep(ceiling * (0.75 + 0.5 * random_fraction()))
            else:
                acquired_at = monotonic()
        con.execute(f"PRAGMA busy_timeout={original_timeout_ms}")
        try:
            yield con
            con.commit()
            committed = True
        except BaseException:
            con.rollback()
            raise
    finally:
        if acquired_at is None:
            con.execute(f"PRAGMA busy_timeout={original_timeout_ms}")
        finished = monotonic()
        timing = WriteTransactionTiming(
            operation=operation,
            attempts=attempts,
            wait_ms=(
                (acquired_at - started) * 1000
                if acquired_at is not None
                else (finished - started) * 1000
            ),
            hold_ms=(
                (finished - acquired_at) * 1000
                if acquired_at is not None
                else 0.0
            ),
            acquired=acquired_at is not None,
            committed=committed,
        )
        _report_timing(timing, observer)


IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
