---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: "QAQC: spec #46 rev3 — REV2 round"
tags: [sprints, qaqc, review]
date: 2026-07-31
project: super-coder
purpose: Independent QAQC verdict on Sprints v2.0 spec (REV3)
---
# QAQC: spec #46 rev3 — REV2 round

- **Reviewed artifact:** spec doc #46 "Sprints v2.0 — Collaboration Loop", REV3 banner revision ("resolves SC-025 by making both red and green registered-PR transitions active Developer wakes through the watcher-backed conversation path").
- **Reviewer:** REV2 (shell #8), independent round for SPRINT 51 phase 0b (msg #420). Not coordinated with REV1's parallel round.
- **Method:** every line of the spec's own `## QAQC Checklist` challenged against the body, plus free adversarial reading for Medium+ issues. Findings-only round; no spec edits.

## Verdict: FAIL

Three Medium findings stand (flags #64, #65, #66). Per the spec's own Preparation and QAQC section, all Medium+ findings must be resolved before approval and before the feature moves to active construction.

## Medium findings (blocking)

### M1 — flag #64 · Watcher wake target contradicts fresh-conversation topology
- **Checklist line:** "Whether every newly observed red and green registered-PR state occurrence actively wakes the owning Developer **through its conversation**" and "Whether the conversation topology (persistent dev lane, fresh review-outcome chats) preserves both context and token economy".
- **Conflict:** GitHub Observation + Dev loop steps 3–4 bind the watcher's active red/green wake to the Developer's **persistent** conversation. Step 6 moves the Developer into a **fresh fix conversation** carrying review notes, then says it "waits for the watcher to wake its conversation on the next red or green transition" — pronoun unresolved. Step 7 puts the merge in a **fresh merge conversation**; step 9 then has the Planner assign the next unit "in the Developer's same conversation" — same as which? Meanwhile "the participant pill always points at the current one" without defining "current" mid-fix-loop.
- **Why Medium:** two defensible readings produce divergent implementations of a stage-5/stage-6 gate behavior (where the wake lands, where the pill points, where the next unit is assigned). One reading strands red/green wakes in a lane the Developer has left; the other contradicts the written watcher contract. Spec is silent where it must pick.

### M2 — flag #65 · Decline semantics undefined under read-equals-acceptance
- **Checklist line:** "Whether the fixed wake, sprint_messages acceptance, decline, coalescing, and three-attempt budget avoid duplicate or endless turns".
- **Conflict:** the domain defines exactly one terminal signal — marking an actionable message read means acceptance. The decline path requires the shell to record a reason, send a result, and "resolve the actionable message... It must not remain unread and wake forever." No resolution state distinct from read/unread is named anywhere; the conceptual data model exposes only "read-equals-acceptance semantics", and Deterministic Capture records only "read state".
- **Why Medium:** decline is a first-class flow (it returns the unit for reassignment) but its durable representation is unspecified. An implementer must invent either a second state, a flag, or a convention of read-with-decline-note — each with different acceptance-audit meaning, and each a silent divergence risk between independently built units.

### M3 — flag #66 · Lifecycle transition authority gaps: abort and prepared-exit
- **Checklist line:** "Whether the lifecycle has one unambiguous authority for every transition, including system auto-pause".
- **Conflict:** the Roles table grants the FnB "pause, resume, or abort"; "any participating shell may pause"; "Only the Planner or FnB resumes." No sentence names an authority for `armed → aborted` or `paused → aborted` — Planner abort authority is neither granted nor denied. Additionally the lifecycle graph gives `prepared` no outgoing transition except `armed`: a declared Sprint that is never armed cannot be cancelled within the lifecycle.
- **Why Medium:** the checklist demands exactly one authority per transition; two transitions lack one. Ambiguous abort authority invites either duplicated authority (Planner and FnB both assuming) or a stall (neither acting) — both failure shapes the spec's own Design Principles forbid.

## Low findings (recorded, non-blocking)

- **L1 — "accepted active work expectation" undefined (Liveness).** The monitor evaluates "each accepted active work expectation", but Work and Parallelism states review assignments are *not* work units. Whether an accepted review request is a monitored expectation is unreadable as written; the narrow reading leaves a stalled review — the classic chain-staller — outside nudge/escalation coverage.
- **L2 — Ambiguous fallback line in Liveness policy.** "Planner escalation uses an eligible configured fallback harness when the primary provider is exhausted" sits in the worker-silence policy; it is unclear whose provider (silent worker's? Planner's?) and what is being booted on the fallback. Read as waking the worker on a fallback, it contradicts the one-nudge limit and the no-cross-harness-resume stance.
- **L3 — "shown in the board" vs Non-Goals.** Work and Parallelism says a planned wave "is shown in the board", and stage rows reference board updates, while Non-Goals defers "The Sprint board web GUI" as future scope. If the board is out of scope, these display claims are dangling; if a minimal board exists, the Non-Goal needs scoping language.
- **L4 — Decline text assumes a work unit.** "leaves the work unit available for reassignment" does not map onto review assignments, which the spec explicitly excludes from work units. Reviewer-decline disposition is unwritten (minor sibling of M2, not folded in because it is fixable with one sentence).
- **L5 — Whole-Sprint auto-pause breadth.** Any single wake's terminal failure (including a nudge) auto-pauses the entire Sprint. It is loud, not silent, so it passes the spec's own bar — but "Partial failure is acceptable" invites a scoped alternative (pause the lane, escalate). Noted as a design-tension the Planner may ratify rather than a defect.

## Checklist lines challenged and found sound

- **Red/green first-observation wake (SC-025 fix):** sound. First snapshot after registration notifies an already-red/green state once; resume/restart reconciliation notifies only on divergence from the last durable observed state; stable transition identities + coalescing make retries safe. The REV3 change is internally consistent and matched in Adversarial Acceptance.
- **Nudge-before-escalation:** sound — 10-minute grace, exactly one nudge per silence episode, one deduplicated Planner escalation after a further 10 minutes; "does not repeatedly boot the worker" is explicit.
- **FnB entry without liveness impact:** sound — "merely opening or viewing a conversation does not count as shell activity and does not wake the shell"; queued-behind-active-turn delivery is explicit.
- **Armed-only zero idle calls:** sound — watcher performs no Sprint GitHub requests in any non-armed state; five-second pulse only while armed.
- **Single ownership of PR/QA state:** sound — watcher is the sole writer of PR state ("Developers never flip PR state"); QAQC approval is a durable record bound to reviewer/verdict/revision, "never a copied boolean on the Sprint row"; duplicated PR state and derived booleans are explicitly banned from the data model.
- **Merge grant:** sound — committed at arming, scoped to the Sprint's registered PRs only, bar is passed-review-with-Medium+-addressed; hard-dependency sequencing means no stacked-PR merge gap.
- **Mid-Sprint spec edit:** sound — bound revision persists, edit surfaced as evidence, resume unblocked, approval ineligibility gates new arming only.
- **Cross-harness Planner fallback:** sound — linked replacement conversation + generated context packet, explicitly no native-session portability pretense; earlier conversations remain enterable.
- **Five skills:** adequately enumerated (`sprint_prep/pln/dev/rev/close`) with contracts-vs-procedure split stated; severity rubric deliberately deferred to `sprint_rev` and excluded from this spec.
- **Verification gates:** every stage 1–10 has an independently executable gate; stages 2–3 thin-vertical-first ordering is explicit; stage 10 demands real-browser, real-GitHub proof.
- **Hard guards:** remaining guards (one active unit per shell, fixed wake prompt, three-attempt budget, armed-only services) each protect a stated invariant (editing lane, idempotency, silent-wedge prevention, idle cost) rather than constraining judgment.
- **Three-attempt budget / auto-pause mechanics:** sound as a mechanism (terminal failure is durable, recorded, and escalated via pause) — breadth noted as L5 only.

## Required before approval

1. Resolve M1: name the wake-target and "current conversation" rule for the fix/merge loops (and step 9's "same conversation").
2. Resolve M2: define the durable decline/resolution state for actionable messages and its audit meaning.
3. Resolve M3: name the abort authority for `armed/paused → aborted`, and either add a `prepared` exit transition or state that prepared Sprints are abandoned, not aborted.

Per spec, any edit to the approved spec body creates a new revision; this QAQC record binds to the REV3 banner revision only.
