"""Named remotes keep SSH authority behind the host vm-broker."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import remote
import vm
import vm_broker


class RemoteVerbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "remote-key"
        self.key.write_text("not-real-key")
        self.key.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.entry = {
            "host": "remote.example",
            "user": "tester",
            "port": 2222,
            "key_path": str(self.key),
            "known_hosts_path": str(self.known_hosts),
        }

    def test_exec_uses_host_key_and_pinned_known_hosts_without_a_shell(self):
        with mock.patch.object(remote, "read_all", return_value={"devbox": self.entry}), \
             mock.patch.object(remote, "_run", return_value=(0, "ok\n", "")) as run:
            result = remote.do_exec("devbox", "uname -a")
        self.assertTrue(result["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn(str(self.key), argv)
        self.assertIn(f"UserKnownHostsFile={self.known_hosts}", argv)
        self.assertIn("tester@remote.example", argv)
        self.assertEqual(argv[-1], "uname -a")

    def test_key_must_be_absolute_regular_and_mode_0600(self):
        relative = {**self.entry, "key_path": "relative-key"}
        with mock.patch.object(remote, "read_all", return_value={"devbox": relative}), \
             mock.patch.object(remote, "_run") as run:
            result = remote.do_status("devbox")
        self.assertEqual(result["error"], "remote_key_invalid")
        run.assert_not_called()

        self.key.chmod(0o644)
        with mock.patch.object(remote, "read_all", return_value={"devbox": self.entry}), \
             mock.patch.object(remote, "_run") as run:
            result = remote.do_exec("devbox", "id")
        self.assertEqual(result["error"], "remote_key_invalid")
        run.assert_not_called()

    def test_undeclared_and_invalid_names_are_refused(self):
        with mock.patch.object(remote, "read_all", return_value={}):
            self.assertEqual(remote.do_status("missing")["error"], "remote_not_found")
            self.assertEqual(remote.do_status("../escape")["error"], "remote_name_invalid")

    def test_managed_known_hosts_stays_under_local_state(self):
        entry = dict(self.entry)
        del entry["known_hosts_path"]
        state_root = self.root / "state" / "remotes"
        with mock.patch.object(remote, "read_all", return_value={"devbox": entry}), \
             mock.patch.object(remote, "REMOTE_STATE_ROOT", state_root), \
             mock.patch.object(remote, "_run", return_value=(0, "ok", "")) as run:
            result = remote.do_status("devbox")
        self.assertTrue(result["ok"])
        self.assertIn(
            f"UserKnownHostsFile={state_root / 'devbox.known_hosts'}",
            run.call_args.args[0],
        )
        self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)

    def test_push_source_must_resolve_inside_repo(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        with mock.patch.object(remote, "read_all", return_value={"devbox": self.entry}), \
             mock.patch.object(remote, "_run") as run:
            result = remote.do_push("devbox", str(outside), "/tmp/outside.txt")
        self.assertEqual(result["error"], "remote_path_not_allowed")
        run.assert_not_called()

    def test_push_and_pull_build_guarded_scp_argv(self):
        source = ENGINE / "scripts" / "remote.py"
        local_destination = ROOT / ".sc-state" / "local" / "remote-test" / "out.txt"
        with mock.patch.object(remote, "read_all", return_value={"devbox": self.entry}), \
             mock.patch.object(remote, "_run", return_value=(0, "", "")) as run:
            pushed = remote.do_push("devbox", str(source), "/tmp/remote.py")
            pulled = remote.do_pull(
                "devbox", "/tmp/result.txt", str(local_destination)
            )
        self.assertTrue(pushed["ok"])
        self.assertTrue(pulled["ok"])
        push_argv = run.call_args_list[0].args[0]
        pull_argv = run.call_args_list[1].args[0]
        self.assertEqual(push_argv[0], "scp")
        self.assertEqual(push_argv[-2:], [str(source), "tester@remote.example:/tmp/remote.py"])
        self.assertEqual(
            pull_argv[-2:],
            ["tester@remote.example:/tmp/result.txt", str(local_destination)],
        )

    def test_pull_destination_outside_allowed_roots_is_refused(self):
        destination = self.root / "outside-result.txt"
        with mock.patch.object(remote, "read_all", return_value={"devbox": self.entry}), \
             mock.patch.object(remote, "_run") as run:
            result = remote.do_pull("devbox", "/tmp/result", str(destination))
        self.assertEqual(result["error"], "remote_path_not_allowed")
        run.assert_not_called()


class RemoteConfigClientTests(unittest.TestCase):
    def test_add_remove_and_list_preserve_public_boundaries(self):
        entry = {
            "host": "remote.example",
            "user": "tester",
            "port": 22,
            "key_path": "/host/private/key",
        }
        saved = {}
        with mock.patch.object(remote, "read_all", return_value={}), \
             mock.patch.object(
                 remote, "write_all", side_effect=lambda value: saved.update(value)
             ), \
             mock.patch.object(
                 remote, "_broker_health",
                 return_value={
                     "ready": True,
                     "start_command": "./sc vm-broker-up",
                 },
             ):
            added = remote.add_remote("devbox", entry)
        self.assertTrue(added["ok"])
        self.assertEqual(saved, {"devbox": entry})
        self.assertNotIn("key_path", added["result"]["remote"])

        with mock.patch.object(remote, "read_all", return_value={"devbox": entry}), \
             mock.patch.object(remote, "write_all") as write:
            removed = remote.remove_remote("devbox")
        self.assertTrue(removed["ok"])
        write.assert_called_once_with({})

        with mock.patch.object(remote, "read_all", return_value={"devbox": entry}):
            listed = remote.list_remotes()
        self.assertEqual(listed["result"]["remotes"][0]["name"], "devbox")
        self.assertNotIn("key_path", listed["result"]["remotes"][0])

    def test_add_rejects_relative_key_before_writing(self):
        entry = {
            "host": "remote.example",
            "user": "tester",
            "port": 22,
            "key_path": "relative",
        }
        with mock.patch.object(remote, "write_all") as write:
            result = remote.add_remote("devbox", entry)
        self.assertEqual(result["error"]["code"], "remote_key_invalid")
        write.assert_not_called()

    def test_broker_client_uses_named_routes(self):
        response = {
            "ok": True,
            "remote": "devbox",
            "exit": 0,
            "stdout": "ok\n",
            "stderr": "",
        }
        with mock.patch.object(vm, "broker_call", return_value=response) as call:
            result = remote.run_broker_operation(
                "exec", "devbox", command="echo ok"
            )
        self.assertTrue(result["ok"])
        call.assert_called_once_with(
            "POST",
            "/remote/devbox/exec",
            {"command": "echo ok"},
            timeout=remote.SSH_TIMEOUT + 15,
        )


class RemoteBrokerSocketTests(unittest.TestCase):
    def setUp(self):
        self.sock = ROOT / "tests" / "_test_remote_broker.sock"
        self.original_socket = vm.SOCKET
        vm.SOCKET = self.sock
        self.server = vm_broker.UnixHTTPServer(str(self.sock), vm_broker.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        vm.SOCKET = self.original_socket
        self.sock.unlink(missing_ok=True)

    def test_named_routes_dispatch_over_the_shared_socket(self):
        with mock.patch.object(
            remote,
            "do_status",
            return_value={"ok": True, "remote": {"name": "devbox"}, "reachable": True},
        ):
            status = vm.broker_call("GET", "/remote/devbox/status")
        self.assertTrue(status["reachable"])

        with mock.patch.object(remote, "do_exec", return_value={
            "ok": True, "remote": "devbox", "exit": 0, "stdout": "ok", "stderr": ""
        }) as execute:
            result = vm.broker_call(
                "POST", "/remote/devbox/exec", {"command": "echo ok"}
            )
        self.assertTrue(result["ok"])
        execute.assert_called_once_with("devbox", "echo ok")

    @unittest.skipUnless(shutil.which("docker"), "docker unavailable")
    def test_sandbox_exec_uses_broker_without_ssh_material(self):
        images = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        image = next(
            (line for line in images.stdout.splitlines() if line.startswith("super-coder-base:")),
            None,
        )
        if not image:
            self.skipTest("no super-coder-base image")
        key_dir = tempfile.TemporaryDirectory()
        self.addCleanup(key_dir.cleanup)
        key = Path(key_dir.name) / "host-only-key"
        key.write_text("host-only")
        key.chmod(0o600)
        entry = {
            "host": "remote.example",
            "user": "tester",
            "port": 22,
            "key_path": str(key),
            "known_hosts_path": str(Path(key_dir.name) / "known_hosts"),
        }
        code = (
            "import pathlib,sys; "
            f"assert not pathlib.Path({str(key)!r}).exists(); "
            f"sys.path[:0]=[{str(ENGINE / 'scripts')!r}]; "
            "import vm,remote; "
            f"vm.SOCKET=pathlib.Path({str(self.sock)!r}); "
            "raise SystemExit(remote.client_main(["
            "'exec','devbox','--json','--','echo','sandbox-ok']))"
        )
        with mock.patch.object(remote, "read_all", return_value={"devbox": entry}), \
             mock.patch.object(
                 remote, "_run", return_value=(0, "sandbox-ok\n", "")
             ):
            completed = subprocess.run(
                [
                    "docker", "run", "--rm", "--user", "0:0", "--network", "none",
                    "-v", f"{ROOT}:{ROOT}:ro", "-w", str(ROOT), image,
                    "python3", "-c", code,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["result"]["stdout"], "sandbox-ok\n")


if __name__ == "__main__":
    unittest.main()
