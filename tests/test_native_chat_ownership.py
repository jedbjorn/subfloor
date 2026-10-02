"""Native ownership outlives foreground/API/registry projection state."""
import dataclasses
import contextlib
import json
import sqlite3
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
import active_chat_registry
import run
from conversation_broker import BrokerStore
from conversation_native_chats import NativeChatsService, project_event
from conversation_reaper import ReaperStore
from conversation_runtime import RuntimeStore
from conversation_runtime_contract import ExecutableBinding, NativeReference, RuntimeContext, RuntimeContractError, RuntimeEvent


@pytest.fixture
def database(tmp_path,request):
    harness=getattr(request,'param','codex')
    path = tmp_path / 'synthetic.db'
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / '.super-coder/schema.sql').read_text())
    for migration in sorted((ROOT / '.super-coder/migrations').glob('*.sql')):
        con.executescript(migration.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(1,'FX','Fixture','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,worktree,creation_idempotency_key,creation_request_hash) VALUES('cv',1,1,?,?,'key','hash')", (harness,str(tmp_path)))
    con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'cv')")
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g','cv',1,1,?,'{}','ready',1,1)",(harness,))
    con.commit()
    yield path, con
    con.close()


@pytest.mark.parametrize('state', ['ready', 'lost', 'closed'])
@pytest.mark.parametrize('cleanup', [{}, {'unit_verified_exited': True, 'outcome': 'pending'},
    {'unit_verified_exited': True, 'outcome': 'complete', 'unresolved_definitions': ['definition']},
    {'unit_verified_exited': True, 'outcome': 'complete', 'unresolved_work': ['child']}])
def test_idle_native_owner_cannot_be_displaced_or_claimed_by_cli(database, state, cleanup):
    _, con = database
    con.execute('UPDATE conversation_runtime_generations SET state=?,cleanup_json=?', (state,json.dumps(cleanup)))
    con.commit()
    assert run.browser_conversation_active(con, 1)
    assert 1 in run.browser_conversation_shell_ids(con)
    for operation in (active_chat_registry.close_active, active_chat_registry.close_for_inactivity):
        with pytest.raises(active_chat_registry.ActiveChatBusy, match='pending cleanup'):
            operation(con, 1)
    assert active_chat_registry.get(con, 1).chat_id == 'cv'
    # A lost registry link and closed chat also cannot release native ownership.
    con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now') WHERE conversation_id='cv'")
    con.commit()
    assert run.browser_conversation_active(con, 1)
    with pytest.raises(active_chat_registry.ActiveChatBusy, match='pending cleanup'):
        active_chat_registry.close_active(con, 1)


def test_verified_cleanup_releases_slot_and_mode_defaults_ephemeral(database):
    _, con = database
    assert con.execute('SELECT runtime_mode FROM conversations').fetchone()[0] == 'ephemeral'
    con.execute('UPDATE conversation_runtime_generations SET cleanup_json=?', (json.dumps({'unit_verified_exited':True,'outcome':'complete'}),))
    active_chat_registry.close_active(con, 1)
    assert not run.browser_conversation_active(con, 1)
    assert not run.browser_conversation_shell_ids(con)


def test_legacy_broker_and_reaper_do_not_take_native_turns(database):
    path, con = database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',state='queued'")
    mid = con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash,state) VALUES('cv','user','1','prompt','queued','m','h','queued')").lastrowid
    con.execute("INSERT INTO conversation_outbox(conversation_id,message_id) VALUES('cv',?)",(mid,))
    con.commit()
    assert BrokerStore(str(path)).claim_next('legacy') is None
    assert con.execute('SELECT state FROM conversation_messages').fetchone()[0] == 'queued'
    con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,state,lease_owner,lease_expires_at,heartbeat_at,started_at,process_pid,process_start_ticks,process_group_id) VALUES('cv',1,?,'running','fixture','2030-01-01','2030-01-01','2030-01-01',999999,1,999999)",(mid,))
    con.execute('DELETE FROM active_shell_chats')
    con.commit()
    assert ReaperStore(str(path)).candidates() == []


