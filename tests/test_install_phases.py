#!/usr/bin/env python3
"""The critical installer runner streams detail and fails with phase identity."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"


class CriticalPhaseRunnerTest(unittest.TestCase):
    def test_child_detail_and_exact_failure_are_preserved(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(SCRIPTS)!r}); "
            "import install; "
            "install.run_critical_phase("
            "'Injected phase', [sys.executable, '-c', "
            "\"import sys; print('child stdout', flush=True); "
            "print('child stderr', file=sys.stderr, flush=True); sys.exit(23)\"])\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 23)
        self.assertLess(completed.stdout.index("Injected phase"),
                        completed.stdout.index("child stdout"))
        self.assertIn("child stderr", completed.stderr)
        self.assertIn("critical phase failed: Injected phase", completed.stderr)
        self.assertIn(f"interpreter: {Path(sys.executable).resolve()}", completed.stderr)
        self.assertIn("exit code: 23", completed.stderr)
        self.assertIn("retry: ./sc install", completed.stderr)


class EngineInternalChildrenTest(unittest.TestCase):
    def test_every_instance_maintaining_phase_runs_as_the_engine(self) -> None:
        """map-setup, snapshot and render run with the shell token dropped,
        exactly as update runs map-setup (review S2)."""
        import ast

        tree = ast.parse((SCRIPTS / "install.py").read_text())
        internal = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "attr", "") == "engine_internal_env"
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertTrue(internal)
        phases = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "run_critical_phase"):
                script = ast.unparse(node.args[1])
                env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
                phases[script] = env.id if isinstance(env, ast.Name) else None
        for script in ("map_setup.py", "snapshot.py", "render.py"):
            matches = [env for argv, env in phases.items() if script in argv]
            self.assertEqual(len(matches), 1, script)
            self.assertIn(matches[0], internal, script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
