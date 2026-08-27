"""Sprint 28 WU122: compatibility floor and two-hop DSH cutover."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import dsh_removal_cleanup  # noqa: E402
import deepseek_web  # noqa: E402
import test_dsh_removal_preparation as preparation  # noqa: E402
import update  # noqa: E402
import update_cutover  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Cutover Test",
    "GIT_AUTHOR_EMAIL": "cutover@example.invalid",
    "GIT_COMMITTER_NAME": "Cutover Test",
    "GIT_COMMITTER_EMAIL": "cutover@example.invalid",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class TargetInspectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q", "-b", "main")

    def test_bridge_floor_is_independently_recorded_once(self) -> None:
        declaration = self.root / update_cutover.FLOOR_DECLARATION_PATH
        write_json(
            declaration,
            {
                "contract": update_cutover.FLOOR_DECLARATION_CONTRACT,
                "fresh_process_cleanup_hook": True,
                "pre_materialization_hook": True,
                "schema_version": 1,
            },
        )
        bridge_ref = commit(self.root, "bridge")
        marker = self.root / ".sc-state/local/dsh-removal/compatibility-floor.json"

        self.assertTrue(
            update_cutover.install_compatibility_marker(
                bridge_ref, repo_root=self.root, marker_path=marker
            )
        )
        marker_body = json.loads(marker.read_text())
        (self.root / "later.txt").write_text("later\n")
        later_ref = commit(self.root, "later")
        self.assertFalse(
            update_cutover.install_compatibility_marker(
                later_ref, repo_root=self.root, marker_path=marker
            )
        )
        self.assertEqual(
            {
                "contract": "sc-dsh-compatibility-floor-v1",
                "engine_ref": bridge_ref,
                "fresh_process_cleanup_hook": True,
                "pre_materialization_hook": True,
            },
            marker_body,
        )
        self.assertEqual(marker_body, json.loads(marker.read_text()))

    def test_target_cutover_is_read_from_git_without_materialization(self) -> None:
        bridge_ref = "1" * 40
        manifest = self.root / update_cutover.TARGET_MANIFEST_PATH
        write_json(
            manifest,
            {
                "contract": "sc-dsh-removal-manifest-v1",
                "cutover": {
                    "cleanup_hook": ".super-coder/scripts/dsh_removal_cleanup.py",
                    "contract": update_cutover.CUTOVER_CONTRACT,
                    "minimum_floor_ref": bridge_ref,
                },
            },
        )
        target_ref = commit(self.root, "removal")

        plan = update_cutover.inspect_target(target_ref, repo_root=self.root)

        self.assertEqual(
            update_cutover.CutoverPlan(
                target_ref=target_ref,
                compatibility_ref=bridge_ref,
                cleanup_hook=".super-coder/scripts/dsh_removal_cleanup.py",
                manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            ),
            plan,
        )
        self.assertFalse((self.root / ".super-coder/scripts").exists())

    def test_invalid_target_metadata_fails_closed(self) -> None:
        manifest = self.root / update_cutover.TARGET_MANIFEST_PATH
        write_json(
            manifest,
            {
                "cutover": {
                    "cleanup_hook": "../../unsafe.py",
                    "contract": update_cutover.CUTOVER_CONTRACT,
                    "minimum_floor_ref": "1" * 40,
                }
            },
        )
        target_ref = commit(self.root, "unsafe")
        with self.assertRaisesRegex(update_cutover.CutoverError, "cleanup_hook"):
            update_cutover.inspect_target(target_ref, repo_root=self.root)


class PrepareCutoverTest(unittest.TestCase):
    PLAN = update_cutover.CutoverPlan(
        target_ref="2" * 40,
        compatibility_ref="1" * 40,
        cleanup_hook=".super-coder/scripts/dsh_removal_cleanup.py",
        manifest_sha256="3" * 64,
    )

    def test_backup_and_stops_precede_durable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            backup_path = root / "shell_db.preupdate.db"
            backup_path.write_bytes(b"db")
            receipt = root / "cutover.json"
            events: list[str] = []
            before = {
                "web_pid": 41,
                "web_start_ticks": 410,
                "service_port": 18977,
                "relay_pid": 42,
                "relay_start_ticks": 420,
                "relay_port": 8977,
            }

            with mock.patch.object(update_cutover, "require_compatibility_floor"):
                prepared = update_cutover.prepare_cutover(
                    self.PLAN,
                    current_ref="1" * 40,
                    backup=lambda: events.append("backup") or backup_path,
                    stop_review=lambda: events.append("stop-review")
                    or ("/bin/pm2", "sc-example"),
                    start_review=lambda _service: events.append("restart-review"),
                    quiesce=lambda: events.append("quiesce")
                    or (before, {"relay": True, "stopped": True, "web": True}),
                    capture_ownership=lambda _plan, _before: [],
                    receipt_path=receipt,
                )

            body = json.loads(receipt.read_text())
            self.assertEqual(["backup", "stop-review", "quiesce"], events)
            self.assertEqual(backup_path, prepared.backup_path)
            self.assertEqual(
                {
                    "relay_pid": 42,
                    "relay_port": 8977,
                    "relay_start_ticks": 420,
                    "service_port": 18977,
                    "web_pid": 41,
                    "web_start_ticks": 410,
                },
                body["process_identities"],
            )
            self.assertEqual("sc-dsh-cutover-receipt-v1", body["contract"])
            self.assertEqual("2" * 40, body["target_ref"])
            self.assertEqual("1" * 40, body["compatibility_ref"])
            self.assertEqual([], [path for path in root.iterdir() if path.suffix == ".pending"])

    def test_quiescence_refusal_restarts_review_and_publishes_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            backup_path = root / "shell_db.preupdate.db"
            backup_path.write_bytes(b"db")
            receipt = root / "cutover.json"
            events: list[object] = []
            service = ("/bin/pm2", "sc-example")

            with mock.patch.object(update_cutover, "require_compatibility_floor"), \
                    self.assertRaisesRegex(RuntimeError, "busy"):
                update_cutover.prepare_cutover(
                    self.PLAN,
                    current_ref="1" * 40,
                    backup=lambda: backup_path,
                    stop_review=lambda: events.append("stop") or service,
                    start_review=lambda value: events.append(("restart", value)),
                    quiesce=mock.Mock(side_effect=RuntimeError("busy")),
                    capture_ownership=lambda _plan, _before: [],
                    receipt_path=receipt,
                )

            self.assertEqual(["stop", ("restart", service)], events)
            self.assertFalse(receipt.exists())

    def test_quiesce_uses_dsh_owner_lock_and_proves_both_ports_released(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            engine = Path(raw) / ".super-coder"
            state_path = engine / "run/deepseek-web.json"
            write_json(
                state_path,
                {
                    "relay_pid": 42,
                    "relay_port": 8977,
                    "relay_start_ticks": 420,
                    "service_port": 18977,
                    "web_pid": 41,
                    "web_start_ticks": 410,
                },
            )
            events: list[object] = []

            @contextlib.contextmanager
            def owner_lock():
                events.append("lock-enter")
                yield
                events.append("lock-exit")

            with mock.patch.object(update_cutover, "ENGINE", engine), mock.patch.object(
                deepseek_web, "_service_lock", owner_lock
            ), mock.patch.object(
                deepseek_web,
                "_stop_unlocked",
                side_effect=lambda: events.append("exact-stop")
                or {"relay": True, "stopped": True, "web": True},
            ), mock.patch.object(
                deepseek_web,
                "_tcp_ready",
                side_effect=lambda _host, port: events.append(("port", port)) or False,
            ):
                before, outcome = update_cutover.quiesce_dsh()

            self.assertEqual(41, before["web_pid"])
            self.assertEqual(True, outcome["web"])
            self.assertEqual(
                [
                    "lock-enter",
                    "exact-stop",
                    "lock-exit",
                    ("port", 18977),
                    ("port", 8977),
                ],
                events,
            )

    def test_quiesce_refuses_when_an_owned_port_survives(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            engine = Path(raw) / ".super-coder"
            write_json(
                engine / "run/deepseek-web.json",
                {"relay_port": 8977, "service_port": 18977},
            )
            with mock.patch.object(update_cutover, "ENGINE", engine), mock.patch.object(
                deepseek_web, "_service_lock", contextlib.nullcontext
            ), mock.patch.object(
                deepseek_web,
                "_stop_unlocked",
                return_value={"relay": True, "stopped": True, "web": True},
            ), mock.patch.object(
                deepseek_web, "_tcp_ready", side_effect=[False, True]
            ), self.assertRaisesRegex(update_cutover.CutoverError, "8977"):
                update_cutover.quiesce_dsh()


class HalfAdoptedGuardTest(unittest.TestCase):
    def test_only_exact_completed_cleanup_admits_materialized_removal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "removal.json"
            engine_ref = root / "engine.ref"
            receipt = root / "cleanup.json"
            target_ref = "2" * 40
            compatibility_ref = "1" * 40
            engine_ref.write_text(target_ref + "\n")
            write_json(
                manifest,
                {
                    "cutover": {
                        "contract": update_cutover.CUTOVER_CONTRACT,
                        "minimum_floor_ref": compatibility_ref,
                    }
                },
            )

            self.assertFalse(
                update_cutover.installed_removal_ready(
                    engine_ref_path=engine_ref,
                    manifest_path=manifest,
                    cleanup_receipt_path=receipt,
                )
            )
            write_json(
                receipt,
                {
                    "compatibility_ref": compatibility_ref,
                    "contract": "sc-dsh-cleanup-receipt-v1",
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    "status": "complete",
                    "target_ref": target_ref,
                },
            )
            self.assertTrue(
                update_cutover.installed_removal_ready(
                    engine_ref_path=engine_ref,
                    manifest_path=manifest,
                    cleanup_receipt_path=receipt,
                )
            )
            receipt_body = json.loads(receipt.read_text())
            receipt_body["target_ref"] = "3" * 40
            write_json(receipt, receipt_body)
            self.assertFalse(
                update_cutover.installed_removal_ready(
                    engine_ref_path=engine_ref,
                    manifest_path=manifest,
                    cleanup_receipt_path=receipt,
                )
            )

    def test_half_adopted_floor_allows_only_update_and_rollback(self) -> None:
        with mock.patch.object(
            update_cutover, "installed_removal_ready", return_value=False
        ), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(0, update_cutover.admit_dispatch(["update"]))
            self.assertEqual(0, update_cutover.admit_dispatch(["rollback"]))
            self.assertEqual(78, update_cutover.admit_dispatch(["launch"]))

        self.assertIn("half-adopted", error.getvalue())

    def test_stable_sc_runs_cutover_guard_before_any_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            shutil.copy2(ROOT / "sc", root / "sc")
            scripts = root / ".super-coder/scripts"
            scripts.mkdir(parents=True)
            log = root / "guard.log"
            (scripts / "update_cutover.py").write_text(
                "import pathlib,sys\n"
                f"pathlib.Path({str(log)!r}).write_text(repr(sys.argv[1:]))\n"
                "raise SystemExit(78)\n"
            )
            (scripts / "dispatch.sh").write_text(
                f"#!/bin/sh\nprintf dispatch > {str(root / 'dispatch.log')!r}\n"
            )

            completed = subprocess.run(
                ["sh", str(root / "sc"), "launch"],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(78, completed.returncode)
            self.assertEqual("['--admit-dispatch', '--', 'launch']", log.read_text())
            self.assertFalse((root / "dispatch.log").exists())


class CleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q", "-b", "main")
        self.first = self.root / ".super-coder/scripts/deepseek_one.py"
        self.second = self.root / ".super-coder/scripts/deepseek_two.py"
        self.first.parent.mkdir(parents=True)
        self.first.write_text("one\n")
        self.second.write_text("two\n")
        self.first_digest = hashlib.sha256(self.first.read_bytes()).hexdigest()
        self.second_digest = hashlib.sha256(self.second.read_bytes()).hexdigest()
        self.compatibility_ref = commit(self.root, "compatibility")
        self.manifest = self.root / ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
        self.first.unlink()
        self.second.unlink()
        write_json(
            self.manifest,
            {
                "contract": "sc-dsh-removal-manifest-v1",
                "cutover": {
                    "cleanup_hook": ".super-coder/scripts/dsh_removal_cleanup.py",
                    "contract": dsh_removal_cleanup.CUTOVER_CONTRACT,
                    "minimum_floor_ref": self.compatibility_ref,
                },
                "generated_artifacts": [],
                "tracked_artifacts": [
                    {
                        "path": ".super-coder/scripts/deepseek_one.py",
                        "sha256": self.first_digest,
                    },
                    {
                        "path": ".super-coder/scripts/deepseek_two.py",
                        "sha256": self.second_digest,
                    },
                ],
            },
        )
        self.target_ref = commit(self.root, "removal target")
        self.first.write_text("one\n")
        self.second.write_text("two\n")
        self.cutover_receipt = self.root / ".sc-state/cutover.json"
        self.cleanup_receipt = self.root / ".sc-state/cleanup.json"
        write_json(
            self.cutover_receipt,
            {
                "compatibility_ref": self.compatibility_ref,
                "contract": dsh_removal_cleanup.RECEIPT_CONTRACT,
                "generated_ownership": [],
                "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                "target_ref": self.target_ref,
            },
        )

    def run_cleanup(self) -> dict:
        return dsh_removal_cleanup.cleanup(
            self.target_ref,
            self.cutover_receipt,
            self.cleanup_receipt,
            root=self.root,
            manifest_path=self.manifest,
        )

    def test_exact_stale_files_delete_and_unrelated_file_survives(self) -> None:
        unrelated = self.root / ".super-coder/scripts/opencode.py"
        unrelated.write_text("retain\n")

        receipt = self.run_cleanup()

        self.assertFalse(self.first.exists())
        self.assertFalse(self.second.exists())
        self.assertEqual("retain\n", unrelated.read_text())
        self.assertEqual("complete", receipt["status"])
        self.assertEqual(2, len(receipt["tracked"]))
        self.assertEqual([], receipt["errors"])

    def test_digest_mismatch_refuses_before_any_delete(self) -> None:
        self.second.write_text("tampered\n")

        with self.assertRaisesRegex(
            dsh_removal_cleanup.CleanupError, "digest mismatch"
        ):
            self.run_cleanup()

        self.assertEqual("one\n", self.first.read_text())
        self.assertEqual("tampered\n", self.second.read_text())
        receipt = json.loads(self.cleanup_receipt.read_text())
        self.assertEqual("refused", receipt["status"])
        self.assertEqual(
            ["tracked artifact digest mismatch: .super-coder/scripts/deepseek_two.py"],
            receipt["errors"],
        )

    def test_symlink_refuses_without_touching_target_or_sibling(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside\n")
        self.first.unlink()
        self.first.symlink_to(outside)

        with self.assertRaisesRegex(dsh_removal_cleanup.CleanupError, "symlink"):
            self.run_cleanup()

        self.assertTrue(self.first.is_symlink())
        self.assertEqual("outside\n", outside.read_text())
        self.assertEqual("two\n", self.second.read_text())

    def test_partial_delete_restores_exact_compatibility_bytes(self) -> None:
        original_unlink = Path.unlink

        def fail_second(path: Path, *args, **kwargs):
            if path == self.second:
                raise OSError("injected partial delete")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_second), self.assertRaisesRegex(
            dsh_removal_cleanup.CleanupError, "injected partial delete"
        ):
            self.run_cleanup()

        self.assertEqual(self.first_digest, hashlib.sha256(self.first.read_bytes()).hexdigest())
        self.assertEqual(self.second_digest, hashlib.sha256(self.second.read_bytes()).hexdigest())
        receipt = json.loads(self.cleanup_receipt.read_text())
        self.assertEqual("restored", receipt["status"])
        self.assertEqual([], [row for row in receipt["errors"] if "restore" in row])

    def test_generated_tree_unexpected_child_refuses_without_deletion(self) -> None:
        generated = self.root / ".super-coder/run/deepseek"
        generated.mkdir(parents=True)
        owned = generated / "owned.json"
        unexpected = generated / "unexpected.txt"
        owned.write_text("owned\n")
        unexpected.write_text("unexpected\n")
        manifest = json.loads(self.manifest.read_text())
        manifest["tracked_artifacts"] = []
        manifest["generated_artifacts"] = [
            {
                "kind": "bounded-tree",
                "ownership": "test exact snapshot",
                "path": ".super-coder/run/deepseek",
            }
        ]
        write_json(self.manifest, manifest)
        receipt = json.loads(self.cutover_receipt.read_text())
        receipt["manifest_sha256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        receipt["generated_ownership"] = [
            {
                "declared_path": ".super-coder/run/deepseek",
                "directories": [],
                "kind": "bounded-tree",
                "paths": [
                    {
                        "path": ".super-coder/run/deepseek/owned.json",
                        "sha256": hashlib.sha256(owned.read_bytes()).hexdigest(),
                        "type": "file",
                    }
                ],
                "roots": [".super-coder/run/deepseek"],
            }
        ]
        write_json(self.cutover_receipt, receipt)

        with self.assertRaisesRegex(
            dsh_removal_cleanup.CleanupError, "unexpected child"
        ):
            self.run_cleanup()

        self.assertEqual("owned\n", owned.read_text())
        self.assertEqual("unexpected\n", unexpected.read_text())


class UpdateIntegrationTest(unittest.TestCase):
    OLD = "1" * 40
    NEW = "2" * 40
    PLAN = update_cutover.CutoverPlan(
        target_ref=NEW,
        compatibility_ref=OLD,
        cleanup_hook=".super-coder/scripts/dsh_removal_cleanup.py",
        manifest_sha256="3" * 64,
    )

    def _main_stack(
        self,
        root: Path,
        events: list[str],
        *,
        prepare_error=None,
        fail_at: str | None = None,
    ):
        state = root / ".sc-state"
        state.mkdir()
        ref = state / "engine.ref"
        ref.write_text(self.OLD + "\n")
        backup = root / "backup.db"
        backup.write_bytes(b"db")
        prepared = update_cutover.PreparedCutover(
            self.PLAN,
            backup,
            ("/bin/pm2", "sc-example"),
            {"web_pid": 41},
        )

        def record(name, result=None):
            def action(*_args, **_kwargs):
                events.append(name)
                if fail_at == name:
                    raise RuntimeError(f"{name} failed")
                return result
            return action

        prepare = mock.Mock(side_effect=prepare_error) if prepare_error else mock.Mock(
            side_effect=lambda *_args, **_kwargs: events.append("prepare") or prepared
        )
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.multiple(
                update,
                REPO_ROOT=root,
                STATE_DIR=state,
                ENGINE_REF=ref,
                ENGINE_REF_PREV=state / "engine.ref.prev",
                EJECTED_MARKER=state / "ejected",
                is_source_repo=mock.Mock(return_value=False),
                repair_git_worktrees=mock.Mock(return_value=()),
                sync_repo_checkout=mock.Mock(),
                fetch_update_ref=mock.Mock(return_value=self.NEW),
                migrate_engine_untrack=mock.Mock(),
                migrate_generated_artifacts_local=mock.Mock(),
                materialize_fetched_engine=mock.Mock(side_effect=record("materialize")),
                ensure_workflows=mock.Mock(return_value=("current", [])),
                expire_sandbox_harnesses=mock.Mock(return_value=None),
                migrate_or_rebuild=mock.Mock(side_effect=record("migrate")),
                migrate_with_service_cutover=mock.Mock(
                    side_effect=AssertionError("ordinary cutover used")
                ),
                refresh_installed_brokers=mock.Mock(),
                sync_skills=mock.Mock(),
                regrant=mock.Mock(return_value=0),
                reconcile_skill_projections=mock.Mock(
                    return_value={"written": [], "skipped": [], "checkouts": []}
                ),
                run_script=mock.Mock(
                    side_effect=lambda name, **_kwargs: events.append(name)
                ),
                start_pm2_review_server=mock.Mock(side_effect=record("start-review")),
                reconcile_linked_dispatchers=mock.Mock(side_effect=record("reconcile")),
                publish_engine_ref=mock.Mock(side_effect=record("publish")),
                _restore_failed_cutover=mock.Mock(
                    side_effect=record("restore-pair")
                ),
            )
        )
        stack.enter_context(
            mock.patch.multiple(
                update.update_cutover,
                inspect_target=mock.Mock(
                    side_effect=lambda _ref: events.append("inspect") or self.PLAN
                ),
                prepare_cutover=prepare,
                run_cleanup=mock.Mock(side_effect=record("cleanup", {})),
            )
        )
        stack.enter_context(
            mock.patch.multiple(
                update.install_mod,
                ensure_gitignore=mock.Mock(return_value=False),
                ensure_harnesses=mock.Mock(),
                wire_make_aliases=mock.Mock(return_value=()),
            )
        )
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack

    def test_second_hop_orders_cleanup_and_publishes_ref_last(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            events: list[str] = []
            stack = self._main_stack(Path(raw), events)
            with stack:
                update.main([])

        self.assertEqual(
            [
                "inspect",
                "prepare",
                "materialize",
                "cleanup",
                "migrate",
                "map_setup.py",
                "snapshot.py",
                "start-review",
                "reconcile",
                "publish",
            ],
            events,
        )

    def test_direct_skip_refuses_before_materialization_or_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events: list[str] = []
            error = update_cutover.CutoverError(
                f"DSH removal requires compatibility floor {self.OLD}"
            )
            stack = self._main_stack(root, events, prepare_error=error)
            with stack, self.assertRaisesRegex(SystemExit, "compatibility floor"):
                update.main([])

            self.assertEqual(["inspect"], events)
            self.assertEqual(
                self.OLD + "\n", (root / ".sc-state/engine.ref").read_text()
            )

    def test_migration_and_publication_failures_pair_restore(self) -> None:
        for failed in ("migrate", "publish"):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as raw:
                events: list[str] = []
                stack = self._main_stack(Path(raw), events, fail_at=failed)
                with stack, self.assertRaisesRegex(RuntimeError, f"{failed} failed"):
                    update.main([])

                self.assertEqual("restore-pair", events[-1])
                if failed == "migrate":
                    self.assertNotIn("publish", events)
                else:
                    self.assertEqual(["publish", "restore-pair"], events[-2:])


class InstalledTwoHopFixtureTest(unittest.TestCase):
    def test_installed_bridge_then_fresh_process_cleanup_reaches_migration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            git(root, "init", "-q", "-b", "main")
            fixture = preparation.load(preparation.PRE_BRIDGE_PATH)
            preparation.materialize_fixture(root, fixture)
            declaration = root / update_cutover.FLOOR_DECLARATION_PATH
            declaration.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                ROOT / update_cutover.FLOOR_DECLARATION_PATH,
                declaration,
            )
            bridge_ref = commit(root, "installed compatibility floor")
            engine_ref = root / ".sc-state/engine.ref"
            engine_ref.write_text(bridge_ref + "\n")
            marker = root / ".sc-state/local/dsh-removal/compatibility-floor.json"
            manifest_source = preparation.load(preparation.MANIFEST_PATH)
            before = {
                row["path"]: preparation.FIXTURES.sha256_file(root / row["path"])
                for row in manifest_source["tracked_artifacts"]
            }

            self.assertTrue(
                update_cutover.install_compatibility_marker(
                    bridge_ref, repo_root=root, marker_path=marker
                )
            )
            self.assertEqual(
                before,
                {
                    row["path"]: preparation.FIXTURES.sha256_file(root / row["path"])
                    for row in manifest_source["tracked_artifacts"]
                },
            )
            self.assertTrue((root / ".super-coder/run/deepseek-web.json").is_file())

            target_manifest = dict(manifest_source)
            target_manifest["cutover"] = {
                "cleanup_hook": ".super-coder/scripts/dsh_removal_cleanup.py",
                "contract": update_cutover.CUTOVER_CONTRACT,
                "minimum_floor_ref": bridge_ref,
            }
            materialized_manifest = (
                root / ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
            )
            write_json(materialized_manifest, target_manifest)
            cleanup_script = root / ".super-coder/scripts/dsh_removal_cleanup.py"
            shutil.copy2(SCRIPTS / "dsh_removal_cleanup.py", cleanup_script)
            for row in target_manifest["tracked_artifacts"]:
                (root / row["path"]).unlink()
            target_ref = commit(root, "materialized removal target")

            preparation.extract_payload(root)
            database = root / ".super-coder/shell_db.db"
            live = sqlite3.connect(database)
            self.addCleanup(live.close)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE proof(value TEXT NOT NULL)")
            live.execute("INSERT INTO proof VALUES ('preserved')")
            live.commit()
            backup_path = root / ".sc-state/backups/shell_db.preupdate.db"
            backup_path.parent.mkdir(parents=True)
            events: list[str] = []

            def backup() -> Path:
                with sqlite3.connect(backup_path) as target:
                    live.backup(target)
                events.append("backup")
                return backup_path

            plan = update_cutover.inspect_target(target_ref, repo_root=root)
            self.assertIsNotNone(plan)
            assert plan is not None
            cutover_receipt = root / ".sc-state/local/dsh-removal/cutover-receipt.json"
            cleanup_receipt = root / ".sc-state/local/dsh-removal/cleanup-receipt.json"
            runtime = json.loads((root / ".super-coder/run/deepseek-web.json").read_text())
            prepared = update_cutover.prepare_cutover(
                plan,
                current_ref=bridge_ref,
                backup=backup,
                stop_review=lambda: events.append("stop-review") or None,
                quiesce=lambda: events.append("quiesce")
                or (runtime, {"relay": True, "stopped": True, "web": True}),
                receipt_path=cutover_receipt,
                marker_path=marker,
                repo_root=root,
            )
            events.append("materialize")
            receipt = update_cutover.run_cleanup(
                prepared,
                engine=root / ".super-coder",
                receipt_path=cutover_receipt,
                cleanup_receipt_path=cleanup_receipt,
            )
            events.append("cleanup")
            with sqlite3.connect(backup_path) as restored:
                self.assertEqual(
                    "preserved", restored.execute("SELECT value FROM proof").fetchone()[0]
                )
            events.append("migration")
            engine_ref.write_text(target_ref + "\n")
            events.append("publish")

            self.assertEqual(
                [
                    "backup",
                    "stop-review",
                    "quiesce",
                    "materialize",
                    "cleanup",
                    "migration",
                    "publish",
                ],
                events,
            )
            self.assertEqual("complete", receipt["status"])
            self.assertEqual(target_ref + "\n", engine_ref.read_text())
            self.assertEqual(
                [],
                [
                    row["path"]
                    for row in target_manifest["tracked_artifacts"]
                    if (root / row["path"]).exists()
                ],
            )


class PairRecoveryTest(unittest.TestCase):
    def test_failed_target_restores_engine_database_and_dispatcher_pair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            git(root, "init", "-q", "-b", "main")
            scripts = root / ".super-coder/scripts"
            scripts.mkdir(parents=True)
            (root / "sc").write_text("bridge dispatcher\n")
            (scripts / "engine_manifest.py").write_text(
                'ENGINE_PATHS = ["sc", ".super-coder/scripts"]\n'
            )
            (scripts / "bridge_only.py").write_text("bridge\n")
            bridge_ref = commit(root, "bridge")
            (root / "sc").write_text("target dispatcher\n")
            (scripts / "bridge_only.py").unlink()
            (scripts / "target_only.py").write_text("target\n")
            target_ref = commit(root, "target")

            state = root / ".sc-state"
            state.mkdir()
            engine_ref = state / "engine.ref"
            engine_ref.write_text(bridge_ref + "\n")
            engine_ref_prev = state / "engine.ref.prev"
            engine_ref_prev.write_text(bridge_ref + "\n")
            database = root / ".super-coder/shell_db.db"
            database.write_bytes(b"target database")
            Path(str(database) + "-wal").write_bytes(b"wal")
            Path(str(database) + "-shm").write_bytes(b"shm")
            backup = root / "shell_db.preupdate.db"
            backup.write_bytes(b"bridge database")
            cutover_receipt = state / "local/dsh-removal/cutover-receipt.json"
            write_json(cutover_receipt, {"contract": "sc-dsh-cutover-receipt-v1"})
            plan = update_cutover.CutoverPlan(
                target_ref=target_ref,
                compatibility_ref=bridge_ref,
                cleanup_hook=".super-coder/scripts/dsh_removal_cleanup.py",
                manifest_sha256="3" * 64,
            )
            prepared = update_cutover.PreparedCutover(
                plan,
                backup,
                ("/bin/pm2", "sc-example"),
                {"web_pid": 41},
            )
            materialized_manifest = root / ".super-coder/engine.manifest"

            with mock.patch.multiple(
                update,
                REPO_ROOT=root,
                ENGINE=root / ".super-coder",
                DB_PATH=database,
                STATE_DIR=state,
                ENGINE_REF=engine_ref,
                ENGINE_REF_PREV=engine_ref_prev,
                start_pm2_review_server=mock.Mock(),
                reconcile_linked_dispatchers=mock.Mock(),
            ), mock.patch.multiple(
                update.engine_manifest,
                REPO_ROOT=root,
                MANIFEST=materialized_manifest,
            ), mock.patch.object(
                update.callable_floor, "require_callable_floor"
            ), mock.patch.object(
                update.update_cutover, "CUTOVER_RECEIPT", cutover_receipt
            ), contextlib.redirect_stdout(io.StringIO()):
                update._restore_failed_cutover(prepared)

                update.start_pm2_review_server.assert_called_once_with(
                    ("/bin/pm2", "sc-example")
                )
                update.reconcile_linked_dispatchers.assert_called_once_with(bridge_ref)

            self.assertEqual("bridge dispatcher\n", (root / "sc").read_text())
            self.assertEqual("bridge\n", (scripts / "bridge_only.py").read_text())
            self.assertFalse((scripts / "target_only.py").exists())
            self.assertEqual(b"bridge database", database.read_bytes())
            self.assertFalse(Path(str(database) + "-wal").exists())
            self.assertFalse(Path(str(database) + "-shm").exists())
            self.assertEqual(bridge_ref + "\n", engine_ref.read_text())
            self.assertFalse(engine_ref_prev.exists())
            self.assertEqual(
                "pair-restored",
                json.loads(cutover_receipt.read_text())["recovery"]["status"],
            )
            self.assertEqual(
                {
                    ".super-coder/scripts/bridge_only.py",
                    ".super-coder/scripts/engine_manifest.py",
                    "sc",
                },
                set(json.loads(materialized_manifest.read_text())),
            )


if __name__ == "__main__":
    unittest.main()
