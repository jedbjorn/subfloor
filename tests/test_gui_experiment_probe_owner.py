"""Finite synthetic owner allocation, immutable binding and cleanup boundaries."""
import dataclasses
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
from test_native_chat_ownership import database  # noqa: F401

from conversation_native_chats import NativeChatsService
from conversation_runtime_checks import Fingerprint
from conversation_runtime_contract import ExecutableBinding, RuntimeContext, RuntimeContractError
from gui_experiment_probe_owner import NativeProbeOwner


@pytest.fixture
def owner(database,monkeypatch):
    path,con=database
    executable=ExecutableBinding(Path('/bin/true'),'a'*64,'test')
    fingerprint=Fingerprint('codex',executable,'test','b'*64,'openai','selected','high','b'*64,'c'*64)
    events=[]
    class Supervisor:
        units=[]
        def preparation_identity(self): return {'pid':123,'start_ticks':456,'unit':'fixed-api','control_group':'/fixed-api'}
        def launch(self,generation):
            assert con.execute('SELECT generation_id FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone()
            events.append('launch')
        def inventory(self): return self.units
        def stop(self,generation):
            events.append('stop')
            return {'os_cleanup':{'complete':True}}
    supervisor=Supervisor()
    def prepare(cid,generation,*,probe_capabilities):
        chat=con.execute('SELECT * FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
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
        con.execute('SELECT shell_id FROM conversation_runtime_generations WHERE generation_id=?',(gen,)).fetchone()[0])
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
