"""Fixed setup source tests; synthetic PTY subprocesses are not native proof."""
import dataclasses
import hashlib
import json
import os
import pty
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'maintainer'),str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import claude_setup as setup
import gui_experiment as fixture
from conversation_runtime_contract import (
    ExecutableBinding,
    RuntimeContext,
    RuntimeContractError,
)
from test_claude_runtime_driver import MEMORY_SOURCE


@pytest.fixture
def context(tmp_path):
    root=tmp_path/'fixture';root.mkdir();
    subprocess.run(['/usr/bin/git','-C',str(root),'init','-q'],check=True,env={'PATH':os.defpath})
    engine=root/'.super-coder';engine.mkdir();(engine/'adapters/claude').mkdir(parents=True)
    (engine/'adapters/claude/adapter.json').write_text(json.dumps({'launch_flags':['--dangerously-skip-permissions']}))
    (engine/'instance.json').write_text('{"port":12345}')
    with sqlite3.connect(engine/'shell_db.db') as con:
        con.execute('CREATE TABLE shells(shell_id INTEGER,user_id INTEGER,flavor TEXT,api_key TEXT)')
        con.execute("INSERT INTO shells VALUES(4,1,'admin','synthetic-fixture-token')")
    generation='a'*32;state=root/'runtime'/generation;state.mkdir(parents=True,mode=0o700)
    boot='No harness memory. Synthetic setup at MAIN.'
    for name in ('CLAUDE.md','AGENTS.md'):(root/name).write_text(boot)
    binary=root/'synthetic-native';binary.write_text(MEMORY_SOURCE)
    mcp=state/'managed.json';mcp.write_text('{"mcpServers":{"browser":{"type":"http","url":"http://127.0.0.1:12345/mcp/FXSETUP"}}}')
    return RuntimeContext(generation,'cv_fixture_setup_'+generation,4,1,'claude',state,root,
        ExecutableBinding(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'source-fixture'),'test',
        hashlib.sha256(boot.encode()).hexdigest(),'b'*64,'bypassPermissions',provider='anthropic',
        model='claude-sonnet-5-5',effort='high',boot_content=boot,managed_mcp_files=(mcp,),
        env={'HOME':'/native/in-place','PATH':os.defpath,'SC_ROOT':str(root),'SC_ENGINE_DIR':str(engine),
             'SC_SHELL_ID':'4','SC_SHELL_SHORTNAME':'fxsetup','SC_API_TOKEN':'synthetic-fixture-token',
             'SC_API_BASE':'http://127.0.0.1:12345','SC_F89_SETUP_RECEIPT':str(root/'fixture-receipt')})


def test_fixed_native_argv_has_no_prompt_resume_channel_or_controller(context):
    context=dataclasses.replace(context,env=dict(context.env)|{'ANTHROPIC_API_KEY':'private',
        'CLAUDE_CODE_RESUME_INTERRUPTED_TURN':'1','CLAUDE_CODE_SKIP_PROMPT_HISTORY':'1','SC_F89_CONTROLLER_ENDPOINT':'private'})
    argv,env,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    assert argv==[str(context.executable.path),'--session-id',str(__import__('uuid').UUID(hex=context.generation_id)),
        '--settings',str(context.state_root/'claude-setup-settings.json'),'--strict-mcp-config','--mcp-config',
        str(context.managed_mcp_files[0]),'--dangerously-skip-permissions','--disallowedTools','CronCreate',
        '--model',context.model,'--effort','high']
    assert not {'--resume','--bg','--print','-p','--dangerously-load-development-channels'}&set(argv)
    assert not {'ANTHROPIC_API_KEY','CLAUDE_CODE_RESUME_INTERRUPTED_TURN','CLAUDE_CODE_SKIP_PROMPT_HISTORY','SC_F89_CONTROLLER_ENDPOINT'}&set(env)
    assert env['HOME']=='/native/in-place' and env['CLAUDE_CODE_DISABLE_AUTO_MEMORY']=='1'
    assert env['SC_API_TOKEN']=='synthetic-fixture-token'
    settings=json.loads((context.state_root/'claude-setup-settings.json').read_text())
    assert settings['autoMemoryEnabled'] is False and settings['permissions']=={'deny':['CronCreate']}
    assert set(settings['hooks'])=={'SessionStart'}
    assert not {'capabilities','grade','auto_memory_disabled','inference_count'}&binding.keys()
    assert all((context.state_root/name).stat().st_mode&0o777==0o600 for name in ('claude-setup-settings.json','claude-setup-binding.json'))


@pytest.mark.parametrize('field',['harness','provider','controller','grades','probe','policy','main','boot','binary','token','api','mcp','alias'])
def test_noncanonical_context_refuses_before_settings_or_native_process(context,field,tmp_path):
    if field=='harness':context=dataclasses.replace(context,harness='codex')
    if field=='provider':context=dataclasses.replace(context,provider='api')
    if field=='controller':context=dataclasses.replace(context,controller_endpoint=context.state_root/'controller.sock')
    if field=='grades':context=dataclasses.replace(context,capability_evidence={'submission':'compatible'})
    if field=='probe':context=dataclasses.replace(context,probe_capabilities=('submission',))
    if field=='policy':context=dataclasses.replace(context,permission_mode='default')
    if field=='main':__import__('shutil').rmtree(context.worktree/'.git')
    if field=='boot':(context.worktree/'CLAUDE.md').write_text('changed')
    if field=='binary':context.executable.path.write_text('changed')
    if field=='token':context=dataclasses.replace(context,env=dict(context.env)|{'SC_API_TOKEN':'wrong'})
    if field=='api':context=dataclasses.replace(context,env=dict(context.env)|{'SC_API_BASE':'http://127.0.0.1:9999'})
    if field=='mcp':context.managed_mcp_files[0].write_text('{"mcpServers":{"browser":{"type":"http","url":"http://127.0.0.1:9999/mcp/FXSETUP"}}}')
    if field=='alias':
        alias=tmp_path/'alias';alias.symlink_to(context.worktree,target_is_directory=True)
        context=dataclasses.replace(context,worktree=alias)
    with pytest.raises(RuntimeContractError):setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    assert not (context.state_root/'claude-setup-settings.json').exists()


