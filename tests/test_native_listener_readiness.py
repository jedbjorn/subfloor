"""Finite real Unix listener checks; synthetic driver/DB, no managed processes."""
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.super-coder/scripts'))
import conversation_runtime_controller as controller_mod
from conversation_native_chats import NativeChatsService
from conversation_runtime import RuntimeClient, attach_connection
from conversation_runtime_contract import RuntimeContractError
from conversation_runtime_controller import Controller, PrivateServer, start_ticks
from test_conversation_runtime_foundation import TestDriver
from test_gui_experiment import seat as seat  # noqa: PLC0414
from test_native_chat_ownership import database as database  # noqa: PLC0414


@pytest.fixture
def listening(tmp_path,monkeypatch):
    endpoint=tmp_path/'listener.sock';tmp_path.chmod(0o700)
    bound,release=threading.Event(),threading.Event()
    original=socket.socket
    class GatedSocket(original):
        def listen(self,backlog=16):
            bound.set()
            assert release.wait(2)
            return super().listen(backlog)
    monkeypatch.setattr(socket,'socket',GatedSocket)
    driver=TestDriver();owner=Controller('g',tmp_path,driver);server=PrivateServer(owner,endpoint)
    thread=threading.Thread(target=server.serve);thread.start();assert bound.wait(1)
    info=endpoint.lstat()
    native={'generation_id':'g','endpoint':str(endpoint),'unit':'synthetic-only',
            'main_pid':os.getpid(),'main_pid_start_ticks':start_ticks(os.getpid()),
            'endpoint_device':info.st_dev,'endpoint_inode':info.st_ino,'control_group':'synthetic-only'}
    client=RuntimeClient(endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()))
    try:yield owner,driver,client,native,release
    finally:
        release.set();owner.shutdown.set();thread.join(2);assert not thread.is_alive()
        end=time.monotonic()+1
        while server.slots._value!=16 and time.monotonic()<end:time.sleep(.005)
        assert server.slots._value==16
        owner.journal.db.close()


def wait(client,native,seconds=.3):
    client.wait_listener(deadline=time.monotonic()+seconds,
        endpoint_device=native['endpoint_device'],endpoint_inode=native['endpoint_inode'])


def test_actual_bind_before_listen_is_not_ready_and_has_no_lease_or_native_effect(listening):
    owner,driver,client,native,_=listening
    before=list(owner.journal.db.iterdump());started=time.monotonic()
    with pytest.raises(RuntimeContractError) as exc:wait(client,native,.08)
    assert exc.value.code=='DEADLINE_EXPIRED' and time.monotonic()-started<.2
    assert list(owner.journal.db.iterdump())==before
    assert owner.context is None and owner.journal.get('lease') is None
    assert driver.emit is None and not driver.writes and not driver.controls


def test_actual_delayed_listen_returns_only_fenced_read_only_status(listening):
    owner,driver,client,native,release=listening
    before=list(owner.journal.db.iterdump());frames=[];handle=owner.handle
    def observe(value,**fields):frames.append(value.copy());return handle(value,**fields)
    owner.handle=observe
    timer=threading.Timer(.04,release.set);timer.start()
    try:wait(client,native)
    finally:timer.join()
    assert list(owner.journal.db.iterdump())==before
    assert len(frames)==1 and frames[0]['op']=='status'
    assert not set(frames[0])&{'consumer','fence','expires','context','command'}
    assert client.lease=={} and driver.emit is None


@pytest.mark.parametrize('mutation',['pid','ticks','inode','device','generation','contract','symlink','mode'])
def test_actual_captured_peer_endpoint_generation_and_contract_refuse(listening,monkeypatch,mutation):
    owner,_driver,client,native,release=listening;release.set()
    if mutation=='pid':client.controller_pid+=1
    elif mutation=='ticks':client.controller_start_ticks+=1
    elif mutation in {'inode','device'}:native['endpoint_'+mutation]+=1
    elif mutation=='generation':client.generation='foreign'
    elif mutation=='contract':monkeypatch.setattr(controller_mod,'CONTRACT_REVISION','foreign')
    elif mutation=='symlink':
        replacement=client.endpoint.with_name('saved.sock');client.endpoint.rename(replacement);client.endpoint.symlink_to(replacement)
    elif mutation=='mode':client.endpoint.chmod(0o666)
    with pytest.raises(RuntimeContractError):wait(client,native)
    assert owner.journal.get('lease') is None and owner.context is None


@pytest.mark.parametrize('frame',[{'error':'LEASE_FENCED','detail':'x'},
    {'ok':None,'error':'LEASE_FENCED','detail':'x'},{'ok':0,'error':'LEASE_FENCED','detail':'x'},
    {'ok':False,'error':'LEASE_FENCED','detail':None},
    {'ok':False,'error':'LEASE_FENCED','detail':'x','private':'x'},
    {'ok':False,'error':'GENERATION_INVALID','detail':'x'},
    {'ok':True,'result':{}},[],b'invalid\n',b'{"ok":false',None])
