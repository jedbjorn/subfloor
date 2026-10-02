"""Ordinary Chats intent survives autonomous busy, API loss and Close."""
import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.super-coder/scripts'))
from conversation_native_chats import NativeChatsService, project_event
from conversation_runtime import RuntimeClient
from conversation_runtime_contract import NativeReference, RuntimeEvent
from test_native_chat_ownership import database  # noqa: F401


@pytest.fixture
def fifo(request):
    path,con=request.getfixturevalue('database')
    runtime={'generation_id':'g','state':'ready','capabilities':{'submission':'compatible'}}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),))
    mids=[]
    for ordinal in range(2):
        mid=con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash) VALUES('cv','user','1','prompt',?,?,?)",('prompt'+str(ordinal),'key'+str(ordinal),'hash')).lastrowid
        con.execute("INSERT INTO conversation_outbox(conversation_id,message_id) VALUES('cv',?)",(mid,));mids.append(mid)
    con.commit()
    service=NativeChatsService(path,path.parent,None)
    lease=service.store.attach('g',1,1,service.consumer)
    class Client(RuntimeClient):
        def __init__(self):
            super().__init__(Path('/unused'),'g',consumer=service.consumer,controller_pid=1,controller_start_ticks=1)
            self.lease,self.calls,self.results=lease,[],[]
        def request(self,op,**fields):
            assert op=='submit'
            self.calls.append(fields['command'])
            return self.results.pop(0)
    client=Client()
    return service,client,con,mids


def test_autonomous_busy_proved_no_write_keeps_same_intent_run_and_fifo(fifo):
    service,client,con,mids=fifo
    client.results=[{'state':'not_written','code':'NATIVE_BUSY'},{'state':'written'},{'state':'written'}]
    service.dispatch_queued('g','cv',client,1,1)
    first=client.calls[0]
    assert con.execute('SELECT COUNT(*) FROM conversation_runs').fetchone()[0]==1
    assert con.execute('SELECT state FROM conversation_messages WHERE message_id=?',(mids[0],)).fetchone()[0]=='queued'
    service.dispatch_queued('g','cv',client,1,1)
    assert client.calls[1]==first
    assert con.execute('SELECT COUNT(*) FROM conversation_runs').fetchone()[0]==1
    service.dispatch_queued('g','cv',client,1,1)
    assert len(client.calls)==2 # Earlier written command gates the next input.
    ref=NativeReference('root',thread_id='root',activity_id='turn')
    events=[RuntimeEvent('activity.processed',ref,request_id=first['request_id']),
            RuntimeEvent('activity.terminal',ref,request_id=first['request_id'],data={'status':'completed'})]
    service.store.ingest('g',1,1,client.lease,{'events':[{'sequence':n,'event':dataclasses.asdict(event)} for n,event in enumerate(events,1)]},project=project_event)
    service.dispatch_queued('g','cv',client,1,1)
    assert client.calls[2]['message_id']==str(mids[1])
    assert client.calls[2]['request_sequence']>first['request_sequence']


@pytest.mark.parametrize('delivery',['unknown','written'])
def test_api_reattach_never_resubmits_written_or_unknown_and_blocks_later_input(fifo,delivery):
    service,client,con,mids=fifo
    client.results=[{'state':delivery}]
    service.dispatch_queued('g','cv',client,1,1)
    replacement=NativeChatsService(service.database,service.root,None)
    # New API consumer obtains a fresh fence; the command delivery remains.
    con.execute('UPDATE conversation_runtime_generations SET consumer_expires=0');con.commit()
    client.consumer=replacement.consumer
    client.lease=replacement.store.attach('g',1,1,client.consumer,lifetime=60)
    replacement.dispatch_queued('g','cv',client,1,1)
    assert len(client.calls)==1
    assert con.execute('SELECT state FROM conversation_outbox WHERE message_id=?',(mids[0],)).fetchone()[0]=='dispatched'
    assert con.execute('SELECT state FROM conversation_outbox WHERE message_id=?',(mids[1],)).fetchone()[0]=='pending'


