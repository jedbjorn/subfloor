#!/usr/bin/env python3
"""Mutation driver for spec #49 U3 (Account Analytics API routes).

Same shape and the same reason as tests/mutations/u2_quota_probes.py: green CI
proves the suite ran, not that any guarantee is protected. Each mutation below
breaks exactly ONE property in the real source, runs the suite, and demands
red; the driver reverts and demands green again. A mutation that stays GREEN is
a finding about the test, not a success.

Asked PER PROPERTY, not per test. Several of these live in one function and one
SQL string, and a single test can constrain the property it names while leaving
an adjacent one entirely free — the upsert's conflict target and its
first_seen-preservation are different guarantees in the same statement, and so
are the 7-day filter and the is_current exemption in the same WHERE clause.

Deliberately included, because it is the one place this unit departs from the
spec's literal mechanism: `ttl-db-clock-only` restores the spec's
captured_at-only TTL. It must redden exactly ONE test — the degraded case where
a probe writes no rows at all — which is what makes the departure load-bearing
rather than decoration.

Usage:
    python3 tests/mutations/u3_quota_api.py           # all mutations
    python3 tests/mutations/u3_quota_api.py --list
    python3 tests/mutations/u3_quota_api.py --only <name>

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
SERVER = ROOT / ".super-coder" / "api" / "server.py"

SUITES = ["tests/test_quota_accounts_api.py"]


@dataclass
class Mutation:
    name: str
    property: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    # ── The upsert: the conflict target is the EXPRESSION ────────────────────
    Mutation(
        name="conflict-target-plain-scope",
        property="the upsert names migration 0096's expression index verbatim",
        path=SERVER,
        old="ON CONFLICT(account_pk, window_kind, COALESCE(scope, '')) DO UPDATE SET",
        new="ON CONFLICT(account_pk, window_kind, scope) DO UPDATE SET",
    ),
    Mutation(
        name="window-insert-without-upsert",
        property="a re-probe UPDATES the account-wide row instead of twinning it",
        path=SERVER,
        old="""ON CONFLICT(account_pk, window_kind, COALESCE(scope, '')) DO UPDATE SET
    used_percent=excluded.used_percent, used=excluded.used,
    limit_value=excluded.limit_value, resets_at=excluded.resets_at,
    captured_at=excluded.captured_at, status=excluded.status,
    probe_version=excluded.probe_version
