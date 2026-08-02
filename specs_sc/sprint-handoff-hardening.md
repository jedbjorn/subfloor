---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: Sprint handoff hardening
tags: [sprints, handoff, recovery]
date: 2026-08-01
project: super-coder
purpose: Reliable model handoffs and recovery
---

# Sprint handoff hardening

## Objective

Harden the narrow seams that let Sprint participants relay work to one another: every shell must load its role skill, open the correct Sprint inbox, receive a durable message, and know exactly how to proceed at task start, handoff, question, blocker, command failure, pause, and stop. The dispatcher must expose the current Sprint command surface, delivered-but-unread messages must be recoverable, and model-authored payloads must stay within the existing storage contract and be confirmed written.

Done means an existing updated fork can move one assignment through Developer acceptance, a participant question and answer, review handoff, Reviewer verdict, Developer continuation, and recovery from one delivered-but-unread message without an operator supplying task-specific instructions.

> [!class1]
> The engine is a relay. It leaves the right durable trail, wakes the right shell, and lets the capable model use its role skill and judgment to do the work and pass it on.

## Governing posture

This spec implements decision #53 while preserving decisions #42 and #45:

- One general wake template guides every participant to the correct Sprint surface. It does not carry the task itself.
- One general participant send command carries questions, answers, blockers, and context. Meaning stays in the freeform message body; no protocol vocabulary or database message taxonomy is introduced.
- Existing typed commands remain authoritative for acceptance, decline, review, completion, pause, and other workflow transitions.
- The role skills, not the wake or database, state what a shell does at start, handoff, contingency, and stop.
- Capable models own execution judgment after they reach the durable message and role skill.
- Recovery restores the trail without accepting, declining, completing, reassigning, or otherwise deciding work for the model.
- Hard guards remain reserved for integrity and bounded payload contracts; completion and ordinary workflow judgment remain advisory.

## Failure summary

| Issue | Observed seam | Required correction |
|---|---|---|
| #897 | Generic wake text routes shells to the wrong inbox | Name the role skill and repeat the exact Sprint inbox command in one general wake template |
| #895 | A terminal `delivered` wake suppresses later dispatch even when its message remains unread | Reconcile message pickup, not delivery state alone |
| #894 | The 8,000-character payload ceiling is absent from model guidance and CLI help | Give shells a 6,000-character working target, a length check, the hard ceiling, and a success-confirmation stop rule |
| #873 | A fork can expose a stale `sc` dispatcher alongside a newer materialized engine | Make dispatcher and pinned engine one verified callable floor in existing forks and shell worktrees |

## General wake

Every participant wake uses one template. Only `sprint_id`, `role`, and `role_skill` vary:

```text
Sprint {sprint_id} handoff for your {role} role. Load `{role_skill}`. Run `sc sprint inbox --sprint {sprint_id}` now and act on the Sprint message(s) using `{role_skill}`. Confirm every Sprint write succeeds before stopping. If the handoff is not complete, load `{role_skill}` again and run `sc sprint inbox --sprint {sprint_id}` again.
```

Role resolution is deterministic:

| Participant role | Wake skill |
|---|---|
| Developer | `sprint_dev` |
| Reviewer, including conformance | `sprint_rev` |
| Originating Planner | `sprint_pln` |

`sprint_prep` is entered deliberately before arming, not by a participant wake. `sprint_pln` routes terminal synthesis into `sprint_close` when durable Sprint state calls for it.

The wake is a routing envelope, not a task payload:

- It includes no message IDs, work-unit IDs, feature IDs, PR numbers, SHAs, task excerpts, or transition-specific procedure.
- The durable Sprint inbox remains the only source for the work and evidence.
- The same template applies to assignments, ordinary participant messages, review requests and outcomes, notifications, nudges, escalations, Planner decisions, and recovery wakes.
- Coalescing may attach several durable messages to one wake; the plural inbox instruction remains correct without enumerating them.
- A retry of one wake identity retains byte-identical prompt text.
- The exact Sprint ID is rendered into both the opening phrase and command. Models never infer it from another identifier.

## Participant relay

Every Sprint participant can address any other participant in the same Sprint through one communication command:

```bash
sc sprint send --sprint <sprint-id> --to <shortname> --body-file <path> --key <stable-key>
```

