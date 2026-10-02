"""Journal outage, delivery ambiguity, tenancy/fencing and Close independence."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'.super-coder/scripts'
sys.path.insert(0,str(SCRIPTS))
from conversation_runtime import RuntimeClient, RuntimeStore
from conversation_runtime_contract import (
    CONTRACT_REVISION,
    DriverStart,
    ExecutableBinding,
    NativeCleanup,
    NativeReference,
    NativeSnapshot,
    RuntimeContext,
    RuntimeContractError,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    StartupConsent,
    WriteReceipt,
    payload_digest,
)
from conversation_runtime_controller import (
    Controller,
    Journal,
    PrivateServer,
    start_ticks,
)


class TestDriver(RuntimeDriver):
    __test__=False
    harness='codex'
    revision='test-only'
    def __init__(self):
        self.writes=[]
        self.controls=[]
        self.emit=None
        self.entered=threading.Event()
        self.release=threading.Event()
        self.block=False
    def start(self,context,emit,*,deadline):
        self.emit=emit
        return DriverStart('ready',RuntimeIdentity('root'))
    def submit(self,command,*,deadline):
        self.writes.append(command.request_id)
        if self.block:
            self.entered.set()
            self.release.wait(3)
        return WriteReceipt('written',True,'A')
    def inventory(self,*,deadline):
        return NativeSnapshot(RuntimeIdentity('root'),None)
    def control(self,command,*,deadline):
        self.controls.append(command.control_id)
        return WriteReceipt('written',True)
    def cleanup(self,*,deadline):
        return NativeCleanup('complete')


def context(root):
    return RuntimeContext('g','cv',1,1,'codex',root,root,ExecutableBinding(Path('/bin/true'),'a'*64,'fixture'),
                          'test-only','b'*64,'c'*64,'unrestricted')


def wire(op,**fields):
    return {'op':op,'generation':'g','contract':CONTRACT_REVISION,'consumer':'api','fence':1}|fields


def command(cid,n,**fields):
    result=dict(request_id=cid,request_sequence=n,**fields)
    return result|{'payload_digest':payload_digest(result)}


def control(cid,n,**fields):
    result=dict(control_id=cid,request_sequence=n,**fields)
    return result|{'payload_digest':payload_digest(result)}


@pytest.fixture
def controller(tmp_path):
    tmp_path.chmod(0o700)
    driver=TestDriver()
    owner=Controller('g',tmp_path,driver)
    owner.handle(wire('attach',expires=time.time()+30))
    owner.context=context(tmp_path)
    owner.identity=RuntimeIdentity('root')
    owner.ready=True
    yield owner,driver
    driver.release.set()


def test_unknown_write_survives_reopen_and_compacted_resolved_id_rejects(tmp_path):
    tmp_path.chmod(0o700)
    path=tmp_path/'journal.sqlite'
    journal=Journal(path,max_commands=6,reserve=2)
    assert journal.reserve_command('uncertain',1,'d','submit') is None
    journal.receipt('uncertain',WriteReceipt('unknown'))
    journal.db.close()
    reopened=Journal(path,max_commands=6,reserve=2)
    assert reopened.reserve_command('uncertain',1,'d','submit')['duplicate']
    reopened.reserve_command('done',2,'e','submit')
    reopened.receipt('done',WriteReceipt('rejected'))
    reopened.ack(0)
    assert reopened.get('floor')==0 # uncertain prefix must not disappear
    reopened.receipt('uncertain',WriteReceipt('rejected'))
    reopened.ack(0)
    assert reopened.get('floor')==2
    with pytest.raises(RuntimeContractError,match='cannot be replayed'):
        reopened.reserve_command('another-id',1,'z','submit')


def test_outage_overflow_marks_partial_refuses_send_preserves_unknown_and_close(tmp_path):
    tmp_path.chmod(0o700)
    journal=Journal(tmp_path/'journal.sqlite',max_events=6,max_commands=6,reserve=2,max_bytes=400000)
    journal.reserve_command('live',1,'d','submit')
    journal.receipt('live',WriteReceipt('unknown'))
    for _ in range(9):
        journal.emit(RuntimeEvent('output.delta',NativeReference('root',activity_id='A'),data={'text':'x'}))
    assert journal.replay(0)['partial']
    assert len(journal.replay(0)['events'])==4
    with pytest.raises(RuntimeContractError,match='dispatch refused'):
        journal.reserve_command('new',2,'e','submit')
    journal.reserve_command('close',2,'e','control',closing=True)
    journal.emit(RuntimeEvent('control.outcome',control_id='close',data={'outcome':'inconclusive'}))
    assert journal.reserve_command('live',1,'d','submit')['state']=='unknown'
    journal.ack(5)
    assert journal.get('floor')==0


def test_primary_native_race_stale_stop_and_transport_ack_not_terminal(controller):
    owner,driver=controller
    first=command('send',1,text='hi')
    assert owner.handle(wire('submit',command=first))['state']=='written'
    assert owner.handle(wire('submit',command=first))['duplicate']
    assert driver.writes==['send']
    owner.emit(RuntimeEvent('activity.started',NativeReference('root',activity_id='A'),request_id='send'))
    owner.emit(RuntimeEvent('activity.started',NativeReference('root',activity_id='B'),source='automation'))
    owner.emit(RuntimeEvent('activity.terminal',NativeReference('root',activity_id='A'),request_id='send'))
    assert owner.journal.get('primary')=='B'
    stop=control('stop',2,action='stop_reply',target={'root_id':'root','activity_id':'A'},expected_activity_id='A')
    assert owner.handle(wire('control',command=stop))['state']=='rejected'
    assert not driver.controls
    unknown=control('current-stop',3,action='stop_reply',target={'root_id':'root','activity_id':'B'},expected_activity_id='B')
    assert owner.handle(wire('control',command=unknown))['acknowledged']
    assert owner.journal.get('primary')=='B'


def test_close_bypasses_blocked_submission_and_old_consumer_fenced(controller):
    owner,driver=controller
    driver.block=True
    thread=threading.Thread(target=lambda:owner.handle(wire('submit',command=command('slow',1,text='hi'),timeout=.2)))
    thread.start()
    assert driver.entered.wait(1)
    started=time.monotonic()
    result=owner.handle(wire('close',command=control('close',2,action='close'),timeout=.2))
    assert result['outcome']=='complete'
    assert time.monotonic()-started<.15
    thread.join(1)
    assert owner.journal.get('close')
    owner.handle(wire('attach',consumer='replacement',fence=2,expires=time.time()+30))
    with pytest.raises(RuntimeContractError,match='expired or replaced'):
        owner.handle(wire('status'))


def test_ingress_rejects_mutated_digest_and_unknown_source_before_reservation(controller):
    owner,_=controller
    bad=command('bad',1,text='hi',source='invented')
    with pytest.raises(RuntimeContractError,match='valid source'):
        owner.handle(wire('submit',command=bad))
    assert owner.journal.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0]==0
    bad=command('bad',1,text='hi');bad['text']='changed'
    with pytest.raises(RuntimeContractError,match='digest mismatch'):
        owner.handle(wire('submit',command=bad))


def test_private_socket_reconnect_same_controller_and_replay_once(controller,tmp_path):
    import os
    owner,driver=controller
    server=PrivateServer(owner,tmp_path/'controller.sock')
    thread=threading.Thread(target=server.serve)
    thread.start()
    try:
        for _ in range(100):
            if server.endpoint.exists():break
            time.sleep(.005)
        client=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()))
        client.lease={'consumer':'api','fence':1}
        client.request('submit',command=command('once',1,text='hi'))
        # API client disappears; controller descriptors and dedup remain.
        del client
        owner.emit(RuntimeEvent('activity.terminal',NativeReference('root',activity_id='A'),request_id='once'))
        replacement=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()))
        replacement.lease={'consumer':'api','fence':1}
        assert replacement.request('submit',command=command('once',1,text='hi'))['duplicate']
        assert driver.writes==['once']
        replay=replacement.request('subscribe',after=0)
        assert len(replay['events'])==1
        replacement.request('ack',sequence=1)
        assert replacement.request('subscribe',after=1)['events']==[]
        driver.block=True;driver.release.clear()
        started=time.monotonic()
        assert replacement.request('submit',command=command('bounded',2,text='hi'),timeout=.04)['state']=='unknown'
        assert time.monotonic()-started<.2
        driver.release.set()
    finally:
        owner.shutdown.set();thread.join(2)


def test_native_open_deadline_is_inconclusive_and_close_remains_available(controller,tmp_path):
    import os
    owner,driver=controller
    owner.context=None
    owner.identity=None
    owner.ready=False
    entered=threading.Event()
    released=threading.Event()
    def blocked_start(context,emit,*,deadline):
        entered.set()
        released.wait(2)
        return DriverStart('ready',RuntimeIdentity('root'))
    driver.start=blocked_start
    server=PrivateServer(owner,tmp_path/'deadline.sock')
    thread=threading.Thread(target=server.serve)
    thread.start()
    try:
        for _ in range(100):
            if server.endpoint.exists():break
            time.sleep(.005)
        client=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()))
        client.lease={'consumer':'api','fence':1}
        captured=dataclasses.asdict(context(tmp_path))
        for key in ('state_root','worktree'):
            captured[key]=str(captured[key])
        captured['executable']['path']=str(captured['executable']['path'])
        with pytest.raises(RuntimeContractError) as timed_out:
            client.request('open',context=captured,timeout=.05)
        assert entered.is_set()
        assert timed_out.value.code=='NATIVE_DEADLINE_INCONCLUSIVE'
        assert owner.context is not None and not owner.ready
        # Captured ownership remains available for exact scoped cleanup.
        assert client.request('status')['ready'] is False
        closed=client.request('close',command=control('close',1,action='close'))
        assert closed['outcome']=='complete'
        assert owner.journal.get('close')
        # Invalid shapes retain their distinct request classification.
        with pytest.raises(RuntimeContractError) as invalid:
            client.request('attach',expires='invalid-expiry')
        assert invalid.value.code=='REQUEST_INVALID'
    finally:
        released.set()
        owner.shutdown.set()
        thread.join(2)


def test_schema_reapplication_store_tenancy_fencing_and_replay_transaction(tmp_path):
    database=tmp_path/'fixture.sqlite'
    con=sqlite3.connect(database)
    con.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY); CREATE TABLE shells(shell_id INTEGER PRIMARY KEY); CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,state TEXT,harness TEXT DEFAULT "codex",provider TEXT,model TEXT,effort TEXT,worktree TEXT); INSERT INTO users VALUES(1); INSERT INTO shells VALUES(1); INSERT INTO conversations(conversation_id,shell_id,owner_user_id,state) VALUES("cv",1,1,"idle");')
    migration=(SCRIPTS.parent/'migrations/0273_conversation_native_runtime.sql').read_text()
    con.executescript(migration);con.executescript(migration)
    con.execute('UPDATE conversations SET worktree=?',(str(tmp_path),));con.commit();con.close()
    store=RuntimeStore(database)
    store.reserve(context(tmp_path),{'unit':'own.service'})
    lease=store.attach('g',1,1,'api',lifetime=1)
    with pytest.raises(RuntimeContractError,match='tenancy'):
        store.status('g',2,1)
    with pytest.raises(RuntimeContractError,match='another API'):
        store.attach('g',1,1,'another')
    intent=store.intent('g',1,1,lease,'same','submit',{'text':'hi'})
    assert store.intent('g',1,1,lease,'same','submit',{'text':'hi'})==intent
    with pytest.raises(RuntimeContractError,match='payload changed'):
        store.intent('g',1,1,lease,'same','submit',{'text':'different'})
    replay={'events':[{'sequence':1,'event':dataclasses.asdict(RuntimeEvent('activity.terminal',NativeReference('root',activity_id='A'),request_id='same'))}]}
    projected=[]
    assert store.ingest('g',1,1,lease,replay,project=lambda *args:projected.append(args[2]))==1
    assert store.ingest('g',1,1,lease,replay,project=lambda *args:projected.append(args[2]))==1
    assert projected==[1]
    assert store.status('g',1,1)['last_sequence']==1


def test_native_primary_busy_retries_proved_no_write_same_intent_in_order(controller):
    owner,driver=controller
    owner.emit(RuntimeEvent('activity.started',NativeReference('root',activity_id='autonomous'),source='automation'))
    first=command('first',1,text='first')
    second=command('second',2,text='second')
    assert owner.handle(wire('submit',command=first))['state']=='not_written'
    assert owner.handle(wire('submit',command=second))['state']=='not_written'
    assert not driver.writes
    owner.emit(RuntimeEvent('activity.terminal',NativeReference('root',activity_id='autonomous'),source='automation'))
    assert owner.handle(wire('submit',command=second))['state']=='not_written'
    assert owner.handle(wire('submit',command=first))['state']=='written'
    owner.emit(RuntimeEvent('activity.terminal',NativeReference('root',activity_id='A'),request_id='first'))
    assert owner.handle(wire('submit',command=second))['state']=='written'
    assert driver.writes==['first','second']
    assert owner.handle(wire('submit',command=second))['duplicate']


def test_child_activity_does_not_replace_or_clear_root_primary(controller):
    owner,_=controller
    owner.emit(RuntimeEvent('activity.started',NativeReference('root',thread_id='root',activity_id='root-turn')))
    child=NativeReference('root',thread_id='child',parent_thread_id='root',activity_id='child-turn')
    owner.emit(RuntimeEvent('activity.started',child))
    owner.emit(RuntimeEvent('activity.terminal',child))
    assert owner.journal.get('primary')=='root-turn'


def test_waiting_dispatch_consumer_is_fenced_before_native_edge(controller):
    owner,driver=controller
    errors=[]
    owner.dispatch_lock.acquire()
    def old_request():
        try:owner.handle(wire('submit',command=command('stale',1,text='old')))
        except RuntimeContractError as exc:errors.append(exc.code)
    thread=threading.Thread(target=old_request);thread.start()
    time.sleep(.03)
    owner.handle(wire('attach',consumer='replacement',fence=2,expires=time.time()+30))
    owner.dispatch_lock.release();thread.join(1)
    assert errors==['LEASE_FENCED'] and not driver.writes


def test_projection_preserves_scoped_opaque_handles_and_terminal_monotonicity(tmp_path):
    database=tmp_path/'fixture.sqlite';con=sqlite3.connect(database)
    con.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY); CREATE TABLE shells(shell_id INTEGER PRIMARY KEY); CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,state TEXT,harness TEXT DEFAULT "codex",provider TEXT,model TEXT,effort TEXT,worktree TEXT); INSERT INTO users VALUES(1); INSERT INTO shells VALUES(1); INSERT INTO conversations(conversation_id,shell_id,owner_user_id,state) VALUES("cv",1,1,"idle");')
    con.executescript((SCRIPTS.parent/'migrations/0273_conversation_native_runtime.sql').read_text())
    con.execute('UPDATE conversations SET worktree=?',(str(tmp_path),));con.commit();con.close()
    store=RuntimeStore(database);store.reserve(context(tmp_path),{})
    lease=store.attach('g',1,1,'api')
    store.intent('g',1,1,lease,'request','submit',{'text':'hi'})
    events=[]
    for thread in ['root','child']:
        events.append(RuntimeEvent('work.observed',NativeReference('root',thread_id=thread,native_process_id='same-opaque'),data={'kind':'terminal'}))
    events.extend([RuntimeEvent('activity.terminal',NativeReference('root',activity_id='A'),request_id='request'),
                   RuntimeEvent('activity.processed',NativeReference('root',activity_id='A'),request_id='request')])
    store.ingest('g',1,1,lease,{'events':[{'sequence':i+1,'event':dataclasses.asdict(e)} for i,e in enumerate(events)]})
    con=sqlite3.connect(database)
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_work').fetchone()[0]==2
    assert con.execute('SELECT state FROM conversation_runtime_commands').fetchone()[0]=='terminal'
    con.close()
    store.intent('g',1,1,lease,'stop','control',{'action':'stop_work'})
    store.receipt('g',1,1,'stop',{'state':'written','acknowledged':True})
    def outcome(n,value):
        event=RuntimeEvent('control.outcome',control_id='stop',data={'outcome':value})
        store.ingest('g',1,1,lease,{'events':[{'sequence':n,'event':dataclasses.asdict(event)}]})
    outcome(5,'inconclusive')
    assert store.command_status('g',1,1,'stop')['state']=='written'
    outcome(6,'complete');outcome(7,'pending')
    store.receipt('g',1,1,'stop',{'state':'written','acknowledged':True})
    original=store.command_status('g',1,1,'stop')
    assert original['state']=='terminal' and original['receipt']['outcome']=='complete'
    client=RuntimeClient(tmp_path/'unused.sock','g',controller_pid=1,controller_start_ticks=1)
    client.lease=lease
    def compacted(*args,**kwargs):
        raise RuntimeContractError('COMMAND_COMPACTED','resolved command below rejection boundary')
    client.request=compacted
    for _ in range(5):
        result=client.control(store,1,1,'stop',action='stop_work')
        assert result['state']=='terminal' and result['receipt']['outcome']=='complete' and result['compacted']
    assert store.command_status('g',1,1,'stop')==original


def test_expired_admission_never_reserves_or_writes_and_ingress_bounds_deadline(controller):
    owner,driver=controller
    owner.dispatch_lock.acquire()
    try:
        started=time.monotonic()
        with pytest.raises(RuntimeContractError,match='waiting for dispatch admission'):
            owner.handle(wire('submit',command=command('expired',1,text='hi'),timeout=.03))
        assert time.monotonic()-started<.15
    finally:
        owner.dispatch_lock.release()
    assert not driver.writes and owner.journal.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0]==0
    for bad in [0,-1,True,float('nan'),float('inf'),121,'1']:
        with pytest.raises(RuntimeContractError,match='positive finite timeout'):
            owner.handle(wire('status',timeout=bad))


def test_claude_setup_choice_is_pre_ready_captured_once_and_never_implies_readiness(controller):
    owner,driver=controller
    owner.ready=False
    owner.context=dataclasses.replace(owner.context,harness='claude');driver.harness='claude'
    setup=StartupConsent('g','epoch','a'*64,'test-only','b'*64,time.time())
    owner.emit(RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup)))
    assert owner.status()['setup']['setup_id']=='epoch' and not owner.ready
    assert owner.handle(wire('submit',command=command('queued',1,text='hi')))['state']=='not_written'
    def choice(cid,n,**options):
        return control(cid,n,action='enable_local_channel',options={'setup_id':'epoch','configuration_sha256':'b'*64}|options)
    assert owner.handle(wire('control',command=choice('stale',2,setup_id='old')))['state']=='rejected'
    current=choice('consent',3)
    assert owner.handle(wire('control',command=current))['state']=='written'
    assert not owner.ready and owner.status()['setup_confirmation']['control_id']=='consent'
    assert owner.handle(wire('control',command=current))['duplicate']
    assert owner.handle(wire('control',command=choice('duplicate-choice',4)))['state']=='rejected'
    assert driver.controls==['consent'] and not driver.writes
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root'),provenance='test successful readiness challenge'))
    assert owner.ready and owner.status()['setup'] is None


def test_setup_partial_choice_retains_fence_and_close_remains_available(controller):
    owner,driver=controller
    owner.ready=False
    owner.context=dataclasses.replace(owner.context,harness='claude');driver.harness='claude'
    owner.emit(RuntimeEvent('runtime.setup',data=dataclasses.asdict(StartupConsent('g','epoch','a'*64,'test-only','b'*64,time.time()))))
    def partial(command,**kwargs):
        driver.controls.append(command.control_id)
        raise OSError('partial finite choice write')
    driver.control=partial
    action=control('consent',1,action='enable_local_channel',options={'setup_id':'epoch','configuration_sha256':'b'*64})
    assert owner.handle(wire('control',command=action))['state']=='unknown'
    assert owner.handle(wire('control',command=action))['duplicate'] and driver.controls==['consent']
    assert owner.handle(wire('close',command=control('close',2,action='close')))['outcome']=='complete'
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root')))
    assert not owner.ready and owner.journal.get('close')


def test_withdrawn_startup_phase_blocks_choice_and_late_setup_cannot_regress_ready(controller):
    owner,driver=controller
    owner.context=dataclasses.replace(owner.context,harness='claude');driver.harness='claude'
    data=dataclasses.asdict(StartupConsent('g','epoch','a'*64,'test-only','b'*64,time.time()))
    owner.ready=False
    owner.emit(RuntimeEvent('runtime.setup',data=data))
    owner.emit(RuntimeEvent('runtime.setup',data=data,freshness='stale',partial=True,grade='inconclusive'))
    assert owner.status()['setup'] is None and not owner.ready
    action=control('stale-choice',1,action='enable_local_channel',options={'setup_id':'epoch','configuration_sha256':'b'*64})
    assert owner.handle(wire('control',command=action))['state']=='rejected' and not driver.controls
    owner.emit(RuntimeEvent('runtime.setup',data=data|{'setup_id':'current-epoch'}))
    assert owner.status()['setup']['setup_id']=='current-epoch'
    owner.emit(RuntimeEvent('runtime.setup',data=data,grade='inconclusive'))
    assert owner.status()['setup'] is None
    owner.emit(RuntimeEvent('runtime.setup',data=data|{'setup_id':'current-epoch'}))
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root')))
    owner.emit(RuntimeEvent('runtime.setup',data=data,freshness='stale',partial=True,grade='inconclusive'))
    assert owner.ready and owner.status()['setup'] is None


def test_async_readiness_retains_selected_observations_without_changing_native_identity(controller):
    owner,driver=controller
    owner.context=dataclasses.replace(owner.context,harness='claude',model='observed-model',effort='high');driver.harness='claude'
    owner.identity=RuntimeIdentity('root','native-session',protocol={'auth_observation':{'method':'claude.ai'}})
    owner.ready=False
    route={'account_type':'claude.ai','model':'observed-model','efforts':['high'],
           'observation_origin':'claude:auth-status+SessionStart+readiness-Stop',
           'model_evidence':'active_selected_model','effort_evidence':'effective_selected_level',
           'catalogue_observed':False,'auth':{'method':'claude.ai','provider':'firstParty'}}
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root',thread_id='child'),data={'native_route':route}))
    assert not owner.ready and 'native_route' not in owner.identity.protocol
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root'),data={'native_route':route},partial=True))
    assert not owner.ready
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root'),data={'native_route':route}))
    identity=owner.status()['identity']
    assert owner.ready and identity['root_id']=='root' and identity['session_id']=='native-session'
    assert identity['protocol']['native_route']==route
    assert identity['protocol']['auth_observation']=={'method':'claude.ai'}
    owner.handle(wire('close',command=control('close',1,action='close')))
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root'),data={'native_route':route|{'model':'late'}}))
    assert not owner.ready and owner.identity.protocol['native_route']['model']=='observed-model'


@pytest.mark.parametrize('change',[{'efforts':[]},{'auth':{'method':'claude.ai'}},{'model':'bad\nmodel'}])
def test_readiness_rejects_unbounded_or_unsanitized_route_before_journal(controller,change):
    owner,_=controller
    before=owner.journal.get('sequence')
    with pytest.raises(RuntimeContractError,match='native'):
        owner.emit(RuntimeEvent('runtime.ready',NativeReference('root',thread_id='root'),data={
            'native_route':{'account_type':'chatgpt','model':'observed','efforts':['high']}|change}))
    assert owner.journal.get('sequence')==before


def test_added_native_observations_are_ignored_before_persistence(controller):
    owner,_=controller
    owner.context=dataclasses.replace(owner.context,model='selected',effort='high')
    owner.ready=False
    route={'account_type':'chatgpt','model':'selected','efforts':['high'],'future_observation':'private-unused',
           'auth':{'method':'chatgpt','provider':'openai','future_field':'private-unused'}}
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root',thread_id='root'),data={'native_route':route}))
    assert owner.ready and not owner.lost
    assert 'future_observation' not in owner.identity.protocol['native_route']
    assert 'future_field' not in owner.identity.protocol['native_route']['auth']
    assert 'private-unused' not in json.dumps(owner.journal.replay(0))


@pytest.mark.parametrize('change,grade',[({'model':'other'},'unverified'),({'efforts':['low']},'unverified'),
    ({'account_type':'claude.ai'},'unverified'),({},'incompatible'),({},'inconclusive')])
def test_async_readiness_cannot_admit_mismatched_or_failed_native_observation(controller,change,grade):
    owner,_=controller
    owner.context=dataclasses.replace(owner.context,model='selected',effort='high')
    owner.ready=False
    route={'account_type':'chatgpt','model':'selected','efforts':['high']}|change
    owner.emit(RuntimeEvent('runtime.ready',NativeReference('root',thread_id='root'),data={'native_route':route},grade=grade))
    assert not owner.ready and not owner.lost and 'native_route' not in owner.identity.protocol
    assert owner.handle(wire('close',command=control('close',1,action='close')))['outcome']=='complete'


def test_startup_projection_binds_generation_binary_driver_and_preserves_close(tmp_path):
    database=tmp_path/'fixture.sqlite';con=sqlite3.connect(database)
    con.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY); CREATE TABLE shells(shell_id INTEGER PRIMARY KEY); CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,state TEXT,harness TEXT,provider TEXT,model TEXT,effort TEXT,worktree TEXT); INSERT INTO users VALUES(1); INSERT INTO shells VALUES(1); INSERT INTO conversations(conversation_id,shell_id,owner_user_id,state,harness) VALUES("cv",1,1,"idle","claude");')
    con.executescript((SCRIPTS.parent/'migrations/0273_conversation_native_runtime.sql').read_text())
    con.execute('UPDATE conversations SET worktree=?',(str(tmp_path),));con.commit();con.close()
    store=RuntimeStore(database);store.reserve(dataclasses.replace(context(tmp_path),harness='claude'),{})
    lease=store.attach('g',1,1,'api')
    setup=StartupConsent('g','epoch','a'*64,'test-only','b'*64,time.time())
    def event(sequence,value):
        store.ingest('g',1,1,lease,{'events':[{'sequence':sequence,'event':dataclasses.asdict(value)}]})
    for wrong in [dataclasses.replace(setup,generation_id='other'),dataclasses.replace(setup,executable_sha256='c'*64),dataclasses.replace(setup,driver_revision='other')]:
        with pytest.raises(RuntimeContractError,match='differs from captured'):
            event(1,RuntimeEvent('runtime.setup',data=dataclasses.asdict(wrong)))
    assert store.status('g',1,1)['last_sequence']==0
    event(1,RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup)))
    assert store.status('g',1,1)['state']=='needs_consent'
    event(2,RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup),freshness='stale',partial=True,grade='inconclusive'))
    assert store.status('g',1,1)['state']=='setup_inconclusive'
    event(3,RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup),grade='inconclusive'))
    assert store.status('g',1,1)['state']=='setup_inconclusive'
    event(4,RuntimeEvent('runtime.setup',data=dataclasses.asdict(setup)))
    assert store.status('g',1,1)['state']=='needs_consent'
    event(5,RuntimeEvent('runtime.ready',NativeReference('root'),grade='incompatible'))
    assert store.status('g',1,1)['state']=='setup_inconclusive'
    event(6,RuntimeEvent('runtime.ready',NativeReference('root'),partial=True))
    assert store.status('g',1,1)['state']=='setup_inconclusive'
    store.state('g',1,1,'needs_consent')
    store.intent('g',1,1,lease,'close','close',{'action':'close'})
    store.state('g',1,1,'closing')
    event(7,RuntimeEvent('runtime.ready',NativeReference('root')))
    assert store.status('g',1,1)['state']=='closing'


def test_late_start_or_processing_cannot_resurrect_terminal_root(controller):
    owner,_=controller
    ref=NativeReference('root',thread_id='root',activity_id='A')
    owner.emit(RuntimeEvent('activity.started',ref))
    owner.emit(RuntimeEvent('activity.terminal',ref))
    owner.emit(RuntimeEvent('activity.processed',ref))
    owner.emit(RuntimeEvent('activity.started',ref))
    assert owner.journal.get('primary') is None


def test_lost_generation_replacement_waits_for_os_and_definition_cleanup(tmp_path):
    database=tmp_path/'fixture.sqlite';con=sqlite3.connect(database)
    con.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY); CREATE TABLE shells(shell_id INTEGER PRIMARY KEY); CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,state TEXT,harness TEXT DEFAULT "codex",provider TEXT,model TEXT,effort TEXT,worktree TEXT); INSERT INTO users VALUES(1); INSERT INTO shells VALUES(1); INSERT INTO conversations(conversation_id,shell_id,owner_user_id,state) VALUES("cv",1,1,"idle");')
    con.executescript((SCRIPTS.parent/'migrations/0273_conversation_native_runtime.sql').read_text())
    con.execute('UPDATE conversations SET worktree=?',(str(tmp_path),));con.commit();con.close()
    store=RuntimeStore(database);first=context(tmp_path);store.reserve(first,{})
    store.state('g',1,1,'lost',{'outcome':'pending','unit_verified_exited':False})
    replacement=dataclasses.replace(first,generation_id='new')
    with pytest.raises(RuntimeContractError,match='ownership remains'):
        store.reserve(replacement,{})
    store.state('g',1,1,'lost',{'outcome':'complete','unit_verified_exited':True,'unresolved_definitions':['owned-definition']})
    with pytest.raises(RuntimeContractError,match='ownership remains'):
        store.reserve(replacement,{})
    store.state('g',1,1,'lost',{'outcome':'complete','unit_verified_exited':True})
    assert store.reserve(replacement,{})=='new'


@pytest.mark.parametrize('boundary',['before_open','queued_native_edge'])
def test_close_before_first_native_start_fences_open(controller,tmp_path,monkeypatch,boundary):
    owner,driver=controller
    owner.context=None;owner.identity=None;owner.ready=False
    starts=[]
    def start(context,emit,*,deadline):
        starts.append(context.generation_id)
        return DriverStart('ready',RuntimeIdentity('root'))
    monkeypatch.setattr(driver,'start',start)
    if boundary=='before_open':
        assert owner.handle(wire('close',command=control('close',1,action='close')))['outcome']=='complete'
    else:
        def queued_edge(function,*,deadline,**fields):
            owner.journal.reserve_command('close',1,'c'*64,'control',closing=True)
            return function()
        monkeypatch.setattr(owner,'call',queued_edge)
    with pytest.raises(RuntimeContractError) as raised:
        owner.handle(wire('open',context=json.loads(json.dumps(dataclasses.asdict(context(tmp_path)),default=str))))
    assert raised.value.code=='RUNTIME_CLOSING'
    assert starts==[] and owner.ready is False
    assert owner.handle(wire('status'))['closing'] is True
