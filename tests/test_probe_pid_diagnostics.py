"""Inert native command parser -> PID proof -> existing normal HTTP witness."""
import json
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.super-coder/scripts'), str(ROOT / '.super-coder/api')]
from conversation_runtime_contract import ProcessIdentity
from conversation_runtime_native_probes import _Scenarios
from gui_experiment_runtime import semantic_witness
from test_codex_command_attribution import emit, setup, snapshot
from test_codex_runtime import deadline
from test_codex_runtime import seat as actual_seat
from test_native_chat_ownership import database  # noqa: F401
from test_native_check_workflow import workflow  # noqa: F401


@pytest.fixture
def target(tmp_path):
    fixture = actual_seat.__wrapped__(tmp_path)
    driver, rpc, events, context = next(fixture)
    setup(rpc)
    emit(rpc, harmless={'future': True})
    rpc.frame('item/commandExecution/outputDelta', turn='command-turn', itemId='item-opaque', delta='TAG=200\n')
    result, work = snapshot(driver)
    proxy = SimpleNamespace(_lock=threading.RLock(), identity=result.identity, events=events,
        owned=SimpleNamespace(process_identity=lambda pid: ProcessIdentity(200, 2000) if pid == 200 else None),
        inventory=lambda **kwargs: driver.inventory(**kwargs))
    scenario = _Scenarios(proxy, frozenset({'submission'}), deadline())
    scenario._root_label = 'TAG'
    try:
        yield scenario, proxy, work, driver, rpc, context
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def detail(scenario):
    return semantic_witness(scenario.work_observation)['root_pid_qualification']


def test_actual_native_target_output_os_decisions_no_extra_rpc(target):
    scenario, _, work, _, rpc, _ = target
    before = list(rpc.calls)
    assert scenario._pid('TAG', work) == ProcessIdentity(200, 2000)
    value = detail(scenario)
    assert value == {'observation': 'observed', 'native_target': 'qualified', 'native_handle': 'observed',
        'terminal_record': 'exact_current_record', 'tag_line': 'observed', 'os_identity': 'matched',
        'exact_output_records': 1, 'complete_output_records': 1}
    assert rpc.calls == before
    assert not any(private in json.dumps(value) for private in ('TAG', 'opaque', 'command-turn', '2000'))


@pytest.mark.parametrize('change,decision', [
    ({'freshness': 'stale'}, 'not_current'), ({'state': 'completed'}, 'not_running'),
    ({'grade': 'inconclusive'}, 'grade_unproved'), ({'activity_id': None}, 'missing_activity'),
    ({'item_id': None}, 'missing_item'), ({'root_id': 'foreign'}, 'association_unproved')])
def test_unqualified_target_does_not_invent_output_absence(target, change, decision):
    scenario, _, work, *_ = target
    ref_fields = {'activity_id', 'item_id', 'root_id'}
    changed = replace(work, reference=replace(work.reference, **change)) if set(change) <= ref_fields else replace(work, **change)
    assert scenario._pid('TAG', changed) is None
    value = detail(scenario)
    assert value['native_target'] == decision and value['terminal_record'] == 'unobserved'
    assert value['tag_line'] == value['os_identity'] == 'unobserved'
    assert 'complete_output_records' not in value


@pytest.mark.parametrize('mutation,record,tag', [
    ({'text': 'unrelated complete\n'}, 'exact_current_record', 'not_in_complete_record'),
    ({'text': 'TAG=not-a-pid\n'}, 'exact_current_record', 'malformed'),
    ({'complete': False}, 'incomplete', 'unobserved'),
    ({'offset': 2}, 'conflicting', 'unobserved'),
    ({'kind': 'assistant'}, 'no_matching_record', 'unobserved')])
def test_exact_record_incomplete_and_assistant_decisions_preserve_refusal(target, mutation, record, tag):
    scenario, proxy, work, *_ = target
    output = next(event for event in proxy.events if event.kind == 'output.delta')
    proxy.events = [replace(output, data={**output.data, **mutation})]
    assert scenario._pid('TAG', work) is None
    value = detail(scenario)
    assert value['terminal_record'] == record and value['tag_line'] == tag
    assert value['os_identity'] == 'unobserved'


@pytest.mark.parametrize('callback,expected', [(None, 'unavailable'), (lambda pid: None, 'no_owned_match')])
def test_no_linux_cause_from_missing_callback_or_identity(target, callback, expected):
    scenario, proxy, work, *_ = target
    proxy.owned.process_identity = callback
    assert scenario._pid('TAG', work) is None
    assert detail(scenario)['os_identity'] == expected


