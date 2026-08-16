#!/usr/bin/env python3
"""Focused Feature #54 binding, evidence-generation, and revision contract."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import model_catalog  # noqa: E402
import route_bindings  # noqa: E402


def route_schema() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
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

    def test_controlled_omitted_and_explicit_high_have_same_fixed_identity(self):
        implicit, implicit_digest = route_bindings.resolve_v2(
            self.controlled_row(), "Codex", "gpt-test", now=self.NOW,
            current_source_fingerprint="2" * 64,
        )
        explicit, explicit_digest = route_bindings.resolve_v2(
            self.controlled_row(), "codex", "gpt-test", " HIGH ", now=self.NOW,
            current_source_fingerprint="2" * 64,
        )

        self.assertEqual(tuple(implicit), route_bindings.BINDING_KEYS)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit_digest, explicit_digest)
        self.assertEqual(implicit["requested_effort"], "high")
        self.assertEqual(implicit["evidence_digest"], "4" * 64)
        self.assertEqual(implicit["control_state"], "controlled")

    def test_uncontrolled_bindings_encode_every_inapplicable_value_as_null(self):
        default, default_digest = route_bindings.resolve_v2(
            None, "claude", None, None, now=self.NOW
        )
        vibe, vibe_digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest", None, now=self.NOW
        )
        default_replay, replay_digest = route_bindings.resolve_v2(
            None, "claude", None, None,
            now=self.NOW + timedelta(days=90),
        )

        self.assertEqual(default, default_replay)
        self.assertEqual(default_digest, replay_digest)
        self.assertNotEqual(default_digest, vibe_digest)
        self.assertEqual(default["control_state"], "harness-default")
        self.assertEqual(vibe["control_state"], "native-uncontrolled")
        for binding in (default, vibe):
            self.assertIsNone(binding["requested_effort"])
            self.assertIsNone(binding["effective_effort"])
            self.assertIsNone(binding["catalogue_generation"])
            self.assertIsNone(binding["evidence_digest"])
            self.assertEqual(binding["transport"], "native-default")

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
                        now=self.NOW, current_source_fingerprint=fingerprint,
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
                        current_source_fingerprint="2" * 64,
                    )
                self.assertEqual(raised.exception.code, "thinking_evidence_missing")
                self.assertIn("does not match", raised.exception.message)

    def test_controlled_route_requires_matching_compatible_harness_version(self):
        cases = (
            {"harness_compatibility": None},
            {"harness_compatibility": "newer-unverified"},
            {"harness_version": "0.144.0"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(route_bindings.RouteResolutionError) as raised:
                    route_bindings.resolve_v2(
                        self.controlled_row(**overrides), "codex", "gpt-test",
                        now=self.NOW, current_source_fingerprint="2" * 64,
                    )
                self.assertEqual(raised.exception.code, "thinking_evidence_missing")

    def test_controlled_route_requires_current_source_fingerprint(self):
        with self.assertRaises(route_bindings.RouteResolutionError) as raised:
            route_bindings.resolve_v2(
                self.controlled_row(), "codex", "gpt-test", now=self.NOW
            )
        self.assertEqual(raised.exception.code, "thinking_evidence_stale")

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
                current_source_fingerprint=old_fingerprint,
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


class ParticipantRevisionTest(unittest.TestCase):
    def setUp(self):
        self.con = route_schema()
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO sprints VALUES (1,'prepared')")
        self.con.execute("INSERT INTO sprint_participants VALUES (10,1,NULL)")
        self.con.execute("INSERT INTO sprint_participants VALUES (11,1,NULL)")
        self.store = route_bindings.ParticipantRouteBindingStore(self.con)
        self.binding, self.digest = route_bindings.resolve_v2(
            None, "vibe", "devstral-latest", None
        )

    def test_arm_then_paused_reroute_appends_and_switches_only_owner(self):
        first = self.store.bind(10, self.binding, self.digest, transition="arm")
        self.con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=1")
        rerouted, rerouted_digest = route_bindings.resolve_v2(
            None, "vibe", "codestral-latest", None
        )
        second = self.store.bind(
            10, rerouted, rerouted_digest, transition="reroute"
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

    def test_prepared_reroute_paused_arm_cross_owner_and_mutation_are_refused(self):
        first = self.store.bind(10, self.binding, self.digest, transition="arm")
        with self.assertRaisesRegex(ValueError, "paused Sprint"):
            self.store.bind(11, self.binding, self.digest, transition="reroute")
        self.con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=1")
        with self.assertRaisesRegex(ValueError, "unbound prepared"):
            self.store.bind(11, self.binding, self.digest, transition="arm")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
