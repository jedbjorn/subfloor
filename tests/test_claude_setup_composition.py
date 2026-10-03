"""Setup packaging interactions on coherent source; no native or systemd runs."""
import time
from pathlib import Path

import pytest
import test_gui_experiment as inherited

fixture, marked, seat = inherited.fixture, inherited.marked, inherited.seat


def test_runtime_none_only_allows_named_setup_registration(seat):
    _, root, receipt = marked(seat)
    supervisor = fixture.NativeSupervisor(receipt)
    with pytest.raises(fixture.FixtureError):
        supervisor.register('1' * 32, 'claude')
    with pytest.raises(fixture.FixtureError):
        supervisor.register('2' * 32, 'codex', purpose='claude_setup')
    with pytest.raises(fixture.FixtureError):
        supervisor.register('3' * 32, 'claude', purpose='claude_setup', test_transport=True)
    native = supervisor.register('4' * 32, 'claude', purpose='claude_setup', deadline=time.monotonic()+1)
    assert native['purpose'] == 'claude_setup' and native['status'] == 'registered'
    assert len(fixture.verify_receipt(receipt)['native_units']) == 1
    assert (root / 'runtime' / ('4' * 32)).is_dir()


def test_registered_setup_refuses_controller_before_unit_observation(seat, monkeypatch):
    _, _, receipt = marked(seat)
    supervisor = fixture.NativeSupervisor(receipt)
    supervisor.register('4' * 32, 'claude', purpose='claude_setup', deadline=time.monotonic()+1)
    calls = []
    monkeypatch.setattr(fixture, 'native_unit_state', lambda *a, **k: calls.append('unit'))
    monkeypatch.setattr(fixture, 'command', lambda *a, **k: calls.append('command'))
    with pytest.raises(fixture.FixtureError, match='setup ownership'):
        supervisor.launch('4' * 32)
    assert calls == []


def test_setup_deadline_refuses_registration_before_state_creation(seat):
    _, root, receipt = marked(seat)
    with pytest.raises(fixture.FixtureError):
        fixture.NativeSupervisor(receipt).register('5' * 32, 'claude', purpose='claude_setup', deadline=time.monotonic()-1)
    assert not (root / 'runtime').exists()


def test_helper_is_exact_approved_source():
    import hashlib
    helper = Path(__file__).resolve().parents[1] / 'maintainer/claude_setup.py'
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == '042e23910fe6bab5773869acd6371a41590d3f67374205571cfc04a823650481'
