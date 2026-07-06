-- 0049 — agents skill: delegated waves for dev + reviewer (feature #13, specs_sc/agents-skill.md)
--
-- New craft skill `agents` (--agents [model]): overlay on spec/review letting
-- the parent shell delegate spec execution to implementer waves and reviews to
-- adversarial finding-panels. Parent-only memory writes; AGENTS spawn ledger
-- with a hard 6h validity window; parent-set timeouts (two-strike floor).
-- Also reseeds `spec` (Step 4) and `review` (Step 2) with one-line overlay
-- pointers.
--
-- Self-contained on purpose: at update time `migrate` runs BEFORE the
-- catalogue sync, so the grants below could not rely on the sync having
-- inserted the skill row — the migration carries the bodies itself (UPSERT by
-- name; skill_id + existing grants preserved). The sync re-asserts the same
-- content harmlessly afterward. 0001 is regenerated from assets for fresh
-- builds; this forward reseed carries the same bodies to installed forks.
--
-- Grants: existing dev + reviewer shells get `agents` here; NEW shells get it
-- from templates/shells/{dev,reviewer}.json.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'agents',
  '--agents [model] — delegate work to spawned subagents under the system''s discipline. Dev — execute a spec''s task plan as implementer waves; reviewer — fan the three review axes out to an adversarial finding-panel. Overlay on spec/review; parent-only memory writes; AGENTS spawn ledger with a hard 6h validity window; parent-set timeouts. Load ONLY when the FnB invokes --agents.',
  'craft',
  NULL,
  0,
  '# agents — delegated waves under your discipline

The FnB invokes this as `--agents [model]`. It is an **overlay** on `spec`
(dev mode) and `review` (review mode): it changes only what is written here.
Everything upstream and downstream of the named steps — loading the spec,
task tracking, flags, the FnB handoff gate — is the base skill, unchanged.
Load the base skill first; apply this on top of it.

`[model]` sets the **worker tier**, passed through verbatim to the harness''s
agent tool. No arg → agents inherit your model. Guidance is one line: heavier
judgment work warrants a heavier worker, and you may bump a single agent''s
tier when a task is judged hard. You — the parent — never change tier; you
stay the judge.

- **Harness:** subagent tooling exists in the claude harness only. No
  subagent tooling in your harness → this skill is inert; run the base
  procedure.
- **Not a workflow-script system.** No deterministic orchestration scripts —
  you spawn agents directly and stay in the loop between waves. Do not
  "upgrade" this to scripted workflows; the point is that you decide scale,
  batching, and prompts live, per this session''s demands.

---

## The contract — four rules, non-negotiable

1. **You are the only memory writer.** Agents never run `sc mem` — no task
   status, no flags, no messages, no current_state, no narrative — and never
   `git push`, open PRs, or message shells. They return diffs and findings;
   you adjudicate and record. This keeps the shared DB coherent and leaves
   the reviewer''s FnB handoff gate untouched.
2. **Prompt ingredients, not canned prompts.** You compose every agent
   prompt fresh, and it must carry: the spec excerpt / done-condition it
   serves, the exact file paths in play, the fork conventions that apply,
   the deadline block (see the ledger check), and a required return shape.
3. **Isolation by role.** More than one implementer in flight → each works
   in its own isolated worktree (writers never share a tree). Reviewer and
   checker agents are read-only; no isolation needed.
4. **Agent claims are inputs, not results.** Re-run the real check yourself
   — `./sc test`, lint, the spec''s done-condition — before marking anything
   done. "Agent says tests pass" is not verification.

---

## The ledger check — before EVERY spawn, before acting on ANY result

The ledger is a single line embedded in current_state (one wave live at a
time, so one line is the complete record):

```
AGENTS wave=2/3 spawned=2026-07-06T14:32Z timeout=30m out=task4,task5
```

Review mode uses axis/lens names in `out=` (e.g.
`out=quality,edges,conformance,api-design`). Stamp `spawned=` from the
clock (UTC) at the moment you spawn — never recalled or recomputed from
context. Remove the line at wave close.

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
work — return immediately.
```

The 6-hour window is a hard constant. You choose timeouts freely under it;
nothing extends it. Step 3b is deliberate: expired output is discarded even
when it looks correct — "looks correct" hours later against a moved tree is
exactly the trap. Step 3c recovers anything real: a diff that genuinely
landed and verifies passes reconciliation as done. Stale ledger text is
never evidence.

---

## Dev mode — overlay on `spec` Step 4

After the task plan exists (base skill, Steps 1–3, unchanged):

1. Classify pending tasks into **dependency waves** — independent tasks may
   run in parallel; dependent tasks sequence. Use `blueprint` for the
   dependency read; don''t reimplement it.
2. Per wave: run the ledger check → mark each wave task `in_progress`
   (`sc mem task start`) → spawn one implementer per task (worktrees if
   more than one) → on each returned diff, spawn checker agent(s) prompted
   to **refute** it → adjudicate, apply, run the real tests → `sc mem task
   done` → update current_state → next wave.
3. One wave live at a time.

Stance amendment: `spec`''s "one task at a time" becomes "one **wave** at a
time" under `--agents`. Each task is still independently verified before it
is marked done — the spirit holds. Step 5 of `spec` (handoff on completion)
is unchanged and is yours, never an agent''s.

## Review mode — overlay on `review` Step 2

Steps 1, 3, and 4 of `review` — loading the diff and its spec, flags, the
FnB-gated handoff — are unchanged. Agents never open flags.

1. Run the ledger check, then fan out **one agent per axis** (code quality /
   edge cases & gaps / spec conformance) **plus one per applicable lens**
   from the base skill''s lens table. Each agent is read-only and returns
   candidate findings in a fixed shape:
   `file:line · claim · severity · how to reproduce`.
2. Dedupe the returns. For an uncertain finding, optionally spawn a skeptic
   prompted to refute it. Adjudicate every survivor yourself — re-read the
   code path; an agent''s finding is a lead, not a verdict.
3. Proceed to base Step 3 with the adjudicated findings. The agents widen
   the search; you remain the gate.

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

Honest limitation: mid-task granularity inside a single agent is only
visible by inspecting its output on demand. There is no per-agent progress
bar — giving agents a write surface would break rule 1.

## Timeouts

Set a timeout per agent at spawn, sized to the task, and record it in the
ledger line — the budget is visible, not private.

At expiry: inspect the agent''s partial output → stop it → either respawn
with a **narrower** prompt (a timeout usually means the prompt was too
broad) or take the task inline.

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
  'Execute a spec across sessions — analyze viability, surface blockers and unclear items, break into tasks (Preparation → impl steps → Verification), and track progress in spec_tasks. Updates current_state at every step. Load when starting, implementing, or building any feature, spec, or roadmap item — before writing code.',
  'craft',
  NULL,
  0,
  '# spec — analyze and execute a spec

Load this skill at the start of any session where you''re building or implementing
a feature — whether or not the work is framed out loud as a "spec." If a spec
governs the work, this is how you execute it; if one should but doesn''t yet, the
`docs` skill authors it first. Run **Analyze** before touching any code. Pause for FnB on blockers or unclear
items you can''t resolve alone.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill), so don''t
auto-pick "the latest" — list the feature''s open specs and choose the target
explicitly. The **active** spec is the unfrozen one that already has a task plan;
the rest are backlog.

```
# the feature''s documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec''s task plan (empty = no plan yet):
sc mem get tasks --doc <doc_id>
```

`get documents --feature <id>` lists every spec/doc under the feature with its
`kind`, `seq`, `frozen`, and `task_count`. `task_count > 0` marks the active spec — resume that one; an empty spec is backlog,
and starting it (Step 3) makes it active. If more than one open spec matches and
which to work is unclear, ask the FnB.

If tasks already exist, skip to **Step 4** (Track).

Read the entire spec body before going further. Do not skim.

---

## Step 2: Analyze

Surface the following before any planning or code:

### Viability
- Can this be completed in the current session? Bounded + clear entry points:
  yes. Multiple layers, migrations, unknown dependencies: no — say so and
  propose a session-sized slice.
- Does the spec state a clear done-condition? If not, that is the first unclear
  item.

### Unclear items
Things you cannot act on without guessing:
- Ambiguous between two interpretations
- Missing a critical detail (which table? which endpoint? which component?)
- Implies knowledge not stated in the spec

List these and ask the FnB before writing the plan.

### Blockers
Hard stops — prior work not shipped, missing environment state, unresolved
external dependency. Open a flag for each:

```
sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

Don''t open flags for unclear items you can resolve by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec means you''re engaging it to build — so the feature''s
`roadmap_status` (loaded in Step 1) must catch up to reality. The horizon stages
are `brainstorm · long_term · near_term · next · in_progress · shipped`.

- Feature sits at `brainstorm`/`long_term`/`near_term` and you''re **building this
  session** → move it to `in_progress`:
  `sc mem roadmap status <feature_id> in_progress`
- You''re only **planning ahead** (no build this session) → move it to `next`.
- Already at `in_progress` (or further) → **no-op**; don''t churn it.

This is a transition you make because you''re *acting on* the spec — not something
that fires from merely reading one for reference. If there is no spec governing the
work (a quick UI fix, a minor migration), skip all stage handling: it doesn''t
apply (see the Stance).

### Confirm the work-stream too

While you''re reconciling the stage, check the same feature''s **work-stream**
(`roadmap.project_id` — the Flow-view grouping). If it''s Ungrouped, assign it now
so the feature shows up in a flow, not the Ungrouped pile:

```
sc mem roadmap project <feature_id> <shortname>   # ''none'' to clear
```

Assign when the stream is obvious; surface to the FnB when it''s ambiguous. No-op
if already assigned. The full create/assess procedure (new streams, new features)
lives in the `docs` skill — this is just the engage-time confirmation so drift
doesn''t accumulate.

Once analysis is clear and blockers are resolved or accepted, generate the task
list and INSERT it. Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

Add each task with `sc mem task add` (one per seq) — each write is live in the
shared DB immediately:

```
sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>"
sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, snapshot + render"
```

Then set `current_state` — no last-done yet, next is Preparation:

```
sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

**Agents overlay:** if this shell is granted `agents` and the FnB invoked
`--agents`, that skill''s overlay replaces this step''s one-task-at-a-time loop
with adjudicated waves — load it and apply it on top of this step.

At the start of each work session, load the current plan state:

```
sc mem get tasks --doc <doc_id>
```

Find the first `pending` task. Mark it `in_progress`:

```
sc mem task start <task_id>
```

Work only that task. When done, mark it complete, then resolve last-done / next-up
with a read:

```
sc mem task done <task_id>
```

Re-read the plan and resolve last-done / next-up from it — `last_done` is the
highest-`seq` `done` task, `next_up` the lowest-`seq` `pending` one:

```
sc mem get tasks --doc <doc_id>
```

Then advance `current_state`:

```
sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

If `next_up` is NULL, all tasks are done — set current_state to reflect that.

---

## Step 5: Hand off on completion

When the **Verification** task passes (`next_up` is NULL — the existing
done-line), the feature is delivered. As the dev, do the handoff — you flip the
horizon and hand the paperwork to the planner; you do **not** freeze the spec or
write the doc (that''s the planner — see the `docs` skill):

1. **Flip the horizon to shipped:**
   ```
   sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag and message the planner with full instructions.**
   `shipped` + an open flag is the honest interim state. The message carries
   everything the planner needs to act without digging:
   ```
   sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   sc mem message send <planner-shortname> "**[Docs pending] <feature_title> (feature <feature_id>)**

   Spec <doc_id> shipped. Flag SC-### is open — your action required:

   1. **Read the shipped code first.** Write the doc from what actually shipped, not from the spec. Drift happens and decisions get made in production — the spec captures the intent, the code is the truth.
   2. Freeze the spec: \`sc mem doc freeze <doc_id>\`
   3. Write the doc (\`kind=''doc''\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec + write
   the doc." The planner closes the flag when the doc lands.

If this fork has no planner-flavor shell, message nobody — surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

If, mid-build, the work grows past the spec''s stated what/why:

- **Small growth** (same mental model, a few more tasks) → the spec is *living*
  while unfrozen; just edit it (`sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the granularity
  test in the `docs` skill) → don''t quietly absorb it. Recommend a **new spec** to
  the FnB, to be authored by the planner against its own feature. Significant creep
  is a planning event, not a dev improvisation.

