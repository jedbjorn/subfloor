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

import interface_state

MAX_INPUT_BYTES = 64 * 1024  # one human frame, per the pinned spike protocol
WAKE_PROMPT = "Check your inbox and act on unread sprint events."
DEFAULT_QUIET_S = 3.0  # debounce, never proof of an empty composer


class BrokerError(ValueError):
    """A refused broker operation (stale generation, bad sequence, gate)."""


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
    that commit on); without it, a held lease refuses."""
    sess = _session(con, session_id)
    if sess[3] != "occupied":
        raise BrokerError(f"session {session_id} is {sess[3]}, not occupied")
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
        " heartbeat_at) VALUES (?,?,?,?,?,datetime('now'))",
        (session_id, sess[1], sess[2], client_id,
         hashlib.sha256(token.encode()).hexdigest()),
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
    and forwards nothing; a gap rejects before any state change. A crash
    between the commits leaves pending_seq for startup reconciliation.
    """
    if payload_len > MAX_INPUT_BYTES:
        raise BrokerError(f"payload {payload_len} > {MAX_INPUT_BYTES} bytes")
    sess = _session(con, session_id)
    if sess[3] != "occupied":
        raise BrokerError(f"session {session_id} is {sess[3]}, not occupied")
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
        return {"ack": client_seq, "duplicate": True}
    if pending_seq is not None:
        # One unacknowledged frame per writer — the client buffers locally.
        raise BrokerError(f"sequence {pending_seq} is pending — wait for its ack")
    if client_seq != lease[2]:
        raise BrokerError(
            f"sequence gap: expected {lease[2]}, got {client_seq} — rejected, "
            "no bytes forwarded")

    # Phase 1 (commit): reserve the sequence, dirty the composer FIRST.
    interface_state.transition(
        con, "composer", session_id, "dirty",
        extra_sets={"pending_seq": client_seq,
                    "pending_reserved_at": _now(con),
                    "last_human_input_at": _now(con)})
    con.commit()

    # Phase 2: forward the exact bytes once. A crash here is the window.
    writer(payload_len)

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


def record_hook(con, shell_id: int, generation: int, hook_seq: int,
                event: str) -> dict:
    """Record one authenticated harness hook with its durable sequence.

    Rejects replays (hook_seq <= last_hook_seq) and stale generations. The
    sequence is the crash-window evidence: a batch's submit/stop hook seqs
    are stamped here, and startup reconciliation trusts only these durable
    stamps — never the broker's memory of what it sent.
    """
    gen = con.execute(
        "SELECT last_hook_seq, ended_at FROM interface_generations "
        "WHERE shell_id=? AND generation=?",
        (shell_id, generation),
    ).fetchone()
    if gen is None:
        raise BrokerError(f"unknown generation {shell_id}/{generation}")
    if gen[1] is not None:
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
        # Ready prompt proven, zero accepted human sequence → idle + clean.
        interface_state.transition(con, "lifecycle", sess[0], "idle")
        interface_state.transition(con, "composer", sess[0], "clean")
    elif event == "prompt_submit":
        # Fenced submit: clears dirty, moves to busy, acknowledges a
        # submitting wake batch whose fence this hook answers.
        input_seq = con.execute(
            "SELECT forwarded_seq FROM interface_input_state WHERE session_id=?",
            (sess[0],)).fetchone()[0]
        interface_state.transition(
            con, "composer", sess[0], "clean",
            extra_sets={"last_submit_seq": input_seq})
        interface_state.transition(con, "lifecycle", sess[0], "busy")
        batch = con.execute(
            "SELECT batch_id FROM planner_wake_batches "
            "WHERE shell_id=? AND generation=? AND state='submitting'",
            (shell_id, generation)).fetchone()
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
    elif event == "turn_stop":
        interface_state.transition(con, "lifecycle", sess[0], "idle")
        batch = con.execute(
            "SELECT batch_id FROM planner_wake_batches "
            "WHERE shell_id=? AND generation=? AND state='running'",
            (shell_id, generation)).fetchone()
        if batch is not None:
            _complete_batch(con, batch[0], hook_seq)
            result["wake_batch_complete"] = batch[0]
    elif event == "session_end":
        interface_state.transition(con, "lifecycle", sess[0], "ended")
    # approval_wait / approval_result / user_input_wait / interrupt map to
    # plain lifecycle moves — added with the adapters (task #83); the
    # mandatory four above are the wake-critical contract.
    con.commit()
    return result


