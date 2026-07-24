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

from dataclasses import dataclass
from pathlib import Path

import db_driver
import interface_broker
import interface_state


def _has_table(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@dataclass(frozen=True)
class LiveLossManifest:
    """Exact durable rows an operator-approved update will terminalize."""

    generations: tuple[tuple[int, int], ...] = ()
    sessions: tuple[int, ...] = ()
    archives: tuple[int, ...] = ()
    archive_links: tuple[tuple[int, int], ...] = ()
    writer_leases: tuple[int, ...] = ()
    input_states: tuple[int, ...] = ()
    orphan_inputs: tuple[int, ...] = ()
    bindings: tuple[int, ...] = ()
    batches: tuple[int, ...] = ()
    items: tuple[int, ...] = ()
    alerts: tuple[int, ...] = ()

    def empty(self) -> bool:
        return not any((
            self.generations, self.sessions, self.archives,
            self.archive_links, self.writer_leases, self.input_states,
            self.orphan_inputs, self.bindings, self.batches, self.items,
            self.alerts,
        ))

    def loss_lines(self) -> list[str]:
        rows = [
            ("Interface generation(s)",
             ", ".join(f"{shell}/{generation}"
                       for shell, generation in self.generations)),
            ("Interface session ID(s)", _csv(self.sessions)),
            ("open archive ID(s)", _csv(self.archives)),
            ("active archive link(s)",
             ", ".join(f"shell {shell}→archive {archive}"
                       for shell, archive in self.archive_links)),
            ("writer lease ID(s)", _csv(self.writer_leases)),
            ("input-state session ID(s)", _csv(self.input_states)),
            ("orphan input-state session ID(s)", _csv(self.orphan_inputs)),
            ("sprint binding ID(s)", _csv(self.bindings)),
            ("wake batch ID(s)", _csv(self.batches)),
            ("wake item ID(s)", _csv(self.items)),
            ("open planner alert ID(s)", _csv(self.alerts)),
        ]
        return [f"{label}: {values}" for label, values in rows if values]


class LiveStateChanged(RuntimeError):
    """The consented loss set changed before the discard write lock."""


def _csv(values) -> str:
    return ", ".join(str(value) for value in values)


def _ids(con, query: str, params=()) -> tuple[int, ...]:
    return tuple(row[0] for row in con.execute(query, params).fetchall())


def _live_loss_manifest(con) -> LiveLossManifest:
    if not _has_table(con, "interface_sessions"):
        return LiveLossManifest()

    active_session = interface_state.active_session_sql("s")
    sessions = _ids(
        con,
        "SELECT s.session_id FROM interface_sessions s "
        f"WHERE {active_session} ORDER BY s.session_id",
    )
    bindings = _ids(
        con,
        "SELECT binding_id FROM sprint_planner_bindings "
        "WHERE released_at IS NULL ORDER BY binding_id",
    )

    session_filter = ",".join("?" for _ in sessions)
    binding_filter = ",".join("?" for _ in bindings)
    archives = ()
    archive_links = ()
    writer_leases = ()
    input_states = ()
    if sessions:
        archives = _ids(
            con,
            "SELECT DISTINCT s.archive_id FROM interface_sessions s "
            "JOIN shell_memory_archives a ON a.archive_id=s.archive_id "
            f"WHERE s.session_id IN ({session_filter}) "
            "AND a.ended_at IS NULL ORDER BY s.archive_id",
            sessions,
        )
        archive_links = tuple(tuple(row) for row in con.execute(
            "SELECT s.shell_id, s.archive_id FROM interface_sessions s "
            "JOIN shells sh ON sh.shell_id=s.shell_id "
            f"WHERE s.session_id IN ({session_filter}) "
            "AND sh.active_archive_id=s.archive_id "
            "ORDER BY s.shell_id, s.archive_id",
            sessions,
        ).fetchall())
        writer_leases = _ids(
            con,
            "SELECT lease_id FROM interface_writer_leases "
            f"WHERE session_id IN ({session_filter}) AND revoked_at IS NULL "
            "ORDER BY lease_id",
            sessions,
        )
        input_states = _ids(
            con,
            "SELECT session_id FROM interface_input_state "
            f"WHERE session_id IN ({session_filter}) ORDER BY session_id",
            sessions,
        )

    alert_clauses = []
    alert_params = []
    if sessions:
        alert_clauses.append(f"session_id IN ({session_filter})")
        alert_params.extend(sessions)
    if bindings:
        alert_clauses.append(f"binding_id IN ({binding_filter})")
        alert_params.extend(bindings)
    alerts = ()
    if alert_clauses:
        alerts = _ids(
            con,
            "SELECT alert_id FROM planner_alerts WHERE resolved_at IS NULL "
            f"AND ({' OR '.join(alert_clauses)}) ORDER BY alert_id",
            alert_params,
        )

    return LiveLossManifest(
        generations=tuple(tuple(row) for row in con.execute(
            "SELECT shell_id, generation FROM interface_generations "
            "WHERE ended_at IS NULL ORDER BY shell_id, generation"
        ).fetchall()),
        sessions=sessions,
        archives=archives,
        archive_links=archive_links,
        writer_leases=writer_leases,
        input_states=input_states,
        orphan_inputs=_ids(
            con,
            "SELECT i.session_id FROM interface_input_state i "
            "WHERE NOT EXISTS (SELECT 1 FROM interface_sessions s "
            "WHERE s.session_id=i.session_id) ORDER BY i.session_id",
        ),
        bindings=bindings,
        batches=_ids(
            con,
            "SELECT batch_id FROM planner_wake_batches "
            "WHERE state <> 'complete' ORDER BY batch_id",
        ),
        items=_ids(
            con,
            "SELECT item_id FROM planner_wake_items "
            "WHERE state NOT IN ('done','cancelled') ORDER BY item_id",
        ),
        alerts=alerts,
    )


def live_loss_manifest(db_path) -> LiveLossManifest:
    """Read the exact loss set shown before destructive update consent."""
    if not Path(str(db_path)).exists():
        return LiveLossManifest()
    con = db_driver.connect(str(db_path))
    try:
        return _live_loss_manifest(con)
    finally:
        con.close()


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
              "terminal_leases_revoked": 0,
              "terminal_alerts_resolved": 0}

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
        # Keep the metadata-only ambiguity, but do not raise a new
        # session-scoped alert: every alert for an ended session is resolved
        # audit, including across repeated service starts (spec #30 req 19).
        interface_state.transition(con, "composer", session_id, "unknown")
        interface_state.transition(
            con, "delivery", session_id, "delivery_unknown")
        counts["terminal_input_parks"] += 1
    cur = con.execute(
        "UPDATE planner_alerts SET resolved_at=("
        "SELECT s.ended_at FROM interface_sessions s "
        "WHERE s.session_id=planner_alerts.session_id) "
        "WHERE resolved_at IS NULL AND EXISTS ("
        "SELECT 1 FROM interface_sessions s "
        "WHERE s.session_id=planner_alerts.session_id "
        f"AND NOT ({active_session}))"
    )
    counts["terminal_alerts_resolved"] = cur.rowcount
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


