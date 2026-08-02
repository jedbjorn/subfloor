#!/usr/bin/env python3
"""Tests for `./sc vm-bake` (vm.do_bake) — re-baking the clean snapshot.

The bake is HOST-authority by design: the snapshot is the trust anchor every
`windows_devkit` run reverts to, so a sandboxed shell must never redefine it
(a re-bake from a compromised sandbox would persist tampering across every
future reset). These pin: the sandbox refusal, the offline invariant (running
guest → graceful shutdown, wait for "shut off", never snapshot live), the
replace-not-stack behavior (delete an existing snapshot first), and honest
failure when the guest won't power down. Mocked at the vm._run seam like the
sibling broker tests.

Run:
    python3 tests/test_vm_bake.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import vm  # noqa: E402

SAVED = {"domain": "win-test", "snapshot": "clean"}


def _virsh_op(argv: list[str]) -> str:
    """The virsh subcommand in a vm._virsh argv (skips --connect <uri>)."""
    args = argv[1:]
    if args and args[0] == "--connect":
        args = args[2:]
    return args[0] if args else ""


class BakeTest(unittest.TestCase):
    def setUp(self):
        # The suite itself often runs inside a sandbox (SC_SANDBOX=1 in the
        # env); clear it so only test_refuses_in_sandbox exercises the
        # refusal path.
        patcher = mock.patch.dict("os.environ")
        patcher.start()
        os.environ.pop("SC_SANDBOX", None)
        self.addCleanup(patcher.stop)

    def test_refuses_in_sandbox(self):
        with mock.patch.dict("os.environ", {"SC_SANDBOX": "1"}):
            r = vm.do_bake()
        self.assertFalse(r["ok"])
        self.assertIn("vm-bake", r["output"])

    def test_missing_config_fields(self):
        with mock.patch.object(vm, "read", return_value={"domain": "d"}), \
             mock.patch.dict("os.environ", {}, clear=False):
            r = vm.do_bake()
        self.assertFalse(r["ok"])

    def test_happy_path_running_guest(self):
        calls = []

        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            calls.append(op)
            if op == "domstate":
                # running until the shutdown was issued, then shut off
                return True, ("shut off" if "shutdown" in calls else "running")
            if op == "snapshot-info":
                return True, "Name: clean"            # old bake exists
            return True, ""                            # shutdown/delete/create ok

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run), \
             mock.patch.object(vm.time, "sleep"):
            r = vm.do_bake()
        self.assertTrue(r["ok"], r["output"])
        self.assertEqual(
            [c for c in calls if c != "domstate"],
            ["shutdown", "snapshot-info", "snapshot-delete", "snapshot-create-as"],
            "must shut down first, replace (not stack) the old snapshot, then bake")
        self.assertIn("powered off", r["output"])

    def test_already_off_skips_shutdown_and_no_old_snapshot_skips_delete(self):
        calls = []

        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            calls.append(op)
            if op == "domstate":
                return True, "shut off"
            if op == "snapshot-info":
                return False, "no snapshot"            # first bake ever
            return True, ""

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run):
            r = vm.do_bake()
        self.assertTrue(r["ok"], r["output"])
        self.assertNotIn("shutdown", calls)
        self.assertNotIn("snapshot-delete", calls)
        self.assertIn("snapshot-create-as", calls)

    def test_guest_that_never_powers_down_fails_honest(self):
        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            if op == "domstate":
                return True, "running"                 # stuck
            if op == "shutdown":
                return True, ""
            self.fail(f"must not reach '{op}' — no snapshot ops on a live guest")

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run), \
             mock.patch.object(vm.time, "sleep"):
            r = vm.do_bake(shutdown_timeout=0)
        self.assertFalse(r["ok"])
        self.assertIn("did not shut off", r["output"])

    def test_bake_is_not_a_broker_verb(self):
        # The security property the design rests on: the broker (the sandbox's
        # only reach into the host) must not expose bake.
        src = (ENGINE / "api" / "vm_broker.py").read_text()
        self.assertNotIn("/bake", src,
                         "bake must stay host-only — a sandbox that can re-bake "
                         "can persist tampering across every reset")


if __name__ == "__main__":
    unittest.main()
