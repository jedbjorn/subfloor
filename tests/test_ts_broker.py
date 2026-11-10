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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_seam_guard  # noqa: E402

# Every test in this module: no real ssh/scp/virsh/tailscale/dispatcher child
# process and no call on a real broker socket. See tests/seat_seam_guard.py.
seat_seam_guard.install(globals())

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

    def test_init_refuses_to_name_a_readonly_host_as_allowed(self):
        current = {"ssh_user": "jed", "allowed_hosts": ["seat"],
                   "readonly_hosts": ["prod"]}
        for argv in (
            ["init", "--allowed-host", "root@prod"],
            ["init", "--allowed-host", "prod", "--replace-hosts"],
            ["init", "--allowed-host", "new", "--allowed-host", "root@prod",
             "--ssh-user", "other", "--no-verify"],
            ["add-host", "root@prod"],
        ):
            with self.subTest(argv=argv), \
                 mock.patch.object(ts, "read", return_value=current), \
                 mock.patch.object(ts, "write") as write:
                code, result = self._run(argv)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"], "host_is_readonly")
                self.assertEqual(result["host"], "prod")
                self.assertIn("nothing was saved", result["message"])
                # A refused change changes NOTHING: no list, no per-host
                # user, no ssh_user, no other host named beside it.
                write.assert_not_called()

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

    def test_init_naming_no_host_checks_the_broker_only_and_says_so(self):
        # `ts init --ssh-user X` used to probe every declared host, machines
        # in real use included, 20s each; one sleeping host failed it.
        current = {"ssh_user": "old", "allowed_hosts": ["seat"],
                   "readonly_hosts": ["prod", "asleep"]}
        calls, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}) as broker, \
             passing:
            code, result = self._run(["init", "--ssh-user", "ops"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])  # nothing on the tailnet was probed
        broker.assert_called_once_with("GET", "/health", timeout=5)
        self.assertTrue(result["broker"]["ready"])
        self.assertEqual(result["verify"]["checks"], [])
        self.assertIn("only the broker was checked", result["verify"]["note"])
        self.assertIn("add-host", result["verify"]["note"])

    def test_init_verifies_only_the_hosts_it_names(self):
        current = {"ssh_user": "ops", "allowed_hosts": ["seat"],
                   "readonly_hosts": ["prod"]}
        calls, passing = self._passing()
        with mock.patch.object(ts, "read", return_value=current), \
             mock.patch.object(ts, "write"), \
             mock.patch.object(ts, "broker_call", return_value={"ok": True}), \
             passing:
            code, _ = self._run(["init", "--allowed-host", "seat2"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(check, ["seat2"]) for check in ts.CHECKS])

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


