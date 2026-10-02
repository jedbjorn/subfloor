"""Independent terminal target proof and exact parsed-output PID attribution."""
# Imported pytest fixtures intentionally share names with their test arguments.
# ruff: noqa: F811
from __future__ import annotations

import dataclasses
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.super-coder/scripts'), str(ROOT / '.super-coder/api')]
import conversation_native_checks
import conversation_routes
from conversation_adapters.codex_runtime import CodexRuntimeDriver
from conversation_native_chats import NativeChatsService, project_event
from conversation_runtime_checks import CleanupProof, CompatibilityChecker
from conversation_runtime_contract import (
    NativeReference,
    NativeWork,
    ProcessIdentity,
    RuntimeContractError,
    RuntimeEvent,
    RuntimeIdentity,
)
from conversation_runtime_native_probes import NativeProbeFactory, _Scenarios
from test_conversation_runtime_native_probes import (  # noqa: F401
    fingerprint,
    owned,
    start,
)
from test_native_check_workflow import database, workflow  # noqa: F401


@pytest.mark.parametrize('mutation', ['assistant', 'partial', 'stale', 'foreign_root', 'foreign_thread',
    'foreign_item', 'foreign_turn', 'missing_item', 'missing_turn', 'wrong_process', 'stale_work',
    'inconclusive_work', 'missing_kind', 'incomplete_line', 'positive'])
def test_pid_uses_actual_parsed_current_terminal_item_not_assistant_echo(owned, mutation):
    driver = start(owned)
    parser = CodexRuntimeDriver()
    parser._identity = RuntimeIdentity('root')
    parser._parents = {'root': None}
    events = []
    parser._emit = events.append
    params = {'threadId': 'root', 'turnId': 'turn', 'itemId': 'terminal', 'delta': 'TAG=200\n'}
    method = 'item/commandExecution/outputDelta'
    if mutation == 'incomplete_line': params['delta'] = 'TAG=200'
    if mutation == 'assistant': method = 'item/agentMessage/delta'
    if mutation == 'foreign_thread':
        params['threadId'] = 'foreign'
        parser._parents['foreign'] = 'root'
    if mutation == 'foreign_item': params['itemId'] = 'another'
    if mutation == 'foreign_turn': params['turnId'] = 'another'
    if mutation == 'missing_item': params.pop('itemId')
    if mutation == 'missing_turn': params.pop('turnId')
    parser._message({'method': method, 'params': params})
    event = events[-1]
    if mutation == 'partial': event = replace(event, partial=True)
    if mutation == 'stale': event = replace(event, freshness='stale')
    if mutation == 'wrong_process': event = replace(event, reference=replace(event.reference, native_process_id='foreign'))
    if mutation == 'missing_kind': event = replace(event, data={'text': 'TAG=200\n'})
    if mutation == 'foreign_root': event = replace(event, reference=replace(event.reference, root_id='foreign'))
    ref = NativeReference('root', 'root', activity_id='turn', item_id='terminal',
                          work_id='opaque', native_process_id='opaque')
    target = NativeWork(ref, 'terminal', 'inProgress', 'fixture', time.time(), freshness='current')
    if mutation == 'stale_work': target = replace(target, freshness='stale')
    if mutation == 'inconclusive_work': target = replace(target, grade='inconclusive')
    owned.client.pids[200] = ProcessIdentity(200, 2000)
    with driver._lock: driver.events = [event]
    scenario = _Scenarios(driver, frozenset({'stop_work'}), time.monotonic()+1)
    scenario._root_label = 'TAG'
    try:
        assert (scenario._pid('TAG', target) is not None) == (mutation == 'positive')
        witness = scenario.witness()
        assert witness['root_tagged_pid_candidates'] == (1 if mutation == 'positive' else 0)
        assert 'TAG=' not in json.dumps(witness)
    finally:
        driver.cleanup(deadline=time.monotonic()+1)


