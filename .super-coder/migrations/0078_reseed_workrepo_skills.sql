-- 0078 — reseed: work-repo reality for the maintainer install's skill set.
--
-- This install's shells maintain an EXTERNAL work repo (instance.json
-- work_repo); the git / source-maintenance / issue_reporting skills were
-- rewritten around it (git -C the work repo, home repo local-only + commit
-- guard, maintainer-not-fork defect routing), and the dev-cycle skills
-- (sprint/spec/review/agents/sprint_orchestration) carry a work-repo pointer.
-- Source assets updated in the same commit; this trailing forward reseed
-- (UPSERT by name; skill_id + grants preserved) carries the change through
-- rebuilds — without it, 0074/0076 replay OLD issue_reporting/sprint bodies
-- over the regenerated 0001.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'agents',
  '--agents [model] — delegate work to spawned subagents under the system''s discipline. Dev — execute a spec''s task plan as implementer waves; reviewer — fan the three review axes out to an adversarial finding-panel. Overlay on spec/review; parent-only memory writes; AGENTS spawn ledger with a hard 6h validity window; parent-set timeouts. Load ONLY when the FnB invokes --agents.',
  'craft',
  NULL,
  0,
  '# agents — delegated waves under your discipline

> **Work repo:** every git/gh command in this procedure runs against `~/Repos/subfloor` (`git -C ~/Repos/subfloor`, `gh --repo jedbjorn/subfloor`) — NEVER against the home repo your cwd sits in. Addressing contract: the `git` skill.

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
4. **Agent claims are inputs, not results.** Re-run the real check yourself
   — `./sc test`, lint, the spec''s done-condition — before marking anything
   done. "Agent says tests pass" is not verification. Diffs: pull them
   yourself (`git -C <worktree> diff`); NEVER adjudicate pasted diffs or
   pasted test output — pastes are lossy and unverifiable.

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
   run the real tests on the merged state -> `sc mem task done` -> update
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
  'git',
  'Git conventions for this install — ALL version control happens in the work repo (~/Repos/subfloor), addressed explicitly with git -C / gh --repo. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK). Use before any git work.',
  'substrate',
  NULL,
  0,
  '# git — version control, the work-repo way

Two repos are in reach; only one takes commits from you:

| Repo | Path | Git role |
|---|---|---|
| **work repo** | `~/Repos/subfloor` (GitHub `jedbjorn/subfloor`) | ALL of it: sync, branch, commit, push, PR |
| **home repo** | the repo your cwd sits in | NONE. Local-only (no remotes); commits refused by a pre-commit guard |

Your cwd is a home worktree -> a bare `git`/`gh` command targets the WRONG repo.
Address the work repo explicitly, every time:

- `git -C ~/Repos/subfloor <cmd>` — never rely on cwd, even right after a `cd`.
- `gh --repo jedbjorn/subfloor <cmd>` for PR operations.
- Success condition: `git -C ~/Repos/subfloor rev-parse --show-toplevel` prints
  the subfloor path before your first write of a session.

NEVER commit, branch, or open a PR in the home repo. The guard blocks the
commit and prints this redirect; `SC_HOME_MAINTENANCE=1` is for FnB-approved
home maintenance only — never a way around a mistake. Home and work repo are
different products with divergent histories: NEVER retarget a commit, branch,
or diff from one onto the other. Built against the wrong repo -> rebuild from
scratch in the right one.

## Sync before you start — hard pre-code gate

Run before each new unit of work. A stale base -> you read code that no longer
exists + your PRs conflict on arrival.

1. `git -C ~/Repos/subfloor fetch origin main && git -C ~/Repos/subfloor rev-list --count HEAD..origin/main` -> 0 = carry on.
2. Behind -> take stock BEFORE touching anything: `git -C ~/Repos/subfloor status` (uncommitted) + `git -C ~/Repos/subfloor rev-list origin/main..HEAD` (unmerged commits) + `git -C ~/Repos/subfloor branch --no-merged origin/main` (unlanded branches).
3. Local state that is NOT yours -> another shell''s in-flight work: leave it untouched, take a worktree seat (below). Yours -> land or stash before syncing.
4. Clean (or FnB said go) -> `git -C ~/Repos/subfloor checkout main && git -C ~/Repos/subfloor pull --ff-only`. Stale feature branch -> `git -C ~/Repos/subfloor rebase origin/main`.

## Shared checkout — one clone, many shells

`~/Repos/subfloor` is ONE checkout shared by every shell. Before switching
branches: `git -C ~/Repos/subfloor status` — a dirty tree or a sibling''s
checked-out branch = someone is mid-work. NEVER reset, stash, or branch-switch
under them; take a worktree seat instead:

    git -C ~/Repos/subfloor worktree add ~/Repos/subfloor-wt/<shortname> -b <type>/<short-desc> origin/main

Work in it with `git -C ~/Repos/subfloor-wt/<shortname> …`; remove the seat
(`git -C ~/Repos/subfloor worktree remove ~/Repos/subfloor-wt/<shortname>`)
once its PR is open.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to `main`. Branch first: `git -C ~/Repos/subfloor checkout -b <type>/<short-desc>` (feat/fix/chore/docs).
2. Commit in logical units. End every message with your shell''s trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open the PR (`gh --repo jedbjorn/subfloor pr create`) -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh --repo jedbjorn/subfloor pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh --repo jedbjorn/subfloor pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh --repo jedbjorn/subfloor pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git -C ~/Repos/subfloor push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh --repo jedbjorn/subfloor pr view <merged-pr> --json headRefOid`.
2. `gh --repo jedbjorn/subfloor pr reopen <closed-pr>` -> `gh --repo jedbjorn/subfloor pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> merge/close as directed; delete the recreated branch again.

## Finish before you stop

