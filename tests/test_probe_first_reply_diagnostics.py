"""Inert JSONL/native-parser/probe/witness observations; no executable or RPC launch."""
import json
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_adapters.codex_runtime import JsonlRpc
from conversation_runtime_contract import NativeSubmission
from conversation_runtime_native_probes import _Scenarios
from gui_experiment_runtime import semantic_witness
from test_codex_runtime import deadline, submission
from test_codex_runtime import seat as actual_seat
from test_native_chat_ownership import database  # noqa: F401
from test_native_check_workflow import workflow  # noqa: F401


@pytest.fixture
def seat(tmp_path):
    yield from actual_seat.__wrapped__(tmp_path)


class InertReader:
    """Actual reader implementation, finite in-memory feed, no Popen/pipe."""
    def __init__(self, driver):
        self.queue = queue.Queue()
        self.handled = 0
        self.guard = threading.Condition()
        self.loss = False
        class Stream:
            def readline(inner, limit):
                return self.queue.get(timeout=1)
        self.rpc = JsonlRpc.__new__(JsonlRpc)
        self.rpc.process = SimpleNamespace(stdout=Stream())
        self.rpc._lock = threading.Lock()
        self.rpc._pending = {}
        self.rpc._lost = self.rpc._closing = False
        def observe(value):
            driver._message(value)
            with self.guard:
                self.handled += 1
                self.guard.notify_all()
        def loss(reason):
            driver._loss(reason)
            with self.guard:
                self.loss = True
                self.guard.notify_all()
        self.rpc._on_message, self.rpc._on_loss = observe, loss
        self.thread = threading.Thread(target=self.rpc._read)
        self.thread.start()

    def feed(self, frames):
        goal = self.handled + len(frames)
        for frame in frames:
            self.queue.put((json.dumps(frame)+'\n').encode())
        with self.guard:
            assert self.guard.wait_for(lambda: self.handled >= goal or self.loss, timeout=1)

    def close(self):
        self.rpc._closing = True
        self.queue.put(b'')
        self.thread.join(timeout=1)
        assert not self.thread.is_alive()


def frame(method, activity, **params):
    return {'method': method, 'params': {'threadId': 'root', 'turnId': activity, **params}}


def scenario_for(driver, events, context, receipt):
    proxy = SimpleNamespace(_lock=threading.RLock(), identity=driver._identity, events=events,
        harness='codex', owned=SimpleNamespace(context=context), diagnostics=[])
    scenario = _Scenarios(proxy, frozenset({'submission'}), deadline())
    scenario.first_request, scenario.first_receipt = 'request', receipt
    return scenario


@pytest.mark.parametrize('before_ack', [False, True])
@pytest.mark.parametrize('terminal_first', [False, True])
def test_actual_reader_first_turn_snapshot_before_normalization(seat, before_ack, terminal_first):
    driver, rpc, events, context = seat
    reader = InertReader(driver)
    def rows(turn):
        start = frame('turn/started', turn, turn={'id': turn, 'status': 'inProgress'})
        refused = frame('item/completed', turn, item={'type': 'agentMessage', 'id': 'bad', 'text': 42})
        final = frame('item/completed', turn, item={'type': 'agentMessage', 'id': 'reply', 'text': 'READY source_nonce'})
        terminal = frame('turn/completed', turn, turn={'id': turn, 'status': 'completed', 'new': True})
        return [start, refused, *([terminal, final] if terminal_first else [final, terminal])]
    try:
        if before_ack:
            rpc.on_submit = lambda turn: reader.feed(rows(turn))
        receipt = driver.submit(submission(), deadline=deadline())
        if not before_ack:
            reader.feed(rows(receipt.native_activity_id))
        scenario = scenario_for(driver, events, context, receipt)
        assert scenario._successful_reply('request', receipt, 'READY source_nonce')
        safe = semantic_witness(scenario.witness())
        assert safe['first_rpc_observation'] == 'observed'
        assert safe['first_rpc_counts'] == {'turn_started': 1, 'turn_completed': 1,
            'item_completed': 2, 'status_completed': 1, 'normalization_refused': 1}
        assert safe['first_output_counts']['assistant_final'] == 1
        assert safe['first_command_receipt'] == 'written' and safe['first_command_acknowledged'] is True
        assert len(driver._unbound) == 0
        assert sum(e.kind == 'activity.processed' for e in events) == 1
        assert not any(method == 'thread/read' for method, _ in rpc.calls)
        assert 'source_nonce' not in json.dumps(safe) and 'request' not in json.dumps(safe)
    finally:
        reader.close()


