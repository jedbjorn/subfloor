"""Inert installer and lifecycle isolation: local Git only, no service/native launch."""
import importlib.util
import io
import json
import shutil
import socket
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview', ROOT / 'maintainer/gui_preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
fx = preview.fixture_module(ROOT / 'maintainer')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def commit(repo):
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Preview test', '-c', 'user.email=test@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'test')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def installed(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    for name in fx.SOURCE_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('inert source')
    (source / 'maintainer').mkdir()
    for name in preview.HELPERS:
        shutil.copyfile(ROOT / 'maintainer' / name, source / 'maintainer' / name)
    sha = commit(source)
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q')
    (project / 'app.py').write_text('print("preview code")')
    (project / '.env').write_text('excluded committed secret placeholder')
    project_sha = commit(project)
    (project / 'untracked.txt').write_text('must not copy')
    package = tmp_path / 'package'
    result = preview.install(source, sha, package, project, project_sha)
    return source, project, package, result


def test_install_is_pinned_self_contained_and_does_not_copy_live_files(installed):
    source, project, package, result = installed
    assert result['native_started'] is False
    assert result['project']['commit'] == git(project, 'rev-parse', 'HEAD')
    assert not (source / 'shell_db.db').exists()
    assert (project / 'untracked.txt').read_text() == 'must not copy'
    (source / 'maintainer/gui_experiment.py').write_text('changed after install')
    _, manifest, captured = preview.load(package)
    names = [m.name for m in captured.validate_project((package / 'project.tar').read_bytes())]
    assert names == ['app.py']
    assert manifest['source_sha'] == result['source_sha']
    assert preview.operate(package, 'status')['state'] == 'installed'
    assert preview.operate(package, 'status')['url'] is None


def test_changed_helper_refuses_before_fixture_import_or_launch(installed, monkeypatch):
    _, _, package, _ = installed
    (package / 'helpers/gui_experiment.py').write_text('raise Exception("changed")')
    monkeypatch.setattr(preview, 'fixture_module', lambda _: pytest.fail('must verify before importing'))
    with pytest.raises(preview.PreviewError, match='helper changed'):
        preview.load(package)


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, kind, mode in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = mode
            if kind == tarfile.REGTYPE:
                member.size = 1
                tar.addfile(member, io.BytesIO(b'x'))
            else:
                member.linkname = '/tmp/foreign'
                tar.addfile(member)
    return stream.getvalue()


@pytest.mark.parametrize('entries', [
    [('../escape', tarfile.REGTYPE, 0o644)],
    [('/absolute', tarfile.REGTYPE, 0o644)],
    [('a', tarfile.REGTYPE, 0o644), ('a', tarfile.REGTYPE, 0o644)],
    [('a', tarfile.SYMTYPE, 0o777)], [('a', tarfile.LNKTYPE, 0o644)],
    [('a', tarfile.FIFOTYPE, 0o644)], [('a', tarfile.REGTYPE, 0o4644)],
    [('.super-coder/shell_db.db', tarfile.REGTYPE, 0o644)],
    [('.git/config', tarfile.REGTYPE, 0o644)], [('a/.env.local', tarfile.REGTYPE, 0o644)],
    [('a/./b', tarfile.REGTYPE, 0o644)], [('a\\b', tarfile.REGTYPE, 0o644)],
])
def test_project_validation_precedes_all_mutation(tmp_path, entries):
    root = tmp_path / 'owned'
    root.mkdir()
    with pytest.raises(fx.FixtureError):
        fx.extract_project(root, tar_bytes(entries))
    assert list(root.iterdir()) == []
    assert not (tmp_path / 'escape').exists()


def test_project_collision_and_committed_symlink_refused(installed, tmp_path):
    _, project, _, _ = installed
    (project / 'link').symlink_to('app.py')
    sha = commit(project)
    with pytest.raises(fx.FixtureError, match='links'):
        fx.committed_project(project, sha)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'app.py').write_text('captured')
    with pytest.raises(fx.FixtureError, match='collides'):
        fx.extract_project(root, tar_bytes([('app.py', tarfile.REGTYPE, 0o644)]))
    assert (root / 'app.py').read_text() == 'captured'


