# Re-review — Sprint 21, Unit 7: Operator status + analytics integration (PR #464)

- **Reviewer:** REV1 · **Author:** DEV3 · **Sprint:** doc #21 · **Scope:** SC-465 fix only (flag #22)
- **Head re-reviewed:** `74ce1a3` (`fix(session): handle release without binding`) — all 6 checks green
- **Prior head:** `46b682b` (initial review: 1 Medium blocking, 7 Lows)
- **Verdict:** **review-clean.** The Medium is fixed, the regression test is a genuine
  guard, and the delta contains nothing else. Merge unblocked.

## What the fix does

One-line change in `cmd_operator_action` (`.super-coder/scripts/session_cli.py:168`):
`status.get("binding", {})` → `(status.get("binding") or {})`. The old form only
defaulted when the key was *absent*; a binding-less shell's status carries
`"binding": null`, so the chained `.get("state")` raised AttributeError before the
release ever reached the server. The null-safe form treats absent and null alike,
falls through the dispatching check, and POSTs the release — the server, not the
client, now decides what release-without-binding means.

## Verification traces (adversarial)

- **Regression test is a real guard** — ran `test_release_without_binding_reaches_
  server_for_both_wait_modes` against the *pre-fix* `session_cli.py` (46b682b): both
  subtests error with the exact reported AttributeError. At head: all 4 tests pass.
  The test asserts the full call sequence (status GET then release POST), exit 0,
  `print_status` on the release payload, and `sleep` never called — it would catch
  both a recurrence and an accidental wait-loop entry on null binding.
- **Both wait modes covered** — `release DEV3` and `release DEV3 --after-turn` via
  `subTest`; the `--after-turn` path matters because it's the branch that would
  otherwise loop on a misread binding state.
- **No sibling instances of the pattern** — grepped `get("binding"` at head: the only
  other access is `print_status` (line 60), which uses defaultless `.get` + a falsy
  guard with early return — already null-safe for both absent and null.
- **No scope creep** — the delta since 46b682b is exactly the 1-line fix + 23 test
  lines. Nothing else moved; the 7 Lows from the initial review are untouched by
  design (report notes, non-blocking, not re-litigated).

## Carried context

- Lows L1–L7 from the initial review (`sprint21-unit7-pr464.md`) remain sprint-report
  notes for the planner; none blocks merge.
- Flag #22 (SC-465) closed with verification notes at re-review.
