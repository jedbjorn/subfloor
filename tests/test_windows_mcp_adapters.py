"""Golden contracts for adapter-owned Windows MCP injection declarations."""
from __future__ import annotations

import contextlib
import json
import os
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
from conversation_adapters.base import ConversationContext, merged_env  # noqa: E402


class WindowsMcpAdapterContractTest(unittest.TestCase):
    def adapter(self, harness: str) -> dict:
        return json.loads((ADAPTERS / harness / "adapter.json").read_text())

    def linked_vm(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            run.ports_mod,
            "resolve",
            return_value={"vm": {"domain": "win-test"}},
        ))
        stack.enter_context(mock.patch.object(
            run.ports_mod, "ensure_vm_mcp_port", return_value=8877,
        ))
        return stack

    def test_capability_matrix_is_explicit(self) -> None:
        supported = {}
        unsupported = {}
        for harness in ("claude", "codex", "opencode", "kimi", "vibe"):
            streamable = self.adapter(harness)["mcp"]["streamable_http"]
            if streamable["supported"]:
                supported[harness] = streamable["managed_servers"][0]["url"]
            else:
                unsupported[harness] = streamable["reason"]

        # The manifest carries a template; the port is per instance (#1836).
        self.assertEqual(supported, {
            "claude": "http://127.0.0.1:{port}/mcp",
            "codex": "http://127.0.0.1:{port}/mcp",
            "opencode": "http://127.0.0.1:{port}/mcp",
            "kimi": "http://127.0.0.1:{port}/mcp",
        })
        self.assertEqual(set(unsupported), {"vibe"})
        self.assertTrue(all(unsupported.values()))

    def test_supported_adapter_recipes_match_harness_goldens(self) -> None:
        expected = {
            "claude": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:8877/mcp",
                "launch_args": [
                    "--mcp-config",
                    (
                        '{"mcpServers":{"windows-mcp":{"type":"http",'
                        '"url":"http://127.0.0.1:8877/mcp"}}}'
                    ),
                ],
            },
            "codex": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:8877/mcp",
                "launch_args": [
                    "-c",
                    'mcp_servers.windows-mcp.url="http://127.0.0.1:8877/mcp"',
                ],
            },
            "opencode": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:8877/mcp",
                "merge_json": {
                    "opencode.json": {
                        "mcp": {
                            "windows-mcp": {
                                "type": "remote",
                                "url": "http://127.0.0.1:8877/mcp",
                                "enabled": True,
                                "oauth": False,
                            }
                        }
                    }
                },
            },
            "kimi": {
                "name": "windows-mcp",
                "url": "http://127.0.0.1:8877/mcp",
                "merge_json": {
                    ".kimi-code/mcp.json": {
                        "mcpServers": {
                            "windows-mcp": {
                                "url": "http://127.0.0.1:8877/mcp",
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
            for harness in ("claude", "codex", "opencode", "kimi"):
                with self.subTest(harness=harness):
                    adapter = self.adapter(harness)
                    first = run.managed_mcp_injection(adapter)
                    second = run.managed_mcp_injection(adapter)
                    self.assertEqual(second, first)

            for harness in ("vibe",):
                with self.subTest(harness=harness):
                    self.assertIsNone(
                        run.managed_mcp_injection(self.adapter(harness))
                    )

    def test_kimi_headless_trust_is_scoped_to_managed_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = ConversationContext(worktree=Path(tmp))
            with self.linked_vm(), mock.patch.dict(os.environ, {}, clear=True):
                for harness in ("claude", "codex", "opencode", "kimi"):
                    adapter = self.adapter(harness)
                    managed = run.managed_mcp_injection(adapter)
                    expected = (
                        {"KIMI_CODE_TRUST_WORKSPACE": "1"}
                        if harness == "kimi" else {}
                    )
                    with self.subTest(harness=harness):
                        self.assertEqual(
                            run.managed_mcp_environment(
                                adapter, managed, headless=True
                            ),
                            expected,
                        )
                        self.assertEqual(
                            run.managed_mcp_environment(
                                adapter, managed, headless=False
                            ),
                            {},
                        )
                        self.assertEqual(
                            merged_env(adapter, context).get(
                                "KIMI_CODE_TRUST_WORKSPACE"
                            ),
                            expected.get("KIMI_CODE_TRUST_WORKSPACE"),
                        )

            with mock.patch.object(
                run.ports_mod,
                "resolve",
                return_value={"port": 8800, "dev_port": 8900},
            ), mock.patch.dict(os.environ, {}, clear=True):
                adapter = self.adapter("kimi")
                self.assertEqual(
                    run.managed_mcp_environment(adapter, None, headless=True),
                    {},
                )
                self.assertNotIn(
                    "KIMI_CODE_TRUST_WORKSPACE", merged_env(adapter, context)
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
                for harness in ("claude", "codex", "opencode", "kimi"):
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
                "url": "http://127.0.0.1:8877/mcp",
                "enabled": True,
                "oauth": False,
            })

    def test_repeat_kimi_launch_merge_preserves_siblings_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".kimi-code" / "mcp.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "custom": {"keep": True},
                "mcpServers": {
                    "sibling": {"url": "https://example.test/mcp"},
                },
            }))

            adapter = self.adapter("kimi")
            with self.linked_vm():
                self.assertEqual(
                    run.apply_managed_mcp(adapter, root),
                    [".kimi-code/mcp.json"],
                )
                first = config.read_text()
                self.assertEqual(
                    run.apply_managed_mcp(adapter, root),
                    [".kimi-code/mcp.json"],
                )
            self.assertEqual(config.read_text(), first)

            merged = json.loads(first)
            self.assertEqual(merged["custom"], {"keep": True})
            self.assertEqual(
                set(merged["mcpServers"]), {"sibling", "windows-mcp"}
            )
            self.assertEqual(merged["mcpServers"]["windows-mcp"], {
                "url": "http://127.0.0.1:8877/mcp",
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

        with self.linked_vm():
            kimi = self.adapter("kimi")
            injection = run.managed_mcp_injection(kimi)
            command = run.headless_command(kimi, "first turn")
        self.assertEqual(
            list(injection["merge_json"]), [".kimi-code/mcp.json"]
        )
        self.assertNotIn("launch_args", injection)
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("mcp_servers.windows-mcp.url", " ".join(command))


if __name__ == "__main__":
    unittest.main()