def test_project_size_and_entry_limits_are_preallocation(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, 'MAX_PROJECT_BYTES', 20)
    with pytest.raises(fx.FixtureError, match='bound'):
        fx.validate_project(tar_bytes([('app.py', tarfile.REGTYPE, 0o644)]))
    monkeypatch.setattr(fx, 'MAX_PROJECT_BYTES', 32768)
    monkeypatch.setattr(fx, 'MAX_PROJECT_ENTRIES', 1)
    with pytest.raises(fx.FixtureError):
        fx.validate_project(tar_bytes([('a', tarfile.REGTYPE, 0o644), ('b', tarfile.REGTYPE, 0o644)]))


@pytest.fixture
def lifecycle(installed, monkeypatch):
    _, _, package, _ = installed
    _, manifest, captured = preview.load(package)
    captured.REGISTRY = package.parent / 'registry'
    original = preview.load
    monkeypatch.setattr(preview, 'load', lambda p: (p.absolute(), json.loads((p / 'install.json').read_text()), captured))
    monkeypatch.setattr(captured, 'unit_state', lambda *a, **k: {'LoadState': 'not-found', 'ActiveState': 'inactive'})
    monkeypatch.setattr(captured, 'owned_unit', lambda *a: False)
    calls = []
    def start(repo, sha, receipt, **kwargs):
        calls.append(('start', receipt, kwargs))
        fid = str(len(calls)).zfill(32)
        root = package / 'runs' / (captured.PREFIX + fid)
        root.mkdir(mode=0o700)
        info = root.stat()
        record = dict(version=1, fixture_id=fid, source_sha=sha,  # noqa: C408 - fixture keyword shape
                      archive_sha256=manifest['engine_sha256'], root=str(root), root_device=info.st_dev,
                      root_inode=info.st_ino, unit=captured.PREFIX + fid + '.service',
                      ownership_nonce='a' * 64, runtime='experimental', port=49123,
                      limits=captured.validate_limits(1800, 512, 64),
                      bootstrap_sha256=manifest['helpers']['gui_experiment.py'], receipt=str(receipt),
                      status='serving', cleanup={'complete': False}, expires_at=10**12,
                      url='http://127.0.0.1:49123', project=manifest['project'], native_harnesses=['codex'], preview=True)
        captured.write_json(root / captured.MARKER, captured.identity(record))
        captured.save(record, receipt)
        return record
    def stop(receipt):
        record = captured.verify_receipt(receipt)
        calls.append(('stop', receipt))
        root = Path(record['root'])
        if root.exists():
            captured.verify_root(record)
            shutil.rmtree(root)
        record.update(status='stopped', cleanup={'complete': True, 'root_removed': True})
        captured.save(record, receipt)
        return record
    monkeypatch.setattr(captured, 'start', start)
    monkeypatch.setattr(captured, 'stop', stop)
    return package, captured, calls, original


def test_fresh_restart_repeated_stop_and_remove_keep_cleanup_receipts(lifecycle):
    package, captured, calls, _ = lifecycle
    first = preview.operate(package, 'start', 49123)
    assert calls[0][2]['native_harnesses'] == ('codex',)
    assert calls[0][2]['lifetime'] == 1800
    old_root = Path(first['workspace'])
    (old_root / 'old-chat-state').write_text('old')
    with pytest.raises(preview.PreviewError, match='existing session'):
        preview.operate(package, 'start')
    second = preview.operate(package, 'restart')
    assert not old_root.exists()
    assert second['workspace'] != first['workspace']
    assert not (Path(second['workspace']) / 'old-chat-state').exists()
    assert preview.operate(package, 'stop')['cleanup_complete'] is True
    assert preview.operate(package, 'stop')['cleanup_complete'] is True
    assert preview.operate(package, 'remove')['cleanup_complete'] is True
    assert not package.exists()
    assert (package.parent / 'package-cleanup.json').is_file()
    assert list(captured.REGISTRY.glob('*.json'))  # ownership retained outside removed state


