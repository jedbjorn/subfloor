"""Normal-check diagnostics retain scoped scalars; synthetic DB only."""
import json
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/".super-coder/scripts"),str(ROOT/".super-coder/api")]
import conversation_native_checks as checks
from test_native_chat_ownership import database  # noqa: F401
from test_native_check_workflow import workflow  # noqa: F401


def bind_probe(captured_workflow):
    value,operation,report,_calls,con=captured_workflow
    first=value.create(1,'witness-key',checks.CODEX_SELECTION)
    runtime={'role':'probe','check_id':first['check_id'],'generation_id':'probe-gen','state':'checking'}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'P','Probe','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe-cv',2,1,'codex','gpt-6.1-sol','high','/synthetic/probe','probe-key','fp','native_experiment',?)",(json.dumps(runtime),))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','probe-cv','probe-gen','preparing',100,1)");con.commit()
    report.update(probe={'conversation_id':'probe-cv','generation_id':'probe-gen'},behavior_witness={
        'waiting_stage':'nonce_recall','first_root_processed':True,'first_successful_reply':True,
        'first_root_terminal_counts':{'completed':1},'first_final_nonce_matches':True,
        'first_close_observed':True,'first_close_outcome':'inconclusive',
        'raw_output':'private','nonce':'private','native_id':'private','auth':'private'})
    return value,operation,report,con,first,runtime


def test_same_check_witness_is_observation_not_admission_or_cleanup(request):
    value,_,_report,_con,first,_=bind_probe(request.getfixturevalue("workflow"))
    result=value.get(1,check_id=first['check_id'])
    assert result['behavior_witness_observation']=='recorded_check'
    assert result['behavior_witness']['first_root_terminal_counts']=={'completed':1}
    assert result['behavior_witness']['first_close_outcome']=='inconclusive'
    assert not result['admissible'] and not result['grades'] and not result['cleanup']
    assert 'private' not in json.dumps(result['behavior_witness'])
    assert result['fingerprint']=='fp'
    assert 'probe_binding' not in result


@pytest.mark.parametrize('field',['fingerprint','observed_probe','generation','role','check','owner','candidate'])
def test_mismatched_scope_clears_witness_without_admission(request,field):
    value,_,report,con,first,runtime=bind_probe(request.getfixturevalue("workflow"))
    if field=='fingerprint':report['fingerprint']='replacement'
    elif field=='observed_probe':report['probe']['generation_id']='replacement'
    elif field in {'generation','role','check'}:
        runtime[{'generation':'generation_id','role':'role','check':'check_id'}[field]]='replacement'
        con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='probe-cv'",(json.dumps(runtime),))
    elif field=='owner':
        con.execute("INSERT INTO users(user_id,username) VALUES(9,'other')")
        con.execute("UPDATE shells SET user_id=9 WHERE shell_id=2")
    elif field=='candidate':con.execute("UPDATE conversation_runtime_probe_jobs SET fingerprint_key='replacement' WHERE fingerprint_key='fp'")
    con.commit()
    result=value.get(1,check_id=first['check_id'])
    assert result.get('behavior_witness') is None and not result['admissible']


def test_historical_same_check_get_keeps_fixed_witness_with_historical_label(request):
    value,operation,report,con,first,_=bind_probe(request.getfixturevalue("workflow"))
    report.update(state='complete',cleanup={'native_outcome':'complete','unit_verified_exited':True},resources={'owned_capacity_released':True,'owner_allocating':False,'owner_closing':False})
    terminal=value.get(1,check_id=first['check_id'])
    assert terminal['state']=='complete' and terminal['behavior_witness_observation']=='historical_check'
    saved=terminal['behavior_witness']
    report['behavior_witness']={'waiting_stage':'unknown','first_close_outcome':'failed'}
    result=checks.NativeChecks(operation).get(1,request_key='witness-key')
    assert result['behavior_witness']==saved and result['behavior_witness_observation']=='historical_check'
    assert not result['admissible']
    runtime=json.loads(con.execute("SELECT runtime_projection FROM conversations WHERE conversation_id='probe-cv'").fetchone()[0]);runtime['generation_id']='replacement'
    con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='probe-cv'",(json.dumps(runtime),));con.commit()
    assert checks.NativeChecks(operation).get(1,request_key='witness-key')['behavior_witness'] is None


@pytest.mark.parametrize('bad',[None,[],True,42,'raw',{'waiting_stage':{},'first_root_processed':1,'root_tagged_pid_candidates':True,'first_close_outcome':'private','raw':'private'}])
def test_witness_reapplies_known_scalar_allowlist(request,bad):
    value,_,report,_,first,_=bind_probe(request.getfixturevalue("workflow"))
    report['behavior_witness']=bad
    result=value.get(1,check_id=first['check_id'])
    witness=result.get('behavior_witness')
    if isinstance(bad,dict):assert witness=={'waiting_stage':'unknown'}
    else:assert witness is None
    assert not result['admissible'] and 'private' not in json.dumps(witness)


@pytest.mark.parametrize('invalid',['private raw text',['private'],42,True])
def test_retained_malformed_witness_never_exposes_unknown_data(invalid):
    row={'result_json':json.dumps({'behavior_witness':invalid,'behavior_witness_observation':'private'}),
         'status':'complete','check_id':'synthetic','request_key':'synthetic','selection_json':json.dumps(checks.CODEX_SELECTION)}
    projected=checks.NativeChecks._projection(row)
    assert projected['behavior_witness'] is None and 'behavior_witness_observation' not in projected
    assert 'private' not in json.dumps(projected)


def test_duplicate_post_retains_intent_but_clears_displaced_witness(request):
    value,_operation,_report,con,first,runtime=bind_probe(request.getfixturevalue("workflow"))
    value.get(1,check_id=first['check_id'])
    assert value.create(1,'witness-key',checks.CODEX_SELECTION)['behavior_witness'] is not None
    runtime['generation_id']='replacement'
    con.execute("UPDATE conversations SET runtime_projection=? WHERE conversation_id='probe-cv'",(json.dumps(runtime),));con.commit()
    replay=value.create(1,'witness-key',checks.CODEX_SELECTION)
    assert replay['check_id']==first['check_id']
    assert replay['behavior_witness'] is None and replay['behavior_witness_observation']=='unavailable'
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_check_requests').fetchone()[0]==1
