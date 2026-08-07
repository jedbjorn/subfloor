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


if __name__ == "__main__":
    unittest.main(verbosity=2)
