"""Host-only, reusable driver provisioning; no package downloads in this suite."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder/scripts"))
import db_broker_runtime as runtime


class BrokerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        config = mock.patch.object(runtime.dbq, "read", return_value={})
        config.start()
        self.addCleanup(config.stop)

    def test_first_use_installs_pinned_driver_in_separate_environment(self):
        with mock.patch.object(runtime, "ready", side_effect=[False, True]), \
             mock.patch.object(runtime.venv, "EnvBuilder") as builder, \
             mock.patch.object(runtime.subprocess, "run") as run:
            python = runtime.ensure()
        self.assertTrue(python.is_relative_to(self.tmp.name))
        builder.assert_called_once_with(with_pip=True)
        builder.return_value.create.assert_called_once_with(python.parent.parent)
        self.assertEqual(run.call_args.args[0], [str(python), "-m", "pip", "install",
                                              "--disable-pip-version-check", runtime.REQUIREMENT])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_ready_environment_is_reused_without_install(self):
        with mock.patch.object(runtime, "ready", return_value=True), \
             mock.patch.object(runtime.venv, "EnvBuilder") as builder, \
             mock.patch.object(runtime.subprocess, "run") as run:
            first = runtime.ensure()
            second = runtime.ensure()
        self.assertEqual(first, second)
        builder.assert_not_called()
        run.assert_not_called()

    def test_persisted_interpreter_override_survives_reinstallation(self):
        override = Path(self.tmp.name) / "custom/bin/python"
        with mock.patch.object(runtime.dbq, "read", return_value={"broker_python": str(override)}), \
             mock.patch.object(runtime, "ready", return_value=True), \
             mock.patch.object(runtime.venv, "EnvBuilder") as builder:
            self.assertEqual(runtime.ensure(), override)
        builder.assert_not_called()

    def test_invalid_override_is_not_modified_or_silently_replaced(self):
        with mock.patch.dict(os.environ, {"SC_DB_BROKER_PYTHON": "/missing/python"}), \
             mock.patch.object(runtime, "ready", return_value=False), \
             mock.patch.object(runtime.venv, "EnvBuilder") as builder:
            with self.assertRaisesRegex(RuntimeError, "configured broker Python"):
                runtime.ensure()
        builder.assert_not_called()

    def test_sandbox_cannot_install_driver(self):
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
            with self.assertRaisesRegex(RuntimeError, "host-only"):
                runtime.ensure()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_failed_install_is_reported(self):
        with mock.patch.object(runtime, "ready", return_value=False), \
             mock.patch.object(runtime.venv, "EnvBuilder"), \
             mock.patch.object(runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.ensure()


if __name__ == "__main__":
    unittest.main()