Bookend to the sync gate. At end of session: `git -C ~/Repos/subfloor status` (uncommitted) + `git -C ~/Repos/subfloor rev-list origin/main..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don''t skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git -C ~/Repos/subfloor restore` / `stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.
4. Took a worktree seat -> remove it once its PR is open.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'issue_reporting',
  'Route engine defects the maintainer way — a subfloor defect is yours to triage and fix in ~/Repos/subfloor (or file on its tracker as backlog); a HOME-substrate defect becomes a flag for the FnB, never an in-place fix. Fires the moment a command fails or lies, a skill contradicts reality, or you work around anything to proceed.',
  'substrate',
  NULL,
  1,
  '# issue_reporting — defects land where they''re fixed

You maintain subfloor: there is no upstream above you to report engine defects
to. A defect either lands in **your backlog** (subfloor) or in **the FnB''s
hands** (home substrate). Route it while the failure is on screen — NEVER
batch to session end.

A workaround IS a signal: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; route it now.

## Boundary — whose defect is it

| Where it lives | What you do |
|---|---|
| **subfloor engine** (`~/Repos/subfloor`: its `sc` + subcommands, `.super-coder/` code, migrations, adapters, boot render, engine skills, `sc mem` API) | You are the maintainer. In current scope -> fix it in subfloor (`git` skill flow). Out of scope -> file it on the tracker (below) so it survives your session. |
| **HOME substrate** (the engine your cwd runs on: its boot doc, its `sc mem`, its launcher) | NOT your work surface. Open a flag (`sc mem flag open "[Engine] <symptom> | Blocker for: <x>"`) + surface to the FnB. NEVER fix in place — home-engine edits are FnB-gated. |
| **Fork reports** (issues filed on subfloor by installed forks: dos-arch, md-converter, ami, rst-c) | Your intake queue — triage like your own findings. |

Unsure which install misbehaved -> check where the failing command ran:
your cwd = home substrate; `~/Repos/subfloor` or a fork = subfloor engine.

## Triggers

Each row = a real engine-defect shape (filed by fork shells doing ordinary
work). Match the left column -> route it.

| You hit | Real case |
|---|---|
| A `./sc` command fails out of the box | `./sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `./sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `./sc lint` said "run `./sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths the seat doesn''t have | `configure_winbox` drove raw `ssh`/`virsh` — neither exists in the broker-only sandbox (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `./sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229) |
| A structural foot-gun keeps re-biting | the cwd trap — bare git resolving to the wrong tree, "my edits vanished" (#225) |

Stale guidance (skill says X, engine does Y) routes the same as a crash.

## Capture — while the failure is on screen

- **where**: which install (subfloor / fork name / home), shell, host seat
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The tracker is public: NEVER paste api keys, tokens, secrets, or private paths.

## Backlog it (subfloor defects out of current scope)

```bash
# 1. dedup — a fork may have hit it first
gh --repo jedbjorn/subfloor issue list --search "<symptom keywords>" --state all

# 2. file — title: <area>: <one-line symptom>
gh --repo jedbjorn/subfloor issue create \
  --title "<area>: <symptom>" \
  --body "<capture block above>"
```

Dedup hit -> comment your repro on the existing issue; do NOT file a duplicate.

## Rules

- One defect per issue/flag. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to the FnB first.
- Filing ≠ unblocked: defect blocks current work -> also open a flag linking
  the issue URL.',
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

> **Work repo:** every git/gh command in this procedure runs against `~/Repos/subfloor` (`git -C ~/Repos/subfloor`, `gh --repo jedbjorn/subfloor`) — NEVER against the home repo your cwd sits in. Addressing contract: the `git` skill.

The reviewer''s job end to end. You are a **different lineage than the code**
— reviewer shells are deliberately booted on a different model family than
the authoring dev, so the review doesn''t share the author''s blind spots ->
read adversarially: disprove the claim that the work is correct, don''t
confirm it. `<self>` = your shell_id.

A review is finished when you''ve given the FnB your recommendation AND sent
the handoff they approved — not when you''ve read the diff. Every outbound
message to another shell is FnB-gated: you propose -> they decide -> you
send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding
before it lands in another shell''s inbox.

---

## Step 1: Load the diff and its spec

Review a diff *against intent*, never in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: the feature''s spec doc (`spec` skill, Step 1 —
  `documents` where `kind=''spec''`). Its done-condition = your yardstick.

Note the **author** — Step 4 proposes a handoff to them. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`); the roster maps
display_name -> shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill''s overlay fans this step out to an adversarial finding-panel.
Load it and apply it on top of this step. Steps 1, 3, and 4 stay yours,
unchanged.

Apply every axis on every review, plus the granted *lenses* matching what
the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with
   existing patterns. Trace the actual code path; NEVER trust the
   description of it.
2. **Edge cases & gaps** — inputs and states the author didn''t handle:
   empty, null, boundary, concurrent, partial-failure, the unhappy path.
   Name what''s missing, not only what''s wrong.
3. **Spec conformance** — diff vs the spec''s done-condition. Flag where the
   implementation diverges from intent AND where the spec itself was silent
   or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

A granted skill that declares it supersedes a lens (says so in its
description — e.g. a fork-local testing skill superseding `test_authoring`)
-> use the superseding skill: it carries the fork''s actual standard.

## Step 3: Open a flag per failure — record, don''t send yet

