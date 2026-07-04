#!/usr/bin/env python3
"""Tests for the model-catalog service (api/model_catalog.py).

The catalog is layered and best-effort: models.dev (keyless, all four
harnesses) → provider APIs (only with env keys) → `opencode models` CLI →
cache → static floor. These tests pin the layering contract: harness→provider
mapping and opencode prefixing, newest-first sorting, claude aliases leading,
opportunistic merges that never fail the sweep, stale-cache-on-failure, and
the floor when everything is down. All sources are injected — no network.

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
        "claude-opus-4-8": {"name": "Claude Opus 4.8", "release_date": "2026-04-01"},
        "claude-fable-5": {"name": "Claude Fable 5", "release_date": "2026-06-09"},
    }},
    "openai": {"models": {"gpt-5.5": {"release_date": "2026-01-01"}}},
    "mistral": {"models": {"devstral-latest": {"release_date": "2025-12-01"}}},
    "ollama-cloud": {"models": {"deepseek-v4-pro": {"release_date": "2026-02-02"}}},
}


def fetch_ok(url, headers=None):
    if url == mc.MODELS_DEV_URL:
        return MODELS_DEV
    if "openai" in url:
        return {"data": [{"id": "gpt-9-preview"}, {"id": "gpt-5.5"}]}
    raise RuntimeError(f"unexpected fetch: {url}")


def fetch_down(url, headers=None):
    raise OSError("network down")


class NoCLI(unittest.TestCase):
    """Base: opencode binary absent unless a test opts in."""

    def setUp(self):
        p = mock.patch.object(mc.shutil, "which", return_value=None)
        p.start()
        self.addCleanup(p.stop)


class BuildTest(NoCLI):
    def test_harness_mapping_and_prefixing(self):
        got = mc.build(fetch=fetch_ok, env={}, run=None)
        self.assertIn("claude-fable-5", [e["id"] for e in got["harnesses"]["claude"]])
        self.assertEqual([e["id"] for e in got["harnesses"]["codex"]], ["gpt-5.5"])
        self.assertEqual([e["id"] for e in got["harnesses"]["opencode"]],
                         ["ollama-cloud/deepseek-v4-pro"])
        self.assertEqual(got["sources"], ["models.dev"])

    def test_claude_aliases_lead_then_newest_first(self):
        ids = [e["id"] for e in mc.build(fetch=fetch_ok, env={}, run=None)
               ["harnesses"]["claude"]]
        self.assertEqual(ids[:3], mc.CLAUDE_ALIASES)
        self.assertEqual(ids[3:5], ["claude-fable-5", "claude-opus-4-8"],
                         "full ids must sort newest release first")

    def test_provider_api_merges_and_dedupes(self):
        got = mc.build(fetch=fetch_ok, env={"OPENAI_API_KEY": "k"}, run=None)
        ids = [e["id"] for e in got["harnesses"]["codex"]]
        self.assertEqual(ids, ["gpt-5.5", "gpt-9-preview"],
                         "keyed-API ids append deduped, models.dev order kept")
        self.assertIn("openai-api", got["sources"])

    def test_bad_provider_key_never_fails_the_sweep(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            raise OSError("401")
        got = mc.build(fetch=fetch, env={"OPENAI_API_KEY": "bad"}, run=None)
        self.assertEqual(got["sources"], ["models.dev"])

    def test_opencode_cli_merges(self):
        with mock.patch.object(mc.shutil, "which", return_value="/usr/bin/opencode"):
            def run(cmd, **kw):
                return mock.Mock(returncode=0,
                                 stdout="ollama-cloud/deepseek-v4-pro\n"
                                        "ollama-cloud/glm-5.1\nnot a model line\n")
            got = mc.build(fetch=fetch_ok, env={}, run=run)
        ids = [e["id"] for e in got["harnesses"]["opencode"]]
        self.assertEqual(ids, ["ollama-cloud/deepseek-v4-pro", "ollama-cloud/glm-5.1"])
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

    def test_refresh_flag_bypasses_fresh_cache(self):
        mc.catalog(fetch=fetch_ok, env={}, run=None)
        def fetch2(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return {"anthropic": {"models": {"claude-next": {"release_date": "2027-01-01"}}}}
            raise RuntimeError
        got = mc.catalog(refresh=True, fetch=fetch2, env={}, run=None)
        self.assertIn("claude-next", [e["id"] for e in got["harnesses"]["claude"]])

    def test_static_floor_when_no_cache_and_no_network(self):
        got = mc.catalog(fetch=fetch_down, env={}, run=None)
        self.assertTrue(got["stale"])
        self.assertEqual(got["sources"], ["static"])
        for harness in ("claude", "codex", "vibe", "opencode"):
            self.assertTrue(got["harnesses"][harness])


if __name__ == "__main__":
    unittest.main()
