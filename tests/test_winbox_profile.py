#!/usr/bin/env python3
"""Tests for scripts/winbox.py — the `.subfloor/winbox.json` resolver and the
provision.ps1 report parser (spec #232, work unit 1).

The resolver is the only place the fork's declaration is trusted, so the shape
validation is pinned here: unknown keys, wrong types, non-absolute guest
workspaces and a declared-but-missing winget manifest all have to raise a typed
WinboxConfigError with a stable code rather than reaching the guest.

Run:
    python3 tests/test_winbox_profile.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import winbox  # noqa: E402


class _Repo(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sc_winbox_")).resolve()
        (self.root / ".subfloor").mkdir()

    def declare(self, payload) -> Path:
        path = self.root / ".subfloor" / "winbox.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
        return path


class LoadTests(_Repo):
    def test_no_file_and_no_manifest_yields_defaults(self):
        profile = winbox.load(self.root)
        self.assertEqual(profile, {
            "winget_manifest": None,
            "checks": [],
            "mcp": True,
            "mcp_port": 8000,
            "workspace": "C:\\SubfloorTest",
            "source": None,
        })

    def test_default_manifest_is_used_only_when_it_exists(self):
        manifest = self.root / "winget-manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        profile = winbox.load(self.root)
        self.assertEqual(profile["winget_manifest"], str(manifest))

    def test_declared_values_override_every_default(self):
        manifest = self.root / "pkgs" / "apps.json"
        manifest.parent.mkdir()
        manifest.write_text("{}", encoding="utf-8")
        path = self.declare({
            "winget_manifest": "pkgs/apps.json",
            "checks": ["dotnet --version", " git --version "],
            "mcp": False,
            "mcp_port": 8123,
            "workspace": "D:\\Lab\\",
        })
        profile = winbox.load(self.root)
        self.assertEqual(profile, {
            "winget_manifest": str(manifest),
            "checks": ["dotnet --version", "git --version"],
            "mcp": False,
            "mcp_port": 8123,
            "workspace": "D:\\Lab",
            "source": str(path),
        })

    def test_explicit_null_manifest_disables_the_default(self):
        (self.root / "winget-manifest.json").write_text("{}", encoding="utf-8")
        self.declare({"winget_manifest": None})
        self.assertIsNone(winbox.load(self.root)["winget_manifest"])

    def test_guest_payload_names_the_manifest_by_basename(self):
        manifest = self.root / "winget-manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        self.declare({"checks": ["git --version"], "mcp_port": 8001})
        payload = winbox.guest_payload(winbox.load(self.root))
        self.assertEqual(payload, {
            "winget_manifest": "winget-manifest.json",
            "checks": ["git --version"],
            "mcp": True,
            "mcp_port": 8001,
            "workspace": "C:\\SubfloorTest",
        })

    def _invalid(self, payload, code):
        self.declare(payload)
        with self.assertRaises(winbox.WinboxConfigError) as raised:
            winbox.load(self.root)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_malformed_and_mistyped_declarations_are_typed_failures(self):
        cases = (
            ("{not json", "winbox_config_invalid"),
            ('["a"]', "winbox_config_invalid"),
            ({"unexpected": 1}, "winbox_config_invalid"),
            ({"checks": "dotnet --version"}, "winbox_config_invalid"),
            ({"checks": ["ok", ""]}, "winbox_config_invalid"),
            ({"checks": ["ok", 7]}, "winbox_config_invalid"),
            ({"mcp": "yes"}, "winbox_config_invalid"),
            ({"mcp_port": 0}, "winbox_config_invalid"),
            ({"mcp_port": 70000}, "winbox_config_invalid"),
            ({"mcp_port": True}, "winbox_config_invalid"),
            ({"mcp_port": "8000"}, "winbox_config_invalid"),
            ({"workspace": ""}, "winbox_config_invalid"),
            ({"workspace": "SubfloorTest"}, "winbox_config_invalid"),
            ({"workspace": "/opt/subfloor"}, "winbox_config_invalid"),
            ({"workspace": 5}, "winbox_config_invalid"),
            ({"winget_manifest": ""}, "winbox_config_invalid"),
            ({"winget_manifest": "../outside.json"}, "winbox_config_invalid"),
            ({"winget_manifest": "absent.json"}, "winbox_manifest_missing"),
        )
        for payload, code in cases:
            with self.subTest(payload=payload):
                self._invalid(payload, code)

    def test_unc_workspace_is_accepted(self):
        self.declare({"workspace": "\\\\lab\\share\\subfloor"})
        self.assertEqual(
            winbox.load(self.root)["workspace"], "\\\\lab\\share\\subfloor"
        )


class ParseReportTests(unittest.TestCase):
    def test_last_json_line_wins_over_chatty_output(self):
        stdout = "\n".join([
            "Installing...",
            '{"steps": [], "ok": false}',
            "winget noise {not json}",
            '{"steps": [{"name": "workspace", "ok": true, "detail": "exists"}],'
            ' "ok": true}',
            "",
        ])
        self.assertEqual(winbox.parse_report(stdout), {
            "steps": [{"name": "workspace", "ok": True, "detail": "exists"}],
            "ok": True,
        })

    def test_missing_detail_defaults_to_empty_string(self):
        report = winbox.parse_report('{"steps":[{"name":"mcp","ok":false}],"ok":false}')
        self.assertEqual(report["steps"][0]["detail"], "")
        self.assertFalse(report["ok"])

    def test_absent_or_invalid_reports_raise(self):
        cases = (
            ("", "winbox_report_missing"),
            ("no json at all\n", "winbox_report_missing"),
            ("[1,2]", "winbox_report_missing"),
            (None, "winbox_report_missing"),
            ('{"steps": [], "ok": "yes"}', "winbox_report_invalid"),
            ('{"ok": true}', "winbox_report_invalid"),
            ('{"steps": "none", "ok": true}', "winbox_report_invalid"),
            ('{"steps": ["x"], "ok": true}', "winbox_report_invalid"),
            ('{"steps": [{"ok": true}], "ok": true}', "winbox_report_invalid"),
            ('{"steps": [{"name": "a"}], "ok": true}', "winbox_report_invalid"),
            (
                '{"steps": [{"name": "a", "ok": true, "detail": 3}], "ok": true}',
                "winbox_report_invalid",
            ),
        )
        for stdout, code in cases:
            with self.subTest(stdout=stdout):
                with self.assertRaises(winbox.WinboxConfigError) as raised:
                    winbox.parse_report(stdout)
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
