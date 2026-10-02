"""Finite synthetic owner allocation, immutable binding and cleanup boundaries."""
import dataclasses
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
from conversation_native_chats import NativeChatsService
from conversation_runtime_checks import Fingerprint
from conversation_runtime_contract import (
    ExecutableBinding,
    RuntimeContext,
    RuntimeContractError,
)
from gui_experiment_probe_owner import NativeProbeOwner
from test_native_chat_ownership import database  # noqa: F401


@pytest.fixture
def owner(request,monkeypatch):
    path,con=request.getfixturevalue('database')
    executable=ExecutableBinding(Path('/bin/true'),'a'*64,'test')
    fingerprint=Fingerprint('codex',executable,'test','b'*64,'openai','selected','high','b'*64,'c'*64)
    events=[]
    def query(sql,values):
        connection=sqlite3.connect(path)
        connection.row_factory=sqlite3.Row
        try:return connection.execute(sql,values).fetchone()
        finally:connection.close()
    class Supervisor:
        def __init__(self):self.units=[]
        def preparation_identity(self): return {'pid':123,'start_ticks':456,'unit':'fixed-api','control_group':'/fixed-api'}
        def launch(self,generation):
            assert query('SELECT generation_id FROM conversation_runtime_generations WHERE generation_id=?',(generation,))
            events.append('launch')
        def inventory(self): return self.units
        def stop(self,generation):
            events.append('stop')
            return {'os_cleanup':{'complete':True}}
    supervisor=Supervisor()
    def prepare(cid,generation,*,probe_capabilities):
        chat=query('SELECT * FROM conversations WHERE conversation_id=?',(cid,))
        events.append('prepare')
        assert json.loads(chat['runtime_projection'])['role']=='probe'
        native={'generation_id':generation,'root':str(path.parent/'runtime'/generation),'unit':'fixed-native','status':'registered','control_group':'/fixed-native'}
        supervisor.units=[native]
        context=RuntimeContext(generation,cid,chat['shell_id'],1,'codex',Path(native['root']),Path(chat['worktree']),
            executable,'test','d'*64,'b'*64,'unrestricted',provider='openai',model='selected',effort='high',
            probe_capabilities=probe_capabilities)
        return context,fingerprint,native
    seat=SimpleNamespace(database=path,root=path.parent,supervisor=supervisor,prepare=prepare)
    service=NativeChatsService(path,path.parent,supervisor)
    service.attach=lambda gen:(SimpleNamespace(consumer='test'),1,
        query('SELECT shell_id FROM conversation_runtime_generations WHERE generation_id=?',(gen,))[0])
    value=NativeProbeOwner(seat,service)
    def git(argv,**kwargs):
        assert argv[:3]==['git','-C',str(path.parent)] and argv[3]=='worktree'
        assert Path(argv[-1]).parent==path.parent/'.sc-worktrees'
        events.append('git')
    monkeypatch.setattr('gui_experiment_probe_owner.subprocess.run',git)
    yield value,fingerprint,con,events
    service.shutdown()


def test_owned_probe_is_distinct_and_only_finite_grants_allow_start(owner):
    value,fingerprint,con,events=owner
    probe=value.allocate(fingerprint,frozenset({'submission','stop_work'}),time.monotonic()+30)
    assert probe.context.conversation_id!='cv' and probe.shell_id!=1
    assert probe.context.probe_capabilities==('stop_work','stop_work_child','stop_work_terminal','submission')
    assert probe.context.capability_evidence=={}
    assert events==['git','prepare','launch']
    row=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(probe.context.conversation_id,)).fetchone()
    projection=json.loads(row['runtime_projection'])
    assert projection['role']=='probe' and projection['capabilities']=={}
    assert json.loads(con.execute('SELECT route_binding FROM conversations WHERE conversation_id=?',(probe.context.conversation_id,)).fetchone()[0])['selector_binding']['proof_state']=='pending_finite_probe'
    con.execute("UPDATE shells SET current_state='nonce' WHERE shell_id=1");con.commit()
    assert not probe.observe_marker('nonce',time.monotonic()+2)
    con.execute("UPDATE shells SET current_state='nonce' WHERE shell_id=?",(probe.shell_id,));con.commit()
    assert probe.observe_marker('nonce',time.monotonic()+2)
    assert not probe.observe_marker('nonce',time.monotonic()-1)