The body says what the sender needs to say. A question is written as a question, a blocker names the blocker, an answer gives the answer, and context gives the useful context. The engine does not require prefixes, categories, or separate verbs for those meanings.

Relay contract:

- Sender and recipient must both be participants in the named Sprint.
- The message is durable and sender-attributed. It is communication only and does not change work-unit or Sprint state.
- The message write and recipient wake are committed together through the existing Sprint message and wake path.
- If the recipient has a usable current Sprint conversation, the wake is queued there. If a run is active, the wake follows that run in the same conversation instead of interrupting it.
- If the recipient has no usable current Sprint conversation, delivery creates a linked Sprint conversation, makes it current for that participant, and queues the same general wake there.
- The caller supplies one stable retry key for the intended recipient and exact body. An honest retry reuses that key; a changed recipient or body uses a new key.
- The success response confirms both the durable message and wake disposition. A local body file, attempted command, or shell narration is not proof of handoff.
- Existing typed commands that already produce a message and wake, such as review request and review outcome, continue to do so. The shell does not send a duplicate generic message for the same handoff.

No new message-kind enum, question table, answer table, routing workflow, or participant protocol is added. This is the missing callable surface over the relay the Sprint domain already owns.

## Pickup and recovery

`delivered` proves that the conversation transport accepted a turn. It does not prove that the participant opened the Sprint inbox or acted on its unread message.

Automated pickup recovery provides one bounded fallback episode. For every still-relevant unread Sprint message whose original wake terminalized, the armed or resuming Sprint must converge on at least one of these conditions:

1. A queued or running participant turn can reach the message; or
2. one deliverable wake exists for the participant.

Recovery behavior:

- Resume reconciles unread messages associated with `delivered` as well as `failed` wakes.
- If no queued or running turn can pick up an unread message, reconciliation creates a replacement wake with a stable recovery identity.
- If a participant turn is live but the flow is stalled, the existing reconciler may queue one deduplicated follow-up using the same general wake. It does not interrupt healthy work merely to restate procedure.
- At most one unresolved fallback wake exists for the same participant and recovery or silence episode.
- Repeated resume, startup reconciliation, or monitor evaluation is idempotent.
- If the stable fallback wake itself exhausts delivery, automated recovery stops. The unread inbox row and failed wake remain durable evidence for FnB recovery; the engine does not recursively recover a recovery wake.
- Acceptance, decline, completion, review, reassignment, and merge state are never inferred from wake delivery.
- Once the relevant message has been handled or is no longer relevant, recovery does not re-wake it.
- Recovery evidence records why a replacement was created and the prior wake and run state. Those identifiers remain system evidence and do not enter the model prompt.

No new workflow controller is introduced. This is a pickup repair at the existing message, wake, conversation, resume, and liveness seams.

## Payload discipline

The existing 8,000-character ceiling remains the hard backstop for every affected model-authored Sprint message or result body. The working target is about 6,000 characters or fewer so a capable model has useful headroom for final edits and encoding differences.

Every affected role skill places this guidance immediately beside `send` and each file-backed handoff command:

```text
Keep this Sprint message or result at about 6,000 characters or fewer; 8,000 characters is the hard maximum. Before submitting, run `wc -m < <path>` and condense if needed. The handoff is complete only when the Sprint command exits successfully and confirms the durable write and wake where applicable.
```

This applies at minimum to generic participant messages, Developer readiness and report-only results, Reviewer verdict and conformance bodies, and Planner close, report, and follow-up bodies that share the domain limit.

The instruction is guidance, not a formatter. A useful payload between the 6,000 target and 8,000 ceiling remains valid. When evidence is larger, the shell summarizes it and points to the durable artifact rather than pasting an oversized body.

CLI help names the hard ceiling for each affected file argument. Authoritative API validation remains in place, and an over-limit error reports actual and maximum character counts. No successful write is reported when validation fails.

## Role-skill operating contract

The role skills are the operational contract. Each of `sprint_pln`, `sprint_dev`, `sprint_rev`, and `sprint_close` places explicit runnable instructions at the point where the shell needs them:

| Situation | Skill instruction |
|---|---|
| Task start or wake | Load the role skill, run `sc sprint inbox --sprint <id>`, and inspect the durable message. Use `accept` or `decline` for actionable work. After acting on an informational message, run `accept` to mark only that message read; it does not change workflow state. |
| Question or ambiguity | Put the concrete question and needed decision in a short body file, use `sc sprint send` with one stable retry key to the participant who can answer, continue independent safe work, and stop at the decision boundary if the answer is required. |
| Incoming question | Answer through `sc sprint send` so the answer is durable and wakes the asker, confirm the write, then run `accept` on the handled question to mark it read. |
| Blocker or cross-unit problem | Developer or Reviewer sends the Planner concise evidence, impact, exact action needed, and a recommendation. The Planner decides whether to continue, re-plan, or pause. Do not invent workflow state or rely on narration. |
| Integrity threat | Developer or Reviewer reports evidence, impact, and a recommendation to the Planner and does not pause. The Planner evaluates the report and may run `pause`; Planner-owned `sprint_close` follows the same authority. |
| Command rejection or transport failure | Treat the write or handoff as incomplete and correct and retry when safe. If the relay itself fails, surface the durable evidence and failed command to FnB; do not build another recovery protocol. |
| No immediate response | Leave the durable message as the source; continue safe independent work or wait at the boundary. Unread recovery and the reconciler own re-waking, so the skill does not tell the shell to spam duplicates. |
| Normal handoff | Use the existing typed command for the state change, such as `request-review` or `record-review`; confirm its durable write and generated wake. |
| Task end | Re-run the Sprint inbox, act on newly arrived messages, mark every handled informational message read with `accept`, confirm the final typed handoff succeeded, and stop at the role boundary. |

Concrete skill text uses each command's actual required arguments and names the correct recipient for that role and situation. It may repeat the start and re-entry instruction. It guides the shell without prescribing how the model reasons, codes, reviews, or phrases its message.

## Dispatcher coherence

The callable `sc` dispatcher and the materialized engine at `.sc-state/engine.ref` form one versioned floor. An update must not report success while the dispatcher routes a shipped command to a missing engine script.

Required behavior:

- Existing forks update onto the current dispatcher without requiring a fresh install.
- Existing Planner, Developer, and Reviewer worktrees invoke the current Sprint command surface without dirtying their feature branches.
- `sc sprint -h` and `sc sprint inbox -h` resolve successfully from the main checkout and an already-existing linked shell worktree after update.
- Update or boot detects dispatcher and engine mismatch and names a supported repair; it never lets a participant discover the mismatch only after a wake.
- The engine pin advances only after the declared callable surface is coherent.
- Fresh install, update, rollback, and worktree boot share the same dispatcher ownership contract.

The implementation may choose the smallest compatible distribution repair. The contract does not require a new package manager, fork commit, or worktree-local engine.

## Construction plan

```linear
Dispatcher coherence :::class1 -> Participant relay + general wake :::class1 -> Pickup recovery :::class2 -> Live fork proof :::class3
```

Role-skill contingency and payload guidance is parallelizable with dispatcher coherence. Pickup recovery follows the relay and general wake so initial messages, replies, typed handoffs, and fallback turns exercise one prompt contract.

### Unit 1 — callable floor

Repair dispatcher ownership and materialization for existing forks and linked worktrees. Cover install, update, rollback, boot, and engine-pin verification as required by #873.

Gate: update a fixture containing the stale `sprint.py` route, retain an existing shell worktree, and prove both roots execute the current `sprint_cli.py` help surface without tracked-work dirtiness.

### Unit 2 — participant relay and wake trail

Expose `sc sprint send` as the one participant communication action over the existing durable Sprint message and wake path. Replace the fixed generic prompt with the general role-aware template, resolve the role skill from durable participant role, render the exact Sprint ID, and implement active-chat versus new-chat wake routing. Update spec #46 obsolete fixed-prompt wording.

Gate: any participant can send freeform text to any other participant in the same Sprint; the commit produces one durable message and one recipient wake; an active chat receives the follow-up; an absent chat is created and made current; all three roles receive the exact template; task and message identifiers never leak into it; a retry reuses identical prompt text and native-turn identity.

### Unit 3 — pickup repair

Extend reconciliation from transport failure to delivered-but-unread pickup failure while preserving the existing conversation and liveness authorities.

Gate: reproduce #895 for an assignment and an ordinary participant relay; resume creates one replacement when the old turn is terminal and the message remains unread, creates none while an adequate turn or wake exists, and repeated reconciliation stays idempotent. The replacement delivers the Unit 2 template.

