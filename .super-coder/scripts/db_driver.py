#!/usr/bin/env python3
"""Thin database accessor for super-coder — SQLite only.

A fork needs only python3 + sqlite3, which the install already requires.
Every script and route opens the engine DB through this one seam, so the
connection PRAGMAs live in a single place.
"""
from __future__ import annotations

import sqlite3


def connect(path):
    """Open the engine SQLite DB at `path` with the standard PRAGMAs."""
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
