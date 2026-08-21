"""Authoritative shipped-harness surface and availability projection."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from conversation_adapters import ADAPTER_TYPES, AdapterError
from conversation_adapters.base import ADAPTERS, load_manifest

SURFACES = ("terminal", "one_shot", "browser", "sprint")
_BROWSER_PROOF = (
    "exact_session_resume",
    "structured_streaming",
    "interruption",
    "session_inspection",
)


def _disabled_harnesses(env: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        value.strip().lower()
        for value in env.get("SC_DISABLED_HARNESSES", "").split(",")
        if value.strip()
    )


def _manifest_paths() -> dict[str, Path]:
    if not ADAPTERS.is_dir():
        return {}
    return {
        path.parent.name: path
        for path in ADAPTERS.glob("*/adapter.json")
        if path.parent.is_dir()
    }


def _declared_surfaces(manifest: Mapping[str, object]) -> dict[str, bool]:
    raw = manifest.get("surfaces")
    if not isinstance(raw, dict) or set(raw) != set(SURFACES):
        raise ValueError("surface declaration must contain the four known surfaces")
    if any(not isinstance(raw[name], bool) for name in SURFACES):
        raise ValueError("surface declarations must be booleans")
    return {name: raw[name] for name in SURFACES}


def _browser_contract_proven(harness: str) -> bool:
    if harness not in ADAPTER_TYPES:
        return False
    try:
        manifest = load_manifest(harness)
    except AdapterError:
        return False
    capabilities = manifest["conversation"]["capabilities"]
    return all(capabilities.get(name) is True for name in _BROWSER_PROOF)


def _proven_surfaces(
    harness: str,
    manifest: Mapping[str, object],
    declared: Mapping[str, bool],
) -> dict[str, bool]:
    launch = manifest.get("launch")
    headless = manifest.get("headless")
    terminal = declared["terminal"] and isinstance(launch, list) and bool(launch)
    one_shot = (
        declared["one_shot"]
        and isinstance(headless, dict)
        and isinstance(headless.get("launch"), list)
        and bool(headless["launch"])
    )
    browser = declared["browser"] and _browser_contract_proven(harness)
    return {
        "terminal": terminal,
        "one_shot": one_shot,
        "browser": browser,
        "sprint": declared["sprint"] and browser,
    }


def _runtime_command(harness: str, manifest: Mapping[str, object]) -> str:
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict) and isinstance(runtime.get("command"), str):
        return runtime["command"]
    launch = manifest.get("launch")
    if isinstance(launch, list) and launch and isinstance(launch[0], str):
        return launch[0]
    return harness


def _compatibility_state(manifest: Mapping[str, object]) -> str:
    compatibility = manifest.get("conversation") or manifest.get(
        "runtime_compatibility"
    )
    if not isinstance(compatibility, dict):
        return "unproven"
    required = (
        "minimum_cli_version",
        "maximum_cli_version_exclusive",
        "verified_cli_version",
    )
    if all(isinstance(compatibility.get(name), str) for name in required):
        return "declared"
    return "unproven"


def known_terminal_harnesses() -> list[str]:
    """Return the compatibility roster historically exposed as strings."""
    found = []
    for harness, path in _manifest_paths().items():
        try:
            manifest = json.loads(path.read_text())
            declared = _declared_surfaces(manifest)
            if _proven_surfaces(harness, manifest, declared)["terminal"]:
                found.append(harness)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return sorted(found)


def project(
    historical_harnesses: Iterable[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    executable: Callable[[str], str | None] = shutil.which,
    deepseek_probe: Callable[..., object] | None = None,
) -> dict[str, dict[str, object]]:
    """Project shipped and historical harnesses without rejecting old names."""
    env = os.environ if env is None else env
    disabled = _disabled_harnesses(env)
    manifests = _manifest_paths()
    names = set(manifests)
    names.update(
        str(name).strip().lower() for name in historical_harnesses if str(name).strip()
    )
    projected: dict[str, dict[str, object]] = {}
    for harness in sorted(names):
        path = manifests.get(harness)
        if path is None:
            projected[harness] = {
                "shipped": False,
                "installed": False,
                "enabled": False,
                "healthy": False,
                "compatibility": "unknown",
                "surfaces": {name: False for name in SURFACES},
                "unavailable_reason": "HARNESS_NOT_SHIPPED",
            }
            continue

        try:
            manifest = json.loads(path.read_text())
            if manifest.get("harness") != harness:
                raise ValueError("manifest harness does not match its directory")
            declared = _declared_surfaces(manifest)
            surfaces = _proven_surfaces(harness, manifest, declared)
        except (OSError, json.JSONDecodeError, ValueError):
            projected[harness] = {
                "shipped": True,
                "installed": False,
                "enabled": False,
                "healthy": False,
                "compatibility": "invalid",
                "surfaces": {name: False for name in SURFACES},
                "unavailable_reason": "HARNESS_MANIFEST_INVALID",
            }
            continue

        runtime_reason = None
        if harness == "deepseek":
            if deepseek_probe is None:
                import deepseek_runtime

                deepseek_probe = deepseek_runtime.runtime_status
            probe_env = dict(env)
            probe_env.pop("SC_DISABLED_HARNESSES", None)
            try:
                runtime_status = deepseek_probe(env=probe_env)
                carrier = getattr(runtime_status, "carrier_python", None)
                installed = bool(getattr(runtime_status, "available", False)) or (
                    isinstance(carrier, str) and Path(carrier).is_file()
                )
                runtime_reason = getattr(runtime_status, "error", None)
            except Exception as exc:
                installed = False
                runtime_reason = getattr(exc, "code", "HARNESS_MANIFEST_INVALID")
        else:
            installed = executable(_runtime_command(harness, manifest)) is not None
        enabled = harness not in disabled
        compatibility = _compatibility_state(manifest)
        healthy = (
            installed
            and enabled
            and compatibility == "declared"
            and any(surfaces.values())
        )
        if not enabled:
            reason = "HARNESS_DISABLED"
        elif not installed:
            reason = runtime_reason or "HARNESS_UNAVAILABLE"
        elif compatibility != "declared":
            reason = "HARNESS_COMPATIBILITY_UNPROVEN"
        elif not any(surfaces.values()):
            reason = "HARNESS_SURFACE_UNPROVEN"
        else:
            reason = None
        projected[harness] = {
            "shipped": True,
            "installed": installed,
            "enabled": enabled,
            "healthy": healthy,
            "compatibility": compatibility,
            "surfaces": surfaces,
            "unavailable_reason": reason,
        }
    return projected