def test_unknown_cleanup_blocks_restart_and_remove(lifecycle, monkeypatch):
    package, captured, calls, _ = lifecycle
    preview.operate(package, 'start')
    def incomplete(_):
        raise captured.FixtureError('CLEANUP_UNVERIFIED', 'unknown')
    monkeypatch.setattr(captured, 'stop', incomplete)
    for action in ('restart', 'remove'):
        with pytest.raises(captured.FixtureError):
            preview.operate(package, action)
    assert package.is_dir()
    assert len([c for c in calls if c[0] == 'start']) == 1
    assert (package / 'runs/1.json').is_file()


def test_foreign_receipt_never_dispatches_stop(lifecycle, monkeypatch):
    package, captured, _, _ = lifecycle
    preview.operate(package, 'start')
    receipt = package / 'runs/1.json'
    record = captured.verify_receipt(receipt)
    record['source_sha'] = 'f' * 40
    captured.save(record, receipt)
    monkeypatch.setattr(captured, 'stop', lambda _: pytest.fail('must refuse foreign source'))
    with pytest.raises(preview.PreviewError, match='another installation'):
        preview.operate(package, 'remove')


def test_expiry_is_visible_without_fresh_admission(lifecycle, monkeypatch):
    package, captured, _, _ = lifecycle
    preview.operate(package, 'start')
    receipt = package / 'runs/1.json'
    record = captured.verify_receipt(receipt)
    record['expires_at'] = 0
    captured.save(record, receipt)
    assert preview.operate(package, 'status')['state'] == 'expired-cleanup-required'
    with pytest.raises(preview.PreviewError):
        preview.operate(package, 'start')


def test_preallocation_failure_rolls_back_but_allocated_failure_retains(lifecycle, monkeypatch):
    package, captured, _, _ = lifecycle
    monkeypatch.setattr(captured, 'start', lambda *a, **k: (_ for _ in ()).throw(captured.FixtureError('BIND_CONFLICT', 'occupied')))
    with pytest.raises(captured.FixtureError):
        preview.operate(package, 'start', 49123)
    assert json.loads((package / 'install.json').read_text())['sessions'] == []
    def allocated(repo, sha, receipt, **kwargs):
        captured.write_json(receipt, {'retained': True})
        raise captured.FixtureError('CLEANUP_UNVERIFIED', 'pending')
    monkeypatch.setattr(captured, 'start', allocated)
    with pytest.raises(captured.FixtureError):
        preview.operate(package, 'start')
    assert json.loads((package / 'install.json').read_text())['sessions'] == ['1']
    assert (package / 'runs/1.json').is_file()


def test_aliased_installation_refused(installed, tmp_path):
    _, _, package, _ = installed
    link = tmp_path / 'alias'
    link.symlink_to(package, target_is_directory=True)
    with pytest.raises(preview.PreviewError):
        preview.load(link)


def test_codex_only_capture_prevents_claude_observation_before_launch(lifecycle, monkeypatch):
    package, captured, _, _ = lifecycle
    preview.operate(package, 'start')
    record = captured.verify_receipt(package / 'runs/1.json')
    argv = []
    monkeypatch.setattr(captured.shutil, 'which', lambda name: '/captured/' + name)
    monkeypatch.setattr(captured, 'command', lambda command, **kwargs: argv.extend(command))
    captured.launch_api(record, Path(record['root']))
    assert any(arg.startswith('--setenv=SC_FIXTURE_NATIVE_CODEX=') for arg in argv)
    assert not any(arg.startswith('--setenv=SC_FIXTURE_NATIVE_CLAUDE=') for arg in argv)
    assert record['native_harnesses'] == ['codex']
    marker = captured.read_json(Path(record['root']) / captured.MARKER)
    assert marker['native_harnesses'] == ['codex']


