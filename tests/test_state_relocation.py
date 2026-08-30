"""Relocation lifecycle, recovery, exclusion, and selector coverage."""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import instance_state
import migrate
import rebuild
import snapshot
import state_relocation
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
import server


def _hold_open(path: str, ready, release) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        ready.set()
        release.wait(timeout=10)
    finally:
        os.close(descriptor)


class RelocationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.engine = self.repo / ".super-coder"
        self.engine.mkdir(parents=True)
        self.config = self.engine / "instance.json"
        self.state_home = self.base / "state"
        self.instance_id = "a" * 32
        self.state = instance_state.resolve(
            instance_config=self.config,
            state_home=self.state_home,
            id_factory=lambda: self.instance_id,
        )
        self.legacy = self.engine / "shell_db.db"

    def create_database(self, path: Path | None = None, value: str = "kept") -> Path:
        target = path or self.legacy
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target)
        try:
            connection.execute("CREATE TABLE schema_migrations (filename TEXT)")
            connection.execute(
                "INSERT INTO schema_migrations VALUES ('0001_fixture.sql')"
            )
            connection.execute("CREATE TABLE payload (value TEXT)")
            connection.execute("INSERT INTO payload VALUES (?)", (value,))
            connection.execute("PRAGMA user_version=7")
            connection.commit()
        finally:
            connection.close()
        return target

    def relocate(self, **kwargs):
        return state_relocation.relocate_legacy_state(
            self.engine,
            state=self.state,
            proc_root=self.base / "empty-proc",
            **kwargs,
        )


