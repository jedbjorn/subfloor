#!/usr/bin/env python3
"""Mutation round trips for Feature #54's live-native Ollama gate.

Each mutation restores one realistic OpenCode/DeepSeek regression: stored
staleness becoming authoritative again, closed native-option admission,
provider-family filtering, global catalogue staleness contaminating the UI,
or legacy generation/version/fingerprint checks blocking first dispatch.
The named focused test must go red, then return green after restoration.

Usage:
    python3 tests/mutations/u6_live_native_ollama_gate.py
    python3 tests/mutations/u6_live_native_ollama_gate.py --list
    python3 tests/mutations/u6_live_native_ollama_gate.py --only <name>
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / ".super-coder"
MODELS = ENGINE / "scripts" / "models.py"
SPRINT = ENGINE / "scripts" / "sprint_domain.py"
OPENCODE = ENGINE / "scripts" / "conversation_adapters" / "opencode.py"
BINDINGS = ENGINE / "api" / "route_bindings.py"
APP = ENGINE / "ui" / "app.js"
TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class Mutation:
    name: str
    property: str
    path: Path
    suite: str
    selector: str
    old: str
    new: str


LIVE_COMPATIBILITY_RETURN = """        require_advertised_live_native(
            binding, proof._advertised_options_by_model
        )
        return"""

MUTATIONS = (
    Mutation(
        "cli-consults-stale-catalogue",
        "CLI live-native resolution never consults stored stale evidence",
        MODELS,
        "tests/test_route_bindings.py",
        "cli_live_resolution_pins_all_decision_267",
        "    if harness in route_bindings.LIVE_NATIVE_HARNESSES:\n"
        "        return resolve_row(",
        "    if False and harness in route_bindings.LIVE_NATIVE_HARNESSES:\n"
        "        return resolve_row(",
    ),
    Mutation(
        "sprint-consults-stale-catalogue",
        "Sprint arm resolves the live harness instead of a 1.18.22 row",
        SPRINT,
        "tests/test_sprint_live_proof.py",
        "glm_max_ignores_stale_11822",
        "    elif harness in route_bindings.LIVE_NATIVE_HARNESSES:\n"
        "        binding, binding_digest = route_bindings.resolve_live_native(",
        "    elif False and harness in route_bindings.LIVE_NATIVE_HARNESSES:\n"
        "        binding, binding_digest = route_bindings.resolve_live_native(",
    ),
    Mutation(
        "closed-native-option-enum",
        "an unfamiliar exact OpenCode option ID remains selectable",
        OPENCODE,
        "tests/test_model_catalog.py",
        "opencode_native_projection_has_no_enum",
        '    return [\n'
        '        _exact_native_identifier(option_id, "native option id")\n'
        '        for option_id in variants\n'
        '    ]',
        '    return [\n'
        '        _exact_native_identifier(option_id, "native option id")\n'
        '        for option_id in variants\n'
        '        if option_id in {"low", "medium", "high", "max"}\n'
        '    ]',
    ),
    Mutation(
        "provider-payload-admission",
        "an unfamiliar provider payload shape cannot hide its exact variant ID",
        OPENCODE,
        "tests/test_model_catalog.py",
        "opencode_native_projection_has_no_enum",
        '    return [\n'
        '        _exact_native_identifier(option_id, "native option id")\n'
        '        for option_id in variants\n'
        '    ]',
        '    return [\n'
        '        _exact_native_identifier(option_id, "native option id")\n'
        '        for option_id in variants\n'
        '        if isinstance(variants[option_id], Mapping)\n'
        '    ]',
    ),
    Mutation(
        "global-stale-disables-native-thinking",
        "a successful live block remains usable beside a stale advisory cache",
        APP,
        "tests/test_shells_ui_contract.py",
        "live_native_model_picker_ignores_global_stale_catalogue",
        '  return block?.authority === "harness-live" && !block.stale && !block.error',
        '  return block?.authority === "harness-live" && !catalog.stale\n'
        "    && !block.stale && !block.error",
    ),
    Mutation(
        "global-stale-hides-native-models",
        "the model picker renders the current live block despite global stale",
        APP,
        "tests/test_shells_ui_contract.py",
        "live_native_model_picker_ignores_global_stale_catalogue",
        "    const models = cat.stale && !liveBlock ? [] : (data.models || []).filter(",
        "    const models = cat.stale ? [] : (data.models || []).filter(",
    ),
    Mutation(
        "legacy-generation-readmitted",
        "legacy live-native generation is history, not first-turn admission",
        BINDINGS,
        "tests/test_route_bindings.py",
        "legacy_v2_live_native_uses_current_exact_ids_without_rewrite",
        LIVE_COMPATIBILITY_RETURN,
        LIVE_COMPATIBILITY_RETURN.replace(
            "        return",
            '        if binding.get("catalogue_generation") is not None:\n'
            '            raise RouteResolutionError("thinking_evidence_stale", '
            '"generation changed", {})\n'
            "        return",
        ),
    ),
    Mutation(
        "legacy-fingerprint-readmitted",
        "legacy live-native fingerprint drift cannot block an exact current ID",
        BINDINGS,
        "tests/test_route_bindings.py",
        "legacy_v2_live_native_uses_current_exact_ids_without_rewrite",
        LIVE_COMPATIBILITY_RETURN,
        LIVE_COMPATIBILITY_RETURN.replace(
            "        return",
            "        if source_fingerprint != proof._source_fingerprint:\n"
            '            raise RouteResolutionError("thinking_evidence_stale", '
            '"fingerprint changed", {})\n'
            "        return",
        ),
    ),
    Mutation(
        "legacy-version-readmitted",
        "legacy live-native captured version cannot block current exact IDs",
        BINDINGS,
        "tests/test_route_bindings.py",
        "legacy_v2_live_native_uses_current_exact_ids_without_rewrite",
        LIVE_COMPATIBILITY_RETURN,
        LIVE_COMPATIBILITY_RETURN.replace(
            "        return",
            "        if harness_version != proof._runtime_status.observed_version:\n"
            '            raise RouteResolutionError("thinking_evidence_stale", '
            '"version changed", {})\n'
            "        return",
        ),
    ),
)


def clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run(mutation: Mutation) -> tuple[bool, bool]:
    clear_caches()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                mutation.suite,
                "-k",
                mutation.selector,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, True
    return result.returncode == 0, False


def apply(mutation: Mutation) -> str:
    original = mutation.path.read_text()
    count = original.count(mutation.old)
    if count != 1:
        raise RuntimeError(
            f"{mutation.name}: anchor matched {count} times in "
            f"{mutation.path}, expected exactly 1"
        )
    mutation.path.write_text(original.replace(mutation.old, mutation.new, 1))
    return original


def interrupted(signum, _frame) -> None:
    raise SystemExit(f"interrupted by signal {signum}")


def main() -> int:
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()
    selected = [
        mutation for mutation in MUTATIONS
        if args.only is None or mutation.name == args.only
    ]
    if args.list:
        for mutation in selected:
            print(f"{mutation.name:40s} {mutation.property}")
        return 0
    if not selected:
        raise SystemExit(f"no mutation named {args.only!r}")

    failures: list[str] = []
    for mutation in selected:
        print(f"{mutation.name:40s} ", end="", flush=True)
        try:
            original = apply(mutation)
        except RuntimeError as exc:
            print("STALE")
            failures.append(str(exc))
            continue
        try:
            mutated_green, mutated_timeout = run(mutation)
        finally:
            mutation.path.write_text(original)
        restored_green, restored_timeout = run(mutation)
        red = not mutated_green and not mutated_timeout
        ok = red and restored_green and not restored_timeout
        print(
            ("red" if red else "HUNG" if mutated_timeout else "GREEN")
            + " -> revert -> "
            + ("green" if restored_green else "HUNG" if restored_timeout else "RED")
        )
        if not ok:
            failures.append(
                f"{mutation.name}: mutated={'hung' if mutated_timeout else 'green'}; "
                f"restored={'hung' if restored_timeout else 'green' if restored_green else 'red'}"
            )

    if failures:
        print(f"\n{len(failures)} of {len(selected)} mutations failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\n{len(selected)}/{len(selected)} mutations red -> revert -> green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