def test_disposable_project_enters_synthetic_git_and_worktrees_only(installed, tmp_path):
    _, project, package, _ = installed
    before = git(project, 'rev-parse', 'HEAD')
    root = tmp_path / 'marked-root'
    root.mkdir(mode=0o700)
    (root / '.super-coder').mkdir()
    (root / 'sc').write_text('inert')
    fx.extract_project(root, (package / 'project.tar').read_bytes())
    fx.bootstrap_repository(root)
    worktree = root / '.sc-worktrees/test'
    fx.command(['git', '-C', str(root), 'worktree', 'add', '--quiet', '-b', 'shell/test', str(worktree)])
    assert (worktree / 'app.py').read_text() == (project / 'app.py').read_text()
    (worktree / 'app.py').write_text('disposable edit')
    assert (project / 'app.py').read_text() == 'print("preview code")'
    assert git(project, 'rev-parse', 'HEAD') == before
    assert not (worktree / '.env').exists()
    assert not (worktree / 'untracked.txt').exists()
    assert not (root / 'maintainer/gui_preview.py').exists()


def test_preview_banner_is_only_captured_profile_and_never_changes_source_html():
    original = (200, [('Content-Length', '34')], b'<html><body>real app</body></html>')
    assert fx.preview_html({}, original) is original
    assert fx.preview_html({'preview': True, 'native_harnesses': ['claude']}, original) is original
    decorated = fx.preview_html({'preview': True, 'native_harnesses': ['codex'], 'expires_at': 1}, original)
    assert b'Isolated native Chat preview' in decorated[2]
    assert b'Stop/restart deletes chats and project edits' in decorated[2]
    assert b'Claude setup pending; Sprints unavailable' in decorated[2]
    assert b'real app' in decorated[2]
    assert not decorated[1]
    assert original[2] == b'<html><body>real app</body></html>'


def test_adjacent_dev_port_conflict_refuses_before_session_reservation(lifecycle, monkeypatch):
    package, _, calls, _ = lifecycle
    with socket.socket() as dev:
        dev.bind(('127.0.0.1', 0))
        dev_port = dev.getsockname()[1]
        with pytest.raises(preview.PreviewError, match='occupied'):
            preview.operate(package, 'start', dev_port - 1)
    assert calls == []
    assert json.loads((package / 'install.json').read_text())['sessions'] == []


def test_preview_port_default_ignores_host_dev_port_and_invalid_types(monkeypatch):
    monkeypatch.setenv('SC_DEV_PORT', '1')
    selected = preview.preview_ports(None)
    assert selected > 1024
    for invalid in (True, 0, 65536, '123'):
        with pytest.raises(preview.PreviewError):
            preview.preview_ports(invalid)


def test_committed_project_cannot_override_engine_or_managed_boot(installed, tmp_path):
    _, project, _, _ = installed
    reserved = ['sc', 'AGENTS.md', 'CLAUDE.md', 'opencode.json',
                '.super-coder/api/server.py', '.sc-state/engine.ref',
                '.subfloor/dev-kit', '.claude/settings.json', '.codex/config.toml']
    for name in reserved:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('project-owned replacement must not enter archive')
    sha = commit(project)
    _, raw = fx.committed_project(project, sha)
    assert [entry.name for entry in fx.validate_project(raw)] == ['app.py']
    root = tmp_path / 'engine-root'
    root.mkdir()
    for name in fx.SOURCE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('captured engine identity')
    fx.extract_project(root, raw)
    assert all((root / name).read_text() == 'captured engine identity' for name in fx.SOURCE_FILES)
    assert (root / 'app.py').read_text() == 'print("preview code")'
    assert not (root / 'AGENTS.md').exists()
    assert not (root / 'opencode.json').exists()
