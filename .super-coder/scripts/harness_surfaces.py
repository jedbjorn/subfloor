"""Authoritative shipped-harness surface and availability projection."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import harness_versions
from conversation_adapters import ADAPTER_TYPES, AdapterError
from conversation_adapters.base import ADAPTERS, load_manifest

SURFACES = ("terminal", "one_shot", "browser", "sprint")
SUPPORTED_HARNESSES = frozenset(harness_versions.HARNESSES)
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
        if path.parent.is_dir() and path.parent.name in SUPPORTED_HARNESSES
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
        and (
            (isinstance(headless.get("launch"), list) and bool(headless["launch"]))
            or isinstance(headless.get("engine_script"), str)
        )
    )
    browser = declared["browser"] and _browser_contract_proven(harness)
    return {
        "terminal": terminal,
        "one_shot": one_shot,
        "browser": browser,
        "sprint": declared["sprint"] and browser,
    }


def _local_web_launch_proven(manifest: Mapping[str, object]) -> bool:
    """Return whether the adapter has an engine-managed local-Web entry."""
    interactive = manifest.get("interactive")
    return (
        isinstance(interactive, dict)
        and interactive.get("kind") == "local_web"
    )


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


def known_runnable_harnesses() -> list[str]:
    """Return shipped harnesses visible in the model-defaults matrix."""
    found = []
    for harness, path in _manifest_paths().items():
        try:
            manifest = json.loads(path.read_text())
            declared = _declared_surfaces(manifest)
            if (
                any(_proven_surfaces(harness, manifest, declared).values())
                or _local_web_launch_proven(manifest)
            ):
                found.append(harness)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return sorted(found)


def known_interactive_harnesses() -> list[str]:
    """Return shipped harnesses eligible for the flavor launch-default star."""
    found = []
    for harness, path in _manifest_paths().items():
        try:
            manifest = json.loads(path.read_text())
            declared = _declared_surfaces(manifest)
            terminal = _proven_surfaces(harness, manifest, declared)["terminal"]
            if terminal or _local_web_launch_proven(manifest):
                found.append(harness)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return sorted(found)


def project(
    historical_harnesses: Iterable[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    executable: Callable[[str], str | None] = shutil.which,
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
            reason = "HARNESS_UNAVAILABLE"
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