### Unit 4 — role-skill contingencies and concise writes

Make every participant skill explicit about start, question, answer, blocker, worker integrity reporting, Planner-owned pause, command failure, typed handoff, informational-message read acknowledgement, inbox recheck, and terminal stop. Put the one generic `send` command only beside communication contingencies. Add the 6,000-character target, `wc -m` preflight, hard 8,000-character ceiling, and successful-write rule beside every affected file-backed command.

Gate: role-skill tests walk every row of the operating contract and assert real command syntax; Developer and Reviewer guidance does not teach pause; Planner and Planner-owned Close guidance owns the pause decision; handled informational messages use `accept` only to mark them read; no skill invents message categories or substitutes `send` for a typed transition; CLI help exposes the ceiling; 8,000 characters succeeds; 8,001 fails without changing work state and reports both counts.

### Unit 5 — real relay and handoff

Run the thin vertical proof in an updated downstream-style fork with pre-existing shell worktrees:

1. Developer wake loads `sprint_dev` and opens the correct Sprint inbox.
2. Developer accepts, sends a concrete question to the Planner, and continues safe independent work.
3. Planner receives the wake in its active chat, loads `sprint_pln`, opens the inbox, and answers through `send`.
4. Developer receives the answer, performs the assigned proof, and requests review through the typed handoff.
5. Reviewer has no active Sprint chat; the relay creates one, wakes it with `sprint_rev`, and the Reviewer records a verdict.
6. Developer receives the outcome through the general wake and performs the next skill-directed action.
7. A separate delivered-but-unread message is recovered without an operator supplying task details.
8. One near-budget message or result is length-checked and durably written before the shell stops.

Gate: durable messages, sender and recipient attribution, dispositions, wakes, active and newly created conversations, native turns, and command receipts establish the whole chain. Shell narration alone is not proof.

## Adversarial gate

The hardening fails review if any of these remains possible:

- A wake omits the exact role skill or Sprint inbox command, or names an alternative inbox surface.
- A prompt requires the model to infer Sprint ID from another identifier.
- A participant cannot send a plain freeform Sprint message to another participant through one documented command.
- A generic send changes workflow state, or a skill uses it instead of the authoritative typed handoff.
- A send writes the message without a recipient wake, reports success without durable evidence, loses the handoff when no active chat exists, or duplicates a durable message on an honest same-key retry.
- A role skill says only to ask or escalate without giving the runnable Sprint relay command and recipient rule.
- An original delivered-but-unread relevant message receives no bounded fallback attempt despite having neither a useful live or queued turn nor a deliverable wake.
- Repeated reconciliation creates an unbounded wake loop.
- A model stops after writing a local body file or after a rejected API call.
- An 8,001-character rejection first becomes discoverable only at the API boundary.
- A fork reports the current engine pin while its callable dispatcher targets a removed Sprint script.
- The solution adds protocol message categories, a second inbox, or hard process gates beyond delivery integrity, payload bounds, and existing authority invariants.

Focused tests run first. Final verification runs the Sprint suites, update, install, and worktree suites, `./sc render-check`, `./sc verify`, and the live downstream-fork proof.

## Non-goals

- Encoding task content, message IDs, PR data, or work-unit procedure in wake prompts.
- Defining ASK, ANSWER, BLOCKED, or other protocol words or database kinds for ordinary participant communication.
- Teaching models how to implement, review, merge, or synthesize beyond their role skills.
- Replacing typed lifecycle and work-unit commands with generic `send`.
- Automatically accepting, declining, completing, reassigning, or merging work during recovery.
- Raising or removing the 8,000-character domain ceiling in this pass.
- Adding a second inbox, orchestration state machine, worktree-local engine, or mandatory operator handoff.
- Treating every long-running turn as stalled or repeatedly waking healthy work.

## Issue closure

| Issue | Closure evidence |
|---|---|
| #897 | General role-aware wake test plus real participant relay and Developer/Reviewer handoff |
| #895 | Delivered-unread resume and stalled-flow fallback tests plus live recovery |
| #894 | Role-skill budget, preflight, and write-confirmation text plus CLI boundary tests |
| #873 | Existing-fork and existing-worktree dispatcher coherence proof |