class _StatefulClient(unittest.TestCase):
    """Runs client verbs against an in-memory block through the REAL
    `ts.write` (so its validation and tier normalization are exercised); only
    the instance-file write underneath is replaced."""

    def setUp(self):
        self.block: dict | None = None

        def update(values, remove=()):
            if "ts" in values:
                self.block = json.loads(json.dumps(values["ts"]))
            if "ts" in remove:
                self.block = None

        for target, name, kwargs in (
            (ts, "read", {"side_effect": lambda: (
                json.loads(json.dumps(self.block)) if self.block else None)}),
            (ts.ports, "update", {"side_effect": update}),
            (ts, "broker_call", {"return_value": {"ok": True}}),
            (ts, "validate", {"side_effect": lambda check, cfg, hosts=None: {
                "ok": True, "output": "fine", "check": check}}),
        ):
            patcher = mock.patch.object(target, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(ts.os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        ts.os.environ.pop("SC_SANDBOX", None)

    def run_client(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ts.client_main(list(argv))
        return code, json.loads(output.getvalue())

    def assert_tiers_disjoint(self, context=""):
        block = self.block or {}
        both = set(block.get("allowed_hosts") or []) & set(
            block.get("readonly_hosts") or [])
        self.assertEqual(both, set(), f"host in both tiers after {context}: {block}")


class TierInvariantTests(_StatefulClient):
    """A read-only host never reaches the exec tier, and no host is ever
    saved in both tiers, across any sequence of commands."""

    def test_the_reviewed_two_step_sequence_cannot_loosen_a_readonly_host(self):
        self.block = {"ssh_user": "ops", "allowed_hosts": ["seat"],
                      "readonly_hosts": ["prod"], "tailscale_bin": "tailscale"}
        before = json.loads(json.dumps(self.block))

        code, result = self.run_client("init", "--allowed-host", "root@prod")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "host_is_readonly")
        self.assertEqual(self.block, before)  # incl. no ssh_users[prod]=root

        code, result = self.run_client(
            "init", "--replace-hosts", "--readonly-host", "other")
        self.assertEqual(code, 0)
        self.assertEqual(self.block["allowed_hosts"], ["seat"])
        self.assertEqual(self.block["readonly_hosts"], ["other"])
        self.assertEqual(result["hosts"]["removed"], ["prod"])
        self.assertNotIn("ssh_users", self.block)
        # prod is no longer declared at all, so exec is refused outright.
        with mock.patch("subprocess.run") as run:
            refused = ts.do_exec("prod", "rm -rf /srv")
        self.assertFalse(refused["ok"])
        run.assert_not_called()

    def test_a_saved_block_with_a_host_in_both_tiers_is_read_as_readonly(self):
        # What the pre-fix code could write (or a hand edit). Replacing the
        # read-only list must DROP the host, not leave it allowed.
        self.block = {"ssh_user": "ops", "allowed_hosts": ["seat", "prod"],
                      "readonly_hosts": ["prod"], "ssh_users": {"prod": "root"}}
        code, result = self.run_client(
            "init", "--replace-hosts", "--readonly-host", "other", "--no-verify")
        self.assertEqual(code, 0)
        self.assertEqual(self.block["allowed_hosts"], ["seat"])
        self.assertEqual(self.block["readonly_hosts"], ["other"])
        self.assertEqual(result["hosts"]["removed"], ["prod"])
        self.assertNotIn("ssh_users", self.block)

    def test_any_write_of_a_both_tier_block_saves_it_readonly_only(self):
        for argv in (
            ("init", "--ssh-user", "x", "--no-verify"),
            ("add-host", "new", "--no-verify"),
            ("remove-host", "seat"),
        ):
            with self.subTest(argv=argv):
                self.block = {"ssh_user": "ops", "allowed_hosts": ["seat", "prod"],
                              "readonly_hosts": ["prod"]}
                code, _ = self.run_client(*argv)
                self.assertEqual(code, 0)
                self.assert_tiers_disjoint(str(argv))
                self.assertIn("prod", self.block["readonly_hosts"])

    def test_no_sequence_of_commands_leaves_a_host_in_both_tiers(self):
        steps = (
            ("init", "--ssh-user", "ops", "--allowed-host", "a",
             "--readonly-host", "p"),
            ("init", "--allowed-host", "p"),                    # refused
            ("add-host", "p"),                                  # refused
            ("add-host", "u@b"),
            ("init", "--readonly-host", "a"),                   # tightens a
            ("init", "--allowed-host", "a"),                    # refused
            ("init", "--allowed-host", "c", "--readonly-host", "c"),
            ("add-host", "b", "--readonly"),                    # tightens b
            ("init", "--replace-hosts", "--allowed-host", "d",
             "--readonly-host", "d"),
            ("init", "--replace-hosts", "--allowed-host", "p"),  # refused or new
            ("init", "--replace-hosts", "--readonly-host", "q"),
            ("remove-host", "q"),
            ("add-host", "q"),               # two deliberate commands: allowed
            ("init", "--replace-hosts", "--allowed-host", "e",
             "--readonly-host", "q"),
            ("remove-host", "nope"),                            # refused
        )
        readonly_ever = set()
        for step in steps:
            before = json.loads(json.dumps(self.block)) if self.block else None
            was_readonly = set((before or {}).get("readonly_hosts") or [])
            code, result = self.run_client(*step, *(
                ["--no-verify"] if step[0] != "remove-host" else []))
            self.assert_tiers_disjoint(str(step))
            if code != 0:
                self.assertEqual(self.block, before, f"refused {step} wrote")
            # A host that was read-only before this command is never allowed
            # after it: loosening takes remove-host, then a second command.
            self.assertEqual(
                was_readonly & set((self.block or {}).get("allowed_hosts") or []),
                set(), f"{step} loosened a read-only host")
            readonly_ever |= was_readonly
        self.assertTrue(readonly_ever)

    def test_put_ts_cannot_save_a_host_in_both_tiers(self):
        saved = ts.write({"ssh_user": "ops", "allowed_hosts": ["seat", "prod"],
                          "readonly_hosts": ["prod"]})
        self.assertEqual(saved["allowed_hosts"], ["seat"])
        self.assert_tiers_disjoint("write")


class NameValidationTests(_StatefulClient):
    """Host and user names become one `tailscale ssh` argv element; nothing
    option-shaped may be saved or run."""

    BAD_ARGUMENTS = (
        "-oProxyCommand=x@h",   # user would be `-oProxyCommand=x`
        "-h",
        "u@-oFoo",
        "--help",
        "u@",
        "@h",
        "u@h h",
        "a b",
        "h;id",
        "u$(id)@h",
        "h/../x",
        "",
    )

    def test_parse_host_rejects_option_shaped_and_unsafe_names(self):
        for value in self.BAD_ARGUMENTS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ts._parse_host(value)

    def test_parse_host_accepts_real_tailnet_names(self):
        for value, expected in (
            ("build-box", ("build-box", "")),
            ("jedi@mac-test", ("mac-test", "jedi")),
            ("box.tail0.ts.net", ("box.tail0.ts.net", "")),
            ("svc_user@100.64.0.2", ("100.64.0.2", "svc_user")),
            ("first.last@h1", ("h1", "first.last")),
        ):
            with self.subTest(value=value):
                self.assertEqual(ts._parse_host(value), expected)

    def test_the_client_refuses_bad_names_before_any_write(self):
        self.block = dict(SAVED)
        before = dict(SAVED)
        for argv in (
            ["init", "--allowed-host=-oProxyCommand=x@h"],
            ["init", "--readonly-host=u@-oFoo"],
            ["init", "--ssh-user=-oProxyCommand=x"],
            ["init", "--allowed-host", "ok-host", "--readonly-host=-h"],
            ["add-host", "u@-oFoo"],
        ):
            with self.subTest(argv=argv):
                code, result = self.run_client(*argv)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"], "host_invalid")
                self.assertEqual(self.block, before)

    def test_write_refuses_a_block_with_a_bad_name_wherever_it_comes_from(self):
        for block in (
            {"allowed_hosts": ["-h"]},
            {"readonly_hosts": ["ok", "u@-oFoo"]},
            {"allowed_hosts": ["h"], "ssh_user": "-oProxyCommand=x"},
            {"allowed_hosts": ["h"], "ssh_users": {"h": "-oProxyCommand=x"}},
            {"allowed_hosts": ["h"], "ssh_users": {"-h": "u"}},
            {"allowed_hosts": ["h"], "ssh_users": ["h"]},
            {"allowed_hosts": "h"},
        ):
            with self.subTest(block=block):
                self.block = None
                with self.assertRaises(ts.BlockInvalid):
                    ts.write(block)
                self.assertIsNone(self.block)

    def test_exec_never_builds_an_option_shaped_target_from_a_hand_edit(self):
        # instance.json can be edited by hand, bypassing every write check.
        self.block = {"allowed_hosts": ["h"], "ssh_users": {"h": "-oProxyCommand=x"}}
        with mock.patch("subprocess.run") as run:
            result = ts.do_exec("h", "uptime")
        self.assertFalse(result["ok"])
        self.assertIn("not a valid name", result["stderr"])
        run.assert_not_called()
        self.block = {"allowed_hosts": ["-oFoo"]}
        with mock.patch("subprocess.run") as run:
            self.assertFalse(ts.do_exec("-oFoo", "uptime")["ok"])
        run.assert_not_called()

    def test_a_hand_edited_bad_entry_can_still_be_removed(self):
        self.block = {"ssh_user": "ops", "allowed_hosts": ["good", "-bad"]}
        code, _ = self.run_client("add-host", "new", "--no-verify")
        self.assertEqual(code, 1)  # the block is refused as a whole, loudly
        code, _ = self.run_client("remove-host", "--", "-bad")
        self.assertEqual(code, 0)
        self.assertEqual(self.block["allowed_hosts"], ["good"])


class PutValidationTests(unittest.TestCase):
    """`PUT /ts` accepted any ssh_users map unvalidated."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.sock = Path(self._dir.name) / "ts.sock"
        patcher = mock.patch.object(ts, "SOCKET", self.sock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.updates = []
        patcher = mock.patch.object(
            ts.ports, "update",
            side_effect=lambda values, remove=(): self.updates.append(values))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.srv = ts_broker.UnixHTTPServer(str(self.sock), ts_broker.Handler)
        thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def test_put_refuses_option_shaped_names_and_saves_nothing(self):
        for block in (
            {"allowed_hosts": ["h"], "ssh_users": {"h": "-oProxyCommand=x"}},
            {"allowed_hosts": ["-oFoo"]},
            {"readonly_hosts": ["h"], "ssh_user": "-l"},
        ):
            with self.subTest(block=block):
                reply = ts.broker_call("PUT", "/ts", {"ts": block})
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "ts_block_invalid")
                self.assertIn("not saved", reply["message"])
        self.assertEqual(self.updates, [])
        self.assertTrue(ts.broker_call("GET", "/health")["ok"])

    def test_put_saves_a_valid_block_with_per_host_users(self):
        block = {"ssh_user": "ops", "allowed_hosts": ["seat"],
                 "readonly_hosts": ["prod"], "ssh_users": {"seat": "jedi"}}
        reply = ts.broker_call("PUT", "/ts", {"ts": block})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.updates, [{"ts": block}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
