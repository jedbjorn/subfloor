#!/usr/bin/env python3
"""live_model probe — spec doc 44 (feature 17), sprint 45 unit 44-U1.

Every fixture under tests/fixtures/live_model/ is a REAL transcript written by
a REAL run of the harness it belongs to, captured deliberately for this unit
(44-U0 scout + the U1 capture runs; provenance in that directory's README).
Nothing here is a hand-authored shape — decision #55: a test asserting against
data that cannot occur manufactures confidence.

The two mutation targets this file exists to pin, kept apart on purpose:

  * MID-SESSION SWITCH (A->B, first != last) pins "the LAST record wins".
    Reading the first record instead of the last turns these red.
  * SWITCH-BACK (A->B->A) pins the failure modes that survive a last-record
    read: dedupe-by-model, first-occurrence-wins, ignore-a-return-to-a-seen
    model. Note it CANNOT pin first-vs-last — first and last are both A — so a
    suite carrying only switch-back fixtures would pass with the selection
    inverted. That is why both shapes are captured for all three harnesses.

Run:
    python3 tests/test_live_model.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
FIXTURES = ROOT / "tests" / "fixtures" / "live_model"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import interface_broker  # noqa: E402  (the writer of `last_human_input_at`)
import live_model  # noqa: E402
from live_model import claude as p_claude  # noqa: E402
from live_model import kimi as p_kimi  # noqa: E402
from live_model import opencode as p_opencode  # noqa: E402

# The two timestamp forms that meet in `probe(active_since=…)`. They are NOT
# mutually orderable as strings — `'T'` (0x54) sorts above `' '` (0x20) — so a
# same-day engine timestamp compares as EARLIER than any observation of that
# day. That is SC-165, and it is why nothing here compares raw strings.
BROKER_TS = "%Y-%m-%d %H:%M:%S"    # interface_broker._now() = SELECT datetime('now')
HARNESS_TS = "%Y-%m-%dT%H:%M:%SZ"  # norm_iso's canonical form = every observed_at

# The cwds the capture runs actually ran in — the fixtures record these paths
# internally (claude per-record `cwd`, kimi state.json `workDir`, opencode
# `session.directory`), so they are the probe's input, not a naming choice.
CLAUDE_SINGLE = "/tmp/lm-capture/claude-single"
CLAUDE_AB = "/tmp/lm-capture/claude-ab"
CLAUDE_BACK = "/tmp/lm-capture/claude"
CLAUDE_SUB = "/tmp/lm-capture/claude-sub"
CLAUDE_SYNTH_SKIP = "/home/j3d1/dos-arch/.sc-worktrees/pln1"
CLAUDE_SYNTH_ONLY = "/home/j3d1/dos-arch/.sc-worktrees/dev2"

KIMI_SINGLE = "/tmp/lm-capture/kimi-single"
KIMI_AB = "/tmp/lm-capture/kimi-ab"
KIMI_BACK = "/tmp/lm-capture/kimi"
KIMI_SUB = "/tmp/lm-capture/kimi-sub"

OC_SINGLE = "/tmp/lm-capture/oc-single"
OC_AB = "/tmp/lm-capture/oc-ab"
OC_BACK = "/tmp/lm-capture/oc-back"
OC_SUB = "/tmp/lm-capture/oc-sub"

# The second capture round (REV2 SC-166): TWO sessions of one worktree, the
# production shape every previous fixture is missing — each of those holds a
# single session, so "which session is the current one" was answered by
# default rather than by the code under test.
CLAUDE_TWO = "/tmp/lm-capture2/claude-two"
KIMI_TWO = "/tmp/lm-capture2/kimi-two"
OC_TWO = "/tmp/lm-capture2/oc-two"


class ProbeCase(unittest.TestCase):
    """Points every harness module at the vendored fixtures."""

    def setUp(self):
        self._saved = (p_claude.DATA_DIR, p_kimi.DATA_DIR, p_opencode.DB)
        p_claude.DATA_DIR = FIXTURES / "claude" / "projects"
        p_kimi.DATA_DIR = FIXTURES / "kimi" / "sessions"
        p_opencode.DB = FIXTURES / "opencode" / "opencode.db"
        live_model.cache_clear()

    def tearDown(self):
        p_claude.DATA_DIR, p_kimi.DATA_DIR, p_opencode.DB = self._saved
        live_model.cache_clear()

    def probe(self, harness, worktree, **kw):
        live_model.cache_clear()  # each assertion reads disk, never a neighbour's cache
        return live_model.probe(harness, worktree, **kw)


class LastRecordWins(ProbeCase):
    """The mid-session A->B fixtures: first != last, so inverting selection bites."""

    def test_claude_reports_the_model_of_the_last_assistant_message(self):
        r = self.probe("claude", CLAUDE_AB)
        self.assertEqual(r["last_model"], "claude-sonnet-5")
        self.assertEqual(r["verdict"], "ok")

    def test_kimi_reports_the_model_of_the_last_request(self):
        r = self.probe("kimi", KIMI_AB)
        self.assertEqual(r["last_model"], "kimi-code/kimi-for-coding")
        self.assertEqual(r["verdict"], "ok")

    def test_opencode_reports_the_model_of_the_last_assistant_message(self):
        r = self.probe("opencode", OC_AB)
        self.assertEqual(r["last_model"], "qwen3.5:397b")
        self.assertEqual(r["verdict"], "ok")

    def test_every_ab_fixture_actually_switches_model(self):
        """The premise the three tests above rest on.

        If a capture were ever re-taken and came back single-model, those
        assertions would still pass while proving nothing about ordering. So
        assert the FIXTURE's own property: it must contain at least two
        distinct model ids, and its first must differ from its last.
        """
        for label, models in (("claude", _claude_models(CLAUDE_AB)),
                              ("kimi", _kimi_models(KIMI_AB)),
                              ("opencode", _opencode_models(OC_AB))):
            with self.subTest(harness=label):
                self.assertGreaterEqual(len(set(models)), 2, models)
                self.assertNotEqual(models[0], models[-1], models)


class SwitchBack(ProbeCase):
    """A->B->A — the case flag #136 proved the analytics schema destroys."""

    def test_claude_switch_back_reports_the_returned_to_model(self):
        r = self.probe("claude", CLAUDE_BACK)
        self.assertEqual(r["last_model"], "claude-haiku-4-5-20251001")

    def test_kimi_switch_back_reports_the_returned_to_model(self):
        r = self.probe("kimi", KIMI_BACK)
        self.assertEqual(r["last_model"], "kimi-code/k3")

    def test_opencode_switch_back_reports_the_returned_to_model(self):
        r = self.probe("opencode", OC_BACK)
        self.assertEqual(r["last_model"], "gpt-oss:120b")

    def test_every_switch_back_fixture_really_returns_to_its_first_model(self):
        """Pins the fixtures as A->B->A rather than A->B.

        Without this, a fixture that silently lost its third turn would leave
        the three tests above asserting a plain mid-session switch under a
        name that claims more.
        """
        for label, models in (("claude", _claude_models(CLAUDE_BACK)),
                              ("kimi", _kimi_models(KIMI_BACK)),
                              ("opencode", _opencode_models(OC_BACK))):
            with self.subTest(harness=label):
                self.assertEqual(models[0], models[-1], models)
                self.assertTrue(any(m != models[0] for m in models), models)


