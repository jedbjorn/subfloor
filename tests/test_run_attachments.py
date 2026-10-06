"""A12: real HTTP/CLI, Sprint board and Git ancestry over rebased heads."""
import json
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / '.super-coder' / p) for p in ('scripts', 'api')]

import job
import run_attachments
import sprint_board
import test_sprint_work_dispatch as sprint_fixture
from runs import RunStore
from test_job import TOKEN
from test_runs import ApiFixture


class AttachmentsTest(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.fixture = sprint_fixture.SprintWorkDispatchCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.con = self.fixture.con
        self.db_path = self.fixture.db_path
        self.root = self.db_path.parent
        self.con.execute('UPDATE shells SET api_key=? WHERE shell_id=1', (TOKEN,))
        self.con.execute("UPDATE shells SET api_key='other-token' WHERE shell_id=4")
        self.con.commit()
        self.unit = self.fixture.create_unit(developer=1)
        self.other_unit = self.fixture.create_unit(developer=4, reviewer=5)
        owner = self.con.execute('SELECT participant_id FROM sprint_participants WHERE shell_id=1').fetchone()[0]
        self.pr = self.con.execute('INSERT INTO sprint_registered_prs(sprint_id,owner_participant_id,repository,pr_number) VALUES(?,?,?,42)',
                                  (self.fixture.sprint_id, owner, 'fixture/project')).lastrowid
        self.con.execute('INSERT INTO sprint_pr_work_units(sprint_id,registered_pr_id,work_unit_id) VALUES(?,?,?)', (self.fixture.sprint_id, self.pr, self.unit))
        self.subscription = self.con.execute('INSERT INTO pr_subscriptions(owner_shell_id,repository,pr_number,sprint_registered_pr_id) VALUES(1,?,?,?)',
                                            ('fixture/project', 42, self.pr)).lastrowid
        self.con.execute("INSERT INTO pr_subscriptions(owner_shell_id,repository,pr_number) VALUES(4,'fixture/project',43)")
        self.con.commit()
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.com')
        self.git('config', 'core.hooksPath', '/dev/null')
        self.git('commit', '--allow-empty', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('commit', '--allow-empty', '-qm', 'receipt')
        self.head = self.git('rev-parse', 'HEAD')
        self.start_api()
        self.attachments = run_attachments.RunAttachments(self.con)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.checkout), *args], text=True).strip()

    def receipt(self, commit=None):
        store = RunStore(self.con, self.engine)
        row = store.register(1, {'kind': 'devkit', 'foreground': True, 'registration_key': str(self.con.execute('SELECT count(*) FROM runs').fetchone()[0]),
                                'argv': ['pytest'], 'cwd': str(self.checkout), 'commit': commit or self.head})
        store.receipt(row['run_id'], 1, {'run_id': row['run_id'], 'hook': 'test', 'argv': ['pytest'], 'commit': row['commit'],
                                      'exit_status': 0, 'duration_s': 1, 'checkout': str(self.checkout), 'log': '.sc-state/test.log',
                                      'summary': {'framework': 'pytest', 'passed': 3, 'failed': 0, 'errors': 0, 'skipped': 1, 'failing': []}})
        self.terminal(row)
        return row['run_id']

    def observe(self, head):
        for table, key, identity in (('pr_subscription_transitions', 'subscription_id', self.subscription), ('sprint_pr_transitions', 'registered_pr_id', self.pr)):
            self.con.execute(f'INSERT INTO {table}({key},normalized_state,transition_key,observed_head_sha) VALUES(?,?,?,?)',
                             (identity, 'green', head + table, head))
        self.con.commit()

    def test_current_stale_and_work_unit_receipts_follow_head(self):
        current, stale = self.receipt(self.base), self.receipt()
        for rid, args in ((current, ['--pr', '42']), (stale, ['--work-unit', str(self.unit)])):
            result = self.cli('attach', str(rid), *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['run_id'], rid)
        self.observe(self.head)
        board = sprint_board.SprintBoardProjection(self.con).board(self.fixture.sprint_id)
        unit = next(u for u in board['work_units'] if u['work_unit_id'] == self.unit)
        self.assertEqual({r['ancestry'] for r in unit['pull_requests'][0]['receipts']}, {'current'})
        # A divergent replacement head leaves the common ancestor current and the removed commit stale.
        self.git('checkout', '-q', '--detach', self.base)
        self.git('commit', '--allow-empty', '-qm', 'replacement')
        replacement = self.git('rev-parse', 'HEAD')
        self.observe(replacement)
        projected = self.attachments.prs(1)['prs'][0]
        states = {r['run_id']: r['ancestry'] for r in projected['receipts']}
        self.assertEqual(states, {current: 'current', stale: 'stale'})
        self.assertEqual(projected['normalized_state'], 'green')
        self.assertEqual(projected['receipts'][0]['summary']['passed'], 3)
        self.assertTrue(projected['receipts'][0]['log_url'].endswith('/tail'))
        self.assertEqual(run_attachments.ancestry(str(self.checkout), 'f' * 40, replacement), 'unknown')

    def test_owner_text_terminal_and_conflict_boundaries_are_audited(self):
        rid = self.receipt()
        for payload in ({'pr': 43}, {'work_unit': self.other_unit}, {'pr': 42, 'receipt': 'pasted PASS'}, {'pr': True}, {'pr': 42, 'work_unit': self.unit}):
            with self.assertRaises(urllib.error.HTTPError):
                job._api('POST', f'/_sc/runs/{rid}/attach', payload)
        other = self.cli('attach', str(rid), '--pr', '42', env={**self.env, 'SC_API_TOKEN': 'other-token'})
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM run_attachment_events WHERE outcome='refused'").fetchone()[0], 6)
        self.assertIsNone(RunStore(self.con).get(rid)['attached_pr'])
        row = self.register(registration_key='running')
        with self.assertRaises(urllib.error.HTTPError):
            job._api('POST', f"/_sc/runs/{row['run_id']}/attach", {'pr': 42})
        self.attachments.attach(rid, 1, {'pr': 42})
        self.attachments.attach(rid, 1, {'pr': 42})
        self.attachments.attach(rid, 1, {'work_unit': self.unit})
        self.assertEqual(self.con.execute("SELECT count(*) FROM run_attachment_events WHERE outcome='attached'").fetchone()[0], 2)
        other_owned = self.fixture.create_unit(developer=1)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.attachments.attach(rid, 1, {'work_unit': other_owned})
        with self.assertRaises(PermissionError):
            self.attachments.attach(rid, None, {'pr': 43})

    def test_ambiguous_pr_requires_repository_and_pruning_preserves_summary(self):
        rid = self.receipt()
        self.con.execute("INSERT INTO pr_subscriptions(owner_shell_id,repository,pr_number) VALUES(1,'fixture/other',42)")
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.attachments.attach(rid, 1, {'pr': 42})
        self.attachments.attach(rid, 1, {'pr': 42, 'repository': 'fixture/project'})
        RunStore(self.con).prune_evidence(rid, 1)
        receipt = self.attachments.pr_receipts('fixture/project', 42, self.head)[0]
        self.assertTrue(receipt['evidence_pruned'])
        self.assertEqual(receipt['summary']['passed'], 3)

    def test_pr_list_shows_open_first_and_caps_with_total(self):
        self.observe(self.head)
        for number in range(100, 100 + run_attachments.PR_LIMIT + 2):
            sub = self.con.execute("INSERT INTO pr_subscriptions(owner_shell_id,repository,pr_number) VALUES(1,'fixture/project',?)",
                                   (number,)).lastrowid
            self.con.execute("INSERT INTO pr_subscription_transitions(subscription_id,normalized_state,transition_key) "
                             "VALUES(?,'merged',?)", (sub, f'merged-{number}'))
        self.con.commit()
        listed = self.attachments.prs(1)
        # The oldest PR is the only open one, so it leads despite 22 newer merged PRs.
        self.assertEqual(listed['prs'][0]['pr_number'], 42)
        self.assertEqual(len(listed['prs']), run_attachments.PR_LIMIT)
        self.assertEqual(listed['total'], run_attachments.PR_LIMIT + 3)

    def test_receipts_and_run_list_return_newest_within_limit(self):
        rids = [self.receipt() for _ in range(run_attachments.RECEIPT_LIMIT + 2)]
        for rid in rids[:-1]:
            self.attachments.attach(rid, 1, {'pr': 42})
        self.attachments.attach(rids[-1], 1, {'work_unit': self.unit})
        receipts = self.attachments.pr_receipts('fixture/project', 42, self.head)
        self.assertEqual([r['run_id'] for r in receipts], rids[::-1][:run_attachments.RECEIPT_LIMIT])
        limited = job._api('GET', '/_sc/runs?limit=2')
        self.assertEqual([r['run_id'] for r in limited['runs']], rids[::-1][:2])
        self.assertEqual(limited['total'], len(rids))
        self.assertEqual(len(job._api('GET', '/_sc/runs')['runs']), len(rids))

    def test_reseed_is_idempotent_and_preserves_shell_identity(self):
        migration = Path(__file__).resolve().parents[1] / '.super-coder/migrations/0279_run_receipt_attachments.sql'
        self.con.execute("UPDATE shells SET system_prompt='custom\n## CODE CRAFT\nkeep me',current_state='keep state' WHERE shell_id=1")
        self.con.commit()
        self.con.executescript(migration.read_text())
        once = tuple(self.con.execute('SELECT system_prompt,current_state FROM shells WHERE shell_id=1').fetchone())
        self.con.executescript(migration.read_text())
        self.assertEqual(once, tuple(self.con.execute('SELECT system_prompt,current_state FROM shells WHERE shell_id=1').fetchone()))
        self.assertEqual(once[0].count('## RUN RECEIPTS'), 1)
        self.assertTrue(once[0].endswith('keep me'))
        self.assertEqual(once[1], 'keep state')
