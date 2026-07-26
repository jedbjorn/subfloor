#!/usr/bin/env python3
"""Guards for the engine allow-list materialized by ``./sc update``.

Two bug classes live here. First, a new tracked top-level file or directory
missing from ENGINE_PATHS never reaches any fork. Second, an updating fork can
run an installed ``update.py`` whose list predates the target ref's list,
silently laying down the new code without its newly declared path for one
update. The source-repo coverage checks pin the first class; two-commit fixture
repos pin target-ref resolution across the version gap, including the retired
path mirror and warned fallback legs.

Known subdirs (scripts/, templates/, …) are materialized whole, so files inside
them are covered; only new top-level files and subdirs need new allow-list
entries. Deliberate per-instance and super-coder-only exclusions remain outside
the list.

Run:
    python3 tests/test_update_materialize.py
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402

# Deliberately NOT materialized — per-instance / gitignored / runtime, per the
# ENGINE_PATHS comment in update.py.
PER_INSTANCE = {
    "instance.json", "shell_db.db", "shell_db.db-wal", "shell_db.db-shm", "map.db",
    "source-policy.json",  # source-repo-only artifact publication policy
    "engine.manifest",  # derived hash baseline — rewritten by each materialize
}

# Tracked upstream, deliberately NOT materialized to forks (file or dir
# prefix): assets/seed/ is super-coder-only (stripped on install) — see the
# ENGINE_PATHS comment in engine_manifest.py.
NOT_MATERIALIZED = (
    ".super-coder/assets/seed/",
    ".super-coder/source-policy.json",
)


def _covered(rel: str) -> bool:
    """True when an ENGINE_PATHS entry archives `rel` (exact file or dir
    prefix — git archive emits whole trees for directory pathspecs)."""
    return any(rel == entry or rel.startswith(entry.rstrip("/") + "/")
               for entry in update.ENGINE_PATHS)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class EnginePathsCoverageTest(unittest.TestCase):
    def test_head_allow_list_is_a_literal_matching_the_export(self):
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            resolved = update._engine_paths_for("HEAD", repo_root=ROOT)

        self.assertEqual(warning.getvalue(), "")
        self.assertEqual(resolved, update.engine_manifest.ENGINE_PATHS)

    def test_every_top_level_engine_file_is_materialized(self):
        listed = set(update.ENGINE_PATHS)
        missing = []
        for entry in sorted(ENGINE.iterdir()):
            if not entry.is_file():
                continue
            if entry.name in PER_INSTANCE or entry.suffix == ".pyc":
                continue
            rel = f".super-coder/{entry.name}"
            if rel not in listed:
                missing.append(rel)
        self.assertEqual(
            missing, [],
            f"top-level engine file(s) absent from update.ENGINE_PATHS — forks "
            f"won't receive them on `./sc update`: {missing}")

    def test_map_schema_specifically_present(self):
        # The file whose omission caused the dos-arch update failure.
        self.assertIn(".super-coder/map_schema.sql", update.ENGINE_PATHS)

    def test_shadow_specifically_present(self):
        # The dir whose omission broke the Interface on every fresh fork (#59).
        self.assertIn(".super-coder/shadow", update.ENGINE_PATHS)

    def test_every_tracked_engine_file_is_materialized(self):
        """The recurrence guard for the class: any git-tracked file under
        .super-coder/ — including one inside a NEW subdirectory — must be
        covered by ENGINE_PATHS, or explicitly opted out in NOT_MATERIALIZED.
        git ls-files lists exactly the upstream-owned set a fork can receive,
        so a new engine dir can't silently miss materialization."""
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--", ".super-coder"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        missing = [rel for rel in tracked
                   if not _covered(rel)
                   and not any(rel.startswith(prefix) for prefix in NOT_MATERIALIZED)]
        self.assertEqual(
            missing, [],
            f"tracked engine file(s) absent from update.ENGINE_PATHS — forks "
            f"won't receive them on `./sc update` (add the path to ENGINE_PATHS "
            f"or opt out in NOT_MATERIALIZED): {missing}")


class EnginePathsAtRefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / ".super-coder" / "scripts"
        self.scripts.mkdir(parents=True)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Update Test")
        _git(self.root, "config", "user.email", "update@example.invalid")

    def commit(self, message: str) -> str:
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def write_manifest(self, paths: list[str]) -> None:
        (self.scripts / "engine_manifest.py").write_text(
            "ENGINE_PATHS = " + repr(paths) + "\n"
        )

    def test_added_path_resolves_from_target_ref_and_materializes(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "floor.txt").write_text("old floor\n")
        old_sha = self.commit("old allow-list")

        target = [*installed, ".super-coder/new-path"]
        (self.root / "sc").write_text("new dispatcher\n")
        self.write_manifest(target)
        (self.scripts / "floor.txt").write_text("new floor\n")
        new_file = self.root / ".super-coder" / "new-path" / "new.txt"
        new_file.parent.mkdir()
        new_file.write_text("new path content\n")
        new_sha = self.commit("add engine path")
        _git(self.root, "checkout", old_sha)
        self.assertFalse(new_file.exists())

        output = io.StringIO()
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE_PATHS=installed,
        ), contextlib.redirect_stdout(output):
            resolved = update._engine_paths_for(
                new_sha, repo_root=self.root
            )
            update.materialize_engine(new_sha)

        self.assertEqual(resolved, target)
        self.assertEqual(new_file.read_text(), "new path content\n")
        self.assertEqual((self.root / "sc").read_text(), "new dispatcher\n")
        self.assertIn(
            "1 engine path(s) newly materialized at "
            f"{new_sha[:12]}: .super-coder/new-path",
            output.getvalue(),
        )

    def test_retired_path_is_not_materialized_and_is_reported(self):
        base = ["sc", ".super-coder/scripts"]
        installed = [*base, ".super-coder/retired-path"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        retired_file = (
            self.root / ".super-coder" / "retired-path" / "retired.txt"
        )
        retired_file.parent.mkdir()
        retired_file.write_text("old upstream content\n")
        old_sha = self.commit("path still installed")

        (self.root / "sc").write_text("new dispatcher\n")
        self.write_manifest(base)
        retired_file.write_text("tracked but no longer engine-owned\n")
        new_sha = self.commit("retire engine path")
        _git(self.root, "checkout", old_sha)
        retired_file.write_text("fork sentinel\n")

        output = io.StringIO()
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE_PATHS=installed,
        ), contextlib.redirect_stdout(output):
            resolved = update._engine_paths_for(
                new_sha, repo_root=self.root
            )
            update.materialize_engine(new_sha)

        self.assertEqual(resolved, base)
        self.assertNotIn(".super-coder/retired-path", resolved)
        self.assertEqual(
            retired_file.read_text(),
            "fork sentinel\n",
            "a target-retired path must stay outside the archive",
        )
        self.assertIn(
            "1 installed engine path(s) retired at "
            f"{new_sha[:12]} — skipping: .super-coder/retired-path",
            output.getvalue(),
        )

    def test_ref_before_manifest_module_uses_update_literal(self):
        (self.root / "README").write_text("precursor\n")
        self.commit("before engine files")

        legacy = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("legacy dispatcher\n")
        (self.scripts / "update.py").write_text(
            "ENGINE_PATHS = " + repr(legacy) + "\n"
        )
        legacy_sha = self.commit("legacy update literal")

        warning = io.StringIO()
        with mock.patch.object(update, "ENGINE_PATHS", ["sc"]), \
                contextlib.redirect_stderr(warning):
            resolved = update._engine_paths_for(
                legacy_sha, repo_root=self.root
            )

        self.assertEqual(resolved, legacy)
        self.assertEqual(warning.getvalue(), "")

    def test_unparseable_target_list_warns_and_uses_installed_list(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        self.commit("literal allow-list")

        (self.scripts / "engine_manifest.py").write_text(
            "ENGINE_PATHS = load_paths()\n"
        )
        target_sha = self.commit("dynamic allow-list")

        warning = io.StringIO()
        with mock.patch.object(update, "ENGINE_PATHS", installed), \
                contextlib.redirect_stderr(warning):
            resolved = update._engine_paths_for(
                target_sha, repo_root=self.root
            )

        self.assertEqual(resolved, installed)
        self.assertEqual(
            warning.getvalue(),
            "WARNING: update could not resolve ENGINE_PATHS at "
            f"{target_sha}: "
            ".super-coder/scripts/engine_manifest.py does not assign a "
            "literal list[str]; "
            ".super-coder/scripts/update.py does not define ENGINE_PATHS; "
            "falling back to installed ENGINE_PATHS.\n",
        )

    def test_materialized_manifest_heals_paths_missed_by_resolution(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        old_sha = self.commit("installed allow-list")

        target = [*installed, ".super-coder/new-path"]
        (self.root / "sc").write_text("new dispatcher\n")
        (self.scripts / "engine_manifest.py").write_text(
            "def load_paths():\n"
            f"    return {target!r}\n"
            "ENGINE_PATHS = load_paths()\n"
        )
        new_file = self.root / ".super-coder" / "new-path" / "new.txt"
        new_file.parent.mkdir()
        new_file.write_text("new path content\n")
        target_sha = self.commit("dynamic target allow-list")
        _git(self.root, "checkout", old_sha)
        self.assertFalse(new_file.exists())

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        warning = io.StringIO()
        output = io.StringIO()
        real_resolve = update._engine_paths_for
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            STATE_DIR=state,
            ENGINE_REF=state / "engine.ref",
            ENGINE_REF_PREV=state / "engine.ref.prev",
            ENGINE_PATHS=installed,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
            local_edits=mock.Mock(return_value={}),
        ), mock.patch.object(
            update,
            "_engine_paths_for",
            wraps=real_resolve,
        ) as resolve, contextlib.redirect_stderr(
            warning
        ), contextlib.redirect_stdout(output):
            update.materialize_fetched_engine(target_sha)

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(
            output.getvalue().count(
                f"resolved 2 engine path(s) for {target_sha[:12]} from "
                "installed ENGINE_PATHS fallback"
            ),
            1,
        )
        self.assertEqual(new_file.read_text(), "new path content\n")
        self.assertEqual(
            warning.getvalue().count("falling back to installed ENGINE_PATHS"),
            1,
        )
        self.assertIn(
            "materialized engine manifest declares 1 path(s) missed by "
            "target-ref resolution; materializing the delta: "
            ".super-coder/new-path",
            warning.getvalue(),
        )
        self.assertEqual(
            set(json.loads((engine / "engine.manifest").read_text())),
            {
                "sc",
                ".super-coder/scripts/engine_manifest.py",
                ".super-coder/scripts/update.py",
                ".super-coder/new-path/new.txt",
            },
            "the rewritten manifest must baseline exactly the healed target "
            "ref files",
        )

    def test_missing_materialized_path_exits_nonzero_and_names_remedy(self):
        target = ["sc", ".super-coder/scripts", ".super-coder/new-path"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(["sc", ".super-coder/scripts"])
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        old_sha = self.commit("installed allow-list")

        (self.root / "sc").write_text("new dispatcher\n")
        self.write_manifest(target)
        new_file = self.root / ".super-coder" / "new-path" / "new.txt"
        new_file.parent.mkdir()
        new_file.write_text("new path content\n")
        target_sha = self.commit("target allow-list")
        _git(self.root, "checkout", old_sha)

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        real_materialize = update.materialize_engine

        def materialize_then_delete(
            ref: str,
            *,
            engine_paths: list[str] | None = None,
        ) -> None:
            real_materialize(ref, engine_paths=engine_paths)
            if engine_paths is not None and ".super-coder/new-path" in engine_paths:
                new_file.unlink()
                new_file.parent.rmdir()

        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            STATE_DIR=state,
            ENGINE_REF=state / "engine.ref",
            ENGINE_REF_PREV=state / "engine.ref.prev",
            ENGINE_PATHS=["sc", ".super-coder/scripts"],
            materialize_engine=materialize_then_delete,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
            local_edits=mock.Mock(return_value={}),
        ), self.assertRaises(SystemExit) as failed:
            update.materialize_fetched_engine(target_sha)

        self.assertEqual(
            str(failed.exception),
            "update: materialized engine is incomplete; missing declared "
            "path(s): .super-coder/new-path\n"
            "  remedy: rerun `./sc update --force`; if the paths remain "
            "missing, report the target engine ref",
        )
        self.assertFalse(
            (state / "engine.ref").exists(),
            "an incomplete tree must not be recorded as the current pin",
        )
        self.assertFalse(
            (engine / "engine.manifest").exists(),
            "an incomplete tree must not receive a clean hash baseline",
        )

    def test_complete_materialize_is_quiet(self):
        target = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(target)
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        old_sha = self.commit("installed floor")

        (self.root / "sc").write_text("new dispatcher\n")
        (self.scripts / "floor.txt").write_text("complete target floor\n")
        target_sha = self.commit("complete target floor")
        _git(self.root, "checkout", old_sha)

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        warning = io.StringIO()
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            STATE_DIR=state,
            ENGINE_REF=state / "engine.ref",
            ENGINE_REF_PREV=state / "engine.ref.prev",
            ENGINE_PATHS=target,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
            local_edits=mock.Mock(return_value={}),
        ), contextlib.redirect_stderr(warning):
            update.materialize_fetched_engine(target_sha)

        self.assertEqual(warning.getvalue(), "")
        self.assertEqual((state / "engine.ref").read_text(), target_sha + "\n")
        self.assertEqual(
            (self.scripts / "floor.txt").read_text(),
            "complete target floor\n",
        )
        self.assertTrue((engine / "engine.manifest").is_file())

    def test_declared_but_absent_path_completes_cleanly(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        old_sha = self.commit("installed floor")

        missing_path = ".super-coder/missing-path"
        (self.root / "sc").write_text("new dispatcher\n")
        self.write_manifest([*installed, missing_path])
        target_sha = self.commit("target declares an absent path")
        _git(self.root, "checkout", old_sha)

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        output = io.StringIO()
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            STATE_DIR=state,
            ENGINE_REF=state / "engine.ref",
            ENGINE_REF_PREV=state / "engine.ref.prev",
            ENGINE_PATHS=installed,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
            local_edits=mock.Mock(return_value={}),
        ), contextlib.redirect_stdout(output):
            update.materialize_fetched_engine(target_sha)

        self.assertIn(
            f"1 target engine path(s) absent at {target_sha[:12]} — "
            f"skipping: {missing_path}",
            output.getvalue(),
        )
        self.assertEqual(
            (state / "engine.ref").read_text(),
            target_sha + "\n",
        )
        self.assertFalse((self.root / missing_path).exists())
        self.assertFalse(
            any(
                path == missing_path
                or path.startswith(missing_path.rstrip("/") + "/")
                for path in json.loads((engine / "engine.manifest").read_text())
            ),
            "a declared path absent at the target ref must stay out of the "
            "materialized file manifest",
        )


if __name__ == "__main__":
    unittest.main()
