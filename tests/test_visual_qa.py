#!/usr/bin/env python3
"""Hermetic tests for the Visual QA runner (no network or browser)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import visual_qa  # noqa: E402


def minimal_config(**overrides):
    raw = {"serve": "npm run preview -- --port {port}", "routes": ["/"]}
    raw.update(overrides)
    return visual_qa.validate_config(raw)


def capture_factory(results, calls):
    class FakeCapture:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def capture(self, url, viewport, output, **kwargs):
            calls.append((url, dict(viewport), output, kwargs))
            result = dict(results.get((url, viewport["name"]), results["default"]))
            if result["image_written"]:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"PNG")
            return result

    return FakeCapture


OK = {
    "ok": True,
    "status": 200,
    "error": None,
    "image_width": 375,
    "image_height": 900,
    "image_written": True,
}
FAILED = {
    "ok": False,
    "status": 500,
    "error": "HTTP 500",
    "image_width": 375,
    "image_height": 812,
    "image_written": True,
}
NO_IMAGE = {
    "ok": False,
    "status": None,
    "error": "navigation failed",
    "image_width": 375,
    "image_height": 812,
    "image_written": False,
}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)

    def test_defaults_are_normalized_to_the_v1_contract(self):
        config = minimal_config()
        self.assertEqual(config["cwd"], ".")
        self.assertEqual(config["setup"], [])
        self.assertEqual(config["port"], 4173)
        self.assertEqual(config["ready_path"], "/")
        self.assertEqual(config["ready_timeout_s"], 120)
        self.assertEqual(config["settle_ms"], 500)
        self.assertEqual(
            config["viewports"],
            [
                {"name": "mobile", "width": 375, "height": 812},
                {"name": "tablet", "width": 768, "height": 1024},
                {"name": "desktop", "width": 1440, "height": 900},
            ],
        )
        self.assertIsNone(config["paths"])
        self.assertEqual(config["services"], [])
        self.assertEqual(config["artifact_retention_days"], 14)
        self.assertEqual(config["output"], "gallery")

    def test_invalid_contract_branches_are_rejected_with_specific_errors(self):
        cases = (
            ({"routes": ["/"]}, "'serve' is required"),
            ({"serve": "x"}, "'routes' must be a list"),
            ({"serve": "x", "routes": ["dashboard"]}, "must start with '/'"),
            ({"serve": "x", "routes": ["/"], "cwd": "../outside"}, "stay within"),
            (
                {"serve": "x", "routes": ["/"], "services": ["redis"]},
                "only [] or ['postgres']",
            ),
            (
                {"serve": "x", "routes": ["/"], "extra": True},
                "unknown config keys: extra",
            ),
            (
                {"serve": "x", "routes": ["/"], "port": True},
                "'port' must be an integer",
            ),
            (
                {"serve": "x", "routes": ["/"], "output": "../gallery"},
                "'output' must be a non-root path within the fork checkout",
            ),
            (
                {"serve": "x", "routes": ["/"], "output": "bad\npath"},
                "'output' must be a non-empty string",
            ),
            (
                {
                    "serve": "x",
                    "routes": ["/"],
                    "viewports": [
                        {"name": "phone", "width": 1, "height": 1},
                        {"name": "phone", "width": 2, "height": 2},
                    ],
                },
                "viewport name 'phone' is duplicated",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    visual_qa.VisualQaError,
                    message.replace("[", "\\[").replace("]", "\\]"),
                ):
                    visual_qa.validate_config(raw)

    def test_absent_config_is_neutral_but_bad_json_is_not(self):
        self.assertIsNone(visual_qa.load_config(self.repo))
        config_path = self.repo / ".sc-state" / "visual-qa.json"
        config_path.parent.mkdir()
        config_path.write_text('{"serve":')
        with self.assertRaisesRegex(visual_qa.VisualQaError, "invalid JSON.*line 1"):
            visual_qa.load_config(self.repo)


class SkipTest(unittest.TestCase):
    def test_path_filter_matches_nested_content_and_rejects_unrelated_changes(self):
        paths = ["src/**", "static/**", "package.json"]
        self.assertFalse(visual_qa.should_skip(paths, ["src/routes/a/b.svelte"]))
        self.assertFalse(visual_qa.should_skip(paths, ["docs/a.md", "package.json"]))
        self.assertTrue(visual_qa.should_skip(paths, ["docs/a.md", "README.md"]))
        self.assertFalse(visual_qa.should_skip([], ["README.md"]))
        self.assertFalse(visual_qa.should_skip(paths, None))

    def test_changed_paths_fetches_base_then_returns_exact_diff(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "fetch":
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(
                command, 0, "src/app.js\ndocs/a.md\n", ""
            )

        changed = visual_qa.pr_changed_paths(
            Path("/repo"), environ={"GITHUB_BASE_REF": "main"}, runner=runner
        )
        self.assertEqual(changed, ["src/app.js", "docs/a.md"])
        self.assertEqual(
            calls[0][0], ["git", "fetch", "--no-tags", "--depth=1", "origin", "main"]
        )
        self.assertEqual(
            calls[1][0], ["git", "diff", "--name-only", "FETCH_HEAD", "HEAD"]
        )
        self.assertEqual(
            [call[1]["cwd"] for call in calls], [Path("/repo"), Path("/repo")]
        )

    def test_unresolved_base_never_causes_a_false_skip(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(["git"], 1, "", "missing ref")
        )
        changed = visual_qa.pr_changed_paths(
            Path("/repo"), environ={"GITHUB_BASE_REF": "main"}, runner=runner
        )
        self.assertIsNone(changed)
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(visual_qa.should_skip(["src/**"], changed))


class GalleryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gallery = Path(temporary.name)

    def test_partial_route_failure_writes_exact_gallery_and_stays_advisory(self):
        config = minimal_config(
            routes=["/", "/broken"],
            viewports=[{"name": "phone", "width": 375, "height": 812}],
        )
        results = {
            "default": OK,
            ("http://app/broken", "phone"): FAILED,
        }
        calls = []
        summary = visual_qa.capture_gallery(
            config,
            "http://app",
            self.gallery,
            capture_factory=capture_factory(results, calls),
        )

        self.assertEqual(summary["outcome"], "passed")
        self.assertEqual(summary["routes_total"], 2)
        self.assertEqual(summary["routes_failed"], 1)
        self.assertEqual([row["ok"] for row in summary["routes"]], [True, False])
        self.assertEqual(
            [call[0] for call in calls], ["http://app/", "http://app/broken"]
        )
        self.assertEqual((self.gallery / "root" / "phone.png").read_bytes(), b"PNG")
        self.assertEqual((self.gallery / "broken" / "phone.png").read_bytes(), b"PNG")
        written = json.loads((self.gallery / "summary.json").read_text())
        self.assertEqual(written["routes"][1]["captures"][0]["status"], 500)
        index = (self.gallery / "index.html").read_text()
        self.assertIn('src="root/phone.png"', index)
        self.assertIn("HTTP 500", index)

    def test_all_routes_failed_is_fatal_and_missing_images_are_not_invented(self):
        config = minimal_config(
            routes=["/one", "/two"],
            viewports=[{"name": "phone", "width": 375, "height": 812}],
        )
        summary = visual_qa.capture_gallery(
            config,
            "http://app",
            self.gallery,
            capture_factory=capture_factory({"default": NO_IMAGE}, []),
        )
        self.assertEqual(summary["outcome"], "failed")
        self.assertEqual(summary["routes_failed"], 2)
        self.assertFalse((self.gallery / "one" / "phone.png").exists())
        self.assertFalse((self.gallery / "two" / "phone.png").exists())
        self.assertEqual(
            (self.gallery / "index.html").read_text().count("No screenshot"), 2
        )


class PrepareGalleryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)

    def test_non_runner_directory_is_rejected_without_losing_contents(self):
        gallery = self.repo / "gallery"
        gallery.mkdir()
        owned = gallery / "tracked-app-file.txt"
        owned.write_bytes(b"keep me")

        with self.assertRaisesRegex(
            visual_qa.VisualQaError,
            "gallery directory exists and isn't visual-QA output",
        ):
            visual_qa.prepare_gallery(gallery)

        self.assertEqual(owned.read_bytes(), b"keep me")
        self.assertEqual([path.name for path in gallery.iterdir()], [owned.name])

    def test_malformed_summary_is_not_accepted_as_runner_ownership(self):
        gallery = self.repo / "gallery"
        gallery.mkdir()
        summary = gallery / "summary.json"
        summary.write_text('{"outcome": "passed"}\n')

        with self.assertRaisesRegex(
            visual_qa.VisualQaError,
            "gallery directory exists and isn't visual-QA output",
        ):
            visual_qa.prepare_gallery(gallery)

        self.assertEqual(summary.read_text(), '{"outcome": "passed"}\n')

    def test_runner_gallery_is_cleared_before_reuse(self):
        gallery = self.repo / "gallery"
        gallery.mkdir()
        summary = {
            "generated_at": "2026-07-20T00:00:00Z",
            "outcome": "passed",
            "routes_total": 1,
            "routes_failed": 0,
            "routes": [],
        }
        (gallery / "summary.json").write_text(json.dumps(summary))
        stale = gallery / "root" / "desktop.png"
        stale.parent.mkdir()
        stale.write_bytes(b"old")

        visual_qa.prepare_gallery(gallery)
        self.assertEqual(list(gallery.iterdir()), [])

    def test_empty_gallery_is_reused_without_error(self):
        gallery = self.repo / "gallery"
        gallery.mkdir()

        visual_qa.prepare_gallery(gallery)
        self.assertEqual(list(gallery.iterdir()), [])

    def test_absent_gallery_is_created(self):
        absent = self.repo / "new-gallery"

        visual_qa.prepare_gallery(absent)

        self.assertTrue(absent.is_dir())
        self.assertEqual(list(absent.iterdir()), [])


class CommentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_comment_pins_marker_table_dimensions_and_artifact_links(self):
        summary = {
            "outcome": "passed",
            "routes_total": 1,
            "routes_failed": 0,
            "routes": [
                {
                    "route": "/",
                    "captures": [
                        {
                            "name": "mobile",
                            "ok": True,
                            "image_width": 375,
                            "image_height": 900,
                            "image_written": True,
                        }
                    ],
                }
            ],
        }
        body = visual_qa.build_comment(
            summary,
            environ={
                "GITHUB_SERVER_URL": "https://github.example",
                "GITHUB_REPOSITORY": "acme/app",
                "GITHUB_RUN_ID": "42",
            },
        )
        self.assertTrue(body.startswith("<!-- subfloor-visual-qa -->\n"))
        self.assertIn("### ✓ Visual QA captured · 1/1 routes served", body)
        self.assertIn("| `/` | ✓ 375×900 |", body)
        self.assertIn("https://github.example/acme/app/actions/runs/42#artifacts", body)
        self.assertIn("inline thumbnails are not available in v1", body)

    def test_existing_sticky_comment_is_updated_not_duplicated(self):
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 7}}))
        calls = []

        def requester(method, url, token, payload=None):
            calls.append((method, url, token, payload))
            if method == "GET":
                return [{"id": 99, "body": "<!-- subfloor-visual-qa -->\nold"}]
            return {}

        result = visual_qa.post_sticky_comment(
            "<!-- subfloor-visual-qa -->\nnew",
            environ={
                "GITHUB_TOKEN": "token",
                "GITHUB_REPOSITORY": "acme/app",
                "GITHUB_EVENT_PATH": str(event),
            },
            requester=requester,
        )
        self.assertTrue(result)
        self.assertEqual([call[0] for call in calls], ["GET", "PATCH"])
        self.assertTrue(calls[1][1].endswith("/repos/acme/app/issues/comments/99"))
        self.assertEqual(calls[1][3], {"body": "<!-- subfloor-visual-qa -->\nnew"})
        self.assertFalse(any(call[0] == "POST" for call in calls))

    def test_comment_failure_is_nonfatal_and_writes_no_false_success(self):
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"number": 7}))

        def fail(*_args, **_kwargs):
            raise PermissionError("read-only token")

        result = visual_qa.post_sticky_comment(
            "<!-- subfloor-visual-qa -->",
            environ={
                "GITHUB_TOKEN": "token",
                "GITHUB_REPOSITORY": "acme/app",
                "GITHUB_EVENT_PATH": str(event),
            },
            requester=fail,
        )
        self.assertFalse(result)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.log = (self.repo / "boot.log").open("w+")
        self.addCleanup(self.log.close)

    def test_ci_app_orders_boot_and_cleans_server_then_service_on_failure(self):
        events = []

        class Handle:
            def stop(self, _log):
                events.append("stop-service")

        class Server:
            pass

        server = Server()

        def services(_config, env, _log):
            events.append("services")
            env["DATABASE_URL"] = "postgresql://test"
            return [Handle()]

        config = minimal_config(port=4567)
        with (
            mock.patch.object(visual_qa, "start_services", side_effect=services),
            mock.patch.object(
                visual_qa,
                "run_setup",
                side_effect=lambda *_args: events.append("setup"),
            ),
            mock.patch.object(
                visual_qa,
                "start_server",
                side_effect=lambda *_args: events.append("server") or server,
            ),
            mock.patch.object(
                visual_qa,
                "wait_until_ready",
                side_effect=lambda *_args, **_kwargs: events.append("ready"),
            ),
            mock.patch.object(
                visual_qa,
                "stop_server",
                side_effect=lambda actual: events.append(
                    "stop-server" if actual is server else "wrong-server"
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "body failed"):
                with visual_qa.ci_app(config, self.repo, self.log) as (url, env):
                    events.append("body")
                    self.assertEqual(url, "http://127.0.0.1:4567")
                    self.assertEqual(env["DATABASE_URL"], "postgresql://test")
                    raise RuntimeError("body failed")

        self.assertEqual(
            events,
            [
                "services",
                "setup",
                "server",
                "ready",
                "body",
                "stop-server",
                "stop-service",
            ],
        )

    def test_setup_failure_stops_service_and_never_launches_server(self):
        stopped = []

        class Handle:
            def stop(self, _log):
                stopped.append("service")

        with (
            mock.patch.object(visual_qa, "start_services", return_value=[Handle()]),
            mock.patch.object(
                visual_qa,
                "run_setup",
                side_effect=visual_qa.VisualQaError("npm failed"),
            ),
            mock.patch.object(visual_qa, "start_server") as start_server,
        ):
            with self.assertRaisesRegex(visual_qa.VisualQaError, "npm failed"):
                with visual_qa.ci_app(minimal_config(), self.repo, self.log):
                    self.fail("ci_app yielded after setup failure")

        self.assertEqual(stopped, ["service"])
        self.assertEqual(start_server.call_count, 0)

    def test_ready_loop_fails_immediately_when_server_exits(self):
        server = mock.Mock()
        server.poll.return_value = 7
        opener = mock.Mock(side_effect=AssertionError("HTTP probe should not run"))
        with self.assertRaisesRegex(
            visual_qa.VisualQaError,
            "serve command exited before readiness \\(code 7\\)",
        ):
            visual_qa.wait_until_ready(
                "http://127.0.0.1:1/", timeout_s=10, server=server, opener=opener
            )
        self.assertEqual(opener.call_count, 0)


class ModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)

    def write_config(self, **overrides):
        raw = {"serve": "npm run preview -- --port {port}", "routes": ["/"]}
        raw.update(overrides)
        target = self.repo / ".sc-state" / "visual-qa.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(raw))
        return target

    def test_init_detects_npm_scripts_and_never_overwrites(self):
        (self.repo / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build", "preview": "vite preview"}})
        )
        (self.repo / "package-lock.json").write_text("{}")
        self.assertEqual(visual_qa.cmd_init(argparse.Namespace(), repo=self.repo), 0)
        target = self.repo / ".sc-state" / "visual-qa.json"
        config = json.loads(target.read_text())
        self.assertEqual(config["setup"], ["npm ci", "npm run build"])
        self.assertEqual(
            config["serve"], "npm run preview -- --port {port} --host 127.0.0.1"
        )
        self.assertEqual(config["routes"], ["/"])

        target.write_text("fork-owned\n")
        self.assertEqual(visual_qa.cmd_init(argparse.Namespace(), repo=self.repo), 0)
        self.assertEqual(target.read_text(), "fork-owned\n")

    def test_ci_without_config_is_neutral_and_never_installs_or_boots(self):
        installer = mock.Mock(side_effect=AssertionError("installer called"))
        app = mock.Mock(side_effect=AssertionError("app called"))
        with mock.patch.object(visual_qa, "publish_result") as publish:
            code = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={},
                installer=installer,
                app_context=app,
            )
        self.assertEqual(code, 0)
        self.assertEqual(installer.call_count, 0)
        self.assertEqual(app.call_count, 0)
        summary = publish.call_args.args[0]
        self.assertEqual(summary["outcome"], "neutral")
        self.assertEqual(
            summary["reason"],
            "Visual QA is not configured — run `./sc visual-qa init`.",
        )
        self.assertEqual(
            json.loads((self.repo / "gallery" / "summary.json").read_text())["outcome"],
            "neutral",
        )

    def test_ci_path_skip_is_neutral_and_does_not_cross_capture_boundaries(self):
        self.write_config(paths=["src/**"])
        installer = mock.Mock(side_effect=AssertionError("installer called"))
        app = mock.Mock(side_effect=AssertionError("app called"))
        changed = mock.Mock(return_value=["docs/readme.md"])
        with mock.patch.object(visual_qa, "publish_result") as publish:
            code = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={"GITHUB_BASE_REF": "main"},
                changed_paths=changed,
                installer=installer,
                app_context=app,
            )
        self.assertEqual(code, 0)
        self.assertEqual(changed.call_count, 1)
        self.assertEqual(installer.call_count, 0)
        self.assertEqual(app.call_count, 0)
        self.assertEqual(
            publish.call_args.args[0]["reason"], "No configured app paths changed."
        )

    def test_ci_uses_validated_config_output_instead_of_tracked_gallery(self):
        self.write_config(
            output="artifacts/visual-qa",
            viewports=[{"name": "phone", "width": 375, "height": 812}],
        )
        tracked = self.repo / "gallery" / "app-image.png"
        tracked.parent.mkdir()
        tracked.write_bytes(b"fork-owned")

        @contextmanager
        def app(_config, _repo, log):
            log.write("ready\n")
            yield "http://app", {}

        github_output = self.repo / "github-output"
        code = visual_qa.cmd_ci(
            argparse.Namespace(),
            repo=self.repo,
            environ={"GITHUB_OUTPUT": str(github_output)},
            changed_paths=lambda *_args, **_kwargs: None,
            installer=lambda: None,
            app_context=app,
            capture_factory=capture_factory({"default": OK}, []),
        )

        output = self.repo / "artifacts" / "visual-qa"
        self.assertEqual(code, 0)
        self.assertEqual(tracked.read_bytes(), b"fork-owned")
        self.assertEqual(
            [path.name for path in tracked.parent.iterdir()], [tracked.name]
        )
        self.assertTrue((output / "root" / "phone.png").is_file())
        self.assertEqual(
            json.loads((output / "summary.json").read_text())["outcome"], "passed"
        )
        self.assertEqual(github_output.read_text(), "output=artifacts/visual-qa\n")

    def test_ci_gallery_collision_publishes_failure_without_touching_fork_files(self):
        self.write_config()
        tracked = self.repo / "gallery" / "tracked-app-file.txt"
        tracked.parent.mkdir()
        tracked.write_bytes(b"keep me")
        step_summary = self.repo / "step-summary.md"
        github_output = self.repo / "github-output"
        installer = mock.Mock(side_effect=AssertionError("installer called"))
        app = mock.Mock(side_effect=AssertionError("app called"))

        with mock.patch.object(visual_qa, "post_sticky_comment") as post:
            code = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_STEP_SUMMARY": str(step_summary),
                },
                installer=installer,
                app_context=app,
            )

        self.assertEqual(code, 1)
        self.assertEqual(installer.call_count, 0)
        self.assertEqual(app.call_count, 0)
        self.assertEqual(tracked.read_bytes(), b"keep me")
        self.assertEqual(
            [path.name for path in tracked.parent.iterdir()], [tracked.name]
        )
        body = post.call_args.args[0]
        self.assertIn("### ✗ Visual QA could not run", body)
        self.assertIn("choose another config key 'output'", body)
        written_summary = step_summary.read_text()
        self.assertIn("### ✗ Visual QA could not run", written_summary)
        self.assertIn("choose another config key 'output'", written_summary)
        self.assertEqual(github_output.read_text(), "output=gallery\n")

    def test_ci_runs_mock_capture_and_fails_only_when_all_routes_fail(self):
        self.write_config(
            routes=["/one", "/two"],
            viewports=[{"name": "phone", "width": 375, "height": 812}],
        )

        @contextmanager
        def app(_config, _repo, log):
            log.write("ready\n")
            yield "http://app", {}

        results = {"default": FAILED, ("http://app/one", "phone"): OK}
        with mock.patch.object(visual_qa, "publish_result") as publish:
            partial = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={},
                changed_paths=lambda *_args, **_kwargs: None,
                installer=lambda: None,
                app_context=app,
                capture_factory=capture_factory(results, []),
            )
        self.assertEqual(partial, 0)
        self.assertEqual(publish.call_args.args[0]["routes_failed"], 1)
        self.assertTrue((self.repo / "gallery" / "one" / "phone.png").exists())
        self.assertTrue((self.repo / "gallery" / "two" / "phone.png").exists())

        with mock.patch.object(visual_qa, "publish_result") as publish:
            failed = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={},
                changed_paths=lambda *_args, **_kwargs: None,
                installer=lambda: None,
                app_context=app,
                capture_factory=capture_factory({"default": FAILED}, []),
            )
        self.assertEqual(failed, 1)
        self.assertEqual(publish.call_args.args[0]["outcome"], "failed")
        self.assertEqual(publish.call_args.args[0]["routes_failed"], 2)

    def test_boot_failure_preserves_log_tail_in_artifact_and_comment_input(self):
        self.write_config()

        @contextmanager
        def broken_app(_config, _repo, log):
            log.write("server never became ready\n")
            log.flush()
            raise visual_qa.VisualQaError("readiness timed out")
            yield  # pragma: no cover

        with mock.patch.object(visual_qa, "publish_result") as publish:
            code = visual_qa.cmd_ci(
                argparse.Namespace(),
                repo=self.repo,
                environ={},
                changed_paths=lambda *_args, **_kwargs: None,
                installer=lambda: None,
                app_context=broken_app,
            )
        self.assertEqual(code, 1)
        summary = publish.call_args.args[0]
        self.assertEqual(summary["error"], "readiness timed out")
        self.assertEqual(summary["boot_log_tail"], "server never became ready")
        self.assertIn(
            "server never became ready",
            (self.repo / "gallery" / "boot.log").read_text(),
        )
        self.assertEqual(
            json.loads((self.repo / "gallery" / "summary.json").read_text())[
                "boot_log_tail"
            ],
            "server never became ready",
        )

    def test_local_run_writes_gallery_but_rejects_unsafe_output(self):
        self.write_config(viewports=[{"name": "phone", "width": 375, "height": 812}])
        args = argparse.Namespace(url="http://app", output=Path("qa-gallery"))
        code = visual_qa.cmd_run(
            args,
            repo=self.repo,
            environ={},
            capture_factory=capture_factory({"default": OK}, []),
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.repo / "qa-gallery" / "root" / "phone.png").exists())
        self.assertEqual(
            json.loads((self.repo / "qa-gallery" / "summary.json").read_text())[
                "outcome"
            ],
            "passed",
        )

        unsafe = argparse.Namespace(url="http://app", output=Path(".."))
        with self.assertRaisesRegex(
            visual_qa.VisualQaError, "within the fork checkout"
        ):
            visual_qa.cmd_run(unsafe, repo=self.repo, environ={})

    def test_playwright_pin_and_root_dispatch_are_literal(self):
        calls = []

        def runner(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        visual_qa.install_playwright(runner=runner)
        self.assertEqual(
            calls[0], [sys.executable, "-m", "pip", "install", "playwright==1.54.0"]
        )
        self.assertEqual(
            calls[1],
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
        )
        dispatcher = (ROOT / "sc").read_text()
        self.assertIn(
            'visual-qa)         exec "$PY" "$S/visual_qa.py" "$@" ;;', dispatcher
        )
        self.assertIn("./sc visual-qa <mode>", dispatcher)

    def test_playwright_install_failure_stops_before_browser_install(self):
        calls = []

        def runner(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, 5)

        with self.assertRaisesRegex(
            visual_qa.VisualQaError, "Playwright setup failed \\(5\\)"
        ):
            visual_qa.install_playwright(runner=runner)
        self.assertEqual(
            calls,
            [[sys.executable, "-m", "pip", "install", "playwright==1.54.0"]],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