def _complete_batch(con, batch_id: int, stop_hook_seq: int) -> None:
    """Reconcile a running batch's items from durable message read state
    (spec #20 Wake Delivery): read → done; unread → back to queued with
    completed_wakes+1. Infrastructure never marks messages read."""
    interface_state.transition(
        con, "wake_batch", batch_id, "complete",
        extra_sets={"stop_hook_seq": stop_hook_seq, "completed_at": _now(con)})
    items = con.execute(
        "SELECT item_id, message_id FROM planner_wake_items "
        "WHERE batch_id=? AND state IN ('batched','submitting','running')",
        (batch_id,)).fetchall()
    for item_id, message_id in items:
        read = con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=?",
            (message_id,)).fetchone()[0]
        if read is not None:
            interface_state.transition(
                con, "wake_item", item_id, "done",
                extra_sets={"done_at": _now(con)})
        else:
            con.execute(
                "UPDATE planner_wake_items SET completed_wakes="
                "completed_wakes + 1, batch_id=NULL WHERE item_id=?",
                (item_id,))
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
        "WHERE binding_id=? AND state='queued' ORDER BY item_id",
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


def submit_wake_batch(con, batch_id: int, writer, now_iso: str,
                      quiet_s: float = DEFAULT_QUIET_S) -> dict:
    """Gate + submit one coalesced fixed-prompt batch under the input lock.

    Revalidates everything the spec requires before a byte moves: occupied
    session, idle lifecycle, clean composer, quiet >= quiet_s since the last
    accepted human input, no pending human frame, current generation. Any
    uncertainty cancels the attempt WITHOUT a state change (the batch stays
    queued awaiting a later event) and without sending a byte. The
    submission is one indivisible writer call; its fence is forwarded_seq+1
    (the broker sequence the submit hook must answer).
    """
    batch = con.execute(
        "SELECT binding_id, shell_id, generation, state "
        "FROM planner_wake_batches WHERE batch_id=?",
        (batch_id,)).fetchone()
    if batch is None:
        raise BrokerError(f"wake batch {batch_id} not found")
    if batch[3] != "queued":
        raise BrokerError(f"wake batch {batch_id} is {batch[3]}, not queued")
    _, shell_id, generation, _ = batch
    sess = con.execute(
        "SELECT session_id, occupancy, lifecycle FROM interface_sessions "
        "WHERE shell_id=? AND generation=? AND occupancy <> 'ended'",
        (shell_id, generation)).fetchone()
    istate = con.execute(
        "SELECT composer, pending_seq, forwarded_seq, last_human_input_at "
        "FROM interface_input_state WHERE session_id=?",
        (sess[0],)).fetchone()

    if sess[1] != "occupied" or sess[2] != "idle":
        return {"submitted": False,
                "reason": f"session not occupied+idle ({sess[1]}/{sess[2]})"}
    if istate[0] != "clean":
        return {"submitted": False, "reason": f"composer is {istate[0]}"}
    if istate[1] is not None:
        return {"submitted": False, "reason": "a human frame is pending"}
    if istate[3] is not None:
        quiet = con.execute(
            "SELECT julianday(?) - julianday(?)", (now_iso, istate[3])
        ).fetchone()[0] * 86400.0
        if quiet < quiet_s:
            return {"submitted": False,
                    "reason": f"quiet {quiet:.2f}s < {quiet_s}s"}

    fence = istate[2] + 1
    interface_state.transition(
        con, "wake_batch", batch_id, "submitting",
        extra_sets={"input_seq_fence": fence})
    con.execute(
        "UPDATE planner_wake_items SET state='submitting' "
        "WHERE batch_id=? AND state='batched'",
        (batch_id,))
    con.commit()

    writer(len(WAKE_PROMPT) + 1)  # the fixed prompt + Enter, indivisible
    # The submit hook (record_hook 'prompt_submit') moves the batch to
    # running with durable evidence. No hook → on restart the batch parks
    # as delivery_unknown and is never blindly resubmitted.
    return {"submitted": True, "input_seq_fence": fence}
