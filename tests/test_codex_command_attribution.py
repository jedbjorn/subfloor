"""Source-only consumed native command frames -> inventory -> strict probe proof."""
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
from conversation_runtime_contract import ProcessIdentity, RuntimeContractError
from conversation_runtime_native_probes import _Scenarios
from test_codex_runtime import deadline
from test_codex_runtime import seat as _codex_seat


@pytest.fixture
def seat(tmp_path):
    yield from _codex_seat.__wrapped__(tmp_path)


def command(*, turn='command-turn', item='item-opaque', process='opaque', status='inProgress', **additions):
    return turn, {'type': 'commandExecution', 'id': item, 'processId': process, 'status': status, **additions}


def emit(rpc, **kwargs):
    turn, value = command(**kwargs)
    rpc.frame('item/started', turn=turn, item=value)


def setup(rpc):
    rpc.turns['root'] = [{'id': 'command-turn', 'status': 'completed'}]
    rpc.terminals['root'] = [{'processId': 'opaque', 'itemId': 'item-opaque'}]


def snapshot(driver):
    result = driver.inventory(deadline=deadline())
    return result, next(w for w in result.work if w.kind == 'terminal' and w.reference.thread_id == 'root')


def test_original_actual_driver_path_now_qualifies_exact_command_pid(seat):
    # Same event path as immutable 42b084/c144709, with corrected assertion.
    driver, rpc, events, _ = seat
    setup(rpc)
    emit(rpc, harmless={'addition': True})
    rpc.frame('item/commandExecution/outputDelta', turn='command-turn', itemId='item-opaque', delta='TAG=200\n')
    result, work = snapshot(driver)
    assert result.freshness == 'current' and not result.partial
    assert work.reference.activity_id == 'command-turn'
    proxy = SimpleNamespace(_lock=threading.RLock(), identity=result.identity, events=events,
        owned=SimpleNamespace(process_identity=lambda pid: ProcessIdentity(200, 2000) if pid == 200 else None))
    scenario = _Scenarios(proxy, frozenset({'submission'}), deadline())
    scenario._root_label = 'TAG'
    assert scenario._pid('TAG', work) == ProcessIdentity(200, 2000)
    assert scenario.work_observation['root_pid_observation'] == 'matched'
    # Attribution cannot weaken the prior exact event/item/turn checks.
    for ref in [replace(work.reference, activity_id='other'), replace(work.reference, item_id='other'),
                replace(work.reference, thread_id='other'), replace(work.reference, root_id='other')]:
        assert scenario._pid('TAG', replace(work, reference=ref)) is None
    output = next(event for event in events if event.kind == 'output.delta')
    for invalid in [replace(output, partial=True), replace(output, freshness='stale'),
                    replace(output, data={**output.data, 'kind': 'assistant'})]:
        proxy.events = [invalid]
        assert scenario._pid('TAG', work) is None


