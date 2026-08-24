-- 0234 — reseed CI fallback authority.
-- Decision #240: unavailable local verification defers to observed registered-PR
-- checks only after implementation is complete; Planner never mutates the
-- package/toolchain seat while that trustworthy fallback exists.

BEGIN;

UPDATE shells
SET system_prompt=replace(system_prompt, 'Run the smallest affected test targets that prove the changed behavior and realistic failure paths. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI; the PR is review-ready only when its required CI checks are green. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.', 'Run every available smallest affected test target that proves the changed behavior and realistic failure paths. Complete the implementation before using CI fallback. If a focused local gate cannot execute because the selected interpreter, runner, or declared dependency is unavailable, record the exact evidence, run the remaining checks, then push/open the PR and register it when the workflow provides registration. Required checks pending -> wait; red -> diagnose, fix, and push; green -> review readiness. A test assertion, source-caused collection error, red CI result, or incomplete code is a failure, never unavailable infrastructure. No configured checks or an untrustworthy watcher after one bounded read -> block because no trustworthy seat remains. An optional browser-capability skip is informational and non-failing. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.')
WHERE flavor='dev'
  AND instr(system_prompt, 'Run the smallest affected test targets that prove the changed behavior and realistic failure paths. When the repository declares an authoritative full-suite CI gate, do not run the repository-wide suite locally merely to duplicate CI; the PR is review-ready only when its required CI checks are green. Run the full suite locally only when no authoritative CI gate exists, the change crosses test/CI/harness infrastructure, the FnB explicitly requests it, or bounded diagnosis requires it. Never start a competing repository-wide suite on a shared host.') > 0;

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
   re-run the available smallest affected targets, lint, and the spec''s
   done-condition yourself; never use bare `sc test` merely to duplicate
   configured CI. If a local gate cannot execute, record the exact seat
   evidence and use the boot fallback only after the merged implementation is
   complete: open/register the PR, wait while required checks are pending, fix
   red, and treat green as proof for the unavailable gate. No trustworthy
   local or registered-PR seat -> block. "Agent says tests pass" is never
   verification. Pull diffs yourself (`git -C <worktree> diff`); NEVER
   adjudicate pasted diffs/output — pastes are lossy.

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
  surface. Verification remains yours through the primary tree or the boot
  registered-PR fallback; never install a toolchain into the agent tree.

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

Load before any feature/spec/roadmap build. Missing spec -> author via `docs`.
Analyze before code; ask FnB on ambiguity, flag hard blockers. `<self>` = your
shell id.

## 1. Select the spec

Never auto-pick the latest document. Read the complete selected body + task
ledger:

```text
sc mem get documents --feature <id>
sc mem get documents --doc <doc_id>
sc mem get tasks --doc <doc_id>
```

The list includes `kind`, `seq`, `frozen`, and `task_count`. Resume the unfrozen
spec with tasks; zero tasks = backlog and needs the plan below. Multiple
plausible specs -> ask FnB. Existing tasks -> track the first unfinished one.

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

Building now moves `brainstorm|long_term|near_term` to `in_progress`; planning
ahead -> `next`; matching/later stages and reference reads do not move.

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

Final Verification follows the boot `TESTING POSTURE`. Complete code; run every
available focused proof; use observed registered-PR checks only for an
unavailable local gate: pending -> wait, red -> fix, green -> review. No checks
or untrustworthy watcher after one bounded read -> block; an optional browser
skip is non-failing. Require every In Scope done-condition + Anticipated User
Activity contract; unexpected reach, weakened hardening, or crossed tenancy
fails. A large spec may stop after a verified task slice; leave later tasks
pending and state the next one.

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
  'dev_kit',
  'Run fork-owned dev-kit hooks and diagnose host or Docker provisioning states without inferring project policy.',
  'substrate',
  NULL,
  0,
  '# dev_kit — target-aware project tooling

`deps`, `test`, `lint`, and `typecheck` are invariant exact-execution hooks on
both host and Docker seats. The fork owns their argv in the tracked
`.subfloor/dev-kit.json`; the engine validates the declaration, selects the
invoking Git checkout, runs that argv without a shell, preserves child output
and status, and reports the selected checkout, cwd, seat, and executable.

The engine never infers manifests, languages, package managers, tools, file
sets, or acceptance policy. It never installs privileged host packages. A
missing hook is intentionally non-successful, not a request for a fallback.

