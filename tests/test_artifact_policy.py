#!/usr/bin/env python3
"""Two-mode persistence contract: tracked stays default, local never publishes."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "render"))
import artifact_policy  # noqa: E402
import flat  # noqa: E402


class ArtifactPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.engine = self.tmp / ".super-coder"
        self.state = self.tmp / ".sc-state"
        self.engine.mkdir()
        self.state.mkdir()
        self.saved = {
            name: getattr(artifact_policy, name)
            for name in (
                "ENGINE", "REPO_ROOT", "STATE_DIR", "LOCAL_DIR",
                "INSTANCE_CONFIG", "SOURCE_POLICY",
            )
        }
        artifact_policy.ENGINE = self.engine
        artifact_policy.REPO_ROOT = self.tmp
        artifact_policy.STATE_DIR = self.state
        artifact_policy.LOCAL_DIR = self.state / "local"
        artifact_policy.INSTANCE_CONFIG = self.engine / "instance.json"
        artifact_policy.SOURCE_POLICY = self.engine / "source-policy.json"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("SC_ARTIFACT_MODE", None)

    def tearDown(self):
        self.env.stop()
        for name, value in self.saved.items():
            setattr(artifact_policy, name, value)

    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def test_downstream_default_remains_tracked(self):
        with mock.patch.object(artifact_policy, "_source_policy_is_tracked", return_value=False):
            self.assertEqual(artifact_policy.mode(), "tracked")
            self.assertEqual(artifact_policy.content_path(), self.state / "content.sql")
            self.assertEqual(artifact_policy.render_root(), self.tmp)

    def test_instance_can_opt_into_local(self):
        self.write_json(artifact_policy.INSTANCE_CONFIG, {"artifact_mode": "local"})
        self.assertEqual(artifact_policy.mode(), "local")
        self.assertEqual(
            artifact_policy.content_path(), self.state / "local" / "content.sql"
        )
        self.assertEqual(
            artifact_policy.render_root(), self.state / "local" / "renders"
        )

    def test_tracked_source_policy_opts_out_only_when_tracked(self):
        self.write_json(artifact_policy.SOURCE_POLICY, {"artifact_mode": "local"})
        with mock.patch.object(artifact_policy, "_source_policy_is_tracked", return_value=False):
            self.assertEqual(artifact_policy.mode(), "tracked")
        with mock.patch.object(artifact_policy, "_source_policy_is_tracked", return_value=True):
            self.assertEqual(artifact_policy.mode(), "local")

    def test_invalid_explicit_mode_fails_closed(self):
        self.write_json(artifact_policy.INSTANCE_CONFIG, {"artifact_mode": "maybe"})
        with self.assertRaises(artifact_policy.ArtifactPolicyError):
            artifact_policy.mode()

    def test_set_mode_preserves_unrelated_instance_config(self):
        self.write_json(artifact_policy.INSTANCE_CONFIG, {"port": 8801, "harness": "codex"})
        artifact_policy.set_mode("local")
        payload = json.loads(artifact_policy.INSTANCE_CONFIG.read_text())
        self.assertEqual(payload["artifact_mode"], "local")
        self.assertEqual(payload["port"], 8801)
        self.assertEqual(payload["harness"], "codex")

    def test_path_command_exposes_active_map_db(self):
        self.write_json(artifact_policy.INSTANCE_CONFIG, {"artifact_mode": "local"})
        with mock.patch("builtins.print") as output:
            self.assertEqual(artifact_policy.main(["path", "map-db"]), 0)
        output.assert_called_once_with(self.state / "local" / "map" / "map.db")

    def test_localization_copies_durable_files_and_sqlite_once(self):
        self.write_json(artifact_policy.INSTANCE_CONFIG, {"artifact_mode": "local"})
        (self.state / "content.sql").write_text("content-v1")
        (self.state / "map_content.sql").write_text("map-v1")
        (self.state / "map.config.json").write_text("{}")
        db = sqlite3.connect(self.state / "map.db")
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('kept')")
        db.commit()
        db.close()

        copied = artifact_policy.prepare_local_state()
        self.assertEqual(len(copied), 4)
        self.assertEqual((self.state / "local" / "content.sql").read_text(), "content-v1")
        local_db = sqlite3.connect(self.state / "local" / "map" / "map.db")
        self.assertEqual(local_db.execute("SELECT value FROM marker").fetchone()[0], "kept")
        local_db.close()

        (self.state / "content.sql").write_text("content-v2")
        self.assertEqual(artifact_policy.prepare_local_state(), [])
        self.assertEqual((self.state / "local" / "content.sql").read_text(), "content-v1")


class RenderPathContainmentTest(unittest.TestCase):
    def test_managed_paths_stay_beneath_kind_root(self):
        root = Path("/tmp/render-root")
        self.assertEqual(
            flat._document_target(root, "specs_sc/a.md", "spec"),
            root / "specs_sc" / "a.md",
        )
        for unsafe in ("../escape.md", "/tmp/escape.md", "docs_sc/wrong.md"):
            with self.assertRaises(ValueError):
                flat._document_target(root, unsafe, "spec")


class SourceCleanlinessTest(unittest.TestCase):
    def test_source_tracks_no_instance_snapshot_or_flat_renders(self):
        tracked = subprocess.run(
            [
                "git", "-C", str(ROOT), "ls-files", "--",
                ".sc-state/content.sql", ".sc-state/map.config.json",
                ".sc-state/map_content.sql", "roadmap_sc.md",
                "docs_sc", "specs_sc", "skills_sc",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(tracked, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
