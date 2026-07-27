#!/usr/bin/env python3
"""Interface input broker — durable two-phase input path (spec #20, task #80).

This is the DB-side half of the broker: the ordered, fenced, metadata-only
commit protocol every accepted human frame and wake submission walks. The
byte transport (tmux send-keys) is INJECTED as `writer` so the crash windows
are hermetically provable — a writer that records then raises simulates
"crash after the tmux write"; one that raises first simulates "crash before".

The crash window (decision #22): accept_human_input commits a `pending`
reservation BEFORE calling writer() and commits `forwarded` only AFTER it
returns. A broker death in between leaves pending_seq set; the next startup's
reconcile (interface_reconcile.startup_reconcile) cannot distinguish
pre-write from post-write, so it parks composer AND delivery as unknown,
revokes the writer, and never replays. This module has no replay path —
there is deliberately no "resend pending" function.

No input bytes are ever stored: only sequence numbers, lengths, and times.
"""
from __future__ import annotations

import hashlib

import interface_hooks
import interface_state
import sprint_state

MAX_INPUT_BYTES = 64 * 1024  # one human frame, per the pinned spike protocol
WAKE_PROMPT = "Check your inbox and act on unread sprint events."
DEFAULT_QUIET_S = 3.0  # debounce, never proof of an empty composer
MAX_COMPLETED_WAKES = 3  # unread after 3 completed wake turns → quarantined
# H-26: how long a batch may sit `queued` under an armed binding before the
# stall becomes an alert. GENEROUS BY DESIGN — it bounds INVISIBILITY, not
# delivery: a batch legitimately waits out a busy planner, and nothing here
# hurries it. What it forbids is issue #638's shape, where a batch was
# invisible for 33 minutes across 11 accumulated items with no way to see the
# cause. Five minutes is far outside any ordinary deferral (the debounce is
# 3 seconds) and far inside the window where a stalled sprint still matters.
WAKE_BATCH_STALL_S = 300.0
# H-27: how long a DECLARED hook may go unobserved before the declaration is
# reported as contradicted. Two measurements, two constants, because they
# measure different silences.
#
# HOOKS_READY_SILENT_S — session creation to the harness's own session_start,
# for a harness that declares it arrives at startup. Not a fitted number:
# nothing in the deployment sample is a distribution to fit (claude 46/46 and
# kimi 2/2 stamped; the codex readings are first-turn-gated and this clause is
# not evaluated for them at all — decisions #98/#99). Three minutes is far
# outside any harness boot and far inside the window where a deaf planner
# still matters.
#
# HOOKS_SUBMIT_SILENT_S — the submitting -> running wait for the submit hook.
# Much tighter, and it can be, because NO model latency sits inside it: the
# engine wrote the bytes and pressed Enter, and UserPromptSubmit fires at that
# keystroke. A minute is two orders of magnitude past the hook's own round
# trip.
#
# Both bound INVISIBILITY, not delivery — nothing here hurries a hook, and
# neither threshold is evaluated for a seat that has already been observed.
HOOKS_READY_SILENT_S = 180.0
HOOKS_SUBMIT_SILENT_S = 60.0


class BrokerError(ValueError):
    """A refused broker operation (stale generation, bad sequence, gate)."""


class PreSendError(Exception):
    """A DEFINITE pre-send failure: the writer proved no byte reached tmux
    (its preflight failed before any send-keys call). Distinct from an
    ambiguous write failure (which parks delivery_unknown): a PreSendError
    returns the batch to queued and rides the coordinator's bounded pre-send
    retry schedule (1s/5s/30s, spec #20 Retry Policy) instead of parking."""


def _begin_immediate(con) -> bool:
    """Serialize a check-then-act gate (REV2 seq-4 L5 TOCTOU): take the DB
    write lock BEFORE the gate reads so a concurrent gate on another
    connection cannot pass on the same pre-commit snapshot. WAL +
    busy_timeout make the contender wait, then re-read post-commit state.
    Returns True when THIS call opened the transaction — the caller must
    then release it (commit or rollback) on every exit path; False when the
    connection was already in a transaction (serialization is then the
    caller's own)."""
    if con.in_transaction:
        return False
    con.execute("BEGIN IMMEDIATE")
    return True


def _now(con) -> str:
    return con.execute("SELECT datetime('now')").fetchone()[0]


def _session(con, session_id: int):
    row = con.execute(
        "SELECT session_id, shell_id, generation, occupancy, lifecycle "
        "FROM interface_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise BrokerError(f"interface session {session_id} not found")
    return row


def _alert(con, *, severity: str, reason: str, session_id=None,
           binding_id=None, message_id=None, batch_id=None,
           sprint_doc_id=None, detail=None) -> None:
    """Raise an alert, deduplicated while open (partial unique index).

    A session-scoped alert takes the write lock before reading terminal state.
    That makes the read + write atomic with durable closure: closure either
    lands first and this row is resolved audit, or lands second and resolves
    the row itself. The caller still owns the surrounding transaction.

    `detail` carries the MEASUREMENT (a verbatim gate reason, a capability
    gap) and is deliberately outside the dedupe key: one open row per
    condition, whose detail REFRESHES to the most recent observation rather
    than minting a new alert per distinct string. Per decision #76 it states
    what was measured, never a verdict.

    `sprint_doc_id` is the post-0102 reconciliation column (with `unit_id`,
    `role`, `signal`) — sprint-flow alerts carry the sprint they belong to
    rather than encoding it into `dedupe_key` alone, so
    `idx_planner_alerts_reconciliation` can find them.

    BOTH NEW SCOPES APPEND; neither widens the key. The obvious edit — splicing
    batch_id in beside the other refs — rewrites the key of EVERY alert,
    including the ones carrying neither new scope. Nothing new would then
    collide with a row already open in a live DB, and each open alert would
    mint one duplicate at deploy. Appending only when set leaves every existing
    key byte-identical, and it cannot merge two rows that should be distinct,
    because a batch determines its binding and a binding its sprint.
    """
    dedupe = (f"{session_id or '-'}|{binding_id or '-'}|{message_id or '-'}"
              f"|{reason}")
    if batch_id is not None:
        dedupe += f"|batch{batch_id}"
    if sprint_doc_id is not None:
        dedupe += f"|sprint{sprint_doc_id}"
    if session_id is not None:
        _begin_immediate(con)
        session = con.execute(
            "SELECT occupancy, lifecycle, ended_at "
            "FROM interface_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if session is not None and not interface_state.session_is_active(*session):
            # A late runtime callback may race durable closure. Preserve one
            # audit row for the event, but never attach an actionable alert to
            # a session that can no longer act.
            ended_at = session[2]
            con.execute(
                "UPDATE planner_alerts SET resolved_at=? "
                "WHERE session_id=? AND resolved_at IS NULL",
                (ended_at, session_id),
            )
            exists = con.execute(
                "SELECT 1 FROM planner_alerts "
                "WHERE session_id=? AND binding_id IS ? AND message_id IS ? "
                "AND batch_id IS ? AND sprint_doc_id IS ? AND reason=? "
                "LIMIT 1",
                (session_id, binding_id, message_id, batch_id, sprint_doc_id,
                 reason),
            ).fetchone()
            if exists is None:
                con.execute(
                    "INSERT INTO planner_alerts "
                    "(session_id, binding_id, message_id, batch_id, "
                    "sprint_doc_id, severity, reason, detail, dedupe_key, "
                    "resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (session_id, binding_id, message_id, batch_id,
                     sprint_doc_id, severity, reason, detail, dedupe,
                     ended_at),
                )
            return
    con.execute(
        "INSERT OR IGNORE INTO planner_alerts "
        "(session_id, binding_id, message_id, batch_id, sprint_doc_id, "
        "severity, reason, detail, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, binding_id, message_id, batch_id, sprint_doc_id,
         severity, reason, detail, dedupe))
    if detail is not None:
        # The row may already have been open (INSERT OR IGNORE did nothing):
        # refresh its measurement so a reader sees the LATEST failing gate,
        # not the one that happened to open the alert.
        con.execute(
            "UPDATE planner_alerts SET detail=? "
            "WHERE dedupe_key=? AND resolved_at IS NULL", (detail, dedupe))


def park_delivery_unknown(con, session_id: int, *,
                          reason: str = "crash_window_delivery_unknown",
                          severity: str = "critical") -> None:
    """Park input delivery as unknown — the crash-window stance (decision
    #22): composer unknown, delivery delivery_unknown, alert raised.
    pending_seq is KEPT as evidence; only reconcile_input() clears the park.
    There is no replay path."""
    interface_state.transition(con, "composer", session_id, "unknown")
    interface_state.transition(con, "delivery", session_id, "delivery_unknown")
    _alert(con, severity=severity, reason=reason, session_id=session_id)


def current_writer(con, session_id: int):
    return con.execute(
        "SELECT lease_id, client_id, next_input_seq FROM interface_writer_leases "
        "WHERE session_id=? AND revoked_at IS NULL",
        (session_id,),
    ).fetchone()


