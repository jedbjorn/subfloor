"""D406 timestamped requests through real source seams; native hooks are synthetic."""
# ruff: noqa: F811
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api'),str(ROOT/'tests')]
import conversation_native_chats as chats
import test_conversation_api as api_tests
from conversation_adapters.claude_runtime import CHANNEL, ClaudeRuntimeDriver
from conversation_runtime import RuntimeClient
from conversation_runtime_contract import (
    NativeControl,
    RuntimeContractError,
    RuntimeIdentity,
)
from conversation_runtime_controller import Controller, PrivateServer, start_ticks
from test_gui_experiment_native_seat import seat  # noqa: F401
from test_native_chat_ownership import database  # noqa: F401

CAPS={'stop_work':'compatible','stop_work_terminal':'compatible','stop_work_child':'inconclusive'}

def parser(root,cid='cv'):
    native=ClaudeRuntimeDriver()
    native._identity=RuntimeIdentity('root')
    native._context=SimpleNamespace(harness='claude',history=None,worktree=root,conversation_id=cid,
        generation_id='g',capability_evidence=CAPS,probe_capabilities=())
    native._ready=True;native._primary_state='idle'
    return native

def snapshot(native,*,malformed=False,prompt='observed'):
    native._snapshot({'hook_event_name':'Stop','prompt_id':prompt,
        'background_tasks':[{'unexpected':'unparseable'}] if malformed else
            [{'id':wid,'type':'shell','status':'running'} for wid in ('target','sibling')],
        'session_crons':[]})

def ingest(service,controller):
    service.store.ingest('g',1,1,service.store.status('g',1,1)|{'consumer':service.consumer,
        'fence':service.store.status('g',1,1)['consumer_fence']},controller.journal.replay(0),project=chats.project_event)

@pytest.fixture
def pipeline(database,tmp_path):
    path,con=database
    native=parser(tmp_path);owner=Controller('g',tmp_path,native)
    owner.identity=native._identity;owner.context=native._context;owner.ready=True
    owner.journal.set('harness','claude');native._emit=owner.emit
    runtime={'generation_id':'g','root_id':'root','state':'ready','capabilities':CAPS}
    con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=?",(json.dumps(runtime),));con.commit()
    service=chats.NativeChatsService(path,tmp_path,None)
    service.store.attach('g',1,1,service.consumer)
    snapshot(native);ingest(service,owner)
    try:yield con,native,owner,service
    finally:owner.journal.db.close()


def public(con):
    row=dict(con.execute("SELECT * FROM conversations WHERE conversation_id='cv'").fetchone())
    return chats.projection(row,con=con),row


def ui_body(runtime,row,harness='claude'):
    app=(ROOT/'.super-coder/ui/app.js').read_text()
    native=app[app.index('// Native experiment: consume'):app.index('async function chatRenderNew(')]
    cv={'conversation_id':'cv','version':1,'state':'idle','runtime':runtime,'route':{'harness':harness}}
    script=native+'\nconsole.log(JSON.stringify(chatNativeControlBody('+json.dumps(cv)+',"stop_work",'+json.dumps(row)+')));'
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,check=False,timeout=5)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def mutate(row,name):
    row=copy.deepcopy(row)
    if name in ('true','string','null','zero','negative','overflow','infinity'):
        row['observed_at']={'true':True,'string':'1','null':None,'zero':0,'negative':-1,'overflow':10**400,'infinity':float('inf')}[name]
    elif name=='missing_timestamp':row.pop('observed_at')
    elif name in ('complete_missing','complete_false','complete_string'):
        if name=='complete_missing':row['data'].pop('snapshot_complete')
        else:row['data']['snapshot_complete']=False if name=='complete_false' else 'true'
    elif name=='partial':row['partial']=True
    elif name=='partial_string':row['partial']='false'
    elif name in ('stale','unknown','current'):row['freshness']=name
    elif name=='nonStop':row['provenance']='claude:SubagentStop-snapshot'
    elif name=='foreign_root':row['reference']['root_id']='foreign'
    elif name=='missing_prompt':row['reference']['activity_id']=None
    elif name=='child_thread':row['reference']['thread_id']='child'
    elif name=='parent':row['reference']['parent_thread_id']='root'
    elif name=='process':row['reference']['native_process_id']='guessed-pid'
    elif name=='os_process':row['reference']['os_process']={'pid':77,'start_ticks':100}
    elif name=='completed':row['data']['state']='completed'
    elif name=='legacy_status':row['data'].pop('state');row['data']['status']='running'
    elif name=='contradictory':row['data']['status']='completed'
    elif name=='unknown_kind':row['data']['kind']='task'
    elif name=='child_kind':row['data']['kind']='child'
    elif name=='terminal_event':row['kind']='work.terminal'
    elif name=='unverified':row['grade']='unverified'
    elif name=='malformed_ref':row['reference']=[]
    elif name=='malformed_data':row['data']=[]
    elif name=='malformed_thread':row['reference']['thread_id']=[]
    return row