def test_http_check_intent_is_bound_before_probe_preparation(owner):
    value,fingerprint,con,_=owner
    selection={'harness':fingerprint.harness,'model':fingerprint.model,'effort':fingerprint.effort}
    con.execute("INSERT INTO conversation_runtime_check_requests VALUES('check',1,'key','hash',?,'accepted',?,1,1)",
        (json.dumps(selection),json.dumps({'fingerprint':fingerprint.key})));con.commit()
    prepare=value.seat.prepare
    def bound_prepare(cid,generation,**kwargs):
        runtime=json.loads(con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0])
        assert runtime['check_id']=='check'
        return prepare(cid,generation,**kwargs)
    value.seat.prepare=bound_prepare
    probe=value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    assert probe.context.conversation_id!='cv'


def test_native_probe_cleanup_requires_retained_native_result_and_owned_os_exit(owner):
    value,fingerprint,con,events=owner
    probe=value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    lease=value.service.store.attach(probe.context.generation_id,1,probe.shell_id,value.service.consumer)
    value.service.store.intent(probe.context.generation_id,1,probe.shell_id,lease,'probe-close','close',{'action':'close'})
    value.service.store.receipt(probe.context.generation_id,1,probe.shell_id,'probe-close',{'state':'written','outcome':'complete'})
    proof=value.cleanup(fingerprint,time.monotonic()+10)
    assert proof.complete and events[-1]=='stop'
    assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='complete'
    assert con.execute('SELECT state FROM conversations WHERE conversation_id=?',(probe.context.conversation_id,)).fetchone()[0]=='closed'


@pytest.mark.parametrize('change',[{'harness':'claude'},{'model':None}])
def test_unresolved_startup_or_route_prerequisite_cannot_allocate_or_infer(owner,change):
    value,fingerprint,con,events=owner
    with pytest.raises(RuntimeContractError):
        value.allocate(dataclasses.replace(fingerprint,**change),frozenset({'submission'}),time.monotonic()+30)
    assert not events and con.execute('SELECT COUNT(*) FROM conversation_runtime_probe_jobs').fetchone()[0]==0


def test_close_during_canonical_prepare_fences_generation_and_launch(owner):
    value,fingerprint,con,events=owner
    prepare=value.seat.prepare
    def closing_prepare(cid,generation,**kwargs):
        captured=prepare(cid,generation,**kwargs)
        version=con.execute('SELECT version FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0]
        value.service.request_close(con,cid,1,version)
        return captured
    value.seat.prepare=closing_prepare
    with pytest.raises(RuntimeContractError,match='Close/stale job'):
        value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    assert events==['git','prepare']
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1 # unrelated retained g only
    probe=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id!=\'cv\'').fetchone()
    assert json.loads(probe[0])['state']=='closing'
    assert value.cleanup(fingerprint,time.monotonic()+10).complete
    assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='complete'


def test_git_failure_before_native_register_releases_job_with_never_launched_proof(owner,monkeypatch):
    value,fingerprint,con,_=owner
    def failed_git(*args,**kwargs): raise OSError('synthetic Git failure')
    monkeypatch.setattr('gui_experiment_probe_owner.subprocess.run',failed_git)
    with pytest.raises(OSError):value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    assert value.cleanup(fingerprint,time.monotonic()+10).complete
    assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='complete'
    assert con.execute("SELECT state FROM conversations WHERE conversation_id!='cv'").fetchone()[0]=='closed'


def test_empty_finished_allocation_cleanup_allows_prerequisite_recovery(owner,monkeypatch):
    value,fingerprint,_,events=owner
    identity=value.seat.supervisor.preparation_identity
    def missing(): raise RuntimeContractError('OWNERSHIP_INVALID','temporary fixture API not ready')
    monkeypatch.setattr(value.seat.supervisor,'preparation_identity',missing)
    with pytest.raises(RuntimeContractError):value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    assert value.cleanup(fingerprint,time.monotonic()+10).complete
    monkeypatch.setattr(value.seat.supervisor,'preparation_identity',identity)
    probe=value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    assert probe.registered and events==['git','prepare','launch']


def test_empty_cleanup_cannot_release_inflight_allocation(owner,monkeypatch):
    import threading
    value,fingerprint,con,events=owner
    entered,release=threading.Event(),threading.Event()
    identity=value.seat.supervisor.preparation_identity
    def blocked_identity():
        entered.set();assert release.wait(3)
        return identity()
    monkeypatch.setattr(value.seat.supervisor,'preparation_identity',blocked_identity)
    errors=[]
    def allocate():
        try:value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
        except RuntimeContractError as exc:errors.append(exc.code)
    worker=threading.Thread(target=allocate);worker.start()
    try:
        assert entered.wait(2)
        assert not value.cleanup(fingerprint,time.monotonic()+10).complete
        release.set();worker.join(3)
        assert errors==['PROBE_ALLOCATION_FENCED'] and not events
        assert con.execute('SELECT COUNT(*) FROM conversation_runtime_probe_jobs').fetchone()[0]==0
        assert value.cleanup(fingerprint,time.monotonic()+10).complete
    finally:
        release.set();worker.join(3)