@pytest.mark.parametrize('deadline',[float('nan'),float('inf'),True,-1,0])
def test_invalid_budget_is_static_and_writes_nothing(context,deadline):
    with pytest.raises(RuntimeContractError):setup.fixed_launch(context,context.worktree,deadline)
    assert not (context.state_root/'claude-setup-settings.json').exists()


@pytest.mark.parametrize('file',['CLAUDE.md','AGENTS.md','managed','settings','observer','executable','canonical_hooks'])
def test_postpreparation_configuration_change_blocks_exec_gate(context,file,monkeypatch):
    canonical=context.worktree/'.claude/settings.local.json'
    canonical.parent.mkdir();canonical.write_text('{"hooks":{}}')
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    if file=='observer':
        # Avoid touching real source; replace the captured digest comparison.
        original=setup.digest
        monkeypatch.setattr(setup,'digest',lambda path:'f'*64 if path==Path(setup.__file__).resolve() else original(path))
    else:
        path={'CLAUDE.md':context.worktree/'CLAUDE.md','AGENTS.md':context.worktree/'AGENTS.md',
              'managed':context.managed_mcp_files[0],'settings':context.state_root/'claude-setup-settings.json',
              'executable':context.executable.path,'canonical_hooks':canonical}[file]
        path.write_text('changed')
    with pytest.raises(RuntimeContractError):setup.revalidate(binding,time.monotonic()+3)


def test_observation_requires_actual_owned_native_descendant(context,monkeypatch):
    setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    setup.write_private(context.state_root/'claude-setup-child.json',{'pid':os.getpid(),'start_ticks':setup.process(os.getpid())[0],'cgroup':setup.process(os.getpid())[1]})
    monkeypatch.setenv('SC_F89_SETUP_GENERATION',context.generation_id)
    monkeypatch.setenv('CLAUDE_CODE_DISABLE_AUTO_MEMORY','1')
    event={'hook_event_name':'SessionStart','source':'startup','session_id':str(__import__('uuid').UUID(hex=context.generation_id)),
           'cwd':str(context.worktree),'private':'never copy'}
    # Our pytest process is not the captured executable; no static grant.
    with pytest.raises(RuntimeContractError):setup.observe_hook(context.state_root,json.dumps(event).encode())
    assert not (context.state_root/'claude-setup-observation.json').exists()
    for change in ({'source':'resume'},{'session_id':'wrong'},{'cwd':'wrong'}):
        with pytest.raises(RuntimeContractError):setup.observe_hook(context.state_root,json.dumps(event|change).encode())


def test_private_file_replacement_and_duplicate_hook_are_refused(tmp_path):
    target=tmp_path/'target';target.write_text('sentinel')
    alias=tmp_path/'alias';alias.symlink_to(target)
    with pytest.raises(OSError):setup.write_private(alias,{'private':'data'})
    assert target.read_text()=='sentinel'
    path=tmp_path/'record';setup.write_private(path,{'first':True})
    with pytest.raises(FileExistsError):setup.write_private(path,{'first':False})
    assert json.loads(path.read_text())=={'first':True}


def test_no_operator_terminal_is_no_child(context,monkeypatch):
    calls=[];monkeypatch.setattr(setup.subprocess,'Popen',lambda *a,**k:calls.append(a))
    monkeypatch.setattr(setup.os,'isatty',lambda fd:False)
    with pytest.raises(RuntimeContractError):setup.run_setup(context,context.worktree,verify_owned=lambda:True,
        record_child=lambda *args:True,deadline=time.monotonic()+3)
    assert calls==[]


@pytest.mark.parametrize('registered',[False,True])
def test_synthetic_pty_child_gate_is_after_durable_callback(context,monkeypatch,registered):
    """Runs only finite Python, no installed Claude CLI/auth/account access."""
    marker=context.state_root/'synthetic-exec'
    binary=Path(sys.executable).resolve()
    binding={'executable':str(binary),'executable_sha256':setup.digest(binary),'files':{},'worktree':str(context.worktree)}
    monkeypatch.setattr(setup,'fixed_launch',lambda *args:([str(binary),'-c',f'from pathlib import Path;Path({str(marker)!r}).write_text("finite synthetic child")'],{'PATH':os.defpath},binding))
    popen=setup.subprocess.Popen
    def synthetic_child(argv,**kwargs):
        fd=kwargs['pass_fds'][0]
        body='import json,os;f=os.fdopen('+str(fd)+',"rb");v=json.loads(f.read());os.chdir(v["binding"]["worktree"]);os.execvpe(v["argv"][0],v["argv"],v["env"])'
        return popen([sys.executable,'-c',body],**kwargs)
    monkeypatch.setattr(setup.subprocess,'Popen',synthetic_child)
    outer,terminal=pty.openpty();calls=[]
    def record(pid,ticks,group):
        calls.append((pid,ticks,group));assert not marker.exists();return registered
    try:
        if registered:
            result=setup.run_setup(context,context.worktree,verify_owned=lambda:True,record_child=record,
                deadline=time.monotonic()+2,input_fd=terminal,output_fd=terminal)
            assert marker.read_text()=='finite synthetic child' and result['state']=='setup_inconclusive'
            assert result['native_input_generated'] is False and result['observation_present'] is False
        else:
            with pytest.raises(RuntimeContractError):setup.run_setup(context,context.worktree,verify_owned=lambda:True,
                record_child=record,deadline=time.monotonic()+2,input_fd=terminal,output_fd=terminal)
            assert not marker.exists()
        assert len(calls)==1
        with pytest.raises(FileNotFoundError):setup.process(calls[0][0])
    finally:
        os.close(outer);os.close(terminal)


