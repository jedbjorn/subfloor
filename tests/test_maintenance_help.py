"""Maintenance argument parsing must exit before reaching mutation boundaries."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.super-coder/scripts'))
import seed_skills
import update


class MaintenanceHelpTest(unittest.TestCase):
    def test_update_help_and_invalid_arguments_do_not_reconcile(self):
        for argv, code in [(['--help'], 0), (['-h'], 0), (['--unknown'], 2),
                           (['--ref'], 2), (['--branch'], 2),
                           (['--branch', 'main', '--ref', 'HEAD'], 2)]:
            with self.subTest(argv=argv), mock.patch.object(update, 'run_update_compat') as run:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        update.main(argv)
                self.assertEqual(caught.exception.code, code)
                run.assert_not_called()

    def test_seed_help_and_invalid_arguments_do_not_open_state_or_write(self):
        for argv, code in [(['--help'], 0), (['-h'], 0), (['--unknown'], 2), (['extra'], 2)]:
            with self.subTest(argv=argv), mock.patch.object(seed_skills.instance_state, 'active_database_path') as state:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        seed_skills.main(argv)
                self.assertEqual(caught.exception.code, code)
                state.assert_not_called()
