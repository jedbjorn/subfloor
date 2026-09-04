"""Diagnostics for render-check source divergence."""
from __future__ import annotations

import hashlib
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_check  # noqa: I001


SCHEMA = """
CREATE TABLE roadmap (
    feature_id INTEGER PRIMARY KEY,
    title TEXT,
    roadmap_status TEXT
);
CREATE TABLE documents (
    document_id INTEGER PRIMARY KEY,
    feature_id INTEGER,
    kind TEXT,
    seq INTEGER,
    title TEXT,
    body TEXT,
    render_path TEXT,
    frozen INTEGER
);
"""


def source(body: str, status: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO roadmap VALUES (7, 'Gateway', ?)", (status,))
    con.execute(
        "INSERT INTO documents VALUES "
        "(11, 7, 'spec', 1, 'Gateway deployment', ?, "
        "'specs_sc/gateway-deployment.md', 0)",
        (body,),
    )
    return con


class RenderCheckDocumentDiagnosticsTest(unittest.TestCase):
    def test_drift_names_the_exact_source_fields_from_snapshot_and_live_db(self):
        snapshot = source("snapshot body", "next")
        live = source("live body", "in_progress")
        self.addCleanup(snapshot.close)
        self.addCleanup(live.close)

        diagnostic = render_check._document_source_diagnostics(
            snapshot,
            live,
            ["specs_sc/gateway-deployment.md", "roadmap_sc.md"],
        )

        self.assertEqual(
            diagnostic.count("source rows for specs_sc/gateway-deployment.md"),
            1,
        )
        self.assertNotIn("source rows for roadmap_sc.md", diagnostic)
        self.assertIn('"roadmap_status": "next"', diagnostic)
        self.assertIn('"roadmap_status": "in_progress"', diagnostic)
        self.assertIn(
            '"body_sha256": "'
            + hashlib.sha256(b"snapshot body").hexdigest()
            + '"',
            diagnostic,
        )
        self.assertIn(
            '"body_sha256": "' + hashlib.sha256(b"live body").hexdigest() + '"',
            diagnostic,
        )

    def test_matching_source_rows_identify_a_stale_mirror(self):
        snapshot = source("same body", "next")
        live = source("same body", "next")
        self.addCleanup(snapshot.close)
        self.addCleanup(live.close)

        diagnostic = render_check._document_source_diagnostics(
            snapshot,
            live,
            ["specs_sc/gateway-deployment.md"],
        )

        self.assertIn(
            "snapshot and live DB rows match; the active mirror is stale",
            diagnostic,
        )
        self.assertNotIn("body_sha256", diagnostic)


class RelocatedSnapshotReportTest(unittest.TestCase):
    """Relocation put content.sql outside the checkout.

    The drift branch names that path; an unguarded relative_to raised there,
    so the crash replaced the very report the operator needed.
    """

    def test_drift_report_names_a_snapshot_outside_the_checkout(self):
        relocated = Path("/var/lib/subfloor/instances/deadbeef/content.sql")
        mirror = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, mirror, True)
        (mirror / "roadmap_sc.md").write_text("stale")

        with (
            mock.patch.object(render_check, "CONTENT", relocated),
            mock.patch.object(render_check, "ACTIVE_ROOT", mirror),
            mock.patch.object(render_check, "LIVE_DB", mirror / "absent.db"),
            mock.patch.object(render_check, "_build_tracked_db"),
            mock.patch.object(render_check.flat, "render_visibility"),
            mock.patch.object(render_check.artifact_policy, "prepare_local_state"),
            mock.patch.object(sys, "stderr", io.StringIO()) as err,
        ):
            self.assertEqual(render_check.main(), 1)

        report = err.getvalue()
        self.assertIn(str(relocated), report)
        self.assertIn("roadmap_sc.md", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