def test_setup_purpose_registration_cannot_be_controller_launch(monkeypatch):
    # Exercise actual method admission without native/systemd creation.
    native={'generation_id':'a'*32,'harness':'claude','status':'registered','purpose':'claude_setup'}
    record={'fixture_id':'b'*32,'native_units':[native]}
    monkeypatch.setattr(fixture,'read_json',lambda path:record)
    monkeypatch.setattr(fixture,'verify_receipt',lambda path:record)
    monkeypatch.setattr(fixture,'verify_root',lambda record:Path('/fixed'))
    monkeypatch.setattr(fixture,'verify_native',lambda *args:None)
    from contextlib import nullcontext
    monkeypatch.setattr(fixture,'ownership_lock',lambda *args,**kwargs:nullcontext())
    calls=[];monkeypatch.setattr(fixture,'command',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(fixture.FixtureError):fixture.NativeSupervisor(Path('/fixed/receipt')).launch('a'*32)
    assert calls==[]


def test_setup_cli_exposes_only_receipt_no_custom_paths_or_prompt(monkeypatch):
    captured=[]
    monkeypatch.setattr(fixture.NativeSupervisor,'launch_claude_setup',lambda self,generation,**kwargs:captured.append((generation,kwargs)) or {'os_cleanup':{'complete':True}})
    monkeypatch.setattr(fixture,'canonical_receipt',lambda path:path)
    assert fixture.main(['claude-setup','--receipt','/fixed/receipt'])==0
    assert len(captured)==1 and setup.budget(captured[0][1]['deadline'])<=30
    for extra in ('--model','--executable','--command','--env','--prompt','--root'):
        with pytest.raises(SystemExit):fixture.main(['claude-setup','--receipt','/fixed/receipt',extra,'arbitrary'])


@pytest.fixture
def preparer(context,monkeypatch):
    root=context.worktree;database=root/'.super-coder/shell_db.db'
    database.unlink()
    with sqlite3.connect(database) as con:
        con.executescript('CREATE TABLE shells(shell_id INTEGER PRIMARY KEY,display_name TEXT,shortname TEXT,flavor TEXT,system_prompt TEXT,user_id INTEGER,api_key TEXT);'
            'CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,shell_id INTEGER,owner_user_id INTEGER,harness TEXT,provider TEXT,model TEXT,effort TEXT,worktree TEXT,creation_idempotency_key TEXT,creation_request_hash TEXT,runtime_projection TEXT,state TEXT DEFAULT "idle",closed_at TEXT);')
    record={'fixture_id':'c'*32,'root':str(root),'native_units':[{'generation_id':context.generation_id,'purpose':'claude_setup','root':str(context.state_root)}]}
    monkeypatch.setattr(fixture,'verify_receipt',lambda receipt:record)
    monkeypatch.setattr(fixture,'verify_root',lambda record:root)
    monkeypatch.setattr(setup,'setup_owned',lambda *args:True)
    monkeypatch.setenv('HOME',str(root/'home'))
    calls=[]
    def prepare(**kwargs):
        calls.append(kwargs)
        assert kwargs['conversation_owned'] is True and kwargs['shell_id']==1
        assert kwargs['harness']=='claude' and kwargs['model']=='claude-sonnet-5-5' and kwargs['effort']=='high'
        with sqlite3.connect(database) as con:
            assert con.execute('SELECT flavor,user_id FROM shells').fetchone()==('admin',1)
            token=con.execute('SELECT api_key FROM shells').fetchone()[0]
        boot='canonical MAIN context'
        for name in ('CLAUDE.md','AGENTS.md'):(root/name).write_text(boot)
        return SimpleNamespace(argv=[],cwd=str(root),boot_content=boot,model=kwargs['model'],effort='high',
            execution_view=SimpleNamespace(prefix=()),env=dict(context.env)|{'SC_SHELL_ID':'1','SC_SHELL_SHORTNAME':'fxccccccccccsetup','SC_API_TOKEN':token})
    fake=SimpleNamespace(ENGINE=root/'.super-coder',REPO_ROOT=root,DB_PATH=database,prepare_launch=prepare,
        load_adapter=lambda name:{'launch_flags':['--dangerously-skip-permissions']},
        managed_mcp_injection=lambda *args:{'name':'browser','launch_args':['--mcp-config',json.dumps({'mcpServers':{'browser':{'type':'http','url':'http://127.0.0.1:12345/mcp/FXCCCCCCCCCCSETUP'}}})]})
    monkeypatch.setitem(sys.modules,'run',fake)
    return SimpleNamespace(receipt=Path('/fixed/receipt')),fake,calls,context


def test_setup_is_genuinely_canonical_start_and_resume_at_main(preparer):
    owner,_run,calls,context=preparer
    value=setup.prepare_claude_setup_context(owner,context.generation_id,context.executable.path,context.executable.sha256,time.monotonic()+3)
    assert [x['boot'].phase for x in calls]==['start','resume']
    assert value.worktree==context.worktree and value.boot_content=='canonical MAIN context'
    assert value.controller_endpoint is None and not value.capability_evidence and not value.probe_capabilities
    assert value.executable.version=='captured-unobserved-version'
    assert value.conversation_id=='cv_fixture_setup_'+context.generation_id
    assert len(setup.validate(value,value.worktree,time.monotonic()+3))==64
    assert not {'memory_policy','native_route','account_type'}&dict(value.env).keys()


@pytest.mark.parametrize('failure',['foreign_engine','native_home','linked_plan','argv','changed_boot','owner_changed','wrong_shell'])
def test_canonical_preparation_failure_never_returns_native_context(preparer,monkeypatch,failure):
    owner,run,calls,context=preparer
    original=run.prepare_launch
    if failure=='foreign_engine':run.ENGINE=Path('/foreign/engine')
    if failure=='native_home':monkeypatch.setenv('HOME','/native/in-place')
    if failure=='wrong_shell':
        with sqlite3.connect(run.DB_PATH) as con:
            con.execute("INSERT INTO shells VALUES(1,'bad','fxccccccccccsetup','dev','bad',1,'token')")
    if failure=='owner_changed':monkeypatch.setattr(setup,'setup_owned',lambda *args:len(calls)<2)
    def broken(**kwargs):
        result=original(**kwargs)
        if failure=='linked_plan':result.cwd=str(context.worktree/'.sc-worktrees/other')
        if failure=='argv':result.argv=['claude','-p','not allowed']
        if failure=='changed_boot' and len(calls)==2:result.boot_content='changed'
        return result
    run.prepare_launch=broken
    with pytest.raises(RuntimeContractError):setup.prepare_claude_setup_context(owner,context.generation_id,context.executable.path,context.executable.sha256,time.monotonic()+3)
    assert not (context.state_root/'claude-setup-managed-mcp.json').exists()
    with sqlite3.connect(run.DB_PATH) as con:
        assert con.execute("SELECT count(*) FROM conversations WHERE state!='closed'").fetchone()[0]==0


def test_setup_generation_cannot_be_prepared_again(preparer):
    owner,_run,calls,context=preparer
    setup.prepare_claude_setup_context(owner,context.generation_id,context.executable.path,context.executable.sha256,time.monotonic()+3)
    with pytest.raises(RuntimeContractError):setup.prepare_claude_setup_context(owner,context.generation_id,context.executable.path,context.executable.sha256,time.monotonic()+3)
    assert len(calls)==2


def test_cleanup_checks_captured_setup_child_even_after_parent_exit(monkeypatch):
    native={'unit':'fixed','endpoint':'/absent/socket','setup_child':{'pid':123,'start_ticks':456}}
    record={}
    monkeypatch.setattr(fixture,'verify_native',lambda *args:None)
    monkeypatch.setattr(fixture,'native_unit_state',lambda n:{'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'})
    monkeypatch.setattr(fixture,'unit_state',lambda *args,**kwargs:{'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'})
    monkeypatch.setattr(fixture,'cgroup_pids',lambda group:[])
    monkeypatch.setattr(fixture,'process_start_ticks',lambda pid:456)
    with pytest.raises(fixture.FixtureError):fixture.stop_native_unit(record,native,deadline=time.monotonic()+.03)
    assert native['os_cleanup']['complete'] is False


def test_owner_withdrawal_after_child_registration_does_not_release_exec(context,monkeypatch):
    marker=context.state_root/'never-exec';binary=Path(sys.executable).resolve()
    binding={'executable':str(binary),'executable_sha256':setup.digest(binary),'files':{},'worktree':str(context.worktree)}
    monkeypatch.setattr(setup,'fixed_launch',lambda *args:([str(binary),'-c',f'from pathlib import Path;Path({str(marker)!r}).touch()'],{},binding))
    owns=[True];children=[]
    def record(pid,ticks,group):children.append(pid);owns[0]=False;return True
    outer,terminal=pty.openpty()
    try:
        with pytest.raises(RuntimeContractError):setup.run_setup(context,context.worktree,verify_owned=lambda:owns[0],record_child=record,
            deadline=time.monotonic()+2,input_fd=terminal,output_fd=terminal)
        assert len(children)==1 and not marker.exists()
        with pytest.raises(FileNotFoundError):setup.process(children[0])
    finally:os.close(outer);os.close(terminal)


@pytest.fixture
def launch_seat(tmp_path,monkeypatch):
    from contextlib import nullcontext
    root=tmp_path/'main';root.mkdir();(root/'.git').mkdir();(root/'maintainer').mkdir()
    (root/'maintainer/claude_setup.py').write_text('# fixed copied helper')
    native=root/'synthetic-native';native.write_text('# source fixture only')
    record={'fixture_id':'b'*32,'root':str(root),'status':'serving','ownership_nonce':'private','native_units':[]}
    saves=[];starts=[];done=[False]
    monkeypatch.setattr(fixture,'verify_receipt',lambda receipt:record)
    monkeypatch.setattr(fixture,'verify_root',lambda record:root)
    monkeypatch.setattr(fixture,'read_json',lambda path:record)
    monkeypatch.setattr(fixture,'ownership_lock',lambda *args,**kwargs:nullcontext())
    monkeypatch.setattr(fixture,'verify_native',lambda *args:None)
    monkeypatch.setattr(fixture,'save',lambda r,p:saves.append(json.loads(json.dumps(r))))
    monkeypatch.setattr(fixture.os,'isatty',lambda fd:True)
    monkeypatch.setattr(fixture.shutil,'which',lambda name:str(native))
    def register(self,generation,harness,**kwargs):
        entry={'generation_id':generation,'harness':harness,'purpose':kwargs['purpose'],'status':'registered',
            'unit':'fixed-native-unit','root':str(root/'runtime'/generation),'endpoint':str(root/'absent.sock')}
        record['native_units'].append(entry);saves.append(json.loads(json.dumps(record)));return entry
    monkeypatch.setattr(fixture.NativeSupervisor,'register',register)
    def state(*args,**kwargs):
        entry=record['native_units'][0]
        return {'LoadState':'not-found' if done[0] else 'loaded','ActiveState':'inactive' if done[0] else 'active',
            'MainPID':'0' if done[0] else '123','Description':fixture.native_description(record,entry),'ControlGroup':'/owned-fixture'}
    monkeypatch.setattr(fixture,'unit_state',state)
    monkeypatch.setattr(fixture,'process_start_ticks',lambda pid:None if done[0] else 456)
    monkeypatch.setattr(fixture,'cgroup_pids',lambda group:[])
    class Launcher:
        def __init__(self,argv,**kwargs):
            starts.append((argv,kwargs));assert saves[-1]['native_units'][0]['status']=='starting'
        def poll(self):return 0 if done[0] else None
        def wait(self,**kwargs):
            assert saves[-1]['native_units'][0]['main_pid']==123
            done[0]=True;return 0
        def terminate(self):done[0]=True
        def kill(self):done[0]=True
    monkeypatch.setattr(fixture.subprocess,'Popen',Launcher)
    return fixture.NativeSupervisor(root/'receipt'),record,saves,starts,done


def test_fixed_setup_unit_registration_precedes_start_then_identity_precedes_wait(launch_seat):
    supervisor,_record,saves,starts,_done=launch_seat
    result=supervisor.launch_claude_setup('a'*32,deadline=time.monotonic()+5)
    assert saves[0]['native_units'][0]['status']=='registered'
    assert result['os_cleanup']['complete'] is True and result['setup_state']=='setup_inconclusive'
    argv,options=starts[0]
    assert '--pty' in argv and '--wait' in argv and 'TasksMax=128' in argv and 'MemoryMax=2048M' in argv
    assert not any(x.startswith('--setenv') for x in argv)
    assert argv[argv.index('--executable')+1].endswith('synthetic-native')
    assert '/usr/bin/env' in argv and '-i' in argv and '-I' in argv
    assert 'SC_API_TOKEN' not in options['env'] and 'HOME' not in options['env']
    assert result['purpose']=='claude_setup' and 'capabilities' not in result
    assert not {'native_route','memory_policy','inference_count'}&result.keys()


def test_existing_setup_never_restarts_or_replays(launch_seat):
    supervisor,record,_saves,starts,_done=launch_seat
    supervisor.launch_claude_setup('a'*32,deadline=time.monotonic()+5)
    with pytest.raises(fixture.FixtureError):supervisor.launch_claude_setup('a'*32,deadline=time.monotonic()+5)
    assert len(starts)==1 and len(record['native_units'])==1


def test_expired_setup_budget_never_allocates(launch_seat):
    supervisor,record,_saves,starts,_done=launch_seat
    with pytest.raises(RuntimeContractError):supervisor.launch_claude_setup('a'*32,deadline=time.monotonic()-1)
    assert starts==[] and record['native_units']==[]


def test_launch_failure_retains_registered_owned_resource_and_bounds_cleanup(launch_seat,monkeypatch):
    supervisor,record,saves,_starts,done=launch_seat
    def failed(*args,**kwargs):done[0]=True;raise OSError('synthetic spawn failure')
    monkeypatch.setattr(fixture.subprocess,'Popen',failed)
    with pytest.raises(OSError):supervisor.launch_claude_setup('a'*32,deadline=time.monotonic()+5)
    assert len(record['native_units'])==1 and record['native_units'][0]['os_cleanup']['complete'] is True
    assert saves[-1]['native_units'][0]['setup_state']=='setup_inconclusive'


@pytest.mark.parametrize('changed',['none','purpose','status','fixture','child','parent','executable','helper','unit'])
def test_final_child_exec_edge_rechecks_durable_owner(context,monkeypatch,changed):
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    pid=os.getpid();parent=os.getppid();ticks,group=setup.process(pid)
    parent_ticks,_=setup.process(parent)
    native={'generation_id':context.generation_id,'purpose':'claude_setup','status':'active',
        'main_pid':parent,'main_pid_start_ticks':parent_ticks,'control_group':group,
        'setup_child':{'pid':pid,'start_ticks':ticks,'control_group':group},
        'setup_executable_sha256':context.executable.sha256,'setup_helper_sha256':setup.digest(Path(setup.__file__).resolve())}
    record={'status':'serving','native_units':[native]}
    state={'Description':'fixed-owned-unit','ActiveState':'active','MainPID':str(parent),'ControlGroup':group}
    monkeypatch.setattr(fixture,'verify_receipt',lambda *args:record)
    monkeypatch.setattr(fixture,'verify_root',lambda *args:context.worktree)
    monkeypatch.setattr(fixture,'verify_native',lambda *args:None)
    monkeypatch.setattr(fixture,'native_description',lambda *args:'fixed-owned-unit')
    monkeypatch.setattr(fixture,'unit_state',lambda *args,**kwargs:state)
    if changed=='purpose':native['purpose']='native'
    if changed=='status':native['status']='stopped'
    if changed=='fixture':record['status']='stopping'
    if changed=='child':native['setup_child']['start_ticks']+=1
    if changed=='parent':native['main_pid_start_ticks']+=1
    if changed=='executable':native['setup_executable_sha256']='f'*64
    if changed=='helper':native['setup_helper_sha256']='f'*64
    if changed=='unit':state['Description']='unrelated'
    assert setup.child_owner_current(binding,time.monotonic()+3) is (changed=='none')


def test_gated_child_refuses_owner_change_after_parent_release(context,monkeypatch):
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    read_fd,write_fd=os.pipe()
    os.write(write_fd,json.dumps({'binding':binding,'deadline':time.monotonic()+3,'argv':['unused'],'env':{}}).encode());os.close(write_fd)
    calls=[]
    monkeypatch.setattr(setup,'child_owner_current',lambda *args:False)
    monkeypatch.setattr(setup.os,'execvpe',lambda *args:calls.append(args))
    with pytest.raises(RuntimeContractError):setup.gated_child(read_fd)
    assert calls==[]


@pytest.mark.parametrize('changed',['none','owner','role','generation','shell','worktree'])
def test_setup_finalization_cannot_close_replaced_or_ordinary_chat(preparer,changed):
    owner,run,_calls,context=preparer
    value=setup.prepare_claude_setup_context(owner,context.generation_id,context.executable.path,context.executable.sha256,time.monotonic()+3)
    with sqlite3.connect(run.DB_PATH) as con:
        if changed=='owner':con.execute('UPDATE conversations SET owner_user_id=2')
        if changed in {'role','generation'}:
            projection={'role':'ordinary' if changed=='role' else 'setup','generation_id':'f'*32 if changed=='generation' else context.generation_id}
            con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(projection),))
        if changed=='shell':con.execute("UPDATE shells SET flavor='dev'")
        if changed=='worktree':con.execute("UPDATE conversations SET worktree='/foreign'")
    if changed=='none':setup.close_setup_conversation(run.DB_PATH,value.conversation_id,context.worktree,context.generation_id)
    else:
        with pytest.raises(RuntimeContractError):setup.close_setup_conversation(run.DB_PATH,value.conversation_id,context.worktree,context.generation_id)
    with sqlite3.connect(run.DB_PATH) as con:
        assert con.execute('SELECT state FROM conversations').fetchone()[0]==('closed' if changed=='none' else 'idle')


