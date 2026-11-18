"""Global API/dev port allocation regressions."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ports  # noqa: E402
import remote  # noqa: E402
import ts  # noqa: E402
import vm  # noqa: E402


class GlobalPortNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_ports_")
        self.addCleanup(temporary.cleanup)
        self.repos = Path(temporary.name)
        self.current = self.repos / "ami"
        self.sibling = self.repos / "sibling"
        self.current_config = self.current / ".super-coder" / "instance.json"
        self.sibling_config = self.sibling / ".super-coder" / "instance.json"
        self.current_config.parent.mkdir(parents=True)
        self.sibling_config.parent.mkdir(parents=True)

    def resolve(self, current: dict | None = None) -> dict:
        if current is not None:
            self.current_config.write_text(json.dumps(current))
        with (
            mock.patch.object(ports, "REPO_ROOT", self.current),
            mock.patch.object(ports, "CONFIG", self.current_config),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            return ports.resolve(persist=True)

    def test_existing_api_port_moves_off_sibling_dev_port(self) -> None:
        self.sibling_config.write_text(json.dumps({
            "repo": "sibling",
            "port": 8871,
            "dev_port": 8812,
        }))

        resolved = self.resolve({
            "repo": "ami",
            "port": 8812,
            "dev_port": 8844,
            "harness": "opencode",
        })

        self.assertNotEqual(resolved["port"], 8812)
        self.assertEqual(resolved["dev_port"], 8844)
        self.assertTrue({resolved["port"], resolved["dev_port"]}.isdisjoint(
            {8871, 8812}
        ))
        self.assertEqual(json.loads(self.current_config.read_text()), resolved)

    def test_new_pair_avoids_both_fields_from_every_sibling(self) -> None:
        self.sibling_config.write_text(json.dumps({
            "repo": "sibling",
            "port": 8800,
            "dev_port": 8801,
        }))

        with (
            mock.patch.object(ports, "REPO_ROOT", self.current),
            mock.patch.object(ports, "CONFIG", self.current_config),
            mock.patch.object(ports, "_offset", return_value=0),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            resolved = ports.resolve()

        self.assertEqual(len({resolved["port"], resolved["dev_port"]}), 2)
        self.assertTrue(
            {resolved["port"], resolved["dev_port"]}.isdisjoint({8800, 8801})
        )

    def ensure_vm_mcp_port(self, offset: int = 0) -> int:
        with (
            mock.patch.object(ports, "REPO_ROOT", self.current),
            mock.patch.object(ports, "CONFIG", self.current_config),
            mock.patch.object(ports, "_offset", return_value=offset),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            return ports.ensure_vm_mcp_port()

    def test_vm_mcp_port_is_per_instance_persisted_and_stable(self) -> None:
        # #1836: a fixed relay port let host-runtime forks reach each
        # other's guests. Each fork gets its own, clear of every sibling port.
        self.sibling_config.write_text(json.dumps({
            "repo": "sibling", "port": 8800, "dev_port": 8801,
            "vm_mcp_port": 8802,
        }))
        self.current_config.write_text(json.dumps({
            "repo": "ami", "port": 8803, "dev_port": 8804,
            "browser_port": 8805,
        }))

        first = self.ensure_vm_mcp_port()
        self.assertEqual(first, 8806)
        self.assertEqual(
            json.loads(self.current_config.read_text())["vm_mcp_port"], 8806
        )
        self.assertEqual(self.ensure_vm_mcp_port(offset=40), 8806)

    def test_vm_mcp_port_moves_off_a_sibling_assignment(self) -> None:
        self.sibling_config.write_text(json.dumps({
            "repo": "sibling", "port": 8800, "dev_port": 8801,
            "vm_mcp_port": 8850,
        }))
        self.current_config.write_text(json.dumps({
            "repo": "ami", "port": 8803, "dev_port": 8804,
            "vm_mcp_port": 8850,
        }))

        self.assertEqual(self.ensure_vm_mcp_port(offset=50), 8851)

    def test_browser_ports_avoid_the_vm_mcp_port(self) -> None:
        self.current_config.write_text(json.dumps({
            "repo": "ami", "port": 8803, "dev_port": 8804,
            "vm_mcp_port": 8810,
        }))
        with (
            mock.patch.object(ports, "REPO_ROOT", self.current),
            mock.patch.object(ports, "CONFIG", self.current_config),
            mock.patch.object(ports, "_offset", return_value=10),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            allocated = ports.ensure_browser_ports()

        self.assertNotIn(8810, {allocated["browser_port"],
                                allocated["browser_proxy_port"]})

    def test_exhausted_global_namespace_fails_instead_of_colliding(self) -> None:
        with mock.patch.object(ports, "_free", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no free Subfloor port"):
                ports._resolve_offset(0, set())

    def test_unknown_instance_key_is_inert_but_preserved_on_disk(self) -> None:
        stored = {
            "repo": "ami",
            "port": 8812,
            "dev_port": 8844,
            "harness": "opencode",
            "retired_host_port": 8977,
        }
        original = json.dumps(stored, separators=(",", ":"))
        self.current_config.write_text(original)

        with (
            mock.patch.object(ports, "REPO_ROOT", self.current),
            mock.patch.object(ports, "CONFIG", self.current_config),
            mock.patch.object(ports, "_free", return_value=True),
        ):
            resolved = ports.resolve(persist=True)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ports.main(["show"]), 0)

        self.assertNotIn("retired_host_port", resolved)
        self.assertNotIn("retired_host_port", json.loads(output.getvalue()))
        self.assertEqual(self.current_config.read_text(), original)

    def test_scoped_updates_preserve_concurrent_blocks_and_unknown_keys(self) -> None:
        stored = {
            "instance_id": "a" * 32,
            "repo": "ami",
            "port": 8812,
            "dev_port": 8844,
            "harness": "opencode",
            "vm": {"domain": "old"},
            "future_extension": {"opaque": True},
        }
        for order in (("vm", "ts"), ("ts", "vm")):
            with self.subTest(order=order):
                self.current_config.write_text(json.dumps(stored))
                with mock.patch.object(ports, "CONFIG", self.current_config):
                    # Both callers derive their intent before either write.
                    first = ports.resolve(persist=False)
                    second = ports.resolve(persist=False)
                    values = {
                        "vm": {"domain": "new"},
                        "ts": {"hosts": ["build"]},
                    }
                    first[order[0]] = values[order[0]]
                    second[order[1]] = values[order[1]]
                    writers = {"vm": vm.write, "ts": ts.write}
                    writers[order[0]](first[order[0]])
                    writers[order[1]](second[order[1]])

                    persisted = json.loads(self.current_config.read_text())
                    self.assertEqual(persisted["vm"], values["vm"])
                    self.assertEqual(persisted["ts"], values["ts"])
                    self.assertEqual(persisted["instance_id"], "a" * 32)
                    self.assertEqual(
                        persisted["future_extension"], {"opaque": True}
                    )

                    vm.write(None)
                    persisted = json.loads(self.current_config.read_text())
                    self.assertNotIn("vm", persisted)
                    self.assertEqual(persisted["ts"], values["ts"])
                    self.assertEqual(persisted["instance_id"], "a" * 32)

    def test_named_remotes_are_a_scoped_managed_block(self) -> None:
        stored = {
            "instance_id": "a" * 32,
            "repo": "ami",
            "port": 8812,
            "dev_port": 8844,
            "future_extension": {"opaque": True},
        }
        self.current_config.write_text(json.dumps(stored))
        remotes = {
            "devbox": {
                "host": "remote.example",
                "user": "tester",
                "port": 22,
                "key_path": "/host/key",
            }
        }
        with mock.patch.object(ports, "CONFIG", self.current_config):
            remote.write_all(remotes)
        persisted = json.loads(self.current_config.read_text())
        self.assertEqual(persisted["remotes"], remotes)
        self.assertEqual(persisted["future_extension"], {"opaque": True})


if __name__ == "__main__":
    unittest.main()
