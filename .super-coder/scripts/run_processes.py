"""Read-only native process observations, retained as conversation evidence.

These snapshots never enter the runs ledger and never confer kill authority.
The root's incarnation and uid must match before its group/ancestry is read.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import conversation_events
import db_driver


def _read_process(path: Path, uptime: float, hz: int, uid: int) -> dict | None:
    if path.stat().st_uid != uid:
        return None
    raw = (path / 'stat').read_text()
    comm, tail = raw.rsplit(')', 1)
    fields = tail.split()
    if fields[0] in {'Z', 'X'}:
        return None
    ticks = int(fields[19])
    with (path / 'cmdline').open('rb') as handle:
        cmdline = handle.read(512).replace(b'\0', b' ').decode(errors='replace').strip()
    if path.stat().st_uid != uid:
        return None
    if int((path / 'stat').read_text().rsplit(')', 1)[1].split()[19]) != ticks:
        return None
    return {
        'pid': int(path.name), 'start_ticks': ticks,
        'ppid': int(fields[1]), 'process_group_id': int(fields[2]),
        'comm': comm.split('(', 1)[1][:128],
        'cmdline': cmdline,
        'age_s': max(0, round(uptime - ticks / hz, 1)),
    }


def snapshot(pid: int, ticks: int, group: int, *, proc: Path = Path('/proc')) -> dict:
    result: dict = {'kind': 'native', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'pid': pid, 'start_ticks': ticks, 'process_group_id': group,
              'processes': [], 'indeterminate': 0, 'omitted': 0, 'root_alive': False}
    uid, hz = os.getuid(), os.sysconf('SC_CLK_TCK')
    try:
        uptime = float((proc / 'uptime').read_text().split()[0])
        root = _read_process(proc / str(pid), uptime, hz, uid)
        if root is None or root['start_ticks'] != ticks or root['process_group_id'] != group:
            return result
    except FileNotFoundError:
        return result
    except (OSError, ValueError, IndexError):
        result['indeterminate'] += 1
        return result
    processes = {pid: root}
    try:
        paths = list(proc.iterdir())
    except OSError:
        result['indeterminate'] += 1
        return result
    for path in paths:
        if not path.name.isdigit() or int(path.name) == pid:
            continue
        try:
            item = _read_process(path, uptime, hz, uid)
            if item is not None:
                processes[item['pid']] = item
        except FileNotFoundError:
            continue  # Exited during the scan; no liveness claim.
        except (OSError, ValueError, IndexError):
            result['indeterminate'] += 1
    selected = {pid} | {p for p, item in processes.items() if item['process_group_id'] == group}
    # Follow ancestry without recursion or unbounded depth, including setsid children.
    pending = set(processes) - selected
    while pending:
        children = {p for p in pending if processes[p]['ppid'] in selected}
        if not children:
            break
        selected.update(children)
        pending.difference_update(children)
    # Recheck the root after enumeration: never attribute a recycled group.
    try:
        current = _read_process(proc / str(pid), uptime, hz, uid)
        if current is None or current['start_ticks'] != ticks or current['process_group_id'] != group:
            return result
    except (OSError, ValueError, IndexError):
        result['indeterminate'] += 1
        return result
    ordered = sorted(selected, key=lambda p: (p != pid, p))
    result['root_alive'] = True
    result['processes'] = [processes[p] for p in ordered[:128]]
    result['omitted'] = max(0, len(ordered) - 128)
    return result


def observe(con, conversation_id: str) -> dict:
    from conversation_broker import BrokerStore

    row = con.execute(
        'SELECT r.*,s.flavor FROM conversation_runs r JOIN shells s USING(shell_id) '
        'WHERE r.conversation_id=? AND r.process_pid IS NOT NULL '
        'ORDER BY r.run_id DESC LIMIT 1', (conversation_id,),
    ).fetchone()
    if row is None or row['flavor'] == 'admin':
        return {'processes': [], 'indeterminate': 0, 'omitted': 0}
    if row['process_start_ticks'] is None or row['process_group_id'] is None:
        return {'processes': [], 'indeterminate': 1, 'omitted': 0}
    observed = snapshot(row['process_pid'], row['process_start_ticks'], row['process_group_id'])
    observed['run_id'] = row['run_id']
    if not observed['root_alive']:
        return observed
    with db_driver.write_transaction(con, 'conversation.process_snapshot'):
        current = con.execute('SELECT process_pid,process_start_ticks FROM conversation_runs WHERE run_id=?',
                              (row['run_id'],)).fetchone()
        if tuple(current) != (row['process_pid'], row['process_start_ticks']):
            return {'processes': [], 'indeterminate': 0, 'omitted': 0}
        BrokerStore._append_event(con, conversation_id=conversation_id,
                                  event_type='run.process.snapshot', payload=observed,
                                  message_id=row['trigger_message_id'], run_id=row['run_id'])
    conversation_events.notify(conversation_id)
    return observed


def last_snapshot(con, run_id: int) -> dict | None:
    # Continuations share the native process with their original turn.
    row = con.execute(
        "SELECT e.payload FROM conversation_events e JOIN conversation_runs r USING(run_id) "
        "WHERE r.trigger_message_id=(SELECT trigger_message_id FROM conversation_runs WHERE run_id=?) "
        "AND e.event_type='run.process.snapshot' ORDER BY e.sequence DESC LIMIT 1", (run_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def termination_evidence(observed: dict | None) -> dict | None:
    if not observed:
        return None
    children = [p for p in observed['processes'] if p['pid'] != observed['pid']]
    if not children:
        return None
    alive = unknown = 0
    for child in children:
        try:
            path = Path('/proc') / str(child['pid'])
            if path.stat().st_uid != os.getuid():
                unknown += 1
                continue
            item = _read_process(path, 0, os.sysconf('SC_CLK_TCK'), os.getuid())
            if item is not None and item['start_ticks'] == child['start_ticks']:
                alive += 1
        except FileNotFoundError:
            pass
        except (OSError, ValueError, IndexError):
            unknown += 1
    count = len(children)
    if alive or unknown:
        label = (f'{count} child processes were observed before this turn ended; '
                 f'{alive} still running, {unknown} indeterminate. ')
    else:
        label = (f'{count} child processes were still running when this turn ended; '
                 'they were terminated with it. ')
    return {'label': label + 'Use `sc job` for work that must outlive a turn.',
            'observed_at': observed['observed_at'], 'children': children,
            'still_running': alive, 'indeterminate': unknown}


def append_termination(con, run_id: int, evidence: dict | None) -> None:
    from conversation_broker import BrokerStore

    if evidence is None:
        return
    row = con.execute('SELECT conversation_id,trigger_message_id FROM conversation_runs WHERE run_id=?',
                      (run_id,)).fetchone()
    exists = con.execute(
        "SELECT 1 FROM conversation_events WHERE message_id=? AND event_type='run.process.ended' LIMIT 1",
        (row['trigger_message_id'],),
    ).fetchone()
    if exists is None:
        BrokerStore._append_event(con, conversation_id=row['conversation_id'],
                                  event_type='run.process.ended', payload=evidence,
                                  message_id=row['trigger_message_id'], run_id=run_id)
