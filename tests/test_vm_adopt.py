#!/usr/bin/env python3
"""Tests for `./sc vm adopt` (vm_adopt) — spec #232, work unit 3.

Adoption is eight phases against a real Windows guest, so everything here is
mocked at the module's declared seams: `vm._run` (virsh, ssh-keygen),
`vm._ssh_ready`, `vm.do_bake`, and `vm_adopt._capture` / `_run_env` /
`_tcp_open` / `_dispatch_verb` / `_sleep`. What is pinned:

- the happy-path phase sequence and the `--json` shape;
- discovery fall-through: DHCP lease -> `domifaddr --source arp` -> `--ssh-host`
  -> `adopt_guest_not_found` with the bootstrap line;
- the SSH wait timeout;
- phase skipping (a saved key that authenticates, sshd_config already hardened,
  a host key already pinned) — the resumability contract;
- the password: never on any argv, never in the human or JSON output, never in
  the written block; the askpass env shape; the temp dir removed even when the
  phase fails;
- provisioning failure naming the failing steps, and a verify failure leaving
  the vm block unwritten;
- the block's field set (no `transfer_dir`, with `known_hosts_path` and
  `workspace`);
- the `SC_SANDBOX` refusal;
- `--no-provision`.

Run:
    python3 tests/test_vm_adopt.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import vm
import vm_adopt

PASSWORD = "Tr0ub4dor&3-correct-horse"
MAC = "52:54:00:ab:cd:ef"
LEASE_IP = "192.168.122.50"
ARP_IP = "192.168.122.100"
FLAG_IP = "192.168.122.200"
HOST_KEY = f"{ARP_IP} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000000000\n"

DOMIFLIST = """
 Interface   Type      Source    Model    MAC
-----------------------------------------------------------
 vnet7       network   default   virtio   52:54:00:ab:cd:ef
"""
DOMIFADDR = f"""
 Name       MAC address          Protocol     Address
-------------------------------------------------------------------------------
 vnet7      {MAC}    ipv4         {ARP_IP}/24
"""
LEASES = f"""
 Expiry Time           MAC address         Protocol   IP address          Hostname
--------------------------------------------------------------------------------
 2026-09-15 10:00:00   {MAC}   ipv4       {LEASE_IP}/24       winbox
