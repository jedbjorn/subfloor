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
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <doc-id>
./sc mem message sent
```

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

File findings and verdicts within the assignment. Send ruling requests to the
planner with alternatives and evidence.

Message IDs are scoped to their recipient. Treat an ID from another actor's
inbox as provenance, never as a fetch instruction; the task row must quote the
substance you need. Ask the planner for that substance when it is absent.

Use IDs returned by creating writes for documents, tasks, flags, and messages.
Confirm the target before an irreversible mutation. Refer to flags by `flag_id`
plus a sprint-scoped label. Establish absence through a complete direct read,
count, or exact-ID query. Before closing a flag by number, resolve and read back
its exact `flag_id`; display names and flag IDs share an integer range.

Validate every absence instrument against a known-positive target before
reporting an empty result as absence. Inspect its exit status and stderr; a
probe that cannot see the positive control leaves the claim unmeasured.

The sprint reconciler compares the board's live expectations with positive work
and result evidence. It reports confirmed divergences to the planner; it never
changes the board or supervises the reviewer. Send a scoped partial before a
turn ends with review unfinished, naming completed checks, evidence, and the
next untouched action. "Nothing found" becomes the explicit clean verdict.

Treat `read_at` in one direction only: READ proves something marked the row
read; UNREAD proves nothing about delivery, liveness, or work. Never infer a
fault or safe action from an unread marker.

## Review a unit

### Pin the review target

Confirm the requested PR and exact head SHA before reading the diff. Confirm CI
belongs to that head.

If `main` advanced, compare intervening changes with the PR's files:

- disjoint surface: review the requested head;
- overlapping surface: request a rebase before review.

For a superseded, force-pushed, or unverified head, send a scoped correction
request and wait for a pinned target.

Drain scoped messages immediately before every durable action you own, including
a verdict, pushed review artifact, or conformance document write. An earlier
inbox check does not satisfy this gate.

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

Before reporting a mutation red set or empty set, validate the test-selection
and result-counting instrument against a known-positive mutation. A selector
that cannot detect its control leaves the set unmeasured.

Require one property per test method, or `subTest` boundaries when setup must be
shared. Sequential assertions for independent properties are not independent
detectors: an early failure masks every later one.

### Classify and hand off

- Major: wrong behavior, security/data risk, or material spec violation.
- Medium: likely production defect or incomplete required path.
- Low: non-blocking clarity, cleanup, or improvement.

Under an ACTIVE sprint document the planner holds the FnB's delegated approval
for your sprint-scoped sends to the planner. Sending them SATISFIES the
outbound-handoff approval gate — whether that gate reaches you from the base
`review` skill or from your own system prompt — rather than bypassing it. The
gate reverts to the FnB when the sprint document freezes. Each verdict includes
location, consequence, required behavior, and severity. The planner routes fix
work or merge authority to the developer.

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

The sprint-scoped send approval rule in **Classify and hand off** applies here.

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
./sc mem message send <planner> "$(<./sprint-result.md)" \
  --kind result --sprint <sprint-doc-id>
./sc mem message sent
```

The planner owns finding disposition and close authority.

**Conformance completion:** every scoped requirement has one verdict, every gap
has evidence and severity, and the planner has the document pointer.

## Stand down

Remove the sprint current-state line when the unit becomes terminal or the
planner confirms receipt of conformance. A frozen sprint document ends
sprint-scoped review authority and restores the base review gate.
