"""Focused contract tests for the private instance-state resolver."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".super-coder" / "scripts" / "instance_state.py"
SPEC = importlib.util.spec_from_file_location("instance_state", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
instance_state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = instance_state
SPEC.loader.exec_module(instance_state)


def _concurrent_resolve_worker(config, state_home, candidate, barrier, results):
    def candidate_factory():
        barrier.wait(timeout=10)
        return candidate

    resolved = instance_state.resolve(
        instance_config=config,
        state_home=state_home,
        id_factory=candidate_factory,
    )
    results.put(("ok", resolved.instance_id, str(resolved.root)))


def _configuration_writer_worker(kind, config, barrier, results):
    scripts = str(ROOT / ".super-coder" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    original_merge = instance_state.merge_instance_config

    def synchronized_merge(*args, **kwargs):
        barrier.wait(timeout=10)
        return original_merge(*args, **kwargs)

    try:
        if kind == "ports":
            import ports

            ports.CONFIG = config
            ports.instance_state.merge_instance_config = synchronized_merge
            ports.update({"vm": {"domain": "test"}})
        else:
            import feature

            feature.INSTANCE = config
            feature.instance_state.merge_instance_config = synchronized_merge
            feature._update_instance({"pg": {}})
        results.put(("ok", kind))
    except BaseException as exc:
        results.put(("error", kind, repr(exc)))
        raise


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
        self.assertEqual(
            resolved.snapshot_lock, resolved.root / ".content-write.lock"
        )
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

    def test_bound_configuration_update_preserves_identity_and_unknown_fields(self):
        self.config.write_text(json.dumps({"custom": True}) + "\n")
        resolved = self.resolve()

        stored = instance_state.update_bound_instance_config(
            self.config,
            {"port": 8837, "installed_at": "2099-01-01"},
        )

        self.assertEqual(stored["instance_id"], resolved.instance_id)
        self.assertTrue(stored["custom"])
        self.assertEqual(stored["port"], 8837)
        self.assertEqual(stored["installed_at"], "2099-01-01")
        with self.assertRaisesRegex(
            instance_state.InstanceStateError,
            "only be assigned by the resolver",
        ):
            instance_state.update_bound_instance_config(
                self.config,
                {"instance_id": "f" * 32},
            )

    def test_concurrent_first_assignment_has_one_durable_winner(self):
        context = multiprocessing.get_context("fork")
        for existing in (False, True):
            with self.subTest(existing_idless_configuration=existing):
                case = self.base / ("existing" if existing else "absent")
                config = case / "repo" / ".super-coder" / "instance.json"
                config.parent.mkdir(parents=True)
                if existing:
                    config.write_text(json.dumps({"port": 8837}) + "\n")
                state_home = case / "state"
                barrier = context.Barrier(2)
                results = context.Queue()
                processes = [
                    context.Process(
                        target=_concurrent_resolve_worker,
                        args=(config, state_home, character * 32, barrier, results),
                    )
                    for character in ("a", "b")
                ]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=15)
                    self.assertFalse(process.is_alive(), "resolver race deadlocked")
                    self.assertEqual(process.exitcode, 0)

                outcomes = [results.get(timeout=2) for _ in processes]
                self.assertEqual(
                    {outcome[0] for outcome in outcomes}, {"ok"}, outcomes
                )
                self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
                self.assertEqual(len({outcome[2] for outcome in outcomes}), 1)
                winner = json.loads(config.read_text())["instance_id"]
                self.assertEqual({outcome[1] for outcome in outcomes}, {winner})
                if existing:
                    self.assertEqual(json.loads(config.read_text())["port"], 8837)
                roots = list((state_home / "subfloor" / "instances").iterdir())
                self.assertEqual([root.name for root in roots], [winner])

    def test_configuration_writers_cannot_erase_a_concurrent_identity(self):
        context = multiprocessing.get_context("fork")
        for kind in ("ports", "feature"):
            with self.subTest(writer=kind):
                case = self.base / kind
                config = case / "repo" / ".super-coder" / "instance.json"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({"port": 8837}) + "\n")
                state_home = case / "state"
                barrier = context.Barrier(2)
                results = context.Queue()
                writer = context.Process(
                    target=_configuration_writer_worker,
                    args=(kind, config, barrier, results),
                )
                resolver = context.Process(
                    target=_concurrent_resolve_worker,
                    args=(config, state_home, "a" * 32, barrier, results),
                )
                writer.start()
                resolver.start()
                for process in (writer, resolver):
                    process.join(timeout=15)
                    self.assertFalse(process.is_alive(), f"{kind} race deadlocked")
                    self.assertEqual(process.exitcode, 0)
                outcomes = [results.get(timeout=2), results.get(timeout=2)]
                self.assertEqual({outcome[0] for outcome in outcomes}, {"ok"})
                stored = json.loads(config.read_text())
                self.assertEqual(stored["instance_id"], "a" * 32)
                self.assertEqual(
                    instance_state.ensure_instance_id(
                        config, id_factory=lambda: "b" * 32
                    ),
                    "a" * 32,
                )
                roots = list((state_home / "subfloor" / "instances").iterdir())
                self.assertEqual([root.name for root in roots], ["a" * 32])

    def test_every_configuration_writer_refuses_malformed_state(self):
        scripts = str(ROOT / ".super-coder" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import feature
        import ports

        self.config.write_text("{\n")
        with (
            mock.patch.object(ports, "CONFIG", self.config),
            self.assertRaisesRegex(RuntimeError, "cannot read"),
        ):
            ports.update({"vm": {"domain": "test"}})
        with (
            mock.patch.object(feature, "INSTANCE", self.config),
            self.assertRaisesRegex(RuntimeError, "cannot read"),
        ):
            feature._update_instance({"pg": {}})
        self.assertEqual(self.config.read_text(), "{\n")
        self.assertFalse(self.state_home.exists())

    def test_configuration_cli_preserves_the_bound_identity(self):
        resolved = self.resolve()

        self.assertEqual(
            instance_state.main(
                ["config-set", str(self.config), "pg", json.dumps({})]
            ),
            0,
        )

        stored = json.loads(self.config.read_text())
        self.assertEqual(stored["instance_id"], resolved.instance_id)
        self.assertEqual(stored["pg"], {})

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

    def test_refuses_symlinked_identity_lock_directory(self):
        self.config.write_text(json.dumps({"port": 8837}) + "\n")
        foreign = self.base / "foreign-locks"
        foreign.mkdir()
        (self.engine / "run").symlink_to(foreign, target_is_directory=True)

        with self.assertRaisesRegex(
            instance_state.InstanceStateError,
            "unsafe instance identity lock directory",
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

    def test_active_database_stays_legacy_until_maintenance_cutover(self):
        resolved = self.resolve()
        legacy = self.engine / "shell_db.db"

        self.assertEqual(instance_state.active_database_path(self.engine), legacy)
        with self.assertRaisesRegex(
            instance_state.MaintenanceCutoverRequired,
            "spec #133 maintenance cutover",
        ):
            instance_state.active_database_path(
                self.engine,
                private_state=resolved,
            )

        self.assertFalse(legacy.exists())
        self.assertFalse(resolved.database.exists())

    def test_snapshot_and_backup_paths_share_the_refusing_activation_seam(self):
        resolved = self.resolve()
        snapshot = self.repo / ".sc-state" / "local" / "content.sql"
        snapshot_lock = snapshot.parent / ".content-write.lock"
        backup_paths = instance_state.active_backup_paths(
            self.repo,
            {"HOME": str(self.base / "home"), "SC_DB_BACKUP_DIR": str(self.base / "override")},
        )

        self.assertEqual(instance_state.active_snapshot_path(self.repo), snapshot)
        self.assertEqual(
            instance_state.active_snapshot_lock_path(self.repo), snapshot_lock
        )
        self.assertEqual(backup_paths.override, self.base / "override")
        self.assertEqual(
            backup_paths.home, self.base / "home" / "db_backups" / "repo"
        )
        self.assertEqual(
            backup_paths.local, self.repo / ".sc-state" / "db_backups"
        )
        for selector in (
            instance_state.active_snapshot_path,
            instance_state.active_snapshot_lock_path,
            instance_state.active_backup_paths,
        ):
            with self.assertRaisesRegex(
                instance_state.MaintenanceCutoverRequired,
                "spec #133 maintenance cutover",
            ):
                selector(self.repo, private_state=resolved)
        self.assertFalse(snapshot.exists())
        self.assertFalse(resolved.snapshot.exists())
        self.assertFalse(resolved.backups.exists())


class ProductionSeamInventoryTests(unittest.TestCase):
    DIRECT_RESOLVER_CALLS: ClassVar[dict[str, tuple[str, ...]]] = dict.fromkeys(
        {
            ".super-coder/api/server.py",
            ".super-coder/api/conversation_routes.py",
            ".super-coder/scripts/analytics.py",
            ".super-coder/scripts/init_fork.py",
            ".super-coder/scripts/install.py",
            ".super-coder/scripts/map_db.py",
            ".super-coder/scripts/models.py",
            ".super-coder/scripts/rebuild.py",
            ".super-coder/scripts/remove.py",
            ".super-coder/scripts/render.py",
            ".super-coder/scripts/render_check.py",
            ".super-coder/scripts/rollback.py",
            ".super-coder/scripts/run.py",
            ".super-coder/scripts/seed_dogfood.py",
            ".super-coder/scripts/seed_skills.py",
            ".super-coder/scripts/shell_liveness.py",
            ".super-coder/scripts/skill.py",
            ".super-coder/scripts/snapshot.py",
            ".super-coder/scripts/update.py",
        },
        ("active_database_path",),
    ) | {
        ".super-coder/scripts/artifact_policy.py": (
            "active_snapshot_path",
            "active_snapshot_lock_path",
        ),
        ".super-coder/scripts/db_backup.py": ("active_backup_paths",),
    }

    def test_inventory_is_exact_and_every_direct_owner_uses_resolver(self):
        inventory = instance_state.production_consumer_inventory()
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
                "instance_configuration",
                "catalogue_writers",
                "legacy_and_candidate_paths",
            },
        )
        paths = [path for entry in inventory for path in entry.paths]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertLessEqual(set(self.DIRECT_RESOLVER_CALLS), set(paths))
        for relative in paths:
            source = ROOT / relative
            self.assertTrue(source.is_file(), relative)

        for relative, calls in self.DIRECT_RESOLVER_CALLS.items():
            source = (ROOT / relative).read_text()
            self.assertIn("instance_state", source, relative)
            for call in calls:
                self.assertIn(call, source, relative)

        dispatcher = (ROOT / ".super-coder/scripts/dispatch.sh").read_text()
        self.assertIn(
            '"$S/instance_state.py" active-database "$ENGINE"',
            dispatcher,
        )
        self.assertEqual(dispatcher.count('instance_state.py" config-set'), 2)
        self.assertNotIn("p.write_text(json.dumps(d", dispatcher)
        self.assertNotIn('> "$f"', dispatcher)
        for relative in (
            ".super-coder/scripts/ports.py",
            ".super-coder/scripts/feature.py",
        ):
            source = (ROOT / relative).read_text()
            self.assertIn("instance_state.merge_instance_config", source, relative)
            self.assertNotRegex(source, r"INSTANCE\.write_text|CONFIG\.write_text")
        for relative in (
            ".super-coder/scripts/vm.py",
            ".super-coder/scripts/ts.py",
            ".super-coder/scripts/pm2.py",
            ".super-coder/scripts/dbq.py",
        ):
            source = (ROOT / relative).read_text()
            self.assertIn("ports.update", source, relative)
            self.assertNotIn("ports.save", source, relative)
        driver = (ROOT / ".super-coder/scripts/db_driver.py").read_text()
        self.assertIn("instance_state.active_database_path", driver)
        installer = (ROOT / ".super-coder/scripts/install.py").read_text()
        self.assertIn("installation_state = instance_state.resolve(", installer)
        self.assertIn("instance_config=ports_mod.CONFIG", installer)
        self.assertIn("instance_state.update_bound_instance_config", installer)
        self.assertIn("installer_changes", installer)
        self.assertNotIn("update_bound_instance_config(ports_mod.CONFIG, cfg)", installer)
        expected_call_chain = {
            ".super-coder/scripts/snapshot.py": ("artifact_policy.content_path",),
            ".super-coder/scripts/rebuild.py": (
                "artifact_policy.content_path",
                "db_backup_mod.select_backup_dir",
            ),
            ".super-coder/scripts/update.py": ("rebuild_mod.backup_existing",),
            ".super-coder/scripts/rollback.py": ("db_backup_mod.latest_backup",),
            ".super-coder/scripts/remove.py": (
                "instance_state.active_backup_paths",
                "db_backup.backup_database",
            ),
        }
        for relative, calls in expected_call_chain.items():
            source = (ROOT / relative).read_text()
            for call in calls:
                self.assertIn(call, source, relative)

    def test_daemons_receive_resolved_path_and_do_not_choose_a_live_target(self):
        server = (ROOT / ".super-coder/api/server.py").read_text()
        for relative in (
            ".super-coder/scripts/conversation_broker.py",
            ".super-coder/scripts/conversation_reaper.py",
            ".super-coder/scripts/sprint_runtime.py",
            ".super-coder/scripts/sprint_pr_watcher.py",
        ):
            source = (ROOT / relative).read_text()
            self.assertNotIn('ENGINE / "shell_db.db"', source, relative)
        self.assertIn("DB_PATH = instance_state.active_database_path(ENGINE)", server)
        self.assertRegex(server, r"conversation_broker\.start_service\([^)]*DB_PATH")
        self.assertRegex(server, r"conversation_reaper\.start_service\([^)]*DB_PATH")

    def test_private_target_activation_exists_only_as_a_refusing_seam(self):
        source = MODULE_PATH.read_text()
        self.assertEqual(
            source.count("return Path(engine) / \"shell_db.db\""),
            1,
        )
        runtime_selector = re.compile(
            r"(?:/|\+)\s*[\"']shell_db\.db[\"']|"
            r"[\"']shell_db\.db[\"']\s*(?:/|\+)"
        )
        self.assertRegex(
            'source = repo_root / ".super-coder" / "shell_db.db"',
            runtime_selector,
            "the bypass detector must fail on a second runtime selector",
        )
        for directory in (
            ROOT / ".super-coder" / "api",
            ROOT / ".super-coder" / "scripts",
            ROOT / ".super-coder" / "render",
        ):
            for extension in ("*.py", "*.sh"):
                for path in directory.rglob(extension):
                    if path == MODULE_PATH:
                        continue
                    self.assertNotRegex(
                        path.read_text(),
                        runtime_selector,
                        path.relative_to(ROOT).as_posix(),
                    )

    def test_inventory_classifies_every_runtime_state_path_reference(self):
        pattern = re.compile(
            r"shell_db\.db|\.sc-state/(?:local/)?content\.sql|"
            r"db_backups|snapshot/content\.sql"
        )
        discovered = {
            source.relative_to(ROOT).as_posix()
            for directory in (
                ROOT / ".super-coder" / "api",
                ROOT / ".super-coder" / "scripts",
                ROOT / ".super-coder" / "render",
            )
            for extension in ("*.py", "*.sh")
            for source in directory.rglob(extension)
            if pattern.search(source.read_text())
        }
        discovered.remove(".super-coder/scripts/instance_state.py")
        classified = {
            path
            for entry in instance_state.production_consumer_inventory()
            for path in entry.paths
        }
        self.assertEqual(
            set(),
            discovered - classified,
            "runtime state references are missing from the cutover inventory",
        )

    def test_snapshot_and_backup_owners_have_no_second_active_selector(self):
        snapshot_selector = re.compile(
            r"LOCAL_DIR\s*/\s*[\"']content\.sql[\"']|"
            r"LOCAL_DIR\s*/\s*[\"']\.content-write\.lock[\"']"
        )
        backup_selector = re.compile(
            r"[\"']db_backups[\"']\s*/\s*repo_root\.name|"
            r"repo_root\s*/\s*[\"']\.sc-state[\"']\s*/\s*[\"']db_backups[\"']"
        )
        self.assertRegex('LOCAL_DIR / "content.sql"', snapshot_selector)
        self.assertRegex(
            'repo_root / ".sc-state" / "db_backups"', backup_selector
        )
        for relative in (
            ".super-coder/scripts/artifact_policy.py",
            ".super-coder/scripts/db_backup.py",
            ".super-coder/scripts/rebuild.py",
            ".super-coder/scripts/snapshot.py",
            ".super-coder/scripts/update.py",
            ".super-coder/scripts/rollback.py",
            ".super-coder/scripts/remove.py",
        ):
            source = (ROOT / relative).read_text()
            self.assertNotRegex(source, snapshot_selector, relative)
            self.assertNotRegex(source, backup_selector, relative)


if __name__ == "__main__":
    unittest.main()
