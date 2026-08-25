#!/usr/bin/env python3
"""Trip every protected-effect sentinel if a refused dispatch reaches here."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

event_dir = Path(os.environ["DSH_EFFECT_DIR"])
event_dir.mkdir(parents=True, exist_ok=True)
credential = Path(os.environ["DSH_ADMIN_CREDENTIAL"])
credential.read_text()
(event_dir / "credential_discovery").write_text("attempted\n")

with sqlite3.connect(event_dir / "effect.db") as con:
    con.execute("CREATE TABLE effect(name TEXT NOT NULL)")
    con.execute("INSERT INTO effect VALUES ('db_write')")
(event_dir / "db_write").write_text("attempted\n")

try:
    urllib.request.urlopen(os.environ["DSH_EFFECT_API"], timeout=1).read()
except OSError:
    pass
(event_dir / "api_effect").write_text("attempted\n")

process_probe = (
    "from pathlib import Path; import sys; "
    + "Path(sys.argv[1]).write_text('attempted\\n')"
)
subprocess.run(
    [
        sys.executable,
        "-c",
        process_probe,
        str(event_dir / "process_start"),
    ],
    check=False,
)
for name in (
    "filesystem_write",
    "message_write",
    "wake_write",
    "denied_marker_write",
):
    (event_dir / name).write_text(json.dumps({"attempted": True}))
