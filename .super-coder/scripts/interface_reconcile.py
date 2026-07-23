#!/usr/bin/env python3
"""Interface startup reconciliation + rebuild/update live-guard (spec #20).

startup_reconcile() is the DB-durable half of "ordinary service restart
reconciles the live DB": it runs once at API boot (server.main, next to the
api-key backfill) and repairs exactly what a broker/service death can leave
behind:

1. Crash-window parking (decision #22 — THE proof point): any input_state
   with a pending (accepted, unacknowledged) human frame cannot tell
   pre-write from post-write. It parks: composer=unknown,
   delivery=delivery_unknown, writer revoked, alert raised. pending_seq is
   KEPT as evidence. There is no replay — no code path re-forwards it;
   only reconcile_input() (operator inspection) clears the park.
2. Batch recovery from durable hook-sequence evidence only: submitting
   without a submit hook stamp → delivery_unknown; submitting with one →
   running (the submit is proven, the stop hook will complete it); running
   with a stop stamp → complete with items reconciled from message read
   state; running without new evidence stays running — the live harness's
   hooks re-drive it after the API is back.
3. Reservation repair: a reserved session past its lease expiry is
   unreconciled (fail closed — we cannot prove the spawn never happened).
4. Lease hygiene: every still-current writer lease is revoked (a restart
   dropped every client); composer/draft state is preserved.

live_refusal_reasons() is the rebuild/update/materialize guard: it names
every live Interface state that must be drained first. Tolerates a pre-0078
DB (no Interface tables → no reasons).
"""
from __future__ import annotations

from pathlib import Path

import db_driver
import interface_broker
import interface_state


def _has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _alert(con, **kw) -> None:
    """Raise an alert, deduplicated while open — interface_broker owns the
    helper (it parks live too); kept here as a thin alias for readability."""
    interface_broker._alert(con, **kw)


def startup_reconcile(con) -> dict:
    """Idempotent post-restart repair. Returns a counts summary for the log."""
    if not _has_table(con, "interface_sessions"):
        return {"skipped": "pre-0078 DB"}

    counts = {"reservations_unreconciled": 0, "parks": 0,
              "batches_delivery_unknown": 0, "batches_proven_running": 0,
              "batches_completed": 0, "leases_revoked": 0,
              "terminal_inputs_removed": 0,
              "terminal_input_parks": 0,
              "terminal_leases_revoked": 0}

    active_session = interface_state.active_session_sql("s")

    # A fully closed session has no actor left to accept new input.  Remove
    # ordinary volatile state, but preserve metadata-only delivery ambiguity:
    # decision #16 requires the pending sequence and operator action to survive
    # closure without continuing to block update (#529).
    cur = con.execute(
        "DELETE FROM interface_input_state WHERE session_id IN ("
        "SELECT s.session_id FROM interface_sessions s "
        f"WHERE NOT ({active_session})) "
        "AND pending_seq IS NULL AND delivery <> 'delivery_unknown'"
    )
    counts["terminal_inputs_removed"] = cur.rowcount
    terminal_parks = con.execute(
        "SELECT i.session_id FROM interface_input_state i "
        "JOIN interface_sessions s ON s.session_id=i.session_id "
        f"WHERE NOT ({active_session}) "
        "AND (i.pending_seq IS NOT NULL OR i.delivery='delivery_unknown')"
    ).fetchall()
    for (session_id,) in terminal_parks:
        interface_broker.park_delivery_unknown(con, session_id)
        counts["terminal_input_parks"] += 1
    cur = con.execute(
        "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
        "revoke_reason='session_end' WHERE revoked_at IS NULL "
        "AND session_id IN (SELECT s.session_id FROM interface_sessions s "
        f"WHERE NOT ({active_session}))"
    )
    counts["terminal_leases_revoked"] = cur.rowcount

    # 1. Crash-window parking — pending human frame at restart.
    parked = con.execute(
        "SELECT i.session_id FROM interface_input_state i "
        "JOIN interface_sessions s ON s.session_id=i.session_id "
        f"WHERE i.pending_seq IS NOT NULL AND {active_session}"
    ).fetchall()
    for (session_id,) in parked:
        interface_broker.park_delivery_unknown(con, session_id)
        counts["parks"] += 1

    # 2. Batch recovery — durable hook-sequence evidence or park.
    live_batches = con.execute(
        "SELECT batch_id, state, submit_hook_seq, stop_hook_seq "
        "FROM planner_wake_batches WHERE state IN ('submitting','running')"
    ).fetchall()
    for batch_id, state, submit_seq, stop_seq in live_batches:
        if stop_seq is not None:
            # The full submit+stop cycle is proven — reconcile items from
            # durable message read state, exactly like a live turn_stop.
            interface_broker._complete_batch(con, batch_id, stop_seq)
            counts["batches_completed"] += 1
        elif submit_seq is not None:
            # Submit proven, stop not yet seen — the batch legitimately runs;
            # the live harness's stop hook completes it.
            if state == "submitting":
                interface_state.transition(con, "wake_batch", batch_id, "running")
            counts["batches_proven_running"] += 1
        else:
            interface_state.transition(con, "wake_batch", batch_id,
                                       "delivery_unknown")
            binding = con.execute(
                "SELECT binding_id FROM planner_wake_batches WHERE batch_id=?",
                (batch_id,)).fetchone()[0]
            _alert(con, severity="critical",
                   reason="wake_batch_delivery_unknown", binding_id=binding)
            counts["batches_delivery_unknown"] += 1

    # 3. Expired reservations fail closed — ambiguous spawn ⇒ unreconciled.
    expired = con.execute(
        "SELECT session_id FROM interface_sessions "
        "WHERE occupancy='reserved' AND reservation_expires_at IS NOT NULL "
        "AND reservation_expires_at < datetime('now')"
    ).fetchall()
    for (session_id,) in expired:
        interface_state.transition(con, "occupancy", session_id, "unreconciled",
                                   extra_sets={"error_detail":
                                               "reservation expired at restart"})
        _alert(con, severity="warning", reason="reservation_expired",
               session_id=session_id)
        counts["reservations_unreconciled"] += 1

    # 4. A restart dropped every client — revoke all current writer leases.
    #    Composer/dirty state is preserved (spec: dirty survives disconnect).
    cur = con.execute(
        "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
        "revoke_reason='service_restart' WHERE revoked_at IS NULL "
        "AND session_id IN (SELECT s.session_id FROM interface_sessions s "
        f"WHERE {active_session})")
    counts["leases_revoked"] = cur.rowcount

    con.commit()
    return counts


