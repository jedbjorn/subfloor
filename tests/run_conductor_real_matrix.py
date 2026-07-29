#!/usr/bin/env python3
"""Manual Step 8 gate: real weak-model OpenCode over the full directive matrix.

The model is real; the fork and role actors are isolated.  A temporary engine
DB receives every allowed issuer/kind pair plus one malformed payload.  A
loopback contract server executes real Conductor mechanics while recording
role-slot launch commands instead of starting worker models.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ROOT / "tests"))

import conductor_routes
import conductor_runtime as runtime
from test_conductor_runtime import (
    ConductorDirectiveMatrixTests,
    build_db,
)


class ContractHandler(BaseHTTPRequestHandler):
    def _dispatch(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        headers = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        status, response_headers, payload = conductor_routes.handle(
            self.command, self.path, headers, body
        )
        self.send_response(status)
        for key, value in response_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _dispatch
    do_POST = _dispatch

    def log_message(self, _format, *_args):
        return


def seed(con) -> None:
    con.executemany(
        "INSERT INTO shells "
        "(shell_id,display_name,shortname,flavor,system_prompt,api_key) "
        "VALUES (?,?,?,?,?,?)",
        (
            (1, "Conductor", "CON1", "conductor", "x", "con-token"),
            (2, "Planner", "PLN1", "planner", "x", "plan-token"),
            (3, "Developer", "DEV1", "dev", "x", "dev-token"),
            (4, "Reviewer", "REV1", "reviewer", "x", "rev-token"),
        ),
    )
    shell_ids = {"dev": 3, "reviewer": 4, "planner": 2, "system": None}
    for index, (issuer, kind, state, payload, linked_unit) in enumerate(
        ConductorDirectiveMatrixTests.CASES, start=1
    ):
        payload = dict(payload)
        sprint_id = 1000 + index
        unit_id = 2000 + index
        con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body,frozen) VALUES "
            "(?,'doc',?,'status: ACTIVE',0)",
            (sprint_id, f"SPRINT: matrix {issuer}:{kind}"),
        )
        con.execute(
            "INSERT INTO sprint_units "
            "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
            "reviewer_shell_id,state,branch,pr_number,review_head) "
            "VALUES (?,?,?, ?,3,4,?,'feat/u',7,?)",
            (
                unit_id,
                sprint_id,
                f"U{index}",
                f"{issuer}:{kind}",
                state,
                "abc" if kind == "merged" else None,
            ),
        )
        if kind == "close":
            payload["conformance_directive_id"] = -12
            con.execute(
                "INSERT INTO directives "
                "(directive_id,issuer_shell_id,issuer_flavor,kind,payload,target,"
                "sprint_doc_id,unit_id,status,executed_at) VALUES "
                "(-12,4,'reviewer','review-clean',?,'conductor',?,NULL,"
                "'executed',datetime('now'))",
                (
                    json.dumps(
                        {
                            "mode": "conformance",
                            "main_sha": payload["main_sha"],
                            "findings": [],
                        }
                    ),
                    sprint_id,
                ),
            )
        con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id,issuer_flavor,kind,payload,target,"
            "sprint_doc_id,unit_id) VALUES (?,?,?,?, 'conductor',?,?)",
            (
                shell_ids[issuer],
                issuer,
                kind,
                json.dumps(payload),
                sprint_id,
                unit_id if linked_unit else None,
            ),
        )
    sprint_id = 1999
    con.execute(
        "INSERT INTO documents "
        "(document_id,kind,title,body,frozen) VALUES "
        "(?,'doc','SPRINT: malformed','status: ACTIVE',0)",
        (sprint_id,),
    )
    con.execute(
        "INSERT INTO sprint_units "
        "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
        "reviewer_shell_id,state) VALUES "
        "(2999,?,'UX','malformed',3,4,'working')",
        (sprint_id,),
    )
    con.execute(
        "INSERT INTO directives "
        "(issuer_shell_id,issuer_flavor,kind,payload,target,"
        "sprint_doc_id,unit_id) VALUES "
        "(3,'dev','ready-for-review','{\"pr_number\":\"bad\"}',"
        "'conductor',?,2999)",
        (sprint_id,),
    )
    con.commit()


def main() -> int:
    if not shutil_which("opencode"):
        print("real matrix: OpenCode missing", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="sc_real_conductor_") as td:
        temp = Path(td)
        db_path = temp / "matrix.db"
        con = build_db(db_path)
        seed(con)
        shell = con.execute("SELECT * FROM shells WHERE shell_id=1").fetchone()
        (temp / "AGENTS.md").write_text(runtime.render_boot(con, shell))
        (temp / "opencode.json").write_text(
            json.dumps(
                {
                    "permission": {
                        "bash": "allow",
                        "external_directory": "allow",
                    },
                }
            )
        )
        scripts = temp / ".super-coder" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("conductor_contracts.py", "cli_entry.py"):
            shutil.copy2(ENGINE / "scripts" / name, scripts / name)
        (temp / "sc").write_text(
            "#!/bin/sh\n"
            'exec python3 "$(dirname "$0")/.super-coder/scripts/'
            'conductor_contracts.py" directives "$@"\n'
        )
        (temp / "sc").chmod(0o755)

        launches: list[list[str]] = []

        def record(command):
            launches.append(command)
            return 7000 + len(launches)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        env = {
            **os.environ,
            "SC_API_BASE": base,
            "SC_API_TOKEN": "con-token",
            "SC_NO_AUTOPRUNE": "1",
        }
        prompt = (
            "Follow AGENTS.md exactly. Drain every pending directive in "
            "ascending id order with sc directives act <id>. Continue after "
            "both executed and refused results. Exit only when the pending "
            "list is empty, then reply MATRIX_COMPLETE."
        )
        try:
            with (
                mock.patch.object(conductor_routes, "DB_PATH", db_path),
                mock.patch.object(runtime, "_default_launcher", record),
                mock.patch.object(runtime, "REPO_ROOT", temp),
            ):
                result = subprocess.run(
                    [
                        "opencode",
                        "run",
                        "-m",
                        runtime.DEFAULT_CONDUCTOR_MODEL,
                        prompt,
                    ],
                    cwd=temp,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        counts = dict(
            con.execute(
                "SELECT status,COUNT(*) n FROM directives GROUP BY status"
            ).fetchall()
        )
        pending = counts.get("pending", 0)
        refused = counts.get("refused", 0)
        executed = counts.get("executed", 0)
        trail = con.execute(
            "SELECT COUNT(*) FROM sentinel_events "
            "WHERE event_kind IN ('conductor-executed','conductor-refused')"
        ).fetchone()[0]
        con.close()
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        print(
            json.dumps(
                {
                    "returncode": result.returncode,
                    "executed": executed,
                    "refused": refused,
                    "pending": pending,
                    "trail": trail,
                    "recorded_role_launches": len(launches),
                },
                sort_keys=True,
            )
        )
        return (
            0
            if (
                result.returncode == 0
                and executed == len(runtime.TRANSITIONS) + 1
                and refused == 1
                and pending == 0
                and trail == len(runtime.TRANSITIONS) + 1
            )
            else 1
        )


def shutil_which(binary: str) -> str | None:
    from shutil import which

    return which(binary)


if __name__ == "__main__":
    raise SystemExit(main())