From a checkout, bare `sc` uses the managed cwd-resolving wrapper on the host
and the equivalent baked wrapper in Docker.
<!-- sc-root-only: the tracked launcher is the fallback when the managed wrapper is unavailable -->
`./sc` remains valid and behaviorally identical for root-checkout commands.

## Read the active seat

Read the boot document''s execution-context section before acting. It is the
authority for this shell''s active seat.

- **Host:** commands and project processes run directly on the host. Respect an
  existing supervisor (`pm2`, `systemd`, or `make`) and bind ad-hoc dev servers
  to `127.0.0.1:$SC_DEV_PORT` unless the task requires another interface.
- **Docker:** the checkout is bind-mounted at its host path. Run a dev server on
  `0.0.0.0:$SC_DEV_PORT`; the published host URL is
  `http://127.0.0.1:$SC_DEV_PORT`. The FnB''s host-supervised app is a separate
  instance.

Host lifecycle remedies such as `sc launch` and `sc enter --devkit-repair`
must be run from a host terminal. If this shell is in Docker, exit the container
before using them. Never restart the FnB''s host stack from a sandbox shell.

## State and remedy contract

User-facing dev-kit output uses these states consistently:

| State | Meaning | Remedy |
|---|---|---|
| **absent** | The message `no fork dev kit declared` means the declaration is absent; the named hook may instead be unconfigured. The engine baseline remains usable, and an absent hook uses exit `78`. | Add or correct the fork-owned declaration only if the fork needs that capability. |
| **invalid** | The declaration, path, mount, image identity, or invocation failed validation before trusted execution. Hook configuration errors exit `64`. | Correct the reported fork-owned file or invocation, then retry the same command. |
| **failed** | A declared hook or provisioning attempt started but did not succeed. Docker retains the container and local attempt evidence and writes no ready receipt. | On the host, inspect `.sc-state/local/dev-kit/`, retry with `sc launch --no-build`, or enter `sc enter --devkit-repair`. |
| **stale** | A declared Docker provision step has no current receipt, or its fingerprint no longer matches the declaration, inputs, checkout, image, or labels. Normal entry is blocked. | On the host, run `sc launch`; if provisioning fails, use the failed/repair path. |
| **advisory** | A declared native apt package or package-dependent candidate failed while the engine baseline remained runnable. Core shell entry stays available; `native_packages=advisory` and `fork_readiness=degraded` are not blocker states. | From the fork root, run `make dos-admin`, inspect the named status/proof evidence and selected baseline, then submit a reviewed tracked remediation. Never infer, rename, unpin, or substitute a package. |
| **ready** | The selected hook can run, or Docker has a current receipt for the exact provision fingerprint and pinned image labels. | Continue with the declared hook or normal `sc enter`. |
| **repair** | An explicit retained-container session is open without a readiness claim. Normal shell entry remains blocked. | Diagnose the declaration/hook, exit to the host, rerun `sc launch`, and require a ready result. |

An unavailable executable exits `126`; a started child keeps its
shell-observable status. `SC_DEVKIT_ROOT`, `SC_DEVKIT_SEAT`, and
`SC_DEVKIT_HOOK` tell fork-owned code which checkout, seat, and hook the engine
selected.

## Ownership layers

- **Engine baseline:** the shipped sandbox image and generic runner. Its baked
  tools are mechanisms, not a promise that a fork uses them.
- **Native packages:** an optional bounded `sandbox.packages.apt` array of exact
  `NAME` / `NAME=VERSION` atoms. The engine installs the canonical array over
  the immutable baseline and proves every package in the final image. Pass =
  the format-version-2 receipt matches the current labels, proof, and checkout.
- **Fork extension:** an optional fork-owned Dockerfile and mounts declared in
  `.subfloor/dev-kit.json`. The Dockerfile must extend `SC_BASE_IMAGE`; the
  engine passes the exact package-layer ID when native packages are declared.
- **Checkout setup:** an optional fork-owned provision hook plus explicit input
  files. A successful receipt is keyed to the declaration, executable, inputs,
  checkout identity, extension image identity, labels, and seat.
- **Host prerequisites:** Git, Docker, language runtimes, credentials, and
  privileged packages installed by the operator. The engine reports missing
  prerequisites; it does not elevate or install them.

Read `.subfloor/dev-kit.json` and its executable before invoking a hook. Run
`sc deps` first only when the declaration makes `deps` the fork''s dependency
policy. A fork may choose a virtualenv, npm, another tool, or no dependency step
at all. In Docker, fork code must treat an out-of-checkout interpreter as
host-managed and shared: verify it, but never install into it.