""",
        new="",
    ),
    Mutation(
        name="coalesce-folds-real-scopes",
        property="COALESCE folds only NULL — two real scopes stay distinct rows",
        path=SERVER,
        old="pk, w[\"window_kind\"], w.get(\"scope\"), w.get(\"used_percent\"),",
        new="pk, w[\"window_kind\"], None, w.get(\"used_percent\"),",
    ),
    # ── The registry: identity survives an account going quiet ───────────────
    Mutation(
        name="first-seen-overwritten",
        property="a returning account keeps its ORIGINAL first_seen",
        path=SERVER,
        old='"account_label=excluded.account_label, plan=excluded.plan, "',
        new='"first_seen=excluded.first_seen, '
            'account_label=excluded.account_label, plan=excluded.plan, "',
    ),
    Mutation(
        name="is-current-never-cleared",
        property="switching accounts moves is_current OFF the old row",
        path=SERVER,
        old='        con.execute("UPDATE harness_quota_account SET is_current=0 WHERE provider=?",\n'
            "                    (provider,))",
        new="        pass",
    ),
    Mutation(
        name="null-ref-writes-a-row",
        property="a provider with no credential file writes NO registry row",
        path=SERVER,
        old='    named = [a for a in accounts if a.get("account_ref")]',
        new="    named = list(accounts)",
    ),
    Mutation(
        name="unauth-wipes-last-known",
        property="an expired token PRESERVES the last known values and their age",
        path=SERVER,
        old="        for w in acct.get(\"windows\") or []:",
        new="        con.execute(\"DELETE FROM harness_quota_window WHERE account_pk=?\", (pk,))\n"
            "        for w in acct.get(\"windows\") or []:",
    ),
    # ── The TTL ──────────────────────────────────────────────────────────────
    Mutation(
        name="ttl-never-suppresses",
        property="a second arrival inside 60s does not re-probe",
        path=SERVER,
        old="            if seen and now - max(seen) < QUOTA_TTL_SECONDS:\n"
            "                return False",
        new="            pass",
    ),
    Mutation(
        name="ttl-db-clock-only",
        property="the TTL still holds when the probe wrote NO rows (the departure)",
        path=SERVER,
        old='            seen = [t for t in (_QUOTA_PROBE["at"], _iso_epoch(newest)) if t]',
        new="            seen = [t for t in (_iso_epoch(newest),) if t]",
    ),
    Mutation(
        name="force-ignored",
        property="the refresh button ALWAYS bypasses the TTL",
        path=SERVER,
        old="        if not force:",
        new="        if True:",
    ),
    Mutation(
        name="route-owns-a-timeout",
        property="this layer re-implements no timeout — probe_all owns it",
        path=SERVER,
        old="    accounts = quota_dispatch.probe_all(notes.append)",
        new="    accounts = quota_dispatch.probe_all(notes.append, timeout=5.0)",
    ),
    # ── The read: the 7-day window, and the account that is exempt from it ───
    Mutation(
        name="activity-window-ignored",
        property="an account outside the 7-day window stops rendering",
        path=SERVER,
        old='"SELECT * FROM harness_quota_account WHERE last_seen >= ? OR is_current=1 "',
        new='"SELECT * FROM harness_quota_account WHERE (last_seen >= ? OR 1=1) "',
    ),
    Mutation(
        name="current-account-not-exempt",
        property="the CURRENT account renders even when its last_seen has aged",
        path=SERVER,
        old='"SELECT * FROM harness_quota_account WHERE last_seen >= ? OR is_current=1 "',
        new='"SELECT * FROM harness_quota_account WHERE last_seen >= ? "',
    ),
    Mutation(
        name="windows-attached-to-wrong-account",
        property="each account carries its OWN windows",
        path=SERVER,
        old='        a["windows"] = by_pk.get(a["account_pk"], [])',
        new='        a["windows"] = [w for ws in by_pk.values() for w in ws]',
    ),
    # ── The response ─────────────────────────────────────────────────────────
    Mutation(
        name="provider-status-splatted",
        property="the response is built from NAMED keys — no probe-dict splat",
        path=SERVER,
        old='    providers = [{"provider": a["provider"], "status": a.get("status"),\n'
            '                  "detail": a.get("detail")} for a in accounts]',
        new="    providers = [dict(a) for a in accounts]",
    ),
    # ── The routes themselves ────────────────────────────────────────────────
    Mutation(
        name="get-route-renamed-to-usage",
        property="the read route is /api/analytics/accounts, never `usage`",
        path=SERVER,
        old='            if path == "/api/analytics/accounts":',
        new='            if path == "/api/analytics/usage-accounts":',
    ),
    Mutation(
        name="probe-route-unreachable",
        property="POST /api/analytics/accounts/probe is wired to the refresh path",
        path=SERVER,
        old='            if path == "/api/analytics/accounts/probe":',
        new='            if path == "/api/analytics/accounts/reprobe":',
    ),
]


# The suite runs in ~1.5s; the timeout is the backstop for a mutation that
# hangs rather than answers. A suite that never answers demonstrates nothing.
SUITE_TIMEOUT_S = 120


def run_suites() -> tuple[bool, bool]:
    """(passed, timed_out). A timeout is a failure."""
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
    """SIGTERM must unwind so the `finally` that reverts the source runs — a
    killed driver otherwise leaves server.py MUTATED in the worktree."""
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
            print(f"{m.name:34s} {m.property}")
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
        print(f"{m.name:34s} ", end="", flush=True)
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