class SingleModel(ProbeCase):
    def test_single_model_sessions(self):
        for harness, worktree, expect in (
                ("claude", CLAUDE_SINGLE, "claude-haiku-4-5-20251001"),
                ("kimi", KIMI_SINGLE, "kimi-code/k3"),
                ("opencode", OC_SINGLE, "gpt-oss:120b")):
            with self.subTest(harness=harness):
                r = self.probe(harness, worktree)
                self.assertEqual(r["last_model"], expect)
                self.assertEqual(r["verdict"], "ok")


class SubagentNoise(ProbeCase):
    """A subagent's model must never be reported as the session's.

    Each capture was driven so the subagent ran a DIFFERENT model from the one
    the main thread ends on — otherwise the assertion would hold for a probe
    that reads the wrong thread, and could not fail.
    """

    def test_claude_ignores_the_subagent_transcript(self):
        r = self.probe("claude", CLAUDE_SUB)
        self.assertEqual(r["last_model"], "claude-sonnet-5")
        self.assertNotIn("subagents", r["source"])

    def test_kimi_reads_only_the_main_agent_wire(self):
        r = self.probe("kimi", KIMI_SUB)
        self.assertEqual(r["last_model"], "kimi-code/kimi-for-coding")
        self.assertTrue(r["source"].endswith("agents/main/wire.jsonl"),
                        r["source"])

    def test_opencode_ignores_child_sessions(self):
        r = self.probe("opencode", OC_SUB)
        self.assertEqual(r["last_model"], "qwen3.5:397b")

    def test_each_subagent_fixture_uses_a_distinct_model(self):
        """The discriminating premise: sub model != main's final model."""
        claude_sub = _claude_models(CLAUDE_SUB, subagents=True)
        self.assertTrue(claude_sub, "no claude subagent transcript in fixture")
        self.assertNotIn("claude-sonnet-5", claude_sub)

        kimi_sub = _kimi_models(KIMI_SUB, agent="agent-0")
        self.assertTrue(kimi_sub)
        self.assertNotIn("kimi-code/kimi-for-coding", kimi_sub)

        oc_child = _opencode_models(OC_SUB, children=True)
        self.assertTrue(oc_child)
        self.assertNotIn("qwen3.5:397b", oc_child)


