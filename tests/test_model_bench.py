"""Hermetic benchmark controller tests: temporary Git repos, no provider calls."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("model_bench", SCRIPTS / "model_bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=bench.clean_environment(),
                          capture_output=True, text=True, check=True).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Bench Test")
    git(path, "config", "user.email", "bench@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    (path / "app.txt").write_text("existing_button existing_view filter_field\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "fixture")
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return git(path, "rev-parse", "HEAD")


def route_proof(route):
    return {"ok": True, "stale": False, "binding": {
        "harness": route["harness"], "requested_model": route["model"],
        "provider_model": route["model"], "native_variant_id": None,
        "requested_effort": route.get("effort"), "effective_effort": route.get("effort"),
    }}


class FreezeRunner(bench.CommandRunner):
    def __init__(self):
        self.calls = []
        self.bad_route = None

    def run(self, args, **kwargs):
        self.calls.append([str(x) for x in args])
        if "models" in args:
            route = {"harness": args[3], "model": args[4]}
            if "--effort" in args:
                route["effort"] = args[args.index("--effort") + 1]
            result = route_proof(route)
            if self.bad_route:
                result["binding"].update(self.bad_route)
            return subprocess.CompletedProcess(args, 0, json.dumps(result), "")
        return super().run(args, **kwargs)


class BenchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        self.engine = self.root / "engine"
        self.target_sha = repository(self.target)
        self.engine_sha = repository(self.engine)
        self.style = self.root / "style.md"
        self.style.write_text("Every button uses the shared Button component.\n")
        self.config_path = self.root / "config.json"
        self.config = {
            "target": {"repo": str(self.target), "ref": "main"},
            "engine": {"source": str(self.engine)},
            "style": {"kind": "file", "source": str(self.style), "destination": ".subfloor/house-style.md",
                      "trap": {"forbidden": ["<button"], "required": ["Button"]}},
            "dev_kit": {"deps": ["true"], "test": ["true"]},
            "routes": [{"harness": "codex", "model": "test-model", "effort": "medium"}],
            "judge": {"harness": "claude", "model": "test-judge"},
            "cards": [{"id": "custom", "text": "Change {view}. Run the tests and commit on a branch.",
                       "slots": {"view": {"value": "existing_view", "kind": "ref"}},
                       "redlines": ["tests_green", "scope_bounded"], "allowed_paths": ["app.txt"]}],
        }
        self.runner = FreezeRunner()

    def freeze(self, config=None):
        self.config_path.write_text(json.dumps(config or self.config))
        return bench.freeze(self.config_path, self.runner)



class FreezeTests(BenchFixture):
    def test_deterministic_complete_freeze_and_no_target_mutation(self):
        before = git(self.target, "rev-parse", "HEAD")
        path = self.freeze()
        raw = path.read_bytes()
        self.assertEqual(self.freeze(), path)
        self.assertEqual(path.read_bytes(), raw)
        frozen = bench.load_campaign(path)
        config = frozen["config"]
        self.assertEqual(config["target"]["ref"], self.target_sha)
        self.assertEqual(config["engine"]["ref"], self.engine_sha)
        self.assertEqual(config["cards"][0]["digest"], bench.digest(config["cards"][0]["text"]))
        self.assertEqual(config["style"]["digest"], bench.digest(self.style.read_text()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        self.assertFalse(any("clone" in x for x in self.runner.calls))

    def test_unknown_keys_at_every_config_object(self):
        paths = [(), ("target",), ("engine",), ("style",), ("style", "trap"),
                 ("dev_kit",), ("routes", 0), ("judge",), ("cards", 0), ("cards", 0, "slots", "view")]
        for path in paths:
            with self.subTest(path=path):
                config = copy.deepcopy(self.config)
                item = config
                for name in path:
                    item = item[name]
                item["unexpected"] = True
                with self.assertRaisesRegex(bench.BenchError, "unknown keys"):
                    self.freeze(config)

    def test_root_overlap_symlinks_and_worktrees_fail_before_clone(self):
        linked = self.root / "linked"
        git(self.target, "worktree", "add", "--detach", str(linked))
        alias = self.root / "alias"
        alias.symlink_to(self.target, target_is_directory=True)
        for key, path in [("copy", self.target / "copies"), ("evidence", self.target / "runs"),
                          ("copy", self.root), ("copy", alias / "copies"),
                          ("copy", linked / "copies"), ("evidence", self.engine / "runs"),
                          ("copy", self.root / ".sc-worktrees" / "outside")]:
            with self.subTest(key=key, path=path):
                config = copy.deepcopy(self.config)
                config[key] = {"root": str(path)}
                with self.assertRaises(bench.BenchError):
                    self.freeze(config)
        config = copy.deepcopy(self.config)
        config["copy"] = {"root": str(self.root / "copies")}
        config["evidence"] = {"root": str(self.root / "copies" / "evidence")}
        with self.assertRaisesRegex(bench.BenchError, "overlap"):
            self.freeze(config)
        self.assertFalse(any("clone" in x for x in self.runner.calls))

    def test_unresolvable_refs_dirty_target_and_uncontained_engine(self):
        for section, ref in [("target", "absent"), ("engine", "f" * 40), ("engine", "main")]:
            with self.subTest(section=section):
                config = copy.deepcopy(self.config)
                config[section]["ref"] = ref
                with self.assertRaises(bench.BenchError):
                    self.freeze(config)
        (self.engine / "second").write_text("new")
        git(self.engine, "add", ".")
        git(self.engine, "commit", "-m", "not upstream")
        config = copy.deepcopy(self.config)
        config["engine"]["ref"] = git(self.engine, "rev-parse", "HEAD")
        with self.assertRaisesRegex(bench.BenchError, "outside"):
            self.freeze(config)
        (self.target / "dirty").write_text("dirty")
        with self.assertRaisesRegex(bench.BenchError, "dirty"):
            self.freeze()

    def test_route_judge_variant_fallback_and_effort_rejected(self):
        config = copy.deepcopy(self.config)
        config["judge"] = {**config["routes"][0], "effort": "high"}
        with self.assertRaisesRegex(bench.BenchError, "judge appears"):
            self.freeze(config)
        for mismatch in ({"provider_model": "fallback"}, {"native_variant_id": "medium"},
                         {"harness": "other"}, {"requested_model": "alias"}, {"effective_effort": "high"}):
            with self.subTest(mismatch=mismatch):
                self.runner.bad_route = mismatch
                with self.assertRaises(bench.BenchError):
                    self.freeze()

    def test_redline_prerequisites_and_menu(self):
        for redline, missing in [("style_trap", "trap"), ("endpoint_present", "serve"),
                                 ("endpoint_param", "serve"), ("endpoint_test_added", "serve"),
                                 ("ui_wired", "serve"), ("migration_present", "migrations"),
                                 ("migration_applies", "migrations"), ("other", "other")]:
            with self.subTest(redline=redline):
                config = copy.deepcopy(self.config)
                config["cards"][0]["redlines"] = [redline]
                if missing == "trap":
                    del config["style"]["trap"]
                with self.assertRaises(bench.BenchError):
                    self.freeze(config)

    def test_reserved_words_and_slot_validation(self):
        for value in ("benchmark", "EVALUATE", "rubric", "judge", "score"):
            with self.subTest(value=value):
                config = copy.deepcopy(self.config)
                config["cards"][0]["text"] = value
                config["cards"][0]["slots"] = {}
                with self.assertRaisesRegex(bench.BenchError, "reserved"):
                    self.freeze(config)
        for slot in ({"value": "missing", "kind": "ref"}, {"value": "existing_button", "kind": "new"},
                     {"value": "../escape", "kind": "new"}, {"value": "two\nlines", "kind": "text"},
                     {"value": "judge", "kind": "text"}, {"value": "existing_view"}):
            with self.subTest(slot=slot):
                config = copy.deepcopy(self.config)
                config["cards"][0]["slots"]["view"] = slot
                with self.assertRaises(bench.BenchError):
                    self.freeze(config)

    def test_presets_fix_kinds_and_new_slots_are_absent(self):
        config = copy.deepcopy(self.config)
        config["serve"] = {"argv": ["app"], "cwd": ".", "port_env": "PORT", "health_path": "/health", "ready_timeout_s": 10}
        config["migrations"] = {"path": "migrations", "apply_argv": ["apply"], "table_check_argv": ["table"]}
        for preset, data in bench.PRESETS.items():
            with self.subTest(preset=preset):
                slots = {name: {"kind": kind, "value": "existing_view" if kind == "ref" else "fresh_name" if kind == "new" else "a description"}
                         for name, kind in data["kinds"].items()}
                config["cards"] = [{"id": preset, "preset": preset, "slots": slots, "allowed_paths": ["**"]}]
                frozen = bench.load_campaign(self.freeze(config))
                self.assertNotIn("{", frozen["config"]["cards"][0]["text"])
                first = next(iter(slots))
                slots[first]["kind"] = "text" if slots[first]["kind"] != "text" else "ref"
                with self.assertRaisesRegex(bench.BenchError, "preset slot kind"):
                    self.freeze(config)

    def test_declaration_is_frozen_and_override_only_fills_missing_hooks(self):
        (self.target / ".subfloor").mkdir()
        kit = {"version": 1, "hooks": {"deps": {"argv": ["echo", "declared"], "cwd": "."}, "test": {"argv": ["true"]}}}
        (self.target / ".subfloor/dev-kit.json").write_text(json.dumps(kit))
        git(self.target, "add", ".")
        git(self.target, "commit", "-m", "dev kit")
        frozen = bench.load_campaign(self.freeze())
        self.assertEqual(frozen["config"]["dev_kit_declaration"], kit)
        config = copy.deepcopy(self.config)
        del config["dev_kit"]
        self.freeze(config)

    def test_file_url_target_has_the_same_local_containment_checks(self):
        config = copy.deepcopy(self.config)
        config["target"]["repo"] = self.target.as_uri()
        config["copy"] = {"root": str(self.target / "copies")}
        with self.assertRaisesRegex(bench.BenchError, "overlap"):
            self.freeze(config)
        config["copy"] = {"root": str(self.root / "copies")}
        config["evidence"] = {"root": str(self.root / "runs")}
        frozen = bench.load_campaign(self.freeze(config))
        self.assertEqual(frozen["config"]["target"]["ref"], self.target_sha)

    def test_frozen_inputs_do_not_advance_and_tampering_fails(self):
        path = self.freeze()
        frozen = bench.load_campaign(path)
        self.style.write_text("changed style")
        (self.target / "new").write_text("new target")
        git(self.target, "add", ".")
        git(self.target, "commit", "-m", "advance")
        self.assertEqual(bench.load_campaign(path), frozen)
        frozen["config"]["cards"][0]["text"] = "changed card"
        path.write_text(json.dumps(frozen))
        with self.assertRaisesRegex(bench.BenchError, "digest mismatch"):
            bench.load_campaign(path)

    def test_public_evidence_root_is_refused(self):
        evidence = self.root / "public"
        evidence.mkdir(mode=0o755)
        config = copy.deepcopy(self.config)
        config["evidence"] = {"root": str(evidence)}
        with self.assertRaisesRegex(bench.BenchError, "owner-only"):
            self.freeze(config)

    def test_all_subprocesses_strip_host_sc_and_git_context(self):
        with mock.patch.dict(os.environ, {"SC_API_TOKEN": "secret", "SC_API_URL": "http://host", "SC_EXTRA": "x", "GIT_DIR": "/wrong"}):
            out = bench.CommandRunner().run([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"])
            env = json.loads(out.stdout)
            self.assertFalse(any(k.startswith(("SC_", "GIT_")) for k in env))
            with self.assertRaisesRegex(bench.BenchError, "contamination"):
                bench.CommandRunner().run(["true"], env={"SC_INJECTED": "bad"})

    def test_engine_manifest_materializes_runner_for_forks(self):
        import engine_manifest
        import update
        path = ".super-coder/scripts/model_bench.py"
        self.assertTrue(any(path == p or path.startswith(p + "/") for p in engine_manifest.ENGINE_PATHS))
        # Exercise the serving update materializer without installing a runtime.
        (self.engine / ".super-coder/scripts").mkdir(parents=True)
        (self.engine / path).write_bytes((ROOT / path).read_bytes())
        git(self.engine, "add", ".")
        git(self.engine, "commit", "-m", "runner")
        destination = self.root / "fork"
        destination.mkdir()
        git(destination, "init", "-b", "main")
        git(destination, "fetch", str(self.engine), "main")
        with mock.patch.object(update, "REPO_ROOT", destination):
            update.materialize_engine("FETCH_HEAD", engine_paths=[".super-coder/scripts"])
        self.assertEqual((destination / path).read_bytes(), (ROOT / path).read_bytes())


STAGES = ["clone", "materialize", "install", "launch", "health", "verify", "route", "style", "deps", "test", "preamble"]


class FakeBackend:
    def __init__(self, fail_at=None, cleanup_failure=None):
        self.calls = []
        self.fail_at = fail_at
        self.cleanup_failure = cleanup_failure

    def stage(self, stage):
        self.calls.append(stage)
        if self.fail_at == stage:
            raise bench.BenchError("deliberate gate failure", "invalid" if stage in ("route", "test") else "infra_failed")

    def preflight(self, config):
        self.stage("preflight")

    def clone(self, config, workspace):
        workspace.mkdir(parents=True)
        self.stage("clone")

    def materialize(self, config, workspace):
        self.stage("materialize")

    def install(self, config, workspace):
        self.stage("install")
        return {"api_port": 11111, "dev_port": 11112}

    def launch(self, workspace):
        self.stage("launch")

    def health(self, workspace, ports):
        self.stage("health")

    def verify(self, workspace):
        self.stage("verify")

    def route(self, workspace, route):
        self.stage("route")
        return {"requested": route["model"], "observed": route["model"], "variant": None}

    def place_style(self, workspace, style):
        self.stage("style")

    def hook(self, config, workspace, name):
        self.stage(name)
        return {"exit_status": 0}

    def preamble(self, config, workspace, route):
        self.stage("preamble")
        return {"base_sha": config["target"]["ref"], "sc_environment_stripped": True, "git_status": ""}

    def stop(self, workspace, ledger):
        self.calls.append("stop")
        if self.cleanup_failure == "stop":
            raise bench.BenchError("deliberate stop failure")

    def remove(self, workspace):
        self.calls.append("remove")
        if self.cleanup_failure == "remove":
            raise OSError("deliberate delete failure")
        bench.HostBackend().remove(workspace)


class GateTests(BenchFixture):

    def test_success_gate_order_callback_preamble_and_teardown(self):
        path = self.freeze()
        backend = FakeBackend()
        observed = []

        def operation(workspace, config, card, route, ledger):
            backend.calls.append("operation")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(ledger["outcome"], "gate_passed")
            observed.append(card["text"])

        result = bench.Controller(backend).run(path, "custom", operation=operation)
        self.assertEqual(backend.calls, ["preflight", *STAGES, "operation", "stop", "remove"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(result["stages"], STAGES)
        self.assertEqual(result["route_proof"], {"requested": "test-model", "observed": "test-model", "variant": None})
        self.assertTrue(result["cleanup"]["services_stopped"])
        self.assertTrue(result["cleanup"]["copy_deleted"])
        self.assertTrue(result["cleanup"]["push_url_disabled"])
        self.assertFalse(Path(result["workspace"]).exists())
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        ledger = path.parent / "route-0-custom/gate.json"
        self.assertEqual(json.loads(ledger.read_text()), result)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

    def test_every_gate_stop_condition_prevents_operation_and_cleans_partial_copy(self):
        path = self.freeze()
        for failure in ["preflight", *STAGES]:
            with self.subTest(failure=failure):
                self.config["wall_cap_minutes"] = 60 + ["preflight", *STAGES].index(failure)
                path = self.freeze()
                backend = FakeBackend(fail_at=failure)
                callback = mock.Mock()
                with self.assertRaises(bench.BenchError):
                    bench.Controller(backend).run(path, "custom", cell_id=failure, operation=callback)
                callback.assert_not_called()
                if failure == "preflight":
                    self.assertEqual(backend.calls, ["preflight"])
                    self.assertFalse((path.parent / failure / "gate.json").exists())
                    continue
                self.assertEqual(backend.calls, ["preflight", *STAGES[:STAGES.index(failure) + 1], "stop", "remove"])
                result = json.loads((path.parent / failure / "gate.json").read_text())
                self.assertTrue(result["cleanup"]["copy_deleted"])
                self.assertEqual(result["outcome"], "invalid" if failure in ("route", "test") else "infra_failed")

    def test_cleanup_failure_fatal_retains_copy_and_blocks_next_cell_until_retry(self):
        path = self.freeze()
        for failure in ("stop", "remove"):
            with self.subTest(failure=failure):
                backend = FakeBackend(fail_at="health", cleanup_failure=failure)
                with self.assertRaises(bench.CleanupError):
                    bench.Controller(backend).run(path, "custom", cell_id=failure)
                ledger = json.loads((path.parent / failure / "gate.json").read_text())
                self.assertTrue(ledger["cleanup"]["fatal"])
                self.assertTrue(Path(ledger["workspace"]).exists())
                self.assertEqual(ledger["outcome"], "infra_failed")
                if failure == "stop":
                    self.assertNotIn("remove", backend.calls)
                next_backend = FakeBackend()
                with self.assertRaisesRegex(bench.BenchError, "prior teardown"):
                    bench.Controller(next_backend).run(path, "custom", cell_id="next")
                self.assertEqual(next_backend.calls, ["preflight"])
                repaired = bench.Controller(next_backend).cleanup(path, failure)
                self.assertTrue(repaired["cleanup"]["copy_deleted"])
                self.assertFalse(repaired["cleanup"]["fatal"])
                # Cleanup is idempotent and no longer needs model/source access.
                bench.Controller(next_backend).cleanup(path, failure)
                bench.Controller(next_backend).run(path, "custom", cell_id=f"after-{failure}")

    def test_operation_failure_also_cleans_without_losing_primary_failure(self):
        path = self.freeze()
        backend = FakeBackend()
        with self.assertRaisesRegex(RuntimeError, "operation broke"):
            bench.Controller(backend).run(path, "custom", operation=mock.Mock(side_effect=RuntimeError("operation broke")))
        self.assertEqual(backend.calls[-2:], ["stop", "remove"])
        ledger = json.loads((path.parent / "route-0-custom/gate.json").read_text())
        self.assertTrue(ledger["cleanup"]["copy_deleted"])

    def test_unowned_prior_copy_and_path_traversal_are_not_deleted(self):
        path = self.freeze()
        frozen = bench.load_campaign(path)
        workspace = bench.copy_path(frozen["config"], frozen["campaign_id"], "prior")
        workspace.mkdir(parents=True)
        valuable = workspace / "keep"
        valuable.write_text("unowned")
        backend = FakeBackend()
        with self.assertRaisesRegex(bench.BenchError, "unowned"):
            bench.Controller(backend).run(path, "custom", cell_id="prior")
        self.assertEqual(valuable.read_text(), "unowned")
        for cell in ("../target", "/target", "a/b"):
            with self.assertRaisesRegex(bench.BenchError, "cell id"):
                bench.Controller(backend).run(path, "custom", cell_id=cell)

    def test_recorded_cell_is_not_overwritten_and_repeats_use_new_id(self):
        path = self.freeze()
        controller = bench.Controller(FakeBackend())
        controller.run(path, "custom")
        ledger = path.parent / "route-0-custom/gate.json"
        before = ledger.read_bytes()
        with self.assertRaisesRegex(bench.BenchError, "already recorded"):
            controller.run(path, "custom")
        self.assertEqual(ledger.read_bytes(), before)
        controller.run(path, "custom", cell_id="repeat-2")

    def test_cleanup_works_after_source_removed_but_refuses_ledger_escape(self):
        path = self.freeze()
        controller = bench.Controller(FakeBackend(cleanup_failure="remove"))
        with self.assertRaises(bench.CleanupError):
            controller.run(path, "custom")
        self.engine.rename(self.root / "engine-unavailable")
        ledger_path = path.parent / "route-0-custom/gate.json"
        original = json.loads(ledger_path.read_text())
        poisoned = copy.deepcopy(original)
        poisoned["workspace"] = str(self.target)
        ledger_path.write_text(json.dumps(poisoned))
        with self.assertRaisesRegex(bench.BenchError, "identity mismatch"):
            bench.Controller(FakeBackend()).cleanup(path, "route-0-custom")
        ledger_path.write_text(json.dumps(original))
        bench.Controller(FakeBackend()).cleanup(path, "route-0-custom")
        self.assertTrue(self.target.exists())

    def test_campaign_lock_refuses_parallel_cells(self):
        path = self.freeze()
        controller = bench.Controller(FakeBackend())
        with controller._locked(bench.load_campaign(path)), self.assertRaisesRegex(bench.BenchError, "another campaign"):
            controller.run(path, "custom")


class HostBoundaryTests(BenchFixture):
    def test_real_clone_disables_push_and_pins_sha_even_after_target_advances(self):
        frozen = bench.load_campaign(self.freeze())
        (self.target / "advance").write_text("later")
        git(self.target, "add", ".")
        git(self.target, "commit", "-m", "later")
        workspace = self.root / "copy"
        backend = bench.HostBackend()
        backend.clone(frozen["config"], workspace)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.target_sha)
        self.assertEqual(git(workspace, "remote", "get-url", "--push", "origin"), bench.PUSH_DISABLED)
        pushed = subprocess.run(["git", "-C", str(workspace), "push", "origin", "HEAD:main"], capture_output=True, check=False)
        self.assertNotEqual(pushed.returncode, 0)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), git(self.target, "rev-parse", "main"))

    def test_real_engine_materialization_uses_its_manifest_and_removes_stale_state(self):
        scripts = self.engine / ".super-coder/scripts"
        scripts.mkdir(parents=True)
        (scripts / "engine_manifest.py").write_text("ENGINE_PATHS = ['sc', '.super-coder/scripts']\n")
        (scripts / "model_bench.py").write_text("pinned runner")
        (self.engine / "sc").write_text("#!/bin/sh\n")
        git(self.engine, "add", ".")
        git(self.engine, "commit", "-m", "engine")
        git(self.engine, "update-ref", "refs/remotes/origin/main", "HEAD")
        config = bench.load_campaign(self.freeze())["config"]
        workspace = self.root / "copy"
        backend = bench.HostBackend()
        backend.clone(config, workspace)
        (workspace / ".super-coder").mkdir()
        (workspace / ".super-coder/retired").write_text("retired")
        (workspace / ".sc-state").mkdir()
        (workspace / ".sc-state/engine.ref").write_text("stale")
        backend.materialize(config, workspace)
        self.assertEqual((workspace / ".super-coder/scripts/model_bench.py").read_text(), "pinned runner")
        self.assertFalse((workspace / ".super-coder/retired").exists())
        self.assertFalse((workspace / ".sc-state").exists())
        self.assertEqual(git(workspace, "rev-parse", "super-coder/main"), config["engine"]["ref"])
        self.assertEqual(git(workspace, "remote", "get-url", "--push", "super-coder"), bench.PUSH_DISABLED)

    def test_install_uses_host_detect_only_operator_and_exact_callable_pin(self):
        config = bench.load_campaign(self.freeze())["config"]
        workspace = self.root / "copy"
        workspace.mkdir()
        (workspace / ".super-coder").mkdir()
        (workspace / ".sc-state").mkdir()
        calls = []

        class InstallRunner(bench.CommandRunner):
            def run(self, args, **kwargs):
                calls.append((args, kwargs))
                self_env = bench.clean_environment(kwargs.get("env"))
                assert not any(k.startswith("SC_") for k in self_env)
                if ".super-coder/scripts/install.py" in args:
                    (workspace / ".super-coder/instance.json").write_text('{"runtime":"host"}')
                    (workspace / ".sc-state/engine.ref").write_text(config["engine"]["ref"])
                out = config["engine"]["ref"] if "engine-ref" in args else ""
                return subprocess.CompletedProcess(args, 0, out, "")

        backend = bench.HostBackend(InstallRunner())
        self.addCleanup(backend.release_ports)
        with mock.patch.dict(os.environ, {"SC_API_TOKEN": "secret", "SC_ADMIN": "1"}):
            ports = backend.install(config, workspace)
        install = next(args for args, _ in calls if ".super-coder/scripts/install.py" in args)
        self.assertIn("--skip-harness-install", install)
        self.assertEqual(install[install.index("--runtime") + 1], "host")
        self.assertIn("--username", install)
        self.assertNotIn("docker", " ".join(str(x) for args, _ in calls for x in args).lower())
        self.assertNotEqual(ports["api_port"], ports["dev_port"])
        for sock in backend.port_sockets:
            self.assertEqual(sock.getsockname()[0], "127.0.0.1")
        instance = json.loads((workspace / ".super-coder/instance.json").read_text())
        self.assertEqual(instance["port"], ports["api_port"])
        install_kwargs = next(kwargs for args, kwargs in calls if ".super-coder/scripts/install.py" in args)
        self.assertEqual(install_kwargs["env"]["XDG_STATE_HOME"], str(workspace / ".bench-state"))

    def test_style_uses_serving_parser_and_public_grant_for_all_kinds(self):
        from skill import parse_local_skill_spec
        frozen = bench.load_campaign(self.freeze())
        for kind in ("file", "doc", "skill"):
            with self.subTest(kind=kind):
                workspace = self.root / f"copy-{kind}"
                workspace.mkdir()
                style = copy.deepcopy(frozen["config"]["style"])
                style["kind"] = kind
                if kind == "skill":
                    style["content"] = "---\nname: project_style\ndescription: Follow the project house style.\n---\nUse the Button component.\n"
                    style["name"] = "project_style"
                backend = bench.HostBackend()
                with mock.patch.object(backend, "sc") as sc:
                    backend.place_style(workspace, style)
                parsed = parse_local_skill_spec((workspace / ".bench-style.md").read_text())
                self.assertEqual(parsed["name"], style["name"])
                self.assertIn(mock.call(workspace, "skill", "grant", style["name"], "DEV1"), sc.call_args_list)
                self.assertIn(mock.call(workspace, "render", "skills", "DEV1"), sc.call_args_list)
                if kind == "file":
                    self.assertEqual((workspace / style["destination"]).read_text(), style["content"])

    def test_style_symlink_escape_is_refused(self):
        config = bench.load_campaign(self.freeze())["config"]
        workspace = self.root / "copy"
        workspace.mkdir()
        (workspace / ".subfloor").symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(bench.BenchError, "symlink"):
            bench.HostBackend().place_style(workspace, config["style"])
        self.assertFalse((self.target / "house-style.md").exists())

    def test_hook_selects_declared_recipe_and_baseline_red_is_invalid(self):
        config = bench.load_campaign(self.freeze())["config"]
        backend = bench.HostBackend()
        workspace = self.root / "copy"
        config["dev_kit_declaration"] = {"hooks": {"test": {"argv": ["declared-test"]}}}
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(backend, "sc", return_value=ok) as sc, mock.patch.object(backend, "command", return_value=ok) as command:
            backend.hook(config, workspace, "test")
            sc.assert_called_once_with(workspace, "test", check=False, timeout=900)
            backend.hook(config, workspace, "deps")
            command.assert_called_once_with(workspace, ["true"], check=False, timeout=900)
        with mock.patch.object(backend, "sc", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(bench.BenchError) as failure:
                backend.hook(config, workspace, "test")
            self.assertEqual(failure.exception.outcome, "invalid")

    def test_cleanup_uses_lifecycle_and_refuses_bound_port(self):
        import socket
        workspace = self.root / "copy"
        backend = bench.HostBackend()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ledger = {"launch_attempted": True, "ports": {"api_port": sock.getsockname()[1]}}
            with mock.patch.object(backend, "sc") as sc:
                with self.assertRaises(bench.CleanupError):
                    backend.stop(workspace, ledger)
                sc.assert_called_once_with(workspace, "down", timeout=60)
        with mock.patch.object(backend, "sc"):
            backend.stop(workspace, ledger)

    def test_health_requires_own_identity_and_never_uses_host_proxy(self):
        workspace = self.root / "copy"
        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = "http://127.0.0.1:1234/api/health"
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(bench, "build_opener", return_value=opener), mock.patch.object(bench.json, "load", return_value={"ok": True, "repo_root": str(self.target), "port": 1234}), self.assertRaisesRegex(bench.BenchError, "another instance"):
            bench.HostBackend().health(workspace, {"api_port": 1234})
        with mock.patch.object(bench, "build_opener", return_value=opener), mock.patch.object(bench.json, "load", return_value={"ok": True, "repo_root": str(workspace), "port": 1234}):
            bench.HostBackend().health(workspace, {"api_port": 1234})


class RegressionTests(BenchFixture):
    def test_installer_source_resolution_ignores_the_disabled_origin(self):
        import install
        workspace = self.root / "copy"
        bench.HostBackend().clone(bench.load_campaign(self.freeze())["config"], workspace)
        git(workspace, "remote", "add", "super-coder", self.engine.as_uri())
        git(workspace, "fetch", "super-coder", "main:refs/remotes/super-coder/main")
        git(workspace, "remote", "set-url", "origin", "disabled://bench-target")
        with mock.patch.object(install, "REPO_ROOT", workspace):
            self.assertEqual(install.sc_remote(), "super-coder")
            self.assertEqual(install.resolve_engine_ref(), self.engine_sha)

    def test_baseline_red_invalidates_campaign_after_successful_cleanup(self):
        path = self.freeze()
        with self.assertRaises(bench.BenchError):
            bench.Controller(FakeBackend(fail_at="test")).run(path, "custom")
        with self.assertRaisesRegex(bench.BenchError, "campaign baseline is invalid"):
            bench.Controller(FakeBackend()).run(path, "custom", cell_id="another-route")

    def test_remote_snapshot_is_pinned_and_scratch_removed_on_resolution_failure(self):
        runner = bench.CommandRunner()
        with bench.Snapshot(runner, "remote", (self.target.as_uri(), "main")) as snapshot:
            scratch = snapshot.repo
            self.assertEqual(snapshot.sha, self.target_sha)
            self.assertTrue(snapshot.contains("existing_view"))
        self.assertFalse(scratch.exists())
        snapshot = bench.Snapshot(runner, "remote", (self.target.as_uri(), "main"))
        with mock.patch.object(bench, "resolve_sha", side_effect=bench.BenchError("bad ref")), self.assertRaises(bench.BenchError):
            snapshot.__enter__()
        self.assertFalse(snapshot.repo.exists())

    def test_serve_migrations_roots_and_file_destination_reject_unknown_or_escaping_fields(self):
        variants = [
            {"copy": {"root": str(self.root / "copies"), "extra": True}},
            {"evidence": {"root": str(self.root / "runs"), "extra": True}},
            {"serve": {"argv": ["app"], "cwd": ".", "port_env": "PORT", "health_path": "/health", "ready_timeout_s": 10, "extra": True}},
            {"migrations": {"path": "migrations", "apply_argv": ["true"], "table_check_argv": ["true"], "extra": True}},
            {"serve": {"argv": ["app"], "cwd": "../target", "port_env": "PORT", "health_path": "/health", "ready_timeout_s": 10}},
            {"serve": {"argv": ["app"], "cwd": ".", "port_env": "SC_API_TOKEN", "health_path": "/health", "ready_timeout_s": 10}},
            {"migrations": {"path": "../target", "apply_argv": ["true"], "table_check_argv": ["true"]}},
            {"style": {**self.config["style"], "destination": "../target/style.md"}},
            {"style": {**self.config["style"], "trap": {"required": ["["], "forbidden": ["Button"]}}},
        ]
        for overrides in variants:
            with self.subTest(overrides=overrides), self.assertRaises(bench.BenchError):
                self.freeze({**copy.deepcopy(self.config), **overrides})

    def test_cleanup_refuses_copy_replaced_by_symlink(self):
        path = self.freeze()
        with self.assertRaises(bench.CleanupError):
            bench.Controller(FakeBackend(cleanup_failure="remove")).run(path, "custom")
        ledger_path = path.parent / "route-0-custom/gate.json"
        workspace = Path(json.loads(ledger_path.read_text())["workspace"])
        workspace.rmdir()
        workspace.symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(bench.BenchError, "symlink"):
            bench.Controller(FakeBackend()).cleanup(path, "route-0-custom")
        self.assertTrue(self.target.is_dir())

    def test_runner_subprocess_timeout_never_prints_secret_stderr(self):
        runner = bench.CommandRunner()
        with self.assertRaises(bench.BenchError) as failure:
            runner.run([sys.executable, "-c", "import sys; sys.stderr.write('seeded-secret'); sys.exit(1)"])
        self.assertNotIn("seeded-secret", str(failure.exception))
        with mock.patch.object(bench.subprocess, "run", side_effect=subprocess.TimeoutExpired(["tool"], 1)), self.assertRaises(bench.BenchError) as failure:
            runner.run(["tool"])
        self.assertEqual(failure.exception.outcome, "infra_failed")

    def test_cli_help_and_invalid_config_do_not_launch(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "model_bench.py"), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        for verb in ("freeze", "run", "cleanup"):
            self.assertIn(verb, result.stdout)
        self.config_path.write_text('{"bad": true}')
        result = subprocess.run([sys.executable, str(SCRIPTS / "model_bench.py"), "freeze", "--config", str(self.config_path)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown keys", result.stderr)

    def test_readonly_db_gate_executes_integrity_and_flavor_checks_under_optimization(self):
        import sqlite3
        workspace = self.root / "copy"
        scripts = workspace / ".super-coder/scripts"
        scripts.mkdir(parents=True)
        state = workspace / ".bench-state"
        state.mkdir()
        database = state / "fixture.db"
        (scripts / "instance_state.py").write_text(
            "from pathlib import Path\ndef active_database_path(engine):\n    return Path('.bench-state/fixture.db').resolve()\n"
        )
        con = sqlite3.connect(database)
        con.execute("CREATE TABLE shells(shortname TEXT, flavor TEXT, is_deleted INTEGER)")
        con.execute("INSERT INTO shells VALUES ('DEV1', 'dev', 0)")
        con.commit()
        backend = bench.HostBackend()
        with mock.patch.object(backend, "sc") as sc, mock.patch.dict(os.environ, {"PYTHONOPTIMIZE": "1"}):
            backend.verify(workspace)
            sc.assert_called_once_with(workspace, "render", "all", "DEV1")
            con.execute("UPDATE shells SET flavor='reviewer'")
            con.commit()
            with self.assertRaises(bench.BenchError):
                backend.verify(workspace)
        con.close()

    def test_host_lock_serializes_different_evidence_roots(self):
        first = self.freeze()
        config = copy.deepcopy(self.config)
        config["evidence"] = {"root": str(self.root / "other-runs")}
        second = self.freeze(config)
        controller = bench.Controller(FakeBackend())
        with controller._locked(bench.load_campaign(first)), self.assertRaisesRegex(bench.BenchError, "another campaign"):
            controller.run(second, "custom")

    def test_cleanup_after_runner_update_still_recovers_retained_copy(self):
        path = self.freeze()
        with self.assertRaises(bench.CleanupError):
            bench.Controller(FakeBackend(cleanup_failure="remove")).run(path, "custom")
        with mock.patch.object(bench, "RUNNER_VERSION", "next-version"):
            with self.assertRaisesRegex(bench.BenchError, "runner changed"):
                bench.Controller(FakeBackend()).run(path, "custom", cell_id="new")
            result = bench.Controller(FakeBackend()).cleanup(path, "route-0-custom")
            self.assertTrue(result["cleanup"]["copy_deleted"])

    def test_style_document_and_skill_ids_freeze_through_public_read_surfaces(self):
        config = copy.deepcopy(self.config)
        original_run = self.runner.run

        def read_artifact(args, **kwargs):
            if "mem" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"document": {"body": "Use the Button component."}}), "")
            if "sql" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps([{"name": "project_style", "description": "Project house style", "content": "Use the Button component."}]), "")
            return original_run(args, **kwargs)

        for kind in ("doc", "skill"):
            with self.subTest(kind=kind), mock.patch.object(self.runner, "run", side_effect=read_artifact):
                config["style"] = {"kind": kind, "source": 42}
                frozen = bench.load_campaign(self.freeze(config))
                self.assertIn("Use the Button component.", frozen["config"]["style"]["content"])
                self.assertEqual(frozen["config"]["style"]["source"], 42)


if __name__ == "__main__":
    unittest.main()
