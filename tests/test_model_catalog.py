#!/usr/bin/env python3
"""Tests for the model-catalog service (api/model_catalog.py).

The catalog is layered and best-effort: models.dev (keyless, all four
harnesses) → provider APIs (only with env keys) → `opencode models` CLI →
cache → static floor. Payload v2 is family-first: per harness `families`
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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
import model_catalog as mc  # noqa: E402

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
        self.assertEqual(got["sources"], ["models.dev"])

    def test_families_newest_first_alias_latest(self):
        fams = mc.build(fetch=fetch_ok, env={}, run=None)["harnesses"]["claude"]["families"]
        self.assertEqual([f["family"] for f in fams], ["fable", "opus"],
                         "families sort by newest release")
        by = {f["family"]: f for f in fams}
        self.assertEqual(by["fable"]["latest"], "claude-fable-5",
                         "no alias → newest concrete id")
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
                         ["ollama-cloud/deepseek-v4-pro", "ollama-cloud/glm-5.1",
                          "zprovider/zeta"])
        self.assertIn("opencode-cli", got["sources"])

    def test_all_sources_down_raises(self):
        with self.assertRaises(RuntimeError):
            mc.build(fetch=fetch_down, env={}, run=None)


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
                         "floor still offers alias family chips")


if __name__ == "__main__":
    unittest.main()