def test_preparation_returns_persisted_identity_and_close_fences_late_start(database):
    path,con=database
    con.execute('DELETE FROM conversation_runtime_generations')
    con.execute("UPDATE conversations SET runtime_mode='native_experiment'");con.commit()
    entered,release,cleaned=threading.Event(),threading.Event(),threading.Event()
    class Supervisor:
        units=[]
        def preparation_identity(self): return {'pid':123,'start_ticks':456,'unit':'owned-api','control_group':'owned-cgroup'}
        def inventory(self): return self.units
        def stop(self,generation):
            assert generation==self.units[0]['generation_id']
            cleaned.set()
            return {'os_cleanup':{'complete':True}}
        def launch(self,generation): pytest.fail('Close must fence the native launch')
    supervisor=Supervisor()
    def prepare(cid,generation):
        supervisor.units=[{'generation_id':generation,'status':'registered'}]
        entered.set();assert release.wait(3)
        return (RuntimeContext(generation,cid,1,1,'codex',path.parent,path.parent,
            ExecutableBinding(Path('/bin/true'),'a'*64,'test'),'test','b'*64,'c'*64,'unrestricted',
            capability_evidence={'submission':'compatible'}),SimpleNamespace(implementation_digest='d'*64,key='fp'),{})
    service=NativeChatsService(path,path.parent,supervisor,prepare_context=prepare)
    try:
        service.schedule_starts();assert entered.wait(2)
        chat=con.execute('SELECT version,runtime_projection FROM conversations').fetchone()
        runtime=json.loads(chat['runtime_projection'])
        assert runtime['state']=='preparing' and runtime['generation_id'] and runtime['preparation_owner']
        assert run.browser_conversation_active(con,1)
        with pytest.raises(active_chat_registry.ActiveChatBusy,match='pending cleanup'):
            active_chat_registry.close_active(con,1)
        service.request_close(con,'cv',1,chat['version'])
        assert con.execute('SELECT state FROM conversations').fetchone()[0]!='closed'
        assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closing'
        release.set();assert cleaned.wait(2)
        service.starts.shutdown(wait=True)
        assert con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==0
        assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
        assert not run.browser_conversation_active(con,1)
    finally:
        release.set();service.shutdown()


def test_restart_reconciles_never_launched_preparation_only_after_owner_exit(database,monkeypatch):
    path,con=database
    con.execute('DELETE FROM conversation_runtime_generations')
    runtime={'generation_id':'provisional','state':'closing','preparation_owner':{'pid':123,'start_ticks':456}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),));con.commit()
    class Supervisor:
        stopped=[]
        exited=False
        def preparation_exited(self,identity): return self.exited
        def inventory(self): return [{'generation_id':'provisional','status':'registered'}]
        def stop(self,generation):
            self.stopped.append(generation)
            return {'os_cleanup':{'complete':True}}
    supervisor=Supervisor()
    service=NativeChatsService(path,path.parent,supervisor)
    service.recover_preparation('cv',runtime)
    assert not supervisor.stopped and run.browser_conversation_active(con,1)
    supervisor.exited=True
    service.recover_preparation('cv',runtime)
    assert supervisor.stopped==['provisional']
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
    assert not run.browser_conversation_active(con,1)
    service.shutdown()


def test_start_admission_cannot_overwrite_concurrent_provisional_close(database,monkeypatch):
    import db_driver
    path,con=database
    con.execute('DELETE FROM conversation_runtime_generations')
    con.execute("UPDATE conversations SET runtime_mode='native_experiment'");con.commit()
    original=db_driver.write_transaction
    retained={'generation_id':'retained','state':'closing','preparation_owner':{'pid':123,'start_ticks':456}}
    @contextlib.contextmanager
    def inject_close(connection,operation,**kwargs):
        if operation=='native_chat.start_intent':
            con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(retained),));con.commit()
        with original(connection,operation,**kwargs):
            yield connection
    monkeypatch.setattr(db_driver,'write_transaction',inject_close)
    supervisor=SimpleNamespace(preparation_identity=lambda:{'pid':123,'start_ticks':456})
    service=NativeChatsService(path,path.parent,supervisor,prepare_context=lambda *args:pytest.fail('concurrent Close must prevent preparation'))
    try:
        service.schedule_starts()
        assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])==retained
        assert not service.starting
    finally:
        service.shutdown()


