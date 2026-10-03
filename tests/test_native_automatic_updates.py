"""Automatic update intents over real migrated rows; no native execution."""
# ruff: noqa: F811 - imported pytest fixtures are resolved by parameter name
import json
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]

import conversation_native_chats
import db_driver
from conversation_native_checks import CODEX_SELECTION, NativeChecks
from conversation_runtime_contract import RuntimeContractError
from gui_experiment_chats import FixtureChats
from test_gui_experiment_native_seat import seat  # noqa: F401
from test_gui_experiment_runtime import operation  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401
from test_native_check_workflow import workflow  # noqa: F401

FP='a'*64


@pytest.fixture
def automatic(workflow,monkeypatch):
    value,op,report,calls,con=workflow
    op.service.database=op.database
    op.service.stopped=threading.Event()
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',op.service)
    op.supervisor=SimpleNamespace(preparation_identity=lambda **kw: {'pid':1,'start_ticks':2})
    op.retained_guard=lambda:None
    op.seat.candidate_fingerprint=lambda **kw:SimpleNamespace(key=FP,harness=kw['harness'])
    op.seat.require_loaded_source=lambda fp:None
    capacity=[1]
    op.seat.probe_capacity=lambda harness:capacity[0]
    report['fingerprint']=FP
    op.future=None
    op.workflow=value
    def begin(*,selection,on_candidate,deadline,expected_fingerprint,validate_intent):
        assert selection==CODEX_SELECTION and expected_fingerprint==FP
        validate_intent();on_candidate(FP);calls.append(('begin',deadline));op.future=Future()
    op.begin=begin
    return value,op,report,calls,con,capacity


def test_duplicate_tick_intent_and_readback_do_not_redispatch(automatic):
    value,_,_,calls,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    assert value.enqueue_automatic(CODEX_SELECTION,FP)['check_id']==first['check_id']
    value.dispatch_automatic();value.dispatch_automatic()
    assert len(calls)==1 and calls[0][0]=='begin'
    result=value.get(1,check_id=first['check_id'])
    assert result['origin']=='installed_change' and '_automatic' not in result
    assert 'consumer' not in json.dumps(result)
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==1


def test_restart_dispatches_only_positive_queued_not_claimed_intent(automatic):
    value,op,_,calls,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    recovered=NativeChecks(op)
    assert recovered.get(1,check_id=first['check_id'])['state']=='accepted'
    recovered.dispatch_automatic()
    assert len(calls)==1
    restarted=NativeChecks(op)
    assert restarted.get(1,check_id=first['check_id'])['state']=='retained'
    restarted.dispatch_automatic()
    assert len(calls)==1


def test_no_room_keeps_queue_then_dispatches_when_real_capacity_returns(automatic):
    value,_,_,calls,_,capacity=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP);capacity[0]=0
    value.dispatch_automatic()
    assert calls==[] and value.get(1,check_id=first['check_id'])['state']=='accepted'
    capacity[0]=1;value.dispatch_automatic()
    assert len(calls)==1


@pytest.mark.parametrize('gap',['job','owner','schema'])
def test_retained_ownership_blocks_before_dispatch(automatic,gap):
    value,op,_,calls,con,_=automatic
    value.enqueue_automatic(CODEX_SELECTION,FP)
    if gap=='job':
        con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('old','cv','g','preparing',9999999999,1)");con.commit()
    else:
        def retained():raise RuntimeContractError('CLEANUP_PENDING','retained child')
        op.retained_guard=retained
    value.dispatch_automatic();assert calls==[]


@pytest.mark.parametrize('gap',['changed','uninstalled','source'])
def test_final_claim_observation_cannot_authorize_stale_owner_or_content(automatic,monkeypatch,gap):
    value,op,_,calls,_,_=automatic
    value.enqueue_automatic(CODEX_SELECTION,FP)
    if gap=='source':
        def loaded(fp):raise RuntimeContractError('LOADED_SOURCE_CHANGED','new API lifetime required')
        op.seat.require_loaded_source=loaded
    else:
        count=[0]
        def candidate(**kw):
            count[0]+=1
            if count[0]==2 and gap=='uninstalled':monkeypatch.setattr(conversation_native_chats,'_SERVICE',None)
            return SimpleNamespace(key='b'*64 if count[0]==2 and gap=='changed' else FP,harness='codex')
        op.seat.candidate_fingerprint=candidate
    value.dispatch_automatic();assert calls==[]