def test_exact_archive_includes_committed_setup_not_dirty_overlay(tmp_path):
    import io
    import tarfile
    repo=tmp_path/'repo';repo.mkdir();(repo/'.super-coder').mkdir();(repo/'maintainer').mkdir()
    (repo/'.super-coder/source').write_text('engine');(repo/'sc').write_text('entry')
    helper=repo/'maintainer/claude_setup.py';helper.write_text('committed fixed setup')
    env={'PATH':os.defpath,'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':'fixture@example.invalid',
         'GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}
    for argv in (['init','-q'],['add','.'],['commit','-qm','synthetic source']):
        subprocess.run(['/usr/bin/git','-C',str(repo),*argv],check=True,env=env)
    helper.write_text('dirty overlay must not appear')
    _,raw=fixture.archive(repo,'HEAD')
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        assert tar.extractfile('maintainer/claude_setup.py').read()==b'committed fixed setup'
        assert 'maintainer/gui_experiment.py' not in tar.getnames()


@pytest.mark.parametrize('name',['settings.local.json','settings.json'])
def test_new_discovered_project_settings_after_binding_refuse_setup(context,name):
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    path=context.worktree/'.claude'/name
    assert binding['discovered_settings'][str(path)] is None
    path.parent.mkdir(exist_ok=True);path.write_text('{"permissions":{"deny":[]}}')
    with pytest.raises(RuntimeContractError,match='discovered project settings changed'):
        setup.revalidate(binding,time.monotonic()+3)


def test_blocking_owner_observation_cannot_fork_after_deadline(context,monkeypatch):
    outer,terminal=pty.openpty();calls=[];observations=[]
    deadline=time.monotonic()+.04
    monkeypatch.setattr(setup,'fixed_launch',lambda *args:([],{},{}))
    monkeypatch.setattr(setup,'revalidate',lambda *args:None)
    monkeypatch.setattr(setup.subprocess,'Popen',lambda *args,**kwargs:calls.append(args))
    def owned():
        observations.append(True)
        if len(observations)==2:time.sleep(max(0,deadline-time.monotonic())+.01)
        return True
    try:
        with pytest.raises(RuntimeContractError,match='setup budget unavailable'):
            setup.run_setup(context,context.worktree,verify_owned=owned,record_child=lambda *args:True,
                deadline=deadline,input_fd=terminal,output_fd=terminal)
        assert len(observations)==2 and calls==[]
    finally:os.close(outer);os.close(terminal)


@pytest.fixture
def transcript(context,tmp_path):
    home=tmp_path/'native-home';project=home/'.claude/projects/exact-main';project.mkdir(parents=True)
    context=dataclasses.replace(context,env=dict(context.env)|{'HOME':str(home)})
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    path=project/(binding['session_id']+'.jsonl')
    row={'type':'system','subtype':'init','sessionId':binding['session_id'],'cwd':str(context.worktree)}
    path.write_text(json.dumps(row)+'\n')
    setup.write_private(context.state_root/'claude-setup-transcript.json',setup.transcript_pointer(binding,path))
    return context,binding,path,row


def test_exact_transcript_zero_is_observed_records_not_missing_file(transcript):
    context,_binding,path,_row=transcript
    value=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert value['state']=='observed' and value['zero_turn_records'] is True
    assert value['record_count']==1 and value['user_records']==value['assistant_records']==0
    assert value['effective_telemetry'] is False
    assert not {'path','session_id','generation_id','cwd','message'}&value.keys()
    assert json.loads((context.state_root/'claude-setup-transcript.json').read_text())['path']==str(path)
    assert (context.state_root/'claude-setup-transcript.json').stat().st_mode&0o777==0o600


def test_actual_user_assistant_records_prevent_zero(transcript):
    context,_binding,path,row=transcript
    with path.open('a') as out:
        for kind in ('user','assistant'):
            out.write(json.dumps(row|{'type':kind,'message':{'content':'private never exported'}})+'\n')
    value=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert value['state']=='observed' and value['zero_turn_records'] is False
    assert value['user_records']==value['assistant_records']==1
    assert 'private' not in json.dumps(value)


@pytest.mark.parametrize('change',['missing','empty','tail','unknown','foreign_session','foreign_cwd','sidechain','oversize','replace','truncate','expired','orphan_terminal'])
def test_unknown_changed_incomplete_transcript_never_means_zero(transcript,change):
    context,_binding,path,row=transcript
    deadline=time.monotonic()+2
    if change=='missing':path.unlink()
    elif change=='empty':path.write_text('')
    elif change=='tail':path.write_text(json.dumps(row))
    elif change=='unknown':path.write_text(json.dumps(row|{'type':'unknown-native-record'})+'\n')
    elif change=='foreign_session':path.write_text(json.dumps(row|{'sessionId':'other'})+'\n')
    elif change=='foreign_cwd':path.write_text(json.dumps(row|{'cwd':'/other'})+'\n')
    elif change=='sidechain':path.write_text(json.dumps(row|{'isSidechain':True})+'\n')
    elif change=='oversize':path.write_bytes(b'x'*(setup.MAX_TRANSCRIPT+1))
    elif change=='replace':
        previous=path.with_suffix('.previous');path.rename(previous);path.write_text(previous.read_text())
    elif change=='truncate':path.write_text('{}\n')
    elif change=='expired':deadline=time.monotonic()-1
    elif change=='orphan_terminal':path.write_text(json.dumps(row|{'subtype':'turn_duration'})+'\n')
    value=setup.transcript_turn_evidence(context.state_root,deadline=deadline)
    assert value['state']=='inconclusive' and 'zero_turn_records' not in value


@pytest.mark.parametrize('change',['uuid','namespace','subagents','symlink_file','symlink_parent'])
def test_hook_pointer_rejects_other_namespace_or_alias(transcript,change,tmp_path):
    _context,binding,path,_row=transcript
    if change=='uuid':path=path.with_name('other.jsonl')
    elif change=='namespace':path=tmp_path/path.name
    elif change=='subagents':path=path.parent/'subagents'/path.name
    elif change=='symlink_file':
        original=path.with_suffix('.private');path.rename(original);path.symlink_to(original)
    else:
        parent=path.parent;original=parent.with_name('original');parent.rename(original);parent.symlink_to(original,target_is_directory=True)
    with pytest.raises(RuntimeContractError):setup.transcript_pointer(binding,path)


def test_pointer_before_native_creates_file_still_needs_attributable_records(transcript):
    context,binding,path,row=transcript
    (context.state_root/'claude-setup-transcript.json').unlink();path.unlink()
    setup.write_private(context.state_root/'claude-setup-transcript.json',setup.transcript_pointer(binding,path))
    assert setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)['state']=='inconclusive'
    path.write_text(json.dumps(row)+'\n')
    assert setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)['zero_turn_records'] is True


