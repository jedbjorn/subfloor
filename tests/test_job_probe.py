"""A10: real detached probe supervisors, HTTP ledger and durable terminal wakes."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / '.super-coder'
sys.path.insert(0, str(ENGINE / 'scripts'))
sys.path.insert(0, str(ENGINE / 'render'))

import compose
import job
import runs
from test_job import build_db
from test_runs import ApiFixture, wait_for


class ProbeTest(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'test.db'
        build_db(str(self.db_path))
        self.start_api()

    def start_probe(self, command, *args):
        result = self.cli('start', '--until', command, *args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = job._api('GET', '/_sc/runs')['runs'][0]
        self.assertEqual(row['kind'], 'probe')
        self.assertEqual(row['argv'], ['/bin/sh', '-c', command])
        return row

    def assert_outcome(self, row, state):
        row = self.finished(row)
        self.assertEqual(row['state'], state)
        lines = Path(row['evidence_path']).read_text().splitlines()
        attempts = [json.loads(line) for line in lines]
        self.assertTrue(attempts)
        self.assertEqual([a['number'] for a in attempts], list(range(1, len(attempts) + 1)))
        self.assertTrue(all(len(a['excerpt']) <= runs.PROBE_EXCERPT for a in attempts))
        self.assertEqual(json.loads(row['terminal_json'])['probe_attempt'], attempts[-1])
        with sqlite3.connect(self.db_path) as con:
            body = con.execute('SELECT body FROM shell_messages WHERE message_id=?',
                               (row['inbox_message_id'],)).fetchone()[0]
            self.assertEqual(con.execute('SELECT count(*) FROM wake_message WHERE message_id=?',
                                         (row['message_id'],)).fetchone()[0], 1)
        self.assertIn(f"Final probe attempt {len(attempts)}: exit={attempts[-1]['exit_code']}", body)
        self.assertIn(json.dumps(attempts[-1]['excerpt']), body)
        self.assertIn(f"sc job status {row['run_id']}", body)
        # A retry is the same outcome and wake, not a second completion.
        retry = job._api('POST', f"/_sc/runs/{row['run_id']}/terminal", json.loads(row['terminal_json']))
        self.assertEqual(retry['message_id'], row['message_id'])
        return attempts

    def test_flips_after_three_attempts_with_exact_bounded_lines(self):
        row = self.start_probe('n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); '
                               'echo "$n" > count; printf "attempt %s\\nsecond line\\n" "$n"; '
                               '[ "$n" -ge 3 ]', '--every', '5', '--timeout', '20')
        attempts = self.assert_outcome(row, 'done')
        self.assertEqual([a['exit_code'] for a in attempts], [1, 1, 0])
        self.assertIn('attempt 3\nsecond line', attempts[-1]['excerpt'])

    def test_never_success_times_out_between_attempts(self):
        row = self.start_probe('echo not-ready; exit 7', '--timeout', '1')
        attempts = self.assert_outcome(row, 'timeout')
        self.assertEqual(attempts[-1]['exit_code'], 7)

    def test_hanging_probe_times_out_and_kills_child_group(self):
        row = self.start_probe('echo waiting; sleep 60', '--timeout', '1')
        attempts = self.assert_outcome(row, 'timeout')
        self.assertEqual(attempts[-1]['exit_code'], -15)
        self.assertIn('waiting', attempts[-1]['excerpt'])

    def test_kill_during_attempt_and_between_attempts(self):
        for command in ('echo running; sleep 60', 'echo retry; exit 2'):
            row = self.start_probe(command, '--timeout', '60')
            log = Path(row['evidence_path'])
            if 'retry' in command:
                wait_for(log.read_text)
            else:
                children = Path(f"/proc/{row['pid']}/task/{row['pid']}/children")
                wait_for(lambda children=children: children.read_text().strip())
            result = self.cli('kill', str(row['run_id']))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_outcome(row, 'killed')

    def test_flood_is_drained_and_bounded_without_hiding_deadline(self):
        row = self.start_probe("yes '0123456789abcdef'", '--timeout', '1')
        attempts = self.assert_outcome(row, 'timeout')
        self.assertEqual(len(attempts[-1]['excerpt']), runs.PROBE_EXCERPT)
        self.assertLess(Path(row['evidence_path']).stat().st_size, 2048)

    def test_invalid_options_never_register_or_launch(self):
        for options in (['--until', 'touch bad', '--every', '4'],
                        ['--until', 'touch bad', '--every', '0'],
                        ['--until', 'touch bad', '--every', '-5'],
                        ['--until', 'touch bad', '--timeout', '0'],
                        ['--until', 'touch bad', '--timeout', '-1'],
                        ['--until', 'touch bad', '--every', 'nan'],
                        ['--until', 'touch bad', '--timeout', '1.5'],
                        ['--until', 'touch bad', '--every', str(sys.maxsize + 1)],
                        ['--until', 'touch bad', '--timeout', str(sys.maxsize + 1)],
                        ['--until', ''], ['--until', '   '],
                        ['--until', 'touch bad', '--', 'true'],
                        ['--every', '5', '--', 'touch', 'bad']):
            with self.subTest(options=options):
                self.assertNotEqual(self.cli('start', *options).returncode, 0)
        self.assertFalse((self.root / 'bad').exists())
        self.assertEqual(job._api('GET', '/_sc/runs')['runs'], [])

    def test_defaults_and_shell_program_preserved(self):
        command = "printf '%s\\n' 'literal $x ; spaces'"
        row = self.start_probe(command)
        self.assert_outcome(row, 'done')
        meta = json.loads(Path(row['evidence_path']).with_name('meta.json').read_text())
        self.assertEqual((meta['every'], meta['timeout']), (30, 3600))

    def test_terminal_attempt_validation_and_unicode_budget(self):
        row = self.register(kind='probe')
        payload = {'state': 'done', 'exit_code': 0, 'finished_at': job._now(),
                   'probe_attempt': {'number': 1, 'exit_code': 0, 'excerpt': '😀' * 160}}
        result = job._api('POST', f"/_sc/runs/{row['run_id']}/terminal", payload)
        self.assertLess(len(result['terminal_json']), 4096)
        for change in ({'number': True}, {'exit_code': True}, {'exit_code': 2},
                       {'excerpt': 'x' * 161}, {'excerpt': None}, {'extra': 1}):
            invalid = {**payload, 'probe_attempt': {**payload['probe_attempt'], **change}}
            with (self.subTest(change=change), self.assertRaises(ValueError),
                  sqlite3.connect(self.db_path) as con):
                runs.RunStore(con, self.engine).terminal(row['run_id'], 1, invalid)

    def test_reconciliation_preserves_final_attempt_and_wake(self):
        row = self.register(kind='probe')
        path = Path(row['evidence_path']).parent
        path.mkdir(parents=True)
        meta = {'run_id': row['run_id'], 'finished_at': job._now(), 'exit_code': 7,
                'timed_out': True,
                'probe_attempt': {'number': 2, 'exit_code': 7, 'excerpt': 'not ready'}}
        job.write_meta(path, meta)
        with sqlite3.connect(self.db_path) as con:
            store = runs.RunStore(con, self.engine)
            store.reconcile()
            first = store.get(row['run_id'])
            store.reconcile()
            self.assertEqual(store.get(row['run_id'])['message_id'], first['message_id'])
        self.assertEqual(first['state'], 'timeout')
        self.assertEqual(json.loads(first['terminal_json'])['probe_attempt'], meta['probe_attempt'])
        self.assertIsNotNone(first['wake_id'])


class ProbeGuidanceTest(unittest.TestCase):
    def test_reseed_is_idempotent_and_boot_teaches_cli_and_watcher_boundary(self):
        template = (ENGINE / 'templates/shells/dev.md').read_text()
        guidance = template.split('## BOUNDED PROBES\n', 1)[1].split('## CODE CRAFT', 1)[0]
        block = '## BOUNDED PROBES\n' + guidance
        old = 'Custom focus\n' + template.replace(block, '')
        migration = (ENGINE / 'migrations/0278_developer_bounded_probes.sql').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'test.db'
            build_db(str(db))
            with sqlite3.connect(db) as con:
                con.row_factory = sqlite3.Row
                con.execute("UPDATE shells SET flavor='dev',system_prompt=? WHERE shell_id=1", (old,))
                con.commit()
                con.executescript(migration)
                con.executescript(migration)
                shell = con.execute('SELECT * FROM shells WHERE shell_id=1').fetchone()
                self.assertEqual(shell['system_prompt'], 'Custom focus\n' + template)
                user = con.execute('SELECT * FROM users WHERE user_id=1').fetchone()
                with mock.patch.object(compose, 'open_map_ro', return_value=None):
                    boot = compose.compose_boot(con, shell, user, 'test', 1, work_dir=Path(tmp), seat='gui')
                self.assertIn(block, boot)
                self.assertIn('GitHub PR state belongs to the PR watcher', boot)
                self.assertIn('remote build finishing', boot)


if __name__ == '__main__':
    unittest.main()
