#!/usr/bin/env python3
"""Tests for `./sc vm bake` (vm.do_bake) — re-baking the baseline snapshot.

Bake is a BROKER verb since spec #232 (decision #372): the guest is a
disposable test box, so a shell that has just installed a toolchain may
redefine the baseline itself. These pin: no sandbox refusal, the offline
invariant (running guest → graceful shutdown, wait for "shut off", never
snapshot live), the replace-not-stack behavior (delete an existing snapshot
first), the vm-block rebase when a NAME is given, and honest failure when the
guest won't power down. Mocked at the vm._run seam like the sibling broker
tests.

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

    def test_runs_in_the_sandbox_now_that_the_broker_owns_bake(self):
        # Spec #232 retired the SC_SANDBOX refusal: a shell reaches bake
        # through the broker, which runs on the host where virsh lives.
        calls = []

        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            calls.append(op)
            if op == "domstate":
                return True, "shut off"
            if op == "snapshot-info":
                return False, "no snapshot"
            return True, ""

        with mock.patch.dict("os.environ", {"SC_SANDBOX": "1"}), \
             mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run):
            r = vm.do_bake()
        self.assertTrue(r["ok"], r["output"])
        self.assertIn("snapshot-create-as", calls)

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

    def test_bake_is_a_broker_verb_under_the_mutation_lock(self):
        # Spec #232: /bake is exposed, and like every other mutation it is
        # serialized by the broker's lock rather than racing another verb.
        src = (ENGINE / "api" / "vm_broker.py").read_text()
        self.assertIn('if self.path == "/bake":', src)
        self.assertIn("vm.do_bake(name)", src)
        bake_block = src.split('if self.path == "/bake":', 1)[1].split(
            'if self.path == "/reset":', 1
        )[0]
        self.assertIn("self._mutate", bake_block)

    def test_named_bake_rebases_the_vm_block_to_the_new_snapshot(self):
        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            if op == "domstate":
                return True, "shut off"
            if op == "snapshot-info":
                return False, "no snapshot"
            if op == "snapshot-create-as":
                self.assertIn("project-ready", argv)
            return True, ""

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run), \
             mock.patch.object(vm, "write") as write:
            r = vm.do_bake("project-ready")
        self.assertTrue(r["ok"], r["output"])
        self.assertEqual(r["snapshot"], "project-ready")
        self.assertTrue(r["baseline_updated"])
        write.assert_called_once_with({**SAVED, "snapshot": "project-ready"})

    def test_unnamed_bake_leaves_the_block_alone(self):
        def fake_run(argv, timeout=30):
            op = _virsh_op(argv)
            if op == "domstate":
                return True, "shut off"
            if op == "snapshot-info":
                return False, "no snapshot"
            return True, ""

        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run", side_effect=fake_run), \
             mock.patch.object(vm, "write") as write:
            r = vm.do_bake()
        self.assertTrue(r["ok"], r["output"])
        self.assertFalse(r["baseline_updated"])
        write.assert_not_called()

    def test_invalid_bake_name_is_refused_before_any_virsh_call(self):
        with mock.patch.object(vm, "read", return_value=SAVED), \
             mock.patch.object(vm, "_run") as run:
            r = vm.do_bake("Bad Name")
        self.assertEqual(r["error"], "snapshot_name_invalid")
        run.assert_not_called()

    def test_bake_client_verb_routes_through_the_broker(self):
        with mock.patch.object(vm, "broker_call", return_value={
            "ok": True,
            "domain": "win-test",
            "domain_state": "powered_off",
            "snapshot": "project-ready",
            "baseline_updated": True,
        }) as call:
            result = vm.run_operation("bake", snapshot="project-ready")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["snapshot"], "project-ready")
        self.assertTrue(result["result"]["baseline_updated"])
        call.assert_called_once_with(
            "POST", "/bake", {"name": "project-ready"},
            timeout=vm.BAKE_CLIENT_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
