#!/usr/bin/env python3
"""Transactional brokered planner wake — event ingress + the wake
coordinator (spec #20 Event Ingress / Wake Delivery / Retry Policy,
sprint 25 seq 8, task #84).

Event ingress: maybe_create_wake_item() runs IN the message insert's own
transaction (unique (binding_id, message_id) is the dedupe backstop) — the
message and its wake work commit or roll back together, so no eligible
sprint event is ever lost and none is ever woken twice. After commit the
producer signals the coordinator.

The coordinator is event-driven (spec: "performs no interval model scan"):
message commits, harness hook callbacks, clean certifications, binding
arms, and one startup pass are its only triggers. A gate that fails on the
quiet debounce schedules ONE re-attempt at the exact debounce deadline
(event-reset, never polling); a definite pre-send failure (PreSendError —
the writer proved no byte moved) rides the bounded 1s/5s/30s retry
schedule and then stops with an alert. Anything ambiguous parks
delivery_unknown inside the broker and is NEVER auto-replayed here — this
module has no resubmit path for parked batches (decision #22: only
operator resolve_batch requeues parked work).
"""
from __future__ import annotations

import asyncio
import threading

import db_driver
import interface_broker
import interface_hooks

ELIGIBLE_KINDS = ("task", "result", "pr_event")
RETRY_DELAYS_S = (1.0, 5.0, 30.0)  # bounded pre-send retries (spec table)


# ── Event ingress (in-transaction wake-item creation) ────────────────────────

def maybe_create_wake_item(con, message_id: int) -> "int | None":
    """Create the wake item for one freshly inserted message iff it is
    eligible (spec #20 Sprint Scope) — same transaction as the message, so
    the pair is atomic. Returns the item_id, or None when ineligible.

    Eligibility, exactly the spec's list: a typed sprint event (task /
    result / pr_event — `shell` and legacy unscoped traffic NEVER wake),
    carrying a sprint_doc_id whose document exists, is unfrozen, and
    declares status ACTIVE; an ACTIVE (unreleased) binding for that sprint
    names this message's recipient as planner; the binding's Interface
    session and generation are still live (not ended/replaced); and the
    harness supports the mandatory lifecycle hooks (a capability gap
    refuses arming AND ingress — the wake would be unverifiable). Message
    bodies are never parsed for sprint identity.
    """
    msg = con.execute(
        "SELECT to_shell_id, kind, sprint_doc_id FROM shell_messages "
        "WHERE message_id=?", (message_id,)).fetchone()
    if msg is None or msg[1] not in ELIGIBLE_KINDS or msg[2] is None:
        return None
    to_shell_id, _, sprint_doc_id = msg
    doc = con.execute(
        "SELECT frozen FROM documents WHERE document_id=?",
        (sprint_doc_id,)).fetchone()
    if doc is None or doc[0] or not interface_broker._sprint_active(
            con, sprint_doc_id):
        return None
    binding = con.execute(
        "SELECT binding_id, session_id, shell_id, generation "
        "FROM sprint_planner_bindings "
        "WHERE sprint_doc_id=? AND planner_shell_id=? AND released_at IS NULL",
        (sprint_doc_id, to_shell_id)).fetchone()
    if binding is None:
        return None
    sess = con.execute(
        "SELECT occupancy, generation, harness, cli_version "
        "FROM interface_sessions WHERE session_id=?", (binding[1],)).fetchone()
    if sess is None or sess[0] != "occupied" or sess[1] != binding[3]:
        return None  # session ended or generation replaced — binding is stale
    if not interface_hooks.capability(sess[2], sess[3])["mandatory_ok"]:
        return None
    cur = con.execute(
        "INSERT OR IGNORE INTO planner_wake_items (binding_id, message_id) "
        "VALUES (?, ?)", (binding[0], message_id))
    return cur.lastrowid if cur.rowcount else None


# ── The coordinator ───────────────────────────────────────────────────────────

