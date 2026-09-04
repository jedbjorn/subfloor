#!/usr/bin/env python3
"""The durability PRAGMAs `db_driver.connect` must hand every caller.

WAL keeps the file intact on power loss, and NORMAL keeps a stalled host fsync
from holding the write lock through COMMIT.

Run:
    python3 -m unittest tests.test_db_driver_sync
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import db_driver  # noqa: E402


class ConnectPragmaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "engine.db"

    def test_connect_uses_wal_with_normal_synchronous(self) -> None:
        con = db_driver.connect(self.path)
        self.addCleanup(con.close)
        self.assertEqual(
            con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
        )
        self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