class StateRelocationTests(RelocationFixture):
    def test_verified_move_activates_private_state_and_cleans_legacy_artifacts(self):
        self.create_database()
        snapshot = self.repo / ".sc-state" / "local" / "content.sql"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("snapshot\n")
        old_backup = self.repo / ".sc-state" / "db_backups" / "old.db"
        old_backup.parent.mkdir(parents=True)
        old_backup.write_bytes(b"backup")

        result = self.relocate()

        self.assertTrue(result.relocated)
        self.assertEqual(result.database, self.state.database)
        self.assertFalse(self.legacy.exists())
        self.assertFalse(Path(str(self.legacy) + "-wal").exists())
        self.assertEqual(
            instance_state.active_database_path(
                self.engine, private_state=self.state
            ),
            self.state.database,
        )
        self.assertEqual(instance_state.active_snapshot_path(
            self.repo, private_state=self.state), self.state.snapshot)
        self.assertEqual(self.state.snapshot.read_text(), "snapshot\n")
        self.assertEqual((self.state.backups / "legacy" / "old.db").read_bytes(), b"backup")
        receipt = json.loads(self.state.relocation_receipt.read_text())
        self.assertEqual(receipt["status"], "private")
        self.assertEqual(receipt["instance_id"], self.instance_id)
        self.assertEqual(len(receipt["database"]["logical_sha256"]), 64)
        self.assertTrue(Path(receipt["backup"]).is_file())
        self.assertEqual(len(self.state.database_generation.read_text().strip()), 32)
        connection = sqlite3.connect(self.state.database)
        try:
            self.assertEqual(
                connection.execute("SELECT value FROM payload").fetchone()[0],
                "kept",
            )
        finally:
            connection.close()

    def test_retry_is_idempotent_after_private_publication(self):
        self.create_database()
        first = self.relocate()
        first_receipt = self.state.relocation_receipt.read_bytes()
        first_fingerprint = state_relocation.database_fingerprint(first.database)

        second = self.relocate()

        self.assertTrue(second.relocated)
        self.assertFalse(second.recovered)
        self.assertEqual(self.state.relocation_receipt.read_bytes(), first_receipt)
        self.assertEqual(
            state_relocation.database_fingerprint(second.database), first_fingerprint
        )
        self.assertEqual(len(list(self.state.backups.glob("shell_db.preupdate.*.db"))), 1)

    def test_pre_publication_failure_leaves_legacy_authoritative(self):
        self.create_database()
        with self.assertRaisesRegex(
            state_relocation.RelocationError, "after candidate"
        ):
            self.relocate(failpoint="after_candidate")

        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.state.database.exists())
        self.assertFalse(self.state.relocation_receipt.exists())
        self.assertEqual(
            instance_state.active_database_path(
                self.engine, private_state=self.state
            ),
            self.legacy,
        )

    def test_retry_recovers_after_publishing_receipt(self):
        self.create_database()
        with self.assertRaisesRegex(
            state_relocation.RelocationError, "after relocation receipt"
        ):
            self.relocate(failpoint="after_receipt")

        self.assertEqual(
            json.loads(self.state.relocation_receipt.read_text())["status"],
            "publishing",
        )
        with self.assertRaisesRegex(
            instance_state.MaintenanceCutoverRequired,
            "relocation_incomplete.*./sc update",
        ):
            instance_state.active_database_path(
                self.engine, private_state=self.state
            )
        self.assertEqual(
            instance_state.maintenance_database_path(
                self.engine, private_state=self.state
            ),
            self.state.database,
        )
        result = self.relocate()
        self.assertTrue(result.recovered)
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.state.database.exists())
        self.assertEqual(
            json.loads(self.state.relocation_receipt.read_text())["status"],
            "private",
        )
        connection = sqlite3.connect(self.state.database)
        try:
            self.assertEqual(
                connection.execute("SELECT value FROM payload").fetchone()[0],
                "kept",
            )
        finally:
            connection.close()

    def test_publishing_refuses_launch_rebuild_migrate_and_snapshot(self):
        self.create_database()
        with self.assertRaisesRegex(
            state_relocation.RelocationError, "after relocation receipt"
        ):
            self.relocate(failpoint="after_receipt")

        environment = {"XDG_STATE_HOME": str(self.state_home)}
        with mock.patch.dict(os.environ, environment), mock.patch.object(
            server, "ENGINE", self.engine
        ), mock.patch.object(
            server.ports_mod, "resolve", return_value={"port": 8800}
        ), self.assertRaisesRegex(SystemExit, "relocation_incomplete"):
            server.main([])

        with mock.patch.dict(os.environ, environment), mock.patch.object(
            rebuild, "ENGINE", self.engine
        ), mock.patch.object(rebuild, "_main_under_lease") as mutate, \
                self.assertRaisesRegex(
                    instance_state.MaintenanceCutoverRequired,
                    "relocation_incomplete",
                ):
            rebuild.main(["--no-backup"])
        mutate.assert_not_called()

        with mock.patch.dict(os.environ, environment), mock.patch.object(
            migrate, "ENGINE", self.engine
        ), mock.patch.object(migrate, "migrate") as mutate, \
                self.assertRaisesRegex(
                    instance_state.MaintenanceCutoverRequired,
                    "relocation_incomplete",
                ):
            migrate.cli_main([str(self.state.database)])
        mutate.assert_not_called()

        with mock.patch.dict(os.environ, environment), mock.patch.object(
            snapshot, "ENGINE", self.engine
        ), mock.patch.object(snapshot, "_main_under_lease") as mutate, \
                self.assertRaisesRegex(
                    instance_state.MaintenanceCutoverRequired,
                    "relocation_incomplete",
                ):
            snapshot.main()
        mutate.assert_not_called()
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.state.database.exists())
        self.assertFalse(Path(str(self.state.database) + ".rebuild").exists())

    def test_retry_recovers_after_database_publish_before_final_receipt(self):
        self.create_database()
        with self.assertRaisesRegex(
            state_relocation.RelocationError, "after private publication"
        ):
            self.relocate(failpoint="after_publish")

        self.assertTrue(self.legacy.exists())
        self.assertTrue(self.state.database.exists())
        with self.assertRaisesRegex(
            instance_state.MaintenanceCutoverRequired, "relocation_incomplete"
        ):
            instance_state.active_database_path(
                self.engine, private_state=self.state
            )
        result = self.relocate()
        self.assertTrue(result.recovered)
        self.assertFalse(self.legacy.exists())

    def test_conflicting_complete_databases_fail_closed(self):
        self.create_database(value="legacy")
        self.create_database(self.state.database, value="private")

        with self.assertRaisesRegex(
            state_relocation.RelocationError, "conflicting complete"
        ):
            self.relocate()
        self.assertTrue(self.legacy.exists())
        self.assertTrue(self.state.database.exists())
        self.assertFalse(self.state.relocation_receipt.exists())

    def test_fresh_install_uses_private_state_without_a_relocation_receipt(self):
        self.assertEqual(
            instance_state.active_database_path(
                self.engine, private_state=self.state
            ),
            self.state.database,
        )
        result = self.relocate()
        self.assertFalse(result.relocated)
        self.assertFalse(self.state.relocation_receipt.exists())
        self.assertTrue(self.state.database_generation.exists())

    def test_legacy_fd_owner_refuses_before_backup_or_candidate(self):
        self.create_database()
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        holder = context.Process(
            target=_hold_open, args=(str(self.legacy), ready, release)
        )
        holder.start()
        self.addCleanup(lambda: holder.kill() if holder.is_alive() else None)
        self.assertTrue(ready.wait(timeout=5))
        try:
            with self.assertRaisesRegex(
                state_relocation.MaintenanceBusy, "runtime_active"
            ):
                state_relocation.relocate_legacy_state(
                    self.engine, state=self.state
                )
        finally:
            release.set()
            holder.join(timeout=5)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.state.database.exists())
        self.assertFalse(self.state.relocation_receipt.exists())
        self.assertFalse(self.state.backups.exists())

    def test_exclusive_maintenance_lease_has_one_winner(self):
        with state_relocation.exclusive_maintenance(
            self.state, command="first"
        ), self.assertRaisesRegex(
            state_relocation.MaintenanceBusy, "maintenance_busy"
        ), state_relocation.exclusive_maintenance(
            self.state, command="second"
        ):
            self.fail("second lease owner entered")

    def test_live_runtime_blocks_maintenance(self):
        with state_relocation.shared_runtime(
            self.state, command="api"
        ), self.assertRaisesRegex(
            state_relocation.MaintenanceBusy, "maintenance_busy"
        ), state_relocation.exclusive_maintenance(
            self.state, command="update"
        ):
            self.fail("maintenance entered while runtime owned the DB")

    def test_maintenance_blocks_runtime_start(self):
        with state_relocation.exclusive_maintenance(
            self.state, command="update"
        ), self.assertRaisesRegex(
            state_relocation.MaintenanceBusy, "maintenance_busy"
        ), state_relocation.shared_runtime(
            self.state, command="api"
        ):
            self.fail("runtime entered while maintenance owned the DB")

    def test_rollback_to_old_floor_reconstructs_verified_legacy_pair(self):
        self.create_database()
        snapshot = self.repo / ".sc-state" / "local" / "content.sql"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("snapshot\n")
        self.relocate()

        restored = state_relocation.restore_legacy_for_old_floor(
            self.engine,
            state=self.state,
            proc_root=self.base / "empty-proc",
        )

        self.assertEqual(restored, self.legacy)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.state.database.exists())
        self.assertEqual(
            json.loads(self.state.relocation_receipt.read_text())["status"],
            "legacy",
        )
        self.assertEqual(
            instance_state.active_database_path(
                self.engine, private_state=self.state
            ),
            self.legacy,
        )
        self.assertEqual(snapshot.read_text(), "snapshot\n")

    def test_remove_deletes_only_claimed_live_root_after_external_backup(self):
        self.create_database()
        self.relocate()
        archive = (
            state_relocation.prepare_removal_archive(self.state)
            / "removal"
            / "fixture.db"
        )
        state_relocation._sqlite_backup(self.state.database, archive)

        state_relocation.remove_private_state(
            self.state,
            verified_backup=archive,
            proc_root=self.base / "empty-proc",
        )

        self.assertFalse(self.state.root.exists())
        self.assertTrue(archive.exists())
        self.assertEqual(
            state_relocation.database_fingerprint(archive)["integrity"], "ok"
        )


if __name__ == "__main__":
    unittest.main()
