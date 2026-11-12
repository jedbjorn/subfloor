"""Runs ledger for subagents (Feature #96, spec #273): upgrade, registration, limits.

Acceptance criteria covered here: 5 (concurrency), 6 (nesting), 7 (run time),
8 (the Reviewer rule), plus the upgrade of a populated ledger and the facts,
result and listing routes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job
import migrate
import runs
from test_job import TOKEN, build_db
from test_runs import ApiFixture

MIGRATION = "0285_run_agents_and_time_limit.sql"
COMMIT = "a" * 40


def agent_payload(key: str, **changes) -> dict:
    agent = {
        "harness": "codex",
        "requested_model": "model-x",
        "requested_effort": None,
        "effective_effort": "default",
        "adapter_version": "codex-cli 0.0.0",
        "brief_bytes": 120,
        **changes.pop("agent", {}),
    }
    return {
        "registration_key": key,
        "kind": "agent",
        "argv": ["codex", "exec", "-m", "model-x", "-"],
        "commit": COMMIT,
        "agent": agent,
        **changes,
    }


def _clauses(sql: str, keyword: str) -> list[str]:
    """Every `<keyword>(...)` clause of a CREATE TABLE, whitespace removed."""
    text = "".join(sql.split()).lower()
    found, start = [], 0
    while (at := text.find(keyword + "(", start)) != -1:
        depth, end = 0, at + len(keyword)
        while True:
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
            if depth == 0:
                break
        found.append(text[at:end])
        start = end
    return sorted(found)


def table_shape(con: sqlite3.Connection, table: str) -> dict:
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    return {
        "columns": {
            row[1]: (row[2].upper(), row[3], row[4], row[5])
            for row in con.execute(f"PRAGMA table_xinfo({table})")
        },
        "checks": _clauses(sql, "check"),
        "uniques": _clauses(sql, "unique"),
        "autoincrement": "autoincrement" in sql.lower(),
        "foreign_keys": sorted(
            (row[2], row[3], row[4]) for row in con.execute(f"PRAGMA foreign_key_list({table})")
        ),
    }


class UpgradeTest(unittest.TestCase):
    """The rebuild carries a populated ledger through unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prior.db"
        con = sqlite3.connect(self.path)
        con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            if migration.name >= MIGRATION:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(filename) VALUES(?)", (migration.name,)
            )
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
            "VALUES (1, 'Dev One', 'dev1', 'x', 1, ?)", (TOKEN,))
        con.commit()
        con.row_factory = sqlite3.Row
        self.con = con
        self.addCleanup(con.close)

    def _populate(self) -> None:
        rows = [
            (3, "job", "done", 0, '{"exit_code":0,"finished_at":"2026-10-04T10:00:00Z","state":"done"}', 0),
            (7, "probe", "running", None, None, 0),
            (9, "devkit", "failed", 2, '{"exit_code":2,"finished_at":"2026-10-04T10:00:00Z","state":"failed"}', 1),
            (12, "native", "lost", None, '{"finished_at":"2026-10-04T10:00:00Z","state":"lost"}', 0),
        ]
        for rid, kind, state, code, terminal, foreground in rows:
            self.con.execute(
                'INSERT INTO runs(run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",'
                "state,exit_code,terminal_json,evidence_path,foreground,attached_pr,evidence_pruned) "
                "VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, f"key-{rid}", kind, f"label {rid}", '["sh","-c","true"]', "/repo",
                 COMMIT, state, code, terminal, f"/engine/run/runs/{rid}/log", foreground,
                 41 if rid == 3 else None, 1 if rid == 9 else 0),
            )
        self.con.execute(
            "INSERT INTO run_attachment_events(run_id,actor_shell_id,target_json,outcome) "
            "VALUES(3,1,'{\"pr\":41}','attached')"
        )
        # The newest id was allocated but its row is not the table maximum any more.
        self.con.execute("UPDATE sqlite_sequence SET seq=20 WHERE name='runs'")
        self.con.commit()

    def _snapshot(self) -> list[dict]:
        return [dict(row) for row in self.con.execute("SELECT * FROM runs ORDER BY run_id")]

    def test_populated_ledger_upgrades_with_rows_ids_and_guards_intact(self):
        self._populate()
        before = self._snapshot()
        # Still first in line; later migrations wait behind it.
        self.assertEqual(migrate.pending(self.con)[0].name, MIGRATION)

        migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)

        after = self._snapshot()
        self.assertEqual(
            [{k: v for k, v in row.items() if k != "timeout_seconds"} for row in after], before
        )
        self.assertTrue(all(row["timeout_seconds"] is None for row in after))
        self.assertEqual(
            self.con.execute("SELECT seq FROM sqlite_sequence WHERE name='runs'").fetchone()[0], 20
        )
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            self.con.execute("SELECT run_id FROM run_attachment_events").fetchone()[0], 3
        )
        self.assertEqual(
            {row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='runs'")},
            {"runs_identity_immutable", "runs_path_immutable", "runs_terminal_immutable"},
        )
        self.assertEqual(
            {row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='runs' "
                "AND name LIKE 'idx_%'")},
            {"idx_runs_owner", "idx_runs_pending", "idx_runs_message", "idx_runs_attached_pr",
             "idx_runs_attached_unit", "idx_runs_live_agents"},
        )
        for statement in (
            "UPDATE runs SET cwd='/elsewhere' WHERE run_id=7",
            "UPDATE runs SET timeout_seconds=5 WHERE run_id=7",
            "UPDATE runs SET evidence_path='/x' WHERE run_id=7",
            "UPDATE runs SET state='done' WHERE run_id=3",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.con.execute(statement)
        self.con.rollback()
        # The next registration still allocates above the carried high-water mark.
        store = runs.RunStore(self.con, Path(self.temp.name) / "engine")
        row = store.register(1, {"registration_key": "next", "argv": ["true"], "cwd": "/repo"})
        self.assertEqual(row["run_id"], 21)
        self.assertNotIn("agent", row)
        self.assertEqual(store.get(3)["state"], "done")

    def test_rebuilt_table_equals_the_prior_one_apart_from_the_intended_changes(self):
        before = table_shape(self.con, "runs")
        migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)
        after = table_shape(self.con, "runs")

        old_kind = "check(kindin('job','devkit','probe','native'))"
        new_kind = "check(kindin('job','devkit','probe','native','agent'))"
        limit = "check(timeout_secondsisnullortimeout_seconds>0)"
        self.assertIn(old_kind, before["checks"])
        self.assertEqual(
            after["checks"],
            sorted([c for c in before["checks"] if c != old_kind] + [new_kind, limit]))
        self.assertEqual(
            after["columns"],
            {**before["columns"], "timeout_seconds": ("INTEGER", 0, None, 0)})
        self.assertEqual(list(after["columns"])[:-1], list(before["columns"]))
        for key in ("uniques", "autoincrement", "foreign_keys"):
            self.assertEqual(after[key], before[key], key)
        # The two that a careless rebuild loses without any other test noticing.
        self.assertEqual(after["columns"]["wake_state"][2], "'none'")
        self.assertEqual(after["columns"]["state"][2], "'registered'")
        self.assertIn(
            "check(foregroundin(0,1)and(foreground=0orkind='devkit'))", after["checks"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,"
                "evidence_path,foreground) VALUES(1,'fg','job','l','[]','/repo','/e',1)")
        self.con.rollback()
        self.con.execute(
            "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,evidence_path) "
            "VALUES(1,'d','job','l','[]','/repo','/e')")
        self.assertEqual(
            tuple(self.con.execute(
                "SELECT state,wake_state,foreground,evidence_pruned FROM runs").fetchone()),
            ("registered", "none", 0, 0))
        self.con.rollback()

    def test_empty_ledger_upgrades_and_accepts_every_kind(self):
        migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)
        self.assertNotIn(MIGRATION, [path.name for path in migrate.pending(self.con)])
        for kind in ("job", "devkit", "probe", "native", "agent"):
            self.con.execute(
                "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,evidence_path) "
                "VALUES(1,?,?,'l','[]','/repo','/e')", (kind, kind))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,evidence_path) "
                "VALUES(1,'bad','other','l','[]','/repo','/e')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE runs SET timeout_seconds=0")
        self.con.rollback()

    def test_a_failed_application_leaves_the_prior_ledger_in_place(self):
        self._populate()
        before = self._snapshot()
        # A second column of the same name makes the first statement fail.
        self.con.execute("ALTER TABLE runs ADD COLUMN timeout_seconds INTEGER")
        self.con.commit()
        with self.assertRaises(sqlite3.OperationalError):
            migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)
        self.assertEqual(
            [{k: v for k, v in row.items() if k != "timeout_seconds"} for row in self._snapshot()],
            before,
        )
        # Still first in line; later migrations wait behind it.
        self.assertEqual(migrate.pending(self.con)[0].name, MIGRATION)


