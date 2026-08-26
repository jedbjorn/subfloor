"""Atomic ownership and route-agent tests for project OpenCode config."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opencode_config


def binding(*, effort: str = "high") -> tuple[dict, str]:
    value = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "opencode",
        "requested_model": "openai/gpt-test",
        "provider_model": "gpt-test",
        "requested_effort": effort,
        "effective_effort": effort,
        "native_variant_id": effort,
        "transport": "opencode-route-agent",
        "catalogue_generation": "1" * 32,
        "evidence_digest": "2" * 64,
        "selector_binding": {"kind": "connected-model"},
        "adapter_metadata": {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {"reasoningEffort": effort},
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return value, digest


def live_binding(model: str, option_id: str) -> tuple[dict, str]:
    value = {
        "contract_version": 3,
        "control_state": "controlled",
        "harness": "opencode",
        "requested_model": model,
        "provider_model": model.split("/", 1)[1],
        "requested_effort": option_id,
        "effective_effort": option_id,
        "native_variant_id": None,
        "native_option_id": option_id,
        "transport": "opencode-route-agent",
        "catalogue_generation": None,
        "evidence_digest": None,
        "selector_binding": {"kind": "harness-live", "selector": model},
        "adapter_metadata": {},
    }
    digest = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return value, digest


class OpenCodeConfigOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "opencode.json"

    def test_template_preserves_agents_and_shell_and_replaces_atomically(self) -> None:
        self.config.write_text(json.dumps({
            "agent": {"existing": {"mode": "primary"}},
            "shell": "/old/wrapper",
            "permission": {"bash": "ask"},
            "forkOwned": True,
        }))

        result = opencode_config.emit_template(self.root, {
            "permission": {"bash": "allow"},
            "plugin": ["engine-plugin"],
        })

        self.assertEqual(result["agent"], {"existing": {"mode": "primary"}})
        self.assertEqual(result["shell"], "/old/wrapper")
        self.assertEqual(result["permission"], {"bash": "allow"})
        self.assertEqual(result["plugin"], ["engine-plugin"])
        self.assertTrue(result["forkOwned"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        lock = self.root / ".sc-state/local/runtime/opencode-config.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_invalid_json_or_failed_merge_leaves_old_bytes_untouched(self) -> None:
        self.config.write_text("{invalid")
        old = self.config.read_bytes()
        with self.assertRaises(opencode_config.OpenCodeConfigError) as invalid:
            opencode_config.merge_json(
                self.root, {"mcp": {}}, operation="managed-mcp"
            )
        self.assertEqual(invalid.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

        self.config.write_text('{"stable":true}\n')
        old = self.config.read_bytes()
        with self.assertRaises(opencode_config.OpenCodeConfigError) as failed:
            opencode_config.mutate(
                self.root,
                "forced-failure",
                lambda _config: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        self.assertEqual(failed.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

    def test_non_object_config_is_refused_without_mutation(self) -> None:
        self.config.write_text('["not-an-object"]\n')
        old = self.config.read_bytes()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as invalid:
            opencode_config.merge_json(
                self.root, {"mcp": {}}, operation="non-object-root"
            )

        self.assertEqual(invalid.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

    def test_replace_failure_leaves_old_bytes_untouched(self) -> None:
        self.config.write_text('{"stable":true}\n')
        old = self.config.read_bytes()

        with (
            mock.patch.object(
                opencode_config.os,
                "replace",
                side_effect=OSError("forced replace failure"),
            ),
            self.assertRaises(opencode_config.OpenCodeConfigError) as failed,
        ):
            opencode_config.merge_json(
                self.root, {"new": True}, operation="replace-failure"
            )

        self.assertEqual(failed.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

    def test_non_json_merge_leaves_old_bytes_untouched(self) -> None:
        self.config.write_text('{"stable":true}\n')
        old = self.config.read_bytes()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as failed:
            opencode_config.merge_json(
                self.root, {"invalid": object()}, operation="non-json-value"
            )

        self.assertEqual(failed.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

    def test_route_agent_uses_full_digest_and_refuses_stale_content(self) -> None:
        value, digest = binding()
        name = opencode_config.ensure_route_agent(self.root, value, digest)
        configured = json.loads(self.config.read_text())

        self.assertEqual(name, f"sc-route-{digest}")
        self.assertEqual(len(name), len("sc-route-") + 64)
        self.assertEqual(configured["agent"][name], {
            "mode": "primary",
            "model": "openai/gpt-test",
            "reasoningEffort": "high",
        })
        configured["agent"][name]["reasoningEffort"] = "low"
        self.config.write_text(json.dumps(configured))
        old = self.config.read_bytes()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as stale:
            opencode_config.ensure_route_agent(self.root, value, digest)

        self.assertEqual(stale.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)

    def test_route_agent_revalidates_stored_overlay_before_config_mutation(self) -> None:
        value, _digest = binding()
        value["adapter_metadata"]["variant_options"] = {
            "reasoningEffort": "high",
            "shell": "/tmp/untrusted",
        }
        digest = hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as invalid:
            opencode_config.ensure_route_agent(self.root, value, digest)

        self.assertEqual(invalid.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertFalse(self.config.exists())

    def test_full_digest_names_do_not_collide_at_old_24_hex_prefix(self) -> None:
        prefix = "a" * 24
        first = opencode_config.route_agent_name(prefix + "0" * 40)
        second = opencode_config.route_agent_name(prefix + "f" * 40)
        self.assertNotEqual(first, second)
        self.assertEqual(first, "sc-route-" + prefix + "0" * 40)
        self.assertEqual(second, "sc-route-" + prefix + "f" * 40)

    def test_forced_writer_interleaving_keeps_both_merges(self) -> None:
        self.config.write_text('{"base":true}\n')
        first_holds_lock = threading.Event()
        release_first = threading.Event()
        errors: list[Exception] = []

        def first_merge(config: dict) -> None:
            config["first"] = {"value": 1}
            first_holds_lock.set()
            if not release_first.wait(timeout=2):
                raise RuntimeError("test did not release first writer")

        def run_first() -> None:
            try:
                opencode_config.mutate(self.root, "first", first_merge)
            except (opencode_config.OpenCodeConfigError, RuntimeError) as exc:
                errors.append(exc)

        def run_second() -> None:
            try:
                opencode_config.merge_json(
                    self.root, {"second": {"value": 2}}, operation="second"
                )
            except (opencode_config.OpenCodeConfigError, RuntimeError) as exc:
                errors.append(exc)

        first = threading.Thread(target=run_first)
        second = threading.Thread(target=run_second)
        first.start()
        self.assertTrue(first_holds_lock.wait(timeout=2))
        second.start()
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(self.config.read_text()), {
            "base": True,
            "first": {"value": 1},
            "second": {"value": 2},
        })

    def test_concurrent_live_routes_keep_independent_exact_payloads(self) -> None:
        routes = (
            (
                *live_binding("ollama-cloud/glm-5.2", "max"),
                {
                    "reasoningEffort": "max",
                    "futureProviderField": {"route": "glm"},
                },
            ),
            (
                *live_binding("ollama-cloud/gemma4:31b", "MAX.Future"),
                {
                    "reasoningEffort": "provider-exact",
                    "futureProviderField": {"route": "gemma"},
                },
            ),
        )
        start = threading.Barrier(3)
        errors: list[Exception] = []

        def write(value: dict, digest: str, payload: dict) -> None:
            try:
                start.wait(timeout=2)
                opencode_config.ensure_live_route_agent(
                    self.root, value, digest, payload
                )
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        workers = [
            threading.Thread(target=write, args=route)
            for route in routes
        ]
        for worker in workers:
            worker.start()
        start.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        agents = json.loads(self.config.read_text())["agent"]
        self.assertEqual(len(agents), 2)
        for value, digest, payload in routes:
            self.assertEqual(
                agents[opencode_config.route_agent_name(digest)],
                {**payload, "mode": "primary", "model": value["requested_model"]},
            )

    def test_lock_timeout_refuses_without_mutation(self) -> None:
        self.config.write_text('{"stable":true}\n')
        runtime = self.root / ".sc-state/local/runtime"
        runtime.mkdir(parents=True)
        lock = runtime / "opencode-config.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.addCleanup(fcntl.flock, descriptor, fcntl.LOCK_UN)
        old = self.config.read_bytes()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as busy:
            opencode_config.merge_json(
                self.root, {"lost": True}, operation="busy", timeout=0.01
            )
        self.assertEqual(busy.exception.code, "HARNESS_CONFIG_BUSY")
        self.assertEqual(self.config.read_bytes(), old)

    def test_symlink_lock_is_refused_without_mutation(self) -> None:
        self.config.write_text('{"stable":true}\n')
        runtime = self.root / ".sc-state/local/runtime"
        runtime.mkdir(parents=True)
        target = self.root / "outside-lock"
        target.write_text("")
        (runtime / "opencode-config.lock").symlink_to(target)
        old = self.config.read_bytes()

        with self.assertRaises(opencode_config.OpenCodeConfigError) as invalid:
            opencode_config.merge_json(
                self.root, {"lost": True}, operation="symlink-lock"
            )

        self.assertEqual(invalid.exception.code, "HARNESS_CONFIG_INVALID")
        self.assertEqual(self.config.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
