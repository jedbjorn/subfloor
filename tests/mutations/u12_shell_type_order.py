"""Mutation round trips for U12 — shell lists grouped by type.

Acceptance evidence, committed rather than run once in a session that dies with
it (the U5/U7/U11 convention). Each entry breaks ONE property in the real
source, asserts the test claiming to pin it actually goes RED, restores the
file, and asserts it goes green again. A property whose mutation stays green is
not pinned, whatever the suite's colour says.

    python3 tests/mutations/u12_shell_type_order.py [-v]

Two harness conditions, both from flag #182 — a same-size mutation reverted
inside one second leaves a VALID `.pyc` behind (invalidation is source
mtime-to-the-second plus size), so the interpreter can keep running mutated
bytecode after the revert and hand back a reading that is simply false:

  * every child runs with PYTHONDONTWRITEBYTECODE=1, and
  * every `__pycache__` under the repo is cleared before each run.

The revert is proven, never assumed: the baseline must come back green after
each round trip, and a failure there is reported as a dirty revert rather than
folded into the mutation's own result.

Two asserted invariants deliberately have NO entry here, and are named rather
than left to look covered: the mobile picker agreeing with the rail, and the
picker's placeholder option staying first. Both hold by CONSTRUCTION — the two
surfaces are filled by one pass over one sorted array, and the placeholder is
appended before the loop begins — so there is no single-property mutation that
can break one without rewriting the loop into something no longer under test.
The assertions stay as guards against exactly that rewrite.

Not named test_*.py on purpose: pytest must not collect it — it edits tracked
source. Every file is restored in a `finally`, so an interrupted run still
leaves the tree clean; `git diff` after a run is the check.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / ".super-coder" / "ui" / "app.js"
SUITE = "tests/test_interface_ui_contract.py"

TABLE = ('const IF_TYPE_ORDER = ["cartographer", "admin", "planner", "dev",\n'
         '  "reviewer", "devops"];')
FALLBACK = "  return i < 0 ? IF_TYPE_ORDER.length : i;"
COMPARATOR = "(a, b) => ifTypeRank(a) - ifTypeRank(b)"

# (label, -k selector, old, new)
MUTATIONS = [
    (
        "M1 the sort goes entirely — the rail is back to the server's "
        "shell_id order, which is the state this unit exists to fix",
        "group_by_type",
        "[...shells].sort(" + COMPARATOR + ")",
        "shells",
    ),
    (
        "M2 the comparator is inverted — the roster reads bottom-up",
        "group_by_type",
        COMPARATOR,
        "(a, b) => ifTypeRank(b) - ifTypeRank(a)",
    ),
    (
        "M3 the rank table is permuted (reviewer above dev) — the order is "
        "the deliverable, so the TABLE has to be pinned, not just the fact "
        "that some sort ran",
        "group_by_type",
        TABLE,
        'const IF_TYPE_ORDER = ["cartographer", "admin", "planner", '
        '"reviewer",\n  "dev", "devops"];',
    ),
    (
        "M4 bespoke shells rank FIRST instead of last — indexOf's -1 is used "
        "raw, which is the natural way to get this wrong",
        "group_by_type",
        FALLBACK,
        "  return i;",
    ),
    (
        "M5 devops is dropped from the table and pools with the unknown "
        "bucket — this is the ambiguity call PLN1 ratified (devops is a KNOWN "
        "type at rank 6, above cc), so it is pinned rather than assumed",
        "group_by_type",
        TABLE,
        'const IF_TYPE_ORDER = ["cartographer", "admin", "planner", "dev",\n'
        '  "reviewer"];',
    ),
    (
        "M6 the sort gains a tie-break of its own — within a type the "
        "server's order stops surviving, which is the half of the contract a "
        "grouping assertion alone never sees",
        "servers_order",
        COMPARATOR,
        "(a, b) => ifTypeRank(a) - ifTypeRank(b) || b.shell_id - a.shell_id",
    ),
    (
        "M7 only the NULL half of the bespoke bucket is handled — a type this "
        "engine has never heard of sorts first (the mutation the cc fixture "
        "alone CANNOT catch)",
        "group_by_type",
        FALLBACK,
        "  return i < 0 && s.flavor == null ? IF_TYPE_ORDER.length : i;",
    ),
    (
        "M8 only the unknown-STRING half is handled — the fork's own cc, "
        "whose flavor is literally null, sorts first",
        "group_by_type",
        FALLBACK,
        "  return i < 0 && s.flavor != null ? IF_TYPE_ORDER.length : i;",
    ),
    (
        "M9 the payload is sorted IN PLACE — the same array is the rail "
        "poll's signature input and renderInterface's selection lookup, and "
        "both read the server's order",
        "group_by_type",
        "[...shells].sort(",
        "shells.sort(",
    ),
]


def clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run(selector: str) -> bool:
    """True when the selected tests pass, run on source rather than bytecode."""
    clear_caches()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-k", selector],
        cwd=ROOT, capture_output=True, text=True, env=env)
    return proc.returncode == 0


def main() -> int:
    verbose = "-v" in sys.argv
    failures = []
    for label, selector, old, new in MUTATIONS:
        original = APP.read_text()
        if original.count(old) != 1:
            failures.append(f"{label}: anchor found {original.count(old)}x, "
                            "expected exactly 1 — the driver has drifted from "
                            "the source")
            print(f"[FAIL] {label}\n       anchor is not unique")
            continue
        try:
            APP.write_text(original.replace(old, new, 1))
            red = not run(selector)
        finally:
            APP.write_text(original)
        green = run(selector)
        ok = red and green
        if not ok:
            failures.append(
                f"{label}: mutated={'RED' if red else 'GREEN (NOT PINNED)'} "
                f"restored={'green' if green else 'RED (dirty revert)'}")
        print(f"[{'ok' if ok else 'FAIL'}] {label}\n       {SUITE} "
              f"-k {selector}: {'red' if red else 'STAYED GREEN'} -> "
              f"{'green' if green else 'STILL RED'}")
        if verbose and not ok:
            print("       ^ this property is not actually pinned")
    print(f"\n{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} round trips "
          "behaved (red under mutation, green on revert)")
    for line in failures:
        print("  FAIL " + line)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