class DiscardAndUsageUpgradeTest(unittest.TestCase):
    """Migration 0286 on a populated database: rows carried, new columns empty."""

    LATER = "0286_agent_worktree_discard_and_usage_run.sql"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        con = sqlite3.connect(Path(self.temp.name) / "prior.db")
        con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            if migration.name >= self.LATER:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(filename) VALUES(?)", (migration.name,))
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
            "VALUES (1, 'Dev One', 'dev1', 'x', 1, ?)", (TOKEN,))
        con.execute(
            "INSERT INTO runs(run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"
            "evidence_path,timeout_seconds) VALUES(5,1,'k','agent','a','[\"codex\"]',"
            "'/repo/.sc-worktrees/agent-5','/e/5/log',7200)")
        con.execute(
            "INSERT INTO run_agents(run_id,harness,requested_model,effective_effort,session_ref,"
            "worktree,base_commit,branch,result_bytes,usage_json,worktree_removed_at) "
            "VALUES(5,'codex','model-x','default','thread-1','/repo/.sc-worktrees/agent-5',?,"
            "'feat/x',12,'{\"input_tokens\":3}','2026-11-12T10:00:00Z')", (COMMIT,))
        con.execute(
            "INSERT INTO session_token_usage(usage_id,shell_id,harness,harness_session_ref,model,"
            "input_tokens,output_tokens,status) VALUES(9,1,'codex','rollout-1','model-x',10,2,'ok')")
        con.execute(
            "INSERT INTO session_token_usage(usage_id,harness,harness_session_ref,status) "
            "VALUES(10,'claude','transcript-1','no_usage')")
        con.commit()
        con.row_factory = sqlite3.Row
        self.con = con
        self.addCleanup(con.close)

    def _rows(self, table: str, key: str) -> list[dict]:
        return [dict(row) for row in self.con.execute(f"SELECT * FROM {table} ORDER BY {key}")]

    def test_populated_tables_upgrade_with_rows_intact_and_new_columns_empty(self):
        agents = self._rows("run_agents", "run_id")
        usage = self._rows("session_token_usage", "usage_id")
        self.assertEqual(migrate.pending(self.con)[0].name, self.LATER)

        migrate.apply(self.con, ENGINE / "migrations" / self.LATER)

        self.assertNotIn(self.LATER, [path.name for path in migrate.pending(self.con)])
        self.assertEqual(
            self._rows("run_agents", "run_id"),
            [{**row, "worktree_discarded_at": None} for row in agents])
        self.assertEqual(
            self._rows("session_token_usage", "usage_id"),
            [{**row, "run_id": None} for row in usage])
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIn("idx_stu_run", {row[1] for row in self.con.execute(
            "PRAGMA index_list(session_token_usage)")})
        # The request stays immutable; the new columns accept their writes.
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE run_agents SET worktree='/elsewhere' WHERE run_id=5")
        self.con.rollback()
        self.con.execute(
            "UPDATE run_agents SET worktree_discarded_at='2026-11-12T11:00:00Z' WHERE run_id=5")
        self.con.execute("UPDATE session_token_usage SET run_id=5 WHERE usage_id=9")
        self.con.commit()
        store = runs.RunStore(self.con, Path(self.temp.name) / "engine")
        self.assertEqual(
            store.get(5)["agent"]["worktree_discarded_at"], "2026-11-12T11:00:00Z")
        self.assertEqual(store.get(5)["agent"]["usage_json"], {"input_tokens": 3})