def test_qualified_never_created_transcript_is_named_not_zero(transcript):
    context,binding,path,_row=transcript
    pointer=context.state_root/'claude-setup-transcript.json'
    pointer.unlink();path.unlink()
    setup.write_private(pointer,setup.transcript_pointer(binding,path))
    result=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert result=={'state':'inconclusive','evidence':'exact_session_transcript_turn_records',
                    'reason':'qualified_transcript_missing'}
    assert not {'zero_turn_records','user_records','assistant_records','record_count',
                'path','cwd','session_id','native_inference_verified'} & result.keys()


@pytest.mark.parametrize('change',['removed','no_pointer','empty','malformed','foreign','parent_replaced',
                                   'source_changed','settings_added','expired'])
def test_unavailable_transcripts_never_become_qualified_missing(transcript,change):
    context,binding,path,_row=transcript
    pointer=context.state_root/'claude-setup-transcript.json'
    deadline=time.monotonic()+2
    if change=='removed':path.unlink()
    elif change=='no_pointer':pointer.unlink()
    elif change=='empty':path.write_text('')
    elif change=='malformed':path.write_text('not-json\n')
    else:
        pointer.unlink();path.unlink()
        setup.write_private(pointer,setup.transcript_pointer(binding,path))
        if change=='foreign':
            value=json.loads(pointer.read_text());value['path']=str(path.with_name('foreign.jsonl'))
            pointer.write_text(json.dumps(value))
        elif change=='parent_replaced':
            path.parent.rename(path.parent.with_name('previous-parent'));path.parent.mkdir()
        elif change=='source_changed':context.executable.path.write_text('changed source')
        elif change=='settings_added':
            settings=context.worktree/'.claude/settings.local.json';settings.parent.mkdir(exist_ok=True)
            settings.write_text('{}')
        elif change=='expired':deadline=time.monotonic()-1
    result=setup.transcript_turn_evidence(context.state_root,deadline=deadline)
    assert result=={'state':'inconclusive','evidence':'exact_session_transcript_turn_records'}