class WakeCoordinator:
    """Event-driven drain of queued wake work through the API-owned input
    path. One instance lives on the API server's asyncio loop; producers
    (routes, the PR poller thread, hook callbacks) signal it thread-safely.

    writer_factory(session_id) -> the broker-owned tmux writer for that
    session's generation (raises interface_broker.PreSendError when its
    preflight proves no byte moved); unmanaged_probe(session_id) -> True
    when an unmanaged writable tmux client is attached (decision #15).
    Both are injected so the coordinator — and every crash window — is
    hermetically testable without tmux.
    """

    def __init__(self, db_path: str, *, writer_factory, unmanaged_probe,
                 quiet_s: float = interface_broker.DEFAULT_QUIET_S):
        self.db_path = str(db_path)
        self.writer_factory = writer_factory
        self.unmanaged_probe = unmanaged_probe
        self.quiet_s = quiet_s
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self._lock = threading.Lock()
        self._scheduled: set[int] = set()       # binding_ids with a drain queued
        self._timers: dict[int, asyncio.TimerHandle] = {}
        self._pre_send_attempts: dict[int, int] = {}  # batch_id -> retries used

    # -- signals (thread-safe; no-ops before start) -----------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def notify_binding(self, binding_id: int) -> None:
        """Signal that a binding may have submittable work. Coalesced: one
        queued drain per binding no matter how many events arrive."""
        if self.loop is None:
            return
        with self._lock:
            if binding_id in self._scheduled:
                return
            self._scheduled.add(binding_id)
        self.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._drain(binding_id)))

    def notify_message(self, message_id: int) -> None:
        binding_id = self._binding_for(
            "SELECT binding_id FROM planner_wake_items WHERE message_id=?",
            (message_id,))
        if binding_id is not None:
            self.notify_binding(binding_id)

    def notify_session(self, session_id: int) -> None:
        binding_id = self._binding_for(
            "SELECT binding_id FROM sprint_planner_bindings "
            "WHERE session_id=? AND released_at IS NULL", (session_id,))
        if binding_id is not None:
            self.notify_binding(binding_id)

    def _binding_for(self, sql: str, params) -> "int | None":
        try:
            con = db_driver.connect(self.db_path)
            try:
                row = con.execute(sql, params).fetchone()
                return row[0] if row else None
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — a signal must never break its producer
            return None

    def startup_pass(self) -> None:
        """The one startup reconciliation of wake work (spec Event Ingress):
        every unreleased binding with queued items gets exactly one drain
        signal. No steady timer is installed — events drive from here."""
        try:
            con = db_driver.connect(self.db_path)
            try:
                rows = con.execute(
                    "SELECT DISTINCT i.binding_id FROM planner_wake_items i "
                    "JOIN sprint_planner_bindings b "
                    "ON b.binding_id=i.binding_id "
                    "WHERE i.state='queued' AND b.released_at IS NULL"
                ).fetchall()
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            return
        for (binding_id,) in rows:
            self.notify_binding(binding_id)

    # -- the drain ---------------------------------------------------------------

    async def _drain(self, binding_id: int) -> None:
        try:
            await asyncio.to_thread(self._drain_sync, binding_id)
        finally:
            with self._lock:
                self._scheduled.discard(binding_id)

    def _schedule_retry(self, binding_id: int, delay: float) -> None:
        """One re-attempt at the exact debounce deadline / retry delay.
        Event-reset semantics: a fresh event may drain first; the timer is
        deduped per binding and the re-drain re-gates from live state.
        call_later is loop-thread-only, so hop through call_soon_threadsafe
        (the drain runs in a worker thread)."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._arm_timer, binding_id, delay)

    def _arm_timer(self, binding_id: int, delay: float) -> None:
        with self._lock:
            old = self._timers.pop(binding_id, None)
            if old is not None:
                old.cancel()
            self._scheduled.add(binding_id)
            self._timers[binding_id] = self.loop.call_later(
                delay,
                lambda: asyncio.ensure_future(self._drain(binding_id)))

    def _drain_sync(self, binding_id: int) -> None:
        con = db_driver.connect(self.db_path)
        try:
            binding = con.execute(
                "SELECT session_id, shell_id, generation, released_at "
                "FROM sprint_planner_bindings WHERE binding_id=?",
                (binding_id,)).fetchone()
            if binding is None or binding[3] is not None:
                return
            session_id = binding[0]
            live = con.execute(
                "SELECT batch_id, state FROM planner_wake_batches "
                "WHERE binding_id=? AND state IN ('queued','submitting',"
                "'running')", (binding_id,)).fetchone()
            if live is not None and live[1] in ("submitting", "running"):
                return  # the hook evidence drives it from here
            if live is None:
                queued = con.execute(
                    "SELECT 1 FROM planner_wake_items WHERE binding_id=? "
                    "AND state='queued' AND batch_id IS NULL LIMIT 1",
                    (binding_id,)).fetchone()
                if queued is None:
                    return
                batch_id = interface_broker.form_batch(con, binding_id)
                con.commit()
            else:
                batch_id = live[0]

            now_iso = con.execute("SELECT datetime('now')").fetchone()[0]
            try:
                out = interface_broker.submit_wake_batch(
                    con, batch_id, self.writer_factory(session_id), now_iso,
                    quiet_s=self.quiet_s,
                    unmanaged_writable=lambda: self.unmanaged_probe(session_id))
            except interface_broker.PreSendError:
                self._pre_send_failed(con, binding_id, batch_id)
                return
            if out.get("submitted"):
                self._pre_send_attempts.pop(batch_id, None)
            elif out.get("retry_after") is not None:
                self._schedule_retry(binding_id, out["retry_after"])
            # any other gate failure (busy/dirty/pending/disarmed/cancelled)
            # awaits the next event — no timer, no poll.
        finally:
            con.close()

    def _pre_send_failed(self, con, binding_id: int, batch_id: int) -> None:
        """Bounded pre-send retries (spec Retry Policy): one definite
        pre-send failure retries at 1s, 5s, 30s and then STOPS — the batch
        stays queued (no byte ever moved) and an alert names the stall."""
        attempts = self._pre_send_attempts.get(batch_id, 0) + 1
        self._pre_send_attempts[batch_id] = attempts
        if attempts <= len(RETRY_DELAYS_S):
            self._schedule_retry(binding_id, RETRY_DELAYS_S[attempts - 1])
            return
        self._pre_send_attempts.pop(batch_id, None)
        interface_broker._alert(
            con, severity="critical", reason="wake_presend_retries_exhausted",
            binding_id=binding_id)
        con.commit()


# ── Module handle: producers signal without knowing the runtime ──────────────
# Routes (any thread), the PR poller thread, and server startup all signal
# through these; with no coordinator bound (Interface stack down, CLI
# contexts, hermetic route tests) they are deliberate no-ops — the work is
# durable and the next startup_pass/coordinator bind drains it.

_COORDINATOR: "WakeCoordinator | None" = None


def bind(coordinator: "WakeCoordinator | None") -> None:
    global _COORDINATOR
    _COORDINATOR = coordinator


def notify_message(message_id: int) -> None:
    if _COORDINATOR is not None:
        _COORDINATOR.notify_message(message_id)


def notify_session(session_id: int) -> None:
    if _COORDINATOR is not None:
        _COORDINATOR.notify_session(session_id)


def notify_binding(binding_id: int) -> None:
    if _COORDINATOR is not None:
        _COORDINATOR.notify_binding(binding_id)
