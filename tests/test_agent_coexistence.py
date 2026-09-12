"""Retained F81 repository boundary; no native provider/session probes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder/scripts'))
import analytics
import run
import shell_alias
from token_parsers import in_repo


class RepositoryBoundaryTest(unittest.TestCase):
    def test_codex_trust_is_exact_and_preserves_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'codex'
            home.mkdir()
            config = home / 'config.toml'
            original = 'model = "user-model"\n[projects."/user-project"]\ntrust_level = "untrusted"\n'
            config.write_text(original)
            worktree = Path(temp) / 'installed/.sc-worktrees/dev'
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}):
                run.trust_codex_worktree(worktree)
                first = config.read_bytes()
                run.trust_codex_worktree(worktree)
            self.assertEqual(config.read_bytes(), first)
            self.assertTrue(first.startswith(original.encode()))
            parsed = tomllib.loads(first.decode())
            self.assertEqual(parsed, {'model': 'user-model', 'projects': {
                '/user-project': {'trust_level': 'untrusted'},
                str(worktree): {'trust_level': 'trusted'}}})

    def test_unrelated_checkout_and_prefix_collision_are_not_attributed(self):
        with mock.patch.object(analytics, 'REPO_ROOT', Path('/repos/application')):
            for cwd in ('/elsewhere/application', '/repos/application-copy', '/repos/application2/.sc-worktrees/dev'):
                self.assertFalse(in_repo(cwd, analytics.REPO_ROOT))
                self.assertEqual(analytics._shell_for_cwd(cwd, {'dev': 2}, [1]), [])
            self.assertTrue(in_repo('/repos/application/subdir', analytics.REPO_ROOT))
            self.assertEqual(analytics._shell_for_cwd('/repos/application', {'dev': 2}, [1]), [1])
            self.assertEqual(analytics._shell_for_cwd('/repos/application/.sc-worktrees/dev', {'dev': 2}, [1]), [2])

    def test_normal_bash_startup_has_no_identity_or_native_command_wrappers(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bin_dir = home / 'bin'
            bin_dir.mkdir()
            for name in ('sc', 'claude', 'codex', 'opencode', 'kimi', 'vibe'):
                path = bin_dir / name
                path.write_text('#!/bin/sh\necho unexpected-invocation >&2\nexit 99\n')
                path.chmod(0o700)
            env = {'HOME': temp, 'PATH': str(bin_dir) + ':' + os.environ['PATH']}
            script = shell_alias.BASH_FUNCTION + '\n' + '''
for name in claude codex opencode kimi vibe; do
  test "$(type -t "$name")" = file || exit 1
done
env
'''
            result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script],
                                    env=env, cwd=temp, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertFalse(any(line.startswith(('SC_', 'IS_SANDBOX=')) for line in result.stdout.splitlines()))

    def test_all_adapter_emission_is_repository_relative(self):
        for path in (ROOT / '.super-coder/adapters').glob('*/adapter.json'):
            adapter = json.loads(path.read_text())
            paths = [adapter['boot_artifact'], *adapter.get('emit', []),
                     *adapter.get('skill_dirs', []), *adapter.get('merge_json', {}),
                     *adapter.get('sandbox', {}).get('merge_json', {})]
            for emitted in paths:
                self.assertFalse(Path(emitted).is_absolute(), (path, emitted))
                self.assertNotIn('..', Path(emitted).parts)
            self.assertNotIn('global_pointer', adapter)
