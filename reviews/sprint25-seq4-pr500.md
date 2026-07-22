# Review — sprint 25 seq 4 · PR #500 (feat/interface-session-schema @b393e3c)

Reviewer: REV1 (Kimi) · spec #20 task #80 · author: DEV4 · date: 2026-07-22
Verdict: **NOT clean — 2 Major, 3 Medium, 4 Low.** Major/Medium block (sprint bar).

Scope reviewed: migration 0078 + schema.sql convergence, two-layer transition
validators, broker two-phase input path, startup reconciliation + crash-window
parking (decision #22), snapshot projection, rebuild/update refusal, 5 test
files. DEV4's 4 ambiguity calls were ratified by the planner (decision #23) and
were NOT re-flagged. All findings verified by tracing code and by hermetic
repro against the PR's real schema+migrations (not just reading tests).

## What passed adversarial review

- **Schema/uniqueness** — all seven invariants present and correct: one
  non-ended session/shell (partial index covers `unreconciled`, so a lost
  generation blocks New chat — correct), one live generation/shell, one current
  writer/session, one live batch/binding, one unreleased binding per planner
  and per sprint, unique (binding,message), idempotency PK
  (actor,operation,key). Migration 0078 and schema.sql are convergent
  (tables, CHECKs, triggers, indexes all match; migration-only
  `sprint_doc_id` columns follow the 0047/0059/0062 precedent with pointer
  comments).
- **Two-layer validators** — `interface_state.py` edge maps match the 0078
  triggers edge-for-edge on all seven machines; edges conform to the spec's
  Occupancy Model plus the ratified additions. `test_interface_transitions.py`
  genuinely walks every (old,new) pair of every machine against BOTH layers
  (app layer + direct trigger replay on a fresh row), states derived from keys
  ∪ targets so no CHECK-listed state is skipped.
- **Crash-window parking (decision #22)** — the core proof holds: pending
  commit BEFORE the write, forwarded commit AFTER; crash before/after the
  tmux write parks identically (composer unknown, delivery_unknown, writer
  revoked, critical alert, pending_seq kept as evidence); reconciliation is
  idempotent with zero re-forwards (no resend path exists — verified);
  `reconcile_input` delivered/not_delivered paths behave as claimed;
  submitting/running batches park as delivery_unknown without durable hook
  stamps; `resolve_batch` requeues items byte-free. BUT see M1/F33 — the
  hook evidence itself is unfenced, which weakens what the stamps prove.
- **Snapshot projection** — volatile tables excluded from PER_INSTANCE_TABLES;
  SENSITIVE_COLUMNS redacts socket/PIDs/start-ticks/hook-hash (tested with
  canary values); row filters keep live rows out; a drift guard executes every
  filter against the live schema.
- **Rebuild/update refusal** — `live_refusal_reasons` covers non-ended
  sessions, unreleased bindings, nonterminal batches (incl. delivery_unknown),
  and input ambiguity; wired into `rebuild.main` (after backup, before any
  file deletion — outgoing DB still intact) and `update.migrate_or_rebuild`;
  pre-0078 DBs tolerated; integration tests assert SystemExit.

## Findings

### M1 — Major · flag #33 · prompt_submit hook is unfenced; no input lock
`interface_broker.record_hook` (`prompt_submit`) clears dirty unconditionally
and moves ANY submitting batch to `running`, without binding the hook to the
batch's `input_seq_fence` and without checking whether a later human sequence
was accepted. The spec requires a *fenced* submit callback ("clean only if no
later human input sequence was accepted"; "prompt_submit: record accepted
input sequence" — the hook contract here carries no input sequence at all).
Repro (hermetic, real schema): wake submits with fence 1 → human frame
accepted (dirty) → prompt_submit hook arrives → composer back to `clean`,
batch `running`. The wake gate is now open over a live human draft — the
interleaved-terminal-bytes failure class the done condition forbids. The
same gap lets a human's own Enter promote a submitting wake batch to
`running`, manufacturing the "durable hook evidence" decision #22's recovery
trusts. Relatedly, the docstring claims submit runs "under the input lock"
but no lock exists: a human frame committing between the submitting-commit
and `writer()` neither cancels the attempt (spec: human ordered first
cancels) nor is blocked. Fix direction: hook payload carries the accepted
input seq; clear dirty / promote the batch only when the fence matches and
no later human seq was accepted; define the lock/queue ownership explicitly.

### M2 — Major · flag #34 · fresh-lease sequence namespace break
`acquire_writer` reseeds `next_input_seq=1` while duplicate detection is
session-scoped (`client_seq <= forwarded_seq`). After any forwarded frame
(N>0), a fresh lease (takeover, reconnect, post-park resend) breaks: a client
continuing session numbering sends N+1 → permanent gap rejection (wedge); a
client reseeding to 1 sends new bytes → false duplicate-ack, never forwarded
(silent loss). Repro confirmed both arms. This also undercuts the PR's own
"client resends under a fresh lease" recovery (flag #22's not_delivered
path) for every case except forwarded_seq=0 — the crash-window test covers
only that degenerate case. Fix direction: reseed the new lease's
next_input_seq from the session's forwarded_seq+1 (session-scoped
numbering, matching "reconnect presents its last acknowledged sequence"),
and extend the resend test to forwarded_seq>0.

### M3 — Medium · flag #35 · failed tmux write wedges the input path
`accept_human_input` does not handle `writer()` raising without process
death (tmux error, broker alive): pending_seq stays set, delivery stays
normal, no alert, and every subsequent frame is rejected "wait for its ack"
until a service restart happens to reconcile. Repro confirmed. Fix: treat
like the crash window — park (composer unknown, delivery_unknown, revoke
writer, alert) on write failure.

### M4 — Medium · flag #36 · interface_generations escapes both guards
No snapshot row filter for `interface_generations` (live rows serialize —
hook hash redacted but the row ships), `live_refusal_reasons` never checks
generations, and nothing in the unit sets `generations.ended_at`. After a
drained chat, a rebuild restores a "live" generation row and
`idx_interface_generations_live` blocks that shell's next generation insert
— New chat bricked post-rebuild with no operator path out. Fix: end the
generation in the drain/session-end path AND filter live generations out of
the snapshot (mirroring sessions), or add generations to the refusal guard.

### M5 — Medium · flag #37 · submit gate omits spec'd revalidations
`submit_wake_batch` revalidates occupancy/lifecycle/composer/pending/quiet/
generation but not the binding/sprint: a sprint close between `form_batch`
and submit leaves a queued batch that still submits (spec: wake requires an
armed, unreleased, ACTIVE sprint binding at submission). Also no
post-restart debounce: NULL `last_human_input_at` skips the quiet check
entirely, while the spec requires every otherwise-clean generation to wait a
fresh full debounce after service restart. `quiet_s=0` is accepted though
the spec forbids zero.

### Lows (notes for the sprint report — not gates)
- L1: `certify_clean` records any client_id without verifying the caller
  holds the current writer lease (spec: "the *writer* may use certify-clean").
- L2: The stop-hook recovery branch (`running` + stop_hook_seq → complete) is
  unreachable as shipped: `record_hook turn_stop` stamps and completes in one
  atomic commit, so no code path leaves a running batch with a stop stamp.
  Two crash-window tests hand-craft the state. Either stamp-then-complete
  (two-phase, matching the input path's discipline) or drop the branch.
- L3: `sprint_planner_bindings.shell_id` is denormalized ("= planner_shell_id")
  with no CHECK; divergence would fence the wrong generation.
- L4: 3-completed-wakes quarantine and the "unread with durable ambiguous
  action → reconcile" branch are not wired in `_complete_batch`; receipts
  exist but are never consulted. Believed to be task #84 scope — planner to
  confirm the handoff point so it doesn't fall between units.

## Process notes

- Repro script used for M1–M3 verification: hermetic, real schema.sql + full
  migration chain, injected recording/failing writers (same technique as the
  PR's own crash-window tests).
- Full suite not re-run (CI green; review verifies, doesn't re-execute).
- Handoff: findings → DEV4 direct (sprint-scoped authority), planner copied.
