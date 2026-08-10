"""Mutation round trips for Sprint 9 U6 progress-carrier replay.

Each mutation breaks one governing health rule in the real projector, requires
the focused test to turn red, restores the source, and requires green again.
The driver is deliberately outside pytest collection because it edits tracked
source while it runs.

    .venv/bin/python tests/mutations/u6_sprint_stall_replay.py [-v]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECTOR = ROOT / ".super-coder" / "scripts" / "sprint_health.py"
SUITE = "tests/test_sprint_health.py"
PYTHON = ROOT / ".venv" / "bin" / "python"

MUTATIONS = [
    (
        "M1 an exact live run no longer wins over exhausted quota or silence",
        "sanitized_historical_corpus or rootless_progress_and_external",
        '        if live_match is not None and disposition != "ready":',
        '        if False and live_match is not None and disposition != "ready":',
    ),
    (
        "M2 a rootless aggregate reports the newest winning clock",
        "rootless_progress_and_external",
        (
            "        since = min(\n"
            "            (candidate.since for candidate in winning if candidate.since is not None),\n"
            "            default=None,\n"
            "        )"
        ),
        (
            "        since = max(\n"
            "            (candidate.since for candidate in winning if candidate.since is not None),\n"
            "            default=None,\n"
            "        )"
        ),
    ),
    (
        "M3 a fresh exhausted provider is projected as available",
        "sanitized_historical_corpus",
        (
            '            else "exhausted"\n'
            "            if exhausted\n"
            '            else "available"'
        ),
        (
            '            else "available"\n'
            "            if exhausted\n"
            '            else "available"'
        ),
    ),
    (
        "M4 closeout attention starts one tick after the exact boundary",
        "closeout_boundary",
        (
            "        since = max(resets)\n"
            "        overdue = self.now - since >= ATTENTION_AFTER\n"
            "        candidate = Candidate("
        ),
        (
            "        since = max(resets)\n"
            "        overdue = self.now - since > ATTENTION_AFTER\n"
            "        candidate = Candidate("
        ),
    ),
    (
        "M5 fan-in keeps only the first independent upstream root",
        "plural_dependency_roots",
        "                value = sorted(set(value))",
        "                value = sorted(set(value))[:1]",
    ),
    (
        "M6 dependency roots propagate only one hop",
        "chain_fork_multilevel_fanin",
        "                    value.extend(roots(upstream))",
        (
            "                    value.extend("
            "[upstream] if candidates[upstream].condition in _ROOT_CONDITIONS else [])"
        ),
    ),
    (
        "M7 an unowned red PR loses its stable cause",
        "pr_and_reply_carrier_matrix",
        '                    "pr_red_unowned" if overdue else "no_progress_grace",',
        '                    "no_progress_carrier" if overdue else "no_progress_grace",',
    ),
    (
        "M8 an unread required reply is mislabeled as read",
        "pr_and_reply_carrier_matrix",
        (
            '        cause = "reply_unread" if is_unread else '
            '"reply_overdue" if overdue else "reply_waiting"'
        ),
        (
            '        cause = "reply_waiting" if is_unread else '
            '"reply_overdue" if overdue else "reply_waiting"'
        ),
    ),
    (
        "M9 a stale runtime reports the missing-runtime cause",
        "machinery_carrier_matrix",
        (
            '        return "runtime_stale", _max_stamp(floor, due), '
            'Evidence("runtime_heartbeat", 0, beat, 5)'
        ),
        (
            '        return "runtime_missing", _max_stamp(floor, due), '
            'Evidence("runtime_heartbeat", 0, beat, 5)'
        ),
    ),
    (
        "M10 unscoped historical recovery evidence leaks into the first unit",
        "recovery_and_nudge_topology",
        "                    self._record_unreadable(signal, item.get(\"work_unit_id\"))",
        (
            "                    self._record_unreadable("
            "signal, item.get(\"work_unit_id\") or min(self.units))"
        ),
    ),
]


def clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run(selector: str) -> tuple[bool, str]:
    clear_caches()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    executable = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    result = subprocess.run(
        [executable, "-m", "pytest", SUITE, "-q", "-k", selector],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode == 0, result.stdout + result.stderr


def main() -> int:
    verbose = "-v" in sys.argv
    failures: list[str] = []
    for label, selector, old, new in MUTATIONS:
        original = PROJECTOR.read_text()
        count = original.count(old)
        if count != 1:
            failures.append(f"{label}: anchor found {count} times, expected 1")
            print(f"[FAIL] {label}\n       mutation anchor is stale")
            continue
        try:
            PROJECTOR.write_text(original.replace(old, new, 1))
            mutated_green, mutated_output = run(selector)
        finally:
            PROJECTOR.write_text(original)
        restored_green, restored_output = run(selector)
        ok = not mutated_green and restored_green
        print(
            f"[{'ok' if ok else 'FAIL'}] {label}\n"
            f"       {SUITE} -k {selector}: "
            f"{'STAYED GREEN' if mutated_green else 'red'} -> "
            f"{'green' if restored_green else 'STILL RED'}"
        )
        if not ok:
            failures.append(label)
            if verbose:
                print(mutated_output if mutated_green else restored_output)

    print(
        f"\n{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} mutations "
        "red -> revert -> green"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