def live_refusal_reasons(db_path) -> list[str]:
    """Why a rebuild/update/materialize must refuse right now (spec #20:
    'rebuild/update refuses while any non-ended session, unreleased binding,
    nonterminal wake batch, or input ambiguity exists'). Empty list = safe.
    A pre-0078 DB has no Interface state → no reasons."""
    if not Path(str(db_path)).exists():
        return []
    con = db_driver.connect(str(db_path))
    try:
        if not _has_table(con, "interface_sessions"):
            return []
        reasons = []
        rows = con.execute(
            "SELECT shell_id, generation FROM interface_generations "
            "WHERE ended_at IS NULL").fetchall()
        for shell_id, generation in rows:
            reasons.append(
                f"generation {shell_id}/{generation} is live — end it first "
                "(a rebuilt live generation bricks that shell's next New "
                "chat)")
        active_session = interface_state.active_session_sql("s")
        rows = con.execute(
            "SELECT s.session_id, s.shell_id, s.occupancy "
            "FROM interface_sessions s "
            f"WHERE {active_session}").fetchall()
        for session_id, shell_id, occupancy in rows:
            reasons.append(
                f"interface session {session_id} (shell {shell_id}) is "
                f"{occupancy} — end/reconcile it first")
        rows = con.execute(
            "SELECT b.binding_id, b.sprint_doc_id "
            "FROM sprint_planner_bindings b "
            "LEFT JOIN interface_sessions s ON s.session_id=b.session_id "
            "WHERE b.released_at IS NULL "
            f"AND (s.session_id IS NULL OR {active_session})").fetchall()
        for binding_id, doc_id in rows:
            reasons.append(
                f"sprint binding {binding_id} (doc {doc_id}) is armed — "
                "release it first")
        rows = con.execute(
            "SELECT wb.batch_id, wb.state FROM planner_wake_batches wb "
            "LEFT JOIN sprint_planner_bindings b ON b.binding_id=wb.binding_id "
            "LEFT JOIN interface_sessions s ON s.session_id=b.session_id "
            "WHERE wb.state <> 'complete' "
            f"AND (b.binding_id IS NULL OR s.session_id IS NULL "
            f"OR {active_session})").fetchall()
        for batch_id, state in rows:
            reasons.append(
                f"wake batch {batch_id} is {state} — drain or reconcile it "
                "first")
        rows = con.execute(
            "SELECT i.session_id FROM interface_input_state i "
            "LEFT JOIN interface_sessions s ON s.session_id=i.session_id "
            "WHERE (i.composer='unknown' OR i.delivery='delivery_unknown' "
            "OR i.pending_seq IS NOT NULL) "
            f"AND (s.session_id IS NULL OR {active_session})").fetchall()
        for (session_id,) in rows:
            reasons.append(
                f"session {session_id} has input ambiguity — inspect the live "
                "TUI and reconcile first")
        return reasons
    finally:
        con.close()
