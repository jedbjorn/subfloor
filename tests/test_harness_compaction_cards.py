"""Feature #95, spec #275 (task #925): the Harness cards' compaction routes,
card data and the modified cards at the top of the Shells view's Harness tab.

Hermetic. The API half runs ``server.dispatch_http`` (the browser gate
included) over a temporary engine database built from schema + migrations.
The card-data half seeds an event log whose every figure is computed by hand
in the comments beside it. The page half runs the real card code of app.js
under node against a minimal fake DOM, in the pattern of
``tests/test_shells_ui_contract.py``.

Acceptance criteria (spec #275 table):
* AC3  ``OperatorOnlyRoutesTest``
* AC12 ``CardDataTest``
* AC13 ``test_cards_*`` (DOM-level checks; the FnB verifies in the browser)
* AC15 ``test_cards_tooltips_are_exact_on_the_four_compacting_cards``
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_gate
import compaction_cards
import compaction_settings
import server
from signin_fixture import sign_in

APP = (ENGINE / "ui" / "app.js").read_text()
STYLE = (ENGINE / "ui" / "style.css").read_text()
CARDS_JS = APP[APP.index("async function renderConversionStatus("):
               APP.index("async function renderShells(root)")]
SHELL_RENDER = APP[APP.index("async function renderShells(root)"):
                   APP.index("// Default Models — the flavor_defaults")]

SHELL_TOKEN = "shell-token"
CLI_TOKEN = "sccli_cards-test"
SAME = "http://127.0.0.1:8800"
NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
ENV_NAMES = ("SC_CHAT_COMPACT_WINDOW_CLAUDE", "SC_CHAT_COMPACT_WINDOW_CODEX")

AUTO_TIP = ("Compacts a chat when its context reaches the trigger size, "
            "checked after each turn.")
IDLE_TIP = ("Compacts a large chat that has gone quiet, before the provider's "
            "cache expires, so your next message is cheap.")


def build_db(path: Path) -> None:
    source = sqlite3.connect(":memory:")
    source.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        source.executescript(migration.read_text())
    source.execute(
        "INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)")
    source.execute(
        "INSERT INTO shells (shell_id,display_name,system_prompt,flavor,shortname,"
        "api_key) VALUES (1,'Dev','x','dev','dev',?)", (SHELL_TOKEN,))
    source.commit()
    target = sqlite3.connect(path)
    source.backup(target)
    target.close()
    source.close()


class EngineDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "engine.db"
        build_db(self.db_path)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self._chat = 0

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    # ── seeding ────────────────────────────────────────────────────────────
    def chat(self, con, harness: str) -> str:
        self._chat += 1
        conversation_id = f"cv_{self._chat:032x}"
        con.execute(
            "INSERT INTO conversations (conversation_id,shell_id,owner_user_id,"
            "harness,effort,worktree,state,title,starred,creation_idempotency_key,"
            "creation_request_hash,created_at,last_activity_at,closed_at) "
            "VALUES (?,1,1,?,'high','/tmp/wt','closed',?,0,?,?,"
            "'2026-10-01 00:00:00','2026-11-13 00:00:00','2026-11-13 00:00:00')",
            (conversation_id, harness, f"chat {self._chat}",
             f"create-{self._chat}", f"hash-{self._chat}"),
        )
        return conversation_id

    def event(self, con, conversation_id: str, event_type: str, payload: dict,
              created_at: str) -> None:
        sequence = con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        con.execute(
            "INSERT INTO conversation_events (conversation_id,sequence,event_type,"
            "payload,created_at) VALUES (?,?,?,?,?)",
            (conversation_id, sequence, event_type, json.dumps(payload), created_at),
        )

    def message(self, con, conversation_id: str, created_at: str) -> None:
        count = con.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        con.execute(
            "INSERT INTO conversation_messages (conversation_id,sender_kind,"
            "sender_ref,message_kind,body,idempotency_key,request_hash,state,"
            "created_at,completed_at) "
            "VALUES (?,'user','1','prompt','hello',?,'h','completed',?,?)",
            (conversation_id, f"msg-{count}", created_at, created_at),
        )


def claude_request(created_at, ctx, *, uncached, read, write, output=100):
    return ("usage", {
        "scope": "request", "context_tokens": ctx,
        "tokens": {"input_tokens": uncached, "cache_read_input_tokens": read,
                   "cache_creation_input_tokens": write, "output_tokens": output},
    }, created_at)


# ── AC3: operator-only routes, validation, refusals ─────────────────────────

class OperatorOnlyRoutesTest(EngineDbCase):
    ROUTES = (
        ("GET", "/api/harnesses/compaction", None),
        ("PUT", "/api/harnesses/claude/compaction", {"trigger_tokens": 180_000}),
        ("GET", "/api/harnesses/claude/compaction-data?days=7", None),
    )

    def setUp(self) -> None:
        super().setUp()
        self.browser = sign_in(self)
        patcher = mock.patch.object(browser_gate, "CLI_TOKEN", CLI_TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, method, path, *, body=None, token=None, origin=SAME,
                cookie=True):
        raw = json.dumps(body).encode() if body is not None else b""
        lines = ["Host: 127.0.0.1:8800", f"Content-Length: {len(raw)}"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        elif cookie:
            lines.append(f"Cookie: {self.browser.cookie}")
        if origin:
            lines += [f"Origin: {origin}", "Sec-Fetch-Site: same-origin"]
        with mock.patch.object(server, "db", side_effect=self.connect):
            status, _headers, out = server.dispatch_http(
                method, path, "\r\n".join(lines), raw)
        return status, json.loads(out)

    def saved_rows(self):
        with self.connect() as con:
            return [tuple(row) for row in con.execute(
                "SELECT harness,auto_enabled,trigger_tokens,idle_after_minutes,"
                "idle_min_tokens FROM chat_compaction_settings ORDER BY harness")]

    def test_shell_key_and_cli_token_get_403_on_all_three_routes(self):
        for token in (SHELL_TOKEN, CLI_TOKEN):
            for method, path, body in self.ROUTES:
                status, out = self.request(method, path, body=body, token=token)
                self.assertEqual(status, 403, (token, method, path, out))
                self.assertEqual(out["error"]["code"], "fnb_operator_required")
                self.assertIn("harness settings", out["error"]["message"])
        self.assertEqual(self.saved_rows(), [])

    def test_signed_out_is_refused(self):
        for method, path, body in self.ROUTES:
            status, out = self.request(method, path, body=body, cookie=False)
            self.assertEqual(status, 401, (method, path))
            self.assertEqual(out["error"]["code"], "sign_in_required")

    def test_cross_origin_save_is_refused(self):
        status, out = self.request(
            "PUT", "/api/harnesses/claude/compaction",
            body={"trigger_tokens": 180_000}, origin="https://attacker.example")
        self.assertEqual(status, 403)
        self.assertEqual(out["error"]["code"], "same_origin_required")
        self.assertEqual(self.saved_rows(), [])

    def test_operator_reads_the_four_cards_with_sources_and_what_applies(self):
        os.environ["SC_CHAT_COMPACT_WINDOW_CODEX"] = "off"
        status, out = self.request("GET", "/api/harnesses/compaction")
        self.assertEqual(status, 200, out)
        self.assertTrue(out["install_wide"])
        cards = {card["harness"]: card for card in out["harnesses"]}
        self.assertEqual([c["harness"] for c in out["harnesses"]],
                         ["claude", "codex", "kimi", "opencode"])
        self.assertEqual(cards["claude"]["idle_after_minutes"], 50)
        self.assertEqual(cards["claude"]["sources"]["trigger_tokens"], "default")
        self.assertFalse(cards["codex"]["auto_enabled"])
        self.assertEqual(cards["codex"]["sources"]["auto_enabled"], "environment")
        self.assertEqual(cards["codex"]["idle_choices"], [5, 10, 15, 20, 25, 30, 35, 40])
        self.assertEqual(cards["kimi"]["idle_choices"][-1], 25)
        self.assertEqual(cards["opencode"]["idle_choices"][-1], 55)
        self.assertFalse(cards["opencode"]["cache_life_measured"])
        self.assertEqual(cards["claude"]["week_compactions"], 0)
        claude_parts = {p["part"]: p for p in cards["claude"]["applies"]}
        self.assertTrue(claude_parts["trigger_passed"]["available"])
        self.assertIn("100,000 to 1,000,000", claude_parts["trigger_passed"]["note"])
        self.assertTrue(claude_parts["idle"]["available"])
        opencode_parts = {p["part"]: p for p in cards["opencode"]["applies"]}
        self.assertFalse(opencode_parts["trigger_passed"]["available"])
        self.assertTrue(opencode_parts["after_turn"]["available"])
        # Kimi has no confirmed headless trigger until task #924's gate passes.
        if not cards["kimi"]["manual_trigger"]:
            self.assertTrue(cards["kimi"]["unavailable_reason"].startswith(
                "not available: no headless compaction trigger in Kimi"))
            kimi_parts = {p["part"]: p for p in cards["kimi"]["applies"]}
            self.assertFalse(kimi_parts["idle"]["available"])

    def test_save_answers_the_card_and_the_first_save_writes_the_whole_row(self):
        os.environ["SC_CHAT_COMPACT_WINDOW_CLAUDE"] = "180000"
        status, out = self.request("PUT", "/api/harnesses/claude/compaction",
                                   body={"idle_after_minutes": 30})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["idle_after_minutes"], 30)
        # The environment trigger in effect is carried into the saved row.
        self.assertEqual(out["trigger_tokens"], 180_000)
        self.assertEqual(set(out["sources"].values()), {"saved"})
        self.assertIn("applies", out)
        self.assertEqual(self.saved_rows(), [("claude", 1, 180_000, 30, 125_000)])
        with self.connect() as con:
            self.assertEqual(con.execute(
                "SELECT updated_by_user_id FROM chat_compaction_settings").fetchone()[0], 1)
        status, out = self.request("PUT", "/api/harnesses/claude/compaction",
                                   body={"idle_after_minutes": None})
        self.assertEqual(status, 200, out)
        self.assertIsNone(out["idle_after_minutes"])
        status, out = self.request("GET", "/api/harnesses/compaction")
        claude = next(c for c in out["harnesses"] if c["harness"] == "claude")
        self.assertIsNone(claude["idle_after_minutes"])
        self.assertEqual(claude["sources"]["idle_after_minutes"], "saved")

    def test_refused_values_leave_the_saved_value_unchanged(self):
        status, _ = self.request("PUT", "/api/harnesses/codex/compaction",
                                 body={"trigger_tokens": 200_000})
        self.assertEqual(status, 200)
        before = self.saved_rows()
        refusals = (
            ("codex", {"idle_after_minutes": 45}, 422, "COMPACTION_SETTING_INVALID"),
            ("kimi", {"idle_after_minutes": 30}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"idle_after_minutes": 7}, 422, "COMPACTION_SETTING_INVALID"),
            # The engine floor: never an engine compaction under 90,000.
            ("codex", {"trigger_tokens": 89_999}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"trigger_tokens": "180000"}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"trigger_tokens": 150_000.5}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"idle_min_tokens": 89_999}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"auto_enabled": "yes"}, 422, "COMPACTION_SETTING_INVALID"),
            ("codex", {"cache_life_minutes": 90}, 422, "COMPACTION_SETTING_UNKNOWN"),
            ("codex", {}, 400, "COMPACTION_SETTINGS_EMPTY"),
            ("vibe", {"trigger_tokens": 180_000}, 404, "COMPACTION_HARNESS_UNKNOWN"),
            ("nosuch", {"trigger_tokens": 180_000}, 404, "COMPACTION_HARNESS_UNKNOWN"),
        )
        for harness, body, expected, code in refusals:
            status, out = self.request("PUT", f"/api/harnesses/{harness}/compaction",
                                       body=body)
            self.assertEqual(status, expected, (harness, body, out))
            self.assertEqual(out["error"]["code"], code, (harness, body))
            self.assertEqual(self.saved_rows(), before, (harness, body))
        # A refusal names the field next to which the card shows it.
        status, out = self.request("PUT", "/api/harnesses/codex/compaction",
                                   body={"trigger_tokens": 89_999})
        self.assertEqual(out["error"]["details"], {"field": "trigger_tokens"})
        self.assertIn("at least 90,000", out["error"]["message"])
        status, out = self.request("PUT", "/api/harnesses/codex/compaction",
                                   body={"trigger_tokens": 90_000, "idle_min_tokens": 90_000})
        self.assertEqual(status, 200, out)

    def test_cards_show_the_value_in_effect_when_raised_to_the_floor(self):
        os.environ["SC_CHAT_COMPACT_WINDOW_CLAUDE"] = "5000"
        status, out = self.request("GET", "/api/harnesses/compaction")
        self.assertEqual(status, 200)
        claude = next(c for c in out["harnesses"] if c["harness"] == "claude")
        self.assertEqual((claude["trigger_tokens"], claude["effective_trigger"]), (5000, 90_000))
        self.assertTrue(claude["trigger_raised_to_floor"])
        self.assertEqual(claude["min_tokens"], 90_000)

    def test_card_data_route_checks_its_period_and_harness(self):
        status, out = self.request("GET", "/api/harnesses/claude/compaction-data")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["days"], 7)
        status, out = self.request("GET", "/api/harnesses/claude/compaction-data?days=30")
        self.assertEqual((status, out["days"]), (200, 30))
        for days in ("14", "x", "-7"):
            status, out = self.request(
                "GET", f"/api/harnesses/claude/compaction-data?days={days}")
            self.assertEqual(status, 400, days)
            self.assertEqual(out["error"]["code"], "COMPACTION_DAYS_INVALID")
        status, out = self.request("GET", "/api/harnesses/vibe/compaction-data")
        self.assertEqual(status, 404)
        self.assertEqual(out["error"]["code"], "COMPACTION_HARNESS_UNKNOWN")


# ── AC12: card data from a seeded event log, hand-computed ──────────────────

class CardDataTest(EngineDbCase):
    """Window: 7 days before NOW (2026-11-13 12:00 UTC), from 2026-11-06 12:00.

    Claude defaults: trigger 250k, wait 50 min, idle size 125k, cache life
    60 min, so the gap buckets are under 5, 5-50, 50-60 and over 60 min.
    """

    def seed_claude(self) -> None:
        with self.connect() as con:
            a = self.chat(con, "claude")
            b = self.chat(con, "claude")
            codex = self.chat(con, "codex")
            for event_type, payload, at in (
                # r0, before the window: only the predecessor of r1.
                claude_request("2026-11-05 12:00:00", 100_000,
                               uncached=1_000, read=99_000, write=0),
                # r1: gap from r0 > 60 min -> over 60; read 0 of 130k: cold.
                # Cold return (gap >= 60, 130k >= 125k): re-reads 2k + 128k.
                claude_request("2026-11-10 10:00:00", 130_000,
                               uncached=2_000, read=0, write=128_000),
                # r2: gap 2 min -> under 5; 130k of 132k read: warm.
                claude_request("2026-11-10 10:02:00", 132_000,
                               uncached=1_000, read=130_000, write=1_000),
                # r3: gap 28 min -> 5-50; 200k of 260k read: warm; >= trigger.
                claude_request("2026-11-10 10:30:00", 260_000,
                               uncached=500, read=200_000, write=59_500),
                # A whole-turn total and a delegated agent's request: neither
                # is a request of the chat.
                ("usage", {"scope": "turn", "tokens": {"input_tokens": 9}},
                 "2026-11-10 10:31:00"),
                ("usage", {"scope": "subagent", "tokens": {"input_tokens": 9}},
                 "2026-11-10 10:31:00"),
                # The harness compacted on its own (no cause, no request).
                ("context.compacted", {"trigger": "auto", "tokens_before": 260_000,
                                       "tokens_after": 40_000}, "2026-11-10 10:31:00"),
                # r4: gap 55 min -> 50-60; 40k of 100k read: cold.
                claude_request("2026-11-10 11:25:00", 100_000,
                               uncached=100, read=40_000, write=59_900),
                ("context.compacted", {"cause": "threshold", "tokens_before": 255_000,
                                       "tokens_after": 30_000}, "2026-11-11 09:00:00"),
                # A record from before causes existed: the button's.
                ("context.compacted", {"trigger": "manual", "request_message_id": 7,
                                       "tokens_before": 200_000, "tokens_after": 50_000},
                 "2026-11-09 09:00:00"),
                ("context.compacted", {"cause": "idle", "tokens_before": 140_000,
                                       "tokens_after": 29_000}, "2026-11-12 12:00:00"),
                # The engine's own compaction requests: the compaction cost.
                ("usage", {"scope": "compaction", "tokens": {
                    "input_tokens": 1_000, "cache_read_input_tokens": 120_000,
                    "cache_creation_input_tokens": 5_000, "output_tokens": 4_000}},
                 "2026-11-11 09:00:00"),
                ("usage", {"scope": "compaction", "tokens": {
                    "input_tokens": 500, "cache_read_input_tokens": 100_000,
                    "cache_creation_input_tokens": 2_000, "output_tokens": 3_000}},
                 "2026-11-12 12:00:00"),
                # Before the window: counted nowhere.
                ("context.compacted", {"cause": "harness", "tokens_before": 1,
                                       "tokens_after": 1}, "2026-11-01 00:00:00"),
                ("usage", {"scope": "compaction", "tokens": {
                    "input_tokens": 99, "cache_read_input_tokens": 99,
                    "cache_creation_input_tokens": 99, "output_tokens": 99}},
                 "2026-11-01 00:00:00"),
            ):
                self.event(con, a, event_type, payload, at)
            # Chat A is written to 8 h after its idle compaction: paid off.
            self.message(con, a, "2026-11-12 20:00:00")
            for event_type, payload, at in (
                # r5: the chat's first request: no gap.
                claude_request("2026-11-12 09:00:00", 50_000,
                               uncached=50_000, read=0, write=0),
                # r6: gap 120 min -> over 60; 39k of 40k read: warm; under the
                # idle size, so not a cold return.
                claude_request("2026-11-12 11:00:00", 40_000,
                               uncached=1_000, read=39_000, write=0),
                # Idle compaction with no message for 24 h (one at 25 h).
                ("context.compacted", {"cause": "idle", "tokens_before": 130_000,
                                       "tokens_after": 31_000}, "2026-11-11 08:00:00"),
                # Idle compaction 6 h ago: its 24 h are still running.
                ("context.compacted", {"cause": "idle", "tokens_before": 128_000,
                                       "tokens_after": 30_000}, "2026-11-13 06:00:00"),
            ):
                self.event(con, b, event_type, payload, at)
            # 25 h after the 11-11 08:00 idle compaction: too late to count.
            self.message(con, b, "2026-11-12 09:00:00")
            # Another harness's chat is never counted on Claude's card.
            self.event(con, codex, "context.compacted", {"cause": "idle"},
                       "2026-11-12 12:00:00")
            event_type, payload, at = claude_request(
                "2026-11-12 12:00:00", 999_999, uncached=1, read=1, write=1)
            self.event(con, codex, event_type, payload, at)

    def data(self, harness, days=7):
        with self.connect() as con:
            return compaction_cards.card_data(con, harness, days, now=NOW)

    def test_each_claude_figure_equals_the_hand_computed_value(self):
        self.seed_claude()
        d = self.data("claude")
        # In the window: r1..r6, in two chats.
        self.assertEqual(d["chats"], 2)
        self.assertEqual(d["requests"], 6)
        self.assertEqual(d["request_time"], "created_at")
        # Sizes 40k, 50k, 100k, 130k, 132k, 260k: median (100k + 130k) / 2.
        self.assertEqual(d["context"]["median_tokens"], 115_000)
        self.assertEqual(d["context"]["readings"], 6)
        self.assertEqual(d["context"]["at_or_above_trigger"], 1)
        self.assertAlmostEqual(d["context"]["share_at_or_above_trigger"], 1 / 6)
        # Reads 0 + 130k + 200k + 40k + 0 + 39k = 409k of
        # 130k + 132k + 260k + 100k + 50k + 40k = 712k.
        self.assertTrue(d["warm_share"]["reported"])
        self.assertEqual(d["warm_share"]["cache_read"], 409_000)
        self.assertEqual(d["warm_share"]["input_side"], 712_000)
        self.assertAlmostEqual(d["warm_share"]["share"], 409_000 / 712_000)
        buckets = {(b["from_minutes"], b["to_minutes"]): b
                   for b in d["cold_by_gap"]["buckets"]}
        self.assertTrue(d["cold_by_gap"]["reported"])
        self.assertEqual(list(buckets), [(0, 5), (5, 50), (50, 60), (60, None)])
        self.assertEqual((buckets[(0, 5)]["requests"], buckets[(0, 5)]["cold"]), (1, 0))
        self.assertEqual((buckets[(5, 50)]["requests"], buckets[(5, 50)]["cold"]), (1, 0))
        self.assertEqual((buckets[(50, 60)]["requests"], buckets[(50, 60)]["cold"]), (1, 1))
        self.assertEqual((buckets[(60, None)]["requests"], buckets[(60, None)]["cold"]), (2, 1))
        self.assertEqual(buckets[(60, None)]["rate"], 0.5)
        self.assertEqual(buckets[(0, 5)]["rate"], 0.0)
        # Cold returns: r1 only, re-reading 2k uncached + 128k written.
        self.assertEqual(d["cold_returns"],
                         {"reported": True, "count": 1, "tokens_reread": 130_000})
        by_cause = d["compactions"]["by_cause"]
        self.assertEqual(d["compactions"]["total"], 6)
        self.assertEqual(by_cause["harness"],
                         {"count": 1, "median_before": 260_000, "median_after": 40_000})
        self.assertEqual(by_cause["threshold"],
                         {"count": 1, "median_before": 255_000, "median_after": 30_000})
        self.assertEqual(by_cause["operator"],
                         {"count": 1, "median_before": 200_000, "median_after": 50_000})
        # Befores 140k, 130k, 128k; afters 29k, 31k, 30k.
        self.assertEqual(by_cause["idle"],
                         {"count": 3, "median_before": 130_000, "median_after": 30_000})
        self.assertEqual(d["idle_payoff"],
                         {"followed": 1, "not_followed": 1, "pending": 1})
        # 1k + 0.5k input, 120k + 100k read, 5k + 2k written, 4k + 3k output.
        cost = d["compaction_cost"]
        self.assertEqual(cost["requests"], 2)
        self.assertTrue(cost["reported"])
        self.assertEqual((cost["input"], cost["cache_read"], cost["cache_write"],
                          cost["output"], cost["total"]),
                         (1_500, 220_000, 7_000, 7_000, 235_500))

    def test_the_summary_counts_this_weeks_compactions_per_harness(self):
        self.seed_claude()
        with self.connect() as con:
            out = compaction_cards.cards(con, now=NOW)
        counts = {card["harness"]: card["week_compactions"] for card in out["harnesses"]}
        self.assertEqual(counts, {"claude": 6, "codex": 1, "kimi": 0, "opencode": 0})

    def test_the_30_day_period_reaches_further_back(self):
        self.seed_claude()
        d = self.data("claude", 30)
        # r0 (11-05) joins; it is the chat's first request, so no new gap.
        self.assertEqual(d["requests"], 7)
        self.assertEqual(d["compactions"]["by_cause"]["harness"]["count"], 2)
        self.assertEqual(d["compaction_cost"]["requests"], 3)

    def test_kimi_gaps_use_requested_at_not_ingest_time(self):
        with self.connect() as con:
            k = self.chat(con, "kimi")
            epoch_ms = int(datetime(2026, 11, 12, 10, 3, tzinfo=timezone.utc).timestamp() * 1000)
            for requested_at, ingest, ctx, uncached, read in (
                # Ingested together at 10:40 after the run ended; requested
                # 10:00 and 10:03 (epoch ms, as the wire's ``time``).
                ("2026-11-12 10:00:00", "2026-11-12 10:40:00", 300_000, 1_000, 299_000),
                (epoch_ms, "2026-11-12 10:40:00", 301_000, 2_000, 299_000),
                # Requested 10:40, 37 min after 10:03 (ingest gap: 50 min):
                # over Kimi's 30-minute cache life, read 0: a cold return.
                ("2026-11-12T10:40:00+00:00", "2026-11-12 11:30:00", 302_000, 302_000, 0),
            ):
                self.event(con, k, "usage", {
                    "scope": "request", "context_tokens": ctx, "requested_at": requested_at,
                    "tokens": {"input_tokens": uncached, "cache_read_tokens": read,
                               "cache_write_tokens": 0, "output_tokens": 10},
                }, ingest)
        d = self.data("kimi")
        self.assertEqual(d["request_time"], "requested_at")
        self.assertEqual(d["requests"], 3)
        buckets = {(b["from_minutes"], b["to_minutes"]): b
                   for b in d["cold_by_gap"]["buckets"]}
        # Kimi: wait 25, cache life 30.
        self.assertEqual(list(buckets), [(0, 5), (5, 25), (25, 30), (30, None)])
        self.assertEqual((buckets[(0, 5)]["requests"], buckets[(0, 5)]["cold"]), (1, 0))
        self.assertEqual(buckets[(5, 25)]["requests"], 0)
        self.assertEqual(buckets[(25, 30)]["requests"], 0)
        self.assertEqual((buckets[(30, None)]["requests"], buckets[(30, None)]["cold"]), (1, 1))
        self.assertEqual(d["cold_returns"],
                         {"reported": True, "count": 1, "tokens_reread": 302_000})
        self.assertAlmostEqual(d["warm_share"]["share"], 598_000 / 903_000)

    def test_kimi_without_requested_at_reports_no_gaps(self):
        with self.connect() as con:
            k = self.chat(con, "kimi")
            for at in ("2026-11-12 10:00:00", "2026-11-12 11:00:00"):
                self.event(con, k, "usage", {
                    "scope": "request", "context_tokens": 200_000,
                    "tokens": {"input_tokens": 200_000, "cache_read_tokens": 0,
                               "cache_write_tokens": 0, "output_tokens": 1},
                }, at)
        d = self.data("kimi")
        self.assertEqual(d["requests"], 2)
        self.assertFalse(d["cold_by_gap"]["reported"])
        self.assertEqual(d["cold_by_gap"]["note"], "request times not reported")
        self.assertEqual(d["cold_by_gap"]["untimed_requests"], 2)
        self.assertEqual(d["cold_returns"],
                         {"reported": False, "count": None, "tokens_reread": None})
        # The split itself is reported, so the warm share still is.
        self.assertEqual(d["warm_share"]["share"], 0.0)

    def test_a_harness_without_a_split_shows_not_reported(self):
        with self.connect() as con:
            o = self.chat(con, "opencode")
            for at, ctx in (("2026-11-12 10:00:00", 130_000),
                            ("2026-11-12 11:30:00", 140_000)):
                # What the OpenCode adapter stores: numeric totals only.
                self.event(con, o, "usage", {
                    "scope": "request", "context_tokens": ctx,
                    "tokens": {"input": 2_000, "output": 300, "reasoning": 0},
                }, at)
            self.event(con, o, "usage", {"scope": "compaction",
                                         "tokens": {"input": 9, "output": 9}},
                       "2026-11-12 12:00:00")
        d = self.data("opencode")
        self.assertEqual(d["requests"], 2)
        self.assertEqual(d["context"]["median_tokens"], 135_000)
        self.assertFalse(d["warm_share"]["reported"])
        self.assertIsNone(d["warm_share"]["share"])
        self.assertFalse(d["cold_by_gap"]["reported"])
        self.assertEqual(d["cold_by_gap"]["note"], "OpenCode reports no cache split")
        # The gap is known (90 min, past OpenCode's 60): one cold return,
        # whose tokens re-read are not reported.
        self.assertEqual(d["cold_returns"],
                         {"reported": True, "count": 1, "tokens_reread": None})
        self.assertFalse(d["compaction_cost"]["reported"])
        self.assertEqual(d["compaction_cost"]["requests"], 1)
        self.assertIsNone(d["compaction_cost"]["total"])

    def test_codex_input_already_counts_the_cached_part(self):
        with self.connect() as con:
            c = self.chat(con, "codex")
            self.event(con, c, "usage", {
                "scope": "request", "context_tokens": 100_000,
                "tokens": {"inputTokens": 100_000, "cachedInputTokens": 98_000,
                           "outputTokens": 500, "totalTokens": 100_500},
            }, "2026-11-12 10:00:00")
        d = self.data("codex")
        self.assertEqual(d["warm_share"]["cache_read"], 98_000)
        self.assertEqual(d["warm_share"]["input_side"], 100_000)
        self.assertEqual(compaction_cards.token_split("codex", {"tokens": {
            "inputTokens": 100_000, "cachedInputTokens": 98_000,
            "outputTokens": 500}}),
            {"input": 2_000, "cache_read": 98_000, "cache_write": None, "output": 500})

    def test_idle_off_merges_the_middle_gap_buckets(self):
        with self.connect() as con:
            compaction_settings.save(con, "claude", {"idle_after_minutes": None})
        d = self.data("claude")
        self.assertEqual([(b["from_minutes"], b["to_minutes"])
                          for b in d["cold_by_gap"]["buckets"]],
                         [(0, 5), (5, 60), (60, None)])

    def test_the_route_answers_the_same_figures(self):
        self.seed_claude()
        browser = sign_in(self)
        lines = ["Host: 127.0.0.1:8800", f"Cookie: {browser.cookie}"]
        with mock.patch.object(server, "db", side_effect=self.connect):
            status, _headers, out = server.dispatch_http(
                "GET", "/api/harnesses/claude/compaction-data?days=7",
                "\r\n".join(lines), b"")
        self.assertEqual(status, 200)
        body = json.loads(out)
        # The route computes at the real now: the seeded week is in its past,
        # so only the shape is compared here; the figures are pinned above.
        self.assertEqual(set(body), set(self.data("claude")))


# ── AC13 / AC15: the modified cards, under node against a fake DOM ──────────

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

FAKE_DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this.className = ""; this._text = ""; this.isConnected = true;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (child) => typeof child === "string" ? child : child.textContent).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids) node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
const store = {};
globalThis.localStorage = {
  getItem: (key) => (key in store ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
};
const toasts = [];
function toast(message) { toasts.push(message); }
function all(root, predicate, found = []) {
  if (predicate(root)) found.push(root);
  for (const child of root.children || []) if (child?.nodeType === 1) all(child, predicate, found);
  return found;
}
const has = (cls) => (node) => (node.className || "").split(" ").includes(cls);
const choices = (life) => [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].filter((m) => m < life);
const cardFor = (harness, overrides = {}) => {
  const life = { claude: 60, codex: 45, kimi: 30, opencode: 60 }[harness];
  const card = {
    harness, auto_enabled: harness !== "opencode", trigger_tokens: 250000,
    idle_after_minutes: { claude: 50, codex: 25, kimi: 25, opencode: null }[harness],
    idle_min_tokens: 125000, saved: false, updated_at: null,
    sources: { auto_enabled: "default", trigger_tokens: harness === "claude" ? "environment" : "default",
               idle_after_minutes: "default", idle_min_tokens: "default" },
    cache_life_minutes: life, cache_life_measured: harness !== "opencode",
    idle_choices: choices(life), min_tokens: 90000, floor_tokens: 90000, week_compactions: 3,
    effective_trigger: harness !== "opencode" ? 250000 : null,
    trigger_raised_to_floor: false, idle_min_raised_to_floor: false,
    display_name: harness, manual_trigger: harness !== "kimi", trigger_native: ["claude", "codex"].includes(harness),
    unavailable_reason: harness === "kimi" ? "not available: no headless compaction trigger in Kimi 2.5.3" : null,
    applies: [{ part: "trigger_passed", label: "Trigger passed to the harness", available: true, note: "as a token count" }],
    ...overrides,
  };
  // As the engine answers: the value in effect is raised to the floor.
  card.effective_trigger = card.auto_enabled ? Math.max(card.trigger_tokens, 90000) : null;
  card.trigger_raised_to_floor = card.trigger_tokens < 90000;
  card.idle_min_raised_to_floor = card.idle_min_tokens < 90000;
  return card;
};
const statuses = {
  claude: { conversion: { version: "2.1.322", tier: "rewrite", verified_at: "2026-11-13", error: null } },
  codex: { conversion: null }, kimi: { conversion: null },
  opencode: { conversion: null }, vibe: { conversion: null },
};
const DATA = {
  harness: "claude", days: 7, chats: 2, requests: 6,
  settings: { trigger_tokens: 250000 },
  context: { readings: 6, median_tokens: 115000, at_or_above_trigger: 1, share_at_or_above_trigger: 1 / 6 },
  warm_share: { reported: true, share: 0.5744 },
  cold_by_gap: { reported: true, buckets: [{ label: "under 5 min", rate: 0, cold: 0, requests: 1 }] },
  cold_returns: { reported: true, count: 1, tokens_reread: 130000 },
  compactions: { total: 1, by_cause: { idle: { count: 1, median_before: 140000, median_after: 29000 } } },
  idle_payoff: { followed: 1, not_followed: 0, pending: 0 },
  compaction_cost: { reported: true, requests: 2, total: 235500 },
};
const requests = [];
let refuse = null, settingsFail = null;
const cardOverrides = {};
// When set, card-data requests wait until the test opens the gate.
let dataGate = null;
const gate = () => { let open; const promise = new Promise((r) => { open = r; }); return { promise, open }; };
async function api(path, method = "GET", body) {
  requests.push({ path, method, body });
  if (path === "/flavor-defaults") return { harness_status: statuses };
  if (path === "/harnesses/compaction") {
    if (settingsFail) throw new Error(settingsFail);
    return { install_wide: true, harnesses: ["claude", "codex", "kimi", "opencode"].map((h) => cardFor(h, cardOverrides[h])) };
  }
  if (path.endsWith("/conversion/verify")) return {};
  if (path.includes("/compaction-data") && dataGate) await dataGate.promise;
  if (path.includes("/compaction-data")) return { ...DATA, harness: path.split("/")[2], days: Number(path.split("days=")[1]) };
  if (method === "PUT") {
    if (refuse) throw new Error(refuse);
    const harness = path.split("/")[2];
    return cardFor(harness, { ...body, sources: { ...cardFor(harness).sources, ...Object.fromEntries(Object.keys(body).map((k) => [k, "saved"])) }, saved: true });
  }
  throw new Error("unexpected API call: " + path);
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
"""


