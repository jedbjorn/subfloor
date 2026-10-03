"""Manual observation/automatic publication share one bound intent; no native IO."""
import dataclasses
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.super-coder/scripts'), str(ROOT / '.super-coder/api')]
import conversation_native_checks as checks
from conversation_runtime_contract import RuntimeContractError
from test_gui_experiment_probe_owner import owner  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401


@pytest.fixture
def pending_manual(request, monkeypatch):
    probe_owner, initial, con, events = request.getfixturevalue('owner')
    fp = dataclasses.replace(initial, model='gpt-6.1-sol')
    operation = SimpleNamespace(database=probe_owner.database, future=None,
        seat=SimpleNamespace(candidate_fingerprint=lambda **_: fp), owner=probe_owner,
        retained_guard=lambda: None)
    workflow = checks.NativeChecks(operation)
    workflow._current_owner = lambda: True
    class HoldThread:
        def __init__(self, **_): pass
        def start(self): pass
    monkeypatch.setattr(checks.threading, 'Thread', HoldThread)
    report = {'state': 'running', 'fingerprint': None, 'probe': None, 'grades': {}}
    operation.status = lambda: report
    manual = workflow.create(1, 'manual', checks.CODEX_SELECTION)
    assert workflow.beginning and manual['state'] == 'accepted'
    return workflow, operation, probe_owner, fp, con, events, manual


def test_automatic_defers_before_manual_binding_then_owned_probe_has_unique_intent(pending_manual):
    workflow, _, probe_owner, fp, con, events, manual = pending_manual
    assert workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key) is None
    row = con.execute('SELECT * FROM conversation_runtime_check_requests').fetchone()
    assert json.loads(row['result_json']) == {}  # No guessed fingerprint/admission.
    workflow._bind(manual['check_id'], fp.key)
    assert workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key)['check_id'] == manual['check_id']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0] == 1
    prepare = probe_owner.seat.prepare
    def same_candidate(*args, **kwargs):
        context, _, native = prepare(*args, **kwargs)
        return dataclasses.replace(context, model=fp.model), fp, native
    probe_owner.seat.prepare = same_candidate
    probe = probe_owner.allocate(fp, frozenset({'submission'}), time.monotonic() + 5)
    assert probe.context.model == fp.model and events == ['git', 'prepare', 'launch']
    runtime = json.loads(con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',
                                     (probe.context.conversation_id,)).fetchone()[0])
    assert runtime['check_id'] == manual['check_id']


def test_new_installed_candidate_is_not_certified_by_unbound_manual(pending_manual):
    workflow, operation, _, fp, con, _, manual = pending_manual
    newer = dataclasses.replace(fp, executable=dataclasses.replace(fp.executable, sha256='f' * 64))
    operation.seat.candidate_fingerprint = lambda **_: newer
    assert workflow.enqueue_automatic(checks.CODEX_SELECTION, newer.key) is None
    assert json.loads(con.execute('SELECT result_json FROM conversation_runtime_check_requests').fetchone()[0]) == {}
    workflow._bind(manual['check_id'], fp.key)
    queued = workflow.enqueue_automatic(checks.CODEX_SELECTION, newer.key)
    assert queued['check_id'] != manual['check_id']
    row = con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?', (queued['check_id'],)).fetchone()
    assert json.loads(row[0])['fingerprint'] == newer.key
    assert not json.loads(row[0])['admissible']


@pytest.mark.parametrize('edge', ['not_current', 'not_beginning', 'different_selection', 'wrong_hash'])
def test_deferral_does_not_adopt_stale_or_unowned_intent(pending_manual, edge):
    workflow, _, _, fp, con, _, _ = pending_manual
    if edge == 'not_current': workflow.current = None
    elif edge == 'not_beginning': workflow.beginning = False
    elif edge == 'different_selection':
        con.execute('UPDATE conversation_runtime_check_requests SET selection_json=?', (json.dumps(checks.CLAUDE_SELECTION),))
    else: con.execute("UPDATE conversation_runtime_check_requests SET request_hash='wrong'")
    con.commit()
    queued = workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key)
    assert queued['state'] == 'accepted' and not queued.get('admissible', False)
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0] == 2


def test_current_service_and_current_candidate_guards_still_refuse(pending_manual):
    workflow, _, _, fp, con, _, _ = pending_manual
    workflow._current_owner = lambda: False
    assert workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key) is None
    workflow._current_owner = lambda: True
    with pytest.raises(RuntimeContractError) as failure:
        workflow.enqueue_automatic(checks.CODEX_SELECTION, 'f' * 64)
    assert failure.value.code == 'CHECK_CANDIDATE_CHANGED'
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0] == 1


def test_automatic_first_manual_click_refuses_without_creating_second_intent(pending_manual):
    workflow, _, _, fp, con, _, _ = pending_manual
    con.execute('DELETE FROM conversation_runtime_check_requests'); con.commit()
    workflow.current = None; workflow.beginning = False
    automatic = workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key)
    with pytest.raises(RuntimeContractError) as failure:
        workflow.create(1, 'operator-after-auto', checks.CODEX_SELECTION)
    assert failure.value.code == 'CLEANUP_PENDING'
    assert workflow.current is None and not workflow.beginning
    readback = workflow.get(1, check_id=automatic['check_id'])
    assert readback['state'] == 'accepted' and not readback['admissible']
    assert readback['origin'] == 'installed_change'
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0] == 1


def test_service_replacement_during_observation_does_not_defer_or_enqueue(pending_manual):
    workflow, operation, _, fp, con, _, _ = pending_manual
    current = [True]
    workflow._current_owner = lambda: current[0]
    def replacement(**_):
        current[0] = False
        return fp
    operation.seat.candidate_fingerprint = replacement
    assert workflow.enqueue_automatic(checks.CODEX_SELECTION, fp.key) is None
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0] == 1
