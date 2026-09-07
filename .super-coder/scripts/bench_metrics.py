"""Spec #222 metric projection over captured harness logs.

``derive_metrics`` returns receipt sections timing, tokens, tools and timeout;
each metric is {value, fidelity, note}. source is the captured codex sessions
root, Claude projects root, or OpenCode database. session_ref is the exact
analytics ref (JSONL path for codex/claude, session id for OpenCode). Pass the
recorded repo_root, not the directory where evidence was copied. No ambient
harness data is read. Capture failures raise; missing measurements stay null.

Token totals come from token_parsers.sweep, including Claude's copied-message
deduplication/subagent attribution and OpenCode's reasoning-in-output rule.
Multiple model rows belonging to the same session are summed. wall_ms and
 timeout use the runner's dispatch/exit clock and timeout result (full).
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TOKEN_KEYS = ('input_tokens', 'cache_read_tokens', 'cache_write_tokens',
              'output_tokens', 'reasoning_tokens')


def metric(value, fidelity, note):
    return {'value': value, 'fidelity': fidelity if value is not None else 'null',
            'note': note}


def epoch_ms(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('metric timestamps must include a timezone')
    return round(dt.astimezone(timezone.utc).timestamp() * 1000)


def records(path):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError('transcript record must be an object')
                yield item


def codex_activity(source, ref):
    """Derived: first_token_ms uses the first assistant response_item timestamp
    (message, reasoning, function_call or custom_tool_call), not event echoes.
    turns counts assistant message items. tool_calls counts call items once by
    call_id; tool_batches counts consecutive call runs, ending at a response
    item other than a call (event metadata does not split a batch). shell_execs
    counts shell/exec/exec_command calls, including namespaced forms. An exec
    wrapper is one call; commands embedded in its code are not guessed.
    Cache-write is null: analytics exposes no codex cache-write class.
    """
    first = None
    turns = batches = calls = shells = 0
    in_batch = False
    seen = set()
    for r in records(ref):
        if r.get('type') != 'response_item':
            continue
        p = r.get('payload') or {}
        kind = p.get('type')
        call = kind in ('function_call', 'custom_tool_call')
        assistant = kind == 'reasoning' or call or (
            kind == 'message' and p.get('role') == 'assistant')
        if assistant and first is None and r.get('timestamp'):
            first = epoch_ms(r['timestamp'])
        if kind == 'message' and p.get('role') == 'assistant':
            turns += 1
        if not call:
            in_batch = False
            continue
        cid = p.get('call_id')
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        batches += not in_batch
        in_batch = True
        calls += 1
        shells += p.get('name', '').split('.')[-1] in ('shell', 'exec', 'exec_command')
    return first, turns, batches, calls, shells


def claude_activity(source, ref):
    """Derived: first_token_ms uses the earliest non-synthetic assistant line
    timestamp; turns counts unique message.id, tool_calls unique tool_use.id,
    tool_batches unique assistant message ids containing tools, shell_execs
    Bash tool_use blocks. Content-block lines share an id and are merged.
    Cross-file copied ids belong to the first file in mtime order, matching
    analytics. Subagent files fold into their parent session. Reasoning tokens
    remain null because the transcript exposes no reasoning usage class.
    """
    from token_parsers.claude import _session_ref

    ref = Path(ref)
    project = ref.parent
    owners = {}
    selected = []
    for path in sorted(project.rglob('*.jsonl'), key=lambda p: p.stat().st_mtime):
        for r in records(path):
            m = r.get('message') or {}
            mid = m.get('id')
            if r.get('type') != 'assistant' or not mid or m.get('model') == '<synthetic>':
                continue
            owner = owners.setdefault(mid, _session_ref(project, path))
            if owner == str(ref) and _session_ref(project, path) == str(ref):
                selected.append(r)
    times = [epoch_ms(r['timestamp']) for r in selected if r.get('timestamp')]
    messages, batches, calls, shells = set(), set(), set(), set()
    for r in selected:
        m = r['message']
        mid = m['id']
        messages.add(mid)
        for b in m.get('content', []):
            if not isinstance(b, dict) or b.get('type') != 'tool_use':
                continue
            if not b.get('id'):
                raise ValueError('Claude tool_use has no id')
            calls.add(b['id'])
            batches.add(mid)
            if b.get('name') == 'Bash':
                shells.add(b['id'])
    return min(times, default=None), len(messages), len(batches), len(calls), len(shells)


def opencode_activity(source, ref):
    """Resolves all verify cells: cache_write_tokens is full (session column);
    first_token_ms, turns, tool_batches, tool_calls and shell_execs are derived.
    First assistant item = message.data.time.created (message creation latency,
    not a streaming first-token measurement). turns = assistant message rows;
    batches = assistant message ids with tool parts; calls = tool part rows;
    shell_execs = tool parts named bash. Query both tables by exact session id
    and join tool parts to assistant ids, excluding other sessions/subagents.
    Missing message/part tables downgrade activity metrics to null, never zero.
    """
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'message', 'part'} <= tables:
            return None, None, None, None, None
        messages = {mid: json.loads(raw) for mid, raw in con.execute(
            'SELECT id, data FROM message WHERE session_id=?', (ref,))}
        assistants = {mid: data for mid, data in messages.items() if data.get('role') == 'assistant'}
        times = [d['time']['created'] for d in assistants.values() if d.get('time', {}).get('created') is not None]
        batches = set()
        calls = shells = 0
        for mid, raw in con.execute('SELECT message_id, data FROM part WHERE session_id=?', (ref,)):
            data = json.loads(raw)
            if mid not in assistants or data.get('type') != 'tool':
                continue
            batches.add(mid)
            calls += 1
            shells += data.get('tool') == 'bash'
    return min(times, default=None), len(assistants), len(batches), calls, shells


ACTIVITY = {'codex': codex_activity, 'claude': claude_activity, 'opencode': opencode_activity}


def derive_metrics(harness, source, *, repo_root, session_ref, dispatched_at,
                   ended_at, timeout=False):
    """Project one captured session onto the receipt's metrics contract.

    Require exact session ownership and valid runner clock bounds. Unknown or
    malformed captures raise ValueError/OSError/sqlite3.Error; callers must
    mark the cell invalid rather than emit fabricated zero measurements.
    """
    if harness not in ACTIVITY:
        raise ValueError('unsupported benchmark harness')
    source = Path(source).resolve()
    if not source.exists():
        raise ValueError('missing capture source')
    ref = str(session_ref)
    if harness != 'opencode':
        path = Path(ref).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError('session transcript must be inside capture source')
        ref = str(path)
    start, end = epoch_ms(dispatched_at), epoch_ms(ended_at)
    if end < start or not isinstance(timeout, bool):
        raise ValueError('invalid runner timing or timeout')
    parser = importlib.import_module('token_parsers.' + harness)
    notices = []
    rows = [r for r in parser.sweep(Path(repo_root), lambda _: None, notices.append,
                                   source=source) if r['harness_session_ref'] == ref]
    if notices or not rows:
        raise ValueError('capture could not be normalized for the selected session')
    first, turns, batches, calls, shells = ACTIVITY[harness](source, ref)
    if first is not None and not start <= first <= end:
        raise ValueError('assistant timestamp outside runner dispatch/exit interval')
    tokens = {}
    for key in TOKEN_KEYS:
        values = [r[key] for r in rows]
        value = sum(values) if all(v is not None for v in values) else None
        tokens[key] = metric(value, 'full', 'token_parsers normalization; unavailable classes remain null')
    return {
        'timing': {'dispatched_at': dispatched_at, 'ended_at': ended_at,
                   'wall_ms': metric(end - start, 'full', 'runner dispatch to exit'),
                   'first_token_ms': metric(first - start if first is not None else None,
                                            'derived', 'first assistant item timestamp; see harness parser rule')},
        'tokens': tokens,
        'tools': {key: metric(value, 'derived', 'captured assistant items; see harness parser rule')
                  for key, value in zip(('turns', 'tool_batches', 'tool_calls', 'shell_execs'),
                                        (turns, batches, calls, shells))},
        'timeout': metric(timeout, 'full', 'runner timeout result'),
    }
