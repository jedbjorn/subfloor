"""Setup-only transport and scalar evidence, synthetic command/DB authority."""
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import test_claude_setup as inherited

setup, fixture = inherited.setup, inherited.fixture
launch_seat = inherited.launch_seat
context = inherited.context


def bus():
    directory = f'/run/user/{os.getuid()}'
    return {'XDG_RUNTIME_DIR': directory, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + directory + '/bus'}


@pytest.mark.parametrize('change', ['missing', 'foreign_directory', 'foreign_bus', 'oversized', 'symlink', 'foreign_owner'])
def test_service_transport_never_invents_or_accepts_foreign_locator(monkeypatch, change):
    env = bus()
    if change == 'missing': env.pop('DBUS_SESSION_BUS_ADDRESS')
    if change == 'foreign_directory': env['XDG_RUNTIME_DIR'] = '/unrelated'
    if change == 'foreign_bus': env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=/unrelated/bus'
    if change == 'oversized': env['DBUS_SESSION_BUS_ADDRESS'] += 'x' * 300
    calls = []
    def info(path):
        calls.append(str(path))
        mode = stat.S_IFDIR | 0o700 if str(path) == bus()['XDG_RUNTIME_DIR'] else stat.S_IFSOCK | 0o600
        if change == 'symlink': mode = stat.S_IFLNK | 0o777
        return SimpleNamespace(st_mode=mode, st_uid=os.getuid() + (change == 'foreign_owner'))
    monkeypatch.setattr(Path, 'lstat', info)
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises((setup.RuntimeContractError, OSError)):
        setup.service_bus()
    if change in {'missing', 'foreign_directory', 'foreign_bus', 'oversized'}:
        assert calls == []


def test_actual_fixed_launch_vector_preserves_only_bus_pair(launch_seat, monkeypatch):
    supervisor, _, _, starts, _ = launch_seat
    monkeypatch.setattr(setup, 'service_bus', bus)
    supervisor.launch_claude_setup('a' * 32, deadline=time.monotonic() + 5)
    argv = starts[0][0]
    assert '/usr/bin/env' in argv and '-i' in argv
    assert 'XDG_RUNTIME_DIR=' + bus()['XDG_RUNTIME_DIR'] in argv
    assert 'DBUS_SESSION_BUS_ADDRESS=' + bus()['DBUS_SESSION_BUS_ADDRESS'] in argv
    assert not any(x.startswith(('HOME=', 'SC_', 'ANTHROPIC_', 'CLAUDE_', 'CODEX_')) for x in argv)


def test_real_ownership_query_requires_transport_with_inert_systemctl(tmp_path, monkeypatch):
    command = tmp_path / 'systemctl'
    command.write_text('#!' + sys.executable + '\nimport os,sys\n'
        'if not all(os.environ.get(k) for k in ("XDG_RUNTIME_DIR","DBUS_SESSION_BUS_ADDRESS")):sys.exit(1)\n'
        'print("LoadState=loaded\\nActiveState=active\\nMainPID=' + str(os.getpid()) + '\\nDescription=fixed\\nControlGroup=/fixed")\n')
    command.chmod(0o700)
    native = {'generation_id': 'a' * 32, 'purpose': 'claude_setup', 'status': 'active',
        'setup_deadline': time.monotonic() + 5, 'main_pid': os.getpid(),
        'main_pid_start_ticks': 12, 'control_group': '/fixed', 'unit': 'inert.service',
        'setup_helper_sha256': setup.digest(Path(setup.__file__))}
    monkeypatch.setattr(setup, 'fixture_module', lambda: fixture)
    monkeypatch.setattr(fixture, 'verify_receipt', lambda path: {'native_units': [native]})
    monkeypatch.setattr(fixture, 'verify_root', lambda record: tmp_path)
    monkeypatch.setattr(fixture, 'verify_native', lambda *args: None)
    monkeypatch.setattr(fixture, 'native_description', lambda *args: 'fixed')
    monkeypatch.setattr(setup, 'process', lambda pid: (12, '/fixed'))
    supervisor = SimpleNamespace(receipt=tmp_path / 'receipt')
    with mock.patch.dict(os.environ, {'PATH': str(tmp_path)}, clear=True), pytest.raises(fixture.FixtureError):
        setup.setup_owned(supervisor, 'a' * 32)
    with mock.patch.dict(os.environ, {'PATH': str(tmp_path), **bus()}, clear=True):
        assert setup.setup_owned(supervisor, 'a' * 32) is True


