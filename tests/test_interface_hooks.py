#!/usr/bin/env python3
"""Cross-harness lifecycle adapter proofs (spec #20 Harness Hooks, sprint
25 seq 7, task #83).

Hermetic coverage of the adapter layer — no harness binary runs here:

1. Capability table: claude/codex/kimi at verified versions satisfy the
   mandatory hook set; unknown harnesses and below-minimum versions fail
   closed (arming blocked, ordinary chat unaffected).
2. Installers MERGE without replacing fork/user hooks: claude rides a
   per-session --settings overlay (nothing rewritten), codex preserves the
   fork's PreToolUse group, kimi preserves user config outside its markers.
   No credential is ever baked into a config file.
3. The emitter: per-generation hook sequences are flock-serialized and
   monotonic from 2 (the entrypoint owns 1); the callback carries ONLY the
   contract fields — stdin prompt content is never read or forwarded; a
   missing Interface env is a local no-op, never a harness-breaking error.

Run:
    python3 tests/test_interface_hooks.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import interface_hook  # noqa: E402
import interface_hooks  # noqa: E402


class CapabilityTest(unittest.TestCase):
    def test_verified_versions_satisfy_mandatory(self):
        for harness, version in (("claude", "2.1.217 (Claude Code)"),
                                 ("codex", "codex-cli 0.145.0"),
                                 ("kimi", "0.27.0")):
            with self.subTest(harness=harness):
                cap = interface_hooks.capability(harness, version)
                self.assertTrue(cap["mandatory_ok"], cap)
                self.assertEqual(cap["missing_mandatory"], [])
                for event in interface_hooks.MANDATORY:
                    self.assertTrue(cap["events"][event],
                                    f"{harness} must deliver {event}")

    def test_optional_event_honesty(self):
        kimi = interface_hooks.capability("kimi", "0.27.0")
        self.assertTrue(kimi["events"]["approval_wait"])
        self.assertTrue(kimi["events"]["approval_result"])
        self.assertTrue(kimi["events"]["interrupt"])
        self.assertFalse(kimi["events"]["user_input_wait"])
        claude = interface_hooks.capability("claude", "2.1.217")
        self.assertFalse(claude["events"]["approval_wait"],
                         "no approval-result event → stays busy (safe)")
        self.assertTrue(claude["events"]["failure"])
        codex = interface_hooks.capability("codex", "0.145.0")
        self.assertFalse(codex["events"]["interrupt"])
        self.assertFalse(codex["events"]["failure"])

    def test_below_minimum_version_fails_closed(self):
        cap = interface_hooks.capability("claude", "2.0.0 (Claude Code)")
        self.assertFalse(cap["mandatory_ok"])
        self.assertFalse(cap["version_ok"])
        cap = interface_hooks.capability("codex", "codex-cli 0.128.0")
        self.assertFalse(cap["mandatory_ok"])

    def test_unknown_harness_and_version_fail_closed(self):
        self.assertFalse(interface_hooks.capability("vim", "1.0")
                         ["mandatory_ok"])
        self.assertFalse(interface_hooks.capability(None, None)
                         ["mandatory_ok"])
        self.assertFalse(interface_hooks.capability("kimi", "unparseable")
                         ["mandatory_ok"])


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.work = self.tmp / "wt"
        self.work.mkdir()

    def test_claude_overlay_is_additive_and_secret_free(self):
        out = interface_hooks.install("claude", self.work,
                                      run_dir=self.run_dir, session_id=7,
                                      cli_version="2.1.217")
        self.assertTrue(out["installed"])
        self.assertEqual(out["argv"][0], "--settings")
        overlay = Path(out["argv"][1])
        self.assertTrue(overlay.exists())
        mode = stat.S_IMODE(overlay.stat().st_mode)
        self.assertEqual(mode, 0o600)
        cfg = json.loads(overlay.read_text())
        self.assertEqual(set(cfg["hooks"]),
                         {"SessionStart", "UserPromptSubmit", "Stop",
                          "StopFailure", "SessionEnd"})
        raw = overlay.read_text()
        self.assertIn("interface_hook.py", raw)
        self.assertNotIn("hook_token", raw,
                         "credentials travel in the env, never in config")
        ss = cfg["hooks"]["SessionStart"][0]
        self.assertEqual(ss["matcher"], "startup|resume")

    def test_codex_merge_preserves_fork_hooks(self):
        hooks_file = self.work / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        fork_group = {"matcher": "^apply_patch$",
                      "hooks": [{"type": "command",
                                 "command": "bash branch-guard.sh"}]}
        hooks_file.write_text(json.dumps(
            {"hooks": {"PreToolUse": [fork_group]}}))
        out = interface_hooks.install("codex", self.work,
                                      run_dir=self.run_dir, session_id=7,
                                      cli_version="codex-cli 0.145.0")
        self.assertTrue(out["installed"])
        self.assertEqual(out["argv"], [])
        cfg = json.loads(hooks_file.read_text())
        self.assertEqual(cfg["hooks"]["PreToolUse"], [fork_group],
                         "the fork's branch-guard group is untouched")
        for native in ("SessionStart", "UserPromptSubmit", "Stop",
                       "SessionEnd"):
            self.assertIn(native, cfg["hooks"], native)
            self.assertIn("interface_hook.py",
                          json.dumps(cfg["hooks"][native]))
        # Idempotent: a second install replaces our groups, never duplicates.
        interface_hooks.install("codex", self.work, run_dir=self.run_dir,
                                session_id=7, cli_version="codex-cli 0.145.0")
        cfg2 = json.loads(hooks_file.read_text())
        self.assertEqual(len(cfg2["hooks"]["Stop"]), 1)

    def test_codex_merge_never_clobbers_unparseable(self):
        hooks_file = self.work / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        hooks_file.write_text("{not json")
        out = interface_hooks.install("codex", self.work,
                                      run_dir=self.run_dir, session_id=7,
                                      cli_version="codex-cli 0.145.0")
        self.assertFalse(out["installed"])
        self.assertEqual(hooks_file.read_text(), "{not json")

    def test_kimi_merge_preserves_user_config(self):
        kimi_home = self.tmp / "kimi-home"
        kimi_home.mkdir()
        cfg_path = kimi_home / "config.toml"
        cfg_path.write_text('model = "k3"\n\n[[hooks]]\nevent = "PreToolUse"\n'
                            'command = "my-own-hook"\n')
        with mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(kimi_home)}):
            out = interface_hooks.install("kimi", self.work,
                                          run_dir=self.run_dir, session_id=7,
                                          cli_version="0.27.0")
        self.assertTrue(out["installed"])
        text = cfg_path.read_text()
        self.assertIn('command = "my-own-hook"', text,
                      "the user's own hooks are preserved")
        self.assertIn(interface_hooks._KIMI_BEGIN, text)
        for native in ("SessionStart", "UserPromptSubmit", "Stop",
                       "Interrupt", "StopFailure", "SessionEnd",
                       "PermissionRequest", "PermissionResult"):
            self.assertIn(f'event = "{native}"', text, native)
        self.assertNotIn("hook_token", text)
        # Idempotent: re-install replaces the managed block, no growth.
        with mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(kimi_home)}):
            interface_hooks.install("kimi", self.work, run_dir=self.run_dir,
                                    session_id=8, cli_version="0.27.0")
        self.assertEqual(cfg_path.read_text().count(
            interface_hooks._KIMI_BEGIN), 1)

    def test_install_gates_on_capability(self):
        out = interface_hooks.install("claude", self.work,
                                      run_dir=self.run_dir, session_id=7,
                                      cli_version="2.0.0")
        self.assertFalse(out["installed"])
        self.assertEqual(out["argv"], [])
        self.assertFalse((self.run_dir / "claude-hooks-7.json").exists())
        out = interface_hooks.install("unknown-cli", self.work,
                                      run_dir=self.run_dir, session_id=7,
                                      cli_version="1.0")
        self.assertFalse(out["installed"])


class EmitterSeqTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_sequence_starts_after_entrypoint(self):
        seq = interface_hook.next_hook_seq(self.tmp, 1, 1)
        self.assertEqual(seq, 2, "the entrypoint's session_start owns seq 1")
        self.assertEqual(interface_hook.next_hook_seq(self.tmp, 1, 1), 3)
        self.assertEqual(interface_hook.next_hook_seq(self.tmp, 1, 1), 4)

    def test_sequence_is_per_generation(self):
        self.assertEqual(interface_hook.next_hook_seq(self.tmp, 1, 1), 2)
        self.assertEqual(interface_hook.next_hook_seq(self.tmp, 1, 2), 2)
        self.assertEqual(interface_hook.next_hook_seq(self.tmp, 2, 1), 2)

    def test_concurrent_allocation_is_unique(self):
        issued = []
        lock = threading.Lock()

        def alloc():
            seq = interface_hook.next_hook_seq(self.tmp, 1, 1)
            with lock:
                issued.append(seq)

        threads = [threading.Thread(target=alloc) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(issued), list(range(2, 22)),
                         "flock serialization forbids double-issue")


class EmitterPostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"SC_INTERFACE_HOOK_TOKEN": "tok-x",
                    "SC_INTERFACE_SHELL_ID": "1",
                    "SC_INTERFACE_GENERATION": "3",
                    "SC_API_BASE": "http://127.0.0.1:9999",
                    "SC_INTERFACE_RUN_DIR": str(self.tmp)}

    def _run(self, argv, urlopen):
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(interface_hook.urllib.request,
                               "urlopen", urlopen):
            return interface_hook.main(argv)

    def test_callback_carries_only_contract_fields(self):
        posts = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            posts.append(req)
            return Resp()

        code = self._run(["--event", "turn_stop", "--pid", "4321"], urlopen)
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 1)
        req = posts[0]
        self.assertEqual(req.full_url,
                         "http://127.0.0.1:9999/api/interface/hook-callbacks")
        self.assertEqual(req.headers["Authorization"], "Bearer tok-x")
        body = json.loads(req.data)
        self.assertEqual(body, {"shell_id": 1, "generation": 3,
                                "hook_seq": 2, "event": "turn_stop",
                                "source": "provider", "pid": 4321},
                         "ONLY event/session/generation/sequence/pid/token "
                         "cross the contract — no content fields")

    def test_stdin_content_is_never_forwarded(self):
        posts = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            posts.append(req)
            return Resp()

        secret = "the user's secret draft prompt"
        with mock.patch("sys.stdin") as fake_stdin:
            fake_stdin.read.return_value = json.dumps({"prompt": secret})
            self._run(["--event", "prompt_submit", "--pid", "1"], urlopen)
        body = json.loads(posts[0].data)
        self.assertNotIn(secret, json.dumps(body))
        fake_stdin.read.assert_not_called()

    def test_missing_env_is_a_quiet_noop(self):
        posts = []

        def urlopen(req, timeout=None):
            posts.append(req)
            raise AssertionError("must not POST without the Interface env")

        env = {k: v for k, v in self.env.items()
               if k != "SC_INTERFACE_HOOK_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(interface_hook.urllib.request,
                               "urlopen", urlopen):
            code = interface_hook.main(["--event", "turn_stop", "--pid", "1"])
        self.assertEqual(code, 0, "an unmanaged session's hook never "
                                 "breaks the harness")
        self.assertEqual(posts, [])

    def test_unknown_event_refused_locally(self):
        posts = []

        def urlopen(req, timeout=None):
            posts.append(req)

        code = self._run(["--event", "bogus", "--pid", "1"], urlopen)
        self.assertEqual(code, 0)
        self.assertEqual(posts, [])

    def test_rejection_is_definitive_no_retry(self):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 409, "conflict",
                                         None, None)

        code = self._run(["--event", "turn_stop", "--pid", "1"], urlopen)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "a 4xx is a definitive answer")


if __name__ == "__main__":
    unittest.main()