Treat package advisories as capability evidence, not authorization to edit a
live declaration or restart the sandbox. Inspect `.sc-state/local/dev-kit/` and
the System-managed Flags record. Pass remediation back to the FnB as a reviewed
tracked change; only the FnB authorizes downstream materialization and cutover.

## Verification-seat fallback

A local gate is unavailable only when the selected interpreter, runner, or
declared dependency cannot execute it. A test assertion, source-caused
collection error, red CI result, or incomplete implementation is a failure,
not unavailable infrastructure.

After implementation is complete, a Developer records the selected seat,
executable, and failure; runs every available affected check; opens/registers
the PR; and uses its observed required checks for the unavailable proof.
Pending -> wait for the native fact. Red -> diagnose, fix, and push. Green ->
the proof is complete and review may start. No configured checks or an
untrustworthy watcher after one bounded read -> no trustworthy seat; block the
lane. An optional browser-capability skip is informational and non-failing.

When this registered-PR fallback exists, a Planner NEVER runs `sc deps`,
installs packages/runtimes, edits `.venv` or the dev-kit declaration, or starts
a repair/restart to manufacture a local seat. Keep ownership with the
Developer through the CI route. Only the FnB may authorize a separate tracked
toolchain or environment change.

## Engine-baseline tools

The standard sandbox image includes `rg`, the `sqlite3` CLI, `curl`, Node 22,
npm, pinned `uv` + `pytest`, and Playwright with Chromium at
`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`. These are available mechanisms,
not inferred lifecycle hooks. On shell boot, an existing assigned-checkout
`.venv/bin` precedes these baseline tools while the checkout root remains first
for bare `sc`; pass = project tools use that checkout''s interpreter without the
engine creating or repairing its environment. A missing `.venv` remains fork
policy through declared hooks. Frontend tools such as `svelte-check`, `tsc`,
and vitest still come from fork-owned dependencies and run only through
declared policy.

After `sc update` changes image-owned tools, run a normal `sc restart` from the
host to build + activate them. `restart --no-build` deliberately retains the
selected image. NEVER install pytest on the host to repair a Docker shell.

## Postgres sidecar (app-only)

When a fork sets `"pg": {}` in `.super-coder/instance.json` (`sc pg-init` adds
it), `sc launch` starts a `postgres:17` sidecar and forwards `DATABASE_URL` into
Docker. This is only the fork application''s database. The engine memory DB is
always SQLite and never reads `DATABASE_URL`.

Inside Docker the app connects by the container hostname in `DATABASE_URL`, not
`127.0.0.1`. The fork owns its Postgres driver and its declared setup/test
hooks. Data persists in the install-owned Docker volume.

An unset `DATABASE_URL` means no sidecar is configured. A set URL with an empty
schema means provision the real app DB through the fork''s migrations and
bootstrap; it is not a blocker and is not permission to create a second
throwaway database.

## Stance

The declaration and active boot seat are the truth. Diagnose the exact state,
use the remedy for that seat, and require observable execution evidence rather
than command narration. Do not convert an absent capability into inferred
policy or a repair session into a readiness claim.',
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

Use for an actionable armed-Sprint assignment. Use the simplest path supported
by current durable state. Treat ownership, lifecycle, writes, and handoffs as
hard boundaries. Repeat a read only when later activity could have changed it
or a command requires live revalidation.

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

Accepting starts ownership; decline concretely. After an informational message,
`accept` marks the message read and does not change Sprint or work-unit state.

For an unusable bookkeeping receipt, retry the exact command once, then use its
normal read surface once to prove the postcondition. For informational `accept`,
prior inbox presence + absence of that exact message id proves the read landed;
name the defect next handoff. NEVER infer assignment ownership, review outcome,
merge authorization, lifecycle/work-unit transition, governing revision, PR
head/green state, or cleanup authority. An unproved postcondition stops.

Assignments and review requests use Force-new delivery; verdicts and PR-event
wakes use Re-enter. Delivery waits for a natural boundary; the runtime owns
bundling, rotation, and recovery. Stop after a successful typed handoff.

## Bound the lane

Read assignment, output, bound revision, dependencies, roles, worktree, grant,
and judgments. Own one active unit; never start another lane or edit another
shell''s worktree. Resolve ambiguity to shippable in-scope work + rationale. Ask
Planner before changing boundary, interface, deliverable, priority, or scope.

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