def run_js(script: str) -> dict:
    proc = subprocess.run(["node", "-e", FAKE_DOM + CARDS_JS + script], text=True,
                          capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def render(body: str) -> dict:
    return run_js(r"""
(async () => {
  const host = new FakeElement("section");
  await renderConversionStatus(host);
  const rows = () => host.children.filter((node) => node.nodeType === 1 && has("run-row")(node));
  const card = (harness) => rows().find((row) => row.children[0].textContent === harness);
  const out = {};
""" + body + r"""
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
""")


@needs_node
def test_cards_keep_the_heading_order_and_conversion_content_collapsed():
    out = render(r"""
  out.heading = host.children[0].tagName + ":" + host.children[0].textContent;
  out.names = rows().map((row) => row.children[0].textContent);
  out.vibe = card("vibe").textContent;
  out.vibeCards = all(card("vibe"), has("cc")).length;
  out.claude = card("claude").textContent;
  out.collapsedBodies = all(host, has("cc-body")).length;
  out.summaries = Object.fromEntries(["claude", "codex", "kimi", "opencode"].map(
    (h) => [h, all(card(h), has("cc-summary"))[0].textContent]));
  out.labels = Object.fromEntries(["claude", "codex", "kimi", "opencode"].map(
    (h) => [h, all(card(h), has("cc-label"))[0].textContent]));
  const verify = all(card("claude"), (n) => n.tagName === "button" && n.textContent === "Re-verify")[0];
  await verify.onclick();
  out.verified = requests.some((r) => r.method === "POST" && r.path === "/harnesses/claude/conversion/verify");
""")
    assert out["heading"] == "h3:GUI background conversion"
    assert out["names"] == ["claude", "codex", "kimi", "opencode", "vibe"]
    # Vibe's card keeps exactly what it showed and gains nothing.
    assert out["vibe"] == "vibeNo conversion hook; use sc job for durable GUI work."
    assert out["vibeCards"] == 0
    # Claude's conversion content and Re-verify are there without expanding.
    for text in ("2.1.322 · rewrite", "Background calls become durable jobs.",
                 "Verified: 2026-11-13", "Re-verify"):
        assert text in out["claude"]
    assert out["verified"] is True
    assert out["collapsedBodies"] == 0
    assert out["summaries"]["claude"] == (
        "▸ Auto compaction on at 250k · Idle compaction after 50 min · 3 compactions this week")
    assert out["summaries"]["opencode"] == (
        "▸ Auto compaction off · Idle compaction off · 3 compactions this week")
    assert out["labels"] == {
        "claude": "Claude (Anthropic)", "codex": "Codex (OpenAI)", "kimi": "Kimi (Moonshot)",
        "opencode": "One setting for every model provider behind OpenCode",
    }


@needs_node
def test_cards_tooltips_are_exact_on_the_four_compacting_cards():
    out = render(r"""
  out.cards = {};
  for (const h of ["claude", "codex", "kimi", "opencode"]) {
    await all(card(h), has("cc-summary"))[0].onclick();
    const settings = all(card(h), has("cc-setting"));
    out.cards[h] = settings.map((setting) => {
      const info = all(setting, has("cc-info"))[0];
      const tip = all(info, has("cc-tip"))[0];
      return { name: all(setting, has("cc-name"))[0].textContent, tabIndex: info.tabIndex,
               role: info.role, tip: tip.textContent, tipRole: tip.role, describedBy: tip.id };
    });
  }
  out.vibeInfo = all(card("vibe"), has("cc-info")).length;
""")
    for harness, settings in out["cards"].items():
        assert [s["name"] for s in settings] == ["Auto compaction", "Idle compaction"], harness
        auto, idle = settings
        assert auto["tip"] == AUTO_TIP
        assert idle["tip"] == IDLE_TIP
        for setting in settings:
            assert setting["tabIndex"] == 0
            assert setting["role"] == "button"
            assert setting["tipRole"] == "tooltip"
    assert out["vibeInfo"] == 0
    # The tooltip opens on hover and on keyboard focus.
    assert ".cc-info:hover .cc-tip, .cc-info:focus .cc-tip" in STYLE


@needs_node
def test_cards_expand_to_settings_then_data_with_limited_dropdowns_and_sources():
    out = render(r"""
  out.cards = {};
  for (const h of ["claude", "codex", "kimi", "opencode"]) {
    await all(card(h), has("cc-summary"))[0].onclick();
    await flush();
    const body = all(card(h), has("cc-body"))[0];
    const wait = all(body, has("cc-wait"))[0];
    out.cards[h] = {
      sections: all(body, (n) => n.tagName === "h4").map((n) => n.textContent),
      options: wait.children.map((o) => o.textContent),
      selected: wait.value,
      sources: all(body, has("cc-source")).map((n) => n.textContent),
      life: all(body, has("cc-life"))[0].textContent,
      note: body.textContent.includes("Applies to every shell's browser chats on this install."),
      quota: all(body, has("cc-quota")).map((a) => a.href + " " + a.textContent),
      figures: all(body, (n) => n.tagName === "dt").map((n) => n.textContent),
      disabled: all(body, (n) => n.tagName === "input" || n.tagName === "select").map((n) => !!n.disabled),
      unavailable: all(body, has("cc-unavailable")).map((n) => n.textContent),
    };
  }
  out.remembered = JSON.parse(localStorage.getItem("sc.harness-cards.expanded"));
  out.dataCalls = requests.filter((r) => r.path.includes("compaction-data")).map((r) => r.path);
""")
    cards = out["cards"]
    minutes = lambda last: ["Off"] + [f"{m} min" for m in range(5, last + 1, 5)]
    assert cards["claude"]["options"] == minutes(55)
    assert cards["codex"]["options"] == minutes(40)
    assert cards["kimi"]["options"] == minutes(25)
    assert cards["opencode"]["options"] == minutes(55)
    assert cards["claude"]["selected"] == "50"
    assert cards["opencode"]["selected"] == ""
    assert "cache life not measured" in cards["opencode"]["life"]
    assert "cache life not measured" not in cards["claude"]["life"]
    for harness, card in cards.items():
        assert card["sections"] == ["Settings", "Data"], harness
        assert card["note"], harness
        assert card["figures"][0] == "Chats, model requests"
    assert cards["claude"]["sources"][0] == "switch: default · trigger: environment"
    assert cards["claude"]["sources"][1] == "wait: default · idle size: default"
    assert cards["claude"]["quota"] == ["#analytics-quota Provider quota (anthropic) on the Analytics view →"]
    assert cards["codex"]["quota"] == ["#analytics-quota Provider quota (openai) on the Analytics view →"]
    assert cards["kimi"]["quota"] == ["#analytics-quota Provider quota (moonshot) on the Analytics view →"]
    assert cards["opencode"]["quota"] == []
    # Kimi without a confirmed trigger shows why and cannot be edited.
    assert cards["kimi"]["unavailable"] == [
        "not available: no headless compaction trigger in Kimi 2.5.3"]
    assert all(cards["kimi"]["disabled"])
    assert not any(cards["claude"]["disabled"])
    assert out["remembered"] == ["claude", "codex", "kimi", "opencode"]
    assert out["dataCalls"][0] == "/harnesses/claude/compaction-data?days=7"


@needs_node
def test_cards_save_on_change_and_show_saved_or_the_refusal_inline():
    out = render(r"""
  await all(card("codex"), has("cc-summary"))[0].onclick();
  await flush();
  let body = all(card("codex"), has("cc-body"))[0];
  const dataCalls = () => requests.filter((r) => r.path.startsWith("/harnesses/codex/compaction-data")).length;
  out.dataCallsBefore = dataCalls();
  const trigger = all(body, has("cc-number"))[0];
  trigger.value = "180,000";
  dataGate = gate();
  const saving = trigger.onchange();
  // Before the reload answers, the repaint still shows the loaded figures.
  await new Promise((resolve) => setImmediate(resolve));
  const during = all(card("codex"), has("cc-body"))[0];
  out.duringSave = all(during, has("cc-data-body"))[0].textContent.includes("Loading") ? "loading"
    : all(during, (n) => n.tagName === "dd").length;
  dataGate.open(); dataGate = null;
  await saving;
  body = all(card("codex"), has("cc-body"))[0];
  out.dataCallsAfter = dataCalls();
  out.figuresAfter = all(body, (n) => n.tagName === "dd").map((n) => n.textContent);
  out.loadingAfter = all(body, has("cc-data-body"))[0].textContent.includes("Loading");
  out.captions = all(body, has("cc-caption")).map((n) => n.textContent);
  out.put = requests.filter((r) => r.method === "PUT").map((r) => [r.path, r.body]);
  out.saved = all(body, has("cc-status")).map((n) => n.textContent).filter(Boolean);
  out.sources = all(body, has("cc-source"))[0].textContent;
  out.summary = all(card("codex"), has("cc-summary"))[0].textContent;
  const wait = all(body, has("cc-wait"))[0];
  wait.value = "";
  await wait.onchange();
  out.waitPut = requests.filter((r) => r.method === "PUT").at(-1).body;
  refuse = "COMPACTION_SETTING_INVALID: trigger_tokens must be a whole number of at least 90,000";
  body = all(card("codex"), has("cc-body"))[0];
  const low = all(body, has("cc-number"))[0];
  low.value = "89999";
  await low.onchange();
  out.refused = all(body, (n) => has("cc-status")(n) && has("error")(n)).map((n) => n.textContent);
""")
    assert out["put"] == [["/harnesses/codex/compaction", {"trigger_tokens": 180000}]]
    # The figures survive the repaint and are reloaded after the save.
    assert out["duringSave"] == 9
    assert out["dataCallsAfter"] == out["dataCallsBefore"] + 1
    assert out["loadingAfter"] is False
    assert len(out["figuresAfter"]) == 9
    assert out["figuresAfter"][0] == "2 chats · 6 model requests"
    assert out["captions"] == [
        "A request is cold when under 50% of its input was read from the cache; "
        "the gap is the time since the chat's previous request.",
        "A request after a gap of at least the cache life; tokens re-read are its "
        "uncached input plus cache writes.",
    ]
    assert out["saved"] == ["Saved"]
    assert out["sources"] == "switch: default · trigger: saved"
    assert out["summary"].startswith("▾ Auto compaction on at 180k")
    assert out["waitPut"] == {"idle_after_minutes": None}
    assert out["refused"] == [
        "COMPACTION_SETTING_INVALID: trigger_tokens must be a whole number of at least 90,000"]


@needs_node
def test_cards_remember_expansion_switch_period_and_show_a_settings_failure_inline():
    out = render(r"""
  localStorage.setItem("sc.harness-cards.expanded", JSON.stringify(["opencode"]));
  const again = new FakeElement("section");
  await renderConversionStatus(again);
  await flush();
  const opencode = again.children.find((n) => n.nodeType === 1 && n.children[0]?.textContent === "opencode");
  out.reopened = all(opencode, has("cc-body")).length;
  out.closed = all(again.children.find((n) => n.nodeType === 1 && n.children[0]?.textContent === "claude"), has("cc-body")).length;
  const thirty = all(opencode, (n) => n.tagName === "button" && n.textContent === "30 days")[0];
  await thirty.onclick();
  out.period = requests.filter((r) => r.path.includes("compaction-data")).at(-1).path;
  out.figures = all(opencode, (n) => n.tagName === "dd").map((n) => n.textContent);
  settingsFail = "COMPACTION_BROKEN: settings table unreadable";
  const failed = new FakeElement("section");
  await renderConversionStatus(failed);
  const claude = failed.children.find((n) => n.nodeType === 1 && n.children[0]?.textContent === "claude");
  out.failedText = claude.textContent;
""")
    assert out["reopened"] == 1
    assert out["closed"] == 0
    assert out["period"] == "/harnesses/opencode/compaction-data?days=30"
    assert out["figures"][0] == "2 chats · 6 model requests"
    assert "115k" in out["figures"]
    # A failed settings call shows inside the card; conversion content stays.
    assert "Compaction settings unavailable: COMPACTION_BROKEN" in out["failedText"]
    assert "Re-verify" in out["failedText"]


def test_no_tab_or_hash_route_is_added():
    tabs = SHELL_RENDER[SHELL_RENDER.index("const tabs"):] if "const tabs" in SHELL_RENDER else SHELL_RENDER
    for word in ("Providers", "Compaction", "shells-providers", "shells-compaction"):
        assert word not in SHELL_RENDER, word
    assert "providers" not in APP[APP.index("let lastRoutedHash"):].split("document.querySelectorAll")[0]
    assert tabs  # the five tabs themselves are pinned by test_shells_ui_contract


@needs_node
def test_cards_show_the_value_in_effect_when_raised_to_the_engine_floor():
    out = render(r"""
  cardOverrides.codex = { trigger_tokens: 5000, sources: { auto_enabled: "environment",
    trigger_tokens: "environment", idle_after_minutes: "default", idle_min_tokens: "default" } };
  const again = new FakeElement("section");
  await renderConversionStatus(again);
  const codex = again.children.find((n) => n.nodeType === 1 && n.children[0]?.textContent === "codex");
  out.summary = all(codex, has("cc-summary"))[0].textContent;
  await all(codex, has("cc-summary"))[0].onclick();
  await flush();
  const body = all(codex, has("cc-body"))[0];
  out.floor = all(body, has("cc-floor")).map((n) => n.textContent);
  out.mins = all(body, has("cc-number")).map((n) => n.min);
  out.claudeFloor = all(card("claude"), has("cc-floor")).length;
""")
    assert out["summary"].startswith("▸ Auto compaction on at 90k")
    assert out["floor"] == [
        "In effect: 90k, raised to the engine floor (the engine never compacts below 90k)"]
    assert out["mins"] == [90000, 90000]
    assert out["claudeFloor"] == 0
