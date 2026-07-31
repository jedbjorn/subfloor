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
from unittest import mock

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


def seed_sprint_doc(con: sqlite3.Connection, doc_id: int = 100,
                    units: int = 1) -> None:
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body, frozen) "
        "VALUES (?, 'doc', 'SPRINT: T', '# SPRINT: T\nstatus: ACTIVE\n', 0)",
        (doc_id,))
    # `units` is what makes it live (H-1) — the `status:` line is prose.
    for i in range(units):
        con.execute(
            "INSERT INTO sprint_units (sprint_doc_id, seq, unit_title) "
            "VALUES (?, ?, ?)", (doc_id, f"U{i + 1}", f"unit {i + 1}"))
    con.execute(
        "INSERT INTO sprints "
        "(sprint_doc_id,state,legacy,planner_shell_id) "
        "VALUES (?,?,1,1)",
        (doc_id, "active" if units else "declared"),
    )
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

    def test_slot_skill_seed_matches_each_source_asset(self):
        for name in (
            "sprint_pln",
            "sprint_dev",
            "sprint_rev",
            "sprint_cond",
            "sprint_onboarding",
        ):
            with self.subTest(name=name):
                asset = (
                    ENGINE / "assets" / "skills" / name / "SKILL.md"
                ).read_text().split("---", 2)[2].strip()
                row = self.con.execute(
                    "SELECT content,is_deleted FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertEqual(row["is_deleted"], 0)
                self.assertIn(f"# {name}", asset)

    def test_legacy_sprint_role_skills_are_retired(self):
        names = (
            "plan_sprint",
            "dev_sprint",
            "rev_sprint",
            "sprint_review",
            "sprint_orchestration",
            "sprint_orchestration_recover",
            "sprint_orchestration_close",
        )
        rows = self.con.execute(
            f"SELECT name,is_deleted FROM skills WHERE name IN "
            f"({','.join('?' for _ in names)})",
            names,
        ).fetchall()
        self.assertTrue(all(row["is_deleted"] for row in rows))
        self.assertTrue(
            {row["name"] for row in rows}.issubset(set(names))
        )

    def test_shell_templates_grant_only_the_slot_skill_for_each_role(self):
        templates = ENGINE / "templates" / "shells"
        planner = json.loads((templates / "planner.json").read_text())["skills"]
        dev = json.loads((templates / "dev.json").read_text())["skills"]
        reviewer = json.loads((templates / "reviewer.json").read_text())["skills"]

        conductor = json.loads((templates / "conductor.json").read_text())["skills"]

        self.assertIn("sprint_pln", planner)
        self.assertIn("sprint_onboarding", planner)
        self.assertIn("sprint_dev", dev)
        self.assertIn("sprint_rev", reviewer)
        self.assertEqual(conductor, ["sprint_cond"])
        legacy = {
            "plan_sprint",
            "dev_sprint",
            "rev_sprint",
            "sprint_review",
            "sprint_orchestration",
            "sprint_orchestration_recover",
            "sprint_orchestration_close",
        }
        self.assertTrue(legacy.isdisjoint(planner + dev + reviewer))

    def test_sprint_onboarding_is_seeded_and_granted_only_to_planner(self):
        rows = self.con.execute(
            "SELECT fs.flavor FROM flavor_skills fs "
            "JOIN skills s ON s.skill_id=fs.skill_id "
            "WHERE s.name='sprint_onboarding' ORDER BY fs.flavor"
        ).fetchall()
        self.assertEqual([row["flavor"] for row in rows], ["planner"])
        text = (
            ENGINE / "assets/skills/sprint_onboarding/SKILL.md"
        ).read_text()
        for phrase in (
            "explanatory skill",
            "browser never performs a second activation step",
            "Cancel Sprint",
            "Do not declare, arm, cancel",
        ):
            self.assertIn(phrase, text)

    def test_role_skills_route_only_through_the_conductor(self):
        def body(name):
            return (ENGINE / "assets" / "skills" / name / "SKILL.md"
                    ).read_text().split("---", 2)[2]

        for name in ("sprint_pln", "sprint_dev", "sprint_rev"):
            with self.subTest(name=name):
                text = body(name)
                self.assertIn("--target conductor", text)
                self.assertIn("mem message send", text)
                self.assertIn('"$SC_SPRINT_RESULT_TARGET"', text)
                self.assertIn("--kind result", text)
                self.assertNotIn("watch inbox", text)


# ── daemon core: diff_events (pure) ──────────────────────────────────────────

def snap(state="OPEN", sha="abc1234def", checks=None, reviews=0, review_state=None):
    return {"state": state, "sha": sha, "checks": checks,
            "reviews": reviews, "review_state": review_state}


class WatchSentinelIdentityTest(unittest.TestCase):
    """A poller observation about a watch says which sprint/unit structurally."""

    def setUp(self):
        self.con = build_db()
        seed_shells(self.con)
        seed_sprint_doc(self.con)
        self.unit = self.con.execute(
            "INSERT INTO sprint_units (sprint_doc_id, seq, unit_title) "
            "VALUES (100,'U3','unit')").lastrowid
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _watch(self, pr, unit_id, doc_id=100):
        wid = self.con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id, "
            "sprint_doc_id, unit_id) VALUES ('o/r',?,1,?,?)",
            (pr, doc_id, unit_id)).lastrowid
        self.con.commit()
        return wid

    def _event_row(self, wid):
        return self.con.execute(
            "SELECT sprint_doc_id, unit_id, evidence FROM sentinel_events "
            "WHERE json_extract(evidence,'$.watch_id')=? "
            "ORDER BY event_id DESC LIMIT 1", (wid,)).fetchone()

    def test_an_observation_on_a_linked_watch_names_the_unit(self):
        wid = self._watch(41, self.unit)
        pr_poller._sentinel_observation(
            self.con, severity="critical",
            reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        row = self._event_row(wid)
        self.assertEqual(row["sprint_doc_id"], 100)
        self.assertEqual(row["unit_id"], self.unit)

    def test_an_unlinked_watch_still_scopes_the_sprint(self):
        """Partial structure beats none: the sprint is known even when the
        unit is not, and claiming a unit here would be a guess."""
        wid = self._watch(42, None)
        pr_poller._sentinel_observation(
            self.con, severity="critical",
            reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        row = self._event_row(wid)
        self.assertEqual(row["sprint_doc_id"], 100)
        self.assertIsNone(row["unit_id"])

    def test_repeated_failures_remain_distinct_observations(self):
        wid = self._watch(43, self.unit)
        for _ in range(3):
            pr_poller._sentinel_observation(
                self.con, severity="critical",
                reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        rows = self.con.execute(
            "SELECT evidence FROM sentinel_events "
            "WHERE json_extract(evidence,'$.watch_id')=?",
            (wid,)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(
            json.loads(row["evidence"])["severity"] == "critical"
            for row in rows))


class DiffEventsTest(unittest.TestCase):
    def diff(self, prev, cur):
        return watch.diff_events(prev, cur, "o/r", 7)

    def test_the_unit_rides_the_event_header_when_the_watch_names_one(self):
        """H-13: the structured ref made visible. The watch knows the unit, so
        the row the planner reads says which unit it is about instead of
        leaving every reader to infer it from the PR number."""
        events, _ = pr_poller.transitions(
            snap(checks="PENDING"), snap(checks="SUCCESS"), "o/r", 7, "U3")
        self.assertEqual(len(events), 1)
        self.assertIn("o/r#7 unit=U3:", events[0]["body"])

    def test_an_unlinked_watch_makes_no_claim_in_the_header(self):
        """Omitted, not `unit=None`. A header that names a unit is a claim,
        and an unlinked watch is not making one — the regex fallback is for
        exactly this traffic."""
        events, _ = pr_poller.transitions(
            snap(checks="PENDING"), snap(checks="SUCCESS"), "o/r", 7)
        self.assertIn("o/r#7:", events[0]["body"])
        self.assertNotIn("unit=", events[0]["body"])

    def test_every_event_kind_carries_the_unit(self):
        """One transition kind carrying the ref while the others drop it is
        the drift shape: the merge event is the one close reads, and it is
        built on a different branch from the checks event."""
        cases = {
            "checks": (snap(checks="PENDING"), snap(checks="SUCCESS")),
            "review": (snap(reviews=0), snap(reviews=1,
                                             review_state="APPROVED")),
            "merged": (snap(), snap(state="MERGED", checks="SUCCESS")),
            "closed": (snap(), snap(state="CLOSED")),
        }
        for kind, (prev, cur) in cases.items():
            with self.subTest(kind=kind):
                events, _ = pr_poller.transitions(prev, cur, "o/r", 7, "U3")
                self.assertTrue(events, f"{kind} emitted nothing to check")
                for e in events:
                    self.assertIn("unit=U3", e["body"])

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


# ── Generic message API, over the real server ────────────────────────────────

class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        con = build_db(cls.db)
        seed_shells(con)
        seed_sprint_doc(con)
        con.close()
        server.DB_PATH = cls.db  # db() reads the module global at call time
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.port}"
        mem.SC_API_TOKEN = TOKEN
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

    def test_send_with_kind_lands_typed(self):
        mem.main(["message", "send", "dev1", "build unit 2", "--kind", "task"])
        rows = self.q("SELECT kind, body FROM shell_messages WHERE body='build unit 2'")
        self.assertEqual(rows[0]["kind"], "task")

    def test_send_default_kind_is_shell(self):
        mem.main(["message", "send", "dev1", "plain mail"])
        rows = self.q("SELECT kind FROM shell_messages WHERE body='plain mail'")
        self.assertEqual(rows[0]["kind"], "shell")

    def test_messages_read_returns_kind(self):
        mem.main(["message", "send", "plan1", "report done", "--kind", "result"])
        data = mem._api("GET", "/_sc/mem/messages")
        kinds = {m["body"]: m.get("kind") for m in data["messages"]}
        self.assertEqual(kinds.get("report done"), "result")

    def test_server_rejects_unknown_kind(self):
        with self.assertRaises(SystemExit):   # _api dies on HTTP 400
            mem._api("POST", "/_sc/mem/messages",
                     {"to": "dev1", "body": "x", "kind": "gossip"})

    def test_server_rejects_removed_message_correlation_fields(self):
        before = self.q("SELECT COUNT(*) AS n FROM shell_messages")[0]["n"]
        with self.assertRaises(SystemExit):
            mem._api(
                "POST",
                "/_sc/mem/messages",
                {
                    "to": "dev1",
                    "body": "must not land",
                    "kind": "result",
                    "sprint_doc_id": 100,
                    "sprint_assignment_id": 1,
                },
            )
        after = self.q("SELECT COUNT(*) AS n FROM shell_messages")[0]["n"]
        self.assertEqual(before, after)
        self.assertEqual(
            [],
            self.q("SELECT body FROM shell_messages WHERE body='must not land'"),
        )


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

    def test_opencode_declares_native_skill_delivery(self):
        adapter = self.adapter("opencode")
        self.assertEqual(
            adapter["skill_dirs"],
            [".claude/skills", ".opencode/skills"],
        )
        self.assertEqual(adapter["env"]["OPENCODE_DISABLE_CLAUDE_CODE"], "1")

    def test_opencode_renders_every_declared_skill_directory(self):
        adapter = self.adapter("opencode")
        with mock.patch.object(
            run.flat,
            "render_skill_md",
            return_value={"written": [], "skipped": []},
        ) as render:
            summary = run.render_harness_skills(
                object(), 7, Path("/tmp/work"), adapter
            )

        self.assertEqual(
            [call.kwargs["skills_dir"] for call in render.call_args_list],
            [Path(".claude/skills"), Path(".opencode/skills")],
        )
        self.assertEqual(
            summary["dirs"],
            [".claude/skills", ".opencode/skills"],
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