def discard_live_state(
        db_path, expected: LiveLossManifest) -> dict[str, int]:
    """Atomically abandon exactly the operator-consented Interface actors.

    This is the deliberately destructive, operator-consented escape hatch for
    an update whose API is down: it uses only the current engine and its local
    DB, so it cannot deadlock on an API-only recovery command.  Durable audit
    rows are terminalized rather than deleted; unread messages and unrelated
    shell memory are untouched. The manifest is re-read under BEGIN IMMEDIATE
    before the first write; any change refuses with zero mutation.
    """
    counts = {
        "sessions_ended": 0,
        "generations_ended": 0,
        "archives_closed": 0,
        "bindings_released": 0,
        "batches_completed": 0,
        "items_cancelled": 0,
        "orphan_inputs_removed": 0,
    }
    if not Path(str(db_path)).exists():
        return counts

    con = db_driver.connect(str(db_path))
    try:
        if not _has_table(con, "interface_sessions"):
            return counts
        con.execute("BEGIN IMMEDIATE")
        current = _live_loss_manifest(con)
        if current != expected:
            raise LiveStateChanged(
                "live Interface state changed while awaiting consent")

        for session_id in expected.sessions:
            shell_id, archive_id = con.execute(
                "SELECT shell_id, archive_id FROM interface_sessions "
                "WHERE session_id=?", (session_id,)
            ).fetchone()
            result = interface_broker.close_session(
                con, session_id, "update_discard")
            if not result["already_ended"]:
                counts["sessions_ended"] += 1
            if archive_id in expected.archives:
                cur = con.execute(
                    "UPDATE shell_memory_archives SET ended_at=datetime('now') "
                    "WHERE archive_id=? AND ended_at IS NULL", (archive_id,))
                counts["archives_closed"] += cur.rowcount
            if (shell_id, archive_id) in expected.archive_links:
                con.execute(
                    "UPDATE shells SET active_archive_id=NULL "
                    "WHERE shell_id=? AND active_archive_id=?",
                    (shell_id, archive_id))

        for shell_id, generation in expected.generations:
            cur = con.execute(
                "UPDATE interface_generations SET ended_at=datetime('now') "
                "WHERE shell_id=? AND generation=? AND ended_at IS NULL",
                (shell_id, generation))
            counts["generations_ended"] += cur.rowcount

        counts["batches_completed"] = len(expected.batches)
        counts["items_cancelled"] = len(expected.items)

        for binding_id in expected.bindings:
            interface_broker.release_binding(
                con, binding_id, "update_discard")
            counts["bindings_released"] += 1

        for item_id in expected.items:
            state = con.execute(
                "SELECT state FROM planner_wake_items WHERE item_id=?",
                (item_id,)).fetchone()[0]
            if state in ("done", "cancelled"):
                continue
            interface_state.transition(
                con, "wake_item", item_id, "cancelled",
                extra_sets={"error": "discarded by operator-approved update"})

        for batch_id in expected.batches:
            state = con.execute(
                "SELECT state FROM planner_wake_batches WHERE batch_id=?",
                (batch_id,)).fetchone()[0]
            if state == "complete":
                continue
            if state == "submitting":
                interface_state.transition(
                    con, "wake_batch", batch_id, "delivery_unknown")
            interface_state.transition(
                con, "wake_batch", batch_id, "complete",
                extra_sets={"completed_at": con.execute(
                    "SELECT datetime('now')").fetchone()[0]})

        for session_id in expected.orphan_inputs:
            cur = con.execute(
                "DELETE FROM interface_input_state WHERE session_id=? "
                "AND NOT EXISTS (SELECT 1 FROM interface_sessions s "
                "WHERE s.session_id=interface_input_state.session_id)",
                (session_id,))
            counts["orphan_inputs_removed"] += cur.rowcount

        for alert_id in expected.alerts:
            con.execute(
                "UPDATE planner_alerts SET resolved_at=datetime('now') "
                "WHERE alert_id=? AND resolved_at IS NULL", (alert_id,))
        # close_session may have created a delivery-unknown alert after the
        # manifest was locked. It is operation-local, not concurrently-created
        # state; resolve only those new alerts tied to the confirmed actors.
        if expected.sessions or expected.bindings:
            clauses = []
            params = []
            if expected.sessions:
                marks = ",".join("?" for _ in expected.sessions)
                clauses.append(f"session_id IN ({marks})")
                params.extend(expected.sessions)
            if expected.bindings:
                marks = ",".join("?" for _ in expected.bindings)
                clauses.append(f"binding_id IN ({marks})")
                params.extend(expected.bindings)
            con.execute(
                "UPDATE planner_alerts SET resolved_at=datetime('now') "
                "WHERE resolved_at IS NULL "
                f"AND ({' OR '.join(clauses)})", params)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    remaining = live_refusal_reasons(db_path)
    if remaining:
        raise RuntimeError(
            "operator-approved Interface discard did not drain: "
            + "; ".join(remaining))
    return counts
