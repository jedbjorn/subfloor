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
import inspect
import io
import json
import os
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
import rollback  # noqa: E402

# Deliberately NOT materialized — per-instance / gitignored / runtime, per the
# ENGINE_PATHS comment in update.py.
PER_INSTANCE = {
    "instance.json", "shell_db.db", "shell_db.db-wal", "shell_db.db-shm", "map.db",
    "engine.manifest",  # derived hash baseline — rewritten by each materialize
}

# Tracked upstream, deliberately NOT materialized to forks (file or dir
# prefix): assets/seed/ is super-coder-only (stripped on install) — see the
# ENGINE_PATHS comment in engine_manifest.py.
NOT_MATERIALIZED = (
    ".super-coder/assets/seed/",
    # Retired in this change; retained here so the source-tree coverage test
    # also passes before the deletion is committed.
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

    def test_retired_interface_chat_migrations_are_not_materialized(self):
        self.assertNotIn(".super-coder/chat_migrations", update.ENGINE_PATHS)

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

    def write_hash_baseline(self, paths: list[str]) -> None:
        engine = self.root / ".super-coder"
        with mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
        ):
            update.engine_manifest.write_manifest(paths)

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

    def test_missing_dispatch_target_does_not_advance_engine_pin(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "floor.txt").write_text("old floor\n")
        old_sha = self.commit("old floor")

        (self.root / "sc").write_text(
            "#!/bin/sh\n"
            'exec "$PY" "$S/sprint_cli.py" "$@"\n'
        )
        (self.root / "sc").chmod(0o755)
        (self.root / ".gitignore").write_text("/.sc-worktrees/\n")
        self.write_manifest(installed)
        new_sha = self.commit("broken target floor")
        _git(self.root, "checkout", old_sha)

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
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
        ):
            with self.assertRaises(SystemExit) as raised:
                update.materialize_fetched_engine(new_sha)

        self.assertIn(
            "dispatcher routes to missing engine script(s): sprint_cli.py",
            str(raised.exception),
        )
        self.assertIn(
            f"cd {self.root} && ./sc update",
            str(raised.exception),
        )
        self.assertFalse((state / "engine.ref").exists())
        self.assertEqual((self.root / "sc").read_text().splitlines()[0], "#!/bin/sh")

    def test_legacy_bridge_repairs_stale_dispatcher_and_rebaselines_manifest(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text(
            "#!/bin/sh\n"
            "here=\"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\"\n"
            "live=\"$(cd \"$here\" && cd \"$(git rev-parse --git-common-dir)/..\" && pwd -P)\"\n"
            "S=\"$live/.super-coder/scripts\"\n"
            "cmd=\"${1:-}\"\n"
            "[ \"$cmd\" = sprint ] && shift\n"
            'exec "${PY:-python3}" "$S/sprint.py" "$@"\n'
        )
        (self.root / "sc").chmod(0o755)
        self.write_manifest(installed)
        (self.scripts / "sprint.py").write_text("print('legacy help')\n")
        old_sha = self.commit("legacy callable floor")
        worktree = self.root / ".sc-worktrees" / "dev1"
        _git(self.root, "worktree", "add", "-b", "shell/dev1", str(worktree), old_sha)

        (self.root / "sc").write_text(
            "#!/bin/sh\n"
            "S=\"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)/.super-coder/scripts\"\n"
            "cmd=\"${1:-}\"\n"
            "[ \"$cmd\" = sprint ] && shift\n"
            'exec "${PY:-python3}" "$S/sprint_cli.py" "$@"\n'
        )
        (self.scripts / "sprint.py").write_text(
            (ROOT / ".super-coder" / "scripts" / "sprint.py").read_text()
        )
        (self.scripts / "sprint_cli.py").write_text(
            "import sys\n"
            "def main(argv=None):\n"
            "    argv = sys.argv[1:] if argv is None else argv\n"
            "    if argv == ['-h']:\n"
            "        print('usage: sc sprint [-h]')\n"
            "    elif argv == ['inbox', '-h']:\n"
            "        print('usage: sc sprint inbox [-h] --sprint SPRINT')\n"
            "    else:\n"
            "        raise SystemExit(2)\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    from cli_entry import run_cli\n"
            "    raise SystemExit(run_cli(main))\n"
        )
        (self.scripts / "cli_entry.py").write_text(
            "def run_cli(fn, *args, **kwargs):\n"
            "    return fn(*args, **kwargs)\n"
        )
        self.write_manifest(installed)
        target_sha = self.commit("current callable floor")
        _git(self.root, "checkout", "--detach", old_sha)

        engine = self.root / ".super-coder"
        manifest = engine / "engine.manifest"
        output = io.StringIO()
        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            ENGINE_PATHS=installed,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=manifest,
        ), contextlib.redirect_stdout(output):
            update.materialize_engine(
                target_sha,
                engine_paths=[".super-coder/scripts"],
            )
            stale_worktree_help = subprocess.run(
                [str(worktree / "sc"), "sprint", "-h"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            repaired = update.repair_callable_dispatcher(target_sha)

        env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"}
        main_help = subprocess.run(
            [str(self.root / "sc"), "sprint", "-h"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        worktree_before = _git(worktree, "status", "--porcelain")
        worktree_help = subprocess.run(
            ["sc", "sprint", "inbox", "-h"],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertTrue(repaired)
        self.assertIn("sprint_cli.py", (self.root / "sc").read_text())
        self.assertNotIn("sprint.py", (self.root / "sc").read_text())
        self.assertIn("legacy update bridge: repaired callable dispatcher", output.getvalue())
        self.assertEqual(0, stale_worktree_help.returncode, stale_worktree_help.stderr)
        self.assertEqual(stale_worktree_help.stdout, "usage: sc sprint [-h]\n")
        self.assertEqual(main_help.returncode, 0, main_help.stderr)
        self.assertEqual(main_help.stdout, "usage: sc sprint [-h]\n")
        self.assertEqual(worktree_help.returncode, 0, worktree_help.stderr)
        self.assertEqual(
            worktree_help.stdout,
            "usage: sc sprint inbox [-h] --sprint SPRINT\n",
        )
        self.assertEqual(_git(worktree, "status", "--porcelain"), worktree_before)
        recorded = json.loads(manifest.read_text())
        self.assertIn("sc", recorded)
        self.assertIn(".super-coder/scripts/sprint.py", recorded)
        self.assertIn(".super-coder/scripts/sprint_cli.py", recorded)

    def test_half_laid_target_bypasses_only_matching_manifest_drift_on_retry(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text(
            "#!/bin/sh\nexec \"$PY\" \"$S/sprint.py\" \"$@\"\n"
        )
        (self.root / "sc").chmod(0o755)
        self.write_manifest(installed)
        (self.scripts / "sprint.py").write_text("print('old')\n")
        old_sha = self.commit("old callable floor")

        (self.root / "sc").write_text(
            "#!/bin/sh\nexec \"$PY\" \"$S/sprint_cli.py\" \"$@\"\n"
        )
        (self.scripts / "sprint.py").unlink()
        (self.scripts / "sprint_cli.py").write_text("print('new')\n")
        target_sha = self.commit("new callable floor")
        _git(self.root, "checkout", old_sha)
        self.write_hash_baseline(installed)

        state = self.root / ".sc-state"
        state.mkdir()
        (state / "engine.ref").write_text(old_sha + "\n")
        engine = self.root / ".super-coder"
        manifest = engine / "engine.manifest"
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
            MANIFEST=manifest,
        ):
            update.materialize_engine(target_sha, engine_paths=installed)
            self.assertFalse(update.repair_callable_dispatcher(old_sha))
            self.assertIn("sprint_cli.py", (self.root / "sc").read_text())

            update.materialize_fetched_engine(target_sha)

        self.assertEqual(target_sha + "\n", (state / "engine.ref").read_text())
        self.assertIn("sprint_cli.py", (self.root / "sc").read_text())
        self.assertNotIn(
            ".super-coder/scripts/sprint.py",
            json.loads(manifest.read_text()),
            "a retired script may linger on disk but must leave engine authority",
        )

    def test_compat_repair_preserves_deliberate_dispatcher_edit_and_manifest(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("#!/bin/sh\necho old\n")
        (self.root / "sc").chmod(0o755)
        self.write_manifest(installed)
        (self.scripts / "floor.py").write_text("print('old')\n")
        old_sha = self.commit("old floor")

        (self.root / "sc").write_text("#!/bin/sh\necho new upstream\n")
        (self.scripts / "floor.py").write_text("print('new')\n")
        target_sha = self.commit("target floor")
        _git(self.root, "checkout", old_sha)
        self.write_hash_baseline(installed)
        manifest = self.root / ".super-coder" / "engine.manifest"
        baseline = manifest.read_text()
        (self.root / "sc").write_text("#!/bin/sh\necho deliberate fork patch\n")

        engine = self.root / ".super-coder"
        state = self.root / ".sc-state"
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
            MANIFEST=manifest,
        ):
            self.assertFalse(update.repair_callable_dispatcher(old_sha))
            with self.assertRaisesRegex(
                SystemExit, "refusing to overwrite local engine edits"
            ):
                update.materialize_fetched_engine(target_sha)

        self.assertEqual(
            "#!/bin/sh\necho deliberate fork patch\n",
            (self.root / "sc").read_text(),
        )
        self.assertEqual(baseline, manifest.read_text())

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
            resolved = update._engine_paths_for(
                new_sha, repo_root=self.root
            )
            update.materialize_fetched_engine(new_sha)

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
        self.assertNotIn(
            ".super-coder/retired-path/retired.txt",
            json.loads((engine / "engine.manifest").read_text()),
            "a retired upstream file lingering on disk must leave the "
            "materialized manifest",
        )

    def test_restore_engine_threads_fallback_authority_to_materialize(self):
        base = ["sc", ".super-coder/scripts"]
        installed = [*base, ".super-coder/upstream-owned"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(base)
        upstream_file = (
            self.root / ".super-coder" / "upstream-owned" / "owned.txt"
        )
        upstream_file.parent.mkdir()
        upstream_file.write_text("old upstream content\n")
        previous_sha = self.commit("previous exact allow-list")

        (self.root / "sc").write_text("new dispatcher\n")
        (self.scripts / "engine_manifest.py").write_text(
            "ENGINE_PATHS = load_paths()\n"
        )
        upstream_file.write_text("new upstream content\n")
        current_sha = self.commit("current unparseable allow-list")

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        engine_ref = state / "engine.ref"
        state.mkdir()
        engine_ref.write_text(current_sha + "\n")

        with mock.patch.multiple(
            rollback,
            REPO_ROOT=self.root,
            ENGINE=engine,
            ENGINE_REF=engine_ref,
        ), mock.patch.multiple(
            rollback.update_mod,
            REPO_ROOT=self.root,
            ENGINE=engine,
            ENGINE_PATHS=installed,
        ), mock.patch.multiple(
            rollback.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
        ), contextlib.redirect_stderr(io.StringIO()):
            rollback.restore_engine(previous_sha)

        self.assertEqual(
            upstream_file.read_text(),
            "old upstream content\n",
            "rollback must materialize the previous engine with the same "
            "installed-list authority used for its deletion sets",
        )
        self.assertEqual(engine_ref.read_text(), previous_sha + "\n")

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

    def test_delta_heal_that_does_not_land_is_refused(self):
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

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
        real_materialize = update.materialize_engine

        def heal_that_does_not_land(
            ref: str,
            *,
            engine_paths: list[str] | None = None,
        ) -> None:
            real_materialize(ref, engine_paths=engine_paths)
            if engine_paths == [".super-coder/new-path"]:
                new_file.unlink()
                new_file.parent.rmdir()

        with mock.patch.multiple(
            update,
            REPO_ROOT=self.root,
            ENGINE=engine,
            STATE_DIR=state,
            ENGINE_REF=state / "engine.ref",
            ENGINE_REF_PREV=state / "engine.ref.prev",
            ENGINE_PATHS=installed,
            materialize_engine=heal_that_does_not_land,
        ), mock.patch.multiple(
            update.engine_manifest,
            REPO_ROOT=self.root,
            ENGINE=engine,
            MANIFEST=engine / "engine.manifest",
            local_edits=mock.Mock(return_value={}),
        ), contextlib.redirect_stderr(
            io.StringIO()
        ), contextlib.redirect_stdout(
            io.StringIO()
        ), self.assertRaises(SystemExit) as failed:
            update.materialize_fetched_engine(target_sha)

        self.assertIn(".super-coder/new-path", str(failed.exception))
        self.assertFalse(
            (state / "engine.ref").exists(),
            "a delta heal that did not land must not be recorded as current",
        )

    def test_invalid_materialized_manifest_names_half_floor_remedy(self):
        installed = ["sc", ".super-coder/scripts"]
        (self.root / "sc").write_text("old dispatcher\n")
        self.write_manifest(installed)
        (self.scripts / "update.py").write_text(
            "from engine_manifest import ENGINE_PATHS\n"
        )
        old_sha = self.commit("installed allow-list")

        (self.root / "sc").write_text("new dispatcher\n")
        (self.scripts / "engine_manifest.py").write_text(
            f"ENGINE_PATHS = {tuple(installed)!r}\n"
        )
        target_sha = self.commit("runtime-invalid target allow-list")
        _git(self.root, "checkout", old_sha)

        state = self.root / ".sc-state"
        engine = self.root / ".super-coder"
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
        ), contextlib.redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as failed:
            update.materialize_fetched_engine(target_sha)

        self.assertEqual(
            str(failed.exception),
            "update: materialized engine manifest does not expose "
            "ENGINE_PATHS as list[str]\n"
            "  engine.ref was not advanced; the recorded engine pin remains "
            "unchanged.\n"
            "  no automated recovery is available; report the target engine "
            "ref upstream",
        )
        self.assertEqual((self.root / "sc").read_text(), "new dispatcher\n")
        self.assertFalse(
            (state / "engine.ref").exists(),
            "the overwritten half floor must not be recorded as current",
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
            "  engine.ref was not advanced; the recorded engine pin remains "
            "unchanged.\n"
            "  no automated recovery is available; report the target engine "
            "ref upstream",
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


class LinkedDispatcherReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Update Test")
        _git(self.root, "config", "user.email", "update@example.invalid")

        stale = self.root / "sc"
        stale.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = deps ] && [ \"$2\" = --help ] && {\n"
            "  : > retired-mutating-flow\n"
            "  echo stale help\n"
            "}\n"
        )
        stale.chmod(0o755)
        old_sha = self._commit("stale dispatcher")
        self.worktree = self.root / ".sc-worktrees" / "dev1"
        _git(
            self.root, "worktree", "add", "-b", "shell/dev1",
            str(self.worktree), old_sha,
        )

        current = self.root / "sc"
        current.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = deps ] && [ \"$2\" = --help ] && {\n"
            "  echo 'Usage: ./sc deps [-h|--help]'\n"
            "  exit 0\n"
            "}\n"
            "exit 2\n"
        )
        current.chmod(0o755)
        self.target_sha = self._commit("current dispatcher")

    def _commit(self, message: str) -> str:
        _git(self.root, "add", "sc")
        _git(self.root, "commit", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def test_clean_stale_worktree_dispatcher_runs_current_read_only_help(self):
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            changed = update.reconcile_linked_dispatchers(
                self.target_sha, worktrees=(self.worktree,)
            )

        done = subprocess.run(
            [str(self.worktree / "sc"), "deps", "--help"],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(changed, (self.worktree,))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "Usage: ./sc deps [-h|--help]\n")
        self.assertFalse((self.worktree / "retired-mutating-flow").exists())
        self.assertEqual(
            (self.worktree / "sc").read_bytes(),
            (self.root / "sc").read_bytes(),
        )

    def test_locally_edited_worktree_dispatcher_is_preserved(self):
        custom = self.worktree / "sc"
        custom.write_text("#!/bin/sh\necho operator-owned\n")
        before = custom.read_bytes()
        warning = io.StringIO()
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                contextlib.redirect_stderr(warning):
            changed = update.reconcile_linked_dispatchers(
                self.target_sha, worktrees=(self.worktree,)
            )

        self.assertEqual(changed, ())
        self.assertEqual(custom.read_bytes(), before)
        self.assertIn(
            f"dispatcher locally edited, left stale: {custom}",
            warning.getvalue(),
        )

    def test_previous_managed_overlay_advances_on_the_next_update(self):
        state = self.root / ".sc-state"
        state.mkdir()
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            update.reconcile_linked_dispatchers(
                self.target_sha, worktrees=(self.worktree,)
            )
        (state / "engine.ref").write_text(self.target_sha + "\n")

        current = self.root / "sc"
        current.write_text("#!/bin/sh\necho next-dispatcher\n")
        current.chmod(0o755)
        next_sha = self._commit("next dispatcher")
        (state / "engine.ref.prev").write_text(self.target_sha + "\n")
        (state / "engine.ref").write_text(next_sha + "\n")
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            changed = update.reconcile_linked_dispatchers(
                next_sha, worktrees=(self.worktree,)
            )

        self.assertEqual(changed, (self.worktree,))
        self.assertEqual((self.worktree / "sc").read_bytes(), current.read_bytes())


class SourceRepoDispatcherReconciliationTest(unittest.TestCase):
    """Source repos have no fetched pin — reconcile from the working tree.

    Skipping them left source-repo shell worktrees on stale launchers forever
    (flag #166: skills documented `sc sprint` while every worktree dispatcher
    predated the verb)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Update Test")
        _git(self.root, "config", "user.email", "update@example.invalid")

        stale = self.root / "sc"
        stale.write_text("#!/bin/sh\necho stale\n")
        stale.chmod(0o755)
        _git(self.root, "add", "sc")
        _git(self.root, "commit", "-m", "stale dispatcher")
        old_sha = _git(self.root, "rev-parse", "HEAD")
        self.worktree = self.root / ".sc-worktrees" / "dev1"
        _git(
            self.root, "worktree", "add", "-b", "shell/dev1",
            str(self.worktree), old_sha,
        )

        current = self.root / "sc"
        current.write_text("#!/bin/sh\necho current\n")
        current.chmod(0o755)
        _git(self.root, "add", "sc")
        _git(self.root, "commit", "-m", "current dispatcher")

    def test_worktree_heals_from_working_tree_bytes(self):
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            changed = update.reconcile_linked_dispatchers(
                None,
                worktrees=(self.worktree,),
                target_bytes=(self.root / "sc").read_bytes(),
            )

        self.assertEqual(changed, (self.worktree,))
        self.assertEqual(
            (self.worktree / "sc").read_bytes(),
            (self.root / "sc").read_bytes(),
        )

    def test_update_reconciliation_uses_source_tree_dispatcher(self):
        source = inspect.getsource(update.reconcile_under_cutover)
        self.assertIn("elif source:", source)
        self.assertIn("target_bytes=canonical.read_bytes()", source)


class StaleBootstrapExecutesLiveFloorTest(unittest.TestCase):
    """The single-owner regression (spec #105): a worktree's committed
    launcher, however old, execs the LIVE engine's dispatcher body — a verb
    added after the branch point is reachable without reconcile or rebase."""

    def test_worktree_bootstrap_reaches_verb_added_after_branch_point(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.name", "Update Test")
        _git(root, "config", "user.email", "update@example.invalid")

        # The real bootstrap under test, committed at the branch point.
        bootstrap = root / "sc"
        bootstrap.write_bytes((ROOT / "sc").read_bytes())
        bootstrap.chmod(0o755)
        _git(root, "add", "sc")
        _git(root, "commit", "-m", "bootstrap at branch point")
        old_sha = _git(root, "rev-parse", "HEAD")
        worktree = root / ".sc-worktrees" / "dev1"
        _git(root, "worktree", "add", "-b", "shell/dev1", str(worktree), old_sha)

        # A NEW floor lands at the main root only — the worktree branch never
        # sees it, and holds no engine of its own.
        scripts = root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        body = scripts / "dispatch.sh"
        body.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = newverb ] && { echo post-branch-verb; exit 0; }\n"
            "exit 2\n"
        )
        body.chmod(0o755)
        self.assertFalse((worktree / ".super-coder").exists())

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SC_DISPATCH", "SC_CALLER_ROOT")
        }
        done = subprocess.run(
            [str(worktree / "sc"), "newverb"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "post-branch-verb\n")

    def test_bootstrap_names_a_floor_without_its_body(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bootstrap = root / "sc"
        bootstrap.write_bytes((ROOT / "sc").read_bytes())
        bootstrap.chmod(0o755)
        # An engine dir exists, but the floor predates the body: the paired
        # rollback/update has not finished. The error must name the miss.
        (root / ".super-coder").mkdir()

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SC_DISPATCH", "SC_CALLER_ROOT")
        }
        done = subprocess.run(
            [str(bootstrap), "help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("engine floor predates this launcher", done.stderr)
        self.assertIn("scripts/dispatch.sh", done.stderr)


class GuardCommittedCopyTest(unittest.TestCase):
    """A manifest mismatch that matches a COMMITTED engine copy is a stale
    checkout, not a fork patch — it must not wedge the update (#581)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Update Test")
        _git(self.root, "config", "user.email", "update@example.invalid")
        dispatcher = self.root / "sc"
        dispatcher.write_text("#!/bin/sh\necho committed\n")
        dispatcher.chmod(0o755)
        _git(self.root, "add", "sc")
        _git(self.root, "commit", "-m", "committed dispatcher")

    def _check(self):
        with mock.patch.object(update, "REPO_ROOT", self.root), \
                mock.patch.object(
                    update.engine_manifest,
                    "local_edits",
                    return_value={"sc": "modified"},
                ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                update.check_local_edits(False)
            return out.getvalue()

    def test_committed_copy_is_not_a_fork_edit(self):
        output = self._check()
        self.assertIn("match a committed engine copy", output)

    def test_uncommitted_edit_still_blocks(self):
        (self.root / "sc").write_text("#!/bin/sh\necho fork-patched\n")
        with self.assertRaises(SystemExit):
            self._check()


class UpdateRefPublicationTest(unittest.TestCase):
    OLD = "a" * 40
    NEW = "b" * 40

    def _patch_main(self, state: Path, *, fail: str | None):
        ref = state / "engine.ref"
        ref.write_text(self.OLD + "\n")
        scripts = []
        events = []

        def run_script(name: str, **kwargs) -> None:
            scripts.append((name, kwargs))
            events.append(name)
            if fail == name:
                raise RuntimeError(f"{name} failed")

        def materialize(sha: str, **kwargs) -> None:
            if kwargs.get("publish_ref", True):
                ref.write_text(sha + "\n")

        def migrate(*, reconcile) -> None:
            events.append("migration")
            if fail == "migration":
                raise RuntimeError("migration failed")
            reconcile()

        def publish(sha: str) -> None:
            events.append("publish")
            ref.write_text(sha + "\n")

        def reconcile(_sha: str, **_kwargs) -> tuple[Path, ...]:
            events.append("reconcile")
            if fail == "reconcile":
                raise RuntimeError("reconcile failed")
            return ()

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.multiple(
            update,
            REPO_ROOT=state.parent,
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
            materialize_fetched_engine=mock.Mock(side_effect=materialize),
            publish_engine_ref=mock.Mock(side_effect=publish),
            reconcile_linked_dispatchers=mock.Mock(side_effect=reconcile),
            ensure_workflows=mock.Mock(return_value=("current", [])),
            expire_sandbox_harnesses=mock.Mock(return_value=None),
            migrate_with_service_cutover=mock.Mock(side_effect=migrate),
            refresh_installed_brokers=mock.Mock(),
            sync_skills=mock.Mock(),
            regrant=mock.Mock(return_value=0),
            reconcile_skill_projections=mock.Mock(
                return_value={"written": [], "skipped": [], "checkouts": []}
            ),
            run_script=mock.Mock(side_effect=run_script),
            snapshot_under_cutover=mock.Mock(
                side_effect=lambda: run_script("snapshot.py")
            ),
        ))
        stack.enter_context(mock.patch.multiple(
            update.install_mod,
            ensure_gitignore=mock.Mock(return_value=False),
            ensure_harnesses=mock.Mock(),
            wire_make_aliases=mock.Mock(return_value=()),
        ))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack, ref, scripts, events

    def test_failure_before_publication_never_overlays_linked_dispatchers(self):
        for failed in ("migration", "snapshot.py"):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as raw:
                state = Path(raw) / ".sc-state"
                state.mkdir()
                stack, ref, _scripts, events = self._patch_main(state, fail=failed)
                with stack, self.assertRaises(RuntimeError):
                    update.main([])
                self.assertEqual(ref.read_text(), self.OLD + "\n")
                self.assertNotIn("publish", events)
                self.assertNotIn("reconcile", events)

    def test_dispatcher_crash_keeps_published_target_recognizable(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".sc-state"
            state.mkdir()
            stack, ref, _scripts, events = self._patch_main(
                state, fail="reconcile"
            )
            with stack, self.assertRaisesRegex(RuntimeError, "reconcile failed"):
                update.main([])

            self.assertEqual(ref.read_text(), self.NEW + "\n")
            self.assertEqual(events[-2:], ["publish", "reconcile"])

    def test_ref_publishes_after_migrate_and_snapshot_before_dispatchers(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".sc-state"
            state.mkdir()
            stack, ref, scripts, events = self._patch_main(state, fail=None)
            with stack:
                update.main([])
            self.assertEqual(
                scripts,
                [
                    ("map_setup.py", {"update_target_ref": self.NEW}),
                    ("snapshot.py", {}),
                ],
            )
            self.assertEqual(
                events,
                [
                    "migration",
                    "map_setup.py",
                    "snapshot.py",
                    "publish",
                    "reconcile",
                ],
            )
            self.assertEqual(ref.read_text(), self.NEW + "\n")


class GeneratedArtifactMigrationTest(unittest.TestCase):
    def test_legacy_artifacts_are_preserved_locally_then_untracked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".sc-state"
            local = state / "local"
            state.mkdir()
            (state / "content.sql").write_text("legacy instance state\n")
            (state / "engine.ref").write_text("a" * 40 + "\n")
            (root / "roadmap_sc.md").write_text("legacy render\n")

            _git(root, "init", "-b", "main")
            _git(root, "config", "user.name", "Update Test")
            _git(root, "config", "user.email", "update@example.invalid")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "legacy tracked artifacts")
            (root / ".gitignore").write_text(
                "/.sc-state/content.sql\n/.sc-state/local/\n/roadmap_sc.md\n"
            )

            with mock.patch.object(update, "REPO_ROOT", root), \
                 mock.patch.multiple(
                     update.artifact_policy,
                     REPO_ROOT=root,
                     STATE_DIR=state,
                     LOCAL_DIR=local,
                 ):
                update.migrate_generated_artifacts_local()

            self.assertEqual(
                (local / "content.sql").read_text(),
                "legacy instance state\n",
            )
            self.assertTrue((state / "content.sql").exists())
            self.assertTrue((root / "roadmap_sc.md").exists())
            tracked = set(_git(root, "ls-files").splitlines())
            self.assertNotIn(".sc-state/content.sql", tracked)
            self.assertNotIn("roadmap_sc.md", tracked)
            self.assertIn(".sc-state/engine.ref", tracked)


if __name__ == "__main__":
    unittest.main()