@pytest.mark.parametrize('database',['claude'],indirect=True)
@pytest.mark.parametrize('name',['true','string','null','zero','negative','missing_timestamp','complete_missing',
    'complete_false','complete_string','partial','partial_string','stale','unknown','current','nonStop',
    'foreign_root','missing_prompt','child_thread','parent','process','os_process','completed','legacy_status',
    'contradictory','unknown_kind','child_kind','terminal_event','unverified',
    'overflow','infinity','malformed_ref','malformed_data','malformed_thread'])
def test_real_store_record_server_and_actual_app_reject_consumed_mutations(pipeline,name):
    con,_native,_owner,_service=pipeline
    runtime,row=public(con);work=runtime['work'][0];bad=mutate(work,name)
    assert ui_body(runtime,bad) is None
    con.execute('UPDATE conversation_runtime_work SET projection_json=? WHERE work_key=?',
        (json.dumps({k:v for k,v in bad.items() if k!='work_key'}),work['work_key']));con.commit()
    con.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(RuntimeContractError):
            chats.persist_control_intent(con,'cv',1,{'version':row['version'],'generation_id':'g',
                'action':'stop_work','work_key':work['work_key']},'request')
    finally:con.rollback()
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_commands').fetchone()[0]==0

@pytest.mark.parametrize('database',['claude'],indirect=True)
@pytest.mark.parametrize('change',['variant','aggregate','root','gid','closing','latest_only'])
def test_captured_admission_not_latest_installation_or_foreign_generation(pipeline,change):
    con,_native,_owner,_service=pipeline;runtime,row=public(con);work=runtime['work'][0]
    if change in ('variant','aggregate'):runtime['capabilities']['stop_work_terminal' if change=='variant' else 'stop_work']='inconclusive'
    elif change=='root':runtime['root_id']='foreign'
    elif change=='gid':runtime['generation_id']='replacement'
    elif change=='closing':runtime['state']='closing'
    else:runtime['capabilities']={};runtime['latest_installed_identity']={'capability_grade':'compatible'}
    if change!='gid':assert ui_body(runtime,work) is None
    con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps({k:v for k,v in runtime.items() if k not in ('work','controls','activity')}),));con.commit()
    con.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(RuntimeContractError):chats.persist_control_intent(con,'cv',1,{'version':row['version'],
            'generation_id':'g','action':'stop_work','work_key':work['work_key']},'request')
    finally:con.rollback()

@pytest.mark.parametrize('database',['claude'],indirect=True)
def test_later_partial_inventory_permits_historical_request_but_never_native_success(pipeline):
    con,native,owner,service=pipeline;snapshot(native,malformed=True,prompt='later');ingest(service,owner)
    runtime,_row=public(con);work=runtime['work'][0]
    assert runtime['partial'] and native.inventory(deadline=time.monotonic()+1).partial
    assert work['partial'] is False and work['data']['snapshot_complete'] is True
    assert ui_body(runtime,work)['work_key']==work['work_key']
    con.execute('BEGIN IMMEDIATE')
    chats.persist_control_intent(con,'cv',1,{'version':_row['version'],'generation_id':'g',
        'action':'stop_work','work_key':work['work_key']},'historical-request')
    con.commit()
    ref=native._work[work['reference']['work_id']].reference
    result=native.control(NativeControl('request',1,'d'*64,'stop_work',target=ref),deadline=time.monotonic()+1)
    assert result.state=='unknown' and result.acknowledged
    assert not any(e['event']['kind']=='control.outcome' for e in owner.journal.replay(0)['events'])


def test_codex_current_guard_stays_current_and_target_specific():
    runtime={'generation_id':'g','state':'ready','capabilities':CAPS}
    row={'work_key':'key','kind':'work.observed','partial':False,'freshness':'current',
        'data':{'kind':'terminal','state':'running'},'reference':{'native_process_id':'pid'}}
    assert ui_body(runtime,row,'codex')
    row['freshness']='last_observed';assert ui_body(runtime,row,'codex') is None


