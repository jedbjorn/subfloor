#!/usr/bin/env python3
"""Build the frozen DSH removal inventory and installed-floor fixtures.

The generated files are review artifacts, not a live updater.  They pin the
last pre-bridge DSH bytes and the state shapes that later two-hop tests must
materialize without depending on Git history (CI checkouts are shallow).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
PAYLOAD_PATH = FIXTURE_DIR / "tracked-artifacts.tar.gz"
PRE_BRIDGE_PATH = FIXTURE_DIR / "pre-bridge.json"
COMPATIBILITY_PATH = FIXTURE_DIR / "compatibility-floor.json"

BASELINE_SOURCE_REF = "f5dbfa0a775ee4bc6f5038c4f9e56ba7ffc92792"
REFERENCE_PATTERN = re.compile(r"deepseek|(?<![a-z0-9])dsh(?![a-z0-9])", re.I)
SCAN_ROOTS = (
    ".dockerignore",
    ".gitignore",
    ".github",
    ".super-coder",
    "docs",
    "maintainer",
    "tests",
    "Makefile",
    "README.md",
    "requirements.txt",
    "sc",
)
SCAN_EXCLUSIONS = (
    ".super-coder/assets/dsh-removal/",
    "tests/fixtures/dsh_removal/",
    "tests/fixtures/live_model/",
    "tests/test_dsh_removal_preparation.py",
)

FROZEN_PARTITIONS = (
    "tracked_artifacts",
    "shared_sources",
    "verification_sources",
    "immutable_reference_migrations",
)

DSH_MIGRATIONS = {
    ".super-coder/migrations/0227_deepseek_controlled_route_binding.sql",
    ".super-coder/migrations/0230_deepseek_stock_host_route_binding.sql",
    ".super-coder/migrations/0235_live_native_route_binding_v3.sql",
    ".super-coder/migrations/0236_live_native_conversation_routes.sql",
}

ACTIVE_STATE_OWNERS = [
    {
        "table": "flavor_defaults",
        "selector": "harness='deepseek'",
        "removal": "delete DSH defaults",
        "retained": "keep exact harness='opencode' rows, including DeepSeek-family model IDs",
    },
    {
        "table": "model_routes",
        "selector": "harness='deepseek'",
        "removal": "delete DSH catalogue routes and errors",
        "retained": "keep OpenCode routes and exact provider/model/option identity",
    },
    {
        "table": "model_catalog_generations",
        "selector": "DSH-owned runtime, version, fingerprint, source, or error payload",
        "removal": "purge DSH-derived current catalogue cache generations",
        "retained": "keep only generations containing no DSH owner or reference",
    },
    {
        "table": "analytics_parse_cache",
        "selector": "harness='deepseek'",
        "removal": "delete DSH parser cache and normalized DSH usage rows",
        "retained": "keep retained-harness usage and cache rows byte-for-byte",
    },
    {
        "table": "conversations",
        "selector": "harness='deepseek'",
        "removal": "delete each DSH conversation root and every descendant message, event, transcript, attachment, recovery, usage, and index row",
        "retained": "keep unrelated conversation graphs byte-for-byte",
    },
    {
        "table": "conversation_runs",
        "selector": "joined conversation harness='deepseek'",
        "removal": "delete every joined run; write no terminal result, tombstone, or recovery evidence",
        "retained": "keep unrelated runs byte-for-byte",
    },
    {
        "table": "conversation_outbox",
        "selector": "joined conversation harness='deepseek'",
        "removal": "delete every joined outbox row without dispatch or terminal evidence",
        "retained": "keep unrelated delivery rows byte-for-byte",
    },
    {
        "table": "active_shell_chats",
        "selector": "chat_id references a DSH conversation",
        "removal": "remove active process/admission pointers",
        "retained": "keep unrelated active chats byte-for-byte",
    },
    {
        "table": "sprint_participants",
        "selector": "harness='deepseek'",
        "removal": "delete the DSH participant and every DSH-owned assignment, relay, wake, report, expectation, event, and route reference",
        "retained": "keep unrelated portions of a mixed Sprint only after every DSH relation is gone",
    },
    {
        "table": "sprint_participant_route_bindings",
        "selector": "harness='deepseek'",
        "removal": "delete every DSH binding revision and all references to it",
        "retained": "keep retained-harness binding revisions byte-for-byte",
    },
]

PROCESSES = [
    {
        "name": "stock-dsh-host",
        "state_path": ".super-coder/run/deepseek-web.json",
        "pid_field": "web_pid",
        "start_ticks_field": "web_start_ticks",
        "command_identity": ["dsh", "--profile", "<profile-id>", "--host", "127.0.0.1"],
        "port_field": "service_port",
    },
    {
        "name": "super-coder-dsh-relay",
        "state_path": ".super-coder/run/deepseek-web.json",
        "pid_field": "relay_pid",
        "start_ticks_field": "relay_start_ticks",
        "command_identity": ["python", "deepseek_web.py", "relay"],
        "port_field": "relay_port",
    },
]

PORTS = [
    {
        "name": "public",
        "owner": "super-coder-dsh-relay",
        "formula": "instance.deepseek_host_port = 8900 + repository offset",
    },
    {
        "name": "private-host-bare-metal",
        "owner": "stock-dsh-host",
        "formula": "instance.deepseek_host_port + 10000",
    },
    {
        "name": "private-relay-sandbox",
        "owner": "super-coder-dsh-relay",
        "formula": "instance.deepseek_host_port + 10000",
    },
]

GENERATED_ARTIFACTS = [
    {
        "path": ".super-coder/run/deepseek",
        "kind": "bounded-tree",
        "ownership": "engine runtime root from deepseek_runtime.RUN_ROOT",
    },
    {
        "path": ".super-coder/run/deepseek-identity/<fork-id>",
        "kind": "marked-bounded-tree",
        "ownership": "registry/profile/credential/proof root carrying sc-dsh contracts and canonical fork identity",
    },
    *[
        {"path": path, "kind": "exact-file", "ownership": "deepseek_web constant or sibling"}
        for path in (
            ".super-coder/run/deepseek-web.json",
            ".super-coder/run/deepseek-web.lock",
            ".super-coder/run/deepseek-shell-api.json",
            ".super-coder/run/deepseek-web-generation.json",
            ".super-coder/run/deepseek-managed-session.json",
            ".super-coder/run/deepseek-web-activity.json",
            ".super-coder/run/deepseek-web-gateway.lock",
            ".super-coder/logs/deepseek-web.log",
        )
    ],
]

RETAINED_CONTROLS = [
    "Claude, Codex, OpenCode, Vibe, and Kimi adapter manifests and install/probe paths",
    "OpenCode-owned DeepSeek-family selectors and native-option identity",
    "unrelated retained-harness conversation and Sprint graphs byte-for-byte",
    "instance port and dev_port plus API/review service ownership",
    "ordinary updater, migration ledger, and service cutover only where no DSH owner or reference remains",
    "user-owned ~/.dsh, external global packages, and unmarked caches",
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *SCAN_ROOTS],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        path
        for path in result.stdout.splitlines()
        if path and not any(path.startswith(prefix) for prefix in SCAN_EXCLUSIONS)
    )


def reference_files() -> list[str]:
    hits: list[str] = []
    for relative in tracked_files():
        path = ROOT / relative
        if REFERENCE_PATTERN.search(relative):
            hits.append(relative)
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            body = path.read_text()
        except UnicodeDecodeError:
            continue
        if REFERENCE_PATTERN.search(body):
            hits.append(relative)
    return hits


def is_owned_artifact(relative: str) -> bool:
    path = Path(relative)
    return (
        relative == ".super-coder/adapters/deepseek/adapter.json"
        or relative.startswith(".super-coder/assets/deepseek/")
        or relative == ".super-coder/scripts/conversation_adapters/deepseek.py"
        or (
            relative.startswith(".super-coder/scripts/")
            and path.name.startswith(("deepseek_", "dsh_"))
        )
        or relative == ".super-coder/scripts/build_deepseek_carrier.py"
    )


def is_verification_source(relative: str) -> bool:
    path = Path(relative)
    return relative.startswith("tests/") and (
        path.name.startswith(("test_deepseek", "test_dsh", "deepseek_"))
        or "deepseek" in path.name
        or "dsh" in path.name
    )


def entry(relative: str, **extra: object) -> dict[str, object]:
    path = ROOT / relative
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        **extra,
    }


def migration_ledger() -> list[dict[str, object]]:
    return [
        entry(str(path.relative_to(ROOT)), filename=path.name)
        for path in sorted((ROOT / ".super-coder/migrations").glob("*.sql"))
    ]


def build_manifest() -> dict[str, object]:
    references = reference_files()
    owned = [path for path in references if is_owned_artifact(path)]
    migrations = [path for path in references if path.startswith(".super-coder/migrations/")]
    verification = [
        path for path in references
        if is_verification_source(path) and path not in migrations
    ]
    shared = sorted(set(references) - set(owned) - set(migrations) - set(verification))
    return {
        "contract": "sc-dsh-removal-manifest-v1",
        "schema_version": 1,
        "baseline_source_ref": BASELINE_SOURCE_REF,
        "governing_spec": {
            "document_id": 178,
            "revision_sha256": "be111eb206b9e6ea09352a90346e8a94544ac1ef3bd932716e4bd90451d33c42",
            "task_id": 680,
            "sprint_id": 29,
            "work_unit_id": 129,
        },
        "scan": {
            "pattern": REFERENCE_PATTERN.pattern,
            "roots": list(SCAN_ROOTS),
            "excluded_provenance_roots": list(SCAN_EXCLUSIONS),
        },
        "freeze_policy": {
            "source_of_truth": [
                ".super-coder/assets/dsh-removal/removal-manifest-v1.json",
                "tests/fixtures/dsh_removal/pre-bridge.json",
                "tests/fixtures/dsh_removal/compatibility-floor.json",
                "tests/fixtures/dsh_removal/tracked-artifacts.tar.gz",
            ],
            "ci_validation": "committed-artifact-internal-consistency-only",
            "refresh_command": (
                "python tests/fixtures/dsh_removal/build_fixtures.py --refresh"
            ),
            "refresh_boundary": (
                "pre-removal preparation only; forbidden after any frozen source, "
                "artifact, verification input, or migration ledger byte changes"
            ),
        },
        "tracked_artifacts": [
            entry(path, ownership="engine-tracked", cleanup="exact-digest-delete")
            for path in owned
        ],
        "shared_sources": [entry(path, ownership="retained-shared-source") for path in shared],
        "verification_sources": [
            entry(path, ownership="source-only-verification") for path in verification
        ],
        "immutable_reference_migrations": [
            entry(
                path,
                disposition=(
                    "remove-after-purge-rebaseline" if path in DSH_MIGRATIONS
                    else "retain-opencode-owned-model-data"
                ),
            )
            for path in migrations
        ],
        "immutable_migration_ledger": migration_ledger(),
        "active_state_owners": ACTIVE_STATE_OWNERS,
        "processes": PROCESSES,
        "ports": PORTS,
        "generated_artifacts": GENERATED_ARTIFACTS,
        "retained_controls": RETAINED_CONTROLS,
        "external_preserve": ["~/.dsh", "global @deepseek-ai/dsh package tree", "unmarked caches"],
        "installed_fixture_contract": {
            "payload": "tests/fixtures/dsh_removal/tracked-artifacts.tar.gz",
            "pre_bridge": "tests/fixtures/dsh_removal/pre-bridge.json",
            "compatibility_floor": "tests/fixtures/dsh_removal/compatibility-floor.json",
            "compatibility_marker": ".sc-state/local/dsh-removal/compatibility-floor.json",
            "cutover_receipt": ".sc-state/local/dsh-removal/cutover-receipt.json",
            "cleanup_receipt": ".sc-state/local/dsh-removal/cleanup-receipt.json",
        },
    }


def build_payload_tar(manifest: dict[str, object]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for artifact in manifest["tracked_artifacts"]:
            relative = str(artifact["path"])
            source = ROOT / relative
            info = tarfile.TarInfo(relative)
            info.size = source.stat().st_size
            info.mode = source.stat().st_mode & 0o777
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with source.open("rb") as handle:
                archive.addfile(info, handle)
    return raw.getvalue()


def build_payload(manifest: dict[str, object]) -> bytes:
    return gzip.compress(build_payload_tar(manifest), compresslevel=9, mtime=0)


def fixture_rows() -> dict[str, list[dict[str, object]]]:
    deepseek_binding = json.dumps(
        {
            "contract_version": 3,
            "control_state": "controlled",
            "harness": "deepseek",
            "requested_model": "deepseek-official/deepseek-chat",
            "provider_model": "deepseek-chat",
            "native_option_id": "high",
            "transport": "deepseek-stock-host-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    opencode_binding = json.dumps(
        {
            "contract_version": 3,
            "control_state": "controlled",
            "harness": "opencode",
            "requested_model": "ollama-cloud/deepseek-v4-pro",
            "provider_model": "deepseek-v4-pro",
            "transport": "opencode-route-agent",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "users": [
            {"user_id": 9001, "username": "dsh-fixture-operator", "email": "fixture@example.invalid", "initials": "DF"}
        ],
        "shells": [
            {"shell_id": 9001, "display_name": "DSH Fixture", "shortname": "DSHF", "flavor": "dev", "system_prompt": "fixture", "user_id": 9001},
            {"shell_id": 9002, "display_name": "OpenCode Fixture", "shortname": "OCF", "flavor": "dev", "system_prompt": "fixture", "user_id": 9001},
            {"shell_id": 9003, "display_name": "Planner Fixture", "shortname": "PLNF", "flavor": "planner", "system_prompt": "fixture", "user_id": 9001},
        ],
        "flavor_defaults": [
            {"flavor": "dev", "harness": "deepseek", "model": "deepseek-official/deepseek-chat", "is_default": 0, "effort": "high"},
            {"flavor": "dev", "harness": "opencode", "model": "ollama-cloud/deepseek-v4-pro", "is_default": 1, "effort": None},
        ],
        "model_catalog_generations": [
            {"generation_id": "d" * 32, "payload_version": 1, "contract_version": 2, "started_at": "2026-08-27 00:00:00", "completed_at": "2026-08-27 00:00:01", "state": "successful", "runtime": "host", "source_summary": "{\"deepseek\":\"live\"}", "harness_versions": "{\"deepseek\":\"0.1.1-rc.2\"}", "source_fingerprints": "{\"deepseek\":\"fixture\"}", "error_summary": None, "payload_digest": "d" * 64},
            {"generation_id": "a" * 32, "payload_version": 1, "contract_version": 2, "started_at": "2026-08-27 00:00:00", "completed_at": "2026-08-27 00:00:01", "state": "successful", "runtime": "host", "source_summary": "{\"opencode\":\"live\"}", "harness_versions": "{\"opencode\":\"1.2.3\"}", "source_fingerprints": "{}", "error_summary": None, "payload_digest": "e" * 64},
        ],
        "model_routes": [
            {"harness": "deepseek", "selector": "deepseek-official/deepseek-chat", "provider": "deepseek-official", "provider_model": "deepseek-chat", "display_name": "DeepSeek Chat", "family": "deepseek", "source": "deepseek-host-api", "availability": "available", "headless_supported": 1, "last_seen_at": "2026-08-27 00:00:01", "generation_id": "d" * 32, "selector_binding": deepseek_binding, "adapter_metadata": "{}"},
            {"harness": "opencode", "selector": "ollama-cloud/deepseek-v4-pro", "provider": "ollama-cloud", "provider_model": "deepseek-v4-pro", "display_name": "DeepSeek V4 Pro", "family": "deepseek", "source": "opencode-live", "availability": "available", "headless_supported": 1, "last_seen_at": "2026-08-27 00:00:01", "generation_id": "a" * 32, "selector_binding": opencode_binding, "adapter_metadata": "{}"},
        ],
        "analytics_parse_cache": [
            {"harness": "deepseek", "parser_version": "fixture-v1", "payload": "{\"runs\":1}", "updated_at": "2026-08-27 00:00:01"},
            {"harness": "opencode", "parser_version": "fixture-v1", "payload": "{\"runs\":2}", "updated_at": "2026-08-27 00:00:01"},
        ],
        "roadmap": [
            {"feature_id": 9001, "title": "DSH fixture Sprint", "roadmap_status": "in_progress", "owning_shell": 9003, "summary": "dirty DSH participant"}
        ],
        "sprints": [
            {"sprint_id": 9001, "feature_id": 9001, "originating_planner_shell_id": 9003, "lifecycle": "paused", "merge_grant_enabled": 0, "conversation_generation": "f" * 32}
        ],
        "sprint_participants": [
            {"participant_id": 9001, "sprint_id": 9001, "shell_id": 9001, "role": "developer", "harness": "deepseek", "model": "deepseek-official/deepseek-chat", "effort": "high", "disposition": "assigned", "active_route_binding_id": 9001},
            {"participant_id": 9002, "sprint_id": 9001, "shell_id": 9002, "role": "reviewer", "harness": "opencode", "model": "ollama-cloud/deepseek-v4-pro", "disposition": "assigned", "active_route_binding_id": 9002},
        ],
        "sprint_participant_route_bindings": [
            {"binding_id": 9001, "participant_id": 9001, "route_revision": 1, "contract_version": 3, "control_state": "controlled", "harness": "deepseek", "requested_model": "deepseek-official/deepseek-chat", "provider_model": "deepseek-chat", "requested_effort": "high", "effective_effort": "high", "native_option_id": "high", "transport": "deepseek-stock-host-v1", "selector_binding": deepseek_binding, "adapter_metadata": "{}", "binding_json": deepseek_binding, "binding_digest": sha256_bytes(deepseek_binding.encode()), "harness_evidence_format": "harness-live-v1", "harness_support_state": "tested"},
            {"binding_id": 9002, "participant_id": 9002, "route_revision": 1, "contract_version": 3, "control_state": "controlled", "harness": "opencode", "requested_model": "ollama-cloud/deepseek-v4-pro", "provider_model": "deepseek-v4-pro", "transport": "opencode-route-agent", "selector_binding": opencode_binding, "adapter_metadata": "{}", "binding_json": opencode_binding, "binding_digest": sha256_bytes(opencode_binding.encode()), "harness_evidence_format": "harness-live-v1", "harness_support_state": "tested"},
        ],
        "conversations": [
            {"conversation_id": "cv_dsh_fixture", "shell_id": 9001, "owner_user_id": 9001, "harness": "deepseek", "provider": "deepseek-official", "model": "deepseek-chat", "effort": "high", "worktree": "/fixture/dsh", "harness_session_ref": "sc-" + "d" * 32, "state": "running", "title": "DSH purge input", "creation_idempotency_key": "dsh-fixture", "creation_request_hash": "d" * 64, "conversation_scope": "normal", "route_contract_version": 3, "route_binding": deepseek_binding},
            {"conversation_id": "cv_opencode_deepseek_fixture", "shell_id": 9002, "owner_user_id": 9001, "harness": "opencode", "provider": "ollama-cloud", "model": "deepseek-v4-pro", "worktree": "/fixture/opencode", "state": "idle", "title": "Retained OpenCode DeepSeek model chat", "creation_idempotency_key": "opencode-fixture", "creation_request_hash": "o" * 64, "conversation_scope": "normal", "route_contract_version": 3, "route_binding": opencode_binding},
        ],
        "conversation_messages": [
            {"message_id": 9001, "conversation_id": "cv_dsh_fixture", "sender_kind": "user", "sender_ref": "9001", "message_kind": "prompt", "body": "purge this DSH-owned prompt graph", "idempotency_key": "dsh-message", "request_hash": "m" * 64, "state": "accepted"},
            {"message_id": 9002, "conversation_id": "cv_opencode_deepseek_fixture", "sender_kind": "user", "sender_ref": "9001", "message_kind": "prompt", "body": "retain OpenCode model access", "idempotency_key": "opencode-message", "request_hash": "n" * 64, "state": "accepted"},
        ],
        "conversation_runs": [
            {"run_id": 9001, "conversation_id": "cv_dsh_fixture", "shell_id": 9001, "trigger_message_id": 9001, "attempt": 1, "harness_session_before": "sc-" + "d" * 32, "state": "running", "lease_owner": "dsh-fixture", "lease_expires_at": "2099-01-01 00:00:00", "started_at": "2026-08-27 00:00:01", "process_pid": 4242, "process_start_ticks": 31337},
        ],
        "conversation_outbox": [
            {"outbox_id": 9001, "conversation_id": "cv_dsh_fixture", "message_id": 9001, "state": "claimed", "claim_owner": "dsh-fixture", "claimed_at": "2026-08-27 00:00:01", "lease_expires_at": "2099-01-01 00:00:00", "attempts": 1}
        ],
        "active_shell_chats": [
            {"shell_id": 9001, "chat_id": "cv_dsh_fixture", "process_pid": 4242, "process_start_ticks": 31337},
            {"shell_id": 9002, "chat_id": "cv_opencode_deepseek_fixture"},
        ],
    }


def floor_fixture(
    *,
    floor: str,
    payload_sha256: str,
    manifest_sha256: str,
    migration_count: int,
) -> dict[str, object]:
    compatibility = floor == "compatibility"
    return {
        "contract": "sc-dsh-installed-update-fixture-v1",
        "schema_version": 1,
        "floor": floor,
        "baseline_source_ref": BASELINE_SOURCE_REF,
        "installed_engine_ref": ("1" if floor == "pre-bridge" else "2") * 40,
        "tracked_payload": {
            "path": "tracked-artifacts.tar.gz",
            "sha256": payload_sha256,
        },
        "target_removal_manifest": {
            "path": ".super-coder/assets/dsh-removal/removal-manifest-v1.json",
            "sha256": manifest_sha256,
        },
        "migration_floor": {
            "first": "0001_seed_skills.sql",
            "last": "0236_live_native_conversation_routes.sql",
            "count": migration_count,
        },
        "compatibility_control": {
            "marker_path": ".sc-state/local/dsh-removal/compatibility-floor.json",
            "marker_present": compatibility,
            "minimum_floor_ref": "2" * 40 if compatibility else None,
            "pre_materialization_hook": compatibility,
            "fresh_process_cleanup_hook": compatibility,
        },
        "instance": {
            "repo": "dsh-removal-fixture",
            "port": 8877,
            "dev_port": 5177,
            "deepseek_host_port": 8977,
            "disabled_harnesses": [],
        },
        "runtime_state": {
            "state_path": ".super-coder/run/deepseek-web.json",
            "web_pid": 4242,
            "web_start_ticks": 31337,
            "relay_pid": 4343,
            "relay_start_ticks": 32337,
            "service_port": 18977,
            "relay_port": 8977,
            "profile_id": "sc-fixture",
            "ownership_contract": "sc-dsh-installed-update-fixture-v1",
        },
        "database_rows": fixture_rows(),
    }


def render_refresh() -> dict[Path, bytes]:
    manifest = build_manifest()
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    payload = build_payload(manifest)
    common = {
        "payload_sha256": sha256_bytes(payload),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "migration_count": len(manifest["immutable_migration_ledger"]),
    }
    pre_bridge = floor_fixture(floor="pre-bridge", **common)
    compatibility = floor_fixture(floor="compatibility", **common)
    return {
        MANIFEST_PATH: manifest_bytes,
        PAYLOAD_PATH: payload,
        PRE_BRIDGE_PATH: (json.dumps(pre_bridge, indent=2, sort_keys=True) + "\n").encode(),
        COMPATIBILITY_PATH: (json.dumps(compatibility, indent=2, sort_keys=True) + "\n").encode(),
    }


def refresh_floor_errors(
    manifest: dict[str, object], *, root: Path = ROOT
) -> list[str]:
    """Return changes that make live-tree fixture refresh unsafe."""
    errors: list[str] = []
    for partition in FROZEN_PARTITIONS:
        for row in manifest[partition]:
            relative = str(row["path"])
            path = root / relative
            if not path.is_file():
                errors.append(f"missing frozen input: {relative}")
                continue
            if sha256_file(path) != row["sha256"]:
                errors.append(f"changed frozen input: {relative}")

    expected_migrations = [
        str(row["path"]) for row in manifest["immutable_migration_ledger"]
    ]
    actual_migrations = [
        str(path.relative_to(root))
        for path in sorted((root / ".super-coder/migrations").glob("*.sql"))
    ]
    if actual_migrations != expected_migrations:
        errors.append("migration ledger membership changed")
    return errors


def assert_refresh_floor() -> None:
    if not MANIFEST_PATH.exists():
        return
    errors = refresh_floor_errors(json.loads(MANIFEST_PATH.read_text()))
    if errors:
        raise SystemExit(
            "DSH fixture refresh is forbidden beyond its preparation floor: "
            + "; ".join(errors)
        )


def frozen_validation_errors() -> list[str]:
    """Validate committed fixtures without consulting mutable live source bytes."""
    errors: list[str] = []
    try:
        manifest_bytes = MANIFEST_PATH.read_bytes()
        manifest = json.loads(manifest_bytes)
        payload = PAYLOAD_PATH.read_bytes()
        pre_bridge = json.loads(PRE_BRIDGE_PATH.read_bytes())
        compatibility = json.loads(COMPATIBILITY_PATH.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load frozen fixture: {exc}"]

    paths = [
        str(row["path"])
        for partition in FROZEN_PARTITIONS
        for row in manifest[partition]
    ]
    if len(paths) != len(set(paths)):
        errors.append("manifest partitions overlap")

    ledger = {str(row["path"]): row for row in manifest["immutable_migration_ledger"]}
    for row in manifest["immutable_reference_migrations"]:
        frozen = ledger.get(str(row["path"]))
        if frozen is None or frozen["sha256"] != row["sha256"]:
            errors.append(f"reference migration not pinned by ledger: {row['path']}")

    payload_digest = sha256_bytes(payload)
    manifest_digest = sha256_bytes(manifest_bytes)
    for fixture in (pre_bridge, compatibility):
        if fixture["tracked_payload"]["path"] != PAYLOAD_PATH.name:
            errors.append(f"{fixture['floor']} payload path mismatch")
        if fixture["tracked_payload"]["sha256"] != payload_digest:
            errors.append(f"{fixture['floor']} payload digest mismatch")
        if fixture["target_removal_manifest"]["path"] != str(
            MANIFEST_PATH.relative_to(ROOT)
        ):
            errors.append(f"{fixture['floor']} target manifest path mismatch")
        if fixture["target_removal_manifest"]["sha256"] != manifest_digest:
            errors.append(f"{fixture['floor']} target manifest digest mismatch")
        if fixture["migration_floor"]["count"] != len(
            manifest["immutable_migration_ledger"]
        ):
            errors.append(f"{fixture['floor']} migration count mismatch")
    if pre_bridge["database_rows"] != compatibility["database_rows"]:
        errors.append("installed-floor database rows differ")

    try:
        tar_bytes = gzip.decompress(payload)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            members = archive.getmembers()
            actual: dict[str, tuple[str, int]] = {}
            for member in members:
                if not member.isfile():
                    errors.append(f"payload member is not a file: {member.name}")
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    errors.append(f"payload member cannot be read: {member.name}")
                    continue
                body = extracted.read()
                actual[member.name] = (sha256_bytes(body), len(body))
                if any((member.mtime, member.uid, member.gid)):
                    errors.append(f"payload metadata is not deterministic: {member.name}")
            if len(actual) != len(members):
                errors.append("payload contains duplicate member paths")
    except (OSError, EOFError, tarfile.TarError) as exc:
        errors.append(f"payload cannot be decoded: {exc}")
        actual = {}

    expected = {
        str(row["path"]): (str(row["sha256"]), int(row["bytes"]))
        for row in manifest["tracked_artifacts"]
    }
    if actual != expected:
        errors.append("payload members do not match frozen tracked-artifact digests")
    return errors


def check_frozen() -> None:
    errors = frozen_validation_errors()
    if errors:
        raise SystemExit("invalid frozen DSH removal fixture: " + "; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_frozen()
        return 0

    assert_refresh_floor()
    for path, body in render_refresh().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    check_frozen()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