def acquire_writer(con, session_id: int, client_id: str, token: str,
                   takeover: bool = False) -> int:
    """Take the session's writer lease. With takeover=True an existing lease
    is atomically revoked first (the old client's frames are rejected from
    that commit on); without it, a held lease refuses.

    The new lease's expected sequence is reseeded from the SESSION's
    forwarded_seq+1, not reset to 1: duplicate detection is session-scoped,
    so a fresh lease (takeover/reconnect/post-park resend) must continue the
    session's sequence — reseeding to 1 would either gap-wedge the client's
    legitimate next frame or false-duplicate-ack new bytes (silent loss)."""
    sess = _session(con, session_id)
    if sess[3] != "occupied":
        raise BrokerError(f"session {session_id} is {sess[3]}, not occupied")
    istate = con.execute(
        "SELECT forwarded_seq FROM interface_input_state WHERE session_id=?",
        (session_id,)).fetchone()
    if istate is None:
        raise BrokerError(f"session {session_id} has no input state row")
    held = current_writer(con, session_id)
    if held is not None:
        if not takeover:
            raise BrokerError(
                f"session {session_id} writer held by {held[1]} — explicit "
                "takeover required")
        con.execute(
            "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
            "revoke_reason='takeover' WHERE lease_id=?",
            (held[0],),
        )
    cur = con.execute(
        "INSERT INTO interface_writer_leases "
        "(session_id, shell_id, generation, client_id, token_hash, "
        " next_input_seq, heartbeat_at) VALUES (?,?,?,?,?,?,datetime('now'))",
        (session_id, sess[1], sess[2], client_id,
         hashlib.sha256(token.encode()).hexdigest(), istate[0] + 1),
    )
    return cur.lastrowid


