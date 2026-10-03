"""Reusable qualification uses actual setup files/parser and a synthetic DB.

Only service/process observation callbacks are inert; this is no native proof.
"""
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_claude_setup as inherited

context, transcript = inherited.context, inherited.transcript
fixture, setup = inherited.fixture, inherited.setup


@pytest.fixture
def completed(transcript, monkeypatch):
    import pwd
    import sqlite3

    context, binding, path, row = transcript
    gid = context.generation_id
    receipt = Path(binding['receipt'])
    record = {'source_sha': '1' * 40, 'fixture_id': '2' * 32, 'root': str(context.worktree),
              'status': 'serving', 'main_pid': 321, 'main_pid_start_ticks': 456,
              'control_group': '/synthetic-api', 'ownership_nonce': 'synthetic-owner'}
    native = {'generation_id': gid, 'harness': 'claude', 'purpose': 'claude_setup', 'status': 'stopped',
              'unit': fixture.PREFIX + record['fixture_id'] + '-native-' + gid + '.service',
              'root': str(context.state_root), 'endpoint': str(fixture.native_endpoint(record['fixture_id'], gid)),
              'root_device': context.state_root.stat().st_dev, 'root_inode': context.state_root.stat().st_ino,
              'main_pid': 654, 'main_pid_start_ticks': 789, 'control_group': '/synthetic-setup',
              'setup_child': {'pid': 987, 'start_ticks': 765, 'control_group': '/synthetic-setup'},
              'setup_helper_sha256': setup.digest(Path(setup.__file__).resolve()),
              'setup_executable_sha256': binding['executable_sha256'],
              'os_cleanup': {'complete': True, 'cgroup_empty': True, 'recorded_process_exited': True},
              'setup_diagnostic': {'phase': 'observation', 'state': 'returned', 'code': 'NONE'},
              'setup_transcript_evidence': setup.transcript_turn_evidence(context.state_root, deadline=time.monotonic()+2)}
    record['native_units'] = [native]
    observation = {'state': 'owned_startup_observed', 'generation_id': gid, 'observed_at': time.time(),
                   **{key: binding[key] for key in ('configuration_sha256', 'executable_sha256', 'source_condition_sha256')},
                   'hook_sha256': native['setup_helper_sha256'], 'auto_memory_enabled_setting': False,
                   'effective_telemetry': False, 'evidence_level': 'configuration_source_flag_inference',
                   'inherited_disable_flag': '1'}
    setup.write_private(context.state_root/'claude-setup-observation.json', observation)
    with sqlite3.connect(context.worktree/'.super-coder/shell_db.db') as con:
        con.execute('CREATE TABLE conversations(conversation_id TEXT,owner_user_id INTEGER,state TEXT,worktree TEXT,'
                    'harness TEXT,provider TEXT,model TEXT,effort TEXT,runtime_projection TEXT,shell_id INTEGER)')
        con.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (context.conversation_id, 1, 'closed', str(context.worktree), 'claude', 'anthropic',
                     context.model, context.effort, json.dumps({'role': 'setup', 'generation_id': gid}), context.shell_id))
    for key in ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_CUSTOM_OAUTH_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=binding['native_home']))
    monkeypatch.setattr(fixture, 'verify_receipt', lambda value: copy.deepcopy(record) if value == receipt else None)
    monkeypatch.setattr(fixture, 'verify_root', lambda value: context.worktree)
    monkeypatch.setattr(fixture, 'unit_state', lambda *args, **kwargs:
                        {'ActiveState': 'active', 'Description': fixture.description(record),
                         'MainPID': '321', 'ControlGroup': '/synthetic-api'})
    monkeypatch.setattr(fixture, 'process_start_ticks', lambda pid: 456 if pid == 321 else None)
    monkeypatch.setattr(fixture, 'cgroup_pids', lambda group: [321] if group == '/synthetic-api' else [])
    confirmation = {'source': record['source_sha'], 'setup_gid': gid,
                    'actions': 'sign_in_trust_quit_only', 'confirmed': True}
    return SimpleNamespace(context=context, binding=binding, path=path, row=row, record=record,
                           native=native, receipt=receipt, confirmation=confirmation)