def test_same_inconclusive_identity_is_not_heavy_retried_every_tick(automatic):
    value,op,_,calls,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    value._save(first['check_id'],'complete',{'admissible':False,'retry_allowed':True})
    for _ in range(3):
        assert value.enqueue_automatic(CODEX_SELECTION,FP)['check_id']==first['check_id']
        value.dispatch_automatic()
    assert calls==[]
    value.note_unavailable('codex')
    next_check=value.enqueue_automatic(CODEX_SELECTION,FP)
    assert next_check['check_id']!=first['check_id']
    assert value.enqueue_automatic(CODEX_SELECTION,FP)['check_id']==next_check['check_id']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==2
    assert op.owner.allocating==set()


def test_disk_only_source_change_remains_queued_for_a_matching_new_api(automatic):
    value,op,_,calls,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    def stale(fp):raise RuntimeContractError('LOADED_SOURCE_CHANGED','new copied source required')
    op.seat.require_loaded_source=stale
    value.dispatch_automatic()
    assert calls==[]
    result=value.get(1,check_id=first['check_id'])
    assert result['state']=='accepted' and result['diagnostics'][0]['code']=='LOADED_SOURCE_CHANGED'
    op.seat.require_loaded_source=lambda fp:None
    new_api=NativeChecks(op)
    new_api.dispatch_automatic()
    assert len(calls)==1


def test_existing_operator_check_is_adopted_without_duplicate_automatic_allocation(automatic):
    value,_,_,calls,con,_=automatic
    con.execute('INSERT INTO conversation_runtime_check_requests VALUES(?,?,?,?,?,?,?,?,?)',
        ('nc_'+'1'*32,1,'operator-key','hash',json.dumps(CODEX_SELECTION),'running',json.dumps({'fingerprint':FP}),1,1));con.commit()
    assert value.enqueue_automatic(CODEX_SELECTION,FP)['check_id']=='nc_'+'1'*32
    ref=value.automatic_reference(CODEX_SELECTION)
    assert ref['origin']=='operator'
    value.dispatch_automatic();assert calls==[]


def test_owner_replacement_after_claim_prevents_source_or_native_dispatch(automatic,monkeypatch):
    value,op,_,calls,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    def begin(**kw):
        monkeypatch.setattr(conversation_native_chats,'_SERVICE',None)
        kw['validate_intent']()
        calls.append('unexpected')
    op.begin=begin
    value.dispatch_automatic()
    assert calls==[]
    row=value.get(1,check_id=first['check_id'])
    assert not row['admissible']


def test_reference_scope_and_public_projection_do_not_expose_private_dispatch(automatic):
    value,_,_,_,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    ref=value.automatic_reference(CODEX_SELECTION)
    assert ref=={'check_id':first['check_id'],'origin':'installed_change','state':'accepted','diagnostic':'AUTOMATIC_CHECK_QUEUED'}
    con.execute('UPDATE conversation_runtime_check_requests SET owner_user_id=2');con.commit()
    assert value.automatic_reference(CODEX_SELECTION) is None
    with pytest.raises(RuntimeContractError):value.get(1,check_id=first['check_id'])


def test_source_manifest_change_is_observed_but_cannot_reload_loaded_driver(seat):
    value,_,_=seat
    fp=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    value.require_loaded_source(fp)
    path=value.root/'.super-coder/scripts/conversation_adapters/codex_runtime.py'
    path.write_text(path.read_text()+'\n# changed same installed binary\n')
    changed=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    assert changed.executable==fp.executable and changed.key!=fp.key
    with pytest.raises(RuntimeContractError,match='new copied API'):value.require_loaded_source(changed)


def test_probe_child_grant_is_withheld_beside_old_root_but_terminal_scope_remains(seat):
    value,_,_=seat
    value.supervisor.inventory=lambda:[{'generation_id':'old','harness':'codex'}]
    context,_,_=value.prepare('cv','generation',probe_capabilities=('submission','stop_reply','stop_work','stop_work_terminal','stop_work_child'))
    assert 'stop_work_child' not in context.probe_capabilities
    assert {'submission','stop_reply','stop_work_terminal'}<=set(context.probe_capabilities)


def test_reserved_child_slot_prevents_another_root_until_composite_cleanup(seat):
    value,_,_=seat
    con=db_driver.connect(str(value.database))
    con.execute("UPDATE conversations SET runtime_projection=?",(json.dumps({'role':'probe','generation_id':'other','state':'starting','probe_child_reserved':True}),));con.commit()
    assert value.probe_capacity('codex')==0
    con.execute("UPDATE conversations SET runtime_projection=?",(json.dumps({'role':'probe','generation_id':'other','state':'closed','probe_child_reserved':True,'preparation_cleanup':{'unit_verified_exited':True,'outcome':'complete','never_launched':True,'unresolved_work':[],'unresolved_definitions':[]}}),));con.commit()
    assert value.probe_capacity('codex')==2
    con.close()


