#!/usr/bin/env python3
"""Tests for the model-catalog service (api/model_catalog.py).

The catalog is layered and best-effort: models.dev (keyless, all five
harnesses) → provider APIs (only with env keys) → OpenCode's connected-provider
    projection → cache → static floor. Payload v8 retains family metadata
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

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import model_catalog as mc  # noqa: E402
import models as routes_cli  # noqa: E402
import route_bindings  # noqa: E402
import deepseek_runtime  # noqa: E402

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


def deepseek_wire_proof(provider, model, options_by_effort, env=None):
    del env
    manifest = deepseek_runtime.load_runtime_manifest()
    registry = deepseek_runtime.load_provider_adapter_registry(
        expected_sha256=manifest["provider_adapters"]["sha256"]
    )
    adapter = registry["providers"][provider]
    adapter_digest = route_bindings.digest_json(adapter)
    proofs = {}
    for effort, options in options_by_effort.items():
        wire = {}
        if effort != "default":
            if adapter["wire_mode"] == "deepseek-request-patch":
                wire["thinking"] = {"type": "enabled"}
            wire["reasoning_effort"] = effort
        native_request = {
            "event_type": "provider.request",
            "provider": provider,
            "model": model,
            "reasoning_effort": None if effort == "default" else effort,
            "purpose": "conversation",
        }
        evidence = {
            "contract": deepseek_runtime.PROVIDER_WIRE_CONTRACT,
            "provider": provider,
            "model": model,
            "effort": effort,
            "provider_options": dict(options),
            "wire_options": wire,
            "native_request": native_request,
            "purpose_proofs": {
                purpose: {
                    "wire_options": wire,
                    "native_request": {**native_request, "purpose": purpose},
                }
                for purpose in deepseek_runtime.PROVIDER_WIRE_PURPOSES
            },
            "runtime_version": "0.1.0rc7",
            "source_commit": "bb4ca698d63714e753f5621b07400e6ebb0b5d97",
            "patch_sha256": manifest["patch"]["sha256"],
            "composition_sha256": adapter["composition_sha256"],
            "provider_registry_sha256": manifest["provider_adapters"]["sha256"],
            "provider_adapter_id": adapter["adapter_id"],
            "provider_adapter_digest": adapter_digest,
        }
        proofs[effort] = {
            **evidence,
            "digest": route_bindings.digest_json(evidence),
        }
    return {
        "contract": deepseek_runtime.PROVIDER_WIRE_CONTRACT,
        "provider": provider,
        "model": model,
        "proofs": proofs,
    }


_AUTO_COMPATIBILITY = object()


def runtime_status(version="2.22.0", compatibility=_AUTO_COMPATIBILITY,
                   error=None, *, harness=None, scope=None):
    harness = harness or (
        "claude" if isinstance(version, str) and version.startswith("2.1.")
        else "vibe"
    )
    ranges = {
        "claude": ("2.1.220", "2.2.0", "2.1.222"),
        "codex": ("0.145.0", "0.147.0", "0.145.0"),
        "deepseek": (None, None, "0.1.0rc7"),
        "kimi": ("0.30.0", "0.34.0", "0.33.0"),
        "opencode": ("1.18.9", "1.19.0", "1.18.9"),
        "vibe": ("2.22.0", "2.23.0", "2.22.0"),
    }
    minimum, maximum, verified = ranges[harness]
    if compatibility is _AUTO_COMPATIBILITY:
        compatibility = "verified" if version == verified else "supported"
    scope = scope or routes_cli.model_catalog.harness_versions.runtime_scope()
    return {
        "harness": harness,
        **scope,
        "version": version,
        "compatibility": compatibility,
        "minimum_version": minimum,
        "maximum_version_exclusive": maximum,
        "verified_version": verified,
        "error": error,
    }


def controlled_bundle(
    harness: str, selector: str, status: dict, fingerprint: str | None
) -> dict:
    scope = {
        "runtime": status["runtime"],
        "runtime_identity": status["runtime_identity"],
    }
    return {
        "runtime_status": status,
        "runtime_scope": scope,
        "source_fingerprint": fingerprint,
    }


def controlled_row(
    harness: str, selector: str, status: dict, fingerprint: str
) -> dict:
    sources = {
        "claude": ("claude-cli", "claude-portable-manifest"),
        "codex": ("codex-cache", "codex-model-cache"),
        "kimi": ("kimi-config", "kimi-alias-config"),
        "opencode": (
            "opencode-provider-api", "opencode-connected-variant"
        ),
    }
    source, evidence_kind = sources[harness]
    metadata = {
        "supported": ["high"],
        "default": "high",
        "digests": {"high": "4" * 64},
        "native_variant_ids": (
            {"high": "high"} if harness == "opencode" else {}
        ),
    }
    adapter_metadata = {}
    if harness == "opencode":
        selected = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {"reasoningEffort": "high"},
        }
        metadata["adapter_metadata_by_effort"] = {"high": selected}
        adapter_metadata = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options_by_effort": {
                "high": {"reasoningEffort": "high"},
            },
        }
    return {
        "harness": harness,
        "selector": selector,
        "provider_model": selector,
        "source": source,
        "availability": "available",
        "headless_supported": 1,
        "stale": 0,
        "last_error": None,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": "1" * 32,
        "evidence_kind": evidence_kind,
        "source_fingerprint": fingerprint,
        "cli_version": f"{harness} {status['version']}",
        "harness_version": status["version"],
        "harness_compatibility": status["compatibility"],
        "supported_efforts": ["high"],
        "effort_metadata": metadata,
        "selector_binding": {"kind": "exact-model", "selector": selector},
        "adapter_metadata": adapter_metadata,
    }


class NoCLI(unittest.TestCase):
    """Base: opencode binary absent unless a test opts in."""

    def setUp(self):
        p = mock.patch.object(mc.shutil, "which", return_value=None)
        p.start()
        self.addCleanup(p.stop)


class ControlledRouteEvidenceTest(unittest.TestCase):
    def test_each_collector_binds_source_to_exact_sandbox_route(self):
        sandbox = {
            "runtime": "sandbox",
            "runtime_identity": "sandbox:collector-image",
        }
        versions = {
            "claude": "2.1.222", "codex": "0.145.0",
            "kimi": "0.33.0", "opencode": "1.18.9",
        }
        selectors = {
            "claude": "opus", "codex": "cache-model",
            "kimi": "configured-alias", "opencode": "provider/model",
        }

        def run(argv, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{argv[0]} {versions[argv[0]]}\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "models_cache.json").write_text(json.dumps({
                "models": [{
                    "slug": "cache-model",
                    "display_name": "Cache Model",
                    "supported_reasoning_levels": [
                        {"effort": "low"}, {"effort": "high"},
                    ],
                    "default_reasoning_level": "high",
                }],
            }))
            kimi_home = root / "kimi"
            kimi_home.mkdir()
            (kimi_home / "config.toml").write_text(
                "[models.configured-alias]\n"
                'provider = "moonshot"\n'
                'model = "kimi-k2"\n'
                'support_efforts = ["low", "high"]\n'
                'default_effort = "high"\n'
            )
            envs = {
                "claude": {},
                "codex": {"CODEX_HOME": str(codex_home)},
                "kimi": {"KIMI_CODE_HOME": str(kimi_home)},
                "opencode": {},
            }
            connected = [{
                "id": "provider/model",
                "provider": "provider",
                "provider_model": "model",
                "cli_version": "opencode 1.18.9",
                "supported_efforts": ["high"],
                "default_effort": "high",
                "selector_binding": {
                    "kind": "connected-model", "selector": "provider/model",
                },
                "adapter_metadata": {
                    "compatibility_manifest": "opencode-1.18.9-v1",
                    "provider_family": "openai-ai-sdk",
                    "variant_options_by_effort": {
                        "high": {"reasoningEffort": "high"},
                    },
                },
                "native_variant_ids": {"high": "high"},
            }]

            for harness, selector in selectors.items():
                status = runtime_status(
                    versions[harness], harness=harness, scope=sandbox
                )
                toml_available = mock.patch.object(
                    mc.toml_compat, "AVAILABLE", True
                ) if harness == "kimi" else contextlib.nullcontext()
                toml_loads = mock.patch.object(
                    mc.toml_compat, "loads", return_value={
                        "models": {"configured-alias": {
                            "provider": "moonshot",
                            "model": "kimi-k2",
                            "support_efforts": ["low", "high"],
                            "default_effort": "high",
                        }},
                    }
                ) if harness == "kimi" else contextlib.nullcontext()
                with (
                    self.subTest(harness=harness, state="present"),
                    mock.patch.object(
                        mc.harness_versions, "runtime_scope",
                        return_value=sandbox,
                    ),
                    mock.patch.object(
                        mc.shutil, "which", return_value=f"/bin/{harness}"
                    ),
                    toml_available,
                    toml_loads,
                ):
                    proof = mc.controlled_route_evidence(
                        harness, selector, env=envs[harness], run=run,
                        opencode_provider=lambda: connected,
                        harness_probe=lambda: {harness: status},
                    )
                self.assertEqual(proof["runtime_status"], status)
                self.assertEqual(proof["runtime_scope"], sandbox)
                self.assertRegex(
                    proof["source_fingerprint"],
                    r"^[0-9a-f]{64}$",
                )

                canonical_toml_available = mock.patch.object(
                    mc.toml_compat, "AVAILABLE", True
                ) if harness == "kimi" else contextlib.nullcontext()
                canonical_toml_loads = mock.patch.object(
                    mc.toml_compat, "loads", return_value={
                        "models": {"configured-alias": {
                            "provider": "moonshot",
                            "model": "kimi-k2",
                            "support_efforts": ["low", "high"],
                            "default_effort": "high",
                        }},
                    }
                ) if harness == "kimi" else contextlib.nullcontext()
                with (
                    self.subTest(harness=harness, state="canonical-resolver"),
                    mock.patch.dict(mc.os.environ, envs[harness], clear=True),
                    mock.patch.object(mc.subprocess, "run", side_effect=run),
                    mock.patch.object(
                        mc, "opencode_connected_models", return_value=connected
                    ),
                    mock.patch.object(
                        mc.harness_versions, "compatibility_status",
                        return_value={harness: status},
                    ),
                    mock.patch.object(
                        mc.harness_versions, "runtime_scope",
                        return_value=sandbox,
                    ),
                    mock.patch.object(
                        mc.shutil, "which", return_value=f"/bin/{harness}"
                    ),
                    canonical_toml_available,
                    canonical_toml_loads,
                ):
                    binding, digest = route_bindings.resolve_v2(
                        controlled_row(
                            harness, selector, status,
                            proof["source_fingerprint"],
                        ),
                        harness,
                        selector,
                        "high",
                    )
                self.assertEqual(binding["requested_model"], selector)
                self.assertEqual(binding["effective_effort"], "high")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

                missing_env = {
                    "codex": {"CODEX_HOME": str(root / "missing-codex")},
                    "kimi": {"KIMI_CODE_HOME": str(root / "missing-kimi")},
                }.get(harness, envs[harness])
                missing_which = None if harness == "claude" else f"/bin/{harness}"
                missing_provider = (lambda: []) if harness == "opencode" \
                    else (lambda: connected)
                toml_available = mock.patch.object(
                    mc.toml_compat, "AVAILABLE", True
                ) if harness == "kimi" else contextlib.nullcontext()
                toml_loads = mock.patch.object(
                    mc.toml_compat, "loads", return_value={}
                ) if harness == "kimi" else contextlib.nullcontext()
                with (
                    self.subTest(harness=harness, state="missing"),
                    mock.patch.object(
                        mc.harness_versions, "runtime_scope",
                        return_value=sandbox,
                    ),
                    mock.patch.object(
                        mc.shutil, "which", return_value=missing_which
                    ),
                    toml_available,
                    toml_loads,
                ):
                    missing = mc.controlled_route_evidence(
                        harness, selector, env=missing_env, run=run,
                        opencode_provider=missing_provider,
                        harness_probe=lambda: {harness: status},
                    )
                self.assertIsNone(missing["source_fingerprint"])


class BuildTest(NoCLI):
    def test_single_harness_runtime_status_uses_version_bounded_probe(self):
        expected = runtime_status()
        with mock.patch.object(
            mc.harness_versions, "compatibility_status",
            return_value={"vibe": expected},
        ) as probe:
            got = mc.harness_runtime_status("vibe")

        self.assertEqual(got, expected)
        probe.assert_called_once_with(("vibe",))

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

    def test_deepseek_catalogue_uses_only_authenticated_exact_models(self):
        calls = []

        def fetch(url, headers=None):
            calls.append((url, headers))
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            if url == "https://gateway.example/deepseek/v1/models":
                return {"data": [
                    {"id": "deepseek-v4-pro"},
                    {"id": "deepseek-v4-flash", "name": "V4 Flash"},
                ]}
            raise AssertionError(url)

        got = mc.build(fetch=fetch, env={
            "DEEPSEEK_API_KEY": "secret-key",
            "DEEPSEEK_BASE_URL": "https://gateway.example/deepseek/v1",
        }, run=None, deepseek_wire_probe=deepseek_wire_proof)

        block = got["harnesses"]["deepseek"]
        self.assertEqual(ids(block), ["deepseek-v4-pro", "deepseek-v4-flash"])
        self.assertEqual(got["sources"], ["models.dev", mc.DEEPSEEK_SOURCE])
        self.assertFalse(got["partial"])
        self.assertEqual(calls[-1], (
            "https://gateway.example/deepseek/v1/models",
            {"Authorization": "Bearer secret-key"},
        ))
        route = block["models"][0]
        self.assertEqual(route["availability"], "available")
        self.assertNotIn("error", block)
        self.assertEqual(route["provider"], "deepseek-official")
        self.assertEqual(route["provider_model"], "deepseek-v4-pro")
        self.assertEqual(route["supported_efforts"], ["low", "high", "max"])
        self.assertEqual(route["default_effort"], "high")
        manifest = deepseek_runtime.load_runtime_manifest()
        provider = deepseek_runtime.provider_adapter("deepseek-official")
        discovery_digest = route_bindings.digest_json({
            "provider": "deepseek-official",
            "endpoint_identity": "https://gateway.example/deepseek/v1",
            "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
            "provider_registry_sha256": manifest["provider_adapters"]["sha256"],
        })
        self.assertEqual(route["selector_binding"], {
            "kind": "authenticated-provider-model",
            "selector": "deepseek-v4-pro",
            "provider_model": "deepseek-v4-pro",
            "provider_route": "deepseek-official",
            "provider_adapter_id": provider["adapter_id"],
            "provider_adapter_digest": route_bindings.digest_json(provider),
            "provider_registry_sha256": manifest["provider_adapters"]["sha256"],
            "credential_kind": "deepseek-api-key",
            "endpoint_identity": "https://gateway.example/deepseek/v1",
            "discovery_evidence_digest": discovery_digest,
            "models_url": "https://gateway.example/deepseek/v1/models",
            "runtime_source_commit": "bb4ca698d63714e753f5621b07400e6ebb0b5d97",
            "provider_wire_contract": deepseek_runtime.PROVIDER_WIRE_CONTRACT,
            "provider_wire_digests": {
                effort: deepseek_wire_proof(
                    "deepseek-official", "deepseek-v4-pro",
                    mc._deepseek_carrier_options()
                )["proofs"][effort]["digest"]
                for effort in ("default", "low", "high", "max")
            },
        })
        mappings = route["adapter_metadata"]["provider_options_by_effort"]
        self.assertEqual(mappings["default"], {
            "omit": ["thinking", "reasoning_effort"], "set": {},
        })
        self.assertEqual(mappings["high"], {
            "omit": [],
            "set": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        })

    def test_ollama_route_uses_only_ollama_credential_and_exact_prefix(self):
        calls = []

        def fetch(url, headers=None):
            calls.append((url, headers))
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            if url == "https://ollama.com/v1/models":
                return {"data": [
                    {"id": "deepseek-v4-pro:0813"},
                    {"id": "deepseek-v4-pro:0813-cloud"},
                ]}
            raise AssertionError(url)

        got = mc.build(
            fetch=fetch,
            env={"OLLAMA_API_KEY": "ollama-secret"},
            run=None,
            deepseek_wire_probe=deepseek_wire_proof,
        )

        route = got["harnesses"]["deepseek"]["models"][0]
        self.assertEqual(route["id"], "ollama-cloud/deepseek-v4-pro:0813")
        self.assertEqual(route["provider"], "ollama-cloud")
        self.assertEqual(route["provider_model"], "deepseek-v4-pro:0813")
        self.assertEqual(route["supported_efforts"], [])
        self.assertIsNone(route["default_effort"])
        self.assertEqual(route["adapter_metadata"]["credential_kind"], "ollama-api-key")
        self.assertEqual(
            route["adapter_metadata"]["provider_options_by_effort"],
            {"default": {"omit": ["thinking", "reasoning_effort"], "set": {}}},
        )
        self.assertEqual(calls[-1], (
            "https://ollama.com/v1/models",
            {"Authorization": "Bearer ollama-secret"},
        ))
        self.assertIn(mc.OLLAMA_CLOUD_SOURCE, got["sources"])
        self.assertNotIn(mc.DEEPSEEK_SOURCE, got["sources"])
        self.assertIn(
            "ollama-cloud/deepseek-v4-pro:0813-cloud",
            ids(got["harnesses"]["deepseek"]),
        )
        serialized = json.dumps(got)
        self.assertNotIn("ollama-secret", serialized)
        self.assertNotIn("OLLAMA_API_KEY", serialized)

    def test_ollama_authenticated_exact_model_is_not_fixed_to_one_selector(self):
        probe = mock.Mock(side_effect=deepseek_wire_proof)

        got = mc.build(
            fetch=lambda url, _headers=None: (
                MODELS_DEV
                if url == mc.MODELS_DEV_URL
                else {"data": [{"id": "deepseek-v4-pro:0813-cloud"}]}
            ),
            env={"OLLAMA_API_KEY": "ollama-secret"},
            run=None,
            deepseek_wire_probe=probe,
        )

        self.assertEqual(
            ids(got["harnesses"]["deepseek"]),
            ["ollama-cloud/deepseek-v4-pro:0813-cloud"],
        )
        self.assertIn(mc.OLLAMA_CLOUD_SOURCE, got["sources"])
        probe.assert_called_once()

    def test_one_provider_failure_does_not_suppress_the_other_provider(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            if url == "https://api.deepseek.com/models":
                raise OSError("deepseek unavailable")
            if url == "https://ollama.com/v1/models":
                return {"data": [{"id": "deepseek-v4-pro:0813"}]}
            raise AssertionError(url)

        got = mc.build(
            fetch=fetch,
            env={
                "DEEPSEEK_API_KEY": "deepseek-secret",
                "OLLAMA_API_KEY": "ollama-secret",
            },
            run=None,
            deepseek_wire_probe=deepseek_wire_proof,
        )

        self.assertEqual(
            ids(got["harnesses"]["deepseek"]),
            ["ollama-cloud/deepseek-v4-pro:0813"],
        )
        self.assertIn(mc.OLLAMA_CLOUD_SOURCE, got["sources"])
        self.assertNotIn(mc.DEEPSEEK_SOURCE, got["sources"])

    def test_http_json_rejects_body_larger_than_four_mebibytes(self):
        reads = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, amount):
                reads.append(amount)
                return b"x" * (4 * 1024 * 1024 + 1)

        with mock.patch.object(
            mc.urllib.request, "urlopen", return_value=Response()
        ):
            with self.assertRaisesRegex(
                ValueError, "model catalogue response exceeds safety limits"
            ):
                mc._http_json("https://provider.example/models")

        self.assertEqual(reads, [4 * 1024 * 1024 + 1])

    def test_deepseek_oversized_http_response_has_stable_fail_closed_result(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _amount):
                return self.body

        def open_request(request, timeout=None):
            self.assertEqual(timeout, mc.TIMEOUT)
            if request.full_url == mc.MODELS_DEV_URL:
                return Response(json.dumps(MODELS_DEV).encode())
            self.assertEqual(request.full_url, "https://api.deepseek.com/models")
            return Response(b"x" * (4 * 1024 * 1024 + 1))

        with mock.patch.object(mc.urllib.request, "urlopen", side_effect=open_request):
            got = mc.build(
                env={"DEEPSEEK_API_KEY": "secret-key"},
                run=None,
                deepseek_wire_probe=deepseek_wire_proof,
            )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            "authenticated DeepSeek model response exceeds safety limits",
        )
        self.assertNotIn(mc.DEEPSEEK_SOURCE, got["sources"])

    def test_deepseek_catalogue_caps_models_and_wire_probe_work(self):
        proof_calls = []

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": f"deepseek-exact-{index}"}
                             for index in range(8)]}

        def prove(provider, model, options_by_effort, env=None):
            proof_calls.append(model)
            return deepseek_wire_proof(provider, model, options_by_effort, env)

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=prove,
        )

        self.assertEqual(
            ids(got["harnesses"]["deepseek"]),
            [f"deepseek-exact-{index}" for index in range(8)],
        )
        self.assertEqual(
            proof_calls,
            [f"deepseek-exact-{index}" for index in range(8)],
        )

    def test_ollama_max_catalogue_proves_only_bounded_exact_selectors(self):
        proof_calls = []
        configured = "deepseek-v4-pro:0813"
        rows = [{"id": configured}] + [
            {"id": f"other-model-{index}"} for index in range(63)
        ]

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            self.assertEqual(url, "https://ollama.com/v1/models")
            return {"data": rows}

        def unavailable_probe(provider, model, options_by_effort, env=None):
            proof_calls.append((provider, model))
            raise TimeoutError("bounded carrier proof timed out")

        got = mc.build(
            fetch=fetch,
            env={"OLLAMA_API_KEY": "ollama-secret"},
            run=None,
            deepseek_wire_probe=unavailable_probe,
        )

        self.assertEqual(
            proof_calls,
            [
                ("ollama-cloud", configured),
                ("ollama-cloud", "other-model-0"),
                ("ollama-cloud", "other-model-1"),
                ("ollama-cloud", "other-model-10"),
            ],
        )
        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

    def test_ollama_explicit_model_outside_background_sample_is_proved(self):
        rows = [
            {"id": model}
            for model in ("model-a", "model-b", "model-c", "model-d", "wanted-model")
        ]
        proof_calls = []

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": rows}

        def prove(provider, model, options_by_effort, env=None):
            proof_calls.append((provider, model))
            return deepseek_wire_proof(provider, model, options_by_effort, env)

        got = mc.build(
            fetch=fetch,
            env={"OLLAMA_API_KEY": "ollama-secret"},
            run=None,
            deepseek_wire_probe=prove,
            deepseek_selector="ollama-cloud/wanted-model",
        )

        self.assertEqual(
            ids(got["harnesses"]["deepseek"]),
            ["ollama-cloud/wanted-model"],
        )
        self.assertEqual(proof_calls, [("ollama-cloud", "wanted-model")])
        self.assertEqual(
            got["harnesses"]["deepseek"]["authenticated_routes"][0][
                "selectors"
            ],
            [f"ollama-cloud/{row['id']}" for row in rows],
        )

    def test_authenticated_provider_rejects_entire_malformed_generation_before_probe(self):
        configured = "deepseek-v4-pro:0813"
        invalid_rows = (
            [{"id": configured}, {"id": configured}],
            [{"id": configured}, {"id": ""}],
            [{"id": configured}, {"id": " bad "}],
            [{"id": configured}, {"id": 7}],
            [{"id": configured}, {}],
            [{"id": configured}, "not-an-object"],
        )

        for rows in invalid_rows:
            probe = mock.Mock(side_effect=AssertionError("invalid rows must not probe"))
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(
                    mc._DeepSeekDiscoveryEvidenceError,
                    mc.DEEPSEEK_DISCOVERY_EVIDENCE_INVALID,
                ):
                    mc._from_deepseek_provider(
                        "ollama-cloud",
                        lambda _url, _headers: {"data": rows},
                        {"OLLAMA_API_KEY": "ollama-secret"},
                        wire_probe=probe,
                    )
                probe.assert_not_called()

    def test_invalid_authenticated_generation_is_route_local(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {
                "data": [
                    {"id": "deepseek-v4-pro:0813"},
                    {"id": "deepseek-v4-pro:0813"},
                ]
            }

        probe = mock.Mock(side_effect=AssertionError("invalid rows must not probe"))
        got = mc.build(
            fetch=fetch,
            env={"OLLAMA_API_KEY": "ollama-secret"},
            run=None,
            deepseek_wire_probe=probe,
        )

        self.assertEqual(ids(got["harnesses"]["claude"]), [
            "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
        ])
        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_DISCOVERY_EVIDENCE_INVALID,
        )
        probe.assert_not_called()

    def test_registry_failure_is_deepseek_only_on_a_cold_successful_build(self):
        failures = (
            FileNotFoundError("provider registry missing"),
            deepseek_runtime.DeepSeekRuntimeError(
                "HARNESS_PROVIDER_ADAPTER_DRIFT", "provider registry digest drifted"
            ),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                mc, "_deepseek_provider_registry", side_effect=failure
            ):
                got = mc.build(fetch=lambda _url, _headers=None: MODELS_DEV, env={}, run=None)

            self.assertEqual(ids(got["harnesses"]["codex"]), ["gpt-5.5"])
            self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
            self.assertEqual(
                got["harnesses"]["deepseek"]["error"],
                mc.DEEPSEEK_PROVIDER_REGISTRY_INVALID,
            )
            self.assertEqual(got["sources"], ["models.dev"])
            self.assertTrue(got["partial"])

    def test_deepseek_catalogue_fails_closed_before_probe_above_model_cap(self):
        proof_calls = []

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": f"deepseek-exact-{index}"}
                             for index in range(9)]}

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=lambda provider, model, options_by_effort, env=None: (
                proof_calls.append(model)
            ),
        )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            "authenticated DeepSeek model response exceeds safety limits",
        )
        self.assertNotIn(mc.DEEPSEEK_SOURCE, got["sources"])
        self.assertEqual(proof_calls, [])

    def test_deepseek_catalogue_fails_closed_before_probe_on_oversized_id(self):
        proof_calls = []

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": "d" * 257}]}

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=lambda provider, model, options_by_effort, env=None: (
                proof_calls.append(model)
            ),
        )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            "authenticated DeepSeek model response exceeds safety limits",
        )
        self.assertEqual(proof_calls, [])

    def test_deepseek_discovery_failure_is_redacted_and_fails_closed(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            raise OSError("Authorization: Bearer secret-key")

        got = mc.build(
            fetch=fetch, env={"DEEPSEEK_API_KEY": "secret-key"}, run=None
        )

        self.assertTrue(got["partial"])
        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_DISCOVERY_ERROR,
        )
        serialized = json.dumps(got)
        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("Bearer", serialized)

    def test_deepseek_tampered_wire_receipt_admits_no_route(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            self.assertEqual(
                headers, {"Authorization": "Bearer secret-key"}
            )
            return {"data": [{"id": "deepseek-v4-pro"}]}

        def tampered_wire(provider, model, options_by_effort, env=None):
            proof = deepseek_wire_proof(provider, model, options_by_effort, env)
            proof["proofs"]["default"]["digest"] = "0" * 64
            return proof

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=tampered_wire,
        )

        self.assertTrue(got["partial"])
        self.assertEqual(got["sources"], ["models.dev"])
        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

    def test_deepseek_wire_only_receipt_admits_no_route(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": "deepseek-v4-pro"}]}

        def wire_only(provider, model, options_by_effort, env=None):
            proof = deepseek_wire_proof(provider, model, options_by_effort, env)
            for item in proof["proofs"].values():
                item.pop("native_request")
                item["digest"] = route_bindings.digest_json({
                    key: item[key]
                    for key in (
                        "contract", "model", "effort", "provider_options",
                        "wire_options", "runtime_version", "source_commit",
                        "patch_sha256",
                    )
                })
            return proof

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=wire_only,
        )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

    def test_deepseek_native_effort_mismatch_admits_no_route(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": "deepseek-v4-pro"}]}

        def mismatched_native(provider, model, options_by_effort, env=None):
            proof = deepseek_wire_proof(provider, model, options_by_effort, env)
            item = proof["proofs"]["low"]
            item["native_request"]["reasoning_effort"] = "high"
            item["digest"] = route_bindings.digest_json({
                key: item[key]
                for key in (
                    "contract", "model", "effort", "provider_options",
                    "wire_options", "native_request", "runtime_version",
                    "source_commit", "patch_sha256",
                )
            })
            return proof

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=mismatched_native,
        )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

    def test_deepseek_missing_session_title_proof_admits_no_route(self):
        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": "deepseek-v4-pro"}]}

        def missing_purpose(provider, model, options_by_effort, env=None):
            proof = deepseek_wire_proof(provider, model, options_by_effort, env)
            item = proof["proofs"]["default"]
            del item["purpose_proofs"]["session-title"]
            item["digest"] = route_bindings.digest_json({
                key: value for key, value in item.items() if key != "digest"
            })
            return proof

        got = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=missing_purpose,
        )

        self.assertEqual(got["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            got["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

    def test_deepseek_controlled_probe_reauthenticates_exact_source(self):
        scope = mc.harness_versions.runtime_scope()
        status = runtime_status(
            "0.1.0rc7", harness="deepseek", scope=scope
        )
        calls = []

        def fetch(url, headers=None):
            calls.append((url, headers))
            return {"data": [{"id": "deepseek-v4-pro"}]}

        proof = mc.controlled_route_evidence(
            "deepseek",
            "deepseek-v4-pro",
            env={"DEEPSEEK_API_KEY": "secret-key"},
            harness_probe=lambda: {"deepseek": status},
            deepseek_fetch=fetch,
            deepseek_wire_probe=deepseek_wire_proof,
        )

        self.assertEqual(proof["runtime_status"], status)
        self.assertEqual(proof["runtime_scope"], scope)
        self.assertRegex(proof["source_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(calls, [(
            "https://api.deepseek.com/models",
            {"Authorization": "Bearer secret-key"},
        )])

        missing = mc.controlled_route_evidence(
            "deepseek",
            "deepseek-v4-pro",
            env={},
            harness_probe=lambda: {"deepseek": status},
            deepseek_fetch=fetch,
        )
        self.assertIsNone(missing["source_fingerprint"])
        self.assertEqual(len(calls), 1)

    def test_deepseek_controlled_probe_serializes_only_selected_model(self):
        scope = mc.harness_versions.runtime_scope()
        status = runtime_status(
            "0.1.0rc7", harness="deepseek", scope=scope
        )
        proof_calls = []

        def fetch(_url, headers=None):
            self.assertEqual(headers, {"Authorization": "Bearer secret-key"})
            return {"data": [{"id": f"deepseek-exact-{index}"}
                             for index in range(8)]}

        def prove(provider, model, options_by_effort, env=None):
            proof_calls.append(model)
            return deepseek_wire_proof(provider, model, options_by_effort, env)

        proof = mc.controlled_route_evidence(
            "deepseek",
            "deepseek-exact-7",
            env={"DEEPSEEK_API_KEY": "secret-key"},
            harness_probe=lambda: {"deepseek": status},
            deepseek_fetch=fetch,
            deepseek_wire_probe=prove,
        )

        self.assertRegex(proof["source_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(proof_calls, ["deepseek-exact-7"])

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

    def test_opencode_live_overlay_preserves_admitted_variant_evidence(self):
        connected = mc.opencode_connected_models({
            "_sc_cli_version": "1.18.9",
            "connected": ["openai"],
            "all": [{
                "id": "openai",
                "npm": "@ai-sdk/openai",
                "models": {"gpt-connected": {
                    "variants": {
                        "high": {"reasoningEffort": "high"},
                    }
                }},
            }],
        })
        with mock.patch.object(mc.shutil, "which", return_value="/usr/bin/opencode"):
            got = mc._with_live_opencode(
                mc.build(fetch=fetch_ok, env={}, run=None),
                lambda: connected,
            )

        model = got["harnesses"]["opencode"]["models"][0]
        self.assertEqual(model["supported_efforts"], ["high"])
        self.assertEqual(model["default_effort"], "high")
        self.assertEqual(model["native_variant_ids"], {"high": "high"})
        self.assertEqual(
            model["adapter_metadata"]["variant_options_by_effort"],
            {"high": {"reasoningEffort": "high"}},
        )

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
        self.con.executescript((
            ROOT / ".super-coder" / "migrations" /
            "0217_harness_support_metadata.sql"
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

    def test_authenticated_deepseek_ids_stay_unbindable_before_wire_proof(self):
        def fetch(url, _headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": [{"id": "deepseek-v4-pro"}]}

        def unavailable_wire(*_args, **_kwargs):
            raise RuntimeError("carrier unavailable")

        payload = mc.build(
            fetch=fetch,
            env={"DEEPSEEK_API_KEY": "secret-key"},
            run=None,
            deepseek_wire_probe=unavailable_wire,
        )
        payload.update(self.verification("deepseek", "0.1.0rc7"))
        self.assertEqual(payload["harnesses"]["deepseek"]["models"], [])
        self.assertEqual(
            payload["harnesses"]["deepseek"]["error"],
            mc.DEEPSEEK_PROVIDER_OPTIONS_UNVERIFIED,
        )

        mc.persist_routes(self.con, payload)

        row = self.con.execute(
            "SELECT availability,source_fingerprint,stale FROM model_routes "
            "WHERE harness='deepseek' AND selector='deepseek-v4-pro'"
        ).fetchone()
        self.assertIsNone(row)
        refused = routes_cli.resolve(
            self.con,
            "deepseek",
            "deepseek-v4-pro",
            effort="default",
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "thinking_evidence_missing")
        self.assertIsNone(refused.get("binding"))

    def test_reordered_catalogue_keeps_explicit_authenticated_route_current(self):
        rows = [
            {"id": model}
            for model in ("model-a", "model-b", "model-c", "model-d", "wanted-model")
        ]

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": rows}

        arguments = {
            "fetch": fetch,
            "env": {"OLLAMA_API_KEY": "ollama-secret"},
            "run": None,
            "deepseek_wire_probe": deepseek_wire_proof,
        }
        status = self.verification("deepseek", "0.1.0rc7")[
            "verification"
        ]["harnesses"]["deepseek"]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            mc, "CACHE", Path(raw) / "model-catalog.json"
        ):
            admitted = mc.ensure_deepseek_route(
                self.con,
                "ollama-cloud/wanted-model",
                **arguments,
                opencode_provider=lambda: [],
                harness_probe=lambda: {"deepseek": status},
            )
        self.assertIsNotNone(admitted)
        self.assertEqual(admitted["provider_model"], "wanted-model")

        rows.reverse()
        background = mc.build(**arguments)
        background.update(self.verification("deepseek", "0.1.0rc7"))
        mc.persist_routes(self.con, background)

        route = self.con.execute(
            "SELECT stale,last_error,generation_id FROM model_routes "
            "WHERE harness='deepseek' AND selector='ollama-cloud/wanted-model'"
        ).fetchone()
        generation = self.con.execute(
            "SELECT generation_id FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(route)[:2], (0, None))
        self.assertEqual(route["generation_id"], generation["generation_id"])

    def test_attempted_failed_deepseek_route_is_not_carried_current(self):
        rows = [
            {"id": model}
            for model in ("model-a", "model-b", "model-c", "model-d", "wanted-model")
        ]

        def fetch(url, headers=None):
            if url == mc.MODELS_DEV_URL:
                return MODELS_DEV
            return {"data": rows}

        environment = {"OLLAMA_API_KEY": "ollama-secret"}
        admitted = mc.build(
            fetch=fetch,
            env=environment,
            run=None,
            deepseek_wire_probe=deepseek_wire_proof,
            deepseek_selector="ollama-cloud/model-a",
        )
        admitted.update(self.verification("deepseek", "0.1.0rc7"))
        mc.persist_routes(self.con, admitted)

        proof_calls = []

        def selective_failure(provider, model, options_by_effort, env=None):
            proof_calls.append(model)
            if model == "model-a":
                raise RuntimeError("exact route wire proof failed")
            return deepseek_wire_proof(provider, model, options_by_effort, env)

        refreshed = mc.build(
            fetch=fetch,
            env=environment,
            run=None,
            deepseek_wire_probe=selective_failure,
        )
        refreshed.update(self.verification("deepseek", "0.1.0rc7"))
        mc.persist_routes(self.con, refreshed)

        current_generation = self.con.execute(
            "SELECT generation_id FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()["generation_id"]
        routes = self.con.execute(
            "SELECT selector,stale,last_error,generation_id FROM model_routes "
            "WHERE harness='deepseek' ORDER BY selector"
        ).fetchall()
        self.assertEqual(proof_calls, ["model-a", "model-b", "model-c", "model-d"])
        self.assertEqual(
            [tuple(route) for route in routes],
            [
                (
                    "ollama-cloud/model-a",
                    1,
                    "DeepSeek provider-option mapper has no outbound wire proof",
                    admitted["catalogue_generation"],
                ),
                ("ollama-cloud/model-b", 0, None, current_generation),
                ("ollama-cloud/model-c", 0, None, current_generation),
                ("ollama-cloud/model-d", 0, None, current_generation),
            ],
        )
        self.assertFalse(any(
            route["selector"].endswith("wanted-model") for route in routes
        ))

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

    def test_partial_opencode_failure_preserves_other_harness_routes(self):
        claude = self.verification("claude", "2.1.222")
        opencode = self.verification("opencode", "1.18.18")
        statuses = {
            "claude": claude["verification"]["harnesses"]["claude"],
            "opencode": opencode["verification"]["harnesses"]["opencode"],
        }
        initial = {
            "fetched_at": "2026-08-20T00:00:00+00:00",
            "stale": False,
            "harnesses": {
                "claude": {"models": [mc._entry(
                    "opus", source="claude-cli", availability="available",
                    cli_version="claude 2.1.222",
                )]},
                "opencode": {"models": [mc._entry(
                    "halo/qwen", source="opencode-provider-api",
                    availability="available", cli_version="opencode 1.18.18",
                )]},
            },
            "verification": {"runtime": "host", "harnesses": statuses},
        }
        mc.persist_routes(self.con, initial)

        partial = {
            **initial,
            "fetched_at": "2026-08-20T00:01:00+00:00",
            "partial": True,
            "errors": ["opencode-provider-api: sidecar unavailable"],
            "harnesses": {
                "claude": initial["harnesses"]["claude"],
                "opencode": {
                    "models": [],
                    "error": "HARNESS_UNAVAILABLE: sidecar unavailable",
                },
            },
        }
        mc.persist_routes(self.con, partial)

        rows = self.con.execute(
            "SELECT harness,selector,stale,last_error FROM model_routes "
            "ORDER BY harness"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("claude", "opus", 0, None),
                ("opencode", "halo/qwen", 1,
                 "HARNESS_UNAVAILABLE: sidecar unavailable"),
            ],
        )
        generation = self.con.execute(
            "SELECT state,error_summary FROM model_catalog_generations "
            "ORDER BY completed_at DESC,generation_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(generation["state"], "successful")
        self.assertEqual(
            json.loads(generation["error_summary"])["errors"],
            ["opencode-provider-api: sidecar unavailable"],
        )

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
        observation = controlled_bundle(
            "codex", "gpt-5.6-sol",
            runtime_status("0.145.0", harness="codex"), fingerprint,
        )
        with mock.patch.object(
            mc, "controlled_route_evidence", return_value=observation
        ) as collector:
            got = routes_cli.resolve(
                self.con, "codex", "gpt-5.6-sol", shell="DEV3"
            )
        collector.assert_called_once_with("codex", "gpt-5.6-sol")
        self.assertTrue(got["ok"])
        self.assertEqual(
            got["command"],
            ["./sc", "run", "DEV3", "--harness", "codex", "-m",
             "gpt-5.6-sol", "--effort", "high"])

    def test_empty_effort_list_alias_admits_only_model_default(self):
        # Spec #160 + decision #223: an alias that declares no effort list
        # persists supported_efforts=[]; the reserved 'default' still binds
        # (no effort transport), omitted effort falls back to it through the
        # bind-time chain, and every explicit named level keeps failing
        # membership.
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
        observation = controlled_bundle(
            "kimi", "kimi-code/legacy",
            runtime_status("0.33.0", harness="kimi"), fingerprint,
        )
        with mock.patch.object(
            mc, "controlled_route_evidence", return_value=observation
        ):
            bound = routes_cli.resolve(
                self.con, "kimi", "kimi-code/legacy", effort="default",
                shell="DEV3",
            )
            named = routes_cli.resolve(
                self.con, "kimi", "kimi-code/legacy", effort="high"
            )
            omitted = routes_cli.resolve(
                self.con, "kimi", "kimi-code/legacy"
            )
        self.assertTrue(bound["ok"])
        binding = bound["binding"]
        self.assertEqual(binding["control_state"], "controlled")
        self.assertEqual(binding["requested_effort"], "default")
        self.assertEqual(binding["effective_effort"], "default")
        self.assertIsNone(binding["evidence_digest"])
        self.assertIsNone(binding["native_variant_id"])
        self.assertEqual(
            bound["command"],
            ["./sc", "run", "DEV3", "--harness", "kimi", "-m",
             "kimi-code/legacy", "--effort", "default"])
        self.assertEqual(
            route_bindings.digest_json(binding), bound["binding_digest"])
        # Decision #223: the omitted effort resolves to the reserved Model
        # default with the exact identity of an explicit 'default'.
        self.assertTrue(omitted["ok"])
        self.assertEqual(omitted["binding"], binding)
        self.assertEqual(omitted["binding_digest"], bound["binding_digest"])
        self.assertFalse(named["ok"])
        self.assertEqual(named["code"], "unsupported_thinking_level")
        self.assertEqual(named["details"]["default_effort"], "default")
        self.assertEqual(named["details"]["requested_effort"], "high")

    def test_default_binds_alongside_advertised_named_efforts(self):
        fresh = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False,
                 "harnesses": {"codex": {"models": [mc._entry(
                     "gpt-5.6-sol", source="codex-cache",
                     availability="available", supported_efforts=["low", "high"],
                     cli_version="codex-cli 0.145.0")]}},
                 **self.verification("codex", "0.145.0")}
        mc.persist_routes(self.con, fresh)
        fingerprint = self.con.execute(
            "SELECT source_fingerprint FROM model_routes WHERE harness='codex' "
            "AND selector='gpt-5.6-sol'"
        ).fetchone()[0]
        observation = controlled_bundle(
            "codex", "gpt-5.6-sol",
            runtime_status("0.145.0", harness="codex"), fingerprint,
        )
        with mock.patch.object(
            mc, "controlled_route_evidence", return_value=observation
        ):
            bound_default = routes_cli.resolve(
                self.con, "codex", "gpt-5.6-sol", effort=" DEFAULT "
            )
            bound_named = routes_cli.resolve(
                self.con, "codex", "gpt-5.6-sol", effort="high"
            )
        self.assertTrue(bound_default["ok"])
        # Trimmed and case-folded like any other effort; the digest covers the
        # literal 'default' and is distinct from the named-effort binding.
        self.assertEqual(
            bound_default["binding"]["requested_effort"], "default")
        self.assertTrue(bound_named["ok"])
        self.assertEqual(bound_named["binding"]["requested_effort"], "high")
        self.assertNotEqual(
            bound_default["binding_digest"], bound_named["binding_digest"])


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

    def test_report_projects_best_effort_route_as_runnable(self):
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
            "harnesses_ready": 3,
            "exact_routes": 4,
            "exact_routes_runnable": 2,
            "harness_defaults": 1,
        })
        by_key = {
            (row["flavor"], row["harness"]): row
            for row in report["defaults"]
        }
        self.assertEqual(by_key[("dev", "codex")], {
            "flavor": "dev", "harness": "codex", "model": "gpt-ready",
            "is_default": True, "state": "runnable", "runnable": True,
            "reason": None,
        })
        self.assertEqual(by_key[("planner", "codex")]["state"],
                         "route-missing")
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
            "harnesses_ready": 3,
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
        "harness_support_state": "tested",
        "supported_efforts": '["high"]',
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": "1" * 32,
        "evidence_kind": "codex-model-cache",
        "source_fingerprint": "3" * 64,
        "effort_metadata": json.dumps({
            "supported": ["high"], "default": "high",
            "digests": {"high": "2" * 64}, "native_variant_ids": {},
        }),
        "selector_binding": '{"kind":"exact-model","selector":"api-model"}',
        "adapter_metadata": "{}",
    }

    @classmethod
    def controlled_route(cls, harness: str) -> dict:
        versions = {
            "claude": "2.1.222", "codex": "0.145.0",
            "kimi": "0.33.0", "opencode": "1.18.9",
        }
        evidence = {
            "claude": "claude-portable-manifest",
            "codex": "codex-model-cache",
            "kimi": "kimi-alias-config",
            "opencode": "opencode-connected-variant",
        }
        row = {
            **cls.ROUTE,
            "harness": harness,
            "cli_version": f"{harness} {versions[harness]}",
            "harness_version": versions[harness],
            "evidence_kind": evidence[harness],
        }
        if harness == "opencode":
            row["effort_metadata"] = json.dumps({
                "supported": ["high"], "default": "high",
                "digests": {"high": "2" * 64},
                "native_variant_ids": {"high": "high"},
                "adapter_metadata_by_effort": {
                    "high": {
                        "compatibility_manifest": "opencode-1.18.9-v1",
                        "provider_family": "openai-ai-sdk",
                        "variant_options": {"reasoningEffort": "high"},
                    },
                },
            })
            row["adapter_metadata"] = json.dumps({
                "compatibility_manifest": "opencode-1.18.9-v1",
                "provider_family": "openai-ai-sdk",
                "variant_options_by_effort": {
                    "high": {"reasoningEffort": "high"},
                },
            })
        return row

    def authenticated_controlled(self, harness: str, row: dict, *,
                                 status: dict, fingerprint: str) -> tuple[int, dict]:
        output = io.StringIO()
        with (
            mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
            mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
            mock.patch.object(
                routes_cli.mem, "_api", return_value={"routes": [row]}
            ),
            mock.patch.object(
                routes_cli.model_catalog, "controlled_route_evidence",
                return_value=controlled_bundle(
                    harness, "api-model", status, fingerprint
                ),
            ) as source_probe,
            mock.patch.object(
                routes_cli, "_open_db", side_effect=AssertionError("opened DB")
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = routes_cli.main([
                "resolve", harness, "api-model", "--json",
            ])
        source_probe.assert_called_once_with(harness, "api-model")
        return exit_code, json.loads(output.getvalue())

    def test_authenticated_controlled_routes_use_execution_seat_proof(self):
        sandbox = {
            "runtime": "sandbox", "runtime_identity": "sandbox:test-image",
        }
        versions = {
            "claude": "2.1.222", "codex": "0.145.0",
            "kimi": "0.33.0", "opencode": "1.18.9",
        }
        for harness, version in versions.items():
            row = self.controlled_route(harness)
            good = runtime_status(
                version, harness=harness, scope=sandbox
            )
            with self.subTest(harness=harness, case="matching-sandbox"):
                exit_code, result = self.authenticated_controlled(
                    harness, row, status=good, fingerprint="3" * 64,
                )
                self.assertEqual(exit_code, 0, result)
                self.assertTrue(result["ok"])
                self.assertEqual(result["binding"]["harness"], harness)
            with self.subTest(harness=harness, case="source-mismatch"):
                exit_code, result = self.authenticated_controlled(
                    harness, row, status=good, fingerprint="9" * 64,
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["code"], "thinking_evidence_stale")
                self.assertNotIn("binding", result)
                self.assertNotIn("binding_digest", result)
                self.assertNotIn("command", result)

        codex = self.controlled_route("codex")
        cases = {
            "container-missing": runtime_status(
                None, compatibility=None, error="HARNESS_UNAVAILABLE",
                harness="codex", scope=sandbox,
            ),
            "container-version-mismatch": runtime_status(
                "0.146.0", harness="codex", scope=sandbox,
            ),
        }
        for name, status in cases.items():
            with self.subTest(case=name):
                exit_code, result = self.authenticated_controlled(
                    "codex", codex, status=status, fingerprint="3" * 64,
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["code"], "thinking_evidence_stale")
                self.assertNotIn("binding", result)
                self.assertNotIn("binding_digest", result)
                self.assertNotIn("command", result)

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
            mock.patch.object(
                routes_cli.model_catalog, "controlled_route_evidence",
                return_value=controlled_bundle(
                    "codex", "api-model",
                    runtime_status("0.145.0", harness="codex"), "3" * 64,
                ),
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

    def test_authenticated_resolve_normalizes_harness_before_lookup(self):
        api = mock.Mock(return_value={"routes": [self.ROUTE]})

        def resolve(harness: str) -> dict:
            output = io.StringIO()
            with (
                mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
                mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
                mock.patch.object(routes_cli.mem, "_api", api),
                mock.patch.object(
                    routes_cli, "_open_db", side_effect=AssertionError("opened DB")
                ),
                mock.patch.object(
                    routes_cli.model_catalog, "controlled_route_evidence",
                    return_value=controlled_bundle(
                        "codex", "api-model",
                        runtime_status("0.145.0", harness="codex"), "3" * 64,
                    ),
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(routes_cli.main([
                    "resolve", harness, "api-model", "--json",
                ]), 0)
            return json.loads(output.getvalue())

        mixed = resolve("Codex")
        lower = resolve("codex")

        self.assertEqual(api.call_args_list, [
            mock.call(
                "GET", "/_sc/model-routes?harness=codex&selector=api-model"
            ),
            mock.call(
                "GET", "/_sc/model-routes?harness=codex&selector=api-model"
            ),
        ])
        self.assertEqual(mixed["binding"], lower["binding"])
        self.assertEqual(mixed["binding_digest"], lower["binding_digest"])

    def test_local_resolve_rejects_unknown_harness_and_accepts_mixed_case(self):
        def resolve(harness: str) -> tuple[int, dict, mock.Mock]:
            output = io.StringIO()
            con = mock.Mock()
            with (
                mock.patch.object(routes_cli.mem, "SC_API_TOKEN", ""),
                mock.patch.object(routes_cli, "_open_db", return_value=con),
                contextlib.redirect_stdout(output),
            ):
                status = routes_cli.main(["resolve", harness, "--json"])
            return status, json.loads(output.getvalue()), con

        with mock.patch.object(
            routes_cli.model_catalog, "harness_runtime_status",
            return_value=runtime_status("2.1.223", harness="claude"),
        ):
            refused_status, refused, refused_con = resolve("not-a-harness")
            accepted_status, accepted, accepted_con = resolve("ClAuDe")

        self.assertEqual(refused_status, 2)
        self.assertEqual(refused["code"], "unsupported_thinking_level")
        self.assertEqual(refused["error"], "Harness is not supported")
        self.assertNotIn("binding", refused)
        self.assertNotIn("binding_digest", refused)
        self.assertNotIn("command", refused)
        refused_con.close.assert_called_once_with()
        self.assertEqual(accepted_status, 0)
        self.assertEqual(accepted["harness"], "claude")
        self.assertEqual(accepted["binding"]["harness"], "claude")
        self.assertEqual(
            accepted["binding"]["control_state"], "harness-default"
        )
        self.assertEqual(
            accepted["command"],
            ["./sc", "run", "<shell>", "--harness", "claude"],
        )
        accepted_con.close.assert_called_once_with()

    def test_local_uncontrolled_resolve_requires_runtime_but_not_version_range(self):
        con = mock.Mock()
        with mock.patch.object(
            routes_cli.model_catalog, "harness_runtime_status",
            return_value=runtime_status(
                version=None, compatibility=None, error="HARNESS_UNAVAILABLE",
                harness="claude",
            ),
        ):
            unavailable_harness = routes_cli.resolve(con, "claude")
        with mock.patch.object(
            routes_cli.model_catalog, "harness_runtime_status",
            return_value=runtime_status(
                version="3.0.0", compatibility="newer-unverified"
            ),
        ):
            incompatible_vibe = routes_cli.resolve(
                con, "vibe", "devstral-latest"
            )

        self.assertFalse(unavailable_harness["ok"])
        self.assertEqual(unavailable_harness["code"], "thinking_evidence_missing")
        self.assertNotIn("binding", unavailable_harness)
        self.assertNotIn("binding_digest", unavailable_harness)
        self.assertNotIn("command", unavailable_harness)
        self.assertIn("no compatible installed runtime", unavailable_harness["error"])
        self.assertTrue(incompatible_vibe["ok"])
        self.assertEqual(
            incompatible_vibe["binding"]["control_state"], "native-uncontrolled"
        )

    def test_local_ready_uncontrolled_routes_keep_typed_null_identity(self):
        con = mock.Mock()
        with mock.patch.object(
            routes_cli.model_catalog, "harness_runtime_status",
            side_effect=lambda harness: runtime_status(
                "2.1.223", harness="claude"
            ) if harness == "claude" else runtime_status(harness="vibe"),
        ):
            default_first = routes_cli.resolve(con, "claude")
            default_replay = routes_cli.resolve(con, "claude")
            vibe_first = routes_cli.resolve(con, "vibe", "vibe-local")
            vibe_replay = routes_cli.resolve(con, "vibe", "vibe-local")

        for result, state in (
            (default_first, "harness-default"),
            (vibe_first, "native-uncontrolled"),
        ):
            binding = result["binding"]
            self.assertTrue(result["ok"])
            self.assertEqual(binding["control_state"], state)
            self.assertIsNone(binding["requested_effort"])
            self.assertIsNone(binding["effective_effort"])
            self.assertIsNone(binding["catalogue_generation"])
            self.assertIsNone(binding["evidence_digest"])
            self.assertNotIn("--effort", result["command"])
        self.assertEqual(
            default_first["binding_digest"], default_replay["binding_digest"]
        )
        self.assertEqual(
            vibe_first["binding_digest"], vibe_replay["binding_digest"]
        )

    def test_authenticated_uncontrolled_resolve_requires_runtime_not_version_range(self):
        cases = (
            (
                ["resolve", "claude", "--json"],
                {"routes": [], "runtime_status": runtime_status(
                    "2.1.223", harness="claude"
                )},
                runtime_status(
                    version=None, compatibility=None,
                    error="HARNESS_UNAVAILABLE",
                    harness="claude",
                ),
                "/_sc/model-routes?harness=claude",
            ),
            (
                ["resolve", "vibe", "devstral-latest", "--json"],
                {"routes": [], "runtime_status": runtime_status()},
                runtime_status(
                    version="3.0.0", compatibility="newer-unverified"
                ),
                "/_sc/model-routes?harness=vibe&selector=devstral-latest",
            ),
        )
        for argv, projection, local_status, path in cases:
            with self.subTest(argv=argv):
                output = io.StringIO()
                api = mock.Mock(return_value=projection)
                with (
                    mock.patch.object(
                        routes_cli.mem, "SC_API_TOKEN", "shell-token"
                    ),
                    mock.patch.object(
                        routes_cli.mem, "SC_API_BASE", "http://engine"
                    ),
                    mock.patch.object(routes_cli.mem, "_api", api),
                    mock.patch.object(
                        routes_cli.model_catalog, "harness_runtime_status",
                        return_value=local_status,
                    ),
                    mock.patch.object(
                        routes_cli, "_open_db",
                        side_effect=AssertionError("opened DB"),
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    status = routes_cli.main(argv)
                result = json.loads(output.getvalue())
                accepted = local_status.get("error") is None
                self.assertEqual(status, 0 if accepted else 2)
                self.assertEqual(result["ok"], accepted)
                if accepted:
                    self.assertEqual(
                        result["binding"]["control_state"], "native-uncontrolled"
                    )
                else:
                    self.assertEqual(result["code"], "thinking_evidence_missing")
                    self.assertNotIn("binding", result)
                api.assert_called_once_with("GET", path)

    def test_authenticated_ready_uncontrolled_binding_is_stable(self):
        projections = {
            "/_sc/model-routes?harness=claude": {
                "routes": [],
            },
            "/_sc/model-routes?harness=vibe&selector=vibe-local": {
                "routes": [],
            },
        }

        def resolve(argv):
            output = io.StringIO()
            with (
                mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
                mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
                mock.patch.object(
                    routes_cli.mem, "_api",
                    side_effect=lambda _method, path: projections[path],
                ),
                mock.patch.object(
                    routes_cli.model_catalog, "harness_runtime_status",
                    side_effect=lambda harness: runtime_status(
                        "2.1.223", harness="claude"
                    ) if harness == "claude" else runtime_status(harness="vibe"),
                ),
                mock.patch.object(
                    routes_cli, "_open_db", side_effect=AssertionError("opened DB")
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(routes_cli.main(argv), 0)
            return json.loads(output.getvalue())

        for argv in (
            ["resolve", "claude", "--json"],
            ["resolve", "vibe", "vibe-local", "--json"],
        ):
            with self.subTest(argv=argv):
                first = resolve(argv)
                replay = resolve(argv)
                self.assertEqual(first["binding_digest"], replay["binding_digest"])
                self.assertIsNone(first["binding"]["requested_effort"])
                self.assertIsNone(first["binding"]["effective_effort"])
                self.assertIsNone(first["binding"]["catalogue_generation"])
                self.assertIsNone(first["binding"]["evidence_digest"])
                self.assertNotIn("--effort", first["command"])

    def test_authenticated_resolution_uses_the_shell_execution_runtime(self):
        host = {"runtime": "host", "runtime_identity": "host:api-host"}
        sandbox = {
            "runtime": "sandbox", "runtime_identity": "sandbox:shell-image",
        }
        host_ready = runtime_status(scope=host)
        host_missing = runtime_status(
            version=None, compatibility=None, error="HARNESS_UNAVAILABLE",
            scope=host,
        )
        sandbox_ready = runtime_status(scope=sandbox)
        sandbox_missing = runtime_status(
            version=None, compatibility=None, error="HARNESS_UNAVAILABLE",
            scope=sandbox,
        )
        sandbox_incompatible = runtime_status(
            version="2.23.0", compatibility="newer-unverified", scope=sandbox,
        )
        cases = (
            ("host-ready-container-missing", host_ready, sandbox_missing,
             sandbox, False),
            ("host-missing-container-ready", host_missing, sandbox_ready,
             sandbox, True),
            ("best-effort-image-version", host_ready, sandbox_incompatible,
             sandbox, True),
            ("matching-bare-metal", host_ready, host_ready, host, True),
        )

        for name, api_status, shell_status, shell_scope, accepted in cases:
            with self.subTest(name=name):
                output = io.StringIO()
                api = mock.Mock(return_value={
                    "routes": [],
                    # A legacy/malicious server value must never be admission
                    # evidence for the shell's execution runtime.
                    "runtime_status": api_status,
                })
                local_probe = mock.Mock(return_value=shell_status)
                with (
                    mock.patch.object(routes_cli.mem, "SC_API_TOKEN", "shell-token"),
                    mock.patch.object(routes_cli.mem, "SC_API_BASE", "http://engine"),
                    mock.patch.object(routes_cli.mem, "_api", api),
                    mock.patch.object(
                        routes_cli.model_catalog, "harness_runtime_status",
                        local_probe,
                    ),
                    mock.patch.object(
                        routes_cli.model_catalog.harness_versions,
                        "runtime_scope", return_value=shell_scope,
                    ),
                    mock.patch.object(
                        routes_cli, "_open_db",
                        side_effect=AssertionError("opened DB"),
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    exit_code = routes_cli.main([
                        "resolve", "vibe", "devstral-latest", "--json",
                    ])
                result = json.loads(output.getvalue())

                self.assertEqual(exit_code, 0 if accepted else 2)
                self.assertEqual(result["ok"], accepted)
                if accepted:
                    self.assertEqual(
                        result["binding"]["control_state"],
                        "native-uncontrolled",
                    )
                    self.assertIsNone(result["binding"]["requested_effort"])
                    self.assertNotIn("--effort", result["command"])
                else:
                    self.assertEqual(result["code"], "thinking_evidence_missing")
                    self.assertNotIn("binding", result)
                    self.assertNotIn("binding_digest", result)
                    self.assertNotIn("command", result)
                local_probe.assert_called_once_with("vibe")
                api.assert_called_once_with(
                    "GET",
                    "/_sc/model-routes?harness=vibe&selector=devstral-latest",
                )

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

    def test_opencode_overlay_failure_does_not_make_fresh_catalog_stale(self):
        with mock.patch.object(
            mc.shutil, "which",
            side_effect=lambda name: (
                "/usr/bin/opencode" if name == "opencode" else None
            ),
        ):
            got = mc.catalog(
                fetch=fetch_ok,
                env={},
                run=None,
                opencode_provider=mock.Mock(
                    side_effect=RuntimeError("sidecar unavailable")
                ),
            )
            cached = mc.catalog(
                fetch=fetch_down,
                env={},
                run=None,
                opencode_provider=mock.Mock(
                    side_effect=RuntimeError("sidecar still unavailable")
                ),
            )

        self.assertTrue(got["partial"])
        self.assertFalse(got["stale"])
        self.assertIn("claude-opus-4-8", ids(got["harnesses"]["claude"]))
        self.assertEqual(got["harnesses"]["opencode"]["models"], [])
        self.assertEqual(
            got["harnesses"]["opencode"]["error"],
            "sidecar unavailable",
        )
        self.assertTrue(cached["partial"])
        self.assertFalse(cached["stale"])
        self.assertIn("claude-opus-4-8", ids(cached["harnesses"]["claude"]))
        self.assertEqual(
            cached["harnesses"]["opencode"]["error"],
            "sidecar still unavailable",
        )

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
