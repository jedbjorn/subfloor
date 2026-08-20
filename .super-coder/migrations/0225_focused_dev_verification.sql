-- 0225 — focused Developer verification posture.
-- Decision #225: local shells prove affected behavior; configured repository
-- CI owns the authoritative full suite and review-readiness gate.

BEGIN;

UPDATE shells
SET system_prompt = CASE
  WHEN instr(system_prompt, '

## CODE CRAFT') > 0 THEN
    replace(system_prompt, '

## CODE CRAFT', '

## TESTING POSTURE

Run the smallest affected test targets that prove the changed behavior and realistic failure paths. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI; the PR is review-ready only when its required CI checks are green. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.

## CODE CRAFT')
  ELSE rtrim(system_prompt) || '

## TESTING POSTURE

Run the smallest affected test targets that prove the changed behavior and realistic failure paths. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI; the PR is review-ready only when its required CI checks are green. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.'
END
WHERE flavor='dev'
  AND instr(system_prompt, '## TESTING POSTURE')=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'agents',
  '--agents [model] — delegate work to spawned subagents under the system''s discipline. Dev — execute a spec''s task plan as implementer waves; reviewer — fan the three review axes out to an adversarial finding-panel. Overlay on spec/review; parent-only memory writes; AGENTS spawn ledger with a hard 6h validity window; parent-set timeouts. Load ONLY when the FnB invokes --agents.',
  'craft',
  NULL,
  0,
  '# agents — delegated waves under your discipline

FnB invokes this as `--agents [model]`. It is an **overlay** on `spec` (dev
mode) and `review` (review mode): it changes only what is written here.
Everything upstream and downstream of the named steps — loading the spec,
task tracking, flags, the FnB handoff gate — is the base skill, unchanged.
Load the base skill first; apply this on top.

`[model]` = worker tier, passed verbatim to the harness''s agent tool. No arg
-> agents inherit your model. Heavier judgment work -> heavier worker; you
may bump a single agent''s tier for a task judged hard. You — the parent —
NEVER change tier; you stay the judge.

**Core loop = implement -> you verify -> adversarially refute -> you fix.**
The refute step is where the quality comes from. Parallel implementers are an
optional scale-up for genuinely large, file-disjoint work, not the headline:
your loop (compose -> wait -> adjudicate -> re-verify) is serial, and field
runs measured hundreds of k of subagent tokens even on small waves — the
spend buys verification depth + an audit trail, not wall-clock.

Fit test before spawning: multi-surface + file-disjoint + spec''d + high
correctness stakes -> waves. Single-file / small fix -> run the base
procedure solo; at most spawn one adversarial skeptic against your own diff
— the cheap, high-ROI slice of this skill.

- **Harness:** subagent tooling exists in the claude harness only. No
  subagent tooling in your harness -> this skill is inert; run the base
  procedure.
- **Not a workflow-script system.** NEVER build deterministic orchestration
  scripts — spawn agents directly and stay in the loop between waves. You
  decide scale, batching, and prompts live, per this session''s demands.

---

## The contract — four rules, non-negotiable

1. **You are the only memory writer.** Agents NEVER run `sc mem` — no task
   status, flags, messages, current_state, narrative — and NEVER `git push`,
   open PRs, or message shells. They return diffs + findings; you adjudicate
   and record. Keeps the shared DB coherent and the reviewer''s FnB handoff
   gate intact.
2. **Prompt ingredients, not canned prompts.** Compose every agent prompt
   fresh; each MUST carry: the spec excerpt / done-condition it serves, the
   exact file paths in play, the fork conventions that apply, the expected
   base commit (agent verifies via `git log -1` before editing and REPORTS a
   mismatch instead of silently proceeding), the deadline block (see the
   ledger check), and a required return shape.
3. **Isolation by conflict risk.** Concurrent writers on the same files — or
   any writer that must touch git state — each get their own isolated
   worktree (writers never share a tree''s index). A file-disjoint wave may
   share your tree, edits only: agents run no `git
   add`/`stash`/`checkout`/`commit`. Read *Worktree reality* below before
   reaching for isolation — it has real costs. Reviewer/checker agents are
   read-only; no isolation needed.
4. **Agent claims are inputs, not results.** Follow the boot `TESTING POSTURE`:
   re-run smallest affected targets, lint, and the spec''s done-condition
   yourself; never use bare `sc test` merely to duplicate configured CI.
   "Agent says tests pass" is not verification. Pull diffs yourself
   (`git -C <worktree> diff`); NEVER adjudicate pasted diffs/output — pastes
   are lossy.

---

## Worktree reality — what isolation actually gives an agent

Harness worktrees are fresh trees; two properties bite (both observed on
first fork runs — super-coder #303, #304):

- **They seed from the default branch (origin/main), not your branch HEAD.**
  In a stacked feature, a later-wave implementer authors — and "verifies" —
  against a base missing the earlier waves'' commits. Hence the base-commit
  ingredient in contract rule 2, and hence: writers return diffs, you apply
  each one to YOUR tree with `git apply --3way` (note: it STAGES — inspect
  via `git diff HEAD`), and every check runs on the merged state.
- **They lack untracked toolchains.** No `node_modules`; sandboxed
  interpreters typically mount only into the primary worktree — an isolated
  agent often cannot run the app''s suite at all. Say so in the prompt so it
  doesn''t burn a turn rediscovering it; treat its tree as an authoring
  surface — verification is yours, in your tree.

---

## The ledger check — before EVERY spawn, before acting on ANY result

The ledger = one line embedded in current_state (one wave live at a time, so
one line is the complete record):

```
AGENTS wave=2/3 spawned=2026-07-06T14:32Z timeout=30m out=task4,task5
```

Review mode uses axis/lens names in `out=` (e.g.
`out=quality,edges,conformance,api-design`). Stamp `spawned=` from the clock
(UTC) at the moment you spawn — NEVER recalled or recomputed from context.
Remove the line at wave close.

Execute this check verbatim; do not interpret it:

```
1. Read current_state.
   No AGENTS line → you may spawn. Write the AGENTS line,
   spawned=<now UTC>, in the same act as spawning.
2. AGENTS line present → age = now(UTC) − spawned.
3. age > 6h → the wave is DEAD. Unconditionally:
   a. Stop any agent still running.
   b. Discard their output UNREAD — do not apply, adjudicate, or "just
      check" it, even if it looks correct.
   c. Reconcile the task plan against reality: a task is done only if its
      diff is on the branch and verification passes NOW.
   d. Remove the AGENTS line; narrative: "wave expired (spawned <ts>);
      reconciled <n> tasks".
   e. Only now may current-session judgment start a NEW wave — fresh
      spawn, fresh timestamp.
4. age ≤ 6h → the wave is LIVE:
   - agents running → monitor; never spawn a duplicate for anything
     listed in out=.
   - agents not running (a prior session died) → their tasks revert to
     pending; respawning is a NEW wave: check no orphan diff already
     landed, then rewrite the AGENTS line with a fresh timestamp.
```

Every agent prompt ends with this deadline block, filled in:

```
Your deadline is <spawned + timeout> UTC. Past it, stop and return
partial results. If the current time is after <spawned + 6h>, do no
work — return immediately. Run all verification synchronously; never
end your turn waiting on a background task — your final message is
your only channel back.
```

The 6-hour window is a hard constant: choose timeouts freely under it;
nothing extends it. Step 3b is deliberate — expired output is discarded even
when it looks correct; "looks correct" hours later against a moved tree is
exactly the trap. Step 3c recovers anything real: a diff that genuinely
landed and verifies NOW passes reconciliation as done. Stale ledger text is
never evidence.

---

## Dev mode — overlay on `spec` Step 4

After the task plan exists (base skill, Steps 1–3, unchanged):

1. Classify pending tasks into **dependency waves** — independent tasks run
   in parallel; dependent tasks sequence. Ordering non-obvious -> use
   `blueprint` for the dependency read; a plan that already encodes the
   order stands on its own.
2. Per wave: run the ledger check -> mark each wave task `in_progress`
   (`sc mem task start`) -> spawn one implementer per task (isolation per
   contract rule 3) -> pull each returned diff yourself and apply it to your
   tree -> spawn checker agent(s) prompted to **refute** it -> adjudicate +
   run the affected tests on the merged state -> `sc mem task done` -> update
   current_state -> next wave.
3. One wave live at a time.

Stance amendment: `spec`''s "one task at a time" becomes "one **wave** at a
time" under `--agents`; each task is still independently verified before it
is marked done. `spec` Step 5 (handoff on completion) is unchanged — yours,
never an agent''s.

## Review mode — overlay on `review` Step 2

`review` Steps 1, 3, and 4 — loading the diff and its spec, flags, the
FnB-gated handoff — are unchanged. Agents never open flags.

1. Run the ledger check, then fan out **one agent per axis** (code quality /
   edge cases & gaps / spec conformance) **plus one per applicable lens**
   from the base skill''s lens table. Each agent is read-only and returns
   candidate findings in a fixed shape:
   `file:line · claim · severity · how to reproduce`.
2. Dedupe the returns. Uncertain finding -> optionally spawn a skeptic
   prompted to refute it. Adjudicate every survivor yourself — re-read the
   code path; a finding is a lead, not a verdict.
3. Proceed to base Step 3 with the adjudicated findings. Agents widen the
   search; you remain the gate.

---

## Monitoring

Agents cannot self-report (contract rule 1) — monitoring is your checkpoint
discipline, written to surfaces the FnB already watches:

| Surface | What it shows |
|---|---|
| task plan (`sc mem get tasks`) | live board — wave tasks flip `in_progress` at spawn, `done` at adjudication; the GUI Tasks tab renders it |
| `current_state` | the in-flight AGENTS ledger line, rewritten at every wave boundary |
| narrative | one line per inflection: wave landed, timeout, checker refuted an implementation |
| on demand | "status?" from the FnB → inspect your running agents'' output, answer in two lines |

Limit: mid-task granularity inside a single agent is visible only by
inspecting its output on demand. No per-agent progress bar — a write surface
for agents would break rule 1.

## Timeouts

Set a timeout per agent at spawn, sized to the task, recorded in the ledger
line — the budget is visible, not private.

At expiry: inspect the agent''s partial output -> stop it -> respawn with a
**narrower** prompt (a timeout usually means the prompt was too broad) /
take the task inline.

**Two-strike rule:** a task whose agent times out twice is done inline by
you, full stop. No respawn loops. Every timeout gets a narrative line —
timeouts are signal about the plan''s granularity.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'spec',
  'Load before implementing any feature, spec, or roadmap item. Analyze viability, surface blockers, plan Preparation → implementation → Verification, and track spec_tasks/current_state across sessions.',
  'craft',
  NULL,
  0,
  '# spec — analyze and execute a spec

Load before implementing any feature/spec/roadmap item. A governing spec is
missing -> use `docs` to author it first. Analyze before code; unresolved
ambiguity goes to FnB, while hard blockers get flags. `<self>` = your shell id.

## 1. Select the spec

Never auto-pick the latest document. Read the complete selected body + task
ledger:

```text
sc mem get documents --feature <id>
sc mem get documents --doc <doc_id>
sc mem get tasks --doc <doc_id>
```

The feature list includes `kind`, `seq`, `frozen`, and `task_count`. Resume the
one unfrozen spec with tasks. An unfrozen zero-task spec is backlog; engaging it
creates the plan below. Multiple plausible open specs -> ask FnB. Existing
tasks -> skip planning and track the first unfinished one.

## 2. Analyze before planning

Return these findings before any code or task writes:

| Check | Pass condition / action |
|---|---|
| Viability | Bounded, clear entry points, session-sized verification. Otherwise propose a verifiable slice. Missing done-condition = unclear. |
| Current Posture | Preparation code/DB state matches the documented baseline. A mismatch stops for Planner/FnB; never redefine it silently. |
| Scope | Every In Scope promise is planned; every Out of Scope item stays out. New/substantively revised specs contain both `## Current Posture` and `## Scope`; ambiguity stops. |
| Anticipated User Activity | Plan stated roles, audience/reach, access, validation, recovery, authority, safety, process curation, and tenancy invariants. Older absence is allowed; ambiguity is not. |
| Unclear item | Two plausible readings, missing target/interface, or unstated required knowledge -> ask FnB; do not flag a question they can answer. |
| Blocker | Missing prerequisite/environment/external dependency -> open one High feature flag and stop at its boundary. |

```text
sc mem flag open "[Spec] <blocked fact> | Blocker for: <feature>" \
  --name SC-### --priority High --feature <feature_id>
```

## 3. Engage and plan

Building this session moves `brainstorm|long_term|near_term` to `in_progress`;
planning ahead moves it to `next`. Matching/later stages are no-ops. Reading
for reference moves nothing, and unspec''d small fixes have no stage ceremony.

```text
sc mem roadmap status <feature_id> in_progress
```

Confirm `roadmap.project_id`. Assign the obvious stream; ask FnB if ambiguous;
leave an existing assignment unchanged:

```text
sc mem roadmap project <feature_id> <shortname>
```

Create exactly: Preparation, independently verifiable implementation steps,
then Verification. Each write is immediately durable:

```text
sc mem task add "Preparation" --feature <id> --doc <doc_id> --seq 0 \
  --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> \
  --desc "<independently verifiable outcome>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <last> \
  --desc "Test every scope promise, done-condition, audience assurance, snapshot, and render"
sc mem state "[<feature>] — last: —. next: Preparation."
```

No task plan = no implementation.

## 4. Execute one task at a time

When FnB explicitly invoked `--agents`, load `agents`; its adjudicated waves
overlay this loop. Otherwise:

```text
sc mem get tasks --doc <doc_id>
sc mem task start <task_id>
# work and verify only this task
sc mem task done <task_id>
```

After completion, re-read the ledger. Set `current_state` to the highest done
task + lowest pending task. Start the next only after the current task is
verified and durably done. Work moved to another feature/spec is cancelled,
never marked done or left pending under a shipped feature:

```text
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
sc mem state "[<feature>] — last: <last_done>. next: <next_up>."
```

The final Verification task follows the boot `TESTING POSTURE`; require focused
local proof + green configured CI, every In Scope done-condition, and the
Anticipated User Activity contract. Unexpected reach, weakened hardening, or
crossed tenancy fails. A large spec may stop after a verified task slice; leave
later tasks pending and state the next one.

## 5. Ship and hand docs to Planner

All tasks done + Verification green -> deliver:

1. `sc mem roadmap status <feature_id> shipped`
2. Open one Medium docs-pending feature flag.
3. Message the planner: read shipped code, freeze the spec, write a `kind=doc`
   document under the feature, then close the flag. Confirm the durable message.
4. Tell FnB that code shipped and Planner owns freeze + docs. No planner shell ->
   message nobody; tell FnB and leave the flag open.

```text
sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" \
  --name SC-### --priority Medium --feature <feature_id>
```

Do not freeze or author the shipped doc as Developer.

## Scope change and stop rules

Small growth within the same intent -> revise the unfrozen spec with `docs`, add
tasks, continue. A separate mental model -> stop and recommend a new feature +
spec; never absorb it silently.

Stop for unresolved ambiguity/blocker, at the end of a verified session slice,
or after the shipped/docs handoff. `current_state` must always point to the
durable plan rather than reproduce its rationale.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Use for an actionable work-unit assignment in an armed Sprint. Use the simplest
path supported by current durable state. Treat ownership, lifecycle
preconditions, durable writes, and typed handoffs as hard boundaries; use
judgment inside them. Repeat a read only when later activity could have changed
it or the next command requires live revalidation.

## Route the entry

Load `sprint_dev` on every entry, then classify it:

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. Do not manufacture a Sprint inbox item; check the inbox once immediately before the next typed handoff. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Accepting marks assignment ownership and starts work. Decline with a concrete
reason when unable to accept. After handling an informational message, run
`accept`; it marks the message read and does not change Sprint or work-unit
state.

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint. Retry the exact command once, then use its normal read surface once to
prove the exact postcondition. For informational `accept`, prior inbox presence
+ absence of that exact message id proves the read landed. Continue under that
proof + name the receipt defect in the next normal handoff. NEVER use this
recovery to infer assignment ownership, review outcome, merge authorization,
lifecycle/work-unit transition, governing revision, PR head/green state, or
cleanup authority. An unproved postcondition stops.

Assignments and review requests use Force-new delivery; verdicts and PR-event
wakes use Re-enter. Delivery waits for a natural boundary; the runtime owns
bundling, rotation, and recovery. Stop after a successful typed handoff.

## Bound the lane

Read the assignment, expected output, bound spec revision, dependencies,
Reviewer, repository/worktree, merge grant, and prior judgments. Own at most one
active work unit; never start a second editing lane or edit another shell''s
worktree. Resolve ambiguity with the shippable in-scope reading + recorded
rationale. Ask the Planner before changing the unit boundary, shared interface,
deliverable cut, priority, or scope.

Put one question, blocker, decision, answer, or useful context item in a short
body file. Unit questions/blockers require a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` for a blocked lane. Cross-unit, closeout, or external
authority rulings are Sprint-level decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the server inherits its scope, so never add
`--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Ask the Reviewer about review evidence and the Planner about scope or
cross-unit authority. Confirm the durable reply, then `accept` the incoming
message. At a decision boundary, stop until the required answer arrives;
unread recovery re-wakes, so send no duplicate reminder.

A stable key identifies recipient + exact body + intent + reply linkage +
scope. Reuse it only for the same failed/ambiguous write; when any of those
fields changes, use a new key. Keep bodies near 6,000 characters and below the
8,000-character hard limit; run `wc -m < <path>`. A handoff completes only when
the command exits successfully and confirms its durable message/state + wake.
If a command is rejected or transport fails, correct and retry safely. If the
relay itself fails, give FnB the attempted command, evidence, impact, and
recommendation; invent no alternate protocol.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

Store scratch proof, diffs, evidence packets, review notes, and report drafts in
gitignored `shared/sprints/sprint-<n>/`. Never commit or PR them. Durable
judgments belong in `record-review`, reports in `sprint_reports`, and decisions
in the relay.

## Build and verify

Sync + branch; implement the smallest complete change. Per boot `TESTING
POSTURE`, run the smallest affected gate + failures; configured CI green =
full-suite proof, red -> diagnose/fix/push/rerun. Keep external calls outside
DB transactions; preserve durable identities and append-only evidence. Record
CI failures, infrastructure anomalies, retries, review friction, and
departures for closeout.

Immediately before `complete-unit`, `register-pr`, or `request-review`, re-run
`sc sprint inbox --sprint <id>` once and act on new messages. After the typed
handoff confirms its durable write, stop without another inbox pass. The
reopened-PR route below is the sole exception.

## Report-only or no-code completion

Only an explicitly planned report/no-code lane may finish without a PR. Keep
the result near 6,000 characters and below 8,000; run `wc -m < <path>`, perform
the pre-handoff inbox check, then require a durable completion receipt:

```text
sc sprint complete-unit --sprint <id> --work-unit <id> \
  --result-file <path>
```

Stop after success. A code lane continues through merge observation.

## Register and observe the PR

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

After `register-pr` succeeds, retain ownership through green. The native
registered-PR watcher creates your engine-wide subscription and sends
self-describing red/green/externally-closed PR-event wakes as Re-enter, even
outside an armed Sprint. Fix red; judge green. Planner and Reviewer receive no
PR-event wakes.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot. Its one pre-handoff inbox
check covers registration replay + the immediately following review request.
Do not wait for a second PR-fact wake: immediately request review. Green
proceeds; any other snapshot returns the watcher diagnostic without partial
handoff. Never register a replacement PR or ask the Planner to bypass observed
green.

Otherwise, when no local action remains, stop for the native PR fact. Start no
recurring loop, scheduled job, daemon, or external watcher. A stalled gate
permits one bounded read, then stop or report its evidence:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop.

## Review handoff and correction

Complete each round in order:

1. Finish readiness judgment + local verification.
2. Perform the once-only inbox check; handle and `accept` new messages.
3. Use `submit` first or `resubmit` after changes requested. The engine injects
   the PR URL, registered id, exact green head, and work-unit id into the
   Reviewer''s canonical bare one-line locator. Create no readiness file. Send
   no scope narrative, verification evidence, rationale, or review-focus
   steering. Put only the work-unit id and spec reference in the PR body; write
   no PR comments or annotations.
4. As the literal final action, run:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

5. Require confirmation of the durable write + Reviewer wake; run no trailing
   command and stop and await the native verdict wake.

Changes requested returns by Re-enter. Apply every blocking finding,
re-establish green, and resubmit with a new review-round key. Do not narrate
cleared findings; the Reviewer verifies the full diff at the engine-injected
head. Record disagreements as judgment. Reviewer owns scope/severity; Planner
executes resulting action.

## Merge boundary

Approval is stale evidence. Immediately before merge, re-read live GitHub,
grant, ownership, unit state, approved head, and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means wait for
the watcher or re-enter the appropriate loop; never bypass it.

## Post-merge handoff

After the authorized merge:

1. Clean the worktree; put merged PR + SHA, unit result, verification,
   judgments, and departures in the handoff file.
2. Re-run `sc sprint inbox --sprint <id>` once; handle and `accept` new items.
3. Run `wc -m < <path>`; keep the body near 6,000 characters and below 8,000.
4. As the literal final action, send:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

5. Require the durable message + Planner wake, then stop immediately. Run no trailing Git,
   Sprint, inbox, cleanup, or status command. Automatic merge
   observation records the PR transition; this handoff releases the next wave.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or unrecoverable environment with evidence,
impact, and recommendation. Stop when merged + reported, declined, returned to
review, paused for a native wake, or awaiting Planner/FnB recovery. Ask for
later work only after this editing lane is terminal.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
