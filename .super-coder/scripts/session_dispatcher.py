#!/usr/bin/env python3
"""Provider-neutral wake dispatcher for managed planner conversations.

The dispatcher owns durable wake-job claims and acknowledgement.  Provider
modules own transport.  A provider module lives at
``adapters/<harness>/session_control.py`` and exposes ``create_adapter()`` with
three blocking operations::

    status(binding) -> "starting" | "idle" | "active" | "dormant" | "error"
    deliver(binding, prompt) -> None
    resume(binding, prompt) -> None

``deliver`` and ``resume`` return only after the injected turn exits.  Resume
implementations must use :mod:`session_supervisor` to fence their child process;
the common dispatcher never treats a native session ID as a lock.

Message bodies never cross the transport boundary.  Every turn receives the
same fixed prompt and the planner reads its token-scoped inbox through the API.
``shell_messages.read_at`` is the only delivery acknowledgement.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
RUN_DIR = ENGINE / "run" / "session-dispatcher"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import session_control  # noqa: E402
import session_supervisor  # noqa: E402


WAKE_PROMPT = (
    "Check your unread sprint inbox, act on every message, and mark each "
    "handled message read."
)
ADAPTER_STATES = frozenset({"starting", "idle", "active", "dormant", "error"})
RETRY_DELAYS = (15, 60, 300)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
MAX_ERROR_CHARS = 500
LOG_LINES = 200


class Adapter(Protocol):
    def status(self, binding: dict) -> str: ...
    def deliver(self, binding: dict, prompt: str) -> None: ...
    def resume(self, binding: dict, prompt: str) -> None: ...


@dataclass(frozen=True)
class WakeBatch:
    binding_id: int
    shell_id: int
    wake_ids: tuple[int, ...]
    message_ids: tuple[int, ...]
    message_watermark: int


@dataclass(frozen=True)
class BatchResult:
    acknowledged: int
    queued: int
    failed: int


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|authorization)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(https?://[^/@:\s]+:)[^/@\s]+@"),
)


def sanitize_error(error: object) -> str:
    """Return a bounded single-line error safe for DB rows and runtime logs."""
    text = " ".join(str(error).replace("\x00", "").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:MAX_ERROR_CHARS] or "unknown session-control failure"


class AttemptLog:
    """Small mode-0600 JSONL audit, one file per binding and bounded in size."""

    def __init__(self, root: Path = RUN_DIR):
        self.root = root

    def write(self, binding_id: int, event: str, **fields: object) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.root / f"binding-{binding_id}.jsonl"
            line = json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "event": event,
                 **{key: sanitize_error(value) if key == "error" else value
                    for key, value in fields.items()}},
                sort_keys=True,
            )
            previous = path.read_text().splitlines() if path.exists() else []
            content = "\n".join((previous + [line])[-LOG_LINES:]) + "\n"
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
            path.chmod(0o600)
        except OSError:
            # The DB ledger remains authoritative; a runtime-log failure must not
            # change dispatch correctness.
            return


def binding_row(con: sqlite3.Connection, binding_id: int) -> dict | None:
    row = con.execute(
        "SELECT b.*, s.shortname, s.flavor, s.api_key, "
        "a.model AS archive_model, a.provider AS archive_provider "
        "FROM shell_session_bindings b JOIN shells s ON s.shell_id=b.shell_id "
        "JOIN shell_memory_archives a ON a.archive_id=b.archive_id "
        "WHERE b.binding_id=?",
        (binding_id,),
    ).fetchone()
    return dict(row) if row else None


def claim_batch(con: sqlite3.Connection, binding_id: int) -> WakeBatch | None:
    """Atomically claim every ready wake for one binding.

    ``BEGIN IMMEDIATE`` plus the binding state transition makes the binding the
    lock.  Two dispatcher processes may race, but only one can move the ready
    rows to ``running``.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        binding = con.execute(
            "SELECT binding_id, shell_id, state, managed "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        if not binding or not binding["managed"] or binding["state"] not in (
            "foreground", "idle", "dormant"
        ):
            con.rollback()
            return None
        jobs = con.execute(
            "SELECT wake_id, trigger_message_id FROM session_wake_jobs "
            "WHERE binding_id=? AND state='queued' "
            "AND available_at <= datetime('now') ORDER BY wake_id",
            (binding_id,),
        ).fetchall()
        if not jobs:
            con.rollback()
            return None

        watermark = con.execute(
            "SELECT COALESCE(MAX(message_id),0) FROM shell_messages "
            "WHERE to_shell_id=?", (binding["shell_id"],)
        ).fetchone()[0]
        session_control.transition_binding(
            con, binding_id, expected=binding["state"], target="dispatching"
        )
        wake_ids = tuple(row["wake_id"] for row in jobs)
        marks = ",".join("?" for _ in wake_ids)
        cur = con.execute(
            f"UPDATE session_wake_jobs SET state='running', "
            f"attempt_count=attempt_count+1, started_at=datetime('now'), "
            f"finished_at=NULL, last_error=NULL WHERE wake_id IN ({marks}) "
            "AND state='queued'",
            wake_ids,
        )
        if cur.rowcount != len(wake_ids):
            raise RuntimeError("wake batch changed during transactional claim")
        con.commit()
        return WakeBatch(
            binding_id=binding_id,
            shell_id=binding["shell_id"],
            wake_ids=wake_ids,
            message_ids=tuple(row["trigger_message_id"] for row in jobs),
            message_watermark=watermark,
        )
    except Exception:
        con.rollback()
        raise


def _return_binding_state(
    con: sqlite3.Connection, binding_id: int, target: str, error: str | None
) -> None:
    row = con.execute(
        "SELECT state FROM shell_session_bindings WHERE binding_id=?", (binding_id,)
    ).fetchone()
    if not row:
        return
    # Release/error may have been requested while the transport was returning.
    # Never overwrite that terminal state with a stale dispatcher result.
    if row["state"] == "dispatching":
        session_control.transition_binding(
            con, binding_id, expected="dispatching", target=target
        )
    con.execute(
        "UPDATE shell_session_bindings SET last_error=?, updated_at=datetime('now') "
        "WHERE binding_id=?",
        (error, binding_id),
    )


def _reconstruct_turn_arrivals(con: sqlite3.Connection, batch: WakeBatch) -> None:
    """Ledger messages that arrived during the turn, including already-read rows."""
    con.execute(
        "INSERT OR IGNORE INTO session_wake_jobs (binding_id, trigger_message_id) "
        "SELECT ?, message_id FROM shell_messages "
        "WHERE to_shell_id=? AND message_id>?",
        (batch.binding_id, batch.shell_id, batch.message_watermark),
    )


def finish_batch(
    con: sqlite3.Connection,
    batch: WakeBatch,
    *,
    return_state: str,
    error: object | None = None,
) -> BatchResult:
    """Apply ``read_at`` acknowledgement and retry only unread running jobs."""
    if return_state not in ("idle", "dormant", "foreground"):
        raise ValueError(f"invalid post-dispatch state {return_state!r}")
    clean_error = sanitize_error(error) if error is not None else None
    con.execute("BEGIN IMMEDIATE")
    try:
        _reconstruct_turn_arrivals(con, batch)
        # A message delivered during the turn can be acknowledged before its
        # wake row is reconstructed.  Mark all such rows done in the same txn.
        con.execute(
            "UPDATE session_wake_jobs SET state='done', finished_at=datetime('now'), "
            "last_error=NULL WHERE binding_id=? AND state!='cancelled' "
            "AND EXISTS (SELECT 1 FROM shell_messages m "
            "WHERE m.message_id=session_wake_jobs.trigger_message_id "
            "AND m.read_at IS NOT NULL)",
            (batch.binding_id,),
        )

        marks = ",".join("?" for _ in batch.wake_ids)
        unread = con.execute(
            f"SELECT wake_id, attempt_count FROM session_wake_jobs "
            f"WHERE wake_id IN ({marks}) AND state='running' ORDER BY wake_id",
            batch.wake_ids,
        ).fetchall()
        terminal = any(row["attempt_count"] >= MAX_ATTEMPTS for row in unread)
        for row in unread:
            if terminal or row["attempt_count"] >= MAX_ATTEMPTS:
                con.execute(
                    "UPDATE session_wake_jobs SET state='failed', "
                    "finished_at=datetime('now'), last_error=? WHERE wake_id=?",
                    (clean_error or "turn exited before inbox acknowledgement", row["wake_id"]),
                )
                continue
            delay = RETRY_DELAYS[row["attempt_count"] - 1]
            con.execute(
                "UPDATE session_wake_jobs SET state='queued', "
                "available_at=datetime('now', ?), finished_at=datetime('now'), "
                "last_error=? WHERE wake_id=?",
                (f"+{delay} seconds",
                 clean_error or "turn exited before inbox acknowledgement",
                 row["wake_id"]),
            )

        if terminal:
            _return_binding_state(
                con, batch.binding_id, "error",
                clean_error or "session wake retry budget exhausted",
            )
        else:
            _return_binding_state(con, batch.binding_id, return_state, clean_error)

        acknowledged = con.execute(
            f"SELECT COUNT(*) FROM session_wake_jobs WHERE wake_id IN ({marks}) "
            "AND state='done'", batch.wake_ids
        ).fetchone()[0]
        queued = con.execute(
            f"SELECT COUNT(*) FROM session_wake_jobs WHERE wake_id IN ({marks}) "
            "AND state='queued'", batch.wake_ids
        ).fetchone()[0]
        failed = con.execute(
            f"SELECT COUNT(*) FROM session_wake_jobs WHERE wake_id IN ({marks}) "
            "AND state='failed'", batch.wake_ids
        ).fetchone()[0]
        con.commit()
        return BatchResult(acknowledged, queued, failed)
    except Exception:
        con.rollback()
        raise


def defer_busy_batch(con: sqlite3.Connection, batch: WakeBatch,
                     *, return_state: str = "idle") -> BatchResult:
    """Undo a claim when the provider became busy before transport started."""
    con.execute("BEGIN IMMEDIATE")
    try:
        marks = ",".join("?" for _ in batch.wake_ids)
        cur = con.execute(
            f"UPDATE session_wake_jobs SET state='queued', "
            f"attempt_count=attempt_count-1, available_at=datetime('now'), "
            f"started_at=NULL, finished_at=NULL, last_error=NULL "
            f"WHERE wake_id IN ({marks}) AND state='running'",
            batch.wake_ids,
        )
        if cur.rowcount != len(batch.wake_ids):
            raise RuntimeError("wake batch changed while deferring busy provider")
        _return_binding_state(con, batch.binding_id, return_state, None)
        con.commit()
        return BatchResult(acknowledged=0, queued=len(batch.wake_ids), failed=0)
    except Exception:
        con.rollback()
        raise


def recover_interrupted(con: sqlite3.Connection, binding_id: int) -> BatchResult | None:
    """Requeue a crash-left running batch after ownership is known vacant."""
    jobs = con.execute(
        "SELECT wake_id, trigger_message_id FROM session_wake_jobs "
        "WHERE binding_id=? AND state='running' ORDER BY wake_id",
        (binding_id,),
    ).fetchall()
    if not jobs:
        return None
    binding = con.execute(
        "SELECT shell_id FROM shell_session_bindings WHERE binding_id=?", (binding_id,)
    ).fetchone()
    if not binding:
        return None
    watermark = con.execute(
        "SELECT COALESCE(MAX(message_id),0) FROM shell_messages WHERE to_shell_id=?",
        (binding["shell_id"],),
    ).fetchone()[0]
    return finish_batch(
        con,
        WakeBatch(
            binding_id,
            binding["shell_id"],
            tuple(row["wake_id"] for row in jobs),
            tuple(row["trigger_message_id"] for row in jobs),
            watermark,
        ),
        return_state="dormant",
        error="dispatcher restarted before inbox acknowledgement",
    )


def set_binding_error(con: sqlite3.Connection, binding_id: int, error: object) -> None:
    clean = sanitize_error(error)
    row = con.execute(
        "SELECT state FROM shell_session_bindings WHERE binding_id=?", (binding_id,)
    ).fetchone()
    if not row:
        return
    target = "error"
    if row["state"] != target:
        session_control.transition_binding(
            con, binding_id, expected=row["state"], target=target
        )
    con.execute(
        "UPDATE shell_session_bindings SET last_error=?, updated_at=datetime('now') "
        "WHERE binding_id=?", (clean, binding_id)
    )
    con.commit()


def load_adapter(binding: dict) -> Adapter:
    harness = binding["harness"]
    path = ENGINE / "adapters" / harness / "session_control.py"
    if not path.is_file():
        raise RuntimeError(f"session-control adapter unavailable for {harness}")
    spec = importlib.util.spec_from_file_location(
        f"sc_session_adapter_{harness.replace('-', '_')}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load session-control adapter for {harness}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "create_adapter", None)
    if not callable(factory):
        raise RuntimeError(f"session-control adapter for {harness} has no create_adapter()")
    adapter = factory()
    if not all(callable(getattr(adapter, name, None))
               for name in ("status", "deliver", "resume")):
        raise RuntimeError(f"session-control adapter for {harness} is incomplete")
    return adapter


def api_ready(binding: dict, api_base: str) -> bool:
    """Prove the target planner can read its authenticated inbox before a turn."""
    token = binding.get("api_key") or ""
    if not api_base or not token:
        return False
    req = urllib.request.Request(
        api_base.rstrip("/") + "/_sc/mem/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read())
        return int(payload.get("shell_id", -1)) == binding["shell_id"]
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def beat(con: sqlite3.Connection, interval: int) -> None:
    con.execute(
        "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
        "VALUES ('session-dispatcher', datetime('now'), ?) "
        "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at, "
        "interval_s=excluded.interval_s", (interval,)
    )
    con.commit()


def poll_once(
    con: sqlite3.Connection,
    *,
    repo_root: Path = REPO_ROOT,
    api_base: str = "",
    adapter_factory: Callable[[dict], Adapter] = load_adapter,
    api_probe: Callable[[dict, str], bool] = api_ready,
    reconcile: Callable[..., str] = session_supervisor.reconcile_binding,
    lease_preflight: Callable[..., None] = session_supervisor.preflight_lease,
    attempt_log: AttemptLog | None = None,
) -> int:
    """Run one reconstruction/dispatch cycle and return attempted binding count."""
    attempt_log = attempt_log or AttemptLog()
    session_control.reconstruct_wake_jobs(con)
    con.commit()
    ids = [row[0] for row in con.execute(
        "SELECT DISTINCT b.binding_id FROM shell_session_bindings b "
        "JOIN session_wake_jobs j ON j.binding_id=b.binding_id "
        "WHERE b.managed=1 AND j.state IN ('queued','running') "
        "ORDER BY b.binding_id"
    )]
    attempted = 0
    for binding_id in ids:
        binding = binding_row(con, binding_id)
        if not binding or binding["state"] in ("released", "error"):
            continue
        try:
            owner = reconcile(con, binding_id, repo_root=repo_root)
            binding = binding_row(con, binding_id)
            if not binding or binding["state"] in ("released", "error"):
                continue
            adapter = adapter_factory(binding)
            status = adapter.status(binding)
            if status not in ADAPTER_STATES:
                raise RuntimeError(f"adapter returned invalid status {status!r}")

            if (
                binding["state"] == "starting"
                and status == "dormant"
                and owner in ("vacant", "stale-cleared")
                and binding["native_session_id"]
            ):
                session_control.transition_binding(
                    con, binding_id, expected="starting", target="dormant"
                )
                con.commit()
                binding = binding_row(con, binding_id)
                if binding is None:
                    continue

            running = con.execute(
                "SELECT COUNT(*) FROM session_wake_jobs "
                "WHERE binding_id=? AND state='running'", (binding_id,)
            ).fetchone()[0]
            if running:
                if owner in ("live", "cleanup") or status == "active":
                    continue
                result = recover_interrupted(con, binding_id)
                attempt_log.write(
                    binding_id, "recovered", queued=result.queued if result else 0,
                    failed=result.failed if result else 0,
                )
                binding = binding_row(con, binding_id)
                if binding is None:
                    continue

            if status in ("starting", "active"):
                continue
            if status == "error":
                set_binding_error(con, binding_id, "provider status probe failed")
                continue
            if status == "dormant" and owner not in ("vacant", "stale-cleared"):
                # A provider probe cannot overrule exact process evidence.  This
                # is the final generic guard before a resume command exists.
                attempt_log.write(
                    binding_id, "resume-fenced",
                    error=f"recorded owner status is {owner}",
                )
                continue
            if not api_probe(binding, api_base):
                # API downtime is a local readiness condition, not a delivery
                # attempt.  Preserve queued rows and retry on the next 1s cycle.
                clean = "engine API unavailable for authenticated inbox read"
                con.execute(
                    "UPDATE shell_session_bindings SET last_error=?, "
                    "updated_at=datetime('now') WHERE binding_id=?",
                    (clean, binding_id),
                )
                con.commit()
                continue

            batch = claim_batch(con, binding_id)
            if batch is None:
                continue
            attempted += 1
            attempt_log.write(
                binding_id, "claimed", wake_ids=list(batch.wake_ids),
                message_count=len(batch.message_ids), transport=status,
            )
            try:
                if status == "dormant":
                    lease_preflight(con, binding_id, repo_root=repo_root)
                    adapter.resume(binding, WAKE_PROMPT)
                    return_state = "dormant"
                else:
                    adapter.deliver(binding, WAKE_PROMPT)
                    return_state = "idle"
                result = finish_batch(con, batch, return_state=return_state)
            except session_control.ProviderBusy as exc:
                result = defer_busy_batch(
                    con, batch,
                    return_state="dormant" if status == "dormant" else "idle",
                )
                attempt_log.write(
                    binding_id, "provider-busy", queued=result.queued, error=exc
                )
                continue
            except Exception as exc:
                result = finish_batch(
                    con, batch,
                    return_state="dormant" if status == "dormant" else "idle",
                    error=exc,
                )
            attempt_log.write(
                binding_id, "finished", acknowledged=result.acknowledged,
                queued=result.queued, failed=result.failed,
            )
        except session_supervisor.LeaseConflict as exc:
            # A live or orphan-fenced owner is expected contention.  Keep the
            # job queued; reconciliation already records dangerous orphan state.
            attempt_log.write(binding_id, "owner-busy", error=exc)
        except Exception as exc:
            clean = sanitize_error(exc)
            con.execute(
                "UPDATE shell_session_bindings SET last_error=?, "
                "updated_at=datetime('now') WHERE binding_id=?",
                (clean, binding_id),
            )
            con.commit()
            attempt_log.write(binding_id, "dispatch-error", error=clean)
    return attempted


def cmd_daemon(args: argparse.Namespace) -> int:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        sys.exit(f"session dispatcher: no usable DB at {DB_PATH} — run ./sc rebuild")
    interval = args.interval or int(os.environ.get("SC_SESSION_DISPATCH_INTERVAL", "1"))
    api_base = args.api_base or os.environ.get("SC_API_BASE", "")
    stopped = False

    def stop(_signum, _frame) -> None:
        nonlocal stopped
        stopped = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    print(
        f"session dispatcher: every {interval}s · api {api_base or 'unavailable'}",
        flush=True,
    )
    while not stopped:
        con = db_driver.connect(DB_PATH)
        try:
            beat(con, interval)
            poll_once(con, api_base=api_base)
        except Exception as exc:
            print(f"session dispatcher: cycle error ({sanitize_error(exc)})", flush=True)
        finally:
            con.close()
        if args.once:
            return 0
        deadline = time.monotonic() + interval
        while not stopped and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sc session-dispatcher")
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--api-base", default="")
    parser.add_argument("--once", action="store_true")
    parser.set_defaults(fn=cmd_daemon)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
