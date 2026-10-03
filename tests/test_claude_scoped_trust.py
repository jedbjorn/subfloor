"""Exact scoped trust source fixtures; no user config or native calls."""
import dataclasses
import importlib
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path
from unittest import mock

import pytest
import test_claude_setup as inherited

setup, fixture = inherited.setup, inherited.fixture
trust = importlib.import_module('claude_trust')
context = inherited.context
launch_seat = inherited.launch_seat


@pytest.fixture
def files(tmp_path):
    home = tmp_path / 'home'; home.mkdir(mode=0o700)
    main = tmp_path / 'MAIN'; main.mkdir(mode=0o700)
    config = home / '.claude.json'
    value = {'oauthAccount': {'opaque': 'must-preserve'}, 'mcpServers': {'kept': True},
             'future': ['unknown', {'nested': 3}], 'projects': {str(main): {'other': 7}, 'other': {'x': True}}}
    config.write_text(json.dumps(value)); config.chmod(0o600)
    return home, main, config, value


def merge(config, main, verify=lambda: True, seconds=1):
    return trust.merge_trust(config, main, deadline=time.monotonic()+seconds, verify_owned=verify)


def test_exact_boolean_preserves_auth_unknown_mcp_and_other_projects(files):
    _, main, config, original = files
    result = merge(config, main)
    expected = {**original, 'projects': {**original['projects'], str(main): {'other': 7, 'hasTrustDialogAccepted': True}}}
    assert json.loads(config.read_text()) == expected
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert result['state'] == 'trust_written' and result['behavior_verified'] is False
    assert result['native_started'] is False
    assert 'must-preserve' not in json.dumps(result) and str(main) not in json.dumps(result)
    assert not Path(str(config)+'.lock').exists()
    inode = config.stat().st_ino
    assert merge(config, main)['state'] == 'already_trusted'
    assert config.stat().st_ino == inode


@pytest.mark.parametrize('mode', ['default','legacy','override','override_legacy'])
def test_captured_config_resolution_legacy_precedence(tmp_path, mode):
    home=tmp_path/'home';home.mkdir(mode=0o700)
    override=tmp_path/'override';override.mkdir(mode=0o700)
    directory=override if mode.startswith('override') else home/'.claude'
    if not directory.exists():directory.mkdir(mode=0o700)
    if 'legacy' in mode:(directory/'.config.json').write_text('{}')
    observed=trust.selected_config(home,trust.config_override(str(override) if mode.startswith('override') else None))
    expected=directory/'.config.json' if 'legacy' in mode else (override if mode.startswith('override') else home)/'.claude.json'
    assert observed==expected


@pytest.mark.parametrize('bad', ['projects','entry','array','duplicate','nan','oversized','symlink','fifo','hardlink','permissions','foreign'])
def test_refuses_unknown_consumed_shape_or_unsafe_file_without_replacement(files, tmp_path, monkeypatch, bad):
    _, main, config, _ = files
    if bad=='projects':config.write_text('{"projects": []}')
    elif bad=='entry':config.write_text(json.dumps({'projects': {str(main): None}}))
    elif bad=='array':config.write_text('[]')
    elif bad=='duplicate':config.write_text('{"a": 1, "a": 2}')
    elif bad=='nan':config.write_text('{"a": NaN}')
    elif bad=='oversized':config.write_bytes(b'x'*(trust.MAX_BYTES+1))
    elif bad=='symlink':
        target=tmp_path/'other';config.rename(target);config.symlink_to(target)
    elif bad=='fifo':config.unlink();os.mkfifo(config,0o600)
    elif bad=='hardlink':os.link(config,tmp_path/'link')
    elif bad=='permissions':config.chmod(0o644)
    elif bad=='foreign':
        uid=os.getuid();monkeypatch.setattr(trust.os,'getuid',lambda:uid+1)
    before=config.lstat()
    with pytest.raises((RuntimeError, OSError, ValueError)):
        merge(config,main)
    assert config.lstat().st_ino==before.st_ino
    assert not Path(str(config)+'.lock').exists()


def test_missing_config_is_created_under_native_lock(files):
    _,main,config,_=files;config.unlink()
    assert merge(config,main)['exact_main_trusted'] is True
    assert json.loads(config.read_text())=={'projects':{str(main):{'hasTrustDialogAccepted':True}}}


def native_actor(config, ready, finish):
    # Actual captured primitive; inert Python actor, not Claude.
    lock=Path(str(config)+'.lock');lock.mkdir(mode=0o700)
    ready.set()
    if not finish.wait(2):return
    value=json.loads(config.read_text());value['concurrent_fresh_field']='preserved'
    config.write_text(json.dumps(value));lock.rmdir()


def test_two_independent_actors_refuse_then_merge_fresh_disk(files):
    _,main,config,_=files
    ctx=multiprocessing.get_context('fork');ready=ctx.Event();finish=ctx.Event()
    actor=ctx.Process(target=native_actor,args=(config,ready,finish));actor.start()
    try:
        assert ready.wait(1)
        with pytest.raises(FileExistsError):merge(config,main)
        finish.set();actor.join(2);assert actor.exitcode==0
        assert merge(config,main)['state']=='trust_written'
        assert json.loads(config.read_text())['concurrent_fresh_field']=='preserved'
    finally:
        if actor.is_alive():actor.kill();actor.join(1)


