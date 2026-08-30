"""The updater quiesces a PM2 review server around DB migration."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402


class Stop(Exception):
    """Sentinel proving main reached the service cutover seam."""


class UpdateServiceCutoverTest(unittest.TestCase):
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
        with mock.patch.object(
            update, "stop_docker_review_server", return_value=None
        ), mock.patch.object(
            update, "stop_pm2_review_server", return_value=service
        ) as stop, mock.patch.object(
            update.state_relocation,
            "relocate_legacy_state",
            return_value=mock.Mock(database=update.DB_PATH),
        ), mock.patch.object(
            update, "migrate_or_rebuild", side_effect=RuntimeError("migration failed")
        ), mock.patch.object(update, "start_pm2_review_server") as start, \
                self.assertRaisesRegex(RuntimeError, "migration failed"):
            update.migrate_with_service_cutover()
        stop.assert_called_once_with()
        start.assert_not_called()

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
