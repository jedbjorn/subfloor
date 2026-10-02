"""D419 shared transfer only: fake driver/owned temporary files, no native reads."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.super-coder/scripts'))
import conversation_history_baseline as files
from conversation_runtime import RuntimeClient
from conversation_runtime_contract import (
    CAP_HISTORY_RESUME,
    MAX_FRAME_BYTES,
    MAX_HISTORY_BASELINE_BYTES,
    ClaudeFileBoundary,
    DriverStart,
    ExecutableBinding,
    HistoryBaseline,
    HistoryRecordBoundary,
    NativeCleanup,
    NativeHistory,
    NativeSnapshot,
    RuntimeContext,
    RuntimeContractError,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    WorkspaceIdentity,
    public_payload,
    runtime_context_wire,
)
from conversation_runtime_controller import (
    Controller,
    PrivateServer,
    runtime_context,
    start_ticks,
)
from test_native_history_continuations import (
    seat as seat,  # noqa: PLC0414 -- pytest fixture export
)


def baseline(**changes):
    return HistoryBaseline('old-cv','old-g','old-root','private-native-session','codex',
                           'a'*64,'b'*64,'c'*64,'d'*64,3,
                           (HistoryRecordBoundary('turn','old-turn',state='completed'),),**changes)


def context(root, value=None):
    history=NativeHistory('old-cv','old-g','old-root','codex','model','high',root,'a'*64,'b'*64,'e'*64)
    return RuntimeContext('g','cv',1,1,'codex',root,root,ExecutableBinding(Path('/bin/true'),'c'*64,'fake'),
                          'fake','f'*64,'a'*64,'unrestricted',model='model',effort='high',
                          history=history,workspace=WorkspaceIdentity(root,root,'branch','a'*40),
                          history_baseline=value,capability_evidence={CAP_HISTORY_RESUME:'compatible'})


def file_root(path):
    path.chmod(0o700)
    st=path.stat()
    return files.GenerationBaselineRoot(path,'old-g',st.st_dev,st.st_ino)


def deadline():
    return time.monotonic()+2


def test_old_private_context_wire_omits_optional_field_and_roundtrips(tmp_path):
    old=dataclasses.replace(context(tmp_path),history=None,workspace=None,capability_evidence={})
    wire=runtime_context_wire(old)
    assert 'history_baseline' not in wire and 'history' not in wire and 'workspace' not in wire
    assert runtime_context(json.loads(json.dumps(wire)))==old
    private=context(tmp_path,baseline())
    assert runtime_context(json.loads(json.dumps(runtime_context_wire(private))))==private
    assert 'private-native-session' not in repr(private)


@pytest.mark.parametrize('change',[
    {'complete':False},{'complete':1},{'capture_sequence':True},{'configuration_sha256':'unknown'},
    {'native_session_id':'raw prompt with spaces'}, {'records':[HistoryRecordBoundary('turn','t')]},
    {'claude_file':'not-a-boundary'}, {'revision':'different'},
])
def test_typed_transfer_refuses_malformed_or_partial(change):
    with pytest.raises((RuntimeContractError,TypeError)):
        dataclasses.replace(baseline(),**change)


@pytest.mark.parametrize('field,value',[
    ('source_conversation_id','other'),('source_generation_id','other'),('native_root_id','other'),
    ('source_boot_digest','f'*64),('source_policy_digest','f'*64),('harness','claude'),
])
def test_context_requires_exact_server_history_binding(tmp_path,field,value):
    history=dataclasses.replace(context(tmp_path).history,**{field:value})
    with pytest.raises(RuntimeContractError):
        dataclasses.replace(context(tmp_path,baseline()),history=history)
    with pytest.raises(RuntimeContractError):
        dataclasses.replace(context(tmp_path,baseline()),history=None)


@pytest.mark.parametrize('change',[
    {'raw_text':'private'}, {'records':[{'kind':'turn','native_id':'t','text':'private'}]},
    {'complete':False}, {'claude_file':{}}, {'records':None}, {'records':True},
])
def test_wire_refuses_unknown_fields_types_and_does_not_echo_private_input(change):
    value=json.loads(baseline().private_bytes())|change
    with pytest.raises(RuntimeContractError) as caught:
        HistoryBaseline.from_private_wire(value)
    assert 'private' not in str(caught.value).replace('private history','')


def test_baseline_has_independent_and_whole_frame_bounds(tmp_path):
    # Maximal bounded ID rows still exceed the private baseline byte cap.
    rows=tuple(HistoryRecordBoundary('item','item-'+str(i)+'x'*240,parent_id='p'*255,content_sha256='a'*64) for i in range(512))
    with pytest.raises(RuntimeContractError):
        dataclasses.replace(baseline(),records=rows)
    with pytest.raises(RuntimeContractError) as caught:
        runtime_context_wire(dataclasses.replace(context(tmp_path,baseline()),boot_content='x'*MAX_FRAME_BYTES))
    assert caught.value.code=='FRAME_TOO_LARGE'


def test_public_boundaries_reject_naked_renamed_asdict_and_drop_named_context_field(tmp_path):
    value=baseline()
    public=public_payload(dataclasses.asdict(context(tmp_path,value))|{'state_root':None,'worktree':None,
        'executable':{},'workspace':None,'history':None})
    assert 'history_baseline' not in public
    for payload in (value,dataclasses.asdict(value),{'renamed':dataclasses.asdict(value)},[dataclasses.asdict(value)]):
        with pytest.raises(RuntimeContractError) as caught:
            public_payload(payload)
        assert caught.value.code=='HISTORY_BASELINE_PRIVATE'
        assert 'private-native-session' not in str(caught.value)
    with pytest.raises(RuntimeContractError):
        RuntimeEvent('runtime.ready',data={'renamed':dataclasses.asdict(value)})
    assert RuntimeEvent('runtime.ready',data={'history_baseline':dataclasses.asdict(value)}).data=={}


class Driver(RuntimeDriver):
    harness='codex'
    revision='fake'
    def __init__(self): self.calls=[]
    def start(self,context,emit,*,deadline):
        raise AssertionError('history cannot fall back to fresh start')
    def resume_history(self,context,emit,*,deadline):
        self.calls.append(context.history_baseline)
        return DriverStart('ready',RuntimeIdentity('old-root'))
    def submit(self,*args,**kwargs): raise AssertionError('no submit')
    def inventory(self,*,deadline): return NativeSnapshot(RuntimeIdentity('old-root'),None)
    def control(self,*args,**kwargs): raise AssertionError('no control')
    def cleanup(self,*,deadline): return NativeCleanup('complete')


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    driver=Driver();owner=Controller('g',tmp_path,driver)
    server=PrivateServer(owner,tmp_path/'socket');thread=threading.Thread(target=server.serve)
    thread.start()
    try:
        for _ in range(100):
            if server.endpoint.exists(): break
            time.sleep(.005)
        client=RuntimeClient(server.endpoint,'g',controller_pid=os.getpid(),controller_start_ticks=start_ticks(os.getpid()),consumer='api')
        client.lease={'consumer':'api','fence':1}
        client.request('attach',expires=time.time()+30)
        yield client,owner,driver
    finally:
        owner.shutdown.set();thread.join(2)


def test_real_private_open_duplicate_binding_and_public_status_no_baseline(private,tmp_path):
    client,_owner,driver=private
    bound=context(tmp_path,baseline())
    assert client.open(bound)['state']=='ready'
    assert client.open(bound)['ready'] is True
    assert driver.calls==[baseline()]
    serialized=json.dumps(client.request('status'))+json.dumps(client.request('subscribe',after=0))
    assert 'private-native-session' not in serialized and 'old-turn' not in serialized and 'history_baseline' not in serialized
    with pytest.raises(RuntimeContractError) as caught:
        client.open(dataclasses.replace(bound,history_baseline=dataclasses.replace(baseline(),capture_sequence=4)))
    assert caught.value.code=='GENERATION_CONFLICT' and len(driver.calls)==1
    with pytest.raises(RuntimeContractError): client.open(dataclasses.replace(bound,history_baseline=None))


def test_real_private_parse_error_is_static_and_close_unconditional(private,tmp_path):
    client,_owner,driver=private
    value=runtime_context_wire(context(tmp_path,baseline()))
    value['history_baseline']['private_payload']='SECRET_PRIVATE_SENTINEL'
    with pytest.raises(RuntimeContractError) as caught:
        client.request('open',context=value)
    assert caught.value.code=='HISTORY_BASELINE_INVALID' and 'SECRET_PRIVATE_SENTINEL' not in str(caught.value)
    assert driver.calls==[]
    from conversation_runtime_contract import payload_digest
    cmd={'control_id':'close','request_sequence':1,'action':'close'}
    cmd['payload_digest']=payload_digest(cmd)
    assert client.request('close',command=cmd)['outcome']=='complete'


def test_controller_status_cannot_echo_baseline_from_driver_protocol(private,tmp_path):
    client,owner,_driver=private
    owner.identity=RuntimeIdentity('old-root',protocol={'history_baseline':dataclasses.asdict(baseline())})
    assert client.request('status')['identity']['protocol']=={}
    owner.identity=RuntimeIdentity('old-root',protocol={'renamed':dataclasses.asdict(baseline())})
    with pytest.raises(RuntimeContractError) as caught: client.request('status')
    assert caught.value.code=='HISTORY_BASELINE_PRIVATE'
    from conversation_runtime_contract import payload_digest
    command={'control_id':'still-close','request_sequence':1,'action':'close'}
    command['payload_digest']=payload_digest(command)
    assert client.request('close',command=command)['outcome']=='complete'


def test_artifact_roundtrip_hash_inode_mode_atomic_expected_replacement(tmp_path):
    root=file_root(tmp_path)
    reference=files.write_baseline(root,baseline(),deadline=deadline())
    assert reference.sha256==hashlib.sha256(baseline().private_bytes()).hexdigest()
    assert (tmp_path/files.FILENAME).stat().st_mode&0o777==0o600
    assert files.read_baseline(root,reference,deadline=deadline())==baseline()
    with pytest.raises(RuntimeContractError): files.write_baseline(root,baseline(),deadline=deadline())
    newer=dataclasses.replace(baseline(),capture_sequence=4)
    updated=files.write_baseline(root,newer,expected=reference,deadline=deadline())
    assert updated.inode!=reference.inode
    with pytest.raises(RuntimeContractError): files.read_baseline(root,reference,deadline=deadline())
    assert files.read_baseline(root,updated,deadline=deadline())==newer
    assert list(tmp_path.iterdir())==[tmp_path/files.FILENAME]


@pytest.mark.parametrize('mutation',['content','truncate','replace','symlink','mode','fifo','oversized','duplicate_json','wrong_generation'])
def test_artifact_refuses_changed_or_incomplete_file(tmp_path,mutation):
    root=file_root(tmp_path);ref=files.write_baseline(root,baseline(),deadline=deadline())
    path=tmp_path/files.FILENAME
    if mutation=='content': path.write_bytes(path.read_bytes().replace(b'old-turn',b'new-turn'))
    elif mutation=='truncate': path.write_bytes(b'{')
    elif mutation=='replace':
        raw=path.read_bytes();path.unlink();path.write_bytes(raw);path.chmod(0o600)
    elif mutation=='symlink': path.unlink();path.symlink_to('/definitely-not-read')
    elif mutation=='mode': path.chmod(0o644)
    elif mutation=='fifo': path.unlink();os.mkfifo(path,0o600)
    elif mutation=='oversized': path.write_bytes(b'x'*(MAX_HISTORY_BASELINE_BYTES+1))
    elif mutation=='duplicate_json': path.write_bytes(b'{"complete":true,"complete":true}')
    elif mutation=='wrong_generation': root=dataclasses.replace(root,generation_id='foreign-g')
    with pytest.raises(RuntimeContractError): files.read_baseline(root,ref,deadline=deadline())


def test_artifact_rejects_replaced_root_parent_symlink_deadline_and_lock(tmp_path):
    actual=tmp_path/'owned';actual.mkdir(mode=0o700);root=file_root(actual)
    ref=files.write_baseline(root,baseline(),deadline=deadline())
    alias=tmp_path/'alias';alias.symlink_to(actual,target_is_directory=True)
    with pytest.raises(RuntimeContractError): files.read_baseline(dataclasses.replace(root,path=alias),ref,deadline=deadline())
    with pytest.raises(RuntimeContractError): files.read_baseline(root,ref,deadline=time.monotonic()-1)
    fd=os.open(actual,os.O_RDONLY|os.O_DIRECTORY)
    import fcntl
    fcntl.flock(fd,fcntl.LOCK_EX)
    start=time.monotonic()
    try:
        with pytest.raises(RuntimeContractError): files.read_baseline(root,ref,deadline=start+.02)
        assert time.monotonic()-start<.15
    finally: os.close(fd)
    actual.rename(tmp_path/'old');actual.mkdir(mode=0o700)
    with pytest.raises(RuntimeContractError): files.read_baseline(root,ref,deadline=deadline())


def test_read_detects_file_change_during_bounded_read(tmp_path,monkeypatch):
    root=file_root(tmp_path);ref=files.write_baseline(root,baseline(),deadline=deadline())
    original=os.read;changed=False
    def mutate(fd,size):
        nonlocal changed
        chunk=original(fd,size)
        if chunk and not changed:
            changed=True
            with (tmp_path/files.FILENAME).open('ab') as output: output.write(b' ')
        return chunk
    monkeypatch.setattr(files.os,'read',mutate)
    with pytest.raises(RuntimeContractError): files.read_baseline(root,ref,deadline=deadline())


def test_claude_boundary_private_only_and_exact_types(tmp_path):
    boundary=ClaudeFileBoundary('/private/project','/private/project/uuid.jsonl',1,2,1,3,500,'a'*64,4)
    value=dataclasses.replace(baseline(),harness='claude',claude_file=boundary)
    assert HistoryBaseline.from_private_wire(json.loads(value.private_bytes()))==value
    for changes in ({'file_inode':True},{'transcript':'/outside/session'},{'namespace':'relative'},{'eof_bytes':-1}):
        with pytest.raises(RuntimeContractError): dataclasses.replace(boundary,**changes)


def test_actual_http_snapshot_excludes_private_baseline(seat):
    # Actual migrated DB/HTTP projection; no server/native process is launched.
    import conversation_routes as routes
    from test_native_history_continuations import CID
    raw=json.loads(baseline().private_bytes())
    row=seat.con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(CID,)).fetchone()
    projection=json.loads(row[0])|{'history_baseline':raw}
    seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(projection),CID))
    seat.con.commit()
    response=routes.handle('GET',f'/api/conversations/{CID}','Host: localhost:8800\r\n',b'')
    assert response[0]==200
    encoded=response[2].decode()
    assert 'history_baseline' not in encoded and 'private-native-session' not in encoded
    # Renaming the full private object must refuse, not silently publish it.
    projection['renamed']=raw
    seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(projection),CID))
    seat.con.commit()
    refused=routes.handle('GET',f'/api/conversations/{CID}','Host: localhost:8800\r\n',b'')
    assert refused[0]!=200 and b'private-native-session' not in refused[2]


def test_duplicate_json_parser_and_unknown_fields_refuse_with_matching_file_hash(tmp_path):
    root=file_root(tmp_path)
    for raw in (b'{"complete":true,"complete":true}',
                json.dumps(json.loads(baseline().private_bytes())|{'raw_private':'never-copy'}).encode()):
        path=tmp_path/files.FILENAME
        path.write_bytes(raw);path.chmod(0o600)
        info=path.stat()
        reference=files.BaselineReference('old-g',hashlib.sha256(raw).hexdigest(),info.st_dev,info.st_ino,len(raw))
        with pytest.raises(RuntimeContractError): files.read_baseline(root,reference,deadline=deadline())
        path.unlink()


def test_write_expiry_or_existing_mismatch_keeps_owned_original(tmp_path,monkeypatch):
    root=file_root(tmp_path);reference=files.write_baseline(root,baseline(),deadline=deadline())
    stale=dataclasses.replace(reference,sha256='f'*64)
    with pytest.raises(RuntimeContractError): files.write_baseline(root,baseline(),expected=stale,deadline=deadline())
    original=os.fsync
    expired=time.monotonic()+.03
    def expire(fd):
        original(fd)
        time.sleep(.04)
    monkeypatch.setattr(files.os,'fsync',expire)
    with pytest.raises(RuntimeContractError): files.write_baseline(root,baseline(),expected=reference,deadline=expired)
    assert (tmp_path/files.FILENAME).read_bytes()==baseline().private_bytes()
    assert list(tmp_path.iterdir())==[tmp_path/files.FILENAME]


def test_wellformed_private_baseline_cannot_grant_history(private,tmp_path):
    client,_owner,driver=private
    bound=dataclasses.replace(context(tmp_path,baseline()),capability_evidence={})
    with pytest.raises(RuntimeContractError) as caught: client.open(bound)
    assert caught.value.code=='NATIVE_HISTORY_UNAVAILABLE' and driver.calls==[]
    assert client.request('status')['ready'] is False


def test_context_baseline_does_not_enter_reusable_fingerprint_or_cache(tmp_path):
    from conversation_runtime_checks import EvidenceCache, Fingerprint
    bare=context(tmp_path)
    private=dataclasses.replace(bare,history_baseline=baseline())
    fp=Fingerprint.capture(private,settings_digest='a'*64,implementation_digest='b'*64)
    assert fp==Fingerprint.capture(bare,settings_digest='a'*64,implementation_digest='b'*64)
    assert EvidenceCache().history_evidence(fp,object()) is None
    assert 'private-native-session' not in json.dumps(EvidenceCache().export())


def test_store_reservation_whitelist_never_persists_baseline(tmp_path):
    import sqlite3

    from conversation_runtime import RuntimeStore
    scripts=Path(__file__).resolve().parents[1]/'.super-coder'
    path=tmp_path/'db'
    con=sqlite3.connect(path)
    con.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY); CREATE TABLE shells(shell_id INTEGER PRIMARY KEY); CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,state TEXT,harness TEXT DEFAULT "codex",provider TEXT,model TEXT,effort TEXT,worktree TEXT,runtime_projection TEXT DEFAULT "{}"); INSERT INTO users VALUES(1); INSERT INTO shells VALUES(1); INSERT INTO conversations(conversation_id,shell_id,owner_user_id,state) VALUES("cv",1,1,"idle");')
    con.executescript((scripts/'migrations/0273_conversation_native_runtime.sql').read_text())
    con.executescript((scripts/'migrations/0278_native_history_continuations.sql').read_text())
    con.execute('UPDATE conversations SET worktree=?',(str(tmp_path),));con.commit();con.close()
    bare=dataclasses.replace(context(tmp_path),history=None,workspace=None,capability_evidence={},model=None,effort=None)
    store=RuntimeStore(path)
    store.reserve(bare,{'unit':'own.service','history_baseline':json.loads(baseline().private_bytes())})
    assert 'history_baseline' not in json.dumps(store.status('g',1,1))
    assert 'private-native-session' not in json.dumps(store.status('g',1,1))



def test_write_detects_changed_owned_temporary_bytes(tmp_path,monkeypatch):
    root=file_root(tmp_path)
    original=os.fsync
    def mutate(fd):
        if not os.fstat(fd).st_mode&0o040000:
            # Same-length, well-formed data change cannot become the requested candidate.
            raw=baseline().private_bytes().replace(b'"capture_sequence":3',b'"capture_sequence":4')
            os.pwrite(fd,raw,0)
        original(fd)
    monkeypatch.setattr(files.os,'fsync',mutate)
    with pytest.raises(RuntimeContractError): files.write_baseline(root,baseline(),deadline=deadline())
    assert not any(path.name.startswith('.history-baseline-') for path in tmp_path.iterdir())
