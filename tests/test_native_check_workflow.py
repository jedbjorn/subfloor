"""Durable cold-check HTTP intent/reconciliation; no native processes."""
import json
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_native_checks as checks
from conversation_runtime_contract import RuntimeContractError
from test_native_chat_ownership import database  # noqa: F401


@pytest.fixture
def workflow(request,monkeypatch):
    path,con=request.getfixturevalue('database')
    calls=[]
    report={'state':'running','fingerprint':'fp','grades':{},'probe':None,'diagnostic':None}
    operation=SimpleNamespace(database=path,future=Future(),
        seat=SimpleNamespace(root=path.parent,candidate_fingerprint=lambda **_:SimpleNamespace(key='fp')),
        service=SimpleNamespace(resolve_route=lambda **_:calls.append('resolve')),
        cache=SimpleNamespace(admission=lambda _: {'submission':'compatible'}),
        owner=SimpleNamespace(lock=threading.RLock(),allocating=set(),closing=set()),
        status=lambda:dict(report),retained_guard=lambda:None)
    def begin(*,selection,on_candidate):
        assert checks.configured_selection(selection)==selection
        assert con.execute("SELECT COUNT(*) FROM conversation_runtime_check_requests WHERE status='accepted'").fetchone()[0]==1
        on_candidate('fp')
        calls.append('begin')
    operation.begin=begin
    class InlineThread:
        def __init__(self,*,target,args=(),**_):self.target,self.args=target,args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(checks.threading,'Thread',InlineThread)
    value=checks.NativeChecks(operation)
    return value,operation,report,calls,con


def test_committed_intent_exact_key_readback_and_duplicate_do_not_restart(workflow):
    value,_,_,calls,con=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    assert first['state']=='running' and not first['admissible']
    duplicate=value.create(1,'stable',checks.CODEX_SELECTION)
    assert duplicate['check_id']==first['check_id'] and calls==['begin']
    assert value.get(1,request_key='stable')['check_id']==first['check_id']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==1
    with pytest.raises(RuntimeContractError,match='outside operator'):
        value.get(2,check_id=first['check_id'])
    with pytest.raises(RuntimeContractError) as exc:value.create(1,'replacement',checks.CODEX_SELECTION)
    assert exc.value.code=='CLEANUP_PENDING' and calls==['begin']


def test_restart_retained_request_never_discovers_or_replaces(workflow):
    value,operation,_,calls,_=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    restarted=checks.NativeChecks(operation)
    retained=restarted.get(1,request_key='stable')
    assert retained['state']=='retained' and not retained['retry_allowed']
    assert retained['diagnostics'][0]['code']=='CHECK_CONSUMER_RESTARTED'
    assert restarted.create(1,'stable',checks.CODEX_SELECTION)['check_id']==first['check_id']
    with pytest.raises(RuntimeContractError) as exc:restarted.create(1,'new',checks.CODEX_SELECTION)
    assert exc.value.code=='CLEANUP_PENDING' and calls==['begin']


@pytest.mark.parametrize('selection',[checks.CODEX_SELECTION,checks.CLAUDE_SELECTION])
def test_restart_can_read_exact_bound_probe_after_http_ack_is_lost(workflow,selection):
    value,operation,_,calls,con=workflow
    first=value.create(1,'stable',selection)
    runtime={'role':'probe','check_id':first['check_id'],'generation_id':'owned-generation','state':'needs_consent','setup':{'setup_id':'owned-phase'}}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'PROBE','Probe','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe-cv',2,1,?,?,?,'/synthetic/probe','probe-key','fp','native_experiment',?)",(selection['harness'],selection['model'],selection['effort'],json.dumps(runtime)))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','probe-cv','owned-generation','preparing',100,1)")
    con.commit()
    result=checks.NativeChecks(operation).get(1,request_key='stable')
    assert result['check_id']==first['check_id']
    assert result['selection']==selection
    assert result['probe']['generation_id']=='owned-generation' and result['probe']['setup']['setup_id']=='owned-phase'
    assert not result['admissible'] and calls==['begin']


@pytest.mark.parametrize('failure',['native','allocating','closing','capacity','variant','changed','resolver'])
def test_start_and_new_check_require_matching_cleanup_and_owner_admission(workflow,failure):
    value,operation,report,_,_=workflow
    first=value.create(1,'stable',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete','unresolved_work':[],'unresolved_definitions':[]},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    if failure=='native':report['cleanup']['native_outcome']='inconclusive'
    elif failure=='allocating':report['resources']['owner_allocating']=True
    elif failure=='closing':report['resources']['owner_closing']=True
    elif failure=='capacity':report['resources']['owned_capacity_released']=False
    elif failure=='variant':report['grades']['submission']='inconclusive'
    elif failure=='changed':operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='replacement')
    elif failure=='resolver':
        def unavailable(**_):raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','missing ordinary route proof')
        operation.service.resolve_route=unavailable
    result=value.get(1,check_id=first['check_id'])
    assert not result['admissible']
    assert result['retry_allowed']==(failure in {'variant','changed','resolver'})