One flag per real failure, against the feature:
```
sc mem flag open "[Review] <what''s wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill''s default: do NOT pair an outbound message here —
the message is the handoff, and handoffs wait for the FnB (Step 4). Nits go
in the summary, not flags; flag only what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Recommendation -> the handoff it implies:

- fixes on the diff -> message to the **author dev**
- a missing or wrong spec -> message to the **planner**
- clean -> nothing to send

Present the findings (flags + summary) and the drafted message(s) to the
FnB. The FnB rules each finding — defect or intended — and approves what
sends. Then, and only then, send:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate — assume there''s a bug and
  find it; "looks fine" is not a review.
- **Verify, don''t trust.** Re-read the claim against the code; trace the
  path. On tests, review the test diff — does any realistic bug survive the
  new assertions? — do NOT re-run the green suite the dev and CI already
  ran. A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the
  bar. Scope creep in the diff = a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don''t push it.
- **Critique and confirm — never build.** Do NOT patch the author''s code;
  flag it and propose it back.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'source-maintenance',
  'Maintain the subfloor engine source at ~/Repos/subfloor — you are its upstream. Use for changes to its sc, .super-coder code, migrations, adapters, prompts, shell templates, engine skills, update/rollback behavior, or its tracked dogfood state. NEVER for the home repo''s engine.',
  'substrate',
  NULL,
  0,
  '# Source maintenance — the subfloor engine

The engine source you maintain lives at **`~/Repos/subfloor`** (GitHub
`jedbjorn/subfloor`). THERE, `.super-coder/` and `sc` are the product and you
are upstream: fix engine defects in that repo directly; the fork
report-upstream / never-edit-engine procedures do not apply to it.

The engine under your OWN cwd (the home repo''s `.super-coder/`) is a different
install: your memory substrate, NOT your work surface. NEVER apply this skill
to it — home-engine changes are FnB-gated maintenance (see the boot doc''s
PROJECT vs ENGINE).

Every command below runs against the work repo: `git -C ~/Repos/subfloor …`,
or scripts from that root — never from your cwd (the `git` skill has the full
addressing contract).

## Orient

1. Confirm the target: `git -C ~/Repos/subfloor remote get-url origin` ->
   `…jedbjorn/subfloor…`. Anything else = wrong repo; stop.
2. Read subfloor''s active decisions/specs before choosing an architecture.
3. Work from a branch (or a worktree seat — `git` skill). Preserve subfloor''s
   tracked `.sc-state/content.sql`; it is that repo''s dogfood memory, not a
   disposable fork seed.

## Change the right source (paths within ~/Repos/subfloor)

| Concern | Authoritative source |
|---|---|
| Runtime and CLI lifecycle | `sc` plus `.super-coder/scripts/` |
| Harness behavior | `.super-coder/adapters/<harness>/adapter.json` |
| Boot-wide instructions | `.super-coder/templates/boot.md` and `render/compose.py` |
| Shell flavor defaults | `.super-coder/templates/shells/*.json` |
| Engine skill | `.super-coder/assets/skills/<name>/SKILL.md`, then `./sc seed-skills` |
| Schema/system content | a new ordered migration; never rewrite an applied migration except the generated skill seed |
| Subfloor''s own team state | its live DB, then `SC_ADMIN=1 ./sc snapshot` (run in subfloor) |

Flat `_sc` markdown and `AGENTS.md`/`CLAUDE.md` are renders. Never author a
behavioral change in them.

## Downstream contract

A subfloor change reaches installed forks (dos-arch, md-converter, ami, rst-c)
only after it merges and they `./sc update` — keep migrations ordered and
non-destructive, and never assume a running shell inherits a changed prompt or
skill before its next boot.

## Finish

Run focused tests, then from `~/Repos/subfloor`:

```bash
./sc map
./sc render-check
./sc verify
git -C ~/Repos/subfloor diff --check
```

If skill assets changed, run `./sc seed-skills` first (in subfloor). Then the
`git` skill''s finish gate: branch -> commit -> push -> PR -> stop.',
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

> **Work repo:** every git/gh command in this procedure runs against `~/Repos/subfloor` (`git -C ~/Repos/subfloor`, `gh --repo jedbjorn/subfloor`) — NEVER against the home repo your cwd sits in. Addressing contract: the `git` skill.

Load at the start of any session that builds or implements a feature, whether
or not the work is framed as a "spec". A spec governs the work -> this skill
executes it; one should exist but doesn''t -> the `docs` skill authors it first.
Run **Analyze** before touching any code. Blockers / unclear items you can''t
resolve alone -> pause for the FnB.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill).
NEVER auto-pick "the latest" — list the feature''s open specs and choose the
target explicitly:

```
# the feature''s documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec''s task plan (empty = no plan yet):
sc mem get tasks --doc <doc_id>
```

`get documents --feature <id>` lists every spec/doc with `kind`, `seq`,
`frozen`, `task_count`. Active spec = the unfrozen one with `task_count > 0`
— resume it. `task_count = 0` = backlog; starting it (Step 3) makes it
active. More than one open spec and the target unclear -> ask the FnB.

Tasks already exist -> skip to **Step 4** (Track).

Read the entire spec body before going further. Do not skim.

---

## Step 2: Analyze

Surface all three before any planning or code:

### Viability
- Session-completable? Bounded + clear entry points = yes. Multiple layers /
  migrations / unknown dependencies = no -> say so + propose a session-sized
  slice.
- No stated done-condition in the spec -> that is the first unclear item.

### Unclear items
Anything you cannot act on without guessing:
- Ambiguous between two interpretations
- Missing a critical detail (which table? which endpoint? which component?)
- Implies knowledge not stated in the spec

List them and ask the FnB before writing the plan.

### Blockers
Hard stops — prior work not shipped, missing environment state, unresolved
external dependency. Open one flag per blocker:

```
sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

NEVER open a flag for an unclear item resolvable by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec = engaging it to build, so the feature''s `roadmap_status`
(loaded in Step 1) must catch up to reality. Stages:
`brainstorm · long_term · near_term · next · in_progress · shipped`.

- At `brainstorm`/`long_term`/`near_term` + building this session ->
  `sc mem roadmap status <feature_id> in_progress`
- Planning ahead only (no build this session) -> move it to `next`.
- Already at `in_progress` (or further) -> no-op; don''t churn it.

The transition fires because you *act on* the spec — reading one for
reference moves nothing. No spec governing the work (quick UI fix, minor
migration) -> skip all stage handling (see Stance).

### Confirm the work-stream too

Check the feature''s work-stream (`roadmap.project_id` — the Flow-view
grouping). Ungrouped -> assign now so the feature shows in a flow:

```
sc mem roadmap project <feature_id> <shortname>   # ''none'' to clear
```

Stream obvious -> assign; ambiguous -> surface to the FnB; already assigned
-> no-op. Full create/assess procedure (new streams, new features) = the
`docs` skill; this is only the engage-time confirmation.

### Write the task plan

Analysis clear + blockers resolved or accepted -> generate the task list.
Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

Add one task per seq with `sc mem task add` — each write is live in the
shared DB immediately:

```
sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>"
sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, snapshot + render"
```

Then set `current_state` — nothing done yet, next = Preparation:

```
sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill''s overlay replaces this step''s one-task-at-a-time loop with
adjudicated waves. Load it and apply it on top of this step.

At each work session''s start, load the plan:

```
sc mem get tasks --doc <doc_id>
```

Find the first `pending` task -> mark it in progress:

```
sc mem task start <task_id>
```

Work ONLY that task. When done:

```
sc mem task done <task_id>
```

A planned task overtaken by a feature split or re-scope (its work moved to
another feature/spec, never built here) is cancelled, not done:

```
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
```

NEVER mark unbuilt work `done` and NEVER leave it `pending` under a shipped
feature — the task ledger is how a planner answers "is this feature actually
finished."

Re-read the plan (`sc mem get tasks --doc <doc_id>`) and resolve from it:
`last_done` = highest-`seq` `done` task; `next_up` = lowest-`seq` `pending`.
Advance `current_state`:

```
sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

`next_up` NULL = all tasks done -> set current_state to reflect that.

---

## Step 5: Hand off on completion

Verification task passes (`next_up` NULL — the existing done-line) = feature
delivered. As the dev: flip the horizon + hand the paperwork to the planner.
Do NOT freeze the spec or write the doc — that''s the planner (`docs` skill).

1. **Flip the horizon to shipped:**
   ```
   sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag + message the planner with full instructions.**
   `shipped` + an open flag = the honest interim state; the message carries
   everything the planner needs without digging:
   ```
   sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   sc mem message send <planner-shortname> "**[Docs pending] <feature_title> (feature <feature_id>)**

   Spec <doc_id> shipped. Flag SC-### is open — your action required:

   1. **Read the shipped code first.** Write the doc from what actually shipped, not from the spec. Drift happens and decisions get made in production — the spec captures the intent, the code is the truth.
   2. Freeze the spec: \`sc mem doc freeze <doc_id>\`
   3. Write the doc (\`kind=''doc''\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec +
   write the doc." The planner closes the flag when the doc lands.

No planner-flavor shell in this fork -> message nobody; surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

Mid-build, the work grows past the spec''s stated what/why:

- **Small growth** (same mental model, a few more tasks) -> the unfrozen spec
  is living; edit it (`sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the
  granularity test in the `docs` skill) -> do NOT quietly absorb it.
  Recommend a **new spec** to the FnB, authored by the planner against its
  own feature. Significant creep = planning event, not dev improvisation.

---

## Stance

- **Analyze before acting.** Analysis finds the gap between what the spec
  says and what the code does.
- **One task at a time.** Start task N+1 only after task N is verified +
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **Spec too large for one session** -> scope a slice at Preparation: cover
  steps 1–K verifiable now, leave K+1–N pending. NEVER start work that can''t
  be verified before the session ends.
- **current_state always reflects the plan.** Update after every task
  completion — last done + next up. The next session resumes from it without
  reading the full task list first.
- **The stage tracks reality — spec''d work only.** Engaging a spec ->
  `in_progress`; finishing -> `shipped`; already matching -> no-op, don''t
  churn. Work with no spec (quick UI tweaks, minor migrations) is exempt
  entirely: no promotion, no handoff, no creep check. Stage discipline never
  blocks small things.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint',
  'Participant loop for a declared multi-shell sprint — dev, reviewer, or conformance slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, close your unit with a structured unit-report result row, report every transition as a result row. Conformance slot: judge the spec against main pre-freeze, four-way verdicts. No scheduled polling — the planner and the watcher daemon wake you. Local long work (suites/benches) rides ./sc job, never a harness background task. Load when a sprint task message names you a participant.',
  'craft',
  NULL,
  0,
  '# sprint — your slot in a coordinated multi-shell push

> **Work repo:** every git/gh command in this procedure runs against `~/Repos/subfloor` (`git -C ~/Repos/subfloor`, `gh --repo jedbjorn/subfloor`) — NEVER against the home repo your cwd sits in. Addressing contract: the `git` skill.

A sprint = a declared, planner-governed push where shells build dependent
units (B on A, C on B); loop = planner → devs → reviewers → devs → planner,
the shells running the handoffs themselves. This skill is the participant
side: a **dev slot** ("The loop"), a **reviewer slot** ("Reviewer slot"),
or a **conformance slot** ("Conformance slot" — the close-out spec-vs-main
pass). Planner side (declare / monitor / close / report) =
`sprint_orchestration`.
`git`, `review`, `messaging` remain the base disciplines underneath.

You are in a sprint ONLY when a planner `task` message names you a
participant and points at a sprint doc. No kickoff -> this skill is inert.

**You never poll on a schedule.** The sprint is event-driven: the planner
wakes you with `task` rows (often by booting you headless — `./sc run` —
with the task as your prompt), the GitHub watcher daemon turns your PR''s
transitions into `pr_event` rows for the planner, and you report every
state change back as a `result` row. A session that has nothing left to
act on ends; the next event boots the next one. Your memory, archives,
and messages accrete across boots — an ephemeral session is still you.

## The sprint doc — one board, planner-owned

Declaration = a `documents` row (kind `doc`, title `SPRINT: …`). Read:

```
sc mem get docs                     # find it in the index
sc mem get doc --doc <N>            # full body
```

Body contract:

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

The planner is the doc''s only writer. NEVER `sc mem doc edit` the sprint
doc — report state changes to the planner as `result` rows; the planner
updates the board.

## Scoped merge authority

The `git` skill''s rule stands: merging is the FnB''s gate. A sprint grants
one narrow exception — merge only when ALL four hold:

- the PR is for **your assigned unit** in this sprint,
- **all checks are green**,
- your unit''s reviewer declared **review-clean** (every Major/Medium
  finding fixed),
- the sprint doc says `status: ACTIVE` and is not frozen.

Everything outside those four — other PRs, other repos, a red or pending
check, an unreviewed diff, a closed or frozen sprint — is the default FnB
gate, unchanged. The authority dies when the sprint closes; in doubt ->
read the doc; `CLOSED` or frozen -> no merge authority.

## Ambiguity calls

A spec ambiguity mid-unit — more than one defensible reading and the
spec doesn''t pick — is yours to call inside a sprint: pick the reading
that keeps your unit shippable and keep building; don''t stall the chain
waiting for a ruling. Scoped like the merge authority: it covers *how*
your unit meets its spec, never *what* the unit is — an interface
another shell reads, scope growth, or cutting a deliverable stays a
planner escalation.

Every call is reported, never silent: with your next `result` row to the
planner, one line per call —
`ambiguity: <what the spec left open> → chose <reading> — <why>`. No
planner overrule -> your call stands; an overrule arrives as a `task`
row and is worked like a review finding. Repeat your open calls in the
review request (step 6) so the reviewer gates against your reading, not
its own guess.

## Local long work — suites, benches, builds

A harness background task is session-scoped: in a headless boot it dies
with the session, silently — "the harness will wake me" is false there.
Never park a suite, bench, build, or watcher on one. Long local work
goes through `./sc job`, two patterns:

- **Fire-and-wake (default):** `./sc job start [--label <x>]
  [--timeout <s>] -- <cmd>` — the job survives your session; completion
  lands in YOUR inbox as a `result` row, and the normal event loop (your
  next boot''s inbox drain) acts on it. If the sprint waits on the
  outcome, report the job id to the planner, then end the turn.
- **Wait-slice (the result decides THIS turn''s next step):**
  `./sc job wait <id>` blocks ≤550s in the foreground — exit 0 =
  finished · 2 = still running. Between slices drain your inbox
  (`sc mem message check`) and act on what landed — a planner hold read
  only after your suite finished was a stale-slot build — then slice
  again.

Set `--timeout` on anything that can wedge: a deadlocked suite becomes
a bounded failure with a completion row, not a four-hour hole in the
sprint.

**Measurements:** a local bench is exploratory only. A perf number that
gates a merge or decides a design is CI-vs-CI on the same runner, in
one run — local numbers die with sessions and double-launches; they
have contaminated a sprint decision before.

## The loop (dev slot)

At the start of every step: `sc mem message check`. A planner `task` row
(hold, re-sequence, scope change) is authoritative over the board — never
start a step on a stale slot. Report to the planner with
`sc mem message send <planner> "…" --kind result` — every transition,
one line each.

**1. Know your slot.** Your kickoff `task` row carries the doc id and
your unit; read the sprint doc, find your row; note upstream (unit +
shell), your reviewer, and downstream (shell). No upstream -> start
immediately. Embed one line in `current_state`, keep it current as your
status walks, drop it at stand-down:

```
SPRINT doc=<id> unit=<seq> upstream=<seq|none> downstream=<shortname|none> status=<...>
```

**2. Prepare.** Run the `git` skill''s sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn''t merged -> branch
stacked on the upstream shell''s branch + accept the retarget duty in
step 4. Buildable against current `main` -> branch from `main`; stack only
for real code dependencies.

**3. Build.** Your dependency not yet merged? Build and commit locally,
but do NOT open your PR out of turn — the planner''s next `task` row (sent
on your upstream''s merge event) is your turn signal; a booted-headless
session simply ends here and the planner re-boots you when the chain
reaches you. Don''t schedule a watcher; don''t poll. Upstream visibly
stalls from where you sit -> `result` row to the planner; don''t sit
silent behind a stuck link.

**4. Take your turn** the moment your dependency is on `main` (your
kickoff said "start now", or a planner `task` row says so):

- stacked on the upstream branch -> retarget first: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise note your base is gone — same
  discipline as the `git` skill''s stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR — then, in the SAME step:

```
./sc watch pr <owner/repo> <pr-number> --shell <planner-shortname>
sc mem message send <planner> "sprint <doc-id>: unit <seq> pr-open — PR #<n>" --kind result
```

The watch is what makes the loop event-driven: the daemon now turns every
CI conclusion, review, and merge on your PR into a `pr_event` row in the
planner''s inbox. Registration is explicit and happens at PR open — a PR
without a watch is invisible to the sprint.

**5. Babysit CI while live.** `gh pr checks <your-pr> --watch` blocks in
your session at zero scheduled cost — use it while you''re booted; if your
session ends first, the daemon''s red/green event reaches the planner and
a `task` row re-boots you. Never a cron, never a scheduled wake.

Triage before fixing: is the failure in something your diff touches? Does
`main` show the same failure? Does the log say timeout / runner died /
network / flaky test you never touched? Anomalous -> `gh run rerun
<run-id> --failed`, don''t patch healthy code. Anomalous red survives two
reruns -> `result` row to the planner (flaky suite, broken `main`, infra)
and hold — planner''s to fix as a unit, not yours to absorb. When a fix
needs a fix, suspect the diagnosis.

Real red -> read the failure, fix, push, watch again — your loop to run,
not the planner''s to chase. Three honest fix attempts without green ->
`result` row with what''s failing and what you''ve tried. Reruns of flakes
count neither as attempts nor as green: merge authority requires actual
green checks — "it''s just a flake" is never a merge.

**6. Pass sprint review.** CI green -> message your unit''s reviewer
`sprint <doc-id>: unit <seq> ready for review — PR #<n>, checks green`
(+ your open ambiguity calls) and tell the planner `in-review`
(--kind result). Major/Medium findings block: fix, push, re-request; keep
CI green across fix pushes. Low findings = notes for the sprint report,
not gates. Disagree with a severity call -> planner rules; don''t litigate
in the thread while the chain waits.

**7. Merge on green + clean, file your unit report, hand off.** All
checks green + reviewer declared review-clean + boundary above satisfied:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
```

Then close your unit with the **unit report** — your merged-notification
to the planner, grown from one line into ONE structured `result` row,
fixed template:

```
sc mem message send <planner-shortname> "$(cat <<''EOF''
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <what the unit does now, 1-2 lines — the claim, not the diff>
judgements: <ambiguity calls incl. final state (ratified/overruled); ''none''>
issues: <CI reds (real vs anomalous), fix loops, stalls, review friction; ''none''>
deviations: <known departures from the spec''s reading + why; ''none''>
follow-ups: <Lows deferred, TODOs left, cleanup owed; ''none''>
EOF
)" --kind result
```

One report per unit, at merge, mandatory — written NOW, while the unit''s
history is still in your context, never reconstructed later. Every field
answered; `none` is an answer. `deviations` is the honesty field: a
deviation declared here is a judgement for the planner to ratify; the
same deviation found only by the conformance pass is a finding. This is
the one sanctioned multi-line `result` row — transitions stay one-line.

(The daemon also emits the merge to the planner and retires your watch —
the `pr_event` is the wake-up, your unit report is the record; send it
anyway: worker self-reports and daemon ground truth cross-check each
other.) No downstream (last link) -> the planner report is the handoff.
Then clean up local per the `git` skill (re-pin base, delete the branch).

**8. Stand down.** Planner close-out message / frozen or `CLOSED` sprint
doc = sprint over: merge authority gone, default gates resume. Drop the
SPRINT line from `current_state` and confirm in a final `result` row.
Your PR watches retired themselves at merge/close — nothing to tear down.

## Reviewer slot

Gate the units the doc''s `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don''t-trust, review against the unit''s
scope); this overlay changes only pace and severity:

1. **Wake = a review request.** A dev''s `ready for review` message — or a
   planner `task` row booting you headless with the request as prompt —
   is next-in-queue work; a waiting review stalls the chain exactly like
   red CI. Keep a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. No trackers, no scheduled polls.
2. **Major/Medium block; Low informs.** Wrong-behavior / data-loss /
   security / spec-violation (Major) or will-bite-soon (Medium) -> the dev
   fixes now; re-review on the fix push. Style / naming / nice-to-have
   refactors (Low) -> one summary note to the planner for the sprint
   report; Low never blocks merge and you don''t re-litigate it.
3. **Handoffs go direct** — scoped relaxation, same shape as the merge
   authority. The base `review` skill gates handoffs behind the FnB;
   inside an ACTIVE sprint, for your assigned units only: message the
   author dev your findings directly + copy the planner one line
   (`unit <seq>: N major, M medium — with <dev>` or `unit <seq>:
   review-clean`), --kind result. The FnB gate is unchanged everywhere
   else and returns the moment the doc freezes.
4. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev''s merge; never leave it implied.
5. **Stand down** on close-out: drop your SPRINT line, confirm to the
   planner in a final `result` row.

## Conformance slot

The sprint''s final gate: after every unit is merged and `main` is green,
*before* the freeze, the planner boots you to answer the one question no
unit reviewer is positioned to answer — **does what shipped on `main`
actually match the spec?** Unit reviewers gated diffs against unit
scopes; you read the integrated whole. Cross-unit seams — one unit''s
interface drifting from what another assumed, a requirement that fell
between two units — are yours to catch.

1. **Wake = the planner''s kickoff.** Its `task` row carries exactly: the
   spec doc id, the sprint doc id, the merge SHA of `main`, your section
   scope (if the pass is sharded), and the planner''s list of **ratified
   judgement calls**. That list is your only narrative input — it is what
   lets you tell an intentional deviation from a silent one. Everything
   else is artifact: judge the spec against the code on `main` at that
   SHA — never the diffs, never the message trail, never the devs''
   reasoning.
2. **Verdicts.** Every spec requirement in scope gets exactly one:
   - `as-specced` — code matches the spec''s reading;
   - `deviated-intentionally` — matches a ratified judgement call;
   - `deviated-silently` — departs from spec, nobody declared it;
   - `unimplemented` — spec requires it, nothing on `main` does it.
   The last two are findings: attach spec section, code location, and
   Major/Medium/Low — the sprint''s severity bar, same meanings.
3. **Output.** Write a `documents` row — `CONFORMANCE: <sprint title>`,
   kind `doc` (`sc mem doc add`) — holding the verdict table + findings,
   then send the planner ONE line pointing at it:
   `sprint <doc-id>: conformance done — doc <id>, N findings (x Major, y
   Medium, z Low)` (--kind result). Detail in the doc, wake-up in the
   message.
4. **No authority.** You file verdicts; you rule on nothing. Fix units,
   deferrals, and severity disputes are the planner''s; anything that
   changes what the sprint *means* is the FnB''s. Same escalation ladder
   as every other slot.
5. **Stand down** when the planner confirms receipt (a re-run on fix
   units arrives as a fresh scoped `task` row).

## Stance

- No scheduled polling, ever: `task` rows and headless boots wake you;
  `pr_event` rows wake the planner; the sprint doc tells you what a wake
  means.
- Nothing that must outlive the turn rides a harness background task —
  local long work goes through `./sc job`; measurement claims are
  CI-vs-CI on one runner.
- Register the watch in the same step that opens the PR — an unwatched PR
  is a silent link, and silent links revert the sprint to polling.
- Report state transitions (`building → pr-open → in-review → fixing →
  merged`) as `result` rows, one line each — not progress prose. The
  unit report at merge is the one sanctioned multi-line row.
- Merge-on-green+clean and direct review handoffs are scoped authority
  inside a declared sprint, never precedent outside one.
- "All units merged" and "the spec shipped" are different claims — the
  conformance slot exists because only the first is otherwise checked.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — run the pre-freeze conformance pass (review shells judge the spec against main), freeze the doc (revoking all scoped authority), and synthesize the sprint report from unit reports + the conformance doc into the fixed skeleton. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration — governing a coordinated multi-shell push

> **Work repo:** every git/gh command in this procedure runs against `~/Repos/subfloor` (`git -C ~/Repos/subfloor`, `gh --repo jedbjorn/subfloor`) — NEVER against the home repo your cwd sits in. Addressing contract: the `git` skill.

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, interview the FnB for the sprint''s models, boot each worker when its
turn comes, watch the event stream, unblock stalls, close out with a
report. The participant loop (build → PR + watch → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) = the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants'' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the watcher daemon, which sends you `pr_event` rows. Your inbox
watcher wakes you the moment any row lands. Workers are ephemeral,
per-task sessions; you are the one long-lived context in the loop — you
manage, you never load code. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != ''shell'' ORDER BY created_at`.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Units that don''t touch each other run in parallel; keep
chains short and the graph wide where the code allows.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don''t let one reviewer become the whole sprint''s
bottleneck).

**How many shells to deploy = your call, not a formula.** Weigh the
magnitude of the push against the capacity actually available — the shells
that exist, reviewer bandwidth, how wide the dependency graph genuinely
runs — and make the call. More units than shells is fine (units queue
behind the chain); more shells than parallel work is waste.

**The model & provider interview — exactly two questions to the FnB:**

1. **Devs** — which harness and model? One answer; every dev in the
   sprint runs it.
2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

Flavor-uniform by design: shells of a flavor are interchangeable workers,
and one answer per flavor keeps the board readable and the review lineage
coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). Every sprint worker still runs at high
effort. Per-unit model mixing is out of scope — the interview covers the real
need, provider choice per role.

**Resolve each answered route before declaring it.** Lazy-load only the two
choices the FnB made — never trust a display name or translate a provider id by
hand:

```
sc models resolve <devs-harness> <devs-model>
sc models resolve <reviewers-harness> <reviewers-model>
```

Each must return `route:` plus an exact `call:` ending in `--effort high`.
Failure means the selector is not locally callable, the harness lacks a
headless/high-effort seam, or Refresh models has not seen it. Run
`sc models list <harness>` for the local choices; the FnB''s **Refresh models**
button in `/#shells` repopulates the same runtime table. Resolve again after a
refresh. Never silently fall back across a provider or lineage.

Common exact selectors: Claude aliases (`fable`, `opus`) and Codex ids
(`gpt-5.6-sol`, `gpt-5.6-terra`) pass directly. Kimi takes the configured alias
shown by `sc models list kimi` (for example `kimi-code/k3`), never the bare
provider model `k3`.

Write the board as a `documents` row:

```
sc mem doc add "SPRINT: <title>" --kind doc --body-file <draft.md>
```

Body contract (the `sprint` skill quotes the same one — keep it exact):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

Note the returned `document_id` — every task and report references it —
and embed `SPRINT doc=<id> governing` in your own `current_state`; drop
it at close-out.

You are the doc''s only writer: devs report transitions as `result` rows;
fold them into the board with `sc mem doc edit <id> --body-file`.

## Step 2: Arm the watcher, kick off

**Arm your inbox watcher first** — the zero-token wake-up that replaces
every scheduled tracker. On the claude harness, run it as a background
task (it blocks until any message row lands for you, then exits — the
exit is your wake-up):

```
./sc watch inbox        # background it via your harness''s background-task tool
```

**Interactive sessions only.** A harness background task is
session-scoped: in a headless (`-p`) boot it dies with the session,
silently — six sprint stalls traced to exactly this. A headless planner
turn arms nothing: drain the inbox, act, end the turn — the next event
row boots you again. The watcher belongs to the long-lived interactive
planner seat, nowhere else.

Re-arm it every time you finish draining your inbox. On other harnesses
the watcher isn''t available — check your inbox at every task boundary
instead; correctness is identical, latency degrades gracefully. (Strong
recommendation, not a gate: the planner seat runs best on claude/Fable —
the one long-lived, low-volume, high-leverage context in the loop, and
the only seat the watcher fully serves.)

**Kick off** — a `task` row per participant (doc id + the instruction to
load the `sprint` skill + the slot), then boot whoever can start:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot; your merge closes with the unit report. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev with the RESOLVED selector; high is invariant:
./sc run <dev> --harness <devs-harness> -m <devs-model> --effort high
```

`./sc run` renders the shell''s boot doc and drains its inbox
non-interactively — the `task` row you just sent is what it acts on. The
default prompt is exactly that ("check your inbox and act"); pass
`-p` only to say something the task row doesn''t. A shell with a live
session refuses to boot (one shell, one session) — a live session reads
the same `task` row at its next inbox check.

Keep `task` bodies model-neutral and constraint-explicit: point at the
sprint doc, the unit, the spec, and the skill — don''t restate them in
your own phrasing. Constraints live in specs, which every lineage reads
the same way.

This kickoff activates each dev''s scoped merge authority and each
reviewer''s direct-handoff authority for its assigned units.

## Step 3: Monitor the event stream

Your watcher wakes you on every row. On wake, drain the inbox and act:

- **`result` rows** (dev/reviewer transitions — pr-open, in-review,
  review-clean, merged, ambiguity calls, stall reports): fold into the
  board, then move whatever it unblocks. A dev''s merge arrives as its
  **unit report** (the one multi-line `result` row — shipped /
  judgements / issues / deviations / follow-ups): file it whole; it is
  a primary source for the sprint report, and its `deviations` +
  `judgements` lines feed the conformance kickoff. A bare one-line
  `merged` with no report -> nudge the dev (`task` row) for it now,
  while the unit is still in its context.
- **`pr_event` rows** (daemon ground truth — checks green/red, review
  submitted, merged, closed): the wake-up for transitions no worker is
  live to report. Green on an in-review unit -> nothing (the reviewer
  gate holds); red -> re-task the unit''s dev (`task` row + `./sc run`);
  merged -> boot the downstream dev whose turn it is.
- Mark rows read as you fold them; then **re-arm the watcher**.

A worker self-report is never the verdict — green checks + the reviewer
gate are the only ground truth; the `pr_event` stream is what makes a
"done" checkable without a context switch. `gh pr checks <n>` /
`gh pr list` remain your on-demand detail reads — detail lives in `gh`,
the message is the wake-up.

At any moment, be able to answer: which link is the bottleneck? The board
is what the FnB and any rebooted shell reads to re-orient mid-sprint —
fold every state change in as it happens. The board + message table ARE
the sprint''s state: a rebooted planner replays the rows and loses
nothing.

Messages are your steering wheel: every dev checks its inbox at each
step start, and a headless boot drains it first thing — your `task` row
is read before that dev''s next move. Steer with `task` rows — holds,
re-sequencing, nudges, rulings on reported reds. The board records state;
messages change behavior; on conflict your latest message wins -> then
update the board to match.

Dev ambiguity reports (`ambiguity: … → chose …`) get a ruling on
receipt: overrule by `task` row while the unit is still un-merged, or
stay silent and the call stands. Either way log the call + outcome the
moment it arrives — the sprint report lists every one, and calls
reconstructed at close-out from old messages are calls lost.

## Step 4: Unblock

Stalls and the moves:

- **Dev wedged on red CI** (it reports after three failed fix attempts,
  per the `sprint` skill): pair another shell onto it / re-scope the
  unit / pull the failing part into a follow-up unit so the chain moves.
- **Anomalous red** (flaky test, runner death, `main` red underneath — the
  dev''s job was to rerun and report, not patch healthy code): fix the
  cause as its own unit, or hold the chain while infra recovers; rule by
  `task` row when the dev may proceed. Don''t count phantom reds against
  the dev''s fix attempts — and don''t let anyone merge over one; green
  means green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain''s tail.
- **Merge broke `main`**: `task` row to all devs to hold merges, insert a
  fix unit at the front of the chain, resume when green.
- **Review stall** (unit sitting `in-review` while its reviewer is idle):
  boot the reviewer — `./sc run <reviewer> --harness <reviewers-harness>
  -m <reviewers-model> --effort high`; its inbox holds the review request. Still stuck
  -> reassign the unit to another reviewer. Severity dispute (dev says
  Low, reviewer says Medium) -> rule by message immediately — a chain
  waiting on a classification argument is pure loss. Dispute about what
  the unit *should do* -> FnB.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.
  The bottleneck question in Step 3 is what surfaces a dead link.
- **Re-sequencing**: edit the board + `task` row to *every* affected dev
  with its new slot — a dev acting on a stale slot is worse than a paused
  one.
- **Every worker boot failing at once**: check provider auth and spend
  limits BEFORE debugging the engine — a monthly cap presents as a
  fleet-wide boot failure and costs an hour of misdiagnosis. Pause at a
  clean gate (units green, nothing mid-merge), surface to the FnB (auth
  switch is theirs), resume where the board says you stopped.
- **CI queue clogged at the tail**: a queued verify whose commit a later
  stack head already supersedes is pure queue time — cancel it (`gh run
  cancel`) and let the head''s run stand for the stack. Cancelling
  anything to protect a measurement run is allowed but logged: rationale
  in the board or a `result` row, and re-run the cancelled check after.
  Green means green — cancellation never substitutes for a verdict on
  what still needs one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can''t unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. **Run the conformance pass — before the freeze.** "All units merged"
   and "the spec shipped" are different claims; this is where the second
   one gets checked. Boot review shell(s) — reviewer lineage, the
   sprint''s reviewer harness/model; one shell by default, shard by spec
   section only when the spec genuinely exceeds one context:

   ```
   sc mem message send <reviewer> "SPRINT <doc-id>: conformance pass — spec doc <spec-id>, main @ <merge-sha><, sections <scope> if sharded>. Ratified judgement calls: <list — the only narrative input>. Load the sprint skill (conformance slot)." --kind task
   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model> --effort high
   ```

   The shell judges the spec against the code on `main` — never the
   diffs, never the trail — and files four-way verdicts (`as-specced` /
   `deviated-intentionally` / `deviated-silently` / `unimplemented`) as
   a `CONFORMANCE: <title>` doc + a one-line `result` pointer.

   **Rule on the findings** — they route like any sprint event:
   - **Major** -> insert a fix unit at the front of the chain under
     still-ACTIVE authority (this is exactly why the pass runs before
     the freeze — a reopened sprint re-grants nothing); re-run the pass
     scoped to the fix when it merges.
   - **Medium** -> your judgment: fix unit now, or defer with the FnB
     told explicitly in the report''s Verdict.
   - **Low** -> Deferred & Follow-ups; never holds the close.
2. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant''s scoped authority ends with it.
3. Message every participant (`task` row): sprint closed, default merge
   gates resume.
4. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
5. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Fixed skeleton — fill it by **reasoning over the unit reports and the
   conformance doc against each other** (a dev''s `deviations: none`
   meeting a `deviated-silently` finding on its unit is exactly what the
   report exists to say), not by pasting either verbatim:

   | Section | Primary source |
   |---|---|
   | `## Verdict` | your synthesis — five-second answer: N units / N PRs, conformance state (conforms / conforms-with-deviations / gaps-found), main green, anything deferred-with-eyes-open |
   | `## Units Shipped` | the board — final table, planned vs. actual order |
   | `## Judgements Made` | unit reports (`judgements:`) + your rulings + severity disputes; every call with its final state |
   | `## Spec Accuracy` | conformance doc — verdict table + findings, cross-checked against unit reports'' `deviations:` |
   | `## Issues Encountered` | unit reports (`issues:`) + the `pr_event`/stall trail — CI fights, anomalous reds, re-scopes, unblocks |
   | `## Deferred & Follow-ups` | unit reports (`follow-ups:`) + reviewers'' Lows + conformance Lows + anything cut — one actionable backlog, the next sprint''s seed list |
   | `## Spec Debt` | judgement calls that should be written back into the spec + places the spec was silent, wrong, or contradictory — the input to the spec-update pass |
   | `## Metrics` (optional) | mechanical from the trail: review cycles per unit, CI reds, boots per shell, planned vs. actual merge order |

   The `kind != ''shell''` message trail remains the in-order backbone;
   the CONFORMANCE doc stays alongside as the report''s evidence trail.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
6. Settle the bookkeeping — close the sprint''s flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- Local long work rides `./sc job` (see the `sprint` skill) — a job''s
  completion is a `result` row like any other wake-up. A hand-rolled
  nohup/poll waiter anywhere in the sprint is a defect: one sprint''s
  hand-rolled waiter carried a self-match bug that masked a dead bench.
- You manage; you never load code. Your context grows at coordination
  density — the workers'' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- The conformance shell files verdicts, never rulings — Major/Medium/Low
  routing stays yours; what the sprint *means* stays the FnB''s.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
