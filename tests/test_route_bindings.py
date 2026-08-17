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
import route_bindings  # noqa: E402
import models as routes_cli  # noqa: E402


def compatible_runtime(version: str = "2.22.0", *, harness: str | None = None,
                       scope: dict | None = None) -> dict:
    harness = harness or ("claude" if version.startswith("2.1.") else "vibe")
    ranges = {
        "claude": ("2.1.220", "2.2.0", "2.1.222"),
        "codex": ("0.145.0", "0.147.0", "0.145.0"),
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


def controlled_source(
    fingerprint: str | None = "2" * 64,
    *,
    harness: str = "codex",
    selector: str = "gpt-test",
    version: str | None = None,
    scope: dict | None = None,
) -> route_bindings.SourceEvidence:
    versions = {
        "claude": "2.1.222", "codex": "0.145.0",
        "kimi": "0.33.0", "opencode": "1.18.9",
    }
    scope = scope or route_bindings.harness_versions.runtime_scope()
    return route_bindings.SourceEvidence(
        harness=harness,
        selector=selector,
        runtime=scope["runtime"],
        runtime_identity=scope["runtime_identity"],
        harness_version=version or versions[harness],
        fingerprint=fingerprint,
    )
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
    return con


class BindingIdentityTest(unittest.TestCase):
    NOW = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)

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
            }),
            selector_binding=json.dumps({
                "kind": "exact-model", "selector": "provider/model",
            }),
            adapter_metadata=json.dumps({
                "variant_options": {"reasoningEffort": "high"},
            }),
        )

    def test_controlled_omitted_and_explicit_high_have_same_fixed_identity(self):
        implicit, implicit_digest = route_bindings.resolve_v2(
            self.controlled_row(), "Codex", "gpt-test", now=self.NOW,
            source_evidence=controlled_source(),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )
        explicit, explicit_digest = route_bindings.resolve_v2(
            self.controlled_row(), "codex", "gpt-test", " HIGH ", now=self.NOW,
            source_evidence=controlled_source(),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )

        self.assertEqual(tuple(implicit), route_bindings.BINDING_KEYS)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit_digest, explicit_digest)
        self.assertEqual(implicit["requested_effort"], "high")
        self.assertEqual(implicit["evidence_digest"], "4" * 64)
        self.assertEqual(implicit["control_state"], "controlled")

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
        canonical, canonical_digest = route_bindings.resolve_v2(
            self.opencode_row(), "opencode", "provider/model", "k",
            now=self.NOW, source_evidence=controlled_source(
                harness="opencode", selector="provider/model"
            ),
            runtime_status=compatible_runtime("1.18.9", harness="opencode"),
        )
        mixed_case, mixed_case_digest = route_bindings.resolve_v2(
            self.opencode_row(), "opencode", "provider/model", " K ",
            now=self.NOW, source_evidence=controlled_source(
                harness="opencode", selector="provider/model"
            ),
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
                    route_bindings.resolve_v2(
                        self.opencode_row(), "opencode", "provider/model",
                        confusable, now=self.NOW,
                        source_evidence=controlled_source(
                            harness="opencode", selector="provider/model"
                        ),
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
                    route_bindings.resolve_v2(
                        self.controlled_row(**overrides), "codex", "gpt-test", effort,
                        now=self.NOW,
                        source_evidence=controlled_source(fingerprint),
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

    def test_controlled_route_requires_matching_compatible_harness_version(self):
        cases = (
            ({"harness_compatibility": None}, "thinking_evidence_missing"),
            ({"harness_compatibility": "newer-unverified"},
             "thinking_evidence_missing"),
            ({"harness_version": "0.144.0"}, "thinking_evidence_stale"),
        )
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    route_bindings.resolve_v2(
                        self.controlled_row(**overrides), "codex", "gpt-test",
                        now=self.NOW,
                    )
                self.assertEqual(raised.exception.code, code)

    def test_controlled_route_requires_scoped_source_evidence(self):
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.resolve_v2(
                self.controlled_row(), "codex", "gpt-test", now=self.NOW
            )
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")

    def test_controlled_route_requires_exact_execution_runtime_evidence(self):
        scope = {"runtime": "sandbox", "runtime_identity": "sandbox:image-a"}
        good = compatible_runtime("0.145.0", harness="codex", scope=scope)
        binding, digest = route_bindings.resolve_v2(
            self.controlled_row(), "codex", "gpt-test", now=self.NOW,
            source_evidence=controlled_source(scope=scope),
            runtime_status=good, runtime_scope=scope,
        )
        self.assertEqual(binding["harness"], "codex")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

        cases = {
            "missing": None,
            "other-harness": compatible_runtime(
                "2.1.222", harness="claude", scope=scope
            ),
            "other-seat": compatible_runtime(
                "0.145.0", harness="codex",
                scope={
                    "runtime": "sandbox",
                    "runtime_identity": "sandbox:image-b",
                },
            ),
            "version-drift": compatible_runtime(
                "0.146.0", harness="codex", scope=scope
            ),
        }
        for name, status in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(
                    route_bindings.RouteResolutionError
                ) as raised:
                    route_bindings.resolve_v2(
                        self.controlled_row(), "codex", "gpt-test",
                        now=self.NOW,
                        source_evidence=controlled_source(scope=scope),
                        runtime_status=status, runtime_scope=scope,
                    )
                self.assertEqual(
                    raised.exception.code, "thinking_evidence_stale"
                )

    def test_controlled_source_proof_must_match_route_and_runtime(self):
        scope = {"runtime": "sandbox", "runtime_identity": "sandbox:image-a"}
        runtime = compatible_runtime(
            "0.145.0", harness="codex", scope=scope
        )
        cases = {
            "untyped-stored-fingerprint": {
                "harness": "codex",
                "selector": "gpt-test",
                **scope,
                "harness_version": "0.145.0",
                "fingerprint": "2" * 64,
            },
            "other-harness": controlled_source(
                harness="claude", selector="gpt-test", scope=scope
            ),
            "other-selector": controlled_source(
                selector="other-model", scope=scope
            ),
            "other-seat": controlled_source(scope={
                "runtime": "sandbox",
                "runtime_identity": "sandbox:image-b",
            }),
            "other-version": controlled_source(
                version="0.146.0", scope=scope
            ),
        }
        for name, source in cases.items():
            with self.subTest(name=name), self.assertRaises(
                route_bindings.RouteResolutionError
            ) as raised:
                route_bindings.resolve_v2(
                    self.controlled_row(), "codex", "gpt-test",
                    now=self.NOW, source_evidence=source,
                    runtime_status=runtime, runtime_scope=scope,
                )
            self.assertEqual(raised.exception.code, "thinking_evidence_stale")
            self.assertFalse(raised.exception.details["persist_route_stale"])

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

    def test_incompatible_harnesses_never_publish_controlled_routes(self):
        cases = (
            ("missing", None, self.status(
                version=None, compatibility=None, error="HARNESS_UNAVAILABLE"
            )),
            ("below", "codex-cli 0.144.0", self.status(
                version="0.144.0", compatibility=None,
                error="HARNESS_VERSION_UNSUPPORTED",
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
            "SELECT harness,selector FROM model_routes ORDER BY selector"
        ).fetchall()
        self.assertEqual([row["state"] for row in generations],
                         ["successful", "successful", "successful"])
        self.assertEqual(
            [json.loads(row["harness_versions"])["codex"]["version"]
             for row in generations],
            [None, "0.144.0", "0.147.0"],
        )
        self.assertEqual(routes, [])

    def test_incompatible_refresh_stales_prior_compatible_route(self):
        first = self.payload("carried")
        model_catalog.persist_routes(self.con, first)
        old_fingerprint = self.con.execute(
            "SELECT source_fingerprint FROM model_routes WHERE selector='carried'"
        ).fetchone()[0]

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
        self.assertEqual(row["stale"], 1)
        self.assertIn("newer-unverified", row["last_error"])
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.resolve_v2(
                row, "codex", "carried",
            )
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")

    def test_live_fingerprint_refuses_unverified_installed_version(self):
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
        self.assertIsNone(fingerprint)

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

                got = routes_cli.resolve(
                    con, "Codex", name, now=datetime.now(timezone.utc),
                    source_evidence=controlled_source(
                        fingerprint, selector=name
                    ),
                    runtime_status=compatible_runtime(
                        "0.145.0", harness="codex"
                    ),
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
                refused_again = routes_cli.resolve(
                    con, "codex", name,
                    source_evidence=controlled_source(
                        row["source_fingerprint"], selector=name
                    ),
                    runtime_status=compatible_runtime(
                        "0.145.0", harness="codex"
                    ),
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
        runtime = compatible_runtime(
            "0.145.0", harness="codex", scope=sandbox
        )
        wrong_source = controlled_source(
            row["source_fingerprint"], selector="seat-bound",
            scope={
                "runtime": "sandbox",
                "runtime_identity": "sandbox:image-b",
            },
        )

        got = routes_cli.resolve(
            self.con, "codex", "seat-bound",
            source_evidence=wrong_source,
            runtime_status=runtime, runtime_scope=sandbox,
        )
        stored = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE selector='seat-bound'"
        ).fetchone()

        self.assertEqual(got["code"], "thinking_evidence_stale")
        self.assertNotIn("binding", got)
        self.assertNotIn("binding_digest", got)
        self.assertNotIn("command", got)
        self.assertEqual(tuple(stored), (0, None))

    def test_authoritative_resolution_stales_version_drift(self):
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

        got = routes_cli.resolve(
            self.con, "codex", "version-drift",
            source_evidence=controlled_source(
                row["source_fingerprint"], selector="version-drift"
            ),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
        )
        stored = self.con.execute(
            "SELECT stale,last_error FROM model_routes "
            "WHERE selector='version-drift'"
        ).fetchone()

        self.assertEqual(got["code"], "thinking_evidence_stale")
        self.assertEqual(tuple(stored), (
            1,
            "thinking_evidence_stale: Installed harness version changed after "
            "refresh; remediation: sc models refresh",
        ))

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

        got = routes_cli.resolve(
            self.con, "codex", "superseded",
            source_evidence=controlled_source(
                row["source_fingerprint"], selector="superseded"
            ),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
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
                "source_evidence": controlled_source(
                    observed["source_fingerprint"],
                    selector="generation-race",
                ),
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
        retried = routes_cli.resolve(
            resolver, "codex", "generation-race",
            source_evidence=controlled_source(
                stored["source_fingerprint"], selector="generation-race",
                version="0.146.0",
            ),
            runtime_status=compatible_runtime("0.146.0", harness="codex"),
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
        resolved = routes_cli.resolve(
            late, "codex", "ordered-success",
            source_evidence=controlled_source(
                route["source_fingerprint"], selector="ordered-success"
            ),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
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
        resolved = routes_cli.resolve(
            late, "codex", "ordered-failure",
            source_evidence=controlled_source(
                route["source_fingerprint"], selector="ordered-failure"
            ),
            runtime_status=compatible_runtime("0.145.0", harness="codex"),
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

        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.require_fresh_route(
                self.con, row, "codex", "rollback-route",
                source_evidence=controlled_source(
                    "wrong-fingerprint", selector="rollback-route"
                ),
                runtime_status=compatible_runtime(
                    "0.145.0", harness="codex"
                ),
            )
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
        binding, _ = route_bindings.resolve_v2(
            BindingIdentityTest.controlled_row(), "codex", "gpt-test", "high",
            now=BindingIdentityTest.NOW,
            source_evidence=controlled_source(),
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
            "binding_json,binding_digest FROM "
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

    def test_runtime_evidence_is_harness_range_and_execution_seat_scoped(self):
        host_scope = {"runtime": "host", "runtime_identity": "host:seat-a"}
        sandbox_scope = {
            "runtime": "sandbox", "runtime_identity": "sandbox:seat-b",
        }
        good = compatible_runtime(scope=host_scope)
        rejected = (
            ("cross-harness", compatible_runtime(
                "2.1.222", harness="claude", scope=host_scope
            ), host_scope),
            ("below-minimum-labelled-verified", {
                **good, "version": "2.21.9", "compatibility": "verified",
            }, host_scope),
            ("maximum-labelled-supported", {
                **good, "version": "2.23.0", "compatibility": "supported",
            }, host_scope),
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
        receipt = self.store.bind(10, binding, digest, transition="arm")

        row, decoded = self.stored_binding(receipt["binding_id"])
        self.assertEqual(
            (row["participant_id"], row["route_revision"],
             row["control_state"], row["harness"], row["binding_digest"]),
            (10, 1, "controlled", "codex", digest),
        )
        self.assertEqual(decoded, binding)
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