def test_overflow_is_unavailable_without_changing_existing_proof(target):
    scenario, proxy, work, *_ = target
    output = next(event for event in proxy.events if event.kind == 'output.delta')
    proxy.events = [output] * 129
    assert scenario._pid('TAG', work) == ProcessIdentity(200, 2000)
    assert detail(scenario) == {'observation': 'overflow'}


def test_inventory_aggregation_does_not_publish_last_row_as_all_rows(target):
    scenario, proxy, work, driver, _, _ = target
    current = driver.inventory(deadline=deadline())
    other = replace(work, reference=replace(work.reference, activity_id=None, item_id='other'))
    proxy.inventory = lambda **_: replace(current, work=(work, other))
    assert scenario._terminal_pid('TAG', 'root') == (work, ProcessIdentity(200, 2000))
    value = detail(scenario)
    assert value['native_target'] == 'mixed' and value['terminal_record'] == 'mixed'
    assert 'complete_output_records' not in value  # Unexamined row is not a measured zero.


@pytest.mark.parametrize('bad', [True, -1, 129, float('nan'), '1'])
def test_scalar_boundary_malformed_counts_unavailable(target, bad):
    scenario, _, work, *_ = target
    scenario._pid('TAG', work)
    raw = dict(scenario.work_observation)
    raw['root_pid_qualification'] = {**raw['root_pid_qualification'], 'complete_output_records': bad, 'private': 'TAG=200'}
    assert semantic_witness(raw)['root_pid_qualification'] == {'observation': 'unavailable'}


def test_current_normal_http_witness_clamps_and_displaced_generation_clears(target, request, monkeypatch):
    import conversation_routes as routes
    import gui_experiment_runtime as runtime
    from test_native_check_witness import bind_probe
    scenario, _, work, *_ = target
    scenario._pid('TAG', work)
    value, operation, report, con, first, projection = bind_probe(request.getfixturevalue('workflow'))
    report['behavior_witness'] = dict(scenario.work_observation)
    report['behavior_witness']['root_pid_qualification']['private'] = 'TAG=200'
    monkeypatch.setattr(routes, 'DB_PATH', operation.database)
    monkeypatch.setattr(runtime, '_FIXTURE', SimpleNamespace(workflow=value))
    def get():
        response = routes.handle('GET', '/api/conversations/native-checks/'+first['check_id'], 'Host: localhost:8800\r\n', b'')
        assert response[0] == 200
        return json.loads(response[2])
    current = get()
    assert current['behavior_witness']['root_pid_qualification']['os_identity'] == 'matched'
    assert 'TAG=200' not in json.dumps(current)
    assert not current['admissible'] and not current['grades'] and not current['cleanup']
    projection['generation_id'] = 'replacement'
    con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='probe-cv'", (json.dumps(projection),))
    con.commit()
    assert get()['behavior_witness'] is None


@pytest.mark.parametrize('harness', ['codex', 'claude'])
def test_shared_probe_content_changes_actual_consumed_digest_and_old_cache_key(tmp_path, target, harness):
    from conversation_runtime_checks import (
        CapabilityEvidence,
        EvidenceCache,
        Fingerprint,
    )
    from gui_experiment_native_seat import NativeFixtureSeat
    from test_conversation_runtime_checks import fingerprint
    source = NativeFixtureSeat.__new__(NativeFixtureSeat)
    # Only the bootstrap is supplied independently; no native executable runs.
    source.root = tmp_path
    source.source_lock = threading.RLock()
    source.source_cache = {}
    scripts = tmp_path/'.super-coder/scripts'
    shutil.copytree(ROOT/'.super-coder', tmp_path/'.super-coder')
    (tmp_path/'fixture_bootstrap.py').write_text('captured inert bootstrap\n')
    before = source.implementation_digest(harness)
    probe = scripts/'conversation_runtime_native_probes.py'
    probe.write_bytes(probe.read_bytes()+b'\n# inert content mutation\n')
    after = source.implementation_digest(harness)
    assert before != after
    original = replace(fingerprint(target[-1]), implementation_digest=before, harness=harness)
    current: Fingerprint = replace(original, implementation_digest=after)
    cache = EvidenceCache()
    cache.put(original, CapabilityEvidence('submission', 'incompatible'))
    assert cache.get(original, 'submission') is not None
    assert cache.get(current, 'submission') is None
