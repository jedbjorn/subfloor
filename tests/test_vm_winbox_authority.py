#!/usr/bin/env python3
"""Spec #232 work unit 1 — broker authority and SSH transfer.

What this pins, at the vm._run / subprocess seams the sibling VM tests use:

* the `transfer` check is retired (validate returns None -> the broker's 404)
* `_check_toolchain` runs the fork's declared winbox checks, not a hard-coded
  `dotnet --version`, and passes when none are declared
* ssh and scp carry the pinned known_hosts and the configured port
* push/pull containment, argv shape, and non-ASCII guest paths
* `reset --off` still lands powered off after a LIVE snapshot revert
* no shipped VM surface still names the retired skills

Run:
    python3 tests/test_vm_winbox_authority.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import vm  # noqa: E402
import vm_broker  # noqa: E402
import winbox  # noqa: E402

SAVED = {
    "domain": "win-test",
    "snapshot": "clean",
    "ssh_host": "192.168.122.100",
    "ssh_user": "tester",
    "ssh_key_path": "/host/key",
    "ssh_port": 2222,
    "known_hosts_path": "/host/state/win-test.known_hosts",
    "workspace": "C:\\SubfloorTest",
}


def _profile(**overrides) -> dict:
    return {
        "winget_manifest": None,
        "checks": [],
        "mcp": True,
        "mcp_port": 8000,
        "workspace": "C:\\SubfloorTest",
        "source": None,
        **overrides,
    }


class RetiredTransferCheckTests(unittest.TestCase):
    def test_transfer_is_no_longer_a_check(self):
        self.assertEqual(vm.CHECKS, ("domain", "ssh", "snapshot", "toolchain"))
        self.assertNotIn("transfer", vm._CHECKS)
        self.assertFalse(hasattr(vm, "_check_transfer"))
        self.assertIsNone(vm.validate("transfer", SAVED))

    def test_validate_transfer_falls_through_to_the_broker_404(self):
        handler = object.__new__(vm_broker.Handler)
        handler._response_started = False
        handler._request_id = "request123"
        handler._request_started = 0.0
        handler.command = "POST"
        handler.path = "/validate/transfer"
        handler._body = mock.Mock(return_value={"vm": SAVED})
        handler._send = mock.Mock()
        handler.do_POST()
        handler._send.assert_called_once_with(
            404, {"ok": False, "error": "no such check"}
        )


class ToolchainCheckTests(unittest.TestCase):
    def test_no_declared_checks_passes(self):
        with mock.patch.object(winbox, "load", return_value=_profile()), \
             mock.patch.object(vm, "_run") as run:
            ok, output = vm._check_toolchain(SAVED)
        self.assertTrue(ok)
        self.assertEqual(output, "no checks declared")
        run.assert_not_called()

    def test_declared_checks_run_over_ssh_in_order(self):
        profile = _profile(checks=["dotnet --version", "git --version"])
        seen = []

        def fake_run(argv, timeout=30):
            seen.append(argv[-1])
            return True, "9.0.100"

        with mock.patch.object(winbox, "load", return_value=profile), \
             mock.patch.object(vm, "_run", side_effect=fake_run):
            ok, output = vm._check_toolchain(SAVED)
        self.assertTrue(ok, output)
        self.assertEqual(seen, ["dotnet --version", "git --version"])
        self.assertIn("2 declared check(s) passed", output)

    def test_a_failing_check_fails_the_probe_and_names_it(self):
        profile = _profile(checks=["dotnet --version", "uv --version"])

        def fake_run(argv, timeout=30):
            if argv[-1] == "uv --version":
                return False, "'uv' is not recognized"
            return True, "9.0.100"

        with mock.patch.object(winbox, "load", return_value=profile), \
             mock.patch.object(vm, "_run", side_effect=fake_run):
            ok, output = vm._check_toolchain(SAVED)
        self.assertFalse(ok)
        self.assertIn("uv --version", output)
        self.assertIn("is not recognized", output)
        self.assertNotIn("dotnet --version:", output)

    def test_an_unusable_winbox_profile_is_surfaced_not_swallowed(self):
        error = winbox.WinboxConfigError("winbox_config_invalid", "checks must be a list")
        with mock.patch.object(winbox, "load", side_effect=error):
            ok, output = vm._check_toolchain(SAVED)
        self.assertFalse(ok)
        self.assertIn("winbox_config_invalid", output)

    def test_nothing_in_vm_py_still_names_the_retired_skills(self):
        source = (ENGINE / "scripts" / "vm.py").read_text()
        for retired in ("configure_winbox", "windows_vm_gui"):
            self.assertNotIn(retired, source)
        self.assertNotIn("dotnet --version", source)


class SshAndScpArgvTests(unittest.TestCase):
    def test_ssh_pins_the_known_hosts_file_and_port(self):
        argv = vm._ssh_argv(SAVED, "echo ok")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("UserKnownHostsFile=/host/state/win-test.known_hosts", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertNotIn("StrictHostKeyChecking=accept-new", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[-2:], ["tester@192.168.122.100", "echo ok"])

    def test_without_a_pin_the_first_contact_still_works(self):
        argv = vm._ssh_argv({k: v for k, v in SAVED.items()
                             if k != "known_hosts_path"}, "echo ok")
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertNotIn("StrictHostKeyChecking=yes", argv)

    def test_scp_shares_the_ssh_options_and_forces_sftp(self):
        argv = vm._scp_argv(SAVED, "/local/file", "tester@host:C:\\dir\\file")
        self.assertEqual(argv[0], "scp")
        self.assertIn("UserKnownHostsFile=/host/state/win-test.known_hosts", argv)
        self.assertIn("-s", argv)  # SFTP: the guest shell never sees the path
        self.assertEqual(argv[argv.index("-P") + 1], "2222")
        self.assertEqual(argv[-3:], ["--", "/local/file", "tester@host:C:\\dir\\file"])

    def test_an_absent_or_bogus_ssh_port_falls_back_to_22(self):
        for port in (None, "", 0, True, 70000, "2222"):
            with self.subTest(port=port):
                cfg = dict(SAVED, ssh_port=port)
                self.assertEqual(vm._ssh_port(cfg), 22)


class PushPullTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="sc_vm_repo_")).resolve()
        (self.repo / ".sc-state" / "local").mkdir(parents=True)
        patcher = mock.patch.object(vm, "repo_root", return_value=self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _artifact(self, name: str, body: bytes = b"payload") -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_push_defaults_to_the_workspace_and_reports_bytes(self):
        source = self._artifact("build/app.msi", b"x" * 1234)
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            result = vm.do_push("build/app.msi")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["destination"], "C:\\SubfloorTest\\app.msi")
        self.assertEqual(result["bytes"], 1234)
        argv = run.call_args[0][0]
        self.assertEqual(argv[-2], str(source))
        self.assertEqual(
            argv[-1], "tester@192.168.122.100:C:\\SubfloorTest\\app.msi"
        )

    def test_push_keeps_non_ascii_names_verbatim_in_the_remote_spec(self):
        # Under SFTP the remote path never reaches cmd.exe, so it is passed
        # unquoted and byte-exact: no shell quoting, no code-page round trip.
        self._artifact("build/Δ rapport ü.txt")
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            result = vm.do_push("build/Δ rapport ü.txt")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["destination"], "C:\\SubfloorTest\\Δ rapport ü.txt"
        )
        remote = run.call_args[0][0][-1]
        self.assertEqual(
            remote, "tester@192.168.122.100:C:\\SubfloorTest\\Δ rapport ü.txt"
        )
        self.assertNotIn('"', remote)

    def test_push_accepts_sc_state_local_and_an_explicit_guest_dest(self):
        source = self.repo / ".sc-state" / "local" / "key.pub"
        source.write_text("ssh-ed25519 AAAA")
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "")) as run:
            result = vm.do_push(str(source), "C:\\ProgramData\\ssh\\probe.pub")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["destination"], "C:\\ProgramData\\ssh\\probe.pub")
        self.assertEqual(
            run.call_args[0][0][-1],
            "tester@192.168.122.100:C:\\ProgramData\\ssh\\probe.pub",
        )

    def test_push_refuses_a_source_outside_the_permitted_roots(self):
        for src in ("~/.ssh/id_ed25519", "/etc/shadow", "../outside.txt"):
            with self.subTest(src=src), \
                 mock.patch.object(vm, "read", return_value=SAVED), \
                 mock.patch.object(vm, "_run") as run:
                result = vm.do_push(src)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "remote_path_not_allowed")
            run.assert_not_called()

    def test_push_reports_a_missing_source_without_calling_scp(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run") as run:
            result = vm.do_push("build/absent.msi")
        self.assertFalse(result["ok"])
        self.assertIn("source not found", result["output"])
        run.assert_not_called()

    def test_push_surfaces_an_scp_failure(self):
        self._artifact("build/app.msi")
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(False, "Permission denied")):
            result = vm.do_push("build/app.msi")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "push_failed")
        self.assertIn("Permission denied", result["output"])

    def test_pull_writes_inside_the_repo_and_reports_bytes(self):
        target = self.repo / ".sc-state" / "local" / "guest" / "Δ out.txt"

        def fake_run(argv, timeout=30):
            Path(argv[-1]).write_bytes(b"seventeen bytes!!")
            return True, ""

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run) as run:
            result = vm.do_pull("C:\\SubfloorTest\\Δ out.txt", str(target))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["destination"], str(target))
        self.assertEqual(result["bytes"], 17)
        argv = run.call_args[0][0]
        self.assertEqual(
            argv[-2], "tester@192.168.122.100:C:\\SubfloorTest\\Δ out.txt"
        )
        self.assertEqual(argv[-1], str(target))

    def test_pull_refuses_a_destination_outside_the_permitted_roots(self):
        for dest in ("/etc/sc_escape_probe", "~/escape.txt", "../escape.txt"):
            with self.subTest(dest=dest), \
                 mock.patch.object(vm, "read", return_value=SAVED), \
                 mock.patch.object(vm, "_run") as run:
                result = vm.do_pull("C:\\SubfloorTest\\out.txt", dest)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "remote_path_not_allowed")
            run.assert_not_called()

    def test_pull_requires_both_paths(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run") as run:
            self.assertFalse(vm.do_pull("", "out.txt")["ok"])
            self.assertFalse(vm.do_pull("C:\\out.txt", "")["ok"])
        run.assert_not_called()

    def test_workspace_falls_back_to_the_winbox_profile(self):
        cfg = {k: v for k, v in SAVED.items() if k != "workspace"}
        with mock.patch.object(
            winbox, "load", return_value=_profile(workspace="D:\\Lab")
        ):
            self.assertEqual(vm._workspace(cfg), "D:\\Lab")
        error = winbox.WinboxConfigError("winbox_config_invalid", "bad")
        with mock.patch.object(winbox, "load", side_effect=error):
            self.assertEqual(vm._workspace(cfg), "C:\\SubfloorTest")


class ResetStateTests(unittest.TestCase):
    def test_off_after_a_live_snapshot_revert_stops_the_domain(self):
        stopped = {
            "ok": True,
            "output": "domain 'win-test' powered off",
            "domain": "win-test",
            "domain_state": "powered_off",
            "stopped": True,
            "forced": False,
        }
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")) as run, \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "do_stop", return_value=stopped) as stop:
            result = vm.do_reset(running=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["domain_state"], "powered_off")
        self.assertEqual(result["reset_outcome"], "confirmed")
        stop.assert_called_once_with()
        self.assertNotIn("--running", run.call_args[0][0])

    def test_off_reports_state_mismatch_when_the_stop_fails(self):
        stopped = {
            "ok": False,
            "error": "stop_timeout",
            "output": "domain did not power off within 60s",
            "domain": "win-test",
            "domain_state": "running",
            "forced": False,
        }
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")), \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "do_stop", return_value=stopped):
            result = vm.do_reset(running=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reset_outcome"], "state_mismatch")

    def test_running_leaves_the_domain_up_and_never_stops_it(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", return_value=(True, "reverted")) as run, \
             mock.patch.object(vm, "_domain_state", return_value=(True, "running")), \
             mock.patch.object(vm, "do_stop") as stop:
            result = vm.do_reset(running=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["domain_state"], "running")
        stop.assert_not_called()
        self.assertIn("--running", run.call_args[0][0])


class BrokerRouteTests(unittest.TestCase):
    def _handler(self, path: str, body: dict):
        handler = object.__new__(vm_broker.Handler)
        handler._response_started = False
        handler._request_id = "request123"
        handler._request_started = 0.0
        handler.command = "POST"
        handler.path = path
        handler.server = mock.Mock(vm_mutation_lock=mock.MagicMock())
        handler.server.vm_mutation_lock.acquire.return_value = True
        handler._body = mock.Mock(return_value=body)
        handler._send = mock.Mock()
        return handler

    def test_bake_and_pull_run_under_the_mutation_lock(self):
        cases = (
            ("/bake", "do_bake", {"name": "project-ready"}),
            ("/pull", "do_pull", {"src": "C:\\out.txt", "dest": "out.txt"}),
            ("/push", "do_push", {"src": "in.txt", "dest": None}),
        )
        for path, verb, body in cases:
            with self.subTest(path=path):
                handler = self._handler(path, body)
                with mock.patch.object(vm, verb, return_value={"ok": True}) as fn:
                    handler.do_POST()
                fn.assert_called_once()
                handler.server.vm_mutation_lock.acquire.assert_called_once_with(
                    timeout=vm.MUTATION_LOCK_TIMEOUT
                )
                handler._send.assert_called_once_with(200, {"ok": True})

    def test_bake_rejects_a_non_string_name_before_touching_the_domain(self):
        handler = self._handler("/bake", {"name": 7})
        with mock.patch.object(vm, "do_bake") as bake:
            handler.do_POST()
        bake.assert_not_called()
        handler._send.assert_called_once_with(
            400, {"ok": False, "error": "name must be a string"}
        )

    def test_reset_rejects_a_non_boolean_running_flag(self):
        handler = self._handler("/reset", {"running": "yes"})
        with mock.patch.object(vm, "do_reset") as reset:
            handler.do_POST()
        reset.assert_not_called()
        handler._send.assert_called_once_with(
            400, {"ok": False, "error": "running must be boolean"}
        )


if __name__ == "__main__":
    unittest.main()
