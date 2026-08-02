---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE: Sprints v2.0 build — shard A (domain → watcher)

- **Sprint:** doc #51 — SPRINT: Sprints v2.0 build (all 10 units merged)
- **Spec judged:** #46 (feature 31), REV8-chain body via `sc mem get doc --doc 46`
- **Judged against:** `main @ cc33349` in ~/Repos/subfloor (synced; integrated whole, not diffs)
- **Shard:** A — spec top through the work-unit/dispatch material: domain model, lifecycle, arming/authority + preparation, participant conversations + pills + FnB entry, sprint messages/wakes/delivery budget, work units/dependencies/waves/dispatcher, PR registration + GitHub watcher. (Dev/review loop procedure, liveness policy, pause/recovery depth, close/report compiler = shard B.)
- **Reviewer:** REV1 (shell 7), sprint 51 unit C conformance slot (task msg #610)
- **Ratified judgement calls:** the kickoff carried none; every deviation below is therefore rated silent.

## Verdict: FAIL — 4 Major, 7 Medium, 14 Low

The engine internals are real and well-tested: lifecycle machine, arming transaction, single-armed invariant, conversation pointers, pill projection, message/wake store semantics, three-attempt budget with auto-pause, dispatcher lane rules, and the watcher are substantially as-specced (verified against code, with tests that genuinely assert). The failures cluster on one theme: **several spec-required capabilities exist only as store-layer functions reachable solely from tests — the shell-reachable surface (API/CLI) was never built for them** — plus two places where merged behavior contradicts the spec's letter.

## Major findings

### M1 — QAQC approval has no production write path (Preparation and QAQC) — `unimplemented`
Nothing outside tests ever INSERTs into `sprint_spec_approvals` (repo-wide grep, non-test: zero hits). The old QAQC table was dropped in `0144_remove_sprint_v1.sql:139`, and `sc mem doc qaqc` still POSTs to `/api/spec-qaqc-reviews` (`.super-coder/scripts/mem.py:790`) — an endpoint that no longer exists in `.super-coder/api/server.py`. The spec requires QAQC approval to be "a durable review record bound to reviewer, verdict, timestamp, findings, and exact spec revision" gating arming; on main the gate exists (`server.py:2001-2021`, arm re-check `sprint_domain.py:1107-1132`) but can never be fed — approvals can only be seeded by hand SQL. The entire preparation eligibility chain is unreachable in production.

### M2 — No shell-reachable sprint inbox / mark-read / decline surface (Sprint Messages and Wakes) — `unimplemented`
Read-equals-acceptance and decline semantics are implemented and trigger-enforced in the store (`sprint_message_delivery.py:152-174, 225-274`; `0148:21-60`) — and callable by no one. No endpoint in the `_sprint_post` dispatch (`server.py:2141-2351`), no `sc sprint` subcommand (`sprint_cli.py:232-353`), and the sprint skills instruct accept/decline without naming a command. The fixed wake prompt tells the shell to "mark the message as read," which on main is impossible for a `sprint_messages` row: actionable messages would sit `pending` forever, declines can never be recorded, and "a decline must never remain unread and wake forever" is untestable in production. The acceptance core of the spec is engine-only.

### M3 — PR merge observation auto-completes work units, including grant bypass (Work and Parallelism) — `deviated-silently`
Spec: "PR state never determines task completion by itself. The shell supplies the completion judgment; the system supplies the PR facts." Main: `observe_in_transaction` calls `observe_merge_in_transaction` on `merged` (`sprint_pr_watcher.py:432-437`), which runs `complete_from_merge_in_transaction` setting `disposition='completed'` with `"source": "pr.merge_observed"` (`sprint_domain.py:1444-1522`). Units that were never `merge_ready` emit a `merge.grant_bypassed` event and complete anyway (`sprint_domain.py:1483-1494`). Since the shell-judged completion path is itself unreachable (M4), merge observation is the *only* production completion path — PR state alone determines completion, the exact inversion of the spec sentence, and the merge grant's bar is advisory against any out-of-band merge.

### M4 — Report-only / no-code completion path unreachable and records no durable result (Work and Parallelism) — `unimplemented`
Spec: "A work unit explicitly planned as report-only or no-code may complete with a durable result rather than a PR." Main: no report-only/no-code marker exists on `sprint_work_units` (`0146:129-146`); the store's `complete()` (`sprint_domain.py:1397-1442`) has no API endpoint, no CLI command, and no production caller; and its `work_unit.completed` event payload is just `{"work_unit_id": …}` — no result artifact at all. Compounds M3: merge-watching is the sole way any unit can reach `completed`.

## Medium findings

### Md1 — QAQC signer is never verified to be a Review shell — `deviated-silently`
Declare (`server.py:2001-2021`) and arm (`sprint_domain.py:1107-1132`) check verdict/kind/feature/revision-freshness but never join `shells` on `reviewer_shell_id` or require `flavor='reviewer'`. Spec: "passed at least one QAQC round signed by a Review shell." Any shell id with a pass row qualifies. (Only reachable once M1 is fixed, hence not Major.)

### Md2 — "Every Medium/High/Critical QAQC finding resolved before approval" unchecked — `unimplemented`
`findings_document_id` is stored on the approval row and read by nothing (repo-wide, non-test: zero consumers). The resolution gate exists nowhere in code — only in `sprint_prep` skill text.

### Md3 — Preparation checks absent: GitHub access/worktrees, Planner fallback capacity — `unimplemented`
No GitHub probe at declare or arm; worktree resolution is `strict=False` (`sprint_participant_chats.py:344`). "Planner fallback capacity identified when another configured harness has usable quota" is not a preparation check — fallback is purely reactive at runtime (`sprint_liveness.py:509-548`). Both live only in skill text.

### Md4 — Planner cannot revise the plan: `replan()` has no production surface — `unimplemented`
Spec: "The Planner may revise waves, assignments, or dependencies as reality changes, provided already-completed history is not rewritten." The store enforces this correctly (`sprint_domain.py:1332-1377`: planner-only, planned-only, append-only before/after event) but no endpoint or CLI command reaches it. Post-creation, waves/assignments/dependencies are frozen in practice.

### Md5 — Sprint conversations are never closed; participant shells stick `browser_active` — implementation gap, spec-silent
`conversation_routes.py:955-959` refuses manual close with "Sprint conversations close only with Sprint lifecycle cleanup" — and no such cleanup exists anywhere (zero writers of `state='closed'` for sprint conversations). Consequence: `browser_conversation_shell_ids` (`run.py:607-613`, `WHERE state!='closed'`) flags every sprint-participant shell BROWSER/red in the CLI picker forever after its first sprint. Pill removal is satisfied (pure projection), but the conversation lifecycle the refusal message promises was never built.

### Md6 — Planner fallback context packet is write-only — `deviated-silently` (partial)
Spec: the replacement conversation "supplies a generated Sprint context packet." Main builds a good bounded packet (`sprint_participant_chats.py:396-464`) and stores it on the link row — and nothing ever reads it (repo-wide, non-test: zero consumers). The replacement session's transcript receives only the generic fixed wake prompt, so the packet is retained but never actually supplied. Adjacent (Low): fallback creation fires only from the liveness-escalation path; primary exhaustion without an escalation never triggers replacement.

### Md7 — FnB messages do not become Sprint messages — `deviated-silently`
Spec Terms define a Sprint message as a record "in the `sprint_messages` domain," and Participant Conversations states "An FnB message becomes a Sprint message and follows normal delivery policy." Main: an FnB POST creates a generic `conversation_messages` row in the Sprint conversation (`conversation_routes.py:1039-1141`) — it never enters `sprint_messages`, so it is invisible to sprint-inbox tooling and to any evidence compiled from the sprint message domain. Delivery behavior (queued behind an active turn, never injected) is as-specced; the domain placement is not.

## Low findings

- **L1 — Merge grant committed at declaration, not in the arm transaction** (`server.py:2046-2053`; locked immutable by trigger on leaving `prepared`, `0146:65-71`). Functionally equivalent to "committed at arming"; the spec's transaction enumeration says otherwise. Clarification candidate.
- **L2 — No explicit cross-sprint participation check** at declare/arm; the single-armed index makes it transitive. A shell may hold one armed + one paused + any prepared participations — consistent with the multi-participation pill rule (REV6), so likely intended; spec's preparation checklist reads stricter. Clarification candidate.
- **L3 — Fix conversation "carries the review notes" only via the inbox**: the fresh fix conversation's transcript contains only the fixed wake prompt; notes travel as the sprint inbox message (`sprint_review_loop.py:150-165`). Satisfies the spec only through the inbox read. Clarification candidate.
- **L4 — Pointer is read at claim time, not delivery time**, and the conversation-turn dedupe key is per-conversation while the target conversation is re-resolved at each claim (`sprint_message_delivery.py:482-518`, `sprint_runtime.py:58-83`): a pointer rotation between a crashed attempt and its reclaim can enqueue a duplicate native turn for the same wake — a narrow violation of "duplicate native turns may not occur."
- **L5 — Passivity is not explicitly recorded**: no column marks a message passive; it is inferable only from the absence of a wake-link row. Spec: "unless explicitly recorded as passive system evidence."
- **L6 — No wake retry backoff**: `record_wake_failure` never sets `available_at`, so a failed wake is re-claimable on the next 5s pulse; the `'coalesced'` attempt outcome in the CHECK constraint (`0146:262`) is never written by anyone.
- **L7 — Unknown GitHub check rollup states normalize to `created`** (`github_pull_requests.py:155` fall-through), not `pending` — an unrecognized non-failing state mis-files.
- **L8 — Closed-without-merge does not cancel an already-queued nudge wake** for the lane: future nudges/escalations are dead (`sprint_liveness.py:613-629`) but a committed pending wake still delivers (`sprint_message_delivery.py:490-492`). Spec silent on in-flight wakes; candidate clarification.
- **L9 — A live participant with a NULL conversation pointer 500s the whole `get_shells` endpoint** (`server.py:343`) instead of skipping the shell — unreachable today (arming provisions everyone) but one bad row takes down the shell list.
- **L10 — Deliver loop doesn't re-check armed between successive wake deliveries in one tick** (`sprint_runtime.py:200-204`): a mid-loop pause can still deliver already-claimed wakes.
- **L11 — `'cancelled'` work-unit disposition is dead code** — nothing sets it, and if one were set it would block dependents forever (only `'completed'` upstream unblocks).
- **L12 — A declined unit returns to `planned` with the same `assigned_shell_id`**; reassignment to a *different* shell has no mechanism (compounds Md4). Spec says "returns to the ready pool for reassignment."
- **L13 — Work-unit "role" is not recorded**: the assigned shell is hard-constrained to `role='developer'` (`sprint_domain.py:1276`). Clarification candidate — the spec lists role as part of the unit record.
- **L14 — Dead nested `_start_runtime_services`** at `server.py:3821-3828` (defined, never used, omits sprint_runtime); harmless, misleading.

## Verified as-specced (spotlights, not exhaustive)

Lifecycle transition map + DB trigger backstop, invalid-transition rejection (`sprint_domain.py:21-27`, `0146:73-83`); pause/abort/resume authority matrix incl. prepared-abort stub report (`sprint_domain.py:1066-1101`); terminal-wake-failure auto-pause via the same pause machinery with pause report (`sprint_domain.py:405-474`); armed-only gating of every poll/monitor/dispatch path (`sprint_domain.py:1835-1841` and re-checks in watcher/liveness/dispatcher); one-transaction arming with rollback tests and DB-only conversation placeholders (`sprint_domain.py:125-156`); single-armed uniqueness enforced inside the arm transaction by partial unique index (`0146:52-53`); merge-grant verification with live GitHub re-read + approved-head pinning (`sprint_review_loop.py:221-268`); full conceptual data model present with append-only triggers (0146); restart recovery of an armed sprint (`sprint_runtime.py:219`, `sprint_pr_watcher.py:265-269`); all-participant conversation provisioning at arming without harness launch (`sprint_participant_chats.py:319-362`); current-conversation pointer moved in the creating transaction with trigger backstop (0147:110-131); one-pill precedence armed > most-recently-paused (`sprint_participant_chats.py:367-390`); pill click-through to current conversation (`ui/app.js:4264-4281`); FnB entry to all participant conversations with side-effect-free viewing; terminal lifecycle removes the pill; message+wake atomic commit, verbatim fixed wake prompt, one-pending-wake coalescing (`0148:17-19`), at-least-once with stable idempotency, three-attempt budget with durable attempt evidence; dispatcher parallel-eligibility + one-editing-lane partial unique index (`0146:149-151`); reviewer-inheritance from owning unit (`sprint_review_loop.py:300-326`); watcher single-PR registration rejection (REV7), armed-only 5s pulse with zero idle calls, change-only append with stable transition identities, backoff without inventing state, first-observed red/green notified once, resume/restart dedupe, closed-without-merge → active Planner + review-expectation resolution (REV8), watcher never requests review, current-conversation routing at claim time.

## Notes for the Planner

- M2/M4/Md4 share one root: unit surfaces that were built store-deep but never exposed. A single "shell command surface" fix unit (API endpoints + `sc sprint inbox|accept|decline|complete-unit|replan`) closes three findings; the skills already assume these commands exist.
- M3 is a design contradiction, not a missing surface: either the watcher must stop completing on merge (shell completes, watcher supplies facts) or the spec sentence needs a Planner-owned revision recording the merge-observation rule as the intended judgment handoff. The `merge.grant_bypassed` event shows the builders saw the seam and chose completion; nobody declared it.
- M1 blocks everything in Preparation: until approvals can be recorded, every armed Sprint's eligibility chain is hand-seeded.
