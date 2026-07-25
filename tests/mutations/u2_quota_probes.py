#!/usr/bin/env python3
"""Mutation driver for spec #49 U2 (quota probe package).

The unit's value is five invariants a reviewer is told to check FROM CODE
(spec #49, Probe Contract) plus one normalization rule. Reading the code
proves none of them, and green CI proves only that the suite ran — so each
mutation below breaks exactly one property in the real source, runs the suite,
and demands red; the driver reverts and demands green again. A mutation that
stays GREEN is a finding about the test, not a success.

Same shape and the same reason as tests/mutations/u7_composer_actions.py: the
round trips live in the repo so they survive the session that made them.

Deliberately absent: a "the wall-clock deadline is dropped" mutation that
HANGS. The containment test bounds its assertion between the contained cost
(~1.2s) and the stub's 3s sleep, so removing the deadline reddens it in 3s
instead of spinning — a suite that never answers demonstrates nothing (the
u7 driver learned this the hard way).

Usage:
    python3 tests/mutations/u2_quota_probes.py          # all mutations
    python3 tests/mutations/u2_quota_probes.py --list
    python3 tests/mutations/u2_quota_probes.py --only <name>

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
PKG = ROOT / ".super-coder" / "scripts" / "quota_probes"
INIT = PKG / "__init__.py"
ANTHROPIC = PKG / "anthropic.py"
OPENAI = PKG / "openai.py"
MOONSHOT = PKG / "moonshot.py"
DISPATCH = PKG / "dispatch.py"

SUITES = ["tests/test_quota_probes.py"]


@dataclass
class Mutation:
    name: str
    property: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    # ── Invariant: expiry is REPORTED, not repaired ──────────────────────────
    Mutation(
        name="expiry-never-detected",
        property="an expired access token is never sent to the provider",
        path=INIT,
        old="    return secs <= epoch_now()",
        new="    return False",
    ),
    Mutation(
        name="moonshot-skips-its-expiry-check",
        property="each provider checks expiry itself, not just the helper",
        path=MOONSHOT,
        old='    if is_expired((creds or {}).get("expires_at")):',
        new="    if False:",
    ),
    # ── Invariant: no credential file means `na`, not 0% ─────────────────────
    Mutation(
        name="absent-credentials-report-zero",
        property="the absence of a limit is not a limit of zero",
        path=ANTHROPIC,
        old='        return result(status="na", is_current=0)',
        new='        return result(status="ok", is_current=0, windows=[window(\n'
            '            window_kind="session", used_percent=0,\n'
            "            captured_at=captured_at, probe_version=PROBE_VERSION)])",
    ),
    # ── Invariant: tokens are never stored, logged, or returned ──────────────
    Mutation(
        name="token-reaches-the-log",
        property="no log line carries a token (the classic debug-line leak)",
        path=ANTHROPIC,
        old="    code, payload = get_json(URL, {",
        new='    log(f"{HARNESS_PROVIDER}: GET {URL} bearer {token}")\n'
            "    code, payload = get_json(URL, {",
    ),
    Mutation(
        name="token-reaches-a-returned-row",
        property="no returned row carries a token",
        path=MOONSHOT,
        old="    common = dict(account_ref=ref, account_label=ref)",
        new="    common = dict(account_ref=ref, account_label=token)",
    ),
    # ── Invariant: read-only on credentials ──────────────────────────────────
    Mutation(
        name="credential-file-rewritten",
        property="a probe never writes the harness's credential file",
        path=ANTHROPIC,
        old="    uuid, email = _identity(log)",
        new="    CREDENTIALS.write_text(CREDENTIALS.read_text())\n"
            "    uuid, email = _identity(log)",
    ),
    # ── The label ruled in decision #69 ──────────────────────────────────────
    Mutation(
        name="anthropic-labelled-by-uuid",
        property="Anthropic's card carries the full email, not the uuid stub",
        path=ANTHROPIC,
        old="    return email or uuid[:8]",
        new="    return uuid[:8]",
    ),
    Mutation(
        name="label-has-no-fallback",
        property="a profile with no address still labels the card",
        path=ANTHROPIC,
        old="    return email or uuid[:8]",
        new="    return email",
    ),
    # ── Invariant: one provider's failure is contained ───────────────────────
    Mutation(
        name="probe-exception-escapes",
        property="a raising provider degrades to its own error row",
        path=DISPATCH,
        old='        return [_error(name, f"probe raised {type(e).__name__}")]',
        new="        raise",
    ),
    Mutation(
        name="dead-provider-module-is-fatal",
        property="an unimportable probe is one error row, not a dead sweep",
        path=DISPATCH,
        old='        return [_error(name, f"probe module unavailable: {e}")]',
        new="        raise",
    ),
    # ── Normalization: kind comes from the observed DURATION ─────────────────
    Mutation(
        name="kind-read-from-the-window-name",
        property="window kind is derived from duration, never from the label",
        path=OPENAI,
        old='    for key in ("primary_window", "secondary_window"):\n'
            "        row = _window_row(rate_limit.get(key), captured_at, log)",
        new='    for key, named in (("primary_window", "five_hour"),\n'
            '                       ("secondary_window", "weekly")):\n'
            "        entry = rate_limit.get(key)\n"
            "        entry = {**entry, \"limit_window_seconds\": None} \\\n"
            "            if isinstance(entry, dict) else entry\n"
            "        row = _window_row(entry, captured_at, log, fallback_kind=named)",
    ),
    Mutation(
        name="unmapped-duration-guessed-as-weekly",
        property="an unrecognized duration is kept as unknown, never guessed",
        path=INIT,
        old="    if 0 < secs <= SHORT_MAX_SECONDS:\n"
            '        return "short"\n'
            "    return None",
        new="    if 0 < secs <= SHORT_MAX_SECONDS:\n"
            '        return "short"\n'
            '    return "weekly"',
    ),
    Mutation(
        name="unknown-kind-dropped",
        property="a window under an unrecognized kind is rendered, not dropped",
        path=ANTHROPIC,
        old="        elif kind is None:\n"
            '            log(f"{HARNESS_PROVIDER}: unrecognized limit kind {raw_kind!r} — "\n'
            '                "kept under its own row")\n'
            "            scope = str(raw_kind) if raw_kind else None",
        new="        elif kind is None:\n            continue",
    ),
    # ── Numbers: never invented, never divided by zero ───────────────────────
    Mutation(
        name="zero-limit-divides",
        property="limit = 0 yields NULL, never a division",
        path=INIT,
        old="    if limit_n <= 0:",
        new="    if limit_n < 0:",
    ),
    Mutation(
        name="counts-back-computed-from-percent",
        property="a count is never invented from a percent",
        path=ANTHROPIC,
        old='            used_percent=as_percent(item.get("percent")),',
        new='            used_percent=as_percent(item.get("percent")),\n'
            '            used=as_percent(item.get("percent")), limit_value=100,',
    ),
    # ── Status vocabulary ────────────────────────────────────────────────────
    Mutation(
        name="rejection-reported-as-error",
        property="401/403 is unauth (last values stand), not a generic error",
        path=INIT,
        old="    if code in (401, 403):",
        new="    if code in (403,):",
    ),
    Mutation(
        name="drift-writes-partial-rows",
        property="a drifted response is an error, never a partial write",
        path=ANTHROPIC,
        old='    limits = payload.get("limits")\n'
            "    if not isinstance(limits, list):",
        new='    limits = payload.get("limits") or []\n'
            "    if False:",
    ),
    # ── Drift vs legitimately-empty: the two must not collapse ───────────────
    # Each provider gets the round trip in BOTH directions — a probe that
    # swallows a lost envelope as zero windows, and a probe that cries drift
    # at an intact envelope reporting none. Only the pair pins the boundary;
    # either alone is satisfied by a probe that gets the other case wrong.
    Mutation(
        name="openai-lost-envelope-parsed-as-zero",
        property="a 200 with no rate_limit{} is error, not an ok reading zero",
        path=OPENAI,
        old="    if not isinstance(rate_limit, dict):",
        new="    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}\n"
            "    if False:",
    ),
    Mutation(
        name="openai-empty-envelope-cried-as-drift",
        property="an intact rate_limit{} reporting no window stays ok",
        path=OPENAI,
        old="    if not isinstance(rate_limit, dict):",
        new="    if not rate_limit or not isinstance(rate_limit, dict):",
    ),
    Mutation(
        name="moonshot-lost-usage-parsed-as-zero",
        property="a 200 with no usage{} is error, not an ok reading zero",
        path=MOONSHOT,
        old='    if not isinstance(payload.get("usage"), dict):',
        new="    if False:",
    ),
    Mutation(
        name="moonshot-absent-limits-cried-as-drift",
        property="an absent `limits` collection is data, never a false alarm",
        path=MOONSHOT,
        old="    if limits is not None and not isinstance(limits, list):",
        new="    if not isinstance(limits, list):",
    ),
    Mutation(
        name="unreadable-200-reported-as-http-200",
        property="a 200 that is not a JSON object says so, not 'HTTP 200'",
        path=INIT,
        old='        return drift(provider, "response was not a JSON object", log)',
        new='        return f"HTTP {code}"',
    ),
]


# The suite runs in ~5s (one mutation deliberately costs a 3s sleep). The
# timeout is the backstop for a mutation that hangs rather than answers.
SUITE_TIMEOUT_S = 120


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
    a killed driver otherwise leaves the package MUTATED in the worktree."""
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
            print(f"{m.name:36s} {m.property}")
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
        print(f"{m.name:36s} ", end="", flush=True)
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
