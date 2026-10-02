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
def seat(tmp_path,monkeypatch):
    engine=tmp_path/'.super-coder';engine.mkdir()
    database=engine/'shell_db.db'
    con=sqlite3.connect(database)
    con.executescript((ROOT/'.super-coder/schema.sql').read_text())
    for migration in sorted((ROOT/'.super-coder/migrations').glob('*.sql')):con.executescript(migration.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id) VALUES(1,'FX','Fixture','dev','synthetic',1)")
    binding={'contract_version':2,'control_state':'controlled','harness':'codex','requested_model':'gpt-6.1-sol','provider_model':'gpt-6.1-sol','requested_effort':'high','effective_effort':'high','native_variant_id':None,'transport':'codex-reasoning-config','catalogue_generation':'a'*32,'evidence_digest':'b'*64,'selector_binding':{'experimental_native':True},'adapter_metadata':{}}
    worktree=tmp_path/'.sc-worktrees/fx';worktree.mkdir(parents=True)
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,runtime_mode,route_contract_version,route_binding) VALUES('cv',1,1,'codex','openai','gpt-6.1-sol','high',?,'key','hash','native_experiment',2,?)",(str(worktree),json.dumps(binding)));con.commit();con.close()
    shutil.copytree(ROOT/'.super-coder/scripts',engine/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
    (engine/'api').mkdir()
    shutil.copyfile(ROOT/'.super-coder/api/route_bindings.py',engine/'api/route_bindings.py')
    shutil.copytree(ROOT/'.super-coder/adapters/codex',engine/'adapters/codex')
    shutil.copyfile(ROOT/'maintainer/gui_experiment.py',tmp_path/'fixture_bootstrap.py')
    binary=tmp_path/'native-binary';binary.write_bytes(b'executable fixture')
    monkeypatch.setattr(run,'ENGINE',engine);monkeypatch.setattr(run,'DB_PATH',str(database))
    events=[]
    class Supervisor:
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


@pytest.mark.parametrize('source',['conversation_adapters/codex_runtime.py','gui_experiment_readiness.py','conversation_boot.py','run.py'])
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