---

## Stance

- **Analyze before acting.** The analysis phase discovers the gap between what
  the spec says and what the code does.
- **One task at a time.** Don''t start task N+1 until task N is verified and
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **If the spec is too large for one session:** scope a session slice at
  Preparation — cover steps 1–K that can be verified now, leave K+1–N pending.
  Don''t start work that can''t be verified before the session ends.
- **current_state always reflects the plan.** After every task completion,
  update it — last done + next up. This is how the next session resumes without
  reading the full task list first.
- **The stage tracks reality, but only for spec''d work.** Engaging a spec moves
  it forward (→ `in_progress`); finishing it hands off (→ `shipped`). No-op when
  the stage already matches — don''t churn it. Work with **no spec** (quick UI
  tweaks, minor migrations) is exempt entirely: no promotion, no handoff, no creep
  check. Stage discipline must never become a blocker for small things.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'review',
  'Reviewer procedure — read a diff against its spec along three axes (code quality, edge cases & gaps, spec conformance), open flags for failures, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. The reviewer''s top-level loop; the lenses live in the skills it points to. Load when reviewing a dev''s work.',
  'craft',
  NULL,
  0,
  '# review — gate a diff against its spec

The reviewer''s job from end to end. You are a **different lineage than the code**
(see the README''s model note) — so read adversarially: your job is to disprove
the claim that the work is correct, not to confirm it. `<self>` = your shell_id.

