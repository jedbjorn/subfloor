"""Exact-ref, revocable DeepSeek proof capability regressions."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import deepseek_web
from deepseek_candidate_authority import DeepSeekCandidateAuthority
from deepseek_identity_registry import (
    HEALTH_CONTRACT,
    DeepSeekIdentityError,
    DeepSeekIdentityRegistry,
    plugin_contract_generation,
    process_start_ticks,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


ROOTS = {
    "candidate-root-a": {
        "conversation_id": "conversation-a",
        "lifecycle_epoch": 3,
        "verified_lineage": ["child-a"],
    },
    "candidate-root-b": {
        "conversation_id": "conversation-b",
        "lifecycle_epoch": 7,
        "verified_lineage": [],
    },
}

SURFACE_ROOT = "sc-" + "a" * 32
SURFACE_CONVERSATION = "candidate-surface-conversation"


def owner_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def runtime_registry(parent: Path) -> tuple[DeepSeekIdentityRegistry, Path, str]:
    repo = parent / "repo"
    worktree = repo / ".sc-worktrees" / "dev"
    worktree.mkdir(parents=True)
    registry = DeepSeekIdentityRegistry(
        repo_root=repo,
        runtime_root=parent / "identity",
    )
    registry.materialize_profile()
    inputs = {
        "canonical_fork_id": registry.layout.fork_id,
        "dedicated_profile_id": registry.layout.profile_id,
        "plugin_bundle_digest": registry.plugin_digest,
        "declared_variable_schema_digest": registry.schema_digest,
        "canonical_registry_path_identity": registry.registry_path_identity,
        "host_boot_generation": "candidate-host",
        "plugin_load_hmr_generation": "candidate-plugin",
    }
    generation = plugin_contract_generation(inputs)
    registry.observe_host(
        host_boot_generation="candidate-host",
        host_pid=os.getpid(),
    )
    owner_json(
        registry.layout.health,
        {
            "contract": HEALTH_CONTRACT,
            "loaded": True,
            "fork_id": registry.layout.fork_id,
            "profile_id": registry.layout.profile_id,
            "registry_path": str(registry.layout.registry.resolve()),
            "host_boot_generation": "candidate-host",
            "host_pid": os.getpid(),
            "host_start_ticks": process_start_ticks(os.getpid()),
            "plugin_load_hmr_generation": "candidate-plugin",
            "plugin_contract_generation": generation,
            "registry_snapshot_generation": None,
            "binding_record_generation": None,
        },
    )
    return registry, worktree, generation


def durable_identity_state(
    registry: DeepSeekIdentityRegistry,
    authority: DeepSeekCandidateAuthority,
) -> tuple[bytes, dict[str, bytes], bytes, dict[str, bytes]]:
    return (
        registry.layout.registry.read_bytes(),
        {
            path.name: path.read_bytes()
            for path in sorted(registry.layout.credentials.glob("*.json"))
        },
        authority.state_path.read_bytes(),
        {
            path.name: path.read_bytes()
            for path in sorted(authority.artifacts.glob("*.json"))
        },
    )


def mint(authority: DeepSeekCandidateAuthority):
    return authority.mint(
        mode="candidate",
        exact_ref="a" * 40,
        pinned_dsh_version="0.1.1-rc.2",
        disposable_baseline="arch-clean-2026-08-25",
        proof_run_id="proof-run-25",
        roots=ROOTS,
        plugin_contract_generation="contract-one",
        ttl_seconds=600,
        live_registry_roots=[],
    )


def admit(authority: DeepSeekCandidateAuthority, artifact: Path, **overrides):
    values = {
        "artifact": artifact,
        "mode": "candidate",
        "exact_ref": "a" * 40,
        "pinned_dsh_version": "0.1.1-rc.2",
        "root_session_id": "candidate-root-a",
        "conversation_id": "conversation-a",
        "lifecycle_epoch": 3,
        "verified_lineage": ["child-a"],
        "plugin_contract_generation": "contract-one",
    }
    values.update(overrides)
    return authority.admit(**values)


def test_mint_requires_clean_seat_and_admits_only_exact_enumerated_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        authority = DeepSeekCandidateAuthority(root, clock=Clock())
        with pytest.raises(DeepSeekIdentityError) as dirty:
            authority.mint(
                mode="candidate",
                exact_ref="a" * 40,
                pinned_dsh_version="0.1.1-rc.2",
                disposable_baseline="arch-clean-2026-08-25",
                proof_run_id="proof-run-25",
                roots=ROOTS,
                plugin_contract_generation="contract-one",
                ttl_seconds=600,
                live_registry_roots=["ordinary-live-session"],
            )
        assert dirty.value.code == "HARNESS_PROOF_SEAT_NOT_CLEAN"
        assert sorted(root.glob("**/*")) == []

        grant = mint(authority)
        admitted = admit(authority, grant.artifact)
        assert admitted == {
            "mode": "candidate",
            "generation": 1,
            "proof_run_id": "proof-run-25",
            "root_session_id": "candidate-root-a",
            "plugin_contract_generation": "contract-one",
        }
        before = (root / "authority.json").read_bytes()
        with pytest.raises(DeepSeekIdentityError) as unlisted:
            admit(
                authority,
                grant.artifact,
                root_session_id="ordinary-session",
                conversation_id="ordinary-conversation",
                lifecycle_epoch=1,
                verified_lineage=[],
            )
        assert unlisted.value.code == "HARNESS_PROOF_ROOT_REFUSED"
        assert (root / "authority.json").read_bytes() == before
        state = json.loads(before)
        assert set(state["roots"]) == {"candidate-root-a", "candidate-root-b"}
        assert "ordinary-session" not in state["roots"]
        token = json.loads(grant.artifact.read_text())["token"]
        assert token not in before.decode()


def test_restart_ratchet_preserves_roots_and_rejects_stale_generation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        authority = DeepSeekCandidateAuthority(Path(raw), clock=Clock())
        first = mint(authority)
        second = authority.ratchet_after_host_restart(
            artifact=first.artifact,
            old_plugin_contract_generation="contract-one",
            new_plugin_contract_generation="contract-two",
            roots=ROOTS,
            ttl_seconds=600,
        )
        assert second.generation == 2
        assert second.proof_run_id == first.proof_run_id == "proof-run-25"
        with pytest.raises(DeepSeekIdentityError) as stale:
            admit(authority, first.artifact)
        assert stale.value.code == "HARNESS_PROOF_CAPABILITY_STALE"
        resumed = admit(
            authority,
            second.artifact,
            plugin_contract_generation="contract-two",
        )
        assert resumed["generation"] == 2
        assert resumed["root_session_id"] == "candidate-root-a"

        changed = {**ROOTS, "new-root": {
            "conversation_id": "new-conversation",
            "lifecycle_epoch": 1,
            "verified_lineage": [],
        }}
        before = (Path(raw) / "authority.json").read_bytes()
        with pytest.raises(DeepSeekIdentityError) as expanded:
            authority.ratchet_after_host_restart(
                artifact=second.artifact,
                old_plugin_contract_generation="contract-two",
                new_plugin_contract_generation="contract-three",
                roots=changed,
                ttl_seconds=600,
            )
        assert expanded.value.code == "HARNESS_PROOF_CAPABILITY_STALE"
        assert (Path(raw) / "authority.json").read_bytes() == before
        assert "new-root" not in before.decode()


def test_expiry_and_revocation_refuse_without_reactivating_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        clock = Clock()
        authority = DeepSeekCandidateAuthority(Path(raw), clock=clock)
        grant = mint(authority)
        clock.value += timedelta(seconds=601)
        with pytest.raises(DeepSeekIdentityError) as expired:
            admit(authority, grant.artifact)
        assert expired.value.code == "HARNESS_PROOF_CAPABILITY_EXPIRED"
        before = (Path(raw) / "authority.json").read_bytes()
        with pytest.raises(DeepSeekIdentityError) as expired_ratchet:
            authority.ratchet_after_host_restart(
                artifact=grant.artifact,
                old_plugin_contract_generation="contract-one",
                new_plugin_contract_generation="contract-two",
                roots=ROOTS,
                ttl_seconds=600,
            )
        assert expired_ratchet.value.code == "HARNESS_PROOF_CAPABILITY_EXPIRED"
        assert (Path(raw) / "authority.json").read_bytes() == before
        assert len(list((Path(raw) / "capabilities").glob("*.json"))) == 1
        state = json.loads((Path(raw) / "authority.json").read_text())
        assert state["state"] == "active"
        assert state["generation"] == 1

    with tempfile.TemporaryDirectory() as raw:
        authority = DeepSeekCandidateAuthority(Path(raw))
        grant = mint(authority)
        assert authority.revoke(artifact=grant.artifact) == {
            "state": "revoked",
            "generation": 1,
            "proof_run_id": "proof-run-25",
        }
        with pytest.raises(DeepSeekIdentityError) as revoked:
            admit(authority, grant.artifact)
        assert revoked.value.code == "HARNESS_PROOF_CAPABILITY_REVOKED"
        state = json.loads((Path(raw) / "authority.json").read_text())
        assert state["state"] == "revoked"
        assert state["token_sha256"] is None


def test_runtime_admission_uses_exact_ref_live_contract_and_current_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def __init__(self, root: Path) -> None:
            self.layout = type("Layout", (), {"root": root})()

        def read_live_health(self):
            return {"plugin_contract_generation": "contract-one"}

        def resolve_record(self, session_id):
            assert session_id == "candidate-root-a"
            return {"record_generation": 4}

        def read_snapshot(self):
            return {"lineage": {
                "child-a": {
                    "root_session_id": "candidate-root-a",
                    "lifecycle_epoch": 3,
                    "record_generation": 4,
                },
                "stale-child": {
                    "root_session_id": "candidate-root-a",
                    "lifecycle_epoch": 3,
                    "record_generation": 3,
                },
                "foreign-child": {
                    "root_session_id": "candidate-root-b",
                    "lifecycle_epoch": 7,
                    "record_generation": 1,
                },
            }}

    with tempfile.TemporaryDirectory() as raw:
        authority = DeepSeekCandidateAuthority(Path(raw) / "proof-authority")
        grant = mint(authority)
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: Registry(Path(raw))
        )
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: "a" * 40)
        monkeypatch.setattr(
            deepseek_web.harness_versions, "probe", lambda _harness: "0.1.1-rc.2"
        )
        admitted = deepseek_web.admit_candidate_execution(
            env={"SC_DSH_PROOF_CAPABILITY_FILE": str(grant.artifact)},
            root_session_id="candidate-root-a",
            conversation_id="conversation-a",
            lifecycle_epoch=3,
        )
        assert admitted == {
            "mode": "candidate",
            "generation": 1,
            "proof_run_id": "proof-run-25",
            "root_session_id": "candidate-root-a",
            "plugin_contract_generation": "contract-one",
        }
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: "b" * 40)
        with pytest.raises(deepseek_web.DeepSeekWebError) as wrong_ref:
            deepseek_web.admit_candidate_execution(
                env={"SC_DSH_PROOF_CAPABILITY_FILE": str(grant.artifact)},
                root_session_id="candidate-root-a",
                conversation_id="conversation-a",
                lifecycle_epoch=3,
            )
        assert wrong_ref.value.code == "HARNESS_PROOF_CAPABILITY_MISMATCH"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("stale", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("expired", "HARNESS_PROOF_CAPABILITY_EXPIRED"),
        ("wrong-ref", "HARNESS_PROOF_CAPABILITY_MISMATCH"),
        ("wrong-generation", "HARNESS_PROOF_CAPABILITY_STALE"),
        ("wrong-root", "HARNESS_PROOF_ROOT_REFUSED"),
        ("partially-recovered", "HARNESS_PROOF_BINDING_MISMATCH"),
    ],
)
def test_candidate_preflight_failures_leave_all_binding_state_unchanged(
    failure: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree, contract_generation = runtime_registry(Path(raw))
        clock = Clock()
        authority = DeepSeekCandidateAuthority(
            registry.layout.root / "proof-authority",
            clock=clock,
        )
        grant = authority.mint(
            mode="candidate",
            exact_ref="a" * 40,
            pinned_dsh_version="0.1.1-rc.2",
            disposable_baseline="arch-clean-2026-08-25",
            proof_run_id="proof-run-surface",
            roots={
                SURFACE_ROOT: {
                    "conversation_id": SURFACE_CONVERSATION,
                    "lifecycle_epoch": 1,
                    "verified_lineage": [],
                }
            },
            plugin_contract_generation=contract_generation,
            ttl_seconds=600,
            live_registry_roots=[],
        )
        artifact = grant.artifact
        root_session_id = SURFACE_ROOT
        exact_ref = "a" * 40
        if failure == "stale":
            authority.revoke(artifact=artifact)
        elif failure == "expired":
            clock.value += timedelta(seconds=601)
        elif failure == "wrong-ref":
            exact_ref = "b" * 40
        elif failure == "wrong-generation":
            presented = json.loads(artifact.read_text())
            presented["generation"] = 99
            owner_json(artifact, presented)
        elif failure == "wrong-root":
            root_session_id = "sc-" + "f" * 32
        elif failure == "partially-recovered":
            registry.create_binding(
                expected_snapshot_generation=0,
                root_session_id=SURFACE_ROOT,
                conversation_id=SURFACE_CONVERSATION,
                lifecycle_epoch=1,
                shell_id=4,
                shell_shortname="DEV4",
                shell_worktree=worktree,
                api_base="http://127.0.0.1:8837",
                token="stale-recovery-token",
                plugin_contract_generation=contract_generation,
            )

        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        monkeypatch.setattr(
            deepseek_web, "_candidate_authority", lambda _registry: authority
        )
        monkeypatch.setattr(
            deepseek_web, "_verify_shell_identity", lambda _env: None
        )
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: exact_ref)
        monkeypatch.setattr(
            deepseek_web,
            "_current_dsh_version",
            lambda: "0.1.1-rc.2",
        )
        env = {
            "SC_API_TOKEN": "candidate-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "4",
            "SC_SHELL_SHORTNAME": "DEV4",
            "SC_DSH_PROOF_CAPABILITY_FILE": str(artifact),
        }
        before = durable_identity_state(registry, authority)
        with pytest.raises(deepseek_web.DeepSeekWebError) as refused:
            deepseek_web.preflight_candidate_execution(
                env=env,
                root_session_id=root_session_id,
                conversation_id=SURFACE_CONVERSATION,
                lifecycle_epoch=1,
                worktree=worktree,
            )
        assert refused.value.code == expected_code
        assert durable_identity_state(registry, authority) == before


def test_candidate_preflight_receipt_allows_exact_create_and_refuses_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree, contract_generation = runtime_registry(Path(raw))
        authority = DeepSeekCandidateAuthority(
            registry.layout.root / "proof-authority"
        )
        grant = authority.mint(
            mode="candidate",
            exact_ref="a" * 40,
            pinned_dsh_version="0.1.1-rc.2",
            disposable_baseline="arch-clean-2026-08-25",
            proof_run_id="proof-run-receipt",
            roots={
                SURFACE_ROOT: {
                    "conversation_id": SURFACE_CONVERSATION,
                    "lifecycle_epoch": 1,
                    "verified_lineage": [],
                }
            },
            plugin_contract_generation=contract_generation,
            ttl_seconds=600,
            live_registry_roots=[],
        )
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        monkeypatch.setattr(
            deepseek_web, "_candidate_authority", lambda _registry: authority
        )
        monkeypatch.setattr(
            deepseek_web, "_verify_shell_identity", lambda _env: None
        )
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: "a" * 40)
        monkeypatch.setattr(
            deepseek_web,
            "_current_dsh_version",
            lambda: "0.1.1-rc.2",
        )
        env = {
            "SC_API_TOKEN": "candidate-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "4",
            "SC_SHELL_SHORTNAME": "DEV4",
            "SC_DSH_PROOF_CAPABILITY_FILE": str(grant.artifact),
        }
        preflight = deepseek_web.preflight_candidate_execution(
            env=env,
            root_session_id=SURFACE_ROOT,
            conversation_id=SURFACE_CONVERSATION,
            lifecycle_epoch=1,
            worktree=worktree,
        )
        assert preflight is not None
        assert preflight["binding_snapshot_generation"] == 0
        assert preflight["binding_record_generation"] is None
        bound = deepseek_web.bind_session_identity(
            env=env,
            root_session_id=SURFACE_ROOT,
            conversation_id=SURFACE_CONVERSATION,
            lifecycle_epoch=1,
            worktree=worktree,
            candidate_preflight=preflight,
        )
        assert bound["record_generation"] == 1
        assert registry.read_snapshot()["snapshot_generation"] == 1

    with tempfile.TemporaryDirectory() as raw:
        registry, worktree, contract_generation = runtime_registry(Path(raw))
        authority = DeepSeekCandidateAuthority(
            registry.layout.root / "proof-authority"
        )
        grant = authority.mint(
            mode="candidate",
            exact_ref="a" * 40,
            pinned_dsh_version="0.1.1-rc.2",
            disposable_baseline="arch-clean-2026-08-25",
            proof_run_id="proof-run-drift",
            roots={
                SURFACE_ROOT: {
                    "conversation_id": SURFACE_CONVERSATION,
                    "lifecycle_epoch": 1,
                    "verified_lineage": [],
                }
            },
            plugin_contract_generation=contract_generation,
            ttl_seconds=600,
            live_registry_roots=[],
        )
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        monkeypatch.setattr(
            deepseek_web, "_candidate_authority", lambda _registry: authority
        )
        env = {
            "SC_API_TOKEN": "candidate-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "4",
            "SC_SHELL_SHORTNAME": "DEV4",
            "SC_DSH_PROOF_CAPABILITY_FILE": str(grant.artifact),
        }
        preflight = deepseek_web.preflight_candidate_execution(
            env=env,
            root_session_id=SURFACE_ROOT,
            conversation_id=SURFACE_CONVERSATION,
            lifecycle_epoch=1,
            worktree=worktree,
        )
        registry.create_binding(
            expected_snapshot_generation=0,
            root_session_id="unrelated-root",
            conversation_id="unrelated-conversation",
            lifecycle_epoch=1,
            shell_id=8,
            shell_shortname="OTHER",
            shell_worktree=worktree,
            api_base="http://127.0.0.1:8837",
            token="unrelated-token",
            plugin_contract_generation=contract_generation,
        )
        before = durable_identity_state(registry, authority)
        with pytest.raises(deepseek_web.DeepSeekWebError) as stale:
            deepseek_web.bind_session_identity(
                env=env,
                root_session_id=SURFACE_ROOT,
                conversation_id=SURFACE_CONVERSATION,
                lifecycle_epoch=1,
                worktree=worktree,
                candidate_preflight=preflight,
            )
        assert stale.value.code == "HARNESS_PROOF_CAPABILITY_STALE"
        assert durable_identity_state(registry, authority) == before


def test_ordinary_runtime_never_reads_candidate_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deepseek_web,
        "_identity_registry",
        lambda _env: pytest.fail("ordinary admission touched proof state"),
    )
    assert deepseek_web.admit_candidate_execution(
        env={},
        root_session_id="ordinary-root",
        conversation_id="ordinary-conversation",
        lifecycle_epoch=1,
    ) is None

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        artifact = root / "untrusted" / "capabilities" / "missing.json"
        registry = type(
            "Registry", (), {
                "layout": type("Layout", (), {"root": root / "fixed"})(),
            },
        )()
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        with pytest.raises(deepseek_web.DeepSeekWebError) as invalid:
            deepseek_web.admit_candidate_execution(
                env={"SC_DSH_PROOF_CAPABILITY_FILE": str(artifact)},
                root_session_id="ordinary-root",
                conversation_id="ordinary-conversation",
                lifecycle_epoch=1,
            )
        assert invalid.value.code == "HARNESS_PROOF_CAPABILITY_UNSAFE"
        assert sorted(root.glob("**/*")) == []


def test_server_mint_derives_runtime_and_uses_only_fixed_authority_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def __init__(self, root: Path, records: dict | None = None) -> None:
            self.layout = type("Layout", (), {"root": root})()
            self.records = records or {}

        def read_snapshot(self):
            return {"records": self.records}

        def read_live_health(self):
            return {"plugin_contract_generation": "contract-one"}

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        registry = Registry(root)
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: "a" * 40)
        monkeypatch.setattr(
            deepseek_web.harness_versions, "probe", lambda _harness: "0.1.1-rc.2"
        )
        grant = deepseek_web.mint_candidate_capability(
            env={},
            mode="candidate",
            disposable_baseline="arch-clean-2026-08-25",
            proof_run_id="proof-run-25",
            roots=ROOTS,
            ttl_seconds=600,
        )
        assert grant["generation"] == 1
        assert grant["exact_ref"] == "a" * 40
        artifact = Path(grant["artifact"])
        assert artifact.parent == root / "proof-authority" / "capabilities"
        assert json.loads(
            (root / "proof-authority" / "authority.json").read_text()
        )["plugin_contract_generation"] == "contract-one"

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        registry = Registry(root, {"ordinary-root": {"state": "terminal"}})
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: registry
        )
        with pytest.raises(deepseek_web.DeepSeekWebError) as dirty_seat:
            deepseek_web.mint_candidate_capability(
                env={},
                mode="candidate",
                disposable_baseline="arch-clean-2026-08-25",
                proof_run_id="proof-run-25",
                roots=ROOTS,
                ttl_seconds=600,
            )
        assert dirty_seat.value.code == "HARNESS_PROOF_SEAT_NOT_CLEAN"
        assert sorted(root.glob("**/*")) == []


def test_server_restart_ratchet_requires_recovered_exact_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def __init__(self, root: Path, *, wrong_epoch: bool = False) -> None:
            self.layout = type("Layout", (), {"root": root})()
            self.wrong_epoch = wrong_epoch

        def read_live_health(self):
            return {"plugin_contract_generation": "contract-two"}

        def read_snapshot(self):
            return {
                "records": {
                    "candidate-root-a": {
                        "state": "active",
                        "conversation_id": "conversation-a",
                        "lifecycle_epoch": 99 if self.wrong_epoch else 3,
                        "record_generation": 5,
                        "plugin_contract_generation": "contract-two",
                    },
                    "candidate-root-b": {
                        "state": "active",
                        "conversation_id": "conversation-b",
                        "lifecycle_epoch": 7,
                        "record_generation": 8,
                        "plugin_contract_generation": "contract-two",
                    },
                },
                "lineage": {
                    "child-a": {
                        "root_session_id": "candidate-root-a",
                        "lifecycle_epoch": 3,
                        "record_generation": 5,
                    },
                },
            }

    with tempfile.TemporaryDirectory() as raw:
        authority = DeepSeekCandidateAuthority(Path(raw) / "proof-authority")
        first = mint(authority)
        monkeypatch.setattr(deepseek_web, "_exact_engine_ref", lambda: "a" * 40)
        monkeypatch.setattr(
            deepseek_web.harness_versions, "probe", lambda _harness: "0.1.1-rc.2"
        )
        monkeypatch.setattr(
            deepseek_web, "_identity_registry", lambda _env: Registry(Path(raw))
        )
        ratcheted = deepseek_web.ratchet_candidate_after_host_restart(
            env={}, artifact=first.artifact, ttl_seconds=600
        )
        assert ratcheted == {
            "mode": "candidate",
            "generation": 2,
            "artifact": ratcheted["artifact"],
            "proof_run_id": "proof-run-25",
            "exact_ref": "a" * 40,
            "plugin_contract_generation": "contract-two",
        }
        assert Path(ratcheted["artifact"]).exists()
        with pytest.raises(DeepSeekIdentityError) as stale:
            authority.describe(artifact=first.artifact)
        assert stale.value.code == "HARNESS_PROOF_CAPABILITY_STALE"

    with tempfile.TemporaryDirectory() as raw:
        authority = DeepSeekCandidateAuthority(Path(raw) / "proof-authority")
        first = mint(authority)
        monkeypatch.setattr(
            deepseek_web,
            "_identity_registry",
            lambda _env: Registry(Path(raw), wrong_epoch=True),
        )
        before = (Path(raw) / "proof-authority" / "authority.json").read_bytes()
        with pytest.raises(deepseek_web.DeepSeekWebError) as mismatch:
            deepseek_web.ratchet_candidate_after_host_restart(
                env={}, artifact=first.artifact, ttl_seconds=600
            )
        assert mismatch.value.code == "HARNESS_PROOF_RESTART_BINDING_MISMATCH"
        assert (
            Path(raw) / "proof-authority" / "authority.json"
        ).read_bytes() == before
        assert len(list(
            (Path(raw) / "proof-authority" / "capabilities").glob("*.json")
        )) == 1
