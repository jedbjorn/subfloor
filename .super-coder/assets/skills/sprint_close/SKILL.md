---
name: sprint_close
description: Route Sprints v2 closeout to the owning role — Reviewer conformance, Planner control actions or completion receipt, and explicit FnB fallbacks — without creating a second close workflow.
category: workflow
common: false
---

# sprint_close — route the terminal boundary

Use as a closeout router, not as a second operating workflow. The Reviewer owns
conformance and final-report judgment; the Planner executes Reviewer control
decisions; clean conformance closes atomically; the FnB owns explicit fallback
and follow-up disposition.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment within them. Repeat a read only when later activity could have
changed it or the next command requires live revalidation.

## Route the entry

- **Reviewer receives `sprint.delivery_terminal`.** Load `sprint_rev` and follow
  **Delivery-terminal closeout**. Inspect the Sprint inbox once, compile bounded
  evidence, and choose between in-Sprint re-entry, abort, or the atomic clean
  `record-conformance` path. The Planner does not initiate this pass.
- **Planner receives a Sprint-scoped Reviewer decision.** Load `sprint_pln` and
  follow **Reviewer decision actions**. Inspect and handle the durable inbox
  message once, then execute the exact requested transition without
  re-adjudicating it.
- **Planner receives an engine-wide completion or cleanup receipt.** Load
  `sprint_pln` and follow **Conclude or abort**. Inspect the self-contained
  receipt and terminal Sprint state directly. The completion receipt leaves
  cleanup pending; the later cleanup receipt makes reuse or recovery explicit.
  Do not run the Sprint inbox, accept the receipt, compile another report, or
  run `complete`.
- **FnB directs a fallback or follow-up disposition.** Use the bounded surfaces
  below and name FnB authority in the evidence.

Assignments and review requests use Force-new delivery; role results use
Re-enter. Neither displaces a live turn; the runtime owns delivery, rotation,
and recovery. A successful typed handoff is the last action of that role's
turn.

## Authority boundary

The Reviewer decides whether evidence warrants re-entry, pause, re-plan,
cancellation, abort, or clean completion. The Planner executes control
decisions. The Reviewer's clean `record-conformance` command is the one narrow
exception: it stores conformance, findings, and the Reviewer-authored final
report, completes the Sprint, and publishes the informational Planner receipt
atomically. FnB retains the board-level override from decision #46.

Any successful completion automatically closes other active participant chats
immutably linked to that Sprint while retaining the originating Planner and the
report-authoring Reviewer. Do not manually close peer chats as an extra
closeout step. Pause, abort, re-entry, failed conformance, and rejected fallback
completion never invoke this cleanup.

Successful close schedules exact participant-worktree and Sprint-artifact
cleanup. No role manually resets those targets after completion. The initial
Planner receipt reports pending cleanup; the System later sends succeeded or
failed cleanup evidence. A failed receipt names the bounded status and retry
commands.

If a command rejects a decision, preserve the returned durable state. Do not
substitute another transition or invent an alternate handoff. Return the
conflict to the deciding role, or surface it to FnB when the relay itself is
unavailable.

## FnB-directed fallbacks

The participating Reviewer normally compiles the bounded packet. Planner or
FnB compilation is valid only when FnB explicitly directs it:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Raise the limit only when truncation counters omit needed evidence; 200 is the
maximum. The packet supplies facts, not judgment. The standalone `complete`
surface is likewise an FnB-directed recovery fallback, never the normal clean
close path.

FnB may inspect any completed Sprint's bounded cleanup state, retry failed
targets, or explicitly adopt exactly one historical completed Sprint that has
no targets. Target identities are System-derived; never supply a path or add a
confirmation handshake:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
sc sprint cleanup --sprint <legacy-id> --adopt-legacy \
  --key <stable-adoption-key>
```

Require the cleanup request id, `created`, action, exact target ids, and
aggregate projection. Reuse a key only for the identical request.

Abort remains a Planner action on a Reviewer decision or FnB override and
deletes nothing:

```text
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

After closure, FnB records one disposition per pending follow-up. `accepted`
acknowledges ship-as-is; `resolved` and `dismissed` require a bounded resolution
file.

```text
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition accepted
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition resolved --resolution-file <path>
```

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Stop

After routing, continue in the owning role skill. Stop when the typed handoff or
terminal receipt has been handled. Do not perform a second close action or
duplicate another role's report.