def test_actual_response_envelope_is_strict_and_stalled_io_has_no_grace(tmp_path,frame):
    endpoint=tmp_path/'bad.sock';ready=threading.Event();done=threading.Event();frames=[]
    def serve():
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.bind(str(endpoint));endpoint.chmod(0o600);sock.listen(1);ready.set()
            sock.settimeout(1)
            conn,_=sock.accept()
            with conn:
                frames.append(conn.recv(8192))
                if frame is None:done.wait(.3)
                else:conn.sendall(frame if isinstance(frame,bytes) else (json.dumps(frame)+'\n').encode())
    thread=threading.Thread(target=serve);thread.start();assert ready.wait(1)
    client=RuntimeClient(endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()))
    info=endpoint.lstat();native={'endpoint_device':info.st_dev,'endpoint_inode':info.st_ino}
    started=time.monotonic()
    try:
        with pytest.raises((RuntimeContractError,ConnectionResetError)):wait(client,native,.09)
        assert time.monotonic()-started<.25
    finally:done.set();thread.join(1);endpoint.unlink()
    assert len(frames)==1 and client.lease=={}


def service(database,listening):
    path,con=database;_,_,_client,native,_=listening
    con.execute('PRAGMA journal_mode=WAL')
    supervisor=SimpleNamespace(listener_identity=lambda generation,**_kwargs:native.copy())
    return NativeChatsService(path,path.parent,supervisor)


def test_service_does_not_write_lease_or_cache_client_before_listen(database,listening):
    value=service(database,listening);_,con=database;owner,_,_,_,release=listening
    try:
        with pytest.raises(RuntimeContractError):value.attach('g',deadline=time.monotonic()+.06)
        assert con.execute('SELECT consumer_id FROM conversation_runtime_generations').fetchone()[0] is None
        assert not value.clients and owner.journal.get('lease') is None
        release.set();client,_,_=value.attach('g',deadline=time.monotonic()+.4)
        assert client.lease['consumer']==value.consumer and owner.journal.get('lease')['fence']==1
        assert owner.context is None
    finally:value.shutdown()


def test_concurrent_first_attach_keeps_same_client_and_fence(database,listening):
    value=service(database,listening);owner,_,_,_,release=listening;release.set()
    frames=[];handle=owner.handle
    def observe(frame,**kwargs):frames.append(frame.copy());return handle(frame,**kwargs)
    owner.handle=observe;results=[];errors=[]
    def attach():
        try:results.append(value.attach('g',deadline=time.monotonic()+1))
        except (RuntimeContractError,OSError) as exc:errors.append(exc)
    threads=[threading.Thread(target=attach) for _ in range(2)]
    try:
        for thread in threads:thread.start()
        for thread in threads:thread.join(2);assert not thread.is_alive()
        assert not errors and len(results)==2 and results[0][0] is results[1][0]
        assert results[0][0].lease['fence']==1 and owner.journal.get('lease')['fence']==1
        assert [f['op'] for f in frames]==['status','attach','status','attach']
    finally:value.shutdown()


@pytest.mark.parametrize('mutation',['endpoint','pid','ticks','owner','shell'])
def test_service_final_identity_or_tenant_change_refuses_before_lease_write(database,listening,mutation):
    value=service(database,listening);_,con=database;owner,_,_,native,release=listening;release.set();calls=[]
    def identity(generation,**_kwargs):
        calls.append(generation)
        if len(calls)==2:
            if mutation in {'endpoint','pid','ticks'}:
                native[{'endpoint':'endpoint_inode','pid':'main_pid','ticks':'main_pid_start_ticks'}[mutation]]+=1
            else:
                con.execute('UPDATE conversation_runtime_generations SET '+{'owner':'owner_user_id','shell':'shell_id'}[mutation]+'=9');con.commit()
        return native.copy()
    value.supervisor.listener_identity=identity
    try:
        with pytest.raises(RuntimeContractError):value.attach('g',deadline=time.monotonic()+.4)
        assert con.execute('SELECT consumer_id FROM conversation_runtime_generations').fetchone()[0] is None
        assert owner.journal.get('lease') is None and not value.clients
    finally:value.shutdown()


def test_expired_deadline_never_connects_or_attaches(listening,monkeypatch):
    _,_,client,native,_=listening
    monkeypatch.setattr(client,'_exchange',lambda *_args,**_kwargs:pytest.fail('expired request cannot execute'))
    with pytest.raises(RuntimeContractError) as exc:wait(client,native,-1)
    assert exc.value.code=='DEADLINE_EXPIRED'


