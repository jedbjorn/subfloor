"""Authoritative harness surface/status projection contracts."""
from __future__ import annotations

import json
import io
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import harness_surfaces
import run


class HarnessSurfaceProjectionTest(unittest.TestCase):
    def test_existing_surface_roster_and_deepseek_gates_are_explicit(self) -> None:
        commands = []

        def executable(command: str) -> str:
            commands.append(command)
            return f"/bin/{command}"

        projection = harness_surfaces.project(
            executable=executable,
        )

        self.assertEqual(
            harness_surfaces.known_terminal_harnesses(),
            ["claude", "codex", "kimi", "opencode", "vibe"],
        )
        self.assertEqual(
            harness_surfaces.known_runnable_harnesses(),
            ["claude", "codex", "deepseek", "kimi", "opencode", "vibe"],
        )
        self.assertEqual(
            harness_surfaces.known_interactive_harnesses(),
            ["claude", "codex", "deepseek", "kimi", "opencode", "vibe"],
        )
        for harness in ("claude", "codex", "kimi", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(projection[harness]["surfaces"], {
                    "terminal": True,
                    "one_shot": True,
                    "browser": True,
                    "sprint": True,
                })
                self.assertTrue(projection[harness]["healthy"])
                self.assertIsNone(projection[harness]["unavailable_reason"])
        self.assertEqual(projection["vibe"]["surfaces"], {
            "terminal": True,
            "one_shot": False,
            "browser": False,
            "sprint": False,
        })
        self.assertEqual(projection["deepseek"], {
            "shipped": True,
            "installed": True,
            "enabled": True,
            "healthy": True,
            "compatibility": "declared",
            "surfaces": {
                "terminal": False,
                "one_shot": True,
                "browser": True,
                "sprint": True,
            },
            "unavailable_reason": None,
        })
        self.assertIn("dsh", commands)
        self.assertNotIn("deepseek-harness", commands)

    def test_missing_runtime_and_disablement_have_stable_distinct_reasons(self) -> None:
        missing = harness_surfaces.project(
            executable=lambda command: None,
        )
        disabled = harness_surfaces.project(
            env={"SC_DISABLED_HARNESSES": " codex, DEEPSEEK "},
            executable=lambda command: command,
        )

        self.assertFalse(missing["deepseek"]["installed"])
        self.assertEqual(
            missing["deepseek"]["unavailable_reason"], "HARNESS_UNAVAILABLE"
        )
        self.assertFalse(disabled["codex"]["enabled"])
        self.assertFalse(disabled["codex"]["healthy"])
        self.assertEqual(disabled["codex"]["unavailable_reason"], "HARNESS_DISABLED")
        self.assertFalse(disabled["deepseek"]["enabled"])
        self.assertEqual(
            disabled["deepseek"]["unavailable_reason"], "HARNESS_DISABLED"
        )

    def test_unknown_historical_harness_is_readable_but_never_runnable(self) -> None:
        projection = harness_surfaces.project(
            ["retired-harness"], executable=lambda command: command
        )

        self.assertEqual(projection["retired-harness"], {
            "shipped": False,
            "installed": False,
            "enabled": False,
            "healthy": False,
            "compatibility": "unknown",
            "surfaces": {
                "terminal": False,
                "one_shot": False,
                "browser": False,
                "sprint": False,
            },
            "unavailable_reason": "HARNESS_NOT_SHIPPED",
        })
        self.assertNotIn(
            "retired-harness", harness_surfaces.known_terminal_harnesses()
        )
        self.assertNotIn(
            "retired-harness", harness_surfaces.known_runnable_harnesses()
        )

    def test_model_visibility_is_separate_from_launch_default_eligibility(self) -> None:
        manifests = {
            "one-shot-only": {
                "harness": "one-shot-only",
                "surfaces": {
                    "terminal": False,
                    "one_shot": True,
                    "browser": False,
                    "sprint": False,
                },
                "headless": {"launch": ["one-shot"]},
            },
            "browser-only": {
                "harness": "browser-only",
                "surfaces": {
                    "terminal": False,
                    "one_shot": False,
                    "browser": True,
                    "sprint": False,
                },
            },
            "local-web-only": {
                "harness": "local-web-only",
                "surfaces": {
                    "terminal": False,
                    "one_shot": False,
                    "browser": False,
                    "sprint": False,
                },
                "interactive": {
                    "kind": "local_web",
                    "launch": ["local-web"],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            adapters = Path(tmp)
            for harness, manifest in manifests.items():
                path = adapters / harness / "adapter.json"
                path.parent.mkdir()
                path.write_text(json.dumps(manifest))
            with mock.patch.object(harness_surfaces, "ADAPTERS", adapters), \
                    mock.patch.object(
                        harness_surfaces,
                        "_browser_contract_proven",
                        side_effect=lambda harness: harness == "browser-only",
                    ):
                visible = harness_surfaces.known_runnable_harnesses()
                defaults = harness_surfaces.known_interactive_harnesses()

        self.assertEqual(
            visible, ["browser-only", "local-web-only", "one-shot-only"]
        )
        self.assertEqual(defaults, ["local-web-only"])

    def test_interactive_detection_includes_terminal_and_local_web_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapters = Path(tmp)
            terminal = adapters / "terminal" / "adapter.json"
            local_web = adapters / "local-web" / "adapter.json"
            browser_only = adapters / "browser-only" / "adapter.json"
            terminal.parent.mkdir()
            local_web.parent.mkdir()
            browser_only.parent.mkdir()
            terminal.write_text(
                '{"harness":"terminal","launch":["terminal"],'
                '"surfaces":{"terminal":true}}'
            )
            local_web.write_text(
                '{"harness":"local-web","surfaces":{"terminal":false},'
                '"interactive":{"kind":"local_web","launch":["web-cli","web"]}}'
            )
            browser_only.write_text(
                '{"harness":"browser-only","runtime":{"command":"browser"},'
                '"surfaces":{"terminal":false}}'
            )
            with mock.patch.object(run, "ADAPTERS", adapters), mock.patch.object(
                run.shutil, "which", side_effect=lambda command: f"/bin/{command}"
            ):
                detected = run.detect_harnesses()

        self.assertEqual(detected, ["local-web", "terminal"])
        self.assertNotIn("browser-only", detected)

    def test_picker_selected_local_web_signals_the_host_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            run, "REPO_ROOT", Path(tmp)
        ):
            run.signal_browser_handoff("31415")
            marker = (
                Path(tmp)
                / ".sc-state"
                / "local"
                / "run"
                / "browser-handoff-31415"
            )
            self.assertEqual(marker.read_text(), "deepseek\n")

            run.signal_browser_handoff("../../outside")
            self.assertFalse((Path(tmp) / "outside").exists())

    def test_selected_local_web_uses_web_validation_in_the_boot_path(self) -> None:
        class StopAfterSelection(RuntimeError):
            pass

        con = mock.Mock()
        chosen = {"shell_id": 5, "shortname": "DEV3", "flavor": "dev"}
        adapter = {
            "harness": "deepseek",
            "surfaces": {"terminal": False},
            "interactive": {"kind": "local_web", "launch": ["dsh", "web"]},
        }
        local_web_gate = mock.Mock(wraps=run.require_local_web_surface)
        terminal_gate = mock.Mock(wraps=run.require_harness_surface)

        with mock.patch.dict(run.os.environ, {"RENDER_ONLY": "1"}, clear=True), \
                mock.patch.object(
                    run.sys,
                    "argv",
                    ["run.py", "DEV3", "--harness", "deepseek"],
                ), \
                mock.patch.object(run.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(
                    run,
                    "flavor_defaults",
                    return_value={
                        "dev": {
                            "default_harness": "claude",
                            "models": {"deepseek": None},
                        }
                    },
                ), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", return_value=chosen), \
                mock.patch.object(run, "browser_conversation_active", return_value=False), \
                mock.patch.object(run, "confirm_live", return_value=True), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(run, "load_adapter", return_value=adapter), \
                mock.patch.object(run, "require_local_web_surface", local_web_gate), \
                mock.patch.object(run, "require_harness_surface", terminal_gate), \
                mock.patch.object(run, "cleanup_before_launch"), \
                mock.patch.object(
                    run, "open_session", side_effect=StopAfterSelection
                ), \
                self.assertRaises(StopAfterSelection):
            run.main()

        local_web_gate.assert_called_once_with(adapter)
        terminal_gate.assert_not_called()

    def test_local_web_boot_passes_selected_shell_id_not_inherited_identity(self) -> None:
        class Con:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "shell_id": 5,
                    "display_name": "Code-01",
                    "shortname": "DEV3",
                    "api_key": "canonical-token",
                }

            def close(self) -> None:
                return None

        class Spinner:
            label = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw) / "worktree"
            worktree.mkdir()
            chosen = {"shell_id": 5, "shortname": "DEV3", "flavor": "dev"}
            adapter = {
                "harness": "deepseek",
                "surfaces": {"terminal": False},
                "interactive": {"kind": "local_web", "launch": ["dsh", "web"]},
            }
            captured = {}
            transcript = io.StringIO()
            fake_analytics = types.SimpleNamespace(
                sweep=lambda **_kwargs: {"inserted": 0, "updated": 0}
            )
            patchers = (
                mock.patch.object(run.sys, "argv", ["run.py", "DEV3", "--harness", "deepseek", "--local-web"]),
                mock.patch.object(run.sys.stdin, "isatty", return_value=False),
                mock.patch.object(run.callable_floor, "require_callable_floor"),
                mock.patch.object(run.install, "is_source_repo", return_value=True),
                mock.patch.object(run.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(run.global_pointer, "write_global_pointers"),
                mock.patch.multiple(
                    run,
                    open_db=mock.DEFAULT,
                    authenticate=mock.DEFAULT,
                    flavor_defaults=mock.DEFAULT,
                    list_shells=mock.DEFAULT,
                    pick_shell=mock.DEFAULT,
                    browser_conversation_active=mock.DEFAULT,
                    confirm_live=mock.DEFAULT,
                    ensure_harness_path=mock.DEFAULT,
                    load_adapter=mock.DEFAULT,
                    cleanup_before_launch=mock.DEFAULT,
                    open_session=mock.DEFAULT,
                    shell_work_dir=mock.DEFAULT,
                    ensure_worktree=mock.DEFAULT,
                    sync_worktree=mock.DEFAULT,
                    link_worktree_map=mock.DEFAULT,
                    main_checkout_note=mock.DEFAULT,
                    declared_work_repo_note=mock.DEFAULT,
                    compose_boot=mock.DEFAULT,
                    render_harness_skills=mock.DEFAULT,
                    atomic_write=mock.DEFAULT,
                    emit_adapter=mock.DEFAULT,
                    resolve_opencode_plugins=mock.DEFAULT,
                    apply_merge_json=mock.DEFAULT,
                    apply_managed_mcp=mock.DEFAULT,
                    apply_sandbox=mock.DEFAULT,
                    review_gui_panel=mock.DEFAULT,
                ),
                mock.patch.object(run.seed_skills, "sync_engine_skills", return_value=[]),
                mock.patch.object(run.ports_mod, "resolve", return_value={"port": 8837}),
                mock.patch.object(run.style, "spinner", return_value=Spinner()),
                mock.patch.object(run.sys, "stdout", transcript),
                mock.patch.dict(sys.modules, {"analytics": fake_analytics}),
                mock.patch.object(
                    run.deepseek_web,
                    "ensure",
                    side_effect=lambda _worktree, *, env: captured.update(env) or {"reused": False, "url": "http://127.0.0.1:8942"},
                ),
            )
            with mock.patch.dict(
                run.os.environ, {"SC_SHELL_ID": "999", "SC_NO_AUTOPRUNE": "1"}, clear=True
            ), ExitStack() as stack:
                applied = [stack.enter_context(patcher) for patcher in patchers]
                run.open_db.return_value = Con()
                run.authenticate.return_value = {"user_id": 1}
                run.flavor_defaults.return_value = {"dev": {"default_harness": "deepseek", "models": {"deepseek": None}}}
                run.list_shells.return_value = [chosen]
                run.pick_shell.return_value = chosen
                run.browser_conversation_active.return_value = False
                run.confirm_live.return_value = True
                run.load_adapter.return_value = adapter
                run.open_session.return_value = (17, 18)
                run.shell_work_dir.return_value = worktree
                run.sync_worktree.return_value = "current"
                run.link_worktree_map.return_value = None
                run.main_checkout_note.return_value = "current"
                run.declared_work_repo_note.return_value = "current"
                run.compose_boot.return_value = "boot"
                run.render_harness_skills.return_value = {"written": [], "deleted": [], "skipped": [], "dirs": []}
                run.emit_adapter.return_value = []
                run.apply_merge_json.return_value = []
                run.apply_managed_mcp.return_value = []
                run.apply_sandbox.return_value = []
                run.review_gui_panel.return_value = "gui"
                run.main()

        self.assertEqual(captured["SC_SHELL_ID"], "5")
        self.assertEqual(captured["SC_SHELL_SHORTNAME"], "DEV3")
        self.assertEqual(captured["SC_API_TOKEN"], "canonical-token")
        self.assertEqual(captured["SC_API_BASE"], "http://127.0.0.1:8837")
        self.assertNotIn("sc_generation=", transcript.getvalue())

    def test_explicit_unsupported_terminal_and_one_shot_requests_fail_early(self) -> None:
        adapter = {
            "harness": "deepseek",
            "surfaces": {"terminal": False, "one_shot": False},
        }

        for surface, label in (("terminal", "terminal"), ("one_shot", "one-shot")):
            with self.subTest(surface=surface), self.assertRaisesRegex(
                ValueError,
                rf"harness 'deepseek' does not support {label}",
            ):
                run.require_harness_surface(adapter, surface)

    def test_local_web_entry_requires_an_explicit_interactive_contract(self) -> None:
        run.require_local_web_surface({
            "harness": "deepseek",
            "interactive": {"kind": "local_web"},
        })
        with self.assertRaisesRegex(ValueError, "does not support local Web entry"):
            run.require_local_web_surface({"harness": "codex"})


if __name__ == "__main__":
    unittest.main()