def test_missing_emit_is_not_observed_zero_and_foreign_unbound_frames_do_not_count(seat):
    driver, rpc, events, context = seat
    receipt = driver.submit(submission(), deadline=deadline())
    driver._message(frame('item/agentMessage/delta', receipt.native_activity_id, delta=42))
    driver._message(frame('item/completed', 'other', item={'id': 'other', 'type': 'agentMessage', 'text': 'other'}))
    driver._message({'method': 'turn/completed', 'params': []})
    driver._message({'method': 'item/agentMessage/delta', 'params': {'threadId': 'foreign',
        'turnId': receipt.native_activity_id, 'delta': 'foreign'}})
    safe = semantic_witness(scenario_for(driver, events, context, receipt).witness())
    assert safe['first_rpc_observation'] == 'unobserved' and 'first_rpc_counts' not in safe
    # A later existing event reveals the prior consumed malformed delta,
    # without adding an event merely to publish diagnostic state.
    rpc.frame('turn/started', turn={'id': receipt.native_activity_id, 'status': 'inProgress'})
    safe = semantic_witness(scenario_for(driver, events, context, receipt).witness())
    assert safe['first_rpc_counts'] == {'turn_started': 1, 'assistant_delta': 1, 'normalization_refused': 1}


@pytest.mark.parametrize('mutation', ['generation', 'root', 'request', 'activity', 'stale', 'binding'])
def test_snapshot_generation_root_request_activity_and_currentness_fences(seat, mutation):
    driver, rpc, events, context = seat
    receipt = driver.submit(submission(), deadline=deadline())
    rpc.frame('turn/started', turn={'id': receipt.native_activity_id, 'status': 'inProgress'})
    scenario = scenario_for(driver, events, context, receipt)
    if mutation == 'generation':
        scenario.owned.context = replace(context, generation_id='replacement')
    elif mutation == 'binding':
        scenario.driver.events = [replace(e, data={**e.data, 'first_rpc_binding_sha256': '0'*64}) for e in events]
    elif mutation == 'stale':
        scenario.driver.events = [replace(e, freshness='stale') for e in events]
    else:
        for event in tuple(events):
            if not event.reference:
                continue
            changed = replace(event, request_id='different') if mutation == 'request' else replace(event,
                reference=replace(event.reference, **{('root_id' if mutation == 'root' else 'activity_id'): 'different'}))
            scenario.driver.events = [changed]
    safe = semantic_witness(scenario.witness())
    assert safe['first_rpc_observation'] in {'unobserved', 'unavailable'}
    assert 'first_rpc_counts' not in safe and 'first_output_counts' not in safe


def test_overflow_omits_all_counts_without_changing_native_processing(seat):
    driver, rpc, events, context = seat
    receipt = driver.submit(submission(), deadline=deadline())
    for _ in range(129):
        rpc.frame('item/agentMessage/delta', turn=receipt.native_activity_id, itemId='reply', delta='x')
    safe = semantic_witness(scenario_for(driver, events, context, receipt).witness())
    assert safe['first_rpc_observation'] == safe['first_output_observation'] == 'overflow'
    assert 'first_rpc_counts' not in safe and 'first_output_counts' not in safe
    assert driver._probe_counts['assistant_delta'] == 128
    assert sum(e.kind == 'output.delta' for e in events) == 129
    assert not driver._lost


def test_ordinary_and_second_commands_have_no_diagnostic_metadata(seat):
    driver, rpc, events, context = seat
    driver._context = replace(context, probe_capabilities=(), capability_evidence={'submission': 'compatible'})
    receipt = driver.submit(submission(), deadline=deadline())
    rpc.frame('turn/started', turn={'id': receipt.native_activity_id, 'status': 'inProgress'})
    assert all(not any(k.startswith('first_rpc') for k in e.data) for e in events)
    assert driver._probe_counts == {}


@pytest.mark.parametrize('phase', ['startup', 'acknowledgement', 'first_turn', 'post_first_turn'])
def test_existing_loss_event_static_phase_and_cleanup_does_not_emit(seat, phase):
    driver, rpc, events, _context = seat
    if phase != 'startup':
        if phase == 'acknowledgement':
            rpc.on_submit = lambda turn: driver._loss('private synthetic reason not exported')
        receipt = driver.submit(submission(), deadline=deadline())
        if phase == 'post_first_turn':
            rpc.frame('turn/completed', turn={'id': receipt.native_activity_id, 'status': 'completed'})
    if phase != 'acknowledgement':
        driver._loss('private synthetic reason not exported')
    loss = next(e for e in reversed(events) if e.kind == 'runtime.lost')
    assert loss.data['probe_lost_phase'] == phase
    before = len(events)
    driver.cleanup(deadline=deadline())
    assert len(events) == before or all(e.kind != 'diagnostic' for e in events[before:])


@pytest.mark.parametrize('value', [None, [], {}, {'turn_started': True}, {'turn_started': -1},
    {'turn_started': 129}, {'turn_started': '1'}, {'turn_started': float('nan')}])
def test_strict_public_allowlist_unavailable_malformed_snapshot(value):
    safe = semantic_witness({'first_rpc_observation': 'observed', 'first_rpc_counts': value,
        'first_rpc_binding_sha256': 'private', 'raw': 'secret'})
    assert safe['first_rpc_observation'] == 'unavailable' and 'first_rpc_counts' not in safe
    assert 'private' not in json.dumps(safe) and 'secret' not in json.dumps(safe)