def test_busy_attach_writer_uses_remaining_budget_and_retains_ownership(database,listening):
    value=service(database,listening);_,con=database;owner,_,_,_,release=listening;release.set()
    con.execute('BEGIN IMMEDIATE');started=time.monotonic()
    try:
        with pytest.raises(RuntimeContractError) as exc:value.attach('g',deadline=started+.09)
        assert exc.value.code in {'CLEANUP_PENDING','DEADLINE_EXPIRED'}
        assert time.monotonic()-started<.25
        assert con.execute('SELECT consumer_id FROM conversation_runtime_generations').fetchone()[0] is None
        assert owner.journal.get('lease') is None and not value.clients
    finally:con.rollback();value.shutdown()


def test_non_wal_or_expired_attach_connection_does_not_configure_database(tmp_path):
    path=tmp_path/'not-serving.db';con=sqlite3.connect(path);con.execute('CREATE TABLE x(v)');con.commit()
    try:
        with pytest.raises(RuntimeContractError) as exc:attach_connection(path,time.monotonic()+.1)
        assert exc.value.code=='CLEANUP_PENDING'
        assert con.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        with pytest.raises(RuntimeContractError) as exc:attach_connection(path,time.monotonic()-1)
        assert exc.value.code=='DEADLINE_EXPIRED'
    finally:con.close()


@pytest.fixture
def supervised(request,listening,monkeypatch):
    # Unit observations/dispatch are synthetic; the protocol socket/client and
    # PID/ticks/cgroup reads are real. No systemd or native launch executes.
    import conversation_runtime as runtime
    from test_gui_experiment import fixture, marked
    seat=request.getfixturevalue('seat');record,root,receipt=marked(seat)
    owner,_driver,client,_native,release=listening
    generation='e'*32;owner.generation=generation;client.generation=generation
    record.update(runtime='experimental',expires_at=time.time()+30)
    fixture.write_json(root/fixture.MARKER,fixture.identity(record));fixture.save(record,receipt)
    monkeypatch.setattr(fixture,'native_endpoint',lambda _fid,_gid:client.endpoint)
    scripts=root/'.super-coder/scripts';scripts.mkdir(parents=True)
    for name in ('conversation_runtime.py','conversation_runtime_controller.py'):
        (scripts/name).write_bytes((ROOT/'.super-coder/scripts'/name).read_bytes())
    # Exercise the launcher's copied-source path guard against the actual
    # identical client implementation in this isolated synthetic fixture.
    monkeypatch.setattr(runtime,'__file__',str(scripts/'conversation_runtime.py'))
    supervisor=fixture.NativeSupervisor(receipt);native=supervisor.register(generation,'codex')
    dispatched=[];group=Path('/proc/self/cgroup').read_text().split('0::',1)[1].strip()
    active={'LoadState':'loaded','ActiveState':'active','Description':fixture.native_description(record,native),
            'MainPID':str(os.getpid()),'ControlGroup':group}
    def state(_native,**kwargs):
        assert 0<kwargs['timeout']<=10
        return active.copy() if dispatched else {'LoadState':'not-found','ActiveState':'inactive'}
    def dispatch(argv,**kwargs):
        assert argv[0]=='systemd-run' and 0<kwargs['timeout']<=10
        dispatched.append(argv)
    monkeypatch.setattr(fixture,'native_unit_state',state)
    monkeypatch.setattr(fixture,'command',dispatch)
    return fixture,supervisor,native,dispatched,active,release,owner


def test_supervisor_does_not_mark_bound_socket_active_without_listen(supervised):
    fixture,value,native,dispatched,_active,_release,owner=supervised
    with pytest.raises(fixture.FixtureError):value.launch(native['generation_id'],deadline=time.monotonic()+.07)
    saved=value.inventory()[0]
    assert len(dispatched)==1 and saved['status']=='starting'
    assert saved['main_pid']==os.getpid() and saved['endpoint_inode']==Path(saved['endpoint']).lstat().st_ino
    assert owner.journal.get('lease') is None and owner.context is None
    assert not saved.get('os_cleanup',{}).get('complete')


def test_supervisor_listening_success_binds_real_peer_before_active_and_no_native_ready(supervised):
    _fixture,value,native,dispatched,_active,release,owner=supervised;release.set()
    result=value.launch(native['generation_id'],deadline=time.monotonic()+.4)
    assert len(dispatched)==1 and result['status']=='active'
    identity=value.listener_identity(native['generation_id'],deadline=time.monotonic()+.2)
    assert identity['main_pid']==os.getpid() and identity['main_pid_start_ticks']==start_ticks(os.getpid())
    assert owner.journal.get('lease') is None and not owner.ready and owner.context is None


