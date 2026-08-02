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

Deliberately included, because it is the property most likely to be "restored"
by a well-meaning later reader: `ttl-hybrid-db-clock-restored` puts the DB's
newest captured_at back into the claim alongside the in-process attempt. The
ratified clock is the ATTEMPT, in this process, alone — a hybrid survives a
restart and then serves a response whose per-provider status list is EMPTY,
because that cache died with the process. Reintroducing it must redden, or the
next person reintroduces it and the suite stays green (it did: the hybrid
shipped once and no test noticed).

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
    # Extra (old, new) edits applied with the same exactly-one-anchor rule. A
    # property whose removal spanned several sites has to be restored at ALL of
    # them — a half-restored clock is a different mutation than the one named.
    also: tuple[tuple[str, str], ...] = ()


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
        old="        if not force and last and now - last < QUOTA_TTL_SECONDS:\n"
            "            return False",
        new="        pass",
    ),
    Mutation(
        name="ttl-hybrid-db-clock-restored",
        property="the clock is the in-process ATTEMPT alone — no DB captured_at",
        path=SERVER,
        old='def _quota_claim(force: bool) -> bool:\n'
            '    """True when THIS caller should probe. `force` is the refresh button."""\n'
            "    now = datetime.now(timezone.utc).timestamp()\n"
            "    with _QUOTA_LOCK:\n"
            "        last = _QUOTA_PROBE[\"at\"]\n"
            "        if not force and last and now - last < QUOTA_TTL_SECONDS:\n"
            "            return False",
        new='def _iso_epoch(ts: "str | None") -> "float | None":\n'
            "    if not ts:\n"
            "        return None\n"
            "    try:\n"
            '        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))\n'
            "    except (ValueError, TypeError):\n"
            "        return None\n"
            "    if dt.tzinfo is None:\n"
            "        dt = dt.replace(tzinfo=timezone.utc)\n"
            "    return dt.timestamp()\n"
            "\n"
            "\n"
            'def _quota_claim(force: bool, newest: "str | None" = None) -> bool:\n'
            '    """True when THIS caller should probe. `force` is the refresh button."""\n'
            "    now = datetime.now(timezone.utc).timestamp()\n"
            "    with _QUOTA_LOCK:\n"
            "        seen = [t for t in (_QUOTA_PROBE[\"at\"], _iso_epoch(newest)) if t]\n"
            "        if not force and seen and now - max(seen) < QUOTA_TTL_SECONDS:\n"
            "            return False",
        # The caller has to feed the restored clock, or `newest` stays None and
        # the mutation is a no-op that passes for the wrong reason.
        also=(
            ("    probe = probe_quota_accounts(con) if _quota_claim(force) else None",
             '    newest = con.execute("SELECT MAX(captured_at) FROM harness_quota_window").fetchone()[0]\n'
             "    probe = probe_quota_accounts(con) if _quota_claim(force, newest) else None"),
        ),
    ),
    Mutation(
        name="force-ignored",
        property="the refresh button ALWAYS bypasses the TTL",
        path=SERVER,
        old="        if not force and last and now - last < QUOTA_TTL_SECONDS:",
        new="        if last and now - last < QUOTA_TTL_SECONDS:",
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
    text = original
    for old, new in ((mutation.old, mutation.new), *mutation.also):
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"{mutation.name}: anchor matched {count} times in "
                f"{mutation.path.name}, expected exactly 1 — the driver is stale")
        text = text.replace(old, new)
    mutation.path.write_text(text)
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