def test_completed_scoped_cleanup_allows_fresh_explicit_intent_and_old_result_is_stable(workflow):
    value,_,report,calls,con=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    completed=value.get(1,check_id=first['check_id'])
    assert completed['admissible'] and completed['retry_allowed']
    second=value.create(1,'second',checks.CODEX_SELECTION)
    assert second['check_id']!=first['check_id'] and calls.count('begin')==2
    before=con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(first['check_id'],)).fetchone()[0]
    value.refresh(first['check_id'])
    after=con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(first['check_id'],)).fetchone()[0]
    assert json.loads(before)==json.loads(after)


def test_historical_admission_is_rechecked_after_installed_identity_changes(workflow):
    value,operation,report,_,_=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    assert value.get(1,check_id=first['check_id'])['admissible']
    operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='replacement')
    readback=checks.NativeChecks(operation).get(1,request_key='first')
    assert not readback['admissible'] and readback['retry_allowed']


def test_failed_discovery_only_releases_intent_after_empty_completed_begin(workflow):
    value,operation,_,_,_=workflow
    operation.future=None
    def unavailable(**_):raise RuntimeContractError('NATIVE_EXECUTABLE_INCONCLUSIVE','missing native identity')
    operation.begin=unavailable
    first=value.create(1,'first',checks.CODEX_SELECTION)
    assert first['state']=='complete' and first['retry_allowed'] and not first['admissible']
    operation.owner.allocating.add('pending')
    second=value.create(1,'second',checks.CODEX_SELECTION)
    assert second['state']=='retained' and not second['retry_allowed']


def test_replacement_operation_cannot_rebind_original_check_or_admit_it(workflow):
    value,operation,report,_,con=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='replacement',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='replacement')
    result=value.get(1,check_id=first['check_id'])
    assert result['fingerprint']=='fp' and not result['admissible'] and not result['retry_allowed']
    with pytest.raises(RuntimeContractError) as exc:
        value._save(first['check_id'],'complete',{'fingerprint':'replacement','admissible':True})
    assert exc.value.code=='CHECK_CANDIDATE_CHANGED'
    assert json.loads(con.execute('SELECT result_json FROM conversation_runtime_check_requests').fetchone()[0])['fingerprint']=='fp'


@pytest.mark.parametrize('change',['owner','shell_owner','mode','role','generation','check','candidate'])
def test_restart_refuses_unbound_or_foreign_probe_descriptor(workflow,change):
    value,operation,_,_,con=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    con.execute("INSERT INTO users(user_id,username) VALUES(2,'foreign')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'P','Probe','dev','synthetic',?)",(2 if change=='shell_owner' else 1,))
    runtime={'role':'probe','check_id':first['check_id'],'generation_id':'probe-gen','state':'needs_consent','setup':{'setup_id':'scoped'}}
    if change=='role':runtime['role']='chat'
    if change=='generation':runtime['generation_id']='replacement'
    if change=='check':runtime['check_id']='another-check'
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe-cv',2,?,'codex','gpt-6.1-sol','high','/synthetic/probe','probe-key',?,?,?)",
        (2 if change=='owner' else 1,'other' if change=='candidate' else 'fp','ephemeral' if change=='mode' else 'native_experiment',json.dumps(runtime)))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','probe-cv','probe-gen','preparing',100,1)");con.commit()
    result=checks.NativeChecks(operation).get(1,request_key='first')
    assert result['probe'] is None and not result['admissible'] and not result['retry_allowed']


def test_historical_retry_waits_for_newer_check_to_finish(workflow):
    value,_,report,_,_=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    assert value.get(1,check_id=first['check_id'])['retry_allowed']
    report.update(state='running',grades={},cleanup={},resources={})
    value.create(1,'second',checks.CODEX_SELECTION)
    assert not value.get(1,check_id=first['check_id'])['retry_allowed']


def test_old_consumer_cannot_regress_completed_check_cleanup(workflow):
    value,_,report,_,con=workflow
    first=value.create(1,'first',checks.CODEX_SELECTION)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    value.get(1,check_id=first['check_id'])
    saved=con.execute('SELECT status,result_json FROM conversation_runtime_check_requests').fetchone()
    value._save(first['check_id'],'retained',{'fingerprint':'fp','probe':None,'retry_allowed':False})
    assert tuple(con.execute('SELECT status,result_json FROM conversation_runtime_check_requests').fetchone())==tuple(saved)


