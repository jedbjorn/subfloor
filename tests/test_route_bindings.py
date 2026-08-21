#!/usr/bin/env python3
"""Focused Feature #54 binding, evidence-generation, and revision contract."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import model_catalog  # noqa: E402
import models as routes_cli  # noqa: E402
import route_bindings  # noqa: E402
import harness_versions  # noqa: E402
import server as api_server  # noqa: E402


def compatible_runtime(version: str = "2.22.0", *, harness: str | None = None,
                       scope: dict | None = None) -> dict:
    harness = harness or ("claude" if version.startswith("2.1.") else "vibe")
    ranges = {
        "claude": ("2.1.220", "2.2.0", "2.1.222"),
        "codex": ("0.145.0", "0.147.0", "0.145.0"),
        "deepseek": (None, None, "0.1.0rc7"),
        "kimi": ("0.30.0", "0.34.0", "0.33.0"),
        "opencode": ("1.18.9", "1.19.0", "1.18.9"),
        "vibe": ("2.22.0", "2.23.0", "2.22.0"),
    }
    minimum, maximum, verified = ranges[harness]
    scope = scope or route_bindings.harness_versions.runtime_scope()
    return {
        "harness": harness,
        **scope,
        "version": version,
        "compatibility": "verified" if version == verified else "supported",
        "minimum_version": minimum,
        "maximum_version_exclusive": maximum,
        "verified_version": verified,
        "error": None,
    }


def controlled_observation(
    fingerprint: str | None = "2" * 64,
    *,
    harness: str = "codex",
    version: str | None = None,
    scope: dict | None = None,
) -> dict:
    versions = {
        "claude": "2.1.222", "codex": "0.145.0",
        "deepseek": "0.1.0rc7", "kimi": "0.33.0", "opencode": "1.18.9",
    }
    scope = scope or route_bindings.harness_versions.runtime_scope()
    status = compatible_runtime(
        version or versions[harness], harness=harness, scope=scope
    )
    return {
        "runtime_status": status,
        "runtime_scope": scope,
        "source_fingerprint": fingerprint,
    }


def resolve_controlled_v2(
    row: dict,
    harness: str,
    selector: str,
    effort: str | None = None,
    *,
    fingerprint: str | None = "2" * 64,
    version: str | None = None,
    scope: dict | None = None,
    **kwargs,
) -> tuple[dict, str]:
    normalized = harness.strip().lower()
    observation = controlled_observation(
        fingerprint, harness=normalized, version=version, scope=scope,
    )
    with mock.patch.object(
        model_catalog, "controlled_route_evidence", return_value=observation
    ) as collector:
        result = route_bindings.resolve_v2(
            row, harness, selector, effort, **kwargs
        )
    collector.assert_called_once_with(normalized, selector)
    return result


def route_schema(path: str | Path = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript((
        ROOT / ".super-coder" / "migrations" / "0075_model_routes.sql"
    ).read_text())
    con.executescript(
        "CREATE TABLE sprints ("
        "sprint_id INTEGER PRIMARY KEY,lifecycle TEXT NOT NULL);"
        "CREATE TABLE sprint_participants ("
        "participant_id INTEGER PRIMARY KEY,sprint_id INTEGER NOT NULL "
        "REFERENCES sprints(sprint_id));"
    )
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0212_route_binding_foundation.sql"
    ).read_text())
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0216_sprint_binding_provenance.sql"
    ).read_text())
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0217_harness_support_metadata.sql"
    ).read_text())
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0218_sprint_binding_support_provenance.sql"
    ).read_text())
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0223_model_default_effort_binding.sql"
    ).read_text())
    con.executescript((
        ROOT / ".super-coder" / "migrations" /
        "0227_deepseek_controlled_route_binding.sql"
    ).read_text())
    return con


def resolve_controlled(
    con,
    harness: str,
    selector: str,
    *,
    fingerprint: str | None,
    version: str | None = None,
    scope: dict | None = None,
    **kwargs,
) -> dict:
    normalized = harness.strip().lower()
    observation = controlled_observation(
        fingerprint, harness=normalized, version=version, scope=scope,
    )
    with mock.patch.object(
        model_catalog, "controlled_route_evidence", return_value=observation
    ) as collector:
        result = routes_cli.resolve(con, harness, selector, **kwargs)
    collector.assert_called_once_with(normalized, selector)
    return result


class BindingIdentityTest(unittest.TestCase):
    NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)

    def test_uncontrolled_binding_requires_exact_raw_runtime_identity(self):
        binding = route_bindings._uncontrolled_binding("vibe", "devstral", None)
        scope = harness_versions.runtime_scope()
        runtime = {
            "harness": "vibe",
            **scope,
            "version": "2.22.0",
            "observed_version": "vibe 2.22.0-dev",
            "compatibility": "prerelease-unverified",
            "error": None,
        }
        with mock.patch.object(model_catalog, "harness_runtime_status", return_value=runtime):
            with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                route_bindings.verify_stored_v2_before_first_turn(
                    None,
                    binding,
                    source_fingerprint=None,
                    harness_version="2.22.0",
                )
        self.assertEqual(raised.exception.code, "route_evidence_stale")

    @staticmethod
    def controlled_row(**overrides) -> dict:
        base = {
            "harness": "codex",
            "selector": "gpt-test",
            "provider_model": "gpt-test",
            "source": "codex-cache",
            "availability": "available",
            "headless_supported": 1,
            "stale": 0,
            "last_error": None,
            "last_seen_at": "2026-08-16T19:00:00+00:00",
            "generation_id": "1" * 32,
            "evidence_kind": "codex-model-cache",
            "source_fingerprint": "2" * 64,
            "cli_version": "codex-cli 0.145.0",
            "harness_version": "0.145.0",
            "harness_compatibility": "verified",
            "harness_support_state": "tested",
            "supported_efforts": '["low","high"]',
            "effort_metadata": json.dumps({
                "supported": ["low", "high"],
                "default": "high",
                "digests": {"low": "3" * 64, "high": "4" * 64},
                "native_variant_ids": {},
            }),
            "selector_binding": '{"kind":"exact-model","selector":"gpt-test"}',
            "adapter_metadata": "{}",
        }
        return {**base, **overrides}

    @classmethod
    def opencode_row(cls) -> dict:
        return cls.controlled_row(
            harness="opencode",
            selector="provider/model",
            provider_model="provider/model",
            source="opencode-provider-api",
            evidence_kind="opencode-connected-variant",
            cli_version="opencode 1.18.9",
            harness_version="1.18.9",
            supported_efforts='["k"]',
            effort_metadata=json.dumps({
                "supported": ["k"],
                "default": "k",
                "digests": {"k": "5" * 64},
                "native_variant_ids": {"k": "k"},
                "adapter_metadata_by_effort": {
                    "k": {
                        "compatibility_manifest": "opencode-1.18.9-v1",
                        "provider_family": "openai-ai-sdk",
                        "variant_options": {"reasoningEffort": "high"},
                    },
                },
            }),
            selector_binding=json.dumps({
                "kind": "exact-model", "selector": "provider/model",
            }),
            adapter_metadata=json.dumps({
                "compatibility_manifest": "opencode-1.18.9-v1",
                "provider_family": "openai-ai-sdk",
                "variant_options_by_effort": {
                    "k": {"reasoningEffort": "high"},
                },
            }),
        )

    @classmethod
    def deepseek_row(cls) -> dict:
        identity = {
            "provider_adapter_id": "deepseek-native-v1",
            "provider_adapter_digest": "1" * 64,
            "provider_registry_sha256": "2" * 64,
            "credential_kind": "deepseek-api-key",
            "endpoint_identity": "https://api.deepseek.com",
            "discovery_evidence_digest": "3" * 64,
            "runtime_version": "0.1.0rc7",
            "source_commit": "b" * 40,
            "patch_sha256": "7" * 64,
            "composition_sha256": "8" * 64,
        }
        selected = {
            "default": {
                "provider_route": "deepseek-official",
                **identity,
                "transport_contract": "deepseek-provider-options-v1",
                "wire_evidence_digest": "5" * 64,
                "provider_options": {
                    "omit": ["thinking", "reasoning_effort"], "set": {},
                },
            },
            "high": {
                "provider_route": "deepseek-official",
                **identity,
                "transport_contract": "deepseek-provider-options-v1",
                "wire_evidence_digest": "6" * 64,
                "provider_options": {
                    "omit": [],
                    "set": {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "high",
                    },
                },
            },
        }
        return cls.controlled_row(
            harness="deepseek",
            selector="deepseek-v4-pro",
            provider_model="deepseek-v4-pro",
            source="deepseek-provider-api",
            evidence_kind="deepseek-authenticated-models",
            availability="available",
            headless_supported=0,
            cli_version="0.1.0rc7",
            harness_version="0.1.0rc7",
            supported_efforts='["high"]',
            effort_metadata=json.dumps({
                "supported": ["high"],
                "default": "high",
                "digests": {"high": "4" * 64},
                "native_variant_ids": {},
                "adapter_metadata_by_effort": selected,
            }),
            selector_binding=json.dumps({
                "kind": "authenticated-provider-model",
                "selector": "deepseek-v4-pro",
                "provider_route": "deepseek-official",
                "models_url": "https://api.deepseek.com/models",
                "runtime_source_commit": "bb4ca698d63714e753f5621b07400e6ebb0b5d97",
            }),
            adapter_metadata=json.dumps({
                "provider_route": "deepseek-official",
                "transport_contract": "deepseek-provider-options-v1",
                "provider_options_by_effort": {
                    key: value["provider_options"] for key, value in selected.items()
                },
                "wire_contract": "deepseek-provider-options-wire-v1",
                "wire_evidence_by_effort": {
                    "default": "5" * 64,
                    "high": "6" * 64,
                },
            }),
        )


    def test_controlled_omitted_and_explicit_high_have_same_fixed_identity(self):
        implicit, implicit_digest = resolve_controlled_v2(
            self.controlled_row(), "Codex", "gpt-test", now=self.NOW,
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )
        explicit, explicit_digest = resolve_controlled_v2(
            self.controlled_row(), "codex", "gpt-test", " HIGH ", now=self.NOW,
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )

        self.assertEqual(tuple(implicit), route_bindings.BINDING_KEYS)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit_digest, explicit_digest)
        self.assertEqual(implicit["requested_effort"], "high")
        self.assertEqual(implicit["evidence_digest"], "4" * 64)
        self.assertEqual(implicit["control_state"], "controlled")

    def test_controlled_omitted_effort_falls_back_to_model_default(self):
        # Decision #223: omitted effort on a controlled exact route resolves
        # high where advertised, else the reserved Model default — so a route
        # without high still binds instead of raising unsupported_thinking_level.
        runtime = compatible_runtime("0.145.0", harness="codex")
        no_high = self.controlled_row(
            supported_efforts='["low","medium"]',
            effort_metadata=json.dumps({
                "supported": ["low", "medium"],
                "default": None,
                "digests": {"low": "3" * 64, "medium": "6" * 64},
                "native_variant_ids": {},
            }),
        )
        binding, _ = resolve_controlled_v2(
            no_high, "codex", "gpt-test", now=self.NOW,
            runtime_status=runtime,
        )
        self.assertEqual(binding["requested_effort"], "default")
        self.assertEqual(binding["effective_effort"], "default")
        self.assertIsNone(binding["evidence_digest"])
        self.assertIsNone(binding["native_variant_id"])

        # A no-thinking route binds through the same chain with the same
        # identity as an explicit Model default.
        empty = self.controlled_row(
            supported_efforts="[]",
            effort_metadata=json.dumps({
                "supported": [], "default": None,
                "digests": {}, "native_variant_ids": {},
            }),
        )
        omitted, omitted_digest = resolve_controlled_v2(
            empty, "codex", "gpt-test", now=self.NOW,
            runtime_status=runtime,
        )
        explicit, explicit_digest = resolve_controlled_v2(
            empty, "codex", "gpt-test", "default", now=self.NOW,
            runtime_status=runtime,
        )
        self.assertEqual(omitted["requested_effort"], "default")
        self.assertEqual(omitted, explicit)
        self.assertEqual(omitted_digest, explicit_digest)

        # An explicitly stored unadvertised level still fails closed.
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            resolve_controlled_v2(
                empty, "codex", "gpt-test", "high", now=self.NOW,
                runtime_status=runtime,
            )
        self.assertEqual(raised.exception.code, "unsupported_thinking_level")

    def test_model_default_bypasses_only_the_effort_value_gates(self):
        row = self.controlled_row(
            supported_efforts="[]",
            effort_metadata=json.dumps({
                "supported": [], "default": None,
                "digests": {}, "native_variant_ids": {},
            }),
        )
        runtime = compatible_runtime("0.145.0", harness="codex")
        binding, digest = resolve_controlled_v2(
            row, "codex", "gpt-test", " DeFaUlt ", now=self.NOW,
            runtime_status=runtime,
        )
        self.assertEqual(binding["requested_effort"], "default")
        self.assertEqual(binding["effective_effort"], "default")
        self.assertIsNone(binding["evidence_digest"])
        self.assertIsNone(binding["native_variant_id"])
        named, named_digest = resolve_controlled_v2(
            self.controlled_row(), "codex", "gpt-test", "high", now=self.NOW,
            runtime_status=runtime,
        )
        self.assertNotEqual(digest, named_digest)

        # Freshness, exact-model evidence, and transport gates are unchanged.
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            resolve_controlled_v2(
                {**row, "stale": 1, "last_error": "network down"},
                "codex", "gpt-test", "default", now=self.NOW,
                runtime_status=runtime,
            )
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            resolve_controlled_v2(
                row, "codex", "gpt-test", "high", now=self.NOW,
                runtime_status=runtime,
            )
        self.assertEqual(raised.exception.code, "unsupported_thinking_level")
        self.assertEqual(raised.exception.details["default_effort"], "default")
        self.assertEqual(raised.exception.details["supported_efforts"], [])

        # 'default' on an uncontrolled route stays rejected (decision #212).
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.resolve_v2(
                None, "codex", None, "default", now=self.NOW,
                runtime_status=runtime,
            )
        self.assertEqual(raised.exception.code, "unsupported_thinking_level")

        # The canonical shape is enforced both ways: no digest on a default
        # binding, a digest on every named-effort binding.
        forged = {**binding, "evidence_digest": "4" * 64}
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.validate_v2_binding(forged)
        forged_named = {**named, "evidence_digest": None}
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.validate_v2_binding(forged_named)

    def test_opencode_model_default_needs_no_variant_or_overlay(self):
        row = self.opencode_row()
        binding, _ = resolve_controlled_v2(
            row, "opencode", "provider/model", "default", now=self.NOW,
            runtime_status=compatible_runtime("1.18.9", harness="opencode"),
        )
        self.assertEqual(binding["requested_effort"], "default")
        self.assertIsNone(binding["native_variant_id"])
        self.assertIsNone(binding["evidence_digest"])
        forged = {**binding, "native_variant_id": "default"}
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.validate_v2_binding(forged)

    def test_opencode_binding_keeps_only_the_selected_admitted_overlay(self):
        row = self.opencode_row()
        row["supported_efforts"] = '["low","high"]'
        row["effort_metadata"] = json.dumps({
            "supported": ["low", "high"],
            "default": "high",
            "digests": {"low": "3" * 64, "high": "4" * 64},
            "native_variant_ids": {"low": "low", "high": "high"},
            "adapter_metadata_by_effort": {
                "low": {
                    "compatibility_manifest": "opencode-1.18.9-v1",
                    "provider_family": "openai-ai-sdk",
                    "variant_options": {"reasoningEffort": "low"},
                },
                "high": {
                    "compatibility_manifest": "opencode-1.18.9-v1",
                    "provider_family": "openai-ai-sdk",
                    "variant_options": {"reasoningEffort": "high"},
                },
            },
        })

        binding, _digest = resolve_controlled_v2(
            row,
            "opencode",
            "provider/model",
            "high",
            now=self.NOW,
            runtime_status=compatible_runtime("1.18.9", harness="opencode"),
        )

        self.assertEqual(binding["native_variant_id"], "high")
        self.assertEqual(binding["adapter_metadata"], {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {"reasoningEffort": "high"},
        })

    def test_opencode_binding_refuses_missing_selected_overlay(self):
        row = self.opencode_row()
        metadata = json.loads(row["effort_metadata"])
        metadata.pop("adapter_metadata_by_effort")
        row["effort_metadata"] = json.dumps(metadata)

        with self.assertRaises(route_bindings.RouteResolutionError) as refused:
            resolve_controlled_v2(
                row,
                "opencode",
                "provider/model",
                "k",
                now=self.NOW,
                runtime_status=compatible_runtime(
                    "1.18.9", harness="opencode"
                ),
            )

        self.assertEqual(refused.exception.code, "thinking_evidence_missing")

    def test_deepseek_binding_pins_default_omission_and_named_wire_mapping(self):
        runtime = compatible_runtime("0.1.0rc7", harness="deepseek")
        default, default_digest = resolve_controlled_v2(
            self.deepseek_row(),
            "deepseek",
            "deepseek-v4-pro",
            "default",
            now=self.NOW,
            runtime_status=runtime,
        )
        named, named_digest = resolve_controlled_v2(
            self.deepseek_row(),
            "deepseek",
            "deepseek-v4-pro",
            "high",
            now=self.NOW,
            runtime_status=runtime,
        )

        self.assertEqual(default["transport"], "deepseek-provider-options-v1")
        self.assertEqual(default["adapter_metadata"], {
            "provider_route": "deepseek-official",
            "provider_adapter_id": "deepseek-native-v1",
            "provider_adapter_digest": "1" * 64,
            "provider_registry_sha256": "2" * 64,
            "credential_kind": "deepseek-api-key",
            "endpoint_identity": "https://api.deepseek.com",
            "discovery_evidence_digest": "3" * 64,
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "5" * 64,
            "runtime_version": "0.1.0rc7",
            "source_commit": "b" * 40,
            "patch_sha256": "7" * 64,
            "composition_sha256": "8" * 64,
            "provider_options": {
                "omit": ["thinking", "reasoning_effort"], "set": {},
            },
        })
        self.assertEqual(named["adapter_metadata"], {
            "provider_route": "deepseek-official",
            "provider_adapter_id": "deepseek-native-v1",
            "provider_adapter_digest": "1" * 64,
            "provider_registry_sha256": "2" * 64,
            "credential_kind": "deepseek-api-key",
            "endpoint_identity": "https://api.deepseek.com",
            "discovery_evidence_digest": "3" * 64,
            "transport_contract": "deepseek-provider-options-v1",
            "wire_evidence_digest": "6" * 64,
            "runtime_version": "0.1.0rc7",
            "source_commit": "b" * 40,
            "patch_sha256": "7" * 64,
            "composition_sha256": "8" * 64,
            "provider_options": {
                "omit": [],
                "set": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                },
            },
        })
        self.assertNotEqual(default_digest, named_digest)

        forged = {
            **default,
            "adapter_metadata": named["adapter_metadata"],
        }
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.validate_v2_binding(forged)

        changed_effort = {
            **named,
            "adapter_metadata": {
                **named["adapter_metadata"],
                "provider_options": {
                    **named["adapter_metadata"]["provider_options"],
                    "set": {
                        **named["adapter_metadata"]["provider_options"]["set"],
                        "reasoning_effort": "low",
                    },
                },
            },
        }
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.validate_v2_binding(changed_effort)

        unsupported_effort = {
            **named,
            "requested_effort": "medium",
            "effective_effort": "medium",
            "adapter_metadata": {
                **named["adapter_metadata"],
                "provider_options": {
                    "omit": [],
                    "set": {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "medium",
                    },
                },
            },
        }
        with self.assertRaises(route_bindings.RouteResolutionError) as invalid:
            route_bindings.validate_v2_binding(unsupported_effort)
        self.assertEqual(
            invalid.exception.details,
            {"reason": "DeepSeek effort is outside the carrier contract"},
        )

        with self.assertRaises(route_bindings.RouteResolutionError) as refused:
            resolve_controlled_v2(
                self.deepseek_row(),
                "deepseek",
                "deepseek-v4-pro",
                "medium",
                now=self.NOW,
                runtime_status=runtime,
            )
        self.assertEqual(refused.exception.code, "unsupported_thinking_level")

    def test_ollama_binding_pins_provider_model_credential_and_endpoint(self):
        binding = {
            "contract_version": 2,
            "control_state": "controlled",
            "harness": "deepseek",
            "requested_model": "ollama-cloud/deepseek-v4-pro",
            "provider_model": "deepseek-v4-pro",
            "requested_effort": "default",
            "effective_effort": "default",
            "native_variant_id": None,
            "transport": "deepseek-provider-options-v1",
            "catalogue_generation": "a" * 32,
            "evidence_digest": None,
            "selector_binding": {
                "kind": "authenticated-provider-model",
                "selector": "ollama-cloud/deepseek-v4-pro",
            },
            "adapter_metadata": {
                "provider_route": "ollama-cloud",
                "provider_adapter_id": "dsh-llm-pi-ai@0.1.0-rc.7/ollama-cloud",
                "provider_adapter_digest": "1" * 64,
                "provider_registry_sha256": "2" * 64,
                "credential_kind": "ollama-api-key",
                "endpoint_identity": "https://ollama.com/v1",
                "discovery_evidence_digest": "3" * 64,
                "transport_contract": "deepseek-provider-options-v1",
                "provider_options": {
                    "omit": ["thinking", "reasoning_effort"], "set": {},
                },
                "wire_evidence_digest": "4" * 64,
                "runtime_version": "0.1.0rc7",
                "source_commit": "b" * 40,
                "patch_sha256": "5" * 64,
                "composition_sha256": "6" * 64,
            },
        }

        route_bindings.validate_v2_binding(binding)
        for field, value in (
            ("credential_kind", "deepseek-api-key"),
            ("endpoint_identity", "https://token@ollama.com/v1"),
            ("endpoint_identity", "https://"),
            ("provider_adapter_digest", "0" * 63),
        ):
            forged = json.loads(json.dumps(binding))
            forged["adapter_metadata"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(route_bindings.RouteResolutionError):
                    route_bindings.validate_v2_binding(forged)

    def test_uncontrolled_bindings_encode_every_inapplicable_value_as_null(self):
        default, default_digest = route_bindings.resolve_v2(
            None, "ClAuDe", None, None, now=self.NOW,
            runtime_status=compatible_runtime("2.1.223"),
        )
        vibe, vibe_digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest", None, now=self.NOW,
            runtime_status=compatible_runtime(),
        )
        default_replay, replay_digest = route_bindings.resolve_v2(
            None, "claude", None, None,
            now=self.NOW + timedelta(days=90),
            runtime_status=compatible_runtime("2.1.223"),
        )

        self.assertEqual(default, default_replay)
        self.assertEqual(default_digest, replay_digest)
        self.assertNotEqual(default_digest, vibe_digest)
        self.assertEqual(default["control_state"], "harness-default")
        self.assertEqual(default["harness"], "claude")
        self.assertEqual(vibe["control_state"], "native-uncontrolled")
        for binding in (default, vibe):
            self.assertIsNone(binding["requested_effort"])
            self.assertIsNone(binding["effective_effort"])
            self.assertIsNone(binding["catalogue_generation"])
            self.assertIsNone(binding["evidence_digest"])
            self.assertEqual(binding["transport"], "native-default")

    def test_unknown_harness_is_refused_before_binding_identity_exists(self):
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.resolve_v2(None, "not-a-harness", None)

        self.assertEqual(raised.exception.code, "unsupported_thinking_level")
        self.assertEqual(raised.exception.message, "Harness is not supported")
        self.assertEqual(
            raised.exception.details, {"harness": "not-a-harness"}
        )

    def test_opencode_ascii_lookup_accepts_case_without_aliasing_unicode(self):
        canonical, canonical_digest = resolve_controlled_v2(
            self.opencode_row(), "opencode", "provider/model", "k",
            now=self.NOW,
            runtime_status=compatible_runtime("1.18.9", harness="opencode"),
        )
        mixed_case, mixed_case_digest = resolve_controlled_v2(
            self.opencode_row(), "opencode", "provider/model", " K ",
            now=self.NOW,
            runtime_status=compatible_runtime("1.18.9", harness="opencode"),
        )

        self.assertEqual(mixed_case, canonical)
        self.assertEqual(mixed_case_digest, canonical_digest)
        self.assertEqual(canonical["requested_effort"], "k")
        self.assertEqual(canonical["native_variant_id"], "k")
        for confusable in ("K", "Ｋ"):
            with self.subTest(confusable=confusable):
                with self.assertRaises(
                    route_bindings.RouteResolutionError
                ) as raised:
                    resolve_controlled_v2(
                        self.opencode_row(), "opencode", "provider/model",
                        confusable, now=self.NOW,
                        runtime_status=compatible_runtime(
                            "1.18.9", harness="opencode"
                        ),
                    )
                self.assertEqual(
                    raised.exception.code, "unsupported_thinking_level"
                )
                self.assertEqual(
                    raised.exception.details["requested_effort"], confusable
                )

    def test_non_null_effort_is_refused_for_vibe_and_harness_default(self):
        for harness, model in (("vibe", "devstral-latest"), ("codex", None)):
            with self.subTest(harness=harness, model=model):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    route_bindings.resolve_v2(
                        None, harness, model, "high", now=self.NOW
                    )
                self.assertEqual(raised.exception.code, "unsupported_thinking_level")

    def test_stale_unsupported_and_source_drift_fail_with_distinct_codes(self):
        cases = (
            ({"stale": 1}, "high", None, "thinking_evidence_stale"),
            ({}, "medium", "2" * 64, "unsupported_thinking_level"),
            ({}, "high", "9" * 64, "thinking_evidence_stale"),
        )
        for overrides, effort, fingerprint, code in cases:
            with self.subTest(code=code, overrides=overrides):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    resolve_controlled_v2(
                        self.controlled_row(**overrides), "codex", "gpt-test", effort,
                        now=self.NOW,
                        fingerprint=fingerprint,
                        runtime_status=compatible_runtime(
                            "0.145.0", harness="codex"
                        ),
                    )
                self.assertEqual(raised.exception.code, code)

    def test_exact_route_rejects_other_harness_and_selector_evidence(self):
        cases = (
            (self.controlled_row(harness="claude"), "codex", "gpt-test"),
            (self.controlled_row(selector="catalog-model"),
             "codex", "different-model"),
        )
        for row, harness, model in cases:
            with self.subTest(evidence=(row["harness"], row["selector"])):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    route_bindings.resolve_v2(
                        row, harness, model, "high", now=self.NOW,
                    )
                self.assertEqual(raised.exception.code, "thinking_evidence_missing")
                self.assertIn("does not match", raised.exception.message)

    def test_controlled_route_accepts_best_effort_version_metadata(self):
        for compatibility in (None, "newer-unverified", "non-semver"):
            with self.subTest(compatibility=compatibility):
                binding, _ = resolve_controlled_v2(
                    self.controlled_row(
                        harness_compatibility=compatibility,
                        harness_support_state="best-effort",
                    ),
                    "codex", "gpt-test", now=self.NOW,
                )
                self.assertEqual(binding["requested_effort"], "high")

    def test_controlled_route_requires_collected_source_evidence(self):
        observation = controlled_observation(None)
        with (
            mock.patch.object(
                model_catalog, "controlled_route_evidence",
                return_value=observation,
            ) as collector,
            self.assertRaises(route_bindings.RouteResolutionError) as raised,
        ):
            route_bindings.resolve_v2(
                self.controlled_row(), "codex", "gpt-test", now=self.NOW
            )
        collector.assert_called_once_with("codex", "gpt-test")
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")

    def test_controlled_route_requires_exact_execution_runtime_evidence(self):
        scope = {"runtime": "sandbox", "runtime_identity": "sandbox:image-a"}
        binding, digest = resolve_controlled_v2(
            self.controlled_row(), "codex", "gpt-test", now=self.NOW,
            scope=scope,
        )
        self.assertEqual(binding["harness"], "codex")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

        other_scope = {
            "runtime": "sandbox", "runtime_identity": "sandbox:image-b",
        }
        cases = {
            "missing": {
                "runtime_status": {}, "runtime_scope": scope,
                "source_fingerprint": "2" * 64,
            },
            "other-harness": {
                "runtime_status": compatible_runtime(
                    "2.1.222", harness="claude", scope=scope
                ),
                "runtime_scope": scope, "source_fingerprint": "2" * 64,
            },
            "other-seat": {
                "runtime_status": compatible_runtime(
                    "0.145.0", harness="codex", scope=other_scope
                ),
                "runtime_scope": scope, "source_fingerprint": "2" * 64,
            },
            "version-drift": controlled_observation(
                harness="codex", version="0.146.0", scope=scope
            ),
        }
        for name, observation in cases.items():
            with (
                self.subTest(name=name),
                mock.patch.object(
                    model_catalog, "controlled_route_evidence",
                    return_value=observation,
                ) as collector,
                self.assertRaises(
                    route_bindings.RouteResolutionError
                ) as raised,
            ):
                route_bindings.resolve_v2(
                    self.controlled_row(), "codex", "gpt-test",
                    now=self.NOW,
                )
            collector.assert_called_once_with("codex", "gpt-test")
            self.assertEqual(
                raised.exception.code, "thinking_evidence_stale"
            )

    def test_public_resolver_does_not_accept_caller_supplied_route_proof(self):
        with self.assertRaises(TypeError) as raised:
            route_bindings.resolve_v2(
                self.controlled_row(), "codex", "gpt-test", now=self.NOW,
                route_proof={"source_fingerprint": "2" * 64},
            )
        self.assertIn("route_proof", str(raised.exception))

    def test_retained_canonical_proof_cannot_bypass_current_source_drift(self):
        scope = {"runtime": "sandbox", "runtime_identity": "sandbox:image-a"}
        with mock.patch.object(
            model_catalog, "controlled_route_evidence",
            return_value=controlled_observation(scope=scope),
        ) as initial_collector:
            proof = route_bindings._probe_controlled_route(
                "codex", "gpt-test"
            )
        initial_collector.assert_called_once_with("codex", "gpt-test")

        binding = digest = None
        with mock.patch.object(
            model_catalog, "controlled_route_evidence",
            return_value=controlled_observation(None, scope=scope),
        ) as current_collector:
            with self.assertRaises(
                route_bindings.RouteResolutionError
            ) as raised:
                binding, digest = route_bindings.resolve_v2(
                    self.controlled_row(), "codex", "gpt-test", now=self.NOW,
                )
            preview = routes_cli.resolve_row(
                self.controlled_row(), "codex", "gpt-test", now=self.NOW,
            )
        self.assertEqual(
            current_collector.call_args_list,
            [mock.call("codex", "gpt-test"), mock.call("codex", "gpt-test")],
        )
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")
        self.assertIsNone(binding)
        self.assertIsNone(digest)
        self.assertEqual(preview["code"], "thinking_evidence_stale")
        self.assertNotIn("binding", preview)
        self.assertNotIn("binding_digest", preview)
        self.assertNotIn("command", preview)

        with self.assertRaises(AttributeError):
            proof._source_fingerprint = self.controlled_row()[
                "source_fingerprint"
            ]

    def test_legacy_gate_requires_a_proven_version_one_row(self):
        self.assertEqual(
            route_bindings.legacy_route(
                row_contract_version=1, harness="vibe", model="old", effort=" High "
            )["effort"],
            " High ",
        )
        with self.assertRaises(route_bindings.RouteResolutionError):
            route_bindings.legacy_route(
                row_contract_version=2, harness="vibe", model="old", effort=None
            )


class LegacySprintBindingUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript((
            ROOT / ".super-coder" / "migrations" / "0075_model_routes.sql"
        ).read_text())
        self.con.executescript(
            "CREATE TABLE sprints (sprint_id INTEGER PRIMARY KEY,lifecycle TEXT NOT NULL);"
            "CREATE TABLE sprint_participants (participant_id INTEGER PRIMARY KEY,"
            "sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id));"
        )
        for migration in (
            "0212_route_binding_foundation.sql",
            "0216_sprint_binding_provenance.sql",
        ):
            self.con.executescript((ROOT / ".super-coder" / "migrations" / migration).read_text())
        self.con.execute("INSERT INTO sprints VALUES (1,'armed')")
        self.con.executemany(
            "INSERT INTO sprint_participants VALUES (?,1,NULL)", ((10,), (11,), (12,))
        )
        self.addCleanup(self.con.close)

    def _insert_legacy(self, participant_id: int, binding: dict, *,
                       source_fingerprint: str | None, harness_version: str) -> None:
        values = [binding[key] for key in route_bindings.BINDING_KEYS]
        self.con.execute(
            "INSERT INTO sprint_participant_route_bindings ("
            "participant_id,route_revision,contract_version,control_state,harness,"
            "requested_model,provider_model,requested_effort,effective_effort,"
            "native_variant_id,transport,catalogue_generation,evidence_digest,"
            "selector_binding,adapter_metadata,binding_json,binding_digest,"
            "source_fingerprint,harness_version"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                participant_id, 1, *values[:11],
                route_bindings.canonical_json(binding["selector_binding"])
                if binding["selector_binding"] is not None else None,
                route_bindings.canonical_json(binding["adapter_metadata"]),
                route_bindings.canonical_json(binding), route_bindings.digest_json(binding),
                source_fingerprint, harness_version,
            ),
        )

    def test_deepseek_migration_keeps_one_advancing_sequence_row(self):
        binding = ParticipantRevisionTest.controlled_binding()
        self._insert_legacy(
            10,
            binding,
            source_fingerprint="2" * 64,
            harness_version="0.145.0",
        )
        for migration in (
            "0217_harness_support_metadata.sql",
            "0218_sprint_binding_support_provenance.sql",
            "0223_model_default_effort_binding.sql",
            "0227_deepseek_controlled_route_binding.sql",
        ):
            self.con.executescript(
                (ROOT / ".super-coder" / "migrations" / migration).read_text()
            )

        self.assertEqual(
            [tuple(row) for row in self.con.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name='sprint_participant_route_bindings'"
            )],
            [("sprint_participant_route_bindings", 1)],
        )

        self.con.execute("UPDATE sprints SET lifecycle='prepared' WHERE sprint_id=1")
        receipt = route_bindings.ParticipantRouteBindingStore(self.con).bind(
            11,
            binding,
            route_bindings.digest_json(binding),
            transition="arm",
            source_fingerprint="2" * 64,
            harness_version="0.145.0",
            harness_support_state="tested",
        )
        self.assertEqual(receipt["binding_id"], 2)
        self.assertEqual(
            [tuple(row) for row in self.con.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name='sprint_participant_route_bindings'"
            )],
            [("sprint_participant_route_bindings", 2)],
        )

    def test_dirty_upgrade_preserves_legacy_semver_and_new_raw_rows_are_exact(self):
        now = datetime.now(timezone.utc)
        controlled, _ = resolve_controlled_v2(
            BindingIdentityTest.controlled_row(last_seen_at=now.isoformat()),
            "codex", "gpt-test", "high", now=now,
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )
        default, _ = route_bindings.resolve_v2(
            None, "claude", None,
            runtime_status=compatible_runtime("2.1.223", harness="claude"),
        )
        vibe, _ = route_bindings.resolve_v2(
            None, "vibe", "devstral", None,
            runtime_status=compatible_runtime("2.22.0", harness="vibe"),
        )
        captured = {
            10: ("2" * 64, "0.147.0"),
            11: (None, "2.1.223"),
            12: (None, "2.22.0"),
        }
        for participant_id, binding in ((10, controlled), (11, default), (12, vibe)):
            self._insert_legacy(participant_id, binding,
                                source_fingerprint=captured[participant_id][0],
                                harness_version=captured[participant_id][1])
        before = [tuple(row) for row in self.con.execute(
            "SELECT binding_json,binding_digest FROM sprint_participant_route_bindings "
            "ORDER BY participant_id"
        )]
        for migration in (
            "0217_harness_support_metadata.sql",
            "0218_sprint_binding_support_provenance.sql",
        ):
            self.con.executescript((ROOT / ".super-coder" / "migrations" / migration).read_text())
        upgraded = self.con.execute(
            "SELECT participant_id,binding_json,binding_digest,harness_evidence_format "
            "FROM sprint_participant_route_bindings ORDER BY participant_id"
        ).fetchall()
        self.assertEqual(before, [tuple(row)[1:3] for row in upgraded])
        self.assertEqual(
            [row["harness_evidence_format"] for row in upgraded],
            ["legacy-semver", "legacy-semver", "legacy-semver"],
        )

        scope = harness_versions.runtime_scope()
        codex = {**compatible_runtime("0.147.0", harness="codex", scope=scope),
                 "observed_version": "codex-cli 0.147.0"}
        claude = {**compatible_runtime("2.1.223", harness="claude", scope=scope),
                  "observed_version": "2.1.223 (Claude Code)"}
        vibe_status = {**compatible_runtime("2.22.0", harness="vibe", scope=scope),
                       "observed_version": "vibe 2.22.0"}
        with (
            mock.patch.object(model_catalog, "controlled_route_evidence", return_value={
                "runtime_status": codex, "runtime_scope": scope,
                "source_fingerprint": "2" * 64,
            }),
            mock.patch.object(model_catalog, "harness_runtime_status", side_effect=lambda h: {
                "claude": claude, "vibe": vibe_status,
            }[h]),
        ):
            for row in upgraded:
                participant_id = int(row["participant_id"])
                route_bindings.verify_stored_v2_before_first_turn(
                    self.con, json.loads(row["binding_json"]),
                    source_fingerprint=captured[participant_id][0],
                    harness_version=captured[participant_id][1],
                    harness_evidence_format=row["harness_evidence_format"],
                )
            changed = {**codex, "version": "0.148.0",
                       "observed_version": "codex-cli 0.148.0"}
            with mock.patch.object(model_catalog, "controlled_route_evidence", return_value={
                "runtime_status": changed, "runtime_scope": scope,
                "source_fingerprint": "2" * 64,
            }), self.assertRaises(route_bindings.RouteResolutionError) as raised:
                route_bindings.verify_stored_v2_before_first_turn(
                    self.con, controlled, source_fingerprint="2" * 64,
                    harness_version="0.147.0",
                    harness_evidence_format="legacy-semver",
                )
        self.assertEqual(raised.exception.code, "route_evidence_stale")

        raw_binding = route_bindings._uncontrolled_binding("vibe", "devstral", None)
        raw_runtime = {**compatible_runtime("2.22.0", harness="vibe", scope=scope),
                       "version": None, "observed_version": "vibe dev-build",
                       "compatibility": "non-semver"}
        with mock.patch.object(model_catalog, "harness_runtime_status", return_value=raw_runtime):
            route_bindings.verify_stored_v2_before_first_turn(
                self.con, raw_binding, source_fingerprint=None,
                harness_version="vibe dev-build",
                harness_evidence_format="raw-observed-v1",
            )
        changed_raw_runtime = {**raw_runtime, "observed_version": "vibe dev-build-next"}
        with mock.patch.object(
            model_catalog, "harness_runtime_status", return_value=changed_raw_runtime
        ):
            with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                route_bindings.verify_stored_v2_before_first_turn(
                    self.con, raw_binding, source_fingerprint=None,
                    harness_version="vibe dev-build",
                    harness_evidence_format="raw-observed-v1",
                )
        self.assertEqual(raised.exception.code, "route_evidence_stale")

    def test_dirty_v6_catalogue_requires_refresh_before_republication(self):
        legacy_generation = "a" * 32
        now = datetime.now(timezone.utc).isoformat()
        self.con.executescript(
            "CREATE TABLE flavor_defaults ("
            "flavor TEXT NOT NULL,harness TEXT NOT NULL,model TEXT,"
            "effort TEXT,is_default INTEGER NOT NULL DEFAULT 0,"
            "PRIMARY KEY (flavor,harness));"
        )
        self.con.execute(
            "INSERT INTO model_catalog_generations ("
            "generation_id,payload_version,contract_version,started_at,completed_at,"
            "state,runtime,source_summary,harness_versions,source_fingerprints,"
            "error_summary,payload_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (legacy_generation, 6, 2, now, now, "successful", "host", "[]", "{}",
             "{}", None, "1" * 64),
        )
        self.con.execute(
            "INSERT INTO model_routes ("
            "harness,selector,source,availability,headless_supported,"
            "high_effort_supported,default_effort,supported_efforts,cli_version,"
            "last_seen_at,stale,generation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("codex", "legacy-codex", "codex-cache", "available", 1, 1,
             "high", "[\"high\"]", "codex-cli 0.147.0", now, 0,
             legacy_generation),
        )
        self.con.execute(
            "INSERT INTO flavor_defaults (flavor,harness,model,effort,is_default) "
            "VALUES ('legacy','codex','legacy-codex',NULL,1)"
        )
        self.con.commit()

        for migration in (
            "0217_harness_support_metadata.sql",
            "0218_sprint_binding_support_provenance.sql",
            "0219_harness_support_refresh_boundary.sql",
        ):
            self.con.executescript(
                (ROOT / ".super-coder" / "migrations" / migration).read_text()
            )

        legacy_route = self.con.execute(
            "SELECT stale,last_error,harness_support_state FROM model_routes "
            "WHERE harness='codex' AND selector='legacy-codex'"
        ).fetchone()
        self.assertEqual(
            tuple(legacy_route),
            (1, "Catalogue refresh required after harness support evidence migration", None),
        )
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM model_catalog_generations"
        ).fetchone())

        status = {
            **GenerationPersistenceTest.status(
                version="0.147.0", compatibility="verified"
            ),
            "observed_version": "codex-cli 0.147.0",
            "verified_version": "0.147.0",
            "maximum_version_exclusive": "0.148.0",
        }
        ordinary = GenerationPersistenceTest.payload(
            "ordinary-codex", cli_version=status["observed_version"], status=status
        )
        refreshed_payload = GenerationPersistenceTest.payload(
            "refreshed-codex", cli_version=status["observed_version"], status=status
        )
        legacy_cache = {
            "v": 6,
            "fetched_at": now,
            "sources": ["codex-cache"],
            "catalogue_generation": legacy_generation,
            "harnesses": {"codex": {"models": [{"id": "legacy-codex"}]}},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(model_catalog, "CACHE", Path(tmp) / "catalog.json"), \
             mock.patch.object(
                 model_catalog, "build", side_effect=[ordinary, refreshed_payload]
             ) as build:
            model_catalog.CACHE.write_text(json.dumps(legacy_cache))
            required = model_catalog.catalog(
                con=self.con, opencode_provider=lambda: []
            )
            defaults = api_server.get_flavor_defaults(self.con)["flavors"]["legacy"]
            unresolved = routes_cli.resolve(self.con, "codex", "legacy-codex")
            refreshed = model_catalog.catalog(
                refresh=True, con=self.con, harness_probe=lambda: {"codex": status},
                opencode_provider=lambda: [],
            )
            cached = json.loads(model_catalog.CACHE.read_text())

        self.assertEqual(build.call_count, 2)
        self.assertTrue(required["stale"])
        self.assertEqual(
            required["error"],
            "Catalogue refresh required after runtime evidence rebuild",
        )
        self.assertEqual(defaults[0]["harness_support_state"], None)
        self.assertEqual(defaults[0]["effective_effort"], None)
        self.assertFalse(unresolved["ok"])
        self.assertEqual(unresolved["code"], "thinking_evidence_stale")
        self.assertFalse(refreshed["stale"])
        self.assertEqual(
            refreshed["harnesses"]["codex"]["models"][0]["harness_support_state"],
            "tested",
        )
        self.assertEqual(cached["v"], 8)
        current = self.con.execute(
            "SELECT selector,stale,harness_version,harness_support_state FROM model_routes "
            "WHERE harness='codex' ORDER BY selector"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in current],
            [
                ("legacy-codex", 1, None, None),
                ("refreshed-codex", 0, "codex-cli 0.147.0", "tested"),
            ],
        )


class GenerationPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.con = route_schema()
        self.addCleanup(self.con.close)
        self.headless = mock.patch.object(
            model_catalog, "_headless_supported", return_value=True
        )
        self.headless.start()
        self.addCleanup(self.headless.stop)

    def shared_connections(self) -> tuple[sqlite3.Connection, sqlite3.Connection]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "routes.db"
        first = route_schema(path)
        second = sqlite3.connect(path)
        second.row_factory = sqlite3.Row
        second.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        return first, second

    @staticmethod
    def status(*, version="0.145.0", compatibility="verified", error=None) -> dict:
        return {
            "version": version,
            "compatibility": compatibility,
            "minimum_version": "0.145.0",
            "maximum_version_exclusive": "0.147.0",
            "verified_version": "0.145.0",
            "error": error,
        }

    @classmethod
    def payload(cls, selector: str = "gpt-test", *, cli_version="codex-cli 0.145.0",
                status: dict | None = None) -> dict:
        return {
            "v": model_catalog.PAYLOAD_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": ["codex-cache"],
            "stale": False,
            "partial": False,
            "harnesses": {"codex": {"models": [model_catalog._entry(
                selector,
                source="codex-cache",
                availability="available",
                provider="openai",
                provider_model=selector,
                supported_efforts=["low", "high"],
                default_effort="high",
                cli_version=cli_version,
            )]}},
            "verification": {"runtime": "host", "harnesses": {
                "codex": status or cls.status(),
            }},
        }

    def test_successful_generation_publishes_one_coherent_snapshot(self):
        payload = self.payload()
        model_catalog.persist_routes(self.con, payload)

        generation = self.con.execute(
            "SELECT generation_id,state,payload_version,harness_versions "
            "FROM model_catalog_generations"
        ).fetchone()
        route = self.con.execute(
            "SELECT generation_id,evidence_kind,evidence_digest,source_fingerprint,"
            "harness_version,harness_compatibility,selector_binding,"
            "effort_metadata,stale FROM model_routes"
        ).fetchone()
        self.assertEqual(generation["state"], "successful")
        self.assertEqual(generation["payload_version"], model_catalog.PAYLOAD_VERSION)
        self.assertEqual(route["generation_id"], generation["generation_id"])
        self.assertEqual(route["evidence_kind"], "codex-model-cache")
        self.assertEqual(route["harness_version"], "0.145.0")
        self.assertEqual(route["harness_compatibility"], "verified")
        self.assertEqual(
            json.loads(generation["harness_versions"])["codex"], self.status()
        )
        self.assertEqual(route["stale"], 0)
        self.assertEqual(len(route["evidence_digest"]), 64)
        self.assertEqual(json.loads(route["selector_binding"])["selector"], "gpt-test")
        self.assertEqual(
            sorted(json.loads(route["effort_metadata"])["digests"]), ["high", "low"]
        )

    def test_partial_refresh_records_failure_without_publishing_partial_rows(self):
        first = self.payload("stable")
        model_catalog.persist_routes(self.con, first)
        original_generation = first["catalogue_generation"]

        partial = self.payload("partial-only")
        partial.update({"partial": True, "stale": True,
                        "errors": ["models.dev: network down"]})
        model_catalog.persist_routes(self.con, partial)

        generations = self.con.execute(
            "SELECT state FROM model_catalog_generations ORDER BY rowid"
        ).fetchall()
        stable = self.con.execute(
            "SELECT generation_id,stale,last_error FROM model_routes "
            "WHERE selector='stable'"
        ).fetchone()
        partial_row = self.con.execute(
            "SELECT 1 FROM model_routes WHERE selector='partial-only'"
        ).fetchone()
        self.assertEqual([row["state"] for row in generations],
                         ["successful", "failed"])
        self.assertEqual(stable["generation_id"], original_generation)
        self.assertEqual(stable["stale"], 1)
        self.assertIn("models.dev", stable["last_error"])
        self.assertIsNone(partial_row)

    def test_failed_explicit_refresh_stays_stale_on_followup_cache_read(self):
        fresh = self.payload()
        fresh["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        fresh.pop("verification")
        statuses = {"codex": self.status()}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            model_catalog, "CACHE", Path(tmp) / "model_catalog.json"
        ), mock.patch.object(
            model_catalog, "build", side_effect=[fresh, RuntimeError("network down")]
        ) as build:
            first = model_catalog.catalog(
                refresh=True, con=self.con, opencode_provider=lambda: [],
                harness_probe=lambda: statuses,
            )
            before_failure = datetime.now(timezone.utc)
            failed = model_catalog.catalog(
                refresh=True, con=self.con, opencode_provider=lambda: [],
                harness_probe=lambda: statuses,
            )
            cached = model_catalog.catalog(
                con=self.con, opencode_provider=lambda: [],
                harness_probe=lambda: (_ for _ in ()).throw(
                    AssertionError("ordinary cache read reprobed")
                ),
            )
            cache_payload = json.loads(model_catalog.CACHE.read_text())

        route = self.con.execute(
            "SELECT stale,last_error FROM model_routes WHERE selector='gpt-test'"
        ).fetchone()
        generation = self.con.execute(
            "SELECT state,started_at,completed_at FROM model_catalog_generations "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertFalse(first["stale"])
        self.assertTrue(failed["stale"])
        self.assertTrue(cached["stale"])
        self.assertTrue(cache_payload["stale"])
        self.assertEqual(build.call_count, 2)
        self.assertEqual(tuple(route), (1, "network down"))
        self.assertEqual(generation["state"], "failed")
        self.assertGreaterEqual(
            datetime.fromisoformat(generation["started_at"]), before_failure
        )
        self.assertGreaterEqual(
            datetime.fromisoformat(generation["completed_at"]),
            datetime.fromisoformat(generation["started_at"]),
        )

    def test_rebuilt_empty_ledger_requires_refresh_before_fresh_cache(self):
        cached = self.payload("cached-only")
        cached.update({
            "catalogue_generation": "a" * 32,
            "generation_state": "successful",
            "generation_published": True,
        })
        ordinary_payload = self.payload("ordinary-advisory")
        explicit_payload = self.payload("explicit-route")
        ordinary_payload.pop("verification")
        explicit_payload.pop("verification")
        statuses = {"codex": self.status()}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            model_catalog, "CACHE", Path(tmp) / "model_catalog.json"
        ), mock.patch.object(
            model_catalog, "build",
            side_effect=[ordinary_payload, explicit_payload],
        ) as build:
            model_catalog.CACHE.write_text(json.dumps(cached))
            ordinary = model_catalog.catalog(
                con=self.con, opencode_provider=lambda: [],
                harness_probe=lambda: statuses,
            )
            ordinary_cache = json.loads(model_catalog.CACHE.read_text())
            ordinary_generations = self.con.execute(
                "SELECT COUNT(*) FROM model_catalog_generations"
            ).fetchone()[0]
            ordinary_routes = self.con.execute(
                "SELECT COUNT(*) FROM model_routes"
            ).fetchone()[0]

            explicit = model_catalog.catalog(
                refresh=True, con=self.con, opencode_provider=lambda: [],
                harness_probe=lambda: statuses,
            )
            explicit_cache = json.loads(model_catalog.CACHE.read_text())

        generation = self.con.execute(
            "SELECT generation_id,state FROM model_catalog_generations"
        ).fetchone()
        route = self.con.execute(
            "SELECT selector,generation_id,stale,last_error FROM model_routes"
        ).fetchone()
        self.assertEqual(build.call_count, 2)
        self.assertTrue(ordinary["stale"])
        self.assertEqual(
            ordinary["error"],
            "Catalogue refresh required after runtime evidence rebuild",
        )
        self.assertEqual(
            ordinary["harnesses"]["codex"]["models"][0]["id"],
            "ordinary-advisory",
        )
        self.assertNotIn("catalogue_generation", ordinary)
        self.assertTrue(ordinary_cache["stale"])
        self.assertNotIn("catalogue_generation", ordinary_cache)
        self.assertEqual(ordinary_generations, 0)
        self.assertEqual(ordinary_routes, 0)
        self.assertFalse(explicit["stale"])
        self.assertEqual(
            explicit["catalogue_generation"], generation["generation_id"]
        )
        self.assertEqual(
            explicit_cache["catalogue_generation"], generation["generation_id"]
        )
        self.assertEqual(generation["state"], "successful")
        self.assertEqual(
            tuple(route),
            ("explicit-route", generation["generation_id"], 0, None),
        )

    def test_missing_harness_is_rejected_but_best_effort_versions_publish(self):
        cases = (
            ("missing", None, self.status(
                version=None, compatibility=None, error="HARNESS_UNAVAILABLE"
            )),
            ("below", "codex-cli 0.144.0", self.status(
                version="0.144.0", compatibility=None,
                error=None,
            )),
            ("newer", "codex-cli 0.147.0", self.status(
                version="0.147.0", compatibility="newer-unverified",
            )),
        )
        for selector, cli_version, status in cases:
            model_catalog.persist_routes(
                self.con,
                self.payload(selector, cli_version=cli_version, status=status),
            )

        generations = self.con.execute(
            "SELECT state,harness_versions FROM model_catalog_generations "
            "ORDER BY rowid"
        ).fetchall()
        routes = self.con.execute(
            "SELECT harness,selector,harness_support_state FROM model_routes "
            "ORDER BY selector"
        ).fetchall()
        self.assertEqual([row["state"] for row in generations],
                         ["successful", "successful", "successful"])
        self.assertEqual(
            [json.loads(row["harness_versions"])["codex"]["version"]
             for row in generations],
            [None, "0.144.0", "0.147.0"],
        )
        self.assertEqual(
            [tuple(row) for row in routes],
            [("codex", "below", "best-effort"),
             ("codex", "newer", "best-effort")],
        )

    def test_refresh_projects_tested_and_best_effort_route_support_to_clients(self):
        tested = {
            **self.status(version="0.145.0", compatibility="verified"),
            "observed_version": "codex-cli 0.145.0",
        }
        best_effort = {
            "harness": "kimi", "version": None,
            "observed_version": "kimi dev-build",
            "compatibility": "non-semver", "minimum_version": "0.30.0",
            "maximum_version_exclusive": "0.34.0", "verified_version": "0.33.0",
            "error": None,
        }
        payload = self.payload("tested-route", cli_version=tested["observed_version"],
                               status=tested)
        payload["harnesses"]["kimi"] = {"models": [model_catalog._entry(
            "best-effort-route", source="kimi-config", availability="available",
            provider="moonshot", provider_model="k3", supported_efforts=["high"],
            default_effort="high", cli_version=best_effort["observed_version"],
        )]}
        statuses = {"codex": tested, "kimi": best_effort}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(model_catalog, "CACHE", Path(tmp) / "catalog.json"), \
             mock.patch.object(model_catalog, "build", return_value=payload):
            refreshed = model_catalog.catalog(
                refresh=True, con=self.con, harness_probe=lambda: statuses,
            )
            cached = json.loads(model_catalog.CACHE.read_text())
        projected = {
            (harness, entry["id"]): entry
            for source in (refreshed, cached)
            for harness, block in source["harnesses"].items()
            for entry in block["models"]
        }
        self.assertEqual(
            (projected[("codex", "tested-route")]["harness_support_state"],
             projected[("codex", "tested-route")]["harness_version"]),
            ("tested", "codex-cli 0.145.0"),
        )
        self.assertEqual(
            (projected[("kimi", "best-effort-route")]["harness_support_state"],
             projected[("kimi", "best-effort-route")]["harness_version"]),
            ("best-effort", "kimi dev-build"),
        )

    def test_real_best_effort_probes_publish_and_resolve_every_route_shape(self):
        cases = (
            ("older", "codex-cli 0.144.0", "older-unverified"),
            ("non-semver", "codex dev-build", "non-semver"),
        )
        for selector, raw_version, compatibility in cases:
            with self.subTest(raw_version=raw_version):
                with mock.patch.object(harness_versions, "probe", return_value=raw_version):
                    status = harness_versions.compatibility_status(("codex",))["codex"]
                self.assertEqual(status["compatibility"], compatibility)
                self.assertIsNone(status["error"])

                model_catalog.persist_routes(
                    self.con,
                    self.payload(selector, cli_version=raw_version, status=status),
                )
                row = dict(self.con.execute(
                    "SELECT * FROM model_routes WHERE selector=?", (selector,)
                ).fetchone())
                self.assertEqual(row["harness_version"], raw_version)
                self.assertEqual(row["harness_support_state"], "best-effort")

                observation = {
                    "runtime_status": status,
                    "runtime_scope": harness_versions.runtime_scope(),
                    "source_fingerprint": row["source_fingerprint"],
                }
                with mock.patch.object(
                    model_catalog, "controlled_route_evidence", return_value=observation
                ):
                    controlled = routes_cli.resolve(self.con, "codex", selector)
                self.assertTrue(controlled["ok"])
                self.assertEqual(controlled["binding"]["requested_model"], selector)
                self.assertEqual(controlled["harness_version"], raw_version)
                self.assertEqual(controlled["harness_support_state"], "best-effort")

                uncontrolled, _ = route_bindings.resolve_v2(
                    None, "codex", None, runtime_status=status
                )
                self.assertEqual(uncontrolled["control_state"], "harness-default")

    def test_mixed_harness_refresh_keeps_route_local_support_state(self):
        scope = harness_versions.runtime_scope()
        codex = {
            **self.status(
                version="0.147.0", compatibility="verified"
            ),
            "harness": "codex",
            "observed_version": "codex-cli 0.147.0",
            "maximum_version_exclusive": "0.148.0",
            "verified_version": "0.147.0",
            **scope,
        }
        kimi = {
            "harness": "kimi",
            "version": None,
            "observed_version": "kimi dev-build",
            "compatibility": "non-semver",
            "minimum_version": "0.30.0",
            "maximum_version_exclusive": "0.34.0",
            "verified_version": "0.33.0",
            "error": None,
            **scope,
        }
        payload = self.payload(
            "codex-tested", cli_version=codex["observed_version"], status=codex
        )
        payload["harnesses"]["kimi"] = {"models": [model_catalog._entry(
            "kimi-best-effort", source="kimi-config", availability="available",
            provider="moonshot", provider_model="k3", supported_efforts=["high"],
            default_effort="high", cli_version=kimi["observed_version"],
        )]}
        payload["verification"]["harnesses"]["kimi"] = kimi
        model_catalog.persist_routes(self.con, payload)

        rows = {
            (row["harness"], row["selector"]): dict(row)
            for row in self.con.execute("SELECT * FROM model_routes")
        }
        self.assertEqual(rows[("codex", "codex-tested")]["harness_support_state"], "tested")
        self.assertEqual(rows[("kimi", "kimi-best-effort")]["harness_support_state"], "best-effort")

        for harness, selector, status in (
            ("codex", "codex-tested", codex),
            ("kimi", "kimi-best-effort", kimi),
        ):
            row = rows[(harness, selector)]
            with mock.patch.object(
                model_catalog,
                "controlled_route_evidence",
                return_value={
                    "runtime_status": status,
                    "runtime_scope": scope,
                    "source_fingerprint": row["source_fingerprint"],
                },
            ):
                resolved = routes_cli.resolve(self.con, harness, selector)
            self.assertTrue(resolved["ok"])
            self.assertEqual(resolved["binding"]["requested_model"], selector)

        older_codex = {
            **codex,
            "version": "0.144.0",
            "observed_version": "codex-cli 0.144.0",
            "compatibility": "older-unverified",
        }
        unavailable_kimi = {**kimi, "observed_version": None, "error": "HARNESS_UNAVAILABLE"}
        payload = self.payload(
            "codex-older", cli_version=older_codex["observed_version"], status=older_codex
        )
        payload["harnesses"]["kimi"] = {"models": [model_catalog._entry(
            "kimi-unavailable", source="kimi-config", availability="available",
            supported_efforts=["high"], cli_version="kimi dev-build",
        )]}
        payload["verification"]["harnesses"]["kimi"] = unavailable_kimi
        model_catalog.persist_routes(self.con, payload)
        older_row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE harness='codex' AND selector='codex-older'"
        ).fetchone())
        self.assertEqual(older_row["harness_support_state"], "best-effort")
        with mock.patch.object(
            model_catalog,
            "controlled_route_evidence",
            return_value={
                "runtime_status": older_codex,
                "runtime_scope": scope,
                "source_fingerprint": older_row["source_fingerprint"],
            },
        ):
            resolved = routes_cli.resolve(self.con, "codex", "codex-older")
        self.assertTrue(resolved["ok"])

    def test_best_effort_refresh_replaces_prior_route_without_staling(self):
        first = self.payload("carried")
        model_catalog.persist_routes(self.con, first)
        model_catalog.persist_routes(
            self.con,
            self.payload(
                "carried",
                cli_version="codex-cli 0.147.0",
                status=self.status(
                    version="0.147.0", compatibility="newer-unverified"
                ),
            ),
        )

        row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE selector='carried'"
        ).fetchone())
        self.assertEqual(row["stale"], 0)
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["harness_support_state"], "best-effort")

    def test_live_fingerprint_accepts_best_effort_installed_version(self):
        entry = model_catalog._entry(
            "gpt-test", source="codex-cache", availability="available",
            supported_efforts=["high"], cli_version="codex-cli 0.147.0",
        )
        with mock.patch.object(
            model_catalog, "_from_codex_cache", return_value=[entry]
        ):
            fingerprint = model_catalog.current_source_fingerprint(
                "codex", "gpt-test", env={}, run=mock.Mock(),
                harness_probe=lambda: {"codex": self.status(
                    version="0.147.0", compatibility="newer-unverified"
                )},
            )
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_authoritative_resolution_durably_stales_drift_and_expiry(self):
        cases = (
            (
                "fingerprint-drift",
                datetime.now(timezone.utc).isoformat(),
                "wrong-fingerprint",
                "Installed route source changed after refresh",
            ),
            (
                "age-expired",
                (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                None,
                "Route evidence is older than 24 hours",
            ),
        )
        for name, fetched_at, supplied_fingerprint, message in cases:
            with self.subTest(name=name):
                con = route_schema()
                self.addCleanup(con.close)
                payload = self.payload(name)
                payload["fetched_at"] = fetched_at
                model_catalog.persist_routes(con, payload)
                row = dict(con.execute(
                    "SELECT * FROM model_routes WHERE selector=?", (name,)
                ).fetchone())
                fingerprint = supplied_fingerprint or row["source_fingerprint"]

                got = resolve_controlled(
                    con, "Codex", name, fingerprint=fingerprint,
                    now=datetime.now(timezone.utc),
                )
                stored = con.execute(
                    "SELECT stale,last_error FROM model_routes WHERE selector=?",
                    (name,),
                ).fetchone()

                self.assertFalse(got["ok"])
                self.assertEqual(got["code"], "thinking_evidence_stale")
                self.assertEqual(stored["stale"], 1)
                self.assertEqual(
                    stored["last_error"],
                    f"thinking_evidence_stale: {message}; "
                    "remediation: sc models refresh",
                )
                refused_again = resolve_controlled(
                    con, "codex", name,
                    fingerprint=row["source_fingerprint"],
                )
                self.assertFalse(refused_again["ok"])
                self.assertEqual(refused_again["code"], "thinking_evidence_stale")

    def test_wrong_seat_source_proof_does_not_stale_shared_route(self):
        model_catalog.persist_routes(self.con, self.payload("seat-bound"))
        row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE selector='seat-bound'"
        ).fetchone())
        sandbox = {
            "runtime": "sandbox", "runtime_identity": "sandbox:image-a",
        }
        other_sandbox = {
            "runtime": "sandbox", "runtime_identity": "sandbox:image-b",
        }
        observation = {
            "runtime_status": compatible_runtime(
                "0.145.0", harness="codex", scope=other_sandbox
            ),
            "runtime_scope": sandbox,
            "source_fingerprint": row["source_fingerprint"],
        }
        with mock.patch.object(
            model_catalog, "controlled_route_evidence",
            return_value=observation,
        ):
            got = routes_cli.resolve(self.con, "codex", "seat-bound")
        stored = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE selector='seat-bound'"
        ).fetchone()

        self.assertEqual(got["code"], "thinking_evidence_stale")
        self.assertNotIn("binding", got)
        self.assertNotIn("binding_digest", got)
        self.assertNotIn("command", got)
        self.assertEqual(tuple(stored), (0, None))

    def test_authoritative_resolution_accepts_changed_cli_text_when_proof_matches(self):
        payload = self.payload("version-drift")
        model_catalog.persist_routes(self.con, payload)
        self.con.execute(
            "UPDATE model_routes SET cli_version='codex-cli 0.146.0' "
            "WHERE selector='version-drift'"
        )
        self.con.commit()
        row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE selector='version-drift'"
        ).fetchone())

        got = resolve_controlled(
            self.con, "codex", "version-drift",
            fingerprint=row["source_fingerprint"],
        )
        self.assertTrue(got["ok"])

    def test_authoritative_resolution_requires_latest_successful_generation(self):
        payload = self.payload("superseded")
        model_catalog.persist_routes(self.con, payload)
        row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE selector='superseded'"
        ).fetchone())
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.con.execute(
            "INSERT INTO model_catalog_generations ("
            "generation_id,payload_version,contract_version,started_at,"
            "completed_at,state,runtime,source_summary,harness_versions,"
            "source_fingerprints,error_summary,payload_digest"
            ") VALUES (?,?,?,?,?,'successful','host','[]','{}','{}',NULL,?)",
            ("f" * 32, 6, 2, later, later, "e" * 64),
        )
        self.con.commit()

        got = resolve_controlled(
            self.con, "codex", "superseded",
            fingerprint=row["source_fingerprint"],
        )
        stored = self.con.execute(
            "SELECT stale,last_error FROM model_routes WHERE selector='superseded'"
        ).fetchone()

        self.assertEqual(got["code"], "thinking_evidence_stale")
        self.assertEqual(tuple(stored), (
            1,
            "thinking_evidence_stale: Route does not belong to the latest "
            "successful generation; remediation: sc models refresh",
        ))

    def test_direct_resolve_retains_pre_probe_generation_identity(self):
        resolver, refresher = self.shared_connections()
        first = self.payload("generation-race")
        first["fetched_at"] = "2026-08-17T00:00:00+00:00"
        model_catalog.persist_routes(resolver, first)
        observed = dict(resolver.execute(
            "SELECT * FROM model_routes WHERE selector='generation-race'"
        ).fetchone())
        second = self.payload(
            "generation-race", cli_version="codex-cli 0.146.0",
            status=self.status(version="0.146.0"),
        )
        second["fetched_at"] = "2026-08-17T00:01:00+00:00"

        def publish_successor(*_args, **_kwargs):
            model_catalog.persist_routes(refresher, second)
            status = compatible_runtime("0.145.0", harness="codex")
            return {
                "runtime_status": status,
                "runtime_scope": {
                    "runtime": status["runtime"],
                    "runtime_identity": status["runtime_identity"],
                },
                "source_fingerprint": observed["source_fingerprint"],
            }

        with mock.patch.object(
            routes_cli.model_catalog,
            "controlled_route_evidence",
            side_effect=publish_successor,
        ) as probe:
            got = routes_cli.resolve(
                resolver, "codex", "generation-race",
                runtime_status=compatible_runtime(
                    "0.145.0", harness="codex"
                ),
            )

        stored = dict(resolver.execute(
            "SELECT * FROM model_routes WHERE selector='generation-race'"
        ).fetchone())
        retried = resolve_controlled(
            resolver, "codex", "generation-race",
            fingerprint=stored["source_fingerprint"], version="0.146.0",
            now=datetime(2026, 8, 17, 0, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(got["code"], "thinking_evidence_stale")
        self.assertEqual(
            got["error"],
            "Route evidence changed during resolution; retry",
        )
        self.assertNotEqual(observed["generation_id"], stored["generation_id"])
        self.assertNotEqual(observed["source_fingerprint"],
                            stored["source_fingerprint"])
        self.assertEqual(stored["stale"], 0)
        self.assertIsNone(stored["last_error"])
        self.assertTrue(retried["ok"])
        self.assertEqual(
            retried["binding"]["catalogue_generation"],
            stored["generation_id"],
        )

    def test_late_success_records_attempt_without_replacing_newer_routes(self):
        late, newer = self.shared_connections()
        newest_payload = self.payload("ordered-success")
        newest_payload["fetched_at"] = "2026-08-17T00:02:00+00:00"
        newest_payload["harnesses"]["codex"]["models"][0][
            "provider_model"
        ] = "new-provider-model"
        model_catalog.persist_routes(newer, newest_payload)
        newest_generation = newest_payload["catalogue_generation"]
        older_payload = self.payload("ordered-success")
        older_payload["fetched_at"] = "2026-08-17T00:01:00+00:00"
        older_payload["harnesses"]["codex"]["models"][0][
            "provider_model"
        ] = "old-provider-model"

        model_catalog.persist_routes(late, older_payload)

        route = dict(late.execute(
            "SELECT * FROM model_routes WHERE selector='ordered-success'"
        ).fetchone())
        generations = late.execute(
            "SELECT generation_id,state FROM model_catalog_generations "
            "ORDER BY completed_at,generation_id"
        ).fetchall()
        resolved = resolve_controlled(
            late, "codex", "ordered-success",
            fingerprint=route["source_fingerprint"],
            now=datetime(2026, 8, 17, 0, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(newest_payload["generation_published"])
        self.assertFalse(older_payload["generation_published"])
        self.assertEqual(len(generations), 2)
        self.assertEqual(
            {row["generation_id"]: row["state"] for row in generations},
            {
                older_payload["catalogue_generation"]: "successful",
                newest_generation: "successful",
            },
        )
        self.assertEqual(route["generation_id"], newest_generation)
        self.assertEqual(route["provider_model"], "new-provider-model")
        self.assertEqual((route["stale"], route["last_error"]), (0, None))
        self.assertTrue(resolved["ok"])

    def test_late_failure_records_attempt_without_staling_newer_routes(self):
        late, newer = self.shared_connections()
        newest_payload = self.payload("ordered-failure")
        newest_payload["fetched_at"] = "2026-08-17T00:02:00+00:00"
        model_catalog.persist_routes(newer, newest_payload)
        newest_generation = newest_payload["catalogue_generation"]
        older_failure = {
            "v": model_catalog.PAYLOAD_VERSION,
            "refresh_started_at": "2026-08-17T00:00:00+00:00",
            "refresh_completed_at": "2026-08-17T00:01:00+00:00",
            "fetched_at": "2026-08-17T00:01:00+00:00",
            "stale": True,
            "partial": False,
            "error": "older refresh failed",
            "harnesses": {},
            "verification": {"runtime": "host", "harnesses": {}},
        }

        model_catalog.persist_routes(late, older_failure)

        route = dict(late.execute(
            "SELECT * FROM model_routes WHERE selector='ordered-failure'"
        ).fetchone())
        generations = late.execute(
            "SELECT generation_id,state,error_summary "
            "FROM model_catalog_generations ORDER BY completed_at,generation_id"
        ).fetchall()
        resolved = resolve_controlled(
            late, "codex", "ordered-failure",
            fingerprint=route["source_fingerprint"],
            now=datetime(2026, 8, 17, 0, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(newest_payload["generation_published"])
        self.assertFalse(older_failure["generation_published"])
        self.assertEqual(len(generations), 2)
        by_generation = {row["generation_id"]: row for row in generations}
        self.assertEqual(
            {key: row["state"] for key, row in by_generation.items()},
            {
                older_failure["catalogue_generation"]: "failed",
                newest_generation: "successful",
            },
        )
        self.assertEqual(
            json.loads(by_generation[
                older_failure["catalogue_generation"]
            ]["error_summary"])["error"],
            "older refresh failed",
        )
        self.assertEqual(route["generation_id"], newest_generation)
        self.assertEqual((route["stale"], route["last_error"]), (0, None))
        self.assertTrue(resolved["ok"])

    def test_losing_catalog_attempts_keep_winner_cache_and_routes(self):
        loser_con, winner_con = self.shared_connections()
        winner = self.payload("winner-route")
        loser = self.payload("loser-route")
        winner.pop("verification")
        loser.pop("verification")
        base = datetime.now(timezone.utc)
        statuses = {"codex": self.status()}

        def clock(when):
            value = mock.Mock()
            value.now.return_value = when
            value.fromisoformat.side_effect = datetime.fromisoformat
            return value

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            model_catalog, "CACHE", Path(tmp) / "model_catalog.json"
        ):
            with mock.patch.object(
                model_catalog, "build", return_value=winner
            ), mock.patch.object(
                model_catalog, "datetime", clock(base + timedelta(minutes=1))
            ):
                winner_response = model_catalog.catalog(
                    refresh=True, con=winner_con,
                    opencode_provider=lambda: [],
                    harness_probe=lambda: statuses,
                )
            with mock.patch.object(
                model_catalog, "build", return_value=loser
            ), mock.patch.object(
                model_catalog, "datetime", clock(base)
            ):
                loser_response = model_catalog.catalog(
                    refresh=True, con=loser_con,
                    opencode_provider=lambda: [],
                    harness_probe=lambda: statuses,
                )
            cached = json.loads(model_catalog.CACHE.read_text())

        generations = loser_con.execute(
            "SELECT generation_id,state FROM model_catalog_generations "
            "ORDER BY completed_at,generation_id"
        ).fetchall()
        fresh_routes = loser_con.execute(
            "SELECT selector,generation_id,stale,last_error FROM model_routes "
            "WHERE stale=0 ORDER BY selector"
        ).fetchall()
        loser_route = loser_con.execute(
            "SELECT 1 FROM model_routes WHERE selector='loser-route'"
        ).fetchone()
        winner_generation = winner_response["catalogue_generation"]
        for payload in (winner_response, loser_response, cached):
            self.assertEqual(payload["catalogue_generation"], winner_generation)
            self.assertEqual(
                payload["harnesses"]["codex"]["models"][0]["id"],
                "winner-route",
            )
            self.assertFalse(payload["stale"])
        self.assertEqual(len(generations), 2)
        self.assertEqual(generations[-1]["generation_id"], winner_generation)
        self.assertEqual(
            [tuple(row) for row in fresh_routes],
            [("winner-route", winner_generation, 0, None)],
        )
        self.assertIsNone(loser_route)

    def test_older_failed_catalog_keeps_successful_winner_cache(self):
        failed_con, winner_con = self.shared_connections()
        winner = self.payload("winner-route")
        winner.pop("verification")
        base = datetime.now(timezone.utc)
        statuses = {"codex": self.status()}

        def clock(when):
            value = mock.Mock()
            value.now.return_value = when
            value.fromisoformat.side_effect = datetime.fromisoformat
            return value

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            model_catalog, "CACHE", Path(tmp) / "model_catalog.json"
        ):
            with mock.patch.object(
                model_catalog, "build", return_value=winner
            ), mock.patch.object(
                model_catalog, "datetime", clock(base + timedelta(minutes=1))
            ):
                winner_response = model_catalog.catalog(
                    refresh=True, con=winner_con,
                    opencode_provider=lambda: [],
                    harness_probe=lambda: statuses,
                )
            with mock.patch.object(
                model_catalog, "build",
                side_effect=RuntimeError("older refresh failed"),
            ), mock.patch.object(model_catalog, "datetime", clock(base)):
                failed_response = model_catalog.catalog(
                    refresh=True, con=failed_con,
                    opencode_provider=lambda: [],
                    harness_probe=lambda: statuses,
                )
            cached = json.loads(model_catalog.CACHE.read_text())

        generations = failed_con.execute(
            "SELECT generation_id,state,error_summary "
            "FROM model_catalog_generations ORDER BY completed_at,generation_id"
        ).fetchall()
        fresh_routes = failed_con.execute(
            "SELECT selector,generation_id,stale,last_error FROM model_routes "
            "WHERE stale=0 ORDER BY selector"
        ).fetchall()
        winner_generation = winner_response["catalogue_generation"]
        for payload in (winner_response, failed_response, cached):
            self.assertEqual(payload["catalogue_generation"], winner_generation)
            self.assertEqual(
                payload["harnesses"]["codex"]["models"][0]["id"],
                "winner-route",
            )
            self.assertFalse(payload["stale"])
        self.assertEqual(
            [row["state"] for row in generations], ["failed", "successful"]
        )
        self.assertEqual(
            json.loads(generations[0]["error_summary"])["error"],
            "older refresh failed",
        )
        self.assertEqual(
            [tuple(row) for row in fresh_routes],
            [("winner-route", winner_generation, 0, None)],
        )

    def test_publication_owner_excludes_commit_after_final_authority_read(self):
        cases = ("successful", "failed")
        for challenger_state in cases:
            with self.subTest(challenger_state=challenger_state), \
                    tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "routes.db"
                observer = route_schema(db_path)
                self.addCleanup(observer.close)
                older = self.payload("owner-older")
                challenger = self.payload("owner-newer")
                older.pop("verification")
                challenger.pop("verification")
                statuses = {"codex": self.status()}
                base = datetime.now(timezone.utc)
                authority_read = threading.Event()
                release_owner = threading.Event()
                challenger_waiting = threading.Event()
                responses = {}
                failures = []
                real_authority = model_catalog._authoritative_generation
                real_flock = model_catalog.fcntl.flock

                clock = mock.Mock()
                clock.now.side_effect = lambda *_args, **_kwargs: (
                    base + timedelta(minutes=1)
                    if threading.current_thread().name == "challenger"
                    else base
                )
                clock.fromisoformat.side_effect = datetime.fromisoformat

                def build_for_thread(*_args, **_kwargs):
                    if threading.current_thread().name == "challenger":
                        if challenger_state == "failed":
                            raise RuntimeError("newer refresh failed")
                        return challenger
                    return older

                def pause_owner_after_authority_read(con):
                    authority = real_authority(con)
                    if threading.current_thread().name == "owner":
                        authority_read.set()
                        if not release_owner.wait(5):
                            raise AssertionError("owner publication was not resumed")
                    return authority

                def observe_challenger_lock(fd, operation):
                    if threading.current_thread().name == "challenger":
                        challenger_waiting.set()
                    return real_flock(fd, operation)

                def refresh(name):
                    con = sqlite3.connect(db_path, timeout=5)
                    con.row_factory = sqlite3.Row
                    try:
                        responses[name] = model_catalog.catalog(
                            refresh=True, con=con,
                            opencode_provider=lambda: [],
                            harness_probe=lambda: statuses,
                        )
                    except Exception as exc:  # noqa: BLE001
                        failures.append(exc)
                    finally:
                        con.close()

                with mock.patch.object(
                    model_catalog, "CACHE", Path(tmp) / "model_catalog.json"
                ), mock.patch.object(
                    model_catalog, "build", side_effect=build_for_thread
                ), mock.patch.object(
                    model_catalog, "datetime", clock
                ), mock.patch.object(
                    model_catalog, "_authoritative_generation",
                    side_effect=pause_owner_after_authority_read,
                ), mock.patch.object(
                    model_catalog.fcntl, "flock",
                    side_effect=observe_challenger_lock,
                ):
                    owner_thread = threading.Thread(
                        target=refresh, args=("owner",), name="owner"
                    )
                    challenger_thread = threading.Thread(
                        target=refresh, args=("challenger",), name="challenger"
                    )
                    owner_thread.start()
                    try:
                        self.assertTrue(authority_read.wait(5))
                        challenger_thread.start()
                        self.assertTrue(challenger_waiting.wait(5))
                        generations_while_owned = observer.execute(
                            "SELECT generation_id FROM model_catalog_generations"
                        ).fetchall()
                        self.assertEqual(len(generations_while_owned), 1)
                        self.assertFalse(model_catalog.CACHE.exists())
                        self.assertNotIn("challenger", responses)
                    finally:
                        release_owner.set()
                        owner_thread.join(5)
                        if challenger_thread.ident is not None:
                            challenger_thread.join(5)
                    self.assertFalse(owner_thread.is_alive())
                    self.assertFalse(challenger_thread.is_alive())
                    cached = json.loads(model_catalog.CACHE.read_text())

                self.assertEqual(failures, [])
                generations = observer.execute(
                    "SELECT generation_id,state FROM model_catalog_generations "
                    "ORDER BY completed_at,generation_id"
                ).fetchall()
                fresh_routes = observer.execute(
                    "SELECT selector,generation_id,stale,last_error "
                    "FROM model_routes WHERE stale=0 ORDER BY selector"
                ).fetchall()
                winner = responses["challenger"]
                self.assertTrue(responses["owner"]["generation_published"])
                self.assertTrue(winner["generation_published"])
                self.assertEqual(len(generations), 2)
                self.assertEqual(
                    generations[-1]["generation_id"],
                    winner["catalogue_generation"],
                )
                self.assertEqual(
                    cached["catalogue_generation"],
                    winner["catalogue_generation"],
                )
                self.assertEqual(cached["generation_state"], challenger_state)
                if challenger_state == "successful":
                    self.assertEqual(
                        [tuple(row) for row in fresh_routes],
                        [("owner-newer", winner["catalogue_generation"], 0, None)],
                    )
                else:
                    self.assertEqual(fresh_routes, [])

    def test_freshness_failure_respects_outer_rollback(self):
        model_catalog.persist_routes(self.con, self.payload("rollback-route"))
        row = dict(self.con.execute(
            "SELECT * FROM model_routes WHERE selector='rollback-route'"
        ).fetchone())
        self.con.execute("INSERT INTO sprints VALUES (99,'prepared')")

        with (
            mock.patch.object(
                model_catalog, "controlled_route_evidence",
                return_value=controlled_observation(
                    "wrong-fingerprint"
                ),
            ) as collector,
            self.assertRaises(route_bindings.RouteResolutionError) as raised,
        ):
            route_bindings.require_fresh_route(
                self.con, row, "codex", "rollback-route",
            )
        collector.assert_called_once_with("codex", "rollback-route")
        staged = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE selector='rollback-route'"
        ).fetchone()
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")
        self.assertEqual(staged["stale"], 1)
        self.assertEqual(
            staged["last_error"],
            "thinking_evidence_stale: Installed route source changed after "
            "refresh; remediation: sc models refresh",
        )

        self.con.rollback()
        stored = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE selector='rollback-route'"
        ).fetchone()
        unrelated = self.con.execute(
            "SELECT COUNT(*) FROM sprints WHERE sprint_id=99"
        ).fetchone()[0]
        self.assertEqual(tuple(stored), (0, None))
        self.assertEqual(unrelated, 0)


class ParticipantRevisionTest(unittest.TestCase):
    CONTROLLED_SOURCE_FINGERPRINT = "2" * 64
    CONTROLLED_HARNESS_VERSION = "0.145.0"

    def setUp(self):
        self.con = route_schema()
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO sprints VALUES (1,'prepared')")
        self.con.execute("INSERT INTO sprint_participants VALUES (10,1,NULL)")
        self.con.execute("INSERT INTO sprint_participants VALUES (11,1,NULL)")
        self.store = route_bindings.ParticipantRouteBindingStore(self.con)
        self.binding, self.digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest", None,
            runtime_status=compatible_runtime(),
        )

    @staticmethod
    def controlled_binding() -> dict:
        binding, _ = resolve_controlled_v2(
            BindingIdentityTest.controlled_row(), "codex", "gpt-test", "high",
            now=BindingIdentityTest.NOW,
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )
        return binding

    def insert_raw(self, binding: dict, binding_digest: str | None = None) -> None:
        values = [binding[key] for key in route_bindings.BINDING_KEYS]
        self.con.execute(
            "INSERT INTO sprint_participant_route_bindings ("
            "participant_id,route_revision,contract_version,control_state,harness,"
            "requested_model,provider_model,requested_effort,effective_effort,"
            "native_variant_id,transport,catalogue_generation,evidence_digest,"
            "selector_binding,adapter_metadata,binding_json,binding_digest"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                10, 1, *values[:11],
                route_bindings.canonical_json(binding["selector_binding"])
                if binding["selector_binding"] is not None else None,
                route_bindings.canonical_json(binding["adapter_metadata"]),
                route_bindings.canonical_json(binding),
                binding_digest or route_bindings.digest_json(binding),
            ),
        )

    def stored_binding(self, binding_id: int) -> tuple[sqlite3.Row, dict]:
        rows = self.con.execute(
            "SELECT participant_id,route_revision,control_state,harness,"
            "binding_json,binding_digest,source_fingerprint,harness_version FROM "
            "sprint_participant_route_bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        decoded = json.loads(row["binding_json"])
        self.assertEqual(tuple(decoded), tuple(sorted(route_bindings.BINDING_KEYS)))
        self.assertNotEqual(tuple(decoded), route_bindings.BINDING_KEYS)
        self.assertEqual(route_bindings.digest_json(decoded), row["binding_digest"])
        route_bindings.validate_v2_binding(decoded)
        return row, decoded

    def test_uncontrolled_runtime_rejection_creates_no_binding_or_pointer(self):
        rejected_statuses = (
            None,
            {
                "version": None, "compatibility": None,
                "error": "HARNESS_UNAVAILABLE",
            },
            {
                "version": "not-a-version", "compatibility": "verified",
                "error": None,
            },
            {
                "version": "1.0.0", "compatibility": None,
                "error": "HARNESS_VERSION_UNSUPPORTED",
            },
            {
                "version": "99.0.0", "compatibility": "newer-unverified",
                "error": None,
            },
        )
        for harness, model in (("claude", None), ("vibe", "devstral-latest")):
            for status in rejected_statuses:
                with self.subTest(harness=harness, runtime_status=status):
                    binding = digest = None
                    with self.assertRaises(
                        route_bindings.RouteResolutionError
                    ) as raised:
                        binding, digest = route_bindings.resolve_v2(
                            None, harness, model, runtime_status=status
                        )
                    self.assertEqual(
                        raised.exception.code, "thinking_evidence_missing"
                    )
                    self.assertIsNone(binding)
                    self.assertIsNone(digest)
                    stored = self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participant_route_bindings"
                    ).fetchone()[0]
                    pointers = self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participants "
                        "WHERE active_route_binding_id IS NOT NULL"
                    ).fetchone()[0]
                    self.assertEqual(stored, 0)
                    self.assertEqual(pointers, 0)

    def test_compatible_uncontrolled_bindings_persist_for_both_states(self):
        default, default_digest = route_bindings.resolve_v2(
            None, "claude", None,
            runtime_status=compatible_runtime("2.1.223"),
        )
        vibe, vibe_digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest",
            runtime_status=compatible_runtime(),
        )

        default_receipt = self.store.bind(
            10, default, default_digest, transition="arm",
            runtime_status=compatible_runtime("2.1.223"),
        )
        vibe_receipt = self.store.bind(
            11, vibe, vibe_digest, transition="arm",
            runtime_status=compatible_runtime(),
        )
        rows = self.con.execute(
            "SELECT participant_id,control_state,requested_model,"
            "requested_effort,catalogue_generation,evidence_digest "
            "FROM sprint_participant_route_bindings ORDER BY participant_id"
        ).fetchall()
        pointers = self.con.execute(
            "SELECT participant_id,active_route_binding_id "
            "FROM sprint_participants ORDER BY participant_id"
        ).fetchall()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (10, "harness-default", None, None, None, None),
                (11, "native-uncontrolled", "devstral-latest", None, None, None),
            ],
        )
        self.assertEqual(
            [tuple(row) for row in pointers],
            [
                (10, default_receipt["binding_id"]),
                (11, vibe_receipt["binding_id"]),
            ],
        )

    def test_runtime_evidence_is_execution_seat_scoped_not_range_gated(self):
        host_scope = {"runtime": "host", "runtime_identity": "host:seat-a"}
        sandbox_scope = {
            "runtime": "sandbox", "runtime_identity": "sandbox:seat-b",
        }
        good = compatible_runtime(scope=host_scope)
        rejected = (
            ("cross-harness", compatible_runtime(
                "2.1.222", harness="claude", scope=host_scope
            ), host_scope),
            ("different-execution-seat", good, sandbox_scope),
        )

        for name, status, expected_scope in rejected:
            with self.subTest(name=name):
                binding = digest = None
                with self.assertRaises(
                    route_bindings.RouteResolutionError
                ) as raised:
                    binding, digest = route_bindings.resolve_v2(
                        None, "vibe", "devstral-latest",
                        runtime_status=status, runtime_scope=expected_scope,
                    )
                self.assertEqual(raised.exception.code, "thinking_evidence_missing")
                self.assertIsNone(binding)
                self.assertIsNone(digest)

                with self.assertRaises(route_bindings.RouteResolutionError):
                    self.store.bind(
                        10, self.binding, self.digest, transition="arm",
                        runtime_status=status, runtime_scope=expected_scope,
                    )
                self.assertEqual(
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participant_route_bindings"
                    ).fetchone()[0],
                    0,
                )

        for name, status in (
            ("older", {**good, "version": "2.21.9", "compatibility": "older"}),
            ("newer", {**good, "version": "2.23.0", "compatibility": "newer-unverified"}),
            ("non-semver", {
                **good, "version": None, "observed_version": "vibe dev-build",
                "compatibility": "non-semver",
            }),
        ):
            with self.subTest(name=name):
                binding, digest = route_bindings.resolve_v2(
                    None, "vibe", "devstral-latest",
                    runtime_status=status, runtime_scope=host_scope,
                )
                self.assertEqual(binding["control_state"], "native-uncontrolled")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                self.assertEqual(
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participants "
                        "WHERE active_route_binding_id IS NOT NULL"
                    ).fetchone()[0],
                    0,
                )

        binding, digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest",
            runtime_status=good, runtime_scope=host_scope,
        )
        receipt = self.store.bind(
            10, binding, digest, transition="arm",
            runtime_status=good, runtime_scope=host_scope,
        )
        self.assertEqual(receipt["route_revision"], 1)
        self.assertEqual(
            self.con.execute(
                "SELECT binding_digest FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            digest,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT active_route_binding_id FROM sprint_participants "
                "WHERE participant_id=10"
            ).fetchone()[0],
            receipt["binding_id"],
        )

    def test_store_cannot_bypass_uncontrolled_runtime_admission(self):
        incompatible = {
            "version": "99.0.0", "compatibility": "newer-unverified",
            "error": None,
        }
        for status in (None, incompatible):
            with self.subTest(runtime_status=status):
                with self.assertRaises(
                    route_bindings.RouteResolutionError
                ) as raised:
                    self.store.bind(
                        10, self.binding, self.digest, transition="arm",
                        runtime_status=status,
                    )
                self.assertEqual(
                    raised.exception.code, "thinking_evidence_missing"
                )
                self.assertEqual(
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participant_route_bindings"
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNone(self.con.execute(
                    "SELECT active_route_binding_id FROM sprint_participants "
                    "WHERE participant_id=10"
                ).fetchone()[0])

    @staticmethod
    def invalid_bindings() -> list[tuple[str, dict]]:
        controlled = ParticipantRevisionTest.controlled_binding()
        uncontrolled, _ = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest", None,
            runtime_status=compatible_runtime(),
        )
        harness_default, _ = route_bindings.resolve_v2(
            None, "claude", None, None,
            runtime_status=compatible_runtime("2.1.223"),
        )
        return [
            ("controlled-vibe", {**controlled, "harness": "vibe"}),
            ("wrong-transport", {**controlled, "transport": "native-default"}),
            ("wrong-native-variant", {**controlled, "native_variant_id": "high"}),
            ("missing-selector", {**controlled, "selector_binding": None}),
            ("opencode-missing-native-variant", {
                **controlled,
                "harness": "opencode",
                "transport": "opencode-route-agent",
                "native_variant_id": None,
            }),
            ("uncontrolled-selector", {
                **uncontrolled, "selector_binding": {"selector": "smuggled"},
            }),
            ("uncontrolled-adapter", {
                **uncontrolled, "adapter_metadata": {"effort": "high"},
            }),
            ("unknown-harness-default", {
                **harness_default, "harness": "not-a-harness",
            }),
            ("malformed-generation", {
                **controlled, "catalogue_generation": "not-a-generation",
            }),
            ("malformed-evidence", {
                **controlled, "evidence_digest": "abcd",
            }),
        ]

    def test_arm_then_paused_reroute_appends_and_switches_only_owner(self):
        first = self.store.bind(
            10, self.binding, self.digest, transition="arm",
            runtime_status=compatible_runtime(),
        )
        self.con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=1")
        rerouted, rerouted_digest = route_bindings.resolve_v2(
            None, "vibe", "codestral-latest", None,
            runtime_status=compatible_runtime(),
        )
        second = self.store.bind(
            10, rerouted, rerouted_digest, transition="reroute",
            runtime_status=compatible_runtime(),
        )

        rows = self.con.execute(
            "SELECT binding_id,route_revision,requested_model FROM "
            "sprint_participant_route_bindings WHERE participant_id=10 "
            "ORDER BY route_revision"
        ).fetchall()
        active = self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=10"
        ).fetchone()[0]
        sibling = self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=11"
        ).fetchone()[0]
        self.assertEqual(first["route_revision"], 1)
        self.assertEqual(second["route_revision"], 2)
        self.assertEqual([row["requested_model"] for row in rows],
                         ["devstral-latest", "codestral-latest"])
        self.assertEqual(active, second["binding_id"])
        self.assertIsNone(sibling)

    def test_controlled_binding_survives_store_json_round_trip(self):
        binding = self.controlled_binding()
        digest = route_bindings.digest_json(binding)
        receipt = self.store.bind(
            10,
            binding,
            digest,
            transition="arm",
            source_fingerprint=self.CONTROLLED_SOURCE_FINGERPRINT,
            harness_version=self.CONTROLLED_HARNESS_VERSION,
            harness_support_state="tested",
        )

        row, decoded = self.stored_binding(receipt["binding_id"])
        self.assertEqual(
            (row["participant_id"], row["route_revision"],
             row["control_state"], row["harness"], row["binding_digest"]),
            (10, 1, "controlled", "codex", digest),
        )
        self.assertEqual(decoded, binding)
        self.assertEqual(
            (row["source_fingerprint"], row["harness_version"]),
            (
                self.CONTROLLED_SOURCE_FINGERPRINT,
                self.CONTROLLED_HARNESS_VERSION,
            ),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT active_route_binding_id FROM sprint_participants "
                "WHERE participant_id=10"
            ).fetchone()[0],
            receipt["binding_id"],
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=11"
        ).fetchone()[0])

    def test_deepseek_controlled_binding_survives_migrated_store_round_trip(self):
        binding, digest = resolve_controlled_v2(
            BindingIdentityTest.deepseek_row(),
            "deepseek",
            "deepseek-v4-pro",
            "default",
            now=BindingIdentityTest.NOW,
            runtime_status=compatible_runtime("0.1.0rc7", harness="deepseek"),
        )

        receipt = self.store.bind(
            10,
            binding,
            digest,
            transition="arm",
            source_fingerprint=self.CONTROLLED_SOURCE_FINGERPRINT,
            harness_version="0.1.0rc7",
            harness_support_state="tested",
        )

        row, decoded = self.stored_binding(receipt["binding_id"])
        self.assertEqual(
            (row["control_state"], row["harness"], row["binding_digest"]),
            ("controlled", "deepseek", digest),
        )
        self.assertEqual(decoded, binding)
        self.assertEqual(decoded["adapter_metadata"]["provider_options"], {
            "omit": ["thinking", "reasoning_effort"], "set": {},
        })
        self.assertEqual(
            self.con.execute(
                "SELECT active_route_binding_id FROM sprint_participants "
                "WHERE participant_id=10"
            ).fetchone()[0],
            receipt["binding_id"],
        )
        self.assertEqual(
            [tuple(row) for row in self.con.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name='sprint_participant_route_bindings'"
            )],
            [("sprint_participant_route_bindings", receipt["binding_id"])],
        )

        sibling = self.store.bind(
            11,
            binding,
            digest,
            transition="arm",
            source_fingerprint=self.CONTROLLED_SOURCE_FINGERPRINT,
            harness_version="0.1.0rc7",
            harness_support_state="tested",
        )
        self.assertGreater(sibling["binding_id"], receipt["binding_id"])
        self.assertEqual(
            [tuple(row) for row in self.con.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name='sprint_participant_route_bindings'"
            )],
            [("sprint_participant_route_bindings", sibling["binding_id"])],
        )

    def test_deepseek_unsupported_effort_cannot_enter_migrated_store(self):
        binding, _ = resolve_controlled_v2(
            BindingIdentityTest.deepseek_row(),
            "deepseek",
            "deepseek-v4-pro",
            "high",
            now=BindingIdentityTest.NOW,
            runtime_status=compatible_runtime("0.1.0rc7", harness="deepseek"),
        )
        binding = {
            **binding,
            "requested_effort": "medium",
            "effective_effort": "medium",
            "adapter_metadata": {
                **binding["adapter_metadata"],
                "provider_options": {
                    "omit": [],
                    "set": {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "medium",
                    },
                },
            },
        }
        digest = route_bindings.digest_json(binding)

        with self.assertRaises(route_bindings.RouteResolutionError):
            self.store.bind(
                10,
                binding,
                digest,
                transition="arm",
                source_fingerprint=self.CONTROLLED_SOURCE_FINGERPRINT,
                harness_version="0.1.0rc7",
                harness_support_state="tested",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_raw(binding, digest)

        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=10"
        ).fetchone()[0])

    def test_deepseek_refuses_harness_default_without_writing(self):
        with self.assertRaises(route_bindings.RouteResolutionError) as refused:
            route_bindings.resolve_v2(
                None,
                "deepseek",
                None,
                runtime_status=compatible_runtime(
                    "0.1.0rc7", harness="deepseek"
                ),
            )

        self.assertEqual(refused.exception.code, "thinking_evidence_missing")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=10"
        ).fetchone()[0])

    def test_typed_uncontrolled_binding_survives_store_json_round_trip(self):
        receipt = self.store.bind(
            10, self.binding, self.digest, transition="arm",
            runtime_status=compatible_runtime(),
        )

        row, decoded = self.stored_binding(receipt["binding_id"])
        self.assertEqual(
            (row["participant_id"], row["route_revision"],
             row["control_state"], row["harness"], row["binding_digest"]),
            (10, 1, "native-uncontrolled", "vibe", self.digest),
        )
        self.assertEqual(decoded, self.binding)
        self.assertIsNone(row["source_fingerprint"])
        self.assertEqual(row["harness_version"], "2.22.0")
        self.assertEqual(
            self.con.execute(
                "SELECT active_route_binding_id FROM sprint_participants "
                "WHERE participant_id=10"
            ).fetchone()[0],
            receipt["binding_id"],
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=11"
        ).fetchone()[0])

    def test_controlled_binding_requires_immutable_source_provenance(self):
        binding = self.controlled_binding()
        digest = route_bindings.digest_json(binding)

        with self.assertRaisesRegex(ValueError, "immutable source provenance"):
            self.store.bind(10, binding, digest, transition="arm")

        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT active_route_binding_id FROM sprint_participants "
                "WHERE participant_id=10"
            ).fetchone()[0]
        )

    def test_validator_accepts_reordered_exact_keys_and_rejects_key_drift(self):
        controlled = self.controlled_binding()
        reordered = dict(reversed(tuple(controlled.items())))

        route_bindings.validate_v2_binding(reordered)
        self.assertEqual(
            route_bindings.digest_json(reordered),
            route_bindings.digest_json(controlled),
        )
        invalid = (
            ("missing", {key: value for key, value in controlled.items()
                         if key != "adapter_metadata"}),
            ("extra", {**controlled, "unexpected": None}),
        )
        for name, binding in invalid:
            with self.subTest(name=name):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    route_bindings.validate_v2_binding(binding)
                self.assertEqual(raised.exception.code, "thinking_evidence_missing")
                self.assertEqual(
                    raised.exception.details,
                    {"reason": "binding must contain exactly the canonical fixed keys"},
                )

    def test_prepared_reroute_paused_arm_cross_owner_and_mutation_are_refused(self):
        first = self.store.bind(
            10, self.binding, self.digest, transition="arm",
            runtime_status=compatible_runtime(),
        )
        with self.assertRaisesRegex(ValueError, "paused Sprint"):
            self.store.bind(
                11, self.binding, self.digest, transition="reroute",
                runtime_status=compatible_runtime(),
            )
        self.con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=1")
        with self.assertRaisesRegex(ValueError, "unbound prepared"):
            self.store.bind(
                11, self.binding, self.digest, transition="arm",
                runtime_status=compatible_runtime(),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprint_participants SET active_route_binding_id=? "
                "WHERE participant_id=11", (first["binding_id"],)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE sprint_participant_route_bindings SET route_revision=9 "
                "WHERE binding_id=?", (first["binding_id"],)
            )

    def test_store_rejects_invalid_bindings_without_row_or_active_pointer(self):
        for name, binding in self.invalid_bindings():
            with self.subTest(name=name):
                with self.assertRaises(route_bindings.RouteResolutionError):
                    self.store.bind(
                        10, binding, route_bindings.digest_json(binding),
                        transition="arm",
                    )
                self.assertEqual(
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_participant_route_bindings"
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNone(self.con.execute(
                    "SELECT active_route_binding_id FROM sprint_participants "
                    "WHERE participant_id=10"
                ).fetchone()[0])

        controlled = self.controlled_binding()
        with self.assertRaisesRegex(ValueError, "canonical v2 contract"):
            self.store.bind(10, controlled, "abcd", transition="arm")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=10"
        ).fetchone()[0])

    def test_database_independently_rejects_invalid_binding_states(self):
        for name, binding in self.invalid_bindings():
            with self.subTest(name=name):
                self.con.execute("SAVEPOINT invalid_binding")
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_raw(binding)
                    self.assertEqual(
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_participant_route_bindings"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertIsNone(self.con.execute(
                        "SELECT active_route_binding_id FROM sprint_participants "
                        "WHERE participant_id=10"
                    ).fetchone()[0])
                finally:
                    self.con.execute("ROLLBACK TO invalid_binding")
                    self.con.execute("RELEASE invalid_binding")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_raw(self.controlled_binding(), binding_digest="abcd")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_route_bindings"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.con.execute(
            "SELECT active_route_binding_id FROM sprint_participants "
            "WHERE participant_id=10"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
