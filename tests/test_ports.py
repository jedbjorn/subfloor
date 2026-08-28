"""Global API/dev port allocation regressions."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ports  # noqa: E402


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

    def test_existing_config_drops_retired_runtime_port(self) -> None:
        resolved = self.resolve({
            "repo": "ami",
            "port": 8821,
            "dev_port": 8844,
            "deepseek_host_port": 8942,
            "harness": "codex",
        })

        self.assertNotIn("deepseek_host_port", resolved)
        self.assertEqual(json.loads(self.current_config.read_text()), resolved)

    def test_exhausted_global_namespace_fails_instead_of_colliding(self) -> None:
        with mock.patch.object(ports, "_free", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no free super-coder port"):
                ports._resolve_offset(0, set())


if __name__ == "__main__":
    unittest.main()
