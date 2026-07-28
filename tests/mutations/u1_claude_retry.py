"""Mutation round trips for F31 S90-U1 Claude retry and terminal guards.

Each mutation removes one operand from the production decision, asserts the
focused detector goes red, restores the committed baseline in ``finally``, and
asserts the detector is green again.

    python3 tests/mutations/u1_claude_retry.py [-v]

Not named ``test_*.py`` because pytest must never collect a source-mutating
driver.  Child interpreters do not read or write bytecode, and caches are
cleared before every run so a same-size replacement cannot survive its revert.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / ".super-coder" / "scripts" / "interface_claude_driver.py"
SUITE = "tests/test_interface_claude_driver.py"

# label, selector, exact old text, one-operand mutant
MUTATIONS = [
    (
        "M1 prompt-present guard removed",
        "retry_posture_operand_matrix",
        "        and not prompt_present\n",
        "        and True\n",
    ),
    (
        "M2 anchor-present guard removed",
        "retry_posture_operand_matrix",
        "        and anchor_present\n",
        "        and True\n",
    ),
    (
        "M3 rewritten/ambiguous-anchor guard removed",
        "retry_posture_operand_matrix",
        "        and anchor_unambiguous\n",
        "        and True\n",
    ),
    (
        "M4 failure-posture guard removed",
        "retry_posture_operand_matrix",
        "        turn_failed\n",
        "        True\n",
    ),
    (
        "M5 clean-process-exit guard removed",
        "process_exit_and_provider_terminal_are_both_required",
        "            process.returncode == 0\n",
        "            True\n",
    ),
    (
        "M6 authoritative-output-terminal guard removed",
        "process_exit_and_provider_terminal_are_both_required",
        '            and parsed.terminal == "completed"\n',
        "            and True\n",
    ),
]


def clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run(selector: str) -> subprocess.CompletedProcess[str]:
    clear_caches()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-k", selector],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def red_set(result: subprocess.CompletedProcess[str]) -> str:
    lines = [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if "SUBFAILED" in line or "FAILED " in line
    ]
    return "; ".join(lines) or f"exit={result.returncode}"


def main() -> int:
    verbose = "-v" in sys.argv
    failures = []
    baseline = run("retry_posture_operand_matrix or "
                   "process_exit_and_provider_terminal_are_both_required")
    if baseline.returncode != 0:
        print("[FAIL] baseline is red; refusing to mutate")
        if verbose:
            print(baseline.stdout)
            print(baseline.stderr)
        return 1

    for label, selector, old, new in MUTATIONS:
        original = SOURCE.read_text()
        count = original.count(old)
        if count != 1:
            failures.append(f"{label}: source anchor found {count}x")
            print(f"[FAIL] {label}\n       source anchor found {count}x")
            continue
        try:
            SOURCE.write_text(original.replace(old, new, 1))
            mutated = run(selector)
        finally:
            SOURCE.write_text(original)
        restored = run(selector)
        red = mutated.returncode != 0
        green = restored.returncode == 0
        ok = red and green
        if not ok:
            failures.append(
                f"{label}: mutated={'red' if red else 'GREEN'}; "
                f"restored={'green' if green else 'RED'}"
            )
        print(
            f"[{'ok' if ok else 'FAIL'}] {label}\n"
            f"       red set: {red_set(mutated)}\n"
            f"       restored: {'green' if green else red_set(restored)}"
        )
        if verbose and not red:
            print(mutated.stdout)
            print(mutated.stderr)

    print(
        f"\n{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} "
        "round trips behaved (red under mutation, green on revert)"
    )
    for failure in failures:
        print("  FAIL " + failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