@pytest.mark.parametrize('child_gap', ['missing_ancestry', 'missing_output', 'no_owned_match'])
@pytest.mark.parametrize('cleanup', ['complete', 'native_unknown', 'os_unknown'])
def test_independent_controls_survive_child_gap_only_after_matching_full_cleanup(owned, child_gap, cleanup):
    request = owned.client.request
    event = owned.client.event
    def emit(event_kind, ref, request_id=None, **data):
        if child_gap == 'missing_output' and ref and ref.thread_id == 'child' and event_kind.startswith('output.'):
            # Model narration still echoes the child PID; it cannot qualify.
            return event('output.final', replace(ref, item_id='assistant'), request_id,
                         text=data.get('text', ''), kind='assistant')
        return event(event_kind, ref, request_id, **data)
    owned.client.event = emit
    def rpc(op, **fields):
        result = request(op, **fields)
        if op == 'snapshot' and child_gap == 'missing_ancestry':
            result['work'] = [w for w in result['work'] if w['kind'] != 'child']
        if op == 'close' and cleanup == 'native_unknown': result['outcome'] = 'inconclusive'
        return result
    owned.client.request = rpc
    actual = replace(owned, process_identity=lambda pid:
                     None if child_gap == 'no_owned_match' and pid == 201 else owned.client.pids.get(pid))
    factory = NativeProbeFactory(lambda *args: actual,
                                lambda *args: CleanupProof(cleanup != 'os_unknown', 'complete'))
    fp = fingerprint(actual)
    checker = CompatibilityChecker()
    result = checker.request(fp, observed_interface={'fixed': True},
        requirements={cap: {'fixed': True} for cap in ('submission', 'stop_reply', 'stop_work')},
        factory=factory, seconds=2.4).result(timeout=4)
    controls = [c[1]['command'] for c in actual.client.calls if c[0] == 'control']
    assert [c['action'] for c in controls] == ['stop_reply', 'stop_work']
    assert controls[1]['target']['thread_id'] == 'root'
    grades = checker.cache.admission(fp)
    expected = 'compatible' if cleanup == 'complete' else 'inconclusive'
    assert result.evidence['submission'].grade == result.evidence['stop_reply'].grade == expected
    assert grades['stop_work_terminal'] == expected and grades['stop_work_child'] == 'inconclusive'
    assert ('target:terminal' in result.evidence['stop_work'].coverage) == (cleanup == 'complete')
    assert not any('target:child' in e.coverage for e in result.evidence.values())
    witness = factory.witness(fp)
    assert witness['sibling_owned_pid_matches'] == 1
    assert witness['waiting_stage'] in {'child_ancestry', 'child_tagged_pid'}
    if child_gap == 'missing_output': assert witness['child_tagged_pid_candidates'] == 0
    if child_gap == 'no_owned_match':
        assert witness['child_tagged_pid_candidates'] == 1 and witness['child_pid_observation'] == 'no_owned_match'
    assert actual.client.marker not in json.dumps(witness)