class SyntheticRecords(ProbeCase):
    """`<synthetic>` is a real claude model value — 50 records in the corpus.

    PLN1 ruling (c), sprint 45: treat it (and any non-real id) as a
    non-explicit record and keep walking back.
    """

    def test_synthetic_tail_is_skipped_for_the_real_model_before_it(self):
        r = self.probe("claude", CLAUDE_SYNTH_SKIP)
        self.assertEqual(r["last_model"], "claude-sonnet-5")
        self.assertEqual(r["verdict"], "ok")

    def test_a_session_with_only_synthetic_records_reports_nothing(self):
        r = self.probe("claude", CLAUDE_SYNTH_ONLY)
        self.assertIsNone(r["last_model"])
        self.assertEqual(r["verdict"], "none")

    def test_the_skip_fixture_really_ends_on_synthetic(self):
        models = _claude_models(CLAUDE_SYNTH_SKIP, raw=True)
        self.assertEqual(models[-1], "<synthetic>", models[-3:])
        self.assertIn("claude-sonnet-5", models)

    def test_no_acceptance_fixture_rests_on_a_synthetic_transition(self):
        """REV2's L1 on the 44-U0 scout (sprint 45): the scout's "60 mid-switch
        sessions" count only reaches 60 if `<synthetic>` is counted as a
        distinct model, so a switch fixture drawn from that pool could be a
        `<synthetic>` transition rather than a real operator switch — a test
        that looks like it proves switching while proving the placeholder bug.

        These fixtures come from deliberate capture runs, not that pool, so
        they are clean by construction. This asserts it rather than assuming
        it, and keeps it true if a fixture is ever re-captured.
        """
        for worktree in (CLAUDE_SINGLE, CLAUDE_AB, CLAUDE_BACK, CLAUDE_SUB):
            with self.subTest(worktree=worktree):
                self.assertNotIn("<synthetic>",
                                 _claude_models(worktree, raw=True))

    def test_placeholder_rule_is_the_shape_not_the_one_value(self):
        for value in ("<synthetic>", "<error>", "", "   ", None):
            self.assertTrue(live_model.placeholder(value), value)
        for value in ("claude-opus-5", "k3", "gpt-oss:120b",
                      "kimi-code/k3", "qwen3.5:397b"):
            self.assertFalse(live_model.placeholder(value), value)


class Verdicts(ProbeCase):
    def test_unsupported_harnesses_never_guess(self):
        for harness in ("codex", "vibe", "nonesuch", "", None):
            with self.subTest(harness=harness):
                r = live_model.probe(harness, CLAUDE_BACK)
                self.assertEqual(r["verdict"], "unsupported")
                self.assertIsNone(r["last_model"])

    def test_codex_stays_unsupported_even_with_a_real_rollout_present(self):
        """`unsupported` is a claim about the FORMAT, not about absence.

        codex's rollouts DO carry an explicit per-turn model
        (`turn_context.payload.model`), so the tempting bug is to read it and
        call codex supported. It is not: nothing in the format distinguishes a
        subagent turn from a main one (651 rollouts, zero agent-attribution
        keys), and doc 44 requires unsupported over sometimes-wrong. This pins
        that the verdict holds with a REAL codex rollout sitting on disk,
        naming the very worktree being probed — the fixture PLN1 kept in scope
        against the flag #174 revisit.
        """
        rollout = (FIXTURES / "codex/sessions/2026/07/25"
                   / "rollout-2026-07-25T11-18-45-019f98ff-c0b0-7b02-b91d-e3a7b66eb2ca.jsonl")
        head = json.loads(rollout.read_text().splitlines()[0])
        worktree = head["payload"]["cwd"]
        models = [json.loads(ln)["payload"]["model"]
                  for ln in rollout.read_text().splitlines()
                  if json.loads(ln).get("type") == "turn_context"]
        self.assertEqual(models, ["gpt-5.5"])  # a real, explicit model id

        r = live_model.probe("codex", worktree)
        self.assertEqual(r["verdict"], "unsupported")
        self.assertIsNone(r["last_model"])
        self.assertIsNone(r["source"])

    def test_no_transcript_yet_is_none(self):
        r = self.probe("claude", "/tmp/lm-capture/never-ran")
        self.assertEqual(r["verdict"], "none")
        self.assertIsNone(r["last_model"])


