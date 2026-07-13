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