def test_cleanup_bound_variant_publishes_through_normal_http_and_service_projection(owned, request, tmp_path):
    actual = replace(owned, process_identity=lambda pid: None if pid == 201 else owned.client.pids.get(pid))
    factory = NativeProbeFactory(lambda *args: actual, lambda *args: CleanupProof(True, 'complete'))
    fp = fingerprint(actual)
    checker = CompatibilityChecker()
    checker.request(fp, observed_interface={'fixed': True},
        requirements={cap: {'fixed': True} for cap in ('submission', 'stop_work')},
        factory=factory, seconds=2.4).result(timeout=4)
    grades = checker.cache.admission(fp)
    assert grades['stop_work_terminal'] == 'compatible' and grades['stop_work_child'] == 'inconclusive'
    value, operation, report, _, con = request.getfixturevalue('workflow')
    operation.cache = checker.cache
    operation.seat.candidate_fingerprint = lambda **_: fp
    operation.begin = lambda *, selection, on_candidate: on_candidate(fp.key)
    first = value.create(1, 'stable', conversation_native_checks.CODEX_SELECTION)
    report.update(state='complete', fingerprint=fp.key, grades=grades,
        cleanup={'native_outcome': 'complete', 'unit_verified_exited': True},
        resources={'owned_capacity_released': True, 'owner_allocating': False, 'owner_closing': False})
    public = value.get(1, check_id=first['check_id'])
    assert public['admissible'] and public['grades']['stop_work_terminal'] == 'compatible'
    assert public['grades']['stop_work_child'] == 'inconclusive'
    NativeChatsService.require_capability(public['grades'], 'stop_work_terminal')
    with pytest.raises(RuntimeContractError): NativeChatsService.require_capability(public['grades'], 'stop_work_child')
    request.getfixturevalue('monkeypatch').undo()
    # Actual normal service captures this admission in reserve/start. No native
    # process is launched: only the owner supervisor/client boundary is fake.
    path = operation.database
    con.execute('DELETE FROM conversation_runtime_generations')
    con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now') WHERE conversation_id='cv'")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('ordinary-cv',1,1,'codex',?,?,?,?,'normal-key','normal-hash','native_experiment',?)",
        (actual.context.provider,actual.context.model,actual.context.effort,str(actual.context.worktree),
         json.dumps({'role': 'ordinary', 'state': 'preparing', 'generation_id': 'ordinary-g'}))); con.commit()
    context = replace(actual.context, generation_id='ordinary-g', conversation_id='ordinary-cv',
                      capability_evidence=grades, probe_capabilities=())
    launched = []
    service = NativeChatsService(path, path.parent, SimpleNamespace(launch=launched.append),
        prepare_context=lambda *args: (context, fp, {}))
    service.attach = lambda _: (SimpleNamespace(open=lambda _: {'state': 'ready'}), {}, False)
    try:
        service.open_generation('ordinary-cv', 'ordinary-g')
        assert launched == ['ordinary-g']
        captured = json.loads(con.execute('SELECT binding_json FROM conversation_runtime_generations').fetchone()[0])
        assert captured['supervision']['captured_capabilities']['stop_work_terminal'] == 'compatible'
        assert captured['supervision']['captured_capabilities']['stop_work_child'] == 'inconclusive'
        lease = service.store.attach('ordinary-g', 1, 1, service.consumer)
        events = [RuntimeEvent('runtime.ready', NativeReference('root', 'root'), grade='compatible'),
                  RuntimeEvent('work.observed', NativeReference('root', 'root', activity_id='turn', item_id='terminal', work_id='opaque', native_process_id='opaque'), data={'kind':'terminal','state':'inProgress'}),
                  RuntimeEvent('work.observed', NativeReference('root', 'child', 'root', 'child-turn', work_id='child'), data={'kind':'child','state':'inProgress'})]
        service.store.ingest('ordinary-g', 1, 1, lease,
            {'events':[{'sequence':n,'event':dataclasses.asdict(e)} for n,e in enumerate(events,1)]}, project=project_event)
        cv = conversation_routes._conversation_projection(conversation_routes._conversation_row(con, 'ordinary-cv', 1), con=con)
        assert cv['runtime']['capabilities']['stop_work_terminal'] == 'compatible'
        assert cv['runtime']['capabilities']['stop_work_child'] == 'inconclusive'
        (tmp_path/'normal-projection.json').write_text(json.dumps({'version':cv['version'],'state':cv['state'],
            'runtime':{k:cv['runtime'][k] for k in ('state','generation_id','capabilities','work')}}))
    finally:
        service.shutdown()