@pytest.mark.parametrize('database,root_thread', [('codex','root'),('claude',None)],indirect=['database'])
def test_replay_projects_root_turn_once_and_child_terminal_does_not_finish_it(database,root_thread):
    path, con = database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',state='queued',runtime_projection=?",(json.dumps({'generation_id':'g','state':'ready'}),))
    mid = con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash,state) VALUES('cv','user','1','prompt','hello','m','h','queued')").lastrowid
    rid = con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,state,lease_owner,lease_expires_at,heartbeat_at,started_at) VALUES('cv',1,?,'starting','fixture','2030-01-01','2030-01-01','2030-01-01')",(mid,)).lastrowid
    con.commit()
    store = RuntimeStore(path)
    lease = store.attach('g',1,1,'api')
    store.intent('g',1,1,lease,'send','submit',{'text':'hello','message_id':mid,'run_id':rid})
    def event(n,kind,thread=root_thread,data=None):
        value = RuntimeEvent(kind,NativeReference('root',thread_id=thread,activity_id='turn'),request_id='send' if thread in {None,'root'} else None,data=data or {})
        return {'sequence':n,'event':dataclasses.asdict(value)}
    frames = [event(1,'activity.processed'),event(2,'output.delta',data={'text':'one'}),event(3,'activity.terminal','child',{'status':'completed'})]
    replay = {'events':frames,'partial':False}
    assert store.ingest('g',1,1,lease,replay,project=project_event) == 3
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0] == 'running'
    count = con.execute('SELECT COUNT(*) FROM conversation_events').fetchone()[0]
    store.ingest('g',1,1,lease,replay,project=project_event)
    assert con.execute('SELECT COUNT(*) FROM conversation_events').fetchone()[0] == count
    store.ingest('g',1,1,lease,{'events':[event(4,'activity.terminal',data={'status':'completed'})],'partial':False},project=project_event)
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0] == 'succeeded'
    assert con.execute('SELECT state FROM conversations').fetchone()[0] == 'idle'
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0] == 'ready'
    store.ingest('g',1,1,lease,{'events':[event(5,'activity.processed')],'partial':False},project=project_event)
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0] == 'succeeded'
    assert con.execute("SELECT COUNT(*) FROM conversation_events WHERE event_type='assistant.delta'").fetchone()[0] == 1


@pytest.mark.parametrize('native_outcome', ['complete','inconclusive'])
def test_close_fence_survives_api_death_and_only_verified_cleanup_releases_slot(database,native_outcome):
    path, con = database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'ready'}),))
    con.commit()
    class OwnedSupervisor:
        def __init__(self): self.stopped=[]
        def stop(self,generation):
            self.stopped.append(generation)
            return {'os_cleanup':{'complete':True}}
    supervisor=OwnedSupervisor()
    service=NativeChatsService(path,path.parent,supervisor)
    with pytest.raises(RuntimeContractError,match='outside operator tenancy'):
        service.request_close(con,'cv',2,1)
    with pytest.raises(RuntimeContractError,match='version changed'):
        service.request_close(con,'cv',1,99)
    service.request_close(con,'cv',1,1)
    assert con.execute('SELECT close_intent FROM conversation_runtime_generations').fetchone()[0]==1
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='idle'
    lease=service.store.attach('g',1,1,service.consumer)
    with pytest.raises(RuntimeContractError,match='blocks later commands'):
        service.store.intent('g',1,1,lease,'late','submit',{'text':'late'})
    # Simulate a verified native response persisted before API death/OS stop.
    # Recovery uses its original control intent, never starts a new native one.
    service.store.receipt('g',1,1,'close:g',{'state':'written','native_cleanup':{'outcome':native_outcome}})
    con.execute('UPDATE conversation_runtime_generations SET consumer_expires=0');con.commit()
    replacement=NativeChatsService(path,path.parent,supervisor)
    replacement.recover_close('g','cv',1,1)
    assert supervisor.stopped==['g']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==1
    cleanup=json.loads(con.execute('SELECT cleanup_json FROM conversation_runtime_generations').fetchone()[0])
    assert cleanup['unit_verified_exited'] is True
    if native_outcome=='complete':
        assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
        assert not run.browser_conversation_active(con,1)
    else:
        assert con.execute('SELECT state FROM conversations').fetchone()[0]=='idle'
        assert run.browser_conversation_active(con,1)
        assert cleanup['outcome']=='pending'


def test_cleanup_chat_failure_rolls_back_generation_and_restart_finalizes_without_native_write(database,monkeypatch):
    path,con=database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'ready'}),))
    con.commit()
    class OwnedSupervisor:
        def stop(self,generation): return {'os_cleanup':{'complete':True}}
    service=NativeChatsService(path,path.parent,OwnedSupervisor())
    service.request_close(con,'cv',1,1)
    service.store.receipt('g',1,1,'close:g',{'state':'written','native_cleanup':{'outcome':'complete'}})
    def crash(con,cid): raise RuntimeError('simulated API death during finalization')
    monkeypatch.setattr(service,'_finish_chat',crash)
    with pytest.raises(RuntimeError,match='API death'):
        service.recover_close('g','cv',1,1)
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='closing'
    assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closing'
    assert run.browser_conversation_active(con,1)
    replacement=NativeChatsService(path,path.parent,OwnedSupervisor())
    replacement.recover_close('g','cv',1,1)
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='closed'
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==1
    assert not run.browser_conversation_active(con,1)


