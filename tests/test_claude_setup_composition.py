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


def test_core_setup_guards_retain_approved_source():
    import ast
    import hashlib
    helper = Path(__file__).resolve().parents[1] / 'maintainer/claude_setup.py'
    tree = ast.parse(helper.read_text())
    hashes = {node.name: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
              for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert hashes['validate'] == 'ff6dbb2f17a52a1eeb7f075c3f5fbd1be29ab9ebfa3e1dcd13b90e1b09c0ad12'
    assert hashes['fixed_launch'] == '74ed1381e793723bdda3c3c78700c897026bc115bf8a9f186e471a2d6a2979cf'
    assert hashes['revalidate'] == '6884d0c426f5ee058818872a7b9605caa365c0928d528bc05bb87a539c87a3cb'
    assert hashes['observe_hook'] == 'b3f9adc9130db20b60561969ec0f4d7b295f6079a3f2f320ebc62c5a1cdd06ef'
    assert hashes['transcript_pointer'] == '3828fc73bcc4ce7a47b3de1e9b3c5b1e7c92574924b0bc1e63c7314092352dd4'
    assert hashes['transcript_turn_evidence'] == '349fa1d2df19129781d24bd56ca4d48a3fdb923db19387d65cd1bc0fd9c8ea7f'
    assert hashes['child_owner_current'] == '936d5377c17d004077219f9d97a00097ef785006ed44291a21e08bcc5966e869'
    assert hashes['gated_child'] == 'e55563dae8b7422bf440b5f990356ddd0d98bacbe627628c44672687119fdf2e'