def test_child_wait_exhaustion_retains_terminal_proof_and_cleanup_reserve(owned):
    # Same bounded checker path as the 177-second owner budget, scaled to a
    # short source test. No callback fails early: child PID observation waits
    # to its finite deadline after root controls have already completed.
    actual = replace(owned, process_identity=lambda pid: None if pid == 201 else owned.client.pids.get(pid))
    cleanup_remaining = []
    def cleanup(fp, deadline):
        cleanup_remaining.append(deadline-time.monotonic())
        return CleanupProof(True, 'complete')
    factory = NativeProbeFactory(lambda *args: actual, cleanup)
    checker = CompatibilityChecker()
    fp = fingerprint(actual)
    began = time.monotonic()
    result = checker.request(fp, observed_interface={'fixed': True},
        requirements={cap: {'fixed': True} for cap in ('submission','stop_reply','stop_work')},
        factory=factory, seconds=3).result(timeout=4)
    assert 1.5 <= time.monotonic()-began < 3
    assert cleanup_remaining and cleanup_remaining[0] >= .8
    assert result.cleanup.complete
    assert checker.cache.admission(fp)['stop_work_terminal'] == 'compatible'
    assert checker.cache.admission(fp)['stop_work_child'] == 'inconclusive'
    assert result.evidence['stop_reply'].grade == 'compatible'
    assert factory.witness(fp)['waiting_stage'] == 'child_tagged_pid'
    controls = [c for c in actual.client.calls if c[0]=='control']
    assert [c[1]['command']['action'] for c in controls] == ['stop_reply','stop_work']


def test_completed_terminal_measurement_cannot_admit_after_cache_publication_failure(owned):
    actual = replace(owned, context=replace(owned.context,
        probe_capabilities=('submission','stop_work','stop_work_terminal')))
    factory = NativeProbeFactory(lambda *args: actual, lambda *args: CleanupProof(True,'complete'))
    checker = CompatibilityChecker()
    put = checker.cache.put
    def publication(fp, evidence):
        if evidence.capability == 'stop_work':
            raise RuntimeContractError('TEST_PUBLICATION_FAILED','synthetic publication refusal')
        put(fp, evidence)
    checker.cache.put = publication
    fp = fingerprint(actual)
    result = checker.request(fp, observed_interface={'fixed':True},
        requirements={cap:{'fixed':True} for cap in ('submission','stop_work')},
        factory=factory, seconds=2).result(timeout=3)
    assert result.cleanup.complete and result.evidence['submission'].grade == 'compatible'
    assert result.evidence['stop_work'].grade == 'inconclusive'
    assert any(d.code == 'CHECK_CACHE_PUBLICATION_FAILED' for d in result.evidence['stop_work'].diagnostics)
    assert checker.cache.admission(fp)['stop_work_terminal'] == 'inconclusive'
    assert [c[1]['command']['action'] for c in actual.client.calls if c[0]=='control'] == ['stop_work']


@pytest.mark.parametrize('collateral', ['os_identity', 'native_item'])
def test_terminal_stop_requires_independent_root_sibling_os_and_native_retention(owned, collateral):
    actual = replace(owned, context=replace(owned.context,
        probe_capabilities=('submission','stop_work','stop_work_terminal')))
    request = actual.client.request
    stopped = False
    def rpc(op, **fields):
        nonlocal stopped
        result = request(op, **fields)
        if op == 'control' and fields['command']['action'] == 'stop_work':
            stopped = True
            if collateral == 'os_identity': actual.client.pids.pop(201, None)
        if op == 'snapshot' and stopped and collateral == 'native_item':
            result['work'] = [w for w in result['work'] if w['reference']['native_process_id'] != 'opaque-sibling']
        return result
    actual.client.request = rpc
    driver = start(actual)
    try:
        result = NativeProbeFactory._exercise(driver, frozenset({'stop_work'}), time.monotonic()+2)
        evidence = result['stop_work']
        assert 'target:terminal' not in evidence.coverage
        assert any(d.capability == 'stop_work_terminal' and d.grade == 'incompatible'
                   for d in evidence.diagnostics)
        assert 200 not in actual.client.pids and 77 in actual.client.pids
    finally:
        driver.cleanup(deadline=time.monotonic()+1)
