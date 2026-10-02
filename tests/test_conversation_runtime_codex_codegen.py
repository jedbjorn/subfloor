"""Fixed synthetic executables in the test's cgroup; no native account/model."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
import conversation_runtime_codex_codegen as codegen
from conversation_runtime_contract import ExecutableBinding


@pytest.fixture
def seat(tmp_path):
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    executable = tmp_path / 'fixed-codegen'
    def make(body):
        executable.write_text(f'#!{sys.executable}\n'+body)
        executable.chmod(0o700)
        return ExecutableBinding(executable, hashlib.sha256(executable.read_bytes()).hexdigest(), 'synthetic')
    binding = make('import sys,json,os\nfrom pathlib import Path\n'
                   'p=Path(sys.argv[-1]); (p/"shape.json").write_text(json.dumps({'
                   '"keys":sorted(os.environ),"home":os.environ["HOME"],"argv":sys.argv[1:]}))\n')
    owner = {'pid': os.getpid(), 'start_ticks': codegen._process(os.getpid())[0],
             'unit': 'fixed-owned-source-test.service', 'control_group': codegen._cgroup(os.getpid())}
    return root, binding, owner, make


def runner(seat):
    root, binding, owner, _ = seat
    def record(identity, cgroup):
        assert codegen._process(identity.pid)[0] == identity.start_ticks
        assert codegen._cgroup(identity.pid) == cgroup == owner['control_group']
        # Synthetic callback only; real owner must persist before returning.
        return True
    return codegen.make_owned_codegen_runner(binding, root, lambda: owner, record_child=record)


def argv(seat):
    root, binding, _, _ = seat
    return (str(binding.path), 'app-server', 'generate-json-schema', '--experimental', '--out', str(root/'schema'))


def test_fixed_command_environment_receipt_and_explicit_cleanup(seat, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', '/unrelated/account')
    monkeypatch.setenv('OPENAI_API_KEY', 'source-test-value')
    monkeypatch.setenv('SC_API_TOKEN', 'source-test-value')
    parent = dict(os.environ)
    owned = runner(seat)
    assert owned(argv(seat), time.monotonic()+3)
    document = json.loads((seat[0]/'schema/shape.json').read_text())
    assert set(document['keys']) == {'HOME', 'PATH', 'LANG', 'LC_ALL'}
    assert document['home'] == str(seat[0]/'home')
    assert document['argv'] == list(argv(seat)[1:])
    assert dict(os.environ) == parent
    proof = owned.receipt
    assert proof['code'] == 'GENERATED'
    assert proof['child_reaped'] and proof['process_group_exited']
    assert proof['child_registered'] and proof['gate_released']
    assert proof['account_access'] is False and proof['inference_count'] == 0
    assert codegen._process(proof['child_pid']) is None
    assert not owned(argv(seat), time.monotonic()+3)  # no automatic replay
    assert owned.cleanup(time.monotonic()+2)
    assert not list(seat[0].iterdir())
    assert owned.cleanup(time.monotonic()+2)


@pytest.mark.parametrize('change', ['extra', 'wrong_executable', 'outside', 'nested', 'expired',
                                  'wrong_pid', 'wrong_ticks', 'wrong_cgroup', 'changed_binary'])
def test_rejection_precedes_fork_and_filesystem_mutation(seat, change, monkeypatch):
    command = argv(seat); deadline = time.monotonic()+3
    if change == 'extra': command += ('--config',)
    if change == 'wrong_executable': command = ('/bin/true',)+command[1:]
    if change == 'outside': command = command[:-1]+(str(seat[0].parent/'foreign'),)
    if change == 'nested': command = command[:-1]+(str(seat[0]/'nested/schema'),)
    if change == 'expired': deadline = time.monotonic()
    if change == 'wrong_pid': seat[2]['pid'] += 1
    if change == 'wrong_ticks': seat[2]['start_ticks'] += 1
    if change == 'wrong_cgroup': seat[2]['control_group'] = '/foreign'
    if change == 'changed_binary': seat[1].path.write_text('changed')
    def forbidden(*args, **kwargs): raise AssertionError('Popen forbidden')
    monkeypatch.setattr(codegen.subprocess, 'Popen', forbidden)
    owned = runner(seat)
    assert not owned(command, deadline)
    assert not list(seat[0].iterdir())
    assert not owned.receipt['child_started']


@pytest.mark.parametrize('alias', ['root', 'output', 'home'])
def test_symlink_foreign_tree_refused_and_preserved(seat, alias):
    root, binding, owner, _ = seat
    foreign = root.parent/'foreign'; foreign.mkdir(mode=0o700)
    marker = foreign/'sentinel'; marker.write_text('preserve')
    if alias == 'root':
        root.rmdir(); root.symlink_to(foreign, target_is_directory=True)
    else: (root/('schema' if alias == 'output' else 'home')).symlink_to(foreign, target_is_directory=True)
    owned = codegen.make_owned_codegen_runner(binding, root, lambda: owner)
    assert not owned(argv(seat), time.monotonic()+3)
    assert marker.read_text() == 'preserve'


def test_owner_rechecked_before_fork_after_private_directory_creation(seat, monkeypatch):
    calls = 0
    def verify():
        nonlocal calls
        calls += 1
        return seat[2] if calls < 4 else {**seat[2], 'unit': 'replacement.service'}
    owned = codegen.make_owned_codegen_runner(seat[1], seat[0], verify)
    monkeypatch.setattr(codegen.subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected fork'))
    assert not owned(argv(seat), time.monotonic()+3)
    assert not owned.receipt['child_started']
    assert not owned.cleanup(time.monotonic()+2)  # replacement guard cannot remove old files


def test_timeout_reaps_owned_resistant_group_and_preserves_unrelated_sentinel(seat):
    binding = seat[3]('import subprocess,sys,time,signal\n'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        'subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(10)"])\n'
        'time.sleep(10)\n')
    updated = (seat[0], binding, seat[2], seat[3])
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
    try:
        owned = runner(updated); started = time.monotonic()
        assert not owned(argv(updated), started+1.4)
        assert time.monotonic()-started < 1.8
        assert owned.receipt['child_reaped'] and owned.receipt['process_group_exited']
        assert sentinel.poll() is None
        assert owned.cleanup(time.monotonic()+2)
    finally:
        sentinel.terminate(); sentinel.wait(timeout=2)


def test_successful_root_exit_also_cleans_live_owned_group(seat):
    binding = seat[3]('import subprocess,sys\n'
        'subprocess.Popen([sys.executable,"-c","import time;time.sleep(10)"])\n')
    updated = (seat[0], binding, seat[2], seat[3])
    owned = runner(updated)
    assert owned(argv(updated), time.monotonic()+3)
    assert owned.receipt['process_group_exited'] and owned.receipt['child_reaped']
    assert owned.cleanup(time.monotonic()+2)


def test_replaced_output_cleanup_refuses_foreign_inode(seat):
    owned = runner(seat); assert owned(argv(seat), time.monotonic()+3)
    original = seat[0]/'schema'; original.rename(seat[0]/'retained-original')
    original.mkdir(mode=0o700); (original/'sentinel').write_text('preserve')
    assert not owned.cleanup(time.monotonic()+2)
    assert (original/'sentinel').read_text() == 'preserve'


def test_changed_executable_after_generation_is_not_a_success(seat, monkeypatch):
    owned = runner(seat)
    original = owned._stop
    def changed(*args):
        stopped = original(*args)
        seat[1].path.write_text('replaced')
        return stopped
    monkeypatch.setattr(owned, '_stop', changed)
    assert not owned(argv(seat), time.monotonic()+3)
    assert owned.receipt['code'] == 'OWNED_CODEGEN_UNAVAILABLE'
    assert owned.cleanup(time.monotonic()+2)


@pytest.mark.parametrize('mode', ['missing', 'false', 'raises'])
def test_durable_child_registration_failure_never_releases_codegen(seat, mode):
    def record(identity, cgroup):
        assert not (seat[0]/'schema/shape.json').exists()
        if mode == 'raises': raise RuntimeError('private owner failure')
        return False
    owned = codegen.make_owned_codegen_runner(seat[1], seat[0], lambda: seat[2],
                                              record_child=None if mode == 'missing' else record)
    assert not owned(argv(seat), time.monotonic()+3)
    assert owned.receipt['child_started'] and owned.receipt['child_reaped']
    assert owned.receipt['process_group_exited'] and not owned.receipt['gate_released']
    assert not (seat[0]/'schema/shape.json').exists()
    assert owned.cleanup(time.monotonic()+2)


def test_concurrent_cleanup_cancels_and_waits_for_owned_child_exit(seat):
    binding = seat[3]('import time\ntime.sleep(10)\n')
    updated = (seat[0], binding, seat[2], seat[3])
    registered = threading.Event()
    def record(identity, cgroup):
        registered.set(); return True
    owned = codegen.make_owned_codegen_runner(binding, seat[0], lambda: seat[2], record_child=record)
    result = []
    worker = threading.Thread(target=lambda: result.append(owned(argv(updated), time.monotonic()+5)))
    worker.start()
    assert registered.wait(1)
    started = time.monotonic()
    assert owned.cleanup(started+2)
    worker.join(timeout=1)
    assert result == [False] and not worker.is_alive()
    assert time.monotonic()-started < 2
    assert owned.receipt['child_reaped'] and owned.receipt['process_group_exited']


def test_bounded_cleanup_does_not_wait_past_its_deadline(seat):
    owned = runner(seat)
    owned._lock.acquire()
    try:
        started = time.monotonic()
        assert not owned.cleanup(started+.03)
        assert time.monotonic()-started < .2
    finally:
        owned._lock.release()
    assert not owned(argv(seat), time.monotonic()+3)
    assert not owned.receipt['child_started']


def test_observer_consumes_generated_files_before_explicit_cleanup(seat):
    from conversation_runtime_codex_schema import observe_codex_schema
    fixture = Path(__file__).parent/'fixtures/codex_native_schema'
    binding = seat[3](f'import shutil,sys,os\nshutil.copytree({str(fixture)!r},sys.argv[-1],dirs_exist_ok=True)\n'
                      'os.chmod(sys.argv[-1],0o700)\n')
    updated = (seat[0], binding, seat[2], seat[3])
    owned = runner(updated)
    observed = observe_codex_schema(binding, seat[0]/'schema', effort='high', run_owned=owned,
                                    deadline=time.monotonic()+3)
    assert observed.generation_completed
    assert set(observed.structural_grades.values()) == {'compatible'}
    assert (seat[0]/'schema/ClientRequest.json').exists()
    assert owned.cleanup(time.monotonic()+2)


def test_timed_run_admission_and_expiry_after_owner_guard(seat):
    owned = runner(seat); owned._lock.acquire()
    results = []
    worker = threading.Thread(target=lambda: results.append(owned(argv(seat), time.monotonic()+.03)))
    try:
        worker.start(); worker.join(.15)
        assert not worker.is_alive() and results == [False]
    finally:
        owned._lock.release(); worker.join(1)
    calls = 0
    def delayed():
        nonlocal calls
        calls += 1
        if calls == 2: time.sleep(.08)
        return seat[2]
    expiring = codegen.make_owned_codegen_runner(seat[1], seat[0], delayed)
    assert not expiring(argv(seat), time.monotonic()+1.02)
    assert not list(seat[0].iterdir())
    assert not expiring.receipt['child_started']
