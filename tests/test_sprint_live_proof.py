"""Stage 10 compositional proof for serial and parallel Sprints v2 runs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ACCEPTANCE = ROOT / "tests" / "fixtures" / "sprint_v2_acceptance.json"
HANDOFF_ACCEPTANCE = (
    ROOT / "tests" / "fixtures" / "sprint_handoff_hardening_acceptance.json"
)
sys.path[:0] = [
    str(ENGINE / "scripts"),
    str(ENGINE / "api"),
    str(ROOT / "tests"),
]

import active_chat_registry
import db_driver
import mem
import model_catalog
import route_bindings
import route_transport
import run as run_mod
import server
import sprint_participant_chats
import sprint_cli
import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
import sprint_runtime
from conversation_adapters.opencode import OpenCodeAdapter
from conversation_broker import BrokerStore
from conversation_launch import ConversationLaunchPreparer
from github_pull_requests import PullRequest
from sprint_route_binding_support import candidate as route_candidate
from test_sprint_v2_domain import apply_schema

TOKENS = {
    1: "live-dev-1",
    2: "live-review-1",
    3: "live-planner",
    4: "live-dev-2",
    5: "live-review-2",
}


class ScenarioGitHub:
    """Mutable GitHub boundary used to drive production watcher transitions."""

    def __init__(self) -> None:
        self.pull_requests: dict[int, PullRequest] = {}
        self.get_calls: list[int] = []
        self.list_calls = 0

    def set(
        self,
        number: int,
        state: str,
        *,
        checks: str | None = "SUCCESS",
        head_sha: str | None = None,
        base_sha: str = "e" * 40,
    ) -> PullRequest:
        head = head_sha or f"{number:040x}"
        pull_request = PullRequest(
            number=number,
            head_ref=f"live-proof/pr-{number}",
            base_ref="live-proof/base",
            head_sha=head,
            state=state,
            merged_at="2026-08-01T00:00:00Z" if state == "MERGED" else None,
            merge_sha=f"{number + 10000:040x}" if state == "MERGED" else None,
            title=f"Live proof PR {number}",
            url=f"https://github.com/acme/live-proof/pull/{number}",
            review_decision="APPROVED" if state in {"OPEN", "MERGED"} else None,
            checks=checks,
            checks_failed=checks == "FAILURE",
            base_sha=base_sha,
        )
        self.pull_requests[number] = pull_request
        return pull_request

    def get(self, number: int) -> PullRequest:
        self.get_calls.append(number)
        return self.pull_requests[number]

    def list(self) -> list[PullRequest]:
        self.list_calls += 1
        return [self.pull_requests[number] for number in sorted(self.pull_requests)]


class SprintBoundRouteDispatchProof(unittest.TestCase):
    """Cross-layer proof from arm-time binding through native dispatch."""

    GENERATION = "1" * 32
    SUCCESSOR_GENERATION = "2" * 32
    FINGERPRINT = "3" * 64
    SUCCESSOR_FINGERPRINT = "8" * 64
    BOUND_DIGEST = "4" * 64
    SUCCESSOR_DIGEST = "5" * 64
    SELECTOR = "openai/sprint-bound-model"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db_path = self.root / "sprint-bound-dispatch.db"
        seed = sqlite3.connect(self.db_path)
        try:
            apply_schema(seed)
        finally:
            seed.close()
        self.con = db_driver.connect(self.db_path)
        self.addCleanup(self.con.close)
        self._seed_identity(self.con)
        worktrees = self.root / "worktrees"

        def shell_work_dir(shortname: str, _flavor: str | None) -> Path:
            path = worktrees / shortname.lower()
            path.mkdir(parents=True, exist_ok=True)
            return path

        route_path_patch = mock.patch.object(
            sprint_participant_chats.run_mod,
            "shell_work_dir",
            side_effect=shell_work_dir,
        )
        route_path_patch.start()
        self.addCleanup(route_path_patch.stop)

    @staticmethod
    def _seed_identity(con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        con.commit()

    @staticmethod
    def _runtime(harness: str) -> tuple[dict, dict]:
        versions = {"kimi": "0.33.0", "opencode": "1.18.9"}
        compatibility = route_bindings._runtime_manifest_compatibility(
            harness, versions[harness]
        )
        scope = route_bindings.harness_versions.runtime_scope()
        return (
            {
                "harness": harness,
                **scope,
                "version": compatibility.version,
                "compatibility": compatibility.compatibility,
                "minimum_version": compatibility.minimum_version,
                "maximum_version_exclusive": (
                    compatibility.maximum_version_exclusive
                ),
                "verified_version": compatibility.verified_version,
                "error": None,
            },
            scope,
        )

    def _seed_sprint(
        self,
        *,
        harness: str,
        model: str | None,
        effort: str | None,
        con: sqlite3.Connection | None = None,
    ) -> int:
        if con is None:
            con = self.con
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Bound dispatch proof','in_progress')"
            ).lastrowid
        )
        body = f"bound dispatch proof for {harness}"
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Bound dispatch proof',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id,"
            "bound_revision_body) VALUES (?,?,?,?,?)",
            (sprint_id, document_id, revision, approval_id, body),
        )
        con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,?,?,?,?,?)",
            (
                (sprint_id, 3, "planner", harness, model, effort),
                (sprint_id, 1, "developer", harness, model, effort),
                (sprint_id, 2, "reviewer", harness, model, effort),
            ),
        )
        con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
            "VALUES (?,1,2,'Bound delivery','Dispatch the bound route')",
            (sprint_id,),
        )
        con.commit()
        return sprint_id

    def _historical_upgrade_database(
        self,
    ) -> tuple[Path, sqlite3.Connection]:
        path = self.root / "historical-sprint-binding.db"
        seed = sqlite3.connect(path)
        try:
            apply_schema(seed, through="0215_reseed_sprint_binding_guidance.sql")
        finally:
            seed.close()
        con = db_driver.connect(path)
        self.addCleanup(con.close)
        self._seed_identity(con)
        return path, con

    @staticmethod
    def _activate_historical_binding(
        con: sqlite3.Connection,
        sprint_id: int,
        binding: dict,
    ) -> tuple[int, int]:
        participants = {
            str(row["role"]): int(row["participant_id"])
            for row in con.execute(
                "SELECT participant_id,role FROM sprint_participants "
                "WHERE sprint_id=?",
                (sprint_id,),
            )
        }
        participant_id = participants["developer"]
        digest = route_bindings.digest_json(binding)
        binding_id = int(
            con.execute(
                "INSERT INTO sprint_participant_route_bindings ("
                "participant_id,route_revision,contract_version,control_state,"
                "harness,requested_model,provider_model,requested_effort,"
                "effective_effort,native_variant_id,transport,"
                "catalogue_generation,evidence_digest,selector_binding,"
                "adapter_metadata,binding_json,binding_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    participant_id,
                    1,
                    binding["contract_version"],
                    binding["control_state"],
                    binding["harness"],
                    binding["requested_model"],
                    binding["provider_model"],
                    binding["requested_effort"],
                    binding["effective_effort"],
                    binding["native_variant_id"],
                    binding["transport"],
                    binding["catalogue_generation"],
                    binding["evidence_digest"],
                    (
                        route_bindings.canonical_json(binding["selector_binding"])
                        if binding["selector_binding"] is not None
                        else None
                    ),
                    route_bindings.canonical_json(binding["adapter_metadata"]),
                    route_bindings.canonical_json(binding),
                    digest,
                ),
            ).lastrowid
        )
        con.execute(
            "UPDATE sprint_participants SET active_route_binding_id=? "
            "WHERE participant_id=?",
            (binding_id, participant_id),
        )
        con.execute(
            "UPDATE sprints SET lifecycle='armed',armed_at=datetime('now'),"
            "conformance_reviewer_shell_id=2,"
            "conformance_owner_generation=1 "
            "WHERE sprint_id=?",
            (sprint_id,),
        )
        con.commit()
        return participants["planner"], participant_id

    @staticmethod
    def _controlled_historical_binding() -> dict:
        return {
            "contract_version": 2,
            "control_state": "controlled",
            "harness": "codex",
            "requested_model": "gpt-5.4",
            "provider_model": "gpt-5.4",
            "requested_effort": "high",
            "effective_effort": "high",
            "native_variant_id": None,
            "transport": "codex-reasoning-config",
            "catalogue_generation": "a" * 32,
            "evidence_digest": "b" * 64,
            "selector_binding": {"kind": "exact-model", "selector": "gpt-5.4"},
            "adapter_metadata": {},
        }

    @staticmethod
    def _variant_metadata(effort: str, verbosity: str) -> tuple[str, str]:
        selected = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {
                "reasoningEffort": effort,
                "textVerbosity": verbosity,
            },
        }
        effort_metadata = {
            "supported": [effort],
            "default": effort,
            "digests": {
                effort: (
                    SprintBoundRouteDispatchProof.BOUND_DIGEST
                    if effort == "high"
                    else SprintBoundRouteDispatchProof.SUCCESSOR_DIGEST
                )
            },
            "native_variant_ids": {effort: effort},
            "adapter_metadata_by_effort": {effort: selected},
        }
        adapter_metadata = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options_by_effort": {
                effort: selected["variant_options"],
            },
        }
        return json.dumps(effort_metadata), json.dumps(adapter_metadata)

    @classmethod
    def _retained_high_metadata(cls) -> tuple[str, str]:
        low = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {
                "reasoningEffort": "low",
                "textVerbosity": "low",
            },
        }
        high = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {
                "reasoningEffort": "high",
                "textVerbosity": "low",
            },
        }
        return (
            json.dumps({
                "supported": ["low", "high"],
                "default": "low",
                "digests": {
                    "low": cls.SUCCESSOR_DIGEST,
                    "high": cls.BOUND_DIGEST,
                },
                "native_variant_ids": {"low": "low", "high": "high"},
                "adapter_metadata_by_effort": {"low": low, "high": high},
            }),
            json.dumps({
                "compatibility_manifest": "opencode-1.18.9-v1",
                "provider_family": "openai-ai-sdk",
                "variant_options_by_effort": {
                    "low": low["variant_options"],
                    "high": high["variant_options"],
                },
            }),
        )

    def _seed_opencode_catalogue(self) -> dict:
        status, scope = self._runtime("opencode")
        now = datetime.now(timezone.utc).isoformat()
        effort_metadata, adapter_metadata = self._variant_metadata("high", "low")
        self.con.execute(
            "INSERT INTO model_catalog_generations "
            "(generation_id,payload_version,contract_version,started_at,"
            "completed_at,state,runtime,source_summary,harness_versions,"
            "source_fingerprints,error_summary,payload_digest) "
            "VALUES (?,6,2,?,?,'successful',?,'[]','{}','{}',NULL,?)",
            (
                self.GENERATION,
                now,
                now,
                scope["runtime"],
                "6" * 64,
            ),
        )
        self.con.execute(
            "INSERT INTO model_routes "
            "(harness,selector,provider,provider_model,source,availability,"
            "headless_supported,high_effort_supported,default_effort,"
            "supported_efforts,cli_version,last_seen_at,stale,generation_id,"
            "evidence_kind,evidence_digest,source_fingerprint,harness_version,"
            "harness_compatibility,selector_binding,effort_metadata,"
            "adapter_metadata) VALUES "
            "('opencode',?,'openai',?,'opencode-provider-api','available',1,1,"
            "'high','[\"high\"]','opencode 1.18.9',?,0,?,"
            "'opencode-connected-variant',?,?,'1.18.9','verified',?,?,?)",
            (
                self.SELECTOR,
                self.SELECTOR,
                now,
                self.GENERATION,
                self.BOUND_DIGEST,
                self.FINGERPRINT,
                json.dumps({"kind": "exact-model", "selector": self.SELECTOR}),
                effort_metadata,
                adapter_metadata,
            ),
        )
        self.con.commit()
        return {
            "runtime_status": status,
            "runtime_scope": scope,
            "source_fingerprint": self.FINGERPRINT,
        }

    def _publish_successor_catalogue(self, *, retain_high: bool = False) -> None:
        now = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        if retain_high:
            effort_metadata, adapter_metadata = self._retained_high_metadata()
            default_effort = "low"
            supported_efforts = '["low","high"]'
        else:
            effort_metadata, adapter_metadata = self._variant_metadata(
                "xhigh", "high"
            )
            default_effort = "xhigh"
            supported_efforts = '["xhigh"]'
        runtime = self.con.execute(
            "SELECT runtime FROM model_catalog_generations WHERE generation_id=?",
            (self.GENERATION,),
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO model_catalog_generations "
            "(generation_id,payload_version,contract_version,started_at,"
            "completed_at,state,runtime,source_summary,harness_versions,"
            "source_fingerprints,error_summary,payload_digest) "
            "VALUES (?,6,2,?,?,'successful',?,'[]','{}','{}',NULL,?)",
            (self.SUCCESSOR_GENERATION, now, now, runtime, "7" * 64),
        )
        self.con.execute(
            "UPDATE model_routes SET generation_id=?,default_effort=?,"
            "supported_efforts=?,evidence_digest=?,source_fingerprint=?,"
            "last_seen_at=?,effort_metadata=?,adapter_metadata=? "
            "WHERE harness='opencode' AND selector=?",
            (
                self.SUCCESSOR_GENERATION,
                default_effort,
                supported_efforts,
                self.SUCCESSOR_DIGEST,
                self.SUCCESSOR_FINGERPRINT,
                now,
                effort_metadata,
                adapter_metadata,
                self.SELECTOR,
            ),
        )
        self.con.commit()

    def _arm(self, sprint_id: int) -> int:
        sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        ).arm(sprint_id, 3)
        row = self.con.execute(
            "SELECT wake.wake_id FROM sprint_wake_outbox wake "
            "JOIN sprint_wake_messages joined USING (wake_id) "
            "JOIN wake_message message USING (message_id) "
            "WHERE message.sprint_id=? AND wake.state='pending' "
            "ORDER BY wake.wake_id LIMIT 1",
            (sprint_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["wake_id"])

    def _deliver_and_claim(self, wake_id: int):
        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).deliver_once(
            "bound-route-proof",
            lambda conversation_id, prompt, key: (
                sprint_runtime.enqueue_conversation_turn(
                    self.db_path, conversation_id, prompt, key
                )
            ),
        )
        self.assertIsNotNone(outcome)
        self.assertEqual((wake_id, "delivered", 1), (
            outcome.wake_id,
            outcome.state,
            outcome.attempt_number,
        ))
        broker_run = BrokerStore(self.db_path).claim_next("bound-route-broker")
        self.assertIsNotNone(broker_run)
        return broker_run

    def _prepare(self, broker_run):
        prepared: list[dict] = []

        def prepare_launch(**kwargs):
            prepared.append(kwargs)
            return SimpleNamespace(
                cwd=str(broker_run.worktree),
                archive_id=42,
                harness=kwargs["harness"],
                model=kwargs["model"],
                effort=kwargs["effort"],
                env={"SC_BOUND_ROUTE_PROOF": "1"},
            )

        context, archive_id = ConversationLaunchPreparer(
            self.db_path,
            prepare_launch=prepare_launch,
            liveness=lambda: {"supported": True, "processes": []},
        )(broker_run)
        self.assertEqual(archive_id, 42)
        self.assertEqual(len(prepared), 1)
        return context, prepared[0]

    def test_harness_default_survives_arm_queue_broker_and_launch(self) -> None:
        status, _scope = self._runtime("kimi")
        self.con.execute(
            "UPDATE flavor_defaults SET model='current-flavor-default',"
            "effort='high' WHERE harness='kimi'"
        )
        self.con.commit()
        sprint_id = self._seed_sprint(harness="kimi", model=None, effort=None)

        with mock.patch.object(
            model_catalog, "harness_runtime_status", return_value=status
        ):
            wake_id = self._arm(sprint_id)
            broker_run = self._deliver_and_claim(wake_id)
        context, launch = self._prepare(broker_run)
        projection = route_transport.context_projection(context, "kimi")

        self.assertEqual(broker_run.route_contract_version, 2)
        self.assertIsNone(broker_run.model)
        self.assertIsNone(broker_run.effort)
        self.assertEqual(broker_run.route_binding["transport"], "native-default")
        self.assertEqual(broker_run.route_binding["control_state"], "harness-default")
        self.assertEqual(
            tuple(
                self.con.execute(
                    "SELECT defaults.model,defaults.effort FROM flavor_defaults "
                    "defaults JOIN shells shell ON shell.flavor=defaults.flavor "
                    "WHERE shell.shell_id=? AND defaults.harness='kimi'",
                    (broker_run.shell_id,),
                ).fetchone()
            ),
            ("current-flavor-default", "high"),
        )
        self.assertIsNone(launch["model"])
        self.assertIsNone(launch["effort"])
        self.assertEqual(launch["route_binding"], broker_run.route_binding)
        self.assertEqual(launch["binding_digest"], broker_run.binding_digest)
        self.assertIsNone(context.model)
        self.assertIsNone(context.effort)
        self.assertIsNone(projection.model)
        self.assertIsNone(projection.effort)
        self.assertEqual(projection.argument_tail, ())

    def test_opencode_dispatch_uses_bound_variant_after_catalogue_changes(self) -> None:
        observation = self._seed_opencode_catalogue()
        sprint_id = self._seed_sprint(
            harness="opencode", model=self.SELECTOR, effort="high"
        )
        with mock.patch.object(
            model_catalog, "controlled_route_evidence", return_value=observation
        ):
            wake_id = self._arm(sprint_id)
            outcome = sprint_message_delivery.SprintWakeDeliveryService(
                self.con, force_new_quiet_seconds=0
            ).deliver_once(
                "bound-opencode-proof",
                lambda conversation_id, prompt, key: (
                    sprint_runtime.enqueue_conversation_turn(
                        self.db_path, conversation_id, prompt, key
                    )
                ),
            )
        self.assertIsNotNone(outcome)
        self.assertEqual((wake_id, "delivered"), (outcome.wake_id, outcome.state))

        self._publish_successor_catalogue()
        broker_run = BrokerStore(self.db_path).claim_next("bound-opencode-broker")
        self.assertIsNotNone(broker_run)
        context, launch = self._prepare(broker_run)
        projection = route_transport.context_projection(context, "opencode")

        class RecordingTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict | None]] = []

            def request(self, method, path, *, query=None, body=None):
                self.calls.append((method, path, body))
                return {}

            def stream(self, *_args, **_kwargs):
                return iter(())

        transport = RecordingTransport()
        adapter = OpenCodeAdapter(
            transport=transport,
            shell_runtime_dir=self.root / "opencode-shells",
        )
        adapter._prepare_shell_environment(context)
        adapter._prompt("native-session", context, "Dispatch stored route")
        config = json.loads((broker_run.worktree / "opencode.json").read_text())
        current = json.loads(
            self.con.execute(
                "SELECT effort_metadata FROM model_routes "
                "WHERE harness='opencode' AND selector=?",
                (self.SELECTOR,),
            ).fetchone()[0]
        )

        self.assertEqual(
            broker_run.route_binding["catalogue_generation"], self.GENERATION
        )
        self.assertEqual(broker_run.route_binding["native_variant_id"], "high")
        self.assertEqual(
            broker_run.route_binding["adapter_metadata"]["variant_options"],
            {"reasoningEffort": "high", "textVerbosity": "low"},
        )
        self.assertEqual(launch["route_binding"], broker_run.route_binding)
        self.assertEqual(projection.native_variant_id, "high")
        self.assertEqual(
            config["agent"][projection.route_agent],
            {
                "mode": "primary",
                "model": self.SELECTOR,
                "reasoningEffort": "high",
                "textVerbosity": "low",
            },
        )
        self.assertEqual(current["native_variant_ids"], {"xhigh": "xhigh"})
        self.assertEqual(
            transport.calls[-1][2],
            {
                "parts": [{"type": "text", "text": "Dispatch stored route"}],
                "model": {
                    "providerID": "openai",
                    "modelID": "sprint-bound-model",
                },
                "agent": projection.route_agent,
            },
        )

    def test_stale_binding_refuses_before_conversation_or_prompt_dispatch(self) -> None:
        observation = self._seed_opencode_catalogue()
        sprint_id = self._seed_sprint(
            harness="opencode", model=self.SELECTOR, effort="high"
        )
        with mock.patch.object(
            model_catalog, "controlled_route_evidence", return_value=observation
        ):
            wake_id = self._arm(sprint_id)
            stored_provenance = tuple(
                self.con.execute(
                    "SELECT source_fingerprint,harness_version FROM "
                    "sprint_participant_route_bindings binding "
                    "JOIN sprint_participants participant "
                    "ON participant.active_route_binding_id=binding.binding_id "
                    "JOIN sprint_wake_outbox wake "
                    "ON wake.participant_id=participant.participant_id "
                    "WHERE wake.wake_id=?",
                    (wake_id,),
                ).fetchone()
            )
            self._publish_successor_catalogue(retain_high=True)
            observation["source_fingerprint"] = self.SUCCESSOR_FINGERPRINT
            deliveries: list[tuple[str, str, str]] = []
            outcome = sprint_message_delivery.SprintWakeDeliveryService(
                self.con, force_new_quiet_seconds=0
            ).deliver_once(
                "stale-bound-route-proof",
                lambda conversation_id, prompt, key: deliveries.append(
                    (conversation_id, prompt, key)
                ),
            )

        self.assertIsNotNone(outcome)
        self.assertEqual(stored_provenance, (self.FINGERPRINT, "1.18.9"))
        self.assertEqual((wake_id, "pending", 1), (
            outcome.wake_id,
            outcome.state,
            outcome.attempt_number,
        ))
        self.assertEqual(deliveries, [])
        current = json.loads(
            self.con.execute(
                "SELECT effort_metadata FROM model_routes "
                "WHERE harness='opencode' AND selector=?",
                (self.SELECTOR,),
            ).fetchone()[0]
        )
        self.assertEqual(current["supported"], ["low", "high"])
        self.assertEqual(current["default"], "low")
        self.assertEqual(current["digests"]["high"], self.BOUND_DIGEST)
        self.assertEqual(
            tuple(
                tuple(row)
                for row in self.con.execute(
                    "SELECT COUNT(*) FROM conversations UNION ALL "
                    "SELECT COUNT(*) FROM conversation_messages UNION ALL "
                    "SELECT COUNT(*) FROM conversation_outbox UNION ALL "
                    "SELECT COUNT(*) FROM conversation_runs UNION ALL "
                    "SELECT COUNT(*) FROM active_shell_chats"
                ).fetchall()
            ),
            ((0,), (0,), (0,), (0,), (0,)),
        )
        self.assertEqual(
            tuple(
                self.con.execute(
                    "SELECT state,attempt_count,last_error "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (wake_id,),
                ).fetchone()
            ),
            (
                "pending",
                1,
                "route_evidence_stale: Stored Sprint route evidence changed "
                "before its first native turn",
            ),
        )

    def test_pre_native_failure_does_not_bypass_later_stale_check(self) -> None:
        observation = self._seed_opencode_catalogue()
        sprint_id = self._seed_sprint(
            harness="opencode", model=self.SELECTOR, effort="high"
        )
        with mock.patch.object(
            model_catalog, "controlled_route_evidence", return_value=observation
        ):
            first_wake_id = self._arm(sprint_id)
            first_run = self._deliver_and_claim(first_wake_id)
            broker = BrokerStore(self.db_path)
            self.assertTrue(
                broker.finish_run(
                    first_run.run_id,
                    "failed",
                    event_type="run.failed",
                    error_code="HARNESS_LAUNCH_FAILED",
                    error_detail="launch preparation failed before native start",
                )
            )
            with db_driver.write_transaction(
                self.con, "test.close_pre_native_failure"
            ):
                closed = active_chat_registry.close_for_wake(
                    self.con, first_run.shell_id
                )
            self.assertIsNotNone(closed)
            self.assertEqual(closed.chat_id, first_run.conversation_id)

            self._publish_successor_catalogue(retain_high=True)
            observation["source_fingerprint"] = self.SUCCESSOR_FINGERPRINT
            participants = {
                str(row["role"]): int(row["participant_id"])
                for row in self.con.execute(
                    "SELECT participant_id,role FROM sprint_participants "
                    "WHERE sprint_id=?",
                    (sprint_id,),
                )
            }
            receipt = sprint_message_delivery.SprintMessageStore(self.con).send(
                sprint_id,
                to_participant_id=participants["developer"],
                from_participant_id=participants["planner"],
                message_kind="notification",
                body="Retry after a pre-native launch failure",
                idempotency_key="pre-native-failure-followup",
                declared_type="new",
            )
            before = tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT COUNT(*) FROM conversations UNION ALL "
                    "SELECT COUNT(*) FROM conversation_messages UNION ALL "
                    "SELECT COUNT(*) FROM conversation_outbox UNION ALL "
                    "SELECT COUNT(*) FROM conversation_runs"
                )
            )
            deliveries: list[tuple[str, str, str]] = []
            outcome = sprint_message_delivery.SprintWakeDeliveryService(
                self.con, force_new_quiet_seconds=0
            ).deliver_once(
                "pre-native-failure-stale-proof",
                lambda conversation_id, prompt, key: deliveries.append(
                    (conversation_id, prompt, key)
                ),
            )

        self.assertIsNotNone(outcome)
        self.assertEqual(receipt.wake_id, outcome.wake_id)
        self.assertEqual(("pending", 1), (outcome.state, outcome.attempt_number))
        self.assertEqual(deliveries, [])
        self.assertEqual(before, (1, 1, 1, 1))
        self.assertEqual(
            tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT COUNT(*) FROM conversations UNION ALL "
                    "SELECT COUNT(*) FROM conversation_messages UNION ALL "
                    "SELECT COUNT(*) FROM conversation_outbox UNION ALL "
                    "SELECT COUNT(*) FROM conversation_runs"
                )
            ),
            before,
        )
        self.assertEqual(
            tuple(
                self.con.execute(
                    "SELECT state,attempt_count,last_error "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (receipt.wake_id,),
                ).fetchone()
            ),
            (
                "pending",
                1,
                "route_evidence_stale: Stored Sprint route evidence changed "
                "before its first native turn",
            ),
        )

    def test_historical_uncontrolled_binding_reaches_launch_after_upgrade(self) -> None:
        db_path, con = self._historical_upgrade_database()
        status, scope = self._runtime("kimi")
        binding, _digest = route_bindings.resolve_v2(
            None,
            "kimi",
            None,
            None,
            runtime_status=status,
            runtime_scope=scope,
        )
        sprint_id = self._seed_sprint(
            harness="kimi", model=None, effort=None, con=con
        )
        planner_id, developer_id = self._activate_historical_binding(
            con, sprint_id, binding
        )
        con.executescript(
            (ENGINE / "migrations" / "0216_sprint_binding_provenance.sql").read_text()
        )
        receipt = sprint_message_delivery.SprintMessageStore(con).send(
            sprint_id,
            to_participant_id=developer_id,
            from_participant_id=planner_id,
            message_kind="notification",
            body="First wake after provenance upgrade",
            idempotency_key="historical-uncontrolled-first-wake",
            declared_type="force-new",
        )
        with mock.patch.object(
            model_catalog, "harness_runtime_status", return_value=status
        ) as runtime_probe:
            outcome = sprint_message_delivery.SprintWakeDeliveryService(
                con, force_new_quiet_seconds=0
            ).deliver_once(
                "historical-uncontrolled-upgrade",
                lambda conversation_id, prompt, key: (
                    sprint_runtime.enqueue_conversation_turn(
                        db_path, conversation_id, prompt, key
                    )
                ),
            )
        self.assertIsNotNone(outcome)
        self.assertEqual((receipt.wake_id, "delivered", 1), (
            outcome.wake_id,
            outcome.state,
            outcome.attempt_number,
        ))
        runtime_probe.assert_called_once_with("kimi")
        broker_run = BrokerStore(db_path).claim_next("historical-upgrade-broker")
        self.assertIsNotNone(broker_run)
        prepared: list[dict] = []

        def prepare_launch(**kwargs):
            prepared.append(kwargs)
            return SimpleNamespace(
                cwd=str(broker_run.worktree),
                archive_id=43,
                harness=kwargs["harness"],
                model=kwargs["model"],
                effort=kwargs["effort"],
                env={},
            )

        context, archive_id = ConversationLaunchPreparer(
            db_path,
            prepare_launch=prepare_launch,
            liveness=lambda: {"supported": True, "processes": []},
        )(broker_run)
        self.assertEqual((archive_id, len(prepared)), (43, 1))
        self.assertEqual(
            (context.model, context.effort, prepared[0]["route_binding"]),
            (None, None, binding),
        )
        self.assertEqual(
            tuple(
                con.execute(
                    "SELECT source_fingerprint,harness_version FROM "
                    "sprint_participant_route_bindings WHERE participant_id=?",
                    (developer_id,),
                ).fetchone()
            ),
            (None, None),
        )

    def test_historical_controlled_binding_fails_closed_after_upgrade(self) -> None:
        _db_path, con = self._historical_upgrade_database()
        binding = self._controlled_historical_binding()
        route_bindings.validate_v2_binding(binding)
        sprint_id = self._seed_sprint(
            harness="codex", model="gpt-5.4", effort="high", con=con
        )
        planner_id, developer_id = self._activate_historical_binding(
            con, sprint_id, binding
        )
        con.executescript(
            (ENGINE / "migrations" / "0216_sprint_binding_provenance.sql").read_text()
        )
        receipt = sprint_message_delivery.SprintMessageStore(con).send(
            sprint_id,
            to_participant_id=developer_id,
            from_participant_id=planner_id,
            message_kind="notification",
            body="Controlled first wake after provenance upgrade",
            idempotency_key="historical-controlled-first-wake",
            declared_type="force-new",
        )
        deliveries: list[tuple[str, str, str]] = []
        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            con, force_new_quiet_seconds=0
        ).deliver_once(
            "historical-controlled-upgrade",
            lambda conversation_id, prompt, key: deliveries.append(
                (conversation_id, prompt, key)
            ),
        )

        self.assertIsNotNone(outcome)
        self.assertEqual((receipt.wake_id, "pending", 1), (
            outcome.wake_id,
            outcome.state,
            outcome.attempt_number,
        ))
        self.assertEqual(deliveries, [])
        self.assertEqual(
            tuple(
                con.execute(
                    "SELECT source_fingerprint,harness_version FROM "
                    "sprint_participant_route_bindings WHERE participant_id=?",
                    (developer_id,),
                ).fetchone()
            ),
            (None, None),
        )
        self.assertEqual(
            tuple(
                con.execute(
                    "SELECT state,attempt_count,last_error "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (receipt.wake_id,),
                ).fetchone()
            ),
            (
                "pending",
                1,
                "route_evidence_stale: Stored Sprint route has no immutable "
                "harness-version evidence",
            ),
        )
        self.assertEqual(
            tuple(
                int(row[0])
                for row in con.execute(
                    "SELECT COUNT(*) FROM conversations UNION ALL "
                    "SELECT COUNT(*) FROM conversation_messages UNION ALL "
                    "SELECT COUNT(*) FROM conversation_runs"
                )
            ),
            (0, 0, 0),
        )


class SprintLiveProof(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        mem.SC_API_BASE = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        route_patch = mock.patch.object(
            sprint_domain, "_participant_binding_candidate", side_effect=route_candidate
        )
        route_patch.start()
        self.addCleanup(route_patch.stop)
        evidence_patch = mock.patch.object(
            sprint_domain.route_bindings,
            "verify_stored_v2_before_first_turn",
        )
        evidence_patch.start()
        self.addCleanup(evidence_patch.stop)
        quiet_env = mock.patch.dict(
            "os.environ", {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
        )
        quiet_env.start()
        self.addCleanup(quiet_env.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "sprint-live-proof.db"
        seed = sqlite3.connect(self.db_path)
        try:
            apply_schema(seed)
        finally:
            seed.close()
        self.con = db_driver.connect(self.db_path)
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,?,1,?)",
            (
                (1, "Developer one", "DEV1", "dev", "prompt", TOKENS[1]),
                (2, "Reviewer one", "REV1", "reviewer", "prompt", TOKENS[2]),
                (3, "Planner", "PLN1", "planner", "prompt", TOKENS[3]),
                (4, "Developer two", "DEV2", "dev", "prompt", TOKENS[4]),
                (5, "Reviewer two", "REV2", "reviewer", "prompt", TOKENS[5]),
            ),
        )
        self.con.commit()
        server.DB_PATH = self.db_path
        self.github = ScenarioGitHub()
        self.reader_factory = lambda _repository: self.github
        self.input_counter = 0

    def write_input(self, body: str) -> str:
        path = Path(self.temp_dir.name) / f"input-{self.input_counter}.txt"
        self.input_counter += 1
        path.write_text(body)
        return str(path)

    def run_cli(self, shell_id: int, *argv: str) -> dict:
        mem.SC_API_TOKEN = TOKENS[shell_id]
        output = io.StringIO()
        with (
            mock.patch.object(
                server.sprint_domain,
                "adapter_for",
                return_value=mock.Mock(probe=mock.Mock(return_value=None)),
            ),
            mock.patch.object(
                server.sprint_pr_watcher,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            mock.patch.object(
                server.sprint_review_loop,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            mock.patch.object(
                server.sprint_recovery,
                "GitHubPullRequestReader",
                return_value=self.github,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, sprint_cli.main(list(argv)))
        return json.loads(output.getvalue())

    def prepare(
        self,
        lanes: tuple[tuple[int, int, tuple[int, ...]], ...],
    ) -> tuple[int, int, list[int]]:
        """Seed one eligible declaration, then create lanes through the store."""
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Live proof feature','in_progress')"
            ).lastrowid
        )
        body = "Sprints v2 live proof contract"
        document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Live proof spec',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            self.con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
        task_ids = [
            int(
                self.con.execute(
                    "INSERT INTO spec_tasks "
                    "(feature_id,document_id,seq,title) VALUES (?,?,?,?)",
                    (feature_id, document_id, index, f"Live task {index}"),
                ).lastrowid
            )
            for index in range(len(lanes))
        ]
        self.con.commit()
        declaration = self.run_cli(
            3,
            "declare",
            "--feature",
            str(feature_id),
            "--spec-approval",
            str(approval_id),
            "--participants-file",
            self.write_input(
                json.dumps(
                    [
                        {
                            "shell_id": 3,
                            "role": "planner",
                            "harness": "codex",
                            "model": "planner-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 1,
                            "role": "developer",
                            "harness": "codex",
                            "model": "dev-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 2,
                            "role": "reviewer",
                            "harness": "kimi",
                            "model": "review-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 4,
                            "role": "developer",
                            "harness": "codex",
                            "model": "dev-model",
                            "effort": "high",
                        },
                        {
                            "shell_id": 5,
                            "role": "reviewer",
                            "harness": "kimi",
                            "model": "review-model",
                            "effort": "high",
                        },
                    ]
                )
            ),
            "--merge-grant",
        )
        sprint_id = declaration["sprint_id"]
        unit_ids: list[int] = []
        for index, (developer, reviewer, dependency_indexes) in enumerate(lanes):
            argv = [
                "plan-unit",
                "--sprint",
                str(sprint_id),
                "--developer-shell",
                str(developer),
                "--reviewer-shell",
                str(reviewer),
                "--title",
                f"Live lane {index + 1}",
                "--expected-output-file",
                self.write_input(f"Merged live lane {index + 1}"),
                "--task",
                str(task_ids[index]),
                "--wave",
                "0",
            ]
            for dependency_index in dependency_indexes:
                argv.extend(("--depends-on", str(unit_ids[dependency_index])))
            unit_ids.append(self.run_cli(3, *argv)["work_unit_id"])
        return sprint_id, document_id, unit_ids

    def watcher(self) -> sprint_pr_watcher.SprintPRWatcher:
        return sprint_pr_watcher.SprintPRWatcher(
            self.con,
            repo_root=ROOT,
            reader_factory=self.reader_factory,
        )

    def deliver_browser_turns(self) -> None:
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            pulse_seconds=1,
        )
        self.assertTrue(runtime.pulse_once())

    def deliver_terminal_turn(self, wake_id: int) -> tuple[str, str]:
        """Deliver one wake into a native turn that has already terminated."""
        captured: list[tuple[str, str]] = []

        def deliver(conversation_id: str, prompt: str, key: str) -> str:
            captured.append((conversation_id, prompt))
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','live-proof','prompt',?,?,?,'completed',"
                    "'2026-08-01 00:00:01')",
                    (conversation_id, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                    "lease_expires_at,started_at,ended_at,exit_code) "
                    "SELECT ?,shell_id,?,'succeeded','live-proof',"
                    "'2026-08-01 00:00:01','2026-08-01 00:00:00',"
                    "'2026-08-01 00:00:01',0 FROM conversations "
                    "WHERE conversation_id=?",
                    (conversation_id, message_id, conversation_id),
                ).lastrowid
            )
            for state in ("queued", "running", "waiting"):
                self.con.execute(
                    "UPDATE conversations SET state=? WHERE conversation_id=?",
                    (state, conversation_id),
                )
            self.con.commit()
            return f"conversation-run:{run_id}"

        outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(f"live-proof-terminal:{wake_id}", deliver)
        self.assertIsNotNone(outcome)
        self.assertEqual(wake_id, outcome.wake_id)
        self.assertEqual("delivered", outcome.state)
        return captured[0]

    def assignment_message(self, unit_id: int) -> int:
        row = self.con.execute(
            "SELECT message_id FROM wake_message "
            "WHERE work_unit_id=? AND message_kind='work_assignment' "
            "ORDER BY message_id DESC LIMIT 1",
            (unit_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    def accept_assignment(self, unit_id: int, developer: int) -> None:
        message_id = self.assignment_message(unit_id)
        self.assertEqual(
            "accepted",
            sprint_message_delivery.SprintMessageStore(self.con).mark_read(
                message_id, developer
            ),
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )

    def review_and_merge(
        self,
        watcher: sprint_pr_watcher.SprintPRWatcher,
        *,
        sprint_id: int,
        unit_id: int,
        developer: int,
        reviewer: int,
        pr_number: int,
        request_changes: bool = False,
    ) -> int:
        self.github.set(pr_number, "OPEN")
        registration = self.run_cli(
            developer,
            "register-pr",
            "--sprint",
            str(sprint_id),
            "--repository",
            "acme/live-proof",
            "--pr",
            str(pr_number),
            "--work-unit",
            str(unit_id),
        )
        messages = sprint_message_delivery.SprintMessageStore(self.con)

        handoff = self.run_cli(
            developer,
            "request-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--readiness-file",
            self.write_input(f"PR {pr_number} is green and ready."),
            "--key",
            f"proof:{pr_number}:review:1",
        )
        self.deliver_browser_turns()
        self.assertEqual(
            "accepted", messages.mark_read(handoff["message_id"], reviewer)
        )
        if request_changes:
            outcome = self.run_cli(
                reviewer,
                "record-review",
                "--sprint",
                str(sprint_id),
                "--registered-pr",
                str(registration["registered_pr_id"]),
                "--verdict",
                "changes_requested",
                "--body-file",
                self.write_input("Exercise the correction loop before approval."),
                "--key",
                f"proof:{pr_number}:changes",
            )
            self.assertEqual("fixing", outcome["disposition"])
            self.github.set(pr_number, "OPEN", checks="FAILURE")
            self.assertTrue(watcher.poll_once())
            self.github.set(pr_number, "OPEN")
            self.assertTrue(watcher.poll_once())
            handoff = self.run_cli(
                developer,
                "request-review",
                "--sprint",
                str(sprint_id),
                "--registered-pr",
                str(registration["registered_pr_id"]),
                "--readiness-file",
                self.write_input(f"PR {pr_number} correction is green."),
                "--key",
                f"proof:{pr_number}:review:2",
            )
            self.deliver_browser_turns()
            self.assertEqual(
                "accepted", messages.mark_read(handoff["message_id"], reviewer)
            )

        approval = self.run_cli(
            reviewer,
            "record-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--verdict",
            "approved",
            "--body-file",
            self.write_input("No Medium-or-higher findings remain."),
            "--key",
            f"proof:{pr_number}:approved",
        )
        self.assertEqual("merge_ready", approval["disposition"])
        authorization = self.run_cli(
            developer,
            "authorize-merge",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
        )
        self.assertEqual(f"{pr_number:040x}", authorization["head_sha"])
        self.github.set(pr_number, "MERGED")
        self.assertTrue(watcher.poll_once())
        self.assertEqual(
            "completed",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )
        return registration["registered_pr_id"]

    def close(self, sprint_id: int, document_id: int) -> dict:
        receipt = self.run_cli(
            2,
            "record-conformance",
            "--sprint",
            str(sprint_id),
            "--body-file",
            self.write_input("Integrated live proof matches its bound contract."),
            "--findings-file",
            self.write_input("[]"),
            "--final-report-file",
            self.write_input(
                "Reviewer final report: the live proof matches its bound contract."
            ),
            "--reason",
            "delivery and conformance complete",
            "--outcome",
            "accepted",
            "--key",
            f"proof:{sprint_id}:conformance",
        )
        self.assertEqual([], receipt["followup_ids"])
        self.assertTrue(receipt["completed"])
        packet = self.run_cli(
            3,
            "compile-report",
            "--sprint",
            str(sprint_id),
            "--limit",
            "50",
        )
        self.assertEqual("completed", packet["scope"]["lifecycle"])
        self.assertEqual(
            document_id, packet["spec_revisions"]["bound"][0]["document_id"]
        )
        self.assertEqual([], packet["unresolved_work"]["work_units"]["items"])
        self.assertEqual([], packet["unresolved_work"]["followups"]["items"])
        return packet

    def test_serial_sprint_runs_correction_merge_dispatch_and_close(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (1, 2, (0,))))
        initial_wakes = self.run_cli(
            3,
            "arm",
            "--sprint",
            str(sprint_id),
            "--conformance-reviewer-shell",
            "2",
        )["wake_ids"]
        self.assertEqual(2, len(initial_wakes))
        self.assertEqual(
            [(units[0], "ready"), (units[1], "planned")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
        )
        self.deliver_browser_turns()
        self.accept_assignment(units[0], 1)
        watcher = self.watcher()
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[0],
            developer=1,
            reviewer=2,
            pr_number=1001,
            request_changes=True,
        )
        self.assertEqual(
            "ready",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (units[1],),
            ).fetchone()[0],
        )
        self.deliver_browser_turns()
        self.accept_assignment(units[1], 1)
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[1],
            developer=1,
            reviewer=2,
            pr_number=1002,
        )
        packet = self.close(sprint_id, document_id)

        event_types = [
            row[0]
            for row in self.con.execute(
                "SELECT event_type FROM sprint_events WHERE sprint_id=? ORDER BY event_id",
                (sprint_id,),
            )
        ]
        self.assertEqual(2, event_types.count("work_unit.completed"))
        self.assertEqual(2, event_types.count("merge.authorized"))
        self.assertEqual(1, event_types.count("review.changes_requested"))
        self.assertEqual(2, event_types.count("review.approved"))
        self.assertEqual("lifecycle.completed", event_types[-1])
        self.assertEqual(2, packet["pr_outcomes"]["total"])
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations link "
                "JOIN sprint_participants p "
                "ON p.participant_id=link.sprint_participant_id "
                "WHERE p.sprint_id=? AND p.shell_id=1",
                (sprint_id,),
            ).fetchone()[0],
        )

    def test_parallel_sprint_completes_out_of_order_without_lane_overlap(self) -> None:
        sprint_id, document_id, units = self.prepare(((1, 2, ()), (4, 5, ())))
        initial_wakes = self.run_cli(
            3,
            "arm",
            "--sprint",
            str(sprint_id),
            "--conformance-reviewer-shell",
            "2",
        )["wake_ids"]
        self.assertEqual(3, len(initial_wakes))
        self.deliver_browser_turns()
        self.accept_assignment(units[0], 1)
        self.accept_assignment(units[1], 4)
        watcher = self.watcher()

        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[1],
            developer=4,
            reviewer=5,
            pr_number=2002,
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (units[0],),
            ).fetchone()[0],
        )
        self.review_and_merge(
            watcher,
            sprint_id=sprint_id,
            unit_id=units[0],
            developer=1,
            reviewer=2,
            pr_number=2001,
        )
        packet = self.close(sprint_id, document_id)

        completions = [
            json.loads(row[0])["work_unit_id"]
            for row in self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='work_unit.completed' ORDER BY event_id",
                (sprint_id,),
            )
        ]
        self.assertEqual([units[1], units[0]], completions)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations "
                "WHERE sprint_id=? AND resolved_at IS NULL",
                (sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(2, packet["planned_vs_actual"]["total"])
        self.assertEqual(2, packet["pr_outcomes"]["total"])

    def test_conversation_and_watcher_writes_share_wal_without_lock_loss(self) -> None:
        sprint_id, _document_id, units = self.prepare(((1, 2, ()),))
        sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        ).arm(sprint_id, 3, conformance_reviewer_shell_id=2)
        self.deliver_browser_turns()
        self.accept_assignment(units[0], 1)
        self.github.set(3001, "OPEN", checks="PENDING")
        watcher = self.watcher()
        watcher.register(
            sprint_id,
            owner_shell_id=1,
            repository="acme/live-proof",
            pr_number=3001,
            work_unit_ids=(units[0],),
        )
        self.github.set(3001, "OPEN")
        conversation_id = str(
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=1",
                (sprint_id,),
            ).fetchone()[0]
        )
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def conversation_write() -> None:
            try:
                barrier.wait()
                sprint_runtime.enqueue_conversation_turn(
                    self.db_path,
                    conversation_id,
                    "concurrent Sprint turn",
                    "live-proof:concurrent-conversation",
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)

        def watcher_write() -> None:
            con = db_driver.connect(self.db_path)
            try:
                barrier.wait()
                sprint_pr_watcher.SprintPRWatcher(
                    con,
                    repo_root=ROOT,
                    reader_factory=self.reader_factory,
                ).poll_once()
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)
            finally:
                con.close()

        threads = [
            threading.Thread(target=conversation_write),
            threading.Thread(target=watcher_write),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE idempotency_key='live-proof:concurrent-conversation'"
            ).fetchone()[0],
        )
        self.assertEqual(
            [("pending",), ("green",)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state FROM sprint_pr_transitions "
                    "ORDER BY transition_id"
                )
            ],
        )

    def test_relay_review_and_terminal_pickup_recovery_form_one_durable_chain(
        self,
    ) -> None:
        sprint_id, _document_id, units = self.prepare(((1, 2, ()),))
        self.run_cli(
            3,
            "arm",
            "--sprint",
            str(sprint_id),
            "--conformance-reviewer-shell",
            "2",
        )
        self.deliver_browser_turns()
        assignment_id = self.assignment_message(units[0])
        developer_start = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            assignment_id,
            [message["message_id"] for message in developer_start["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(assignment_id),
        )

        arming_planner_conversation = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.sprint_id=? AND participant.shell_id=3",
            (sprint_id,),
        ).fetchone()[0]
        self.assertIsNotNone(
            arming_planner_conversation,
            "arming opens the overseeing Planner's fresh wake chat",
        )
        question_prefix = "Which downstream compatibility fixture owns the proof? "
        question = question_prefix + "q" * (6000 - len(question_prefix))
        question_receipt = self.run_cli(
            1,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "PLN1",
            "--body-file",
            self.write_input(question),
            "--key",
            "proof:68:question",
        )
        self.assertTrue(question_receipt["message_created"])
        self.assertEqual("pending", question_receipt["wake_state"])
        self.assertEqual(
            arming_planner_conversation,
            question_receipt["conversation_id"],
            "later Planner-bound traffic re-enters the chat opened by arming",
        )
        self.assertEqual(
            (1, 3, 6000, question),
            tuple(
                self.con.execute(
                    "SELECT sender.shell_id,recipient.shell_id,length(m.body),m.body "
                    "FROM wake_message m "
                    "JOIN sprint_participants sender "
                    "ON sender.participant_id=m.from_participant_id "
                    "JOIN sprint_participants recipient "
                    "ON recipient.participant_id=m.to_participant_id "
                    "WHERE m.message_id=?",
                    (question_receipt["message_id"],),
                ).fetchone()
            ),
        )
        self.deliver_browser_turns()
        planner_conversation = str(
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=3",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertNotEqual("None", planner_conversation)
        planner_inbox = self.run_cli(3, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            (question_receipt["message_id"], question),
            [
                (message["message_id"], message["body"])
                for message in planner_inbox["messages"]
            ],
        )
        delivered_prompt = self.con.execute(
                "SELECT cm.body FROM sprint_wake_outbox w "
                "JOIN conversation_messages cm "
                "ON cm.idempotency_key=w.idempotency_key WHERE w.wake_id=?",
                (question_receipt["wake_id"],),
            ).fetchone()[0]
        self.assertIn(
            sprint_message_delivery.wake_prompt(sprint_id, "planner"),
            delivered_prompt,
        )
        self.assertIn(question, delivered_prompt)
        self.run_cli(
            3,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(question_receipt["message_id"]),
        )

        answer_receipt = self.run_cli(
            3,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "DEV1",
            "--body-file",
            self.write_input(
                "Use the legacy update fixture with its pre-existing linked worktree."
            ),
            "--key",
            "proof:68:answer",
        )
        self.deliver_browser_turns()
        developer_answer = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            answer_receipt["message_id"],
            [message["message_id"] for message in developer_answer["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(answer_receipt["message_id"]),
        )

        self.github.set(6801, "OPEN")
        registration = self.run_cli(
            1,
            "register-pr",
            "--sprint",
            str(sprint_id),
            "--repository",
            "acme/live-proof",
            "--pr",
            "6801",
            "--work-unit",
            str(units[0]),
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT (SELECT chat_id FROM active_shell_chats "
                "WHERE shell_id=participant.shell_id) "
                "FROM sprint_participants participant "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        review_request = self.run_cli(
            1,
            "request-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--readiness-file",
            self.write_input("The downstream handoff proof is green and ready."),
            "--key",
            "proof:68:dev-to-review",
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT (SELECT chat_id FROM active_shell_chats "
                "WHERE shell_id=participant.shell_id) "
                "FROM sprint_participants participant "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.deliver_browser_turns()
        reviewer_new = str(
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=2",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertNotEqual("None", reviewer_new)
        reviewer_inbox = self.run_cli(2, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            review_request["message_id"],
            [message["message_id"] for message in reviewer_inbox["messages"]],
        )
        self.run_cli(
            2,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(review_request["message_id"]),
        )

        outcome = self.run_cli(
            2,
            "record-review",
            "--sprint",
            str(sprint_id),
            "--registered-pr",
            str(registration["registered_pr_id"]),
            "--verdict",
            "changes_requested",
            "--body-file",
            self.write_input("Retain the terminal-turn recovery evidence."),
            "--key",
            "proof:68:review-to-dev",
        )
        self.deliver_browser_turns()
        developer_outcome = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            outcome["message_id"],
            [message["message_id"] for message in developer_outcome["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(outcome["message_id"]),
        )

        recovery_message = self.run_cli(
            3,
            "send",
            "--sprint",
            str(sprint_id),
            "--to",
            "DEV1",
            "--body-file",
            self.write_input("Preserve this separate delivered-unread recovery proof."),
            "--key",
            "proof:68:delivered-unread",
        )
        terminal_conversation, terminal_prompt = self.deliver_terminal_turn(
            recovery_message["wake_id"]
        )
        self.assertEqual(recovery_message["conversation_id"], terminal_conversation)
        self.assertIn(
            sprint_message_delivery.wake_prompt(sprint_id, "developer"),
            terminal_prompt,
        )
        self.assertIn(
            "Preserve this separate delivered-unread recovery proof.",
            terminal_prompt,
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT read_at FROM wake_message WHERE message_id=?",
                (recovery_message["message_id"],),
            ).fetchone()[0]
        )
        prior_pickup_turns = [
            int(row[0])
            for row in self.con.execute(
                "SELECT m.message_id FROM conversation_messages m "
                "JOIN sprint_participant_conversations pc "
                "ON pc.conversation_id=m.conversation_id "
                "JOIN sprint_participants p "
                "ON p.participant_id=pc.sprint_participant_id "
                "WHERE p.sprint_id=? AND p.shell_id=1 AND m.state='queued'",
                (sprint_id,),
            )
        ]
        for message_id in prior_pickup_turns:
            self.con.execute(
                "UPDATE conversation_messages SET state='running' WHERE message_id=?",
                (message_id,),
            )
            self.con.execute(
                "UPDATE conversation_messages SET state='completed',"
                "completed_at=datetime('now') WHERE message_id=?",
                (message_id,),
            )
        self.con.commit()

        self.run_cli(
            1,
            "pause",
            "--sprint",
            str(sprint_id),
            "--reason",
            "Prove delivered-unread pickup recovery",
        )
        resumed = self.run_cli(
            3,
            "resume",
            "--sprint",
            str(sprint_id),
            "--reason",
            "Restore the Developer correction handoff",
        )
        self.assertEqual(1, len(resumed["requeued_wake_ids"]))
        replacement = resumed["requeued_wake_ids"][0]
        self.assertNotEqual(recovery_message["wake_id"], replacement)
        self.deliver_browser_turns()
        recovered_inbox = self.run_cli(1, "inbox", "--sprint", str(sprint_id))
        self.assertIn(
            recovery_message["message_id"],
            [message["message_id"] for message in recovered_inbox["messages"]],
        )
        self.run_cli(
            1,
            "accept",
            "--sprint",
            str(sprint_id),
            "--message",
            str(recovery_message["message_id"]),
        )
        evidence = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.requeued' ORDER BY event_id DESC LIMIT 1",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("delivered", evidence["prior_wake_state"])
        self.assertEqual("completed", evidence["prior_turn_state"]["message_state"])
        self.assertEqual("succeeded", evidence["prior_turn_state"]["run_state"])
        self.assertEqual(recovery_message["wake_id"], evidence["prior_wake_id"])
        self.assertEqual(replacement, evidence["replacement_wake_id"])
        self.assertEqual(
            ("fixing", "armed", "delivered"),
            tuple(
                self.con.execute(
                    "SELECT u.disposition,s.lifecycle,w.state "
                    "FROM sprint_work_units u JOIN sprints s USING (sprint_id) "
                    "JOIN sprint_wake_outbox w ON w.wake_id=? "
                    "WHERE u.work_unit_id=?",
                    (replacement, units[0]),
                ).fetchone()
            ),
        )

    def test_handoff_hardening_manifest_references_compositional_proof(self) -> None:
        manifest = json.loads(HANDOFF_ACCEPTANCE.read_text())
        self.assertEqual(68, manifest["spec_document_id"])
        self.assertEqual(6, len(manifest["gates"]))
        identities = {(entry["file"], entry["test"]) for entry in manifest["gates"]}
        self.assertEqual(len(manifest["gates"]), len(identities))
        for entry in manifest["gates"]:
            with self.subTest(gate=entry["gate"]):
                source = ROOT.joinpath(entry["file"]).read_text()
                self.assertIn(f"def {entry['test']}", source)

    def test_adversarial_acceptance_manifest_references_real_gates(self) -> None:
        manifest = json.loads(ACCEPTANCE.read_text())
        self.assertEqual(46, manifest["spec_document_id"])
        self.assertEqual(18, len(manifest["scenarios"]))
        self.assertEqual(6, len(manifest["invariant_sweep"]))
        entries = manifest["scenarios"] + manifest["invariant_sweep"]
        identities = {(entry["file"], entry["test"]) for entry in entries}
        self.assertEqual(len(entries), len(identities))
        for entry in entries:
            with self.subTest(entry=entry.get("scenario") or entry["invariant"]):
                source = ROOT.joinpath(entry["file"]).read_text()
                self.assertIn(f"def {entry['test']}", source)


if __name__ == "__main__":
    unittest.main()