def test_existing_job_cleanup_retains_fence_until_late_allocation_finishes(owner,monkeypatch):
    import threading
    value,fingerprint,con,events=owner
    entered,release=threading.Event(),threading.Event()
    def blocked_git(*args,**kwargs):
        entered.set();assert release.wait(3)
    monkeypatch.setattr('gui_experiment_probe_owner.subprocess.run',blocked_git)
    errors=[]
    def allocate():
        try:value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
        except RuntimeContractError as exc:errors.append(exc.code)
    worker=threading.Thread(target=allocate);worker.start()
    try:
        assert entered.wait(2)
        assert not value.cleanup(fingerprint,time.monotonic()+10).complete
        assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='preparing'
        assert fingerprint.key in value.closing and fingerprint.key in value.allocating
        release.set();worker.join(3)
        assert errors==['PROBE_ALLOCATION_FENCED'] and events==['prepare']
        assert 'launch' not in events
        assert value.cleanup(fingerprint,time.monotonic()+10).complete
        assert con.execute('SELECT status FROM conversation_runtime_probe_jobs').fetchone()[0]=='complete'
        assert fingerprint.key not in value.closing
    finally:
        release.set();worker.join(3)


def test_probe_role_refuses_ordinary_input_even_with_cached_submission(owner,monkeypatch):
    import conversation_native_chats
    import conversation_routes
    value,fingerprint,con,_=owner
    probe=value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',
        (json.dumps({'role':'probe','state':'ready','capabilities':{'submission':'compatible'}}),probe.context.conversation_id));con.commit()
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',value.service)
    with pytest.raises(conversation_routes.ApiError) as error:
        conversation_routes._create_message(con,{'user_id':1},probe.context.conversation_id,
            {'Idempotency-Key':'attempted-probe-input'},{'text':'ordinary prompt'})
    assert error.value.code=='PROBE_INPUT_UNAVAILABLE'
    assert con.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0]==0


def test_stale_cleanup_cannot_complete_replacement_job_or_release_its_fence(owner,monkeypatch):
    value,fingerprint,con,_=owner
    def failed_git(*args,**kwargs):raise OSError('synthetic Git failure')
    monkeypatch.setattr('gui_experiment_probe_owner.subprocess.run',failed_git)
    with pytest.raises(OSError):value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
    old=con.execute('SELECT generation_id FROM conversation_runtime_probe_jobs').fetchone()[0]
    original=value.service.finish_preparation
    replaced=[]
    def finish(cid,generation):
        value.service.finish_preparation=original
        assert value.cleanup(fingerprint,time.monotonic()+10).complete
        monkeypatch.setattr('gui_experiment_probe_owner.subprocess.run',lambda *args,**kwargs:None)
        replacement=value.allocate(fingerprint,frozenset({'submission'}),time.monotonic()+30)
        replaced.append(replacement)
        value.closing.add(fingerprint.key) # current-generation cleanup fence
        original(cid,generation)
    value.service.finish_preparation=finish
    assert not value.cleanup(fingerprint,time.monotonic()+10).complete
    job=con.execute('SELECT generation_id,status FROM conversation_runtime_probe_jobs').fetchone()
    assert job[0]!=old and job[0]==replaced[0].context.generation_id and job[1]=='preparing'
    assert fingerprint.key in value.closing


def test_real_checker_factory_entry_accepts_owned_common_fixture_boundary(owner,monkeypatch):
    from conversation_runtime_checks import CompatibilityChecker
    from conversation_runtime_contract import DriverStart, NativeCleanup
    from conversation_runtime_native_probes import (
        ControllerProbeDriver,
        NativeProbeFactory,
    )
    value,fingerprint,_,_=owner
    reached=[]
    def unavailable(driver,context,emit,*,deadline):
        reached.append(context.generation_id)
        assert value.root.resolve() in context.worktree.resolve().parents
        assert value.root.resolve() in context.state_root.resolve().parents
        return DriverStart('unavailable',detail='test transport account absent')
    monkeypatch.setattr(ControllerProbeDriver,'start',unavailable)
    monkeypatch.setattr(ControllerProbeDriver,'cleanup',lambda *args,**kwargs:NativeCleanup('complete'))
    result=CompatibilityChecker().request(fingerprint,observed_interface={},requirements={'submission':{}},
        factory=NativeProbeFactory(value.allocate,value.cleanup),seconds=30).result(timeout=5)
    assert len(reached)==1
    assert all(d.code!='PROBE_OWNERSHIP_INVALID' for evidence in result.evidence.values() for d in evidence.diagnostics)
