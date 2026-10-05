"""Runs UI process evidence: real children, incarnation boundaries and replay."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder' / 'scripts'))
import run_processes
from conversation_adapters.base import SubprocessRunner, cleanup_owned_process
from conversation_broker import BrokerStore
from test_conversation_broker import ConversationBrokerCase


@pytest.fixture
def broker_case():
    case = ConversationBrokerCase('runTest')
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.fixture
def native_process(tmp_path):
    child_file = tmp_path / 'child.pid'
    source = ('import subprocess,time; from pathlib import Path; '
              f'p=subprocess.Popen([{sys.executable!r}, "-c", "import time; time.sleep(60)"], '
              'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
              f'Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)')
    process = SubprocessRunner().spawn([sys.executable, '-c', source], cwd=tmp_path, env=os.environ)
    deadline = time.monotonic() + 5
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert child_file.exists()
    try:
        yield process, int(child_file.read_text())
    finally:
        cleanup_owned_process(process, 1)
        process.stdout.close()
        process.stderr.close()


def bind_process(case, process, *, admin=False):
    cid, mid, rid = case.add_live_run(state='running')
    ticks = int(Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
    with case.connect() as con:
        BrokerStore._append_event(con, conversation_id=cid, event_type='message.accepted',
                                  payload={}, message_id=mid, run_id=None)
        con.execute('UPDATE conversation_runs SET process_pid=?,process_start_ticks=?,process_group_id=? WHERE run_id=?',
                    (process.pid, ticks, process.pid, rid))
        if admin:
            con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=1")
    return cid, mid, rid, ticks


def test_real_child_snapshot_and_broker_terminal_replay(broker_case, native_process):
    case = broker_case
    process, child_pid = native_process
    cid, mid, rid, ticks = bind_process(case, process)
    con = case.connect()
    try:
        observed = run_processes.observe(con, cid)
        assert {p['pid'] for p in observed['processes']} >= {process.pid, child_pid}
        assert observed['kind'] == 'native'
        assert observed['observed_at']
        assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
        assert run_processes.snapshot(process.pid, ticks + 1, process.pid)['processes'] == []
        process.send_signal(signal.SIGTERM)
        cleanup_owned_process(process, 1)
        deadline = time.monotonic() + 5
        while run_processes.termination_evidence(observed)['still_running'] and time.monotonic() < deadline:
            time.sleep(.01)
        store = BrokerStore(case.db_path)
        assert store.finish_run(rid, 'succeeded', event_type='run.completed', process_exited=True)
        assert not store.finish_run(rid, 'succeeded', event_type='run.completed', process_exited=True)
        evidence = [json.loads(r[0]) for r in con.execute(
            "SELECT payload FROM conversation_events WHERE message_id=? AND event_type='run.process.ended'", (mid,))]
        assert len(evidence) == 1
        assert evidence[0]['still_running'] == 0
        assert '1 child processes were still running' in evidence[0]['label']
        assert 'terminated with it' in evidence[0]['label']
    finally:
        con.close()


def test_lingering_release_records_loss_once(broker_case, native_process):
    case = broker_case
    process, _ = native_process
    cid, _, rid, _ = bind_process(case, process)
    with case.connect() as con:
        run_processes.observe(con, cid)
    store = BrokerStore(case.db_path)
    store.finish_run(rid, 'succeeded', event_type='run.completed', keep_process_link=True)
    cleanup_owned_process(process, 1)
    store.release_process_link(rid)
    store.release_process_link(rid)
    with case.connect() as con:
        assert con.execute("SELECT count(*) FROM conversation_events WHERE event_type='run.process.ended'").fetchone()[0] == 1


def test_live_child_is_never_reported_terminated(broker_case, native_process):
    case = broker_case
    process, _ = native_process
    cid, _, _, _ = bind_process(case, process)
    with case.connect() as con:
        observed = run_processes.observe(con, cid)
    evidence = run_processes.termination_evidence(observed)
    assert evidence['still_running'] == 1
    assert 'terminated with it' not in evidence['label']
    with mock.patch.object(run_processes, '_read_process', side_effect=PermissionError):
        evidence = run_processes.termination_evidence(observed)
    assert evidence['indeterminate'] == 1
    assert 'terminated with it' not in evidence['label']


def test_admin_and_unknown_process_do_not_produce_observations(broker_case, native_process):
    case = broker_case
    process, _ = native_process
    cid, _, rid, _ = bind_process(case, process, admin=True)
    with case.connect() as con:
        assert run_processes.observe(con, cid)['processes'] == []
        assert run_processes.last_snapshot(con, rid) is None
    with mock.patch.object(run_processes, '_read_process', side_effect=PermissionError):
        assert run_processes.snapshot(process.pid, 1, process.pid)['indeterminate'] == 1
    with mock.patch.object(run_processes.os, 'getuid', return_value=os.getuid() + 1):
        assert run_processes.snapshot(process.pid, 1, process.pid)['processes'] == []


def test_operator_filters_before_limit_and_shell_credentials_cannot_observe(broker_case, native_process, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder' / 'api'))
    import server
    from runs import RunStore

    case = broker_case
    process, _ = native_process
    cid, _, _, _ = bind_process(case, process)
    with case.connect() as con:
        con.execute("UPDATE shells SET api_key='runs-ui-owner' WHERE shell_id=1")
        con.commit()
        store = RunStore(con, case.root / 'engine')
        for i in range(202):
            row = store.register(1 if i < 201 else 2, {'registration_key': str(i),
                'argv': ['true'], 'cwd': str(case.worktree), 'label': str(i), 'kind': 'job'})
            con.execute('UPDATE runs SET state=?,wake_state=? WHERE run_id=?',
                        ('lost' if i == 0 else 'done', 'blocked' if i == 0 else 'none', row['run_id']))
            con.commit()
    monkeypatch.setattr(server, 'DB_PATH', str(case.db_path))
    monkeypatch.setattr(server, 'ENGINE', case.root / 'engine')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def get(path, token=None):
        req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}' + path,
                                     headers={'Authorization': f'Bearer {token}'} if token else {})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    try:
        fleet = get('/api/runs')
        assert len(fleet['runs']) == 200
        assert fleet['attention_count'] == 1
        filtered = get('/api/runs?state=lost&kind=job&shell_id=1')
        assert [r['label'] for r in filtered['runs']] == ['0']
        assert get('/api/runs?shell_id=2')['attention_count'] == 0
        owner = get('/_sc/runs?shell_id=2', 'runs-ui-owner')
        assert all(r['owner_shell_id'] == 1 for r in owner['runs'])
        with pytest.raises(urllib.error.HTTPError) as error:
            get(f'/api/runs/processes?conversation_id={cid}', 'runs-ui-owner')
        assert error.value.code == 403
        assert get(f'/api/runs/processes?conversation_id={cid}')['processes']
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def finish_native_turn(case, rid, process):
    from conversation_adapters.base import NativeTurn, NormalizedEvent
    from conversation_broker import ConversationBroker, _ActiveRun
    from test_conversation_broker import FakeAdapter

    store = BrokerStore(case.db_path)
    with case.connect() as con:
        run = store._run_from_row(con.execute(store._run_select() + ' WHERE r.run_id=?', (rid,)).fetchone())
    turn = NativeTurn('claude', 'fixture-session', 'fixture-turn', case.worktree, opaque=process)

    def stream():
        process.send_signal(signal.SIGTERM)
        turn.metadata['returncode'] = process.wait(timeout=5)
        yield NormalizedEvent('run.completed')

    broker = ConversationBroker(case.db_path, store=store)
    assert broker._consume_stream(_ActiveRun(run=run), FakeAdapter(), turn, source=stream)


def test_broker_stream_waits_for_existing_cleanup(broker_case, native_process, monkeypatch):
    from conversation_adapters import base

    case = broker_case
    process, _ = native_process
    cid, _, rid, _ = bind_process(case, process)
    with case.connect() as con:
        run_processes.observe(con, cid)
    original_cleanup = base.cleanup_owned_process

    def slow_cleanup(process, timeout):
        time.sleep(.1)
        original_cleanup(process, timeout)

    monkeypatch.setattr(base, 'cleanup_owned_process', slow_cleanup)
    finish_native_turn(case, rid, process)
    assert process._sc_conversation_cleanup_done.is_set()
    with case.connect() as con:
        evidence = json.loads(con.execute("SELECT payload FROM conversation_events WHERE event_type='run.process.ended'").fetchone()[0])
        assert evidence['still_running'] == 0
        assert 'terminated with it' in evidence['label']
