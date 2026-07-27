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
import shlex
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

    def _sprint_orchestration_declaration(self) -> str:
        body = self.con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        start = "## 3. Declare the sprint"
        end = "## 4. Arm event-driven wake"
        self.assertEqual(body.count(start), 1)
        self.assertEqual(body.count(end), 1)
        return body.split(start, 1)[1].split(end, 1)[0]

    def test_sprint_declaration_normatively_requires_feature_link(self):
        declaration = " ".join(self._sprint_orchestration_declaration().split())
        self.assertIn(
            "linked to its governing roadmap feature with "
            "`--feature <feature-id>`",
            declaration,
        )

    def test_sprint_declaration_doc_add_carries_feature_link(self):
        declaration = self._sprint_orchestration_declaration()
        lines = declaration.splitlines()
        invocations = []
        for index, line in enumerate(lines):
            if not line.strip().startswith("./sc mem doc add"):
                continue
            invocation = line.strip()
            while invocation.endswith("\\"):
                index += 1
                invocation = f"{invocation[:-1]} {lines[index].strip()}"
            invocations.append(shlex.split(invocation))

        self.assertEqual(len(invocations), 1)
        tokens = invocations[0]
        self.assertEqual(
            tokens[:5],
            ["./sc", "mem", "doc", "add", "SPRINT: <title>"],
        )
        for option, value in (
            ("--kind", "doc"),
            ("--feature", "<feature-id>"),
            ("--body-file", "<draft.md>"),
        ):
            with self.subTest(option=option):
                self.assertEqual(tokens.count(option), 1)
                self.assertEqual(tokens[tokens.index(option) + 1], value)

    def test_sprint_declaration_readback_resolves_feature_link(self):
        declaration = self._sprint_orchestration_declaration()
        invocations = [
            shlex.split(line.strip())
            for line in declaration.splitlines()
            if line.strip().startswith("./sc mem get documents")
        ]
        self.assertEqual(invocations, [[
            "./sc", "mem", "get", "documents", "--feature", "<feature-id>",
        ]])

    def test_sprint_declaration_pass_condition_requires_resolved_link(self):
        declaration = " ".join(self._sprint_orchestration_declaration().split())
        self.assertIn(
            "**Pass condition:** the `<feature-id>` document read-back names "
            "the sprint document, and",
            declaration,
        )

    def test_sprint_watch_scope_reseed_matches_asset_and_is_idempotent(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == "0086_reseed_sprint_watch_scope.sql":
                break
            con.executescript(migration.read_text())

        old = con.execute(
            "SELECT content FROM skills WHERE name='sprint'").fetchone()[0]
        self.assertNotIn("--sprint <doc-id>", old.split("**5. Babysit", 1)[0])

        reseed = (MIGRATIONS / "0086_reseed_sprint_watch_scope.sql").read_text()
        con.executescript(reseed)
        once = con.execute(
            "SELECT content FROM skills WHERE name='sprint'").fetchone()[0]
        con.executescript(reseed)
        twice = con.execute(
            "SELECT content FROM skills WHERE name='sprint'").fetchone()[0]

        # 0086 is idempotent on its own: applying it twice is applying it once.
        self.assertEqual(twice, once)
        # Its scope fix survives every LATER reseed. Asserting `once == asset`
        # would pin 0086 as the last word on this skill and silently forbid any
        # subsequent reseed, so replay the rest of the chain and require the
        # chain — not one migration — to converge on the asset.
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name > "0086_reseed_sprint_watch_scope.sql":
                con.executescript(migration.read_text())
        retired = con.execute(
            "SELECT is_deleted FROM skills WHERE name='sprint'").fetchone()[0]
        final = con.execute(
            "SELECT content FROM skills WHERE name='sprint_dev'").fetchone()[0]
        asset = (ENGINE / "assets" / "skills" / "sprint_dev" /
                 "SKILL.md").read_text().split("---", 2)[2].strip()
        self.assertEqual(retired, 1)
        self.assertEqual(final, asset)
        self.assertIn("--sprint <doc-id>", final)

    def test_sprint_orchestration_has_no_billing_gate(self):
        body = self.con.execute(
            "SELECT content FROM skills WHERE name='sprint_orchestration'").fetchone()[0]
        self.assertIn("Which harness and model should every developer use?", body)
        self.assertIn("Which harness and model should every reviewer use?", body)
        self.assertIn("operator-managed inputs", body)
        for retired_gate in (
                "Billing approval required",
                "billing-exception:",
                "CODEX_API_KEY",
                "ANTHROPIC_API_KEY",
                "Extra Usage",
                "flexible-credit balance"):
            self.assertNotIn(retired_gate, body)

    def test_sprint_chain_migration_grants_roles_and_retires_combined_skill(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == "0103_sprint_skill_chain.sql":
                break
            con.executescript(migration.read_text())
        con.executescript(
            "INSERT INTO users (user_id, username, is_active) "
            "VALUES (1, 'T', 1);"
            "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
            "system_prompt, user_id) VALUES "
            "(1, 'Planner', 'plan1', 'planner', 'x', 1),"
            "(2, 'Dev', 'dev1', 'dev', 'x', 1),"
            "(3, 'Reviewer', 'rev1', 'reviewer', 'x', 1);")

        reseed = (MIGRATIONS / "0103_sprint_skill_chain.sql").read_text()
        con.executescript(reseed)
        con.executescript(reseed)

        grants = {}
        for shortname, skill in con.execute(
                "SELECT sh.shortname, sk.name FROM shell_skills ss "
                "JOIN shells sh ON sh.shell_id=ss.shell_id "
                "JOIN skills sk ON sk.skill_id=ss.skill_id "
                "WHERE sk.is_deleted=0"):
            grants.setdefault(shortname, set()).add(skill)

        self.assertTrue(
            {"sprint_orchestration", "sprint_orchestration_recover",
             "sprint_orchestration_close"}.issubset(grants["plan1"]))
        self.assertIn("sprint_dev", grants["dev1"])
        self.assertIn("sprint_review", grants["rev1"])
        self.assertEqual(
            con.execute("SELECT is_deleted FROM skills WHERE name='sprint'")
            .fetchone()[0], 1)

    def test_shell_templates_grant_only_the_role_specific_sprint_skills(self):
        templates = ENGINE / "templates" / "shells"
        planner = json.loads((templates / "planner.json").read_text())["skills"]
        dev = json.loads((templates / "dev.json").read_text())["skills"]
        reviewer = json.loads((templates / "reviewer.json").read_text())["skills"]

        self.assertTrue(
            {"sprint_orchestration", "sprint_orchestration_recover",
             "sprint_orchestration_close"}.issubset(planner))
        self.assertIn("sprint_dev", dev)
        self.assertIn("sprint_review", reviewer)
        self.assertNotIn("sprint", dev + reviewer)

    def test_role_skills_keep_the_planner_as_the_event_router(self):
        def body(name):
            return (ENGINE / "assets" / "skills" / name / "SKILL.md"
                    ).read_text().split("---", 2)[2]

        orchestration = body("sprint_orchestration")
        dev = body("sprint_dev")
        review = body("sprint_review")

        self.assertIn("developer reports\na green exact head", orchestration)
        self.assertIn("The planner sends and boots the reviewer", dev)
        self.assertIn(
            "The planner routes fix work or\nmerge authority to the developer",
            review)
        self.assertNotIn(
            "Send Major and Medium findings directly to the developer", review)

    def test_recovery_requires_process_and_subject_evidence_before_reboot(self):
        recovery = (
            ENGINE / "assets" / "skills" / "sprint_orchestration_recover" /
            "SKILL.md").read_text().split("---", 2)[2]

        self.assertIn("/proc/<pid>/stat", recovery)
        self.assertIn("positive CPU delta as active work", recovery)
        self.assertIn(
            "the board and scoped task prove which sprint and unit", recovery)
        self.assertIn("A reconciler finding requests a checkup", recovery)

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


class WatchAlertIdentityTest(unittest.TestCase):
    """H-13's other half: an alert about a watch says WHICH UNIT structurally.

    0102 gave planner_alerts `sprint_doc_id`/`unit_id` and the watch path left
    both NULL, so every reader was pushed back to parsing `dedupe_key` or
    grepping prose. The watch is the one alert source that knows the answer.
    """

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

    def _alert_row(self, wid):
        return self.con.execute(
            "SELECT sprint_doc_id, unit_id, dedupe_key FROM planner_alerts "
            "WHERE watch_id=?", (wid,)).fetchone()

    def test_an_alert_on_a_linked_watch_names_the_unit(self):
        wid = self._watch(41, self.unit)
        pr_poller._alert(self.con, severity="critical",
                         reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        row = self._alert_row(wid)
        self.assertEqual(row["sprint_doc_id"], 100)
        self.assertEqual(row["unit_id"], self.unit)

    def test_an_unlinked_watch_still_scopes_the_sprint(self):
        """Partial structure beats none: the sprint is known even when the
        unit is not, and claiming a unit here would be a guess."""
        wid = self._watch(42, None)
        pr_poller._alert(self.con, severity="critical",
                         reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        row = self._alert_row(wid)
        self.assertEqual(row["sprint_doc_id"], 100)
        self.assertIsNone(row["unit_id"])

    def test_the_dedupe_identity_did_not_move(self):
        """The structured columns are ADDITIVE. If dedupe_key changed shape,
        every alert already open would re-raise as a new row the moment this
        shipped — a fleet-wide alert storm on upgrade."""
        wid = self._watch(43, self.unit)
        for _ in range(3):
            pr_poller._alert(self.con, severity="critical",
                             reason="pr_poll_failure", watch_id=wid)
        self.con.commit()
        rows = self.con.execute(
            "SELECT dedupe_key FROM planner_alerts WHERE watch_id=?",
            (wid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], f"-|-|{wid}|-|pr_poll_failure")


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

    def test_stale_reconciler_is_rendered_while_poller_is_fresh(self):
        con = build_db()
        self.addCleanup(con.close)
        con.executescript(
            "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
            "VALUES ('watch', datetime('now'), 30);"
            "INSERT INTO daemon_heartbeats (name, beat_at, interval_s) "
            "VALUES ('reconcile', datetime('now', '-3600 seconds'), 600);"
        )

        line = watch.daemon_line(server.Handler._daemon_state(None, con))

        self.assertIn("poller: live", line)
        self.assertNotIn("poller: STALE", line)
        self.assertIn("reconciler: STALE", line)
        self.assertIn("worker reconciliation is NOT running", line)

        con.execute(
            "UPDATE daemon_heartbeats SET beat_at = datetime('now', '-200 seconds') "
            "WHERE name = 'reconcile'")
        line = watch.daemon_line(server.Handler._daemon_state(None, con))
        self.assertIn("reconciler: live", line)

        con.execute("DELETE FROM daemon_heartbeats WHERE name = 'watch'")
        line = watch.daemon_line(server.Handler._daemon_state(None, con))
        self.assertIn("poller: never run", line)


# ── API: /_sc/watches + message kinds, over the real server ─────────────────

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
        self.assertEqual(
            watch.main(["pr", "own/repo", "11", "--sprint", "100"]), 0)
        rows = self.q("SELECT shell_id, closed_at, sprint_doc_id FROM watched_prs "
                      "WHERE repo='own/repo' AND pr_number=11")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shell_id"], 1)   # the token shell (plan1)
        self.assertEqual(rows[0]["sprint_doc_id"], 100)

    # ── H-13: structured PR↔unit linkage ────────────────────────────────────

    def unit(self, seq, doc_id=100):
        con = sqlite3.connect(self.db)
        try:
            uid = con.execute(
                "INSERT INTO sprint_units (sprint_doc_id, seq, unit_title) "
                "VALUES (?,?,?)", (doc_id, seq, f"unit {seq}")).lastrowid
            con.commit()
            return uid
        finally:
            con.close()

    def test_register_links_the_watch_to_a_board_unit(self):
        uid = self.unit("U3")
        self.assertEqual(watch.main(
            ["pr", "own/repo", "31", "--sprint", "100", "--unit", "U3"]), 0)
        rows = self.q("SELECT unit_id FROM watched_prs WHERE pr_number=31")
        self.assertEqual(rows[0]["unit_id"], uid)

    def test_registering_without_a_unit_leaves_the_link_null(self):
        """The link is NULLABLE and stays that way. Unscoped traffic is what
        the reconciler's regex fallback exists for; inventing a unit here
        would make every watch claim one."""
        watch.main(["pr", "own/repo", "32", "--sprint", "100"])
        self.assertIsNone(
            self.q("SELECT unit_id FROM watched_prs WHERE pr_number=32")[0][
                "unit_id"])

    def test_an_undeclared_unit_is_refused_not_silently_dropped(self):
        """A typo'd --unit that registered an UNLINKED watch would report
        success and leave the planner believing in a link that is not there —
        the reconciler would then answer by regex and nobody would know."""
        with self.assertRaises(SystemExit):
            watch.main(["pr", "own/repo", "33", "--sprint", "100",
                        "--unit", "U99"])
        self.assertEqual(
            self.q("SELECT 1 FROM watched_prs WHERE pr_number=33"), [])

    def test_a_unit_on_another_sprints_board_is_not_this_sprints_unit(self):
        """`seq` is unique per sprint, not globally: U3 exists on most boards.
        Resolving it without the sprint would link a watch to a stranger."""
        con = sqlite3.connect(self.db)
        seed_sprint_doc(con, 200)
        con.close()
        self.unit("U7", doc_id=200)
        with self.assertRaises(SystemExit):
            watch.main(["pr", "own/repo", "34", "--sprint", "100",
                        "--unit", "U7"])

    def test_re_registering_with_a_unit_links_the_live_watch(self):
        """The idempotent path must not swallow --unit: "already watched" plus
        a dropped flag is a link the operator has no way to see is missing."""
        watch.main(["pr", "own/repo", "35", "--sprint", "100"])
        uid = self.unit("U4")
        watch.main(["pr", "own/repo", "35", "--sprint", "100", "--unit", "U4"])
        rows = self.q("SELECT unit_id FROM watched_prs WHERE pr_number=35")
        self.assertEqual(len(rows), 1, "the re-registration minted a row")
        self.assertEqual(rows[0]["unit_id"], uid)

    def test_the_link_is_correctable(self):
        """A mis-typed --unit has to be fixable in place. The alternative is
        retiring the watch and losing its baseline to repair a typo."""
        watch.main(["pr", "own/repo", "36", "--sprint", "100", "--unit", "U3"])
        right = self.unit("U5")
        watch.main(["pr", "own/repo", "36", "--sprint", "100", "--unit", "U5"])
        self.assertEqual(
            self.q("SELECT unit_id FROM watched_prs WHERE pr_number=36")[0][
                "unit_id"], right)

    def test_the_watch_list_reports_the_unit(self):
        self.unit("U6")
        watch.main(["pr", "own/repo", "37", "--sprint", "100", "--unit", "U6"])
        r = watch._api("GET", "/_sc/watches")
        row = [w for w in r["watches"] if w["pr_number"] == 37][0]
        self.assertEqual(row["unit_seq"], "U6")

    def test_register_for_another_shell(self):
        watch.main(["pr", "own/repo", "12", "--shell", "dev1",
                    "--sprint", "100"])
        rows = self.q("SELECT shell_id FROM watched_prs WHERE pr_number=12")
        self.assertEqual(rows[0]["shell_id"], 2)

    def test_register_unknown_shell_dies(self):
        with self.assertRaises(SystemExit):
            watch.main(["pr", "own/repo", "13", "--shell", "nobody",
                        "--sprint", "100"])

    def test_register_bad_repo_dies(self):
        with self.assertRaises(SystemExit):
            watch.main(["pr", "not-a-repo", "14", "--sprint", "100"])

    def test_duplicate_live_watch_is_idempotent(self):
        watch.main(["pr", "own/repo", "15", "--sprint", "100"])
        watch.main(["pr", "own/repo", "15", "--sprint", "100"])
        self.assertEqual(len(self.q(
            "SELECT 1 FROM watched_prs WHERE pr_number=15")), 1)

    def test_retired_watch_reregisters_as_new_row_keeping_history(self):
        watch.main(["pr", "own/repo", "16", "--sprint", "100"])
        con = sqlite3.connect(self.db)
        con.execute("UPDATE watched_prs SET closed_at=datetime('now'), "
                    "last_seen='{\"state\":\"MERGED\"}' WHERE pr_number=16")
        con.commit()
        con.close()
        watch.main(["pr", "own/repo", "16", "--sprint", "100"])
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
                watch.main(["pr", "own/repo", "18", "--sprint", "100"])
        finally:
            pr_poller.gh_fetch = real
        self.assertEqual(self.q("SELECT 1 FROM watched_prs WHERE pr_number=18"), [])

    def test_registration_stores_the_baseline(self):
        watch.main(["pr", "own/repo", "19", "--sprint", "100"])
        row = self.q("SELECT last_seen FROM watched_prs WHERE pr_number=19")[0]
        self.assertIn('"state": "OPEN"', row["last_seen"])
        self.assertIn('"sha": "abc1234def"', row["last_seen"])

    def test_registration_refuses_scope_unmade_during_baseline(self):
        """The TOCTOU window the BEGIN IMMEDIATE revalidation exists to close.

        Registration validates SPRINT-DOC IDENTITY (H-2), not liveness, so the
        mutation that must lose the race is one that stops the target being a
        sprint board at all — here a retitle mid-baseline."""
        con = sqlite3.connect(self.db)
        seed_sprint_doc(con, 101)
        con.close()
        real = pr_poller.baseline_read

        def unmake_scope(repo, pr):
            con = sqlite3.connect(self.db)
            try:
                con.execute(
                    "UPDATE documents SET title='Retro notes' "
                    "WHERE document_id=101")
                con.commit()
            finally:
                con.close()
            return ({"state": "OPEN", "sha": "retitled-during-baseline",
                     "checks": "PENDING", "reviews": 0,
                     "review_state": None}, None)

        pr_poller.baseline_read = unmake_scope
        try:
            with self.assertRaises(SystemExit) as denied:
                watch.main(
                    ["pr", "own/repo", "22", "--sprint", "101"])
        finally:
            pr_poller.baseline_read = real

        self.assertIn("HTTP 409", str(denied.exception))
        self.assertIn("not a SPRINT doc", str(denied.exception))
        self.assertEqual(
            self.q("SELECT 1 FROM watched_prs WHERE pr_number=22"), [])

    def test_registration_on_a_frozen_sprint_is_accepted_but_dormant(self):
        """H-2's deliberate loosening, stated as behaviour rather than left
        implicit. Registration is an identity check, so a frozen sprint takes
        the row; the poller then never sees it, because arming reads liveness.
        Registering during the declaration window rides the same rule."""
        con = sqlite3.connect(self.db)
        seed_sprint_doc(con, 113)
        con.execute("UPDATE documents SET frozen=1 WHERE document_id=113")
        seed_sprint_doc(con, 114, units=0)          # board not declared yet
        con.commit()
        con.close()
        real = pr_poller.baseline_read
        pr_poller.baseline_read = lambda repo, pr: (
            {"state": "OPEN", "sha": "s", "checks": None, "reviews": 0,
             "review_state": None}, None)
        try:
            watch.main(["pr", "own/repo", "31", "--sprint", "113"])
            watch.main(["pr", "own/repo", "32", "--sprint", "114"])
        finally:
            pr_poller.baseline_read = real

        self.assertEqual(
            [(r["pr_number"], r["sprint_doc_id"]) for r in self.q(
                "SELECT pr_number, sprint_doc_id FROM watched_prs "
                "WHERE pr_number IN (31, 32) ORDER BY pr_number")],
            [(31, 113), (32, 114)], "both registrations were accepted")
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            armed = {w["sprint_doc_id"] for w in pr_poller.armed_watches(con)}
        finally:
            con.close()
        self.assertNotIn(113, armed, "a frozen sprint is never polled")
        self.assertNotIn(114, armed, "an undeclared board is never polled")

    def test_scoped_registration_rebinds_legacy_and_resolves_alert(self):
        con = sqlite3.connect(self.db)
        watch_id = con.execute(
            "INSERT INTO watched_prs (repo, pr_number, shell_id) "
            "VALUES ('own/repo', 21, 1)").lastrowid
        con.execute(
            "INSERT INTO planner_alerts "
            "(watch_id, severity, reason, dedupe_key) VALUES "
            "(?, 'critical', 'pr_watch_unscoped', ?)",
            (watch_id, f"-|-|{watch_id}|-|pr_watch_unscoped"))
        con.commit()
        con.close()

        watch.main(["pr", "own/repo", "21", "--sprint", "100"])

        rows = self.q(
            "SELECT watch_id, sprint_doc_id, last_seen FROM watched_prs "
            "WHERE pr_number=21")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["watch_id"], watch_id)
        self.assertEqual(rows[0]["sprint_doc_id"], 100)
        self.assertIn('"checks": "PENDING"', rows[0]["last_seen"])
        alert = self.q(
            "SELECT resolved_at FROM planner_alerts WHERE watch_id=?",
            watch_id)[0]
        self.assertIsNotNone(alert["resolved_at"])

    def test_legacy_rebind_refuses_scope_unmade_during_baseline(self):
        con = sqlite3.connect(self.db)
        seed_sprint_doc(con, 102)
        watch_id = con.execute(
            "INSERT INTO watched_prs "
            "(repo, pr_number, shell_id, last_seen) "
            "VALUES ('own/repo', 23, 1, '{\"state\":\"OPEN\"}')").lastrowid
        con.execute(
            "INSERT INTO planner_alerts "
            "(watch_id, severity, reason, dedupe_key) VALUES "
            "(?, 'critical', 'pr_watch_unscoped', ?)",
            (watch_id, f"-|-|{watch_id}|-|pr_watch_unscoped"))
        con.commit()
        con.close()
        real = pr_poller.baseline_read

        def unmake_scope(repo, pr):
            con = sqlite3.connect(self.db)
            try:
                con.execute(
                    "UPDATE documents SET title='Retro notes' "
                    "WHERE document_id=102")
                con.commit()
            finally:
                con.close()
            return ({"state": "OPEN", "sha": "retitled-during-baseline",
                     "checks": "SUCCESS", "reviews": 1,
                     "review_state": "APPROVED"}, None)

        pr_poller.baseline_read = unmake_scope
        try:
            with self.assertRaises(SystemExit) as denied:
                watch.main(
                    ["pr", "own/repo", "23", "--sprint", "102"])
        finally:
            pr_poller.baseline_read = real

        self.assertIn("HTTP 409", str(denied.exception))
        row = self.q(
            "SELECT sprint_doc_id, last_seen FROM watched_prs "
            "WHERE watch_id=?", watch_id)[0]
        self.assertIsNone(row["sprint_doc_id"])
        self.assertEqual(row["last_seen"], '{"state":"OPEN"}')
        alert = self.q(
            "SELECT resolved_at FROM planner_alerts WHERE watch_id=?",
            watch_id)[0]
        self.assertIsNone(alert["resolved_at"])

    def test_list_shows_live_watches(self):
        watch.main(["pr", "own/repo", "17", "--sprint", "100"])
        self.assertEqual(watch.main(["list"]), 0)

    def test_unscoped_registration_fails_loudly_without_a_row(self):
        with self.assertRaises(SystemExit):
            watch.main(["pr", "own/repo", "20"])
        with self.assertRaises(SystemExit):
            watch._api("POST", "/_sc/watches",
                       {"repo": "own/repo", "pr_number": 20})
        self.assertEqual(
            self.q("SELECT 1 FROM watched_prs WHERE pr_number=20"), [])

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

    def test_api_refuses_pr_event_from_a_shell_token(self):
        """H-3 — kind parity. The CLI's argparse `choices` was the ONLY fence:
        any holder of any shell key could POST kind='pr_event' straight past it
        and mint a PR transition GitHub never reported, waking a planner on
        forged ground truth. The refusal belongs at shell-token ingress, which
        is what this route is."""
        before = self.q("SELECT COUNT(*) c FROM shell_messages "
                        "WHERE kind='pr_event'")[0]["c"]
        with self.assertRaises(SystemExit) as denied:
            mem._api("POST", "/_sc/mem/messages",
                     {"to": "plan1", "body": "own/repo#9 merged (forged)",
                      "kind": "pr_event", "sprint_doc_id": 100})
        self.assertIn("HTTP 403", str(denied.exception))
        self.assertEqual(
            self.q("SELECT COUNT(*) c FROM shell_messages "
                   "WHERE kind='pr_event'")[0]["c"], before,
            "the refused send must leave no row")

    def test_the_poller_still_emits_pr_event_through_its_own_path(self):
        """The other half of H-3, and the reason it is a 403 and not a schema
        change: the poller writes `pr_event` by direct DB insert, so the API
        refusal cannot reach it. If this ever goes red, the poller has been
        moved onto the API and needs a system credential — not a carve-out in
        the ingress check.

        Its own DB, not the class fixture: poll_cycle sweeps EVERY armed watch,
        so the sibling tests' registrations would share the batched read.
        """
        con = build_db()
        try:
            seed_shells(con)
            seed_sprint_doc(con)
            con.execute(
                "INSERT INTO watched_prs (repo, pr_number, shell_id, "
                "sprint_doc_id, last_seen) VALUES ('own/repo', 77, 1, 100, ?)",
                (json.dumps({"state": "OPEN", "sha": "aaa", "checks": "PENDING",
                             "reviews": 0, "review_state": None}),))
            con.commit()
            pr_poller.poll_cycle(con, fetch=lambda q: pr_poller.GhResult(
                data={"r0": {"pullRequest": {
                    "state": "MERGED", "headRefOid": "bbb",
                    "reviews": {"totalCount": 0, "nodes": []},
                    "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                                      {"state": "SUCCESS"}}}]},
                }}}))
            emitted = con.execute(
                "SELECT body FROM shell_messages WHERE kind='pr_event'"
            ).fetchall()
        finally:
            con.close()
        self.assertTrue(emitted, "the poller's own path is unaffected")

    def test_message_scope_must_name_a_sprint_doc(self):
        """H-2 — validate sprint scope at the write boundary. This route took
        ANY integer, so a typo'd --sprint produced a message scoped to nothing:
        never woke, never filtered with the sprint's traffic, reported success.
        """
        spec_id = 100 - 1
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO documents (document_id, kind, title, body) "
            "VALUES (?, 'spec', 'Sprint flow hardening', 'x')", (spec_id,))
        con.commit()
        con.close()
        for scope in (spec_id, 99999):
            with self.subTest(scope=scope):
                with self.assertRaises(SystemExit) as denied:
                    mem._api("POST", "/_sc/mem/messages",
                             {"to": "dev1", "body": "scoped wrong",
                              "kind": "task", "sprint_doc_id": scope})
                self.assertIn("HTTP 422", str(denied.exception))
        self.assertEqual(
            self.q("SELECT COUNT(*) c FROM shell_messages "
                   "WHERE body='scoped wrong'")[0]["c"], 0)

    def test_message_scope_accepts_a_sprint_doc_at_any_liveness(self):
        """The deliberately distinct half of H-2: tagging validates IDENTITY,
        never liveness. A coordination message in the declaration window (board
        declared, units not yet) and a result row sent after close are both
        legal traffic — gating them on liveness would make the sprint's own
        open and close unreportable."""
        con = sqlite3.connect(self.db)
        seed_sprint_doc(con, 121, units=0)            # declaration window
        seed_sprint_doc(con, 122)
        con.execute("UPDATE documents SET frozen=1 WHERE document_id=122")
        con.commit()
        con.close()
        for scope in (121, 122):
            with self.subTest(scope=scope):
                out = mem._api("POST", "/_sc/mem/messages",
                               {"to": "dev1", "body": f"legal {scope}",
                                "kind": "result", "sprint_doc_id": scope})
                self.assertIn("message_id", out)
                self.assertEqual(
                    self.q("SELECT sprint_doc_id s FROM shell_messages "
                           "WHERE message_id=?", out["message_id"])[0]["s"],
                    scope)

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
        seed_sprint_doc(con)
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
        r = watch._api("POST", "/_sc/watches",
                       {"repo": "own/repo", "pr_number": 44,
                        "sprint_doc_id": 100})
        self.assertTrue(r["daemon"]["stale"])   # still the -1h beat from test_c
        r = watch._api("POST", "/_sc/watches",
                       {"repo": "own/repo", "pr_number": 44,
                        "sprint_doc_id": 100})
        self.assertTrue(r.get("existing"))      # idempotent path carries it too
        self.assertTrue(r["daemon"]["stale"])

    def test_e_pre_migration_db_degrades_to_never_run(self):
        self.x("DROP TABLE daemon_heartbeats")
        r = watch._api("GET", "/_sc/watches")
        self.assertIsNone(r["daemon"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
