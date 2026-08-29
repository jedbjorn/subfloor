#!/usr/bin/env python3
"""Tests for the infrastructure-only `./sc feature` registry."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import feature


class RegistryIntegrityTest(unittest.TestCase):
    def test_registry_contains_only_infrastructure_features(self):
        self.assertEqual(set(feature.FEATURES), {"pg", "windows", "tailnet", "pm2"})

    def test_registry_shape(self):
        for name, f in feature.FEATURES.items():
            self.assertIn("block", f, name)
            self.assertIn("block_auto", f, name)
            self.assertIsInstance(f["block"], str, name)
            if not f["block_auto"]:
                self.assertTrue(f.get("link"),
                                f"operator-linked feature '{name}' has no link steps")

    def test_pg_block_matches_pg_init(self):
        # `./sc pg-init` (in the sc dispatcher) and `feature enable pg` write the
        # same instance.json key — if this drifts, launch won't see the sidecar.
        self.assertEqual(feature.FEATURES["pg"]["block"], "pg")
        sc = (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text()
        self.assertIn("d['pg']={}", sc.replace(" ", ""),
                      "sc pg-init no longer writes the `pg` key feature.py expects")

    def test_registry_does_not_name_retired_global_procedures(self):
        retired = {
            "app_deploy_setup", "configure_winbox", "query_authoring_pg",
            "windows_devkit", "windows_vm_gui",
        }
        registry = repr(feature.FEATURES)
        for name in retired:
            self.assertNotIn(f"'{name}'", registry)
        for item in feature.FEATURES.values():
            self.assertNotIn("grants", item)


class InfrastructureToggleTest(unittest.TestCase):
    def test_pg_enable_creates_auto_block_without_a_live_database(self):
        written = mock.Mock()
        with (
            mock.patch.object(feature, "_instance", return_value={}),
            mock.patch.object(feature, "_write_instance", written),
        ):
            self.assertEqual(feature.cmd_enable("pg"), 0)
        written.assert_called_once_with({"pg": {}})

    def test_link_only_enable_does_not_invent_host_configuration(self):
        written = mock.Mock()
        with (
            mock.patch.object(feature, "_instance", return_value={}),
            mock.patch.object(feature, "_write_instance", written),
        ):
            self.assertEqual(feature.cmd_enable("windows"), 0)
        written.assert_not_called()

    def test_disable_removes_only_the_selected_block(self):
        written = mock.Mock()
        with (
            mock.patch.object(feature, "_instance", return_value={"pg": {}, "vm": {}}),
            mock.patch.object(feature, "_write_instance", written),
        ):
            self.assertEqual(feature.cmd_disable("pg"), 0)
        written.assert_called_once_with({"vm": {}})


if __name__ == "__main__":
    unittest.main()
