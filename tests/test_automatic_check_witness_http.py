"""Strict check diagnostics through migrated automatic intents and normal HTTP."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_routes as routes
import gui_experiment_runtime as runtime
from conversation_native_checks import CODEX_SELECTION, NativeChecks
from test_native_automatic_updates import FP
from test_native_automatic_updates import automatic as automatic  # noqa: PLC0414
from test_native_chat_ownership import database as database  # noqa: PLC0414
from test_native_check_workflow import workflow as workflow  # noqa: PLC0414


def bound_check(value,op,report,con):
    first=value.enqueue_automatic(CODEX_SELECTION,FP);value.dispatch_automatic()
    projection={'role':'probe','check_id':first['check_id'],'generation_id':'probe-gen','state':'checking'}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'P','Probe','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe-cv',2,1,'codex','gpt-6.1-sol','high','/synthetic/probe','probe-key',?,'native_experiment',?)",(FP,json.dumps(projection)))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES(?,'probe-cv','probe-gen','preparing',100,1)",(FP,));con.commit()
    report['probe']={'conversation_id':'probe-cv','generation_id':'probe-gen'}
    return first


def get_http(value,op,first,monkeypatch):
    monkeypatch.setattr(routes,'DB_PATH',op.database)
    monkeypatch.setattr(runtime,'_FIXTURE',SimpleNamespace(workflow=value))
    response=routes.handle('GET','/api/conversations/native-checks/'+first['check_id'],'Host: localhost:8800\r\n',b'')
    assert response[0]==200
    return json.loads(response[2])


def test_actual_normal_get_retains_automatic_binding_and_known_dff_role_scalars(automatic,monkeypatch):
    value,op,report,calls,con,_=automatic
    first=bound_check(value,op,report,con)
    witness={'waiting_stage':'sibling_tagged_pid','first_root_processed':True,'raw_pid':123,'nonce':'PRIVATE','history_baseline':{'native_session_id':'PRIVATE'}}
    for role in ('root','sibling','child'):
        witness.update({role+'_tagged_pid_candidates':2,role+'_owned_pid_matches':1,
                        role+'_rejected_pid_output_records':128,role+'_pid_observation':'matched'})
    report['behavior_witness']=witness
    result=get_http(value,op,first,monkeypatch)
    safe=result['behavior_witness']
    assert safe['waiting_stage']=='sibling_tagged_pid' and safe['sibling_owned_pid_matches']==1
    assert safe['child_rejected_pid_output_records']==128 and safe['root_pid_observation']=='matched'
    assert result['behavior_witness_observation']=='recorded_check' and result['origin']=='installed_change'
    assert not result['admissible'] and not result['grades'] and not result['retry_allowed']
    assert len(calls)==1 and '_automatic' not in result and 'probe_binding' not in result
    assert 'PRIVATE' not in json.dumps(result) and 'raw_pid' not in json.dumps(result)
    stored=json.loads(con.execute('SELECT result_json FROM conversation_runtime_check_requests WHERE check_id=?',(first['check_id'],)).fetchone()[0])
    assert stored['_automatic']['expected_fingerprint']==FP and stored['_automatic']['consumer']==value.consumer_token


@pytest.mark.parametrize('invalid',[True,False,None,'1',-1,129,{},[]])
def test_normal_get_never_coerces_role_counts_or_private_pid_observation(automatic,monkeypatch,invalid):
    value,op,report,_calls,con,_=automatic
    first=bound_check(value,op,report,con)
    witness={'waiting_stage':'sibling_tagged_pid'}
    for role in ('root','sibling','child'):
        witness.update({role+'_'+suffix:invalid for suffix in ('tagged_pid_candidates','owned_pid_matches','rejected_pid_output_records')})
        witness[role+'_pid_observation']='PRIVATE'
    report['behavior_witness']=witness
    result=get_http(value,op,first,monkeypatch)
    assert result['behavior_witness']=={'waiting_stage':'sibling_tagged_pid'}
    assert not result['admissible'] and not result['grades']


@pytest.mark.parametrize('observation',['unobserved','matched','ambiguous','no_owned_match','missing_terminal_output'])
def test_fixed_pid_observation_is_diagnostic_without_cause_or_admission(automatic,monkeypatch,observation):
    value,op,report,_calls,con,_=automatic
    first=bound_check(value,op,report,con)
    report['behavior_witness']={'waiting_stage':'sibling_tagged_pid','sibling_pid_observation':observation,'pid_cause':'PRIVATE'}
    result=get_http(value,op,first,monkeypatch)
    assert result['behavior_witness']['sibling_pid_observation']==observation
    assert 'pid_cause' not in result['behavior_witness'] and not result['admissible']


def test_historical_automatic_witness_retains_schema_fence_and_never_dispatches_on_get(automatic,monkeypatch):
    value,op,report,calls,con,_=automatic
    first=bound_check(value,op,report,con)
    report.update(state='complete',behavior_witness={'waiting_stage':'finished'},
        cleanup={'native_outcome':'complete','unit_verified_exited':True,'unresolved_work':[],'unresolved_definitions':[]},
        resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    before=get_http(value,op,first,monkeypatch)
    assert before['behavior_witness_observation']=='historical_check' and not before['admissible']
    from conversation_runtime_contract import RuntimeContractError
    def unknown_cleanup():raise RuntimeContractError('CLEANUP_PENDING','synthetic schema cleanup unknown')
    op.retained_guard=unknown_cleanup
    recovered=NativeChecks(op)
    result=get_http(recovered,op,first,monkeypatch)
    assert result['behavior_witness']=={'waiting_stage':'finished'}
    assert result['behavior_witness_observation']=='historical_check'
    assert result['origin']=='installed_change' and not result['retry_allowed'] and not result['admissible']
    assert result['diagnostics'][0]['code']=='CLEANUP_PENDING' and len(calls)==1