def test_supervisor_unit_change_after_read_only_status_retains_starting(supervised):
    fixture,value,native,_dispatched,active,release,owner=supervised;release.set()
    handle=owner.handle
    def mutate(frame,**kwargs):
        try:return handle(frame,**kwargs)
        finally:active['Description']='changed-after-status'
    owner.handle=mutate
    with pytest.raises(fixture.FixtureError):value.launch(native['generation_id'],deadline=time.monotonic()+.3)
    assert value.inventory()[0]['status']=='starting' and owner.journal.get('lease') is None


def test_supervisor_expiry_before_dispatch_never_calls_command(supervised,monkeypatch):
    fixture,value,native,dispatched,_active,_release,_owner=supervised
    def delayed_state(_native,**_kwargs):
        time.sleep(.04)
        return {'LoadState':'not-found','ActiveState':'inactive'}
    monkeypatch.setattr(fixture,'native_unit_state',delayed_state)
    with pytest.raises(fixture.FixtureError):value.launch(native['generation_id'],deadline=time.monotonic()+.01)
    assert not dispatched and value.inventory()[0]['status'] in {'registered','starting'}


def test_recovery_attach_fences_expired_consumer_without_open_or_command_replay(database,listening):
    value=service(database,listening);path,con=database;owner,driver,_,_,release=listening;release.set()
    recovered=NativeChatsService(path,path.parent,value.supervisor)
    try:
        first,_,_=value.attach('g',deadline=time.monotonic()+.3)
        con.execute('UPDATE conversation_runtime_generations SET consumer_expires=0');con.commit()
        second,_,_=recovered.attach('g',deadline=time.monotonic()+.3)
        assert second.lease['fence']==first.lease['fence']+1
        with pytest.raises(RuntimeContractError) as exc:first.request('status',timeout=.2)
        assert exc.value.code=='LEASE_FENCED'
        assert not owner.ready and owner.context is None and driver.emit is None
        assert not driver.writes and not driver.controls
    finally:value.shutdown();recovered.shutdown()


def test_close_stays_available_after_listener_failure_without_invented_cleanup(database,listening):
    value=service(database,listening);_,con=database;owner,driver,_,_,release=listening
    try:
        with pytest.raises(RuntimeContractError):value.attach('g',deadline=time.monotonic()+.04)
        assert json.loads(con.execute('SELECT cleanup_json FROM conversation_runtime_generations').fetchone()[0])=={}
        release.set();client,_,_=value.attach('g',deadline=time.monotonic()+.3)
        command=value.store.intent('g',1,1,client.lease,'close-key','close',{'action':'close'})
        result=client.request('close',command=command,timeout=.3)
        assert result['outcome']=='complete' and owner.journal.get('close') is True
        assert owner.context is None and not driver.writes
        # Native completion here is the synthetic driver's receipt. No OS,
        # capacity, preparation or definition cleanup proof is fabricated.
        assert json.loads(con.execute('SELECT cleanup_json FROM conversation_runtime_generations').fetchone()[0])=={}
    finally:value.shutdown()


@pytest.mark.parametrize('edge',['before_write','before_commit'])
def test_canonical_attach_expiry_rolls_back_at_final_sql_edges(database,listening,monkeypatch,edge):
    value=service(database,listening);_,con=database;owner,_,_,_,release=listening;release.set()
    if edge=='before_write':
        original=value.store._owned
        def owned(*args):
            row=original(*args);time.sleep(.04);return row
        monkeypatch.setattr(value.store,'_owned',owned)
    else:
        original_connect=sqlite3.connect
        class DelayedCommit(sqlite3.Connection):
            def execute(self,sql,*args):
                result=super().execute(sql,*args)
                if sql.startswith('UPDATE conversation_runtime_generations SET consumer_id='):time.sleep(.04)
                return result
        def connect(*args,**kwargs):
            return original_connect(*args,**kwargs,factory=DelayedCommit)
        monkeypatch.setattr(sqlite3,'connect',connect)
    try:
        with pytest.raises(RuntimeContractError) as exc:value.attach('g',deadline=time.monotonic()+.02)
        assert exc.value.code=='DEADLINE_EXPIRED'
        row=con.execute('SELECT consumer_id,consumer_fence FROM conversation_runtime_generations').fetchone()
        assert row['consumer_id'] is None and row['consumer_fence']==0
        assert owner.journal.get('lease') is None and not value.clients
    finally:value.shutdown()


def test_missing_or_aliased_canonical_db_is_not_created_or_used(tmp_path):
    missing=tmp_path/'absent.db'
    with pytest.raises(FileNotFoundError):attach_connection(missing,time.monotonic()+.1)
    assert not missing.exists()
    target=tmp_path/'real.db';con=sqlite3.connect(target);con.execute('PRAGMA journal_mode=WAL');con.close()
    alias=tmp_path/'alias.db';alias.symlink_to(target)
    with pytest.raises(RuntimeContractError) as exc:attach_connection(alias,time.monotonic()+.1)
    assert exc.value.code=='OWNERSHIP_INVALID'
