"""Native ownership outlives foreground/API/registry projection state."""
import dataclasses
import json
import sqlite3
import sys
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
from conversation_runtime_contract import NativeReference, RuntimeContractError, RuntimeEvent


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'synthetic.db'
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / '.super-coder/schema.sql').read_text())
    for migration in sorted((ROOT / '.super-coder/migrations').glob('*.sql')):
        con.executescript(migration.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(1,'FX','Fixture','dev','synthetic',1)")
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,worktree,creation_idempotency_key,creation_request_hash) VALUES('cv',1,1,'codex',?,'key','hash')", (str(tmp_path),))
    con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'cv')")
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g','cv',1,1,'codex','{}','ready',1,1)")
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


def test_replay_projects_root_turn_once_and_child_terminal_does_not_finish_it(database):
    path, con = database
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',state='queued',runtime_projection=?",(json.dumps({'generation_id':'g','state':'ready'}),))
    mid = con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash,state) VALUES('cv','user','1','prompt','hello','m','h','queued')").lastrowid
    rid = con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,state,lease_owner,lease_expires_at,heartbeat_at,started_at) VALUES('cv',1,?,'starting','fixture','2030-01-01','2030-01-01','2030-01-01')",(mid,)).lastrowid
    con.commit()
    store = RuntimeStore(path)
    lease = store.attach('g',1,1,'api')
    store.intent('g',1,1,lease,'send','submit',{'text':'hello','message_id':mid,'run_id':rid})
    def event(n,kind,thread='root',data=None):
        value = RuntimeEvent(kind,NativeReference('root',thread_id=thread,activity_id='turn'),request_id='send' if thread=='root' else None,data=data or {})
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
    lease=service.store.attach('g',1,1,'api')
    with pytest.raises(RuntimeContractError,match='blocks later commands'):
        service.store.intent('g',1,1,lease,'late','submit',{'text':'late'})
    # Simulate a verified native response persisted before API death/OS stop.
    # Recovery uses its original control intent, never starts a new native one.
    service.store.receipt('g',1,1,'close:g',{'state':'written','native_cleanup':{'outcome':native_outcome}})
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
