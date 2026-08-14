"""Keyed, system-managed runtime advisories.

The lifecycle client owns evidence and generation allocation.  The API store
owns idempotency, stale-generation rejection, and the Flags projection.  Human
flags never pass through this module.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_KIND = "sandbox_native_readiness"
TOKEN_HEADER = "X-SC-Runtime-Token"
TOKEN_PATH = Path(".sc-state/local/runtime-flags.token")
SOURCE_PREFIX = "sandbox-native-readiness"
REMEDY = (
    "Core sandbox remains available. Run make dos-admin from this fork root "
    "to inspect package evidence and the selected base, then submit reviewed "
    "remediation. Do not unpin, rename, or substitute a package silently."
)
HEX_SHA256 = frozenset("0123456789abcdef")
OPEN_FIELDS = frozenset(
    {
        "checkout_identity",
        "source_commit",
        "source_tracked_clean",
        "declaration_digest",
        "package_digest",
        "failing_atoms",
        "classification",
        "detail",
        "image_ids",
        "evidence_path",
        "core_runtime",
        "native_packages",
        "fork_readiness",
        "selected_runtime",
        "cutover",
        "remedy",
    }
)
CLEARANCE_FIELDS = frozenset(
    {
        "clearance_kind",
        "source_commit",
        "source_tracked_clean",
        "failed_generation",
        "old_declaration_digest",
        "current_declaration_digest",
        "baseline_id",
        "extension_id",
        "package_layer_id",
        "requested",
        "observed",
        "proof_digest",
        "package_receipt",
        "evidence",
        "cutover_owed",
    }
)


class RuntimeFlagError(RuntimeError):
    """Runtime-advisory request or persistence failed."""


def canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_key(installation_identity: str, checkout_identity: str) -> str:
    value = f"{SOURCE_PREFIX}\0{installation_identity}\0{checkout_identity}"
    return hashlib.sha256(value.encode()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in HEX_SHA256 for character in value)
    )


def _is_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in HEX_SHA256 for character in value)
    )


def error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _validate_open(advisory: Any) -> str | None:
    if not isinstance(advisory, dict):
        return "advisory must be an object"
    unknown = sorted(set(advisory) - OPEN_FIELDS)
    missing = sorted(OPEN_FIELDS - set(advisory))
    if unknown:
        return f"advisory has unknown field {unknown[0]!r}"
    if missing:
        return f"advisory is missing field {missing[0]!r}"
    if advisory["remedy"] != REMEDY:
        return "advisory remedy does not match the managed contract"
    if advisory["core_runtime"] != "ready":
        return "advisory core_runtime must be 'ready'"
    if advisory["native_packages"] != "advisory":
        return "advisory native_packages must be 'advisory'"
    if advisory["fork_readiness"] != "degraded":
        return "advisory fork_readiness must be 'degraded'"
    if advisory["selected_runtime"] not in {"existing_unchanged", "engine_baseline"}:
        return "advisory selected_runtime is invalid"
    if advisory["cutover"] not in {"unchanged", "baseline_fallback"}:
        return "advisory cutover is invalid"
    if not _is_sha256(advisory["checkout_identity"]):
        return "advisory checkout_identity must be lowercase SHA-256"
    if not _is_object_id(advisory["source_commit"]):
        return "advisory source_commit must be an immutable Git object ID"
    if type(advisory["source_tracked_clean"]) is not bool:
        return "advisory source_tracked_clean must be boolean"
    if not isinstance(advisory["failing_atoms"], list) or len(
        advisory["failing_atoms"]
    ) > 64 or any(
        not isinstance(atom, str) or len(atom.encode("utf-8")) > 256
        for atom in advisory["failing_atoms"]
    ):
        return "advisory failing_atoms must be a bounded array of package atoms"
    image_ids = advisory["image_ids"]
    if not isinstance(image_ids, dict) or set(image_ids) - {
        "parent", "engine_base", "package_layer", "candidate"
    } or any(
        not isinstance(value, str)
        or (value != "none" and not value.startswith("sha256:"))
        for value in image_ids.values()
    ):
        return "advisory image_ids must contain only bounded lifecycle identities"
    if not isinstance(advisory["evidence_path"], str) or not 1 <= len(
        advisory["evidence_path"]
    ) <= 1024:
        return "advisory evidence_path must contain 1-1024 characters"
    if advisory["classification"] not in {
        "native_package_candidate", "package_receipt", "stale_no_build"
    }:
        return "advisory classification is invalid"
    if not isinstance(advisory["detail"], str) or len(advisory["detail"]) > 2000:
        return "advisory detail must be a string of at most 2000 characters"
    return None


def _validate_clearance(clearance: Any) -> str | None:
    if not isinstance(clearance, dict):
        return "clearance must be an object"
    unknown = sorted(set(clearance) - CLEARANCE_FIELDS)
    missing = sorted(CLEARANCE_FIELDS - set(clearance))
    if unknown:
        return f"clearance has unknown field {unknown[0]!r}"
    if missing:
        return f"clearance is missing field {missing[0]!r}"
    kind = clearance["clearance_kind"]
    if kind not in {"current_contract", "declaration_absent", "packages_removed"}:
        return "clearance_kind is invalid"
    if clearance["source_tracked_clean"] is not True:
        return "clearance requires a clean tracked source"
    if type(clearance["failed_generation"]) is not int or clearance["failed_generation"] < 1:
        return "clearance failed_generation must be a positive integer"
    if not isinstance(clearance["requested"], list) or not isinstance(
        clearance["observed"], list
    ):
        return "clearance requested and observed must be arrays"
    if kind == "current_contract":
        if not clearance["requested"]:
            return "current-contract clearance requires the complete package set"
        if not _is_sha256(clearance["proof_digest"]):
            return "current-contract clearance requires an exact proof digest"
        if not isinstance(clearance["package_receipt"], str) or clearance[
            "package_receipt"
        ] in {"", "none"}:
            return "current-contract clearance requires a package receipt"
    else:
        package_none = (
            clearance["package_layer_id"] == "none"
            and clearance["requested"] == []
            and clearance["observed"] == []
            and clearance["proof_digest"] == "none"
            and clearance["package_receipt"] == "none"
        )
        if not package_none:
            return "no-longer-applicable clearance requires explicit package-none evidence"
    return None


def validate_request(source: str, body: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not _is_sha256(source):
        return None, error("validation_error", "source key must be lowercase SHA-256", field="source_key")
    if not isinstance(body, dict):
        return None, error("validation_error", "request body must be an object", field="body")
    allowed = {"state", "source_kind", "generation", "evidence_digest", "advisory", "clearance"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        return None, error("validation_error", "unknown field", field=unknown[0])
    state = body.get("state")
    if state not in {"open", "resolved"}:
        return None, error("validation_error", "state must be 'open' or 'resolved'", field="state")
    if body.get("source_kind") != SOURCE_KIND:
        return None, error("validation_error", "source_kind is invalid", field="source_kind")
    generation = body.get("generation")
    if type(generation) is not int or generation < 1:
        return None, error("validation_error", "generation must be a positive integer", field="generation")
    evidence = body.get("advisory") if state == "open" else body.get("clearance")
    wrong = _validate_open(evidence) if state == "open" else _validate_clearance(evidence)
    if wrong:
        return None, error("validation_error", wrong, field=state)
    expected = canonical_digest(evidence)
    if body.get("evidence_digest") != expected:
        return None, error(
            "validation_error",
            "evidence_digest does not match the canonical payload",
            field="evidence_digest",
            expected=expected,
        )
    return body, None


def put_runtime_flag(con, source: str, body: Any) -> tuple[int, dict[str, Any]]:
    request, invalid = validate_request(source, body)
    if invalid is not None:
        return 422, invalid
    assert request is not None
    generation = request["generation"]
    latest = con.execute(
        "SELECT flag_id,resolved,source_generation,evidence_digest FROM flags "
        "WHERE source_key=? AND management_state='system' "
        "ORDER BY source_generation DESC,flag_id DESC LIMIT 1",
        (source,),
    ).fetchone()
    if latest is not None and generation < latest["source_generation"]:
        return 409, error(
            "stale_generation",
            "a newer runtime-advisory generation already exists",
            latest_generation=latest["source_generation"],
        )
    wanted_resolved = request["state"] == "resolved"
    if latest is not None and generation == latest["source_generation"]:
        if (
            bool(latest["resolved"]) == wanted_resolved
            and latest["evidence_digest"] == request["evidence_digest"]
        ):
            return 200, {
                "flag_id": latest["flag_id"],
                "generation": generation,
                "state": request["state"],
                "created": False,
                "idempotent": True,
            }
        return 409, error(
            "generation_conflict",
            "generation already carries different evidence or state",
            generation=generation,
        )

    payload = request["advisory"] if not wanted_resolved else request["clearance"]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    open_row = con.execute(
        "SELECT flag_id,source_generation FROM flags WHERE source_key=? "
        "AND management_state='system' AND resolved=0 AND COALESCE(is_deleted,0)=0",
        (source,),
    ).fetchone()
    if wanted_resolved:
        if open_row is None:
            return 409, error(
                "clearance_not_applicable",
                "no open managed advisory exists for this source key",
                source_key=source,
            )
        if payload["failed_generation"] != open_row["source_generation"]:
            return 409, error(
                "clearance_generation_mismatch",
                "clearance does not cite the current failed generation",
                failed_generation=open_row["source_generation"],
            )
        notes = (
            f"managed clearance: {payload['clearance_kind']}; "
            f"evidence: {payload['evidence']}"
        )
        con.execute(
            "UPDATE flags SET resolved=1,resolved_date=?,resolution_notes=?,"
            "source_generation=?,evidence_digest=?,source_payload=? WHERE flag_id=?",
            (
                datetime.now(timezone.utc).date().isoformat(),
                notes,
                generation,
                request["evidence_digest"],
                encoded,
                open_row["flag_id"],
            ),
        )
        con.commit()
        return 200, {
            "flag_id": open_row["flag_id"],
            "generation": generation,
            "state": "resolved",
            "created": False,
            "idempotent": False,
        }

    description = (
        f"[{payload['classification']}] {payload['detail']}\n\n"
        f"evidence: {payload['evidence_path']}\n\n{payload['remedy']}"
    )
    if open_row is None:
        cursor = con.execute(
            "INSERT INTO flags "
            "(display_name,priority,description,feature_id,source_kind,source_key,"
            "source_generation,evidence_digest,management_state,severity,"
            "blocking_scope,blocks_runtime,source_payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Native sandbox readiness advisory",
                "Low",
                description,
                None,
                SOURCE_KIND,
                source,
                generation,
                request["evidence_digest"],
                "system",
                "advisory",
                "none",
                0,
                encoded,
            ),
        )
        flag_id = cursor.lastrowid
        created = True
    else:
        flag_id = open_row["flag_id"]
        con.execute(
            "UPDATE flags SET description=?,source_generation=?,evidence_digest=?,"
            "source_payload=? WHERE flag_id=?",
            (description, generation, request["evidence_digest"], encoded, flag_id),
        )
        created = False
    con.commit()
    return (201 if created else 200), {
        "flag_id": flag_id,
        "generation": generation,
        "state": "open",
        "created": created,
        "idempotent": False,
    }


def _token_path(installation_root: Path) -> Path:
    return installation_root / TOKEN_PATH


def ensure_system_token(installation_root: Path) -> str:
    path = _token_path(installation_root)
    try:
        current = path.read_text(encoding="ascii").strip()
    except OSError:
        current = ""
    if len(current) == 64 and all(character in HEX_SHA256 for character in current):
        return current
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_hex(32)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    stored = path.read_text(encoding="ascii").strip()
    if len(stored) != 64 or any(character not in HEX_SHA256 for character in stored):
        raise RuntimeFlagError(f"managed runtime token is invalid: {path}")
    return stored


def token_matches(installation_root: Path, supplied: str) -> bool:
    try:
        expected = _token_path(installation_root).read_text(encoding="ascii").strip()
    except OSError:
        return False
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def put_via_api(
    installation_root: Path,
    port: int,
    source: str,
    body: Mapping[str, Any],
    *,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    token = ensure_system_token(installation_root)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/_sc/runtime-flags/{source}",
        data=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except (json.JSONDecodeError, OSError):
            payload = error("http_error", f"runtime advisory API returned {exc.code}")
        return exc.code, payload


def reconcile_pending(con, installation_root: Path) -> list[dict[str, Any]]:
    """Apply locally durable intents in generation order without HTTP recursion."""
    pending = installation_root / ".sc-state/local/runtime-flags/pending"
    if not pending.is_dir():
        return []
    results = []
    for path in sorted(pending.glob("*.json")):
        try:
            envelope = json.loads(path.read_text())
            source = envelope["source_key"]
            body = envelope["body"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        status, payload = put_runtime_flag(con, source, body)
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        if status in {200, 201} or (status == 409 and code == "stale_generation"):
            path.unlink(missing_ok=True)
        results.append({"status": status, "payload": payload, "path": str(path)})
    return results
