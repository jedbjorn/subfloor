#!/usr/bin/env python3
"""Tests for the model-catalog service (api/model_catalog.py).

The catalog is layered and best-effort: models.dev (keyless, all five
harnesses) → provider APIs (only with env keys) → OpenCode's connected-provider
    projection → cache → static floor. Payload v6 retains family metadata
(newest-first; claude families with a CLI alias resolve `latest` to the
alias), the flat `models` list for sub-version search, and fork-local harness
and configured-route verification. These tests pin
that contract: harness→provider mapping and opencode prefixing, family
grouping/alias resolution, opportunistic merges that never fail the sweep,
stale-cache-on-failure, version-mismatched caches ignored, and the floor
when everything is down. All sources are injected — no network.

Run:
    python3 tests/test_model_catalog.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import model_catalog as mc  # noqa: E402
import models as routes_cli  # noqa: E402

MODELS_DEV = {
    "anthropic": {"models": {
        "claude-opus-4-8": {"name": "Claude Opus 4.8",
                            "release_date": "2026-04-01", "family": "claude-opus"},
        "claude-opus-4-7": {"name": "Claude Opus 4.7",
                            "release_date": "2026-02-01", "family": "claude-opus"},
        "claude-fable-5": {"name": "Claude Fable 5",
                           "release_date": "2026-06-09", "family": "claude-fable"},
    }},
    "openai": {"models": {"gpt-5.5": {"release_date": "2026-01-01", "family": "gpt"}}},
    "mistral": {"models": {"devstral-latest": {"release_date": "2025-12-01",
                                               "family": "devstral"}}},
    "ollama-cloud": {"models": {"deepseek-v4-pro": {"release_date": "2026-02-02",
                                                    "family": "deepseek"}}},
    "kimi-for-coding": {"models": {
        "k3": {"name": "Kimi K3", "release_date": "2026-07-16", "family": "kimi-k3"},
        "kimi-for-coding": {"name": "Kimi K2.7 Code",
                            "release_date": "2026-06-12", "family": "kimi-k2"},
    }},
}


def fetch_ok(url, headers=None):
    if url == mc.MODELS_DEV_URL:
        return MODELS_DEV
    if "openai" in url:
        return {"data": [{"id": "gpt-9-preview"}, {"id": "gpt-5.5"}]}
    raise RuntimeError(f"unexpected fetch: {url}")


def fetch_down(url, headers=None):
    raise OSError("network down")


def ids(harness_block):
    return [e["id"] for e in harness_block["models"]]


class NoCLI(unittest.TestCase):
    """Base: opencode binary absent unless a test opts in."""

    def setUp(self):
        p = mock.patch.object(mc.shutil, "which", return_value=None)
        p.start()
        self.addCleanup(p.stop)


class BuildTest(NoCLI):
    def test_harness_mapping_and_prefixing(self):
        got = mc.build(fetch=fetch_ok, env={}, run=None)
        self.assertEqual(got["v"], mc.PAYLOAD_VERSION)
        self.assertIn("claude-fable-5", ids(got["harnesses"]["claude"]))
        self.assertEqual(ids(got["harnesses"]["codex"]), ["gpt-5.5"])
        self.assertEqual(ids(got["harnesses"]["opencode"]),
                         ["ollama-cloud/deepseek-v4-pro"])
        # kimi maps to the kimi-for-coding provider; ids stay bare (not
        # provider-prefixed like opencode) — the form the CLI reports.
        self.assertEqual(ids(got["harnesses"]["kimi"]), ["k3", "kimi-for-coding"])
        self.assertEqual(got["sources"], ["models.dev"])

    def test_families_newest_first_alias_latest(self):
        fams = mc.build(fetch=fetch_ok, env={}, run=None)["harnesses"]["claude"]["families"]
        self.assertEqual([f["family"] for f in fams], ["fable", "opus"],
                         "families sort by newest release")
        by = {f["family"]: f for f in fams}
        self.assertEqual(by["fable"]["latest"], "fable",
                         "aliased family → the self-tracking alias")
        self.assertEqual(by["opus"]["latest"], "opus",
                         "aliased family → the self-tracking alias")
        self.assertEqual(by["opus"]["n"], 2)

    def test_opencode_family_latest_is_prefixed(self):
        fams = mc.build(fetch=fetch_ok, env={}, run=None)["harnesses"]["opencode"]["families"]
        self.assertEqual(fams[0]["family"], "deepseek")
        self.assertEqual(fams[0]["latest"], "ollama-cloud/deepseek-v4-pro")

    def test_provider_api_merges_and_dedupes(self):
        got = mc.build(fetch=fetch_ok, env={"OPENAI_API_KEY": "k"}, run=None)
        self.assertEqual(ids(got["harnesses"]["codex"]), ["gpt-5.5", "gpt-9-preview"],
                         "keyed-API ids append deduped, models.dev order kept")
        self.assertIn("openai-api", got["sources"])

    def test_bad_provider_key_never_fails_the_sweep(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            raise OSError("401")
        got = mc.build(fetch=fetch, env={"OPENAI_API_KEY": "bad"}, run=None)
        self.assertEqual(got["sources"], ["models.dev"])

    def test_opencode_live_overlay_keeps_only_connected_provider_models(self):
        with mock.patch.object(mc.shutil, "which", return_value="/usr/bin/opencode"):
            got = mc._with_live_opencode(
                mc.build(fetch=fetch_ok, env={}, run=None),
                lambda: [
                    {
                        "id": "openai/gpt-connected",
                        "provider": "openai",
                        "provider_model": "gpt-connected",
                        "name": "GPT Connected",
                        "family": "gpt",
                        "release_date": "2026-07-01",
                    },
                    {
                        "id": "ollama-cloud/glm-connected",
                        "provider": "ollama-cloud",
                        "provider_model": "glm-connected",
                        "name": "GLM Connected",
                        "family": "glm",
                        "release_date": "2026-06-01",
                    },
                ],
            )
        self.assertEqual(ids(got["harnesses"]["opencode"]),
                         ["openai/gpt-connected",
                          "ollama-cloud/glm-connected"])
        self.assertTrue(all(
            model["availability"] == "available"
            for model in got["harnesses"]["opencode"]["models"]
        ))
        self.assertIn("opencode-provider-api", got["sources"])

    def test_opencode_without_local_runtime_exposes_no_available_routes(self):
        got = mc._with_live_opencode(
            mc.build(fetch=fetch_ok, env={}, run=None),
            lambda: self.fail("provider API must not run without opencode"),
        )
        self.assertEqual(ids(got["harnesses"]["opencode"]), [])

    def test_all_sources_down_raises(self):
        with self.assertRaises(RuntimeError):
            mc.build(fetch=fetch_down, env={}, run=None)


class LocalRouteDiscoveryTest(unittest.TestCase):
    def test_codex_cache_is_locally_available_with_efforts(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(json.dumps({"models": [{
                "slug": "gpt-local", "display_name": "GPT Local",
                "visibility": "list", "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "medium"}, {"effort": "high"}],
            }]}))
            run = mock.Mock(return_value=mock.Mock(
                returncode=0, stdout="codex-cli 9.9\n", stderr=""))
            with mock.patch.object(
                    mc.shutil, "which",
                    side_effect=lambda name: "/bin/codex" if name == "codex" else None):
                got = mc.build(fetch=fetch_down, env={"CODEX_HOME": tmp}, run=run)
        model = got["harnesses"]["codex"]["models"][0]
        self.assertEqual(model["id"], "gpt-local")
        self.assertEqual(model["availability"], "available")
        self.assertEqual(model["source"], "codex-cache")
        self.assertIn("high", model["supported_efforts"])

    @unittest.skipUnless(mc.toml_compat.AVAILABLE, "stdlib TOML parser unavailable")
    def test_kimi_config_selector_is_alias_not_provider_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.toml").write_text(
                'default_model = "kimi-code/k3"\n'
                '[models."kimi-code/k3"]\n'
                'provider = "managed:kimi-code"\nmodel = "k3"\n'
                'display_name = "K3"\nsupport_efforts = ["low", "high"]\n'
                'default_effort = "high"\n')
            run = mock.Mock(return_value=mock.Mock(
                returncode=0, stdout="0.27.0\n", stderr=""))
            with mock.patch.object(
                    mc.shutil, "which",
                    side_effect=lambda name: "/bin/kimi" if name == "kimi" else None):
                got = mc.build(fetch=fetch_down, env={"KIMI_CODE_HOME": tmp}, run=run)
        model = got["harnesses"]["kimi"]["models"][0]
        self.assertEqual(model["id"], "kimi-code/k3")
        self.assertEqual(model["provider_model"], "k3")
        self.assertEqual(model["availability"], "available")


class RoutePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        migration = ROOT / ".super-coder" / "migrations" / "0075_model_routes.sql"
        self.con.executescript(migration.read_text())
        self.con.executescript(
            "CREATE TABLE sprints (sprint_id INTEGER PRIMARY KEY,lifecycle TEXT);"
            "CREATE TABLE sprint_participants ("
            "participant_id INTEGER PRIMARY KEY,sprint_id INTEGER);"
        )
        self.con.executescript((
            ROOT / ".super-coder" / "migrations" /
            "0212_route_binding_foundation.sql"
        ).read_text())

    def tearDown(self):
        self.con.close()

    @staticmethod
    def verification(harness: str, version: str) -> dict:
        return {"verification": {"runtime": "host", "harnesses": {
            harness: {
                "version": version, "compatibility": "verified",
                "minimum_version": version,
                "maximum_version_exclusive": "99.0.0",
                "verified_version": version, "error": None,
            }
        }}}

    def test_persist_marks_exact_high_effort_route_runnable(self):
        payload = {
            "fetched_at": "2026-07-21T00:00:00+00:00", "stale": False,
            "harnesses": {"kimi": {"models": [mc._entry(
                "kimi-code/k3", source="kimi-config", availability="available",
                provider="managed:kimi-code", provider_model="k3",
                supported_efforts=["low", "high"],
                cli_version="kimi 0.33.0")]}},
            **self.verification("kimi", "0.33.0"),
        }
        mc.persist_routes(self.con, payload)
        row = self.con.execute(
            "SELECT selector, availability, headless_supported, "
            "high_effort_supported, stale FROM model_routes").fetchone()
        self.assertEqual(tuple(row), ("kimi-code/k3", "available", 1, 1, 0))

    def test_failed_refresh_keeps_route_and_marks_it_stale(self):
        fresh = {"fetched_at": "2026-07-21T00:00:00+00:00", "stale": False,
                 "harnesses": {"claude": {"models": [mc._entry(
                     "fable", source="claude-cli", availability="available",
                     supported_efforts=["high"],
                     cli_version="claude 2.1.222")]}},
                 **self.verification("claude", "2.1.222")}
        mc.persist_routes(self.con, fresh)
        mc.persist_routes(self.con, {"fetched_at": None, "stale": True,
                                     "error": "network down", "harnesses": {}})
        row = self.con.execute(
            "SELECT stale, last_error FROM model_routes WHERE selector='fable'").fetchone()
        self.assertEqual(tuple(row), (1, "network down"))

    def test_resolver_returns_exact_high_effort_sc_run_call(self):
        fresh = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False,
                 "harnesses": {"codex": {"models": [mc._entry(
                     "gpt-5.6-sol", source="codex-cache",
                     availability="available", supported_efforts=["high"],
                     cli_version="codex-cli 0.145.0")]}},
                 **self.verification("codex", "0.145.0")}
        mc.persist_routes(self.con, fresh)
        fingerprint = self.con.execute(
            "SELECT source_fingerprint FROM model_routes WHERE harness='codex' "
            "AND selector='gpt-5.6-sol'"
        ).fetchone()[0]
        got = routes_cli.resolve(
            self.con, "codex", "gpt-5.6-sol", shell="DEV3",
            current_source_fingerprint=fingerprint)
        self.assertTrue(got["ok"])
        self.assertEqual(
            got["command"],
            ["./sc", "run", "DEV3", "--harness", "codex", "-m",
             "gpt-5.6-sol", "--effort", "high"])

    def test_resolver_rejects_unverified_high_effort(self):
        fresh = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False,
                 "harnesses": {"kimi": {"models": [mc._entry(
                     "kimi-code/legacy", source="kimi-config",
                     availability="available", supported_efforts=[],
                     cli_version="kimi 0.33.0")]}},
                 **self.verification("kimi", "0.33.0")}
        mc.persist_routes(self.con, fresh)
        fingerprint = self.con.execute(
            "SELECT source_fingerprint FROM model_routes WHERE harness='kimi' "
            "AND selector='kimi-code/legacy'"
        ).fetchone()[0]
        got = routes_cli.resolve(
            self.con, "kimi", "kimi-code/legacy",
            current_source_fingerprint=fingerprint,
        )
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "unsupported_thinking_level")


class RuntimeVerificationTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript((
            ROOT / ".super-coder" / "migrations" / "0075_model_routes.sql"
        ).read_text())
        self.con.execute(
            "CREATE TABLE flavor_defaults ("
            "flavor TEXT NOT NULL,harness TEXT NOT NULL,model TEXT,"
            "is_default INTEGER NOT NULL DEFAULT 0,"
            "PRIMARY KEY (flavor,harness))"
        )

    @staticmethod
    def harnesses():
        base = {
            "minimum_version": "1.0.0",
            "maximum_version_exclusive": "2.0.0",
            "verified_version": "1.5.0",
        }
        return {
            "codex": {
                **base, "version": "2.0.0",
                "compatibility": "newer-unverified", "error": None,
            },
            "claude": {
                **base, "version": "1.5.0",
                "compatibility": "verified", "error": None,
            },
            "opencode": {
                **base, "version": "1.6.0",
                "compatibility": "supported", "error": None,
            },
            "vibe": {
                "version": None, "compatibility": None,
                "minimum_version": None,
                "maximum_version_exclusive": None,
                "verified_version": None,
                "error": "HARNESS_UNAVAILABLE",
            },
        }

    def insert_default(self, flavor, harness, model, is_default=0):
        self.con.execute(
            "INSERT INTO flavor_defaults (flavor,harness,model,is_default) "
            "VALUES (?,?,?,?)",
            (flavor, harness, model, is_default),
        )

    def test_report_combines_harness_compatibility_and_exact_route_evidence(self):
        self.insert_default("dev", "codex", "gpt-ready", 1)
        self.insert_default("planner", "codex", "gpt-missing", 1)
        self.insert_default("reviewer", "claude", None, 1)
        self.insert_default("cartographer", "opencode", "openai/local", 1)
        self.insert_default("dev", "vibe", "vibe-model")
        mc.persist_routes(self.con, {
            "fetched_at": "2026-08-09T00:00:00+00:00",
            "stale": False,
            "harnesses": {
                "codex": {"models": [mc._entry(
                    "gpt-ready", source="codex-cache",
                    availability="available", supported_efforts=["high"],
                    cli_version="codex-cli 2.0.0",
                )]},
                "opencode": {"models": [mc._entry(
                    "openai/local", source="opencode-provider-api",
                    availability="available", cli_version="opencode 1.6.0",
                )]},
            },
            "verification": {"runtime": "sandbox",
                             "harnesses": self.harnesses()},
        })

        report = mc.runtime_verification(
            self.con, env={"SC_SANDBOX": "1"}, harness_probe=self.harnesses
        )

        self.assertEqual(report["runtime"], "sandbox")
        self.assertEqual(report["harnesses"]["codex"]["compatibility"],
                         "newer-unverified")
        self.assertEqual(report["summary"], {
            "harnesses_checked": 4,
            "harnesses_ready": 2,
            "exact_routes": 4,
            "exact_routes_runnable": 1,
            "harness_defaults": 1,
        })
        by_key = {
            (row["flavor"], row["harness"]): row
            for row in report["defaults"]
        }
        self.assertEqual(by_key[("dev", "codex")], {
            "flavor": "dev", "harness": "codex", "model": "gpt-ready",
            "is_default": True, "state": "harness-error", "runnable": False,
            "reason": "HARNESS_VERSION_UNVERIFIED",
        })
        self.assertEqual(by_key[("planner", "codex")]["state"],
                         "harness-error")
        self.assertFalse(by_key[("planner", "codex")]["runnable"])
        self.assertEqual(by_key[("reviewer", "claude")]["state"],
                         "harness-default")
        self.assertIsNone(by_key[("reviewer", "claude")]["runnable"])
        self.assertEqual(by_key[("cartographer", "opencode")]["state"],
                         "runnable")
        self.assertTrue(by_key[("cartographer", "opencode")]["runnable"])
        self.assertEqual(by_key[("dev", "vibe")]["reason"],
                         "HARNESS_UNAVAILABLE")
        self.assertFalse(by_key[("dev", "vibe")]["runnable"])

    def test_probe_failure_is_visible_without_skipping_the_response(self):
        def fail_probe():
            raise RuntimeError("probe transport failed")

        report = mc.runtime_verification(
            self.con, env={}, harness_probe=fail_probe
        )

        self.assertEqual(report["runtime"], "host")
        self.assertEqual(report["error"], "probe transport failed")
        self.assertEqual(report["harnesses"], {})
        self.assertEqual(report["defaults"], [])
        self.assertEqual(report["summary"], {
            "harnesses_checked": 0,
            "harnesses_ready": 0,
            "exact_routes": 0,
            "exact_routes_runnable": 0,
            "harness_defaults": 0,
        })

    def test_non_runnable_route_states_keep_exact_failure_reasons(self):
        self.insert_default("dev", "codex", "gpt-stale")
        self.insert_default("planner", "claude", "opus-advisory")
        self.insert_default("reviewer", "vibe", "vibe-local")
        self.insert_default("cartographer", "kimi", "kimi-low")
        mc.persist_routes(self.con, {
            "fetched_at": "2026-08-09T00:00:00+00:00",
            "stale": False,
            "harnesses": {
                "codex": {"models": [mc._entry(
                    "gpt-stale", availability="available",
                    supported_efforts=["high"]
                )]},
                "claude": {"models": [mc._entry(
                    "opus-advisory", availability="advisory",
                    supported_efforts=["high"]
                )]},
                "vibe": {"models": [mc._entry(
                    "vibe-local", availability="available"
                )]},
                "kimi": {"models": [mc._entry(
                    "kimi-low", availability="available",
                    supported_efforts=["low"]
                )]},
            },
        })
        self.con.execute(
            "UPDATE model_routes SET stale=1,last_error='catalog offline' "
            "WHERE harness='codex' AND selector='gpt-stale'"
        )
        statuses = self.harnesses()
        statuses["codex"] = {
            **statuses["claude"], "version": "1.6.0",
            "compatibility": "supported",
        }
        statuses["vibe"] = {
            "version": "vibe 1.0.0", "compatibility": None,
            "minimum_version": None, "maximum_version_exclusive": None,
            "verified_version": None, "error": None,
        }
        statuses["kimi"] = {
            **statuses["claude"], "version": "1.6.0",
            "compatibility": "supported",
        }

        report = mc.runtime_verification(
            self.con, env={}, harness_probe=lambda: statuses
        )

        by_harness = {row["harness"]: row for row in report["defaults"]}
        self.assertEqual(
            (by_harness["codex"]["state"], by_harness["codex"]["reason"]),
            ("route-stale", "catalog offline"),
        )
        self.assertEqual(
            (by_harness["claude"]["state"], by_harness["claude"]["reason"]),
            ("route-unavailable", "route is advisory, not locally available"),
        )
        self.assertEqual(
            (by_harness["vibe"]["state"], by_harness["vibe"]["reason"]),
            ("headless-unsupported", "harness has no headless launch seam"),
        )
        self.assertEqual(
            (by_harness["kimi"]["state"], by_harness["kimi"]["reason"]),
            ("effort-unsupported",
             "default high-effort route was not locally verified"),
        )
        self.assertTrue(all(
            row["runnable"] is False for row in report["defaults"]
        ))

    def test_explicit_refresh_caches_verification_for_later_gui_reads(self):
        self.insert_default("dev", "codex", "gpt-5.5", 1)
        with tempfile.TemporaryDirectory() as tmp:
            old_cache = mc.CACHE
            mc.CACHE = Path(tmp) / "model_catalog.json"
            self.addCleanup(lambda: setattr(mc, "CACHE", old_cache))
            probe = mock.Mock(return_value=self.harnesses())
            with mock.patch.object(mc.shutil, "which", return_value=None):
                first = mc.catalog(
                    refresh=True, fetch=fetch_ok, env={}, run=None, con=self.con,
                    opencode_provider=lambda: [], harness_probe=probe,
                )
                second = mc.catalog(
                    fetch=fetch_down, env={}, run=None, con=self.con,
                    opencode_provider=lambda: [],
                    harness_probe=mock.Mock(
                        side_effect=AssertionError("reprobed")
                    ),
                )

        probe.assert_called_once_with()
        self.assertEqual(first["verification"]["summary"]["exact_routes"], 1)
        self.assertEqual(
            first["verification"]["summary"]["exact_routes_runnable"], 0
        )
        self.assertEqual(second["verification"], first["verification"])
        self.assertFalse(second["stale"])

    def test_failed_first_refresh_still_persists_fork_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cache = mc.CACHE
            mc.CACHE = Path(tmp) / "model_catalog.json"
            self.addCleanup(lambda: setattr(mc, "CACHE", old_cache))
            with mock.patch.object(mc.shutil, "which", return_value=None):
                got = mc.catalog(
                    refresh=True, fetch=fetch_down, env={}, run=None,
                    con=self.con, opencode_provider=lambda: [],
                    harness_probe=self.harnesses,
                )
            cached = json.loads(mc.CACHE.read_text())

        self.assertTrue(got["stale"])
        self.assertEqual(got["sources"], ["static"])
        self.assertEqual(got["verification"]["summary"], {
            "harnesses_checked": 4,
            "harnesses_ready": 2,
            "exact_routes": 0,
            "exact_routes_runnable": 0,
            "harness_defaults": 0,
        })
        self.assertEqual(cached["verification"], got["verification"])
        self.assertIsNone(cached["fetched_at"])


class RouteCliConnectionTest(unittest.TestCase):
    ROUTE = {
        "harness": "codex", "selector": "api-model", "source": "live-api",
        "availability": "available", "stale": 0, "headless_supported": 1,
        "high_effort_supported": 1, "cli_version": "codex-cli 0.145.0",
        "harness_version": "0.145.0", "harness_compatibility": "verified",
        "supported_efforts": '["high"]',
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": "1" * 32,
        "evidence_kind": "codex-model-cache",
        "source_fingerprint": "3" * 64,
        "current_source_fingerprint": "3" * 64,
        "effort_metadata": json.dumps({
            "supported": ["high"], "default": "high",
            "digests": {"high": "2" * 64}, "native_variant_ids": {},
        }),
        "selector_binding": '{"kind":"exact-model","selector":"api-model"}',
        "adapter_metadata": "{}",
    }

    def test_list_and_resolve_use_shell_api_without_opening_database(self):
        with (
            mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
            mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
            mock.patch.object(
                routes_cli.mem, "_api", return_value={"routes": [self.ROUTE]}
            ) as api,
            mock.patch.object(
                routes_cli, "_open_db", side_effect=AssertionError("opened DB")
            ),
        ):
            self.assertEqual(routes_cli.main(["list", "codex"]), 0)
            self.assertEqual(
                routes_cli.main(["resolve", "codex", "api-model", "--json"]),
                0,
            )
        self.assertEqual(api.call_args_list, [
            mock.call("GET", "/_sc/model-routes?harness=codex"),
            mock.call(
                "GET", "/_sc/model-routes?harness=codex&selector=api-model"
            ),
        ])

    def test_no_token_root_list_keeps_normal_database_connection(self):
        con = mock.Mock()
        with (
            mock.patch.object(routes_cli.mem, "SC_API_TOKEN", ""),
            mock.patch.object(routes_cli, "_open_db", return_value=con) as opened,
            mock.patch.object(routes_cli, "_list", return_value=0),
        ):
            self.assertEqual(routes_cli.main(["list"]), 0)
        opened.assert_called_once_with()

    def test_resolve_rejects_an_explicitly_blank_shell(self):
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(
                SystemExit, "--shell requires a non-blank value"
            ):
                routes_cli._resolve_args([
                    "resolve", "codex", "api-model", "--shell", value,
                ])

    def test_refresh_keeps_the_wal_enabled_write_lane(self):
        con = mock.Mock()
        payload = {"stale": False, "sources": ["test-source"]}
        with (
            mock.patch.object(routes_cli, "_open_db", return_value=con) as opened,
            mock.patch.object(routes_cli.model_catalog, "catalog", return_value=payload),
        ):
            self.assertEqual(routes_cli.main(["refresh"]), 0)
        opened.assert_called_once_with()


class CatalogCacheTest(NoCLI):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = mc.CACHE
        mc.CACHE = Path(self.tmp.name) / "model_catalog.json"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(mc, "CACHE", self._orig))

    def test_writes_cache_and_serves_it_without_refetch(self):
        first = mc.catalog(fetch=fetch_ok, env={}, run=None)
        self.assertFalse(first["stale"])
        self.assertTrue(mc.CACHE.exists())
        second = mc.catalog(fetch=fetch_down, env={}, run=None)  # would fail live
        self.assertFalse(second["stale"], "fresh cache must serve without a fetch")
        self.assertEqual(second["harnesses"], first["harnesses"])

    def test_fresh_cache_still_refreshes_connected_opencode_models(self):
        provider_models = mock.Mock(side_effect=[
            [{
                "id": "openai/first",
                "provider": "openai",
                "provider_model": "first",
            }],
            [{
                "id": "anthropic/second",
                "provider": "anthropic",
                "provider_model": "second",
            }],
        ])
        with mock.patch.object(
            mc.shutil, "which", return_value="/usr/bin/opencode"
        ):
            first = mc.catalog(
                fetch=fetch_ok,
                env={},
                run=None,
                opencode_provider=provider_models,
            )
            second = mc.catalog(
                fetch=fetch_down,
                env={},
                run=None,
                opencode_provider=provider_models,
            )
        self.assertEqual(
            ids(first["harnesses"]["opencode"]),
            ["openai/first"],
        )
        self.assertEqual(
            ids(second["harnesses"]["opencode"]),
            ["anthropic/second"],
        )
        self.assertFalse(second["stale"], "the base cache remains fresh")
        self.assertEqual(provider_models.call_count, 2)

    def test_stale_cache_served_when_refresh_fails(self):
        mc.catalog(fetch=fetch_ok, env={}, run=None)
        aged = json.loads(mc.CACHE.read_text())
        aged["fetched_at"] = "2020-01-01T00:00:00+00:00"
        mc.CACHE.write_text(json.dumps(aged))
        got = mc.catalog(fetch=fetch_down, env={}, run=None)
        self.assertTrue(got["stale"])
        self.assertIn("claude", got["harnesses"])

    def test_version_mismatched_cache_is_ignored(self):
        # a v1-era cache (fresh timestamp, old shape) must not be served
        mc.catalog(fetch=fetch_ok, env={}, run=None)
        old = json.loads(mc.CACHE.read_text())
        del old["v"]
        mc.CACHE.write_text(json.dumps(old))
        got = mc.catalog(fetch=fetch_down, env={}, run=None)
        self.assertEqual(got["sources"], ["static"],
                         "shape-mismatched cache → treated as absent → floor")

    def test_refresh_flag_bypasses_fresh_cache(self):
        mc.catalog(fetch=fetch_ok, env={}, run=None)
        def fetch2(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return {"anthropic": {"models": {
                    "claude-next": {"release_date": "2027-01-01"}}}}
            raise RuntimeError
        got = mc.catalog(refresh=True, fetch=fetch2, env={}, run=None)
        self.assertIn("claude-next", ids(got["harnesses"]["claude"]))

    def test_static_floor_when_no_cache_and_no_network(self):
        got = mc.catalog(fetch=fetch_down, env={}, run=None)
        self.assertTrue(got["stale"])
        self.assertEqual(got["sources"], ["static"])
        for harness in ("claude", "codex", "vibe"):
            self.assertTrue(got["harnesses"][harness]["models"])
        self.assertEqual(
            got["harnesses"]["opencode"]["models"],
            [],
            "OpenCode never promotes a static route without a connected provider",
        )
        floor_fams = {f["family"]: f["latest"]
                      for f in got["harnesses"]["claude"]["families"]}
        self.assertEqual(floor_fams.get("opus"), "opus",
                         "floor retains alias family compatibility metadata")
        self.assertEqual(floor_fams.get("fable"), "fable",
                         "fable ships in the floor with its self-tracking alias")


if __name__ == "__main__":
    unittest.main()