@pytest.mark.parametrize('mutation',['owner','deadline','target','lock','tail'])
def test_post_read_late_changes_never_overwrite_foreign_or_late_state(files, mutation, monkeypatch):
    _,main,config,original=files
    calls=0
    def verify():
        nonlocal calls
        calls+=1
        if calls==2:
            if mutation=='owner':return False
            if mutation=='deadline':
                monkeypatch.setattr(trust.time,'monotonic',lambda:float('inf'))
            if mutation=='target':
                config.unlink();config.write_text('{"late": true}');config.chmod(0o600)
            if mutation=='tail':
                config.write_text(json.dumps({**original,'late':True}))
            if mutation=='lock':
                lock=Path(str(config)+'.lock');lock.rmdir();lock.mkdir(mode=0o700)
        return True
    with pytest.raises((RuntimeError, OSError)):
        merge(config,main,verify)
    assert json.loads(config.read_text()).get('projects',{}).get(str(main),{}).get('hasTrustDialogAccepted') is not True
    if mutation=='lock':assert Path(str(config)+'.lock').exists()


def test_lock_hold_is_independently_capped_even_with_long_outer_budget(files, monkeypatch):
    _,main,config,_=files;start=time.monotonic();clock=[start]
    monkeypatch.setattr(trust.time,'monotonic',lambda:clock[0])
    calls=0
    def verify():
        nonlocal calls
        calls+=1
        if calls==2:clock[0]=start+2.01
        return True
    with pytest.raises(RuntimeError):merge(config,main,verify,seconds=20)
    assert not Path(str(config)+'.lock').exists()


def test_same_pending_lock_never_reclaimed_even_if_old(files):
    _,main,config,_=files;lock=Path(str(config)+'.lock');lock.mkdir()
    os.utime(lock,(0,0));before=lock.stat()
    with pytest.raises(FileExistsError):merge(config,main)
    assert lock.stat().st_ino==before.st_ino


def test_prepared_real_canonical_context_and_changed_required_source(context, files, monkeypatch):
    home,_,_,_=files
    context=dataclasses.replace(context,env={**context.env,'HOME':str(home)})
    with pytest.raises(RuntimeError):
        trust.prepared_trust(context,context.worktree,deadline=time.monotonic()+3,verify_owned=lambda:True)
    # Clearly synthetic source recognition; actual Git/boot/MCP/token guards
    # run unmodified. This cannot certify an installed native version.
    monkeypatch.setattr(trust,'NATIVE_SHA256',context.executable.sha256)
    result=trust.prepared_trust(context,context.worktree,deadline=time.monotonic()+3,verify_owned=lambda:True)
    assert result['exact_main_trusted'] is True
    context.executable.path.write_text('changed')
    with pytest.raises(RuntimeError):
        trust.prepared_trust(context,context.worktree,deadline=time.monotonic()+3,verify_owned=lambda:True)


def test_legacy_selection_added_after_read_refuses_default_write(context, files, monkeypatch):
    home,_,config,_=files;directory=home/'.claude';directory.mkdir(mode=0o700)
    context=dataclasses.replace(context,env={**context.env,'HOME':str(home)})
    monkeypatch.setattr(trust,'NATIVE_SHA256',context.executable.sha256)
    calls=0
    def verify():
        nonlocal calls
        calls+=1
        if calls==3:(directory/'.config.json').write_text('{}')
        return True
    with pytest.raises(RuntimeError):
        trust.prepared_trust(context,context.worktree,deadline=time.monotonic()+3,verify_owned=verify)
    assert str(context.worktree) not in json.loads(config.read_text())['projects']


@pytest.mark.parametrize('mutation',['source','custom_oauth','parent'])
def test_captured_source_and_parent_changes_refuse_before_trust_commit(context,files,monkeypatch,mutation):
    home,_,config,_=files
    context=dataclasses.replace(context,env={**context.env,'HOME':str(home)})
    monkeypatch.setattr(trust,'NATIVE_SHA256',context.executable.sha256)
    if mutation=='custom_oauth':
        context=dataclasses.replace(context,env={**context.env,'CLAUDE_CODE_CUSTOM_OAUTH_URL':'unsupported'})
    calls=0
    def verify():
        nonlocal calls
        calls+=1
        if calls==3:
            if mutation=='source':context.executable.path.write_text('changed native source')
            if mutation=='parent':
                home.rename(home.with_name('moved-home'))
                home.mkdir(mode=0o700)
                config.write_text('{}');config.chmod(0o600)
        return True
    with pytest.raises((RuntimeError,OSError)):
        trust.prepared_trust(context,context.worktree,deadline=time.monotonic()+3,verify_owned=verify)
    assert str(context.worktree) not in json.loads(config.read_text()).get('projects',{})