def test_unknown_future_fields_do_not_widen_counts_or_loss_and_close_stays_independent():
    safe = semantic_witness({'first_rpc_observation': 'observed',
        'first_rpc_counts': {'turn_started': 1, 'future_payload': 'secret'},
        'first_command_receipt': 'raw exception', 'first_command_acknowledged': 1,
        'first_reader_loss': 'raw exception', 'first_native_lost_phase': 'raw exception',
        'first_close_observed': False, 'first_close_outcome': 'inconclusive'})
    assert safe['first_rpc_counts'] == {'turn_started': 1}
    assert 'first_command_receipt' not in safe and 'first_command_acknowledged' not in safe
    assert safe['first_close_outcome'] == 'inconclusive' and safe['first_close_observed'] is False


def test_second_turn_is_not_added_to_first_snapshot_and_unknown_write_no_binding(seat):
    driver, rpc, events, context = seat
    first = driver.submit(submission(), deadline=deadline())
    rpc.frame('turn/completed', turn={'id': first.native_activity_id, 'status': 'completed'})
    before = dict(driver._probe_counts)
    second = driver.submit(NativeSubmission('second', 2, 'digest', 'second'), deadline=deadline())
    rpc.frame('turn/started', turn={'id': second.native_activity_id, 'status': 'inProgress'})
    rpc.frame('item/agentMessage/delta', turn=second.native_activity_id, itemId='second-item', delta='second')
    assert driver._probe_counts == before
    assert all('first_rpc_observation' not in e.data for e in events if e.request_id == 'second')
    safe = semantic_witness(scenario_for(driver, events, context, first).witness())
    assert safe['first_rpc_counts'] == before


def test_unknown_ack_does_not_associate_buffered_turn_or_manufacture_native_counters(seat):
    driver, rpc, events, context = seat
    rpc.fail_method = 'turn/start'
    receipt = driver.submit(submission(), deadline=deadline())
    rpc.frame('turn/started', turn={'id': 'unacknowledged', 'status': 'inProgress'})
    safe = semantic_witness(scenario_for(driver, events, context, receipt).witness())
    assert safe['first_command_receipt'] == 'unknown'
    assert safe['first_command_acknowledged'] is False
    assert safe['first_rpc_observation'] == 'unobserved'
    assert 'first_rpc_counts' not in safe and 'first_output_counts' not in safe


def test_partial_normalized_output_is_counted_only_as_refused_diagnostic(seat):
    driver, rpc, events, context = seat
    receipt = driver.submit(submission(), deadline=deadline())
    rpc.frame('item/agentMessage/delta', turn=receipt.native_activity_id, delta='private-no-item')
    scenario = scenario_for(driver, events, context, receipt)
    safe = semantic_witness(scenario.witness())
    assert safe['first_output_counts']['refused_output'] == 1
    assert safe['first_output_counts']['assistant_delta'] == 0
    assert not scenario._successful_reply('request', receipt, 'private-no-item')
    scenario.driver.diagnostics = [SimpleNamespace(code='PROBE_READER_UNAVAILABLE')]
    assert semantic_witness(scenario.witness())['first_reader_loss'] == 'reader_unavailable'


@pytest.mark.parametrize('displaced', [False, True])
def test_reader_driver_probe_existing_normal_http_witness_no_grade_or_cleanup_upgrade(seat, request, monkeypatch, displaced):
    import conversation_routes as routes
    import gui_experiment_runtime as runtime
    from test_native_check_witness import bind_probe
    driver, rpc, events, context = seat
    context = replace(context, generation_id='probe-gen')
    driver._context = context
    reader = InertReader(driver)
    try:
        receipt = driver.submit(submission(), deadline=deadline())
        reader.feed([frame('turn/started', receipt.native_activity_id,
            turn={'id': receipt.native_activity_id, 'status': 'inProgress'})])
        value, operation, report, con, first, projection = bind_probe(request.getfixturevalue('workflow'))
        report['behavior_witness'] = scenario_for(driver, events, context, receipt).witness()
        if displaced:
            projection['generation_id'] = 'replacement'
            con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='probe-cv'",
                        (json.dumps(projection),))
            con.commit()
        monkeypatch.setattr(routes, 'DB_PATH', operation.database)
        monkeypatch.setattr(runtime, '_FIXTURE', SimpleNamespace(workflow=value))
        response = routes.handle('GET', '/api/conversations/native-checks/'+first['check_id'],
                                 'Host: localhost:8800\r\n', b'')
        assert response[0] == 200
        result = json.loads(response[2])
        if displaced:
            assert result['behavior_witness'] is None
        else:
            assert result['behavior_witness']['first_rpc_counts'] == {'turn_started': 1}
            assert result['behavior_witness']['first_command_receipt'] == 'written'
        assert not result['admissible'] and not result['grades'] and not result['cleanup']
        assert 'first_rpc_binding_sha256' not in json.dumps(result)
        assert not any(method == 'thread/read' for method, _ in rpc.calls)
    finally:
        reader.close()