class Freshness(ProbeCase):
    """`stale` — and the format `active_since` actually arrives in (SC-165).

    Every value the probe compares against comes from the ENGINE, not from a
    harness: the Interface writes `last_human_input_at` through
    `interface_broker._now()`. So the timestamps here are built by SHIFTING a
    real observation and rendering it in the writer's own form — not by naming
    a far-future instant, which no run of this feature can produce and which
    passes on nothing but the year digits.

    Shifting rather than reading the clock is also what keeps these tests
    honest tomorrow: an `active_since` of "now" is on a later calendar DATE
    than the fixtures from the day after capture, and a date difference is the
    one case the broken string compare got right by accident.
    """

    def observation(self, worktree=CLAUDE_BACK):
        """A real observed_at from a real fixture — the thing being aged."""
        r = self.probe("claude", worktree)
        self.assertEqual(r["verdict"], "ok")
        self.assertIsNotNone(r["live_model_at"])
        return r["live_model_at"]

    def test_the_engine_writes_last_human_input_at_in_sqlite_datetime_form(self):
        """The premise every test in this class is built on, asserted against
        the writer itself: if the broker ever moves to ISO-Z, this goes red and
        says so, instead of the stale tests quietly testing nothing."""
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        written = interface_broker._now(con)
        datetime.strptime(written, BROKER_TS)  # exact parse: space, no Z
        self.assertNotIn("T", written)
        self.assertNotIn("Z", written)
        with self.assertRaises(ValueError):
            datetime.strptime(written, HARNESS_TS)

    def test_activity_after_the_observation_projects_stale(self):
        fresh = self.probe("claude", CLAUDE_BACK)
        later = self.probe("claude", CLAUDE_BACK,
                           active_since=_shift(fresh["live_model_at"], 60,
                                               BROKER_TS))
        self.assertEqual(later["verdict"], "stale")
        # stale still REPORTS the reading — the consumer needs it to decide,
        # and the value is not wrong, only possibly overtaken.
        self.assertEqual(later["last_model"], fresh["last_model"])

    def test_activity_before_the_observation_stays_ok(self):
        r = self.probe("claude", CLAUDE_BACK,
                       active_since=_shift(self.observation(), -60, BROKER_TS))
        self.assertEqual(r["verdict"], "ok")

    def test_the_same_instant_in_either_form_gets_the_same_verdict(self):
        """The defect itself. One instant, one minute after the observation,
        written the two ways the two sides of this comparison write it: the
        verdict is a fact about time and must not depend on which."""
        observed = self.observation()
        verdicts = {fmt: self.probe("claude", CLAUDE_BACK,
                                    active_since=_shift(observed, 60, fmt))
                    ["verdict"] for fmt in (BROKER_TS, HARNESS_TS)}
        self.assertEqual(verdicts[BROKER_TS], verdicts[HARNESS_TS], verdicts)
        self.assertEqual(verdicts[BROKER_TS], "stale")

    def test_an_unreadable_active_since_asserts_no_freshness(self):
        """Absent and unparseable are the same answer — `ok` without a
        freshness claim. Comparing against a value we cannot place in time
        would be a verdict invented from noise."""
        for since in (None, "", "not a timestamp", "yesterday"):
            with self.subTest(active_since=since):
                self.assertEqual(
                    self.probe("claude", CLAUDE_BACK,
                               active_since=since)["verdict"], "ok")