def test_late_native_command_frame_requires_new_inventory(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    _, first = snapshot(driver)
    assert first.reference.activity_id is None
    emit(rpc)
    rpc.frame('item/commandExecution/outputDelta', turn='command-turn', itemId='item-opaque', delta='TAG=200\n')
    _, later = snapshot(driver)
    assert first.reference.activity_id is None and later.reference.activity_id == 'command-turn'


def test_qualified_thread_read_item_preserves_completed_foreground_command_origin(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    _, value = command(extra_future_field={'unused': True})
    rpc.turns['root'][0]['items'] = [value, {'type': 'futureUnusedItem'}]
    result, work = snapshot(driver)
    assert not result.partial and work.reference.activity_id == 'command-turn'
    assert result.primary is None  # Never borrow a current/active foreground turn.


@pytest.mark.parametrize('changed', [
    {'turn': None}, {'turn': 42}, {'turn': 'x'*513}, {'process': None}, {'process': 42},
    {'process': ''}, {'process': 'x'*513}, {'status': None}, {'status': {}}, {'status': 'future'},
    {'status': 'completed'}, {'status': 'failed'}, {'item': None}, {'item': 42}, {'item': 'x'*513},
])
def test_missing_malformed_or_finished_command_cannot_supply_activity(seat, changed):
    driver, rpc, _, _ = seat
    setup(rpc)
    if any(isinstance(value, str) and len(value) > 255 for value in changed.values()):
        with pytest.raises(RuntimeContractError, match='bounded opaque'):
            emit(rpc, **changed)
    else:
        emit(rpc, **changed)
    _, work = snapshot(driver)
    assert work.reference.activity_id is None


def test_initial_unassigned_handle_can_later_observe_exact_native_binding(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc, process=None)
    assert snapshot(driver)[1].reference.activity_id is None
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id == 'command-turn'


@pytest.mark.parametrize('changed', [{'turn': 'other'}, {'process': 'other'}, {'status': {}}, {'turn': None}])
def test_conflicting_or_malformed_later_frame_withdraws_binding_and_replay_cannot_restore(seat, changed):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id == 'command-turn'
    emit(rpc, **changed)
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id is None


def test_duplicate_native_replay_is_idempotent_and_foreign_thread_does_not_supply_root_binding(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    rpc.frame('item/started', thread='foreign', turn='command-turn', item=command()[1])
    assert snapshot(driver)[1].reference.activity_id is None
    emit(rpc)
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id == 'command-turn'
    assert len(driver._command_activity) == 1


def test_same_opaque_handle_on_different_item_is_ambiguous(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    emit(rpc, item='other-item')
    assert snapshot(driver)[1].reference.activity_id is None


def test_different_process_or_thread_inventory_cannot_reuse_binding(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    rpc.terminals['root'][0]['processId'] = 'other-process'
    assert snapshot(driver)[1].reference.activity_id is None
    rpc.child('child')
    rpc.terminals['child'] = [{'processId': 'opaque', 'itemId': 'item-opaque'}]
    observed = driver.inventory(deadline=deadline())
    assert next(w for w in observed.work if w.kind == 'terminal' and w.reference.thread_id == 'child').reference.activity_id is None


@pytest.mark.parametrize('completion', ['completed', 'failed', 'interrupted', 'declined'])
def test_command_finish_tombstone_refuses_late_replayed_frame_and_stale_read_item(seat, completion):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    rpc.frame('item/completed', turn='command-turn', item=command(status=completion)[1])
    emit(rpc)
    rpc.turns['root'][0]['items'] = [command()[1]]
    assert snapshot(driver)[1].reference.activity_id is None


def test_completion_during_terminal_list_cannot_leave_attributed_running_snapshot(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    original = rpc.request
    def race(method, params, *, deadline):
        value = original(method, params, deadline=deadline)
        if method == 'thread/backgroundTerminals/list':
            rpc.frame('item/completed', turn='command-turn', item=command(status='completed')[1])
        return value
    rpc.request = race
    assert snapshot(driver)[1].reference.activity_id is None


@pytest.mark.parametrize('items', [None, {}, [42]])
def test_malformed_consumed_read_items_remain_partial(seat, items):
    driver, rpc, _, _ = seat
    setup(rpc)
    rpc.turns['root'][0]['items'] = items
    assert snapshot(driver)[0].partial is True


def test_pending_capacity_refuses_new_observation_but_preserves_qualified_control_and_recovers(seat, monkeypatch):
    import conversation_adapters.codex_runtime as codex
    driver, rpc, _, _ = seat
    setup(rpc)
    monkeypatch.setattr(codex, 'MAX_TRACKED', 2)
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id == 'command-turn'
    emit(rpc, item='pending-one', process='pending-one')
    emit(rpc, item='pending-two', process='pending-two')
    emit(rpc, item='new', process='new')
    rpc.terminals['root'].append({'itemId': 'new', 'processId': 'new'})
    result, existing = snapshot(driver)
    assert existing.reference.activity_id == 'command-turn'
    new = next(w for w in result.work if w.reference.item_id == 'new')
    assert new.reference.activity_id is None and new.data['activity_attribution'] == 'pending_capacity'
    emit(rpc, item='pending-one', process='pending-one', status='completed')
    snapshot(driver)  # Complete native absence prunes the finished pending entry.
    emit(rpc, item='new', process='new')
    result, _ = snapshot(driver)
    assert next(w for w in result.work if w.reference.item_id == 'new').reference.activity_id == 'command-turn'
    assert len(driver._command_pending) <= 2 and len(driver._command_activity) <= 2


def test_background_capacity_refuses_only_new_identities_and_retains_tombstones(seat, monkeypatch):
    import conversation_adapters.codex_runtime as codex
    driver, rpc, _, _ = seat
    setup(rpc)
    monkeypatch.setattr(codex, 'MAX_TRACKED', 2)
    emit(rpc)
    snapshot(driver)
    emit(rpc, item='second', process='second')
    rpc.terminals['root'].append({'itemId': 'second', 'processId': 'second'})
    snapshot(driver)
    emit(rpc, status='completed')
    rpc.terminals['root'].pop(0)
    emit(rpc, item='third', process='third')
    rpc.terminals['root'].append({'itemId': 'third', 'processId': 'third'})
    result = driver.inventory(deadline=deadline())
    second = next(w for w in result.work if w.reference.item_id == 'second')
    third = next(w for w in result.work if w.reference.item_id == 'third')
    assert second.reference.activity_id == 'command-turn'
    assert third.reference.activity_id is None and third.data['activity_attribution'] == 'background_capacity'
    assert len(driver._command_activity) == 2
    driver.cleanup(deadline=deadline())
    assert driver._command_activity == {} and driver._command_pending == {}
    fresh = codex.CodexRuntimeDriver(rpc_factory=lambda **kwargs: None)
    assert fresh._command_activity == {} and fresh._command_pending == {}


def test_many_finished_foreground_commands_do_not_enroll_or_permanently_consume_capacity(seat, monkeypatch):
    import conversation_adapters.codex_runtime as codex
    driver, rpc, _, _ = seat
    setup(rpc)
    rpc.terminals['root'].clear()
    monkeypatch.setattr(codex, 'MAX_TRACKED', 2)
    for number in range(8):
        emit(rpc, item=str(number), process=str(number))
        emit(rpc, item=str(number), process=str(number), status='completed')
        result = driver.inventory(deadline=deadline())
        assert not result.partial and driver._command_pending == {} and driver._command_activity == {}
    setup(rpc)
    emit(rpc)
    assert snapshot(driver)[1].reference.activity_id == 'command-turn'


def test_command_completion_during_thread_read_prevents_stale_items_from_reviving_binding(seat):
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    rpc.turns['root'][0]['items'] = [command()[1]]
    original = rpc.request
    def race(method, params, *, deadline):
        value = original(method, params, deadline=deadline)
        if method == 'thread/read':
            rpc.frame('item/completed', turn='command-turn', item=command(status='completed')[1])
        return value
    rpc.request = race
    result, work = snapshot(driver)
    assert result.partial and work.reference.activity_id is None


def test_attributed_terminal_preserves_exact_expected_activity_fence(seat):
    from conversation_runtime_contract import NativeControl
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    target = snapshot(driver)[1].reference
    missing = NativeControl('missing', 1, 'digest', 'stop_work', target)
    assert driver.control(missing, deadline=deadline()).state == 'rejected'
    assert not any(method == 'thread/backgroundTerminals/terminate' for method, _ in rpc.calls)
    exact = replace(missing, control_id='exact', expected_activity_id=target.activity_id)
    assert driver.control(exact, deadline=deadline()).state == 'written'


def test_current_inventory_withdrawal_refuses_old_attributed_control_without_native_write(seat):
    from conversation_runtime_contract import NativeControl
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    target = snapshot(driver)[1].reference
    emit(rpc, turn='conflicting-turn')
    assert snapshot(driver)[1].reference.activity_id is None
    command = NativeControl('stale', 1, 'digest', 'stop_work', target, target.activity_id)
    assert driver.control(command, deadline=deadline()).state == 'rejected'
    assert not any(method == 'thread/backgroundTerminals/terminate' for method, _ in rpc.calls)


def test_control_read_edge_completion_refuses_previously_attributed_target(seat):
    from conversation_runtime_contract import NativeControl
    driver, rpc, _, _ = seat
    setup(rpc)
    emit(rpc)
    target = snapshot(driver)[1].reference
    original = rpc.request
    def race(method, params, *, deadline):
        value = original(method, params, deadline=deadline)
        if method == 'thread/backgroundTerminals/list':
            rpc.frame('item/completed', turn='command-turn', item=command(status='completed')[1])
        return value
    rpc.request = race
    intent = NativeControl('stale', 1, 'digest', 'stop_work', target, target.activity_id)
    assert driver.control(intent, deadline=deadline()).state == 'rejected'
    assert not any(method == 'thread/backgroundTerminals/terminate' for method, _ in rpc.calls)


def test_real_factory_terminal_call_site_supplies_exact_observed_activity(tmp_path):
    from conversation_runtime_checks import CleanupProof, CompatibilityChecker
    from conversation_runtime_native_probes import NativeProbeFactory
    from test_conversation_runtime_native_probes import fingerprint, owned
    actual = owned.__wrapped__(tmp_path)
    actual = replace(actual, context=replace(actual.context,
        probe_capabilities=('submission', 'stop_work', 'stop_work_terminal')))
    original = actual.client.request
    def native_fence(op, *, timeout, **fields):
        if op == 'control' and fields['command']['target'].get('native_process_id'):
            command = fields['command']
            # Same consumed native driver equality fence independently exercised above.
            if command['target'].get('activity_id') != command.get('expected_activity_id'):
                return {'state': 'rejected', 'acknowledged': False}
        return original(op, timeout=timeout, **fields)
    actual.client.request = native_fence
    factory = NativeProbeFactory(lambda *args: actual, lambda *args: CleanupProof(True, 'complete'))
    checker = CompatibilityChecker()
    result = checker.request(fingerprint(actual), observed_interface={'fixed': True},
        requirements={'submission': {'fixed': True}, 'stop_work': {'fixed': True}},
        factory=factory, seconds=2.4).result(timeout=4)
    assert result.evidence['submission'].grade == 'compatible'
    assert 'target:terminal' in result.evidence['stop_work'].coverage
    intents = [fields['command'] for op, fields, _ in actual.client.calls if op == 'control']
    assert len(intents) == 1 and intents[0]['expected_activity_id'] == intents[0]['target']['activity_id']


def test_explicit_unobserved_activity_refuses_while_existing_none_legacy_boundary_remains(seat):
    from conversation_runtime_contract import NativeControl
    driver, rpc, _, _ = seat
    setup(rpc)
    target = snapshot(driver)[1].reference
    assert target.activity_id is None
    invented = replace(target, activity_id='not-observed')
    assert driver.control(NativeControl('unproved', 1, 'digest', 'stop_work', invented, invented.activity_id),
        deadline=deadline()).state == 'rejected'
    assert not any(method == 'thread/backgroundTerminals/terminate' for method, _ in rpc.calls)
    # Existing native handle-only control contract stays unchanged; this
    # component action does not earn PID/OS/behavioral capability evidence.
    assert driver.control(NativeControl('legacy', 2, 'digest', 'stop_work', target),
        deadline=deadline()).state == 'written'