class AgentLedgerCase(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        build_db(str(self.db_path))
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        for sid, name in ((2, "pln1"), (3, "rev1"), (4, "rev2")):
            self.con.execute(
                "INSERT INTO shells(shell_id,display_name,shortname,system_prompt,user_id,api_key) "
                "VALUES(?,?,?,'x',1,?)", (sid, name, name, f"{name}-token"))
        self.con.commit()
        self.store = runs.RunStore(self.con, self.root / "engine")
        self.start_api()

    def api_error(self, method, path, body=None) -> tuple[int, str]:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            job._api(method, path, body)
        return caught.exception.code, json.loads(caught.exception.read())["error"]

    def finish(self, row, owner=1):
        return self.store.terminal(
            row["run_id"], owner,
            {"state": "done", "exit_code": 0, "finished_at": "2026-11-12T10:00:00Z"},
        )


class RegistrationTest(AgentLedgerCase):
    def test_agent_registers_with_its_facts_worktree_and_default_time_limit(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/x"}))
        self.assertEqual(row["kind"], "agent")
        self.assertEqual(row["state"], "registered")
        self.assertEqual(row["timeout_seconds"], 7200)
        self.assertEqual(row["label"], "codex:model-x")
        worktree = str(self.root / ".sc-worktrees" / f"agent-{row['run_id']}")
        self.assertEqual(row["cwd"], worktree)
        self.assertEqual(
            row["agent"],
            {
                "harness": "codex", "requested_model": "model-x", "provider_model": None,
                "requested_effort": None, "effective_effort": "default", "session_ref": None,
                "worktree": worktree, "base_commit": COMMIT, "branch": "feat/x",
                "continues_run_id": None, "result_bytes": None, "usage_json": None,
                "adapter_version": "codex-cli 0.0.0", "worktree_removed_at": None,
                "worktree_discarded_at": None,
            },
        )
        # Registration is idempotent on its key and refuses a changed request.
        self.assertEqual(
            job._api("POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/x"})), row
        )
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/y"}))
        self.assertEqual(code, 400)
        self.assertIn("different input", error)

    def test_agent_projection_is_on_the_listing_and_absent_from_other_kinds(self):
        agent = job._api("POST", "/_sc/runs", agent_payload("one"))
        command = self.register(registration_key="plain", timeout_seconds=90)
        listed = {row["run_id"]: row for row in job._api("GET", "/_sc/runs")["runs"]}
        self.assertEqual(listed[agent["run_id"]]["agent"]["harness"], "codex")
        self.assertNotIn("agent", listed[command["run_id"]])
        self.assertEqual(listed[command["run_id"]]["timeout_seconds"], 90)
        only = job._api("GET", "/_sc/runs?kind=agent")
        self.assertEqual([row["run_id"] for row in only["runs"]], [agent["run_id"]])
        # The operator listing reads through the same projection.
        fleet = {row["run_id"]: row for row in self.store.list(None)}
        self.assertEqual(fleet[agent["run_id"]]["agent"]["requested_model"], "model-x")

    def test_malformed_agent_requests_register_nothing(self):
        cases = {
            "an agent object": {"agent": None},
            "unknown agent field": {"agent": {"owner_shell_id": 4}},
            "engine-assigned worktree": {"cwd": str(self.root)},
            "base commit": {"commit": "main"},
            "harness must be": {"agent": {"harness": ""}},
            "requested_model must be": {"agent": {"requested_model": None}},
            "brief_bytes must be": {"agent": {"brief_bytes": 0}},
            "timeout_seconds must be": {"timeout_seconds": 0},
        }
        for fragment, changes in cases.items():
            with self.subTest(fragment=fragment):
                missing = "agent" in changes and changes["agent"] is None
                payload = agent_payload("bad", **({} if missing else dict(changes)))
                if missing:
                    payload["agent"] = None
                code, error = self.api_error("POST", "/_sc/runs", payload)
                self.assertEqual(code, 400)
                self.assertIn(fragment, error)
        code, error = self.api_error(
            "POST", "/_sc/runs",
            {"registration_key": "x", "argv": ["true"], "cwd": str(self.root),
             "agent": {"harness": "codex"}})
        self.assertIn("require an agent run", error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM run_agents").fetchone()[0], 0)

    def test_harness_model_and_effort_shape_is_refused_with_400(self):
        cases = [
            ("harness must be a shipped harness name", {"harness": "Codex Exec"}),
            ("harness must be a shipped harness name", {"harness": "-x"}),
            ("requested_model must not start with a dash", {"requested_model": "-m"}),
            ("requested_model must not contain quotes", {"requested_model": 'a"b'}),
            ("requested_model must not contain quotes", {"requested_model": "a\nb"}),
            ("requested_effort must not contain quotes",
             {"requested_effort": 'low"\nsandbox_mode="read-only'}),
            ("effective_effort must not start with a dash", {"effective_effort": "-c"}),
        ]
        for fragment, agent in cases:
            with self.subTest(agent=agent):
                code, error = self.api_error("POST", "/_sc/runs", agent_payload("bad", agent=agent))
                self.assertEqual(code, 400)
                self.assertIn(fragment, error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_admin_is_refused_with_403(self):
        self.con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=1")
        self.con.commit()
        code, error = self.api_error("POST", "/_sc/runs", agent_payload("admin"))
        self.assertEqual(code, 403)
        self.assertIn(runs.ADMIN_CONFUSION, error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        # Admin's command jobs are untouched, and every other flavor registers.
        self.assertEqual(self.register(registration_key="cmd")["kind"], "job")
        for flavor in ("dev", "planner", "reviewer", None):
            with self.subTest(flavor=flavor):
                self.con.execute("UPDATE shells SET flavor=? WHERE shell_id=2", (flavor,))
                self.con.commit()
                row = self.store.register(2, agent_payload(f"as-{flavor}"))
                self.assertEqual(row["state"], "registered")
                self.finish(row, owner=2)

    def test_a_shell_named_like_a_run_worktree_refuses_every_launch(self):
        # Such a shell can only predate the reserved prefix. Its worktree is
        # the path a run of that number would be given, so no launch proceeds.
        self.con.execute("UPDATE shells SET shortname='Agent-12' WHERE shell_id=4")
        self.con.commit()
        code, error = self.api_error("POST", "/_sc/runs", agent_payload("clash"))
        self.assertEqual(code, 403)
        self.assertIn(runs.RESERVED_NAME_CONFUSION, error)
        self.assertIn("'Agent-12'", error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        # A name that is not agent-<number> names no run's worktree; a deleted shell holds none.
        for change in ("shortname='agent-smith'", "shortname='agent-12', is_deleted=1"):
            with self.subTest(change=change):
                self.con.execute(f"UPDATE shells SET {change} WHERE shell_id=4")
                self.con.commit()
                row = job._api("POST", "/_sc/runs", agent_payload(change))
                self.assertEqual(row["state"], "registered")
                self.finish(row)

    def test_branch_and_the_rest_of_the_request_are_immutable(self):
        row = self.store.register(1, agent_payload("fixed", agent={"branch": "feat/x"}))
        for column, value in (("branch", "feat/y"), ("harness", "claude"),
                              ("requested_model", "m"), ("requested_effort", "high"),
                              ("worktree", "/x"), ("base_commit", "b" * 40),
                              ("continues_run_id", 1), ("run_id", 99)):
            with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                self.con.execute(
                    f"UPDATE run_agents SET {column}=? WHERE run_id=?", (value, row["run_id"]))
            self.con.rollback()
        self.assertEqual(self.store.get(row["run_id"])["agent"]["branch"], "feat/x")

    def test_sc_job_start_records_its_time_limit(self):
        timed = self.start_job("exit 0", "--timeout", "45")
        self.assertEqual(timed["timeout_seconds"], 45)
        self.finished(timed)
        plain = self.start_job("exit 0")
        self.assertIsNone(plain["timeout_seconds"])
        self.finished(plain)

    def test_a_worktree_that_cannot_be_created_ends_the_run_failed_and_frees_its_slot(self):
        import job_agent

        rows = [self.store.register(1, agent_payload(f"k{i}")) for i in range(3)]
        doomed = rows[-1]
        Path(doomed["cwd"]).mkdir(parents=True)  # left behind by an earlier run of this number
        made = job_agent.create_worktree_or_fail(
            doomed["run_id"], COMMIT, root=self.root,
            submit=lambda payload: self.store.terminal(doomed["run_id"], 1, payload))
        self.assertIsNone(made)
        final = self.store.get(doomed["run_id"])
        self.assertEqual((final["state"], final["exit_code"]), ("failed", 127))
        self.assertIn("already exists, left by an earlier run",
                      json.loads(final["terminal_json"])["spawn_error"])
        self.assertEqual(final["wake_state"], "pending")
        self.assertEqual(self.store.register(1, agent_payload("next"))["state"], "registered")

    def test_brief_size_is_capped_at_64_kib(self):
        row = job._api("POST", "/_sc/runs", agent_payload("fits", agent={"brief_bytes": 65536}))
        self.assertEqual(row["state"], "registered")
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("over", agent={"brief_bytes": 65537}))
        self.assertEqual(code, 400)
        self.assertIn("65537 bytes; the limit is 65536 bytes", error)


class ConcurrencyLimitTest(AgentLedgerCase):
    """AC 5: the fourth for one shell and the seventh on the install are refused."""

    def test_parallel_registrations_never_exceed_three_per_shell(self):
        def attempt(index):
            try:
                return job._api("POST", "/_sc/runs", agent_payload(f"parallel-{index}"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())["error"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        held = sorted(o["run_id"] for o in outcomes if isinstance(o, dict))
        refused = [o for o in outcomes if isinstance(o, tuple)]
        self.assertEqual(len(held), 3)
        self.assertEqual(len(refused), 5)
        for code, error in refused:
            self.assertEqual(code, 429)
            self.assertIn("live subagent limit for one shell reached: 3 of 3 live", error)
            self.assertIn(f"(runs {', '.join(map(str, held))})", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 3)

    def test_a_finished_run_frees_its_slot_and_other_kinds_take_none(self):
        rows = [self.store.register(1, agent_payload(f"k{i}")) for i in range(3)]
        for index in range(4):
            self.register(registration_key=f"command-{index}")
        with self.assertRaises(runs.RunLimitError):
            self.store.register(1, agent_payload("fourth"))
        self.finish(rows[0])
        self.assertEqual(self.store.register(1, agent_payload("fourth"))["state"], "registered")

    def test_a_run_with_no_live_process_holds_no_slot(self):
        rows = [self.store.register(1, agent_payload(f"k{i}")) for i in range(3)]
        with self.assertRaises(runs.RunLimitError) as caught:
            self.store.register(1, agent_payload("fourth"))
        error = str(caught.exception)
        # `kill` refuses a run whose process is gone, so it is offered only for a running one.
        self.assertIn("`sc job kill <id>` stops one that is running", error)
        self.assertNotIn("or stop one with", error)

        # Reconciliation gave up on a registered row: it stays unfinished for
        # operator recovery, is skipped by every later reconcile, and has no process.
        self.con.execute(
            "UPDATE runs SET wake_state='blocked', last_error='reconciliation blocked' "
            "WHERE run_id=?", (rows[0]["run_id"],))
        self.con.commit()
        self.store.reconcile()
        self.assertEqual(self.store.get(rows[0]["run_id"])["state"], "registered")
        fourth = self.store.register(1, agent_payload("fourth"))
        self.assertEqual(fourth["state"], "registered")

        # A running row whose supervisor belonged to another boot is proven gone.
        self.con.execute(
            "UPDATE runs SET state='running', supervisor_pid=1, supervisor_start_ticks=1, "
            "boot_id='an-earlier-boot', wake_state='blocked' WHERE run_id=?",
            (rows[1]["run_id"],))
        self.con.commit()
        self.assertEqual(self.store.register(1, agent_payload("fifth"))["state"], "registered")
        self.con.execute(
            "UPDATE runs SET wake_state='none' WHERE run_id=?", (rows[1]["run_id"],))
        self.con.commit()
        with self.assertRaises(runs.RunLimitError) as caught:
            self.store.register(1, agent_payload("sixth"))
        held = [rows[2]["run_id"], fourth["run_id"], fourth["run_id"] + 1]
        self.assertIn(f"3 of 3 live (runs {', '.join(map(str, held))})", str(caught.exception))
        # The same two rows hold no install slot either.
        for index in range(3):
            self.store.register(2, agent_payload(f"other-{index}"))
        with self.assertRaises(runs.RunLimitError) as caught:
            self.store.register(3, agent_payload("seventh"))
        self.assertIn("6 of 6 live", str(caught.exception))
        for gone in rows[:2]:
            self.assertNotIn(f"{gone['run_id']} (", str(caught.exception))

    def test_a_live_supervisor_keeps_its_slot(self):
        import os as _os

        rows = [self.store.register(1, agent_payload(f"k{i}")) for i in range(3)]
        me = _os.getpid()
        ticks = int(Path(f"/proc/{me}/stat").read_text().rsplit(")", 1)[1].split()[19])
        self.con.execute(
            "UPDATE runs SET state='running', supervisor_pid=?, supervisor_start_ticks=?, "
            "boot_id=? WHERE run_id=?", (me, ticks, runs.boot_id(), rows[0]["run_id"]))
        self.con.commit()
        self.assertEqual(self.store._agents_without_a_process(), frozenset())
        with self.assertRaises(runs.RunLimitError):
            self.store.register(1, agent_payload("fourth"))

    def test_admission_is_decided_inside_the_write_transaction(self):
        # The interleaving of two registrations is not forced here; the parallel
        # test above exercises it. This pins the structure that makes it safe:
        # the count and the insert share one write transaction, and the /proc
        # probe happens before it.
        seen = []
        admission = self.store._agent_admission
        probe = self.store._agents_without_a_process

        def admit(owner, gone=frozenset()):
            seen.append(("admission", self.con.in_transaction))
            return admission(owner, gone)

        def probed():
            seen.append(("probe", self.con.in_transaction))
            return probe()

        self.store._agent_admission = admit
        self.store._agents_without_a_process = probed
        row = self.store.register(1, agent_payload("one"))
        self.assertEqual(seen, [("probe", False), ("admission", True)])
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM run_agents WHERE run_id=?", (row["run_id"],)).fetchone()[0], 1)

    def test_the_seventh_on_the_install_is_refused_naming_every_holder(self):
        held = []
        for owner in (1, 2):
            held += [self.store.register(owner, agent_payload(f"k{i}")) for i in range(3)]
        with self.assertRaises(runs.RunLimitError) as caught:
            self.store.register(3, agent_payload("seventh"))
        error = str(caught.exception)
        self.assertIn("live subagent limit for this install reached: 6 of 6 live", error)
        for row, name in zip(held, ["dev1"] * 3 + ["pln1"] * 3):
            self.assertIn(f"{row['run_id']} ({name})", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 6)
        # A command job is not a subagent: the install limit does not touch it.
        self.assertEqual(
            self.store.register(
                3, {"registration_key": "cmd", "argv": ["true"], "cwd": str(self.root)}
            )["kind"], "job")


class NestingTest(AgentLedgerCase):
    """AC 6: a registration that carries a subagent's run id is refused."""

    def test_a_subagent_cannot_launch_a_subagent_but_can_start_a_command_job(self):
        parent = job._api("POST", "/_sc/runs", agent_payload("parent"))
        code, error = self.api_error(
            "POST", "/_sc/runs",
            agent_payload("child", agent={"launched_from_run": str(parent["run_id"])}))
        self.assertEqual(code, 403)
        self.assertIn(runs.NESTING_CONFUSION, error)
        self.assertIn(f"subagent run {parent['run_id']}", error)
        self.assertIn("nesting depth 1", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 1)
        # The same environment may still start a command job; it belongs to the shell.
        command = self.register(registration_key="from-inside")
        self.assertEqual(command["kind"], "job")
        self.assertEqual(command["owner_shell_id"], parent["owner_shell_id"])


class RunTimeTest(AgentLedgerCase):
    """AC 7: default 7,200 recorded, more than 28,800 refused, the limit enforced."""

    def test_default_ceiling_and_refusal(self):
        self.assertEqual(self.store.register(1, agent_payload("default"))["timeout_seconds"], 7200)
        self.assertEqual(
            self.store.register(1, agent_payload("ceiling", timeout_seconds=28800))[
                "timeout_seconds"], 28800)
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("over", timeout_seconds=28801))
        self.assertEqual(code, 400)
        self.assertIn("limited to 28800 seconds (8 hours); 28801 was requested", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 2)

    def test_a_run_past_its_limit_ends_timeout_and_wakes(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True, text=True, check=True).stdout.strip()

        git("init", "-q", "-b", "main")
        (self.root / "tracked.txt").write_text("one\n")
        git("add", "tracked.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@subfloor.local",
            "commit", "-q", "-m", "first")
        base = git("rev-parse", "HEAD")
        row = job._api(
            "POST", "/_sc/runs", agent_payload("short", timeout_seconds=1, commit=base))
        jobdir = Path(row["evidence_path"]).parent
        jobdir.mkdir(parents=True)
        (jobdir / "log").touch()
        job.write_meta(jobdir, {
            "run_id": row["run_id"], "job_id": str(row["run_id"]), "kind": "agent",
            "label": row["label"], "cmd": ["sleep", "60"], "cwd": row["cwd"],
            "timeout": row["timeout_seconds"], "started_at": job._now(),
            "log": str(jobdir / "log"),
            "agent": {
                "harness": "codex", "stdin": False, "brief_index": None,
                "run_dir_indexes": [], "env": {}, "env_unset": [],
                "result": {"source": "stdout_json", "field": "result"},
                "session": {"source": "pinned"}, "session_ref": None,
                "base_commit": base, "branch": None, "continues_run_id": None,
            },
        })
        job.supervise(jobdir)
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual(final["state"], "timeout")
        self.assertEqual(final["wake_state"], "pending")
        self.assertIsNotNone(final["message_id"])
        body = self.con.execute(
            "SELECT body FROM shell_messages WHERE message_id=?", (final["inbox_message_id"],)
        ).fetchone()[0]
        self.assertIn(f"Run {row['run_id']} (agent: codex:model-x) timeout", body)
        # A finished run holds no slot.
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM runs WHERE kind='agent' AND state IN ('registered','running')"
            ).fetchone()[0], 0)


class ReviewerRuleTest(AgentLedgerCase):
    """AC 8: a Reviewer of an armed or paused run may not delegate."""

    def _move(self, run_id: int, lifecycle: str) -> None:
        outcome = "done" if lifecycle == "completed" else None
        self.con.execute(
            "UPDATE sprints SET lifecycle=?, terminal_outcome=? WHERE sprint_id=?",
            (lifecycle, outcome, run_id))
        self.con.commit()

    def _run(self, lifecycle: str, members: dict[int, str]) -> int:
        """One run walked through the ledger's own lifecycle guards."""
        run_id = self.con.execute(
            "INSERT INTO sprints(feature_id,originating_planner_shell_id) VALUES(1,2)"
        ).lastrowid
        for shell_id, role in members.items():
            self.con.execute(
                "INSERT INTO sprint_participants(sprint_id,shell_id,role,harness) VALUES(?,?,?,'codex')",
                (run_id, shell_id, role))
        reviewer = next(shell for shell, role in members.items() if role == "reviewer")
        self.con.execute(
            "UPDATE sprints SET merge_grant_enabled=1, conformance_reviewer_shell_id=?, "
            "conformance_owner_generation=1 WHERE sprint_id=?", (reviewer, run_id))
        self.con.commit()
        self._move(run_id, "armed")
        if lifecycle != "armed":
            self._move(run_id, lifecycle)
        return run_id

    def test_reviewer_is_refused_while_developer_and_planner_register(self):
        self._run("completed", {4: "reviewer"})
        armed = self._run("armed", {1: "developer", 2: "planner", 3: "reviewer"})
        with self.assertRaises(PermissionError) as caught:
            self.store.register(3, agent_payload("review"))
        error = str(caught.exception)
        self.assertIn(runs.REVIEWER_CONFUSION, error)
        self.assertIn(f"{armed} (armed)", error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.store.register(1, agent_payload("dev"))["state"], "registered")
        self.assertEqual(self.store.register(2, agent_payload("plan"))["state"], "registered")
        # A Reviewer whose run has ended, and one in none, register.
        self.assertEqual(self.store.register(4, agent_payload("free"))["state"], "registered")
        # The refused Reviewer may still start a command job.
        self.assertEqual(
            self.store.register(
                3, {"registration_key": "cmd", "argv": ["true"], "cwd": str(self.root)}
            )["kind"], "job")

    def test_paused_holds_the_rule_and_the_route_answers_403(self):
        run_id = self._run("paused", {1: "reviewer"})
        code, error = self.api_error("POST", "/_sc/runs", agent_payload("review"))
        self.assertEqual(code, 403)
        self.assertIn(runs.REVIEWER_CONFUSION, error)
        self.assertIn("(paused)", error)
        self._move(run_id, "armed")
        self.assertEqual(self.api_error("POST", "/_sc/runs", agent_payload("review"))[0], 403)
        self._move(run_id, "completed")
        self.assertEqual(
            job._api("POST", "/_sc/runs", agent_payload("review"))["state"], "registered")


class FactsAndResultTest(AgentLedgerCase):
    def test_facts_are_written_once_and_only_by_the_owner(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one"))
        path = f"/_sc/runs/{row['run_id']}/facts"
        facts = {"session_ref": "thread-1", "provider_model": "model-x-served", "result_bytes": 12}
        self.assertEqual(job._api("POST", path, facts)["agent"]["session_ref"], "thread-1")
        again = job._api("POST", path, facts)["agent"]
        self.assertEqual(
            (again["provider_model"], again["result_bytes"]), ("model-x-served", 12))
        code, error = self.api_error("POST", path, {"session_ref": "thread-2"})
        self.assertEqual(code, 409)
        self.assertIn("conflicting agent fact: session_ref", error)
        for bad in ({}, {"harness": "claude"}, {"result_bytes": -1}, {"session_ref": ""},
                    {"worktree_removed_at": "yesterday"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.api_error("POST", path, bad)[0], 400)
        with self.assertRaises(PermissionError):
            self.store.facts(row["run_id"], 2, {"result_bytes": 1})
        command = self.register(registration_key="plain")
        code, error = self.api_error(
            "POST", f"/_sc/runs/{command['run_id']}/facts", {"result_bytes": 1})
        self.assertIn("require an agent run", error)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE run_agents SET harness='claude'")
        self.con.rollback()

    def test_agent_facts_are_carried_by_the_instance_snapshot(self):
        import snapshot

        row = self.store.register(1, agent_payload("kept", timeout_seconds=600))
        self.store.facts(row["run_id"], 1, {"session_ref": "thread-1"})
        tables = snapshot.PER_INSTANCE_TABLES
        self.assertLess(tables.index("runs"), tables.index("run_agents"))
        script = "\n".join(
            line for table in ("runs", "run_agents") for line in snapshot.dump_table(self.con, table)
        )
        rebuilt_path = self.root / "rebuilt.db"
        build_db(str(rebuilt_path))
        rebuilt = sqlite3.connect(rebuilt_path)
        self.addCleanup(rebuilt.close)
        rebuilt.executescript(script)
        self.assertEqual(
            runs.RunStore(rebuilt, self.root / "engine").get(row["run_id"]),
            self.store.get(row["run_id"]))

    def test_result_is_read_from_the_registered_path_and_bounded(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one"))
        path = f"/_sc/runs/{row['run_id']}/result"
        empty = job._api("GET", path)
        self.assertEqual(
            (empty["available"], empty["text"], empty["truncated"], empty["result_bytes"]),
            (False, "", False, None))
        self.assertEqual(empty["agent"]["harness"], "codex")
        evidence = Path(row["evidence_path"]).parent
        evidence.mkdir(parents=True)
        (evidence / "result.md").write_text("the final message\n")
        whole = job._api("GET", path)
        self.assertEqual(
            (whole["available"], whole["text"], whole["truncated"], whole["result_bytes"]),
            (True, "the final message\n", False, 18))
        (evidence / "result.md").write_text("x" * (runs.AGENT_RESULT_MAX + 500))
        long = job._api("GET", path)
        self.assertEqual(len(long["text"]), 262144)
        self.assertTrue(long["truncated"])
        self.assertEqual(long["result_bytes"], runs.AGENT_RESULT_MAX + 500)
        with self.assertRaises(PermissionError):
            self.store.result(row["run_id"], 2)
        command = self.register(registration_key="plain")
        code, error = self.api_error("GET", f"/_sc/runs/{command['run_id']}/result")
        self.assertEqual(code, 400)
        self.assertIn("only an agent run has a result", error)


if __name__ == "__main__":
    unittest.main()
