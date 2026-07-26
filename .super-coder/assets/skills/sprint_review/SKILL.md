---
name: sprint_review
description: Execute an assigned sprint unit review or the close-time conformance pass. Review the requested exact head, test high-value properties adversarially, return Major/Medium/Low findings and a recommendation to the planner, declare review-clean explicitly, or write requirement-level conformance verdicts against integrated main. Load when the boot sprint directive or a scoped task assigns review work.
category: craft
common: false
---

# sprint_review

Use the base `review` discipline with the sprint's exact-head and
planner-routed handoff contract.

## Activate from the record

Read the scoped task and board:

```sh
./sc mem message check
./sc sprint board --sprint <doc-id>
./sc mem get doc --doc <doc-id>
```

Confirm the assigned unit or conformance scope. Keep:

```text
SPRINT doc=<id> reviewing=<unit or conformance>
```

Every review transition uses a durable scoped result:

```sh
./sc mem message send <planner> "<review transition>" \
  --kind result --sprint <doc-id>
```

File findings and verdicts within the assignment. Send ruling requests to the
planner with alternatives and evidence.

Use IDs returned by creating writes for documents, tasks, flags, and messages.
Confirm the target before an irreversible mutation. Refer to flags by `flag_id`
plus a sprint-scoped label. Establish absence through a complete direct read,
count, or exact-ID query.

## Review a unit

### Pin the review target

Confirm the requested PR and exact head SHA before reading the diff. Confirm CI
belongs to that head.

If `main` advanced, compare intervening changes with the PR's files:

- disjoint surface: review the requested head;
- overlapping surface: request a rebase before review.

For a superseded, force-pushed, or unverified head, send a scoped correction
request and wait for a pinned target.

### Review adversarially

Trace behavior against:

- unit scope and governing spec;
- correctness and error handling;
- empty, boundary, concurrent, partial-failure, and permission states;
- repository conventions and avoidable complexity;
- tests that constrain the new behavior.

For a high-value property, mutate the implementation or condition, prove the
relevant test fails, restore the source, and prove it passes. Record the
property tested and result. Use a narrowed interference review after a rebase
or hand-resolved hunk.

### Classify and hand off

- Major: wrong behavior, security/data risk, or material spec violation.
- Medium: likely production defect or incomplete required path.
- Low: non-blocking clarity, cleanup, or improvement.

Send findings and the recommendation to the planner. Include location,
consequence, required behavior, and severity. The planner routes fix work or
merge authority to the developer.

On a clean pass, explicitly send the planner:

```text
U1 review-clean head=<sha>; mutation=<property/result>; findings=0
```

Use `--kind result --sprint <doc-id>`. A clean verdict applies only to the named
head and any later head whose contribution is proven unchanged across disjoint
interference.

**Unit-review completion:** the planner holds an explicit recommendation for the
review head, with every blocking finding named.

## Run close-time conformance

Use this procedure when the task says `Conformance`.

Read the governing spec and integrated code on `main` at the supplied SHA.
Treat ratified deviations from the task as the complete intentional-deviation
list. Judge the integrated result, not PR diffs or developer narratives.

Give every requirement in scope exactly one verdict:

- `as-specced`: implementation matches the spec;
- `deviated-intentionally`: implementation matches a ratified deviation;
- `deviated-silently`: implementation departs without a ratified decision;
- `unimplemented`: no integrated implementation satisfies it.

For `deviated-silently` and `unimplemented`, attach:

- spec section;
- code location or confirmed absence;
- Major, Medium, or Low severity;
- observable consequence.

At the supplied main SHA, re-check every present-tense code claim in the whole
spec, beginning with the problem statement and correction sections. A
provenance line carries the ref and SHA it actually verified. Mark historical
claims as historical.

Create a document titled `CONFORMANCE: <sprint title>`, kind `doc`, containing:

```markdown
## Scope
## Main SHA
## Requirement Verdicts
## Findings
## Evidence
```

Persist it with `./sc mem doc add`, then send one pointer:

```sh
./sc mem message send <planner> \
  "Conformance complete: doc <id>; <n> findings (<major>/<medium>/<low>)" \
  --kind result --sprint <sprint-doc-id>
```

The planner owns finding disposition and close authority.

**Conformance completion:** every scoped requirement has one verdict, every gap
has evidence and severity, and the planner has the document pointer.

## Stand down

Remove the sprint current-state line when the unit becomes terminal or the
planner confirms receipt of conformance. A frozen sprint document ends
sprint-scoped review authority and restores the base review gate.