def test_inventory_callback_close_refuses_register_before_canonical_writes(seat):
    value,events,_=seat
    original=value.require_loaded_source
    def loaded(fp):
        original(fp)
        con=db_driver.connect(str(value.database))
        con.execute("UPDATE conversations SET runtime_projection=?",(json.dumps({'role':'ordinary','generation_id':'generation','state':'closing'}),));con.commit();con.close()
    value.require_loaded_source=loaded
    with pytest.raises(RuntimeContractError):value.prepare('cv','generation',probe_capabilities=('submission',))
    assert events==[]


def test_registration_callback_close_retains_resource_without_canonical_start(seat):
    value,events,_=seat
    original=value.supervisor.register
    def register(*args):
        registered=original(*args)
        con=db_driver.connect(str(value.database))
        con.execute("UPDATE conversations SET runtime_projection=?",(json.dumps({'role':'ordinary','generation_id':'generation','state':'closing'}),));con.commit();con.close()
        return registered
    value.supervisor.register=register
    with pytest.raises(RuntimeContractError):value.prepare('cv','generation',probe_capabilities=('submission',))
    assert events==['register']


def test_automatic_deadline_is_shared_with_schema_and_dispatch(operation,monkeypatch):
    import gui_experiment_runtime as runtime
    value,fp,_,_=operation
    clock=[100.0];monkeypatch.setattr(runtime.time,'monotonic',lambda:clock[0])
    original=value.native_schema
    def schema(candidate,deadline):
        assert deadline==190
        clock[0]+=5
        return original(candidate,deadline)
    value.native_schema=schema
    captured={}
    future=Future()
    value.checker.request=lambda candidate,**kw:captured.update(kw) or future
    value.begin(deadline=210,expected_fingerprint=fp.key,validate_intent=lambda:None)
    assert captured['seconds']==105


def test_actual_publisher_adopts_check_reference_without_modifying_captured_runtime(automatic,monkeypatch):
    value,op,_,calls,con,_=automatic
    old={'role':'ordinary','generation_id':'g','state':'ready','capabilities':{'submission':'compatible'},'primary':{'activity_id':'owned'}}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'AUTO','Auto','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('ordinary',2,1,'codex',?,?,'/synthetic','ordinary','hash','native_experiment',?)",(CODEX_SELECTION['model'],CODEX_SELECTION['effort'],json.dumps(old)));con.commit()
    op.cache.get=lambda *args:None
    chats=FixtureChats(op)
    chats.resolve_route=lambda *args:(_ for _ in ()).throw(RuntimeContractError('CAPABILITY_INCONCLUSIVE','new identity'))
    chats.reconcile_automatic()
    projection=json.loads(con.execute("SELECT runtime_projection FROM conversations WHERE conversation_id='ordinary'").fetchone()[0])
    assert projection['capabilities']==old['capabilities'] and projection['primary']==old['primary']
    assert projection['latest_installed_identity']['check_ref']['check_id']
    assert len(calls)==1
    assert value.config()['candidates'][0]['automatic_check']['check_id']==projection['latest_installed_identity']['check_ref']['check_id']


def test_replacement_supersedes_only_queued_intent_retaining_unknown_probe(automatic):
    value,op,_,calls,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    op.seat.candidate_fingerprint=lambda **kw:SimpleNamespace(key='b'*64,harness='codex')
    second=value.enqueue_automatic(CODEX_SELECTION,'b'*64)
    assert second['check_id']!=first['check_id']
    assert value.get(1,check_id=first['check_id'])['state']=='complete'
    result=json.loads(con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(second['check_id'],)).fetchone()[0])
    result['_automatic'].update(phase='claimed',consumer='c'*32)
    con.execute('UPDATE conversation_runtime_check_requests SET result_json=? WHERE check_id=?',(json.dumps(result),second['check_id']));con.commit()
    op.seat.candidate_fingerprint=lambda **kw:SimpleNamespace(key='d'*64,harness='codex')
    value.enqueue_automatic(CODEX_SELECTION,'d'*64)
    assert value.get(1,check_id=second['check_id'])['state']=='retained'
    refs=value.config()['candidates'][0]['retained_checks']
    assert {ref['check_id'] for ref in refs}=={second['check_id'],value.automatic_reference(CODEX_SELECTION)['check_id']}
    value.dispatch_automatic();assert calls==[]


@pytest.mark.parametrize('change',['consumer','fingerprint','unknown_field'])
def test_torn_private_queue_metadata_is_not_never_dispatched_evidence(automatic,change):
    value,_,_,calls,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    result=json.loads(con.execute('SELECT result_json FROM conversation_runtime_check_requests').fetchone()[0])
    if change=='consumer':result['_automatic']['consumer']='1'*32
    elif change=='fingerprint':result['fingerprint']='b'*64
    else:result['_automatic']['extra']='private'
    con.execute('UPDATE conversation_runtime_check_requests SET result_json=?',(json.dumps(result),));con.commit()
    value.dispatch_automatic()
    assert calls==[] and value.get(1,check_id=first['check_id'])['state']=='retained'
    assert '_automatic' not in value.get(1,check_id=first['check_id'])


