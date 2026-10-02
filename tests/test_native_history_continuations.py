"""Association/admission source fixtures. Fake grades are not native proof."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.super-coder/scripts'),str(ROOT/'.super-coder/api')]
import conversation_native_chats
import conversation_native_history as history_owner
import conversation_routes as routes
import route_bindings
from conversation_adapters.codex_runtime import DRIVER_REVISION
from conversation_native_chats import NativeChatsService
from conversation_runtime import RuntimeStore
from conversation_runtime_checks import Fingerprint
from conversation_runtime_contract import (
    ExecutableBinding,
    RuntimeContext,
    RuntimeContractError,
    WorkspaceIdentity,
)
from gui_experiment_probe_owner import NativeProbeOwner

CID='cv_'+'1'*32
CLEAN={'outcome':'complete','native_outcome':'complete','unit_verified_exited':True,
       'unresolved_work':[],'unresolved_definitions':[]}


@pytest.fixture
def seat(tmp_path,monkeypatch):
    path=tmp_path/'.super-coder/shell_db.db';path.parent.mkdir()
    con=sqlite3.connect(path);con.row_factory=sqlite3.Row
    con.executescript((ROOT/'.super-coder/schema.sql').read_text())
    for file in sorted((ROOT/'.super-coder/migrations').glob('*.sql')):con.executescript(file.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'synthetic'),(2,'foreign')")
    con.execute("INSERT INTO shells(shell_id,shortname,display_name,flavor,system_prompt,user_id,api_key) VALUES(1,'FX','Synthetic','dev','synthetic',1,'synthetic-token')")
    worktree=tmp_path/'.sc-worktrees/fx';worktree.mkdir(parents=True)
    fp=Fingerprint('codex',ExecutableBinding(Path('/synthetic/native'),'b'*64,'synthetic-version'),
                   DRIVER_REVISION,'c'*64,'openai','selected-model','high','c'*64,'d'*64)
    binding=NativeProbeOwner.candidate_binding(fp)
    binding['selector_binding'].update(proof_state='checked_native_selection',native_fingerprint=fp.key)
    proof={'binding':binding,'binding_digest':route_bindings.digest_json(binding),'fingerprint':fp.key,
           'grade':'compatible','coverage':sorted(history_owner.HISTORY_COVERAGE)}
    projection={'role':'ordinary','generation_id':'old-g','state':'closed','root_id':'owned-old-root','cleanup':CLEAN}
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,route_contract_version,route_binding,worktree,state,closed_at,creation_idempotency_key,creation_request_hash,runtime_mode,runtime_projection,conversation_scope) VALUES(?,1,1,'codex','openai','selected-model','high',2,?,?,'closed',datetime('now'),'old-key','old-hash','native_experiment',?,'sprint')",
        (CID,route_bindings.canonical_json(binding),str(worktree),json.dumps(projection)))
    context={'generation_id':'old-g','conversation_id':CID,'shell_id':1,'owner_user_id':1,
             'harness':'codex','provider':'openai','model':'selected-model','effort':'high','worktree':str(worktree),
             'boot_digest':'e'*64,'policy_digest':'c'*64}
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,cleanup_json,created_at,updated_at) VALUES('old-g',?,1,1,'codex',?,'closed',?,1,1)",
        (CID,json.dumps({'context':context}),json.dumps(CLEAN)))
    con.commit()
    calls=[]
    def checked(history):calls.append(history);return proof.copy()
    service=NativeChatsService(path,tmp_path,SimpleNamespace(),history_admission=checked)
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',service)
    monkeypatch.setattr(routes,'DB_PATH',path)
    monkeypatch.setattr(routes.run_mod,'REPO_ROOT',tmp_path)
    monkeypatch.setattr(routes,'_live_shell_session',lambda row:None)
    yield SimpleNamespace(con=con,path=path,root=tmp_path,worktree=worktree,service=service,proof=proof,calls=calls,fp=fp)
    service.starts.shutdown(wait=True);con.close()


def request(method='POST',key='history-key',body=None,path=None,extra=''):
    response=routes.handle(method,path or f'/api/conversations/{CID}/history-resume',
        'Host: localhost:8800\r\nIdempotency-Key: '+key+extra,json.dumps(body if body is not None else {'version':1}).encode())
    return response[0],json.loads(response[2])


def assert_no_destination(seat):
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_native_history').fetchone()[0]==0
    assert seat.con.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]==1
    assert seat.con.execute('SELECT COUNT(*) FROM active_shell_chats').fetchone()[0]==0
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_boot_snapshots').fetchone()[0]==0


def test_closed_sprint_history_links_new_identity_without_reopening_or_copying_old_records(seat):
    before=dict(seat.con.execute('SELECT * FROM conversations').fetchone())
    status,result=request()
    assert status==201,result
    new=result['conversation']
    assert new['conversation_id']!=CID and new['scope']=='normal' and not new['sprint_managed']
    assert new['runtime']['generation_id']!='old-g' and new['runtime']['capabilities']=={}
    assert new['runtime']['history']=={'source_conversation_id':CID,'source_generation_id':'old-g'}
    assert dict(seat.con.execute('SELECT * FROM conversations WHERE conversation_id=?',(CID,)).fetchone())==before
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0]==0
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_boot_snapshots').fetchone()[0]==0


def test_stable_key_and_readback_reconcile_after_installed_service_disappears(seat,monkeypatch):
    status,first=request();assert status==201
    monkeypatch.setattr(conversation_native_chats,'_SERVICE',None)
    status,replayed=request();assert status==200 and replayed==first
    status,readback=request('GET',path=f'/api/conversations/{CID}/history-resume?request_key=history-key')
    assert status==200 and readback==first
    assert len(seat.calls)==2
    assert request(body={'version':1,'title':'different'})[0]==409
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_native_history').fetchone()[0]==1


@pytest.mark.parametrize('gap',['service','coverage','goals_tools','grade','fingerprint','route','native_cleanup','os_cleanup','definitions','work','old_generation','unknown_submit','accepted_submit','foreign_generation','captured_context','provisional','source_role'])
def test_incomplete_or_conflicting_history_refuses_before_allocation(seat,monkeypatch,gap):
    if gap=='service':seat.service.history_resolver=None
    elif gap in {'coverage','goals_tools'}:seat.proof['coverage'].remove('pre_resume_goal_tool_policy' if gap=='goals_tools' else 'owned_history_baseline')
    elif gap=='grade':seat.proof['grade']='inconclusive'
    elif gap=='fingerprint':seat.proof['fingerprint']='f'*64
    elif gap=='route':
        seat.proof['binding']=dict(seat.proof['binding'])|{'requested_model':'different'}
        seat.proof['binding_digest']=route_bindings.digest_json(seat.proof['binding'])
    elif gap in {'native_cleanup','os_cleanup','definitions','work'}:
        cleanup=CLEAN|({'native_outcome':'unknown'} if gap=='native_cleanup' else {'unit_verified_exited':False} if gap=='os_cleanup' else {'unresolved_definitions':['owned-definition']} if gap=='definitions' else {'unresolved_work':['child']})
        seat.con.execute('UPDATE conversation_runtime_generations SET cleanup_json=?',(json.dumps(cleanup),))
    elif gap=='old_generation':
        seat.con.execute("INSERT INTO conversation_runtime_generations VALUES('earlier',?,1,1,'codex','{}','lost','{}',0,1,NULL,0,0,0,0,0)",(CID,))
    elif gap in {'unknown_submit','accepted_submit'}:
        seat.con.execute("INSERT INTO conversation_runtime_commands VALUES('old-g','uncertain',1,'submit','digest','{}',?,'{}')",('unknown' if gap=='unknown_submit' else 'accepted',))
    elif gap=='foreign_generation':seat.con.execute('UPDATE conversation_runtime_generations SET owner_user_id=2')
    elif gap=='captured_context':seat.con.execute("UPDATE conversation_runtime_generations SET binding_json='{}'")
    else:
        runtime=json.loads(seat.con.execute('SELECT runtime_projection FROM conversations').fetchone()[0])
        runtime.update({'preparation_owner':{'pid':1}} if gap=='provisional' else {'role':'probe'})
        seat.con.execute('UPDATE conversations SET runtime_projection=?',(json.dumps(runtime),))
    seat.con.commit()
    status,result=request()
    assert status in {404,409,422},result
    assert_no_destination(seat)


@pytest.mark.parametrize('occupied',['registry','other_chat','other_native','other_preparer','other_unknown_preparation','cli'])
def test_reassigned_or_pending_workspace_is_not_displaced(seat,monkeypatch,occupied):
    if occupied=='cli':monkeypatch.setattr(routes,'_live_shell_session',lambda row:{'pid':123})
    else:
        runtime=({'generation_id':'unlaunched','preparation_owner':{'pid':123}} if occupied=='other_preparer'
                 else {'generation_id':'unlaunched','preparation_cleanup':None} if occupied=='other_unknown_preparation' else {})
        seat.con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,worktree,creation_idempotency_key,creation_request_hash,state,closed_at,runtime_projection) VALUES('other',1,1,'codex',?,'other','other',?,?,?)",
            (str(seat.worktree),'idle' if occupied in {'other_chat','registry'} else 'closed',None if occupied in {'other_chat','registry'} else '2026-01-01',json.dumps(runtime)))
        if occupied=='registry':seat.con.execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'other')")
        if occupied=='other_native':seat.con.execute("INSERT INTO conversation_runtime_generations VALUES('other-g','other',1,1,'codex','{}','lost','{}',0,1,NULL,0,0,0,1,1)")
    seat.con.commit()
    status,_=request();assert status==409
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_native_history').fetchone()[0]==0
    assert seat.calls==[] or occupied=='cli'


def test_absent_same_path_workspace_is_not_recreated_by_api_and_different_destination_refuses(seat,monkeypatch):
    seat.worktree.rmdir()
    assert request()[0]==201 and not seat.worktree.exists()
    # Canonical boot, not POST, owns any later recreation. The old source path
    # never gets rewritten to pretend an unrelated destination is the same.
    monkeypatch.setattr(routes.run_mod,'shell_work_dir',lambda *a,**k:seat.root/'different')
    assert request(key='different-destination')[0]==409


@pytest.mark.parametrize('race',['owner_withdraw','same_db_replacement','proof','source_cleanup','slot','canonical'])
def test_final_transaction_rechecks_owner_source_proof_and_slot(seat,monkeypatch,race):
    original=seat.service.history_resolver
    source_reader=history_owner.source_history
    current=[]
    def capture(con,*args):
        current[:]=[con]
        return source_reader(con,*args)
    monkeypatch.setattr(history_owner,'source_history',capture)
    count=0
    def resolve(history):
        nonlocal count
        count+=1
        proof=original(history)
        if count==2:
            if race=='owner_withdraw':conversation_native_chats._SERVICE=None
            elif race=='same_db_replacement':conversation_native_chats._SERVICE=NativeChatsService(seat.path,seat.root,SimpleNamespace())
            elif race=='proof':proof=proof|{'fingerprint':'f'*64}
            elif race=='source_cleanup':current[0].execute("UPDATE conversation_runtime_generations SET cleanup_json='{}'")
            elif race=='slot':
                current[0].execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,worktree,creation_idempotency_key,creation_request_hash) VALUES('other',1,1,'codex',?,'other','other')",(str(seat.worktree),))
                current[0].execute("INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(1,'other')")
            elif race=='canonical':monkeypatch.setattr(routes.run_mod,'shell_work_dir',lambda *a,**k:seat.root/'changed')
        return proof
    seat.service.history_resolver=resolve
    status,result=request();assert status==409,result
    assert_no_destination(seat)


@pytest.mark.parametrize('extra,body',[
    ('\r\nOrigin: https://foreign.test',{'version':1}),
    ('\r\nAuthorization: Bearer synthetic-token',{'version':1}),
    ('',{'version':1,'native_root_id':'client-choice'}),
    ('',{'version':1,'source_generation_id':'client-choice'}),
    ('',{'version':1,'worktree':'/client-choice'}),
])
def test_operator_origin_and_finite_typed_body(seat,extra,body):
    assert request(body=body,extra=extra)[0] in {403,422}
    assert_no_destination(seat)


def test_association_migration_reapplication_is_idempotent_and_identity_is_immutable(seat):
    assert request()[0]==201
    seat.con.executescript((ROOT/'.super-coder/migrations/0278_native_history_continuations.sql').read_text())
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_native_history').fetchone()[0]==1
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        seat.con.execute("UPDATE conversation_native_history SET source_generation_id='replacement'")


def test_dirty_synthetic_migration_preserves_rows_and_stamps_once(tmp_path):
    import migrate
    con=sqlite3.connect(tmp_path/'dirty.db')
    con.executescript((ROOT/'.super-coder/schema.sql').read_text())
    files=sorted((ROOT/'.super-coder/migrations').glob('*.sql'))
    migrate.applied_set(con)
    for file in files:
        if file.name!='0278_native_history_continuations.sql':migrate.apply(con,file)
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'retained-synthetic-user')")
    con.commit()
    path=ROOT/'.super-coder/migrations/0278_native_history_continuations.sql'
    for _ in range(2):
        if path.name not in migrate.applied_set(con):migrate.apply(con,path)
    assert con.execute('SELECT username FROM users WHERE user_id=1').fetchone()[0]=='retained-synthetic-user'
    assert con.execute('SELECT COUNT(*) FROM schema_migrations WHERE filename=?',(path.name,)).fetchone()[0]==1
    assert con.execute('SELECT COUNT(*) FROM conversation_native_history').fetchone()[0]==0
    con.close()


def test_fresh_git_capture_is_current_checkout_not_historical_branch_restoration(tmp_path):
    def git(*args):return subprocess.run(['git',*args],capture_output=True,text=True,check=True).stdout.strip()
    repo=tmp_path/'repo';git('init',str(repo));git('-C',str(repo),'config','user.name','Fixture');git('-C',str(repo),'config','user.email','fixture@example.test')
    (repo/'current').write_text('fresh content');git('-C',str(repo),'add','current');git('-C',str(repo),'commit','-m','fixture')
    worktree=repo/'.sc-worktrees/fx';git('-C',str(repo),'worktree','add','-b','fresh-current-branch',str(worktree))
    observed=history_owner.observe_workspace(worktree,repo)
    assert observed.branch=='fresh-current-branch' and observed.head==git('-C',str(worktree),'rev-parse','HEAD')
    assert observed.cwd==worktree and observed.git_common_dir==repo/'.git'
    git('-C',str(repo),'worktree','remove',str(worktree))
    git('-C',str(repo),'worktree','add',str(worktree),'fresh-current-branch')
    assert history_owner.observe_workspace(worktree,repo)==observed
    assert not (worktree/'shared/sprints').exists()


def test_dataclass_history_is_not_positive_owner_issuance_at_reservation(seat):
    _,history,_=history_owner.source_history(seat.con,CID,1)
    context=RuntimeContext('invented-g','invented-cv',1,1,'codex',seat.root/'state',seat.worktree,
        seat.fp.executable,seat.fp.driver_revision,'f'*64,seat.fp.policy_digest,'unrestricted',
        provider='openai',model='selected-model',effort='high',history=history,
        workspace=WorkspaceIdentity(seat.worktree,seat.root/'.git','current','a'*40),capability_evidence={'history_resume':'compatible'})
    with pytest.raises(RuntimeContractError,match='canonical history'):
        RuntimeStore(seat.path).reserve(context,{'fingerprint':seat.fp.key})
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1


def test_immediate_close_releases_issued_but_never_allocated_continuation(seat):
    _,result=request()
    destination=result['conversation'];cid=destination['conversation_id']
    seat.service.request_close(seat.con,cid,1,destination['version'])
    row=seat.con.execute('SELECT state,runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
    assert row['state']=='closed' and json.loads(row['runtime_projection'])['state']=='closed'
    assert seat.con.execute('SELECT COUNT(*) FROM active_shell_chats').fetchone()[0]==0
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1


def test_close_after_start_candidate_read_fences_preparation_dispatch(seat):
    _,result=request()
    destination=result['conversation'];cid=destination['conversation_id']
    prepared=[]
    seat.service.prepare_context=lambda *args:prepared.append(args)
    def owner():
        seat.service.request_close(seat.con,cid,1,destination['version'])
        return {'pid':123,'start_ticks':456,'unit':'synthetic-api','control_group':'synthetic-group'}
    seat.service.supervisor=SimpleNamespace(preparation_identity=owner)
    seat.service.schedule_starts()
    assert prepared==[] and seat.service.starting==set()
    row=seat.con.execute('SELECT state,runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
    assert row['state']=='closed' and json.loads(row['runtime_projection'])['state']=='closed'
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1


def canonical_seat(seat,monkeypatch):
    """Real canonical Git rule, test-only boot transport, no native executable."""
    import gui_experiment_native_seat as native_seat
    import run
    from conversation_runtime_checks import EvidenceCache
    def git(*args):subprocess.run(['git',*args],capture_output=True,check=True)
    git('init',str(seat.root));git('-C',str(seat.root),'config','user.name','Fixture');git('-C',str(seat.root),'config','user.email','fixture@example.test')
    (seat.root/'current-source').write_text('current source, not old Sprint artifact')
    git('-C',str(seat.root),'add','current-source');git('-C',str(seat.root),'commit','-m','fixture')
    seat.worktree.rmdir()
    run.ensure_worktree(seat.worktree,'FX')
    events=[]
    class Supervisor:
        def codegen_clean(self):return True
        def register(self,generation,harness):
            events.append('register')
            state=seat.root/'runtime'/generation;state.mkdir(parents=True)
            return {'root':str(state),'endpoint':str(state/'controller.sock'),'unit':'synthetic-unit'}
    plan=SimpleNamespace(argv=[],model='selected-model',effort='high',boot_content='new canonical boot, never old bytes',cwd=str(seat.worktree),
                         env={'SC_API_BASE':'http://127.0.0.1:43210','PATH':'masked'},execution_view=SimpleNamespace(prefix=('canonical-view',)))
    monkeypatch.setattr(run,'ENGINE',seat.root/'.super-coder');monkeypatch.setattr(run,'DB_PATH',str(seat.path))
    def prepare(**kwargs):
        events.append('canonical-start')
        assert kwargs['boot'].conversation_id!=CID
        run.ensure_worktree(seat.worktree,'FX')
        return plan
    monkeypatch.setattr(run,'prepare_launch',prepare)
    monkeypatch.setattr(native_seat,'prepared_plan',lambda **kwargs:plan)
    monkeypatch.setattr(run,'load_adapter',lambda harness:{'launch_flags':['--sandbox','danger-full-access','--ask-for-approval','never']})
    monkeypatch.setattr(run,'managed_mcp_injection',lambda *args:{'name':'browser','url':plan.env['SC_API_BASE']+'/mcp/FX','launch_args':['-c','mcp_servers.browser.url="http://127.0.0.1:43210/mcp/FX"']})
    cache=EvidenceCache()
    monkeypatch.setattr(cache,'admission',lambda fp:{'submission':'compatible'})
    value=native_seat.NativeFixtureSeat(database=seat.path,root=seat.root,supervisor=Supervisor(),
        native_bindings={'HOME':'/synthetic/native-home','CODEX':str(seat.fp.executable.path)},cache=cache)
    value.observers['codex']=SimpleNamespace(observe=lambda:SimpleNamespace(binding=seat.fp.executable))
    monkeypatch.setattr(value,'candidate_fingerprint',lambda *args:seat.fp)
    monkeypatch.setattr(value,'settings_digest',lambda harness:seat.fp.policy_digest)
    monkeypatch.setattr(value,'implementation_digest',lambda harness:seat.fp.implementation_digest)
    return value,events


@pytest.mark.parametrize('removed',[False,True])
def test_canonical_preparation_captures_fresh_same_path_git_and_positive_owner_issuance(seat,monkeypatch,removed):
    value,events=canonical_seat(seat,monkeypatch)
    if removed:subprocess.run(['git','-C',str(seat.root),'worktree','remove',str(seat.worktree)],check=True,capture_output=True)
    status,result=request();assert status==201
    cid=result['conversation']['conversation_id'];generation=result['conversation']['runtime']['generation_id']
    runtime=result['conversation']['runtime']|{'state':'preparing','preparation_owner':{'unit':'synthetic-api'},'preparation_cleanup':None}
    seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(runtime),cid));seat.con.commit()
    context,fp,native=value.prepare(cid,generation,checked_fingerprint=seat.fp,history_proof=seat.proof)
    assert events==['register','canonical-start'] and context.history.source_conversation_id==CID
    assert context.boot_content=='new canonical boot, never old bytes'
    assert context.workspace==history_owner.observe_workspace(seat.worktree,seat.root)
    assert context.workspace.branch=='shell/fx' and context.workspace.git_common_dir==seat.root/'.git'
    assert context.capability_evidence['history_resume']=='compatible'
    RuntimeStore(seat.path).reserve(context,native|{'fingerprint':fp.key})
    captured=json.loads(seat.con.execute('SELECT binding_json FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone()[0])['context']
    assert captured['history']['native_root_id']=='owned-old-root' and captured['workspace']['head']==context.workspace.head
    with pytest.raises(RuntimeContractError,match='reattach'):
        value.prepare(cid,generation,checked_fingerprint=seat.fp,history_proof=seat.proof)
    assert events==['register','canonical-start']


@pytest.mark.parametrize('edge',['before_observer','observer','after_register'])
@pytest.mark.parametrize('change',['closing','generation','role','owner','source_cleanup','proof'])
def test_history_seat_never_launches_or_returns_replaced_preparation(seat,monkeypatch,edge,change):
    value,events=canonical_seat(seat,monkeypatch)
    _,result=request();cid=result['conversation']['conversation_id'];generation=result['conversation']['runtime']['generation_id']
    runtime=result['conversation']['runtime']|{'state':'preparing','preparation_owner':{'unit':'synthetic-api'},'preparation_cleanup':None}
    seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(runtime),cid));seat.con.commit()
    def mutate():
        current=dict(runtime)
        if change=='closing':current['state']='closing'
        elif change=='generation':current['generation_id']='replacement'
        elif change=='role':current['role']='probe'
        elif change=='owner':conversation_native_chats._SERVICE=None
        elif change=='source_cleanup':seat.con.execute("UPDATE conversation_runtime_generations SET cleanup_json='{}' WHERE generation_id='old-g'")
        elif change=='proof':seat.service.history_resolver=lambda history:seat.proof|{'fingerprint':'f'*64}
        seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(current),cid));seat.con.commit()
    if edge=='before_observer':mutate()
    elif edge=='observer':value.observers['codex']=SimpleNamespace(observe=lambda:mutate() or SimpleNamespace(binding=seat.fp.executable))
    else:
        original=value.supervisor.register
        def register(*args):result=original(*args);mutate();return result
        monkeypatch.setattr(value.supervisor,'register',register)
    with pytest.raises(RuntimeContractError):value.prepare(cid,generation,checked_fingerprint=seat.fp,history_proof=seat.proof)
    if edge!='after_register':assert events==[]
    else:assert events==['register']
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1


@pytest.mark.parametrize('stage',['observer','registered','prepared'])
@pytest.mark.parametrize('change',['closing','shell_owner','generation','preparer','association'])
def test_blocking_history_proof_cannot_authorize_later_preparation_side_effects(seat,monkeypatch,stage,change):
    value,events=canonical_seat(seat,monkeypatch)
    _,result=request();cid=result['conversation']['conversation_id'];generation=result['conversation']['runtime']['generation_id']
    runtime=result['conversation']['runtime']|{'state':'preparing','preparation_owner':{'unit':'synthetic-api'},'preparation_cleanup':None}
    seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(runtime),cid));seat.con.commit()
    observed=[]
    original=value.observers['codex'].observe
    def observe():
        observed.append(True)
        return original()
    value.observers['codex'].observe=observe
    def proof(history):
        armed=(bool(observed) if stage=='observer' else 'register' in events if stage=='registered' else 'canonical-start' in events)
        if armed:
            current=runtime|({'state':'closing'} if change=='closing' else {'generation_id':'replacement'} if change=='generation' else {'preparation_owner':{'unit':'replacement-api'}} if change=='preparer' else {})
            seat.con.execute('UPDATE conversations SET runtime_projection=? WHERE conversation_id=?',(json.dumps(current),cid))
            if change=='shell_owner':seat.con.execute('UPDATE shells SET user_id=2 WHERE shell_id=1')
            if change=='association':seat.con.execute('DELETE FROM conversation_native_history WHERE conversation_id=?',(cid,))
            seat.con.commit()
        return seat.proof.copy()
    seat.service.history_resolver=proof
    with pytest.raises(RuntimeContractError):value.prepare(cid,generation,checked_fingerprint=seat.fp,history_proof=seat.proof)
    assert events==([] if stage=='observer' else ['register'] if stage=='registered' else ['register','canonical-start'])
    link=seat.con.execute('SELECT workspace_json FROM conversation_native_history WHERE conversation_id=?',(cid,)).fetchone()
    assert link is None if change=='association' else link['workspace_json']=='{}'
    assert seat.con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]==1