def test_setup_clear_retains_bus_and_synthetic_preparation_home(tmp_path, monkeypatch):
    generation = 'a' * 32
    context = SimpleNamespace(conversation_id='fixed-cv')
    monkeypatch.setattr(setup, 'fixture_module', lambda: fixture)
    monkeypatch.setattr(fixture, 'verify_receipt', lambda path: {})
    monkeypatch.setattr(fixture, 'verify_root', lambda record: tmp_path)
    monkeypatch.setattr(setup, 'setup_owned', lambda *args: all(os.environ.get(k) == v for k, v in bus().items()))
    monkeypatch.setattr(setup, 'service_bus', bus)
    phases = []
    monkeypatch.setattr(setup, 'setup_phase', lambda *args: phases.append(args[2:]))
    def prepare(*args):
        assert os.environ['HOME'] == str(tmp_path / 'home')
        assert all(os.environ[k] == v for k, v in bus().items())
        assert 'ANTHROPIC_API_KEY' not in os.environ
        return context
    monkeypatch.setattr(setup, 'prepare_claude_setup_context', prepare)
    monkeypatch.setattr(setup, 'run_setup', lambda *args, **kwargs: {'source_only': True})
    monkeypatch.setattr(setup, 'close_setup_conversation', lambda *args: None)
    with mock.patch.dict(os.environ, {**bus(), 'ANTHROPIC_API_KEY': 'must-not-transfer'}, clear=True):
        assert setup.setup_unit(tmp_path / 'receipt', generation, tmp_path / 'inert', 'b' * 64, time.monotonic()+5) == {'source_only': True}
    assert phases == [('preparation', 'attempted', 'NONE'), ('native_start', 'attempted', 'NONE'), ('observation', 'returned', 'NONE')]


def test_failure_phase_has_static_code_not_exception_payload(tmp_path, monkeypatch):
    generation = 'a' * 32
    monkeypatch.setattr(setup, 'fixture_module', lambda: fixture)
    monkeypatch.setattr(fixture, 'verify_receipt', lambda path: {})
    monkeypatch.setattr(fixture, 'verify_root', lambda record: tmp_path)
    monkeypatch.setattr(setup, 'setup_owned', lambda *args: True)
    monkeypatch.setattr(setup, 'service_bus', bus)
    phases = []
    monkeypatch.setattr(setup, 'setup_phase', lambda *args: phases.append(args[2:]))
    monkeypatch.setattr(setup, 'prepare_claude_setup_context', lambda *args: (_ for _ in ()).throw(setup.RuntimeContractError('SETUP_INCONCLUSIVE', 'private-secret')))
    with mock.patch.dict(os.environ, bus(), clear=True), pytest.raises(setup.RuntimeContractError):
        setup.setup_unit(tmp_path / 'receipt', generation, tmp_path / 'inert', 'b' * 64, time.monotonic()+5)
    assert phases[-1] == ('preparation', 'failed', 'CONTRACT_REFUSED')
    assert 'private-secret' not in json.dumps(phases)


def test_existing_child_exec_gate_receives_only_fixed_transport(context, monkeypatch):
    import pty
    captured = []
    monkeypatch.setattr(setup, 'service_bus', bus)
    original = setup.subprocess.Popen
    def child(argv, **kwargs):
        if '_child' not in argv:
            return original(argv, **kwargs)
        captured.append(kwargs['env'])
        raise RuntimeError('inert child boundary')
    monkeypatch.setattr(setup.subprocess, 'Popen', child)
    outer, terminal = pty.openpty()
    try:
        with pytest.raises(RuntimeError, match='inert child'):
            setup.run_setup(context, context.worktree, verify_owned=lambda: True,
                record_child=lambda *args: True, deadline=time.monotonic()+2,
                input_fd=terminal, output_fd=terminal)
        assert captured == [{'PATH': os.defpath, **bus()}]
    finally:
        os.close(outer); os.close(terminal)


@pytest.mark.parametrize('mutation', ['valid', 'generation', 'helper', 'extra', 'code', 'symlink', 'fifo', 'oversized', 'expired'])
def test_private_diagnostic_reader_is_bounded_and_never_exports_payload(tmp_path, mutation):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    target = root / 'claude-setup-phase-preparation-failed.json'
    value = {'generation': 'a' * 32, 'helper_sha256': 'b' * 64, 'phase': 'preparation', 'state': 'failed', 'code': 'CONTRACT_REFUSED'}
    if mutation == 'generation': value['generation'] = 'other'
    if mutation == 'helper': value['helper_sha256'] = 'c' * 64
    if mutation == 'extra': value['private'] = 'never-export'
    if mutation == 'code': value['code'] = 'never-export'
    if mutation == 'fifo': os.mkfifo(target, 0o600)
    elif mutation == 'symlink': target.symlink_to(tmp_path / 'unrelated')
    else:
        setup.write_private(target, value)
        if mutation == 'oversized': target.write_text('x' * 513)
    result = setup.setup_diagnostic(root, 'a' * 32, 'b' * 64, deadline=time.monotonic()+(-1 if mutation=='expired' else 1))
    if mutation == 'valid': assert result == {'phase': 'preparation', 'state': 'failed', 'code': 'CONTRACT_REFUSED'}
    else: assert result == {'phase': 'unavailable', 'state': 'unavailable', 'code': 'UNAVAILABLE'}
    assert 'never-export' not in json.dumps(result)