def test_old_verified_closed_generation_repairs_unfinished_chat_without_attach(database,monkeypatch):
    path,con=database
    cleanup={'outcome':'complete','unit_verified_exited':True}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'closing'}),))
    con.execute("UPDATE conversation_runtime_generations SET state='closed',close_intent=1,cleanup_json=?",(json.dumps(cleanup),))
    con.commit()
    class NoNativeAccess:
        def stop(self,generation): raise AssertionError('already verified exited')
    service=NativeChatsService(path,path.parent,NoNativeAccess())
    monkeypatch.setattr(service,'attach',lambda generation: pytest.fail('closed generation must not attach'))
    # Run one consumer iteration without spawning an unbounded worker.
    monkeypatch.setattr(service.wake,'wait',lambda seconds: service.stopped.set())
    service.run()
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
    assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closed'
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==0


@pytest.mark.parametrize('delivery',['written','unknown'])
def test_native_control_stable_request_never_rewrites_ambiguous_or_written_delivery(database,monkeypatch,delivery):
    path,con=database
    runtime={'generation_id':'g','state':'ready','primary':{'root_id':'root','thread_id':'root','activity_id':'turn'},'capabilities':{'stop_reply':'compatible'}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),));con.commit()
    service=NativeChatsService(path,path.parent,None)
    class Client:
        writes=[]
        def request(self,op,**fields):
            self.writes.append((op,fields))
            if delivery=='unknown': raise OSError('lost response after possible native write')
            return {'state':'written','native_ack':True}
    client=Client();monkeypatch.setattr(service,'attach',lambda generation:(client,1,1))
    body={'version':1,'generation_id':'g','action':'stop_reply','expected_activity_id':'turn'}
    first=service.control(con,'cv',1,'stable',body)
    assert first['state']==delivery
    duplicate=service.control(con,'cv',1,'stable',body)
    assert duplicate['duplicate'] is True and duplicate['state']==delivery
    assert len(client.writes)==1
    with pytest.raises(RuntimeContractError,match='different request'):
        service.control(con,'cv',1,'stable',body|{'expected_activity_id':'new-turn'})
    assert len(client.writes)==1
    command=client.writes[0][1]['command']
    assert command['target']==runtime['primary']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==1


@pytest.mark.parametrize('variant,verdict', [('terminal','compatible'),('child','inconclusive')])
def test_stop_work_admits_only_matching_direct_target_coverage(database,monkeypatch,variant,verdict):
    path,con=database
    runtime={'generation_id':'g','state':'ready','capabilities':{'stop_work':'compatible','stop_work_terminal':'compatible','stop_work_child':'inconclusive'}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),))
    work=dataclasses.asdict(RuntimeEvent('work.observed',NativeReference('root',thread_id='root' if variant=='terminal' else 'child',activity_id='child-turn',native_process_id='opaque-process' if variant=='terminal' else None),data={'kind':variant}))
    con.execute("INSERT INTO conversation_runtime_work(generation_id,work_key,projection_json,last_sequence) VALUES('g','stored-target',?,1)",(json.dumps(work),));con.commit()
    service=NativeChatsService(path,path.parent,None)
    calls=[]
    class Client:
        def request(self,op,**fields): calls.append(fields);return {'state':'written'}
    monkeypatch.setattr(service,'attach',lambda generation:(Client(),1,1))
    body={'version':1,'generation_id':'g','action':'stop_work','work_key':'stored-target','expected_activity_id':'child-turn'}
    if verdict=='compatible':
        assert service.control(con,'cv',1,'key',body)['state']=='written'
        assert calls[0]['command']['target']==work['reference']
    else:
        with pytest.raises(RuntimeContractError) as raised: service.control(con,'cv',1,'key',body)
        assert raised.value.code=='CAPABILITY_INCONCLUSIVE'
        assert calls==[]
        assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==0


