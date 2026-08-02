#!/usr/bin/env python3
"""Contract pins for the dev-kit surface in the `sc` dispatcher (QAQC-02).

`sc` is POSIX sh, so these pin the wiring textually (the same style as the
refusal pins in test_eject.py) plus one live probe of the find-prune behavior:

  - `_sc_find_manifests` must prune `.sc-worktrees/` — each shell worktree is a
    sibling checkout of the same repo; descending would install/test every
    manifest N×.
  - `_sc_devtool` must resolve .venv → PATH in that order, and lint/typecheck
    must go through it (the ".venv or die" guard was a closed loop when the
    .venv is host-managed and in-sandbox pip is skipped).
  - The sandbox image must bake ruff + mypy — the PATH fallback _sc_devtool
    lands on in that host-managed case.

Run:
    python3 tests/test_devkit_sc.py
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SC = (ROOT / "sc").read_text()
DOCKERFILE = (ROOT / ".super-coder" / "Dockerfile").read_text()


def _extract_find_manifests() -> str:
    """The _sc_find_manifests function body, for a live run in a scratch tree."""
    m = re.search(r"_sc_find_manifests\(\) \{.*?\n\}", SC, re.S)
    assert m, "_sc_find_manifests not found in sc"
    return m.group(0)


def _run_python_test_presence(root: Path) -> bool:
    script = (
        f'here="{root}"\n{_extract_find_manifests()}\n'
        f'{_extract("_sc_has_python_tests")}\n'
        "_sc_has_python_tests\n"
    )
    return subprocess.run(["sh", "-c", script], check=False).returncode == 0


class FindManifestsTest(unittest.TestCase):
    def test_prunes_sc_worktrees_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "package.json").write_text("{}")
            wt = root / ".sc-worktrees" / "dev" / "app"
            wt.mkdir(parents=True)
            (wt / "package.json").write_text("{}")
            script = f'here="{root}"\n{_extract_find_manifests()}\n' \
                     f"_sc_find_manifests 'package.json'\n"
            out = subprocess.run(["sh", "-c", script], capture_output=True,
                                 text=True).stdout.splitlines()
            self.assertEqual(out, [str(root / "app" / "package.json")],
                             "worktree copies must be pruned — one repo, one "
                             "manifest walk")


class PythonTestPresenceTest(unittest.TestCase):
    def test_nested_tests_directory_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test = root / "nested" / "app" / "tests" / "test_health.py"
            test.parent.mkdir(parents=True)
            test.write_text("def test_health(): pass\n")
            self.assertTrue(_run_python_test_presence(root))

    def test_pruned_and_non_test_paths_do_not_count(self):
        ignored = (
            ".super-coder", ".sc-state", ".sc-worktrees/dev", ".git",
            ".venv", "venv", "__pycache__", "node_modules", "dist", "build",
            "vendor",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ignored:
                test = root / directory / "tests" / "test_shadow.py"
                test.parent.mkdir(parents=True)
                test.write_text("raise AssertionError('must stay pruned')\n")
            (root / "app").mkdir()
            (root / "app" / "test_outside_tests.py").write_text("pass\n")
            (root / "tests").mkdir()
            (root / "tests" / "health.py").write_text("pass\n")
            self.assertFalse(_run_python_test_presence(root))

    def test_tests_component_in_repo_parent_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tests" / "repo"
            source = root / "app" / "test_health.py"
            source.parent.mkdir(parents=True)
            source.write_text("pass\n")
            self.assertFalse(_run_python_test_presence(root))

    def test_presence_stops_after_first_matching_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "walked-every-candidate"
            first = root / "tests" / "test_first.py"
            first.parent.mkdir()
            first.write_text("pass\n")
            script = (
                f'here="{root}"\n'
                "_sc_find_manifests() {\n"
                f'  printf "%s\\n" "{first}"\n'
                "  candidate=0\n"
                "  while [ \"$candidate\" -lt 10000 ]; do\n"
                f'    printf "%s\\n" "{root}/src/test_$candidate.py" || exit\n'
                "    candidate=$((candidate + 1))\n"
                "  done\n"
                f'  : > "{marker}"\n'
                "}\n"
                f'{_extract("_sc_has_python_tests")}\n'
                "_sc_has_python_tests\n"
            )
            done = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True,
                check=False, timeout=5,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertFalse(marker.exists(),
                             "presence detection consumed candidates after a match")

    def test_sc_test_caches_one_presence_result_for_both_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "presence-calls"
            pytest = root / ".venv" / "bin" / "pytest"
            pytest.parent.mkdir(parents=True)
            pytest.write_text("#!/bin/sh\nexit 0\n")
            pytest.chmod(0o755)
            script = (
                f'here="{root}"\nPY=python3\n'
                f'_sc_has_python_tests() {{ echo called >> "{calls}"; return 0; }}\n'
                "_sc_venv_runnable() { return 0; }\n"
                "_sc_find_manifests() { return 0; }\n"
                "_sc_wants_pytest() { return 1; }\n"
                "sc_deps() { echo unexpected provisioning >&2; return 99; }\n"
                f'{_extract("sc_test")}\n'
                "sc_test\n"
            )
            done = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["called"])

    def test_nested_suite_provisions_then_runs_one_root_pytest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test = root / "nested" / "app" / "tests" / "test_health.py"
            test.parent.mkdir(parents=True)
            test.write_text("def test_health(): pass\n")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            pytest = fake_bin / "pytest"
            pytest.write_text(
                "#!/bin/sh\n"
                "printf 'PYTEST cwd=%s args=%s\\n' \"$PWD\" \"$*\"\n"
            )
            pytest.chmod(0o755)
            script = (
                f'here="{root}"\nPY=python3\n'
                f'PATH="{fake_bin}:$PATH"\nexport PATH\n'
                f'{_extract_find_manifests()}\n'
                f'{_extract("_sc_has_python_tests")}\n'
                f'{_extract("_sc_wants_pytest")}\n'
                "sc_deps() { echo PROVISIONED; }\n"
                f'{_extract("sc_test")}\n'
                "sc_test\n"
            )
            done = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(done.stderr, "")
            self.assertEqual(done.stdout.count("PROVISIONED\n"), 1)
            self.assertEqual(
                [line for line in done.stdout.splitlines()
                 if line.startswith("PYTEST ")],
                [f"PYTEST cwd={root} args="],
            )


class DevtoolResolutionTest(unittest.TestCase):
    def test_devtool_prefers_venv_then_path(self):
        body = re.search(r"_sc_devtool\(\) \{.*?\n\}", SC, re.S)
        self.assertIsNotNone(body, "_sc_devtool missing from sc")
        text = body.group(0)
        venv_at = text.index('"$venv/bin/$1"')
        path_at = text.index("command -v")
        self.assertLess(venv_at, path_at,
                        ".venv copy must win over the PATH fallback — fork "
                        "pins + [tool.*] config ride the venv copy")

    def test_lint_and_typecheck_use_devtool(self):
        for fn in ("sc_lint", "sc_typecheck"):
            body = re.search(fn + r"\(\) \{.*?\n\}", SC, re.S).group(0)
            self.assertIn("_sc_devtool", body,
                          f"{fn} must resolve its tool via _sc_devtool — a "
                          f"bare '.venv or die' guard is the QAQC-02 dead loop")

    def test_host_managed_error_names_the_host_fix(self):
        self.assertIn("host-managed", SC)
        self.assertNotIn("no .venv/bin/ruff — run ./sc deps first", SC,
                         "the closed-loop error copy must be gone")


class ImageFallbackTest(unittest.TestCase):
    def test_image_bakes_ruff_and_mypy(self):
        self.assertRegex(DOCKERFILE, r"pip install[^\n]*ruff[^\n]*mypy",
                         "the sandbox image must bake ruff + mypy — the PATH "
                         "fallback for host-managed-.venv forks")

    def test_image_bakes_pytest(self):
        """sc_test's fallback lands on the PATH pytest. It sat in the running
        image by accident (pip-installed, in no layer), so a clean rebuild
        would have removed the thing the fallback depends on."""
        self.assertRegex(DOCKERFILE, r"pip install[^\n]*pytest",
                         "the sandbox image must bake pytest — sc_test falls "
                         "back to it when the .venv is not runnable here")


def _extract(fn: str) -> str:
    """One sh function body out of `sc`, for a live run in a scratch tree."""
    m = re.search(fn + r"\(\) \{.*?\n\}", SC, re.S)
    assert m, f"{fn} not found in sc"
    return m.group(0)


class VenvRunnableTest(unittest.TestCase):
    """A bind-mounted .venv records its interpreter as an UNVERSIONED symlink,
    so the same path resolves to a different python minor on the host and in
    the sandbox. The shims then exist, are executable, and import nothing.
    `-x` cannot see that; _sc_venv_runnable must."""

    def _probe(self, interpreter_version: str, packages_for: str | None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / ".venv"
            (venv / "bin").mkdir(parents=True)
            # A stand-in interpreter: the probe only asks it its version.
            python = venv / "bin" / "python"
            python.write_text(f"#!/bin/sh\necho {interpreter_version}\n")
            python.chmod(0o755)
            if packages_for:
                (venv / "lib" / f"python{packages_for}"
                 / "site-packages").mkdir(parents=True)
            script = (f'here="{root}"\n{_extract("_sc_venv_runnable")}\n'
                      "_sc_venv_runnable && echo RUNNABLE || echo NOT\n")
            return subprocess.run(["sh", "-c", script], capture_output=True,
                                  text=True).stdout.strip()

    def test_matching_interpreter_and_site_packages_is_runnable(self):
        self.assertEqual(self._probe("3.12", "3.12"), "RUNNABLE")

    def test_minor_version_skew_is_not_runnable(self):
        """The live defect: venv built by host py3.14, resolved here as py3.13.
        Every .venv/bin/* shim is -x and every import fails."""
        self.assertEqual(self._probe("3.13", "3.14"), "NOT")

    def test_missing_interpreter_is_not_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (f'here="{tmp}"\n{_extract("_sc_venv_runnable")}\n'
                      "_sc_venv_runnable && echo RUNNABLE || echo NOT\n")
            out = subprocess.run(["sh", "-c", script], capture_output=True,
                                 text=True).stdout.strip()
        self.assertEqual(out, "NOT")


class VenvRunnabilityGateTest(unittest.TestCase):
    """Both tool-resolution paths must consult the probe. Existence checks
    alone put an unimportable shim ahead of a working PATH copy — for sc_test
    that fails the ENTIRE suite with ModuleNotFoundError, which reads as a
    real test failure."""

    def test_devtool_gates_the_venv_copy_on_runnability(self):
        # Anchor on the line that RETURNS the venv copy, not on the body: the
        # probe is also named in the error branch, so a body-wide assertIn
        # passes vacuously when the gate itself is stripped.
        body = _extract("_sc_devtool")
        gate = [ln for ln in body.splitlines()
                if "printf" in ln and '"$venv/bin/$1"' in ln]
        self.assertEqual(len(gate), 1, "expected one venv-resolution line")
        self.assertIn("_sc_venv_runnable", gate[0],
                      "the venv copy is returned without probing that it runs")
        # It must still WIN when it does run — fork pins + config ride it.
        self.assertLess(body.index('"$venv/bin/$1"'), body.index("command -v"))

    def test_sc_test_gates_the_venv_pytest_on_runnability(self):
        body = _extract("sc_test")
        self.assertLess(body.index('pytest_bin="$venv/bin/pytest"'),
                        body.index("command -v pytest"),
                        "the .venv pytest must be preferred when runnable and "
                        "only then fall through to the baked PATH copy")

    def test_sc_test_never_runs_an_unprobed_venv_pytest(self):
        """The regression this closes: `[ -x $venv/bin/pytest ]` deciding on
        its own what to EXECUTE. Existence may still gate the self-heal
        (provision when absent) and a diagnostic line — only execution is
        pinned, and it must go through the probed resolution."""
        body = _extract("sc_test")
        self.assertIn('"$pytest_bin" "$@"', body)
        self.assertNotIn('"$venv/bin/pytest" "$@"', body,
                         "the suite must run the PROBED pytest, never the raw "
                         "venv path")
        # Every selection of the venv copy is guarded by the probe.
        for m in re.finditer(r'pytest_bin="\$venv/bin/pytest"', body):
            self.assertIn("_sc_venv_runnable", body[max(0, m.start() - 200):m.start()],
                          "the venv pytest was selected without the probe")


class DepsHostManagedVerifyTest(unittest.TestCase):
    """#314/#324/#339 — the sandbox pip-skip must never green-lie: declared
    pins missing from the host-managed tree are a hard failure, not a ✓."""

    def test_skip_branch_verifies_and_fails_loud(self):
        body = re.search(r"sc_deps\(\) \{.*?\n\}", SC, re.S).group(0)
        self.assertIn("host-managed venv is missing declared python deps", body)
        self.assertIn("importlib.metadata", body)
        # the failure sets rc, so `✓ deps: done` can't follow a missing pin
        miss_at = body.index("missing declared python deps")
        self.assertIn("rc=1", body[miss_at:miss_at + 600])

    def test_verify_snippet_flags_missing_pins_live(self):
        import sys
        m = re.search(r'-c \'\n(import importlib.*?)\n\'\)"', SC, re.S)
        self.assertIsNotNone(m, "deps verify python snippet missing from sc")
        snippet = m.group(1)
        import importlib.metadata as md
        present = next(iter(md.distributions())).metadata["Name"]
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text(f"{present}\n"
                           "definitely-not-a-real-dist-xyz==9.9\n"
                           "# a comment\n"
                           "-e ./local\n"
                           "https://example.com/wheel.whl\n")
            out = subprocess.run([sys.executable, "-c", snippet],
                                 input=f"{req}\n", capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            flagged = [l for l in out.stdout.splitlines() if l]
            self.assertEqual(flagged, ["definitely-not-a-real-dist-xyz==9.9"],
                             "exactly the missing plain pin — present dists, "
                             "comments, editables, URLs all pass through")


class TestVerbExitFiveTest(unittest.TestCase):
    """#310 — pytest exit 5 (nothing collected) on a bare `./sc test` is a
    JS-only fork, not a failed suite; with explicit args it stays a failure."""

    def test_exit_five_handled_only_for_bare_invocation(self):
        body = re.search(r"sc_test\(\) \{.*?\n\}", SC, re.S).group(0)
        self.assertIn('"$prc" -eq 5', body)
        self.assertIn('[ $# -eq 0 ]', body)
        self.assertIn("not counted as a failure", body)


if __name__ == "__main__":
    unittest.main()
