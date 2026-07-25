#!/usr/bin/env python3
"""Mutation driver for spec #49 U5 (the verification gate + flag #195's fix).

Same shape and the same reason as tests/mutations/u2_quota_probes.py: green CI
proves the suite ran, not that anything is constrained. Each mutation below
breaks exactly ONE property in the real source, runs the gate's suites, and
demands red; the driver reverts and demands green again. A mutation that stays
GREEN is a finding about the test, not a success.

Asked per PROPERTY, not per test. The gate spans four files that four
different units own, so the mutations reach into all of them — that is what a
cross-unit checkbox means. Where a property already has a leg in another
unit's driver it is NOT repeated here; these are only the properties this unit
is the first to constrain.

The two flag #195 legs come first and are the reason this file exists. Both
halves of that fix are pinned independently, because either one alone leaves a
real hole: without the dispatch half any exception message still reaches the
log, and without the get_json half a corrupt credential file degrades to an
`error` with no diagnosis at all.

Usage:
    python3 tests/mutations/u5_quota_gate.py          # all mutations
    python3 tests/mutations/u5_quota_gate.py --list
    python3 tests/mutations/u5_quota_gate.py --only <name>

Exit 0 = every mutation reproduced red->revert->green.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / ".super-coder"
PKG = ENGINE / "scripts" / "quota_probes"
INIT = PKG / "__init__.py"
DISPATCH = PKG / "dispatch.py"
MOONSHOT = PKG / "moonshot.py"
SERVER = ENGINE / "api" / "server.py"
APP = ENGINE / "ui" / "app.js"

SUITES = ["tests/test_quota_gate.py", "tests/test_quota_probes.py"]
SUITE_TIMEOUT_S = 300


@dataclass
class Mutation:
    name: str
    property: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    # ── Flag #195: no token in any log line, API response, or DB row ─────────
    Mutation(
        name="exception-message-reaches-the-log",
        property="a contained failure is logged by TYPE — a message can carry a token",
        path=DISPATCH,
        old='        log(f"quota_probes: {name} probe failed: {type(e).__name__}")',
        new='        log(f"quota_probes: {name} probe failed: {e!r}")',
    ),
    Mutation(
        name="not-header-safe-token-escapes-get_json",
        property="a token illegal in a header fails inside get_json, not up the stack",
        path=INIT,
        old="    except ValueError as e:",
        new="    except ZeroDivisionError as e:",
    ),
    Mutation(
        name="header-failure-reports-nothing-actionable",
        property="a corrupt credential file still says WHY, without the value",
        path=INIT,
        old='        return 0, "invalid request header (check the credential file)"',
        new="        return 0, None",
    ),
    # ── Checkbox 11's positive control — the sweep must not pass vacuously ───
    Mutation(
        name="sweep-probes-nobody",
        property="'no token anywhere' is not satisfied by a probe that never ran",
        path=DISPATCH,
        old="    return [row for name in names for row in results.get(name, [])]",
        new="    return []",
    ),
    # ── Checkbox 3: a missing credential file is `na`, never 0% ─────────────
    Mutation(
        name="moonshot-absent-credentials-are-not-na",
        property="the na leg holds for ALL THREE providers, not two of them",
        path=MOONSHOT,
        old='        return result(status="na", is_current=0)',
        new='        return result(status="ok", is_current=0)',
    ),
    # ── Checkbox 7: hidden, then switched back, with first_seen intact ──────
    Mutation(
        name="returning-account-is-re-minted",
        property="an account that went quiet keeps its ORIGINAL first_seen",
        path=SERVER,
        old='            "account_label=excluded.account_label, plan=excluded.plan, "\n'
            '            "last_seen=excluded.last_seen, is_current=excluded.is_current",',
        new='            "account_label=excluded.account_label, plan=excluded.plan, "\n'
            '            "first_seen=excluded.first_seen, "\n'
            '            "last_seen=excluded.last_seen, is_current=excluded.is_current",',
    ),
    Mutation(
        name="hidden-account-is-deleted-not-filtered",
        property="the 7-day window is a filter; the row survives to come back",
        path=SERVER,
        old='    accounts = rows(con.execute(\n'
            '        "SELECT * FROM harness_quota_account WHERE last_seen >= ? OR is_current=1 "',
        new='    con.execute("DELETE FROM harness_quota_account "\n'
            '                "WHERE last_seen < ? AND is_current=0", (cutoff,))\n'
            '    accounts = rows(con.execute(\n'
            '        "SELECT * FROM harness_quota_account WHERE last_seen >= ? OR is_current=1 "',
    ),
    # ── Checkbox 8: the deep link survives a reload ─────────────────────────
    Mutation(
        name="boot-never-routes",
        property="a page load honours the hash it was loaded on",
        path=APP,
        old="  routeFromHash();   // honor #tab on load (refresh / deep link), else Shells",
        new="  // routeFromHash() removed",
    ),
    Mutation(
        name="hash-changes-are-not-listened-for",
        property="back/forward and in-page hash writes re-route",
        path=APP,
        old='window.addEventListener("hashchange", routeFromHash);',
        new="// hashchange listener removed",
    ),
    # ── Checkbox 9: toggling twice inside a minute performs one probe ───────
    Mutation(
        name="every-arrival-probes",
        property="the TTL absorbs a section toggle",
        path=SERVER,
        old="        if not force and last and now - last < QUOTA_TTL_SECONDS:\n"
            "            return False",
        new="        if False:\n            return False",
    ),
    Mutation(
        name="arrival-forces-the-ttl",
        property="the section GETs on arrival and never forces a probe",
        path=APP,
        old='    anDrawAccounts(root, await api("/analytics/accounts"));',
        new='    anDrawAccounts(root, await api("/analytics/accounts/probe", "POST"));',
    ),
    # ── Checkbox 4: a hung provider costs its own card and no others ────────
    Mutation(
        name="a-failed-provider-renders-no-card",
        property="a timed-out provider says so rather than going quiet",
        path=APP,
        old='    if (statusOf.get(name)?.status === "na" && !known.has(name)) continue;',
        new='    if (statusOf.get(name)?.status !== "ok") continue;',
    ),
]


def run_suites() -> tuple[bool, bool]:
    """(passed, timed_out). A timeout is a failure: a suite that cannot finish
    has not demonstrated the property."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *SUITES],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, True
    return result.returncode == 0, False


