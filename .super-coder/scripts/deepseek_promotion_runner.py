#!/usr/bin/env python3
"""Dedicated control plane for disposable DeepSeek promotion proof runs.

The runner, rather than Browser, Sprint, one-shot, or ambient configuration,
owns capability mint, context installation, restart adoption, and teardown.
Conversation roots are derived from the canonical engine database and the
one-shot root is minted by the server. The later clean-room canary drives this
API on the disposable Arch seat and owns its receipt.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import deepseek_web

RUNNER_CONTRACT = "sc-dsh-promotion-runner-v1"
CONTEXT_CONTRACT = "sc-dsh-proof-context-v1"
ACCEPTANCE_CONTRACT = "sc-dsh-candidate-acceptance-v1"
ENGINE = Path(__file__).resolve().parents[1]


class PromotionRunnerError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class PromotionRun:
    env: Mapping[str, str]
    runner_state: Path
    proof_run_id: str
    mode: str

    def _state(self) -> dict[str, Any]:
        return _owner_json(self.runner_state, "HARNESS_PROOF_RUNNER_UNAVAILABLE")

    def environment_for(
        self, *, root_session_id: str, base_env: Mapping[str, str]
    ) -> dict[str, str]:
        state = self._state()
        path = state.get("contexts", {}).get(root_session_id)
        if state.get("state") != "active" or not isinstance(path, str):
            raise PromotionRunnerError(
                "HARNESS_PROOF_ROOT_REFUSED",
                "the dedicated runner does not own this active proof root",
            )
        result = dict(base_env)
        result.pop("SC_DSH_PROOF_CAPABILITY_FILE", None)
        result["SC_DSH_PROOF_CONTEXT_FILE"] = path
        return result

    def ratchet_after_host_restart(self, *, ttl_seconds: int) -> dict[str, Any]:
        with _runner_lock(self.runner_state.parent):
            state = self._state()
            artifact = Path(str(state.get("artifact", "")))
            try:
                grant = deepseek_web.ratchet_candidate_after_host_restart(
                    env=self.env, artifact=artifact, ttl_seconds=ttl_seconds
                )
            except deepseek_web.DeepSeekWebError:
                _finish_runner_state(self.runner_state, state="failed")
                raise
            state = self._state()
            state["artifact"] = grant["artifact"]
            state["generation"] = grant["generation"]
            _write_contexts(state)
            _atomic_owner_json(self.runner_state, state)
            return grant

    def revoke(self) -> dict[str, Any]:
        with _runner_lock(self.runner_state.parent):
            state = self._state()
            try:
                receipt = deepseek_web.revoke_candidate_capability(
                    env=self.env, artifact=Path(str(state.get("artifact", "")))
                )
            except BaseException:
                _finish_runner_state(self.runner_state, state="failed")
                raise
            else:
                _finish_runner_state(self.runner_state, state="revoked")
            return receipt


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _ensure_owner_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise PromotionRunnerError(
            "HARNESS_PROOF_RUNNER_UNSAFE", "promotion runner directory is unsafe"
        )
    os.chmod(path, 0o700)


@contextmanager
def _runner_lock(authority_root: Path):
    _ensure_owner_dir(authority_root)
    descriptor = os.open(authority_root / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
        fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_owner_json(path: Path, value: object) -> None:
    _ensure_owner_dir(path.parent)
    descriptor, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _owner_json(path: Path, code: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or path.is_symlink()
        ):
            raise OSError("unsafe owner-only artifact")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_uid != metadata.st_uid
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise OSError("owner-only artifact changed before open")
            value = json.load(handle)
            after = os.fstat(handle.fileno())
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                raise OSError("owner-only artifact changed during read")
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise PromotionRunnerError(code, "owner-only runner artifact is unavailable") from exc
    if not isinstance(value, dict):
        raise PromotionRunnerError(code, "owner-only runner artifact is malformed")
    return value


def _acceptance(
    *, mode: str, exact_ref: str, authority_root: Path
) -> dict[str, Any] | None:
    if mode == "candidate":
        return None
    path = authority_root / "candidate-acceptance.json"
    receipt = _owner_json(path, "HARNESS_PROOF_ACCEPTANCE_REQUIRED")
    if (
        receipt.get("contract") != ACCEPTANCE_CONTRACT
        or receipt.get("state") != "candidate-accepted"
        or receipt.get("reviewed") is not True
        or receipt.get("fnb_accepted") is not True
        or receipt.get("candidate_ref") == exact_ref
        or receipt.get("retirement_ref") != exact_ref
    ):
        raise PromotionRunnerError(
            "HARNESS_PROOF_ACCEPTANCE_REQUIRED",
            "promoted proof requires accepted candidate evidence and the exact retirement ref",
        )
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _context_path(authority_root: Path, root_session_id: str) -> Path:
    return authority_root / "contexts" / f"{hashlib.sha256(root_session_id.encode()).hexdigest()}.json"


def _write_contexts(state: dict[str, Any]) -> None:
    authority_root = Path(state["authority_root"])
    contexts: dict[str, str] = {}
    for root_session_id, root in state["roots"].items():
        path = _context_path(authority_root, root_session_id)
        _atomic_owner_json(
            path,
            {
                "contract": CONTEXT_CONTRACT,
                "runner_id": state["runner_id"],
                "proof_run_id": state["proof_run_id"],
                "mode": state["mode"],
                "surface": root["surface"],
                "root_session_id": root_session_id,
                "conversation_id": root["conversation_id"],
                "lifecycle_epoch": root["lifecycle_epoch"],
                "generation": state["generation"],
                "artifact": state["artifact"],
            },
        )
        contexts[root_session_id] = str(path)
    state["contexts"] = contexts


def _finish_runner_state(path: Path, *, state: str) -> None:
    try:
        current = _owner_json(path, "HARNESS_PROOF_RUNNER_UNAVAILABLE")
    except PromotionRunnerError:
        return
    current["state"] = state
    current["runner_token_sha256"] = None
    _atomic_owner_json(path, current)
    for raw in current.get("contexts", {}).values():
        try:
            Path(raw).unlink()
        except (FileNotFoundError, OSError, TypeError):
            pass


def start(
    *,
    env: Mapping[str, str],
    mode: str,
    disposable_baseline: str,
    proof_run_id: str,
    conversation_ids: Sequence[str],
    include_one_shot: bool,
    ttl_seconds: int,
) -> PromotionRun:
    """Authenticate one dedicated run, derive its roots, and install contexts."""
    if mode not in {"candidate", "promoted"} or not include_one_shot:
        raise PromotionRunnerError(
            "HARNESS_PROOF_RUNNER_INVALID",
            "promotion runner requires candidate/promoted mode and one-shot coverage",
        )
    registry = deepseek_web._identity_registry(env)
    authority_root = registry.layout.root / "proof-authority"
    exact_ref = deepseek_web._exact_engine_ref()
    roots = deepseek_web._canonical_promotion_roots(
        conversation_ids=tuple(conversation_ids)
    )
    acceptance = _acceptance(
        mode=mode, exact_ref=exact_ref, authority_root=authority_root
    )
    runner_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(48)
    authority_roots = {
        root_session_id: {
            key: value for key, value in root.items() if key != "surface"
        }
        for root_session_id, root in roots.items()
    }
    state_path = authority_root / "runner.json"
    state = {
        "contract": RUNNER_CONTRACT,
        "state": "authorizing",
        "runner_id": runner_id,
        "runner_pid": os.getpid(),
        "runner_start_ticks": deepseek_web.process_start_ticks(os.getpid()),
        "runner_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "authority_root": str(authority_root.resolve()),
        "mode": mode,
        "proof_run_id": proof_run_id,
        "exact_ref": exact_ref,
        "roots": roots,
        "roots_sha256": _digest(authority_roots),
        "acceptance": acceptance,
        "artifact": None,
        "generation": None,
        "contexts": {},
    }
    with _runner_lock(authority_root):
        if state_path.exists():
            existing = _owner_json(
                state_path, "HARNESS_PROOF_RUNNER_UNAVAILABLE"
            )
            if existing.get("state") in {"active", "authorizing"}:
                raise PromotionRunnerError(
                    "HARNESS_PROOF_RUNNER_BUSY",
                    "another dedicated promotion run is active",
                )
        _atomic_owner_json(state_path, state)
        try:
            grant = deepseek_web.mint_candidate_capability(
                env=env,
                mode=mode,
                disposable_baseline=disposable_baseline,
                proof_run_id=proof_run_id,
                conversation_ids=tuple(conversation_ids),
                ttl_seconds=ttl_seconds,
                runner_authorization={"state_path": str(state_path), "token": token},
            )
            state["state"] = "active"
            state["artifact"] = grant["artifact"]
            state["generation"] = grant["generation"]
            state["roots"] = grant["roots"]
            _write_contexts(state)
            _atomic_owner_json(state_path, state)
        except BaseException:
            _finish_runner_state(state_path, state="failed")
            raise
    return PromotionRun(dict(env), state_path, proof_run_id, mode)


def inject_conversation_context(
    *, env: Mapping[str, str], conversation_id: str, lifecycle_epoch: int
) -> dict[str, str]:
    """Install proof context only when the active runner enumerated this root."""
    result = dict(env)
    result.pop("SC_DSH_PROOF_CAPABILITY_FILE", None)
    state_path = (
        deepseek_web._identity_registry(env).layout.root
        / "proof-authority"
        / "runner.json"
    )
    try:
        state = _owner_json(state_path, "HARNESS_PROOF_RUNNER_UNAVAILABLE")
    except PromotionRunnerError:
        return result
    if state.get("state") != "active":
        return result
    for root_session_id, root in state.get("roots", {}).items():
        if (
            root.get("conversation_id") == conversation_id
            and root.get("lifecycle_epoch") == lifecycle_epoch
            and root.get("surface") in {"browser", "sprint"}
        ):
            context = state.get("contexts", {}).get(root_session_id)
            if isinstance(context, str):
                result["SC_DSH_PROOF_CONTEXT_FILE"] = context
            return result
    return result


def _active_run(env: Mapping[str, str]) -> PromotionRun:
    state_path = (
        deepseek_web._identity_registry(env).layout.root
        / "proof-authority"
        / "runner.json"
    )
    state = _owner_json(state_path, "HARNESS_PROOF_RUNNER_UNAVAILABLE")
    if (
        state.get("contract") != RUNNER_CONTRACT
        or state.get("state") != "active"
        or not isinstance(state.get("proof_run_id"), str)
        or state.get("mode") not in {"candidate", "promoted"}
    ):
        raise PromotionRunnerError(
            "HARNESS_PROOF_RUNNER_UNAVAILABLE",
            "no active dedicated promotion run is available",
        )
    return PromotionRun(
        dict(env), state_path, state["proof_run_id"], state["mode"]
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Dedicated disposable DeepSeek promotion runner control plane"
    )
    commands = result.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="mint and install a new proof run")
    begin.add_argument("--mode", required=True, choices=("candidate", "promoted"))
    begin.add_argument("--disposable-baseline", required=True)
    begin.add_argument("--proof-run-id", required=True)
    begin.add_argument("--conversation", action="append", required=True)
    begin.add_argument("--ttl-seconds", type=int, default=3600)
    ratchet = commands.add_parser(
        "ratchet", help="adopt a recovered Host contract for the same roots"
    )
    ratchet.add_argument("--ttl-seconds", type=int, default=3600)
    commands.add_parser("revoke", help="revoke authority and close every proof root")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    env = os.environ
    try:
        if args.command == "start":
            run = start(
                env=env,
                mode=args.mode,
                disposable_baseline=args.disposable_baseline,
                proof_run_id=args.proof_run_id,
                conversation_ids=args.conversation,
                include_one_shot=True,
                ttl_seconds=args.ttl_seconds,
            )
            state = run._state()
            result = {
                "state": state["state"],
                "mode": state["mode"],
                "proof_run_id": state["proof_run_id"],
                "exact_ref": state["exact_ref"],
                "generation": state["generation"],
                "contexts": state["contexts"],
            }
        elif args.command == "ratchet":
            result = _active_run(env).ratchet_after_host_restart(
                ttl_seconds=args.ttl_seconds
            )
        else:
            result = _active_run(env).revoke()
    except (PromotionRunnerError, deepseek_web.DeepSeekWebError) as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