def test_new_content_at_final_commit_callback_is_preserved(files):
    _,main,config,_=files;calls=0
    def verify():
        nonlocal calls
        calls+=1
        if calls==3:config.write_text('{"late": "must-preserve"}')
        return True
    with pytest.raises(RuntimeError):merge(config,main,verify)
    assert json.loads(config.read_text())=={'late':'must-preserve'}
    assert not any('.sc-trust-' in p.name for p in config.parent.iterdir())


def test_fixed_pretrust_launch_preserves_only_validated_captured_override(launch_seat,tmp_path,monkeypatch):
    supervisor,record,_,starts,_=launch_seat
    root=Path(record['root']);(root/'maintainer/claude_trust.py').write_text('synthetic source')
    override=tmp_path/'native-config';override.mkdir(mode=0o700)
    monkeypatch.setattr(setup,'service_bus',lambda:{'XDG_RUNTIME_DIR':'/run/user/1','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1/bus'})
    with mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(override),'CLAUDE_CODE_CUSTOM_OAUTH_URL':'must-not-transfer'},clear=True):
        supervisor.launch_claude_pretrust('a'*32,deadline=time.monotonic()+5)
    argv=starts[0][0]
    assert 'CLAUDE_CONFIG_DIR='+str(override) in argv
    assert not any('must-not-transfer' in x for x in argv)


def test_fixed_pretrust_vector_has_no_native_child_or_tui(launch_seat, monkeypatch):
    supervisor,record,_,starts,_=launch_seat
    root=Path(record['root'])
    (root/'maintainer/claude_trust.py').write_text('synthetic source, no execution')
    monkeypatch.setattr(setup,'service_bus',lambda:{'XDG_RUNTIME_DIR':'/run/user/1','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1/bus'})
    monkeypatch.delenv('CLAUDE_CONFIG_DIR',raising=False)
    supervisor.launch_claude_pretrust('a'*32,deadline=time.monotonic()+5)
    argv=starts[0][0]
    assert '_pretrust' in argv and '--pipe' in argv and '--pty' not in argv
    assert '_child' not in argv and not any(x.startswith(('HOME=','ANTHROPIC_','CODEX_')) for x in argv)


def test_pretrust_unit_uses_prepared_main_and_never_run_setup(context, monkeypatch):
    generation=context.generation_id
    monkeypatch.setattr(setup,'fixture_module',lambda:fixture)
    monkeypatch.setattr(fixture,'verify_receipt',lambda path:{'native_units':[{'generation_id':generation,'trust_helper_sha256':setup.digest(Path(trust.__file__))}]})
    monkeypatch.setattr(fixture,'verify_root',lambda record:context.worktree)
    monkeypatch.setattr(setup,'setup_owned',lambda *args:True)
    monkeypatch.setattr(setup,'service_bus',dict)
    monkeypatch.setattr(setup,'setup_phase',lambda *args:None)
    monkeypatch.setattr(setup,'prepare_claude_setup_context',lambda *args:context)
    monkeypatch.setattr(setup,'close_setup_conversation',lambda *args:None)
    calls=[]
    monkeypatch.setattr(setup,'run_setup',lambda *args,**kwargs:calls.append('native'))
    expected={'state':'trust_written','exact_main_trusted':True,'target_sha256':'a'*64,'native_started':False,'behavior_verified':False}
    def prepared(ctx,main,**kwargs):
        assert ctx is context and main==context.worktree and kwargs['verify_owned']() is True
        return expected
    monkeypatch.setattr(trust,'prepared_trust',prepared)
    with mock.patch.dict(os.environ,{},clear=True):
        assert setup.setup_unit(context.worktree/'receipt',generation,context.executable.path,context.executable.sha256,time.monotonic()+5,pretrust=True)==expected
    assert calls==[]
    assert trust.read_result(context.state_root,generation,setup.digest(Path(trust.__file__)),deadline=time.monotonic()+1)==expected


@pytest.mark.parametrize('bad',['generation','helper','extra','wrong_bool','symlink','fifo','missing','oversized'])
def test_private_result_malformed_is_unavailable_not_trusted(tmp_path,bad):
    tmp_path.chmod(0o700);target=tmp_path/'claude-trust-result.json'
    value={'state':'trust_written','exact_main_trusted':True,'target_sha256':'a'*64,'native_started':False,'behavior_verified':False,'generation':'b'*32,'helper_sha256':'c'*64}
    if bad=='generation':value['generation']='wrong'
    if bad=='helper':value['helper_sha256']='wrong'
    if bad=='extra':value['private']='never-export'
    if bad=='wrong_bool':value['exact_main_trusted']=1
    if bad=='fifo':os.mkfifo(target,0o600)
    elif bad=='symlink':target.symlink_to(tmp_path/'other')
    elif bad!='missing':
        target.write_text(json.dumps(value) if bad!='oversized' else 'x'*513);target.chmod(0o600)
    result=trust.read_result(tmp_path,'b'*32,'c'*64,deadline=time.monotonic()+1)
    assert result['state']=='trust_inconclusive' and result['exact_main_trusted'] is False
    assert 'never-export' not in json.dumps(result)
