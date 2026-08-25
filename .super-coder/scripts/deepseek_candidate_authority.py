"""Revocable exact-ref authority for disposable DeepSeek proof sessions.

Ordinary Browser, Sprint, and one-shot calls never consult this store.  The
dedicated promotion runner may present one owner-only capability artifact to
admit only the root sessions enumerated when the server minted it.  A Host
restart advances the capability generation without changing those roots,
lifecycle epochs, or verified lineage.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_identity_registry import DeepSeekIdentityError

CONTRACT = "sc-dsh-proof-capability-v1"
MODES = frozenset({"candidate", "promoted"})
EXACT_REF = re.compile(r"[0-9a-f]{40}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class CapabilityGrant:
    mode: str
    generation: int
    artifact: Path
    proof_run_id: str
    exact_ref: str
    plugin_contract_generation: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _owner_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise DeepSeekIdentityError(
            "HARNESS_PROOF_CAPABILITY_UNSAFE",
            "proof capability directory is unsafe",
        )
    os.chmod(path, 0o700)


def _atomic_owner_write(path: Path, payload: bytes) -> None:
    _owner_directory(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _owner_json(path: Path, *, unavailable: str) -> dict[str, Any]:
    try:
        stat = path.lstat()
        if path.is_symlink() or stat.st_mode & 0o777 != 0o600:
            raise OSError("unsafe mode")
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise DeepSeekIdentityError(unavailable, "proof capability is unavailable") from exc
    if not isinstance(value, dict):
        raise DeepSeekIdentityError(unavailable, "proof capability is malformed")
    return value


class DeepSeekCandidateAuthority:
    """Owner-only candidate/promoted proof capability state."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.state_path = self.root / "authority.json"
        self.lock_path = self.root / "authority.lock"
        self.artifacts = self.root / "capabilities"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _locked(self):
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
            fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _roots(
        roots: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not roots:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_INVALID",
                "proof capability requires enumerated root sessions",
            )
        normalized: dict[str, dict[str, Any]] = {}
        for session_id, raw in sorted(roots.items()):
            epoch = raw.get("lifecycle_epoch")
            conversation_id = raw.get("conversation_id")
            lineage = raw.get("verified_lineage", [])
            if (
                SAFE_ID.fullmatch(session_id) is None
                or not isinstance(epoch, int)
                or isinstance(epoch, bool)
                or epoch <= 0
                or not isinstance(conversation_id, str)
                or not conversation_id
                or not isinstance(lineage, Sequence)
                or isinstance(lineage, (str, bytes))
                or any(
                    not isinstance(item, str) or SAFE_ID.fullmatch(item) is None
                    for item in lineage
                )
                or len(set(lineage)) != len(lineage)
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_INVALID",
                    "proof root identity, lifecycle epoch, or lineage is invalid",
                )
            normalized[session_id] = {
                "conversation_id": conversation_id,
                "lifecycle_epoch": epoch,
                "verified_lineage": sorted(lineage),
            }
        return normalized

    def _new_artifact(self, *, token: str, generation: int) -> Path:
        path = self.artifacts / f"capability-{generation}-{uuid.uuid4().hex}.json"
        _atomic_owner_write(
            path,
            _canonical({"contract": CONTRACT, "generation": generation, "token": token})
            + b"\n",
        )
        return path

    def _read_state(self) -> dict[str, Any]:
        state = _owner_json(
            self.state_path, unavailable="HARNESS_PROOF_CAPABILITY_UNAVAILABLE"
        )
        if state.get("contract") != CONTRACT:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_INVALID",
                "proof capability authority contract is invalid",
            )
        return state

    def mint(
        self,
        *,
        mode: str,
        exact_ref: str,
        pinned_dsh_version: str,
        disposable_baseline: str,
        proof_run_id: str,
        roots: Mapping[str, Mapping[str, Any]],
        plugin_contract_generation: str,
        ttl_seconds: int,
        live_registry_roots: Sequence[str],
    ) -> CapabilityGrant:
        """Mint generation one only from a clean disposable proof seat."""
        if (
            mode not in MODES
            or EXACT_REF.fullmatch(exact_ref) is None
            or not pinned_dsh_version
            or not disposable_baseline
            or not proof_run_id
            or not plugin_contract_generation
            or ttl_seconds <= 0
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_INVALID",
                "proof capability mint inputs are invalid",
            )
        normalized = self._roots(roots)
        if live_registry_roots:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_SEAT_NOT_CLEAN",
                "initial proof capability requires an empty live session set",
            )
        _owner_directory(self.root)
        _owner_directory(self.artifacts)
        with self._locked():
            if self.state_path.exists():
                current = self._read_state()
                if current.get("state") == "active":
                    raise DeepSeekIdentityError(
                        "HARNESS_PROOF_CAPABILITY_BUSY",
                        "another proof capability is active",
                    )
            token = secrets.token_urlsafe(48)
            artifact = self._new_artifact(token=token, generation=1)
            now = self.clock().astimezone(timezone.utc)
            state = {
                "contract": CONTRACT,
                "state": "active",
                "mode": mode,
                "generation": 1,
                "exact_ref": exact_ref,
                "pinned_dsh_version": pinned_dsh_version,
                "disposable_baseline": disposable_baseline,
                "proof_run_id": proof_run_id,
                "roots": normalized,
                "plugin_contract_generation": plugin_contract_generation,
                "token_sha256": _digest(token),
                "artifact": str(artifact),
                "created_at": _stamp(now),
                "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)),
                "ratchets": [],
                "revoked_at": None,
            }
            _atomic_owner_write(self.state_path, _canonical(state) + b"\n")
            return CapabilityGrant(
                mode, 1, artifact, proof_run_id, exact_ref,
                plugin_contract_generation,
            )

    def _presented(self, artifact: Path) -> tuple[dict[str, Any], str, int]:
        try:
            resolved = artifact.resolve(strict=True)
        except OSError as exc:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_UNAVAILABLE",
                "proof capability presentation is unavailable",
            ) from exc
        if not artifact.is_absolute() or artifact.is_symlink() or artifact != resolved:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_UNSAFE",
                "proof capability must use its exact owner-only artifact path",
            )
        if resolved.parent != self.artifacts:
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_UNSAFE",
                "proof capability artifact belongs to another authority",
            )
        raw = _owner_json(
            resolved, unavailable="HARNESS_PROOF_CAPABILITY_UNAVAILABLE"
        )
        token = raw.get("token")
        generation = raw.get("generation")
        if (
            raw.get("contract") != CONTRACT
            or not isinstance(token, str)
            or not token
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_CAPABILITY_INVALID",
                "proof capability presentation is malformed",
            )
        return raw, token, generation

    def describe(self, *, artifact: Path) -> dict[str, Any]:
        """Return only the nonsecret runtime contract for a current presentation."""
        _raw, token, generation = self._presented(artifact)
        with self._locked():
            state = self._read_state()
            if (
                state.get("state") != "active"
                or state.get("generation") != generation
                or state.get("token_sha256") != _digest(token)
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "proof capability generation is stale",
                )
            try:
                expired = self.clock().astimezone(timezone.utc) >= _parse_stamp(
                    state.get("expires_at")
                )
            except (TypeError, ValueError) as exc:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_INVALID",
                    "proof capability expiry is malformed",
                ) from exc
            if expired:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_EXPIRED",
                    "proof capability has expired",
                )
            return {
                "mode": state["mode"],
                "exact_ref": state["exact_ref"],
                "pinned_dsh_version": state["pinned_dsh_version"],
                "proof_run_id": state["proof_run_id"],
                "generation": generation,
            }

    def recovery_contract(self, *, artifact: Path) -> dict[str, Any]:
        """Return the immutable roots needed for server-side restart proof."""
        described = self.describe(artifact=artifact)
        with self._locked():
            state = self._read_state()
            if state.get("generation") != described["generation"]:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "proof capability changed during restart inspection",
                )
            return {
                **described,
                "roots": json.loads(json.dumps(state["roots"])),
                "plugin_contract_generation": state[
                    "plugin_contract_generation"
                ],
            }

    def refusal_contract(self, *, artifact: Path) -> dict[str, Any]:
        """Return current roots for fail-closed teardown of any owned generation."""
        _raw, _token, presented_generation = self._presented(artifact)
        with self._locked():
            state = self._read_state()
            return {
                "state": state["state"],
                "generation": state["generation"],
                "presented_generation": presented_generation,
                "proof_run_id": state["proof_run_id"],
                "roots": json.loads(json.dumps(state["roots"])),
            }

    def admit(
        self,
        *,
        artifact: Path,
        mode: str,
        exact_ref: str,
        pinned_dsh_version: str,
        root_session_id: str,
        conversation_id: str,
        lifecycle_epoch: int,
        verified_lineage: Sequence[str],
        plugin_contract_generation: str,
    ) -> dict[str, Any]:
        """Validate one admission without mutating durable authority."""
        _raw, token, generation = self._presented(artifact)
        with self._locked():
            state = self._read_state()
            if state.get("state") != "active":
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_REVOKED",
                    "proof capability is not active",
                )
            try:
                expired = self.clock().astimezone(timezone.utc) >= _parse_stamp(
                    state.get("expires_at")
                )
            except (TypeError, ValueError) as exc:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_INVALID",
                    "proof capability expiry is malformed",
                ) from exc
            if expired:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_EXPIRED",
                    "proof capability has expired",
                )
            if generation != state.get("generation") or _digest(token) != state.get(
                "token_sha256"
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "proof capability generation is stale",
                )
            if (
                state.get("mode") != mode
                or state.get("exact_ref") != exact_ref
                or state.get("pinned_dsh_version") != pinned_dsh_version
                or state.get("plugin_contract_generation")
                != plugin_contract_generation
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_MISMATCH",
                    "proof capability does not match the exact runtime",
                )
            expected = state.get("roots", {}).get(root_session_id)
            actual = {
                "conversation_id": conversation_id,
                "lifecycle_epoch": lifecycle_epoch,
                "verified_lineage": sorted(verified_lineage),
            }
            if expected != actual:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_ROOT_REFUSED",
                    "proof capability does not enumerate this exact root lifecycle",
                )
            return {
                "mode": mode,
                "generation": generation,
                "proof_run_id": state["proof_run_id"],
                "root_session_id": root_session_id,
                "plugin_contract_generation": plugin_contract_generation,
            }

    def ratchet_after_host_restart(
        self,
        *,
        artifact: Path,
        old_plugin_contract_generation: str,
        new_plugin_contract_generation: str,
        roots: Mapping[str, Mapping[str, Any]],
        ttl_seconds: int,
    ) -> CapabilityGrant:
        """Advance one generation while preserving the exact proof run roots."""
        _raw, token, generation = self._presented(artifact)
        normalized = self._roots(roots)
        if (
            not new_plugin_contract_generation
            or new_plugin_contract_generation == old_plugin_contract_generation
            or ttl_seconds <= 0
        ):
            raise DeepSeekIdentityError(
                "HARNESS_PROOF_RATCHET_INVALID",
                "proof capability restart transition is invalid",
            )
        with self._locked():
            state = self._read_state()
            if (
                state.get("state") != "active"
                or state.get("generation") != generation
                or state.get("token_sha256") != _digest(token)
                or state.get("plugin_contract_generation")
                != old_plugin_contract_generation
                or state.get("roots") != normalized
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "proof capability restart transition is stale or changes roots",
                )
            try:
                expired = self.clock().astimezone(timezone.utc) >= _parse_stamp(
                    state.get("expires_at")
                )
            except (TypeError, ValueError) as exc:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_INVALID",
                    "proof capability expiry is malformed",
                ) from exc
            if expired:
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_EXPIRED",
                    "expired proof authority cannot ratchet after restart",
                )
            next_generation = generation + 1
            next_token = secrets.token_urlsafe(48)
            next_artifact = self._new_artifact(
                token=next_token, generation=next_generation
            )
            now = self.clock().astimezone(timezone.utc)
            state["ratchets"].append(
                {
                    "from_generation": generation,
                    "to_generation": next_generation,
                    "old_plugin_contract_generation": old_plugin_contract_generation,
                    "new_plugin_contract_generation": new_plugin_contract_generation,
                    "at": _stamp(now),
                }
            )
            state.update(
                {
                    "generation": next_generation,
                    "plugin_contract_generation": new_plugin_contract_generation,
                    "token_sha256": _digest(next_token),
                    "artifact": str(next_artifact),
                    "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)),
                }
            )
            _atomic_owner_write(self.state_path, _canonical(state) + b"\n")
            return CapabilityGrant(
                state["mode"], next_generation, next_artifact,
                state["proof_run_id"], state["exact_ref"],
                new_plugin_contract_generation,
            )

    def revoke(self, *, artifact: Path) -> dict[str, Any]:
        _raw, token, generation = self._presented(artifact)
        with self._locked():
            state = self._read_state()
            if (
                state.get("state") != "active"
                or state.get("generation") != generation
                or state.get("token_sha256") != _digest(token)
            ):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_STALE",
                    "only the current proof capability may revoke authority",
                )
            state["state"] = "revoked"
            state["revoked_at"] = _stamp(self.clock())
            state["token_sha256"] = None
            _atomic_owner_write(self.state_path, _canonical(state) + b"\n")
            return {
                "state": "revoked",
                "generation": generation,
                "proof_run_id": state["proof_run_id"],
            }

    def revoke_for_refusal(
        self, *, artifact: Path, reason_code: str | None = None
    ) -> dict[str, Any]:
        """Revoke the current proof run after its owned roots were fenced."""
        self._presented(artifact)
        with self._locked():
            state = self._read_state()
            if state.get("state") == "active":
                state["state"] = "revoked"
                state["revoked_at"] = _stamp(self.clock())
                state["token_sha256"] = None
                if reason_code is not None:
                    operation = "refusal"
                    code = reason_code
                    if reason_code.startswith("ratchet:"):
                        operation = "ratchet"
                        code = reason_code.removeprefix("ratchet:")
                    state["failure"] = {
                        "operation": operation,
                        "code": code,
                        "at": state["revoked_at"],
                    }
                _atomic_owner_write(self.state_path, _canonical(state) + b"\n")
            return {
                "state": state["state"],
                "generation": state["generation"],
                "proof_run_id": state["proof_run_id"],
                "failure": state.get("failure"),
            }

    def record_refusal_outcomes(
        self, *, artifact: Path, roots: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """Attach nonsecret teardown evidence after durable revocation."""
        self._presented(artifact)
        with self._locked():
            state = self._read_state()
            failure = state.get("failure")
            if state.get("state") != "revoked" or not isinstance(failure, dict):
                raise DeepSeekIdentityError(
                    "HARNESS_PROOF_CAPABILITY_INVALID",
                    "proof refusal outcomes require revoked failure evidence",
                )
            failure["roots"] = json.loads(json.dumps(roots))
            _atomic_owner_write(self.state_path, _canonical(state) + b"\n")
