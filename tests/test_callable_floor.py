#!/usr/bin/env python3
"""Regression tests for the dispatcher/materialized-engine callable floor."""
from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import callable_floor  # noqa: E402
import install  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CallableFloorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / ".super-coder" / "scripts"
        self.scripts.mkdir(parents=True)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Callable Floor Test")
        _git(self.root, "config", "user.email", "floor@example.invalid")

    def write_dispatcher(self, script: str = "sprint_cli.py") -> None:
        dispatcher = self.root / "sc"
        dispatcher.write_text(
            "#!/bin/sh\n"
            "S=\"$PWD/.super-coder/scripts\"\n"
            f'exec "$PY" "$S/{script}" "$@"\n'
        )
        dispatcher.chmod(0o755)

    def commit_floor(self) -> str:
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", "callable floor")
        return _git(self.root, "rev-parse", "HEAD")

    def test_exact_dispatcher_and_present_route_are_coherent(self) -> None:
        self.write_dispatcher()
        (self.scripts / "sprint_cli.py").write_text("print('help')\n")
        ref = self.commit_floor()

        self.assertEqual(
            callable_floor.inspect_callable_floor(
                self.root,
                expected_ref=ref,
            ),
            (),
        )

    def test_stale_dispatcher_reports_ref_mismatch_and_missing_route(self) -> None:
        self.write_dispatcher()
        (self.scripts / "sprint_cli.py").write_text("print('help')\n")
        ref = self.commit_floor()
        self.write_dispatcher("sprint.py")

        self.assertEqual(
            callable_floor.inspect_callable_floor(
                self.root,
                expected_ref=ref,
            ),
            (
                f"dispatcher does not match engine ref {ref[:12]}",
                "dispatcher routes to missing engine script(s): sprint.py",
            ),
        )

    def test_pruned_ref_falls_back_to_manifest_dispatcher_hash(self) -> None:
        self.write_dispatcher()
        dispatcher = (self.root / "sc").read_bytes()
        manifest = {"sc": hashlib.sha256(dispatcher).hexdigest()}
        (self.root / ".super-coder" / "engine.manifest").write_text(
            json.dumps(manifest) + "\n"
        )
        (self.scripts / "sprint_cli.py").write_text("print('help')\n")

        self.assertEqual(
            callable_floor.inspect_callable_floor(
                self.root,
                expected_ref="f" * 40,
            ),
            (),
        )
        self.write_dispatcher("other.py")
        (self.scripts / "other.py").write_text("print('other')\n")
        self.assertEqual(
            callable_floor.inspect_callable_floor(
                self.root,
                expected_ref="f" * 40,
            ),
            (
                "dispatcher does not match the materialized manifest for "
                "engine ref ffffffffffff",
            ),
        )

    def test_failure_names_the_supported_main_checkout_repair(self) -> None:
        self.write_dispatcher("sprint.py")
        with self.assertRaises(SystemExit) as raised:
            callable_floor.require_callable_floor(
                self.root,
                expected_ref=None,
                context="session launch",
            )

        self.assertEqual(
            str(raised.exception),
            "session launch: callable dispatcher/engine floor mismatch:\n"
            "  - .sc-state/engine.ref is missing or empty\n"
            "  - dispatcher routes to missing engine script(s): sprint.py\n"
            "  supported repair: run the update from the main checkout:\n"
            f"    cd {self.root} && ./sc update",
        )

    def test_malformed_pin_is_not_accepted_as_a_manifest_authority(self) -> None:
        state = self.root / ".sc-state"
        state.mkdir()
        (state / "engine.ref").write_text("not-a-sha\n")

        self.assertIsNone(callable_floor.read_engine_ref(self.root))

    def test_install_verifies_callable_floor_before_publishing_pin(self) -> None:
        source = inspect.getsource(install.main)
        self.assertLess(
            source.index("callable_floor.require_callable_floor"),
            source.index("write_engine_ref(pinned)"),
        )


if __name__ == "__main__":
    unittest.main()
