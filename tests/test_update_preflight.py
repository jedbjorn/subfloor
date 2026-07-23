#!/usr/bin/env python3
"""Mutation sentinel for update's installed live-state preflight (#528)."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402


def fingerprint(paths: list[Path]) -> dict[str, tuple[int, int, str | None]]:
    out = {}
    for path in paths:
        stat = path.stat()
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path)] = (stat.st_mtime_ns, stat.st_size, digest)
    return out


class UpdatePreflightSentinelTest(unittest.TestCase):
    def test_live_refusal_precedes_every_installed_floor_mutation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            engine = root / ".super-coder"
            state = root / ".sc-state"
            workflow = root / ".github" / "workflows"
            engine.mkdir()
            state.mkdir()
            workflow.mkdir(parents=True)

            paths = [
                root / "sc",
                engine,
                engine / "schema.sql",
                engine / "scripts",
                engine / "scripts" / "update.py",
                workflow,
                workflow / "subfloor-visual-qa.yml",
                root / ".gitignore",
                state / "engine.ref.prev",
                state / "engine.ref",
            ]
            for path in paths:
                if path.suffix == "" and path.name in {"scripts"}:
                    path.mkdir()
                elif path in {engine, workflow}:
                    continue
                else:
                    path.write_text(f"sentinel:{path.name}\n")

            before = fingerprint(paths)
            with mock.patch.multiple(
                update,
                REPO_ROOT=root,
                ENGINE=engine,
                DB_PATH=engine / "shell_db.db",
                STATE_DIR=state,
                ENGINE_REF=state / "engine.ref",
                ENGINE_REF_PREV=state / "engine.ref.prev",
                EJECTED_MARKER=state / "ejected",
            ), mock.patch.object(
                update, "is_source_repo", return_value=False
            ), mock.patch.object(
                update, "fetch_update_ref", return_value="a" * 40
            ) as fetch, mock.patch.object(
                update.interface_reconcile,
                "live_refusal_reasons",
                return_value=["interface session 7 is occupied"],
            ), mock.patch.object(
                update, "migrate_engine_untrack"
            ) as untrack, mock.patch.object(
                update.install_mod, "ensure_gitignore"
            ) as gitignore, mock.patch.object(
                update, "materialize_fetched_engine"
            ) as materialize, mock.patch.object(
                update, "ensure_workflows"
            ) as workflows:
                with self.assertRaises(SystemExit) as ctx:
                    update.main([])

            self.assertIn("refusing", str(ctx.exception))
            fetch.assert_called_once()
            untrack.assert_not_called()
            gitignore.assert_not_called()
            materialize.assert_not_called()
            workflows.assert_not_called()
            self.assertEqual(
                fingerprint(paths),
                before,
                "update refusal changed installed hashes or mtimes",
            )


if __name__ == "__main__":
    unittest.main()