def qualify(value, **kwargs):
    return setup.qualify_setup(SimpleNamespace(receipt=value.receipt), value.context.generation_id,
                               kwargs.get('confirmation', value.confirmation),
                               deadline=kwargs.get('deadline', time.monotonic()+2))


def test_current_owned_files_and_human_confirmation_qualify_without_readiness_grant(completed):
    result = qualify(completed)
    assert result['state'] == 'qualified'
    assert result['turn_record_observation'] == 'observed_zero_work_turn_records'
    assert result['native_inference_verified'] is False and result['effective_telemetry'] is False
    assert result['native_trust_acceptance'] == 'unproved'
    assert not {'grade', 'admissible', 'account', 'model', 'effort', 'configuration_sha256', 'path'} & result.keys()
    encoded = json.dumps(result)
    assert completed.binding['native_home'] not in encoded and str(completed.context.worktree) not in encoded
    saved = completed.context.state_root/'claude-setup-confirmation.json'
    assert json.loads(saved.read_text()) == completed.confirmation and saved.stat().st_mode & 0o777 == 0o600
    assert qualify(completed)['state'] == 'inconclusive'  # Human act is captured once, never replayed.


@pytest.mark.parametrize('change', ['missing', 'false', 'numeric', 'gid', 'source', 'extra', 'work'])
def test_missing_or_different_human_act_never_qualifies(completed, change):
    value = dict(completed.confirmation)
    if change == 'missing': value = {}
    if change == 'false': value['confirmed'] = False
    if change == 'numeric': value['confirmed'] = 1
    if change == 'gid': value['setup_gid'] = 'b'*32
    if change == 'source': value['source'] = 'f'*40
    if change == 'extra': value['extra'] = 'private'
    if change == 'work': value['actions'] = 'typed_work_prompt'
    assert qualify(completed, confirmation=value)['state'] == 'inconclusive'
    assert not (completed.context.state_root/'claude-setup-confirmation.json').exists()


@pytest.mark.parametrize('change', ['user', 'assistant', 'empty', 'newtype', 'removed', 'foreign', 'changed-settings'])
def test_refresh_exact_transcript_and_configuration_after_confirmation(completed, change):
    row = dict(completed.row)
    if change in {'user', 'assistant'}:
        row.update(type=change, message={'content': 'synthetic work'})
    if change == 'newtype': row['type'] = 'unknown'
    if change == 'foreign': row['sessionId'] = 'foreign'
    if change == 'removed': completed.path.unlink()
    elif change == 'empty': completed.path.write_text('')
    else: completed.path.write_text(json.dumps(row)+'\n')
    if change == 'changed-settings':
        (completed.context.state_root/'claude-setup-settings.json').write_text('{}')
    assert qualify(completed)['state'] == 'inconclusive'


def test_qualified_missing_is_labeled_inference_not_zero(completed):
    completed.path.unlink()
    pointer = completed.context.state_root/'claude-setup-transcript.json'
    pointer.unlink()
    setup.write_private(pointer, setup.transcript_pointer(completed.binding, completed.path))
    completed.native['setup_transcript_evidence'] = setup.transcript_turn_evidence(completed.context.state_root,
                                                                                deadline=time.monotonic()+2)
    result = qualify(completed)
    assert result['state'] == 'qualified' and result['turn_record_observation'] == 'unobserved'
    assert result['native_inference_verified'] is False
    assert 'zero_turn_records' not in result