def test_late_write_receipt_after_verified_close_never_restores_cancelled_outbox(fifo):
    service,client,con,_=fifo
    def request(op,**fields):
        service.request_close(con,'cv',1,1)
        service._commit_cleanup('g','cv',1,1,{'outcome':'complete','unit_verified_exited':True})
        return {'state':'written'}
    client.request=request
    service.dispatch_queued('g','cv',client,1,1)
    assert {row[0] for row in con.execute('SELECT state FROM conversation_outbox')}=={'cancelled'}
    assert con.execute('SELECT state FROM conversations').fetchone()[0]=='closed'
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0]=='unknown'


def test_probe_role_cannot_dispatch_ordinary_input_despite_compatible_cache(fifo):
    service,client,con,_=fifo
    runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])
    runtime['role']='probe'
    con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(runtime),));con.commit()
    service.dispatch_queued('g','cv',client,1,1)
    assert not client.calls and con.execute('SELECT COUNT(*) FROM conversation_runs').fetchone()[0]==0


def test_successful_terminal_recovers_missing_processing_observation(fifo):
    service,client,con,_=fifo
    client.results=[{'state':'written'}]
    service.dispatch_queued('g','cv',client,1,1)
    ref=NativeReference('root',thread_id='root',activity_id='turn')
    event=RuntimeEvent('activity.terminal',ref,request_id=client.calls[0]['request_id'],data={'status':'completed'})
    service.store.ingest('g',1,1,client.lease,{'events':[{'sequence':1,'event':dataclasses.asdict(event)}]},project=project_event)
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0]=='succeeded'


def test_partial_terminal_does_not_complete_gui_run(fifo):
    service,client,con,_=fifo
    client.results=[{'state':'written'}]
    service.dispatch_queued('g','cv',client,1,1)
    ref=NativeReference('root',thread_id='root',activity_id='turn')
    event=RuntimeEvent('activity.terminal',ref,request_id=client.calls[0]['request_id'],partial=True,data={'status':'completed'})
    service.store.ingest('g',1,1,client.lease,{'events':[{'sequence':1,'event':dataclasses.asdict(event)}]},project=project_event)
    assert con.execute('SELECT state FROM conversation_runs').fetchone()[0]=='starting'


def test_final_assistant_chunks_mirror_once_but_child_and_terminal_outputs_remain_native(fifo):
    from conversation_native_chats import projection
    service,client,con,_=fifo
    client.results=[{'state':'written'}]
    service.dispatch_queued('g','cv',client,1,1)
    request=client.calls[0]['request_id']
    ref=NativeReference('root',thread_id='root',activity_id='turn',item_id='assistant-item')
    child=NativeReference('root',thread_id='child',activity_id='child-turn',item_id='child-item')
    terminal=NativeReference('root',thread_id='root',activity_id='turn',item_id='command-item',native_process_id='opaque')
    events=[RuntimeEvent('activity.processed',ref,request_id=request),
        RuntimeEvent('output.delta',ref,request_id=request,data={'kind':'assistant','text':'first'}),
        RuntimeEvent('output.final',ref,request_id=request,data={'kind':'assistant','text':'first','offset':0}),
        RuntimeEvent('output.final',ref,request_id=request,data={'kind':'assistant','text':'first','offset':0}),
        RuntimeEvent('output.final',ref,request_id=request,data={'kind':'assistant','text':'second','offset':4096}),
        RuntimeEvent('output.final',child,request_id=request,data={'kind':'assistant','text':'child-output'}),
        RuntimeEvent('output.final',terminal,request_id=request,data={'kind':'terminal','text':'terminal-output'})]
    service.store.ingest('g',1,1,client.lease,{'events':[{'sequence':n,'event':dataclasses.asdict(event)} for n,event in enumerate(events,1)]},project=project_event)
    mirrored=[json.loads(row[0]) for row in con.execute("SELECT payload FROM conversation_events WHERE event_type='assistant.delta' ORDER BY sequence")]
    assert [row['text'] for row in mirrored]==['first','second']
    row=con.execute('SELECT * FROM conversations').fetchone()
    public=projection(row,con=con)['activity']
    outputs=[item for item in public if item['kind'].startswith('output.')]
    assert all(item['engine_mirrored'] for item in outputs[:4])
    assert all(not item['engine_mirrored'] for item in outputs[4:])
    native=[json.loads(row[0]) for row in con.execute("SELECT payload FROM conversation_events WHERE event_type='output.final' ORDER BY sequence")]
    assert native[0]['engine_mirrored'] is True and native[-1]['engine_mirrored'] is False