def test_file_created_during_missing_source_revalidation_stays_unavailable(transcript,monkeypatch):
    context,binding,path,row=transcript
    pointer=context.state_root/'claude-setup-transcript.json'
    pointer.unlink();path.unlink()
    setup.write_private(pointer,setup.transcript_pointer(binding,path))
    original=setup.revalidate
    def changed(binding,deadline):
        original(binding,deadline)
        path.write_text(json.dumps(row|{'type':'user','message':{'content':'private'}})+'\n')
    monkeypatch.setattr(setup,'revalidate',changed)
    result=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert result=={'state':'inconclusive','evidence':'exact_session_transcript_turn_records'}


@pytest.mark.parametrize('minimum_size',[1,-1,False,'0',None])
def test_missing_transcript_requires_exact_never_present_size(transcript,minimum_size):
    context,binding,path,_row=transcript
    pointer=context.state_root/'claude-setup-transcript.json'
    pointer.unlink();path.unlink()
    value=setup.transcript_pointer(binding,path);value['minimum_size']=minimum_size
    setup.write_private(pointer,value)
    result=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert result=={'state':'inconclusive','evidence':'exact_session_transcript_turn_records'}


def test_missing_transcript_final_observation_deadline_fences_reason(transcript,monkeypatch):
    context,binding,path,_row=transcript
    pointer=context.state_root/'claude-setup-transcript.json'
    pointer.unlink();path.unlink()
    setup.write_private(pointer,setup.transcript_pointer(binding,path))
    deadline=time.monotonic()+.03
    original=setup.transcript_pointer;calls=[]
    def late(*args):
        value=original(*args);calls.append(True)
        if len(calls)==2:time.sleep(max(0,deadline-time.monotonic())+.01)
        return value
    monkeypatch.setattr(setup,'transcript_pointer',late)
    result=setup.transcript_turn_evidence(context.state_root,deadline=deadline)
    assert result=={'state':'inconclusive','evidence':'exact_session_transcript_turn_records'}


