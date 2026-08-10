"""Behavioral tests for the fork-owned v1 dev-kit declaration."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import devkit
import seed_skills
from devkit import DevkitConfigError, load_declaration

RUNNER = ROOT / ".super-coder" / "scripts" / "devkit.py"
SOURCE_DEVKIT = ROOT / ".subfloor" / "dev-kit"
DEVKIT_SKILL = (
    ROOT / ".super-coder" / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md"
)
DEVKIT_RESEED = (
    ROOT
    / ".super-coder"
    / "migrations"
    / "0191_reseed_target_aware_dev_kit.sql"
)


class DeclarationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        (self.root / ".subfloor").mkdir(parents=True)

    def write(self, value: object) -> None:
        (self.root / ".subfloor" / "dev-kit.json").write_text(json.dumps(value))

    def assert_invalid(self, value: object, message: str) -> None:
        self.write(value)
        with self.assertRaisesRegex(DevkitConfigError, message):
            load_declaration(self.root)

    def test_absent_is_distinct_from_invalid(self):
        self.assertIsNone(load_declaration(self.root))
        (self.root / ".subfloor" / "dev-kit.json").write_text("{")
        with self.assertRaisesRegex(DevkitConfigError, r"malformed JSON"):
            load_declaration(self.root)

    def test_declaration_file_cannot_be_read_through_an_escaping_symlink(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"version": 1}')
        (self.root / ".subfloor" / "dev-kit.json").symlink_to(outside)
        with self.assertRaisesRegex(
            DevkitConfigError, r"\$: must stay inside the invoking checkout"
        ):
            load_declaration(self.root)

    def test_version_and_keys_are_strict(self):
        for version in (True, 0, 2, "1"):
            with self.subTest(version=version):
                self.assert_invalid({"version": version}, r"\$\.version: must be integer 1")
        self.assert_invalid(
            {"version": 1, "tools": {}}, r"\$: unknown key 'tools'"
        )
        declaration = self.root / ".subfloor" / "dev-kit.json"
        declaration.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(DevkitConfigError, r"duplicate key 'version'"):
            load_declaration(self.root)

    def test_hook_schema_rejects_unknown_names_keys_and_empty_argv(self):
        self.assert_invalid(
            {"version": 1, "hooks": {"build": {"argv": ["tool"]}}},
            r"unknown hook 'build'",
        )
        self.assert_invalid(
            {"version": 1, "hooks": {"test": {"argv": ["tool"], "env": {}}}},
            r"\$\.hooks\.test: unknown key 'env'",
        )
        for argv in ([], [""], "tool"):
            with self.subTest(argv=argv):
                self.assert_invalid(
                    {"version": 1, "hooks": {"test": {"argv": argv}}},
                    r"\$\.hooks\.test\.argv",
                )

    def test_hook_defaults_cwd_and_classifies_executables(self):
        tool = self.root / "tools" / "run tests"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n")
        self.write(
            {
                "version": 1,
                "hooks": {
                    "deps": {"argv": ["uv", "sync"]},
                    "test": {"argv": ["../tools/run tests"], "cwd": "app"},
                },
            }
        )
        (self.root / "app").mkdir()
        declaration = load_declaration(self.root)
        self.assertIsNotNone(declaration)
        deps = declaration.hooks["deps"]
        test = declaration.hooks["test"]
        self.assertEqual((deps.cwd_declared, deps.cwd), (".", self.root.resolve()))
        self.assertEqual((deps.executable_kind, deps.executable), ("path", "uv"))
        self.assertIsNone(deps.resolved_executable)
        self.assertEqual(test.executable_kind, "relative")
        self.assertEqual(test.resolved_executable, tool.resolve())

    def test_cwd_must_exist_and_stay_in_checkout(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for cwd in ("missing", "../outside", "escape"):
            with self.subTest(cwd=cwd):
                self.assert_invalid(
                    {"version": 1, "hooks": {"test": {"argv": ["tool"], "cwd": cwd}}},
                    r"\$\.hooks\.test\.cwd",
                )

    def test_relative_executable_cannot_escape_lexically_or_through_symlink(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "tool").write_text("no")
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for executable in ("../outside/tool", "./escape/tool", "/bin/sh", "C:\\tool.exe"):
            with self.subTest(executable=executable):
                self.assert_invalid(
                    {"version": 1, "hooks": {"test": {"argv": [executable]}}},
                    r"\$\.hooks\.test\.argv\[0\]",
                )

    def test_provision_hook_must_reference_declared_hook(self):
        self.assert_invalid(
            {"version": 1, "provision": {"hook": "deps"}},
            r"\$\.provision\.hook: must name a declared hook",
        )
        self.write(
            {
                "version": 1,
                "hooks": {"deps": {"argv": ["uv"]}},
                "provision": {"hook": "deps"},
            }
        )
        declaration = load_declaration(self.root)
        self.assertEqual(declaration.provision.hook, "deps")

    def test_provision_inputs_must_be_existing_contained_files(self):
        outside = Path(self.temp.name) / "outside.lock"
        outside.write_text("lock")
        (self.root / "escape.lock").symlink_to(outside)
        base = {
            "version": 1,
            "hooks": {"deps": {"argv": ["uv"]}},
            "provision": {"hook": "deps", "inputs": []},
        }
        for input_path in ("missing.lock", "../outside.lock", "escape.lock"):
            with self.subTest(input_path=input_path):
                value = json.loads(json.dumps(base))
                value["provision"]["inputs"] = [input_path]
                self.assert_invalid(value, r"\$\.provision\.inputs\[0\]")

    def test_sandbox_paths_are_canonical_and_contained(self):
        dockerfile = self.root / ".subfloor" / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        self.write(
            {
                "version": 1,
                "sandbox": {
                    "dockerfile": ".subfloor/Dockerfile",
                    "mounts": [{"name": "python-env", "target": ".venv"}],
                },
            }
        )
        declaration = load_declaration(self.root)
        self.assertEqual(declaration.sandbox.dockerfile, dockerfile.resolve())
        self.assertEqual(declaration.sandbox.context, dockerfile.parent.resolve())
        self.assertEqual(declaration.sandbox.mounts[0].target, (self.root / ".venv").resolve())

    def test_sandbox_mount_names_are_bounded_safe_identifiers(self):
        dockerfile = self.root / ".subfloor" / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        for name in ("Upper", "with.dot", "-leading", "x" * 49, "two words"):
            with self.subTest(name=name):
                self.assert_invalid(
                    {
                        "version": 1,
                        "sandbox": {
                            "dockerfile": ".subfloor/Dockerfile",
                            "mounts": [{"name": name, "target": ".venv"}],
                        },
                    },
                    r"\$\.sandbox\.mounts\[0\]\.name",
                )

    def test_sandbox_mounts_reject_protected_file_and_overlapping_targets(self):
        dockerfile = self.root / ".subfloor" / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        (self.root / "file-target").write_text("not a directory")
        protected = (".", ".git", ".super-coder/cache", ".sc-state/cache", ".subfloor")
        for target in (*protected, "file-target"):
            with self.subTest(target=target):
                self.assert_invalid(
                    {
                        "version": 1,
                        "sandbox": {
                            "dockerfile": ".subfloor/Dockerfile",
                            "mounts": [{"name": "cache", "target": target}],
                        },
                    },
                    r"\$\.sandbox\.mounts\[0\]\.target",
                )
        self.assert_invalid(
            {
                "version": 1,
                "sandbox": {
                    "dockerfile": ".subfloor/Dockerfile",
                    "mounts": [
                        {"name": "cache", "target": "build"},
                        {"name": "nested", "target": "build/nested"},
                    ],
                },
            },
            r"must not overlap mount target 'build'",
        )

    def test_sandbox_dockerfile_context_and_mount_cannot_escape_by_symlink(self):
        outside = Path(self.temp.name) / "outside-sandbox"
        outside.mkdir()
        (outside / "Dockerfile").write_text("FROM scratch\n")
        (self.root / "escape-sandbox").symlink_to(outside, target_is_directory=True)
        cases = (
            {
                "dockerfile": "escape-sandbox/Dockerfile",
            },
            {
                "dockerfile": ".subfloor/Dockerfile",
                "context": "escape-sandbox",
            },
            {
                "dockerfile": ".subfloor/Dockerfile",
                "mounts": [{"name": "cache", "target": "escape-sandbox/cache"}],
            },
        )
        (self.root / ".subfloor" / "Dockerfile").write_text("FROM scratch\n")
        for sandbox in cases:
            with self.subTest(sandbox=sandbox):
                self.assert_invalid(
                    {"version": 1, "sandbox": sandbox},
                    r"must stay inside the invoking checkout",
                )

    def test_canonical_json_is_stable_across_key_order(self):
        first = {"version": 1, "hooks": {"test": {"cwd": ".", "argv": ["tool"]}}}
        second = {"hooks": {"test": {"argv": ["tool"], "cwd": "."}}, "version": 1}
        self.write(first)
        left = load_declaration(self.root).canonical_json
        self.write(second)
        right = load_declaration(self.root).canonical_json
        self.assertEqual(left, right)


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout with spaces"
        (self.root / ".subfloor").mkdir(parents=True)
        subprocess.run(
            ("git", "init", "-q", str(self.root)), check=True
        )

    def write(self, value: object) -> None:
        (self.root / ".subfloor" / "dev-kit.json").write_text(json.dumps(value))

    def run_hook(self, hook: str, *arguments: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            (sys.executable, str(RUNNER), "run", str(self.root), hook, *arguments),
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_absent_invalid_and_missing_hook_are_distinct(self):
        absent = self.run_hook("test")
        self.assertEqual(absent.returncode, 78)
        self.assertIn("hook state: absent", absent.stderr)
        self.write({"version": 2})
        invalid = self.run_hook("test")
        self.assertEqual(invalid.returncode, 64)
        self.assertIn("hook state: invalid", invalid.stderr)
        self.assertIn("$.version", invalid.stderr)
        self.write({"version": 1})
        missing = self.run_hook("test")
        self.assertEqual(missing.returncode, 78)
        self.assertIn("hook state: absent", missing.stderr)
        self.assertIn("not configured", missing.stderr)

    def test_relative_hook_preserves_literal_arguments_cwd_context_and_output(self):
        work = self.root / "work dir"
        work.mkdir()
        tool = work / "capture"
        tool.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
            "'root': os.environ['SC_DEVKIT_ROOT'], 'seat': os.environ['SC_DEVKIT_SEAT'], "
            "'hook': os.environ['SC_DEVKIT_HOOK']}))\n"
        )
        tool.chmod(0o755)
        self.write(
            {
                "version": 1,
                "hooks": {"test": {"argv": ["./capture", "declared arg"], "cwd": "work dir"}},
            }
        )
        arguments = ("$(touch escaped)", "*.py", "two words", "--leading")
        done = self.run_hook("test", *arguments)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["argv"], ["declared arg", *arguments])
        self.assertEqual(payload["cwd"], str(work.resolve()))
        self.assertEqual(payload["root"], str(self.root.resolve()))
        self.assertEqual(payload["seat"], "host")
        self.assertEqual(payload["hook"], "test")
        self.assertFalse((self.root / "escaped").exists())
        self.assertIn(f"dev-kit executable: {tool.resolve()}", done.stderr)

    def test_bare_path_resolution_is_reported_and_docker_context_is_supplied(self):
        binary = Path(self.temp.name) / "bin"
        binary.mkdir()
        tool = binary / "fork-tool"
        tool.write_text(
            "#!/bin/sh\nprintf 'seat=%s args=%s\\n' \"$SC_DEVKIT_SEAT\" \"$*\"\n"
        )
        tool.chmod(0o755)
        self.write({"version": 1, "hooks": {"lint": {"argv": ["fork-tool", "check"]}}})
        environment = dict(os.environ)
        environment.update({"PATH": f"{binary}:{environment['PATH']}", "SC_SANDBOX": "1"})
        done = self.run_hook("lint", "literal arg", env=environment)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout, "seat=docker args=check literal arg\n")
        self.assertIn(f"dev-kit executable: {tool.resolve()}", done.stderr)

    def test_bare_path_resolution_never_falls_back_after_selection(self):
        first_bin = Path(self.temp.name) / "first"
        fallback_bin = Path(self.temp.name) / "fallback"
        first_bin.mkdir()
        fallback_bin.mkdir()
        first = first_bin / "fork-tool"
        first.write_text("#!/bin/sh\nexit 0\n")
        first.chmod(0o755)
        fallback_marker = self.root / "fallback-ran"
        fallback = fallback_bin / "fork-tool"
        fallback.write_text(f"#!/bin/sh\ntouch '{fallback_marker}'\n")
        fallback.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["fork-tool"]}}})
        environment = dict(os.environ)
        environment["PATH"] = f"{first_bin}:{fallback_bin}:{environment['PATH']}"
        original_resolver = devkit._resolve_executable

        def resolve_then_remove(hook, child_environment):
            selected = original_resolver(hook, child_environment)
            selected.unlink()
            return selected

        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(
            devkit, "invoking_checkout", return_value=self.root.resolve()
        ), mock.patch.object(
            devkit, "_resolve_executable", side_effect=resolve_then_remove
        ):
            status = devkit.run_hook(
                self.root, "test", (), environment=environment
            )
        self.assertEqual(status, 126)
        self.assertFalse(fallback_marker.exists())

    def test_start_failure_is_126_and_child_failure_is_unchanged(self):
        self.write({"version": 1, "hooks": {"test": {"argv": ["./missing"]}}})
        missing = self.run_hook("test")
        self.assertEqual(missing.returncode, 126)
        self.assertIn("start failed", missing.stderr)
        self.assertIn(f"dev-kit checkout: {self.root.resolve()}", missing.stderr)
        self.assertIn("dev-kit seat: host", missing.stderr)
        self.assertIn(f"dev-kit cwd: {self.root.resolve()}", missing.stderr)
        self.assertIn("dev-kit argv: ./missing", missing.stderr)
        child = self.root / "fail"
        child.write_text("#!/bin/sh\necho child-error >&2\nexit 23\n")
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./fail"]}}})
        failed = self.run_hook("test")
        self.assertEqual(failed.returncode, 23)
        self.assertIn("child-error", failed.stderr)

    def test_signal_terminated_child_uses_shell_observable_status(self):
        child = self.root / "signal"
        child.write_text("#!/bin/sh\nkill -9 $$\n")
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./signal"]}}})
        failed = self.run_hook("test")
        self.assertEqual(failed.returncode, 137)

    def test_dangling_provision_reference_fails_before_child_execution(self):
        marker = self.root / "child-ran"
        child = self.root / "child"
        child.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        child.chmod(0o755)
        self.write(
            {
                "version": 1,
                "hooks": {"test": {"argv": ["./child"]}},
                "provision": {"hook": "deps"},
            }
        )
        failed = self.run_hook("test")
        self.assertEqual(failed.returncode, 64)
        self.assertIn("$.provision.hook", failed.stderr)
        self.assertFalse(marker.exists())

    def test_linked_worktree_is_the_subject_checkout(self):
        main = Path(self.temp.name) / "main"
        linked = Path(self.temp.name) / "linked checkout"
        main.mkdir()
        subprocess.run(("git", "init", "-q", str(main)), check=True)
        (main / "tracked").write_text("base")
        subprocess.run(("git", "-C", str(main), "add", "tracked"), check=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(main),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "base",
            ),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(main), "worktree", "add", "-q", "--detach", str(linked)),
            check=True,
        )
        (linked / ".subfloor").mkdir()
        child = linked / ".subfloor" / "root"
        child.write_text("#!/bin/sh\nprintf '%s\\n' \"$SC_DEVKIT_ROOT\"\n")
        child.chmod(0o755)
        (linked / ".subfloor" / "dev-kit.json").write_text(
            json.dumps({"version": 1, "hooks": {"test": {"argv": ["./.subfloor/root"]}}})
        )
        done = subprocess.run(
            (sys.executable, str(RUNNER), "run", str(linked), "test"),
            cwd=main,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout, f"{linked.resolve()}\n")
        self.assertIn(f"dev-kit checkout: {linked.resolve()}", done.stderr)

    def test_dispatcher_routes_every_lifecycle_verb_through_one_adapter(self):
        dispatch = (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text()
        for hook in ("deps", "test", "lint", "typecheck"):
            self.assertRegex(
                dispatch,
                rf'(?m)^\s*{hook}\)\s+sc_devkit_hook {hook} "\$@" ;;$',
            )


class SourcePolicyTest(unittest.TestCase):
    def test_source_repository_declares_all_four_hooks(self):
        declaration = load_declaration(ROOT)
        self.assertEqual(set(declaration.hooks), {"deps", "test", "lint", "typecheck"})
        for name, hook in declaration.hooks.items():
            self.assertEqual(hook.argv, ("./.subfloor/dev-kit", name))
            self.assertEqual(hook.resolved_executable, (ROOT / ".subfloor" / "dev-kit").resolve())

    def test_source_policy_is_outside_the_materialized_engine(self):
        from engine_manifest import ENGINE_PATHS

        self.assertEqual(ENGINE_PATHS[0], "sc")
        self.assertTrue(all(path.startswith(".super-coder/") for path in ENGINE_PATHS[1:]))
        self.assertNotIn(".subfloor", ENGINE_PATHS)

    def test_docker_host_managed_venv_is_verified_without_pip(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "checkout"
            (root / ".subfloor").mkdir(parents=True)
            (root / ".venv" / "bin").mkdir(parents=True)
            script = root / ".subfloor" / "dev-kit"
            shutil.copy2(SOURCE_DEVKIT, script)
            (root / "requirements.txt").write_text(
                "wu31-package-that-does-not-exist==1\n"
            )
            calls = base / "python-calls"
            outside_python = base / "outside-python"
            outside_python.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n"
            )
            outside_python.chmod(0o755)
            (root / ".venv" / "bin" / "python").symlink_to(outside_python)
            environment = dict(os.environ)
            environment.update(
                {"SC_DEVKIT_ROOT": str(root), "SC_DEVKIT_SEAT": "docker"}
            )
            done = subprocess.run(
                (str(script), "deps"),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("verifying without pip", done.stdout)
            self.assertIn("wu31-package-that-does-not-exist==1", done.stderr)
            self.assertNotIn("-m pip", calls.read_text())

    def test_docker_host_managed_test_never_falls_back_to_image_python(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "checkout"
            (root / ".subfloor").mkdir(parents=True)
            (root / ".venv" / "bin").mkdir(parents=True)
            script = root / ".subfloor" / "dev-kit"
            shutil.copy2(SOURCE_DEVKIT, script)
            outside_python = base / "outside-python"
            outside_python.write_text("#!/bin/sh\nexit 1\n")
            outside_python.chmod(0o755)
            (root / ".venv" / "bin" / "python").symlink_to(outside_python)
            fallback_marker = base / "fallback-ran"
            fallback = base / "fallback-python"
            fallback.write_text(
                f"#!/bin/sh\ntouch {shlex.quote(str(fallback_marker))}\n"
            )
            fallback.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                {
                    "SC_DEVKIT_ROOT": str(root),
                    "SC_DEVKIT_SEAT": "docker",
                    "SC_PYTHON": str(fallback),
                }
            )
            done = subprocess.run(
                (str(script), "test"),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(done.returncode, 126, done.stdout + done.stderr)
            self.assertIn("host-managed venv", done.stderr)
            self.assertIn("install it", done.stderr)
            self.assertFalse(fallback_marker.exists())

    def test_distributed_guidance_describes_fork_owned_hooks(self):
        skill = DEVKIT_SKILL.read_text()
        readme = (ROOT / "docs" / "README.md").read_text()
        readme_section = readme.split("### Dev kit", 1)[1].split("## Opt-in features", 1)[0]
        for guidance in (skill, readme_section):
            self.assertIn(".subfloor/dev-kit.json", guidance)
            self.assertIn("exit `78`", guidance)
            self.assertIn("no fork dev kit declared", guidance)
            self.assertNotIn("every `requirements*.txt`", guidance)

        self.assertLess(
            skill.index("invariant exact-execution hooks"),
            skill.index("## Read the active seat"),
        )
        self.assertNotIn("## You are in a container", skill)
        self.assertIn("boot document's execution-context section", skill)
        for state in ("absent", "invalid", "failed", "stale", "ready", "repair"):
            self.assertIn(f"| **{state}** |", skill)
        self.assertIn("Engine baseline", skill)
        self.assertIn("Fork extension", skill)
        self.assertIn("Checkout setup", skill)
        self.assertIn("Host prerequisites", skill)
        self.assertIn("never infers manifests", skill)
        self.assertIn("never installs privileged host packages", skill)
        self.assertIn("`./sc` remains valid", skill)
        self.assertIn("exit the container", skill)


class DevKitReseedConformanceTest(unittest.TestCase):
    def test_trailing_reseed_converges_dirty_downstream_and_replays(self):
        expected = seed_skills.parse_skill(DEVKIT_SKILL)
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
                "INSERT INTO skills VALUES "
                "(41,'dev_kit','stale description','wrong','old-command',1,"
                "'You are in a container',1),"
                "(99,'fork_only_skill','local','fork',NULL,0,'bespoke body',0);"
            )
            migration = DEVKIT_RESEED.read_text()
            con.executescript(migration)
            con.executescript(migration)
            actual = con.execute(
                "SELECT skill_id,name,description,category,command,common,content,"
                "is_deleted FROM skills WHERE name='dev_kit'"
            ).fetchone()
            local = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='fork_only_skill'"
            ).fetchone()

        self.assertEqual(
            actual,
            (
                41,
                expected["name"],
                expected["description"],
                expected["category"],
                expected["command"],
                expected["common"],
                expected["content"],
                0,
            ),
        )
        self.assertEqual(local, ("local", "fork", None, 0, "bespoke body", 0))

    def test_later_migrations_do_not_override_the_terminal_reseed(self):
        migrations = sorted(
            (ROOT / ".super-coder" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
        )
        later = migrations[migrations.index(DEVKIT_RESEED) + 1 :]
        self.assertTrue(later)
        for migration in later:
            with self.subTest(migration=migration.name):
                self.assertNotIn("  'dev_kit',", migration.read_text())


class DispatcherHelpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fork"
        scripts = self.root / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(RUNNER, scripts / "devkit.py")
        shutil.copy2(RUNNER.with_name("cli_entry.py"), scripts / "cli_entry.py")
        (self.root / ".subfloor").mkdir()
        capture = self.root / ".subfloor" / "capture"
        capture.write_text("#!/bin/sh\nprintf '<%s>\\n' \"$@\"\n")
        capture.chmod(0o755)
        (self.root / ".subfloor" / "dev-kit.json").write_text(
            json.dumps(
                {"version": 1, "hooks": {"test": {"argv": ["./.subfloor/capture"]}}}
            )
        )
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        self.dispatch = ROOT / ".super-coder" / "scripts" / "dispatch.sh"

    def run_dispatch(self, *arguments: str, python: str = sys.executable):
        environment = dict(os.environ)
        environment.update({"SC_CALLER_ROOT": str(self.root), "SC_PYTHON": python})
        return subprocess.run(
            ("sh", str(self.dispatch), "test", *arguments),
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_help_is_engine_owned_and_needs_no_python(self):
        done = self.run_dispatch("--help", python="/definitely/missing/python")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Usage: ./sc test", done.stdout)
        self.assertEqual(done.stderr, "")

    def test_separator_is_removed_and_later_help_is_literal(self):
        separated = self.run_dispatch("--", "--help")
        self.assertEqual(separated.returncode, 0, separated.stdout + separated.stderr)
        self.assertEqual(separated.stdout, "<--help>\n")
        literal = self.run_dispatch("literal", "--help")
        self.assertEqual(literal.returncode, 0, literal.stdout + literal.stderr)
        self.assertEqual(literal.stdout, "<literal>\n<--help>\n")


if __name__ == "__main__":
    unittest.main()
