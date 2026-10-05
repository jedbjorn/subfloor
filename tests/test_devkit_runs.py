"""A9: real dispatcher, pytest selections, API ledger and detached supervisor."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))

import devkit
import devkit_receipts
import job
import runs
from test_job import build_db
from test_runs import ENGINE, ApiFixture


class DevkitRunsTest(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'test.db'
        build_db(str(self.db_path))
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO shells(shell_id,display_name,shortname,system_prompt,user_id,api_key) VALUES(2,'Other','other','x',1,'other-token')")
        self.con.commit()
        self.start_api()
        self.checkout = self.root / 'checkout with spaces'
        self.checkout.mkdir()
        scripts = self.checkout / '.super-coder/scripts'
        shutil.copytree(ENGINE / 'scripts', scripts, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ENGINE.parent / 'sc', self.checkout / 'sc')
        (self.checkout / '.subfloor').mkdir()
        runner = self.checkout / '.subfloor/runner'
        runner.write_text(f'#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, \"-m\", \"pytest\", *sys.argv[1:]])\n')
        runner.chmod(0o755)
        (self.checkout / '.subfloor/dev-kit.json').write_text(json.dumps({
            'version': 1, 'hooks': {name: {'argv': ['./.subfloor/runner']}
                                  for name in ('test', 'lint', 'typecheck', 'deps')}}))
        (self.checkout / 'test_pass.py').write_text('import pytest\ndef test_good(): pass\n@pytest.mark.skip\ndef test_skip(): pass\n')
        (self.checkout / 'test_fail.py').write_text('def test_bad(): assert False\n')
        (self.checkout / '.gitignore').write_text('.sc-state/\n.super-coder/\n__pycache__/\n.pytest_cache/\n')
        for args in (('init', '-q'), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')):
            subprocess.run(['git', '-C', str(self.checkout), *args], check=True, capture_output=True)
        self.env.update(SC_PYTHON=sys.executable, SC_SHELL_FLAVOR='dev', PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
        for key in ('SC_DEVKIT_RUN_ID', 'SC_DISPATCH', 'SC_CALLER_ROOT', 'SC_DEVKIT_OUTPUT'):
            self.env.pop(key, None)

    def hook(self, selection, seat='tui', **extra):
        return subprocess.run(['./sc', 'test', selection, '-q'], cwd=self.checkout,
                              env={**self.env, 'SC_SEAT': seat, **extra},
                              capture_output=True, text=True, timeout=20, check=False)

    def latest(self):
        return job._api('GET', '/_sc/runs')['runs'][0]

    def test_a9_real_passing_and_failing_selections_in_both_seats(self):
        for seat in ('tui', 'gui'):
            for selection, code in (('test_pass.py', 0), ('test_fail.py', 1)):
                with self.subTest(seat=seat, selection=selection):
                    before = self.con.execute('SELECT count(*) FROM runs').fetchone()[0]
                    done = self.hook(selection, seat)
                    self.assertEqual(done.returncode, 0 if seat == 'gui' else code, done.stderr)
                    self.assertEqual('job:' in done.stdout, seat == 'gui', done.stdout)
                    row = self.finished(self.latest())
                    self.assertEqual(row['kind'], 'devkit')
                    self.assertEqual(row['exit_code'], code)
                    receipt = row['receipt_json']
                    self.assertIsNotNone(receipt, row)
                    self.assertEqual(receipt['argv'], ['./.subfloor/runner', selection, '-q'])
                    self.assertEqual(receipt['commit'], row['commit'])
                    self.assertEqual(receipt['checkout'], str(self.checkout))
                    self.assertEqual(receipt['seat'], 'host')
                    self.assertEqual(receipt['exit_status'], code)
                    self.assertEqual(receipt['summary']['failed'], code)
                    self.assertEqual(receipt['summary']['passed'], 1 - code)
                    self.assertEqual(receipt['summary']['skipped'], 1 - code)
                    if code:
                        self.assertEqual(receipt['summary']['failing'], ['test_fail.py::test_bad'])
                    log = self.checkout / receipt['log']
                    self.assertIn('failed' if code else 'passed', log.read_text())
                    self.assertEqual(json.loads(log.with_suffix('.receipt.json').read_text()), receipt)
                    self.assertEqual(self.con.execute('SELECT count(*) FROM runs').fetchone()[0], before + 1)
                    self.assertEqual(self.con.execute('SELECT count(*) FROM wake_message WHERE message_id=?', (row['message_id'],)).fetchone()[0], 1)

    def test_full_output_and_admin_foreground(self):
        full = self.hook('test_fail.py', SC_DEVKIT_OUTPUT='full')
        self.assertEqual(full.returncode, 1, full.stderr)
        self.assertIn('1 failed', full.stdout)
        self.assertEqual(self.latest()['receipt_json']['summary']['failed'], 1)
        count = len(job._api('GET', '/_sc/runs')['runs'])
        admin = self.hook('test_pass.py', 'gui', SC_SHELL_FLAVOR='admin')
        self.assertEqual(admin.returncode, 0, admin.stderr)
        self.assertNotIn('job:', admin.stdout)
        self.assertEqual(len(job._api('GET', '/_sc/runs')['runs']), count)

    def test_registration_refusal_launches_nothing(self):
        marker = self.checkout / 'executed'
        (self.checkout / 'test_marker.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_ok(): pass\n')
        for seat in ('tui', 'gui'):
            done = self.hook('test_marker.py', seat, SC_API_TOKEN='foreign-token')
            self.assertNotEqual(done.returncode, 0)
            self.assertFalse(marker.exists())
        self.assertEqual(job._api('GET', '/_sc/runs')['runs'], [])

    def test_receipt_owner_conflicts_recovery_and_pruning(self):
        self.assertEqual(self.hook('test_pass.py').returncode, 0)
        row = self.latest()
        receipt = row['receipt_json']
        route = f"/_sc/runs/{row['run_id']}/receipt"
        self.assertEqual(job._api('POST', route, receipt)['receipt_json'], receipt)
        for token, payload, status in (('foreign-token', receipt, 401), ('other-token', receipt, 403),
                                      (self.env['SC_API_TOKEN'], {**receipt, 'exit_status': 5}, 409)):
            with self.subTest(status=status), self.assertRaises(urllib.error.HTTPError) as caught:
                devkit_receipts.api({**self.env, 'SC_API_TOKEN': token}, 'POST', route, payload)
            self.assertEqual(caught.exception.code, status)
        log = self.checkout / receipt['log']
        with mock.patch.object(devkit, 'LOG_RETENTION', 0):
            devkit._prune_logs(log.parent, {**self.env, 'SC_API_BASE': 'http://127.0.0.1:1'})
            self.assertTrue(log.exists())
            self.assertTrue(log.with_suffix('.receipt.json').exists())
            devkit._prune_logs(log.parent, self.env)
        self.assertFalse(log.exists())
        self.assertFalse(log.with_suffix('.receipt.json').exists())
        retained = self.latest()
        self.assertEqual(retained['receipt_json'], receipt)
        self.assertEqual(retained['evidence_pruned'], 1)

    def test_reconciler_recovers_receipt_before_terminal_wake(self):
        row = self.register(kind='devkit')
        directory = Path(row['evidence_path']).parent
        directory.mkdir(parents=True)
        receipt = {'hook': 'test', 'argv': ['pytest'], 'checkout': str(self.checkout),
                   'commit': None, 'branch': None, 'seat': 'host', 'exit_status': 0,
                   'duration_s': 1, 'summary': None, 'log': '.sc-state/local/selection.log',
                   'run_id': row['run_id']}
        devkit_receipts.atomic_json(directory / 'receipt.json', receipt)
        job.write_meta(directory, {'run_id': row['run_id'], 'finished_at': job._now(), 'exit_code': 0})
        runs.RunStore(self.con, self.engine).reconcile()
        recovered = self.latest()
        self.assertEqual(recovered['state'], 'done')
        self.assertEqual(recovered['receipt_json'], receipt)
        self.assertIsNotNone(recovered['message_id'])

    def test_no_summary_and_wrapper_marker_is_scrubbed(self):
        script = self.checkout / 'plain.py'
        script.write_text(f'#!{sys.executable}\nimport os\nassert "SC_DEVKIT_RUN_ID" not in os.environ\nprint("2 passed progress only")\n')
        script.chmod(0o755)
        declaration = {'version': 1, 'hooks': {'test': {'argv': ['./plain.py']}}}
        (self.checkout / '.subfloor/dev-kit.json').write_text(json.dumps(declaration))
        done = self.hook('literal; $(touch never)', 'gui')
        self.assertEqual(done.returncode, 0, done.stderr)
        row = self.finished(self.latest())
        self.assertEqual(row['exit_code'], 0)
        self.assertIsNone(row['receipt_json']['summary'])
        self.assertEqual(row['receipt_json']['argv'][-2:], ['literal; $(touch never)', '-q'])
        self.assertFalse((self.checkout / 'never').exists())


class PytestSummaryTest(unittest.TestCase):
    def test_errors_ansi_subtests_and_missing_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'log'
            for text, expected in (
                ('FAILED tests/a.py::test_fail - AssertionError\nERROR tests/b.py::test_error - bad\n\x1b[31m=== 2 failed, 5 passed, 3 skipped, 1 error, 6 subtests passed in 0.50s ===\x1b[0m\n', (5, 2, 1, 3)),
                ('progress: 500 passed\nprocess killed\n', None),
                ('no tests ran in 0.01s\n', None),
            ):
                log.write_text(text)
                summary = devkit_receipts.pytest_summary(log)
                if expected is None:
                    self.assertIsNone(summary)
                else:
                    self.assertEqual(tuple(summary[k] for k in ('passed', 'failed', 'errors', 'skipped')), expected)
                    self.assertEqual(summary['failing'], ['tests/a.py::test_fail', 'tests/b.py::test_error'])

    def test_reseed_upgrades_untouched_starter_and_preserves_custom_skill(self):
        desired = (ENGINE / 'assets/seed/skills/dev_kit/SKILL.md').read_text().split('---', 2)[2].strip()
        for custom in (False, True):
            with self.subTest(custom=custom), sqlite3.connect(':memory:') as con:
                con.executescript((ENGINE / 'schema.sql').read_text())
                for migration in sorted((ENGINE / 'migrations').glob('*.sql')):
                    if migration.name >= '0276':
                        break
                    con.executescript(migration.read_text())
                if custom:
                    con.execute("UPDATE skills SET content='fork-owned custom body' WHERE name='dev_kit'")
                    con.commit()
                con.executescript((ENGINE / 'migrations/0276_devkit_run_receipts.sql').read_text())
                actual = con.execute("SELECT content FROM skills WHERE name='dev_kit'").fetchone()[0]
                self.assertEqual(actual, 'fork-owned custom body' if custom else desired)