def test_claude_requested_candidate_has_no_availability_or_grade(workflow):
    value,_,_,_,_=workflow
    candidates=value.config()['candidates']
    assert [row['harness'] for row in candidates]==['codex','claude']
    assert all(row['proof_state']=='requested_candidate' and row['grades']=={} for row in candidates)
    assert checks.configured_selection(checks.CLAUDE_SELECTION)==checks.CLAUDE_SELECTION
    assert checks.configured_selection(checks.CLAUDE_SELECTION) is not checks.CLAUDE_SELECTION


@pytest.mark.parametrize('body',[
    {'harness':'claude','model':'claude-sonnet-5-5','effort':'low'},
    {'harness':'claude','model':'other','effort':'high'},
    {**checks.CLAUDE_SELECTION,'provider':'anthropic'},
    {**checks.CLAUDE_SELECTION,'argv':[]},
    {**checks.CLAUDE_SELECTION,'setup_id':'borrowed'},
])
def test_unconfigured_claude_request_refuses_before_dispatch(workflow,body):
    value,_,_,calls,con=workflow
    with pytest.raises(RuntimeContractError) as exc:value.create(1,'key',body)
    assert exc.value.code=='CHECK_SELECTION_INVALID'
    assert calls==[] and con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==0


def test_claude_selection_is_durable_and_never_rebound_to_codex(workflow):
    value,operation,_,calls,con=workflow
    dispatched=[]
    original=operation.begin
    def begin(*,selection,on_candidate):
        dispatched.append(dict(selection));original(selection=selection,on_candidate=on_candidate)
    operation.begin=begin
    first=value.create(1,'claude-key',checks.CLAUDE_SELECTION)
    assert first['selection']==checks.CLAUDE_SELECTION and dispatched==[checks.CLAUDE_SELECTION]
    assert json.loads(con.execute('SELECT selection_json FROM conversation_runtime_check_requests').fetchone()[0])==checks.CLAUDE_SELECTION
    with pytest.raises(RuntimeContractError) as exc:value.create(1,'claude-key',checks.CODEX_SELECTION)
    assert exc.value.code=='CHECK_IDEMPOTENCY_CONFLICT' and dispatched==[checks.CLAUDE_SELECTION]
    restarted=checks.NativeChecks(operation)
    assert restarted.get(1,request_key='claude-key')['selection']==checks.CLAUDE_SELECTION
    assert calls==['begin']


def test_claude_start_rechecks_selected_fingerprint_and_resolver(workflow):
    value,operation,report,_,_=workflow
    first=value.create(1,'claude-key',checks.CLAUDE_SELECTION)
    selections=[]
    operation.seat.candidate_fingerprint=lambda **selection:selections.append(selection) or SimpleNamespace(key='fp')
    operation.service.resolve_route=lambda **selection:selections.append(selection)
    report.update(state='complete',fingerprint='fp',grades={'submission':'compatible'},
        cleanup={'unit_verified_exited':True,'native_outcome':'complete'},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    assert value.get(1,check_id=first['check_id'])['admissible']
    assert selections and all(selection==checks.CLAUDE_SELECTION for selection in selections)
    operation.seat.candidate_fingerprint=lambda **_:SimpleNamespace(key='codex-or-replacement')
    assert not value.get(1,check_id=first['check_id'])['admissible']


@pytest.mark.parametrize('captured',[True,False])
def test_submission_preview_exposes_only_captured_codex_without_readiness_claim(workflow,monkeypatch,captured):
    import conversation_native_chats
    value,operation,_,calls,_=workflow
    operation.seat.behavioral_profile='submission-preview'
    operation.seat.native_bindings={'CODEX':'/inert/captured-codex'} if captured else {}
    operation.service.stopped=threading.Event()
    operation.service.database=operation.database
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',operation.service)
    config=value.config()
    assert config['enabled'] is True and config['preview_profile']=='submission-preview'
    assert [row['harness'] for row in config['candidates']]==(['codex'] if captured else [])
    assert all(row['model']=='gpt-6.1-sol' and row['effort']=='high'
               and row['grades']=={} and row['proof_state']=='requested_candidate'
               and row['automatic_check'] is None for row in config['candidates'])
    assert calls==[]


def test_full_fixture_keeps_existing_requested_candidates_and_default_profile(workflow):
    value,operation,_,calls,_=workflow
    operation.seat.behavioral_profile='full'
    operation.seat.native_bindings={'CODEX':'/inert/captured-codex'}
    config=value.config()
    assert 'preview_profile' not in config
    assert [row['harness'] for row in config['candidates']]==['codex','claude']
    assert calls==[]


@pytest.mark.parametrize('binding',[None,True,123,'','relative/path','/inert/path\n'])
def test_preview_malformed_captured_binding_offers_no_route(workflow,binding):
    value,operation,_,calls,_=workflow
    operation.seat.behavioral_profile='submission-preview'
    operation.seat.native_bindings={'CODEX':binding}
    assert value.config()['candidates']==[] and calls==[]