"""
REPORT = json.dumps({
    "steps": [
        {"name": "workspace", "ok": True, "detail": "C:\\SubfloorTest"},
        {"name": "winget", "ok": True, "detail": "v1.9"},
        {"name": "windows-mcp", "ok": True, "detail": "listening on 127.0.0.1:8000"},
    ],
    "ok": True,
})


def _virsh_op(argv: list[str]) -> str:
    args = argv[1:]
    if args and args[0] == "--connect":
        args = args[2:]
    return args[0] if args else ""


def _decoded(remote: str) -> str:
    """The PowerShell behind an -EncodedCommand guest command, else the text."""
    marker = "-EncodedCommand "
    if marker not in remote:
        return remote
    blob = remote.split(marker, 1)[1].strip()
    return base64.b64decode(blob).decode("utf-16-le")


class Harness:
    """One fake guest + host, wired into every vm_adopt seam."""

    def __init__(self, root: Path, *, lease: bool = False, arp: bool = True,
                 domstate: str = "running", keyscan: str = HOST_KEY,
                 harden: str = "CHANGED", report: str = REPORT,
                 tcp_open: bool = True, key_auth: bool = True,
                 mcp_listening: bool = True, check_rc: int = 0,
                 keygen_writes: bool = True) -> None:
        self.root = root
        self.lease = lease
        self.arp = arp
        self.domstate = domstate
        self.keyscan = keyscan
        self.harden = harden
        self.report = report
        self.tcp_open = tcp_open
        self.key_auth = key_auth
        self.mcp_listening = mcp_listening
        self.check_rc = check_rc
        self.keygen_writes = keygen_writes
        self.argvs: list[list[str]] = []
        self.envs: list[dict] = []
        self.helper_bodies: list[str] = []
        self.written: dict | None = None
        self.dispatched: list[str] = []
        self.temp_dirs: list[Path] = []
        self.baked: list[str] = []
        self.net_updates: list[list[str]] = []
        self.bake_ok = True

    # -- seams ---------------------------------------------------------------

    def run(self, argv, timeout=30):
        self.argvs.append(list(argv))
        if argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            if self.keygen_writes:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE\n")
                Path(f"{path}.pub").write_text(
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKEY subfloor-adopt\n"
                )
            return True, ""
        op = _virsh_op(argv)
        if op == "domstate":
            return True, self.domstate
        if op == "start":
            self.domstate = "running"
            return True, "Domain started"
        if op == "domiflist":
            return True, DOMIFLIST
        if op == "net-dhcp-leases":
            return (True, LEASES) if self.lease else (True, "")
        if op == "domifaddr":
            return (True, DOMIFADDR) if self.arp else (True, "")
        if op == "net-dumpxml":
            return True, "<network><ip/></network>"
        if op == "net-update":
            self.net_updates.append(list(argv))
            return True, "Updated network default persistent config and live state"
        return True, ""

    def domain_state(self, cfg):
        return True, vm.DOMAIN_STATES.get(self.domstate.strip().lower(), "unknown")

    def ssh_ready(self, cfg, timeout=10):
        return (True, None) if self.key_auth else (False, "Permission denied")

    def capture(self, argv, timeout):
        self.argvs.append(list(argv))
        if argv[0] == "ssh-keyscan":
            return (0, self.keyscan, "# host key noise on stderr") if self.keyscan \
                else (1, "", "connection refused")
        if argv[0] == "scp":
            return 0, "", ""
        remote = argv[-1]
        noise = "Warning: using post-quantum key exchange\n"
        script = _decoded(remote)
        if "sshd_config" in script:
            return 0, self.harden + "\n", noise
        if "Restart-Service sshd" in script:
            return 255, "", "Connection to host closed by remote host.\n"
        if remote.startswith("cmd /c if not exist"):
            return 0, "", noise
        if "provision.ps1" in remote:
            return 0, "chatty winget output\n" + self.report + "\n", noise
        if remote == "echo ok":
            return 0, "ok\n", noise
        if "Get-NetTCPConnection" in remote:
            return (0, "True\n", noise) if self.mcp_listening else (0, "False\n", noise)
        return self.check_rc, "1.2.3\n", noise

    def run_env(self, argv, env, timeout):
        self.argvs.append(list(argv))
        self.envs.append(dict(env))
        # The askpass helper the phase just wrote must exist while ssh runs.
        helper = Path(env["SSH_ASKPASS"])
        assert helper.is_file(), "the askpass helper is missing during the ssh call"
        self.helper_bodies.append(helper.read_text(encoding="utf-8"))
        self.temp_dirs.append(helper.parent)
        return 0, "", "post-quantum warning\n"

    def tcp(self, host, port, timeout=5):
        return self.tcp_open

    def dispatch(self, verb, timeout=120):
        self.dispatched.append(verb)
        return True, "→ vm-broker up (pid 123)"

    def bake(self, name=None, **kwargs):
        self.baked.append(name)
        if not self.bake_ok:
            return {"ok": False, "output": "guest did not shut off within 180s"}
        return {
            "ok": True,
            "output": f"graceful shutdown sent; baked '{name}' (offline) "
                      "— guest left powered off",
            "domain": "w10c-testing", "domain_state": "powered_off",
            "snapshot": name, "baseline_updated": False,
        }

    def write(self, block):
        self.written = dict(block) if block else None
        return self.written

    # -- wiring --------------------------------------------------------------

    def patches(self, saved: dict | None = None):
        return (
            mock.patch.object(vm, "repo_root", return_value=self.root),
            mock.patch.object(vm, "read", return_value=saved),
            mock.patch.object(vm, "write", side_effect=self.write),
            mock.patch.object(vm, "_run", side_effect=self.run),
            mock.patch.object(vm, "_domain_state", side_effect=self.domain_state),
            mock.patch.object(vm, "_ssh_ready", side_effect=self.ssh_ready),
            mock.patch.object(vm, "do_bake", side_effect=self.bake),
            mock.patch.object(vm, "broker_call", return_value={"ok": True}),
            mock.patch.object(vm_adopt, "_capture", side_effect=self.capture),
            mock.patch.object(vm_adopt, "_run_env", side_effect=self.run_env),
            mock.patch.object(vm_adopt, "_tcp_open", side_effect=self.tcp),
            mock.patch.object(vm_adopt, "_dispatch_verb", side_effect=self.dispatch),
            mock.patch.object(vm_adopt, "_sleep", lambda _s: None),
        )


class AdoptTestBase(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict("os.environ")
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SC_SANDBOX", None)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".sc-state" / "local" / "vm").mkdir(parents=True)

    def adopt(self, harness: Harness, *, saved: dict | None = None, **options):
        arguments = {
            "domain": "w10c-testing",
            "ssh_user": "sctest",
            "password_file": str(self._password_file()),
        }
        arguments.update(options)
        stdout = io.StringIO()
        contexts = harness.patches(saved)
        for context in contexts:
            context.start()
        try:
            with redirect_stdout(stdout):
                value = vm_adopt.run_adopt(**arguments)
        finally:
            for context in contexts:
                context.stop()
        self.console = stdout.getvalue()
        return value

    def _password_file(self) -> Path:
        path = self.root / ".sc-state" / "local" / "pw"
        path.write_text(PASSWORD + "\n", encoding="utf-8")
        return path

    @staticmethod
    def statuses(value: dict) -> list[tuple[str, str]]:
        return [(p["name"], p["status"]) for p in value["phases"]]


class HappyPathTest(AdoptTestBase):
    def test_phase_sequence_and_json_shape(self):
        harness = Harness(self.root)
        value = self.adopt(harness)

        self.assertTrue(value["ok"], json.dumps(value, indent=2))
        self.assertEqual(value["operation"], "adopt")
        self.assertEqual(
            self.statuses(value),
            [("locate", "done"), ("wait", "done"), ("install_key", "done"),
             ("harden", "done"), ("provision", "done"), ("verify", "done"),
             ("write_block", "done"), ("broker", "done"), ("baseline", "done")],
        )
        # The spec names phases and vm at the top level of the --json object;
        # the engine's stable envelope carries them under `result`. Both hold.
        self.assertEqual(value["phases"], value["result"]["phases"])
        self.assertEqual(value["vm"], value["result"]["vm"])
        self.assertEqual(value["error"], None)
        for phase in value["phases"]:
            self.assertEqual(set(phase), {"name", "status", "detail"})
            self.assertIn(phase["status"], ("done", "skipped", "failed"))
        self.assertEqual(harness.dispatched, ["vm-broker-up"])
        self.assertEqual(harness.baked, ["baseline"])

    def test_written_block_has_no_transfer_dir_and_pins_the_host_key(self):
        harness = Harness(self.root)
        value = self.adopt(harness)
        block = harness.written
        self.assertEqual(
            set(block),
            {"domain", "snapshot", "libvirt_uri", "ssh_host", "ssh_user",
             "ssh_key_path", "ssh_port", "mcp_port", "known_hosts_path",
             "workspace"},
        )
        self.assertNotIn("transfer_dir", block)
        self.assertEqual(block["ssh_host"], ARP_IP)
        self.assertEqual(block["snapshot"], "baseline")
        self.assertEqual(block["libvirt_uri"], "qemu:///system")
        self.assertEqual(block["workspace"], "C:\\SubfloorTest")
        self.assertTrue(block["known_hosts_path"].endswith("w10c-testing.known_hosts"))
        self.assertEqual(value["vm"], block)
        self.assertEqual(
            Path(block["known_hosts_path"]).read_text(encoding="utf-8"), HOST_KEY
        )

    def test_console_prints_the_bootstrap_line_before_waiting(self):
        harness = Harness(self.root)
        self.adopt(harness)
        self.assertIn(
            "powershell -ExecutionPolicy Bypass -Command "
            "\"[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm ",
            self.console,
        )
        self.assertIn("/.super-coder/assets/winbox/bootstrap.ps1 | iex\"", self.console)

    def test_bootstrap_url_override_is_used_verbatim(self):
        harness = Harness(self.root)
        self.adopt(harness, bootstrap_url="https://example.invalid/b.ps1")
        self.assertIn("irm https://example.invalid/b.ps1 | iex", self.console)

    def test_human_report_renders_every_phase_and_the_next_steps(self):
        harness = Harness(self.root)
        value = self.adopt(harness)
        text = vm_adopt.human_report(value)
        for name in ("locate", "wait", "install_key", "harden", "provision",
                     "verify", "write_block", "broker", "baseline"):
            self.assertIn(name, text)
        self.assertIn("VM adopted: w10c-testing · baseline 'baseline'", text)
        self.assertIn("./sc vm start", text)
        self.assertIn("./sc vm status", text)

    def test_key_install_appends_to_both_files_and_fixes_the_acl(self):
        harness = Harness(self.root)
        self.adopt(harness)
        remote = next(
            argv[-1] for argv in harness.argvs
            if argv[0] == "ssh" and "administrators_authorized_keys" in argv[-1]
        )
        self.assertIn(">>", remote)          # append, never overwrite
        self.assertIn("findstr", remote)     # and never twice
        self.assertIn(r"%USERPROFILE%\.ssh\authorized_keys", remote)
        self.assertIn("icacls", remote)
        self.assertIn("/inheritance:r", remote)
        self.assertIn('/grant "Administrators:F"', remote)
        self.assertIn('/grant "SYSTEM:F"', remote)

    def test_powered_off_domain_is_started_without_an_ssh_wait(self):
        harness = Harness(self.root, domstate="shut off")
        value = self.adopt(harness)
        self.assertTrue(value["ok"])
        self.assertIn("started the powered-off domain", value["phases"][0]["detail"])
        self.assertIn("start", [_virsh_op(a) for a in harness.argvs if a[0] == "virsh"])


class DiscoveryTest(AdoptTestBase):
    def test_dhcp_lease_wins_and_gets_a_reservation(self):
        harness = Harness(self.root, lease=True)
        value = self.adopt(harness)
        self.assertTrue(value["ok"])
        self.assertEqual(harness.written["ssh_host"], LEASE_IP)
        self.assertIn("dhcp lease", value["phases"][0]["detail"])
        self.assertEqual(len(harness.net_updates), 1)
        argv = harness.net_updates[0]
        self.assertIn("ip-dhcp-host", argv)
        self.assertIn(f"<host mac='{MAC}' ip='{LEASE_IP}'/>", argv)
        self.assertIn("--live", argv)
        self.assertIn("--config", argv)

    def test_arp_is_used_when_no_lease_exists(self):
        harness = Harness(self.root, lease=False, arp=True)
        value = self.adopt(harness)
        self.assertEqual(harness.written["ssh_host"], ARP_IP)
        self.assertIn("arp", value["phases"][0]["detail"])
        # A static in-guest address gets no DHCP reservation.
        self.assertEqual(harness.net_updates, [])

    def test_ssh_host_flag_is_the_last_resort(self):
        harness = Harness(self.root, lease=False, arp=False)
        value = self.adopt(harness, ssh_host=FLAG_IP)
        self.assertTrue(value["ok"])
        self.assertEqual(harness.written["ssh_host"], FLAG_IP)
        self.assertIn("--ssh-host", value["phases"][0]["detail"])

    def test_nothing_resolves_reports_guest_not_found_with_the_bootstrap_line(self):
        harness = Harness(self.root, lease=False, arp=False)
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_guest_not_found")
        self.assertIn("bootstrap.ps1 | iex", value["error"]["message"])
        self.assertEqual(self.statuses(value), [("locate", "failed")])
        self.assertIsNone(harness.written)

    def test_reservation_refusal_is_a_warning_not_a_failure(self):
        harness = Harness(self.root, lease=True)
        real_run = harness.run

        def refuse(argv, timeout=30):
            if _virsh_op(argv) == "net-update":
                harness.net_updates.append(list(argv))
                return False, "error: Unable to add ip-dhcp-host"
            return real_run(argv, timeout)

        harness.run = refuse
        value = self.adopt(harness)
        self.assertTrue(value["ok"])
        harden = next(p for p in value["phases"] if p["name"] == "harden")
        self.assertEqual(harden["status"], "done")
        self.assertIn("WARNING", harden["detail"])


class WaitTest(AdoptTestBase):
    def test_ssh_timeout_when_the_guest_never_answers(self):
        harness = Harness(self.root, tcp_open=False)
        value = self.adopt(harness, wait=1)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_ssh_timeout")
        self.assertEqual(self.statuses(value), [("locate", "done"), ("wait", "failed")])
        self.assertIsNone(harness.written)
        self.assertIn("bootstrap line has not run", value["error"]["message"])


class ResumabilityTest(AdoptTestBase):
    SAVED: ClassVar[dict] = {
        "domain": "w10c-testing",
        "snapshot": "baseline",
        "ssh_host": ARP_IP,
        "ssh_user": "sctest",
        "ssh_key_path": "/keys/existing.key",
        "ssh_port": 22,
    }

    def test_key_phase_skips_when_the_saved_key_authenticates(self):
        harness = Harness(self.root)
        value = self.adopt(harness, saved=self.SAVED, ssh_user=None,
                           password_file=None)
        self.assertTrue(value["ok"])
        key_phase = next(p for p in value["phases"] if p["name"] == "install_key")
        self.assertEqual(key_phase["status"], "skipped")
        self.assertIn("already authenticates", key_phase["detail"])
        # No password was read and no password-authenticated ssh was made.
        self.assertEqual(harness.envs, [])
        self.assertEqual(harness.written["ssh_key_path"], "/keys/existing.key")
        # ssh_user came from the saved block.
        self.assertEqual(harness.written["ssh_user"], "sctest")

    def _pin(self) -> None:
        (self.root / ".sc-state" / "local" / "vm" / "w10c-testing.known_hosts"
         ).write_text(HOST_KEY, encoding="utf-8")

    def test_harden_skips_when_password_auth_is_already_off(self):
        self._pin()
        harness = Harness(self.root, harden="UNCHANGED")
        value = self.adopt(harness)
        harden = next(p for p in value["phases"] if p["name"] == "harden")
        self.assertEqual(harden["status"], "skipped")
        self.assertIn("already set", harden["detail"])
        # No sshd restart was attempted.
        self.assertFalse(any(
            "Restart-Service sshd" in _decoded(argv[-1])
            for argv in harness.argvs if argv[0] == "ssh"
        ))

    def test_pin_skips_when_the_known_hosts_file_already_matches(self):
        self._pin()
        harness = Harness(self.root, harden="CHANGED")
        value = self.adopt(harness)
        harden = next(p for p in value["phases"] if p["name"] == "harden")
        # sshd_config still changed, so the phase as a whole is `done`; the
        # pin half of it reports that it had nothing to write.
        self.assertEqual(harden["status"], "done")
        self.assertIn("already pinned", harden["detail"])

    def test_baseline_is_retaken_even_when_the_snapshot_exists(self):
        harness = Harness(self.root)
        value = self.adopt(harness)
        baseline = next(p for p in value["phases"] if p["name"] == "baseline")
        self.assertEqual(baseline["status"], "done")
        self.assertIn("replaced, never stacked", baseline["detail"])


class PasswordContainmentTest(AdoptTestBase):
    def test_password_never_reaches_any_argv_output_or_the_block(self):
        harness = Harness(self.root)
        value = self.adopt(harness)
        self.assertTrue(value["ok"])
        self.assertTrue(harness.envs, "the password path never ran")
        for argv in harness.argvs:
            for element in argv:
                self.assertNotIn(PASSWORD, element)
        self.assertNotIn(PASSWORD, json.dumps(value))
        self.assertNotIn(PASSWORD, json.dumps(harness.written))
        self.assertNotIn(PASSWORD, vm_adopt.human_report(value))
        self.assertNotIn(PASSWORD, self.console)

    def test_askpass_env_shape(self):
        harness = Harness(self.root)
        self.adopt(harness)
        env = harness.envs[0]
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertTrue(env["SSH_ASKPASS"].endswith("askpass.sh"))
        self.assertTrue(env["DISPLAY"], "DISPLAY must be set for askpass to fire")
        self.assertEqual(env["SC_ADOPT_PASSWORD"], PASSWORD)
        # The helper prints the variable; its body never holds the secret.
        self.assertIn("$SC_ADOPT_PASSWORD", harness.helper_bodies[0])
        self.assertNotIn(PASSWORD, harness.helper_bodies[0])

    def test_password_ssh_disables_pubkey_and_allows_exactly_one_prompt(self):
        harness = Harness(self.root)
        self.adopt(harness)
        argv = next(
            a for a in harness.argvs
            if a[0] == "ssh" and "PubkeyAuthentication=no" in a
        )
        self.assertIn("PreferredAuthentications=password", argv)
        self.assertIn("NumberOfPasswordPrompts=1", argv)
        self.assertIn("BatchMode=no", argv)

    def test_askpass_temp_dir_is_removed_even_when_the_phase_fails(self):
        harness = Harness(self.root, key_auth=False)
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_key_install_failed")
        self.assertTrue(harness.temp_dirs, "the askpass helper never existed")
        for directory in harness.temp_dirs:
            self.assertFalse(directory.exists(), f"{directory} survived the failure")

    def test_password_file_strips_exactly_one_trailing_newline(self):
        path = self.root / ".sc-state" / "local" / "pw2"
        path.write_text("secret \n", encoding="utf-8")
        harness = Harness(self.root)
        self.adopt(harness, password_file=str(path))
        self.assertEqual(harness.envs[0]["SC_ADOPT_PASSWORD"], "secret ")


class ProvisionAndVerifyTest(AdoptTestBase):
    def _profile(self, **fields) -> None:
        directory = self.root / ".subfloor"
        directory.mkdir(exist_ok=True)
        (directory / "winbox.json").write_text(json.dumps(fields), encoding="utf-8")

    def test_provision_ships_the_script_and_the_resolved_profile(self):
        self._profile(checks=["dotnet --version"], mcp=False)
        harness = Harness(self.root)
        value = self.adopt(harness)
        self.assertTrue(value["ok"], json.dumps(value, indent=2))
        scps = [argv for argv in harness.argvs if argv[0] == "scp"]
        targets = [argv[-1] for argv in scps]
        self.assertTrue(any(t.endswith(r"C:\SubfloorTest\provision.ps1") for t in targets))
        self.assertTrue(any(t.endswith(r"C:\SubfloorTest\winbox.json") for t in targets))
        shipped = next(
            argv[-2] for argv in scps if argv[-1].endswith("winbox.json")
        )
        # The staged copy is removed once it is shipped.
        self.assertFalse(Path(shipped).exists())
        self.assertTrue(any(
            argv[0] == "ssh" and argv[-1].startswith("cmd /c if not exist")
            for argv in harness.argvs
        ))

    def test_provision_failure_names_the_failing_steps(self):
        harness = Harness(self.root, report=json.dumps({
            "steps": [
                {"name": "workspace", "ok": True, "detail": ""},
                {"name": "winget", "ok": False,
                 "detail": "winget not found — install App Installer"},
                {"name": "check: dotnet --version", "ok": False, "detail": "exit 9009"},
            ],
            "ok": False,
        }))
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_provision_failed")
        self.assertIn("winget", value["error"]["message"])
        self.assertIn("check: dotnet --version", value["error"]["message"])
        self.assertIsNone(harness.written)
        self.assertEqual(value["phases"][-1]["name"], "provision")
        self.assertEqual(value["phases"][-1]["status"], "failed")

    def test_missing_report_line_is_a_provision_failure(self):
        harness = Harness(self.root, report="winget was chatty and said nothing else")
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_provision_failed")
        self.assertIn("winbox_report_missing", value["error"]["message"])

    def test_verify_failure_leaves_the_block_unwritten(self):
        self._profile(checks=["dotnet --version"])
        harness = Harness(self.root, check_rc=9009)
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_verify_failed")
        self.assertIn("dotnet --version", value["error"]["message"])
        self.assertIn("was NOT written", value["error"]["message"])
        self.assertIsNone(harness.written)
        self.assertEqual(harness.dispatched, [])
        self.assertEqual(harness.baked, [])

    def test_missing_mcp_listener_fails_verify(self):
        harness = Harness(self.root, mcp_listening=False)
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_verify_failed")
        self.assertIn("127.0.0.1:8000", value["error"]["message"])

    def test_no_provision_skips_provisioning_and_its_checks(self):
        self._profile(checks=["dotnet --version"])
        harness = Harness(self.root, check_rc=9009, mcp_listening=False)
        value = self.adopt(harness, provision=False)
        self.assertTrue(value["ok"], json.dumps(value, indent=2))
        provision = next(p for p in value["phases"] if p["name"] == "provision")
        self.assertEqual(provision["status"], "skipped")
        verify = next(p for p in value["phases"] if p["name"] == "verify")
        self.assertEqual(verify["status"], "done")
        self.assertIn("--no-provision", verify["detail"])
        self.assertFalse([argv for argv in harness.argvs if argv[0] == "scp"])
        self.assertFalse(any(
            "provision.ps1" in argv[-1] for argv in harness.argvs if argv[0] == "ssh"
        ))

    def test_invalid_winbox_profile_fails_provision(self):
        self._profile(mcp_port=0)
        harness = Harness(self.root)
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_provision_failed")
        self.assertIn("winbox_config_invalid", value["error"]["message"])


class GuardTest(AdoptTestBase):
    def test_sandbox_refusal(self):
        with mock.patch.dict("os.environ", {"SC_SANDBOX": "1"}):
            value = vm_adopt.run_adopt(domain="w10c-testing", ssh_user="sctest")
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_sandboxed")
        self.assertIn("refuses to run in the sandbox", value["error"]["message"])
        self.assertIn("./sc vm adopt --domain w10c-testing", value["error"]["message"])
        self.assertEqual(value["phases"], [])

    def test_ssh_user_is_required_without_a_saved_block(self):
        with mock.patch.object(vm, "read", return_value=None):
            value = vm_adopt.run_adopt(domain="w10c-testing")
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_config_invalid")
        self.assertIn("--ssh-user is required", value["error"]["message"])

    def test_snapshot_name_is_validated(self):
        with mock.patch.object(vm, "read", return_value=None):
            value = vm_adopt.run_adopt(
                domain="w10c-testing", ssh_user="sctest", snapshot="Bad Name"
            )
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_config_invalid")

    def test_broker_failure_stops_adoption_after_the_block_is_written(self):
        harness = Harness(self.root)
        harness.dispatch = lambda verb, timeout=120: (False, "→ vm-broker: refused")
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_broker_failed")
        self.assertIsNotNone(harness.written)
        self.assertEqual(harness.baked, [])

    def test_baseline_failure_is_reported_with_its_own_code(self):
        harness = Harness(self.root)
        harness.bake_ok = False
        value = self.adopt(harness)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "adopt_baseline_failed")
        self.assertEqual(value["phases"][-1]["name"], "baseline")


class ClientParserTest(unittest.TestCase):
    def test_adopt_is_a_client_subcommand_with_the_documented_flags(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return vm.operation_success("adopt", {
                "domain": kwargs["domain"], "phases": [], "vm": None,
                "next_steps": [],
            })

        module = mock.Mock(run_adopt=fake, human_report=lambda value: "ok")
        with mock.patch.dict(sys.modules, {"vm_adopt": module}), \
             redirect_stdout(io.StringIO()):
            code = vm.client_main([
                "adopt", "--domain", "w10c-testing", "--ssh-user", "sctest",
                "--ssh-host", ARP_IP, "--snapshot", "clean",
                "--libvirt-uri", "qemu:///session", "--password-file", "/tmp/pw",
                "--bootstrap-url", "https://example.invalid/b.ps1",
                "--no-provision", "--wait", "60",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured, {
            "domain": "w10c-testing", "ssh_user": "sctest", "ssh_host": ARP_IP,
            "snapshot": "clean", "libvirt_uri": "qemu:///session",
            "password_file": "/tmp/pw",
            "bootstrap_url": "https://example.invalid/b.ps1",
            "provision": False, "wait": 60,
        })

    def test_defaults_match_the_spec(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return vm.operation_error("adopt", "adopt_guest_not_found", "none")

        module = mock.Mock(run_adopt=fake, human_report=lambda value: "no")
        with mock.patch.dict(sys.modules, {"vm_adopt": module}), \
             redirect_stdout(io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            code = vm.client_main(["adopt", "--domain", "w10c-testing"])
        self.assertEqual(code, 1)
        self.assertEqual(captured["snapshot"], "baseline")
        self.assertEqual(captured["libvirt_uri"], "qemu:///system")
        self.assertEqual(captured["wait"], 900)
        self.assertIs(captured["provision"], True)
        self.assertIsNone(captured["ssh_user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