def test_actual_http_private_control_unknown_duplicate_then_native_result(monkeypatch,tmp_path):
    case=api_tests.ConversationApiCase(methodName='runTest');case.setUp()
    owner=None;thread=None
    try:
        cid='cv_'+'a'*32
        # This seam starts from an owned synthetic native row, not a native
        # creation/account certificate; real creation admission is separate.
        with case.connect() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash) VALUES(?,1,1,'claude','anthropic','selected-model','high',?,'fixture','fixture')",(cid,str(case.root)))
            con.commit()
        with case.connect() as con:
            con.execute("UPDATE conversations SET runtime_mode='native_experiment',runtime_projection=? WHERE conversation_id=?",
                (json.dumps({'generation_id':'g','root_id':'root','state':'ready','capabilities':CAPS}),cid))
            con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,created_at,updated_at) VALUES('g',?,1,1,'claude','{}','ready',1,1)",(cid,));con.commit()
        native=parser(case.root,cid);owner=Controller('g',tmp_path,native)
        owner.identity=native._identity;owner.context=native._context;owner.ready=True
        owner.journal.set('harness','claude');native._emit=owner.emit
        server=PrivateServer(owner,tmp_path/'sock');thread=threading.Thread(target=server.serve);thread.start()
        service=chats.NativeChatsService(case.db_path,case.root,None)
        client=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()),consumer=service.consumer)
        deadline=time.monotonic()+1
        while True:
            try:client.request('status',timeout=.1)
            except RuntimeContractError as exc:assert exc.code=='LEASE_FENCED';break
            except (FileNotFoundError,ConnectionRefusedError):assert time.monotonic()<deadline;time.sleep(.005)
        client.attach(service.store,1,1,deadline=time.monotonic()+1)
        monkeypatch.setattr(service,'attach',lambda generation:(client,1,1))
        monkeypatch.setattr(chats,'_SERVICE',service)
        snapshot(native);ingest(service,owner)
        status,_,detail=case.request('GET',f'/api/conversations/{cid}')
        assert status==200,detail
        runtime=detail['runtime'];work=next(row for row in runtime['work'] if row['reference']['work_id']=='target')
        body=ui_body(runtime,work);body['version']=detail['version']
        status,_,result=case.request('POST',f'/api/conversations/{cid}/runtime-controls',body=body,key='one-request')
        assert status==202 and result['state']=='unknown',result
        assert len(native._notifications)==1
        status,_,duplicate=case.request('POST',f'/api/conversations/{cid}/runtime-controls',body=body,key='one-request')
        assert status==202 and duplicate['duplicate'] and len(native._notifications)==1
        control=result['control_id']
        def hook(name,**fields):
            with native._condition:
                native._hook({'hook_event_name':name,'session_id':'root','cwd':str(case.root),**fields})
        hook('UserPromptSubmit',prompt_id='stop-turn',prompt=f'<channel source="{CHANNEL}" generation_id="g" conversation_id="{cid}" control_id="{control}"></channel>')
        hook('PostToolUse',prompt_id='stop-turn',tool_name='TaskStop',tool_use_id='tool',
            tool_input={'task_id':'target'},tool_response={'task_id':'target','task_type':'local_bash'})
        ingest(service,owner)
        status,_,pending=case.request('GET',f'/api/conversations/{cid}')
        assert not any(c['receipt'].get('outcome')=='complete' for c in pending['runtime']['controls'])
        hook('Stop',prompt_id='stop-turn',background_tasks=[{'id':'sibling','type':'shell','status':'running'}],session_crons=[])
        ingest(service,owner)
        status,_,resolved=case.request('GET',f'/api/conversations/{cid}')
        receipt=resolved['runtime']['controls'][0]['receipt']
        assert receipt['native_outcome']=='native_stopped' and receipt['os_verified'] is False
        assert native._work['sibling'].state=='running'
        assert resolved['runtime']['state']=='ready' and receipt['snapshot_complete'] is True
    finally:
        if owner:owner.shutdown.set()
        if thread:thread.join(1);assert not thread.is_alive()
        if owner:owner.journal.db.close()
        case.doCleanups()

@pytest.mark.parametrize('database',['claude'],indirect=True)
def test_fresh_chromium_uses_actual_normalized_store_projection(pipeline,tmp_path):
    import shutil
    if not shutil.which('node'):pytest.skip('Node required')
    check=subprocess.run(['node','-e',"require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright')"],capture_output=True,text=True,check=False)
    if check.returncode:pytest.skip('Use the named ephemeral Playwright module')
    con,_native,_owner,_service=pipeline;runtime,row=public(con)
    detail={'conversation_id':'cv','version':row['version'],'state':'idle','runtime_mode':'native_experiment',
            'route':{'harness':'claude'},'runtime':runtime}
    file=tmp_path/'public-source.json';file.write_text(json.dumps(detail))
    result=subprocess.run(['node',str(ROOT/'tests/browser/claude_timestamped_controls.cjs'),str(ROOT),str(file)],
                          capture_output=True,text=True,check=False,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    evidence=json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence['native_launches']==0 and evidence['negative_cases']==12 and evidence['duplicate_writes']==0


def test_claude_probe_is_provider_scoped_consumed_source(seat):
    import shutil
    value,_events,_plan=seat
    root=value.root
    shutil.copytree(ROOT/'.super-coder/adapters/claude',root/'.super-coder/adapters/claude')
    shutil.copytree(ROOT/'.super-coder/assets/runtime/claude',root/'.super-coder/assets/runtime/claude')
    module=root/'.super-coder/scripts/conversation_runtime_claude_probe.py'
    assert module in value.implementation_files('claude')
    assert module not in value.implementation_files('codex')
    before=value.implementation_digest('claude')
    module.write_text(module.read_text()+'\n# source mutation\n')
    assert value.implementation_digest('claude')!=before
    module.unlink()
    with pytest.raises(RuntimeContractError) as raised:value.implementation_digest('claude')
    assert raised.value.code=='SOURCE_INVALID'
    module.symlink_to(ROOT/'.super-coder/scripts/conversation_runtime_claude_probe.py')
    with pytest.raises(RuntimeContractError) as raised:value.implementation_digest('claude')
    assert raised.value.code=='SOURCE_INVALID'
