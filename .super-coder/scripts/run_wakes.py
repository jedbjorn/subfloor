"""Non-Sprint re-enter delivery receipts and bounded CLI contention recovery.

The original conversation message/outbox is reused after proven pre-dispatch
SHELL_BUSY; no new logical prompt or wake intent is minted. Native failures are
never automatically replayed. Transport retries retain the same wake key.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sprint_participant_chats import SprintConversationError

import db_driver

BACKOFF = (15, 60, 180, 300)


def _stamp(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def record_enqueue(con, message_id, native_ref):
    if not isinstance(native_ref, str) or not native_ref.startswith('conversation-message:'):
        return
    message = con.execute('SELECT sprint_id,declared_type FROM wake_message WHERE message_id=?',
                          (message_id,)).fetchone()
    if message['sprint_id'] is not None or message['declared_type'] != 're-enter':
        return
    with db_driver.write_transaction(con, 'wake.receipt'):
        con.execute('INSERT INTO engine_wake_receipts(message_id,conversation_message_id) '
                    'VALUES(?,?) ON CONFLICT(message_id) DO UPDATE SET '
                    'conversation_message_id=excluded.conversation_message_id',
                    (message_id, int(native_ref.split(':')[1])))


def record_transport_failure(con, lease, exc, now):
    """Engine-only path: retry transport indefinitely, block invalid routes."""
    reason = f'{type(exc).__name__}: wake delivery unavailable'
    permanent = isinstance(exc, (KeyError, ValueError, PermissionError, SprintConversationError))
    with db_driver.write_transaction(con, 'wake.engine_failure'):
        con.execute('INSERT INTO engine_wake_failures(wake_id,attempts,last_attempt_at,last_error) '
                    'VALUES(?,1,?,?) ON CONFLICT(wake_id) DO UPDATE SET '
                    'attempts=attempts+1,last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error',
                    (lease.wake_id, _stamp(now), reason))
        attempts = con.execute('SELECT attempts FROM engine_wake_failures WHERE wake_id=?',
                               (lease.wake_id,)).fetchone()[0]
        if not permanent:
            followup = con.execute("SELECT wake_id FROM sprint_wake_outbox WHERE receiver_shell_id=? "
                                   "AND state='pending' AND wake_id<>?",
                                   (lease.receiver_shell_id, lease.wake_id)).fetchone()
            if followup:
                con.execute('UPDATE sprint_wake_messages SET wake_id=? WHERE wake_id=?',
                            (lease.wake_id, followup[0]))
                con.execute("UPDATE sprint_wake_outbox SET state='cancelled',quiet_since=NULL WHERE wake_id=?",
                            (followup[0],))
        con.execute("UPDATE sprint_wake_outbox SET state=?,last_error=?,available_at=?,"
                    "failed_at=CASE WHEN ? THEN datetime('now') ELSE NULL END,quiet_since=NULL,"
                    "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL WHERE wake_id=? "
                    "AND claim_owner=?", ('failed' if permanent else 'pending', reason,
                    _stamp(now + timedelta(seconds=BACKOFF[min(attempts - 1, 3)])),
                    int(permanent), lease.wake_id, lease.claim_owner))
        if permanent:
            con.execute("UPDATE runs SET wake_state='blocked',last_error=? WHERE message_id IN "
                        '(SELECT message_id FROM sprint_wake_messages WHERE wake_id=?)',
                        (reason + '; operator recovery required', lease.wake_id))
    return attempts, 'failed' if permanent else 'pending'


def _contains_prompt(path, offset, marker, body):
    """Require the actual wake text in this native turn's transcript slice."""
    if not path:
        return False
    try:
        with Path(path).open('rb') as handle:
            handle.seek(offset or 0)
            text = handle.read(16 * 1024 * 1024).decode(errors='replace')
    except OSError:
        return False
    def contains(value):
        if isinstance(value, str):
            return marker in value and body in value
        if isinstance(value, dict):
            return any(contains(v) for v in value.values())
        if isinstance(value, list):
            return any(contains(v) for v in value)
        return False
    for line in text.splitlines():
        try:
            if contains(json.loads(line)):
                return True
        except ValueError:
            continue
    return False