def apply(mutation: Mutation) -> str:
    original = mutation.path.read_text()
    count = original.count(mutation.old)
    if count != 1:
        raise SystemExit(
            f"{mutation.name}: anchor matched {count} times in "
            f"{mutation.path.name}, expected exactly 1 — the driver is stale")
    mutation.path.write_text(original.replace(mutation.old, mutation.new))
    return original


def _die(signum, frame):
    """SIGTERM must unwind so the `finally` that reverts the source runs —
    a killed driver otherwise leaves the tree MUTATED in the worktree."""
    raise SystemExit(f"interrupted by signal {signum}")


def main() -> int:
    signal.signal(signal.SIGTERM, _die)
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default=None, help="run one mutation by name")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if args.only is None or m.name == args.only]
    if args.list:
        for m in selected:
            print(f"{m.name:40s} {m.property}")
        return 0
    if not selected:
        raise SystemExit(f"no mutation named {args.only!r}")

    print("baseline: ", end="", flush=True)
    passed, timed_out = run_suites()
    if not passed:
        print("TIMED OUT" if timed_out else "RED", "— fix the tree before mutating")
        return 1
    print("green")

    failures = []
    for m in selected:
        print(f"{m.name:40s} ", end="", flush=True)
        original = apply(m)
        try:
            passed, timed_out = run_suites()
            red = not passed
            print("hung " if timed_out else ("red " if red else "GREEN "),
                  end="", flush=True)
        finally:
            m.path.write_text(original)     # restored even on Ctrl-C / SIGTERM
        green, _ = run_suites()
        print("-> revert -> " + ("green" if green else "STILL RED"))
        if not (red and green) or timed_out:
            failures.append((m, "hung" if timed_out else "did not round-trip"))

    print()
    if failures:
        print(f"{len(failures)} of {len(selected)} mutations FAILED:")
        for m, why in failures:
            print(f"  - {m.name} ({why}): {m.property}")
        return 1
    print(f"{len(selected)}/{len(selected)} mutations red -> revert -> green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
