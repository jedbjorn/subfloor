# Sprint 25 — Unit 7 review: cross-harness lifecycle adapters (PR #508)

- Reviewer: REV2 · 2026-07-23 · branch `feat/harness-lifecycle-hooks` vs `main`, +1585/-164
- Spec: doc #20 (Harness Hooks, Input Broker, Occupancy Model) · task #83 · CI 6/6 green
- Planner scrutiny list (msg #468): start-ready window, content discarding, installer no-clobber, L5 TOCTOU, route auth.

## Verdict

**2 Medium (flags #49, #50), 5 Low. No Major.** Both Mediums fail closed
(availability-only, never wrong delivery) and route naturally to seq-8
hardening — planner rules whether they block this merge.

## Scrutiny-point results

1. **START-READY (ambiguity #1)** — *gate rationale partially wrong as implemented; flag #49 (Medium).*
   Two-phase start verified in code + tests: entrypoint `session_start`
   (source=entrypoint) is identity-only (reserved→occupied, lifecycle stays
   `starting`, composer `unknown`); provider `session_start` moves
   starting→idle and cleans only with zero accepted input. Tests
   `test_hook_session_start_promotes`, `test_provider_readiness_never_cleans_after_human_input`
   prove both phases and would go red on regression.
   **But** the accepted rationale — "the wake gate's quiet debounce absorbs
   the residual window" — does not hold: `submit_wake_batch`'s quiet
   baseline is `occupied_at` (set at the *pre-exec* entrypoint promote,
   never updated at provider readiness). For a slow claude/codex boot
   (>3s — exactly when prompt-paint lag is worst) the debounce is already
   satisfied when the pre-prompt SessionStart arrives, so the gate can
   submit into a not-yet-painted TUI. PTY buffering + the submit-hook
   fence keep it fail-closed (batch parks `delivery_unknown`, never silent
   misdelivery), so this is wake *loss* in a startup race. Fix
   (first-turn_stop-as-readiness or quiet-from-provider-readiness) → seq 8,
   per the planner's own framing; seq-11 spec-debt already covers
   wake-into-fresh validation.

2. **CONTENT DISCARDING** — *verified clean.* The emitter never reads stdin
   (`</dev/null` in every registered command; `test_stdin_content_is_never_forwarded`
   proves `stdin.read` uncalled); the callback body is exactly
   {shell_id, generation, hook_seq, event, source, pid}
   (`test_callback_carries_only_contract_fields` asserts the exact dict);
   the route 422s any extra field. Token travels in launch env only — no
   credential in any config file (asserted in installer tests).

3. **INSTALLER NO-CLOBBER** — *verified.* claude = per-session `--settings`
   overlay (0600, nothing rewritten, additive by design); codex = merges
   event groups into project `.codex/hooks.json`, preserving the fork's
   PreToolUse branch-guard group, idempotent re-install, unparseable file
   left untouched (all three proven in `test_codex_merge_*`); kimi =
   marker-fenced block in user `config.toml`, idempotent replace. Fork/user
   hooks preserved in every adapter.

4. **L5 TOCTOU fix** — *verified closed.* Both `submit_wake_batch` (gate
   reads + submitting commit) and `accept_human_input` (lock check +
   pending reservation) serialize under `BEGIN IMMEDIATE`; production
   connections carry `busy_timeout=5000`, so a contender blocks, then
   re-reads post-commit state and refuses. WAL snapshots start at first
   read, which happens after lock acquisition — no pre-commit snapshot can
   pass. All exit paths release the transaction (rollback on gate_fail /
   exception, began=False after commit). Tests prove both race orderings
   plus lock-holder blocking (`test_gate_reads_cannot_observe_a_pre_commit_snapshot`
   goes red without the fix: the racer would read freely and pass).

5. **ROUTE AUTH** — *verified.* Unknown event/source/field → 422; illegal
   transition → 409 (InterfaceTransitionError caught, logged); PID fence on
   any event carrying a pid + required for session_start; replayed
   sequences and stale/ended generations rejected; rejections audited via
   `_log`. interrupt/failure end the turn (`_turn_finished` reconciles a
   running batch exactly like turn_stop; walk-back through busy from
   approval/user_input). Tests cover every one of these.

## Findings

- **#49 (Medium)** — start-ready quiet baseline is pre-exec, not readiness
  (detail in scrutiny 1). Fail-closed; seq-8 fix anticipated by planner.
- **#50 (Medium)** — hook allocation order ≠ commit order. The emitter's
  flock serializes seq *allocation* only; the POST runs outside the lock,
  so concurrent cross-event hooks can commit out of order and the
  receiver's `hook_seq <= last` reject permanently loses the earlier event.
  Worst traced case: turn_stop commits before prompt_submit → idle→idle
  illegal → 409, event lost; delayed prompt_submit then leaves the batch
  stuck `running`, lifecycle busy, composer dirty until next turn or
  operator reconcile. Always fails closed (never a wrong wake);
  availability-only; needs transport failure + sub-second window. Cheap
  fix: hold the flock through the POST, or server-side out-of-order
  tolerance in a small window.

## Lows (sprint report, non-blocking)

- L1: claude install path (`_claude_overlay`) does not guard write errors —
  an OSError propagates through `interface_exec` and kills the launch,
  contradicting `install()`'s documented "write failed → chat still
  launches" contract (codex/kimi return False instead). Rare trigger
  (engine-owned run dir).
- L2: kimi merge replace path does `tail.lstrip("\n")` — blank lines between
  the END marker and following user content are eaten on block replacement,
  so "preserved byte-for-byte" is slightly overbroad.
- L3: claude hook `timeout: 5` < emitter worst case (~3s + 0.3s + 3s retry)
  — a hung API gets the hook killed mid-retry. Event lost, fail-closed.
  codex/kimi use 10s.
- L4: kimi managed block lives in the *user-level* config.toml (the only
  config kimi 0.27 reads), so non-Interface kimi sessions also spawn the 8
  hook commands (they no-op via exit 0 without the env). Latency/noise
  only; documented necessity.
- L5: PID fence applies only when a pid is present; required only for
  session_start. Non-start events without pid skip the fence — reachable
  only by the generation-token holder itself (the token is the authority),
  so no real hole; tightening to require pid on every event is free.

## Notes

- Accepted ambiguities #2 (approval unmapped, stays-busy safe), #3 (codex
  SessionEnd in binary), #4 (entrypoint two-phase contract change) verified
  as declared — not re-flagged per planner instruction.
- Test suite quality: new tests are stringent (assert exact contract dict,
  prove no-clobber with a fork group fixture, prove both TOCTOU orderings,
  prove the gate never reads a pre-commit snapshot). 43 new/updated
  hermetic tests; CI 6/6 green.

## Addendum (second pass, same session — deep trace of axes 3/5)

- M3 (flag #51, Medium): NOT all hook-callback rejections are audited,
  contra spec #20 Harness Hooks ("wrong tokens, stale generations,
  replayed sequences, illegal transitions, and PID mismatch are rejected
  and audited"). Audited: bad token/stale generation (403, `_log`),
  unknown event (422, `_log`), PID mismatch (403, `_log`), illegal
  transition (409, `_log`). NOT audited: replayed/stale hook_seq and all
  other `BrokerError` 409s (the route returns `_err(409)` with no `_log`),
  unknown source / unknown fields / missing fields (422), no-session
  (404), session_start-without-pid (422). Replayed sequences are the
  spec-named category that matters most here — and the missing audit is
  exactly the diagnostic #50's out-of-order losses would need in
  production. One `_log` per rejection path fixes it.
- L1 broadened: the unguarded-`install()` crash class is wider than
  claude write errors. `_codex_merge` crashes the launch on valid-JSON
  non-dict `.codex/hooks.json` (`[]` → `cfg.setdefault` AttributeError)
  or a non-dict `hooks` value (`hooks.get` AttributeError), and its
  mkdir/`os.replace` OSErrors are likewise uncaught; `interface_exec`
  calls `install()` outside any try/except, so any of these kills the
  launch before exec — same contract violation, more realistic trigger
  (user/fork-owned hooks.json shapes). Same fix covers all branches:
  wrap the `install()` call in `interface_exec.main`, fail open.
- Correction to the #50 worst-case trace (cosmetic, conclusion stands):
  `interface_state.check` makes a same-state move an always-legal no-op,
  so an early turn_stop landing while lifecycle is idle returns 200
  (no-op), not "idle→idle illegal → 409". The lost event is still the
  earlier prompt_submit (rejected stale once the later seq commits);
  the stranded state is then batch stuck `submitting` holding the input
  lock (wake fence unanswered) — recovered only by startup reconcile or
  operator resolve_batch, same wedge class as flag #49's lost submission.
- Verified clean on re-trace: emitter content discipline (stdin never
  read; `</dev/null`; exact contract body asserted in test), record_hook
  duplicate-seq concurrency resolves benignly (same-state no-op
  idempotency + rollback-on-error), `_alert` dedupe via partial unique
  index + INSERT OR IGNORE (re-alerts after resolution, as designed).
