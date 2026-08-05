"""Golden contracts for adapter-owned Windows MCP injection declarations."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / ".super-coder" / "adapters"
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run  # noqa: E402


class WindowsMcpAdapterContractTest(unittest.TestCase):
    def adapter(self, harness: str) -> dict:
        return json.loads((ADAPTERS / harness / "adapter.json").read_text())

    def linked_vm(self):
        return mock.patch.object(
            run.ports_mod,
            "resolve",
            return_value={"vm": {"domain": "win-test"}},
        )

    def test_capability_matrix_is_explicit(self) -> None:
        supported = {}
        unsupported = {}
        for harness in ("claude", "codex", "opencode", "kimi", "vibe"):
            streamable = self.adapter(harness)["mcp"]["streamable_http"]
            if streamable["supported"]:
                supported[harness] = streamable["managed_server"]["url"]
            else:
                unsupported[harness] = streamable["reason"]

        self.assertEqual(supported, {
            "claude": "http://127.0.0.1:18000/mcp",
            "codex": "http://127.0.0.1:18000/mcp",
            "opencode": "http://127.0.0.1:18000/mcp",
        })
        self.assertEqual(set(unsupported), {"kimi", "vibe"})
        self.assertTrue(all(unsupported.values()))

    def test_supported_adapter_recipes_match_harness_goldens(self) -> None:
        expected = {
            "claude": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:18000/mcp",
                "launch_args": [
                    "--mcp-config",
                    (
                        '{"mcpServers":{"windows-mcp":{"type":"http",'
                        '"url":"http://127.0.0.1:18000/mcp"}}}'
                    ),
                ],
            },
            "codex": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:18000/mcp",
                "launch_args": [
                    "-c",
                    'mcp_servers.windows-mcp.url="http://127.0.0.1:18000/mcp"',
                ],
            },
            "opencode": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:18000/mcp",
                "merge_json": {
                    "opencode.json": {
                        "mcp": {
                            "windows-mcp": {
                                "type": "remote",
                                "url": "http://127.0.0.1:18000/mcp",
                                "enabled": True,
                                "oauth": False,
                            }
                        }
                    }
                },
            },
        }

        with self.linked_vm():
            actual = {
                harness: run.managed_mcp_injection(self.adapter(harness))
                for harness in expected
            }
        self.assertEqual(actual, expected)

    def test_repeat_resolution_is_stable_and_unsupported_is_empty(self) -> None:
        with self.linked_vm():
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    adapter = self.adapter(harness)
                    first = run.managed_mcp_injection(adapter)
                    second = run.managed_mcp_injection(adapter)
                    self.assertEqual(second, first)

            for harness in ("kimi", "vibe"):
                with self.subTest(harness=harness):
                    self.assertIsNone(
                        run.managed_mcp_injection(self.adapter(harness))
                    )

    def test_unlinked_fork_has_no_managed_recipe_for_supported_adapters(self):
        opencode = self.adapter("opencode")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.linked_vm():
                self.assertEqual(
                    run.emit_adapter(opencode, root), ["opencode.json"]
                )
                self.assertEqual(
                    run.apply_managed_mcp(opencode, root), ["opencode.json"]
                )
            linked = json.loads((root / "opencode.json").read_text())
            self.assertIn("windows-mcp", linked["mcp"])

            with mock.patch.object(
                run.ports_mod,
                "resolve",
                return_value={"port": 8800, "dev_port": 8900},
            ):
                for harness in ("claude", "codex", "opencode"):
                    with self.subTest(harness=harness):
                        candidate = self.adapter(harness)
                        self.assertIsNone(run.managed_mcp_injection(candidate))
                        command = run.headless_command(candidate, "first turn")
                        self.assertNotIn("--mcp-config", command)
                        self.assertNotIn(
                            "mcp_servers.windows-mcp.url", " ".join(command)
                        )

                self.assertEqual(
                    run.emit_adapter(opencode, root), ["opencode.json"]
                )
                self.assertEqual(run.apply_managed_mcp(opencode, root), [])
                unlinked = json.loads((root / "opencode.json").read_text())
                self.assertEqual(unlinked["mcp"], {})

    def test_repeat_opencode_launch_merge_preserves_siblings_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "opencode.json"
            config.write_text(json.dumps({
                "model": "existing/model",
                "mcp": {
                    "sibling": {
                        "type": "remote",
                        "url": "https://example.test/mcp",
                        "enabled": True,
                    }
                },
            }))

            adapter = self.adapter("opencode")
            with self.linked_vm():
                self.assertEqual(
                    run.apply_managed_mcp(adapter, root), ["opencode.json"]
                )
                first = config.read_text()
                self.assertEqual(
                    run.apply_managed_mcp(adapter, root), ["opencode.json"]
                )
            self.assertEqual(config.read_text(), first)

            merged = json.loads(first)
            self.assertEqual(merged["model"], "existing/model")
            self.assertEqual(set(merged["mcp"]), {"sibling", "windows-mcp"})
            self.assertEqual(merged["mcp"]["windows-mcp"], {
                "type": "remote",
                "url": "http://127.0.0.1:18000/mcp",
                "enabled": True,
                "oauth": False,
            })

    def test_headless_launch_receives_one_managed_definition(self) -> None:
        with self.linked_vm():
            for harness in ("claude", "codex"):
                with self.subTest(harness=harness):
                    adapter = self.adapter(harness)
                    command = run.headless_command(adapter, "first turn")
                    injection = run.managed_mcp_injection(adapter)["launch_args"]
                    self.assertEqual(
                        command.count(injection[0]),
                        1,
                        "a repeat-safe launch carries one injection flag",
                    )
                    index = command.index(injection[0])
                    self.assertEqual(
                        command[index:index + len(injection)], injection
                    )

        kimi = self.adapter("kimi")
        command = run.headless_command(kimi, "first turn")
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("mcp_servers.windows-mcp.url", " ".join(command))


if __name__ == "__main__":
    unittest.main()
