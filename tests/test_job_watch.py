"""`sc job watch pr` and `sc job watch timer` (Feature #96, spec #273 "Watchers").

Hermetic: the real verbs and the real probe supervisor over a temporary
ledger, with a stand-in `gh` that answers from the recorded GitHub responses
in tests/fixtures/review/github_prs.json.

Acceptance criteria covered here: 14 (`watch pr` fires once on a settled
state, never on pending, and times out at its limit) and 15 (`watch timer`
fires once; a stopped timer wakes with `killed`), plus the limit of ten live
watchers for one shell.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import job
import runs
from job_agent_fixtures import DelegationCase
from test_runs import wait_for

# Recorded pull requests, by the state the PR watcher's normalization gives them.
GREEN, RED, MERGED, CLOSED, PENDING = 821, 822, 823, 826, 828
HEADS = {GREEN: "1" * 40, RED: "2" * 40, MERGED: "3" * 40, CLOSED: "8" * 40, PENDING: "a" * 40}
WATCHED = 4100


class WatchCase(DelegationCase):
    def answer(self, recorded: int | None) -> None:
        """Make the watched PR read as one of the recorded pull requests."""
        self.gh_map.write_text(json.dumps({} if recorded is None else {str(WATCHED): recorded}))

    def watch(self, *args) -> dict:
        before = self.newest_id()
        done = self.cli("watch", *args)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        row = job._api("GET", "/_sc/runs")["runs"][0]
        self.assertGreater(row["run_id"], before)
        self.assertEqual(row["kind"], "probe")
        self.assertIn(f"job: {row['run_id']} watching", done.stdout)
        self.assertIn("grants no merge authority", done.stdout)
        return row

    def attempts(self, row: dict) -> list[dict]:
        return [json.loads(line) for line in Path(row["evidence_path"]).read_text().splitlines()]


class WatchPullRequestTest(WatchCase, unittest.TestCase):
    """AC 14."""

    def test_each_settled_state_wakes_once_naming_state_and_head(self):
        for recorded, state in ((GREEN, "green"), (RED, "red"), (MERGED, "merged"),
                                (CLOSED, "closed")):
            with self.subTest(state=state):
                self.answer(recorded)
                row = self.watch("pr", "--pr", str(WATCHED), "--repository", "owner/name",
                                 "--every", "5")
                self.assertEqual(
                    row["argv"],
                    ["sc", "job", "watch", "pr", "--pr", str(WATCHED),
                     "--repository", "owner/name"])
                self.assertEqual(row["timeout_seconds"], 3600)
                final = self.finished(row)
                self.assertEqual(final["state"], "done")
                lines = self.wake(final).splitlines()
                self.assertEqual(
                    lines[0], f"Run {row['run_id']} (watch pr: PR #{WATCHED}) done; exit=0.")
                self.assertEqual(
                    lines[1],
                    f"Condition: PR #{WATCHED} in owner/name: checks settle (green or red), "
                    "or it is merged or closed.")
                self.assertEqual(lines[2], f"Final state: {state} at head {HEADS[recorded]}.")
                self.assertIn("grants no review, merge, or Sprint authority: green is not a "
                              "directive.", lines[3])
                # One attempt, one outcome.
                self.assertEqual(len(self.attempts(final)), 1)
        # Every read named the repository and went through `gh pr view`.
        views = [json.loads(line) for line in self.gh_calls.read_text().splitlines()
                 if json.loads(line)[:2] == ["pr", "view"]]
        self.assertTrue(all(call[-2:] == ["--repo", "owner/name"] for call in views))
        # Green is proven at the head commit: the reader also read that commit's runs.
        self.assertIn(
            ["run", "list", "--commit", HEADS[GREEN]],
            [json.loads(line)[:4] for line in self.gh_calls.read_text().splitlines()])

    def test_pending_does_not_wake_and_the_first_settled_read_does(self):
        self.answer(PENDING)
        row = self.watch("pr", "--pr", str(WATCHED), "--every", "5")
        self.assertEqual(row["argv"], ["sc", "job", "watch", "pr", "--pr", str(WATCHED)])
        wait_for(lambda: self.attempts(row))
        first = self.attempts(row)[0]
        self.assertEqual(first["exit_code"], 1)
        self.assertEqual(first["excerpt"].strip(), f"pending at head {HEADS[PENDING]}")
        live = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual(live["state"], "running")
        self.assertEqual(self.wake_bodies(row), [])

        self.answer(GREEN)
        final = self.finished(row)
        self.assertEqual(final["state"], "done")
        body = self.wake(final)
        self.assertIn(f"Final state: green at head {HEADS[GREEN]}.", body)
        self.assertGreaterEqual(len(self.attempts(final)), 2)
        # It fired once: nothing reads the PR again.
        calls = len(self.gh_calls.read_text().splitlines())
        time.sleep(0.5)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), calls)
        self.assertIn("pr: PR #4100", self.cli("list", "--kind", "probe", "--all").stdout)

    def test_a_pr_that_never_settles_ends_timeout_at_the_limit(self):
        self.answer(PENDING)
        row = self.watch("pr", "--pr", str(WATCHED), "--every", "5", "--timeout", "3")
        self.assertEqual(row["timeout_seconds"], 3)
        final = self.finished(row)
        self.assertEqual(final["state"], "timeout")
        lines = self.wake(final).splitlines()
        self.assertIn("(watch pr: PR #4100) timeout;", lines[0])
        self.assertEqual(
            lines[2], f"Final state: timeout; last read: pending at head {HEADS[PENDING]}.")

    def test_the_default_interval_is_sixty_seconds_for_an_hour(self):
        self.assertEqual((job.WATCH_PR_EVERY, job.PROBE_TIMEOUT), (60, 3600))
        self.answer(PENDING)
        row = self.watch("pr", "--pr", str(WATCHED))
        meta = job.read_meta(Path(row["evidence_path"]).parent)
        self.assertEqual((meta["every"], meta["timeout"]), (60, 3600))
        self.assertEqual(row["timeout_seconds"], 3600)
        wait_for(lambda: self.attempts(row))
        # No second read arrives within the next seconds: the delay is not the
        # five-second minimum.
        reads = len(self.attempts(row))
        time.sleep(6)
        self.assertEqual(len(self.attempts(row)), reads)
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        self.assertEqual(self.finished(row)["state"], "killed")

    def test_a_repository_must_be_owner_slash_name(self):
        self.answer(GREEN)
        for value in ("name-only", "-owner/name", "owner/name/extra", "owner /name",
                      "https://github.com/owner/name", "owner/"):
            with self.subTest(value=value):
                refused = self.cli("watch", "pr", "--pr", str(WATCHED), f"--repository={value}")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("--repository must be owner/name", refused.stderr)
        self.assertEqual(self.newest_id(), 0)
        self.assertFalse(self.gh_calls.exists())

    def test_a_pr_that_cannot_be_read_starts_no_watch(self):
        self.answer(None)
        done = self.cli("watch", "pr", "--pr", str(WATCHED))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f"cannot read PR #{WATCHED}", done.stderr)
        self.assertIn("no watch started", done.stderr)
        for options in (("--pr", "0"), ("--pr", str(WATCHED), "--every", "4"),
                        ("--pr", str(WATCHED), "--timeout", "0")):
            self.assertNotEqual(self.cli("watch", "pr", *options).returncode, 0)
        self.assertEqual(self.newest_id(), 0)

    def test_the_watch_uses_the_pr_watchers_normalization(self):
        import github_pull_requests
        import sprint_pr_watcher

        self.assertIs(sprint_pr_watcher.normalize_state, github_pull_requests.normalize_state)


class WatchTimerTest(WatchCase, unittest.TestCase):
    """AC 15."""

    def test_a_timer_wakes_once_after_its_delay(self):
        started = time.monotonic()
        row = self.watch("timer", "--after", "2")
        self.assertEqual(row["argv"], ["sc", "job", "watch", "timer", "--after", "2"])
        self.assertEqual(row["timeout_seconds"], 2)
        self.assertEqual(self.wake_bodies(row), [])
        final = self.finished(row)
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertEqual((final["state"], final["exit_code"]), ("done", 0))
        self.assertEqual(self.wake(final).splitlines()[:3], [
            f"Run {row['run_id']} (watch timer: timer 2s) done; exit=0.",
            "Condition: timer, 2 seconds.",
            "Final state: the time passed.",
        ])
        # A repeated submission is the same wake.
        job._api("POST", f"/_sc/runs/{row['run_id']}/terminal", json.loads(final["terminal_json"]))
        self.assertEqual(len(self.wake_bodies(final)), 1)

    def test_a_stopped_timer_wakes_with_killed(self):
        row = self.watch("timer", "--after", "600", "--label", "nudge")
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        final = self.finished(row)
        self.assertEqual((final["state"], final["exit_code"]), ("killed", None))
        lines = self.wake(final).splitlines()
        self.assertEqual(lines[0], f"Run {row['run_id']} (watch timer: nudge) killed; exit=unknown.")
        self.assertEqual(lines[2], "Final state: killed.")
        self.assertNotEqual(self.cli("watch", "timer", "--after", "0").returncode, 0)

    def test_a_timer_is_limited_to_twenty_four_hours(self):
        refused = self.cli("watch", "timer", "--after", "86401")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("HTTP 400", refused.stderr)
        self.assertIn("a timer is limited to 86400 seconds (24 hours); 86401 was requested",
                      refused.stderr)
        self.assertEqual(self.newest_id(), 0)
        row = self.watch("timer", "--after", "86400")
        self.assertEqual(row["timeout_seconds"], 86400)
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        self.finished(row)


class WatcherLimitTest(WatchCase, unittest.TestCase):
    def test_the_eleventh_live_watcher_is_refused_naming_the_holders(self):
        held = [self.watch("timer", "--after", "600") for _ in range(9)]
        self.assertEqual(
            self.cli("start", "--until", "false", "--every", "5").returncode, 0)
        held.append(job._api("GET", "/_sc/runs")["runs"][0])
        ids = ", ".join(str(row["run_id"]) for row in held)
        for verb in (("watch", "timer", "--after", "5"),
                     ("start", "--until", "true")):
            refused = self.cli(*verb)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("HTTP 429", refused.stderr)
            self.assertIn(
                f"live watcher limit for one shell reached: 10 of {runs.WATCH_LIVE_PER_SHELL} "
                f"live (runs {ids})", refused.stderr)
        self.assertEqual(self.newest_id(), held[-1]["run_id"])
        # A command job is not a watcher, and another shell has its own ten.
        self.assertEqual(self.cli("start", "--", "true").returncode, 0)
        # An ended watcher frees its slot.
        self.assertEqual(self.cli("kill", str(held[0]["run_id"])).returncode, 0)
        self.finished(held[0])
        self.watch("timer", "--after", "600")
        for row in job._api("GET", "/_sc/runs?kind=probe")["runs"]:
            if row["state"] == "running":
                self.cli("kill", str(row["run_id"]))
        for row in held[1:]:
            self.finished(row)


if __name__ == "__main__":
    unittest.main()
