#!/usr/bin/env python3
"""Tests for the watched-PR polling cutover (spec #20 task #85, decision
#19): pr_poller's sprint scoping, registration baselines, bounded poll cycle
(runs/observations/dedupe/backoff/blind windows), wake-item creation, the
watched_prs uniqueness rebuild (migration 0080), and the service scheduler.

Stdlib `unittest`, matching the sibling suites. The GitHub seam is injectable
(`poll_cycle(con, fetch=...)` / `baseline_read(..., fetch=...)`), so every
transition is exercised hermetically — no network, no gh.

Run:
    python3 tests/test_pr_poller.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import pr_poller  # noqa: E402
import watch  # noqa: E402  (retired daemon verb)


def build_db(path: "Path | None" = None, skip: "set[str] | None" = None) -> sqlite3.Connection:
    """Fresh DB the way the engine ships it: schema.sql + every migration
    (optionally minus the named ones — the 0080 migration test)."""
    con = sqlite3.connect(path if path else ":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        if skip and p.name in skip:
            continue
        con.executescript(p.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def seed_shells(con: sqlite3.Connection) -> None:
    con.executescript(
        "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);"
        "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
        "VALUES (1, 'Planner', 'plan1', 'x', 1, 'tok'), (2, 'Dev', 'dev1', 'x', 1, NULL);")
    con.commit()


def seed_sprint_doc(con, doc_id: int, status: str = "ACTIVE", frozen: int = 0,
                    title: str = "SPRINT: T", kind: str = "doc") -> None:
    con.execute(
        "INSERT INTO documents (document_id, kind, title, body, frozen) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, kind, title, f"# {title}\nstatus: {status}\ndeclared: today\n", frozen))
    con.commit()


def gh_node(state="OPEN", sha="abc1234def", checks=None, reviews=0, review_state=None):
    return {"state": state, "headRefOid": sha,
            "reviews": {"totalCount": reviews,
                        "nodes": ([{"state": review_state}] if review_state else [])},
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                              ({"state": checks} if checks else None)}}]}}


def fetch_ok(*prs):
    """A gh_fetch stand-in: r0, r1, … map to the sorted (repo, pr) pairs."""
    return lambda q: pr_poller.GhResult(
        data={f"r{i}": {"pullRequest": p} for i, p in enumerate(prs)})


def fetch_err(error="boom", rate_limited=False):
    return lambda q: pr_poller.GhResult(error=error, rate_limited=rate_limited)


def watch_row(con, watch_id):
    return con.execute("SELECT * FROM watched_prs WHERE watch_id=?",
                       (watch_id,)).fetchone()


# ── sprint scoping ────────────────────────────────────────────────────────────

class SprintScopingTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()

    def tearDown(self):
        self.con.close()

    def test_only_active_unfrozen_sprint_docs_count(self):
        seed_sprint_doc(self.con, 100)                                # ACTIVE
        seed_sprint_doc(self.con, 101, status="CLOSED")               # closed
        seed_sprint_doc(self.con, 102, frozen=1)                      # frozen
        seed_sprint_doc(self.con, 103, title="SPRINT REPORT: T")      # not a board
        seed_sprint_doc(self.con, 104, kind="spec")                   # not a doc
        seed_sprint_doc(self.con, 105, status="active")               # case matters
        self.assertEqual(pr_poller.active_sprint_doc_ids(self.con), {100})

    def test_is_active_sprint(self):
        seed_sprint_doc(self.con, 100)
        seed_sprint_doc(self.con, 101, status="CLOSED")
        self.assertTrue(pr_poller.is_active_sprint(self.con, 100))
        self.assertFalse(pr_poller.is_active_sprint(self.con, 101))
        self.assertFalse(pr_poller.is_active_sprint(self.con, 999))

    def test_armed_watches_excludes_unscoped_and_inactive(self):
        seed_shells(self.con)
        seed_sprint_doc(self.con, 100)
        seed_sprint_doc(self.con, 101, status="CLOSED")
        self.con.executescript(
            "INSERT INTO watched_prs (repo, pr_number, shell_id, sprint_doc_id) VALUES "
            "('o/r', 1, 1, 100),"     # armed
            "('o/r', 2, 1, NULL),"    # unscoped → dormant
            "('o/r', 3, 1, 101);"     # closed sprint → dormant
            "UPDATE watched_prs SET closed_at=datetime('now') WHERE pr_number=3;")
        self.con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id, sprint_doc_id, closed_at) "
            "VALUES ('o/r', 4, 1, 100, datetime('now'))")   # retired
        self.con.commit()
        armed = pr_poller.armed_watches(self.con)
        self.assertEqual([(w["repo"], w["pr_number"]) for w in armed], [("o/r", 1)])


# ── registration baselines ────────────────────────────────────────────────────

class BaselineReadTest(unittest.TestCase):
    def test_ok_returns_normalized_fingerprint(self):
        fp, err = pr_poller.baseline_read("o/r", 7, fetch_ok(gh_node(checks="PENDING")))
        self.assertIsNone(err)
        self.assertEqual(fp["state"], "OPEN")
        self.assertEqual(fp["checks"], "PENDING")
        self.assertEqual(fp["sha"], "abc1234def")
        # normalized only — nothing else rides along
        self.assertEqual(set(fp), {"state", "sha", "checks", "reviews", "review_state"})

    def test_gh_failure_is_a_retryable_error_not_a_watch(self):
        fp, err = pr_poller.baseline_read("o/r", 7, fetch_err("API rate limit exceeded"))
        self.assertIsNone(fp)
        self.assertIn("rate limit", err)

    def test_unreadable_pr_is_an_error(self):
        fp, err = pr_poller.baseline_read("o/r", 7, fetch_ok(None))
        self.assertIsNone(fp)
        self.assertIn("unreadable", err)


# ── per-repo backoff + blind windows ──────────────────────────────────────────

class PollerStateTest(unittest.TestCase):
    def test_failure_escalates_and_caps(self):
        s = pr_poller.PollerState()
        now = 1000.0
        s.record_failure("o/r", now, 30)
        self.assertFalse(s.due("o/r", now + 59))
        self.assertTrue(s.due("o/r", now + 61))          # 30 * 2^1
        s.record_failure("o/r", now + 61, 30)
        self.assertFalse(s.due("o/r", now + 61 + 119))   # 30 * 2^2
        for i in range(20):
            s.record_failure("o/r", now + 100000 + i, 30)
        r = s._r("o/r")
        self.assertLessEqual(r["skip_until"] - (now + 100000 + 19),
                             pr_poller.BACKOFF_CAP_S)

    def test_success_after_failure_is_a_blind_window(self):
        s = pr_poller.PollerState()
        self.assertFalse(s.record_success("o/r"))        # clean history
        s.record_failure("o/r", 0.0, 30)
        self.assertTrue(s.record_success("o/r"))         # blind
        self.assertFalse(s.record_success("o/r"))        # converged again

    def test_other_repos_stay_due(self):
        s = pr_poller.PollerState()
        s.record_failure("o/r", 0.0, 30)
        self.assertTrue(s.due("other/repo", 1.0))


# ── the poll cycle ────────────────────────────────────────────────────────────

class PollCycleTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        seed_shells(self.con)
        seed_sprint_doc(self.con, 100)
        # PR 1 watched by BOTH shells (fan-out); PR 2 by shell 1;
        # PR 3 unscoped (dormant) — all under one repo.
        self.con.executescript(
            "INSERT INTO watched_prs (repo, pr_number, shell_id, sprint_doc_id) VALUES "
            "('o/r', 1, 1, 100), ('o/r', 1, 2, 100), ('o/r', 2, 1, 100),"
            "('o/r', 3, 1, NULL);")
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def messages(self):
        return self.con.execute(
            "SELECT from_shell_id, to_shell_id, kind, body, sprint_doc_id, dedupe_key "
            "FROM shell_messages ORDER BY message_id").fetchall()

    def test_first_cycle_baselines_armed_watches_only(self):
        n = pr_poller.poll_cycle(self.con, fetch_ok(
            gh_node(checks="PENDING"), gh_node(checks="PENDING")))
        self.assertEqual(n["events"], 0)
        seen = {r["pr_number"]: r["last_seen"] for r in self.con.execute(
            "SELECT pr_number, last_seen FROM watched_prs")}
        self.assertTrue(seen[1] and seen[2])         # armed → baselined
        self.assertIsNone(seen[3])                   # dormant → untouched

    def test_dormant_watch_is_never_fetched(self):
        def fetch(q):
            self.assertNotIn("number: 3", q)
            return pr_poller.GhResult(data={
                "r0": {"pullRequest": gh_node(checks="PENDING")},
                "r1": {"pullRequest": gh_node(checks="PENDING")}})
        pr_poller.poll_cycle(self.con, fetch)

    def test_unscoped_watch_raises_one_deduped_alert_without_being_fetched(self):
        pr_poller.poll_cycle(
            self.con,
            fetch_ok(gh_node(checks="PENDING"), gh_node(checks="PENDING")))
        pr_poller.poll_cycle(
            self.con,
            fetch_ok(gh_node(checks="PENDING"), gh_node(checks="PENDING")))

        alerts = self.con.execute(
            "SELECT watch_id, severity, reason, resolved_at "
            "FROM planner_alerts WHERE reason='pr_watch_unscoped'").fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["watch_id"], 4)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertIsNone(alerts[0]["resolved_at"])

    def test_transition_fans_out_scoped_and_idempotent(self):
        pr_poller.poll_cycle(self.con, fetch_ok(
            gh_node(checks="PENDING"), gh_node(checks="PENDING")))
        n = pr_poller.poll_cycle(self.con, fetch_ok(
            gh_node(checks="SUCCESS"), gh_node(checks="PENDING")))
        self.assertEqual(n["events"], 2)             # one per subscriber
        msgs = self.messages()
        self.assertEqual({m["to_shell_id"] for m in msgs}, {1, 2})
        for m in msgs:
            self.assertEqual(m["kind"], "pr_event")
            self.assertEqual(m["sprint_doc_id"], 100)
            self.assertIn("checks green", m["body"])
            self.assertTrue(m["dedupe_key"].startswith("pr-event|"))
        # The transition is durable as an observation riding its run.
        obs = self.con.execute(
            "SELECT watch_id, run_id, head_sha, transition, blind_window "
            "FROM pr_poll_observations").fetchall()
        self.assertEqual(len(obs), 2)
        for o in obs:
            self.assertEqual(o["transition"], "checks:SUCCESS")
            self.assertEqual(o["head_sha"], "abc1234def")
            self.assertEqual(o["blind_window"], 0)
            self.assertIsNotNone(o["run_id"])

    def test_semantic_dedupe_survives_a_replayed_transition(self):
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="PENDING"),
                                                gh_node(checks="PENDING")))
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="SUCCESS"),
                                                gh_node(checks="PENDING")))
        self.assertEqual(len(self.messages()), 2)
        # A replay (state store rewound — restart with a stale snapshot, a
        # double-fire) re-detects the transition but the dedupe key suppresses
        # the duplicate pr_event: no double wake, ever.
        self.con.execute(
            "UPDATE watched_prs SET last_seen=? WHERE pr_number=1",
            (json.dumps({"state": "OPEN", "sha": "abc1234def", "checks": "PENDING",
                         "reviews": 0, "review_state": None}),))
        self.con.commit()
        n = pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="SUCCESS"),
                                                    gh_node(checks="PENDING")))
        self.assertEqual(n["events"], 0)
        self.assertEqual(len(self.messages()), 2)

    def test_quiet_successful_poll_writes_no_observation(self):
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="PENDING"),
                                                gh_node(checks="PENDING")))
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="PENDING"),
                                                gh_node(checks="PENDING")))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM pr_poll_observations").fetchone()[0], 0)

    def test_merge_retires_and_early_merge_waits_for_checks(self):
        # #375 end to end on the new cycle: merge with PENDING checks retains,
        # the deferred conclusion retires.
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="PENDING"),
                                                gh_node(checks="PENDING")))
        n = pr_poller.poll_cycle(self.con, fetch_ok(
            gh_node(checks="PENDING"), gh_node(state="MERGED", checks="PENDING")))
        self.assertEqual(n["events"], 1)             # merge event, PR 2's one subscriber
        live = {r["pr_number"] for r in self.con.execute(
            "SELECT pr_number FROM watched_prs WHERE closed_at IS NULL")}
        self.assertEqual(live, {1, 2, 3})            # PR 2 retained
        n = pr_poller.poll_cycle(self.con, fetch_ok(
            gh_node(checks="PENDING"), gh_node(state="MERGED", checks="SUCCESS")))
        self.assertEqual(n["events"], 1)             # the deferred conclusion
        live = {r["pr_number"] for r in self.con.execute(
            "SELECT pr_number FROM watched_prs WHERE closed_at IS NULL")}
        self.assertEqual(live, {1, 3})               # now retired

    def test_scoped_merge_emits_pr_event_queues_wake_and_retires(self):
        self.con.executescript(
            "INSERT INTO interface_generations (shell_id, generation) VALUES (1, 1);"
            "INSERT INTO interface_sessions "
            "(shell_id, generation, occupancy, lifecycle) "
            "VALUES (1, 1, 'occupied', 'idle');"
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation) "
            "VALUES (100, 1, 1, 1, 1);"
            "UPDATE watched_prs SET last_seen="
            "'{\"state\":\"OPEN\",\"sha\":\"abc1234def\",\"checks\":\"PENDING\","
            "\"reviews\":0,\"review_state\":null}' "
            "WHERE repo='o/r' AND pr_number=2 AND shell_id=1;")
        self.con.commit()

        n = pr_poller.poll_cycle(
            self.con,
            fetch_ok(gh_node(checks="PENDING"),
                     gh_node(state="MERGED", checks="SUCCESS")))

        self.assertEqual(n["events"], 2)
        self.assertEqual(n["retired"], 1)
        merge = self.con.execute(
            "SELECT message_id, kind, body, sprint_doc_id FROM shell_messages "
            "WHERE body LIKE '%merged%'").fetchone()
        self.assertEqual(merge["kind"], "pr_event")
        self.assertEqual(merge["sprint_doc_id"], 100)
        self.assertIn("watch retired", merge["body"])
        item = self.con.execute(
            "SELECT binding_id, state FROM planner_wake_items "
            "WHERE message_id=?", (merge["message_id"],)).fetchone()
        self.assertEqual((item["binding_id"], item["state"]), (1, "queued"))
        row = self.con.execute(
            "SELECT closed_at FROM watched_prs "
            "WHERE repo='o/r' AND pr_number=2 AND shell_id=1").fetchone()
        self.assertIsNotNone(row["closed_at"])

    def test_failed_fetch_audits_backs_off_then_marks_blind_window(self):
        state = pr_poller.PollerState()
        pr_poller.poll_cycle(self.con, state=state, fetch=fetch_ok(
            gh_node(checks="PENDING"), gh_node(checks="PENDING")))
        t = time.monotonic()
        n = pr_poller.poll_cycle(self.con, state=state, now=t,
                                 fetch=fetch_err("connect timeout"))
        self.assertEqual(n["errors"], 1)
        run = self.con.execute(
            "SELECT status, error, finished_at FROM pr_poll_runs "
            "ORDER BY run_id DESC").fetchone()
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["error"], "connect timeout")
        self.assertIsNotNone(run["finished_at"])
        # Alert raised, deduplicated while open.
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM planner_alerts WHERE reason='pr_poll_failure' "
            "AND resolved_at IS NULL").fetchone()[0], 1)
        # In backoff: the next cycle skips the repo without fetching.
        n = pr_poller.poll_cycle(self.con, state=state, now=t + 5,
                                 fetch=lambda q: self.fail("fetched during backoff"))
        self.assertEqual(n["skipped_backoff"], 1)
        # Recovery after the backoff: success marks the blind window — GitHub
        # may have moved while polls failed.
        n = pr_poller.poll_cycle(self.con, state=state, now=t + 120,
                                 fetch=fetch_ok(gh_node(checks="SUCCESS"),
                                                gh_node(checks="PENDING")))
        self.assertEqual(n["events"], 2)
        obs = self.con.execute(
            "SELECT blind_window FROM pr_poll_observations").fetchall()
        self.assertTrue(all(o["blind_window"] == 1 for o in obs))

    def test_rate_limited_run_status(self):
        n = pr_poller.poll_cycle(self.con, fetch=fetch_err(
            "API rate limit exceeded for user", rate_limited=True))
        self.assertEqual(n["errors"], 1)
        self.assertEqual(self.con.execute(
            "SELECT status FROM pr_poll_runs").fetchone()["status"], "rate_limited")

    def test_one_repo_backing_off_does_not_block_another(self):
        self.con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id, sprint_doc_id) "
            "VALUES ('other/repo', 9, 1, 100)")
        self.con.commit()
        state = pr_poller.PollerState()
        state.record_failure("o/r", time.monotonic(), 30)
        n = pr_poller.poll_cycle(self.con, state=state, fetch=fetch_ok(
            gh_node(checks="PENDING")))
        self.assertEqual(n["skipped_backoff"], 1)
        self.assertEqual(n["repos"], 1)              # only other/repo polled
        self.assertIsNotNone(self.con.execute(
            "SELECT last_seen FROM watched_prs WHERE repo='other/repo'").fetchone()[0])

    def test_no_armed_watches_skips_the_fetch(self):
        self.con.execute("UPDATE watched_prs SET closed_at=datetime('now')")
        self.con.commit()
        n = pr_poller.poll_cycle(
            self.con, fetch=lambda q: self.fail("fetched with no armed watches"))
        self.assertEqual(n["watches"], 0)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM pr_poll_runs").fetchone()[0], 0)

    def test_run_audit_carries_source_and_watch_count(self):
        pr_poller.poll_cycle(self.con, source="reconcile", fetch=fetch_ok(
            gh_node(checks="PENDING"), gh_node(checks="PENDING")))
        run = self.con.execute("SELECT * FROM pr_poll_runs").fetchone()
        self.assertEqual(run["repo"], "o/r")
        self.assertEqual(run["source"], "reconcile")
        self.assertEqual(run["watch_count"], 3)      # the dormant one excluded
        self.assertEqual(run["status"], "ok")

    def test_wake_item_created_when_binding_is_armed(self):
        # A live (sprint, planner) binding turns the pr_event into scoped wake
        # work in the same transaction — unique (binding_id, message_id).
        self.con.executescript(
            "INSERT INTO interface_generations (shell_id, generation) VALUES (1, 1);"
            "INSERT INTO interface_sessions (shell_id, generation, occupancy, lifecycle) "
            "VALUES (1, 1, 'occupied', 'idle');"
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, generation) "
            "VALUES (100, 1, 1, 1, 1);")
        self.con.commit()
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="PENDING"),
                                                gh_node(checks="PENDING")))
        pr_poller.poll_cycle(self.con, fetch_ok(gh_node(checks="SUCCESS"),
                                                gh_node(checks="PENDING")))
        items = self.con.execute(
            "SELECT i.binding_id, i.state, m.kind, m.sprint_doc_id "
            "FROM planner_wake_items i JOIN shell_messages m "
            "ON m.message_id = i.message_id").fetchall()
        self.assertEqual(len(items), 1)              # planner's watch only — not dev1's
        self.assertEqual(items[0]["binding_id"], 1)
        self.assertEqual(items[0]["state"], "queued")
        self.assertEqual(items[0]["kind"], "pr_event")
        self.assertEqual(items[0]["sprint_doc_id"], 100)


# ── migration 0080: the uniqueness rebuild ────────────────────────────────────

class MigrationCutoverTest(unittest.TestCase):
    def test_active_watch_uniqueness_and_history_retention(self):
        con = build_db()
        seed_shells(con)
        seed_sprint_doc(con, 100)
        try:
            # One ACTIVE watch per (repo, pr, shell, scope) — the new contract.
            con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id) "
                        "VALUES ('o/r', 7, 1)")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id) "
                            "VALUES ('o/r', 7, 1)")
            # …but a sprint-scoped live watch for the same PR coexists with
            # the unscoped one (different scope, different key).
            con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id, "
                        "sprint_doc_id) VALUES ('o/r', 7, 1, 100)")
            # Closing frees the key: re-registration is a NEW row, history kept.
            con.execute("UPDATE watched_prs SET closed_at=datetime('now') "
                        "WHERE sprint_doc_id IS NULL")
            con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id) "
                        "VALUES ('o/r', 7, 1)")
            rows = con.execute(
                "SELECT closed_at, sprint_doc_id FROM watched_prs "
                "ORDER BY watch_id").fetchall()
            self.assertEqual(len(rows), 3)
            idx = con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_watched_prs_active'").fetchone()
            self.assertIsNotNone(idx)
        finally:
            con.close()

    def test_rebuild_preserves_rows_and_ids(self):
        # A pre-0080 DB (all migrations except the cutover) carrying a live and
        # a retired watch: 0080 must rebuild the table without losing either.
        con = build_db(skip={"0080_pr_polling_cutover.sql"})
        seed_shells(con)
        con.executescript(
            "INSERT INTO watched_prs (watch_id, repo, pr_number, shell_id) "
            "VALUES (1, 'o/r', 7, 1), (2, 'o/r', 8, 1);"
            "UPDATE watched_prs SET closed_at=datetime('now') WHERE watch_id=2;")
        con.commit()
        con.executescript((MIGRATIONS / "0080_pr_polling_cutover.sql").read_text())
        rows = con.execute(
            "SELECT watch_id, pr_number, closed_at FROM watched_prs "
            "ORDER BY watch_id").fetchall()
        self.assertEqual([(r["watch_id"], r["pr_number"]) for r in rows],
                         [(1, 7), (2, 8)])
        self.assertIsNone(rows[0]["closed_at"])
        self.assertIsNotNone(rows[1]["closed_at"])
        con.close()


# ── the retired daemon verb + the service scheduler ───────────────────────────

class CutoverTest(unittest.TestCase):
    def test_daemon_verb_is_retired_and_exits_clean(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            # Exit 0 — legacy systemd Restart=on-failure units stop cleanly.
            self.assertEqual(watch.main(["daemon"]), 0)
            self.assertEqual(watch.main(["daemon", "--once"]), 0)
        self.assertIn("RETIRED", out.getvalue())
        self.assertIn("sole PR poller", out.getvalue())

    def test_scheduler_beats_and_polls_armed_watches(self):
        tmp = Path(tempfile.mkdtemp()) / "shell_db.db"
        con = build_db(tmp)
        seed_shells(con)
        seed_sprint_doc(con, 100)
        con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id, "
                    "sprint_doc_id) VALUES ('o/r', 1, 1, 100)")
        con.commit()
        con.close()
        poller = pr_poller.Poller(tmp, interval=30,
                                  fetch=fetch_ok(gh_node(checks="PENDING")))
        poller.start()
        time.sleep(0.5)     # the first iteration runs before the first wait
        poller.stop()
        poller.join(timeout=5)
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM daemon_heartbeats WHERE name='watch'"
            ).fetchone()[0], 1)
            self.assertIsNotNone(con.execute(
                "SELECT last_seen FROM watched_prs").fetchone()["last_seen"])
            run = con.execute("SELECT source, status FROM pr_poll_runs").fetchone()
            self.assertEqual(run["source"], "startup")
            self.assertEqual(run["status"], "ok")
        finally:
            con.close()

    def test_scheduler_surfaces_unscoped_watch_without_fetching_it(self):
        tmp = Path(tempfile.mkdtemp()) / "shell_db.db"
        con = build_db(tmp)
        seed_shells(con)
        con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id) "
            "VALUES ('o/r', 1, 1)")
        con.commit()
        con.close()
        poller = pr_poller.Poller(
            tmp, interval=30,
            fetch=lambda q: self.fail("unscoped watch must not be fetched"))
        poller.start()
        time.sleep(0.5)
        poller.stop()
        poller.join(timeout=5)
        con = sqlite3.connect(tmp)
        try:
            row = con.execute(
                "SELECT severity, reason, resolved_at FROM planner_alerts "
                "WHERE watch_id=1").fetchone()
            self.assertEqual(row[:2], ("critical", "pr_watch_unscoped"))
            self.assertIsNone(row[2])
        finally:
            con.close()

    def test_beat_failure_never_blocks_the_poll(self):
        # Pre-0068 DB (code newer than schema): the beat raises, the poll must
        # still run — heartbeat error, not cycle error, and never a crash.
        import contextlib
        import io
        tmp = Path(tempfile.mkdtemp()) / "shell_db.db"
        con = build_db(tmp)
        seed_shells(con)
        seed_sprint_doc(con, 100)
        con.execute("INSERT INTO watched_prs (repo, pr_number, shell_id, "
                    "sprint_doc_id) VALUES ('o/r', 1, 1, 100)")
        con.execute("DROP TABLE daemon_heartbeats")
        con.commit()
        con.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            poller = pr_poller.Poller(tmp, interval=30,
                                      fetch=fetch_ok(gh_node(checks="PENDING")))
            poller.start()
            time.sleep(0.5)     # the first iteration runs before the first wait
            poller.stop()
            poller.join(timeout=5)
        self.assertIn("heartbeat error", out.getvalue())
        self.assertNotIn("cycle error", out.getvalue())
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        try:
            self.assertIsNotNone(con.execute(
                "SELECT last_seen FROM watched_prs").fetchone()["last_seen"])
        finally:
            con.close()

    def test_scheduler_self_disables_without_gh(self):
        import shutil
        old = shutil.which
        shutil.which = lambda name: None
        tmp = Path(tempfile.mkdtemp()) / "shell_db.db"
        build_db(tmp).close()
        try:
            poller = pr_poller.Poller(tmp, interval=30)   # no injected fetch
            poller.start()
            poller.join(timeout=5)
        finally:
            shutil.which = old
        self.assertFalse(poller.is_alive())
        con = sqlite3.connect(tmp)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM daemon_heartbeats").fetchone()[0], 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
