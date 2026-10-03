"""Preview core admission uses real checker/factory cleanup, without native I/O."""
# ruff: noqa: F811
from __future__ import annotations

import dataclasses
import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'),
                str(Path(__file__).resolve().parents[1] / '.super-coder/api')]
from conversation_runtime_checks import (
    CheckResult,
    CleanupProof,
    CompatibilityChecker,
    EvidenceCache,
)
from conversation_runtime_contract import RuntimeContractError, payload_digest
from conversation_runtime_native_probes import NativeProbeFactory
from test_conversation_runtime_native_probes import fingerprint, owned  # noqa: F401
from test_gui_experiment_native_seat import seat, seat_module  # noqa: F401
from test_gui_experiment_runtime import operation  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401


@pytest.mark.parametrize('operation', ['full', 'submission-preview'], indirect=True)
def test_only_captured_preview_filters_requirements(operation):
    value, fp, _, _ = operation
    captured = {}
    future = Future()
    value.checker.request = lambda actual, **kwargs: captured.update(kwargs) or future
    value.begin()
    expected = {'submission'} if value._behavioral_profile == 'submission-preview' else {'submission', 'stop_reply', 'stop_work'}
    assert set(captured['requirements']) == expected
    assert captured['requirements']['submission'] == {'native': True}
    assert captured['seconds'] <= 177
    assert value.fingerprint == fp
    future.set_result(CheckResult(fp, {}, CleanupProof(True, 'complete')))
    assert value.status()['grades']['stop_reply'] == 'unverified'
    assert value.status()['grades']['stop_work'] == 'unverified'


def test_preview_profile_is_policy_identity_for_candidate_probe_and_ordinary(seat):
    full, _, _ = seat
    preview = seat_module.NativeFixtureSeat(database=full.database, root=full.root,
        supervisor=full.supervisor, native_bindings=full.native_bindings, cache=full.cache,
        behavioral_profile='submission-preview')
    preview.observers = full.observers
    # Keep the existing full-profile policy operand byte-identical.
    assert full.settings_digest('codex') == payload_digest({
        'adapter': seat_module.run.load_adapter('codex'),
        'fixture_mcp': {'revision': 1, 'tools': ['fixture_identity', 'fixture_state']}})
    old = full.candidate_fingerprint('codex', 'gpt-6.1-sol', 'high')
    new = preview.candidate_fingerprint('codex', 'gpt-6.1-sol', 'high')
    assert old.key != new.key and old.settings_digest != new.settings_digest
    assert old.implementation_digest == new.implementation_digest
    context, actual, _ = preview.prepare('cv', 'generation', probe_capabilities=('submission',))
    assert actual == new and context.policy_digest == new.policy_digest
    assert {key:context.capability_evidence[key] for key in ('submission','stop_reply','stop_work','automation')} == {
        'submission': 'unverified', 'stop_reply': 'unverified', 'stop_work': 'unverified', 'automation': 'unverified'}
    assert context.capability_evidence['stop_work_terminal']=='inconclusive'
    assert context.capability_evidence['stop_work_child']=='inconclusive'
    with pytest.raises(AttributeError): preview.behavioral_profile = 'full'


@pytest.mark.parametrize('invalid', [None, True, [], {}, 'operator-custom'])
def test_profile_refuses_request_shaped_or_unknown_values(seat, invalid):
    full, _, _ = seat
    with pytest.raises(RuntimeContractError) as error:
        seat_module.NativeFixtureSeat(database=full.database, root=full.root,
            supervisor=full.supervisor, native_bindings=full.native_bindings, cache=full.cache,
            behavioral_profile=invalid)
    assert error.value.code == 'CONTEXT_INVALID'


@pytest.mark.parametrize('cleanup', ['complete', 'native_unknown', 'os_unknown'])
def test_real_submission_only_scenario_needs_both_cleanup_proofs(owned, cleanup):
    context = dataclasses.replace(owned.context, probe_capabilities=('submission',))
    owned = dataclasses.replace(owned, context=context)
    owned.client.context = context
    original = owned.client.request
    def request(op, **kwargs):
        if op == 'close' and cleanup == 'native_unknown':
            owned.client.calls.append((op,kwargs,kwargs['timeout']))
            return {'outcome': 'inconclusive', 'unresolved_work': [], 'unresolved_definitions': []}
        return original(op, **kwargs)
    owned.client.request = request
    allocated = []
    def allocate(fp, caps, deadline):
        allocated.append(caps)
        return owned
    factory = NativeProbeFactory(allocate, lambda fp, deadline: CleanupProof(cleanup != 'os_unknown', 'complete'))
    checker = CompatibilityChecker(cache=EvidenceCache())
    fp = fingerprint(owned)
    result = checker.request(fp, observed_interface={'fixed': True},
        requirements={'submission': {'fixed': True}}, factory=factory, seconds=3).result(timeout=4)
    assert allocated == [frozenset({'submission'})]
    commands = [fields['command'] for op, fields, _ in owned.client.calls if op == 'submit']
    assert len(commands) == 2 and 'remembered nonce' in commands[1]['text']
    assert 'fixture_state with marker' in commands[0]['text']
    assert not any(word in commands[0]['text'] for word in ('background terminal', 'Spawn exactly', 'time.sleep'))
    assert not any(op == 'control' for op, _, _ in owned.client.calls)
    assert sum(op == 'close' for op, _, _ in owned.client.calls) == 1
    admitted = cleanup == 'complete'
    assert result.evidence['submission'].grade == ('compatible' if admitted else 'inconclusive')
    assert ('owned_unit_cleanup' in result.evidence['submission'].coverage) is admitted
    assert checker.cache.admission(fp)['submission'] == ('compatible' if admitted else 'inconclusive')
    assert checker.cache.admission(fp)['stop_reply'] == 'unverified'
    assert checker.cache.admission(fp)['stop_work'] == 'unverified'
    # Full-profile behavioral evidence cannot be reused under the preview key.
    other = dataclasses.replace(fp, settings_digest=payload_digest({'different_profile': 'full'}))
    assert checker.cache.admission(other)['submission'] == 'unverified'