Stable key = recipient + exact body + intent + reply + scope. Reuse it only for
the same failed/ambiguous write; when any of those fields changes, use a new
key. Keep bodies near 6,000 characters and below
8,000; run `wc -m < <path>`. Handoff completes only when the command exits
successfully and confirms durable state + wake. If a command is rejected or
transport fails, correct/retry. If relay itself fails, give FnB command +
evidence + impact + recommendation; invent no alternate protocol.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

Scratch proof/diffs/reports -> gitignored `shared/sprints/sprint-<n>/`; never
commit/PR them. Durable judgment -> `record-review`; reports -> `sprint_reports`;
decisions -> relay.

## Build and verify

Sync + branch; implement the smallest complete change. Per boot `TESTING
POSTURE`, finish code + run every available smallest affected gate. If the
selected interpreter, runner, or declared dependency cannot execute one,
record exact seat evidence; the registered PR supplies only that proof. Test
assertion/source collection red or incomplete code = failure. Optional browser
skip = non-failing. Keep external calls outside DB transactions; preserve
durable identities and append-only evidence. Record failures, anomalies,
retries, review friction, and departures for closeout.

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

Register complete code even when a local gate is unavailable; registration
obtains evidence, not review. After `register-pr` succeeds, retain ownership;
Red/green/closed Re-enter wakes continue. Required checks: pending -> native
wake; red -> fix/push; green -> judge/request review; none or untrustworthy
watcher after one bounded read -> report + block. Follow context: armed -> fix
red + judge/pass green; paused -> fix red now + judge green, review after
resume; no active Sprint -> fix red if needed + no action on green.
Planner/Reviewer get none.

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

1. Finish readiness judgment + available local proof; require observed green.
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

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Use after `sprint_prep` arms Sprint. System records; Reviewer judges/reports;
Planner structures/controls; FnB board override = decision #46.

Use the simplest path supported by current durable state. Treat authority,
lifecycle, writes, and handoffs as hard boundaries. Repeat a read only when
later activity could have changed it or a command requires revalidation.

## Route the entry

Load `sprint_pln` on every entry, then classify:

| Trigger | Route |
|---|---|
| Sprint decision, merged-work handoff, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; handle that message. |
| Engine-wide completion or cleanup receipt | Inspect receipt + terminal state directly; it is informational—do not run the Sprint inbox, accept it, or close again. |
| Live FnB instruction | Act under board override; name FnB authority in durable evidence. |

Do not poll. Armed runtime owns scheduled dispatch + unread wake recovery;
registered-PR watcher owns subscription observation. Developer-owned subscriptions send
red/green/closed facts to Developers, never Planner.

Assignments/review requests use Force-new delivery; Planner-bound results use
Re-enter. Delivery waits for a natural boundary; runtime owns bundling,
rotation, recovery, and coordinate mode. Stop after a successful typed handoff.

## Durable running loop

