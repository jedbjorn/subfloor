"""Source-owned typecheck hook resolves flat engine modules without hiding errors."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SourceTypecheckHookTest(unittest.TestCase):
    def test_engine_roots_and_caller_path_preserve_type_diagnostics(self) -> None:
        if shutil.which("mypy") is None and not (ROOT / ".venv/bin/mypy").is_file():
            self.skipTest("declared mypy dependency is unavailable")

        with tempfile.TemporaryDirectory() as td:
            scratch = Path(td)
            (scratch / "caller_module.py").write_text("name: str = 'caller'\n")
            subject = scratch / "subject.py"
            imports = "import update\nimport server\nimport flat\nimport caller_module\n"
            env = {**os.environ, "SC_DEVKIT_ROOT": str(ROOT), "MYPYPATH": str(scratch)}

            def check(value: str) -> subprocess.CompletedProcess[str]:
                subject.write_text(imports + f"answer: int = {value}\n")
                return subprocess.run(
                    [str(ROOT / ".subfloor/dev-kit"), "typecheck", "--follow-imports=silent", str(subject)],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            good = check("1")
            self.assertEqual(0, good.returncode, good.stdout + good.stderr)
            bad = check("'wrong'")
            self.assertEqual(1, bad.returncode, bad.stdout + bad.stderr)
            self.assertIn("[assignment]", bad.stdout)
            self.assertNotIn("[import-not-found]", bad.stdout)


if __name__ == "__main__":
    unittest.main()