def test_claim_marked_unit_failure_is_not_native_dispatch(automatic):
    value,op,_,calls,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    def replaced(**_):raise RuntimeContractError('CHECK_OWNER_CHANGED','marked API changed')
    op.supervisor.preparation_identity=replaced
    value.dispatch_automatic()
    assert calls==[] and not value.get(1,check_id=first['check_id'])['admissible']


def test_background_child_without_identity_retains_capacity(seat):
    value,_,_=seat
    con=db_driver.connect(str(value.database))
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('old','cv',1,1,'codex','{}','ready',1,1)")
    event={'reference':{'root_id':'root'},'data':{'kind':'child'},'partial':True,'freshness':'unknown'}
    con.execute('INSERT INTO conversation_runtime_work VALUES(?,?,?,?)',('old','opaque',json.dumps(event),1));con.commit();con.close()
    assert value.probe_capacity('codex')==0


def test_current_complete_cleanup_cannot_make_contradictory_native_exit_free(seat):
    value,_,_=seat
    con=db_driver.connect(str(value.database))
    runtime={'generation_id':'other','state':'closed','preparation_cleanup':{'unit_verified_exited':True,'outcome':'complete','native_outcome':'inconclusive','unresolved_work':[],'unresolved_definitions':[]}}
    con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(runtime),));con.commit();con.close()
    assert value.probe_capacity('codex')==1


def test_running_probe_snapshot_setup_remains_exact_after_api_restart(automatic):
    value,op,_,_,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP);value.dispatch_automatic()
    runtime={'role':'probe','check_id':first['check_id'],'generation_id':'probe-g','state':'needs_consent','setup':{'setup_id':'owned'}}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'PROBE','Probe','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe',2,1,'codex',?,?,'/synthetic/probe','probe',?,'native_experiment',?)",(CODEX_SELECTION['model'],CODEX_SELECTION['effort'],FP,json.dumps(runtime)))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES(?,'probe','probe-g','preparing',9999999999,1)",(FP,));con.commit()
    restarted=NativeChecks(op)
    result=restarted.get(1,check_id=first['check_id'])
    assert result['probe']['generation_id']=='probe-g' and result['probe']['setup']=={'setup_id':'owned'}
    assert not result['admissible']
    assert restarted.retained_references(CODEX_SELECTION)['retained_checks'][0]['check_id']==first['check_id']


def test_claimed_request_hash_change_at_marked_owner_edge_refuses_dispatch(automatic):
    value,op,_,calls,con,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    def owner(**kw):
        con.execute('UPDATE conversation_runtime_check_requests SET request_hash=? WHERE check_id=?',
                    ('changed',first['check_id']));con.commit()
        return {'pid':1,'start_ticks':2}
    op.supervisor.preparation_identity=owner
    value.dispatch_automatic()
    assert calls==[] and not value.get(1,check_id=first['check_id'])['admissible']


def test_schema_failure_retains_cleanup_diagnostic_and_no_retry_after_restart(automatic):
    value,op,_,calls,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    def pending():raise RuntimeContractError('CLEANUP_PENDING','owned codegen exit unknown')
    def begin(**kw):
        op.retained_guard=pending;calls.append('attempt')
        raise RuntimeContractError('NATIVE_SCHEMA_INCONCLUSIVE','fixed child unproved')
    op.begin=begin
    value.dispatch_automatic()
    restart=NativeChecks(op)
    result=restart.get(1,check_id=first['check_id'])
    assert result['state']=='retained' and result['diagnostics'][0]['code']=='CLEANUP_PENDING'
    assert not result['retry_allowed'] and not result['admissible']
    restart.dispatch_automatic()
    assert calls==['attempt']


def test_completed_historical_check_cannot_advertise_retry_with_new_schema_ownership(automatic):
    value,op,_,_,_,_=automatic
    first=value.enqueue_automatic(CODEX_SELECTION,FP)
    value._save(first['check_id'],'complete',{'admissible':False,'retry_allowed':True})
    def pending():raise RuntimeContractError('CLEANUP_PENDING','retained schema group')
    op.retained_guard=pending
    result=value.get(1,check_id=first['check_id'])
    assert result['state']=='complete' and not result['retry_allowed'] and not result['admissible']
    assert result['diagnostics'][0]['code']=='CLEANUP_PENDING'
