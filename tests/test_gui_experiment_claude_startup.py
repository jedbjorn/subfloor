"""Configured source/dependency prerequisites; no native login or inference."""
import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import gui_experiment_native_seat as runtime
from conversation_runtime_contract import ExecutableBinding, RuntimeContractError
from test_claude_runtime_driver import MEMORY_SOURCE


@pytest.fixture
def seat(request):
    from test_gui_experiment_native_seat import seat as seat_fixture
    return seat_fixture.__wrapped__(request.getfixturevalue('tmp_path'),request.getfixturevalue('monkeypatch'),request)


def configured(seat,monkeypatch,source=MEMORY_SOURCE):
    value,events,_=seat
    binary=value.root/'native-binary';binary.write_text(source)
    fp=dataclasses.replace(value.candidate_fingerprint('codex','selected','high'),
        harness='claude',provider='anthropic',executable=ExecutableBinding(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'synthetic'))
    value.supervisor.preparation_identity=lambda:{'pid':123,'start_ticks':456,'unit':'fixed-api','control_group':'/fixed-api'}
    value.candidate_fingerprint=lambda *args:fp
    monkeypatch.setattr(runtime.run,'load_adapter',lambda name:{'launch_flags':['--dangerously-skip-permissions']})
    return value,fp,events


def test_consumed_source_is_a_pending_prerequisite_not_observed_memory(seat,monkeypatch):
    value,fp,events=configured(seat,monkeypatch)
    calls=[]
    value.prepare_claude_assets=lambda end:calls.append(end)
    result=value.claude_startup_prerequisite(fp,time.monotonic()+10)
    assert result['state']=='configured_pending_owned_hook'
    assert result['evidence_level']=='configuration_source_flag_inference' and result['effective_telemetry'] is False
    assert len(result['source_condition_sha256'])==64
    assert not {'auto_memory_disabled','memory_policy','capabilities','grade'}&result.keys()
    assert calls and events==[]


@pytest.mark.parametrize('failure',['unknown_source','hash_changed','identity_changed','deadline','permission','missing_dependencies'])
def test_preopen_prerequisite_failure_has_no_registration_or_boot(seat,monkeypatch,failure):
    value,fp,events=configured(seat,monkeypatch,'unknown disable source' if failure=='unknown_source' else MEMORY_SOURCE)
    value.prepare_claude_assets=lambda end:None
    if failure=='hash_changed':fp=dataclasses.replace(fp,executable=dataclasses.replace(fp.executable,sha256='f'*64))
    if failure=='identity_changed':fp=dataclasses.replace(fp,implementation_digest='f'*64)
    if failure=='permission':monkeypatch.setattr(runtime.run,'load_adapter',lambda name:{'launch_flags':['--permission-mode','default']})
    if failure=='missing_dependencies':
        def missing(end):raise RuntimeContractError('CHANNEL_UNAVAILABLE','synthetic missing pinned dependencies')
        value.prepare_claude_assets=missing
    with pytest.raises(RuntimeContractError):
        value.claude_startup_prerequisite(fp,time.monotonic()+(-1 if failure=='deadline' else 10))
    assert events==[]


def asset_seat(seat,monkeypatch):
    value,_,_=seat
    assets=value.root/'.super-coder/assets/runtime/claude'
    assets.parent.mkdir(parents=True)
    shutil.copytree(ROOT/'.super-coder/assets/runtime/claude',assets,ignore=shutil.ignore_patterns('node_modules'))
    (value.root/'home').mkdir()
    value.supervisor.preparation_identity=lambda:{'pid':123,'start_ticks':456,'unit':'fixed-api','control_group':'/fixed-api'}
    monkeypatch.setattr(runtime.shutil,'which',lambda name,**kwargs:'/usr/bin/'+name)
    monkeypatch.setattr(runtime.subprocess,'run',lambda argv,**kwargs:SimpleNamespace(returncode=0,stdout='v22.1.0'))
    return value,assets


def test_dependency_install_is_fixed_pinned_owned_and_child_environment_only(seat,monkeypatch):
    value,assets=asset_seat(seat,monkeypatch)
    parent=dict(os.environ);calls=[]
    class Process:
        pid=987
        def __init__(self,argv,**kwargs):
            calls.append((argv,kwargs))
            installed=assets/'node_modules/@modelcontextprotocol/sdk/package.json'
            installed.parent.mkdir(parents=True);installed.write_text('{"version":"1.31.0"}')
        def wait(self,**kwargs):return 0
        def poll(self):return 0
    monkeypatch.setattr(runtime.subprocess,'Popen',Process)
    value.prepare_claude_assets(time.monotonic()+10)
    value.prepare_claude_assets(time.monotonic()+10)
    assert len(calls)==1
    argv,options=calls[0]
    assert argv==['/usr/bin/npm','ci','--omit=dev','--ignore-scripts','--no-audit','--no-fund']
    assert options['cwd']==assets and options['start_new_session'] is True
    assert options['env']['HOME']==str(value.root/'home') and options['env']['PATH']==os.defpath
    assert set(options['env'])=={'HOME','PATH','npm_config_cache','npm_config_userconfig','npm_config_globalconfig'}
    assert dict(os.environ)==parent and value.claude_assets_ready


def test_dependency_timeout_reaps_only_owned_child_and_stays_inconclusive(seat,monkeypatch):
    value,_=asset_seat(seat,monkeypatch)
    signals=[]
    class Process:
        pid=987
        waits=0
        def __init__(self,*args,**kwargs):pass
        def wait(self,**kwargs):
            self.waits+=1
            if self.waits==1:raise subprocess.TimeoutExpired('fixed npm',1)
            return -15
        def poll(self):return None
    monkeypatch.setattr(runtime.subprocess,'Popen',Process)
    monkeypatch.setattr(runtime.os,'killpg',lambda pid,sig:signals.append((pid,sig)))
    with pytest.raises(RuntimeContractError) as exc:value.prepare_claude_assets(time.monotonic()+10)
    assert exc.value.code=='CHANNEL_UNAVAILABLE' and signals==[(987,runtime.signal.SIGTERM)]
    assert not value.claude_assets_ready and not value.assets_lock.locked()


def test_cached_dependency_parent_alias_never_selects_external_sdk(seat,monkeypatch,tmp_path):
    value,assets=asset_seat(seat,monkeypatch)
    external=tmp_path/'external';external.mkdir()
    (assets/'node_modules').symlink_to(external,target_is_directory=True)
    with pytest.raises(RuntimeContractError):value.prepare_claude_assets(time.monotonic()+10)
    assert not value.claude_assets_ready and not list(external.iterdir())
