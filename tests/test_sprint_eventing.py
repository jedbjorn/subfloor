#!/usr/bin/env python3
"""Tests for sprint eventing (specs_sc/sprint-eventing.md): message kinds,
the watched_prs registry, the watcher daemon's diff/emit core, the /_sc/watches
API + `sc watch pr`, `sc mem message --kind`, and `sc run`'s headless
resolution order.

Stdlib `unittest`, matching the sibling suites. The daemon's GitHub seam is
injectable (`poll_once(con, fetch=...)`), so every transition is exercised
hermetically — no network, no gh. API tests stand up the real server.Handler
on an ephemeral port (the test_mem harness pattern).

Run:
    python3 tests/test_sprint_eventing.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
ADAPTERS = ENGINE / "adapters"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import mem  # noqa: E402
import pr_poller  # noqa: E402
import run  # noqa: E402
import server  # noqa: E402
import watch  # noqa: E402

TOKEN = "test-token-cafebabe"


def _baseline_node():
    """The fake immediate GitHub read behind registration (PENDING = the
    watch arms mid-flight, exactly like the real baseline path)."""
    return {"state": "OPEN", "headRefOid": "abc1234def",
            "reviews": {"totalCount": 0, "nodes": []},
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                              {"state": "PENDING"}}}]}}


def build_db(path: "Path | None" = None) -> sqlite3.Connection:
    """Fresh DB the way the engine ships it: schema.sql + every migration."""
    con = sqlite3.connect(path if path else ":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed_shells(con: sqlite3.Connection) -> None:
    con.executescript(
        "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);"
        "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
        f"VALUES (1, 'Planner', 'plan1', 'x', 1, '{TOKEN}'), (2, 'Dev', 'dev1', 'x', 1, NULL);")
    con.commit()


# ── schema: kind column + watched_prs ────────────────────────────────────────

class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        seed_shells(self.con)

    def tearDown(self):
        self.con.close()

    def test_kind_defaults_to_shell(self):
        self.con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) VALUES (1, 2, 'hi')")
        self.assertEqual(
            self.con.execute("SELECT kind FROM shell_messages").fetchone()["kind"], "shell")

    def test_kind_check_constraint(self):
        for ok in ("shell", "task", "result", "pr_event"):
            self.con.execute(
                "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind) "
                "VALUES (1, 2, 'b', ?)", (ok,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind) "
                "VALUES (1, 2, 'b', 'gossip')")

    def test_watched_prs_shape_and_unique(self):
        self.con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id) VALUES ('o/r', 7, 1)")
        with self.assertRaises(sqlite3.IntegrityError):   # (repo, pr, shell) unique
            self.con.execute(
                "INSERT INTO watched_prs (repo, pr_number, shell_id) VALUES ('o/r', 7, 1)")
        # same PR, different subscriber — allowed
        self.con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id) VALUES ('o/r', 7, 2)")
        with self.assertRaises(sqlite3.IntegrityError):   # FK on shell_id
            self.con.execute(
                "INSERT INTO watched_prs (repo, pr_number, shell_id) VALUES ('o/r', 7, 99)")
        idx = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_watched_prs_live'"
        ).fetchone()
        self.assertIsNotNone(idx, "live-watch partial index missing")

    def test_model_routes_runtime_table_is_migrated(self):
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(model_routes)")}
        self.assertTrue({"harness", "selector", "availability",
                         "high_effort_supported", "stale"}.issubset(cols))

    def test_sprint_orchestration_seed_matches_source_asset(self):
        asset = (ENGINE / "assets" / "skills" / "sprint_orchestration" /
                 "SKILL.md").read_text().split("---", 2)[2].strip()
        body = self.con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        self.assertEqual(body, asset)

    def test_sprint_orchestration_requires_plan_billing_by_default(self):
        body = self.con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        self.assertIn("two routine routing questions to the FnB", body)
        self.assertIn("Plan billing by default; observe, never mutate auth", body)
        self.assertIn('if [ -n "${CODEX_API_KEY+x}" ]', body)
        self.assertIn('key=s.get("apiKeySource")', body)
        self.assertIn("billing=api source=CODEX_API_KEY", body)
        self.assertIn("Billing approval required: provider=<openai|anthropic>", body)
        self.assertIn("Default scope = one launch", body)
        self.assertIn("billing-exception: provider=<openai|anthropic>", body)
        self.assertIn("current environment unchanged", body)
        self.assertIn("No matching, unexpired approval -> do not launch", body)
        self.assertIn("Do not claim Extra Usage was\nvalidated", body)
        self.assertNotIn("env -u", body)
        self.assertNotIn("flexible-credit balance", body)

    def test_billing_approval_reseed_replaces_scrub_and_is_idempotent(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            con.executescript(migration.read_text())
            if migration.name == "0078_reseed_sprint_plan_billing.sql":
                break

        before = con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        self.assertIn("env -u CODEX_API_KEY", before)
        self.assertIn("env -u ANTHROPIC_API_KEY", before)

        reseed = (MIGRATIONS / "0079_reseed_sprint_billing_approval.sql").read_text()
        con.executescript(reseed)
        once = con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        con.executescript(reseed)
        twice = con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]

        self.assertIn("observe, never mutate auth", once)
        self.assertIn("Default scope = one launch", once)
        self.assertNotIn("env -u", once)
        self.assertEqual(twice, once)


# ── daemon core: diff_events (pure) ──────────────────────────────────────────

def snap(state="OPEN", sha="abc1234def", checks=None, reviews=0, review_state=None):
    return {"state": state, "sha": sha, "checks": checks,
            "reviews": reviews, "review_state": review_state}


class DiffEventsTest(unittest.TestCase):
    def diff(self, prev, cur):
        return watch.diff_events(prev, cur, "o/r", 7)

    def test_baseline_pending_is_silent(self):
        events, terminal = self.diff(None, snap(checks="PENDING"))
        self.assertEqual(events, [])
        self.assertFalse(terminal)

    def test_baseline_already_green_emits(self):
        events, terminal = self.diff(None, snap(checks="SUCCESS"))
        self.assertEqual(len(events), 1)
        self.assertIn("checks green", events[0])
        self.assertFalse(terminal)

    def test_pending_to_green(self):
        events, _ = self.diff(snap(checks="PENDING"), snap(checks="SUCCESS"))
        self.assertEqual(len(events), 1)
        self.assertIn("checks green", events[0])

    def test_pending_to_red(self):
        events, _ = self.diff(snap(checks="PENDING"), snap(checks="FAILURE"))
        self.assertIn("checks red", events[0])

    def test_steady_green_is_silent(self):
        events, _ = self.diff(snap(checks="SUCCESS"), snap(checks="SUCCESS"))
        self.assertEqual(events, [])

    def test_new_push_going_green_is_a_fresh_transition(self):
        events, _ = self.diff(snap(checks="SUCCESS", sha="aaa1111"),
                              snap(checks="SUCCESS", sha="bbb2222"))
        self.assertEqual(len(events), 1)
        self.assertIn("checks green", events[0])

    def test_review_submitted(self):
        events, _ = self.diff(snap(reviews=0),
                              snap(reviews=1, review_state="CHANGES_REQUESTED"))
        self.assertEqual(len(events), 1)
        self.assertIn("review submitted (CHANGES_REQUESTED)", events[0])

    def test_baseline_never_replays_review_history(self):
        events, _ = self.diff(None, snap(reviews=3, checks="PENDING"))
        self.assertEqual(events, [])

    def test_merged_is_terminal(self):
        events, terminal = self.diff(snap(), snap(state="MERGED"))
        self.assertTrue(terminal)
        self.assertTrue(any("merged" in e for e in events))

    def test_green_and_merged_in_one_poll_both_emit(self):
        events, terminal = self.diff(snap(checks="PENDING"),
                                     snap(state="MERGED", checks="SUCCESS"))
        self.assertEqual(len(events), 2)
        self.assertTrue(terminal)

    def test_closed_without_merge(self):
        events, terminal = self.diff(snap(), snap(state="CLOSED"))
        self.assertTrue(terminal)
        self.assertIn("closed without merge", events[0])

    def test_baseline_already_merged_still_wakes(self):
        events, terminal = self.diff(None, snap(state="MERGED"))
        self.assertTrue(terminal)
        self.assertTrue(any("merged" in e for e in events))

    # ── merge with checks still running (#375) ──────────────────────────────

    def test_merged_with_pending_checks_retains_the_watch(self):
        events, terminal = self.diff(snap(checks="PENDING"),
                                     snap(state="MERGED", checks="PENDING"))
        self.assertFalse(terminal)
        self.assertEqual(len(events), 1)
        self.assertIn("merged", events[0])
        self.assertIn("watch retained", events[0])
        self.assertNotIn("watch retired", events[0])

    def test_baseline_merged_with_pending_checks_wakes_and_retains(self):
        events, terminal = self.diff(None, snap(state="MERGED", checks="PENDING"))
        self.assertFalse(terminal)
        self.assertTrue(any("watch retained" in e for e in events))

    def test_retained_watch_is_silent_until_checks_conclude(self):
        events, terminal = self.diff(snap(state="MERGED", checks="PENDING"),
                                     snap(state="MERGED", checks="PENDING"))
        self.assertEqual(events, [])
        self.assertFalse(terminal)

    def test_post_merge_green_emits_and_retires(self):
        events, terminal = self.diff(snap(state="MERGED", checks="PENDING"),
                                     snap(state="MERGED", checks="SUCCESS"))
        self.assertTrue(terminal)
        self.assertEqual(len(events), 1)          # no duplicate merged event
        self.assertIn("checks green", events[0])
        self.assertIn("watch retired", events[0])

    def test_post_merge_red_emits_and_retires(self):
        events, terminal = self.diff(snap(state="MERGED", checks="PENDING"),
                                     snap(state="MERGED", checks="FAILURE"))
        self.assertTrue(terminal)
        self.assertIn("checks red", events[0])
        self.assertIn("watch retired", events[0])

    def test_merged_with_no_checks_retires_immediately(self):
        events, terminal = self.diff(snap(), snap(state="MERGED", checks=None))
        self.assertTrue(terminal)
        self.assertTrue(any("watch retired" in e for e in events))

    def test_closed_with_pending_checks_retires_immediately(self):
        events, terminal = self.diff(snap(checks="PENDING"),
                                     snap(state="CLOSED", checks="PENDING"))
        self.assertTrue(terminal)
        self.assertIn("closed without merge", events[0])

    def test_event_bodies_are_one_line_with_repo_pr_sha(self):
        events, _ = self.diff(snap(checks="PENDING"), snap(checks="SUCCESS"))
        self.assertNotIn("\n", events[0])
        self.assertIn("o/r#7", events[0])
        self.assertIn("abc1234", events[0])


# ── daemon heartbeat (#359): beat upsert + liveness rendering ────────────────
# The poll-cycle coverage that used to ride this file's PollOnceTest moved to
# tests/test_pr_poller.py with the polling cutover (spec #20 task #85): the
# retired daemon verb's exit-clean contract is covered there too.

class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()

    def tearDown(self):
        self.con.close()

    def beat_rows(self):
        return self.con.execute(
            "SELECT name, beat_at, interval_s FROM daemon_heartbeats").fetchall()

    def test_beat_inserts_then_updates_one_row(self):
        watch.beat(self.con, 75)
        rows = self.beat_rows()
        self.assertEqual([(r["name"], r["interval_s"]) for r in rows], [("watch", 75)])
        watch.beat(self.con, 30)   # re-beat upserts — never a second row
        rows = self.beat_rows()
        self.assertEqual([(r["name"], r["interval_s"]) for r in rows], [("watch", 30)])


class BuildQueryTest(unittest.TestCase):
    def test_build_query_batches_and_aliases(self):
        q = watch.build_query([("o/r", 1), ("other/repo", 22)])
        self.assertIn('r0: repository(owner: "o", name: "r")', q)
        self.assertIn('r1: repository(owner: "other", name: "repo")', q)
        self.assertIn("pullRequest(number: 22)", q)


class DaemonLineTest(unittest.TestCase):
    def test_never_run(self):
        self.assertIn("never run", watch.daemon_line(None))
        self.assertIn("NOT being polled", watch.daemon_line(None))

    def test_live(self):
        line = watch.daemon_line(
            {"beat_at": "2026-07-15 09:00:00", "interval_s": 75, "age_s": 14, "stale": False})
        self.assertIn("live", line)
        self.assertIn("14s ago", line)
        self.assertNotIn("NOT being polled", line)

    def test_stale(self):
        line = watch.daemon_line(
            {"beat_at": "2026-07-14 20:22:19", "interval_s": 75, "age_s": 14520, "stale": True})
        self.assertIn("STALE", line)
        self.assertIn("4h ago", line)
        self.assertIn("NOT being polled", line)


# ── API: /_sc/watches + message kinds, over the real server ─────────────────

class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        con = build_db(cls.db)
        seed_shells(con)
        con.close()
        server.DB_PATH = cls.db  # db() reads the module global at call time
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        for mod in (mem, watch):
            mod.SC_API_BASE = f"http://127.0.0.1:{cls.port}"
            mod.SC_API_TOKEN = TOKEN
        # Registration takes an immediate GitHub baseline (spec #20 task #85)
        # — hermetic suite, so the gh seam is faked at the module global the
        # server resolves at call time.
        cls._real_fetch = pr_poller.gh_fetch
        pr_poller.gh_fetch = lambda q: pr_poller.GhResult(
            data={"r0": {"pullRequest": _baseline_node()}})

    @classmethod
    def tearDownClass(cls):
        pr_poller.gh_fetch = cls._real_fetch
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_register_defaults_to_the_token_shell(self):
        self.assertEqual(watch.main(["pr", "own/repo", "11"]), 0)
        rows = self.q("SELECT shell_id, closed_at FROM watched_prs "
                      "WHERE repo='own/repo' AND pr_number=11")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shell_id"], 1)   # the token shell (plan1)

    def test_register_for_another_shell(self):
        watch.main(["pr", "own/repo", "12", "--shell", "dev1"])
        rows = self.q("SELECT shell_id FROM watched_prs WHERE pr_number=12")
        self.assertEqual(rows[0]["shell_id"], 2)

    def test_register_unknown_shell_dies(self):
        with self.assertRaises(SystemExit):
            watch.main(["pr", "own/repo", "13", "--shell", "nobody"])

    def test_register_bad_repo_dies(self):
        with self.assertRaises(SystemExit):
            watch.main(["pr", "not-a-repo", "14"])

    def test_duplicate_live_watch_is_idempotent(self):
        watch.main(["pr", "own/repo", "15"])
        watch.main(["pr", "own/repo", "15"])
        self.assertEqual(len(self.q(
            "SELECT 1 FROM watched_prs WHERE pr_number=15")), 1)

    def test_retired_watch_reregisters_as_new_row_keeping_history(self):
        watch.main(["pr", "own/repo", "16"])
        con = sqlite3.connect(self.db)
        con.execute("UPDATE watched_prs SET closed_at=datetime('now'), "
                    "last_seen='{\"state\":\"MERGED\"}' WHERE pr_number=16")
        con.commit()
        con.close()
        watch.main(["pr", "own/repo", "16"])
        rows = self.q("SELECT closed_at, last_seen FROM watched_prs "
                      "WHERE pr_number=16 ORDER BY watch_id")
        # The 0080 cutover: the closed row is RETAINED (with its fingerprint)
        # and re-registration inserts a fresh row holding the new baseline.
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["closed_at"])
        self.assertEqual(rows[0]["last_seen"], '{"state":"MERGED"}')
        self.assertIsNone(rows[1]["closed_at"])
        self.assertIn('"checks": "PENDING"', rows[1]["last_seen"])

    def test_failed_baseline_creates_no_watch(self):
        real = pr_poller.gh_fetch
        pr_poller.gh_fetch = lambda q: pr_poller.GhResult(error="connect timeout")
        try:
            with self.assertRaises(SystemExit):   # _api dies on the 502
                watch.main(["pr", "own/repo", "18"])
        finally:
            pr_poller.gh_fetch = real
        self.assertEqual(self.q("SELECT 1 FROM watched_prs WHERE pr_number=18"), [])

    def test_registration_stores_the_baseline(self):
        watch.main(["pr", "own/repo", "19"])
        row = self.q("SELECT last_seen FROM watched_prs WHERE pr_number=19")[0]
        self.assertIn('"state": "OPEN"', row["last_seen"])
        self.assertIn('"sha": "abc1234def"', row["last_seen"])

    def test_list_shows_live_watches(self):
        watch.main(["pr", "own/repo", "17"])
        self.assertEqual(watch.main(["list"]), 0)

    def test_send_with_kind_lands_typed(self):
        mem.main(["message", "send", "dev1", "build unit 2", "--kind", "task"])
        rows = self.q("SELECT kind, body FROM shell_messages WHERE body='build unit 2'")
        self.assertEqual(rows[0]["kind"], "task")

    def test_send_default_kind_is_shell(self):
        mem.main(["message", "send", "dev1", "plain mail"])
        rows = self.q("SELECT kind FROM shell_messages WHERE body='plain mail'")
        self.assertEqual(rows[0]["kind"], "shell")

    def test_cli_refuses_pr_event_kind(self):
        with self.assertRaises(SystemExit):   # argparse: not in choices
            mem.main(["message", "send", "dev1", "forged", "--kind", "pr_event"])

    def test_messages_read_returns_kind(self):
        mem.main(["message", "send", "plan1", "report done", "--kind", "result"])
        data = mem._api("GET", "/_sc/mem/messages")
        kinds = {m["body"]: m.get("kind") for m in data["messages"]}
        self.assertEqual(kinds.get("report done"), "result")

    def test_server_rejects_unknown_kind(self):
        with self.assertRaises(SystemExit):   # _api dies on HTTP 400
            mem._api("POST", "/_sc/mem/messages",
                     {"to": "dev1", "body": "x", "kind": "gossip"})

    def test_watches_require_auth(self):
        saved = watch.SC_API_TOKEN
        watch.SC_API_TOKEN = "wrong-token"
        try:
            with self.assertRaises(SystemExit):
                watch.main(["list"])
        finally:
            watch.SC_API_TOKEN = saved


# ── headless boot: resolution order + argv shape ─────────────────────────────

class HeadlessTest(unittest.TestCase):
    FDEF = {"default_harness": "claude",
            "models": {"claude": "opus", "codex": "gpt-5.4", "opencode": "zai/glm"}}

    def adapter(self, name):
        return json.loads((ADAPTERS / name / "adapter.json").read_text())

    def test_explicit_model_flag_wins(self):
        self.assertEqual(run.resolve_headless_model("sonnet", self.FDEF, "claude"), "sonnet")

    def test_flavor_default_fills_when_no_flag(self):
        self.assertEqual(run.resolve_headless_model(None, self.FDEF, "codex"), "gpt-5.4")

    def test_no_flag_no_flavor_lets_the_harness_pick(self):
        self.assertIsNone(run.resolve_headless_model(None, None, "claude"))
        self.assertIsNone(run.resolve_headless_model(
            None, {"default_harness": None, "models": {}}, "claude"))

    def test_claude_headless_argv(self):
        cmd = run.headless_command(
            self.adapter("claude"), "do the task", "opus", effort="high")
        self.assertEqual(cmd, ["claude", "--model", "opus", "--effort", "high",
                               "-p", "do the task"])

    def test_codex_headless_argv(self):
        cmd = run.headless_command(self.adapter("codex"), "do it", "gpt-5.4",
                                   ["--dangerously-bypass-approvals-and-sandbox"],
                                   effort="high")
        self.assertEqual(cmd, ["codex", "exec", "-m", "gpt-5.4",
                               "-c", 'model_reasoning_effort="high"',
                               "--dangerously-bypass-approvals-and-sandbox", "do it"])

    def test_opencode_headless_argv(self):
        cmd = run.headless_command(self.adapter("opencode"), "p", "zai/glm")
        self.assertEqual(cmd, ["opencode", "run", "-m", "zai/glm", "p"])

    def test_no_model_omits_the_flag(self):
        cmd = run.headless_command(self.adapter("claude"), "p")
        self.assertEqual(cmd, ["claude", "-p", "p"])

    def test_vibe_has_no_headless_seam(self):
        self.assertIsNone(run.headless_command(self.adapter("vibe"), "p"))

    def test_kimi_headless_argv(self):
        cmd = run.headless_command(
            self.adapter("kimi"), "do it", "kimi-code/k3", effort="high")
        self.assertEqual(cmd, ["kimi", "-m", "kimi-code/k3", "-p", "do it"])
        self.assertEqual(
            run.headless_effort_env(self.adapter("kimi"), "high"),
            {"KIMI_MODEL_THINKING_EFFORT": "high"})

    def test_unroutable_model_fails_instead_of_silent_drop(self):
        adapter = self.adapter("kimi")
        del adapter["headless"]["model_flag"]
        with self.assertRaisesRegex(ValueError, "cannot apply requested model"):
            run.headless_command(adapter, "p", "kimi-code/k3", effort="high")

    def test_harness_without_effort_control_fails_high_effort(self):
        with self.assertRaisesRegex(ValueError, "cannot apply effort"):
            run.headless_command(
                self.adapter("opencode"), "p", "zai/glm", effort="high")

    def test_prompt_is_the_final_positional(self):
        cmd = run.headless_command(self.adapter("claude"), "trailing prompt", "opus",
                                   ["--dangerously-skip-permissions"])
        self.assertEqual(cmd[-1], "trailing prompt")


# ── API: daemon liveness on /_sc/watches (#359) ──────────────────────────────
# Own DB + server so heartbeat state can't leak into ApiTest. Method names are
# alphabetically ordered on purpose — the class DB is shared and the sequence
# never → live → stale → dropped-table walks one heartbeat row through its
# states.

class DaemonLivenessApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        con = build_db(cls.db)
        seed_shells(con)
        con.close()
        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        watch.SC_API_BASE = f"http://127.0.0.1:{cls.port}"
        watch.SC_API_TOKEN = TOKEN
        cls._real_fetch = pr_poller.gh_fetch
        pr_poller.gh_fetch = lambda q: pr_poller.GhResult(
            data={"r0": {"pullRequest": _baseline_node()}})

    @classmethod
    def tearDownClass(cls):
        pr_poller.gh_fetch = cls._real_fetch
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def x(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def test_a_no_heartbeat_reports_never_run(self):
        r = watch._api("GET", "/_sc/watches")
        self.assertIsNone(r["daemon"])

    def test_b_fresh_beat_reports_live(self):
        self.x("INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
               "VALUES ('watch', datetime('now'), 75)")
        d = watch._api("GET", "/_sc/watches")["daemon"]
        self.assertFalse(d["stale"])
        self.assertEqual(d["interval_s"], 75)
        self.assertLessEqual(d["age_s"], 5)

    def test_c_old_beat_reports_stale(self):
        self.x("UPDATE daemon_heartbeats SET beat_at=datetime('now', '-1 hour') "
               "WHERE name='watch'")
        d = watch._api("GET", "/_sc/watches")["daemon"]
        self.assertTrue(d["stale"])
        self.assertGreaterEqual(d["age_s"], 3600)

    def test_d_registration_response_carries_daemon(self):
        r = watch._api("POST", "/_sc/watches", {"repo": "own/repo", "pr_number": 44})
        self.assertTrue(r["daemon"]["stale"])   # still the -1h beat from test_c
        r = watch._api("POST", "/_sc/watches", {"repo": "own/repo", "pr_number": 44})
        self.assertTrue(r.get("existing"))      # idempotent path carries it too
        self.assertTrue(r["daemon"]["stale"])

    def test_e_pre_migration_db_degrades_to_never_run(self):
        self.x("DROP TABLE daemon_heartbeats")
        r = watch._api("GET", "/_sc/watches")
        self.assertIsNone(r["daemon"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