def test_missing_qualification_refreshes_after_capturing_confirmation(completed, monkeypatch):
    completed.path.unlink()
    pointer = completed.context.state_root/'claude-setup-transcript.json'
    pointer.unlink()
    setup.write_private(pointer, setup.transcript_pointer(completed.binding, completed.path))
    completed.native['setup_transcript_evidence'] = setup.transcript_turn_evidence(completed.context.state_root,
                                                                                deadline=time.monotonic()+2)
    original = setup.write_private
    def confirmed(path, value):
        original(path, value)
        completed.path.write_text(json.dumps({**completed.row, 'type': 'user',
                                             'message': {'content': 'synthetic work'}})+'\n')
    monkeypatch.setattr(setup, 'write_private', confirmed)
    assert qualify(completed)['state'] == 'inconclusive'
    assert setup.transcript_turn_evidence(completed.context.state_root,
                                         deadline=time.monotonic()+2)['user_records'] == 1


@pytest.mark.parametrize('field,value', [('os_cleanup', None), ('os_cleanup', []), ('setup_child', None)])
def test_malformed_private_receipt_is_static_unavailable(completed, field, value):
    completed.native[field] = value
    assert qualify(completed) == {'state': 'inconclusive', 'code': 'SETUP_EVIDENCE_UNAVAILABLE',
                                  'effective_telemetry': False, 'native_inference_verified': False}


@pytest.mark.parametrize('change', ['cleanup', 'alive-child', 'owner', 'role', 'open-preparer', 'source', 'namespace', 'expiry'])
def test_current_source_ownership_preparer_and_absolute_budget_fences(completed, change, monkeypatch):
    import sqlite3

    if change == 'cleanup': completed.native['os_cleanup']['complete'] = 1
    if change == 'alive-child': monkeypatch.setattr(fixture, 'process_start_ticks', lambda pid: 456 if pid == 321 else 765)
    if change == 'owner': completed.record['main_pid_start_ticks'] = 457
    if change in {'role', 'open-preparer'}:
        with sqlite3.connect(completed.context.worktree/'.super-coder/shell_db.db') as con:
            if change == 'role': con.execute("UPDATE conversations SET runtime_projection='{}'")
            else: con.execute("UPDATE conversations SET state='open'")
    if change == 'source': completed.native['setup_helper_sha256'] = '0'*64
    if change == 'namespace': monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/unsupported-namespace')
    deadline = time.monotonic()-1 if change == 'expiry' else time.monotonic()+2
    assert qualify(completed, deadline=deadline)['state'] == 'inconclusive'


def test_blocking_owner_callback_cannot_return_qualified_after_budget(completed, monkeypatch):
    deadline = time.monotonic()+.02
    def late(*args, **kwargs):
        time.sleep(max(0, deadline-time.monotonic())+.01)
        return {'ActiveState': 'active'}
    monkeypatch.setattr(fixture, 'unit_state', late)
    assert qualify(completed, deadline=deadline)['state'] == 'inconclusive'
    assert not (completed.context.state_root/'claude-setup-confirmation.json').exists()


@pytest.mark.parametrize('mutation', ['state', 'shell-owner'])
def test_final_preparer_join_refreshed_after_transcript_observation(completed, monkeypatch, mutation):
    import sqlite3

    original = setup.transcript_turn_evidence
    def changed(state, *, deadline):
        evidence = original(state, deadline=deadline)
        with sqlite3.connect(completed.context.worktree/'.super-coder/shell_db.db') as con:
            if mutation == 'state':
                con.execute("UPDATE conversations SET state='idle'")
            else:
                con.execute('UPDATE shells SET user_id=2')
        return evidence
    monkeypatch.setattr(setup, 'transcript_turn_evidence', changed)
    assert qualify(completed)['state'] == 'inconclusive'


def test_private_record_symlink_refused_without_public_payload(completed, tmp_path):
    path = completed.context.state_root/'claude-setup-observation.json'
    other = tmp_path/'private'; path.rename(other); path.symlink_to(other)
    result = qualify(completed)
    assert result == {'state': 'inconclusive', 'code': 'SETUP_EVIDENCE_UNAVAILABLE',
                      'effective_telemetry': False, 'native_inference_verified': False}
    assert 'private' not in json.dumps(result)