def reconcile(con, *, now=None):
    now = now or datetime.now(timezone.utc)
    rows = con.execute(
        'SELECT e.*,m.body,c.conversation_id,c.state AS chat_state,r.run_id,r.state AS run_state,'
        'r.error_code,r.error_detail,r.transcript_path,r.transcript_offset '
        'FROM engine_wake_receipts e JOIN wake_message m USING(message_id) '
        'JOIN conversation_messages cm ON cm.message_id=e.conversation_message_id '
        'JOIN conversations c ON c.conversation_id=cm.conversation_id '
        'LEFT JOIN conversation_runs r ON r.run_id=(SELECT MAX(cr.run_id) FROM conversation_runs cr '
        'WHERE cr.trigger_message_id=cm.message_id) WHERE e.blocked_reason IS NULL').fetchall()
    for row in rows:
        consumed = row['run_id'] is not None and _contains_prompt(
            row['transcript_path'], row['transcript_offset'],
            f"wake_message #{row['message_id']} ", row['body'])
        with db_driver.write_transaction(con, 'wake.engine_reconcile'):
            if consumed:
                con.execute("UPDATE runs SET wake_state='consumed',turn_run_id=?,last_error=NULL "
                            'WHERE message_id=?', (row['run_id'], row['message_id']))
                continue
            busy = row['run_state'] == 'failed' and row['error_code'] == 'SHELL_BUSY'
            if busy and row['last_run_id'] != row['run_id']:
                attempt = row['busy_attempts'] + 1
                blocked = 'SHELL_BUSY retry ladder exhausted; release CLI slot and recover via operator' if attempt > len(BACKOFF) else None
                retry = None if blocked else _stamp(now + timedelta(seconds=BACKOFF[attempt-1]))
                con.execute('UPDATE engine_wake_receipts SET busy_attempts=?,last_run_id=?,retry_at=?,'
                            'blocked_reason=? WHERE message_id=?',
                            (attempt, row['run_id'], retry, blocked, row['message_id']))
                con.execute('UPDATE runs SET wake_state=?,last_error=?,turn_run_id=NULL WHERE message_id=?',
                            ('blocked' if blocked else 'pending', blocked or 'SHELL_BUSY; queued for retry', row['message_id']))
                continue
            if row['retry_at'] and row['retry_at'] <= _stamp(now):
                if row['chat_state'] == 'closed':
                    reason = 'wake chat closed during CLI contention; operator recovery required'
                    con.execute('UPDATE engine_wake_receipts SET blocked_reason=? WHERE message_id=?',
                                (reason, row['message_id']))
                    con.execute("UPDATE runs SET wake_state='blocked',last_error=? WHERE message_id=?",
                                (reason, row['message_id']))
                    continue
                con.execute("UPDATE conversation_messages SET state='queued',completed_at=NULL WHERE message_id=?",
                            (row['conversation_message_id'],))
                con.execute("UPDATE conversation_outbox SET state='pending',claim_owner=NULL,claimed_at=NULL,"
                            'lease_expires_at=NULL,run_id=NULL,dispatched_at=NULL WHERE message_id=?',
                            (row['conversation_message_id'],))
                con.execute("UPDATE conversations SET state='queued',version=version+1 WHERE conversation_id=? "
                            "AND state NOT IN ('running','closed')", (row['conversation_id'],))
                con.execute('UPDATE engine_wake_receipts SET retry_at=NULL WHERE message_id=?', (row['message_id'],))
                continue
            if row['retry_at'] or busy:
                continue
            terminal_failure = row['run_state'] in {'failed','cancelled','unknown'}
            state = 'blocked' if terminal_failure else 'enqueued'
            reason = 'wake turn ended without transcript consumption evidence; operator recovery required' if terminal_failure else None
            con.execute('UPDATE runs SET wake_state=?,turn_run_id=?,last_error=? '
                        "WHERE message_id=? AND wake_state<>'consumed'",
                        (state, row['run_id'], reason, row['message_id']))
            if terminal_failure:
                con.execute('UPDATE engine_wake_receipts SET blocked_reason=? WHERE message_id=?',
                            (reason, row['message_id']))