Read only trigger-required lifecycle, unit, dependency, route, PR, expectation,
and anomaly facts. Browser presence is not progress.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
sc sprint dispatch --sprint <id>
```

Dispatch every dependency-ready lane; returned ids are wake identities.
Disposition + messages are release facts. Stable assignment generations and
occupied lanes make dispatch repeat-safe.

Accept/decline only actionable items. After acting on an informational message,
run `accept`; it marks the message read and does not change Sprint or work-unit
state.

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint. Retry the exact command once, then use its normal read surface once to
prove the exact postcondition. For informational `accept`, prior inbox presence
+ absence of that exact message id proves the read landed. Continue under that
proof + name the receipt defect in the next normal handoff. NEVER use this
recovery to infer assignment ownership, review outcome, merge authorization,
lifecycle/work-unit transition, governing revision, PR head/green state, or
cleanup authority. An unproved postcondition stops.

- Keep dependencies as hard sequence; restructure current projection under
  Planner authority, record why, and never rewrite completed history.
- Developers own local/PR proof, review/fix/merge. Complete code + unavailable
  local gate -> registered CI: pending wait, red fix, green review; browser skip
  is non-failing. With fallback, Planner NEVER mutates packages/toolchains or
  runs repair. No checks/untrustworthy watcher after one read -> blocker.
  Reviewers own verdicts/conformance; do not proxy handoffs/judgments.
- Record Reviewer decision id + exact action + receipt; never rewrite rationale
  as Planner judgment.
- Mid-Sprint spec edits require owning Planner/FnB + durable Reviewer decision.
  Record old/new revision hashes; binding changes only when the decision says so.
- Use native wakes. Start no recurring loop, scheduled job, manual participant
  boot, or external watcher. One stalled-gate inspection is allowed:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop. Act on its bounded watcher evidence,
then return to native delivery.

## Relay contract

Unit question/blocker requiring reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` for a blocked lane. Cross-unit, closeout, or external
authority rulings are Sprint-level:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Answer through the original message; the server inherits its scope, so never
add `--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm reply, then `accept` incoming. A missing answer stops at the decision
boundary; unread recovery re-wakes, so send no duplicate. Choose a stable key
for recipient + body + intent + reply + scope; reuse it only for that identical
write. When any of those fields changes, use a new key.

Keep bodies near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. Handoff completes only when the command exits successfully
and confirms durable write + wake. If a command is rejected or transport fails,
correct/retry safely. If relay itself fails, give FnB attempted command +
durable evidence; invent no alternate protocol. Relay Developer integrity
evidence, impact, and recommendation to Reviewer. Send required context before
pausing because paused Sprint relay is unavailable.

Keep scratch review notes, diffs, evidence, reports, and proof in gitignored
`shared/sprints/sprint-<n>/`; never commit/branch/PR them. Durable judgment,
reports, and decisions belong in `record-review`, `sprint_reports`, and relay.

## Reviewer decisions and Planner actions

Review/conformance/re-enter/abort judgment remains Reviewer-owned. Planner
independently owns operational plan structure: pause-safe recall, cancellation
of unreleased scope, reassignment, repeated task lanes, and route changes.

For a required-reply Reviewer→Planner Re-enter decision, keep this order:

1. Re-run `sc sprint inbox --sprint <id>`; verify assigned Reviewer + retain id.
2. Send linked acknowledgement:

```text
sc sprint send --sprint <id> --to <reviewer-shortname> --body-file <path> \
  --intent information --reply-to <decision-message-id> \
  --key <stable-control-reply-key>
```

3. Require the reply command to confirm its durable message and wake; retry the
   same command/key if ambiguous.
4. Accept the decision:

```text
sc sprint accept --sprint <id> --message <decision-message-id>
```

5. Only after acceptance, execute the requested transition without
   re-adjudicating it. The linked reply must precede any pause or
   abort that makes the Sprint relay unavailable.

Record decision id + reply, acceptance, and action receipts. Clean conformance
closes atomically and sends informational receipt; no reply/accept is needed.

The FnB board-level override from decision #46 remains distinct. If action
fails lifecycle/authority/disposition precondition, send refusal + durable state
to Reviewer (or FnB for override), substitute nothing, and stop.

### Pause or resume

Pause for Reviewer decision or safe Planner restructuring; preserve partial
artifacts, interrupt intent, judgment, and evidence:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Resume only after recording recovery/restructure and reconciling native runs,
unread messages, wakes, units, PRs, capacity, and spec drift:

```text
sc sprint resume --sprint <id> [--reason <validated-reconciliation-reason>]
```

Preserve the current conformance owner on ordinary resume. Replace that owner
only while paused, only with an eligible participating Reviewer, and always
record a reason:

```text
sc sprint resume --sprint <id> \
  --conformance-reviewer-shell <replacement-shell-id> \
  --reason <ownership-replacement-reason>
```

Require the receipt and board projection to show the replacement owner and a
new ownership generation before treating the Sprint as resumed.

Exhausted recovery wake = bounded manual evidence: preserve unread message +
failed wake, involve FnB, create no recursive fallback. Drift informs but never
silently blocks resume.

Aborted-Sprint PR ownership repair belongs to the originating Planner. Keep
replacement Sprint paused; establish old/new identity. The originating Planner
may reconcile that identity (FnB may override):

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

It refuses a live source Sprint or target Sprint, a non-originating Planner,
invalid/owned target, and closed-unmerged PR. Receipt records owners, live head,
authority, and merged completion when applicable. Require a separate Reviewer
decision before resuming.

### Modify, recall, repeat, reassign, or reroute

Cancel unreleased scope with retained terminal reason/Reviewer id:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reason>
```

Edit an unreleased lane; omitted fields stay, `--clear-dependencies` means none:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  [--developer-shell <id>] [--reviewer-shell <id>] [--title <title>] \
  [--expected-output-file <path>] [--task <task-id>] [--wave <n>] \
  [--depends-on <work-unit-id> | --clear-dependencies] \
  [--output-kind code|report-only|no-code]