def test_startup_control_uses_exact_stored_phase_and_close_fences_new_action(database,monkeypatch):
    path,con=database
    runtime={'generation_id':'g','state':'needs_consent','setup':{'setup_id':'phase','configuration_sha256':'a'*64}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),));con.commit()
    service=NativeChatsService(path,path.parent,None)
    writes=[]
    class Client:
        def request(self,op,**fields): writes.append(fields);return {'state':'written'}
    monkeypatch.setattr(service,'attach',lambda generation:(Client(),1,1))
    body={'version':1,'generation_id':'g','action':'enable_local_channel','setup_id':'phase'}
    with pytest.raises(RuntimeContractError): service.control(con,'cv',2,'other-owner',body)
    with pytest.raises(RuntimeContractError): service.control(con,'cv',1,'stale',body|{'setup_id':'withdrawn'})
    assert writes==[]
    service.control(con,'cv',1,'choice',body)
    assert writes[0]['command']['options']==runtime['setup']
    assert writes[0]['command']['target'] is None
    service.request_close(con,'cv',1,1)
    version=con.execute('SELECT version FROM conversations').fetchone()[0]
    with pytest.raises(RuntimeContractError): service.control(con,'cv',1,'new-choice',body|{'version':version})
    assert len(writes)==1


def test_late_pending_cleanup_cannot_regress_verified_terminal_ownership(database):
    path,con=database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'closing'}),));con.commit()
    class Supervisor:
        def stop(self,generation): return {'os_cleanup':{'complete':True}}
    service=NativeChatsService(path,path.parent,Supervisor())
    service.finish_cleanup('g','cv',1,1,{'outcome':'complete'})
    service.finish_cleanup('g','cv',1,1,{'outcome':'inconclusive','unresolved_work':['unknown-child']})
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='closed'
    assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closed'
    assert not run.browser_conversation_active(con,1)


def test_replacement_consumer_fences_old_cleanup_completion(database):
    path,con=database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps({'generation_id':'g','state':'closing'}),));con.commit()
    class Supervisor:
        def stop(self,generation):
            con.execute('UPDATE conversation_runtime_generations SET consumer_expires=0');con.commit()
            RuntimeStore(path).attach('g',1,1,'replacement')
            return {'os_cleanup':{'complete':True}}
    service=NativeChatsService(path,path.parent,Supervisor())
    service.store.attach('g',1,1,service.consumer)
    with pytest.raises(RuntimeContractError) as raised:
        service.finish_cleanup('g','cv',1,1,{'outcome':'complete'})
    assert raised.value.code=='LEASE_FENCED'
    assert con.execute('SELECT state FROM conversation_runtime_generations').fetchone()[0]=='ready'
    assert json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])['state']=='closing'
    assert run.browser_conversation_active(con,1)


@pytest.mark.parametrize('database',['claude'],indirect=True)
@pytest.mark.parametrize('observation',['bash','partial','unknown-task'])
def test_actual_claude_work_events_survive_store_and_admit_only_proved_scoped_terminal(database,monkeypatch,observation):
    from conversation_adapters.claude_runtime import ClaudeRuntimeDriver
    from conversation_runtime_contract import RuntimeIdentity
    path,con=database
    runtime={'generation_id':'g','state':'ready','capabilities':{'stop_work':'compatible','stop_work_terminal':'compatible','stop_work_child':'inconclusive'}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),))
    con.commit()
    events=[];driver=ClaudeRuntimeDriver();driver._identity=RuntimeIdentity('root');driver._emit=events.append
    if observation!='unknown-task':
        driver._tool({'hook_event_name':'PostToolUse','tool_name':'Bash','tool_use_id':'tool','tool_input':{},'tool_response':{'backgroundTaskId':'native-bash-id'}},None)
    if observation!='bash':
        driver._snapshot({'background_tasks':[{'id':'native-bash-id'}],'session_crons':[]})
    service=NativeChatsService(path,path.parent,None)
    lease=service.store.attach('g',1,1,service.consumer)
    service.store.ingest('g',1,1,lease,{'events':[{'sequence':n,'event':dataclasses.asdict(event)} for n,event in enumerate(events,1)],'partial':False})
    rows=con.execute('SELECT work_key,projection_json FROM conversation_runtime_work').fetchall()
    assert len(rows)==1
    selected=json.loads(rows[0]['projection_json'])
    assert selected['data']['kind']==('task' if observation=='unknown-task' else 'terminal')
    writes=[]
    class Client:
        def request(self,op,**fields):writes.append(fields);return {'state':'written'}
    monkeypatch.setattr(service,'attach',lambda generation:(Client(),1,1))
    body={'version':1,'generation_id':'g','action':'stop_work','work_key':rows[0]['work_key']}
    if observation=='bash':
        assert service.control(con,'cv',1,'stop',body)['state']=='written'
        assert writes[0]['command']['target']['work_id']=='native-bash-id'
    else:
        with pytest.raises(RuntimeContractError) as raised:service.control(con,'cv',1,'stop',body)
        assert raised.value.code==('WORK_INCONCLUSIVE' if observation=='partial' else 'CAPABILITY_INCONCLUSIVE')
        assert writes==[]
