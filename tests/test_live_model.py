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
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
FIXTURES = ROOT / "tests" / "fixtures" / "live_model"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import live_model  # noqa: E402
from live_model import claude as p_claude  # noqa: E402
from live_model import kimi as p_kimi  # noqa: E402
from live_model import opencode as p_opencode  # noqa: E402

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

    def test_no_transcript_yet_is_none(self):
        r = self.probe("claude", "/tmp/lm-capture/never-ran")
        self.assertEqual(r["verdict"], "none")
        self.assertIsNone(r["last_model"])

    def test_activity_after_the_observation_projects_stale(self):
        fresh = self.probe("claude", CLAUDE_BACK)
        self.assertEqual(fresh["verdict"], "ok")
        later = self.probe("claude", CLAUDE_BACK,
                           active_since="2099-01-01T00:00:00Z")
        self.assertEqual(later["verdict"], "stale")
        # stale still REPORTS the reading — the consumer needs it to decide,
        # and the value is not wrong, only possibly overtaken.
        self.assertEqual(later["last_model"], fresh["last_model"])

    def test_activity_before_the_observation_stays_ok(self):
        r = self.probe("claude", CLAUDE_BACK,
                       active_since="2000-01-01T00:00:00Z")
        self.assertEqual(r["verdict"], "ok")


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
        self.assertEqual(
            live_model.probe("claude", CLAUDE_BACK)["verdict"], "ok")
        self.assertEqual(
            live_model.probe("claude", CLAUDE_BACK,
                             active_since="2099-01-01T00:00:00Z")["verdict"],
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
        self.con.execute("INSERT INTO interface_input_state VALUES (7, ?)",
                         ("2099-01-01T00:00:00Z",))
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


# ----------------------------------------------------------------- fixture readers
# Deliberately independent of the probe: these re-derive each fixture's model
# sequence straight from the bytes, so a bug in the probe cannot make the
# fixture-premise assertions agree with it.

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


def _kimi_models(worktree, agent="main"):
    base = FIXTURES / "kimi" / "sessions"
    out = []
    for state_path in base.glob("wd_*/session_*/state.json"):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("workDir") != worktree:
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
