"""Healthy-runtime and offline-maintenance snapshot routing."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import snapshot  # noqa: E402
import server  # noqa: E402


class SnapshotLiveRuntimeTest(unittest.TestCase):
    def test_healthy_runtime_routes_before_requesting_maintenance(self) -> None:
        with mock.patch.dict(os.environ, {"SC_ADMIN": "1"}, clear=False), \
             mock.patch.object(snapshot.instance_state, "active_database_path"), \
             mock.patch.object(
                 snapshot, "_snapshot_via_runtime_api", return_value="snapshot: wrote live"
             ), mock.patch.object(
                 snapshot.state_relocation,
                 "exclusive_maintenance",
                 side_effect=AssertionError("healthy runtime must not be stopped"),
             ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(snapshot.main(), 0)

        self.assertIn("snapshot: wrote live", output.getvalue())

    def test_api_absence_retains_offline_exclusive_maintenance(self) -> None:
        state = mock.Mock()
        with mock.patch.dict(os.environ, {"SC_ADMIN": "1"}, clear=False), \
             mock.patch.object(snapshot.instance_state, "active_database_path"), \
             mock.patch.object(snapshot, "_snapshot_via_runtime_api", return_value=None), \
             mock.patch.object(
                 snapshot.instance_state, "maintenance_state", return_value=state
             ), mock.patch.object(
                 snapshot.state_relocation,
                 "exclusive_maintenance",
                 return_value=contextlib.nullcontext(),
             ) as lease, mock.patch.object(
                 snapshot.state_relocation, "refuse_live_database_owners"
             ), mock.patch.object(snapshot, "_main_under_lease", return_value=0):
            self.assertEqual(snapshot.main(), 0)

        lease.assert_called_once_with(state, command="snapshot")

    def test_runtime_owned_mode_is_read_only_and_never_requests_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "engine.db"
            database.touch()
            connection = mock.Mock()
            with mock.patch.dict(os.environ, {"SC_ADMIN": "1"}, clear=False), \
                 mock.patch.object(snapshot, "DB_PATH", database), \
                 mock.patch.object(snapshot.instance_state, "active_database_path"), \
                 mock.patch.object(snapshot.artifact_policy, "prepare_local_state", return_value=[]), \
                 mock.patch.object(snapshot.db_driver, "connect", return_value=connection), \
                 mock.patch.object(snapshot, "persist_instance") as persist, \
                 mock.patch.object(snapshot, "snapshot_map"), \
                 mock.patch.object(
                     snapshot.state_relocation,
                     "exclusive_maintenance",
                     side_effect=AssertionError("runtime-owned snapshot requested a lease"),
                 ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(snapshot.main(runtime_owned=True), 0)

        persist.assert_called_once_with(connection)
        connection.close.assert_called_once_with()

    def test_api_snapshot_subprocess_uses_runtime_owned_mode(self) -> None:
        self.assertEqual(server._SCRIPTS["snapshot"][2][-1], "--runtime-owned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
