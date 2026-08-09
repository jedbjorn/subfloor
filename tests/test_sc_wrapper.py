"""Managed host ``sc`` wrapper lifecycle and targeting coverage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sc_wrapper


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


class WrapperFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / "state"),
        }
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()

    @property
    def wrapper(self) -> Path:
        return sc_wrapper.wrapper_path(self.env)

    @property
    def registry(self) -> Path:
        return sc_wrapper.state_dir(self.env) / sc_wrapper.REGISTRY_NAME


class WrapperTargetingTest(WrapperFixture):
    def _tracked_launcher(self, repo: Path) -> None:
        git(repo, "init", "-q", "-b", "main")
        launcher = repo / "sc"
        launcher.write_text(
            "#!/bin/sh\n"
            "printf 'cwd=%s\\n' \"$PWD\"\n"
            "printf 'arg=%s\\n' \"$@\"\n"
            "exit 23\n"
        )
        launcher.chmod(0o755)
        git(repo, "add", "sc")

    def test_wrapper_selects_callers_git_root_and_preserves_cwd_argv_status(self) -> None:
        self._tracked_launcher(self.repo_a)
        nested = self.repo_a / "nested"
        nested.mkdir()
        sc_wrapper.register_install(self.repo_a, self.env)

        result = subprocess.run(
            [str(self.wrapper), "one", "two words"],
            cwd=nested,
            env={**os.environ, **self.env},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 23)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"cwd={nested.resolve()}", "arg=one", "arg=two words"],
        )

    def test_wrapper_fails_127_outside_git_and_for_untracked_launcher(self) -> None:
        sc_wrapper.register_install(self.repo_a, self.env)
        outside = subprocess.run(
            [str(self.wrapper)],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(outside.returncode, 127)
        self.assertIn("not inside a Git checkout", outside.stderr)

        git(self.repo_a, "init", "-q", "-b", "main")
        launcher = self.repo_a / "sc"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        untracked = subprocess.run(
            [str(self.wrapper)],
            cwd=self.repo_a,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(untracked.returncode, 127)
        self.assertIn("no tracked sc launcher", untracked.stderr)


class WrapperLifecycleTest(WrapperFixture):
    def test_unchanged_last_owner_removes_wrapper_but_not_login_path(self) -> None:
        sc_wrapper.register_install(self.repo_a, self.env)

        result = sc_wrapper.unregister_install(self.repo_a, self.env)

        self.assertIn("removed unchanged managed wrapper", result)
        self.assertFalse(self.wrapper.exists())
        self.assertEqual(json.loads(self.registry.read_text())["installs"], [])
        self.assertIn(sc_wrapper.PATH_BEGIN, (self.home / ".profile").read_text())

    def test_unregistered_removal_preserves_an_existing_managed_wrapper(self) -> None:
        sc_wrapper.register_install(self.repo_a, self.env)

        result = sc_wrapper.unregister_install(self.repo_b, self.env)

        self.assertIn("no registration", result)
        self.assertTrue(self.wrapper.exists())
        self.assertEqual(
            json.loads(self.registry.read_text())["installs"],
            [str(self.repo_a.resolve())],
        )

    def test_multi_install_last_owner_and_modified_wrapper_preservation(self) -> None:
        first = sc_wrapper.register_install(self.repo_a, self.env)
        second = sc_wrapper.register_install(self.repo_b, self.env)
        self.assertIn(str(self.repo_a), first)
        self.assertIn(str(self.repo_b), second)
        data = json.loads(self.registry.read_text())
        self.assertEqual(data["schema"], sc_wrapper.REGISTRY_SCHEMA)
        self.assertEqual(
            data["installs"],
            sorted([str(self.repo_a.resolve()), str(self.repo_b.resolve())]),
        )
        self.assertIn(
            "# managed-by: subfloor sc-wrapper v1",
            self.wrapper.read_text(),
        )
        self.assertTrue(self.wrapper.stat().st_mode & stat.S_IXUSR)

        for profile_name in (".profile", ".zprofile"):
            profile = self.home / profile_name
            self.assertEqual(profile.read_text().count(sc_wrapper.PATH_BEGIN), 1)
            sc_wrapper.register_install(self.repo_a, self.env)
            self.assertEqual(profile.read_text().count(sc_wrapper.PATH_BEGIN), 1)

        first_remove = sc_wrapper.unregister_install(self.repo_a, self.env)
        self.assertIn("removed registration", first_remove)
        self.assertTrue(self.wrapper.exists())

        self.wrapper.write_text("#!/bin/sh\necho user-modified\n")
        last_remove = sc_wrapper.unregister_install(self.repo_b, self.env)
        self.assertIn("preserved unrelated wrapper", last_remove)
        self.assertEqual(self.wrapper.read_text(), "#!/bin/sh\necho user-modified\n")
        self.assertEqual(json.loads(self.registry.read_text())["installs"], [])

    def test_conflicting_targets_are_never_overwritten(self) -> None:
        target = self.wrapper
        target.parent.mkdir(parents=True)
        cases = ("file", "directory", "symlink")
        for case in cases:
            with self.subTest(case=case):
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    target.rmdir()
                if case == "file":
                    target.write_text("user command\n")
                elif case == "directory":
                    target.mkdir()
                else:
                    destination = self.root / "user-sc"
                    destination.write_text("user command\n")
                    target.symlink_to(destination)

                with self.assertRaises(sc_wrapper.WrapperError) as raised:
                    sc_wrapper.register_install(self.repo_a, self.env)
                self.assertIn("use ./sc", str(raised.exception))
                self.assertFalse(self.registry.exists())

    def test_registry_replacement_failure_leaves_previous_document_intact(self) -> None:
        sc_wrapper.register_install(self.repo_a, self.env)
        before = self.registry.read_bytes()
        real_replace = sc_wrapper.os.replace

        def fail_registry(source: Path, target: Path) -> None:
            if Path(target) == self.registry:
                raise OSError("injected registry replace failure")
            real_replace(source, target)

        with (
            mock.patch.object(sc_wrapper.os, "replace", side_effect=fail_registry),
            self.assertRaises(OSError),
        ):
            sc_wrapper.register_install(self.repo_b, self.env)

        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(json.loads(before)["installs"], [str(self.repo_a.resolve())])

    def test_atomic_create_never_replaces_a_racing_user_command(self) -> None:
        target = self.wrapper
        target.parent.mkdir(parents=True)
        target.write_text("user command\n")

        with self.assertRaises(sc_wrapper.WrapperError):
            sc_wrapper._atomic_create(target, sc_wrapper.WRAPPER_BYTES, 0o755)

        self.assertEqual(target.read_text(), "user command\n")

    def test_concurrent_registrations_serialize_without_lost_roots(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(SCRIPTS)!r}); "
            "import sc_wrapper; from pathlib import Path; "
            "sc_wrapper.register_install(Path(sys.argv[1]))"
        )
        env = {**os.environ, **self.env}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(repo)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for repo in (self.repo_a, self.repo_b)
        ]
        results = [process.communicate(timeout=20) for process in processes]
        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(
            json.loads(self.registry.read_text())["installs"],
            sorted([str(self.repo_a.resolve()), str(self.repo_b.resolve())]),
        )


class DockerWrapperContractTest(unittest.TestCase):
    def test_docker_wrapper_is_git_root_only_without_sc_root_fallback(self) -> None:
        dockerfile = (ROOT / ".super-coder" / "Dockerfile").read_text()
        wrapper = dockerfile[dockerfile.index("# Durable bare `sc`") :]
        self.assertIn('git -C "$top" ls-files --error-unmatch -- sc', wrapper)
        self.assertNotIn('[ -n "$SC_ROOT" ]', wrapper)
        self.assertNotIn("via git toplevel or SC_ROOT", wrapper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
