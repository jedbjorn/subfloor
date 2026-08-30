"""Focused contract tests for the private instance-state resolver."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".super-coder" / "scripts" / "instance_state.py"
SPEC = importlib.util.spec_from_file_location("instance_state", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
instance_state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = instance_state
SPEC.loader.exec_module(instance_state)


class InstanceStateResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.engine = self.repo / ".super-coder"
        self.engine.mkdir(parents=True)
        self.config = self.engine / "instance.json"
        self.state_home = self.base / "xdg-state"
        self.fixed_id = "0123456789abcdef0123456789abcdef"

    def resolve(self, **overrides):
        arguments = {
            "instance_config": self.config,
            "state_home": self.state_home,
            "id_factory": lambda: self.fixed_id,
        }
        arguments.update(overrides)
        return instance_state.resolve(**arguments)

    def test_creates_opaque_identity_and_private_canonical_paths(self):
        resolved = self.resolve()

        self.assertEqual(resolved.instance_id, self.fixed_id)
        self.assertEqual(
            resolved.root,
            self.state_home / "subfloor" / "instances" / self.fixed_id,
        )
        self.assertEqual(resolved.database, resolved.root / "shell_db.db")
        self.assertEqual(resolved.snapshot, resolved.root / "content.sql")
        self.assertEqual(resolved.backups, resolved.root / "db_backups")
        self.assertEqual(resolved.maintenance_lock, resolved.root / "maintenance.lock")
        self.assertEqual(
            resolved.database_generation, resolved.root / "database-generation"
        )
        self.assertEqual(resolved.relocation_receipt, resolved.root / "relocation.json")
        self.assertEqual(resolved.recovery_evidence, resolved.root / "recovery")
        self.assertEqual(
            json.loads(self.config.read_text())["instance_id"], self.fixed_id
        )
        self.assertEqual(stat.S_IMODE(resolved.root.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((resolved.root / "owner.json").stat().st_mode), 0o600
        )
        self.assertFalse(resolved.database.exists())

    def test_preserves_existing_instance_config_and_identity(self):
        self.config.write_text(json.dumps({"port": 8837, "custom": True}) + "\n")
        first = self.resolve()
        second = self.resolve(id_factory=lambda: "f" * 32)

        stored = json.loads(self.config.read_text())
        self.assertEqual(first, second)
        self.assertEqual(stored["port"], 8837)
        self.assertTrue(stored["custom"])
        self.assertEqual(stored["instance_id"], self.fixed_id)

    def test_repository_movement_keeps_private_binding(self):
        first = self.resolve()
        moved = self.base / "renamed-repository"
        self.repo.rename(moved)

        second = instance_state.resolve(
            instance_config=moved / ".super-coder" / "instance.json",
            state_home=self.state_home,
        )

        self.assertEqual(second.instance_id, first.instance_id)
        self.assertEqual(second.root, first.root)

    def test_copy_without_owner_local_config_gets_new_identity(self):
        first = self.resolve()
        copied_config = self.base / "copy" / ".super-coder" / "instance.json"
        copied_config.parent.mkdir(parents=True)
        second = instance_state.resolve(
            instance_config=copied_config,
            state_home=self.state_home,
            id_factory=lambda: "f" * 32,
        )

        self.assertNotEqual(first.instance_id, second.instance_id)
        self.assertNotEqual(first.root, second.root)

    def test_uses_xdg_state_home_without_accepting_a_database_path(self):
        xdg_home = self.base / "environment-state"
        resolved = instance_state.resolve(
            instance_config=self.config,
            environ={"XDG_STATE_HOME": str(xdg_home)},
            id_factory=lambda: self.fixed_id,
        )

        self.assertEqual(
            resolved.root,
            xdg_home / "subfloor" / "instances" / self.fixed_id,
        )

    def test_refuses_invalid_persisted_identity(self):
        self.config.write_text(json.dumps({"instance_id": "repo-name"}) + "\n")

        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "invalid instance ID"
        ):
            self.resolve()

    def test_refuses_writable_by_others_instance_config(self):
        self.config.write_text(json.dumps({"instance_id": self.fixed_id}) + "\n")
        os.chmod(self.config, 0o666)

        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "writable-by-others"
        ):
            self.resolve()

    def test_refuses_symlinked_instance_config(self):
        target = self.base / "foreign-config.json"
        target.write_text(json.dumps({"instance_id": self.fixed_id}))
        self.config.symlink_to(target)

        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "symlinked instance configuration"
        ):
            self.resolve()

    def test_refuses_symlinked_private_directory(self):
        foreign = self.base / "foreign-state"
        foreign.mkdir()
        root = self.state_home / "subfloor" / "instances" / self.fixed_id
        root.parent.mkdir(parents=True, mode=0o700)
        os.chmod(root.parent.parent, 0o700)
        os.chmod(root.parent, 0o700)
        root.symlink_to(foreign, target_is_directory=True)

        with self.assertRaisesRegex(
            instance_state.InstanceStateError,
            "symlinked private instance-state directory",
        ):
            self.resolve()

    def test_refuses_non_private_directory_permissions(self):
        root = self.state_home / "subfloor" / "instances" / self.fixed_id
        root.mkdir(parents=True, mode=0o755)
        os.chmod(root.parent.parent, 0o700)
        os.chmod(root.parent, 0o700)
        os.chmod(root, 0o755)

        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "non-private private instance-state"
        ):
            self.resolve()

    def test_refuses_unclaimed_or_foreign_state(self):
        root = self.state_home / "subfloor" / "instances" / self.fixed_id
        root.mkdir(parents=True, mode=0o700)
        os.chmod(root.parent.parent, 0o700)
        os.chmod(root.parent, 0o700)
        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "unclaimed private instance state"
        ):
            self.resolve()

        (root / "owner.json").write_text(
            json.dumps({"instance_id": "f" * 32, "owner_uid": os.geteuid()})
        )
        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "foreign private instance state"
        ):
            self.resolve()

    def test_create_false_is_read_only_and_requires_existing_identity(self):
        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "has no instance ID"
        ):
            self.resolve(create=False)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state_home.exists())

    def test_create_false_does_not_create_missing_state_namespace(self):
        self.config.write_text(json.dumps({"instance_id": self.fixed_id}) + "\n")

        with self.assertRaisesRegex(
            instance_state.InstanceStateError, "private state namespace does not exist"
        ):
            self.resolve(create=False)

        self.assertFalse(self.state_home.exists())


class DeferredCutoverInventoryTests(unittest.TestCase):
    def test_inventory_is_exact_and_production_imports_are_deferred(self):
        inventory = instance_state.deferred_consumer_inventory()
        owners = {entry.owner for entry in inventory}
        self.assertEqual(
            owners,
            {
                "api_and_daemons",
                "db_driver",
                "snapshot_and_render",
                "backup_and_rebuild",
                "install_and_update",
                "rollback_remove_and_eject",
                "shell_entry_and_liveness",
                "catalogue_writers",
            },
        )
        paths = [path for entry in inventory for path in entry.paths]
        self.assertEqual(len(paths), len(set(paths)))
        for relative in paths:
            source = ROOT / relative
            self.assertTrue(source.is_file(), relative)
            self.assertNotIn(
                "import instance_state",
                source.read_text(),
                f"{relative} crossed the spec #133 deferred-cutover boundary",
            )

    def test_inventory_covers_every_current_direct_database_path_owner(self):
        pattern = re.compile(r'DB_PATH\s*=\s*ENGINE\s*/\s*"shell_db\.db"')
        discovered = {
            source.relative_to(ROOT).as_posix()
            for directory in (ROOT / ".super-coder" / "api", ROOT / ".super-coder" / "scripts")
            for source in directory.rglob("*.py")
            if pattern.search(source.read_text())
        }
        self.assertEqual(
            discovered,
            {
                ".super-coder/api/conversation_routes.py",
                ".super-coder/api/server.py",
                ".super-coder/scripts/analytics.py",
                ".super-coder/scripts/init_fork.py",
                ".super-coder/scripts/models.py",
                ".super-coder/scripts/rebuild.py",
                ".super-coder/scripts/remove.py",
                ".super-coder/scripts/render.py",
                ".super-coder/scripts/rollback.py",
                ".super-coder/scripts/run.py",
                ".super-coder/scripts/seed_dogfood.py",
                ".super-coder/scripts/seed_skills.py",
                ".super-coder/scripts/shell_liveness.py",
                ".super-coder/scripts/skill.py",
                ".super-coder/scripts/snapshot.py",
                ".super-coder/scripts/update.py",
            },
        )


if __name__ == "__main__":
    unittest.main()
