"""F81 cleanup uses disposable homes only; native acceptance is separate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
import global_pointer as cleanup


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.target = self.home / '.codex/AGENTS.md'
        self.target.parent.mkdir()
        self.backup = self.target.with_name('AGENTS.md.pre-sc.bak')
        self.catalogue = json.loads(cleanup.CATALOGUE.read_text())
        self.known = cleanup._known(self.catalogue)
        self.pointer = self.catalogue['templates'][-1]['content'].encode()

    def run_cleanup(self, apply=True, **kwargs):
        return cleanup.reconcile(home=self.home, environ={}, report=False, apply=apply, **kwargs)

    def result(self, apply=True):
        return next(r for r in self.run_cleanup(apply) if r.path == self.target)

    def test_all_shipped_templates_and_line_endings_remove_or_restore(self):
        self.assertEqual(len(self.known), 6)
        for pointer in self.known:
            for restore in (False, True):
                with self.subTest(pointer=pointer[:60], restore=restore):
                    self.backup.unlink(missing_ok=True)
                    self.target.write_bytes(pointer)
                    if restore:
                        self.backup.write_bytes(b'private user instructions\x00\xff')
                        self.backup.chmod(0o600)
                    self.assertEqual(self.result(False).status, 'restorable' if restore else 'removable')
                    self.assertEqual(self.target.read_bytes(), pointer)
                    self.assertEqual(self.result().status, 'clean')
                    if restore:
                        self.assertEqual(self.target.read_bytes(), self.backup.read_bytes())
                        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(self.result().status, 'preserved-user-content')
                    else:
                        self.assertFalse(self.target.exists())
                        self.assertEqual(self.result().status, 'clean')

    def test_missing_target_never_restores_backup_or_creates_directories(self):
        self.backup.write_bytes(b'user')
        self.assertEqual(self.result().status, 'clean')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.backup.read_bytes(), b'user')
        self.assertFalse((self.home / '.claude').exists())
        self.assertFalse((self.home / '.config').exists())

    def test_user_content_and_edited_pointer_preserved(self):
        for content, status in ((b'user mentions Subfloor', 'preserved-user-content'),
                                (self.pointer + b'\nuser edits', 'unresolved')):
            self.target.write_bytes(content)
            self.backup.write_bytes(b'keep backup')
            self.assertEqual(self.result().status, status)
            self.assertEqual(self.target.read_bytes(), content)
            self.assertEqual(self.backup.read_bytes(), b'keep backup')

    def test_ambiguous_backup_and_permissions_are_preserved(self):
        for content in (self.pointer, self.pointer + b'edited'):
            self.target.write_bytes(self.pointer)
            self.backup.write_bytes(content)
            self.assertEqual(self.result().status, 'unresolved')
            self.assertEqual(self.target.read_bytes(), self.pointer)
            self.assertEqual(self.backup.read_bytes(), content)
        self.backup.chmod(0)
        self.assertEqual(self.result().status, 'unresolved')
        self.backup.chmod(0o600)
        self.backup.unlink()
        self.target.chmod(0)
        self.assertEqual(self.result().status, 'unresolved')
        self.target.chmod(0o600)

    def test_symlink_and_nonregular_target_or_backup(self):
        other = self.home / 'other'
        other.write_bytes(self.pointer)
        for path in (self.target, self.backup):
            for kind in ('symlink', 'directory', 'fifo'):
                self.target.write_bytes(self.pointer)
                path.unlink(missing_ok=True)
                if kind == 'symlink':
                    path.symlink_to(other)
                elif kind == 'directory':
                    path.mkdir()
                else:
                    os.mkfifo(path)
                self.assertEqual(self.result().status, 'unresolved')
                self.assertEqual(other.read_bytes(), self.pointer)
                if path == self.backup:
                    self.assertEqual(self.target.read_bytes(), self.pointer)
                if kind == 'directory':
                    path.rmdir()
                else:
                    path.unlink()

    def test_symlink_ancestor_never_followed_even_when_target_absent(self):
        self.target.parent.rmdir()
        real = self.home / 'real'
        real.mkdir()
        self.target.parent.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.result().status, 'unresolved')
        (real / self.target.name).write_bytes(self.pointer)
        self.assertEqual(self.result().status, 'unresolved')
        self.assertEqual((real / self.target.name).read_bytes(), self.pointer)

    def test_configured_and_explicit_roots_include_defaults_and_deduplicate(self):
        env = {'CODEX_HOME': str(self.target.parent), 'CLAUDE_CONFIG_DIR': str(self.home / 'claude-custom'),
               'XDG_CONFIG_HOME': str(self.home / 'xdg'), 'OPENCODE_CONFIG_DIR': str(self.home / 'oc-custom')}
        legacy = self.home / 'legacy'
        roots = (f'codex={legacy}', f'codex={legacy}', f'codex={self.target.parent}')
        results = cleanup.reconcile(home=self.home, environ=env, config_roots=roots, report=False)
        paths = [r.path for r in results]
        self.assertEqual(len(paths), 7)
        self.assertEqual(paths.count(self.target), 1)
        self.assertIn(self.home / '.claude/CLAUDE.md', paths)
        self.assertIn(self.home / '.config/opencode/AGENTS.md', paths)
        self.assertIn(legacy / 'AGENTS.md', paths)
        self.assertFalse(legacy.exists())

    def test_legacy_effective_codex_home_expansion(self):
        for raw, root in (('~/custom', self.home / 'custom'),
                          ('relative-config', self.home / 'relative-config')):
            root.mkdir()
            target = root / 'AGENTS.md'
            target.write_bytes(self.pointer)
            with mock.patch.object(cleanup.Path, 'cwd', return_value=self.home):
                results = cleanup.reconcile(home=self.home, environ={'CODEX_HOME': raw}, report=False)
            self.assertEqual(next(r.status for r in results if r.path == target), 'clean')
            self.assertFalse(target.exists())

    def test_invalid_roots_are_truthful_and_do_not_touch_targets(self):
        self.target.write_bytes(self.pointer)
        for root in ('codex=relative', 'bogus=/tmp', f'codex={self.home}/../bad'):
            self.assertEqual(self.run_cleanup(apply=False, config_roots=(root,))[0].status, 'unresolved')
            self.assertEqual(self.target.read_bytes(), self.pointer)

    def test_sandbox_never_inspects_mounted_host(self):
        self.target.write_bytes(self.pointer)
        for variable in ('IS_SANDBOX', 'SC_SANDBOX'):
            with mock.patch.object(cleanup, '_reconcile') as inspect:
                results = cleanup.reconcile(home=self.home, environ={variable: '1'}, report=False)
            inspect.assert_not_called()
            self.assertEqual(results[0].status, 'unresolved')
        self.assertEqual(self.target.read_bytes(), self.pointer)

    def test_user_change_before_replacement_preserved(self):
        for change_backup in (False, True):
            self.target.write_bytes(self.pointer)
            self.backup.write_bytes(b'original')
            validate = cleanup._validate
            def edit_then_validate(*args, change_backup=change_backup, validate=validate):
                (self.backup if change_backup else self.target).write_bytes(b'user edit')
                return validate(*args)
            with mock.patch.object(cleanup, '_validate', side_effect=edit_then_validate):
                result = self.result()
            self.assertEqual(result.status, 'unresolved')
            self.assertIn('changed-during-cleanup', result.reason)
            self.assertEqual((self.backup if change_backup else self.target).read_bytes(), b'user edit')
            self.assertEqual(list(self.target.parent.glob('.sc-harness-cleanup-*')), [])

    def test_changed_parent_is_preserved(self):
        self.target.write_bytes(self.pointer)
        original = self.target.parent
        moved = self.home / 'moved'
        validate = cleanup._validate
        def move_then_validate(*args):
            original.rename(moved)
            original.mkdir()
            self.target.write_bytes(b'user edit')
            return validate(*args)
        with mock.patch.object(cleanup, '_validate', side_effect=move_then_validate):
            result = self.result()
        self.assertEqual(result.status, 'unresolved')
        self.assertEqual(self.target.read_bytes(), b'user edit')
        self.assertEqual((moved / 'AGENTS.md').read_bytes(), self.pointer)

    def test_interruption_before_and_after_replace_converges(self):
        self.target.write_bytes(self.pointer)
        self.backup.write_bytes(b'private')
        with mock.patch.object(cleanup.os, 'replace', side_effect=OSError('interrupted')):
            self.assertEqual(self.result().status, 'unresolved')
        self.assertEqual(self.target.read_bytes(), self.pointer)
        self.assertEqual(self.backup.read_bytes(), b'private')
        replace = os.replace
        def replace_then_interrupt(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('interrupted after replacement')
        with mock.patch.object(cleanup.os, 'replace', side_effect=replace_then_interrupt):
            self.assertEqual(self.result().status, 'unresolved')
        self.assertEqual(self.result().status, 'preserved-user-content')
        self.assertEqual(self.target.read_bytes(), b'private')
        self.assertEqual(self.backup.read_bytes(), b'private')

    def test_concurrent_processes_share_directory_lock(self):
        self.target.write_bytes(self.pointer)
        self.backup.write_bytes(b'user')
        env = {**os.environ, 'HOME': str(self.home)}
        for key in ('SC_SANDBOX', 'IS_SANDBOX', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME'):
            env.pop(key, None)
        commands = [subprocess.Popen([sys.executable, cleanup.__file__, '--apply'], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for process in commands:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(self.target.read_bytes(), b'user')
        self.assertEqual(self.backup.read_bytes(), b'user')

    def test_cli_defaults_to_check_and_reports_partial_failure_without_api(self):
        self.target.write_bytes(self.pointer)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'SC_API_URL': 'http://127.0.0.1:1'}
        command = [sys.executable, cleanup.__file__]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('removable', result.stdout)
        self.assertEqual(self.target.read_bytes(), self.pointer)
        edited = self.home / '.claude/CLAUDE.md'
        edited.parent.mkdir()
        edited.write_bytes(self.pointer + b'edit')
        result = subprocess.run(command + ['--apply'], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(edited), result.stdout)
        self.assertIn('incomplete', result.stdout)
        self.assertFalse(self.target.exists())
        self.assertEqual(edited.read_bytes(), self.pointer + b'edit')

    def test_dispatcher_without_api_database_or_identity(self):
        checkout = self.home / 'source'
        scripts = checkout / '.super-coder/scripts'
        scripts.mkdir(parents=True)
        assets = scripts.parent / 'assets'
        assets.mkdir()
        shutil.copy2(ROOT / 'sc', checkout / 'sc')
        for name in ('dispatch.sh', 'global_pointer.py'):
            shutil.copy2(ROOT / '.super-coder/scripts' / name, scripts / name)
        shutil.copy2(cleanup.CATALOGUE, assets / cleanup.CATALOGUE.name)
        self.target.write_bytes(self.pointer)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'SC_PYTHON': sys.executable}
        result = subprocess.run([str(checkout / 'sc'), 'harness-cleanup', '--apply'],
                                cwd=checkout, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.target.exists())
        self.assertFalse((checkout / '.sc-state').exists())
        dispatch = scripts / 'dispatch.sh'
        text = dispatch.read_text()
        boundary = 'sc_host_runtime() { [ "$(sc_runtime)" = host ]; }'
        self.assertIn(boundary, text)
        dispatch.write_text(text.replace(boundary, 'sc_host_runtime() { exit 23; }'))
        for command in ('launch', 'enter', 'enter-dev'):
            self.target.write_bytes(self.pointer)
            result = subprocess.run([str(checkout / 'sc'), command], cwd=checkout,
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            self.assertFalse(self.target.exists(), command)
        self.target.write_bytes(self.pointer)
        result = subprocess.run([str(checkout / 'sc'), 'launch', '--help'], cwd=checkout,
                                env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), self.pointer)

    def test_repeated_ensure_and_refresh_use_cleanup_without_recreation(self):
        import install

        self.target.write_bytes(self.pointer)
        reconcile = cleanup.reconcile
        def isolated_cleanup():
            return reconcile(home=self.home, environ={}, report=False)
        with mock.patch.object(cleanup, 'reconcile', side_effect=isolated_cleanup), \
                mock.patch.object(install, '_harness_installed', return_value=True), \
                mock.patch.object(install, '_run_harness_install', return_value=(0, '', 0)), \
                mock.patch.object(install.shutil, 'which', return_value='/mock/curl'):
            install.ensure_harnesses()
            self.assertFalse(self.target.exists())
            install.update_harnesses()
            self.assertFalse(self.target.exists())
            self.backup.write_bytes(b'user backup')
            self.target.write_bytes(b'user instructions')
            install.ensure_harnesses()
            install.update_harnesses()
        self.assertEqual(self.target.read_bytes(), b'user instructions')
        self.assertEqual(self.backup.read_bytes(), b'user backup')

    def test_no_active_global_declarations_or_writer_calls(self):
        for path in (ROOT / '.super-coder/adapters').glob('*/adapter.json'):
            self.assertNotIn('global_pointer', json.loads(path.read_text()))
        for name in ('install.py', 'run.py'):
            text = (ROOT / '.super-coder/scripts' / name).read_text()
            self.assertNotIn('write_global_pointers', text)
            self.assertIn('global_pointer.reconcile()', text)


if __name__ == '__main__':
    unittest.main()
