"""Role-scoped boot inventory coverage for the fork development contract."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "render"))
import compose
import run
import seed_skills


class DevToolsBootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.checkout = Path(self.tmp.name).resolve()
        self.subfloor = self.checkout / ".subfloor"
        self.subfloor.mkdir()
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "SC_DEV_PORT": "8123",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_declaration(self, value: dict, *, executable: bool = True) -> None:
        script = self.subfloor / "dev-kit"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755 if executable else 0o644)
        (self.subfloor / "dev-kit.json").write_text(json.dumps(value))

    def status_path(self) -> Path:
        identity = hashlib.sha256(str(self.checkout).encode()).hexdigest()[:20]
        path = (
            self.checkout
            / ".sc-state"
            / "local"
            / "dev-kit"
            / identity
            / "status.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def receipt(self, **state: str) -> dict:
        declaration = run.devkit.load_declaration(self.checkout)
        assert declaration is not None
        return {
            "checkout_identity": hashlib.sha256(
                str(self.checkout).encode()
            ).hexdigest(),
            "declaration_digest": hashlib.sha256(
                declaration.canonical_json.encode()
            ).hexdigest(),
            "package_digest": "none",
            "engine_ref": run.sandbox_devkit._engine_ref(self.checkout, ENGINE),
            **state,
        }

    @staticmethod
    def declaration(*, provision: bool = False) -> dict:
        value = {
            "version": 1,
            "hooks": {
                "test": {"argv": ["./.subfloor/dev-kit", "test"], "cwd": "."}
            },
        }
        if provision:
            value["provision"] = {"hook": "test", "inputs": []}
        return value

    def test_absent_and_invalid_are_distinct_without_invented_hooks(self) -> None:
        absent = run.collect_dev_tools(
            self.checkout, "host", environment=self.environment
        )
        self.assertEqual(absent["state"], "absent")
        self.assertEqual(absent["hooks"], {})
        self.assertEqual(absent["app_database"], "unavailable")

        self.write_declaration({"version": 2})
        invalid = run.collect_dev_tools(
            self.checkout, "host", environment=self.environment
        )
        self.assertEqual(invalid["state"], "invalid")
        self.assertIn("$.version", invalid["detail"])
        self.assertEqual(invalid["hooks"], {})

    def test_host_ready_reports_only_declared_hook_and_withholds_database_url(self) -> None:
        self.write_declaration(self.declaration())
        environment = {
            **self.environment,
            "DATABASE_URL": "postgres://secret.example/app",
        }
        inventory = run.collect_dev_tools(
            self.checkout, "host", environment=environment
        )
        self.assertEqual(inventory["state"], "ready")
        self.assertEqual(
            inventory["hooks"]["test"],
            {
                "state": "configured",
                "cwd": ".",
                "executable": "./.subfloor/dev-kit",
            },
        )
        rendered = compose.render_dev_tools("dev", inventory)
        self.assertIn("`sc test` — configured", rendered)
        self.assertIn("`sc lint` — unavailable (not declared)", rendered)
        self.assertIn("127.0.0.1:8123", rendered)
        self.assertIn("configured (URL withheld)", rendered)
        self.assertNotIn("secret.example", rendered)

        empty_database = run.collect_dev_tools(
            self.checkout,
            "host",
            environment={**self.environment, "DATABASE_URL": ""},
        )
        self.assertEqual(empty_database["app_database"], "configured (URL withheld)")

    def test_missing_executable_is_declared_but_not_ready(self) -> None:
        self.write_declaration(self.declaration(), executable=False)
        inventory = run.collect_dev_tools(
            self.checkout, "host", environment=self.environment
        )
        self.assertEqual(inventory["state"], "declared")
        self.assertEqual(inventory["hooks"]["test"]["state"], "unavailable")

    def test_container_receipt_states_are_evidence_backed(self) -> None:
        self.write_declaration(self.declaration(provision=True))
        stale = run.collect_dev_tools(
            self.checkout, "container", environment=self.environment
        )
        self.assertEqual(stale["state"], "stale")

        self.status_path().write_text(
            json.dumps(self.receipt(fork_readiness="ready"))
        )
        ready = run.collect_dev_tools(
            self.checkout, "container", environment=self.environment
        )
        self.assertEqual(ready["state"], "ready")
        self.assertIn("0.0.0.0:8123 -> 127.0.0.1:8123", ready["dev_port"])

        self.status_path().write_text(
            json.dumps(self.receipt(native_packages="advisory"))
        )
        advisory = run.collect_dev_tools(
            self.checkout, "container", environment=self.environment
        )
        self.assertEqual(advisory["state"], "advisory")

        self.status_path().write_text("not-json")
        failed = run.collect_dev_tools(
            self.checkout, "container", environment=self.environment
        )
        self.assertEqual(failed["state"], "failed")

    def test_mismatched_receipt_never_claims_ready(self) -> None:
        self.write_declaration(self.declaration(provision=True))
        receipt = self.receipt(fork_readiness="ready")
        receipt["declaration_digest"] = "stale"
        self.status_path().write_text(json.dumps(receipt))
        inventory = run.collect_dev_tools(
            self.checkout, "container", environment=self.environment
        )
        self.assertEqual(inventory["state"], "stale")

    def test_repair_overrides_declaration_and_makes_no_readiness_claim(self) -> None:
        self.write_declaration(self.declaration())
        repair = run.collect_dev_tools(
            self.checkout,
            "container",
            repair=True,
            environment=self.environment,
        )
        self.assertEqual(repair["state"], "repair")
        self.assertEqual(repair["hooks"], {})
        self.assertIn("no readiness claim", repair["declaration"])

    def test_boot_and_planner_skill_share_hook_and_state_vocabulary(self) -> None:
        skill = seed_skills.parse_skill(
            ENGINE / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md"
        )["content"]
        for state in compose.DEV_TOOL_STATES:
            self.assertIn(f"`{state}`", skill)
        for hook in compose.DEV_TOOL_HOOKS:
            self.assertIn(f"`{hook}`", skill)


if __name__ == "__main__":
    unittest.main()
