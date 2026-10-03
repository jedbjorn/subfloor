#!/usr/bin/env python3
"""Install and operate a disposable, exact-source native Chat preview.

This is an evaluation package, not the production installer. Stop/restart
remove chat state and project edits. Native account/session files stay in the
operator's existing provider home; credentials are never copied into this kit.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HELPERS = ('gui_preview.py', 'gui_experiment.py', 'claude_setup.py', 'claude_trust.py')
MAX_SESSIONS = 32


class PreviewError(RuntimeError):
    pass


def preflight() -> None:
    if (sys.version_info[:2] != (3, 14)
            or importlib.metadata.version('websockets') != '16.1.1'):
        raise PreviewError('use Python 3.14 with the source-pinned websockets 16.1.1 dependency')


def preview_ports(requested: int | None) -> int:
    """One bounded availability check, not a reservation or retrying manager."""
    if requested is not None and (type(requested) is not int or not 1 <= requested <= 65535):
        raise PreviewError('preview API port must be 1..65535')
    try:
        with socket.socket() as api, socket.socket() as dev:
            api.bind(('127.0.0.1', requested or 0))
            selected = int(api.getsockname()[1])
            dev.bind(('127.0.0.1', selected + 1 if selected < 65535 else 65534))
            return selected
    except OSError:
        raise PreviewError('preview API or adjacent dev port is occupied; choose another --port') from None


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def private_dir(path: Path) -> None:
    info = path.lstat()
    if (path.resolve() != path.absolute() or path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise PreviewError('preview requires an owner-only real directory')


def remove_owned_directory(path: Path, expected: tuple[int, int] | list[int]) -> None:
    """Delete only the original allocation, using its captured directory fd."""
    private_dir(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_fd = None
    try:
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != tuple(expected):
            raise PreviewError('preview directory changed; replacement and ownership retained')
        root_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != tuple(expected):
            raise PreviewError('preview directory changed; replacement and ownership retained')
        # Traverse through the original fd. A renamed/replaced top-level path
        # can never redirect recursive deletion into the replacement's files.
        with os.scandir(root_fd) as entries:
            for entry in entries:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != tuple(expected):
                    raise PreviewError('preview directory changed; replacement and ownership retained')
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.name, dir_fd=root_fd)
                else:
                    os.unlink(entry.name, dir_fd=root_fd)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != tuple(expected):
            raise PreviewError('preview directory changed; replacement and ownership retained')
        os.rmdir(path.name, dir_fd=parent_fd)
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def fixture_module(base: Path):
    spec = importlib.util.spec_from_file_location('preview_fixture', base / 'gui_experiment.py')
    if spec is None or spec.loader is None:
        raise PreviewError('captured fixture helper is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    # Local Git objects only; no user hooks, credential prompts, or network fetch.
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
                            capture_output=True, timeout=30, check=True,
                            env={'PATH': os.defpath, 'GIT_TERMINAL_PROMPT': '0'})
    return result.stdout.decode().strip()


def install(source_repo: Path, ref: str, destination: Path,
            project_repo: Path | None = None, project_ref: str = 'HEAD') -> dict:
    preflight()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise PreviewError('installation destination must be new with an existing parent')
    if destination.parent.resolve() != destination.parent:
        raise PreviewError('installation parent must not be aliased')
    source_repo = source_repo.resolve(strict=True)
    sha = git(source_repo, 'rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}')
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise PreviewError('exact committed source is required')
    # Build from the selected commit, not the launcher's mutable worktree.
    destination.mkdir(mode=0o700)
    allocation = destination.stat()
    try:
        source = destination / 'source.git'
        source.mkdir(mode=0o700)
        git(source, 'init', '--bare', '--quiet')
        git(source, 'fetch', '--quiet', '--no-tags', '--depth=1', str(source_repo), sha)
        helpers = destination / 'helpers'
        helpers.mkdir(mode=0o700)
        for name in HELPERS:
            data = subprocess.run(['git', '-C', str(source), 'show', f'{sha}:maintainer/{name}'],
                                  capture_output=True, timeout=10, check=True,
                                  env={'PATH': os.defpath}).stdout
            if len(data) > 256 * 1024:
                raise PreviewError('captured helper is oversized')
            (helpers / name).write_bytes(data)
            (helpers / name).chmod(0o600)
        fx = fixture_module(helpers)
        _, engine = fx.archive(source, sha)
        manifest: dict[str, Any] = {
            'format': 1, 'source_sha': sha, 'engine_sha256': hashlib.sha256(engine).hexdigest(),
            'helpers': {name: digest(helpers / name) for name in HELPERS},
            'python': str(Path(sys.executable).absolute()), 'lifetime_seconds': 1800,
            'project': None, 'sessions': [], 'active': None,
            'evaluation_only': True, 'restart_discards_state': True,
        }
        if project_repo is not None:
            info, raw = fx.committed_project(project_repo.resolve(strict=True), project_ref)
            (destination / 'project.tar').write_bytes(raw)
            (destination / 'project.tar').chmod(0o600)
            manifest['project'] = info
        (destination / 'runs').mkdir(mode=0o700)
        info = destination.stat()
        manifest['directory_identity'] = [info.st_dev, info.st_ino]
        fx.write_json(destination / 'install.json', manifest)
        return {'installed': True, 'source_sha': sha, 'package': str(destination),
                'project': manifest['project'], 'native_started': False,
                'command': f'{sys.executable} {helpers / "gui_preview.py"} status --package {destination}'}
    except Exception:
        remove_owned_directory(destination, (allocation.st_dev, allocation.st_ino))
        raise


def load(package: Path):
    package = package.absolute()
    private_dir(package)
    private_dir(package / 'helpers')
    private_dir(package / 'runs')
    private_dir(package / 'source.git')
    # Do not execute changed helper code before validating every pinned byte.
    manifest_path = package / 'install.json'
    info = manifest_path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
        raise PreviewError('preview manifest is not a bounded private regular file')
    manifest = json.loads(manifest_path.read_text())
    own = package.stat()
    if (manifest.get('format') != 1 or manifest.get('directory_identity') != [own.st_dev, own.st_ino]
            or manifest.get('lifetime_seconds') != 1800 or manifest.get('evaluation_only') is not True):
        raise PreviewError('preview installation identity differs')
    sha = manifest['source_sha']
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise PreviewError('invalid source pin')
    for name in HELPERS:
        path = package / 'helpers' / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
            raise PreviewError('captured helper was replaced')
        captured = subprocess.run(['git', '-C', str(package / 'source.git'), 'show',
                                   f'{sha}:maintainer/{name}'], capture_output=True, timeout=10,
                                  check=True, env={'PATH': os.defpath}).stdout
        if hashlib.sha256(captured).hexdigest() != manifest['helpers'].get(name) or digest(path) != manifest['helpers'][name]:
            raise PreviewError('captured helper changed')
    fx = fixture_module(package / 'helpers')
    _, engine = fx.archive(package / 'source.git', sha)
    if hashlib.sha256(engine).hexdigest() != manifest['engine_sha256']:
        raise PreviewError('captured source archive changed')
    sessions = manifest.get('sessions')
    if (not isinstance(sessions, list) or len(sessions) > MAX_SESSIONS
            or sessions != [str(i) for i in range(1, len(sessions) + 1)]
            or manifest.get('active') not in [None, *sessions]):
        raise PreviewError('preview session registry differs')
    if manifest.get('project') is not None:
        fx.validate_project_info(manifest['project'])
        path = package / 'project.tar'
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > fx.MAX_PROJECT_BYTES:
            raise PreviewError('captured project archive changed')
        raw = path.read_bytes()
        if digest(path) != manifest['project']['archive_sha256']:
            raise PreviewError('captured project digest changed')
        fx.validate_project(raw)
    return package, manifest, fx


@contextlib.contextmanager
def locked(package: Path):
    private_dir(package.absolute())
    fd = os.open(package.absolute() / 'preview.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PreviewError('preview lifecycle operation already in progress') from None
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def receipt_record(package: Path, manifest: dict, fx, session: str) -> tuple[Path, dict]:
    receipt = package / 'runs' / f'{session}.json'
    record = fx.verify_receipt(receipt)
    if (record['source_sha'] != manifest['source_sha']
            or record['archive_sha256'] != manifest['engine_sha256']
            or record['bootstrap_sha256'] != manifest['helpers']['gui_experiment.py']
            or Path(record['root']).parent != package / 'runs'
            or record['runtime'] != 'experimental'
            or record.get('project') != manifest['project']
            or record.get('native_harnesses') != ['codex']
            or record.get('preview') is not True):
        raise PreviewError('preview receipt belongs to another installation')
    return receipt, record


def summary(package: Path, manifest: dict, fx) -> dict:
    result = {'source_sha': manifest['source_sha'], 'state': 'installed', 'url': None,
              'lifetime_seconds': 1800, 'native_roots_limit_per_harness': 2,
              'restart_discards_state': True, 'project': manifest['project'],
              'support': {'codex': 'normal-GUI-core-demonstrated-capabilities-check-at-start',
                          'claude': 'setup-and-cold-check-required',
                          'controls': 'capability-specific-may-be-unavailable',
                          'sprints': 'unavailable-in-this-preview',
                          'history': 'acceptance-pending'}, 'cleanup_complete': None}
    if manifest['active'] is not None:
        _, record = receipt_record(package, manifest, fx, manifest['active'])
        expired = time.time() >= record['expires_at']
        state = fx.unit_state(record, timeout=2)
        alive = fx.owned_unit(record, state) and state.get('ActiveState') == 'active'
        phase = record['status']
        if phase != 'stopped' and (expired or not alive):
            phase = 'expired-cleanup-required' if expired else 'inactive-cleanup-required'
        result.update(state=phase,
                      url=record.get('url'), dev_port=record['port'] + 1 if record['port'] < 65535 else 65534,
                      seconds_remaining=max(0, int(record['expires_at'] - time.time())),
                      workspace=record['root'], cleanup_complete=record.get('cleanup', {}).get('complete') is True)
        if manifest['project'] is not None:
            result['project_workspace'] = str(Path(record['root']) / 'preview-project')
            result['chat_project_relative'] = 'preview-project/'
    return result


def operate(package: Path, action: str, port: int | None = None) -> dict:
    with locked(package):
        package, manifest, fx = load(package)
        if action == 'status':
            return summary(package, manifest, fx)
        if action in {'stop', 'restart', 'remove'}:
            # Retain package/receipts on any unknown cleanup. No fresh start,
            # removal, or admission after a partial stop.
            for session in manifest['sessions']:
                receipt, _ = receipt_record(package, manifest, fx, session)
                record = fx.stop(receipt)
                if record.get('cleanup', {}).get('complete') is not True:
                    raise PreviewError('cleanup remains unverified; installation retained')
            if action == 'remove':
                result = {'removed': True, 'cleanup_complete': True, 'source_sha': manifest['source_sha']}
                # Verify/delete the exact original package before claiming its
                # removal. An owner replacement during stop remains untouched.
                remove_owned_directory(package, manifest['directory_identity'])
                fx.write_json(package.with_name(package.name + '-cleanup.json'), result)
                return result
            if action == 'stop':
                return summary(package, manifest, fx)
        if action not in {'start', 'restart'}:
            raise PreviewError('unknown preview lifecycle action')
        preflight()
        if action == 'start' and manifest['active'] is not None:
            _, record = receipt_record(package, manifest, fx, manifest['active'])
            if record.get('cleanup', {}).get('complete') is not True:
                raise PreviewError('existing session retained; use stop before a fresh start')
        if len(manifest['sessions']) >= MAX_SESSIONS:
            raise PreviewError('evaluation session bound reached; remove and reinstall after cleanup')
        selected_port = preview_ports(port)
        session = str(len(manifest['sessions']) + 1)
        receipt = package / 'runs' / f'{session}.json'
        manifest['sessions'].append(session)
        manifest['active'] = session
        # Reserve the fixed receipt before launch. Failure retains cleanup ownership.
        fx.write_json(package / 'install.json', manifest)
        project = None if manifest['project'] is None else (manifest['project'], (package / 'project.tar').read_bytes())
        try:
            fx.start(package / 'source.git', manifest['source_sha'], receipt,
                     temp_parent=package / 'runs', runtime='experimental', port=selected_port,
                     lifetime=1800, project=project, native_harnesses=('codex',), preview=True)
        except Exception:
            # Preallocation failures are safe to roll back only when no receipt
            # exists; ambiguous allocated failures remain available to stop.
            if not receipt.exists() and not receipt.is_symlink():
                manifest['sessions'].pop()
                manifest['active'] = manifest['sessions'][-1] if manifest['sessions'] else None
                fx.write_json(package / 'install.json', manifest)
            raise
        return summary(package, manifest, fx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('install')
    setup.add_argument('--source-repo', type=Path, required=True)
    setup.add_argument('--ref', required=True)
    setup.add_argument('--package', type=Path, required=True)
    setup.add_argument('--project-repo', type=Path)
    setup.add_argument('--project-ref', default='HEAD')
    for action in ('start', 'status', 'stop', 'restart', 'remove'):
        command = sub.add_parser(action)
        command.add_argument('--package', type=Path, required=True)
        if action in {'start', 'restart'}:
            command.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == 'install':
            result = install(args.source_repo, args.ref, args.package, args.project_repo, args.project_ref)
        else:
            result = operate(args.package, args.action, getattr(args, 'port', None))
        print(json.dumps(result, sort_keys=True))
        return 0
    except PreviewError as exc:
        print(json.dumps({'state': 'unavailable', 'error': 'PREVIEW_OPERATION_REFUSED',
                          'detail': str(exc), 'cleanup': 'retained-if-allocated'}), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - private fixture/OS errors never dump ledger/native state
        print(json.dumps({'state': 'unavailable', 'cleanup': 'retained-if-allocated',
                          'error': 'PREVIEW_OPERATION_REFUSED'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
