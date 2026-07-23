#!/usr/bin/env python3
"""Tests for the model-catalog service (api/model_catalog.py).

The catalog is layered and best-effort: models.dev (keyless, all five
harnesses) → provider APIs (only with env keys) → `opencode models` CLI →
cache → static floor. Payload v3 retains family metadata for compatibility:
(newest-first; claude families with a CLI alias resolve `latest` to the
alias) plus the flat `models` list for sub-version search. These tests pin
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

    def test_opencode_cli_merges_ollama_cloud_first(self):
        with mock.patch.object(mc.shutil, "which", return_value="/usr/bin/opencode"):
            def run(cmd, **kw):
                return mock.Mock(returncode=0,
                                 stdout="zprovider/zeta\nollama-cloud/glm-5.1\n"
                                        "not a model line\n")
            got = mc.build(fetch=fetch_ok, env={}, run=run)
        self.assertEqual(ids(got["harnesses"]["opencode"]),
                         ["ollama-cloud/glm-5.1", "zprovider/zeta",
                          "ollama-cloud/deepseek-v4-pro"])
        self.assertIn("opencode-cli", got["sources"])

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

    def tearDown(self):
        self.con.close()

    def test_persist_marks_exact_high_effort_route_runnable(self):
        payload = {
            "fetched_at": "2026-07-21T00:00:00+00:00", "stale": False,
            "harnesses": {"kimi": {"models": [mc._entry(
                "kimi-code/k3", source="kimi-config", availability="available",
                provider="managed:kimi-code", provider_model="k3",
                supported_efforts=["low", "high"])]}},
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
                     supported_efforts=["high"])]}}}
        mc.persist_routes(self.con, fresh)
        mc.persist_routes(self.con, {"fetched_at": None, "stale": True,
                                     "error": "network down", "harnesses": {}})
        row = self.con.execute(
            "SELECT stale, last_error FROM model_routes WHERE selector='fable'").fetchone()
        self.assertEqual(tuple(row), (1, "network down"))

    def test_resolver_returns_exact_high_effort_sc_run_call(self):
        fresh = {"fetched_at": "2026-07-21T00:00:00+00:00", "stale": False,
                 "harnesses": {"codex": {"models": [mc._entry(
                     "gpt-5.6-sol", source="codex-cache",
                     availability="available", supported_efforts=["high"])]}}}
        mc.persist_routes(self.con, fresh)
        got = routes_cli.resolve(
            self.con, "codex", "gpt-5.6-sol", shell="DEV3")
        self.assertTrue(got["ok"])
        self.assertEqual(
            got["command"],
            ["./sc", "run", "DEV3", "--harness", "codex", "-m",
             "gpt-5.6-sol", "--effort", "high"])

    def test_resolver_rejects_unverified_high_effort(self):
        fresh = {"fetched_at": "2026-07-21T00:00:00+00:00", "stale": False,
                 "harnesses": {"kimi": {"models": [mc._entry(
                     "kimi-code/legacy", source="kimi-config",
                     availability="available", supported_efforts=[])]}}}
        mc.persist_routes(self.con, fresh)
        got = routes_cli.resolve(self.con, "kimi", "kimi-code/legacy")
        self.assertFalse(got["ok"])
        self.assertIn("high-effort", got["error"])


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
        for harness in ("claude", "codex", "vibe", "opencode"):
            self.assertTrue(got["harnesses"][harness]["models"])
        floor_fams = {f["family"]: f["latest"]
                      for f in got["harnesses"]["claude"]["families"]}
        self.assertEqual(floor_fams.get("opus"), "opus",
                         "floor retains alias family compatibility metadata")
        self.assertEqual(floor_fams.get("fable"), "fable",
                         "fable ships in the floor with its self-tracking alias")


if __name__ == "__main__":
    unittest.main()
