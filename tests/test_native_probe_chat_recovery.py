"""GUI probe Close shares the factory receipt; restart never resumes checks."""
import json
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_native_checks as checks
from conversation_native_chats import NativeChatsService
from test_gui_experiment_runtime import operation  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401
from test_native_check_workflow import workflow  # noqa: F401


def probe_projection(con,*,state='checking'):
    runtime={'role':'probe','generation_id':'g','state':state,'check_deadline':time.time()+60}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','cv','g','preparing',?,1)",(time.time()+60,));con.commit()
    return runtime


def test_gui_close_payload_reconciles_factory_command_without_conflict(request):
    path,con=request.getfixturevalue('database')
    probe_projection(con)
    service=NativeChatsService(path,path.parent,None)
    lease=service.store.attach('g',1,1,service.consumer)
    version=con.execute('SELECT version FROM conversations').fetchone()[0]
    service.request_close(con,'cv',1,version)
    retained=service.store.intent('g',1,1,lease,'probe-close','close',{'action':'close'})
    assert retained['control_id']=='probe-close'
    assert set(retained)=={'control_id','request_sequence','action','payload_digest'}
    assert service.close_id('g','cv')=='probe-close'
    assert con.execute('SELECT close_intent FROM conversation_runtime_generations').fetchone()[0]==1
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==1


def test_gui_close_projects_existing_factory_close_without_new_command(request):
    path,con=request.getfixturevalue('database')
    probe_projection(con)
    service=NativeChatsService(path,path.parent,None)
    lease=service.store.attach('g',1,1,service.consumer)
    command=service.store.intent('g',1,1,lease,'probe-close','close',{'action':'close'})
    version=con.execute('SELECT version FROM conversations').fetchone()[0]
    service.request_close(con,'cv',1,version)
    assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closing'
    retained=service.store.intent('g',1,1,lease,'probe-close','close',{'action':'close'})
    assert retained==command
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==1


def test_current_probe_factory_keeps_reader_and_receives_scoped_close(request):
    value,fp,con,_=request.getfixturevalue('operation')
    probe_projection(con)
    con.execute('UPDATE conversation_runtime_probe_jobs SET fingerprint_key=?',(fp.key,));con.commit()
    value.fingerprint,value.future=fp,Future()
    calls=[]
    value.cancel=lambda:calls.append('close')
    assert value.consume_probe('cv','g') and not calls
    runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])
    runtime['state']='closing'
    con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(runtime),));con.commit()
    assert value.consume_probe('cv','g') and calls==['close']


def test_restart_close_uses_retained_probe_identity_then_releases_exact_job(request):
    value,_,con,_=request.getfixturevalue('operation')
    probe_projection(con)
    value.supervisor.stop=lambda generation:{'os_cleanup':{'complete':generation=='g'}}
    lease=value.service.store.attach('g',1,1,value.service.consumer)
    version=con.execute('SELECT version FROM conversations').fetchone()[0]
    value.service.request_close(con,'cv',1,version)
    calls=[]
    def native_request(op,**fields):
        calls.append((op,fields))
        assert fields['command']['control_id']=='probe-close'
        return {'outcome':'complete','unresolved_work':[],'unresolved_definitions':[]}
    client=SimpleNamespace(request=native_request,subscribe=lambda *a,**k:None,lease=lease)
    assert not value.consume_probe('cv','g')
    value.service.close_generation('g','cv',client,1,1)
    assert len(calls)==1 and calls[0][0]=='close'
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
    assert not value.consume_probe('cv','g')
    assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='complete'


def test_restart_cleanup_readback_allows_new_check_without_restoring_grades(request):
    value,op,_,calls,con=request.getfixturevalue('workflow')
    first=value.create(1,'original',checks.CODEX_SELECTION)
    runtime={'role':'probe','check_id':first['check_id'],'generation_id':'probe-gen','state':'closed',
        'preparation_cleanup':{'unit_verified_exited':True,'never_launched':True}}
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(2,'P','Probe','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,model,effort,worktree,state,closed_at,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection) VALUES('probe-cv',2,1,'codex','gpt-6.1-sol','high','/synthetic/probe','closed',datetime('now'),'probe-key','fp','native_experiment',?)",(json.dumps(runtime),))
    con.execute("INSERT INTO conversation_runtime_probe_jobs VALUES('fp','probe-cv','probe-gen','complete',100,1)");con.commit()
    restarted=checks.NativeChecks(op)
    op.owner.allocating.add('fp')
    assert not restarted.get(1,request_key='original')['retry_allowed']
    op.owner.allocating.clear()
    result=restarted.get(1,request_key='original')
    assert result['state']=='complete' and result['retry_allowed'] and not result['admissible']
    assert result['grades']=={} and calls==['begin']


def test_conflicting_captured_generation_cannot_certify_never_launched(request):
    from conversation_runtime_contract import RuntimeContractError
    value,_,con,_=request.getfixturevalue('operation')
    runtime=probe_projection(con,state='closed')
    runtime['preparation_cleanup']={'unit_verified_exited':True,'never_launched':True}
    con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now'),runtime_projection=?",(json.dumps(runtime),))
    con.execute('UPDATE conversation_runtime_generations SET owner_user_id=2');con.commit()
    try:
        value.consume_probe('cv','g')
    except RuntimeContractError as exc:
        assert exc.code=='PROBE_INVALID'
    else:
        raise AssertionError('conflicting generation was accepted as absent')
    assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='preparing'
