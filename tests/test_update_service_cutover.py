"""The updater quiesces a PM2 review server around DB migration."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402


class Stop(Exception):
    """Sentinel proving main reached the service cutover seam."""


class UpdateServiceCutoverTest(unittest.TestCase):
    def setUp(self):
        self.intent_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.intent_temp.cleanup)
        self.intent_path = Path(self.intent_temp.name) / "update-runtime-intent.json"
        patcher = mock.patch.object(
            update, "_runtime_intent_path", return_value=self.intent_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_in_process_snapshot_uses_scoped_update_admin_authority(self):
        observed_admin = []

        def snapshot(*, lease_held):
            snapshot_mod.require_admin("snapshot")
            observed_admin.append((lease_held, os.environ.get("SC_ADMIN")))

        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(
            snapshot_mod, "main", side_effect=snapshot
        ):
            os.environ.pop("SC_ADMIN", None)
            update.snapshot_under_cutover()
            self.assertNotIn("SC_ADMIN", os.environ)

        self.assertEqual(observed_admin, [(True, "1")])

    def test_in_process_snapshot_restores_admin_environment_after_failure(self):
        with mock.patch.dict(os.environ, {"SC_ADMIN": "caller"}), mock.patch.object(
            snapshot_mod, "main", side_effect=SystemExit("snapshot failed")
        ):
            with self.assertRaisesRegex(SystemExit, "snapshot failed"):
                update.snapshot_under_cutover()
            self.assertEqual(os.environ.get("SC_ADMIN"), "caller")

    def assert_restart_failure_stops_all(
        self,
        *,
        pm2: bool,
        docker: bool,
        docker_start_fails: bool = False,
    ) -> None:
        pm2_service = ("/usr/bin/pm2", "sc-example") if pm2 else None
        docker_service = ("/usr/bin/docker", "sc-example") if docker else None
        running = set()
        stopped = []

        def start_pm2(service):
            if service is not None:
                running.add("PM2")

        def start_docker(service):
            if service is None:
                return
            if docker_start_fails:
                raise SystemExit("injected Docker start failure")
            running.add("Docker")

        def stop_restarted(label, _service):
            running.discard(label)
            stopped.append(label)
            return None

        health_failure = mock.Mock(
            side_effect=SystemExit("injected readiness failure")
        )
        with mock.patch.object(
            update, "stop_pm2_review_server", return_value=pm2_service
        ), mock.patch.object(
            update, "stop_docker_review_server", return_value=docker_service
        ), mock.patch.object(
            update.instance_state, "resolve", return_value=mock.Mock()
        ), mock.patch.object(
            update.instance_state,
            "active_database_path",
            return_value=update.DB_PATH,
        ), mock.patch.object(
            update.state_relocation,
            "exclusive_maintenance",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            update.state_relocation,
            "relocate_legacy_state",
            return_value=mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update, "bind_cutover_state"
        ), mock.patch.object(
            update, "migrate_or_rebuild"
        ), mock.patch.object(
            update, "start_pm2_review_server", side_effect=start_pm2
        ), mock.patch.object(
            update, "start_docker_review_server", side_effect=start_docker
        ), mock.patch.object(
            update, "require_restarted_runtime_health", health_failure
        ), mock.patch.object(
            update, "_stop_restarted_runtime", side_effect=stop_restarted
        ), self.assertRaisesRegex(
            SystemExit,
            "all managed runtimes are stopped.*private state remains authoritative",
        ):
            update.migrate_with_service_cutover()

        self.assertEqual(running, set())
        expected = [label for label, present in (
            ("Docker", docker), ("PM2", pm2)
        ) if present]
        self.assertEqual(stopped, expected)
        if docker_start_fails:
            health_failure.assert_not_called()
        else:
            health_failure.assert_called_once_with()

    def test_pm2_is_stopped_when_later_docker_start_fails(self):
        self.assert_restart_failure_stops_all(
            pm2=True, docker=True, docker_start_fails=True
        )

    def test_both_runtimes_stop_when_final_health_fails(self):
        self.assert_restart_failure_stops_all(pm2=True, docker=True)

    def test_pm2_only_runtime_stops_when_final_health_fails(self):
        self.assert_restart_failure_stops_all(pm2=True, docker=False)

    def test_docker_only_runtime_stops_when_final_health_fails(self):
        self.assert_restart_failure_stops_all(pm2=False, docker=True)

    def test_failed_update_retains_docker_relaunch_intent(self):
        state = mock.Mock()
        with mock.patch.object(
            update.instance_state, "resolve", return_value=state
        ), mock.patch.object(
            update, "stop_docker_review_server", return_value=("docker", "sc-example")
        ), mock.patch.object(
            update, "stop_pm2_review_server", return_value=None
        ), mock.patch.object(
            update.state_relocation,
            "exclusive_maintenance",
            side_effect=update.state_relocation.MaintenanceBusy("busy"),
        ), self.assertRaisesRegex(SystemExit, "runtime remains stopped"):
            update.migrate_with_service_cutover()

        self.assertEqual(
            json.loads(self.intent_path.read_text()),
            {"runtimes": ["docker"], "version": 1},
        )

    def test_invalid_runtime_intent_refuses_before_shutdown(self):
        self.intent_path.write_text("not json\n")
        state = mock.Mock()
        with mock.patch.object(
            update.instance_state, "resolve", return_value=state
        ), mock.patch.object(
            update, "stop_docker_review_server"
        ) as docker_stop, mock.patch.object(
            update, "stop_pm2_review_server"
        ) as pm2_stop, self.assertRaisesRegex(
            SystemExit, "runtime relaunch intent refused before shutdown"
        ):
            update.migrate_with_service_cutover()

        docker_stop.assert_not_called()
        pm2_stop.assert_not_called()

    def test_retry_rehydrates_absent_docker_and_clears_intent_after_health(self):
        self.intent_path.write_text(
            json.dumps({"version": 1, "runtimes": ["docker"]})
        )
        state = mock.Mock()
        with mock.patch.object(
            update.instance_state, "resolve", return_value=state
        ), mock.patch.object(
            update, "stop_docker_review_server", return_value=None
        ), mock.patch.object(
            update, "stop_pm2_review_server", return_value=None
        ), mock.patch.object(
            update.shutil, "which", side_effect=lambda name: f"/bin/{name}"
        ), mock.patch.object(
            update.state_relocation,
            "exclusive_maintenance",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            update.state_relocation,
            "relocate_legacy_state",
            return_value=mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update.instance_state, "active_database_path", return_value=update.DB_PATH
        ), mock.patch.object(update, "bind_cutover_state"), mock.patch.object(
            update, "migrate_or_rebuild"
        ), mock.patch.object(update, "start_pm2_review_server") as pm2_start, \
             mock.patch.object(update, "start_docker_review_server") as docker_start, \
             mock.patch.object(update, "require_restarted_runtime_health") as health:
            update.migrate_with_service_cutover()

        pm2_start.assert_called_once_with(None)
        docker_start.assert_called_once_with(("/bin/docker", f"sc-{update.REPO_ROOT.name}"))
        health.assert_called_once_with()
        self.assertFalse(self.intent_path.exists())

    def test_absent_docker_container_is_recreated_through_normal_launch(self):
        completed = [
            mock.Mock(returncode=1, stdout="", stderr="No such container"),
            mock.Mock(returncode=0, stdout="launched", stderr=""),
            mock.Mock(returncode=0, stdout="true\n", stderr=""),
        ]
        with mock.patch.object(
            update.subprocess, "run", side_effect=completed
        ) as run, contextlib.redirect_stdout(io.StringIO()):
            update.start_docker_review_server(("/bin/docker", "sc-example"))

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/bin/docker", "start", "sc-example"],
                [str(update.REPO_ROOT / "sc"), "launch", "--no-build"],
                ["/bin/docker", "inspect", "-f", "{{.State.Running}}", "sc-example"],
            ],
        )

    def test_shutdown_failure_is_named_instead_of_claiming_stopped(self):
        service = ("/usr/bin/pm2", "sc-example")
        with mock.patch.object(
            update, "start_pm2_review_server"
        ), mock.patch.object(
            update, "start_docker_review_server"
        ), mock.patch.object(
            update, "require_restarted_runtime_health",
            side_effect=SystemExit("injected readiness failure"),
        ), mock.patch.object(
            update, "_stop_restarted_runtime",
            return_value="PM2 sc-example: stop denied",
        ), self.assertRaisesRegex(
            SystemExit,
            "shutdown could not be proven:\\n  - PM2 sc-example: stop denied",
        ):
            update.restart_review_servers(service, None)

    def test_first_legacy_adoption_rebinds_snapshot_and_completes_once(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            engine = repo / ".super-coder"
            engine.mkdir(parents=True)
            state_home = Path(raw) / "state"
            environment = {"XDG_STATE_HOME": str(state_home)}
            with mock.patch.dict(os.environ, environment):
                state = update.instance_state.resolve(
                    instance_config=engine / "instance.json",
                    state_home=state_home,
                    id_factory=lambda: "a" * 32,
                )
            legacy_db = engine / "shell_db.db"
            connection = sqlite3.connect(legacy_db)
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations (filename TEXT)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations VALUES ('0001_fixture.sql')"
                )
                connection.execute("CREATE TABLE payload (value TEXT)")
                connection.execute("INSERT INTO payload VALUES ('kept')")
                connection.commit()
            finally:
                connection.close()
            legacy_snapshot = repo / ".sc-state" / "local" / "content.sql"
            legacy_snapshot.parent.mkdir(parents=True)
            legacy_snapshot.write_text("legacy snapshot\n")

            migration_targets = []
            snapshot_targets = []

            def migrate(**_kwargs):
                migration_targets.append(update.DB_PATH)

            def snapshot_body():
                self.assertEqual(os.environ.get("SC_ADMIN"), "1")
                snapshot_targets.append(
                    (snapshot_mod.DB_PATH, snapshot_mod.OUT_PATH)
                )
                snapshot_mod.OUT_PATH.write_text("private snapshot\n")

            service = ("/usr/bin/pm2", "sc-example")
            target_sha = "b" * 40
            with mock.patch.dict(os.environ, environment), mock.patch.multiple(
                update,
                ENGINE=engine,
                REPO_ROOT=repo,
                STATE_DIR=repo / ".sc-state",
                DB_PATH=legacy_db,
                stop_docker_review_server=mock.Mock(return_value=None),
                stop_pm2_review_server=mock.Mock(return_value=service),
                migrate_or_rebuild=mock.Mock(side_effect=migrate),
                refresh_installed_brokers=mock.Mock(),
                sync_skills=mock.Mock(),
                regrant=mock.Mock(return_value=0),
                reconcile_skill_projections=mock.Mock(return_value={
                    "written": [], "skipped": [], "checkouts": []
                }),
                run_script=mock.Mock(),
                publish_engine_ref=mock.Mock(),
                reconcile_linked_dispatchers=mock.Mock(),
                start_pm2_review_server=mock.Mock(),
                start_docker_review_server=mock.Mock(),
                require_restarted_runtime_health=mock.Mock(),
            ), mock.patch.multiple(
                snapshot_mod,
                ENGINE=engine,
                REPO_ROOT=repo,
                DB_PATH=legacy_db,
                OUT_PATH=legacy_snapshot,
                LEGACY_PATH=engine / "snapshot" / "content.sql",
                _main_under_lease=mock.Mock(side_effect=snapshot_body),
            ), mock.patch.object(
                update.rebuild_mod, "DB_PATH", legacy_db
            ), mock.patch.object(
                update.rebuild_mod, "SNAPSHOT", legacy_snapshot
            ), mock.patch.object(
                update.install_mod, "wire_make_aliases", return_value=()
            ), contextlib.redirect_stdout(io.StringIO()):
                update.migrate_with_service_cutover(
                    reconcile=lambda: update.reconcile_under_cutover(
                        source=False,
                        target_sha=target_sha,
                        worktrees=(),
                        target_source="file:///engine-source",
                    )
                )

                self.assertEqual(migration_targets, [state.database])
                self.assertEqual(
                    snapshot_targets, [(state.database, state.snapshot)]
                )
                self.assertEqual(update.rebuild_mod.DB_PATH, state.database)
                self.assertEqual(update.rebuild_mod.SNAPSHOT, state.snapshot)
                self.assertFalse(legacy_db.exists())
                self.assertFalse(legacy_snapshot.exists())
                self.assertTrue(state.database.exists())
                self.assertEqual(state.snapshot.read_text(), "private snapshot\n")
                update.publish_engine_ref.assert_called_once_with(target_sha)
                self.assertEqual(
                    (repo / ".sc-state/engine.source").read_text(),
                    "file:///engine-source\n",
                )
                update.start_pm2_review_server.assert_called_once_with(service)
                update.start_docker_review_server.assert_called_once_with(None)
                update.require_restarted_runtime_health.assert_called_once_with()

    def test_update_uses_only_its_preupdate_backup_class(self):
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "shell_db.db"
            database.write_bytes(b"database")
            with mock.patch.object(update, "DB_PATH", database), mock.patch.object(
                update.rebuild_mod, "backup_existing"
            ) as backup, mock.patch.object(
                update.migrate_mod, "migrate"
            ) as migrate, contextlib.redirect_stdout(io.StringIO()):
                update.migrate_or_rebuild()

        backup.assert_called_once_with(prefix="preupdate")
        migrate.assert_called_once_with(str(database), backup=False)

    def test_main_routes_migration_through_the_service_cutover(self):
        with mock.patch.object(update, "is_source_repo", return_value=True), \
                mock.patch.object(update, "repair_git_worktrees") as repair, \
                mock.patch.object(update, "ensure_workflows", return_value=("source", [])), \
                mock.patch.object(update.install_mod, "ensure_harnesses"), \
                mock.patch.object(update, "expire_sandbox_harnesses", return_value=None), \
                mock.patch.object(
                    update, "migrate_with_service_cutover", side_effect=Stop
                ) as cutover, mock.patch.object(
                    update, "migrate_or_rebuild",
                    side_effect=AssertionError("main bypassed service cutover"),
                ), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(Stop):
            update.main(["--no-fetch"])
        repair.assert_called_once_with()
        cutover.assert_called_once()
        self.assertTrue(callable(cutover.call_args.kwargs["reconcile"]))

    def test_running_pm2_server_stops_before_migration_and_starts_after(self):
        order = []
        service = ("/usr/bin/pm2", "sc-example")
        with mock.patch.object(
            update, "stop_docker_review_server", return_value=None
        ), mock.patch.object(
            update, "stop_pm2_review_server",
            side_effect=lambda: order.append("stop-old") or service,
        ), mock.patch.object(
            update.state_relocation,
            "relocate_legacy_state",
            side_effect=lambda *_args, **_kwargs: order.append("relocate")
            or mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update.instance_state,
            "active_database_path",
            return_value=update.DB_PATH,
        ), mock.patch.object(
            update, "migrate_or_rebuild",
            side_effect=lambda **_kwargs: order.append("migrate"),
        ), mock.patch.object(
            update, "start_pm2_review_server",
            side_effect=lambda value: order.append(("start-new", value)),
        ), mock.patch.object(
            update, "require_restarted_runtime_health",
        ):
            update.migrate_with_service_cutover()
        self.assertEqual(
            order,
            ["stop-old", "relocate", "migrate", ("start-new", service)],
        )

    def test_migration_failure_never_restarts_incompatible_old_code(self):
        service = ("/usr/bin/pm2", "sc-example")
        docker_service = ("/usr/bin/docker", "sc-example")
        with mock.patch.object(
            update, "stop_docker_review_server", return_value=docker_service
        ), mock.patch.object(
            update, "stop_pm2_review_server", return_value=service
        ) as stop, mock.patch.object(
            update.state_relocation,
            "relocate_legacy_state",
            return_value=mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update.instance_state,
            "active_database_path",
            return_value=update.DB_PATH,
        ), mock.patch.object(
            update, "migrate_or_rebuild", side_effect=RuntimeError("migration failed")
        ), mock.patch.object(
            update, "start_pm2_review_server"
        ) as pm2_start, mock.patch.object(
            update, "start_docker_review_server"
        ) as docker_start, \
                self.assertRaisesRegex(RuntimeError, "migration failed"):
            update.migrate_with_service_cutover()
        stop.assert_called_once_with()
        pm2_start.assert_not_called()
        docker_start.assert_not_called()

    def test_reconciliation_finishes_before_lease_release_and_relaunch(self):
        order = []

        @contextlib.contextmanager
        def lease(*_args, **_kwargs):
            order.append("lease-enter")
            try:
                yield
            finally:
                order.append("lease-exit")

        with mock.patch.object(
            update, "stop_docker_review_server",
            side_effect=lambda: order.append("docker-stop") or ("docker", "sc-example"),
        ), mock.patch.object(
            update, "stop_pm2_review_server",
            side_effect=lambda: order.append("pm2-stop") or ("pm2", "sc-example"),
        ), mock.patch.object(
            update.instance_state, "resolve", return_value=mock.Mock()
        ), mock.patch.object(
            update.instance_state, "active_database_path", return_value=update.DB_PATH
        ), mock.patch.object(
            update.instance_state, "active_snapshot_path", return_value=Path("snapshot")
        ), mock.patch.object(
            update.state_relocation, "exclusive_maintenance", side_effect=lease
        ), mock.patch.object(
            update.state_relocation, "relocate_legacy_state",
            return_value=mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update, "migrate_or_rebuild",
            side_effect=lambda **_kwargs: order.append("migrate"),
        ), mock.patch.object(
            update, "start_pm2_review_server",
            side_effect=lambda _value: order.append("pm2-start"),
        ), mock.patch.object(
            update, "start_docker_review_server",
            side_effect=lambda _value: order.append("docker-start"),
        ), mock.patch.object(
            update, "require_restarted_runtime_health",
            side_effect=lambda: order.append("health"),
        ):
            update.migrate_with_service_cutover(
                reconcile=lambda: order.append("reconcile")
            )

        self.assertEqual(
            order,
            [
                "docker-stop", "pm2-stop", "lease-enter", "migrate",
                "reconcile", "lease-exit", "pm2-start", "docker-start", "health",
            ],
        )

    def test_post_stop_failure_keeps_both_runtimes_down(self):
        with mock.patch.object(
            update, "stop_docker_review_server", return_value=("docker", "sc-example")
        ), mock.patch.object(
            update, "stop_pm2_review_server", return_value=("pm2", "sc-example")
        ), mock.patch.object(
            update.state_relocation,
            "exclusive_maintenance",
            side_effect=update.state_relocation.MaintenanceBusy("busy"),
        ), mock.patch.object(
            update, "start_pm2_review_server"
        ) as pm2_start, mock.patch.object(
            update, "start_docker_review_server"
        ) as docker_start, self.assertRaisesRegex(SystemExit, "runtime remains stopped"):
            update.migrate_with_service_cutover()
        pm2_start.assert_not_called()
        docker_start.assert_not_called()

    def test_pm2_lifecycle_targets_only_the_running_repo_server(self):
        completed = [
            mock.Mock(returncode=0, stdout="27182\n", stderr=""),
            mock.Mock(returncode=0, stdout="stopped\n", stderr=""),
            mock.Mock(returncode=0, stdout="started\n", stderr=""),
        ]
        with mock.patch.object(update.shutil, "which", return_value="/bin/pm2"), \
                mock.patch.object(
                    update.ports, "resolve", return_value={"repo": "example"}
                ), mock.patch.object(
                    update.subprocess, "run", side_effect=completed
                ) as run, contextlib.redirect_stdout(io.StringIO()):
            service = update.stop_pm2_review_server()
            update.start_pm2_review_server(service)
        self.assertEqual(service, ("/bin/pm2", "sc-example"))
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/bin/pm2", "pid", "sc-example"],
                ["/bin/pm2", "stop", "sc-example"],
                ["/bin/pm2", "start", "sc-example"],
            ],
        )

    def test_docker_lifecycle_stops_and_restarts_only_the_repo_runtime(self):
        completed = [
            mock.Mock(returncode=0, stdout="true\n", stderr=""),
            mock.Mock(returncode=0, stdout="stopped\n", stderr=""),
            mock.Mock(returncode=0, stdout="started\n", stderr=""),
            mock.Mock(returncode=0, stdout="true\n", stderr=""),
        ]
        with mock.patch.object(
            update.shutil, "which", return_value="/bin/docker"
        ), mock.patch.object(
            update.subprocess, "run", side_effect=completed
        ) as run, contextlib.redirect_stdout(io.StringIO()):
            service = update.stop_docker_review_server()
            update.start_docker_review_server(service)
        container = f"sc-{update.REPO_ROOT.name}"
        self.assertEqual(service, ("/bin/docker", container))
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/bin/docker", "inspect", "-f", "{{.State.Running}}", container],
                ["/bin/docker", "stop", container],
                ["/bin/docker", "start", container],
                ["/bin/docker", "inspect", "-f", "{{.State.Running}}", container],
            ],
        )

    def test_stopped_or_unregistered_pm2_server_is_not_started(self):
        probe = mock.Mock(returncode=0, stdout="0\n", stderr="")
        with mock.patch.object(update.shutil, "which", return_value="/bin/pm2"), \
                mock.patch.object(
                    update.ports, "resolve", return_value={"repo": "example"}
                ), mock.patch.object(
                    update.subprocess, "run", return_value=probe
                ) as run:
            service = update.stop_pm2_review_server()
            update.start_pm2_review_server(service)
        self.assertIsNone(service)
        run.assert_called_once_with(
            ["/bin/pm2", "pid", "sc-example"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_stop_failure_refuses_migration(self):
        completed = [
            mock.Mock(returncode=0, stdout="27182\n", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="stop denied"),
        ]
        with mock.patch.object(update.shutil, "which", return_value="/bin/pm2"), \
                mock.patch.object(update, "stop_docker_review_server", return_value=None), \
                mock.patch.object(
                    update.ports, "resolve", return_value={"repo": "example"}
                ), mock.patch.object(
                    update.subprocess, "run", side_effect=completed
                ), mock.patch.object(update, "migrate_or_rebuild") as migrate, \
                self.assertRaisesRegex(SystemExit, "refusing to migrate"):
            update.migrate_with_service_cutover()
        migrate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