```

Never edit a released lane in place. Pause → recall unmerged lane → replan →
resume:

```text
sc sprint pause --sprint <id> --reason <restructure-reason>
sc sprint recall-unit --sprint <id> --work-unit <id> --reason <obsolete-reason>
sc sprint replan-unit --sprint <id> --work-unit <id> <changed-fields>
sc sprint resume --sprint <id> --reason <validated-replan-reason>
```

Recall preserves message/event history, returns only unmerged work to planned,
and refuses terminal/PR-bound work. PR-bound work stays; plan replacement or use
reconciliation. Resume creates fresh assignment generation. One spec task may
govern repeated verification/replacement lanes; each lane lists it once. Do not
duplicate the spec task.

Close a released lane terminal when its work finished out-of-band (a PR that
merged while paused) or its lane is abandoned — the PR-bound case recall
refuses:

```text
sc sprint resolve-unit --sprint <id> --work-unit <id> \
  --to completed|cancelled --reason <reason>
```

Paused-only; retires the lane''s open expectations, supersedes its PR links
(registration kept for reconcile-pr), and wakes both seats. Recall+replan
ships revised work; resolve only closes the lane.

To change future assignment/review model, pause armed Sprint, clear released
expectation, then validate + replace route:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute directly. Paused reroute retires the
participant''s own released expectations and queues fresh ones on the
replacement route at resume; another seat''s open turn still blocks. Existing
chats/runs remain history; next Force-new delivery uses replacement. Reroute
declared participants only. On decline, preserve reason and choose
replacement from current capacity; ask Reviewer only if review/conformance
judgment changes.

### Re-enter after conformance

Reviewer decision names findings; governing tasks (existing for same scope,
new title/description for new scope); and grouping, waves, dependencies,
routing, capacity. Preserve it; do not absorb extra/post-Sprint scope or
maximize occupancy.

```text
sc mem task add "<task-title>" --feature <feature-id> \
  --doc <governing-spec-document-id> --seq <next-seq> \
  --desc "<task-description>"

sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

Reuse task for exact repair/repeat; add only genuinely new scope. Bind every
task, then confirm routes + dependency graph and capacity plan match the
decision. Planner may reassign or reroute for operational capacity; tell
Reviewer if conflict changes scope/judgment. Release ready lanes with
`sc sprint dispatch --sprint <id>`. Engine sends next delivery-terminal wake;
Planner does not initiate conformance.

### Conclude or abort

The clean `record-conformance` command atomically stores conformance, follow-ups,
Reviewer-authored final report, completion, and informational engine-wide
Planner receipt. On Re-enter, verify Sprint/reports/outcome/completed state.
Do not run `complete`; notification is informational because closure is already
terminal. Planner does not author a second report. The originating Planner and
report-authoring Reviewer remain open. Do not manually close peer chats. Pause,
abort, re-entry, failed conformance, and rejected fallback retain no-cleanup
behavior.

The initial completion receipt reports `cleanup_state=pending`. Delivery is
finished, but managed worktrees are not reusable. Stop for the engine-wide
cleanup receipt; do not poll or manually reset participant trees. On
`cleanup_state=succeeded`, treat the slots as reusable. On failure, inspect once
and retry only after correcting the named condition:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
```

Require `created`, cleanup request id, action, exact target ids, and aggregate
projection. Reuse the key only for the same request. FnB alone may add
`--adopt-legacy` for a completed Sprint with no scheduled targets; neither caller
supplies a path.

Do not compile or editorialize by default; Reviewer owns evidence/report. Only
FnB-directed fallback may run:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Abort only on Reviewer decision/FnB override; it is terminal and deletes
nothing.

## Handoffs and stop

Planner receives no PR-event wakes. Never dispatch the next wave from merge
observation. The merged-work handoff wake is the only normal next-wave dispatch trigger.
On it:

1. Run `sc sprint inbox --sprint <id>`; inspect merged handoff + unit/dependency state.
2. Handle earlier informational items and `accept` each, including handoff.
3. Finish reconciliation and Planner bookkeeping; no work remains.
4. As literal final action run `sc sprint dispatch --sprint <id>`.
5. Require durable assignments + New wakes, then stop. Run no trailing command.
   Empty dispatch remains final; investigate only on a later durable wake.

On an initial clean completion receipt, verify the named Sprint is terminal and
record `cleanup_state=pending`; run no close command. Stop until the
engine-authored cleanup success or failure receipt arrives. The Planner does
not author a second report, accept an actionable handoff, poll cleanup, or ask
another role to reset a worktree.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
