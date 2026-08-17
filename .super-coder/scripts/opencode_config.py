"""Single atomic owner for project-scoped OpenCode configuration."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

LOCK_TIMEOUT_SECONDS = 10.0
ROUTE_AGENT_NAME = re.compile(r"^sc-route-[0-9a-f]{64}$")
VARIANT_MANIFEST = "opencode-1.18.9-v1"


def canonical_variant_options(
    value: Any,
    *,
    provider_family: str | None,
) -> dict[str, Any] | None:
    """Validate one sanitized overlay against the closed compatibility manifest."""
    if not isinstance(value, dict) or not value:
        return None
    if provider_family == "openai-ai-sdk":
        allowed = {"reasoningEffort", "reasoningSummary", "textVerbosity"}
        if set(value) - allowed or not all(
            isinstance(item, str) for item in value.values()
        ):
            return None
        if value.get("reasoningEffort") not in {
            None, "none", "minimal", "low", "medium", "high", "xhigh",
        }:
            return None
        if value.get("reasoningSummary") not in {
            None, "auto", "concise", "detailed",
        }:
            return None
        if value.get("textVerbosity") not in {None, "low", "medium", "high"}:
            return None
        result = dict(value)
    elif provider_family == "anthropic-ai-sdk":
        if set(value) != {"thinking"} or not isinstance(value["thinking"], dict):
            return None
        thinking = value["thinking"]
        budget = thinking.get("budgetTokens")
        if (
            set(thinking) != {"type", "budgetTokens"}
            or thinking.get("type") != "enabled"
            or not isinstance(budget, int)
            or isinstance(budget, bool)
            or budget < 1
            or budget > 131072
        ):
            return None
        result = {"thinking": dict(thinking)}
    else:
        return None
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return result if len(encoded) <= 1024 else None


class OpenCodeConfigError(Exception):
    """Stable pre-dispatch OpenCode project-config refusal."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _canonical_worktree(worktree: Path) -> Path:
    try:
        resolved = Path(worktree).resolve(strict=True)
    except OSError as exc:
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", f"OpenCode worktree is unavailable: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", "OpenCode worktree must be a directory"
        )
    return resolved


def _open_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        code = "HARNESS_CONFIG_INVALID"
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            detail = "OpenCode config lock must not be a symlink"
        else:
            detail = f"cannot open OpenCode config lock: {exc}"
        raise OpenCodeConfigError(code, detail) from exc


def _take_lock(descriptor: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise OpenCodeConfigError(
                    "HARNESS_CONFIG_BUSY",
                    f"OpenCode config remained locked for {timeout:g} seconds",
                ) from exc
            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))


def _read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", f"OpenCode project config is invalid: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", "OpenCode project config must be a JSON object"
        )
    return value


def _atomic_replace(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OpenCodeConfigError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", f"cannot replace OpenCode project config: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def mutate(
    worktree: Path,
    operation: str,
    merger: Callable[[dict[str, Any]], None],
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Serialize one named read/merge/replace operation under the OS lock."""
    if not operation or timeout < 0:
        raise ValueError("operation is required and timeout must be non-negative")
    root = _canonical_worktree(worktree)
    runtime = root / ".sc-state" / "local" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "opencode-config.lock"
    descriptor = _open_lock(lock_path)
    try:
        _take_lock(descriptor, timeout)
        config_path = root / "opencode.json"
        config = _read_config(config_path)
        try:
            merger(config)
        except OpenCodeConfigError:
            raise
        except Exception as exc:
            raise OpenCodeConfigError(
                "HARNESS_CONFIG_INVALID",
                f"OpenCode config operation {operation!r} failed: {exc}",
            ) from exc
        if not isinstance(config, dict):
            raise OpenCodeConfigError(
                "HARNESS_CONFIG_INVALID",
                f"OpenCode config operation {operation!r} produced a non-object",
            )
        _atomic_replace(config_path, config)
        return config
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def merge_json(
    worktree: Path,
    patch: Mapping[str, Any],
    *,
    operation: str,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", "OpenCode config patch must be an object"
        )
    return mutate(
        worktree,
        operation,
        lambda config: _deep_merge(config, patch),
        timeout=timeout,
    )


def emit_template(
    worktree: Path,
    template: Mapping[str, Any],
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Update engine base keys while retaining route agents and shell owner."""
    if not isinstance(template, Mapping):
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", "OpenCode template must be a JSON object"
        )

    def apply(config: dict[str, Any]) -> None:
        preserved = {
            key: config[key] for key in ("agent", "shell") if key in config
        }
        config.update(template)
        config.update(preserved)

    return mutate(worktree, "emit-template", apply, timeout=timeout)


def route_agent_name(binding_digest: str) -> str:
    name = f"sc-route-{binding_digest}"
    if ROUTE_AGENT_NAME.fullmatch(name) is None:
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID",
            "OpenCode route agent requires the complete lowercase binding digest",
        )
    return name


def route_agent_projection(binding: Mapping[str, Any]) -> dict[str, Any]:
    model = binding.get("requested_model")
    metadata = binding.get("adapter_metadata")
    options = metadata.get("variant_options") if isinstance(metadata, dict) else None
    manifest = (
        metadata.get("compatibility_manifest") if isinstance(metadata, dict) else None
    )
    family = metadata.get("provider_family") if isinstance(metadata, dict) else None
    canonical = canonical_variant_options(options, provider_family=family)
    if (
        not isinstance(model, str)
        or not model
        or manifest != VARIANT_MANIFEST
        or canonical is None
        or canonical != options
    ):
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID",
            "OpenCode binding has no exact admitted manifest variant overlay",
        )
    projection = {"mode": "primary", "model": model}
    projection.update(canonical)
    return projection


def ensure_route_agent(
    worktree: Path,
    binding: Mapping[str, Any],
    binding_digest: str,
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> str:
    if binding.get("harness") != "opencode" or binding.get("control_state") != "controlled":
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID", "route agents require a controlled OpenCode binding"
        )
    calculated = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if calculated != binding_digest:
        raise OpenCodeConfigError(
            "HARNESS_CONFIG_INVALID",
            "OpenCode route agent binding digest does not match its content",
        )
    name = route_agent_name(binding_digest)
    expected = route_agent_projection(binding)

    def apply(config: dict[str, Any]) -> None:
        agents = config.setdefault("agent", {})
        if not isinstance(agents, dict):
            raise OpenCodeConfigError(
                "HARNESS_CONFIG_INVALID", "OpenCode agent config must be an object"
            )
        current = agents.get(name)
        if current is not None and current != expected:
            raise OpenCodeConfigError(
                "HARNESS_CONFIG_INVALID",
                f"OpenCode route agent {name} has mismatched content",
            )
        agents[name] = expected

    mutate(worktree, "ensure-route-agent", apply, timeout=timeout)
    return name
