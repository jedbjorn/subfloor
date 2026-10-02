"""Copied canonical preparation keeps native login separate from GUI inventory."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import gui_experiment_native_seat as seat_module
import run
from conversation_runtime_checks import EvidenceCache
from conversation_runtime_contract import ExecutableBinding, RuntimeContractError


@pytest.fixture
def seat(tmp_path,monkeypatch,request):
    engine=tmp_path/'.super-coder';engine.mkdir()
    database=engine/'shell_db.db'
    con=sqlite3.connect(database)
    con.executescript((ROOT/'.super-coder/schema.sql').read_text())
    for migration in sorted((ROOT/'.super-coder/migrations').glob('*.sql')):con.executescript(migration.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(1,'FX','Fixture','dev','synthetic',1)")
    binding={'contract_version':2,'control_state':'controlled','harness':'codex','requested_model':'gpt-6.1-sol','provider_model':'gpt-6.1-sol','requested_effort':'high','effective_effort':'high','native_variant_id':None,'transport':'codex-reasoning-config','catalogue_generation':'a'*32,'evidence_digest':'b'*64,'selector_binding':{'experimental_native':True},'adapter_metadata':{}}
    if getattr(request,'param',None)=='pending':
        binding['selector_binding']['proof_state']='pending_finite_probe'
    worktree=tmp_path/'.sc-worktrees/fx';worktree.mkdir(parents=True)
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,route_contract_version,route_binding,runtime_projection) VALUES('cv',1,1,'codex','openai','gpt-6.1-sol','high',?,'key','hash','native_experiment',2,?,?)",(str(worktree),json.dumps(binding),json.dumps({'role':'ordinary','generation_id':'generation','state':'preparing'})));con.commit();con.close()
    shutil.copytree(ROOT/'.super-coder/scripts',engine/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
    (engine/'api').mkdir()
    shutil.copyfile(ROOT/'.super-coder/api/route_bindings.py',engine/'api/route_bindings.py')
    shutil.copytree(ROOT/'.super-coder/adapters/codex',engine/'adapters/codex')
    shutil.copyfile(ROOT/'maintainer/gui_experiment.py',tmp_path/'fixture_bootstrap.py')
    binary=tmp_path/'native-binary';binary.write_bytes(b'executable fixture')
    monkeypatch.setattr(run,'ENGINE',engine);monkeypatch.setattr(run,'DB_PATH',str(database))
    events=[]
    class Supervisor:
        def codegen_clean(self):return True
        def begin_codegen(self,*args,**kwargs):pass
        def finish_codegen(self,*args,**kwargs):return True
        def register(self,generation,harness):
            events.append('register')
            state=tmp_path/'runtime'/generation;state.mkdir(parents=True)
            return {'root':str(state),'endpoint':str(state/'private.sock')}
    plan=SimpleNamespace(argv=[],model='gpt-6.1-sol',effort='high',boot_content='canonical no-memory boot',cwd=str(worktree),env={'SC_API_BASE':'http://127.0.0.1:43210','SC_API_TOKEN':'synthetic-token','HOME':'masked','PATH':'masked'},execution_view=SimpleNamespace(prefix=('canonical-view',)))
    def prepare(**kwargs):
        events.append('canonical-start')
        assert kwargs['route_binding']==binding
        return plan
    monkeypatch.setattr(run,'prepare_launch',prepare)
    monkeypatch.setattr(seat_module,'prepared_plan',lambda **kwargs: plan)
    monkeypatch.setattr(run,'load_adapter',lambda harness:{'launch_flags':['--sandbox','danger-full-access','--ask-for-approval','never']})
    monkeypatch.setattr(run,'managed_mcp_injection',lambda adapter,short:{'name':'browser','url':'http://127.0.0.1:43210/mcp/FX','launch_args':['-c','mcp_servers.browser.url="http://127.0.0.1:43210/mcp/FX"']})
    value=seat_module.NativeFixtureSeat(database=database,root=tmp_path,supervisor=Supervisor(),native_bindings={'HOME':'/explicit-native-home','CODEX':str(binary)},cache=EvidenceCache())
    value.observers['codex']=SimpleNamespace(observe=lambda: SimpleNamespace(binding=ExecutableBinding(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'observed-version')))
    return value,events,plan


def test_fixed_context_registers_before_canonical_boot_and_never_unmasks_server_environment(seat):
    value,events,plan=seat
    original=dict(os.environ)
    context,fingerprint,_=value.prepare('cv','generation',probe_capabilities=('submission',))
    assert events==['register','canonical-start']
    assert context.env['HOME']=='/explicit-native-home'
    assert context.env['SC_API_TOKEN']=='synthetic-token'
    assert context.env['CODEX_HOME']=='/explicit-native-home/.codex'
    assert plan.env['HOME']=='masked' and plan.env['PATH']=='masked'
    assert dict(os.environ)==original
    assert context.execution_prefix==('canonical-view',)
    assert context.capability_evidence['submission']=='unverified'
    assert context.probe_capabilities==('submission',)
    assert fingerprint.implementation_digest==value.implementation_digest('codex')


@pytest.mark.parametrize('source',['conversation_adapters/codex_runtime.py','gui_experiment_readiness.py','conversation_boot.py','run.py','execution_view.py','execution_view_exec.py'])
def test_source_change_invalidates_fingerprint_without_dynamic_owner_ids(seat,source):
    value,_,_=seat
    initial=value.implementation_digest('codex')
    settings=value.settings_digest('codex')
    path=value.root/'.super-coder/scripts'/source
    path.write_text(path.read_text()+'\n# different source identity\n')
    assert value.implementation_digest('codex')!=initial
    assert value.settings_digest('codex')==settings


def test_changed_canonical_policy_stays_inconclusive_without_native_launch(seat,monkeypatch):
    value,_,_=seat
    monkeypatch.setattr(run,'load_adapter',lambda harness:{'launch_flags':['--ask-for-approval','on-request']})
    with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation')
    assert raised.value.code=='PERMISSION_INCONCLUSIVE'


@pytest.mark.parametrize('seat',['pending'],indirect=True)
def test_pending_route_cannot_prepare_an_ordinary_chat_or_unregistered_probe(seat):
    value,events,_=seat
    for grants in [(),('submission',)]:
        with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation',probe_capabilities=grants)
        assert raised.value.code=='PROBE_ROUTE_ONLY'
    assert events==[]


@pytest.mark.parametrize('seat',['pending'],indirect=True)
@pytest.mark.parametrize('wrong',['expired','foreign_generation','foreign_fingerprint','replaced_projection','closing'])
def test_pending_candidate_job_must_match_unexpired_open_generation(seat,wrong):
    import time
    value,events,_=seat
    con=sqlite3.connect(value.database)
    projection={'role':'probe','state':'closing' if wrong=='closing' else 'preparing','generation_id':'replacement' if wrong=='replaced_projection' else 'generation'}
    con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(projection),))
    con.execute('INSERT INTO conversation_runtime_probe_jobs VALUES(?,?,?,\'preparing\',?,?)',
                ('c'*64 if wrong=='foreign_fingerprint' else 'b'*64,'cv','other' if wrong=='foreign_generation' else 'generation',time.time()+(-1 if wrong=='expired' else 30),time.time()))
    con.commit();con.close()
    with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation',probe_capabilities=('submission',))
    assert raised.value.code=='PROBE_ROUTE_ONLY' and events==[]


@pytest.mark.parametrize('alias',['leaf','parent'])
def test_external_worktree_alias_is_rejected_before_registration_or_boot_writes(seat,tmp_path,alias):
    value,events,_=seat
    external=tmp_path.parent/(tmp_path.name+'-external');external.mkdir()
    selected=value.root/'.sc-worktrees/fx'
    if alias=='leaf':
        selected.rmdir();selected.symlink_to(external,target_is_directory=True)
    else:
        selected.rmdir();selected.parent.rmdir();selected.parent.symlink_to(external,target_is_directory=True)
        (external/'fx').mkdir()
    with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation')
    assert raised.value.code=='WORKTREE_INVALID'
    assert events==[]
    assert list((external/'fx' if alias=='parent' else external).iterdir())==[]


@pytest.mark.parametrize('change',['closing','generation','owner','preparer','role'])
def test_identity_observation_cannot_hide_changed_preparation_owner(seat,change):
    value,events,_=seat
    observe=value.observers['codex'].observe
    def interrupted_observe():
        con=sqlite3.connect(value.database)
        runtime={'role':'ordinary','generation_id':'replacement' if change=='generation' else 'generation',
                 'state':'closing' if change=='closing' else 'preparing'}
        if change=='preparer':runtime['preparation_owner']={'unit':'replacement'}
        if change=='role':runtime['role']='probe'
        con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(runtime),))
        if change=='owner':con.execute('UPDATE shells SET user_id=NULL')
        con.commit();con.close()
        return observe()
    value.observers['codex'].observe=interrupted_observe
    with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation')
    assert raised.value.code=='RUNTIME_CLOSING' and events==[]


def test_worktree_alias_created_during_observation_has_no_canonical_writes(seat,tmp_path):
    value,events,_=seat
    observe=value.observers['codex'].observe
    external=tmp_path/'foreign';external.mkdir()
    def interrupted_observe():
        worktree=value.root/'.sc-worktrees/fx'
        worktree.rmdir();worktree.symlink_to(external,target_is_directory=True)
        return observe()
    value.observers['codex'].observe=interrupted_observe
    with pytest.raises(RuntimeContractError) as raised:value.prepare('cv','generation')
    assert raised.value.code=='WORKTREE_INVALID' and events==[] and not list(external.iterdir())


def test_schema_generation_records_inert_child_before_use_and_reaps_before_return(seat,monkeypatch):
    import time

    import conversation_runtime_codex_codegen as codegen
    import conversation_runtime_codex_schema as schema
    from conversation_runtime_contract import ProcessIdentity
    value,events,_=seat
    fingerprint=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    owner={'pid':1,'start_ticks':2,'unit':'marked-api.service','control_group':'/marked-api'}
    value.supervisor.preparation_identity=lambda **kwargs:owner
    def record(process,cgroup,*,deadline):
        assert process==ProcessIdentity(123,456) and cgroup==owner['control_group']
        assert deadline>time.monotonic();events.append('record-child');return True
    value.supervisor.record_codegen_child=record
    class Runner:
        def __init__(self):
            self.receipt={'child_registered':True,'gate_released':True,'child_reaped':True,'process_group_exited':True,'files_removed':True}
        def __call__(self,argv,deadline):
            assert argv==(str(fingerprint.executable.path),'app-server','generate-json-schema','--experimental','--out',str(self.root/'schema'))
            assert self.guard()==owner
            assert self.register(ProcessIdentity(123,456),owner['control_group'])
            events.append('codegen');return True
        def cleanup(self,deadline):events.append('cleanup');return True
    def make(binding,root,guard,*,record_child):
        assert binding==fingerprint.executable and root.stat().st_mode&0o777==0o700
        result=Runner();result.root,result.guard,result.register=root,guard,record_child;return result
    def observe(binding,path,*,effort,run_owned,deadline):
        assert effort=='high' and deadline<time.monotonic()+20
        assert run_owned((str(binding.path),'app-server','generate-json-schema','--experimental','--out',str(path)),deadline)
        return schema.NativeSchemaObservation(binding,generation_completed=True)
    monkeypatch.setattr(codegen,'make_owned_codegen_runner',make)
    monkeypatch.setattr(schema,'observe_codex_schema',observe)
    result,receipt=value.observe_native_schema(fingerprint,time.monotonic()+177)
    assert result.generation_completed and receipt['cleanup_complete'] is True
    assert events==['record-child','codegen','cleanup']


def test_schema_expiry_or_changed_source_refuses_before_owned_root_mutation(seat,monkeypatch):
    import dataclasses
    import time
    value,events,_=seat
    fingerprint=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    value.supervisor.preparation_identity=lambda **kwargs:{}
    with pytest.raises(RuntimeContractError):value.observe_native_schema(fingerprint,time.monotonic()-1)
    monkeypatch.setattr(value,'candidate_fingerprint',lambda *args:dataclasses.replace(fingerprint,implementation_digest='d'*64))
    with pytest.raises(RuntimeContractError):value.observe_native_schema(fingerprint,time.monotonic()+20)
    assert not (value.root/'runtime').exists() and events==[]


def test_schema_cleanup_failure_retains_owned_root_and_refuses_reuse(seat,monkeypatch):
    import time

    import conversation_runtime_codex_codegen as codegen
    import conversation_runtime_codex_schema as schema
    value,_,_=seat
    fingerprint=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    value.supervisor.preparation_identity=lambda **kwargs:{}
    value.supervisor.record_codegen_child=lambda *args,**kwargs:True
    runner=SimpleNamespace(cleanup=lambda deadline:False,receipt={})
    monkeypatch.setattr(codegen,'make_owned_codegen_runner',lambda *args,**kwargs:runner)
    monkeypatch.setattr(schema,'observe_codex_schema',lambda *args,**kwargs:schema.NativeSchemaObservation(fingerprint.executable,generation_completed=True))
    with pytest.raises(RuntimeContractError):value.observe_native_schema(fingerprint,time.monotonic()+20)
    assert len(list((value.root/'runtime').iterdir()))==1


def test_actual_codegen_runner_content_participates_in_captured_fingerprint(seat):
    value,_,_=seat
    initial=value.implementation_digest('codex')
    path=value.root/'.super-coder/scripts/conversation_runtime_codex_codegen.py'
    path.write_text(path.read_text()+'\n# different consumed runner content\n')
    assert value.implementation_digest('codex')!=initial


def test_pending_codegen_refuses_fresh_root_and_ordinary_prepare_before_mutation(seat,monkeypatch):
    import time
    value,events,_=seat
    fingerprint=value.candidate_fingerprint('codex','gpt-6.1-sol','high')
    monkeypatch.setattr(value.supervisor,'codegen_clean',lambda:False)
    with pytest.raises(RuntimeContractError,match='previous codegen'):value.observe_native_schema(fingerprint,time.monotonic()+20)
    with pytest.raises(RuntimeContractError,match='previous codegen'):value.prepare('cv','generation')
    assert events==[] and not (value.root/'runtime').exists()
