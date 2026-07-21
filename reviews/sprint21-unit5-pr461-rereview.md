# Re-review — Sprint 21, Unit 5: Codex app-server adapter (PR #461)

- **Reviewer:** REV1 · **Author:** DEV3 · **Sprint:** doc #21 · **Scope:** SC-462 fix only (flag #19)
- **Head re-reviewed:** `f9fa34c` (`fix(session): reject approval-gated managed wake`) — all 6 checks green
- **Prior head:** `34b44f2` (initial review: 1 Medium blocking, 7 Lows)
- **Verdict:** **review-clean.** The Medium is fixed at all three layers with regression
  tests proving no side effects. Merge unblocked.

## What the fix does

One shared predicate, `validate_managed_wake_posture` (`scripts/session_control.py`),
called at three chokepoints:

1. **`manage_session_control` (api/server.py)** — validates *before* any DB mutation,
   after the deliver/resume capability check; a prompting posture rolls back and
   returns the actionable error string. Arming never happens, so the wedge can't form.
2. **`CodexAdapter.deliver`** — first line, before the version-capability check and
   before `_endpoint`/client open. Defense-in-depth for posture drift between launch
   registration and wake (config edit + relaunch on an already-managed binding).
3. **`CodexAdapter.resume`** — first line, before the resume-capability check, the
   re-probe, and any child spawn.

## Verification traces (adversarial)

- **Exception classification in the dispatcher** — the key trap: the guard raises bare
  `SessionControlError`, and `ProviderBusy` is a *subclass* of it. Traced
  `session_dispatcher.py:539` — the dispatcher catches `ProviderBusy` first, then
  `Exception`; a posture error is not a `ProviderBusy` instance, so it lands in
  `finish_batch(..., error=exc)` → clean, actionable `last_error` through the normal
  retry-to-terminal path, with **no transport opened and no server-side turn left
  active**. The original wedge (protocol error + orphaned active turn) cannot occur.
- **Guard semantics** — launcher registration (`codex-session.py:139-147`) always
  writes `settings` with both `sandbox` and `approval_policy`. On the host path,
  unset config values register as `None`, which the guard **rejects** (keys present,
  `None != "never"`), i.e. the default prompting Codex config fails closed — the
  exact SC-462 scenario. Sandboxed launches register `danger-full-access`/`never`
  and pass. `approval_policy="on-failure"` is rejected unless full-access — stricter
  than my proposed minimum, and correctly so (on-failure can still prompt).
- **Fail-open early returns** — settings absent or neither key present → guard
  passes. Checked: at this head only the codex adapter registers session-control
  capabilities, and its launcher always writes both keys; other providers
  (claude/kimi/opencode/vibe) register nothing, so the generic guard in `manage`
  cannot false-reject them. Providers that later record these keys opt into the
  vocabulary; ones that don't are untouched. Acceptable generic contract — noted
  for units 4/6 reviewers, not a finding.
- **Tests** — `test_manage_rejects_approval_prompting_posture_without_mutation`
  asserts payload=None, exact error string, binding row unchanged
  (`dormant/managed=0/last_error NULL`), and zero wake jobs for the trigger message.
  Adapter tests assert deliver refuses with **no client opened** and resume refuses
  with **no probe and no child spawn**. `SessionControlError(RuntimeError)` makes the
  `assertRaisesRegex(RuntimeError, ...)` expectations sound.

## Carried context

- Ratified judgement (per DEV3's message): effort remains config-effective at launch —
  L3 stands as ratified, no code change. Lows L1–L7 remain report notes, non-blocking.
- Deferred-risk note from the initial review (live-verification coverage of status
  shapes) is unchanged and still lands with unit 8's provider smoke gates.
