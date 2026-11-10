#!/usr/bin/env python3
"""Smoke tests for the tailnet broker (api/ts_broker.py + scripts/ts.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style and the
sibling tests (test_vm_broker.py). The broker drives a real tailnet via the
tailscale CLI, which no CI box has; so we mock at the subprocess seam
(`ts._run` / `subprocess.run`) and exercise the parts that DO run everywhere: the
verb dispatch + scoping, the JSON shapes the `tailscale` skill depends on, and
the real unix-socket HTTP transport end to end (a live broker on a temp socket,
driven by the same `ts.broker_call` client the in-sandbox server proxies through).

Run:
    python3 tests/test_ts_broker.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import ts  # noqa: E402
import ts_broker  # noqa: E402

SAVED = {
    "ssh_user": "tester",
    "allowed_hosts": ["build-box", "deploy-target"],
    "tailscale_bin": "tailscale",
}

READONLY_SAVED = dict(SAVED, readonly_hosts=["production"])

# A minimal `tailscale status --json` payload.
STATUS_JSON = json.dumps({
    "BackendState": "Running",
    "Self": {"HostName": "cachy", "DNSName": "cachy.tail0.ts.net.",
             "TailscaleIPs": ["100.64.0.1"]},
    "Peer": {
        "k1": {"HostName": "build-box", "DNSName": "build-box.tail0.ts.net.",
               "TailscaleIPs": ["100.64.0.2"], "Online": True},
        "k2": {"HostName": "deploy-target", "DNSName": "deploy-target.tail0.ts.net.",
               "TailscaleIPs": ["100.64.0.3"], "Online": False},
    },
})


class VerbDispatchTests(unittest.TestCase):
    """The verbs operate on the SAVED block + a named host and shape results."""

    def test_exec_returns_exit_stdout_stderr_and_targets_tailscale_ssh(self):
        fake = mock.Mock(returncode=0, stdout="hello\n", stderr="")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = ts.do_exec("build-box", "echo hello")
        self.assertEqual(r, {"ok": True, "exit": 0, "stdout": "hello\n", "stderr": ""})
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["tailscale", "ssh"])
        self.assertIn("tester@build-box", argv)

    def test_exec_denies_a_host_outside_allowed_hosts(self):
        # Fail-closed scoping: a host not in allowed_hosts is rejected pre-ssh.
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run") as run:
            r = ts.do_exec("rogue-host", "whoami")
        self.assertFalse(r["ok"])
        self.assertIn("not in allowed_hosts", r["stderr"])
        run.assert_not_called()

    def test_exec_with_no_allowed_hosts_denies_everything(self):
        with mock.patch.object(ts, "read", return_value={"ssh_user": "tester"}), \
             mock.patch("subprocess.run") as run:
            r = ts.do_exec("build-box", "whoami")
        self.assertFalse(r["ok"])
        self.assertIn("no allowed_hosts", r["stderr"])
        run.assert_not_called()

    def test_every_readonly_command_table_entry_passes(self):
        fake = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.object(ts, "read", return_value=READONLY_SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            for prefix in ts.READONLY_COMMANDS:
                with self.subTest(prefix=prefix):
                    command = " ".join((*prefix, "target"))
                    r = ts.do_exec("production", command)
                    self.assertTrue(r["ok"], r)
        self.assertEqual(run.call_count, len(ts.READONLY_COMMANDS))

    def test_each_forbidden_character_is_structurally_refused(self):
        with mock.patch.object(ts, "read", return_value=READONLY_SAVED), \
             mock.patch("subprocess.run") as run:
            for character in ts.READONLY_FORBIDDEN_CHARACTERS:
                with self.subTest(character=character):
                    r = ts.do_exec("production", f"uptime {character} whoami")
                    self.assertEqual(r["error"], "readonly_refused")
                    self.assertEqual(
                        r["token"], ts.READONLY_TOKEN_DISPLAY.get(character, character)
                    )
        run.assert_not_called()

    def test_each_forbidden_character_remains_unrestricted_on_allowed_host(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            for character in ts.READONLY_FORBIDDEN_CHARACTERS:
                with self.subTest(character=character):
                    command = f"uptime {character} whoami"
                    r = ts.do_exec("build-box", command)
                    self.assertTrue(r["ok"])
                    self.assertEqual(run.call_args[0][0][-1], command)
        self.assertEqual(run.call_count, len(ts.READONLY_FORBIDDEN_CHARACTERS))

    def test_sudo_and_off_table_verb_are_structurally_refused(self):
        with mock.patch.object(ts, "read", return_value=READONLY_SAVED), \
             mock.patch("subprocess.run") as run:
            for command, token in (("sudo uptime", "sudo"), ("reboot", "reboot")):
                with self.subTest(command=command):
                    r = ts.do_exec("production", command)
                    self.assertEqual(r["error"], "readonly_refused")
                    self.assertEqual(r["token"], token)
        run.assert_not_called()

    def test_readonly_subcommand_restrictions_are_enforced(self):
        cases = (
            ("systemctl restart app", "restart"),
            ("journalctl --vacuum-time=1d", "--vacuum-time=1d"),
            ("journalctl --rotate", "--rotate"),
            ("pm2 restart app", "restart"),
            ("pm2 logs app", "logs"),
            ("docker stats", "stats"),
            ("docker restart app", "restart"),
        )
        with mock.patch.object(ts, "read", return_value=READONLY_SAVED), \
             mock.patch("subprocess.run") as run:
            for command, token in cases:
                with self.subTest(command=command):
                    r = ts.do_exec("production", command)
                    self.assertEqual(r["error"], "readonly_refused")
                    self.assertEqual(r["token"], token)
        run.assert_not_called()

    def test_allowed_hosts_only_entry_remains_unrestricted(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake) as run:
            r = ts.do_exec("build-box", "sudo reboot; now")
        self.assertTrue(r["ok"])
        run.assert_called_once()

    def test_readonly_host_is_implicitly_allowed(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        cfg = {"readonly_hosts": ["production"]}
        with mock.patch.object(ts, "read", return_value=cfg), \
             mock.patch("subprocess.run", return_value=fake):
            r = ts.do_exec("production", "uptime")
        self.assertTrue(r["ok"])

    def test_exec_empty_command_is_a_clean_error(self):
        with mock.patch.object(ts, "read", return_value=SAVED):
            r = ts.do_exec("build-box", "   ")
        self.assertFalse(r["ok"])
        self.assertIn("empty command", r["stderr"])

    def test_status_summarizes_self_and_peers(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.do_status()
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "Running")
        self.assertEqual(r["self"]["host"], "cachy")
        self.assertEqual(r["self"]["dns"], "cachy.tail0.ts.net")  # trailing dot stripped
        hosts = sorted(p["host"] for p in r["peers"])
        self.assertEqual(hosts, ["build-box", "deploy-target"])

    def test_configured_cli_reflects_a_linked_tailnet(self):
        # `./sc ts-broker-up` calls `ts.py configured` to self-skip when unlinked.
        with mock.patch.object(ts, "read", return_value=SAVED):
            self.assertEqual(ts.main(["configured"]), 0)
        with mock.patch.object(ts, "read", return_value=None):
            self.assertEqual(ts.main(["configured"]), 1)


class CheckTests(unittest.TestCase):
    """validate() runs one live check against a CANDIDATE block."""

    def test_auth_passes_when_backend_running(self):
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.validate("auth", SAVED)
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "auth")

    def test_auth_fails_when_not_logged_in(self):
        stopped = json.dumps({"BackendState": "NeedsLogin", "Self": {}, "Peer": {}})
        with mock.patch.object(ts, "_run", return_value=(True, stopped)):
            r = ts.validate("auth", SAVED)
        self.assertFalse(r["ok"])
        self.assertIn("tailscale up", r["output"])

    def test_peer_flags_a_missing_allowed_host(self):
        cfg = dict(SAVED, allowed_hosts=["build-box", "ghost"])
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.validate("peer", cfg)
        self.assertFalse(r["ok"])
        self.assertIn("ghost", r["output"])

    def test_peer_and_ssh_checks_include_implicitly_allowed_readonly_hosts(self):
        cfg = {"readonly_hosts": ["build-box"], "ssh_user": "tester"}
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            peer = ts.validate("peer", cfg)
        self.assertTrue(peer["ok"])
        with mock.patch.object(ts, "_run", return_value=(True, "ok")) as run:
            ssh = ts.validate("ssh", cfg)
        self.assertTrue(ssh["ok"])
        self.assertIn("tester@build-box", run.call_args[0][0])

    def test_unknown_check_is_none(self):
        self.assertIsNone(ts.validate("nope", SAVED))

    def test_per_host_user_overrides_the_block_default(self):
        cfg = dict(SAVED, ssh_users={"deploy-target": "jedi"})
        self.assertEqual(
            ts._ssh_argv(cfg, "build-box", "id")[2], "tester@build-box")
        self.assertEqual(
            ts._ssh_argv(cfg, "deploy-target", "id")[2], "jedi@deploy-target")
        # A block written before ssh_users existed behaves exactly as before.
        self.assertEqual(
            ts._ssh_argv(SAVED, "deploy-target", "id")[2], "tester@deploy-target")

    def test_exec_uses_the_per_host_user(self):
        cfg = dict(SAVED, ssh_users={"deploy-target": "jedi"})
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(ts, "read", return_value=cfg), \
             mock.patch.object(ts.subprocess, "run", return_value=fake) as run:
            ts.do_exec("deploy-target", "uptime")
        self.assertIn("jedi@deploy-target", run.call_args[0][0])

    def test_scoped_ssh_check_probes_every_named_host_as_its_own_user(self):
        cfg = dict(SAVED, ssh_users={"deploy-target": "jedi"})
        with mock.patch.object(ts, "_run", return_value=(True, "ok")) as run:
            result = ts.validate("ssh", cfg, ["build-box", "deploy-target"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            [call.args[0][2] for call in run.call_args_list],
            ["tester@build-box", "jedi@deploy-target"],
        )

    def test_scoped_ssh_check_names_the_host_that_failed(self):
        def fake(argv, timeout=30):
            return ("build-box" in argv[2], "denied")
        with mock.patch.object(ts, "_run", side_effect=fake):
            result = ts.validate("ssh", SAVED, ["build-box", "deploy-target"])
        self.assertFalse(result["ok"])
        self.assertIn("deploy-target", result["output"])
        self.assertNotIn("'build-box'", result["output"])


class SocketTransportTests(unittest.TestCase):
    """A live broker on a temp socket, driven by the real broker_call client —
    proves the unix-socket HTTP transport the container relies on actually works."""

    def setUp(self):
        self.sock = Path(__file__).resolve().parent / "_test_ts_broker.sock"
        self._orig_socket = ts.SOCKET
        ts.SOCKET = self.sock  # both server (ts_broker path) + client read this
        self.srv = ts_broker.UnixHTTPServer(str(self.sock), ts_broker.Handler)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ts.SOCKET = self._orig_socket
        self.sock.unlink(missing_ok=True)

    def test_health(self):
        r = ts.broker_call("GET", "/health")
        self.assertEqual(r, {"ok": True, "service": "ts-broker"})

    def test_unknown_route_is_404_shaped(self):
        r = ts.broker_call("GET", "/nope")
        self.assertFalse(r["ok"])

    def test_validate_proxies_the_candidate_cfg_in_the_body(self):
        # The in-sandbox server proxies validate through exactly this path.
        with mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.broker_call("POST", "/validate/auth", {"ts": SAVED})
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "auth")

    def test_exec_round_trips_over_the_socket(self):
        fake = mock.Mock(returncode=2, stdout="out", stderr="err")
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch("subprocess.run", return_value=fake):
            r = ts.broker_call("POST", "/exec",
                               {"host": "build-box", "command": "exit 2"})
        self.assertEqual(r["exit"], 2)
        self.assertEqual(r["stdout"], "out")

    def test_readonly_refusal_round_trips_over_the_socket(self):
        with mock.patch.object(ts, "read", return_value=READONLY_SAVED), \
             mock.patch("subprocess.run") as run:
            r = ts.broker_call(
                "POST", "/exec", {"host": "production", "command": "uptime\nreboot"}
            )
        self.assertEqual(r["error"], "readonly_refused")
        self.assertEqual(r["token"], r"\n")
        run.assert_not_called()

    def test_status_round_trips_over_the_socket(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "_run", return_value=(True, STATUS_JSON)):
            r = ts.broker_call("GET", "/status")
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "Running")

    def test_failed_config_save_is_a_structured_reply_not_an_empty_one(self):
        # Issue #1576: an exception in the PUT handler closed the connection
        # with no reply. The save failure must come back as JSON and the
        # broker must keep serving.
        with mock.patch.object(
            ts, "write", side_effect=PermissionError(13, "Permission denied")
        ):
            r = ts.broker_call("PUT", "/ts", {"ts": dict(SAVED)})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "ts_write_failed")
        self.assertIn("not saved", r["message"])
        self.assertIn("./sc ts init", r["message"])
        self.assertTrue(ts.broker_call("GET", "/health")["ok"])

    def test_a_fault_in_a_get_route_is_still_json(self):
        with mock.patch.object(ts, "do_status", side_effect=RuntimeError("boom")):
            r = ts.broker_call("GET", "/status")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])

    def test_validate_passes_the_host_scope_through(self):
        with mock.patch.object(ts, "_run", return_value=(True, "ok")) as run:
            r = ts.broker_call(
                "POST", "/validate/ssh",
                {"ts": dict(SAVED), "hosts": ["deploy-target"]},
            )
        self.assertTrue(r["ok"])
        self.assertEqual(run.call_count, 1)
        self.assertIn("tester@deploy-target", run.call_args[0][0])

    def test_broker_call_raises_when_nothing_listens(self):
        ts.SOCKET = self.sock.with_name("_absent.sock")
        with self.assertRaises(ConnectionError):
            ts.broker_call("GET", "/health")


class ClientTests(unittest.TestCase):
    def setUp(self):
        # No client test may start a real broker or run the real tailscale
        # CLI: both seams fail loudly unless a test replaces them.
        for name in ("_dispatch_broker_up", "_run"):
            patcher = mock.patch.object(
                ts, name, side_effect=AssertionError(f"unexpected real {name}")
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(ts.os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        ts.os.environ.pop("SC_SANDBOX", None)

    def _run(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ts.client_main(argv)
        return code, json.loads(output.getvalue())

    def _passing(self):
        """Every validator passes, recording (check, hosts) per call."""
        calls = []

        def fake(check, cfg, hosts=None):
            calls.append((check, list(hosts or [])))
            return {"ok": True, "output": "fine", "check": check}

        return calls, mock.patch.object(ts, "validate", side_effect=fake)

    def test_status_calls_the_broker(self):
        response = {"ok": True, "backend": "Running", "self": {}, "peers": []}
        with mock.patch.object(ts, "broker_call", return_value=response) as call:
            code, result = self._run(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(result, response)
        call.assert_called_once_with("GET", "/status")

    def test_exec_calls_the_broker_with_command_after_separator(self):
        response = {"ok": True, "exit": 0, "stdout": "up", "stderr": ""}
        with mock.patch.object(ts, "broker_call", return_value=response) as call:
            code, result = self._run(["exec", "production", "--", "uptime"])
        self.assertEqual(code, 0)
        self.assertEqual(result, response)
        call.assert_called_once_with(
            "POST", "/exec", {"host": "production", "command": "uptime"}
        )

    # -- status: nothing linked vs broker down ------------------------------

    def test_status_with_nothing_linked_says_so_instead_of_broker_down(self):
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "broker_call", side_effect=ConnectionError("x")):
            code, result = self._run(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "ts_not_linked")
        self.assertIn("./sc ts init", result["message"])
        self.assertNotIn("ts-broker-up", result["message"])

    def test_status_with_a_linked_tailnet_and_no_broker_names_the_start(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "broker_call", side_effect=ConnectionError("x")):
            code, result = self._run(["exec", "build-box", "--", "uptime"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "broker_unreachable")
        self.assertEqual(result["operation"], "exec")
        self.assertEqual(result["start_command"], "./sc ts-broker-up")

    # -- init ---------------------------------------------------------------

    def test_init_preserves_unspecified_values_and_reports_broker_health(self):
        current = {"ssh_user": "ops", "allowed_hosts": ["build-box"]}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            code, result = self._run([
                "init", "--readonly-host", "production",
                "--tailscale-bin", "/bin/ts", "--no-verify",
            ])
        expected = {
            "ssh_user": "ops",
            "allowed_hosts": ["build-box"],
            "readonly_hosts": ["production"],
            "tailscale_bin": "/bin/ts",
        }
        self.assertEqual(code, 0)
        write.assert_called_once_with(expected)
        self.assertEqual(result["ts"], expected)
        self.assertFalse(result["verified"])
        self.assertTrue(result["broker"]["ready"])
        self.assertEqual(result["broker"]["start_command"], "./sc ts-broker-up")

    def test_no_verify_init_reports_how_to_start_a_down_broker(self):
        # The save-only form: nothing is tested and nothing is started.
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", side_effect=ConnectionError("down")):
            code, result = self._run(["init", "--no-verify"])
        self.assertEqual(code, 0)
        self.assertFalse(result["broker"]["ready"])
        self.assertEqual(result["broker"]["start_command"], "./sc ts-broker-up")

    def test_init_writes_only_ts_and_preserves_vm_and_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "instance.json"
            vm_block = {"domain": "dev-vm", "snapshot": "clean"}
            initial = {"port": 8837, "dev_port": 4173, "vm": vm_block}
            config.write_text(json.dumps(initial) + "\n")
            with mock.patch.object(ts.ports, "CONFIG", config), \
                 mock.patch.object(ts, "broker_call", side_effect=ConnectionError("down")):
                code, result = self._run([
                    "init", "--ssh-user", "ops", "--readonly-host", "production",
                    "--no-verify",
                ])
            written = json.loads(config.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(written["port"], initial["port"])
        self.assertEqual(written["dev_port"], initial["dev_port"])
        self.assertEqual(written["vm"], vm_block)
        self.assertEqual(written["ts"], result["ts"])

    def test_init_adds_to_the_saved_hosts_instead_of_replacing_them(self):
        # Issue #1598: `init --ssh-user jedi --allowed-host mac-test` dropped
        # every host already declared.
        current = {"ssh_user": "jed", "allowed_hosts": ["dev"],
                   "readonly_hosts": ["blade"], "tailscale_bin": "tailscale"}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            code, result = self._run(
                ["init", "--allowed-host", "jedi@mac-test", "--no-verify"])
        self.assertEqual(code, 0)
        block = write.call_args[0][0]
        self.assertEqual(block["allowed_hosts"], ["dev", "mac-test"])
        self.assertEqual(block["readonly_hosts"], ["blade"])
        self.assertEqual(block["ssh_user"], "jed")
        self.assertEqual(block["ssh_users"], {"mac-test": "jedi"})
        self.assertEqual(result["hosts"]["added"], ["mac-test"])
        self.assertEqual(result["hosts"]["kept"], ["dev", "blade"])
        self.assertEqual(result["hosts"]["removed"], [])

    def test_replace_hosts_keeps_the_wholesale_form_and_reports_the_drop(self):
        current = {"ssh_user": "jed", "allowed_hosts": ["dev", "old"],
                   "readonly_hosts": ["blade"], "ssh_users": {"old": "x"}}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            code, result = self._run([
                "init", "--allowed-host", "dev", "--replace-hosts", "--no-verify"])
        self.assertEqual(code, 0)
        block = write.call_args[0][0]
        self.assertEqual(block["allowed_hosts"], ["dev"])
        self.assertEqual(block["readonly_hosts"], ["blade"])  # not named: kept
        self.assertNotIn("ssh_users", block)  # pruned with its host
        self.assertEqual(result["hosts"]["removed"], ["old"])

    def test_naming_a_readonly_host_as_allowed_never_loosens_it(self):
        current = {"ssh_user": "jed", "allowed_hosts": [], "readonly_hosts": ["blade"]}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            code, result = self._run(
                ["init", "--allowed-host", "blade", "--no-verify"])
        block = write.call_args[0][0]
        self.assertEqual(code, 0)
        self.assertIn("blade", block["readonly_hosts"])
        self.assertIn("stays read-only", result["hosts"]["notes"][0])
        with mock.patch.object(ts, "read", return_value=block):
            self.assertIsNotNone(ts.do_exec("blade", "rm -rf /x").get("error"))

    def test_naming_an_allowed_host_readonly_tightens_it(self):
        current = {"ssh_user": "jed", "allowed_hosts": ["dev", "blade"]}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            self._run(["init", "--readonly-host", "blade", "--no-verify"])
        block = write.call_args[0][0]
        self.assertEqual(block["allowed_hosts"], ["dev"])
        self.assertEqual(block["readonly_hosts"], ["blade"])

    # -- add-host / remove-host ---------------------------------------------

    def test_add_host_keeps_every_other_host_and_verifies_only_the_new_one(self):
        current = {"ssh_user": "jed", "allowed_hosts": ["dev"],
                   "readonly_hosts": ["blade"], "tailscale_bin": "tailscale"}
        calls, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}), \
             passing:
            code, result = self._run(["add-host", "jedi@mac-test"])
        self.assertEqual(code, 0)
        block = write.call_args[0][0]
        self.assertEqual(block["allowed_hosts"], ["dev", "mac-test"])
        self.assertEqual(block["readonly_hosts"], ["blade"])
        self.assertEqual(block["ssh_users"], {"mac-test": "jedi"})
        self.assertTrue(result["verified"])
        self.assertEqual(
            calls,
            [(check, ["mac-test"]) for check in ts.CHECKS],
        )

    def test_add_host_readonly_declares_the_diagnostic_tier(self):
        _, passing = self._passing()
        with mock.patch.object(ts, "read", return_value={"ssh_user": "jed"}), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}), \
             passing:
            code, _ = self._run(["add-host", "blade", "--readonly"])
        self.assertEqual(code, 0)
        self.assertEqual(write.call_args[0][0]["readonly_hosts"], ["blade"])
        self.assertEqual(write.call_args[0][0]["allowed_hosts"], [])

    def test_add_host_refuses_to_loosen_a_readonly_host(self):
        current = {"ssh_user": "jed", "readonly_hosts": ["blade"]}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write:
            code, result = self._run(["add-host", "blade"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "host_is_readonly")
        write.assert_not_called()

    def test_remove_host_drops_one_host_and_its_user(self):
        current = {"ssh_user": "jed", "allowed_hosts": ["dev", "mac-test"],
                   "readonly_hosts": ["blade"], "ssh_users": {"mac-test": "jedi"}}
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}):
            code, result = self._run(["remove-host", "mac-test"])
        self.assertEqual(code, 0)
        block = write.call_args[0][0]
        self.assertEqual(block["allowed_hosts"], ["dev"])
        self.assertEqual(block["readonly_hosts"], ["blade"])
        self.assertNotIn("ssh_users", block)
        self.assertEqual(result["hosts"]["removed"], ["mac-test"])

    def test_remove_host_of_an_undeclared_host_changes_nothing(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "write") as write:
            code, result = self._run(["remove-host", "ghost"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "host_not_declared")
        write.assert_not_called()

    def test_a_malformed_host_is_refused_before_any_write(self):
        with mock.patch.object(ts, "read", return_value=SAVED), \
             mock.patch.object(ts, "write") as write:
            code, result = self._run(["add-host", "jedi@"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "host_invalid")
        write.assert_not_called()

    # -- link-time verification ---------------------------------------------

    def test_init_starts_a_down_broker_and_verifies_the_link(self):
        health = iter([ConnectionError("down"), {"ok": True}])

        def broker(method, path, body=None, timeout=130):
            value = next(health)
            if isinstance(value, Exception):
                raise value
            return value

        calls, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", side_effect=broker), \
             mock.patch.object(
                 ts, "_dispatch_broker_up", return_value=(True, "up")) as up, \
             passing:
            code, result = self._run(
                ["init", "--ssh-user", "ops", "--allowed-host", "build-box"])
        self.assertEqual(code, 0)
        up.assert_called_once_with()
        self.assertTrue(result["broker"]["ready"])
        self.assertTrue(result["broker"]["started"])
        self.assertTrue(result["verified"])
        self.assertEqual([c for c, _ in calls], list(ts.CHECKS))

    def test_a_failed_check_is_reported_per_check_with_the_next_command(self):
        def fake(check, cfg, hosts=None):
            return {"ok": check != "auth", "output": "NeedsLogin", "check": check}

        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write") as write, \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}), \
             mock.patch.object(ts, "validate", side_effect=fake):
            code, result = self._run(
                ["init", "--ssh-user", "ops", "--allowed-host", "build-box"])
        self.assertEqual(code, 1)
        write.assert_called_once()  # saved, and the result says so
        self.assertEqual(result["error"], "ts_verify_failed")
        self.assertTrue(result["saved"])
        self.assertIn("IS saved", result["message"])
        by_name = {item["check"]: item for item in result["verify"]["checks"]}
        self.assertTrue(by_name["daemon"]["ok"])
        self.assertFalse(by_name["auth"]["ok"])
        self.assertIn("tailscale up", by_name["auth"]["next"])
        # One cause, reported once: the dependents are skipped, not re-failed.
        self.assertTrue(by_name["peer"]["skipped"])
        self.assertTrue(by_name["ssh"]["skipped"])

    def test_a_block_with_no_hosts_verifies_the_node_and_says_exec_is_refused(self):
        calls, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}), \
             passing:
            code, result = self._run(["init", "--ssh-user", "ops"])
        self.assertEqual(code, 0)
        self.assertEqual([c for c, _ in calls], ["daemon", "auth"])
        self.assertIn("add-host", result["verify"]["note"])

    def test_when_the_broker_cannot_be_started_the_exact_command_is_given(self):
        _, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", side_effect=ConnectionError("x")), \
             mock.patch.object(
                 ts, "_dispatch_broker_up", return_value=(False, "run/ not writable")), \
             passing:
            code, result = self._run(
                ["init", "--ssh-user", "ops", "--allowed-host", "build-box"])
        self.assertEqual(code, 0)  # the link itself verified
        self.assertFalse(result["broker"]["ready"])
        self.assertEqual(result["broker"]["start_command"], "./sc ts-broker-up")
        self.assertIn("not writable", result["broker"]["detail"])

    def test_a_sandboxed_caller_verifies_through_the_broker_and_never_starts_it(self):
        seen = []

        def broker(method, path, body=None, timeout=130):
            seen.append((method, path, body))
            return {"ok": True, "output": "fine"}

        ts.os.environ["SC_SANDBOX"] = "1"
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", side_effect=broker):
            code, result = self._run(
                ["init", "--ssh-user", "ops", "--allowed-host", "build-box"])
        self.assertEqual(code, 0)
        self.assertIn(
            ("POST", "/validate/ssh",
             {"ts": result["ts"], "hosts": ["build-box"]}),
            seen,
        )

    def test_a_failed_save_is_never_reported_as_saved(self):
        with mock.patch.object(ts, "read", return_value=None), \
             mock.patch.object(ts, "write", side_effect=PermissionError("denied")):
            code, result = self._run(["init", "--ssh-user", "ops"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "ts_write_failed")
        self.assertNotIn("ts", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
