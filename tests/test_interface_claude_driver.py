#!/usr/bin/env python3
"""Fixture-backed Claude driver, parser, resolver, and retry-safety proofs."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import interface_chat  # noqa: E402
import interface_claude_driver as claude_driver  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "f31_claude"
PROVIDER_SESSION = "d1f38597-ee45-4b5b-97fd-8d39cda6b8ec"

EXPECTED_SHA256 = {
    "claude-controlled-failure.meta.txt":
        "d86b07ea7f280a3d8338e980a612e02558b2d03b1300c62fac8496f6e8b37595",
    "claude-controlled-failure.stderr.txt":
        "9279113dd3a1a9cd5c85c8c83f431664e6f3f12721e675c71d64a5ae207a81bc",
    "claude-controlled-failure.stdout.jsonl":
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "claude-force-headless.meta.txt":
        "6b5720f78ce3d8adbadc2684b75c31d2fbe0a867ba5aeb6f1961783a40f18777",
    "claude-force-headless.stdout.jsonl":
        "155ed0a9431275fc675ab3c2099194ca27649d15db5f2bbec4eafbd5bc46108c",
    "claude-headless1.meta.txt":
        "3a734849f881fb52880670823ba51844a9043f7b69d480c34f6c33b178b221c6",
    "claude-headless1.stdout.jsonl":
        "5b625a8bacec6fcfb009bd282c68b236abbf3d5dc433969b43e391abd8da4004",
    "claude-tool.meta.txt":
        "5e0d28a0a4dabf0862aaeb463de96c2a29561ab80d63625eaa5a93b16e664509",
    "claude-tool.stdout.jsonl":
        "01538bcd557c659dc4758770ae259ff9353aa3bb60f2566948b5909f91f8b17e",
    "claude.version.txt":
        "74fd5c2567221e143c16e917305e16131d853eb58f8c5ca3c2fdad9037ae4956",
}


class FakeRunner:
    def __init__(self, result: claude_driver.ProcessResult, callback=None):
        self.result = result
        self.callback = callback
        self.calls = []

    def run(self, argv, *, env, cwd):
        self.calls.append((list(argv), dict(env), cwd))
        if self.callback is not None:
            self.callback()
        return self.result


class SequenceRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, *, env, cwd):
        self.calls.append((list(argv), dict(env), cwd))
        return self.results.pop(0)


class ClaudeFixtureContractTest(unittest.TestCase):
    def test_s1b_fixture_bytes_match_published_checksums(self):
        actual_files = {path.name for path in FIXTURES.iterdir() if path.is_file()}
        self.assertEqual(actual_files, set(EXPECTED_SHA256))
        for name, expected in EXPECTED_SHA256.items():
            with self.subTest(name=name):
                actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)
        self.assertEqual((FIXTURES / "claude.version.txt").read_text().strip(), "2.1.220 (Claude Code)")

    def test_parser_maps_text_preview_completion_and_usage(self):
        outcome = claude_driver.ClaudeStreamParser().parse(
            (FIXTURES / "claude-headless1.stdout.jsonl").read_text()
        )
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.provider_session_id, PROVIDER_SESSION)
        self.assertEqual(outcome.terminal, "completed")
        self.assertEqual("".join(outcome.previews), "HEADLESS1 CEDAR-741")
        self.assertEqual(
            [event["kind"] for event in outcome.events],
            ["message_completed", "usage"],
        )
        self.assertEqual(
            outcome.events[0],
            {
                "kind": "message_completed",
                "role": "assistant",
                "payload": {"text": "HEADLESS1 CEDAR-741"},
            },
        )

    def test_parser_maps_tool_call_result_and_authoritative_message(self):
        outcome = claude_driver.ClaudeStreamParser().parse(
            (FIXTURES / "claude-tool.stdout.jsonl").read_text()
        )
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.terminal, "completed")
        self.assertEqual(
            [event["kind"] for event in outcome.events],
            ["tool_call", "tool_result", "message_completed", "usage"],
        )
        self.assertEqual(outcome.events[0]["payload"]["tool_name"], "Bash")
        self.assertEqual(
            outcome.events[0]["payload"]["arguments"]["command"],
            "printf TOOL_CLAUDE",
        )
        self.assertEqual(
            outcome.events[1]["payload"],
            {
                "tool_call_id": "toolu_016tFD83TRoptD2S1ERZ5LEd",
                "content": "TOOL_CLAUDE",
                "is_error": False,
            },
        )
        self.assertEqual(
            outcome.events[2]["payload"]["text"], "TOOL_DONE_CLAUDE"
        )

    def test_failed_aborted_result_is_terminal_even_with_zero_exit_fixture(self):
        outcome = claude_driver.ClaudeStreamParser().parse(
            (FIXTURES / "claude-force-headless.stdout.jsonl").read_text()
        )
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.terminal, "failed")
        self.assertTrue(outcome.aborted)
        self.assertEqual(
            (FIXTURES / "claude-force-headless.meta.txt").read_text().splitlines()[0],
            "rc=0",
        )

    def test_unknown_record_retains_no_payload_and_only_names_bounded_type(self):
        secret = "UNKNOWN_PROVIDER_PAYLOAD_DO_NOT_STORE"
        outcome = claude_driver.ClaudeStreamParser().parse(
            json.dumps(
                {
                    "type": "future.provider.record",
                    "subtype": "v9",
                    "payload": secret,
                }
            ),
            require_boundary=False,
            require_terminal=False,
        )
        self.assertEqual(outcome.events, [])
        self.assertEqual(outcome.previews, [])
        self.assertEqual(
            outcome.health_keys, ["unknown:future.provider.record:v9"]
        )
        self.assertNotIn(secret, json.dumps(outcome.health_keys))

    def test_malformed_json_cannot_be_mistaken_for_unknown_or_completion(self):
        outcome = claude_driver.ClaudeStreamParser().parse(
            '{"type":"system","subtype":"init","session_id":"x"}\n'
            '{"broken":\n'
        )
        self.assertEqual(outcome.error, "malformed_json_line_2")
        self.assertIsNone(outcome.terminal)
        self.assertEqual(outcome.events, [])

    def test_session_boundary_rejects_wrong_cwd_model_and_api_key_billing(self):
        valid = {
            "type": "system",
            "subtype": "init",
            "session_id": "provider-1",
            "cwd": "/expected",
            "model": "claude-fable-5",
            "apiKeySource": "none",
        }
        cases = [
            ({"cwd": "/wrong"}, "provider_cwd_mismatch"),
            ({"model": "claude-other-5"}, "provider_model_mismatch"),
            ({"apiKeySource": "ANTHROPIC_API_KEY"}, "api_key_billing_disallowed"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected):
                outcome = claude_driver.ClaudeStreamParser().parse(
                    json.dumps({**valid, **changes}),
                    expected_provider_session_id="provider-1",
                    expected_cwd="/expected",
                    expected_model="fable",
                    require_terminal=False,
                )
                self.assertEqual(outcome.error, expected)
        good = claude_driver.ClaudeStreamParser().parse(
            json.dumps(valid),
            expected_provider_session_id="provider-1",
            expected_cwd="/expected",
            expected_model="fable",
            require_terminal=False,
        )
        self.assertIsNone(good.error)

    def test_retry_posture_operand_matrix(self):
        cases = [
            ("proved absent after exact anchor", True, True, False, True, True),
            ("prompt present", True, True, True, True, False),
            ("anchor missing", False, True, False, True, False),
            ("anchor rewritten or ambiguous", True, False, False, True, False),
            ("process and output did not fail", True, True, False, False, False),
        ]
        for (
            label,
            anchor_present,
            anchor_unambiguous,
            prompt_present,
            turn_failed,
            expected,
        ) in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    claude_driver.retry_allowed(
                        anchor_present=anchor_present,
                        anchor_unambiguous=anchor_unambiguous,
                        prompt_present=prompt_present,
                        turn_failed=turn_failed,
                    ),
                    expected,
                )


class ClaudeDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_obj.name)
        self.cwd = str(self.tmp / "work")
        Path(self.cwd).mkdir()

    def tearDown(self):
        self.tmp_obj.cleanup()

    def context(
        self,
        label: str,
        *,
        transcript: bool = True,
        provider: str | None = PROVIDER_SESSION,
    ):
        root = self.tmp / label
        root.mkdir()
        store = interface_chat.ChatStore(root / "chat.db")
        store.migrate()
        store.create_session(
            "local-session",
            shell_id=5,
            harness="claude",
            cwd=self.cwd,
            provider_session_id=provider,
        )
        home = root / "home"
        resolver = claude_driver.ClaudeTranscriptResolver(home)
        transcript_path = (
            home / ".claude" / "projects" / "project" / f"{PROVIDER_SESSION}.jsonl"
        )
        if transcript:
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "sessionId": PROVIDER_SESSION,
                        "cwd": self.cwd,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        return store, resolver, transcript_path

    def result(self, name: str, rc: int = 0, stderr: str = ""):
        return claude_driver.ProcessResult(
            (FIXTURES / name).read_text().replace("<CWD>", self.cwd), stderr, rc
        )

    def test_pre_turn_anchor_and_user_events_commit_before_exact_spawn(self):
        store, resolver, _ = self.context("pre-spawn")

        def inspect_committed_state():
            with store.connect() as con:
                turn = con.execute(
                    "SELECT state, pre_turn_anchor_json FROM chat_turns"
                ).fetchone()
                cursor = con.execute(
                    "SELECT resolution_status FROM chat_transcript_cursors"
                ).fetchone()
                events = con.execute(
                    "SELECT kind, payload_json FROM chat_events ORDER BY event_seq"
                ).fetchall()
            self.assertEqual(turn["state"], "running")
            self.assertEqual(json.loads(turn["pre_turn_anchor_json"])["status"], "ready")
            self.assertEqual(cursor["resolution_status"], "ready")
            self.assertEqual(
                [row["kind"] for row in events], ["user_message", "turn_started"]
            )
            self.assertEqual(json.loads(events[0]["payload_json"])["text"], "hello")

        runner = FakeRunner(
            self.result("claude-headless1.stdout.jsonl"),
            callback=inspect_committed_state,
        )
        driver = claude_driver.ClaudeDriver(
            store, runner=runner, resolver=resolver
        )
        with mock.patch.dict(
            claude_driver.os.environ,
            {
                "ANTHROPIC_API_KEY": "must-not-reach-child",
                "ANTHROPIC_AUTH_TOKEN": "must-not-reach-child",
            },
        ):
            result = driver.run_turn("local-session", "hello")
        expected_argv = [
            "claude",
            "-p",
            "hello",
            "--resume",
            PROVIDER_SESSION,
            "--model",
            "fable",
            "--effort",
            "low",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]
        self.assertEqual(result.status, "completed")
        self.assertEqual(runner.calls[0][0], expected_argv)
        self.assertEqual(runner.calls[0][1]["IS_SANDBOX"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", runner.calls[0][1])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", runner.calls[0][1])
        self.assertEqual(runner.calls[0][2], self.cwd)
        with store.connect() as con:
            state = con.execute(
                "SELECT state, exit_code, retry_safe FROM chat_turns"
            ).fetchone()
            kinds = [
                row[0]
                for row in con.execute(
                    "SELECT kind FROM chat_events ORDER BY event_seq"
                )
            ]
        self.assertEqual(tuple(state), ("completed", 0, 0))
        self.assertEqual(
            kinds,
            [
                "user_message",
                "turn_started",
                "message_completed",
                "usage",
                "turn_completed",
            ],
        )
        self.assertNotIn("message_preview", kinds)

    def test_new_session_binds_stream_id_and_next_turn_resumes_it(self):
        store, resolver, _ = self.context(
            "new-session", transcript=False, provider=None
        )
        runner = SequenceRunner(
            [
                self.result("claude-headless1.stdout.jsonl"),
                self.result("claude-headless1.stdout.jsonl"),
            ]
        )
        driver = claude_driver.ClaudeDriver(store, runner=runner, resolver=resolver)
        first = driver.run_turn("local-session", "first")
        second = driver.run_turn("local-session", "second")
        self.assertEqual((first.status, second.status), ("completed", "completed"))
        self.assertNotIn("--resume", runner.calls[0][0])
        resume_index = runner.calls[1][0].index("--resume")
        self.assertEqual(runner.calls[1][0][resume_index + 1], PROVIDER_SESSION)
        self.assertEqual(
            store.session("local-session")["provider_session_id"],
            PROVIDER_SESSION,
        )

    def test_live_then_backfill_inserts_zero_rows_with_stable_keys(self):
        store, resolver, transcript = self.context("dedupe")
        prompt = "RUN THE CONTROLLED TOOL"
        tool_rows = [
            json.loads(line)
            for line in (FIXTURES / "claude-tool.stdout.jsonl").read_text().splitlines()
            if json.loads(line).get("type") in {"assistant", "user"}
        ]

        def append_transcript():
            rows = [
                {
                    "type": "user",
                    "sessionId": PROVIDER_SESSION,
                    "cwd": self.cwd,
                    "message": {"role": "user", "content": prompt},
                },
                *tool_rows,
            ]
            with transcript.open("a") as stream:
                for row in rows:
                    stream.write(json.dumps(row, separators=(",", ":")) + "\n")

        runner = FakeRunner(
            self.result("claude-tool.stdout.jsonl"),
            callback=append_transcript,
        )
        driver = claude_driver.ClaudeDriver(store, runner=runner, resolver=resolver)
        live = driver.run_turn("local-session", prompt)
        self.assertEqual(live.status, "completed")
        with store.connect() as con:
            before_rows = con.execute(
                "SELECT event_key, kind, payload_json FROM chat_events "
                "WHERE turn_id=? ORDER BY event_seq",
                (live.turn_id,),
            ).fetchall()
        status, inserted = driver.backfill_turn(live.turn_id)
        with store.connect() as con:
            after_rows = con.execute(
                "SELECT event_key, kind, payload_json FROM chat_events "
                "WHERE turn_id=? ORDER BY event_seq",
                (live.turn_id,),
            ).fetchall()
        self.assertEqual(status, "exact")
        self.assertEqual(inserted, 0)
        self.assertEqual([tuple(row) for row in after_rows], [tuple(row) for row in before_rows])
        durable = [
            row for row in before_rows
            if row["kind"] in {"tool_call", "tool_result", "message_completed"}
        ]
        self.assertEqual(len(durable), 3)
        for row in durable:
            self.assertRegex(row["event_key"], r"^ev1:[0-9a-f]{24}$")

    def failure_result(self):
        return self.result("claude-force-headless.stdout.jsonl", rc=0)

    def test_retry_guard_allows_only_exact_anchor_with_prompt_absent(self):
        store, resolver, _ = self.context("retry-absent")
        driver = claude_driver.ClaudeDriver(
            store, runner=FakeRunner(self.failure_result()), resolver=resolver
        )
        result = driver.run_turn("local-session", "ABSENT_PROMPT")
        self.assertEqual((result.status, result.retry_safe), ("failed", True))
        self.assertEqual(store.retry_prompt(result.turn_id), "ABSENT_PROMPT")

    def test_retry_guard_rejects_prompt_present_after_anchor(self):
        store, resolver, transcript = self.context("retry-present")
        prompt = "PRESENT_PROMPT"

        def append_prompt():
            with transcript.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": PROVIDER_SESSION,
                            "message": {"content": prompt},
                        }
                    )
                    + "\n"
                )

        driver = claude_driver.ClaudeDriver(
            store,
            runner=FakeRunner(self.failure_result(), callback=append_prompt),
            resolver=resolver,
        )
        result = driver.run_turn("local-session", prompt)
        self.assertFalse(result.retry_safe)
        with self.assertRaisesRegex(interface_chat.ChatStoreError, "not eligible"):
            store.retry_prompt(result.turn_id)

    def test_retry_guard_rejects_missing_anchor(self):
        store, resolver, _ = self.context("retry-missing", transcript=False)
        driver = claude_driver.ClaudeDriver(
            store, runner=FakeRunner(self.failure_result()), resolver=resolver
        )
        result = driver.run_turn("local-session", "MISSING_ANCHOR")
        self.assertFalse(result.retry_safe)
        with store.connect() as con:
            status = con.execute(
                "SELECT resolution_status FROM chat_transcript_cursors "
                "WHERE turn_id=?",
                (result.turn_id,),
            ).fetchone()[0]
        self.assertEqual(status, "gap")

    def test_retry_guard_rejects_ambiguous_rewritten_anchor(self):
        store, resolver, transcript = self.context("retry-ambiguous")
        anchor_line = transcript.read_bytes()

        def duplicate_anchor():
            transcript.write_bytes(b'{"prefix":true}\n' + anchor_line + anchor_line)

        driver = claude_driver.ClaudeDriver(
            store,
            runner=FakeRunner(self.failure_result(), callback=duplicate_anchor),
            resolver=resolver,
        )
        result = driver.run_turn("local-session", "AMBIGUOUS")
        self.assertFalse(result.retry_safe)
        with store.connect() as con:
            status = con.execute(
                "SELECT resolution_status FROM chat_transcript_cursors "
                "WHERE turn_id=?",
                (result.turn_id,),
            ).fetchone()[0]
        self.assertEqual(status, "gap")

    def test_relocated_unique_anchor_still_proves_absence(self):
        store, resolver, transcript = self.context("retry-relocated")
        anchor_line = transcript.read_bytes()

        def relocate_anchor():
            transcript.write_bytes(b'{"prefix":true}\n' + anchor_line)

        driver = claude_driver.ClaudeDriver(
            store,
            runner=FakeRunner(self.failure_result(), callback=relocate_anchor),
            resolver=resolver,
        )
        result = driver.run_turn("local-session", "RELOCATED_ABSENT")
        self.assertTrue(result.retry_safe)
        with store.connect() as con:
            status = con.execute(
                "SELECT resolution_status FROM chat_transcript_cursors "
                "WHERE turn_id=?",
                (result.turn_id,),
            ).fetchone()[0]
        self.assertEqual(status, "relocated")

    def test_process_exit_and_provider_terminal_are_both_required(self):
        cases = [
            ("success", "claude-headless1.stdout.jsonl", 0, "completed", None),
            ("bad-exit", "claude-headless1.stdout.jsonl", 9, "failed", "process_exit"),
            ("failed-output", "claude-force-headless.stdout.jsonl", 0, "failed", "provider_failed"),
            (
                "missing-terminal",
                "claude-headless1.stdout.jsonl",
                0,
                "failed",
                "missing_terminal_result",
            ),
        ]
        for label, fixture, rc, status, failure in cases:
            with self.subTest(label=label):
                store, resolver, _ = self.context(f"terminal-{label}")
                stdout = (FIXTURES / fixture).read_text().replace(
                    "<CWD>", self.cwd
                )
                if label == "missing-terminal":
                    stdout = "\n".join(
                        line
                        for line in stdout.splitlines()
                        if json.loads(line).get("type") != "result"
                    )
                runner = FakeRunner(
                    claude_driver.ProcessResult(stdout, "", rc)
                )
                result = claude_driver.ClaudeDriver(
                    store, runner=runner, resolver=resolver
                ).run_turn("local-session", f"prompt-{label}")
                self.assertEqual(result.status, status)
                self.assertEqual(result.failure_code, failure)

    def test_lost_process_closes_turn_and_preserves_retry_proof(self):
        store, resolver, _ = self.context("lost-process")

        def lose_process():
            raise RuntimeError("simulated lost child")

        result = claude_driver.ClaudeDriver(
            store,
            runner=FakeRunner(
                self.result("claude-headless1.stdout.jsonl"),
                callback=lose_process,
            ),
            resolver=resolver,
        ).run_turn("local-session", "not submitted")
        with store.connect() as con:
            turn = con.execute(
                "SELECT state, failure_code, failure_diagnostic, retry_safe "
                "FROM chat_turns WHERE turn_id=?",
                (result.turn_id,),
            ).fetchone()
            session_mode = con.execute(
                "SELECT host_mode FROM chat_sessions "
                "WHERE session_id='local-session'"
            ).fetchone()[0]
        self.assertEqual(result.failure_code, "process_lost")
        self.assertEqual(
            tuple(turn), ("failed", "process_lost", "RuntimeError", 1)
        )
        self.assertEqual(session_mode, "idle_chat")

    def test_stderr_diagnostic_is_scrubbed_bounded_and_never_a_message(self):
        store, resolver, _ = self.context("stderr")
        stderr = (self.cwd + "/secret/file ") * 500
        runner = FakeRunner(claude_driver.ProcessResult("", stderr, 1))
        result = claude_driver.ClaudeDriver(
            store, runner=runner, resolver=resolver
        ).run_turn("local-session", "stderr prompt")
        with store.connect() as con:
            turn = con.execute(
                "SELECT failure_diagnostic FROM chat_turns WHERE turn_id=?",
                (result.turn_id,),
            ).fetchone()
            message_payloads = [
                row[0]
                for row in con.execute(
                    "SELECT payload_json FROM chat_events "
                    "WHERE turn_id=? AND kind='message_completed'",
                    (result.turn_id,),
                )
            ]
        diagnostic = turn["failure_diagnostic"]
        self.assertLessEqual(len(diagnostic.encode()), claude_driver.STDERR_LIMIT_BYTES)
        self.assertIn("<CWD>", diagnostic)
        self.assertNotIn(self.cwd, diagnostic)
        self.assertEqual(message_payloads, [])

    def test_unknown_payload_never_reaches_db_or_wal(self):
        store, _, _ = self.context("unknown-payload")
        secret = "RAW_UNKNOWN_SECRET_9451"
        parsed = claude_driver.ClaudeStreamParser().parse(
            json.dumps({"type": "new-kind", "raw": secret}),
            require_boundary=False,
            require_terminal=False,
        )
        store.increment_health("claude", parsed.health_keys)
        with store.connect() as con:
            rows = con.execute(
                "SELECT counter_key, count FROM chat_health"
            ).fetchall()
            event_count = con.execute(
                "SELECT COUNT(*) FROM chat_events"
            ).fetchone()[0]
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("unknown:new-kind", 1)])
        self.assertEqual(event_count, 0)
        for path in (store.db_path, Path(str(store.db_path) + "-wal")):
            if path.exists():
                self.assertNotIn(secret.encode(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
