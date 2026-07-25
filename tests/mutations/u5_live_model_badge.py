"""Mutation round trips for spec #43 U5 — the live-model badge.

Acceptance evidence, committed rather than run once in a worktree that dies
with the session (REV1's U2 finding L3). Each entry below breaks ONE property
in the real source, asserts the test that claims to pin it actually goes RED,
restores the file, and asserts it goes green again. A property whose mutation
stays green is not pinned, whatever the suite's colour says.

    python3 tests/mutations/u5_live_model_badge.py [-v]

Not named test_*.py on purpose: pytest must not collect it — it edits tracked
source. It restores every file in a finally block, so an interrupted run still
leaves the tree clean; `git diff` after a run is the check.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / ".super-coder" / "ui" / "app.js"
SUITE = "tests/test_interface_ui_contract.py"

GATE = ('  if (!s || s.live_model_verdict !== "ok" || !s.live_model) '
        'return null;')
OBSERVED = """  const observed = s.live_model_at
    ? "observed " + s.live_model_at
    : "the transcript recorded no observation time";"""

# (label, -k selector for the test that must go red, file, old, new)
MUTATIONS = [
    (
        "M1 rail signature drops the live fields (the planner's own: without "
        "them the badge repaints nothing on the 5s poll)",
        "repaints_the_badge", APP,
        ",\n    s.live_model, s.live_model_verdict]));",
        "]));",
    ),
    (
        "M2 a STALE reading is allowed to paint as live (the fail direction "
        "that matters — never claim live what is not)",
        "stale_reading", APP, GATE,
        '  if (!s || (s.live_model_verdict !== "ok"\n'
        '      && s.live_model_verdict !== "stale") || !s.live_model) '
        'return null;',
    ),
    (
        "M3 the verdict is gated negatively, so an unknown future verdict "
        "claims live instead of falling back",
        "fresh_explicit_reading", APP, GATE,
        '  if (!s || s.live_model_verdict === "stale"\n'
        '      || s.live_model_verdict === "none"\n'
        '      || s.live_model_verdict === "unsupported"\n'
        '      || !s.live_model) return null;',
    ),
    (
        "M4 an `ok` verdict with no model id still paints a badge",
        "fresh_explicit_reading", APP, GATE,
        '  if (!s || s.live_model_verdict !== "ok") return null;',
    ),
    (
        "M5 the hover drops the observation time",
        "fresh_explicit_reading", APP, '\n      + " — " + observed,', ",",
    ),
    (
        "M6 the hover drops the source of the reading",
        "fresh_explicit_reading", APP,
        '\n      + (s.harness ? s.harness + " transcript" : "session transcript")',
        ' + "transcript"',
    ),
    (
        "M7 a missing observation time is papered over with render time — a "
        "claim about the harness nobody observed",
        "never_invents_an_observation_time", APP, OBSERVED,
        '  const observed = "observed "\n'
        '    + (s.live_model_at || new Date().toISOString());',
    ),
    (
        "M8 the mobile picker makes its own model decision instead of "
        "sharing the row's",
        "one_model_decision", APP,
        'const mobileModel = model ? " · " + model.text : "";',
        'const mobileModel = model\n'
        '      ? " · " + ifLaunchedDisplay(s.model_route).text : "";',
    ),
]


def run(selector: str) -> bool:
    """True when the selected tests pass."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-k", selector],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    verbose = "-v" in sys.argv
    failures = []
    for label, selector, path, old, new in MUTATIONS:
        original = path.read_text()
        if original.count(old) != 1:
            failures.append(f"{label}: anchor found {original.count(old)}x, "
                            "expected exactly 1 — the driver has drifted from "
                            "the source")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            red = not run(selector)
        finally:
            path.write_text(original)
        green = run(selector)
        ok = red and green
        if not ok:
            failures.append(
                f"{label}: mutated={'RED' if red else 'GREEN (NOT PINNED)'} "
                f"restored={'green' if green else 'RED (dirty revert)'}")
        mark = "ok" if ok else "FAIL"
        print(f"[{mark}] {label}\n       -k {selector}: "
              f"{'red' if red else 'STAYED GREEN'} -> "
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