class Robustness(ProbeCase):
    """The route must survive every shape of bad transcript."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_transcript_truncated_mid_line_still_reports(self):
        """A live session caught mid-append: the tail line is a real prefix of
        real bytes, so it will not parse. The walk must step back over it, not
        give up and not raise."""
        src = next((FIXTURES / "claude" / "projects"
                    / "-tmp-lm-capture-claude-ab").glob("*.jsonl"))
        proj = self.tmp / "projects" / "-tmp-lm-capture-claude-ab"
        proj.mkdir(parents=True)
        raw = src.read_bytes()
        # cut 40 bytes into the final record — exactly what an interrupted
        # append leaves behind
        cut = raw.rfind(b"\n", 0, len(raw) - 1) + 1
        (proj / src.name).write_bytes(raw[:cut + 40])
        p_claude.DATA_DIR = self.tmp / "projects"

        r = self.probe("claude", CLAUDE_AB)
        self.assertEqual(r["last_model"], "claude-sonnet-5")

    def test_an_unreadable_transcript_is_none_not_an_exception(self):
        # Running as uid 0 in the sandbox, so a mode-000 file is still
        # readable; a DIRECTORY where a transcript is expected raises the same
        # OSError family for the same reason (the path cannot be read as a
        # file) and works at any uid.
        proj = self.tmp / "projects" / "-tmp-lm-capture-claude-ab"
        (proj / "broken.jsonl").mkdir(parents=True)
        p_claude.DATA_DIR = self.tmp / "projects"

        r = self.probe("claude", CLAUDE_AB)
        self.assertEqual(r["verdict"], "none")

    def test_a_probe_that_raises_is_contained_and_logged_once(self):
        seen = []

        def boom(_worktree):
            raise RuntimeError("format drift")

        original = p_claude.read
        p_claude.read = boom
        self.addCleanup(setattr, p_claude, "read", original)

        first = live_model.probe("claude", CLAUDE_BACK, log=seen.append)
        self.assertEqual(first["verdict"], "none")
        # Expire the READING cache only — `cache_clear()` would also drop the
        # log-once ledger, which is the thing under test.
        live_model._cache.clear()
        second = live_model.probe("claude", CLAUDE_BACK, log=seen.append)
        self.assertEqual(second["verdict"], "none")
        self.assertEqual(len(seen), 1, seen)

    def test_opencode_prefers_the_parent_when_a_child_session_is_newer(self):
        """The `parent_id IS NULL` guard on SESSION selection.

        In every real capture the parent session updates AFTER its child — the
        subagent returns and then the main thread answers — so no captured
        fixture can make the newest session be a subagent's, and dropping the
        guard leaves the whole suite green. The hazard is still real: a
        subagent that outlives its parent's last message (parent interrupted,
        long-running child) inverts that order, and the probe would then report
        the subagent's model as the session's.

        So this perturbs a COPY of the real fixture — only `time_updated`, the
        one field that decides the race — rather than asserting against a
        hand-authored database. Same category as the truncation case above: a
        real fixture placed in a real state the captures could not be made to
        reach on demand.
        """
        db = self.tmp / "opencode.db"
        shutil.copy(FIXTURES / "opencode" / "opencode.db", db)
        con = sqlite3.connect(db)
        with con:
            parent, updated = con.execute(
                "SELECT id, time_updated FROM session "
                "WHERE directory=? AND parent_id IS NULL", (OC_SUB,)).fetchone()
            changed = con.execute(
                "UPDATE session SET time_updated=? "
                "WHERE parent_id=?", (updated + 5000, parent)).rowcount
        con.close()
        self.assertEqual(changed, 1, "fixture must hold exactly one child session")
        p_opencode.DB = db

        r = self.probe("opencode", OC_SUB)
        self.assertEqual(r["last_model"], "qwen3.5:397b")
        self.assertTrue(r["source"].endswith(f"#{parent}"), r["source"])

    def test_a_missing_data_dir_is_none(self):
        p_claude.DATA_DIR = self.tmp / "does-not-exist"
        p_kimi.DATA_DIR = self.tmp / "does-not-exist"
        p_opencode.DB = self.tmp / "does-not-exist.db"
        for harness, worktree in (("claude", CLAUDE_BACK),
                                  ("kimi", KIMI_BACK),
                                  ("opencode", OC_BACK)):
            with self.subTest(harness=harness):
                self.assertEqual(
                    self.probe(harness, worktree)["verdict"], "none")


class WorktreeIsolation(ProbeCase):
    """One shell's model must never be another's."""

    def test_claude_project_dir_prefix_collision_is_resolved_by_cwd(self):
        """`_encode` is lossy and `-tmp-lm-capture-claude` is a PREFIX of the
        -ab/-sub/-single fixture dirs, so the prefilter matches all four. Only
        the per-record `cwd` separates them — and the newest file among them
        belongs to a DIFFERENT worktree, so a probe that trusted mtime alone
        would answer with its model."""
        by_worktree = {wt: self.probe("claude", wt)["last_model"]
                       for wt in (CLAUDE_BACK, CLAUDE_AB, CLAUDE_SUB,
                                  CLAUDE_SINGLE)}
        self.assertEqual(by_worktree[CLAUDE_AB], "claude-sonnet-5")
        self.assertEqual(by_worktree[CLAUDE_SUB], "claude-sonnet-5")
        self.assertEqual(by_worktree[CLAUDE_BACK], "claude-haiku-4-5-20251001")
        self.assertEqual(by_worktree[CLAUDE_SINGLE],
                         "claude-haiku-4-5-20251001")
        # and each answer came from its OWN project dir
        for wt in (CLAUDE_BACK, CLAUDE_AB, CLAUDE_SUB, CLAUDE_SINGLE):
            src = self.probe("claude", wt)["source"]
            self.assertIn(p_claude._encode(wt) + "/", src.replace("\\", "/"))

    def test_kimi_and_opencode_separate_worktrees(self):
        self.assertNotEqual(self.probe("kimi", KIMI_AB)["last_model"],
                            self.probe("kimi", KIMI_BACK)["last_model"])
        self.assertNotEqual(self.probe("opencode", OC_AB)["last_model"],
                            self.probe("opencode", OC_BACK)["last_model"])


class CurrentSessionSelection(ProbeCase):
    """Several sessions in ONE worktree — the probe must answer with the
    CURRENT one (REV2 SC-166).

    Every other fixture holds exactly one session per worktree, so the
    selection step could not be wrong: inverting newest-first in all three
    probes at once left the whole suite green. In production it is the normal
    case — a project dir accumulates one transcript per boot (8 in this
    shell's own) — and a regression there reports a DEAD session's model as
    the live one.

    claude and kimi select on file mtime, which git does not carry: a fresh
    checkout stamps every fixture file at checkout time, so the ordering under
    test would be whatever the runner's filesystem produced. These tests
    therefore copy the real transcripts and set the mtimes themselves, and
    assert the answer FOLLOWS the mtime by running both orders — the same
    category as the opencode `time_updated` perturbation below: real bytes,
    placed in a state the capture cannot be asked for on demand. opencode
    keeps its ordering field inside the database, so its fixture needs no
    such help.
    """

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_claude_answers_with_the_newest_transcript_of_the_worktree(self):
        sessions = _claude_session_models(CLAUDE_TWO)
        src = FIXTURES / "claude" / "projects" / p_claude._encode(CLAUDE_TWO)
        proj = self.tmp / "projects" / src.name
        shutil.copytree(src, proj)
        p_claude.DATA_DIR = self.tmp / "projects"

        for newest, expected in sessions.items():
            with self.subTest(newest=newest.name):
                _stamp_newest(proj / newest.name,
                              [proj / p.name for p in sessions])
                r = self.probe("claude", CLAUDE_TWO)
                self.assertEqual(r["last_model"], expected)
                self.assertTrue(r["source"].endswith(newest.name), r["source"])

    def test_kimi_answers_with_the_newest_session_of_the_workdir(self):
        sessions = _kimi_session_models(KIMI_TWO)
        src = FIXTURES / "kimi" / "sessions"
        base = self.tmp / "sessions"
        shutil.copytree(src, base)
        p_kimi.DATA_DIR = base

        wires = {sess: base / sess.relative_to(src) / "agents/main/wire.jsonl"
                 for sess in sessions}
        for sess, expected in sessions.items():
            with self.subTest(newest=sess.name):
                _stamp_newest(wires[sess], list(wires.values()))
                r = self.probe("kimi", KIMI_TWO)
                self.assertEqual(r["last_model"], expected)
                self.assertEqual(r["source"], str(wires[sess]))

    def test_opencode_answers_with_the_newest_session_of_the_directory(self):
        sessions = _opencode_session_models(OC_TWO)  # oldest -> newest
        newest_id, expected = sessions[-1]
        r = self.probe("opencode", OC_TWO)
        self.assertEqual(r["last_model"], expected)
        self.assertTrue(r["source"].endswith(f"#{newest_id}"), r["source"])

    def test_every_two_session_fixture_really_holds_a_discriminating_pair(self):
        """The premise: >1 session recorded against ONE worktree, and the
        newest answering a DIFFERENT model from the others. Without the second
        half the assertions above would hold for a probe that picks any
        session at all."""
        claude = list(_claude_session_models(CLAUDE_TWO).values())
        kimi = list(_kimi_session_models(KIMI_TWO).values())
        opencode = _opencode_session_models(OC_TWO)  # (id, model), oldest first
        for label, models in (("claude", claude), ("kimi", kimi)):
            with self.subTest(harness=label):
                # mtime decides which of these is current and the tests above
                # try BOTH, so every one of them must be discriminating.
                self.assertGreaterEqual(len(models), 2, models)
                self.assertEqual(len(set(models)), len(models), models)
        answered = [m for _, m in opencode if m]
        self.assertGreaterEqual(len(answered), 2, opencode)
        self.assertIsNotNone(opencode[-1][1], opencode)
        self.assertNotIn(opencode[-1][1], answered[:-1], opencode)


class Caching(ProbeCase):
    def test_repeat_calls_inside_the_ttl_do_not_re_read_disk(self):
        calls = []
        original = p_claude.read

        def counting(worktree):
            calls.append(worktree)
            return original(worktree)

        p_claude.read = counting
        self.addCleanup(setattr, p_claude, "read", original)
        live_model.cache_clear()

        first = live_model.probe("claude", CLAUDE_BACK)
        second = live_model.probe("claude", CLAUDE_BACK)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, calls)

    def test_the_cache_is_keyed_per_shell(self):
        live_model.cache_clear()
        a = live_model.probe("claude", CLAUDE_BACK)["last_model"]
        b = live_model.probe("claude", CLAUDE_AB)["last_model"]
        self.assertNotEqual(a, b)

    def test_a_cached_reading_still_re_evaluates_freshness(self):
        """The verdict depends on the CALLER's activity clock, which moves
        independently of the transcript. Caching the reading must not freeze
        the verdict computed from it."""
        live_model.cache_clear()
        first = live_model.probe("claude", CLAUDE_BACK)
        self.assertEqual(first["verdict"], "ok")
        self.assertEqual(
            live_model.probe("claude", CLAUDE_BACK,
                             active_since=_shift(first["live_model_at"], 60,
                                                 BROKER_TS))["verdict"],
            "stale")


class RouteProjection(ProbeCase):
    """interface_routes._live_model — the mapping the API actually serves."""

    def setUp(self):
        super().setUp()
        import interface_routes
        self.routes = interface_routes
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("CREATE TABLE interface_input_state "
                         "(session_id INTEGER, last_human_input_at TEXT)")

    def test_probe_result_is_renamed_onto_the_api_fields(self):
        got = self.routes._live_model(self.con, "claude", None,
                                      worktree=CLAUDE_BACK)
        self.assertEqual(
            got, {"live_model": "claude-haiku-4-5-20251001",
                  "live_model_at": got["live_model_at"],
                  "live_model_verdict": "ok"})
        self.assertIsNotNone(got["live_model_at"])

    def test_last_human_input_drives_the_stale_verdict(self):
        """End to end on the REAL column contents: `last_human_input_at` holds
        what `interface_broker._now()` writes (`Freshness` pins that form), one
        minute after this observation — the everyday case a rail poll sees, not
        a year no session can be in."""
        observed = self.routes._live_model(
            self.con, "claude", None, worktree=CLAUDE_BACK)["live_model_at"]
        self.con.execute("INSERT INTO interface_input_state VALUES (7, ?)",
                         (_shift(observed, 60, BROKER_TS),))
        got = self.routes._live_model(self.con, "claude", 7,
                                      worktree=CLAUDE_BACK)
        self.assertEqual(got["live_model_verdict"], "stale")
        self.assertEqual(got["live_model"], "claude-haiku-4-5-20251001")

    def test_no_harness_means_no_claim(self):
        got = self.routes._live_model(self.con, None, None,
                                      worktree=CLAUDE_BACK)
        self.assertEqual(got, {"live_model": None, "live_model_at": None,
                               "live_model_verdict": "none"})

    def test_a_failure_resolving_the_worktree_does_not_raise(self):
        got = self.routes._live_model(self.con, "claude", None,
                                      shortname=None, flavor=None)
        self.assertEqual(got["live_model_verdict"], "none")


# ----------------------------------------------------------------- helpers

def _shift(observed_at, seconds, fmt):
    """A real observation, moved `seconds` and rendered in `fmt`.

    The two forms are the point: an instant is one fact, and which side of the
    seam writes it must not change the verdict it produces.
    """
    dt = datetime.strptime(observed_at, HARNESS_TS) + timedelta(seconds=seconds)
    return dt.strftime(fmt)


def _stamp_newest(newest, paths, *, gap_s=600):
    """Make `newest` the most recently modified of `paths`.

    Selection-by-mtime cannot be asserted on checked-out fixtures at all: git
    records content, never mtimes, so a fresh clone stamps every file at
    checkout time and the ordering is the runner's, not the fixture's. The
    tests own the field they are testing.
    """
    base = 1_800_000_000
    for path in paths:
        os.utime(path, (base, base if path == newest else base - gap_s))


# ----------------------------------------------------------------- fixture readers
# Deliberately independent of the probe: these re-derive each fixture's model
# sequence straight from the bytes, so a bug in the probe cannot make the
# fixture-premise assertions agree with it.

def _claude_session_models(worktree):
    """{transcript path: its last explicit model} for one project dir."""
    proj = FIXTURES / "claude" / "projects" / p_claude._encode(worktree)
    out = {}
    for path in sorted(proj.glob("*.jsonl")):
        models = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = (rec.get("message") or {}).get("model")
            if model and not live_model.placeholder(model):
                models.append(model)
        if models:
            out[path] = models[-1]
    return out


def _kimi_session_models(worktree):
    """{session dir: its last explicit model} for one workDir."""
    out = {}
    for state_path in sorted((FIXTURES / "kimi" / "sessions").glob(
            "wd_*/session_*/state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("workDir") != worktree:
            continue
        models = _kimi_models(worktree, session=state_path.parent.name)
        if models:
            out[state_path.parent] = models[-1]
    return out


def _opencode_session_models(worktree):
    """[(session id, last explicit model | None)] for one directory,
    OLDEST-updated first — the ordering the probe has to get right."""
    con = sqlite3.connect(
        f"file:{FIXTURES / 'opencode' / 'opencode.db'}?mode=ro", uri=True)
    try:
        out = []
        for sid, in con.execute(
                "SELECT id FROM session WHERE directory=? AND parent_id IS NULL "
                "ORDER BY time_updated ASC, id ASC", (worktree,)):
            row = con.execute(
                "SELECT json_extract(data,'$.modelID') FROM message "
                "WHERE session_id=? AND json_extract(data,'$.role')='assistant' "
                "AND json_extract(data,'$.modelID') IS NOT NULL "
                "ORDER BY time_created DESC, id DESC LIMIT 1", (sid,)).fetchone()
            out.append((sid, row[0] if row else None))
    finally:
        con.close()
    return out


def _claude_models(worktree, subagents=False, raw=False):
    proj = (FIXTURES / "claude" / "projects" / p_claude._encode(worktree))
    paths = (sorted(proj.rglob("subagents/*.jsonl")) if subagents
             else sorted(proj.glob("*.jsonl")))
    out = []
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = (rec.get("message") or {}).get("model")
            if model and (raw or not live_model.placeholder(model)):
                out.append(model)
    return out


def _kimi_models(worktree, agent="main", session=None):
    base = FIXTURES / "kimi" / "sessions"
    out = []
    for state_path in sorted(base.glob("wd_*/session_*/state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("workDir") != worktree:
            continue
        if session is not None and state_path.parent.name != session:
            continue
        wire = state_path.parent / "agents" / agent / "wire.jsonl"
        if not wire.exists():
            continue
        for line in wire.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "llm.request" and rec.get("modelAlias"):
                out.append(rec["modelAlias"])
    return out


def _opencode_models(worktree, children=False):
    con = sqlite3.connect(
        f"file:{FIXTURES / 'opencode' / 'opencode.db'}?mode=ro", uri=True)
    try:
        where = ("s.parent_id IS NOT NULL" if children else "s.parent_id IS NULL")
        if children:
            rows = con.execute(
                "SELECT json_extract(m.data,'$.modelID') FROM session s "
                "JOIN message m ON m.session_id=s.id "
                "JOIN session p ON p.id=s.parent_id "
                "WHERE p.directory=? AND s.parent_id IS NOT NULL "
                "AND json_extract(m.data,'$.role')='assistant' "
                "ORDER BY m.time_created, m.id", (worktree,)).fetchall()
        else:
            rows = con.execute(
                "SELECT json_extract(m.data,'$.modelID') FROM session s "
                "JOIN message m ON m.session_id=s.id "
                f"WHERE s.directory=? AND {where} "
                "AND json_extract(m.data,'$.role')='assistant' "
                "ORDER BY m.time_created, m.id", (worktree,)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows if r[0]]


if __name__ == "__main__":
    unittest.main(verbosity=2)