A review is not finished when you''ve read the diff. **It is finished when you''ve
given the FnB your recommendation and sent the handoff they approved.** Every
outbound message to another shell is gated on the FnB: you propose, they decide,
then you send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding before it
lands in another shell''s inbox.

---

## Step 1: Load the diff and its spec

You review a diff *against intent*, not in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: load the feature''s spec doc (the `spec` skill, Step 1
  — `documents` where `kind=''spec''`). The done-condition in that spec is your
  yardstick.

Note the **author** — you''ll propose a handoff to them in Step 4. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`) — the roster maps display_name
→ shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

**Agents overlay:** if this shell is granted `agents` and the FnB invoked
`--agents`, that skill''s overlay fans this step out to an adversarial
finding-panel — load it and apply it on top of this step. Steps 1, 3, and 4
stay yours, unchanged.

Apply every axis, every review — combined with the granted *lenses* that sharpen
whichever area the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with existing
   patterns. Trace the actual code path; don''t trust the description of it.
2. **Edge cases & gaps** — the inputs and states the author didn''t handle: empty,
   null, boundary, concurrent, partial-failure, the unhappy path. Name what''s
   missing, not only what''s wrong.
3. **Spec conformance** — read the diff against its spec''s done-condition. Flag
   where the implementation diverges from intent, and where the spec itself was
   silent or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

If this fork grants a skill that supersedes a lens (says so in its description —
e.g. a fork-local testing skill superseding `test_authoring`), use the
superseding skill: it carries the fork''s actual standard.

## Step 3: Open a flag per failure — record, don''t yet send

Each real failure is a flag against the feature — a record of what you found:
```
sc mem flag open "[Review] <what''s wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill''s default, **do not pair an outbound message here.** The
message is the handoff, and handoffs wait for the FnB (Step 4). Don''t open flags
for nits you can state in the summary; flag what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Assemble your recommendation and the handoff it implies:

- fixes on the diff → a message to the **author dev**
- a missing or wrong spec → a message to the **planner**
- clean → nothing to send

Present the findings (flags + summary) and the drafted message(s) to the FnB. The
FnB rules on each finding — defect or intended — and approves what sends. Then,
and only then, send the approved handoff:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate. Assume there''s a bug and go find
  it; "looks fine" is not a review.
- **Verify, don''t trust.** Re-run the tests, re-read the claim against the code.
  A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the bar.
  Scope creep in the diff is a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs.
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don''t push it.
- **You critique and confirm — you don''t build.** Don''t patch the author''s code;
  flag it and propose it back.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

-- grant to existing dev + reviewer shells (no-op where already granted)
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id
FROM shells s, skills k
WHERE COALESCE(s.is_deleted, 0) = 0
  AND s.flavor IN ('dev', 'reviewer')
  AND k.name = 'agents' AND k.is_deleted = 0;

COMMIT;
