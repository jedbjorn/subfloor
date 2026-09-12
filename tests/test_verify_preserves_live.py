"""Candidate verify must leave a newer live memory DB and artifacts untouched."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VerifyPreservesLiveTest(unittest.TestCase):
    def test_stale_snapshot_does_not_replace_live_db_or_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            checkout = base / "source"
            checkout.mkdir()
            shutil.copytree(ROOT / ".super-coder", checkout / ".super-coder",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db*"))
            shutil.copy2(ROOT / "sc", checkout / "sc")
            env = {k: v for k, v in os.environ.items() if not k.startswith("SC_")}
            env.update({"HOME": str(base / "home"),
                        "XDG_STATE_HOME": str(base / "state"),
                        "TMPDIR": str(base / "tmp"), "SC_ADMIN": "1"})
            (base / "tmp").mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                            "https://github.com/jedbjorn/subfloor.git"], check=True)
            scripts = checkout / ".super-coder" / "scripts"

            def run(script: str, *args: str,
                    overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                process_env = {**env, **(overrides or {})}
                return subprocess.run([sys.executable, str(scripts / script), *args],
                                      cwd=checkout, env=process_env, capture_output=True,
                                      text=True, timeout=120, check=False)

            for script, args in (("rebuild.py", ()),
                                 ("init_fork.py", ("--username", "verify")),
                                 ("snapshot.py", ())):
                result = run(script, *args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            # A source identity planted by an installed fork must never reach
            # the copied candidate, where it could redirect maintenance.
            (checkout / ".super-coder" / "instance.json").write_text(
                '{"instance_id":"0123456789abcdef0123456789abcdef"}\n'
            )
            (checkout / ".sc-state" / "local" / "skills_retired.json").write_text(
                '["dev_kit"]\n'
            )
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin",
                            "https://example.invalid/no-network-fork.git"], check=True)

            db = checkout / ".super-coder" / "shell_db.db"
            with sqlite3.connect(db) as con:
                con.execute("UPDATE shells SET bootstrapped=1, current_state='newer live write' "
                            "WHERE shell_id=1")
            live = sqlite3.connect(db)
            try:
                live.execute("PRAGMA journal_mode=WAL")
                live.execute("PRAGMA wal_autocheckpoint=0")
                live.execute("UPDATE shells SET current_state='uncheckpointed newer write' "
                             "WHERE shell_id=1")
                live.commit()
                paths = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
                before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths if p.exists()}
                artifacts = checkout / ".sc-state" / "local"
                artifact_before = {p.relative_to(artifacts): p.read_bytes()
                                   for p in artifacts.rglob("*") if p.is_file()}

                external_git = base / "external-git"
                subprocess.run(["git", "init", "-q", str(external_git)], check=True)
                external_index = base / "external-index"
                external_index.write_bytes(b"untouched external index")
                result = run("verify.py", overrides={
                    "GIT_DIR": str(external_git / ".git"),
                    "GIT_INDEX_FILE": str(external_index),
                    "GIT_WORK_TREE": str(external_git),
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(external_index.read_bytes(), b"untouched external index")
                self.assertIn("booted Planner", result.stdout)
                self.assertEqual(before,
                                 {p: (p.stat().st_ino, p.read_bytes()) for p in before})
                self.assertEqual(artifact_before,
                                 {p.relative_to(artifacts): p.read_bytes()
                                  for p in artifacts.rglob("*") if p.is_file()})
                self.assertEqual(live.execute(
                    "SELECT current_state FROM shells WHERE shell_id=1"
                ).fetchone()[0], "uncheckpointed newer write")
                self.assertEqual(list((base / "tmp").glob("sc-verify-*")), [])
                self.assertFalse((checkout / "AGENTS.md").exists())
                self.assertFalse((checkout / "CLAUDE.md").exists())

                before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths if p.exists()}
                snapshot = artifacts / "content.sql"
                snapshot.write_text("THIS IS NOT SQL\n")
                failed = run("verify.py")
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(before,
                                 {p: (p.stat().st_ino, p.read_bytes()) for p in before})
                self.assertEqual(list((base / "tmp").glob("sc-verify-*")), [])
            finally:
                live.close()


if __name__ == "__main__":
    unittest.main()
