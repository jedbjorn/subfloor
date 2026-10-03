"""Reviewed private transfer through the real proxy/socket, never native reads."""
import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.super-coder/scripts'))
from conversation_runtime_contract import (
    DriverStart,
    RuntimeContractError,
    RuntimeIdentity,
    runtime_context_wire,
)
from conversation_runtime_native_probes import ControllerProbeDriver, OwnedProbe
from test_conversation_runtime_native_probes import Store
from test_gui_experiment_native_seat import seat as seat  # noqa: PLC0414
from test_private_history_baseline import baseline, context
from test_private_history_baseline import private as private  # noqa: PLC0414


@pytest.mark.parametrize('kind',['ordinary','history','private_baseline'])
def test_actual_probe_proxy_private_open_serializes_paths_without_public_baseline(private,tmp_path,kind):
    client,owner,native=private
    bound=context(tmp_path,baseline() if kind=='private_baseline' else None)
    if kind=='ordinary':
        bound=dataclasses.replace(bound,history=None,workspace=None,capability_evidence={})
        native.start=lambda context,emit,*,deadline:DriverStart('ready',RuntimeIdentity('old-root'))
    probe=OwnedProbe(bound,client,Store(),1,1,'synthetic-owned-unit',tmp_path,True)
    driver=ControllerProbeDriver(probe)
    try:
        assert driver.start(bound,lambda event:None,deadline=time.monotonic()+3).state=='ready'
        assert owner.context==bound
        # This seam verifies serialization/duplicate binding, not concurrent
        # Journal SQLite access. End the optional synthetic replay before the
        # independent readbacks; all native/reader concurrency claims are held.
        driver._stop.set()
        driver._reader.join(1)
        assert not driver._reader.is_alive()
        assert client.request('open',context=runtime_context_wire(bound),timeout=1)['ready'] is True
        if kind!='ordinary':
            assert native.calls==[bound.history_baseline]
            assert isinstance(owner.context.history.source_worktree,Path)
            assert isinstance(owner.context.workspace.cwd,Path)
            assert isinstance(owner.context.workspace.git_common_dir,Path)
        public=json.dumps(client.request('status',timeout=1))+json.dumps(client.request('subscribe',after=0,timeout=1))
        assert 'history_baseline' not in public and 'private-native-session' not in public
        assert 'old-turn' not in public
        if kind=='private_baseline':
            changed=dataclasses.replace(bound,history_baseline=dataclasses.replace(baseline(),capture_sequence=4))
            with pytest.raises(RuntimeContractError) as caught:
                client.request('open',context=runtime_context_wire(changed),timeout=1)
            assert caught.value.code=='GENERATION_CONFLICT'
    finally:
        assert driver.cleanup(deadline=time.monotonic()+2).outcome=='complete'


def claude_sources(value):
    shutil.copytree(ROOT/'.super-coder/adapters/claude',value.root/'.super-coder/adapters/claude')
    shutil.copytree(ROOT/'.super-coder/assets/runtime/claude',value.root/'.super-coder/assets/runtime/claude')
    value.observers['claude']=SimpleNamespace(observe=value.observers['codex'].observe)


@pytest.mark.parametrize('harness',['codex','claude'])
def test_baseline_helper_mutation_invalidates_fingerprint_and_loaded_source(seat,harness):
    value,_,_=seat
    if harness=='claude':claude_sources(value)
    fp=value.candidate_fingerprint(harness,'selected-model','high')
    value.loaded_implementations[harness]=fp.implementation_digest
    helper=value.root/'.super-coder/scripts/conversation_history_baseline.py'
    assert helper in value.implementation_files(harness)
    value.require_loaded_source(fp)
    helper.write_text(helper.read_text()+'\n# changed consumed private helper\n')
    changed=value.candidate_fingerprint(harness,'selected-model','high')
    assert changed.key!=fp.key and changed.implementation_digest!=fp.implementation_digest
    assert value.cache.admission(changed)['submission']!='compatible'
    with pytest.raises(RuntimeContractError) as caught:value.require_loaded_source(changed)
    assert caught.value.code=='LOADED_SOURCE_CHANGED'


@pytest.mark.parametrize('harness',['codex','claude'])
def test_consumed_helper_missing_refuses_identity_and_preparation(seat,harness):
    value,events,_=seat
    if harness=='claude':claude_sources(value)
    (value.root/'.super-coder/scripts/conversation_history_baseline.py').unlink()
    with pytest.raises(RuntimeContractError) as caught:value.candidate_fingerprint(harness,'selected-model','high')
    assert caught.value.code=='SOURCE_INVALID'
    with pytest.raises(RuntimeContractError):value.prepare('cv','generation')
    assert events==[]
