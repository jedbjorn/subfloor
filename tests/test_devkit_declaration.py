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
    / "0241_global_skill_simplification.sql"
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

    def test_boot_visible_declaration_fields_reject_markdown_injection(self):
        (self.root / "safe").mkdir()
        attacks = (
            ({"argv": ["tool`\n## injected"]}, r"\.argv\[0\]"),
            ({"argv": ["tool"], "cwd": "safe`\n## injected"}, r"\.cwd"),
            ({"argv": ["tool\x1b[31m"]}, r"\.argv\[0\]"),
        )
        for hook, field in attacks:
            with self.subTest(hook=hook):
                self.assert_invalid(
                    {"version": 1, "hooks": {"test": hook}},
                    field + ": must not contain control characters or Markdown",
                )
        for dockerfile in (
            ".subfloor/Dockerfile`injected",
            ".subfloor/Dockerfile\n## injected",
        ):
            with self.subTest(dockerfile=dockerfile):
                (self.root / dockerfile).write_text("FROM scratch\n")
                self.assert_invalid(
                    {"version": 1, "sandbox": {"dockerfile": dockerfile}},
                    r"\.sandbox\.dockerfile: must not contain control characters "
                    "or Markdown",
                )

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

    def test_native_apt_packages_are_exact_sorted_atoms(self):
        self.write(
            {
                "version": 1,
                "sandbox": {
                    "packages": {
                        "apt": ["zlib1g=1:1.2.13.dfsg-1", "ca-certificates", "git+all"],
                    }
                },
            }
        )
        packages = load_declaration(self.root).sandbox.packages
        self.assertEqual(
            packages.canonical_atoms,
            ("ca-certificates", "git+all", "zlib1g=1:1.2.13.dfsg-1"),
        )
        self.assertEqual(packages.apt[2].version, "1:1.2.13.dfsg-1")

    def test_native_apt_invalidity_is_package_local_and_never_inferred(self):
        invalid = (
            [],
            ["a"],
            ["Curl"],
            ["curl amd64"],
            ["curl:amd64"],
            ["--no-install-recommends"],
            ["https://example.invalid/pkg.deb"],
            ["curl;rm"],
            ["curl="],
            ["curl=latest"],
            ["curl=1=2"],
            ["curl", "curl=1.0"],
            ["aa"] * 65,
        )
        for index, atoms in enumerate(invalid):
            with self.subTest(index=index, atoms=atoms[:2]):
                self.write(
                    {"version": 1, "sandbox": {"packages": {"apt": atoms}}}
                )
                sandbox = load_declaration(self.root).sandbox
                self.assertIsNone(sandbox.packages)
                self.assertIsNotNone(sandbox.package_error)

        self.write(
            {
                "version": 1,
                "sandbox": {"packages": {"apt": ["curl"], "pip": ["x"]}},
            }
        )
        sandbox = load_declaration(self.root).sandbox
        self.assertIsNone(sandbox.packages)
        self.assertIn("unknown key 'pip'", sandbox.package_error)

    def test_native_apt_name_length_boundary_is_exact(self):
        name_128 = "a" * 128
        self.write(
            {"version": 1, "sandbox": {"packages": {"apt": [name_128]}}}
        )
        self.assertEqual(
            load_declaration(self.root).sandbox.packages.canonical_atoms,
            (name_128,),
        )

        self.write(
            {"version": 1, "sandbox": {"packages": {"apt": ["a" * 129]}}}
        )
        self.assertIn(
            "name must match", load_declaration(self.root).sandbox.package_error
        )

    def test_native_apt_aggregate_byte_limit_is_exact(self):
        exact = [f"p{index:02d}" + "a" * 125 for index in range(64)]
        self.assertEqual(sum(len(atom.encode()) for atom in exact), 8192)
        self.write(
            {"version": 1, "sandbox": {"packages": {"apt": exact}}}
        )
        self.assertEqual(len(load_declaration(self.root).sandbox.packages.apt), 64)

        over = [atom + "=1" for atom in exact]
        self.write(
            {"version": 1, "sandbox": {"packages": {"apt": over}}}
        )
        self.assertIn(
            "at most 8192 UTF-8 bytes",
            load_declaration(self.root).sandbox.package_error,
        )

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

    def full_environment(self, **updates: str) -> dict[str, str]:
        environment = dict(os.environ)
        environment["SC_DEVKIT_OUTPUT"] = "full"
        environment.update(updates)
        return environment

    def compact_log(self, done: subprocess.CompletedProcess) -> Path:
        line = next(
            item for item in done.stderr.splitlines() if item.startswith("dev-kit log: ")
        )
        return self.root / line.removeprefix("dev-kit log: ")

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
        done = self.run_hook("test", *arguments, env=self.full_environment())
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
        environment = self.full_environment(
            PATH=f"{binary}:{os.environ['PATH']}", SC_SANDBOX="1"
        )
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

    def test_compact_success_is_bounded_and_raw_log_is_byte_complete(self):
        child = self.root / "huge-success"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "for number in range(10000):\n"
            "    prefix = b'\\x1b[31mwarning\\x1b[0m' if number == 4321 else b'line'\n"
            "    sys.stdout.buffer.write(prefix + b'-' + str(number).encode() + b'\\n')\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./huge-success"]}}})

        done = self.run_hook("test")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        displayed = done.stdout + done.stderr
        self.assertLessEqual(len(displayed.splitlines()), 80)
        self.assertLessEqual(len(displayed.encode()), 16 * 1024)
        self.assertNotIn("\x1b", displayed)
        self.assertIn("dev-kit exit status: 0", displayed)
        self.assertIn("dev-kit output:", displayed)
        self.assertIn("10000 lines", displayed)
        self.assertIn("SC_DEVKIT_OUTPUT=full ./sc test", displayed)
        log = self.compact_log(done)
        expected = b"".join(
            (
                (b"\x1b[31mwarning\x1b[0m" if number == 4321 else b"line")
                + b"-"
                + str(number).encode()
                + b"\n"
            )
            for number in range(10000)
        )
        self.assertEqual(log.read_bytes(), expected)
        self.assertEqual(list(log.parent.glob("*.running")), [])

    def test_compact_failure_preserves_status_bounds_and_omission_evidence(self):
        child = self.root / "huge-failure"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "for number in range(2000):\n"
            "    label = 'fatal error' if number == 999 else 'detail'\n"
            "    print(f'{label}-{number}')\n"
            "raise SystemExit(23)\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"lint": {"argv": ["./huge-failure"]}}})

        done = self.run_hook("lint")

        self.assertEqual(done.returncode, 23)
        displayed = done.stdout + done.stderr
        self.assertLessEqual(len(displayed.splitlines()), 240)
        self.assertLessEqual(len(displayed.encode()), 48 * 1024)
        self.assertIn("fatal error-999", displayed)
        self.assertIn("excerpt omitted:", displayed)
        self.assertIn("hook state: failed", displayed)
        raw = self.compact_log(done).read_text()
        self.assertTrue(raw.startswith("detail-0\n"))
        self.assertIn("fatal error-999\n", raw)
        self.assertTrue(raw.endswith("detail-1999\n"))

    def test_compact_single_huge_line_keeps_memory_and_display_bounded(self):
        child = self.root / "huge-line"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./huge-line"]}}})

        done = self.run_hook("test")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        displayed = done.stdout + done.stderr
        self.assertLessEqual(len(displayed.splitlines()), 80)
        self.assertLessEqual(len(displayed.encode()), 16 * 1024)
        self.assertIn("[line truncated]", displayed)
        self.assertEqual(self.compact_log(done).stat().st_size, 2 * 1024 * 1024)

    def test_compact_display_normalizes_controls_before_enforcing_bounds(self):
        child = self.root / "controls"
        raw = b"raw\rreturn\bbackspace\x00nul\x1b[31mred\x1b[0m\n"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.stdout.buffer.write({raw!r})\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./controls"]}}})
        adversarial = "return\rback\bspace\x1b[31m" + ("metadata-line\n" * 100)

        done = self.run_hook("test", adversarial)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        displayed = done.stdout + done.stderr
        self.assertLessEqual(len(displayed.splitlines()), 80)
        self.assertLessEqual(len(displayed.encode()), 16 * 1024)
        for control in ("\n", "\r", "\b", "\x00", "\x1b"):
            if control == "\n":
                continue
            self.assertNotIn(control, displayed)
        self.assertIn(r"metadata-line\n", displayed)
        self.assertIn(r"return\rback\bspace", displayed)
        self.assertIn(r"raw\rreturn\bbackspace\x00nulred", displayed)
        self.assertEqual(self.compact_log(done).read_bytes(), raw)

    def test_full_mode_inherits_streams_and_status_without_creating_a_log(self):
        child = self.root / "full-failure"
        child.write_text(
            "#!/bin/sh\nprintf 'full-out\\n'\nprintf 'full-err\\n' >&2\nexit 17\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"typecheck": {"argv": ["./full-failure"]}}})

        done = self.run_hook("typecheck", env=self.full_environment())

        self.assertEqual(done.returncode, 17)
        self.assertEqual(done.stdout, "full-out\n")
        self.assertIn("full-err\n", done.stderr)
        self.assertNotIn("dev-kit log:", done.stderr)
        self.assertFalse((self.root / ".sc-state").exists())

    def test_compact_log_merges_stdout_and_stderr_without_losing_bytes(self):
        child = self.root / "merged"
        child.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "os.write(1, b'first-out\\n')\n"
            "os.write(2, b'second-err\\n')\n"
            "os.write(1, b'third-out\\n')\n"
        )
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"lint": {"argv": ["./merged"]}}})

        done = self.run_hook("lint")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            self.compact_log(done).read_bytes(),
            b"first-out\nsecond-err\nthird-out\n",
        )

    def test_invalid_mode_rejects_before_child_and_deps_ignores_the_mode(self):
        marker = self.root / "ran"
        child = self.root / "child"
        child.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'direct-output\\n'\n")
        child.chmod(0o755)
        self.write(
            {
                "version": 1,
                "hooks": {
                    "test": {"argv": ["./child"]},
                    "deps": {"argv": ["./child"]},
                },
            }
        )
        environment = dict(os.environ)
        environment["SC_DEVKIT_OUTPUT"] = "verbose"

        invalid = self.run_hook("test", env=environment)
        self.assertEqual(invalid.returncode, 64)
        self.assertIn("must be 'compact' or 'full'", invalid.stderr)
        self.assertFalse(marker.exists())

        deps = self.run_hook("deps", env=environment)
        self.assertEqual(deps.returncode, 0, deps.stdout + deps.stderr)
        self.assertEqual(deps.stdout, "direct-output\n")
        self.assertTrue(marker.exists())
        self.assertNotIn("dev-kit log:", deps.stderr)

    def test_compact_retention_keeps_newest_twenty_and_never_running_files(self):
        child = self.root / "success"
        child.write_text("#!/bin/sh\nprintf 'new-log\\n'\n")
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./success"]}}})
        directory = self.root / ".sc-state" / "local" / "devkit-logs" / "test"
        directory.mkdir(parents=True)
        for number in range(25):
            path = directory / f"old-{number:02d}.log"
            path.write_text(str(number))
            os.utime(path, ns=(number + 1, number + 1))
        live = directory / "other-process.running"
        live.write_text("still-live")

        done = self.run_hook("test")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        finalized = list(directory.glob("*.log"))
        self.assertEqual(len(finalized), 20)
        self.assertIn(self.compact_log(done), finalized)
        self.assertEqual(live.read_text(), "still-live")

    def test_concurrent_compact_runs_use_distinct_atomic_logs(self):
        child = self.root / "capture"
        child.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"lint": {"argv": ["./capture"]}}})
        commands = [
            (sys.executable, str(RUNNER), "run", str(self.root), "lint", value)
            for value in ("first", "second")
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=self.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for command in commands
        ]
        for process in processes:
            process.communicate(timeout=10)

        self.assertEqual([process.returncode for process in processes], [0, 0])
        logs = list(
            (self.root / ".sc-state" / "local" / "devkit-logs" / "lint").glob("*.log")
        )
        self.assertEqual(len(logs), 2)
        self.assertEqual({path.read_text() for path in logs}, {"first\n", "second\n"})
        self.assertEqual(
            list(logs[0].parent.glob("*.running")),
            [],
        )

    def test_interruption_reaps_real_child_retains_log_and_reports_path(self):
        child = self.root / "interrupt"
        child.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        child.chmod(0o755)
        self.write({"version": 1, "hooks": {"test": {"argv": ["./interrupt"]}}})

        real_popen = devkit.subprocess.Popen
        spawned = []

        class InterruptedProcess:
            def __init__(self, *args, **kwargs):
                self.child = real_popen(*args, **kwargs)
                self.interrupted = False
                spawned.append(self.child)

            def poll(self):
                return self.child.poll()

            def terminate(self):
                return self.child.terminate()

            def kill(self):
                return self.child.kill()

            def wait(self, *args, **kwargs):
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                return self.child.wait(*args, **kwargs)

        def cleanup_children():
            for process in spawned:
                if process.poll() is None:
                    process.kill()
                process.wait()

        self.addCleanup(cleanup_children)

        output = io.StringIO()
        with contextlib.redirect_stderr(output), mock.patch.object(
            devkit.subprocess, "Popen", side_effect=InterruptedProcess
        ), mock.patch.object(
            devkit, "invoking_checkout", return_value=self.root.resolve()
        ), mock.patch.object(
            devkit, "_main_checkout", return_value=self.root.resolve()
        ), self.assertRaises(KeyboardInterrupt):
            devkit.run_hook(self.root, "test", ())

        running = list(
            (self.root / ".sc-state" / "local" / "devkit-logs" / "test").glob(
                "*.running"
            )
        )
        self.assertEqual(len(running), 1)
        self.assertIn(str(running[0].relative_to(self.root)), output.getvalue())
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawned[0].pid, 0)

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
        environment = dict(os.environ)
        environment["SC_DEVKIT_OUTPUT"] = "full"
        done = subprocess.run(
            (sys.executable, str(RUNNER), "run", str(linked), "test"),
            cwd=main,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout, f"{linked.resolve()}\n")
        self.assertIn(f"dev-kit checkout: {linked.resolve()}", done.stderr)

        compact_environment = dict(os.environ)
        compact_environment.pop("SC_DEVKIT_OUTPUT", None)
        compact = subprocess.run(
            (sys.executable, str(RUNNER), "run", str(linked), "test"),
            cwd=main,
            env=compact_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(compact.returncode, 0, compact.stdout + compact.stderr)
        log_line = next(
            line
            for line in compact.stderr.splitlines()
            if line.startswith("dev-kit log: ")
        )
        log = main / log_line.removeprefix("dev-kit log: ")
        self.assertEqual(log.read_text(), f"{linked.resolve()}\n")
        self.assertFalse((linked / ".sc-state").exists())

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
            self.assertIn("sc deps", guidance)
            self.assertIn("sc test", guidance)
            self.assertNotIn("every `requirements*.txt`", guidance)

        self.assertNotIn("## You are in a container", skill)
        for state in ("absent", "invalid", "failed", "stale", "advisory", "ready", "repair"):
            self.assertIn(f"`{state}`", skill)
        self.assertIn("Engine baseline", skill)
        self.assertIn("## Seats and evidence", skill)
        self.assertIn("Host hooks use the host checkout", skill)
        self.assertIn("Container hooks use the", skill)
        self.assertIn("does not infer project policy", skill)
        self.assertIn("not the fork's test assertions", skill)


class DevKitReseedConformanceTest(unittest.TestCase):
    def test_trailing_reseed_preserves_custom_downstream_dev_kit_and_replays(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(
                "CREATE TABLE shells ("
                "shell_id INTEGER PRIMARY KEY, flavor TEXT, "
                "system_prompt TEXT NOT NULL);"
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
                "CREATE TABLE shell_skills (shell_id INTEGER, skill_id INTEGER);"
                "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
                "UNIQUE(flavor,skill_id));"
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
                "dev_kit",
                "stale description",
                "wrong",
                "old-command",
                1,
                "You are in a container",
                1,
            ),
        )
        self.assertEqual(local, ("local", "fork", None, 0, "bespoke body", 0))

    def test_later_migrations_do_not_override_the_terminal_reseed(self):
        migrations = sorted(
            (ROOT / ".super-coder" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
        )
        later = migrations[migrations.index(DEVKIT_RESEED) + 1 :]
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
        shutil.copy2(RUNNER.with_name("artifact_policy.py"), scripts / "artifact_policy.py")
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
        environment.update(
            {
                "SC_CALLER_ROOT": str(self.root),
                "SC_PYTHON": python,
                "SC_DEVKIT_OUTPUT": "full",
            }
        )
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
