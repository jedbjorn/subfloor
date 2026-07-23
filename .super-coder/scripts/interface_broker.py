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

MAX_INPUT_BYTES = 64 * 1024  # one human frame, per the pinned spike protocol
WAKE_PROMPT = "Check your inbox and act on unread sprint events."
DEFAULT_QUIET_S = 3.0  # debounce, never proof of an empty composer
MAX_COMPLETED_WAKES = 3  # unread after 3 completed wake turns → quarantined


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
           binding_id=None, message_id=None) -> None:
    """Raise an alert, deduplicated while open (partial unique index)."""
    dedupe = f"{session_id or '-'}|{binding_id or '-'}|{message_id or '-'}|{reason}"
    con.execute(
        "INSERT OR IGNORE INTO planner_alerts "
        "(session_id, binding_id, message_id, severity, reason, dedupe_key) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, binding_id, message_id, severity, reason, dedupe))


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
    and resolves or parks session-scoped wake state by the existing
    ambiguity rules (a pending human frame parks delivery_unknown; a
    batch with a proven stop reconciles from read state; a batch with no
    stop evidence parks — no live harness will re-drive it). Queued wake
    work is deliberately left queued for a future generation.

    Idempotent: an already-ended session returns its original terminal
    result without state churn."""
    row = con.execute(
        "SELECT shell_id, generation, occupancy, lifecycle, end_reason "
        "FROM interface_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise BrokerError(f"interface session {session_id} not found")
    shell_id, generation, occupancy, lifecycle, prior_reason = row
    if occupancy == "ended":
        return {"session_id": session_id, "already_ended": True,
                "end_reason": prior_reason}

    interface_state.transition(
        con, "occupancy", session_id, "ended",
        extra_sets={"ended_at": _now(con), "end_reason": end_reason})
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
    con.execute(
        "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
        "revoke_reason='session_end' "
        "WHERE session_id=? AND revoked_at IS NULL", (session_id,))

    # Session-scoped wake state: the generation is provably over, so a
    # pending human frame can never be acked and a live batch can never
    # see its stop hook. Resolve from durable evidence or park — the same
    # rules startup reconciliation applies (decision #22).
    istate = con.execute(
        "SELECT pending_seq, delivery FROM interface_input_state "
        "WHERE session_id=?", (session_id,)).fetchone()
    if istate is not None and istate[0] is not None \
            and istate[1] != "delivery_unknown":
        park_delivery_unknown(con, session_id)
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
    return {"session_id": session_id, "already_ended": False,
            "end_reason": end_reason}


def record_hook(con, shell_id: int, generation: int, hook_seq: int,
                event: str, source: str = "provider") -> dict:
    """Record one authenticated harness hook with its durable sequence.

    Rejects replays (hook_seq <= last_hook_seq), stale generations, and
    unknown events. The sequence is the crash-window evidence: a batch's
    submit/stop hook seqs are stamped here, and startup reconciliation
    trusts only these durable stamps — never the broker's memory of what
    it sent.

    `source` distinguishes the entrypoint's pre-exec identity claim
    (reserved → occupied promotion; NOT readiness) from a provider-native
    hook delivered by the emitter — only the provider's session_start is
    real start-readiness (sprint 25 seq 7).
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
        "SELECT session_id, lifecycle FROM interface_sessions "
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
            istate = con.execute(
                "SELECT composer, pending_seq, forwarded_seq "
                "FROM interface_input_state WHERE session_id=?",
                (sess[0],)).fetchone()
            if istate is not None and istate[1] is None and istate[2] == 0:
                interface_state.transition(con, "composer", sess[0], "clean")
            _hook_capability_alerts(con, sess[0])
        # source='entrypoint': identity/promotion only (the route owns the
        # reserved → occupied move); readiness waits for the provider hook.
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
                con.execute(
                    "UPDATE planner_wake_items SET state='running' "
                    "WHERE batch_id=? AND state='submitting'",
                    (batch[0],))
                result["wake_batch_running"] = batch[0]
        interface_state.transition(con, "lifecycle", sess[0], "busy")
    elif event == "turn_stop":
        if _turn_finished(con, sess, shell_id, generation, hook_seq):
            result["wake_batch_complete"] = True
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


def form_batch(con, binding_id: int) -> int:
    """Coalesce a binding's currently queued items into one batch (the
    fixed-prompt submission unit). The partial unique index backstops the
    one-live-batch invariant; items join oldest first."""
    binding = con.execute(
        "SELECT shell_id, generation FROM sprint_planner_bindings "
        "WHERE binding_id=? AND released_at IS NULL",
        (binding_id,)).fetchone()
    if binding is None:
        raise BrokerError(f"binding {binding_id} not found or released")
    cur = con.execute(
        "INSERT INTO planner_wake_batches (binding_id, shell_id, generation) "
        "VALUES (?,?,?)",
        (binding_id, binding[0], binding[1]))
    batch_id = cur.lastrowid
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE binding_id=? AND state='queued' AND batch_id IS NULL "
        "ORDER BY item_id",
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


def release_binding(con, binding_id: int, reason: str) -> "int | None":
    """Release one binding and cancel its queued wake work with an audit
    reason (spec Sprint Scope): messages stay UNREAD; a live submitting/
    running batch is left for hook reconciliation — its fenced evidence
    still resolves it. Returns the cancelled-item count, or None when the
    binding does not exist. An already-released binding is a no-op (0).
    The caller owns the transaction (commit)."""
    row = con.execute(
        "SELECT released_at FROM sprint_planner_bindings WHERE binding_id=?",
        (binding_id,)).fetchone()
    if row is None:
        return None
    if row[0] is not None:
        return 0
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
        _cancel_batch(con, batch_id_)
        cancelled += batched
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE binding_id=? AND state='queued'", (binding_id,)).fetchall()
    for (item_id,) in items:
        interface_state.transition(
            con, "wake_item", item_id, "cancelled",
            extra_sets={"error": f"binding released: {reason}"})
        cancelled += 1
    return cancelled


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


def _sprint_active(con, sprint_doc_id: int) -> bool:
    """The sprint doc's body contract carries a `status: ACTIVE|CLOSED` line
    (the planner is its only writer); a wake may submit only while ACTIVE."""
    row = con.execute(
        "SELECT body FROM documents WHERE document_id=?",
        (sprint_doc_id,)).fetchone()
    if row is None or row[0] is None:
        return False
    for line in row[0].splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() == "ACTIVE"
    return False


def _cancel_batch(con, batch_id: int) -> None:
    """Close a still-queued batch without sending a byte (the binding was
    released or the sprint left ACTIVE between form and submit): the batch
    completes empty and its batched items are cancelled — a wake must never
    fire for a sprint that is no longer armed."""
    interface_state.transition(
        con, "wake_batch", batch_id, "complete",
        extra_sets={"completed_at": _now(con)})
    items = con.execute(
        "SELECT item_id FROM planner_wake_items "
        "WHERE batch_id=? AND state='batched'", (batch_id,)).fetchall()
    for (item_id,) in items:
        interface_state.transition(con, "wake_item", item_id, "cancelled")


def submit_wake_batch(con, batch_id: int, writer, now_iso: str,
                      quiet_s: float = DEFAULT_QUIET_S,
                      unmanaged_writable=None) -> dict:
    """Gate + submit one coalesced fixed-prompt batch under the input lock.

    Revalidates everything the spec requires before a byte moves: the binding
    still armed and its sprint still ACTIVE AND unfrozen (a close or freeze
    between form_batch and submit CANCELS the batch — freeze is how sprint
    authority is revoked, so a post-freeze wake is exactly what must not
    fire), a live occupied session (an ended session gate-fails with an
    ALERT and the batch stays queued for a future generation — End chat
    deliberately does not release the sprint binding, so this gate must
    never crash on it), idle lifecycle, clean
    composer, quiet >= quiet_s since the last accepted human input AND since
    REAL provider readiness (flag #49: the provider session_start stamp, NOT
    the pre-exec occupied_at — a >3s claude/codex boot must not submit into
    an unpainted TUI) AND since the last service restart (a fresh full
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
        frozen = con.execute(
            "SELECT frozen FROM documents WHERE document_id=?",
            (binding[0],)).fetchone()
        if (frozen is None or frozen[0]
                or not _sprint_active(con, binding[0])):
            _cancel_batch(con, batch_id)
            con.commit()
            began = False
            return {"submitted": False, "cancelled": True,
                    "reason": "sprint doc is frozen or not ACTIVE"}

        sess = con.execute(
            "SELECT session_id, occupancy, lifecycle, occupied_at, "
            "created_at, provider_ready_at, harness, cli_version "
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
            "SELECT composer, pending_seq, forwarded_seq, last_human_input_at "
            "FROM interface_input_state WHERE session_id=?",
            (sess[0],)).fetchone()

        def gate_fail(reason, **extra):
            if began:
                con.rollback()
            return {"submitted": False, "reason": reason, **extra}

        if sess[1] != "occupied" or sess[2] != "idle":
            return gate_fail(
                f"session not occupied+idle ({sess[1]}/{sess[2]})")
        if istate[0] != "clean":
            return gate_fail(f"composer is {istate[0]}")
        if istate[1] is not None:
            return gate_fail("a human frame is pending")
        cap = interface_hooks.capability(sess[6], sess[7])
        if not cap["mandatory_ok"]:
            return gate_fail(
                f"harness {sess[6]!r} lacks mandatory lifecycle hooks "
                f"(missing: {', '.join(cap['missing_mandatory']) or 'version'})"
                " — wake cannot submit")
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
        # never the pre-exec occupied_at), session start, and the last
        # service restart (startup_reconcile revokes every lease with reason
        # 'service_restart'; that stamp is the restart time).
        baseline = max(t for t in (istate[3], sess[3], sess[4], sess[5])
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

        fence = istate[2] + 1
        interface_state.transition(
            con, "wake_batch", batch_id, "submitting",
            extra_sets={"input_seq_fence": fence})
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
