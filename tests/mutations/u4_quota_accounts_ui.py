"""Mutation round trips for sprint 52 U4 — the Account Analytics UI.

Acceptance evidence, committed rather than run once in a session that dies with
it (the U1/U2/U3 convention). Each entry breaks ONE property in the real source,
asserts the test claiming to pin it actually goes RED, restores the file, and
asserts it goes green again. A property whose mutation stays green is not
pinned, whatever the suite's colour says.

    python3 tests/mutations/u4_quota_accounts_ui.py [-v]

Asked per PROPERTY, not per test. Three of this unit's rules have TWO failure
directions each, and a one-directional leg is satisfied by a fix that merely
trades the bug for its mirror image — the shape U2's reviewer caught mid-sprint:

  * n/a versus a measured zero: M9 draws a bar under an absent number (a meter
    reads as measured), M11 swallows a REAL 0% into n/a. Either alone blesses
    the other.
  * the seven-day note: M16 renders it on every page, M17 on none. "Never an
    empty page" and "not a note nobody reads" are different requirements living
    in one condition.
  * muted: M13 drops the signed-out half, M14 drops the unauth half. One
    condition, two independent reasons a card is not current.

M6 is the one that matters most and the one a percent-only suite cannot see: it
colours from a provider's status rather than from used_percent. Every threshold
test would stay green under it, because they all use consistent fixtures — only
the deliberately-contradictory card (an `error` provider at 22%) reddens.

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
SUITE = "tests/test_quota_accounts_ui.py"

THRESHOLDS = ('const anQuotaClass = (pct) => pct == null ? "" : '
              'pct >= 95 ? " red" : pct >= 80 ? " amber" : "";')
FILL_GUARD = "  if (pct != null) {"
MUTED = '  const muted = !current || status === "unauth";'
WINS = "  const wins = [...(acct.windows || [])].sort((a, b) => anWindowRank(a) - anWindowRank(b));"
GROUP_ORDER = ("  for (const name of [...providers.map((p) => p.provider), "
               "...accounts.map((a) => a.provider)]) {")
NOTE_GATE = "  if (accounts.length && !accounts.some(fresh))"
DISPATCH = "  return anView === \"accounts\" ? anAccountSection(body) : anTokenSection(body);"

# (label, -k selector, old, new)
MUTATIONS = [
    (
        "M1 the amber bar moves to 90 — the threshold TABLE is the deliverable, "
        "so pinning 'some colour rule ran' is not enough",
        "threshold_driven",
        THRESHOLDS,
        'const anQuotaClass = (pct) => pct == null ? "" : '
        'pct >= 95 ? " red" : pct >= 90 ? " amber" : "";',
    ),
    (
        "M2 the red bar moves to 90 — an 80-only suite would not notice",
        "threshold_driven",
        THRESHOLDS,
        'const anQuotaClass = (pct) => pct == null ? "" : '
        'pct >= 90 ? " red" : pct >= 80 ? " amber" : "";',
    ),
    (
        "M3 the bounds go strict — 80.0 and 95.0 exactly, the two values a "
        "provider reports most often, fall a class short",
        "threshold_driven",
        THRESHOLDS,
        'const anQuotaClass = (pct) => pct == null ? "" : '
        'pct > 95 ? " red" : pct > 80 ? " amber" : "";',
    ),
    (
        "M4 an ABSENT percent is painted normal-green rather than left "
        "uncoloured — the reassuring colour is the wrong default for a number "
        "we do not have",
        "null_percent_reads_na",
        THRESHOLDS,
        'const anQuotaClass = (pct) => pct == null ? " amber" : '
        'pct >= 95 ? " red" : pct >= 80 ? " amber" : "";',
    ),
    (
        "M5 the meter is unclamped — a provider reporting over 100 overflows "
        "the track instead of filling it",
        "threshold_driven",
        "    fill.style.width = Math.max(0, Math.min(100, pct)) + \"%\";",
        "    fill.style.width = pct + \"%\";",
    ),
    (
        "M6 COLOUR IS TAKEN FROM THE PROVIDER'S STATUS instead of used_percent "
        "— openai's limit_reached painted red at 22%. Every consistent fixture "
        "stays green under this; only the contradictory card reddens",
        "provider_status_never_decides_colour",
        "  for (const w of wins) card.append(anWindowRow(w));",
        "  for (const w of wins) card.append(anWindowRow("
        "status === \"error\" ? { ...w, used_percent: 100 } : w));",
    ),
    (
        "M7 the accounts array drives provider state — the status list is "
        "ignored, so a provider that failed before naming anybody vanishes",
        "error_before_identification",
        GROUP_ORDER,
        "  for (const name of accounts.map((a) => a.provider)) {",
    ),
    (
        "M8 `na` stops being skipped — a missing credential file renders a "
        "card, which is the absence of a limit shown as a limit",
        "na_provider_renders_no_card",
        '    if (groups.has(name) || statusOf.get(name)?.status === "na") continue;',
        "    if (groups.has(name)) continue;",
    ),
    (
        "M9 a NULL percent draws its bar anyway — labelled n/a, drawn as a "
        "0% meter, which is exactly 'a zero rendered as if measured'",
        "null_percent_reads_na or moonshot_all_null or zero_limit",
        FILL_GUARD,
        "  if (true) {",
    ),
    (
        "M10 a NULL percent reads 0% — the same defect in the text rather "
        "than the bar",
        "null_percent_reads_na or moonshot_all_null or zero_limit",
        'const anPct = (pct) => pct == null ? "n/a" : Math.round(pct * 10) / 10 + "%";',
        'const anPct = (pct) => pct == null ? "0%" : Math.round(pct * 10) / 10 + "%";',
    ),
    (
        "M11 THE MIRROR TRADE: n/a swallows a genuinely measured 0%. A fix for "
        "M9/M10 that lands here has moved the bug, not removed it",
        "real_zero_percent",
        'const anPct = (pct) => pct == null ? "n/a" : Math.round(pct * 10) / 10 + "%";',
        'const anPct = (pct) => !pct ? "n/a" : Math.round(pct * 10) / 10 + "%";',
    ),
    (
        "M12 refresh is enabled on a signed-out card — a button that fires a "
        "probe which provably cannot change the card it sits on",
        "non_current_account or non_current_codex",
        "  if (!current) {",
        "  if (false) {",
    ),
    (
        "M13 muted loses the signed-out half — the non-current card renders at "
        "full emphasis, reading as live",
        "non_current_account",
        MUTED,
        '  const muted = status === "unauth";',
    ),
    (
        "M14 muted loses the unauth half — an expired token's last-known "
        "numbers render as if they were measured now",
        "unauth_keeps_last_known",
        MUTED,
        "  const muted = !current;",
    ),
    (
        "M15 the card's age comes from last_seen rather than the newest "
        "capture — a three-hour-old snapshot reports itself as minutes old, "
        "which is the one thing the age exists to prevent",
        "unauth_keeps_last_known",
        "  return caps.length ? caps[caps.length - 1] : acct.last_seen;",
        "  return acct.last_seen;",
    ),
    (
        "M16 the seven-day note renders on EVERY page — a warning that is "
        "always on is a warning nobody reads",
        "fresh_account_carries_no_stale_note",
        NOTE_GATE,
        "  if (accounts.length)",
    ),
    (
        "M17 the note never renders — the all-stale page then reads as the "
        "whole truth about the last week",
        "every_account_stale",
        NOTE_GATE,
        "  if (false)",
    ),
    (
        "M18 an unrecognized window is FILTERED OUT — the probe stores it "
        "under its raw duration specifically so the panel can show it",
        "unrecognized_window",
        WINS,
        "  const wins = [...(acct.windows || [])]"
        ".filter((w) => AN_WINDOW_LABEL[w.window_kind])"
        ".sort((a, b) => anWindowRank(a) - anWindowRank(b));",
    ),
    (
        "M19 indexOf's -1 is used raw, so an unrecognized window sorts FIRST "
        "— the natural way to get the rank table wrong",
        "unrecognized_window",
        "  return i < 0 ? AN_WINDOW_ORDER.length : i;",
        "  return i;",
    ),
    (
        "M20 the UI re-sorts the accounts — the API's current-first ordering "
        "stops surviving, and the current account is what the operator came for",
        "non_current_account",
        "  for (const a of accounts) groups.get(a.provider)?.accounts.push(a);",
        "  for (const a of [...accounts].sort((x, y) => String(x.account_label)"
        ".localeCompare(String(y.account_label)))) groups.get(a.provider)?.accounts.push(a);",
    ),
    (
        "M21 the label is truncated to its local part — decision #67 withdrew "
        "exactly this, because jed@gmail.com and jed@work.com both reduce to "
        "'jed' in the multi-account case the panel exists to show",
        "full_account_label",
        '  head.append(el("span", { className: "an-acct-label" }, acct.account_label || acct.account_ref));',
        '  head.append(el("span", { className: "an-acct-label" }, '
        'String(acct.account_label || acct.account_ref).split("@")[0]));',
    ),
    (
        "M22 a reset already past renders as negative time rather than 'due'",
        "countdown",
        '  return ms <= 0 ? "due" : "in " + anDuration(ms);',
        '  return "in " + anDuration(ms);',
    ),
    (
        "M23 the router branch goes — #analytics-accounts falls through to the "
        "default view and the deep link is dead",
        "hash_routes or unknown_hash",
        '  if (raw === "analytics" || raw.startsWith("analytics-")) {',
        "  if (false) {",
    ),
    (
        "M24 the branch matches but ignores which sub-view it matched — the "
        "deep link lands on Token Analytics",
        "hash_routes",
        '    anView = raw === "analytics-accounts" ? "accounts" : "tokens";',
        '    anView = "tokens";',
    ),
    (
        "M25 the toggle re-renders in place instead of setting the hash — the "
        "section works but stops surviving a reload, which is a spec checkbox",
        "deep_linkable",
        '    b.onclick = () => { location.hash = mode === "accounts" ? "analytics-accounts" : "analytics"; };',
        "    b.onclick = () => { anView = mode; renderAnalytics(root); };",
    ),
    (
        "M26 arrival POSTs the force-probe — the 60s TTL is defeated from the "
        "client, so toggling twice inside a minute hits three endpoints twice",
        "arrival_probes_once",
        '    anDrawAccounts(root, await api("/analytics/accounts"));',
        '    anDrawAccounts(root, await api("/analytics/accounts/probe", "POST"));',
    ),
    (
        "M27 both sections run on every arrival — reading token spend now "
        "costs three third-party calls, which is what the spec's 'probe fires "
        "ONLY on arrival at this section' forbids",
        "each_section_fires_only_its_own_work",
        DISPATCH,
        "  await anTokenSection(body);\n"
        "  return anView === \"accounts\" ? anAccountSection(body) : undefined;",
    ),
    (
        "M28 a failed refresh leaves the button wedged in 'probing…' — the "
        "operator's only retry is a page reload",
        "failed_refresh",
        "    btn.disabled = false;\n    btn.textContent = label;",
        "    void label;",
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
