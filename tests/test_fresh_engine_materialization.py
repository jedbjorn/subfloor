"""Fresh-root admission for the gitignored materialized engine floor."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
install = importlib.import_module("install")
update = importlib.import_module("update")


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class FreshEngineMaterializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "engine-source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Engine Source")
        git(self.source, "config", "user.email", "engine@example.invalid")
        shutil.copy2(ROOT / "sc", self.source / "sc")
        scripts = self.source / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "dispatch.sh").write_text(
            "#!/bin/sh\nprintf 'dispatch:%s\\n' \"$*\"\n"
        )
        (scripts / "install.py").write_text("# installer sentinel\n")
        (self.source / ".super-coder" / "schema.sql").write_text(
            "-- schema sentinel\n"
        )
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", "engine fixture")
        self.ref = git(self.source, "rev-parse", "HEAD")

    def fresh_root(
        self,
        *,
        ref: str | None = None,
        source: str | None = None,
    ) -> Path:
        root = Path(tempfile.mkdtemp(dir=self.base, prefix="downstream-"))
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Downstream")
        git(root, "config", "user.email", "downstream@example.invalid")
        shutil.copy2(ROOT / "sc", root / "sc")
        if ref is not None or source is not None:
            state = root / ".sc-state"
            state.mkdir()
            if ref is not None:
                (state / "engine.ref").write_text(ref + "\n")
            if source is not None:
                (state / "engine.source").write_text(source + "\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "fresh downstream")
        return root

    def run_sc(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("SC_DISPATCH", "SC_CALLER_ROOT")
        }
        return subprocess.run(
            (str(root / "sc"), *args),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_install_materializes_exact_floor_once_then_dispatches_offline(self) -> None:
        root = self.fresh_root(ref=self.ref, source=self.source.as_uri())

        installed = self.run_sc(root, "install", "--fixture")

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn(f"materialized engine {self.ref[:12]}", installed.stdout)
        self.assertIn("dispatch:install --fixture", installed.stdout)
        self.assertTrue((root / ".super-coder/scripts/dispatch.sh").is_file())
        self.assertEqual(
            list((root / ".sc-state/local").glob("engine-bootstrap.*")),
            [],
        )

        moved_source = self.source.with_name("engine-source-offline")
        self.source.rename(moved_source)
        again = self.run_sc(root, "install", "--again")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, "dispatch:install --again\n")

    def test_missing_and_invalid_provenance_fail_without_writes(self) -> None:
        fixtures = (
            (None, None, "missing or unsafe .sc-state directory"),
            (self.ref, None, "missing or unsafe .sc-state/engine.source"),
            ("not-a-ref", self.source.as_uri(), "engine.ref is not a lowercase SHA-1"),
            (self.ref, "relative/source", "not a supported absolute Git locator"),
        )
        for ref, source, message in fixtures:
            with self.subTest(message=message):
                root = self.fresh_root(ref=ref, source=source)
                before = git(root, "status", "--porcelain=v1")
                completed = self.run_sc(root, "install")
                self.assertEqual(completed.returncode, 1)
                self.assertIn(message, completed.stderr)
                self.assertIn("No engine was published", completed.stderr)
                self.assertFalse((root / ".super-coder").exists())
                self.assertEqual(git(root, "status", "--porcelain=v1"), before)

    def test_partial_target_is_never_replaced(self) -> None:
        root = self.fresh_root(ref=self.ref, source=self.source.as_uri())
        partial = root / ".super-coder" / "partial-marker"
        partial.parent.mkdir()
        partial.write_text("retain\n")

        completed = self.run_sc(root, "install")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("engine floor predates this launcher", completed.stderr)
        self.assertEqual(partial.read_text(), "retain\n")
        self.assertFalse((root / ".super-coder/scripts/dispatch.sh").exists())

    def test_linked_worktree_cannot_own_initial_materialization(self) -> None:
        root = self.fresh_root(ref=self.ref, source=self.source.as_uri())
        linked = self.base / "linked"
        git(root, "worktree", "add", "-b", "linked", str(linked))

        completed = self.run_sc(linked, "install")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("linked worktree cannot own", completed.stderr)
        self.assertFalse((linked / ".super-coder").exists())
        self.assertFalse((root / ".super-coder").exists())

    def test_failed_fetch_leaves_no_target_and_retry_can_succeed(self) -> None:
        root = self.fresh_root(
            ref=self.ref,
            source=(self.base / "missing-source").as_uri(),
        )

        failed = self.run_sc(root, "install")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("declared source could not fetch engine.ref", failed.stderr)
        self.assertFalse((root / ".super-coder").exists())
        self.assertEqual(
            list((root / ".sc-state/local").glob("engine-bootstrap.*")),
            [],
        )

        (root / ".sc-state/engine.source").write_text(
            self.source.as_uri() + "\n"
        )
        retried = self.run_sc(root, "install")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("dispatch:install", retried.stdout)

    def test_incomplete_candidate_and_non_install_verb_never_claim_success(self) -> None:
        (self.source / ".super-coder/schema.sql").unlink()
        git(self.source, "add", "-u")
        git(self.source, "commit", "-m", "incomplete engine")
        incomplete_ref = git(self.source, "rev-parse", "HEAD")
        root = self.fresh_root(ref=incomplete_ref, source=self.source.as_uri())

        launch = self.run_sc(root, "launch")
        self.assertEqual(launch.returncode, 1)
        self.assertIn("no engine found", launch.stderr)
        self.assertFalse((root / ".super-coder").exists())

        install = self.run_sc(root, "install")
        self.assertEqual(install.returncode, 1)
        self.assertIn("staged engine is incomplete", install.stderr)
        self.assertIn("No engine was published", install.stderr)
        self.assertNotIn("dispatch:", install.stdout)
        self.assertNotIn("health", install.stdout.lower())
        self.assertFalse((root / ".super-coder").exists())


class EngineProvenancePublicationTest(unittest.TestCase):
    def test_install_publication_commits_source_before_ref_without_pending_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = "https://github.com/jedbjorn/subfloor.git"
            with mock.patch.object(install, "REPO_ROOT", root):
                install.write_engine_provenance("a" * 40, source)

            state = root / ".sc-state"
            self.assertEqual((state / "engine.source").read_text(), source + "\n")
            self.assertEqual((state / "engine.ref").read_text(), "a" * 40 + "\n")
            self.assertFalse((state / "engine.source.pending").exists())
            self.assertFalse((state / "engine.ref.pending").exists())

    def test_update_publication_uses_bound_state_and_ref_commit_point(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".sc-state"
            published: list[str] = []
            with (
                mock.patch.object(update, "STATE_DIR", state),
                mock.patch.object(
                    update,
                    "publish_engine_ref",
                    side_effect=published.append,
                ),
            ):
                update.publish_engine_provenance(
                    "b" * 40, "file:///engine-source"
                )

            self.assertEqual(
                (state / "engine.source").read_text(), "file:///engine-source\n"
            )
            self.assertEqual(published, ["b" * 40])
            self.assertFalse((state / "engine.source.pending").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
