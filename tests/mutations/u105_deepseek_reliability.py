"""Exact-ref mutation receipt for Sprint 25 WU105 DeepSeek reliability.

Each mutation breaks one production boundary from Doc #174 v5, requires its
focused test to turn red, restores the exact source bytes, and requires green
again. The driver runs only in an owned clean worktree because the negative
proof temporarily edits tracked source.

    python3.14 tests/mutations/u105_deepseek_reliability.py \
        --receipt shared/sprints/sprint-25/wu105-mutation-receipt.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    selector: str
    old: str
    new: str


MUTATIONS = (
    Mutation(
        "retry-attempt-ceiling",
        ROOT / ".super-coder/scripts/conversation_adapters/deepseek.py",
        "tests/test_deepseek_adapter.py::test_browser_readiness_exhaustion_becomes_alias_free_chat_only",
        "READINESS_MAX_ATTEMPTS = 3",
        "READINESS_MAX_ATTEMPTS = 4",
    ),
    Mutation(
        "invariant-mismatch-does-not-retry",
        ROOT / ".super-coder/scripts/conversation_adapters/deepseek.py",
        "tests/test_deepseek_adapter.py::test_managed_identity_mismatch_refuses_immediately_without_retry",
        "        if exc.code not in TRANSIENT_READINESS_CODES:\n            return False",
        "        if False and exc.code not in TRANSIENT_READINESS_CODES:\n            return False",
    ),
    Mutation(
        "sprint-exhaustion-never-becomes-chat-only",
        ROOT / ".super-coder/scripts/conversation_adapters/deepseek.py",
        "tests/test_deepseek_adapter.py::test_sprint_readiness_exhaustion_refuses_only_pre_prompt_turn",
        '            context.env.get("SC_CONVERSATION_SURFACE", "browser") == "browser"',
        '            context.env.get("SC_CONVERSATION_SURFACE", "browser") in {"browser", "sprint"}',
    ),
    Mutation(
        "managed-refusal-does-not-stop-model-host",
        ROOT / ".super-coder/scripts/conversation_adapters/deepseek.py",
        "tests/test_deepseek_adapter.py::test_managed_authority_refusal_preserves_model_and_native_inference",
        (
            "        raise AdapterError(last_error.code, last_error.detail) from last_error\n\n"
            "    @staticmethod\n    def _retry_readiness"
        ),
        (
            "        deepseek_web.stop(env=env)\n"
            "        raise AdapterError(last_error.code, last_error.detail) from last_error\n\n"
            "    @staticmethod\n    def _retry_readiness"
        ),
    ),
    Mutation(
        "one-shot-retry-attempt-ceiling",
        ROOT / ".super-coder/scripts/deepseek_one_shot.py",
        "tests/test_deepseek_adapter.py::test_one_shot_readiness_exhaustion_fails_only_that_invocation",
        "READINESS_MAX_ATTEMPTS = 3",
        "READINESS_MAX_ATTEMPTS = 4",
    ),
    Mutation(
        "binding-create-revalidation",
        ROOT / ".super-coder/scripts/deepseek_web.py",
        "tests/test_deepseek_identity_registry.py::test_runtime_binding_tracks_exact_lifecycle_key_rotation_and_retirement",
        "        if record is None:\n            registry.create_binding(",
        "        if False and record is None:\n            registry.create_binding(",
    ),
    Mutation(
        "binding-rotate-revalidation",
        ROOT / ".super-coder/scripts/deepseek_web.py",
        "tests/test_deepseek_identity_registry.py::test_runtime_binding_tracks_exact_lifecycle_key_rotation_and_retirement",
        "            elif not registry.binding_current(\n",
        "            elif False and not registry.binding_current(\n",
    ),
    Mutation(
        "binding-reopen-revalidation",
        ROOT / ".super-coder/scripts/deepseek_web.py",
        "tests/test_deepseek_identity_registry.py::test_runtime_binding_tracks_exact_lifecycle_key_rotation_and_retirement",
        "            if lifecycle_epoch > previous_epoch:\n",
        "            if False and lifecycle_epoch > previous_epoch:\n",
    ),
    Mutation(
        "protected-root-preprompt-revalidation",
        ROOT / ".super-coder/scripts/conversation_adapters/deepseek.py",
        "tests/test_deepseek_adapter.py::test_candidate_managed_reentry_revalidates_before_prompt_and_closes_unknown",
        (
            "            if self._proof_authority is not None:\n"
            "                self._proof_quiesced = False\n"
            "                self._revalidate_proof_authority(context, session_ref)\n"
        ),
        (
            "            if self._proof_authority is not None:\n"
            "                self._proof_quiesced = False\n"
        ),
    ),
    Mutation(
        "fresh-domain-per-tool-root",
        ROOT / ".super-coder/assets/deepseek/sc-shell-env-plugin.mjs",
        "tests/test_deepseek_identity_registry.py::test_bash_and_pwsh_tool_executions_use_the_fixed_domain_launcher",
        '    const domainId = randomUUID().replaceAll("-", "");',
        f'    const domainId = {"0" * 32!r};',
    ),
    Mutation(
        "ratchet-fences-only-enumerated-roots",
        ROOT / ".super-coder/scripts/deepseek_web.py",
        "tests/test_deepseek_candidate_authority.py::test_failed_ratchet_revokes_and_fences_real_registry_roots",
        '    roots = contract["roots"]\n',
        '    roots = registry.read_snapshot()["records"]\n',
    ),
    Mutation(
        "closing-refuses-new-tool-root",
        ROOT / ".super-coder/scripts/deepseek_execution_domain.py",
        "tests/test_deepseek_identity_registry.py::test_admitted_tool_snapshot_survives_close_while_new_root_refuses",
        '    if not isinstance(record, dict) or record.get("state") != "active":\n',
        '    if not isinstance(record, dict):\n',
    ),
    Mutation(
        "admitted-tool-is-not-revalidated-or-killed-after-close",
        ROOT / ".super-coder/scripts/deepseek_execution_domain.py",
        "tests/test_deepseek_identity_registry.py::test_real_admitted_tool_execution_survives_close_while_new_root_refuses",
        (
            "    _pid, status = os.waitpid(child, 0)\n"
            "    cleanup_domain(domain, parent)\n"
        ),
        (
            "    while True:\n"
            "        _pid, status = os.waitpid(child, os.WNOHANG)\n"
            "        if _pid:\n"
            "            break\n"
            "        try:\n"
            "            _binding_snapshot(\n"
            "                registry_path=args.registry.resolve(strict=True),\n"
            "                fork_id=args.fork_id,\n"
            "                profile_id=args.profile_id,\n"
            "                environment=environment,\n"
            "            )\n"
            "        except ExecutionDomainError:\n"
            "            os.kill(child, signal.SIGKILL)\n"
            "        time.sleep(0.01)\n"
            "    cleanup_domain(domain, parent)\n"
        ),
    ),
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(selector: str) -> tuple[bool, str]:
    _clear_caches()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", selector],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, hashlib.sha256(output.encode()).hexdigest()


def _apply(mutation: Mutation, original: bytes) -> None:
    text = original.decode()
    count = text.count(mutation.old)
    if count != 1:
        raise RuntimeError(
            f"{mutation.name}: mutation anchor matched {count} times, expected 1"
        )
    mutation.path.write_text(text.replace(mutation.old, mutation.new, 1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--only", choices=[item.name for item in MUTATIONS])
    args = parser.parse_args(argv)

    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise SystemExit("tracked worktree must be clean before mutation proof")
    selected = tuple(
        item for item in MUTATIONS if args.only is None or item.name == args.only
    )
    originals = {item.path: item.path.read_bytes() for item in selected}
    results: list[dict[str, object]] = []
    failures: list[str] = []
    try:
        for mutation in selected:
            original = originals[mutation.path]
            try:
                _apply(mutation, original)
                mutated_green, mutated_output = _run(mutation.selector)
            finally:
                mutation.path.write_bytes(original)
            restored_green, restored_output = _run(mutation.selector)
            passed = not mutated_green and restored_green
            results.append(
                {
                    "name": mutation.name,
                    "selector": mutation.selector,
                    "mutated_red": not mutated_green,
                    "restored_green": restored_green,
                    "mutated_output_sha256": mutated_output,
                    "restored_output_sha256": restored_output,
                }
            )
            print(
                f"[{'ok' if passed else 'FAIL'}] {mutation.name}: "
                f"{'red' if not mutated_green else 'STAYED GREEN'} -> "
                f"{'green' if restored_green else 'STILL RED'}"
            )
            if not passed:
                failures.append(mutation.name)
    finally:
        for path, original in originals.items():
            path.write_bytes(original)

    touched = sorted({item.path for item in selected})
    receipt: dict[str, object] = {
        "contract": "sc-dsh-reliability-verification-receipt-v1",
        "sprint_id": 25,
        "work_unit_id": 105,
        "task_id": 652,
        "governing_document_id": 174,
        "historical_sprint_binding_sha256": (
            "84056c2fc7206b83f2d3beb71150545326d5e33f557cb5f2329f55321eab0bdf"
        ),
        "active_reliability_sha256": (
            "06bb2bc31856575b88983d522fd881ad8e9b68c75714a804ff6cdb5bbd98aeb8"
        ),
        "scope_decision_id": 261,
        "reviewer_disposition_message_id": 1385,
        "candidate_ref": _git("rev-parse", "HEAD"),
        "candidate_tree": _git("rev-parse", "HEAD^{tree}"),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "retirement_authority_advanced": False,
        "source_sha256": {
            str(path.relative_to(ROOT)): _sha256(path) for path in touched
        },
        "mutations": results,
        "passed": not failures,
    }
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(
        f"{len(selected) - len(failures)}/{len(selected)} mutations red -> green; "
        f"receipt {receipt['receipt_sha256']}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
