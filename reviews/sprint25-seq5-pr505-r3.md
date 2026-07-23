# Re-review (r3) — Sprint 25 seq 5 · PR #505 (feat/interface-vertical-slice) @18d7216

Reviewer: REV1 (Kimi K3) · 2026-07-23 · task #81 · spec #20 · scoped re-review of
the flag #46 fix (r2: reviews/sprint25-seq5-pr505-r2.md). Fix diff
80d2490..18d7216: +9/-8, one file (.super-coder/ui/app.js).
CI at review time: 6/6 pass on 18d7216 (CodeQL, Analyze×2, render-check,
verify, tests); PR open, mergeable=MERGEABLE.
Verdict: **REVIEW-CLEAN — flag #46 verified FIXED.** All Major/Medium findings
resolved; DEV3 clear to merge. 1 new Low (report-only).

## Flag #46 (Medium, sessionStorage-held operator capability) — FIXED

Claim-by-claim, read against the code at 18d7216 (not the description):

1. **Used once for the exchange, then discarded.** `ifOpToken` is set only in
   the prompt path (app.js:2145) and sent only as `Authorization: Bearer` on
   `POST /api/interface/browser-sessions` (app.js:2133) — the only
   Authorization use in the file; the token never reaches a URL, body, or the
   DOM (the prompt prefill that echoed it into the dialog is gone).
2. **Not persisted.** The `sessionStorage.setItem("sc-if-op", …)` line is
   deleted; repo-wide grep for `sc-if-op` at 18d7216 returns exactly one hit —
   the init-time `removeItem` cleanup (app.js:2129), which also purges keys
   older builds left behind. Verified, not assumed.
3. **Cleared from JS memory after exchange.** On mint: `if (r.ok) { ifCsrf =
   data.csrf; ifOpToken = null; return; }` (app.js:2138). On a rejected paste
   (second 401): also nulled (app.js:2149). On the happy path the token's
   memory lifetime is one loopback round-trip. (Residual edge: see Low #4.)
4. **Subsequent mutations ride cookie + CSRF only.** Traced `apiIf`: sends
   `credentials: "same-origin"` + `X-CSRF: ifCsrf` + per-attempt
   Idempotency-Key — no Authorization header, no token. On a later 401/403 it
   nulls `ifCsrf` and re-bootstraps; the fresh bootstrap POSTs without a token,
   gets 401, and re-prompts the operator (bootstrap attempt 0). The re-prompt
   path is reachable and correct; the HttpOnly cookie itself is JS-invisible
   (server side verified in r2).
5. **No other JS-reachable path retains the cap.** Repo-wide grep for
   `ifOpToken`: app.js only, all six references inside `ifBootstrap`.

This is exactly the remediation r2 prescribed (drop persistence, re-prompt on
401, memory at most). DEV3 closed flag #46 with notes; confirmed absent from
the open-flags list.

## Lows (report-only; sprint-report tally now 4)

1-3. Carried from r2: unrun pane-death e2e (needs one full-stack execution
   before conformance treats #40 as e2e-proven); non-constant-time token
   compare; mid-session sidecar death slow-fails on 10s timeouts.
4. **NEW — residual memory retention on the failed-exchange path.** If the
   exchange attempt fails non-401 (5xx, or a network reject — `fetch` is not
   wrapped, so a network error propagates), `ifOpToken` stays set for the page
   lifetime, and a later `apiIf` silently re-bootstraps WITH the retained
   token (attempt 0 sends Authorization without re-prompting) — "one-shot"
   becomes "until a session mints". Reachability while set: app.js loads as a
   classic script (index.html:48 `<script src="/app.js">`), so the top-level
   binding sits in the global lexical environment, readable by any same-origin
   script. Far narrower than #46 (requires an exchange failure; memory only,
   dies with the page; any successful mint clears it) — hardening, not a gate:
   null `ifOpToken` on every non-ok exit (e.g. `finally`), don't reuse it
   silently. Also noted: no automated regression guard exists for this
   client-side contract — the pytest interface suites can't see app.js (the
   original #46 equally slipped r1's tests); verification here is by code
   read.

## Handoff

- DEV3: `review-clean` declared — flag #46 verified fixed; merge on green per
  scoped sprint authority. Low #4 above is report-only, yours to pick up or
  defer.
- PLN1: unit 5 review-clean; Lows tally 4 (3 carried + 1 new). Low #1 (unrun
  pane-death e2e) still needs a full-stack execution before the conformance
  pass treats #40 as proven end-to-end.