def accept_human_input(con, session_id: int, client_seq: int,
                       payload_len: int, writer) -> dict:
    """The ordered two-phase human-input path (spec #20 Input Broker 1–5).

    1. occupied session + current writer lease + monotonic sequence;
    2. bounded payload (length only — bytes stay client-side);
    3. COMMIT a metadata-only pending reservation + composer dirty;
    4. writer(payload_len) — the injected tmux write, exactly once;
    5. COMMIT forwarded, then the caller acks the client.

    An exact duplicate of a known-forwarded sequence returns its prior ack
    and forwards nothing; a gap rejects before any state change. While a
    wake batch holds the input lock (state 'submitting') every new frame is
    refused — later input is ordered after the indivisible submission. A
    crash between the commits leaves pending_seq for startup reconciliation;
    a writer() failure WITHOUT process death takes the same park immediately
    (delivery unknown, writer revoked, alert) since the bytes may have
    landed.

    The gate reads + the phase-1 commit are serialized under BEGIN IMMEDIATE
    (REV2 seq-4 L5): a wake submission committing its input lock on another
    connection cannot slip between this frame's lock check and its
    reservation — whichever commits first wins; the loser re-reads and
    refuses (lock held) or re-gates (frame pending).
    """
    began = _begin_immediate(con)
    try:
        if payload_len > MAX_INPUT_BYTES:
            raise BrokerError(
                f"payload {payload_len} > {MAX_INPUT_BYTES} bytes")
        sess = _session(con, session_id)
        if sess[3] != "occupied":
            raise BrokerError(
                f"session {session_id} is {sess[3]}, not occupied")
        lease = current_writer(con, session_id)
        if lease is None:
            raise BrokerError(f"session {session_id} has no writer")
        istate = con.execute(
            "SELECT composer, delivery, pending_seq, forwarded_seq "
            "FROM interface_input_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if istate is None:
            raise BrokerError(f"session {session_id} has no input state row")
        _, _, pending_seq, forwarded_seq = istate

        if client_seq <= forwarded_seq:
            # Known-forwarded duplicate: replay the ack, never the bytes.
            if began:
                con.rollback()
            return {"ack": client_seq, "duplicate": True}
        # The input lock: while a wake batch is submitting, its fixed prompt is
        # the indivisible input — a human frame is ordered AFTER it (spec #20
        # Retry Policy), never interleaved inside the submission.
        locked = con.execute(
            "SELECT 1 FROM planner_wake_batches "
            "WHERE shell_id=? AND generation=? AND state='submitting'",
            (sess[1], sess[2])).fetchone()
        if locked is not None:
            raise BrokerError(
                "a wake submission holds the input lock — this frame is "
                "ordered after it; retry once the wake is acknowledged")
        if pending_seq is not None:
            # One unacknowledged frame per writer — the client buffers locally.
            raise BrokerError(
                f"sequence {pending_seq} is pending — wait for its ack")
        if client_seq != lease[2]:
            raise BrokerError(
                f"sequence gap: expected {lease[2]}, got {client_seq} — "
                "rejected, no bytes forwarded")

        # Phase 1 (commit): reserve the sequence, dirty the composer FIRST.
        interface_state.transition(
            con, "composer", session_id, "dirty",
            extra_sets={"pending_seq": client_seq,
                        "pending_reserved_at": _now(con),
                        "last_human_input_at": _now(con)})
        con.commit()
        began = False
    except Exception:
        if began:
            con.rollback()
        raise

    # Phase 2: forward the exact bytes once. A crash here is the window.
    try:
        writer(payload_len)
    except Exception:
        # The write failed WITHOUT process death (e.g. tmux error): the frame
        # may or may not have landed — exactly the crash-window ambiguity, so
        # take the same stance live: park delivery unknown, revoke the writer,
        # alert, keep pending_seq as evidence, never replay. reconcile_input()
        # is the only way out.
        park_delivery_unknown(con, session_id)
        con.execute(
            "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
            "revoke_reason='write_failure' WHERE lease_id=?",
            (lease[0],))
        con.commit()
        raise

    # Phase 3 (commit): mark forwarded, clear the reservation, bump the lease.
    con.execute(
        "UPDATE interface_input_state SET forwarded_seq=?, pending_seq=NULL, "
        "pending_reserved_at=NULL, updated_at=datetime('now') "
        "WHERE session_id=?",
        (client_seq, session_id))
    con.execute(
        "UPDATE interface_writer_leases SET next_input_seq=? WHERE lease_id=?",
        (client_seq + 1, lease[0]))
    con.commit()
    return {"ack": client_seq, "duplicate": False}


def certify_clean(con, session_id: int, client_id: str, client_seq: int) -> None:
    """Writer certification of an empty composer — the only non-hook path
    dirty|unknown → clean. Records the certifying writer and sequence."""
    interface_state.transition(
        con, "composer", session_id, "clean",
        extra_sets={"certified_by": client_id, "certified_seq": client_seq,
                    "certified_at": _now(con)})


def set_browser_composer(con, session_id: int, client_id: str,
                         state: str) -> None:
    """Set the current writer's metadata-only browser draft state.

    Browser draft bytes never cross this boundary. The separate column is
    deliberate: clearing a browser textarea must not certify the harness/tmux
    composer clean. BEGIN IMMEDIATE serializes this state with the planner wake
    gate so whichever action commits first is observed by the other.

    It rides the writer lease in both directions: only the current writer may
    set it, and a restart that revokes every lease resets it to clean
    (H-9, `interface_reconcile.startup_reconcile`). A dirty draft whose client
    is provably gone is unownable by construction — nobody can certify it, and
    it would block the wake gate with no alert and no owner.
    """
    if state not in ("clean", "dirty"):
        raise BrokerError(f"invalid browser composer state {state!r}")
    began = _begin_immediate(con)
    try:
        sess = _session(con, session_id)
        if sess[3] != "occupied":
            raise BrokerError(
                f"session {session_id} is {sess[3]}, not occupied")
        lease = current_writer(con, session_id)
        if lease is None or lease[1] != client_id:
            raise BrokerError(
                "browser composer state rides the current writer lease")
        row = con.execute(
            "SELECT browser_composer FROM interface_input_state "
            "WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise BrokerError(f"session {session_id} has no input state row")
        if row[0] != state:
            con.execute(
                "UPDATE interface_input_state SET browser_composer=?, "
                "updated_at=datetime('now') WHERE session_id=?",
                (state, session_id))
        con.commit()
    except Exception:
        if began and con.in_transaction:
            con.rollback()
        raise


def reconcile_input(con, session_id: int, outcome: str) -> None:
    """Explicit operator reconciliation of a delivery_unknown park.

    outcome='delivered' — operator confirmed the frame reached the pane: the
    pending sequence is folded into forwarded_seq (never re-sent).
    outcome='not_delivered' — operator confirmed it never landed: the
    reservation is dropped; the client resends from its own buffer.
    Either way delivery returns to normal; composer goes to unknown→certify
    or stays as reconciled evidence demands (certification is a separate,
    deliberate act)."""
    if outcome not in ("delivered", "not_delivered"):
        raise BrokerError(f"unknown reconcile outcome {outcome!r}")
    row = con.execute(
        "SELECT pending_seq, forwarded_seq, delivery "
        "FROM interface_input_state WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise BrokerError(f"session {session_id} has no input state row")
    pending_seq, forwarded_seq, delivery = row
    if delivery != "delivery_unknown":
        raise BrokerError(
            f"session {session_id} delivery is {delivery}, not delivery_unknown")
    new_forwarded = forwarded_seq
    if outcome == "delivered" and pending_seq is not None:
        new_forwarded = max(forwarded_seq, pending_seq)
    interface_state.transition(
        con, "delivery", session_id, "normal",
        extra_sets={"pending_seq": None, "pending_reserved_at": None,
                    "forwarded_seq": new_forwarded})


def _close_volatile_children(con, session_id: int) -> None:
    """Terminalize/remove generation-volatile state in the caller's txn.

    A delivery-unknown input row is no longer live, but it remains the
    metadata-only ambiguity record required by decision #16.  Preserve that
    park and its operator alert; remove only input state with no pending or
    ambiguous delivery evidence.
    """
    con.execute(
        "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
        "revoke_reason='session_end' "
        "WHERE session_id=? AND revoked_at IS NULL", (session_id,))
    input_state = con.execute(
        "SELECT pending_seq, delivery FROM interface_input_state "
        "WHERE session_id=?", (session_id,)).fetchone()
    if input_state is None:
        return
    pending_seq, delivery = input_state
    if pending_seq is None and delivery != "delivery_unknown":
        con.execute(
            "DELETE FROM interface_input_state WHERE session_id=?",
            (session_id,))
        return
    park_delivery_unknown(con, session_id)


def close_session(con, session_id: int, end_reason: str) -> dict:
    """THE one closure helper (spec #30 Lifecycle Contract) — every close
    producer (operator terminate, cancel start, reconcile-close, spawn
    failure, provider session_end) converges through here instead of
    composing lifecycle/occupancy moves independently.

    One transaction boundary (the caller's connection): records end
    reason/time, terminalizes occupancy AND lifecycle (walking through
    `stopping` where no direct edge exists — a hook that won the race can
    never strand occupied/ended, and nothing ever moves terminal →
    nonterminal), ends the matching generation, revokes active leases,
    removes ordinary volatile input state, preserves a metadata-only
    delivery-unknown park, and resolves or parks durable session-scoped wake
    state by the existing ambiguity rules (a batch with a proven stop
    reconciles from read state; a batch with no stop evidence parks — no live
    harness will re-drive it). Queued wake work is deliberately left queued
    for a future generation.

    Idempotent: a FULLY terminal session (occupancy AND lifecycle ended)
    returns its original terminal result without state churn. A partially
    closed legacy row — a pre-convergence closure that ended occupancy but
    left lifecycle nonterminal — is converged, never silently no-op'ed
    (SC-065); its original end reason/time are kept."""
    row = con.execute(
        "SELECT shell_id, generation, occupancy, lifecycle, ended_at, end_reason "
        "FROM interface_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise BrokerError(f"interface session {session_id} not found")
    shell_id, generation, occupancy, lifecycle, ended_at, prior_reason = row
    if not interface_state.session_is_active(occupancy, lifecycle, ended_at):
        _close_volatile_children(con, session_id)
        con.execute(
            "UPDATE planner_alerts SET resolved_at=datetime('now') "
            "WHERE session_id=? AND resolved_at IS NULL", (session_id,))
        return {"session_id": session_id, "already_ended": True,
                "end_reason": prior_reason}

    if occupancy != "ended":
        interface_state.transition(
            con, "occupancy", session_id, "ended",
            extra_sets={"ended_at": _now(con), "end_reason": end_reason})
        recorded_reason = end_reason
    else:
        # Legacy partial row: occupancy already ended — re-stamping the
        # reason/time would falsify the original terminal record.
        recorded_reason = prior_reason
    if lifecycle != "ended":
        if lifecycle in ("idle", "busy", "approval", "user_input"):
            # No direct edge to ended — converge through stopping (the
            # only nonterminal staging state every live state can reach).
            interface_state.transition(con, "lifecycle", session_id,
                                       "stopping")
        interface_state.transition(con, "lifecycle", session_id, "ended")
    con.execute(
        "UPDATE interface_generations SET ended_at=datetime('now') "
        "WHERE shell_id=? AND generation=? AND ended_at IS NULL",
        (shell_id, generation))
    # Ordinary input/composer state is generation-volatile.  A pending or
    # delivery-unknown row is different: it is the decision #16 ambiguity
    # record, so park it durably while the live guard ignores its terminal
    # parent.  Everything else is removed in this same transaction (#529).
    _close_volatile_children(con, session_id)

    # Session-scoped wake state: the generation is provably over, so a live
    # batch can never see its stop hook. Resolve from durable evidence or park
    # the durable batch audit; queued work remains for a future generation.
    batches = con.execute(
        "SELECT batch_id, binding_id, state, stop_hook_seq "
        "FROM planner_wake_batches "
        "WHERE shell_id=? AND generation=? AND state IN ('submitting','running')",
        (shell_id, generation)).fetchall()
    for batch_id, binding_id, _state, stop_seq in batches:
        if stop_seq is not None:
            _complete_batch(con, batch_id, stop_seq)
        else:
            interface_state.transition(con, "wake_batch", batch_id,
                                       "delivery_unknown")
            _alert(con, severity="critical",
                   reason="wake_batch_delivery_unknown", binding_id=binding_id)
    # Every alert tied directly to this session is generation-scoped. Once the
    # generation is durably closed it is no longer current, including an input
    # ambiguity raised while converging closure; keep the row as resolved audit.
    con.execute(
        "UPDATE planner_alerts SET resolved_at=datetime('now') "
        "WHERE session_id=? AND resolved_at IS NULL", (session_id,))
    return {"session_id": session_id, "already_ended": False,
            "end_reason": recorded_reason}


def record_hook(con, shell_id: int, generation: int, hook_seq: int,
                event: str, source: str = "provider",
                hooks_installed: bool = False) -> dict:
    """Record one authenticated harness hook with its durable sequence.

    Rejects replays (hook_seq <= last_hook_seq), stale generations, and
    unknown events. The sequence is the crash-window evidence: a batch's
    submit/stop hook seqs are stamped here, and startup reconciliation
    trusts only these durable stamps — never the broker's memory of what
    it sent.

    `source` distinguishes the entrypoint's pre-exec identity claim from a
    provider-native hook delivered by the emitter. Only the provider's
    session_start is real PROVIDER readiness (sprint 25 seq 7) — the
    entrypoint's claim proves the process is up and nothing more, and it is
    stamped into its own column. On a 'first_turn_gated' harness that weaker
    proof is nonetheless what moves starting → idle, because the provider hook
    never arrives unbidden (flag #303, decisions #98/#99).

    `hooks_installed` is the entrypoint's report of whether this session's
    hook config actually landed on disk (interface_hooks.install). It exists
    because `capability()` is a STATIC table lookup — it says what the harness
    version CAN deliver, never what this launch installed — so on the promoting
    path it is not enough: a codex seat whose `.codex/hooks.json` is
    unparseable installs nothing, yet its capability still reads
    mandatory_ok=True. Promoting on the claim alone would arm a seat with ZERO
    lifecycle hooks — no prompt_submit fence, no turn_stop — so the promotion
    takes this operand and defaults to False (absent proof is not proof).
    """
    if event not in interface_hooks.EVENTS:
        raise BrokerError(f"unknown hook event {event!r} — rejected")
    if source not in interface_hooks.SOURCES:
        raise BrokerError(f"unknown hook source {source!r} — rejected")
    gen = con.execute(
        "SELECT last_hook_seq, ended_at FROM interface_generations "
        "WHERE shell_id=? AND generation=?",
        (shell_id, generation),
    ).fetchone()
    if gen is None:
        raise BrokerError(f"unknown generation {shell_id}/{generation}")
    if gen[1] is not None:
        if event == "session_end":
            # A provider hook may ACKNOWLEDGE an already-ended generation
            # (its own end, or a close that won the race) without reopening
            # it — a clean 200, never a rejection loop the emitter retries.
            return {"hook_seq": hook_seq, "event": event,
                    "acknowledged": True, "already_ended": True}
        raise BrokerError(f"generation {shell_id}/{generation} has ended")
    if hook_seq <= gen[0]:
        raise BrokerError(
            f"stale hook sequence {hook_seq} (last {gen[0]}) — rejected")
    con.execute(
        "UPDATE interface_generations SET last_hook_seq=? "
        "WHERE shell_id=? AND generation=?",
        (hook_seq, shell_id, generation))

    sess = con.execute(
        "SELECT session_id, lifecycle, harness, cli_version "
        "FROM interface_sessions "
        "WHERE shell_id=? AND generation=? AND occupancy <> 'ended'",
        (shell_id, generation),
    ).fetchone()
    result = {"hook_seq": hook_seq, "event": event}

    if event == "session_start":
        if source == "provider":
            # Real provider readiness (seq 7): the harness's own start hook,
            # not the entrypoint's identity claim. starting → idle; composer
            # unknown → clean ONLY while zero human input has been accepted
            # (spec: clean requires the ready callback AND no accepted human
            # sequence). Readiness arriving after human input leaves the
            # composer as it is — dirty/unknown still need submit/certify.
            if sess[1] == "starting":
                interface_state.transition(con, "lifecycle", sess[0], "idle")
            # REAL provider readiness (flag #49, decisions #28/#31): stamp
            # the quiet baseline NOW — never at the pre-exec occupied_at —
            # so a >3s harness boot cannot let a queued wake submit into an
            # unpainted TUI. The wake gate measures quiet from max(human
            # input, provider_ready_at, ...), so this stamp resets the
            # debounce to the moment the provider actually proved alive.
            con.execute(
                "UPDATE interface_sessions SET provider_ready_at=datetime('now') "
                "WHERE session_id=?", (sess[0],))
            # H-27: the declaration is no longer contradicted — the hook it
            # promised has arrived. Only the readiness row (batch_id IS NULL);
            # a batch's submit silence is its own condition and its own row.
            con.execute(
                "UPDATE planner_alerts SET resolved_at=datetime('now') "
                "WHERE session_id=? AND reason='hooks_declared_but_silent' "
                "AND batch_id IS NULL AND resolved_at IS NULL", (sess[0],))
            istate = con.execute(
                "SELECT composer, pending_seq, forwarded_seq "
                "FROM interface_input_state WHERE session_id=?",
                (sess[0],)).fetchone()
            if istate is not None and istate[1] is None and istate[2] == 0:
                interface_state.transition(con, "composer", sess[0], "clean")
            _hook_capability_alerts(con, sess[0])
        else:
            # source='entrypoint': the pre-exec identity claim (the route owns
            # the reserved → occupied move). It proves the PANE IS LIVE and
            # interface_exec is about to exec the harness — the PROCESS is up.
            # It is NOT provider readiness, so it is stamped into its own
            # column and never into provider_ready_at, which keeps meaning
            # "the provider handshaked" for every reader (flag #303
            # condition 1). The stamp is unconditional because it is true of
            # every claim; only the PROMOTION below takes further operands.
            con.execute(
                "UPDATE interface_sessions SET process_ready_at=datetime('now') "
                "WHERE session_id=?", (sess[0],))
            cap = interface_hooks.capability(sess[2], sess[3])
            if cap["readiness"] == interface_hooks.FIRST_TURN_GATED \
                    and hooks_installed:
                # This harness's own session_start cannot arrive until a human
                # submits the first turn (codex 0.145.0 — measured), so waiting
                # for it deadlocks the very seat that exists to BE woken: no
                # readiness → lifecycle stays 'starting' → the wake gate never
                # sees occupied+idle → the submit that would have triggered the
                # hook never goes out. Promote on the weak proof instead; the
                # first real turn still fires session_start and still upgrades
                # the record to true provider readiness above.
                #
                # `hooks_installed` is the second operand and it is what keeps
                # this fail-CLOSED: the promotion is only sound while the seat
                # really has the lifecycle hooks its capability advertises. If
                # the install failed, nothing here fires and the seat stays
                # 'starting' — exactly the pre-fix disposition for a seat that
                # can deliver no hooks at all.
                if sess[1] == "starting":
                    interface_state.transition(
                        con, "lifecycle", sess[0], "idle")
                istate = con.execute(
                    "SELECT composer, pending_seq, forwarded_seq "
                    "FROM interface_input_state WHERE session_id=?",
                    (sess[0],)).fetchone()
                if istate is not None and istate[1] is None \
                        and istate[2] == 0:
                    interface_state.transition(
                        con, "composer", sess[0], "clean")
                _hook_capability_alerts(con, sess[0])
    elif event == "prompt_submit":
        # Fenced submit callback. A prompt_submit hook clears dirty -> clean
        # and promotes a submitting wake batch ONLY when it provably answers
        # that batch's submission: no human input sequence may have been
        # accepted after the batch's input_seq_fence (spec: "clean only if no
        # later human input sequence was accepted"). Without the fence a
        # human's own Enter would manufacture the durable hook evidence
        # decision #22 recovery trusts. A fence violation parks the batch as
        # delivery_unknown — the wake may or may not have been consumed.
        composer, forwarded_seq = con.execute(
            "SELECT composer, forwarded_seq FROM interface_input_state "
            "WHERE session_id=?", (sess[0],)).fetchone()
        batch = con.execute(
            "SELECT batch_id, binding_id, input_seq_fence "
            "FROM planner_wake_batches "
            "WHERE shell_id=? AND generation=? AND state='submitting'",
            (shell_id, generation)).fetchone()
        fenced = batch is None or (
            batch[2] is not None and forwarded_seq < batch[2])
        if not fenced:
            interface_state.transition(con, "wake_batch", batch[0],
                                       "delivery_unknown")
            _alert(con, severity="critical",
                   reason="wake_batch_delivery_unknown", binding_id=batch[1])
            result["wake_batch_delivery_unknown"] = batch[0]
        else:
            # 'unknown' is never cleared by a hook — only exact recovery plus
            # certification clears an ambiguity (spec #20 Composer).
            if composer in ("clean", "dirty"):
                interface_state.transition(
                    con, "composer", sess[0], "clean",
                    extra_sets={"last_submit_seq": forwarded_seq})
            if batch is not None:
                interface_state.transition(
                    con, "wake_batch", batch[0], "running",
                    extra_sets={"submit_hook_seq": hook_seq,
                                "submitted_at": _now(con)})
                # H-27: the submit hook answered, so this batch's silence is
                # over — including an alert opened while it was pending.
                resolve_batch_alerts(con, batch[0])
                con.execute(
                    "UPDATE planner_wake_items SET state='running' "
                    "WHERE batch_id=? AND state='submitting'",
                    (batch[0],))
                result["wake_batch_running"] = batch[0]
        interface_state.transition(con, "lifecycle", sess[0], "busy")
    elif event == "turn_stop":
        if _turn_finished(con, sess, shell_id, generation, hook_seq):
            result["wake_batch_complete"] = True
        # A provider turn_stop is the later successful turn evidence. Interrupt
        # and failure also finish a turn, but must not clear an earlier failure.
        con.execute(
            "UPDATE planner_alerts SET resolved_at=datetime('now') "
            "WHERE session_id=? AND reason='turn_failure' "
            "AND resolved_at IS NULL", (sess[0],))
    elif event == "session_end":
        # The chat is provably over: converge FULL durable closure through
        # the one helper — occupancy AND lifecycle terminal, generation
        # ended, leases revoked, wake state resolved/parked. Ending only
        # the lifecycle here stranded occupied/ended sessions that no
        # route could converge (#532).
        close_session(con, sess[0], "provider_session_end")
    elif event == "approval_wait":
        # Optional (kimi PermissionRequest): busy → approval + alert. A
        # harness without this event simply stays busy — safe (spec).
        if sess[1] == "busy":
            interface_state.transition(con, "lifecycle", sess[0], "approval")
            _alert(con, severity="warning", reason="approval_wait",
                   session_id=sess[0])
    elif event == "approval_result":
        if sess[1] == "approval":
            interface_state.transition(con, "lifecycle", sess[0], "busy")
            con.execute(
                "UPDATE planner_alerts SET resolved_at=datetime('now') "
                "WHERE session_id=? AND reason='approval_wait' "
                "AND resolved_at IS NULL", (sess[0],))
    elif event == "user_input_wait":
        if sess[1] == "busy":
            interface_state.transition(
                con, "lifecycle", sess[0], "user_input")
            _alert(con, severity="warning", reason="user_input_wait",
                   session_id=sess[0])
    elif event in ("interrupt", "failure"):
        # The turn is over (user cancel / provider error). kimi's Stop does
        # not fire on interrupt and claude's Stop does not fire on API
        # error, so these events ARE that harness's turn-stop: preserve
        # every queue, record the explicit terminal state, and reconcile a
        # running batch exactly like turn_stop (spec Harness Hooks).
        if _turn_finished(con, sess, shell_id, generation, hook_seq):
            result["wake_batch_complete"] = True
        if event == "failure":
            _alert(con, severity="warning", reason="turn_failure",
                   session_id=sess[0])
        result["turn_terminal"] = event
    con.commit()
    return result


def _turn_finished(con, sess, shell_id: int, generation: int,
                   stop_hook_seq: int) -> bool:
    """turn_stop / interrupt / failure: the model turn ended. Lifecycle
    walks back to idle (through busy from approval/user_input — Stop may
    arrive while a wait state is up), and a running wake batch reconciles
    from durable read state. Returns True when a batch was completed."""
    if sess[1] in ("approval", "user_input"):
        interface_state.transition(con, "lifecycle", sess[0], "busy")
        interface_state.transition(con, "lifecycle", sess[0], "idle")
    else:
        interface_state.transition(con, "lifecycle", sess[0], "idle")
    batch = con.execute(
        "SELECT batch_id FROM planner_wake_batches "
        "WHERE shell_id=? AND generation=? AND state='running'",
        (shell_id, generation)).fetchone()
    if batch is not None:
        _complete_batch(con, batch[0], stop_hook_seq)
        return True
    return False


def _hook_capability_alerts(con, session_id: int) -> None:
    """Spec Harness Hooks: a harness lacking distinct approval/user-input
    hooks stays busy during those waits (safe) and Interface REPORTS the
    degradation; a mandatory-hook gap blocks sprint-wake arming — never
    the ordinary chat. Evaluated once per generation at provider
    readiness; alerts dedupe while open."""
    row = con.execute(
        "SELECT harness, cli_version FROM interface_sessions "
        "WHERE session_id=?", (session_id,)).fetchone()
    cap = interface_hooks.capability(row[0] if row else None,
                                     row[1] if row else None)
    if not cap["mandatory_ok"]:
        _alert(con, severity="warning", reason="wake_not_armable",
               session_id=session_id)
    elif cap["degraded"]:
        _alert(con, severity="info", reason="hooks_degraded",
               session_id=session_id)


def _complete_batch(con, batch_id: int, stop_hook_seq: int) -> None:
    """Reconcile a running batch's items from durable message read state
    (spec #20 Wake Delivery): read → done; unread with a durable ambiguous
    action (an action receipt still intent/unknown) → reconcile; unread
    without ambiguity → back to queued with completed_wakes+1 — except the
    third completed wake, which QUARANTINES the item and alerts (newer work
    is never blocked). A queued item whose message was read during the turn
    completes without a wake of its own ("new message handled in the turn:
    complete it"). Infrastructure never marks messages read."""
    interface_state.transition(
        con, "wake_batch", batch_id, "complete",
        extra_sets={"stop_hook_seq": stop_hook_seq, "completed_at": _now(con)})
    binding_id = con.execute(
        "SELECT binding_id FROM planner_wake_batches WHERE batch_id=?",
        (batch_id,)).fetchone()[0]
    items = con.execute(
        "SELECT item_id, message_id FROM planner_wake_items "
        "WHERE batch_id=? AND state IN ('batched','submitting','running')",
        (batch_id,)).fetchall()
    for item_id, message_id in items:
        _reconcile_item(con, item_id, message_id, binding_id)
    # Messages that arrived DURING the turn and were read in it complete
    # without riding a batch (spec: "new message handled in the turn:
    # complete it; otherwise leave it queued").
    strays = con.execute(
        "SELECT i.item_id, i.message_id FROM planner_wake_items i "
        "JOIN shell_messages m ON m.message_id=i.message_id "
        "WHERE i.binding_id=? AND i.state='queued' AND m.read_at IS NOT NULL",
        (binding_id,)).fetchall()
    for item_id, _ in strays:
        interface_state.transition(
            con, "wake_item", item_id, "done",
            extra_sets={"done_at": _now(con)})


def _reconcile_item(con, item_id: int, message_id: int,
                    binding_id: int) -> None:
    """One batched item's stop-hook reconciliation (see _complete_batch)."""
    read = con.execute(
        "SELECT read_at FROM shell_messages WHERE message_id=?",
        (message_id,)).fetchone()[0]
    if read is not None:
        interface_state.transition(
            con, "wake_item", item_id, "done",
            extra_sets={"done_at": _now(con)})
        return
    ambiguous = con.execute(
        "SELECT receipt_id, state FROM planner_action_receipts "
        "WHERE message_id=? AND state IN ('intent','unknown')",
        (message_id,)).fetchone()
    if ambiguous is not None:
        # A durable ambiguous action: the planner started a side effect whose
        # result was never observed. Park for operator reconciliation —
        # NEVER requeue blind (spec Wake Delivery; decision #12).
        interface_state.transition(
            con, "wake_item", item_id, "reconcile",
            extra_sets={"ambiguity":
                        f"action receipt {ambiguous[0]} is {ambiguous[1]}"})
        _alert(con, severity="warning", reason="wake_item_reconcile",
               binding_id=binding_id, message_id=message_id)
        return
    wakes = con.execute(
        "SELECT completed_wakes FROM planner_wake_items WHERE item_id=?",
        (item_id,)).fetchone()[0] + 1
    con.execute(
        "UPDATE planner_wake_items SET completed_wakes=?, batch_id=NULL "
        "WHERE item_id=?", (wakes, item_id))
    if wakes >= MAX_COMPLETED_WAKES:
        # Three completed wakes left it unread: quarantine + alert; newer
        # work continues past it (spec Wake Delivery; decision #12).
        interface_state.transition(con, "wake_item", item_id, "quarantined")
        _alert(con, severity="warning", reason="wake_item_quarantined",
               binding_id=binding_id, message_id=message_id)
    else:
        interface_state.transition(con, "wake_item", item_id, "queued")


def note_gate_failure(con, batch_id: int, reason: str) -> None:
    """Record the gate reason that just refused this batch, verbatim (H-26).

    Written on EVERY failed attempt, including the ones long before the stall
    threshold, so that when the alert finally opens it names the gate that has
    actually been failing — not whichever one happened to fail last. Those
    differ exactly when a seat is flapping, which is the case a reader most
    needs the truth about.

    Its own transaction: the caller has just rolled the gate read back, and
    this measurement must outlive that rollback."""
    con.execute(
        "UPDATE planner_wake_batches "
        "SET last_gate_reason=?, last_gate_at=datetime('now') "
        "WHERE batch_id=?", (reason, batch_id))
    con.commit()


def stalled_batch_alert(con, batch_id: int, threshold_s=None) -> bool:
    """Open the deduped `wake_batch_stalled` alert if this batch has been
    queued past the threshold under a still-armed binding (H-26). Returns
    True when the batch qualifies as stalled.

    threshold_s resolves at CALL time, not in the signature: a default of
    `WAKE_BATCH_STALL_S` would bind the constant once at import and leave
    the module attribute a decoy that reads correctly and changes nothing.

    Carries the last failing gate verbatim in `detail`. States what was
    measured and nothing more — decision #76 forbids a monitor that reports a
    verdict, and "which gate said no" is exactly the fact the operator cannot
    otherwise obtain."""
    if threshold_s is None:
        threshold_s = WAKE_BATCH_STALL_S
    row = con.execute(
        "SELECT b.binding_id, b.state, b.last_gate_reason, "
        "       (julianday('now') - julianday(b.created_at)) * 86400.0, "
        "       p.released_at, p.sprint_doc_id "
        "FROM planner_wake_batches b "
        "JOIN sprint_planner_bindings p ON p.binding_id = b.binding_id "
        "WHERE b.batch_id=?", (batch_id,)).fetchone()
    if row is None:
        return False
    binding_id, state, gate_reason, queued_s, released_at, doc_id = row
    if state != "queued" or released_at is not None:
        return False
    if queued_s is None or queued_s < threshold_s:
        return False
    _alert(con, severity="warning", reason="wake_batch_stalled",
           binding_id=binding_id, batch_id=batch_id, sprint_doc_id=doc_id,
           detail=(gate_reason or "no gate attempt recorded"))
    return True


def _silence_detail(harness, cli_version, unobserved: str,
                    measured: str) -> str:
    """The one sentence a `hooks_declared_but_silent` alert carries: which
    harness at which version declared which event, and what was actually
    seen. States the measurement and stops there — decision #76 forbids a
    monitor that reports a verdict, and "which declared event never arrived"
    is precisely the fact no other surface holds."""
    return (f"harness={harness!r} cli_version={cli_version!r} "
            f"declares {unobserved!r} — {measured}")


def hooks_silence_alert(con, binding_id: int, ready_s=None,
                        submit_s=None) -> "float | None":
    """Check a declared hook chain against what has ACTUALLY arrived, for the
    session under one armed binding (H-27). Opens a deduped
    `hooks_declared_but_silent` naming the harness, its cli_version, and the
    declared event that was never observed.

    Returns the seconds until the nearest threshold falls due while one is
    still pending, so the caller can arm a single bounded re-check; None when
    nothing is pending. Thresholds resolve at CALL time, never in the
    signature — a default argument would bind the constant at import and
    leave the module attribute a decoy that reads correctly and changes
    nothing (the same trap H-26's stall threshold sprang).

    TWO MEASUREMENTS, AND THE ONE THIS DELIBERATELY DOES NOT MAKE.

    1. Readiness. A harness that declares session_start arrives at startup
       (claude `startup_hook`, kimi `session_created`) and has not stamped
       `provider_ready_at` is contradicting its own declaration. NOT
       evaluated for a `first_turn_gated` harness: there readiness is gated
       on a human submitting the first turn, so the delay is unbounded and a
       healthy idle codex seat waiting to be woken is indistinguishable by
       elapsed time from a broken one. Alerting on it would fire on every
       correct codex seat — the monitor lying that decision #76 forbids and
       this sprint exists to remove (decisions #98/#99, U7 finding F8).

    2. Submit silence. The engine itself wrote the bytes and pressed Enter,
       so a batch that entered `submitting` and stayed there is a submit hook
       that did not fire — no model latency sits inside that wait, and no
       healthy shape produces it. This is where U7's F13 measurement lands on
       the post-U7 floor: a live, trusted, hooks-installed seat is now
       promoted on the weak process_ready_at proof and the wake goes out, so
       a dead chain no longer parks in `starting` where the arming gate could
       see it — it parks HERE, where `_drain_sync` returns early and nothing
       in flight ever looks again.

    NOT MEASURED: H-27's literal "a completed human turn produced no
    turn_stop". In band, the only evidence that a turn completed IS
    turn_stop. Every independent route fires on a healthy shape — a later
    prompt_submit arriving while a batch runs is type-ahead exactly as much
    as it is a silent stop (both land on lifecycle `busy`, and nothing in the
    row tells them apart), and "the triggering rows were all read" is the
    ordinary read-then-work-for-twenty-minutes planner turn. A session that
    ends mid-batch is already covered: close_session parks it
    delivery_unknown with a critical alert. The failure that clause aimed at
    is caught strictly earlier and without confound by measurement 2 — a
    chain that will not deliver turn_stop did not deliver prompt_submit
    either.

    Commits its own measurement. The coordinator calls this BEFORE any of the
    drain's own writes and then returns down several paths that never reach a
    commit — an observation that survives only one of them is not an
    observation.
    """
    if ready_s is None:
        ready_s = HOOKS_READY_SILENT_S
    if submit_s is None:
        submit_s = HOOKS_SUBMIT_SILENT_S
    sess = con.execute(
        "SELECT s.session_id, s.harness, s.cli_version, s.provider_ready_at, "
        "       (julianday('now') - julianday(s.created_at)) * 86400.0, "
        "       b.sprint_doc_id "
        "FROM sprint_planner_bindings b "
        "JOIN interface_sessions s ON s.session_id = b.session_id "
        "WHERE b.binding_id=? AND b.released_at IS NULL "
        "AND s.occupancy <> 'ended'", (binding_id,)).fetchone()
    if sess is None:
        return None  # H-27 scopes to an ARMED binding on a live session
    session_id, harness, cli_version, ready_at, age_s, doc_id = sess
    pending = []

    readiness = interface_hooks.capability(harness, cli_version)["readiness"]
    if ready_at is None and readiness != interface_hooks.FIRST_TURN_GATED:
        if age_s is not None and age_s >= ready_s:
            _alert(con, severity="warning",
                   reason="hooks_declared_but_silent", session_id=session_id,
                   sprint_doc_id=doc_id,
                   detail=_silence_detail(
                       harness, cli_version, "session_start",
                       f"unobserved {age_s:.0f}s after session creation"))
        elif age_s is not None:
            pending.append(ready_s - age_s)

    batch = con.execute(
        "SELECT batch_id, "
        "       (julianday('now') - julianday(submitting_at)) * 86400.0 "
        "FROM planner_wake_batches "
        "WHERE binding_id=? AND state='submitting'", (binding_id,)).fetchone()
    # A NULL submitting_at is a batch that predates migration 0117, not a
    # batch submitted zero seconds ago: unmeasured, never reported as silent.
    if batch is not None and batch[1] is not None:
        if batch[1] >= submit_s:
            _alert(con, severity="warning",
                   reason="hooks_declared_but_silent", session_id=session_id,
                   batch_id=batch[0], sprint_doc_id=doc_id,
                   detail=_silence_detail(
                       harness, cli_version, "prompt_submit",
                       f"batch {batch[0]} submitted {batch[1]:.0f}s ago and "
                       f"the submit hook has not answered"))
        else:
            pending.append(submit_s - batch[1])

    con.commit()
    return min(pending) if pending else None


def resolve_batch_alerts(con, batch_id: int) -> None:
    """A batch that submitted or cancelled is no longer stalled (H-26).

    Keyed on the batch, so this resolves that batch's rows and nothing else's
    — a second binding stalled on the same seat keeps its own alert open."""
    con.execute(
        "UPDATE planner_alerts SET resolved_at=datetime('now') "
        "WHERE batch_id=? AND resolved_at IS NULL", (batch_id,))


def sweep_read_queued(con, binding_id: int) -> int:
    """Complete a binding's queued items whose message is ALREADY READ (H-4).

    A planner that drained its inbox by hand — or was woken first by the
    harness's own task notification — has already handled those rows; a wake
    turn for them delivers nothing and trains the planner to dismiss wake
    prompts. They complete as `done` with no batch, which is the disposition
    `_complete_batch` already gives a row read during another batch's turn.
    Returns how many were swept, for the caller's batch record."""
    items = con.execute(
        "SELECT i.item_id FROM planner_wake_items i "
        "JOIN shell_messages m ON m.message_id = i.message_id "
        "WHERE i.binding_id=? AND i.state='queued' AND i.batch_id IS NULL "
        "AND m.read_at IS NOT NULL "
        "ORDER BY i.item_id",
        (binding_id,)).fetchall()
    for (item_id,) in items:
        interface_state.transition(
            con, "wake_item", item_id, "done",
            extra_sets={"done_at": _now(con)})
    return len(items)


def form_batch(con, binding_id: int, skipped_read: int = 0) -> int:
    """Coalesce a binding's currently queued UNREAD items into one batch (the
    fixed-prompt submission unit). The partial unique index backstops the
    one-live-batch invariant; items join oldest first.

    An item whose message was read between queueing and here never joins the
    batch (H-4) — `sweep_read_queued` completes it instead. The join is the
    load-bearing half of that rule: the sweep can lose a race with a planner
    reading a row, this cannot.

    `skipped_read` records how many rows the sweep retired on the way to this
    batch (H-28). Suppression must itself be observable — this spec's own
    monitor rule — or a queue that went quiet because rows were correctly
    skipped reads identically to one that went quiet because nothing
    arrived."""
    binding = con.execute(
        "SELECT shell_id, generation FROM sprint_planner_bindings "
        "WHERE binding_id=? AND released_at IS NULL",
        (binding_id,)).fetchone()
    if binding is None:
        raise BrokerError(f"binding {binding_id} not found or released")
    cur = con.execute(
        "INSERT INTO planner_wake_batches "
        "(binding_id, shell_id, generation, skipped_read) VALUES (?,?,?,?)",
        (binding_id, binding[0], binding[1], skipped_read))
    batch_id = cur.lastrowid
    items = con.execute(
        "SELECT i.item_id FROM planner_wake_items i "
        "JOIN shell_messages m ON m.message_id = i.message_id "
        "WHERE i.binding_id=? AND i.state='queued' AND i.batch_id IS NULL "
        "AND m.read_at IS NULL "
        "ORDER BY i.item_id",
        (binding_id,)).fetchall()
    for (item_id,) in items:
        interface_state.transition(
            con, "wake_item", item_id, "batched",
            extra_sets={"batch_id": batch_id})
    return batch_id


def resolve_batch(con, batch_id: int) -> None:
    """Operator resolution of a delivery_unknown batch: the batch closes as
    audit (delivery_unknown → complete) and its still-in-flight items return
    to queued — never blindly resubmitted inside the parked batch; a NEW
    batch forms only after the input park itself is reconciled."""
    batch = con.execute(
        "SELECT state FROM planner_wake_batches WHERE batch_id=?",
        (batch_id,)).fetchone()
    if batch is None:
        raise BrokerError(f"wake batch {batch_id} not found")
    if batch[0] != "delivery_unknown":
        raise BrokerError(f"wake batch {batch_id} is {batch[0]}, "
                          "not delivery_unknown")
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE batch_id=? AND state IN ('batched','submitting','running')",
        (batch_id,)).fetchall()
    for (item_id,) in items:
        con.execute(
            "UPDATE planner_wake_items SET batch_id=NULL WHERE item_id=?",
            (item_id,))
        interface_state.transition(con, "wake_item", item_id, "queued")
    interface_state.transition(
        con, "wake_batch", batch_id, "complete",
        extra_sets={"completed_at": _now(con)})
    con.commit()


def sprint_still_running(con, sprint_doc_id) -> bool:
    """Is this sprint LIVE and still holding work? (H-6.) A live sprint whose
    every unit is merged or cancelled is finished in all but the freeze, and
    a release there is the ordinary end of a sprint — not a planner going
    deaf mid-flight."""
    if sprint_doc_id is None or not sprint_state.is_live_sprint(
            con, sprint_doc_id):
        return False
    return con.execute(
        "SELECT 1 FROM sprint_units "
        "WHERE sprint_doc_id=? AND state NOT IN ('merged','cancelled') "
        "LIMIT 1", (sprint_doc_id,)).fetchone() is not None


def release_binding(con, binding_id: int, reason: str) -> "int | None":
    """Release one binding and dispose of its queued wake work with an audit
    reason (spec Sprint Scope): messages stay UNREAD; a live submitting/
    running batch is left for hook reconciliation — its fenced evidence
    still resolves it. Returns the cancelled-item count, or None when the
    binding does not exist. An already-released binding is a no-op (0).
    The caller owns the transaction (commit).

    H-6 — RELEASE IS AN EVENT, NOT AN ABSENCE, and it changes what happens to
    the queued items in exactly one case. When the sprint is still running
    (live, with non-terminal units), a release means the planner has gone
    deaf mid-sprint: a generation change is the common cause and nothing said
    so. Two consequences here:

    - a critical `binding_released_live_sprint` opens, scoped to the SPRINT
      and to nothing else. binding_id and session_id are deliberately NULL,
      because every caller of this function follows it with a binding- or
      session-scoped resolve sweep — being inside that set is exactly how the
      old alerts vanished at the moment they became true. It resolves only
      when a new binding is armed for this sprint, or the sprint closes.

    - the queued items are HELD rather than cancelled. Cancelling them was
      right while nothing could ever reach them again; H-6's re-parent at arm
      time is what makes them reachable, and `cancelled` is a terminal state
      with no way back. They stay `queued` on the released binding, where no
      drain path can see them — every one requires `released_at IS NULL` —
      until arming re-parents them. Returns 0 cancelled in that case, which
      is the truth: nothing was cancelled.

    A sprint that is closed, frozen, or has no unit left running takes the
    original path unchanged — cancel, because nothing will ever arm again."""
    row = con.execute(
        "SELECT released_at, sprint_doc_id FROM sprint_planner_bindings "
        "WHERE binding_id=?", (binding_id,)).fetchone()
    if row is None:
        return None
    if row[0] is not None:
        return 0
    sprint_doc_id = row[1]
    hold = sprint_still_running(con, sprint_doc_id)
    con.execute(
        "UPDATE sprint_planner_bindings "
        "SET released_at=datetime('now'), release_reason=? "
        "WHERE binding_id=?", (reason, binding_id))
    cancelled = 0
    batches = con.execute(
        "SELECT batch_id FROM planner_wake_batches "
        "WHERE binding_id=? AND state='queued'", (binding_id,)).fetchall()
    for (batch_id_,) in batches:
        batched = con.execute(
            "SELECT COUNT(*) FROM planner_wake_items "
            "WHERE batch_id=? AND state='batched'",
            (batch_id_,)).fetchone()[0]
        # The BATCH always closes either way: it is generation-bound
        # (shell_id, generation) and can never submit under a new one.
        _close_batch_unsent(con, batch_id_, hold_items=hold)
        if not hold:
            cancelled += batched
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE binding_id=? AND state='queued'", (binding_id,)).fetchall()
    if not hold:
        for (item_id,) in items:
            interface_state.transition(
                con, "wake_item", item_id, "cancelled",
                extra_sets={"error": f"binding released: {reason}"})
            cancelled += 1
    if hold:
        held = con.execute(
            "SELECT COUNT(*) FROM planner_wake_items "
            "WHERE binding_id=? AND state='queued'",
            (binding_id,)).fetchone()[0]
        _alert(con, severity="critical",
               reason="binding_released_live_sprint",
               sprint_doc_id=sprint_doc_id,
               detail=(f"binding {binding_id} released ({reason}) while the "
                       f"sprint still has non-terminal units; {held} queued "
                       f"wake item(s) held for the next binding armed here"))
    return cancelled


def reparent_wake_items(con, binding_id: int, sprint_doc_id) -> int:
    """Adopt the wake items held by this sprint's RELEASED bindings (H-6).

    Items scope only through `binding_id`, so a release strands them behind a
    binding no drain will ever look at again; the sprint is recovered by
    joining the released rows' `sprint_doc_id`, across EVERY released
    generation — a sprint that lost two planner generations in a row has
    items behind both.

    Only unbatched queued items move: a batch is generation-bound and was
    closed at release, and its items were returned to `queued` there."""
    reparented = con.execute(
        "UPDATE planner_wake_items SET binding_id=? "
        "WHERE state='queued' AND batch_id IS NULL AND binding_id IN "
        "(SELECT binding_id FROM sprint_planner_bindings "
        " WHERE sprint_doc_id=? AND released_at IS NOT NULL)",
        (binding_id, sprint_doc_id)).rowcount
    con.execute(
        "UPDATE planner_alerts SET resolved_at=datetime('now') "
        "WHERE sprint_doc_id=? AND reason='binding_released_live_sprint' "
        "AND resolved_at IS NULL", (sprint_doc_id,))
    return reparented


def close_sprint_wake_work(con, sprint_doc_id, reason: str) -> int:
    """Sprint close: cancel the items H-6 held behind released bindings and
    close the deaf-sprint alert.

    Runs even when the sprint has NO unreleased binding left, which is the
    case that needs it: `release_bindings_for_sprint` is guarded on one
    existing, so a sprint that lost its planner and was then closed would
    otherwise leave held items queued forever behind a released binding."""
    items = con.execute(
        "SELECT i.item_id FROM planner_wake_items i "
        "JOIN sprint_planner_bindings b ON b.binding_id = i.binding_id "
        "WHERE b.sprint_doc_id=? AND b.released_at IS NOT NULL "
        "AND i.state='queued'", (sprint_doc_id,)).fetchall()
    for (item_id,) in items:
        interface_state.transition(
            con, "wake_item", item_id, "cancelled",
            extra_sets={"error": f"sprint closed: {reason}"})
    con.execute(
        "UPDATE planner_alerts SET resolved_at=datetime('now') "
        "WHERE sprint_doc_id=? AND reason='binding_released_live_sprint' "
        "AND resolved_at IS NULL", (sprint_doc_id,))
    return len(items)


def release_bindings_for_sprint(con, sprint_doc_id: int,
                                reason: str) -> "list[int]":
    """Sprint close (spec Sprint Scope): release every unreleased binding of
    the sprint and cancel its queued wake work, and resolve the bindings'
    open alerts — a released binding's wake failures are no longer
    actionable. Returns the released binding ids. The caller owns the
    transaction; used by the operator close path (doc status: CLOSED /
    freeze) so no orphan armed binding or stranded queued batch survives a
    sprint close."""
    rows = con.execute(
        "SELECT binding_id FROM sprint_planner_bindings "
        "WHERE sprint_doc_id=? AND released_at IS NULL",
        (sprint_doc_id,)).fetchall()
    ids = [r[0] for r in rows]
    for binding_id in ids:
        release_binding(con, binding_id, reason)
        con.execute(
            "UPDATE planner_alerts SET resolved_at=datetime('now') "
            "WHERE binding_id=? AND resolved_at IS NULL", (binding_id,))
    return ids


def _close_batch_unsent(con, batch_id: int, hold_items: bool = False) -> None:
    """Close a still-queued batch without sending a byte (the binding was
    released or the sprint stopped being live between form and submit): the
    batch completes empty — a wake must never fire for a sprint that is no
    longer armed.

    Its batched items are cancelled, EXCEPT when `hold_items` (H-6: the
    sprint is still running, so a future binding will adopt them). Then they
    ride the legal batched -> queued edge back and drop their batch_id: the
    batch is generation-bound and dead, the items are not."""
    interface_state.transition(
        con, "wake_batch", batch_id, "complete",
        extra_sets={"completed_at": _now(con)})
    resolve_batch_alerts(con, batch_id)   # H-26: a cancelled batch is not stalled
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE batch_id=? AND state='batched'", (batch_id,)).fetchall()
    for (item_id,) in items:
        if hold_items:
            interface_state.transition(con, "wake_item", item_id, "queued",
                                       extra_sets={"batch_id": None})
        else:
            interface_state.transition(con, "wake_item", item_id, "cancelled")


def _cancel_batch(con, batch_id: int) -> None:
    """Cancel a batch outright — every item terminal. The submit gate's
    sprint-no-longer-live path, where nothing will adopt them."""
    _close_batch_unsent(con, batch_id, hold_items=False)


def submit_wake_batch(con, batch_id: int, writer, now_iso: str,
                      quiet_s: float = DEFAULT_QUIET_S,
                      unmanaged_writable=None) -> dict:
    """Gate + submit one coalesced fixed-prompt batch under the input lock.

    Revalidates everything the spec requires before a byte moves: the binding
    still armed and its sprint still LIVE per `sprint_state` (a close, freeze
    or retitle between form_batch and submit CANCELS the batch — freeze is how
    sprint authority is revoked, so a post-freeze wake is exactly what must not
    fire), a live occupied session (an ended session gate-fails with an
    ALERT and the batch stays queued for a future generation — End chat
    deliberately does not release the sprint binding, so this gate must
    never crash on it), idle lifecycle, clean harness/tmux composer, clean
    metadata-only browser composer, quiet >= quiet_s since the last accepted
    human input AND since
    readiness (flag #49: the provider session_start stamp, NOT the pre-exec
    occupied_at — a >3s claude boot must not submit into an unpainted TUI; on
    a first_turn_gated harness the provider stamp cannot arrive unbidden, so
    the weaker process_ready_at stamp is the baseline instead, flag #303) AND
    since the last service restart (a fresh full
    debounce is owed after every restart), no pending human frame, mandatory
    lifecycle hooks actually supported by the session's harness, and NO
    unmanaged writable tmux client (decision #15: one is an immediate
    composer-unknown + disarm + alert, recoverable only by removal plus
    explicit clean certification). quiet_s must be > 0 (the spec forbids a
    zero debounce). Transient gate failures (busy/dirty/quiet) cancel the
    attempt WITHOUT a state change — the batch stays queued awaiting a
    later event; the quiet failure carries retry_after so the coordinator
    can re-attempt at the exact debounce deadline (event-reset, never a
    poll).

    The unmanaged-client probe runs BEFORE the write txn (SC-013): it
    shells out to tmux, and a wedged-but-alive server must never hang the
    drain thread while this gate holds the SQLite write lock.

    From the 'submitting' commit until the fenced submit hook, the batch
    holds the input lock: accept_human_input refuses new frames, so no human
    input can interleave inside the fixed submission. The submission is one
    indivisible writer call; its fence is forwarded_seq+1 (the broker
    sequence the submit hook must answer). A writer raising PreSendError
    PROVES no byte moved: the batch returns to queued (a legal edge) for the
    coordinator's bounded pre-send retries (1s/5s/30s) — it never parks.
    Any OTHER writer failure is ambiguous (the prompt may have landed): the
    batch parks as delivery_unknown, which also releases the lock.

    The gate reads + the 'submitting' commit are serialized under BEGIN
    IMMEDIATE (REV2 seq-4 L5 TOCTOU): two concurrent submitters on separate
    connections can no longer both pass the gate on the same pre-commit
    snapshot — the second blocks on the write lock, then re-reads state
    'submitting' and refuses; a human frame racing the gate either commits
    its pending reservation first (this gate then sees it and cancels the
    attempt) or loses to the 'submitting' commit and is refused by the lock.
    """
    if quiet_s <= 0:
        raise BrokerError("quiet_s must be > 0 — a zero debounce is forbidden")
    # Decision #15 probe runs BEFORE the write txn (SC-013): it shells out
    # to tmux, and a wedged-but-alive server must never hang the drain
    # thread while this gate holds the SQLite write lock.
    unmanaged = unmanaged_writable is not None and unmanaged_writable()
    began = _begin_immediate(con)
    try:
        batch = con.execute(
            "SELECT binding_id, shell_id, generation, state "
            "FROM planner_wake_batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        if batch is None:
            raise BrokerError(f"wake batch {batch_id} not found")
        if batch[3] != "queued":
            raise BrokerError(
                f"wake batch {batch_id} is {batch[3]}, not queued")
        binding_id, shell_id, generation, _ = batch

        # Revalidate the arming at SUBMIT time: a sprint close or binding
        # release since form_batch cancels the batch outright (no byte).
        binding = con.execute(
            "SELECT sprint_doc_id, released_at FROM sprint_planner_bindings "
            "WHERE binding_id=?", (binding_id,)).fetchone()
        if binding is None or binding[1] is not None:
            _cancel_batch(con, batch_id)
            con.commit()
            began = False
            return {"submitted": False, "cancelled": True,
                    "reason": "binding released — sprint no longer armed"}
        if not sprint_state.is_live_sprint(con, binding[0]):
            _cancel_batch(con, batch_id)
            con.commit()
            began = False
            return {"submitted": False, "cancelled": True,
                    "reason": "sprint is no longer live (frozen, retitled, "
                              "or its board was emptied)"}

        sess = con.execute(
            "SELECT session_id, occupancy, lifecycle, occupied_at, "
            "created_at, provider_ready_at, harness, cli_version, "
            "process_ready_at "
            "FROM interface_sessions "
            "WHERE shell_id=? AND generation=? AND occupancy <> 'ended'",
            (shell_id, generation)).fetchone()
        if sess is None:
            # End chat (_end_session) deliberately does NOT release the
            # binding or cancel queued wake work — chat and sprint
            # lifecycles are separate — so an armed binding can outlive its
            # session. The batch STAYS queued for a future generation; spec
            # Retry Policy requires harness/session loss to queue AND alert
            # (SC-011: a silent crash stall is the failure class this
            # feature exists to prevent).
            if began:
                con.rollback()
                began = False
            _alert(con, severity="critical", reason="wake_session_ended",
                   binding_id=binding_id)
            con.commit()
            return {"submitted": False, "reason": "session ended"}
        istate = con.execute(
            "SELECT composer, browser_composer, pending_seq, forwarded_seq, "
            "last_human_input_at FROM interface_input_state WHERE session_id=?",
            (sess[0],)).fetchone()

        def gate_fail(reason, **extra):
            if began:
                con.rollback()
            # H-26: record WHICH gate refused, verbatim, on every attempt.
            # Without this a persistent gate failure is indistinguishable
            # from a transient deferral — the batch shows depth and never
            # cause, which is issue #638's whole shape.
            note_gate_failure(con, batch_id, reason)
            return {"submitted": False, "reason": reason, **extra}

        if sess[1] != "occupied" or sess[2] != "idle":
            return gate_fail(
                f"session not occupied+idle ({sess[1]}/{sess[2]})")
        if istate[0] != "clean":
            return gate_fail(f"composer is {istate[0]}")
        if istate[1] != "clean":
            return gate_fail(f"browser composer is {istate[1]}")
        if istate[2] is not None:
            return gate_fail("a human frame is pending")
        cap = interface_hooks.capability(sess[6], sess[7])
        if not cap["mandatory_ok"]:
            reason = (
                f"harness {sess[6]!r} lacks mandatory lifecycle hooks "
                f"(missing: {', '.join(cap['missing_mandatory']) or 'version'})"
                " — wake cannot submit")
            # H-26: this one is NOT a deferral. Waiting cannot clear it — the
            # arm-path check passed a harness that has since degraded or
            # changed — so it alerts on the first refusal instead of aging
            # into the stall threshold like a gate that might yet pass.
            #
            # Detail carries harness, cli_version and missing_mandatory
            # VERBATIM because capability() fails mandatory_ok identically on
            # a missing-or-unparseable cli_version and on a genuinely
            # unsupported harness. A metadata-capture miss and a real
            # capability gap need completely different fixes, and only these
            # three fields tell them apart.
            #
            # Deliberate departure from the requirement's letter, stated
            # here and in the unit report: the reason key is distinct from
            # the arm path's `wake_not_armable` rather than reusing it. The
            # arm-path alert is session-scoped, so reusing the key would let
            # an already-open arm-time row swallow this one by dedupe — a
            # degradation-since-arming would then be silent, which is the
            # exact failure class this requirement exists to close.
            if began:
                con.rollback()
                began = False
            _alert(con, severity="critical",
                   reason="wake_hooks_missing_at_submit",
                   binding_id=binding_id, batch_id=batch_id,
                   sprint_doc_id=binding[0],
                   detail=(f"harness={sess[6]!r} "
                           f"cli_version={sess[7]!r} "
                           f"missing_mandatory="
                           f"{list(cap['missing_mandatory'])!r}"))
            con.commit()
            note_gate_failure(con, batch_id, reason)
            return {"submitted": False, "reason": reason}
        if unmanaged:
            # Decision #15: an unmanaged writable client bypasses the ordered
            # input boundary — detection sets composer unknown (which disarms
            # wake: the gate requires clean), alerts, and requires removal +
            # explicit clean certification before rearming. The probe itself
            # ran before the write txn (SC-013); only its verdict is applied
            # here.
            interface_state.transition(con, "composer", sess[0], "unknown")
            _alert(con, severity="critical",
                   reason="unmanaged_writable_client", session_id=sess[0])
            con.commit()
            began = False
            return {"submitted": False, "disarmed": True,
                    "reason": "unmanaged writable tmux client — composer "
                              "unknown, wake disarmed until removal + "
                              "clean certification"}
        # Quiet baseline (#49): the most recent of — last accepted human
        # input, REAL provider readiness (the provider session_start stamp,
        # never the pre-exec occupied_at), the weaker process-readiness stamp
        # (flag #303: on a first_turn_gated harness that is the stamp we armed
        # on, so the debounce must be owed from it and not from an invariant
        # that it equals occupied_at), session start, and the last service
        # restart (startup_reconcile revokes every lease with reason
        # 'service_restart'; that stamp is the restart time).
        baseline = max(t for t in (istate[4], sess[3], sess[4], sess[5],
                                   sess[8])
                       if t is not None)
        restart_at = con.execute(
            "SELECT MAX(revoked_at) FROM interface_writer_leases "
            "WHERE session_id=? AND revoke_reason='service_restart'",
            (sess[0],)).fetchone()[0]
        if restart_at is not None and restart_at > baseline:
            baseline = restart_at
        quiet = con.execute(
            "SELECT julianday(?) - julianday(?)", (now_iso, baseline)
        ).fetchone()[0] * 86400.0
        if quiet < quiet_s:
            return gate_fail(f"quiet {quiet:.2f}s < {quiet_s}s",
                             retry_after=quiet_s - quiet)

        fence = istate[3] + 1
        interface_state.transition(
            con, "wake_batch", batch_id, "submitting",
            extra_sets={"input_seq_fence": fence,
                        # H-27: when the wait for the submit hook STARTED.
                        # `submitted_at` cannot serve — it is written by that
                        # very hook, so on a silent seat it never arrives.
                        "submitting_at": _now(con)})
        # H-26: the gate passed — whatever was stalling this batch is over.
        resolve_batch_alerts(con, batch_id)
        # Items ride legal edges only: a first attempt's items are 'batched',
        # a bounded-retry re-attempt's were returned to 'queued' with the
        # batch — walk those through 'batched' before 'submitting'.
        con.execute(
            "UPDATE planner_wake_items SET state='batched' "
            "WHERE batch_id=? AND state='queued'", (batch_id,))
        con.execute(
            "UPDATE planner_wake_items SET state='submitting' "
            "WHERE batch_id=? AND state='batched'",
            (batch_id,))
        con.commit()
        began = False
    except Exception:
        if began:
            con.rollback()
        raise

    try:
        writer(len(WAKE_PROMPT) + 1)  # the fixed prompt + Enter, indivisible
    except PreSendError:
        # DEFINITE pre-send failure (the writer's preflight proved no byte
        # moved): the batch returns to queued — a legal edge — and the
        # coordinator's bounded retry schedule (1s/5s/30s) decides the next
        # attempt. NEVER parked: nothing is ambiguous.
        interface_state.transition(con, "wake_batch", batch_id, "queued")
        con.execute(
            "UPDATE planner_wake_items SET state='queued' "
            "WHERE batch_id=? AND state='submitting'", (batch_id,))
        con.commit()
        raise
    except Exception:
        # The prompt may or may not have landed and no submit hook can be
        # trusted to disambiguate — park exactly like the restart path (never
        # auto-retry; resolve_batch requeues after operator inspection).
        interface_state.transition(con, "wake_batch", batch_id,
                                   "delivery_unknown")
        _alert(con, severity="critical", reason="wake_batch_delivery_unknown",
               binding_id=binding_id)
        con.commit()
        raise
    # The submit hook (record_hook 'prompt_submit') moves the batch to
    # running with durable evidence. No hook → on restart the batch parks
    # as delivery_unknown and is never blindly resubmitted.
    return {"submitted": True, "input_seq_fence": fence}
