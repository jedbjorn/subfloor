"""Synthetic exact-source Sprint bootstrap boundaries; zero vendor execution."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.super-coder/scripts'), str(ROOT / '.super-coder/api')]
import gui_experiment_sprint as sprint
import harness_versions
import route_bindings
import sprint_domain
import sprint_runtime
from conversation_adapters.codex import CodexAdapter

SPEC = importlib.util.spec_from_file_location('sprint_maintainer', ROOT / 'maintainer/gui_experiment.py')
assert SPEC and SPEC.loader
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
FID = 'a' * 32


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'shell_db.db'
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / '.super-coder/schema.sql').read_text())
    for file in sorted((ROOT / '.super-coder/migrations').glob('*.sql')):
        con.executescript(file.read_text())
    con.execute("INSERT INTO users(user_id,username) VALUES(1,'fixture'),(2,'other')")
    sprint.seed_roles(con, FID)
    con.commit()
    yield path, con
    con.close()


@pytest.fixture
def inert_alias(tmp_path):
    root = tmp_path / 'fixture'
    root.mkdir(mode=0o700)
    executable = tmp_path / 'inert-codex'
    executable.write_bytes(b'inert metadata fixture; never executed')
    executable.chmod(0o700)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    alias = sprint.MetadataAlias(root, executable, digest)
    yield alias, executable


@pytest.mark.parametrize('purpose,runtime', [('native-sprint','none'),('other','experimental')])
def test_invalid_purpose_refuses_before_any_fixture_creation(purpose, runtime, tmp_path):
    with mock.patch.object(fixture, 'start_serialized', side_effect=AssertionError('no preparation')), pytest.raises(fixture.FixtureError) as exc:
        fixture.start(tmp_path, 'HEAD', tmp_path/'receipt', purpose=purpose, runtime=runtime)
    assert exc.value.code == 'PURPOSE_INVALID'


def test_ordinary_identity_remains_unchanged():
    row = {key: key for key in fixture.IDENTITY_KEYS}
    assert fixture.identity(row) == row
    changed = row | {'purpose': 'native-sprint', 'sprint_executable_sha256': 'a'*64}
    assert fixture.identity(changed) == changed


def test_alias_child_path_and_real_canonical_version_functions(inert_alias, monkeypatch):
    alias, executable = inert_alias
    old_env = dict(os.environ)
    child_path = alias.prepare(deadline=time.monotonic()+1)
    assert child_path == str(alias.directory)+os.pathsep+os.defpath
    assert dict(os.environ) == old_env  # helper returns child scope, never mutates host env
    monkeypatch.setenv('PATH', child_path)
    monkeypatch.setenv('HOME', str(alias.root/'home'))
    for name in ('CODEX_HOME','OPENAI_API_KEY','SC_API_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    calls = []
    def metadata_only(argv, **kwargs):
        assert argv == ['codex', '--version']
        assert 'CODEX_HOME' not in os.environ and 'OPENAI_API_KEY' not in os.environ
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='codex-cli 0.159.1\n', stderr='')
    monkeypatch.setattr(subprocess, 'run', metadata_only)
    status = harness_versions.compatibility_status(('codex',))['codex']
    assert status['compatibility'] == 'newer-unverified' and status['error'] is None
    scope = harness_versions.runtime_scope()
    binding, _digest = route_bindings.resolve_v2(None,'codex',None,None,runtime_status=status,runtime_scope=scope)
    assert binding['control_state'] == 'harness-default'
    assert binding['requested_model'] is binding['effective_effort'] is None
    probe = CodexAdapter().probe()
    assert probe.compatibility == 'newer-unverified'
    assert calls == [['codex','--version'], ['codex','--version']]
    alias.verify(deadline=time.monotonic()+1)
    assert executable.read_bytes().startswith(b'inert')


def test_null_planner_candidate_uses_actual_canonical_arm_function(database, monkeypatch):
    _, con = database
    participant = con.execute("SELECT 4 AS participant_id,'codex' AS harness,NULL AS model,NULL AS effort,'ephemeral' AS runtime_mode").fetchone()
    monkeypatch.setattr(harness_versions,'_probe',lambda _: 'codex-cli 0.159.1')
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:pytest.fail('source-only; no native subprocess'))
    candidate = sprint_domain._participant_binding_candidate(con, participant)
    assert candidate.binding['requested_model'] is None
    assert candidate.harness_version == 'codex-cli 0.159.1'
    assert candidate.source_fingerprint is None
    # Exact route lacks genuine catalogue; no native cache or invented row substitutes.
    exact = con.execute("SELECT 4 AS participant_id,'codex' AS harness,'gpt-6.1-sol' AS model,'high' AS effort,'ephemeral' AS runtime_mode").fetchone()
    with pytest.raises(route_bindings.RouteResolutionError):
        sprint_domain._participant_binding_candidate(con, exact)


@pytest.mark.parametrize('mutation', ['binary','alias','directory','deadline'])
def test_alias_revalidation_refuses_changed_or_foreign_inputs(inert_alias, mutation, tmp_path):
    alias, executable = inert_alias
    alias.prepare(deadline=time.monotonic()+1)
    deadline = time.monotonic()+1
    if mutation == 'binary':
        executable.write_bytes(b'changed')
    elif mutation == 'alias':
        alias.alias.unlink(); alias.alias.symlink_to('/bin/false')
    elif mutation == 'directory':
        alias.directory.rename(tmp_path/'moved')
        alias.directory.symlink_to(tmp_path/'moved')
    else:
        deadline = time.monotonic()-1
    with pytest.raises(sprint.SprintFixtureError):
        alias.verify(deadline=deadline)


def test_fifo_executable_refuses_without_blocking(tmp_path):
    fifo = tmp_path/'fifo'; os.mkfifo(fifo,0o700)
    with pytest.raises(sprint.SprintFixtureError):
        sprint.executable_identity(fifo, deadline=time.monotonic()+.1)


def test_real_schema_roles_tokens_no_catalogue_or_native_grades(database):
    _, con = database
    sprint.validate_roles(con, FID)
    rows = con.execute('SELECT shell_id,flavor,user_id,api_key FROM shells ORDER BY shell_id').fetchall()
    assert [row['flavor'] for row in rows] == ['dev','reviewer','dev','planner']
    assert len({row['api_key'] for row in rows}) == 4
    assert con.execute('SELECT COUNT(*) FROM model_routes').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0] == 0
    with pytest.raises(sprint.SprintFixtureError):
        sprint.seed_roles(con,FID)
    con.execute("UPDATE shells SET user_id=2 WHERE shell_id=4")
    with pytest.raises(sprint.SprintFixtureError):
        sprint.validate_roles(con,FID)


class ThreadStub:
    def __init__(self, alive=False):
        self.alive, self.joins, self.stops = alive, [], 0
        self._cleanup_thread = None
    def stop(self):self.stops += 1
    def join(self, timeout):self.joins.append(timeout)
    def is_alive(self):return self.alive


@pytest.mark.parametrize('pending', ['main','cleanup'])
def test_unknown_worker_retains_close_and_shutdown_ownership(tmp_path, monkeypatch, pending):
    events = []
    seat = sprint.SprintSeat(tmp_path/'db', FID, record=events.append)
    seat.service = ThreadStub(pending == 'main')
    seat.service._cleanup_thread = ThreadStub(pending == 'cleanup')
    monkeypatch.setattr(sprint,'close_operator_chats',lambda *a,**kw:pytest.fail('worker not joined'))
    with pytest.raises(sprint.SprintFixtureError):
        seat.shutdown(deadline=time.monotonic()+.1)
    assert events[-1]['state'] == 'cleanup_inconclusive'
    assert seat.service.stops == 1


def test_real_non_daemon_worker_is_joined_before_canonical_close(tmp_path, monkeypatch):
    events, closed = [], []
    seat = sprint.SprintSeat(tmp_path/'db', FID, record=events.append)
    seat.service = ThreadStub()
    ready, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=lambda:(ready.set(),release.wait()),daemon=False)
    worker.start(); assert ready.wait(1)
    seat.service._cleanup_thread = worker
    timer = threading.Timer(.02,release.set);timer.start()
    def close(*a,**kw):
        assert not worker.is_alive();closed.append(True);return 1
    monkeypatch.setattr(sprint,'close_operator_chats',close)
    try:
        seat.shutdown(deadline=time.monotonic()+1)
    finally:
        release.set();worker.join(1);timer.join(1)
    assert closed == [True] and events[-1]['state'] == 'stopped'
    assert events[-1]['whole_unit_cleanup_verified'] is False


def test_service_start_uses_existing_runtime_no_delivery_override(tmp_path, monkeypatch):
    calls, events = [], []
    service = ThreadStub(True)
    service.wait_ready = lambda timeout: True
    monkeypatch.setattr(sprint_runtime,'service',lambda:None)
    def start(path, **kw):calls.append((path,kw));return service
    monkeypatch.setattr(sprint_runtime,'start_service',start)
    seat = sprint.SprintSeat(tmp_path/'db',FID,record=events.append)
    seat.start(deadline=time.monotonic()+1)
    assert calls == [(tmp_path/'db',{})] and events[-1]['model_pickup_proved'] is False
    assert events[0]['state'] == 'starting'


def test_service_deadline_never_starts_or_certifies(tmp_path, monkeypatch):
    seat = sprint.SprintSeat(tmp_path/'db',FID,record=lambda _:None)
    monkeypatch.setattr(sprint_runtime,'start_service',lambda *a,**kw:pytest.fail('expired'))
    with pytest.raises(sprint.SprintFixtureError):
        seat.start(deadline=time.monotonic()-1)


def planner_chat(con, model=None):
    con.execute("INSERT INTO roadmap(feature_id,title) VALUES(1,'Synthetic')")
    con.execute('INSERT INTO sprints(sprint_id,feature_id,originating_planner_shell_id) VALUES(1,1,4)')
    con.execute("INSERT INTO sprint_participants(participant_id,sprint_id,shell_id,role,harness) VALUES(1,1,4,'planner','codex')")
    cid = 'cv_'+'1'*32
    binding, _ = route_bindings.resolve_v2(None,'codex',None,None,
        runtime_status={'harness':'codex','runtime':'host','runtime_identity':'host:source-test','observed_version':'codex-cli 0.159.1','error':None},
        runtime_scope={'runtime':'host','runtime_identity':'host:source-test'})
    con.execute("INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash,conversation_scope,route_contract_version,route_binding) VALUES(?,4,1,'codex','openai',?,NULL,'/synthetic/planner','creation','hash','sprint',?,?)",(cid,model,2 if model is None else 1,route_bindings.canonical_json(binding) if model is None else None))
    con.execute('INSERT INTO sprint_participant_conversations(sprint_participant_id,conversation_id) VALUES(1,?)',(cid,))
    con.execute('INSERT INTO active_shell_chats(shell_id,chat_id) VALUES(4,?)',(cid,))
    con.commit()
    return cid


def test_actual_canonical_enqueue_then_close_retains_unprocessed_wake(database, monkeypatch):
    path, con = database
    cid = planner_chat(con)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**kw:pytest.fail('no model/native launch'))
    sprint_runtime.enqueue_conversation_turn(path,cid,'Literal typed Planner wake','wake-stable-key')
    assert con.execute('SELECT state FROM conversation_outbox').fetchone()[0] == 'pending'
    assert con.execute('SELECT COUNT(*) FROM conversation_runs').fetchone()[0] == 0
    assert sprint.close_operator_chats(path,FID,deadline=time.monotonic()+1) == 1
    assert con.execute('SELECT state FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0] == 'closed'
    assert con.execute('SELECT state FROM conversation_outbox').fetchone()[0] == 'cancelled'
    assert con.execute('SELECT sender_kind,body FROM conversation_messages').fetchone()[:] == ('engine','Literal typed Planner wake')
    assert con.execute('SELECT COUNT(*) FROM conversation_runs').fetchone()[0] == 0
    assert sprint.close_operator_chats(path,FID,deadline=time.monotonic()+1) == 0


@pytest.mark.parametrize('uncertainty', ['process','deadline','model','owner','completed_run'])
def test_planner_unknown_obligation_cannot_be_closed_or_called_pickup(database, uncertainty):
    path, con = database
    cid = planner_chat(con, 'other' if uncertainty == 'model' else None)
    deadline = time.monotonic()+1
    if uncertainty == 'process':
        con.execute('UPDATE active_shell_chats SET process_pid=123,process_start_ticks=456')
    elif uncertainty == 'model':
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            con.execute("UPDATE conversations SET model='changed' WHERE conversation_id=?",(cid,))
    elif uncertainty == 'owner':
        con.execute('UPDATE shells SET user_id=2 WHERE shell_id=4')
    elif uncertainty == 'completed_run':
        message = con.execute("INSERT INTO conversation_messages(conversation_id,sender_kind,sender_ref,message_kind,body,idempotency_key,request_hash) VALUES(?,'engine','fixture','prompt','wake','wake','hash')", (cid,)).lastrowid
        con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,lease_owner,lease_expires_at,state,started_at,ended_at) VALUES(?,4,?,'fixture','2026-10-03 13:00:00','succeeded','2026-10-03 12:00:00','2026-10-03 12:01:00')", (cid,message))
    else:
        deadline = time.monotonic()-1
    con.commit()
    with pytest.raises(sprint.SprintFixtureError):
        sprint.close_operator_chats(path,FID,deadline=deadline)
    assert con.execute('SELECT state FROM conversations').fetchone()[0] != 'closed'


def test_conditional_consumed_helper_fingerprint_missing_symlink_mutation(tmp_path):
    import json

    from conversation_runtime_contract import RuntimeContractError
    from gui_experiment_native_seat import NativeFixtureSeat
    root = tmp_path/('subfloor-gui-experiment-'+FID);root.mkdir()
    seat = NativeFixtureSeat.__new__(NativeFixtureSeat)
    seat.root = root;seat.source_lock=threading.RLock();seat.source_cache={};seat.loaded_implementations={}
    helper = root/'.super-coder/scripts/gui_experiment_sprint.py'
    ordinary = seat.implementation_files('codex')
    assert helper not in ordinary
    assets=root/'.super-coder/assets/runtime/claude';assets.mkdir(parents=True,exist_ok=True)
    for path in set(ordinary+seat.implementation_files('claude')):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('inert source')
    # Claude assets are source dependencies; establish directory before call.
    assets=root/'.super-coder/assets/runtime/claude';assets.mkdir(parents=True,exist_ok=True)
    marker=root/'.gui-experiment-owner.json'
    marker.write_text(json.dumps({'purpose':'native-sprint','fixture_id':FID,'runtime':'experimental'}));marker.chmod(0o600)
    assert helper in seat.implementation_files('codex') and helper in seat.implementation_files('claude')
    with pytest.raises(RuntimeContractError):seat.implementation_digest('codex')
    helper.write_text('source-v1')
    digest = seat.implementation_digest('codex')
    seat.loaded_implementations['codex'] = digest
    fp = SimpleNamespace(harness='codex',implementation_digest=digest)
    seat.require_loaded_source(fp)
    helper.write_text('source-v2')
    assert seat.implementation_digest('codex') != digest
    with pytest.raises(RuntimeContractError) as exc:seat.require_loaded_source(fp)
    assert exc.value.code=='LOADED_SOURCE_CHANGED'
    helper.unlink();helper.symlink_to(root/'fixture_bootstrap.py')
    with pytest.raises(RuntimeContractError):seat.implementation_digest('codex')
    marker.unlink();marker.symlink_to(root/'fixture_bootstrap.py')
    with pytest.raises(OSError):seat.implementation_files('codex')


@pytest.mark.parametrize('bad', [{'state':[]},{'state':'complete'}, {'state':'stopped','planner_chats_closed':True},
                               {'state':'ready','auth':'secret'}, {'state':'stopped','whole_unit_cleanup_verified':True},
                               {'state':'cleanup_inconclusive','code':['raw']}])
def test_fixed_worker_status_cannot_promote_or_export_unknown_fields(bad):
    with pytest.raises(sprint.SprintFixtureError):sprint.worker_status(bad)


@pytest.mark.parametrize('kind', ['missing','symlink','oversize','fifo','public','malformed'])
def test_private_worker_receipt_refuses_unavailable_or_mutated_inputs(tmp_path,kind):
    root=tmp_path/'root';root.mkdir(mode=0o700)
    path=root/'sprint-status.json'
    if kind=='symlink':path.symlink_to('/dev/null')
    elif kind=='fifo':os.mkfifo(path,0o600)
    elif kind!='missing':
        path.write_bytes(b'x'*4097 if kind=='oversize' else b'not json')
        path.chmod(0o644 if kind=='public' else 0o600)
    with pytest.raises((fixture.FixtureError,OSError)):
        fixture.read_sprint_status(root)


def test_actual_canonical_worktree_identity_for_all_seeded_roles(tmp_path,monkeypatch):
    import run
    repo=tmp_path/'git';repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
    git('init','-q','-b','main')
    (repo/'source').write_text('synthetic exact source')
    git('add','source');git('-c','user.name=Synthetic','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-qm','source')
    source=git('rev-parse','HEAD')
    monkeypatch.setattr(run,'REPO_ROOT',repo)
    for _sid,short,_flavor,_owner in sprint.role_rows(FID):
        path=repo/'.sc-worktrees'/short
        run.ensure_worktree(path,short)
        assert git('-C',str(path),'rev-parse','HEAD')==source
        assert git('-C',str(path),'branch','--show-current')=='shell/'+short
        assert Path(git('-C',str(path),'rev-parse','--git-common-dir')).resolve()==repo/'.git'


def test_pending_worker_receipt_prevents_root_deletion_after_actual_stop_proof(tmp_path,monkeypatch):
    from test_gui_experiment import marked, missing_state
    monkeypatch.setattr(fixture,'REGISTRY',tmp_path/'registry')
    # Existing maintainer fixture helper is a separate imported module; bind its registry too.
    import test_gui_experiment as original
    monkeypatch.setattr(original.fixture,'REGISTRY',tmp_path/'registry')
    record,root,receipt=marked(tmp_path)
    record['purpose']='native-sprint'
    fixture.write_json(root/fixture.MARKER,fixture.identity(record));fixture.save(record,receipt)
    fixture.write_json(root/'sprint-status.json',{'fixture_id':record['fixture_id'],'source_sha':record['source_sha'],
                      'purpose':'native-sprint','unit':record['unit'],'state':'cleanup_inconclusive'})
    monkeypatch.setattr(fixture,'unit_state',lambda _:missing_state())
    monkeypatch.setattr(fixture,'cgroup_pids',lambda _:[])
    monkeypatch.setattr(fixture,'process_start_ticks',lambda _:None)
    with pytest.raises(fixture.FixtureError) as exc:fixture.stop(receipt)
    assert exc.value.code=='CLEANUP_UNVERIFIED'
    assert root.exists() and fixture.read_json(receipt)['cleanup']['complete'] is False


@pytest.mark.parametrize('owner', [[], {'pid':True,'start_ticks':1,'unit':'unit','control_group':'group'}])
def test_private_worker_owner_type_is_strict(tmp_path,owner):
    path=tmp_path/'sprint-status.json'
    fixture.write_json(path,{'state':'stopped','owner':owner})
    with pytest.raises(fixture.FixtureError):fixture.read_sprint_status(tmp_path)


def test_durable_cleanup_readback_is_empty_only_when_no_native_obligations(database):
    path,con=database
    sprint.require_durable_cleanup(path,deadline=time.monotonic()+1)
    cid=planner_chat(con)
    con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,cleanup_json,created_at,updated_at) VALUES('g',?,4,1,'codex','{}','closing','{}',1,1)",(cid,));con.commit()
    with pytest.raises(sprint.SprintFixtureError):sprint.require_durable_cleanup(path,deadline=time.monotonic()+1)


def test_fixed_copied_helper_load_refuses_hash_symlink_and_missing(tmp_path):
    path=tmp_path/fixture.SPRINT_HELPER;path.parent.mkdir(parents=True)
    raw=(ROOT/fixture.SPRINT_HELPER).read_bytes();path.write_bytes(raw)
    sha=hashlib.sha256(raw).hexdigest()
    assert fixture.sprint_helper(tmp_path,sha).PURPOSE=='native-sprint'
    with pytest.raises(fixture.FixtureError):fixture.sprint_helper(tmp_path,'a'*64)
    path.unlink();path.symlink_to(ROOT/fixture.SPRINT_HELPER)
    with pytest.raises(OSError):fixture.sprint_helper(tmp_path,sha)
    path.unlink()
    with pytest.raises(OSError):fixture.sprint_helper(tmp_path,sha)


@pytest.mark.parametrize('change', ['alias','directory','binary'])
def test_alias_rechecks_all_path_identities_after_bounded_hash(inert_alias,monkeypatch,tmp_path,change):
    alias,executable=inert_alias;alias.prepare(deadline=time.monotonic()+1)
    original=sprint.executable_identity
    def change_after_hash(path,*,deadline):
        result=original(path,deadline=deadline)
        if change=='alias':alias.alias.unlink();alias.alias.symlink_to('/bin/false')
        elif change=='directory':
            alias.directory.rename(tmp_path/'old-directory');alias.directory.mkdir(mode=0o700)
            alias.alias.symlink_to(executable)
        else:executable.write_bytes(b'changed-after-hash')
        return result
    monkeypatch.setattr(sprint,'executable_identity',change_after_hash)
    with pytest.raises(sprint.SprintFixtureError):alias.verify(deadline=time.monotonic()+1)


def test_shutdown_expiry_after_close_refuses_final_stopped_receipt(tmp_path,monkeypatch):
    records=[];seat=sprint.SprintSeat(tmp_path/'db',FID,record=records.append);seat.service=ThreadStub(False)
    def exhausted(*args,**kw):time.sleep(.025);return 0
    monkeypatch.setattr(sprint,'close_operator_chats',exhausted)
    with pytest.raises(sprint.SprintFixtureError):seat.shutdown(deadline=time.monotonic()+.01)
    assert records==[{'state':'cleanup_inconclusive','code':'SPRINT_PLANNER_CLOSE_PENDING'}]


def test_expired_cleanup_never_opens_database(tmp_path,monkeypatch):
    monkeypatch.setattr(sqlite3,'connect',lambda *args,**kw:pytest.fail('expired cleanup cannot open DB'))
    with pytest.raises(sprint.SprintFixtureError):sprint.close_operator_chats(tmp_path/'db',FID,deadline=time.monotonic()-1)


def test_canonical_close_writer_wait_uses_original_absolute_deadline(database):
    path,con=database;cid=planner_chat(con)
    blocker=sqlite3.connect(path,check_same_thread=False);blocker.execute('BEGIN IMMEDIATE')
    release=threading.Timer(.25,blocker.rollback);release.start();before=time.monotonic()
    try:
        with pytest.raises(sprint.SprintFixtureError):sprint.close_operator_chats(path,FID,deadline=before+.025)
        assert time.monotonic()-before<.10
        assert con.execute('SELECT state FROM conversations WHERE conversation_id=?',(cid,)).fetchone()[0]!='closed'
    finally:release.join(1);blocker.close()


@pytest.mark.parametrize('obligation', ['native','artifact','clean'])
def test_not_started_retains_actual_durable_obligations_even_with_valid_helper(tmp_path,monkeypatch,database,obligation):
    import test_gui_experiment as original
    monkeypatch.setattr(fixture,'REGISTRY',tmp_path/'registry');monkeypatch.setattr(original.fixture,'REGISTRY',tmp_path/'registry')
    _path,con=database;cid=planner_chat(con)
    if obligation=='native':
        con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,state,cleanup_json,created_at,updated_at) VALUES('g',?,4,1,'codex','{}','closing','{}',1,1)",(cid,))
    elif obligation=='artifact':
        con.execute("INSERT INTO sprint_participants(sprint_id,shell_id,role,harness) VALUES(1,2,'reviewer','codex')")
        con.execute("UPDATE sprints SET conformance_reviewer_shell_id=2,conformance_owner_generation=1,merge_grant_enabled=1 WHERE sprint_id=1")
        con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=1")
        con.execute("UPDATE sprints SET lifecycle='completed',terminal_outcome='accepted' WHERE sprint_id=1")
        con.execute("INSERT INTO sprint_cleanup_targets(sprint_id,target_kind,canonical_path,repository_root,git_common_dir) VALUES(1,'artifact_dir','/synthetic/artifact','/synthetic','/synthetic/.git')")
    con.commit();record,root,receipt=original.marked(tmp_path)
    db=root/'.super-coder/shell_db.db';db.parent.mkdir()
    with sqlite3.connect(db) as copy:con.backup(copy)
    helper=root/fixture.SPRINT_HELPER;helper.parent.mkdir();helper.write_bytes((ROOT/fixture.SPRINT_HELPER).read_bytes())
    record.update(purpose='native-sprint',runtime='experimental',sprint_helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())
    fixture.write_json(root/fixture.MARKER,fixture.identity(record));fixture.save(record,receipt)
    fixture.write_json(root/'sprint-status.json',{'fixture_id':record['fixture_id'],'source_sha':record['source_sha'],'purpose':'native-sprint','unit':record['unit'],'state':'not_started'})
    monkeypatch.setattr(fixture,'unit_state',lambda _:original.missing_state());monkeypatch.setattr(fixture,'cgroup_pids',lambda _:[])
    monkeypatch.setattr(fixture,'process_start_ticks',lambda _:None)
    if obligation=='clean':
        assert fixture.stop(receipt)['cleanup']['complete'] is True and not root.exists()
    else:
        with pytest.raises(fixture.FixtureError):fixture.stop(receipt)
        assert root.exists() and db.exists() and fixture.read_json(receipt)['cleanup']['complete'] is False
