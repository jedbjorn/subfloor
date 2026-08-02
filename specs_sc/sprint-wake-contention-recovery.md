---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: SHELL_BUSY wake contention recovery
tags: [sprint, wake, broker, defect-fix]
date: 2026-08-02
project: super-coder
purpose: Busy-shell wakes retry, then escalate
---

# SHELL_BUSY wake contention recovery

## Overview

> [!class4]
> A Sprint wake turn that lands while the target shell's session slot is held fails as **"Turn failed — SHELL_BUSY"** and, after one automatic repair attempt, strands silently: no retry, no Planner notice, no pause. Observed live in dos-arch Sprint 3 — REV2's Reviewer handoff failed and needed a human Retry in the GUI. The sprint is paused pending this fix.

The engine's per-shell one-session slot is working as designed; what is missing is a **contention class** between "delivered" and "failed". A busy slot is transient by nature (harness teardown lag, a lingering CLI session, an orphan process) — the machinery must retry it durably and escalate to a human only when retries exhaust, per decisions #53 and #60.

## Defect chain

Four seams compose into the strand; each is individually defensible, the composition is the defect:

```linear
Wake enqueued, marked delivered :::class1 -> Broker launch refused SHELL_BUSY :::class4 -> Run terminal-failed, no requeue :::class4 -> One-shot repair, then silence :::class4
```

1. **Delivery is just an enqueue.** `sprint_runtime.enqueue_conversation_turn` inserts a queued conversation turn; the wake outbox row goes to `delivered` and the three-attempt/auto-pause budget (`sprint_domain.record_wake_failure`) disengages permanently. The actual dispatch happens later, in the conversation broker.
2. **The broker refuses busy slots pre-dispatch.** `conversation_launch.py:91-122` snapshots liveness, drains for 2s (40×0.05s), then raises `ConversationLaunchError("SHELL_BUSY", …)` — correct one-slot enforcement.
3. **Every launch-preparer exception is a proven terminal failure.** `conversation_broker.py:1441-1443` → `_finish_error(…, proven=True)` → run `failed`, `error_code='SHELL_BUSY'` durably recorded (`conversation_broker.py:1318-1329`). No retry channel exists in the broker.
4. **The stranded-wake reconciler repairs exactly once.** `_reconcile_unread_wakes_in_transaction` (`sprint_domain.py:925`) creates one recovery wake per original — recovery-keyed wakes are excluded from the scan (`:949`), and a prior recovery in a non-pending state hits `continue` (`:971-973`). A second consecutive SHELL_BUSY (5s pulse apart, against a 2s drain window) strands the wake with no event trail past `wake.requeued` and no escalation.

## Design

**Decision: SHELL_BUSY at a wake turn's launch is transient slot contention, not a fault.** Contention is retried durably with backoff at the sprint reconciliation seam; exhaustion escalates Planner notice → pause. The broker's fail-fast behavior is **unchanged** — for human browser turns an immediate refusal plus the GUI Retry affordance is the right UX, and decision #60 names the durable outbox machinery, not the broker, as the sole coordination authority.

Mechanics:

- **Classification.** The reconciler reads the failed pickup turn's run row; `error_code = 'SHELL_BUSY'` selects the contention path. Everything else keeps today's once-only repair semantics, byte-for-byte.
- **Chained recovery with backoff.** Contention recovery wakes carry an attempt ordinal in the idempotency key (`sprint-recovery:{sprint}:busy:{orig_wake}:{n}`), and each new wake's `available_at` is pushed by an escalating schedule — **15s, 60s, 180s, 300s** — before `claim_next` will lease it (the `available_at <= now` filter already exists; no delivery-side change).
- **Budget.** `WAKE_CONTENTION_ATTEMPTS = 5` (module constant beside the schedule; both FnB-adjustable defaults, not new config surface). Chain length is derived by counting durable outbox rows on the key prefix — restart-safe with **no schema migration**. If counting proves awkward in implementation, a migration adding an explicit attempt column is acceptable; the dev decides, this spec permits both.
- **Event trail.** Every retry emits `wake.requeued` extended with `{classification: "shell_busy", attempt: n, backoff_seconds: s}` — **exactly one event per chain link**.
- **Event/relink dedupe (required).** Today the reconciler re-adopts a pending recovery wake on *every* 5s pulse and unconditionally re-links and re-emits `wake.requeued` (`sprint_domain.py:971-985`). With `available_at ≈ now` that is 1-2 duplicates; with 15-300s backoffs it becomes up to ~60 duplicate events per chain link, burying the very trail this spec promises. The reconciler must **skip both the event and the relink when the adopted recovery wake is already linked to the message set and unchanged** — dedupe keyed on the (original wake, recovery wake) pair. This applies to the existing non-contention path too; fixing it there is in scope.
- **Escalation.** Exhaustion pauses the sprint via the existing `_pause_in_transaction` with reason `wake_contention_exhausted` (mirror of `wake_delivery_exhausted`) and a Planner notice naming the shell, the attempt count, and the last observed slot state (`busy` vs `orphan`, from the run's `error_detail`) — an orphan verdict tells the FnB the remedy is killing a pid, not waiting.
- **Decision #53 fit.** #53 bounds automated recovery to one fallback episode before human escalation. The bounded chain **is** one episode: a single contention incident retried within a fixed budget, terminating in exactly one human escalation (the pause). No retry spawns further automation, and no chain restarts after exhaustion. Conformance should read the chain as the episode, not each attempt as one.

## Alternatives rejected

- **Broker-side deferred relaunch** — would change dispatch semantics for every conversation turn and duplicate the outbox's job; the broker has no durable deferral channel and decision #60 forbids growing one.
- **Pre-enqueue liveness gate in wake delivery** — racy (the slot can be taken in the check-to-launch gap), so the retry chain is needed anyway; may be added later as a cheap short-circuit, out of scope here.
- **Lengthening the 2s drain window** — mitigates only the teardown race, not orphan-held slots, and stretches every browser turn's latency floor. The retry chain covers both causes.

## Edge cases

- **Orphan-held slot** — never frees; budget exhausts in ~9.5 minutes worst case → pause + notice carrying `orphan`, pointing the FnB at the pid remedy (`shell_liveness.py --text`).
- **Human Retry succeeds mid-chain** — the message gets picked up and read; the reconciler's scan filters on `m.read_at IS NULL` and `_participant_has_pickup_turn`, so the chain stops naturally. A still-pending recovery wake for a read message is unclaimable (the claim query joins on unread messages) — inert, consistent with existing delivered-unread handling.
- **Recovery wake's own turn hits SHELL_BUSY** — re-enters classification; the chain continues to the budget. The `:949` recovery-key exclusion is narrowed to non-contention keys.
- **Engine restart mid-chain** — attempts and `available_at` are durable outbox rows; the chain resumes exactly where it stopped.
- **Pause/resume interplay** — the resume-time reconcile (`sprint-resume:` keys) composes: key namespaces stay distinct, and a paused sprint's reconciler does not run (armed-only switch), so no chain grows while paused.
- **Concurrent wakes for one participant** — the existing `pending/delivering` dedupe (`sprint_domain.py:954-958`) already collapses onto one deliverable wake; unchanged.
- **Resume after exhaustion (ruled during Sprint 86 review — SC-051 / flag #139).** A human `resume()` on a sprint paused with `wake_contention_exhausted` is the post-escalation human act, and it **resets the episode**: the stranded recovery wake is re-delivered (`pending`, `available_at = now`) as the human-sanctioned retry, with a fresh contention budget. Resume must NOT re-classify the durable stale evidence of the exhausted chain (failed run row + prior recovery rows) into an instant re-pause — that made the escalation notice's own orphan remedy (kill the pid) incapable of unblocking resume. This is consistent with decision #53: the exhausted chain was one automated episode ending in one human escalation; the human's resume begins a new episode, which again terminates in at most one further escalation if contention persists. Implementation constraint: chain-length derivation must not count prior-episode rows against the new budget — distinguish episodes in the idempotency key (e.g. an episode ordinal) or take the spec's already-permitted explicit attempt-column migration; the dev decides. `resume()`'s return value must reflect the true outcome (no `changed=True` on a sprint that ends paused).

## Verification

- **Unit** (`tests/test_sprint_recovery.py`, `tests/test_sprint_v2_domain.py`): SHELL_BUSY-failed pickup turn produces a chained recovery wake with the correct key ordinal and `available_at`; chain stops when the message is read or a pickup turn exists; attempt 5 exhaustion pauses with `wake_contention_exhausted` + Planner notice carrying shell and slot state; a non-SHELL_BUSY failure keeps today's one-shot behavior; restart mid-chain resumes the count from durable rows.
- **Delivery** (`tests/test_sprint_message_delivery.py`): `claim_next` refuses a backoff wake before `available_at` and leases it after.
- **Dedupe** (`tests/test_sprint_recovery.py`): repeated reconciler pulses across one backoff window emit exactly one `wake.requeued` per chain link and perform no redundant relinks.
- **No broker tests change** — the broker is untouched.

## Out of scope

- The 2s drain window constant and the browser-turn UX on SHELL_BUSY — unchanged by design.
- Liveness-monitor interplay (silence episodes, decision #42) — the pause escalation here is independent and earlier; reconciling the two escalation clocks is future work if they ever double-fire.
- The premature-green rollup defect — sibling spec, same feature.