def test_owned_synthetic_hook_captures_pointer_privately_only(context,tmp_path,monkeypatch):
    # Actual pytest OS identity, synthetic hook fields; no native account/CLI.
    binary=Path(sys.executable).resolve();home=tmp_path/'native-home'
    project=home/'.claude/projects/captured-main';project.mkdir(parents=True)
    context=dataclasses.replace(context,executable=ExecutableBinding(binary,setup.digest(binary),'synthetic-python'),
        env=dict(context.env)|{'HOME':str(home)})
    monkeypatch.setattr(setup,'_memory_disable_source',lambda *args,**kwargs:'d'*64)
    _,_,binding=setup.fixed_launch(context,context.worktree,time.monotonic()+3)
    setup.write_private(context.state_root/'claude-setup-child.json',{'pid':os.getpid(),
        'start_ticks':setup.process(os.getpid())[0],'cgroup':setup.process(os.getpid())[1]})
    monkeypatch.setenv('SC_F89_SETUP_GENERATION',context.generation_id)
    monkeypatch.setenv('CLAUDE_CODE_DISABLE_AUTO_MEMORY','1')
    path=project/(binding['session_id']+'.jsonl')
    event={'hook_event_name':'SessionStart','source':'startup','session_id':binding['session_id'],
        'cwd':str(context.worktree),'transcript_path':str(path),'private':'discard'}
    value=setup.observe_hook(context.state_root,json.dumps(event).encode())
    assert value['state']=='owned_startup_observed'
    assert 'transcript_path' not in value and 'private' not in json.dumps(value)
    assert json.loads((context.state_root/'claude-setup-transcript.json').read_text())['path']==str(path)
    # Missing exact file remains inconclusive, despite actual owned hook.
    assert setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)['state']=='inconclusive'


@pytest.mark.parametrize('field,value',[('isSidechain','true'),('isSidechain',1),('isSidechain',None),('sessionId',None),('cwd',None)])
def test_malformed_consumed_transcript_attribution_stays_inconclusive(transcript,field,value):
    context,_binding,path,row=transcript
    with path.open('a') as out:out.write(json.dumps(row|{field:value})+'\n')
    result=setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)
    assert result['state']=='inconclusive' and 'zero_turn_records' not in result


def test_explicit_root_false_transcript_attribution_is_observed(transcript):
    context,_binding,path,row=transcript
    path.write_text(json.dumps(row|{'isSidechain':False})+'\n')
    assert setup.transcript_turn_evidence(context.state_root,deadline=time.monotonic()+2)['zero_turn_records'] is True
