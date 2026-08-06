Triple-reviewer reproduction during Sprint 2 close reconciliation on 2026-08-05:

- REV1, REV2, and REV3 each completed a normal Claude/Opus Sprint review turn successfully (runs 329, 330, and 331).
- The next Re-enter deliveries to the same three Sprint chats all terminalized `unknown` with `HARNESS_WORKTREE_MISMATCH: Claude session belongs to a different worktree` (runs 335–338 and recovery runs 340–341).
- All three active-registry rows then pointed at `state=error` chats with no process PID/start-ticks. `sc sprint monitor --sprint 2` returned `outcomes: []`, so ordinary Sprint liveness had no due recovery action despite all reconciliation reviewers being unusable.
- The browser conversation API rejects a shell Bearer token with `OPERATOR_REQUIRED`; the supported localhost operator form works only when the Authorization header is omitted.

Operational recovery used here: resolve the exact three registry chat ids and versions through read-only reporting/API reads, verify each was Sprint-scoped, `error`, and had no live PID, then PATCH only those exact chats to `state=closed`. All three close writes succeeded and preserved their transcripts. No reviewer messages were duplicated; unread Sprint-message recovery is left to create fresh chats.

Impact: one poisoned historical cwd observation can simultaneously strand every Claude reviewer after otherwise-successful turns, while the Sprint monitor reports no actionable liveness outcome. The UI symptom remains the generic “Turn outcome could not be proven.”
