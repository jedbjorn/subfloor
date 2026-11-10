-- 0282 — skills dedupe and posture pass. Seeds two skills moved out of the
-- boot templates (spec_authoring for the Planner pack, spec_execution for the
-- Developer pack) and reseeds every engine skill this pass rewrote:
-- cli_orchestration, curate, flag_sweep, git, git_cleanup, harness_readiness, issue_reporting, remote_seats, self_update, snapshot, sprint_dev, sprint_pln, sprint_protocol, sprint_rev, tailscale_diagnostics, windows_testing.
-- Packs are reconciled from templates/shells/*.json on update; the two grants
-- below make a fresh migrate converge without a reconcile. dev_kit is
-- fork-owned: only the exact untouched prior starter is upgraded. No schema
-- change; full-body UPSERTs by name keep skill_id and grants. Idempotent.
BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'spec_authoring',
  'Load before you create a roadmap feature, write or revise a spec, set or check acceptance criteria, bring a spec into a Sprint, move or freeze a spec, or write a shipped feature''s doc. Holds the feature/spec/doc model, posture-then-challenge, the acceptance feasibility walk, the spec contract, and the revise/freeze/document commands. Planner.',
  'workflow',
  NULL,
  0,
  '# spec_authoring — from objective to resolved contract

## Features, specs, and docs

A feature is a `roadmap` row, alive from `brainstorm` onward. Specs
(`documents`, kind `spec`) hang off the feature, ordered by `seq`; a feature
may hold several unfrozen specs at once, and freezing one never gates the
others. A spec is **shipped** when frozen, **active** when unfrozen with rows
in `spec_tasks`, otherwise **backlog**. The **doc** (kind `doc`) is the
feature''s readable face, written when its first spec ships. Bodies live in
the DB; never author a loose `.md` as the canonical copy.

Every feature belongs to a work-stream (`projects`). On every feature create,
spec author, or spec update, assess it in the same act: `sc mem get projects`;
a new feature -> `sc mem roadmap add "<title>" --project <shortname>`;
ungrouped -> `sc mem roadmap project <feature_id> <shortname>`; no fit ->
`sc mem project add <shortname> "<title>" --purpose "…"` then assign. Ask the
FnB only when several streams fit. Quick fixes need no feature.

## Posture, then challenge

Before writing: `sc mem get documents`, `sc mem get decisions`, and the repo''s
own docs (`sc map-sql "SELECT path FROM dr_filepath WHERE role=''doc''"`). A
spec that touches a recorded decision honors it or supersedes it explicitly
with `sc mem decision "…" --parent <id>`; never silently re-decide.
Documentation is the preferred account of intended posture; when it is absent,
ambiguous, or plausibly stale, verify the narrow code path and name that
fallback in the spec. Documentation and code disagree -> surface both to the
FnB; do not pick one as truth.

Walk the proposed workflow end to end. Challenge contradictions, missing
boundaries, hidden assumptions, audience mismatches, partial failure,
concurrent use, permissions, and the unhappy path. Ask the FnB to resolve
anything that could change implementation or acceptance; a deferral belongs in
Out of Scope with the boundary it leaves behind. The conversation is input;
the spec is the resolved contract: settled FnB decisions sit beside the clause
they govern, and an unconfirmed suggestion is never promoted to a requirement.

## Acceptance feasibility

Every acceptance criterion must be testable and achievable by the shells who
will carry it: a named seat, a named action, an observable result. Specs
derail when a Developer discovers mid-Sprint that nothing on hand can prove
success, so settle the proof path before handing a spec to implementation.
Two seats are always in play — the Developer proving behavior during
implementation, and a post-deploy check in Dev or an alpha production
environment — and between the `drive_browser` skill and the fork''s VM seats
(`remote_seats`, plus any fork-declared seat) a proof path nearly always
exists, even when it is not yet configured. Missing or unconfigured resources
are a prerequisite to raise with the FnB and resolve or deliberately defer,
never grounds to narrow the claim silently, label inference as proof, or leave
the gap for the Developer to find.

Before committing acceptance criteria or handing a spec to implementation,
walk each criterion through these questions in order. Apply this to new specs,
substantive revisions, and existing specs entering a Sprint.

1. **Can we meet it with available resources?** Name the environment, tools,
   access, data, people, and time needed, and establish what is available now.
   A hoped-for capability is an unresolved prerequisite, not a test plan; a
   seat that exists but is not yet configured for this check is a prerequisite
   to surface and discuss with the FnB, not a reason to drop the criterion.
2. **What evidence is worth obtaining?** Separate directly proven success
   from inferred success. Weigh the value of direct proof against its cost;
   assess how accurately success can be inferred from available tests and
   observations, naming assumptions, blind spots, and residual uncertainty.
   Choose and justify the evidence level with the FnB when it changes the
   acceptance promise. Never label inference as proof or demand direct proof
   merely because it sounds stronger. Evidence choices do not waive the
   correctness, authorization, or tenancy requirements themselves.
3. **Is post-implementation verification practical in Dev?** Describe the
   setup, action, observable result, and cleanup on the actual available seat.
   Bound the effort and state what that environment can and cannot establish;
   mocks or a Dev pass do not prove behavior beyond their demonstrated reach.
4. **Can the FnB supply the missing proof?** Consider concrete privileged
   console commands run by the FnB in cooperation with shells during or after
   the Sprint. Specify the target, commands, expected evidence, shell role,
   cleanup, and timing; confirm FnB participation before depending on it.
   Distinguish Sprint completion from any pending post-Sprint acceptance, and
   keep unperformed checks explicitly pending.
5. **Is a privileged process still justified?** Only if direct proof remains
   required and the preceding paths are insufficient, consider implementing a
   narrowly scoped privileged process. State why it is worth building, its
   exact operations and authority, lifetime, cleanup, and verification path.
   Resolve that scope with the FnB before adding it to implementation; a
   verification gap is not authority to build general privileged tooling.

Record the result in `## Acceptance Criteria`: for each criterion, give the
claim, required evidence level and rationale, verification method and seat,
owner and timing, prerequisites, and observable pass condition. Keep this
proportional to the work; a compact table is sufficient. Unavailable required
proof -> resolve the prerequisite, agree a narrower claim or justified
inference, or defer the criterion explicitly before releasing dependent work.
If feasibility breaks during implementation, return to the FnB and the normal
spec revision/Sprint rebind procedure before expanding verification scope.
Do not keep adding code to chase an acceptance claim the resources cannot prove.

## The spec contract

Every new spec and every substantive revision of an unfrozen spec carries:

| section | holds |
|---|---|
| `## Current Posture` | related systems and behavior before the change; documents and decisions consulted; code paths read because documentation was missing, ambiguous, or stale |
| `## Scope` → `### In Scope` / `### Out of Scope` | what this delivery adds, changes, or removes; what it deliberately excludes and the boundary retained ("not in this delivery", never "never") |
| design sections | synthesized FnB decisions and rationale beside the requirement they constrain |
| `## Acceptance Criteria` | per-criterion evidence choice, feasibility, verification method, owner, timing, prerequisites, and observable pass condition from Acceptance feasibility above |
| `## Anticipated User Activity` | `### Vocabulary`, `### Expected Activity`, `### Reach`, `### Audience and Assurance`, `### Data Tenancy`, `### Beyond Intention`; about 60 lines at most |

Vocabulary roster: Valid Privileged User, Valid User, Visitor, Future
Potential User, System, Shell, Unexpected Participant. Audience postures:
Unknown, Authenticated, Operational, Technical, Administrator. Separate process
curation (how much explanation a surface needs) from safety hardening
(correctness, validation, authorization, tenancy, safe failure — never waived
by expertise). Soft vocabulary, hard invariants: write anticipated activity,
Unexpected Participant, Beyond Intention, Reach, and tenancy; never threat
model, attack, adversary, exploit, abuse case, vulnerability, breach,
privilege escalation, exfiltration, or malicious. Internal-only features still
carry the section.

Author with
`sc mem doc add "<title>" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/<slug>.md`;
`--seq` auto-advances. Body format: the `themed_markdown` skill.

## Revise, freeze, document

Unfrozen -> edit in place: `sc mem doc edit <document_id> --body-file ./draft.md`
(also `--title`, `--render-path`); no new row, no seq bump. Frozen -> title
and body are refused; open a new spec under the same feature; only
`--render-path` still moves. When a new era makes a feature''s history
misleading: create the fresh feature with its work-stream, run
`sc mem doc move <document_id> --feature <target>` (atomic across the spec,
its tasks, and document-linked decisions; refuses frozen, doc-kind, terminal,
or Sprint-bound), re-read under the target, then retitle the old feature and
set its truthful terminal status.

On the dev''s docs-pending flag: `sc mem doc freeze <document_id>`; read the
shipped code, not the spec, and write
`sc mem doc add "<feature> — how it works" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/<slug>.md`;
then `sc mem flag close <flag_id> --notes "Spec frozen; doc <id> written → docs_sc/<slug>.md"`.
Until that close, shipped + open flag is the truthful interim state.

When a successor replaces a document, retire the old one with
`sc mem doc retire <id> [--superseded-by <id>]` — metadata about the document,
never an edit to it, so a frozen body stays untouched while readers are pointed
at what is current (`--undo` reverses a wrong id).',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'spec_execution',
  'Load before you build against a feature spec, task, or work unit, and before you choose which tests prove a change. Holds spec selection, the pre-plan analysis, the task ledger, one-task-at-a-time execution, verification, the ship and docs handoff, and the testing posture (smallest affected gate, long-running checks, CI fallback, when the full suite runs). Developer.',
  'workflow',
  NULL,
  0,
  '# spec_execution — build what the spec promises, prove what you built

A feature spec governs the work whenever one exists. Unspec''d quick fixes (a
small UI tweak, a minor migration) skip the task plan; the testing posture
below applies to every change.

## Before you touch code

1. **Select the spec.** An assignment that names a task or work unit ->
   `sc context --task <id>` / `sc context --work-unit <id>` is your planning
   context. Otherwise `sc mem get documents --feature <id>`, then the whole
   body with `sc mem get documents --doc <doc_id>` and its ledger with
   `sc mem get tasks --doc <doc_id>`. Never auto-pick the latest document;
   several plausible specs -> ask the FnB. Existing tasks -> resume the first
   unfinished one. Read broader indexes only for an unresolved need.
2. **Analyze before planning.** Viability: bounded, clear entry points,
   session-sized verification; a missing done-condition is unclear. Current
   Posture matches the documented baseline; a mismatch stops for the
   Planner/FnB — never redefine it silently. Every In Scope promise is
   planned and every Out of Scope item stays out. Anticipated User Activity
   roles, reach, authority, and tenancy are planned. Two plausible readings or
   unstated required knowledge -> ask the FnB. A missing prerequisite,
   environment, or external dependency -> open one High flag on the feature
   and stop at its boundary.
3. **Plan.** Building now moves `brainstorm|long_term|near_term` to
   `in_progress`: `sc mem roadmap status <feature_id> in_progress`. Confirm
   the work-stream; assign the obvious one with
   `sc mem roadmap project <feature_id> <shortname>`, ask when ambiguous. Lay
   the ledger: `Preparation` at seq 0, one row per independently verifiable
   step, `Verification` last, each
   `sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> --desc "<outcome>"`,
   then `sc mem state "[F<id>] last: —. next: Preparation."`. No task plan, no
   build.

## Build

4. **Execute one task at a time.** `sc mem task start <id>`; work and verify
   only that task; `sc mem task done <id>`; re-read the ledger; set
   `current_state` to the highest done + lowest pending task. Work moved
   elsewhere -> `sc mem task cancel <id> --notes "moved to F<id> task #<n>"`;
   a whole unfrozen spec moves intact with
   `sc mem doc move <document_id> --feature <target>`. Small growth within the
   same intent -> revise the unfrozen spec and add tasks; a separate mental
   model -> stop and recommend a new feature + spec, never absorb it.
5. **Verification** follows TESTING POSTURE below and requires every In Scope
   done-condition and Anticipated User Activity contract; unexpected reach,
   weakened hardening, or crossed tenancy fails. A large spec may stop after a
   verified task slice with the next task named in `current_state`.
6. **Ship and hand docs to the Planner.** All tasks done + Verification green
   -> `sc mem roadmap status <feature_id> shipped`; open one Medium flag
   `"[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc"
   --feature <id>`; message the planner to freeze the spec, write the
   `kind=doc` document, and close the flag; tell the FnB. No planner shell ->
   tell the FnB and leave the flag open. Never freeze or author the shipped
   doc yourself.

## TESTING POSTURE

- **What to run.** Run every available smallest affected test target that
  proves the changed behavior and realistic failure paths.
- **Long checks.** Use `sc job start --label gate -- <command and args>` for
  checks that may outlive the current tool call or session. After confirmed
  registration/start, end the turn and continue on the owner wake; inspect
  `sc job status <id>` and `sc job tail <id>`. A lost outcome is unknown,
  never a pass. Do not set up independent watchers.
- **CI fallback.** Complete the implementation before using CI fallback. If a
  focused local gate cannot execute because the selected interpreter, runner,
  or declared dependency is unavailable, record the exact evidence, run the
  remaining checks, then push/open the PR and register it when the workflow
  provides registration. Required checks pending -> wait; red -> diagnose,
  fix, and push; green -> review readiness.
- **What counts as failure.** A test assertion, source-caused collection
  error, red CI result, or incomplete code is a failure, never unavailable
  infrastructure. No configured checks or an untrustworthy watcher after one
  bounded read -> block because no trustworthy seat remains. An optional
  browser-capability skip is informational and non-failing.
- **The full suite.** When the repository declares an authoritative full-suite
  CI gate, do not run the repository-wide suite locally merely to duplicate
  CI. Run the full suite locally only when no authoritative CI gate exists,
  the change crosses test/CI/harness infrastructure, the FnB explicitly
  requests it, or bounded diagnosis requires it. Never start a competing
  repository-wide suite on a shared host.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'cli_orchestration',
  'Planner-run delivery from a terminal seat — the FnB names the work, you stay the oversight layer, and native or cross-harness CLI agents verify the spec, build, review, deploy to Dev, and verify. Covers the kickoff contract, agent launch per harness (Claude, Codex, Kimi), terminal-seat PR/CI monitors, the FnB''s merge and deploy directives, acceptance review, and cleanup.',
  'workflow',
  NULL,
  0,
  '# cli_orchestration — run a delivery from the terminal

Use when the FnB assigns a spec, task set, or work unit "in this chat" and
wants you to orchestrate it with your harness''s own agents. You are the
oversight layer: you brief, launch, monitor, verify handoffs, and surface
questions. Agents do the work. Keep your context window for the FnB and you.

## Seat gate

This loop needs a terminal seat: boot `## SEAT` says "You hold a terminal".
On the GUI seat your background agents and commands die when your turn ends.
Tell the FnB that this work needs a terminal session and stop.

## Your lane

- Do not write code or propose fixes. Route findings to the agent that owns
  the change. Offer an opinion only when the FnB asks for one.
- Delegate reading as well as writing. Have agents read the large sources
  and report only conclusions, identifiers, and evidence paths.
- Surface every question that could change scope, acceptance, or authority
  to the FnB, verbatim and with the agent''s evidence. Do not answer it
  yourself, and do not let agents choose for the FnB.
- You own the durable writes: `sc mem state`, narrative, flags, decisions,
  and task status. Agents report back to you; they do not write `sc mem`.
- Never accept an agent''s report as proof. Verify each claim against the
  durable artifact: the branch on the remote, the PR, check results, and
  receipts or evidence files.

## Kickoff contract

1. Load the assignment with `sc context --task <id>` or
   `--work-unit <id>`. Read the spec''s tasks and run the version-control sync
   from your boot.
2. Restate the assignment in one block: the work, the lanes, the model and
   effort per lane, the monitors, the verification seat, and what stays with
   the FnB.
3. Ask the open questions before launching anything, and ask them in one
   batch:
   - Spec freshness. The spec may name sites that have moved on main. Offer
     a spec-verification agent before dev starts.
   - Merge authority. Your boot''s merge gate has two forms and this loop
     runs outside an armed Sprint, so only the first applies: an explicit FnB
     directive naming the PR. Ask for it here: which of this assignment''s PRs
     you may merge, and on what condition, for example "green and review
     clean". The FnB''s answer is that directive for those PRs. It is not a
     standing grant or a third form: no answer, a PR outside the assignment,
     or an unmet condition -> report the PR as ready and wait.
   - Deploy authority. Ask for the target (Dev only, unless the FnB says
     otherwise), when to deploy, and whether a final deploy at the end
     replaces the intermediate ones.
   - Prerequisites and gates. List the PRs, seats, or flags the work waits
     on, and decide with the FnB whether to wait or to proceed.
   - Lanes the FnB has not assigned, such as doc or skill edits, and whether
     an agent takes each one.
   - What stays with the FnB, such as release, follow-up disposition, and
     production.
4. When the FnB answers, record the directives (merge, deploy, models, gates)
   in one `sc mem narrative` line and point `sc mem state` at it. They last
   for this assignment only; cite the merge directive when you merge.

## Lanes and order

Run the lanes in this order. Independent lanes run in parallel.

1. Spec verification. One agent checks every site, contract, and budget the
   spec names against current `origin/main`. It reports a drift table. You
   take any drift to the FnB, who may revise the spec (`sc mem doc edit`)
   before the build starts.
2. Baseline evidence, when acceptance compares before with after. Capture it
   on the verification seat before any deploy changes that seat.
3. Development. The agent builds one branch per unit in its own worktree. It
   pushes the branch and opens a PR. It does not merge.
4. Review. Use a reviewer from a different model family than the developer
   wherever one is available. Give the reviewer the spec and the diff, not
   your view of them. Route findings back to the developer, and repeat until
   the review is clean or the FnB rules on a disagreement.
5. Merge and deploy, only under the FnB''s recorded directive. Run
   `gh pr merge <n> --squash` (or the repo''s convention) on a PR the directive
   covers, once its condition holds: checks green and review clean unless the
   FnB said otherwise. Deploy only to the directed target, using the fork''s
   dev_kit or remote-seat procedure.
6. Acceptance review. Before any verification run, show the FnB the method
   and pass condition for each criterion. Change them only with the FnB''s
   agreement.
7. Verification. An agent runs the agreed criteria on the named seat and
   keeps transcripts, receipts, and readback. It reports each criterion as
   pass, fail, or pending, with the evidence path. It never presents
   inference as proof.
8. Evaluation. Report the results to the FnB. The FnB decides the follow-ups
   and you record them as flags or tasks.

## Launching agents

Name the model and effort the FnB asked for, exactly. If a harness cannot set
one of them, say so and do not substitute silently.

**Native subagents** keep their output out of your context:

| harness | how |
|---|---|
| Claude Code | Agent tool. Set `model` (alias such as `opus`) and `effort` only when the FnB names them. Agents run in the background and notify you on completion. Use `isolation: "worktree"` for a builder. Continue an agent with SendMessage instead of starting a new one. |
| Codex | Native multi-agent tools, when `codex features list` shows `multi_agent` enabled. If you need a model or effort the native tool cannot pin, use a CLI child instead. |
| Kimi, others | Use CLI children. |

**CLI children** let any harness run any other harness, which gives you
cross-family review. Write the brief to a file and run each child as one
background command with its own log:

```bash
# Claude child (from Codex/Kimi or for a pinned model)
claude -p --model <model> --effort <level> --dangerously-skip-permissions \
  --output-format json < brief.md > lane.json 2> lane.err
# Codex child
codex exec -m <model> -c model_reasoning_effort=<level> -s danger-full-access \
  -C <worktree> -o lane.final.md - < brief.md > lane.log 2>&1
# Kimi child (prompt mode auto-approves; --yolo/--auto are refused with -p)
KIMI_MODEL_THINKING_EFFORT=<level> kimi -m <alias> -p "$(cat brief.md)" > lane.log 2>&1
```

To continue a child, resume its session: `claude -p --resume <session_id>`
(the id is in the JSON result) or `codex exec resume <thread_id> "<follow-up>"`.
For Kimi, start a fresh child and include the earlier report in the brief.

Rules for CLI children:

- Run a child from its own worktree, never from yours. Your worktree holds
  your rendered boot file, so a child started there boots as you.
- A child inherits your `SC_*` environment, so the engine sees it as you.
  The brief forbids `sc mem` writes. Read-only `sc` verbs and the project
  verbs you name (`sc test`, `sc context`) are allowed.
- Start one background child, or one monitor, per tool call. A process
  started inside a compound command can lose its completion signal.
- Keep the run directory (briefs, logs, final messages) outside git, for
  example `${TMPDIR:-/tmp}/sc-orch-<shortname>/`.

## The brief

Each brief is self-contained. It contains:

- the goal, and the spec and task ids with the command that reads them;
- the worktree path, branch name, and base (`origin/main`);
- the scope boundary and what is out of scope;
- the authority: push the branch and open a PR, yes; merge, deploy, and
  `sc mem` writes, no;
- the acceptance that applies to this lane and the tests to run;
- a stop rule: on a question that changes scope or acceptance, stop and
  report it instead of choosing; and
- the report shape: PR number and URL, commit SHAs, each test run with its
  result, evidence paths, open questions, and nothing else.

## Monitors

On the terminal seat, set up your own monitors: no engine service watches a
PR for you here. This is specific to this loop on this seat. On the GUI seat
and inside a Sprint the engine delivers PR facts as wakes, and your boot''s
"never poll GitHub" rule holds.

- **PR checks:** `gh pr checks <n> --watch --interval 60` as a background
  command; it exits when the checks settle. Read the result with
  `gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup`.
- **Merge conflicts:** an open PR whose `mergeable` is `CONFLICTING` belongs
  to its developer. Route it back.
- **Stalls:** if a child''s log has stopped growing past the expected window,
  read its tail before you decide. Kill and relaunch only with a fresh brief.
- **No auto-wake:** if your harness does not re-invoke you when a background
  command exits, read every log and exit status at each turn and before any
  status report. Report only what you have read.

## Ledger

Keep a short ledger in the run directory. List each lane with its agent
or session id, model and effort, worktree, branch, PR, monitor, and status.
Update it at every event. After a context compaction, the ledger and
`sc mem state` are how you recover the run.

## Close

Before you report done:

- Confirm every PR is merged, closed, or handed to the FnB with its state.
- Remove every worktree you created with `git worktree remove <path>`, then
  run `git worktree prune`. Delete local branches whose PRs merged.
- Stop every background child and monitor you started. Leave no orphans.
- Delete the run directory, or name it to the FnB as evidence.
- Mark tasks done, open flags for the follow-ups the FnB decided, and close
  the flags your work cleared.
- Update `sc mem state` and add a closing narrative line.
- Give the FnB a final report: what shipped (PRs), the verification result
  for each criterion, what remains pending, and the decisions that are theirs.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'curate',
  'The periodic L&S sweep. Run when the STATUS L&S line says "curation due" — resolve contradictions, merge entries stating one rule, recommend recurring processes upstream, move environment facts out, then stamp `sc mem curated`. Yours alone; never delegate it.',
  'substrate',
  NULL,
  1,
  '# curate — the L&S sweep

Write-time triage (`--supersedes` / `--new`) catches contradiction and
restatement pairwise, at the moment of writing. It **cannot** catch the
emergent cluster: five entries can each be a valid distinct rule and only in
aggregate be five instances of one principle. That is this pass''s job, along
with recommendation, category drift, and size drift.

**Yours alone.** Law 3 and Law 5 reserve curation to the shell. Never hand this
to a subagent, never let another shell run it for you, never accept a proposed
retirement from anyone else. Read your own set; decide yourself.

Trigger: `## STATUS` says `L&S: … — curation due`. Nothing else fires it.

## Load the set

```
sc mem get lns          # entry ids + bodies — the active set, all of it
```

Read every entry before deciding anything. This is one cheap read; the whole
set is already in your boot doc anyway.

## Pass 1 — Consistency

Find entries that **contradict** each other. One of them is the newer
understanding; the other is superseded and still rendering as live guidance.

```
sc mem lns "<the surviving rule>" --supersedes <old_id>
```

Write-time triage should prevent most of these from ever forming. What you find
here predates the loop or crossed in while two sessions ran.

## Pass 2 — Cluster

Group entries that state **one rule**. Merge each group to a single imperative
rule:

```
sc mem lns "<the one rule>" --supersedes 30,33,34,37,38
```

Three or more members is the bar. Two statements of a rule are often
legitimately two rules — merging at two is usually wrong.

The incidents behind the entries are already in the narrative. They do not need
a second home, and the merged rule must not try to carry them: an entry is the
rule, ≤500 chars, hard-enforced.

## Pass 3 — Recommend

A cluster of three or more that keeps **recurring across sessions** is a
candidate reusable process. Follow the recommendation route in
`issue_reporting` — search first, then comment on the matching
`skills: recommend <topic>` issue or open one. Curation never creates or
promotes a skill. Keep one compressed L&S entry carrying the knowledge until a
reviewed upstream skill ships **and is granted**; filing is not grounds to
retire it. In the Subfloor source repo "upstream" is this repo: the same
search-then-file route applies, and an accepted skill is authored here by
branch and PR. If issue search or creation is unavailable, surface the failure to
the FnB, keep the L&S, and create no local skill or asset.

## Pass 4 — Category

An entry that is an **environment fact** (a routing quirk, a term to avoid, a
path) is not an operating principle. Move it into an existing authoritative
skill when one owns that fact. Otherwise keep one compressed entry and include
the missing ownership in a recommendation; do not invent a local skill during
curation.

```
sc mem retire <entry_id>  # only after the authoritative replacement is live
```

## Stamp

```
sc mem curated
```

**Stamp even if you retired nothing.** A clean set is a legitimate outcome; if
an honest sweep left the counter running, the advisory would stand forever and
you would learn to ignore it. The stamp says "I looked," not "I cut."

## Stance

Curate the set toward ~12–14 entries, not toward the cap. Cap 20 is a ceiling
never to reach — if you ever hit it, this sweep is not running. Recommendation
issues do not bypass the cap by deleting knowledge before its replacement ships.

The trigger firing often does not mean the threshold is wrong; it means entries
are being written faster than they are reconciled. Fix that at write time, with
`--supersedes`.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'flag_sweep',
  'Planner-owned periodic or on-demand delivery reconciliation — auto-close flags whose gating work is provably done, open missing ship/docs handoffs, and surface judgment calls to the FnB. Use for a requested sweep or when delivery state needs reconciliation.',
  'substrate',
  NULL,
  0,
  '# flag_sweep — reconcile flags against state

Planner-owned. Run periodically or when the FnB asks for delivery-state
reconciliation; never make it a boot ritual. Working shells close the flags
their own work clears; this sweep is the
backstop for dropped handoffs + shipped work nobody documented. Two directions:
close what''s provably resolved, open what''s provably missing.

---

## Step 1: Load the bounded delivery audit

Run `sc mem delivery-audit` (Planner-only). It returns `recent_flag_names`,
`open_flags` (each with its feature''s `roadmap_status` and `frozen_docs`),
`implemented_but_unshipped`, and `shipped_but_undocumented`. These are the
sweep''s whole input; run no engine SQL of your own.

`frozen_docs` counts ANY frozen document on the feature — kind=''spec'' AND
kind=''doc'' both qualify, so a fork that freezes its shipped `doc` rows is
never reported as undocumented.

Sort every open flag into exactly one bucket (Step 2 / Step 4). Auto-close
only on unambiguous evidence — any doubt -> Step 4, not a close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note MUST cite the
evidence.

**A. Docs-pending flag, doc now exists** = `[Docs]`-tagged doc-pending flag
(however worded — "doc pending", "docs pending", "feature doc pending") on a
feature with `frozen_docs > 0`:
```
sc mem flag close <flag_id> --notes "Auto: frozen spec doc now exists for feature #<id> (flag_sweep)."
```

**B. Ship-blocker, feature now shipped** = flag of the form
`… | Blocker for: <X>` + linked feature''s `roadmap_status` is `shipped` (or
later) + the flag text is about that feature shipping / becoming available. A
separate concern that merely hangs off the same feature does NOT qualify:
```
sc mem flag close <flag_id> --notes "Auto: blocking feature #<id> (<title>) now shipped (flag_sweep)."
```

**C. Ship-drift flag, now shipped AND documented** = `[Ship] … not marked
shipped` flag (opened by Step 3A) covers two halves — mark shipped + reconcile
the doc — so close only when BOTH hold: `roadmap_status` is `shipped` (or
later) + `frozen_docs > 0`. Shipped-but-undocumented -> leave open:
```
sc mem flag close <flag_id> --notes "Auto: feature #<id> (<title>) now shipped with a frozen doc (flag_sweep)."
```

NEVER message on close. NEVER reopen a flag. A close whose evidence you had to infer -> Step 4.

---

## Step 3: Open the flags nobody opened

The audit already computed both gaps and applied the dedup guards, so open
one flag per returned row and re-derive nothing. A feature exits 3A before 3B
can apply. Pick `SC-###` from the highest numbered value in
`recent_flag_names`. Open each flag in Planner''s own queue.
Do not message yourself.

### 3A — Implemented but not marked shipped (ship-drift)

Rows: `implemented_but_unshipped` — the spec''s Verification task is `done`,
the feature is not shipped or retired, and no open `[Ship]`/`[Docs]` or
organic ship/docs-pending handoff covers it. The dev''s flip to `shipped` was
missed. The `[Ship]` flag governs both halves of the dropped handoff (mark
shipped + reconcile the doc to the spec) and stays open until a planner does
both:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
```

### 3B — Shipped but undocumented (docs-pending)

Rows: `shipped_but_undocumented` — a shipped feature with no frozen document
and no open `[Docs]` or organic docs-pending handoff. The dev''s docs-pending
flag was skipped:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
```

The guards match the `[Docs]`/`[Ship]` tag at position zero, then fall back to
`''%doc%pending%''` for untagged wordings; the fallback''s over-breadth only ever
SKIPS an open — the conservative direction.

---

## Step 4: Surface the rest — don''t guess

Everything that isn''t a clean Step-2 close / Step-3 open -> short list to the
FnB (no `send` unless a specific shell owns it): review-failure flags (author
dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can''t verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn''t auto-act*

The FnB or the owning shell closes these with a real note. Auto-act ONLY on
unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB + cited in the note,
  or it surfaces. A wrongly-closed live blocker is worse than a straggler.
- **Backstop, not owner.** The shell that did the work closes its own flag
  with the richer "how" note; don''t race to close a flag whose owner is still
  active on that feature.
- **Both directions, every sweep.** An implemented-but-unshipped spec and an
  undocumented shipped feature are dropped handoffs; the signal is already in
  the DB (a `done` Verification task, a missing frozen doc) — surfacing them
  is deterministic.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'git',
  'Load when a Git step goes beyond the every-session rules in your boot — a push or gh call fails on auth, the FnB hands you a stack to merge, a PR just merged and your tree needs cleanup, you are unsure whether a file may be committed (engine, renders, generated config), or you need to know which repo and worktree you are in. Sync, branch, PR, merge gate, and finish stay in boot.',
  'substrate',
  NULL,
  0,
  '# git — the event procedures

Your boot''s VERSION CONTROL section carries the every-session rules: sync the
base, branch before you build, commit → push → PR → stop, the merge gate''s two
forms, the disposable `shell/<shortname>` base, and the finish gate. This skill
holds what fires on an event.

One repo at its root -> plain `git` (cwd = repo root) is safe. Project = this
repo minus `.super-coder/`. In a tracking fork the engine (`.super-coder/`) is
gitignored, materialized by `sc update`, and authored upstream in Subfloor —
NEVER commit or edit anything under it. In the Subfloor source repository
(`git ls-files --error-unmatch .super-coder/schema.sql` exits 0) `.super-coder/`
is tracked project source and `.sc-state/engine.ref` is not the delivery unit;
engine changes still land by branch and PR.

## GitHub capability boundary

`sc launch` and `sc restart` re-resolve Git transport and GitHub API
capabilities from the host on every invocation, including `--no-build` forms.
`build`, `enter`, and an already running sandbox do not refresh auth. Pass = the
lifecycle summary says `ready` for the operation you need; `unavailable` and
`unverified` are NEVER readiness claims.

Preserve the configured `origin` transport. For SSH, fix the host agent and
load an authorized GitHub identity; NEVER copy or mount private keys. For
HTTPS/API, fix a scoped host `SC_GH_TOKEN` or the host `gh` OAuth login. Then
run `sc launch` or `sc restart`; the running sandbox remains unchanged
until that refresh. NEVER rewrite the remote or start an interactive login
inside the sandbox to work around a missing capability.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Run receipts

Attach a finished local run to its PR or work unit with
`sc job attach <id> --pr <number>|--work-unit <id>` (`sc job attach --help`).
The rules live in the Developer boot''s RUN RECEIPTS section; a receipt
supplements CI and never authorizes a merge.

## Never commit the engine or derived files

- In a fork `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, the per-harness skill renders (`.claude/skills/`, `.agents/skills/`, `.opencode/skills/`), the engine-managed harness config (`.claude/settings.local.json`, `.codex/hooks.json`), and `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main`. Admin shell = the one exception: repo root on `main`, committing there by mandate.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'git_cleanup',
  'Admin-only — triage and clean the repo''s git state across main + every worktree. The acting sibling of git_hygiene.py''s read pass: delete what''s provably merged, preserve (never discard) outstanding work, sync to remote. Use when the FnB asks to tidy/clean git, prune branches, or reconcile worktrees.',
  'substrate',
  NULL,
  0,
  '# git_cleanup — the act pass over git state

`git_hygiene.py` reads; this acts on its report. **Admin shell only** — the one vantage at the repo root on `main`, seeing every worktree, exempt from the branch-guard. A working shell NEVER runs this; it tidies only its own worktree.

Governing asymmetry: a MERGED PR = proof a branch is safe to delete; uncommitted work has NO proof it is disposable. Delete only on evidence; preserve by default; discard only on the FnB''s explicit per-item OK. Unsure -> surface, never guess destructively.

Expect the report to be quiet — still run it:

- Since #119, `git_prune.py` deletes the provably-merged branch set (Tier A.1''s `stale` set, repo-global) at every boot -> Tier A often already clear. This pass = backstop for what automation won''t touch: `gh`-down unprovable merges, dirty worktrees, unpushed work, `main` fast-forward, remote-ref pruning.
- Working shells self-finish (sync before build, land/surface before stop) -> Tier B/C should be rare. A full Tier B/C = a shell skipped its finish gate -> fix it AND send that shell a note, not a silent fix.

## Investigation order — scripts first, always

1. Scripts: `git_hygiene.py` (git state) + `shell_liveness.py` (who''s live). Read their output; never re-derive by hand what one pass gives you.
2. Git history — only when a verdict is ambiguous (`merged: null`, unexpected dirty file): `git -C <path> log` / `reflog` / `show`.
3. Working-tree contents — last, only when history doesn''t explain it.

## Step 1 — Read the state (never skip)

```bash
python3 .super-coder/scripts/git_hygiene.py --text       # git: dirty/stale/clean
python3 .super-coder/scripts/shell_liveness.py --text     # who has a live session
```
Drop `--text` for JSON when driving decisions programmatically.

- `git_hygiene` -> every worktree (path, branch, dirty count, sample files, ahead/behind) + every local branch''s staleness (`merged` = true / false / null-unknown, with PR number). `gh_available: false` in the JSON -> treat every `merged: null` as unknown, never safe.
- `shell_liveness` -> which shells have a live harness session right now (read from `/proc` cwd — instant, self-cleaning). Your OWN session shows as the repo-root `is_self` entry — expected, not a blocker; the gate is about OTHER shells (Tier C).

## Step 2 — Triage into three tiers, act top-down

Sort every report item into exactly one tier.

### Tier A — auto-safe (act without asking). Only these three:

1. Merged-PR branches — `merged: true` + `is_base: false` + `checked_out: false`:
   ```bash
   git branch -D <branch>
   ```
   Squash-merge is the project default -> `git branch -d` refuses; that refusal is expected, not a stop signal. What survives boot-time `git_prune.py` is residue: merged since the last boot, or merged during a `gh`-down boot.
2. Dead remote-tracking refs:
   ```bash
   git fetch --prune
   ```
3. `main` behind origin + clean tree (admin''s root tree only):
   ```bash
   git pull --ff-only          # never a plain pull/merge on main — no merge bubbles
   ```
   `--ff-only` refuses -> main diverged -> Tier B/C, not auto.

NEVER auto-delete: a `merged: null` branch, an `is_base` branch (`main` or any `shell/<shortname>` — long-lived moving bases), or a branch checked out in a worktree.

### Tier B — outstanding work (propose -> FnB OK -> act). Preserve, never discard.

- Unpushed commits (`ahead > 0`): show the FnB `git -C <path> log origin/<base>..HEAD --oneline` first -> propose push + PR.
- Admin''s OWN root tree dirty: show the diff + proposed message -> on OK, cut a feature branch off main, commit, push, PR. NEVER discard the admin tree''s dirt without explicit instruction.

### Tier C — other shells'' dirty worktrees (gated; preserve-only)

Any other shell''s worktree: `is_main: false` + `dirty > 0`.

1. **Liveness gate.** Committing files is non-destructive, but re-branching a worktree under a mid-session shell stomps that live session. Read the `shell_liveness` verdict:
   - `safe_to_clean_all: true` -> admin presence confirmed, every other worktree dormant, nothing indeterminate -> act on all.
   - shortname in `active_other_shells` -> that shell is LIVE -> surface only, do NOT touch its tree. The others remain safe.
   - `indeterminate > 0` -> a harness process whose cwd was unreadable (another OS user, say) -> do NOT assume all-clear -> surface.
2. **Attribution.** The commit carries THAT shell''s trailer, never the admin''s. Read the display name for `shell/<shortname>` from `sc mem get shells`, then export the identity on the commit so the tracked `prepare-commit-msg` hook writes the trailer for you:
   ```bash
   SC_SHELL_NAME="<display_name>" SC_SHELL_SHORTNAME="<SHORTNAME>" git -C $WT commit -m "<msg>"
   ```
3. **Preserve** (shell cleared as not live):
   ```bash
   WT=.sc-worktrees/<shortname>
   git -C $WT checkout -b <type>/<short-desc>           # feature branch off its HEAD
   git -C $WT add -A
   SC_SHELL_NAME="<display_name>" SC_SHELL_SHORTNAME="<SHORTNAME>" git -C $WT commit -m "<msg>"
   git -C $WT push -u origin <type>/<short-desc>
   gh pr create --repo <owner/repo> --head <type>/<short-desc> --fill   # open, never merge
   ```
4. **Message the owning shell** — it must never boot to a silently rearranged tree:
   ```bash
   sc mem message send <shortname> ''git_cleanup: your worktree had uncommitted work. I preserved it on branch `<type>/<short-desc>` and opened PR #<n>. Your tree now sits on that branch — `git checkout shell/<shortname>` to return to your base.''
   ```
   Report the same to the FnB. Worktree left untouched (live / indeterminate) -> no message.

## Hard nevers

- NEVER `git checkout -- `, `git reset --hard`, `git clean`, or `git stash drop` on uncommitted work without the FnB''s explicit per-item OK — preserve is reversible, discard is not.
- NEVER commit another shell''s work under the admin''s attribution.
- NEVER act on a worktree whose shell may be live — surface instead.
- NEVER merge a PR — opening is the default; merging is the FnB''s gate.
- NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.
- In a tracking fork, NEVER touch the engine — there `.super-coder/` is a gitignored materialized dependency, not your code. In the Subfloor source repo it is tracked project source: treat a branch or dirty file under it as work like any other (Tier B/C), and land engine changes by branch and PR, never as a cleanup edit.

## Step 3 — Report

Close with: deleted (evidence — PR #), pushed/PR''d (links), surfaced and awaiting the FnB''s call, + a final `git_hygiene.py --text` as the after-state. Nothing outstanding -> say so and stop.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'harness_readiness',
  'Read Subfloor harness/model support states, refresh the supplied local evidence, run bounded compatibility checks, and prepare an exact upstream handoff when the installed runtime is unqualified. Developer-only.',
  'substrate',
  NULL,
  0,
  '# harness_readiness — qualify the installed route

Subfloor reports maintained harness support as `tested`, `best-effort`, or
`newer-unverified`. These states describe source evidence; they do not hide a
locally discovered model or silently substitute another route.

## Read the supplied evidence

```bash
sc harness-status
sc models refresh
sc models list <harness>
sc models resolve <harness> <selector> [--effort <level>] --json
```

Record the complete version line, active host/container seat, exact selector,
effort, evidence source, digest/fingerprint, and resolve result. Pass = list and
resolve agree on the same fresh local route. A public model absent from local
evidence remains unavailable for that account; an unsupported effort fails
before dispatch.

## Use the smallest available compatibility check

When the FnB authorizes a provider call or harness refresh, exercise the exact
installed model/effort through the fork''s declared hook or the adapter''s native
one-shot surface. Pass = one request uses the requested route, returns parseable
events and session identity, and performs no fallback or changed-effort retry.

`sc update-harnesses`, sandbox rebuild, provider-token use, and session restart
remain operator-authorized boundaries. A host result does not prove the
container seat, and a passing newer build does not promote the maintained
source baseline.

## Hand source maintenance upstream

In a fork, use `issue_reporting` when the installed version or adapter
contract remains unqualified. Include the complete version line, seat and engine commit,
selector/effort, status/list/resolve outputs, sanitized native-check result,
expected versus actual behavior, and the narrow failing boundary.

Tracking forks do not edit materialized `.super-coder/` metadata or adapters.
In the Subfloor source repo there is no upstream to hand to: carry the same
evidence to the FnB and author the metadata or adapter change on a branch and
PR, when that work is yours or assigned. Pass after a published fix = the exact build reports `tested`, simulated newer
builds remain `best-effort`, and the local route still resolves from fresh
evidence after the authorized update/restart.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'issue_reporting',
  'Report engine defects — the moment a sc command fails or lies, a skill contradicts your reality, the API blocks a documented workflow, or you work around the engine to proceed. From a fork, file a GitHub issue on Subfloor (app bugs stay in the fork); in the Subfloor source repo, flag it and fix it here.',
  'substrate',
  NULL,
  1,
  '# issue_reporting — the backwards flow

An engine defect fixed upstream reaches every fork via `sc update`; worked
around silently, every fork re-derives the workaround. File the issue while
the failure is on screen — NEVER batch to session end.

A workaround IS a report: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; file it now.

## In the source repo

In the Subfloor source repo (your boot''s PROJECT vs ENGINE section says you
are upstream) there is no upstream above you. The triggers and the capture
list below still apply; the route changes. Open a flag with the captured
evidence (`sc mem flag open "[Engine] <symptom> | Blocker for: <x>" …`) and
tell the FnB; when the fix is yours or assigned, author it on a branch and PR.
A GitHub issue on `jedbjorn/subfloor` is still right when the defect should
be publicly tracked or a matching issue already exists — dedup first, as
below. Everything else in this skill is fork procedure.

## Boundary — engine vs fork

| Where | What |
|---|---|
| **Upstream — file it** | anything the engine materializes/owns: `.super-coder/`, `sc` + every subcommand, engine skills (this catalogue), the boot doc render, the sandbox / dev kit, `sc update` + migrations, the `_sc` API + `sc mem` |
| **Fork — don''t** | the repo''s app code, DB-canonical fork-local skills, operator-owned host config |

Unsure -> "would the same problem hit any other fork?" yes = upstream.

## Triggers

Each row = a real engine defect filed by a fork shell doing ordinary work.
Match the left column -> file.

| You hit | Real case |
|---|---|
| A `sc` command fails out of the box | `sc verify` always aborted — its own render step failed the Admin serialization gate it never satisfied (#227) |
| A command exits green without doing the work | `sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `sc lint` said "run `sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths your seat doesn''t have | a sandbox skill drove raw host-only `ssh`/`virsh` paths (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `sc`, then bare git hit the wrong tree, "my edits vanished" (#225) |
| The sandbox can reach something it shouldn''t | `do_push` src/dest weren''t contained — sandbox→host escape (#228) |

Stale guidance (skill says X, engine does Y) files the same as a crash.

## Capture — while the failure is on screen

- **engine ref** = `sc engine-ref` — first line of every report (Subfloor''s engine commit)
- **staleness** = compare that ref to upstream head:
  `git ls-remote https://github.com/jedbjorn/subfloor HEAD` — write
  `current` or `behind head <sha7>`. Behind + the symptom is a missing
  command or a skill/engine mismatch -> the fix may already be shipped:
  ask your FnB for `sc update` first, and file only if the defect
  survives the update (or updating isn''t an option — then the staleness
  note carries that caveat). Triage reads this line to tell a live
  engine defect from a stale fork build.
- **fork + seat**: repo name, shell flavor, sandbox/host
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The issue is public: NEVER paste api keys, tokens, secrets, or private paths.

## File it

```bash
# 1. dedup — someone may have hit it first
gh issue list --repo jedbjorn/subfloor --search "<symptom keywords>" --state all

# 2. file — title: [<fork>] <area>: <one-line symptom>
gh issue create --repo jedbjorn/subfloor \
  --title "[<fork>] <area>: <symptom>" \
  --body "$(cat <<''EOF''
- engine ref: <sha from .sc-state/engine.ref> · <current | behind head <sha7>>
- fork/seat: <repo> · <shell flavor> · <sandbox|host>

**Ran / followed:** <command or skill+step>
**Expected:** <what the docs/skill promise>
**Actual:** <exact trimmed output>
**Workaround:** <what unblocked you, or "blocked">
EOF
)"
```

`jedbjorn/subfloor` = engine upstream; confirm: `git remote get-url super-coder`.

Dedup hit -> comment your engine ref + repro on the existing issue; do NOT
file a duplicate.

No `gh` / no network from your seat -> save the identical body as a fork flag:
`sc mem flag open "[Engine] <symptom> | Blocker for: <x>" --name UP-###`, then
message the **admin** shell to relay it upstream.

## Authorized curation recommendation

The `curate` skill has one FnB-authorized exception to the normal enhancement
gate below. When a recurring L&S cluster may warrant a reusable upstream skill,
the curating shell may search and file the recommendation directly without
asking the FnB first.

Search all upstream issues before opening anything. Add evidence to a matching
recommendation, or open one titled `skills: recommend <topic>` containing the
trigger, repeated incidents, proposed ownership boundary, expected users, why
existing skills do not cover it, and a compact candidate procedure.

This route recommends; it never creates or promotes a skill. Keep one compressed
L&S entry until a reviewed upstream skill ships and is granted. If issue search
or creation is unavailable, surface the failure to the FnB, keep the L&S, and
create no local skill or asset. Deliberate fork-specific authoring remains the
Planner-owned workflow in `fork_skill_design`.

## Rules

- One defect per issue. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to your FnB first, except the authorized curation
  recommendation route above.
- Filing ≠ unblocked: defect blocks work -> also open a fork flag linking the
  issue URL.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'remote_seats',
  'Drive another machine through the host vm-broker — posture 1 owns a test VM''s lifecycle with sc vm (start, exec, push/pull, snapshot, reset); posture 2 runs exec, push, and pull against named remotes with sc remote. Load before touching a test seat or named remote. Opt-in; Linux targets need nothing more, Windows adds windows_testing.',
  'substrate',
  'sc remote',
  0,
  '# remote_seats — two postures, one broker

## Posture

GitHub has history and a test VM has snapshots, so a mistake there is
recoverable. A machine in real use — a workstation, a server, production — has
no undo, so there you run read-only diagnostics unless the FnB assigned the
change. That is a precaution against accidents, not distrust. The class of
machine decides, not the route: a test seat reached over the tailnet is still
a test seat.

A test seat is disposable and shared. Any shell may reset one to its snapshot
at the end of its own session without asking, on every OS. Refreshing the
shared snapshot itself is the owner''s call (decision #434).

## Access model

Reach a target through the host-side vm-broker: `./sc vm` and `./sc remote`
speak to its unix socket, on bare metal or in a sandbox. The broker holds the
libvirt connection, the SSH keys, and the known-hosts files, so key material
has one host-side home and never lands in a worktree, a log, or a sandbox
(decision #353). This is key hygiene, not a mechanism that confines you: use
the broker instead of raw `ssh`, `scp`, or `virsh` because it pins host keys,
serializes sessions behind one lock, and returns structured results. Two
postures share it (decision #355):

| Posture | Who owns lifecycle | Shell verbs |
|---|---|---|
| 1 · host to VM | this engine, through `./sc vm` | adopt, status, start, stop, restart, snapshot, bake, reset (`--running` or `--off`), push, pull, exec, capture |
| 2 · VM to VM | the FnB or the upstream instance | `./sc remote` status, exec, push, pull |

`./sc remote` has no lifecycle verbs. When a named remote is a test seat, its
fork skill names the reset procedure; otherwise report the need for a start,
reset, or snapshot instead of improvising one.

## Set up once per fork

The FnB links the targets; each command writes only its own block of the
gitignored `instance.json` and reports whether the broker is up plus the
command that starts it (`./sc vm-broker-up`).

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <guest-account>
./sc vm init --domain <libvirt-domain> --snapshot <baseline> \
  --ssh-host <guest-address> --ssh-user <guest-user> --ssh-key-path </abs/host/key>
./sc remote add <name> --host <address> --user <user> --key-path </abs/host/key> [--port 22] [--known-hosts-path </abs/path>]
./sc ts init ...        # Tailscale diagnostics tier; see tailscale_diagnostics
```

`adopt` is the normal path for a Windows guest: the operator runs one bootstrap
line in the guest console, then `adopt` installs a host-held key, provisions the
guest, writes the block and takes the baseline snapshot. It is host-only and
idempotent. `init` is the hand-link path for a guest already prepared by other
means. `windows_testing` carries the Windows detail.

Remote names match `[a-z0-9][a-z0-9-]{0,31}`. `key_path` is an absolute,
host-owned, mode-0600 file; the broker refuses anything else. Without
`known_hosts_path` the broker pins the host key on first contact into its own
file under `.sc-state/local/remotes/`. `./sc remote list` and
`./sc remote remove <name>` manage the block; no output includes key material.

## Posture 1 session

```bash
./sc vm status                       # broker, domain, SSH readiness, MCP tunnel
./sc vm start                        # only when off; waits for SSH
./sc vm push <local-file> [<dest>]   # scp to the guest; dest defaults under the workspace
./sc vm pull <guest-path> <dest>     # scp back into the repo or .sc-state/local/
./sc vm exec -- <command>            # or --command-file <utf-8 file>
./sc vm capture [--output <path>]    # console screenshot under .sc-state/local/vm-captures
./sc vm reset --off                  # back to the configured baseline, powered off
```

Lifecycle verbs beside those:

- `stop` is graceful; `--force` is the only route to `virsh destroy`. `restart`.
- `snapshot list`, `snapshot create <name>` (any state; a running domain gets a
  live snapshot, and a hypervisor that refuses one answers
  `snapshot_live_unsupported`), `snapshot delete <name>` (the configured
  baseline is refused).
- `reset [<name>] --off|--running`; exactly one of the two flags is required.
- `bake [<name>]`: graceful shutdown, then a replace-not-stack offline snapshot
  that becomes the baseline. `./sc vm-bake` runs the same operation against
  libvirt in-process with no broker — host-only, never in the sandbox, and only
  when the broker is down.

`push` sources and `pull` destinations must sit inside the repo or
`.sc-state/local/`, and never inside `.sc-state/local/vm/` (the host''s key and
host-key pin live there); a pull destination inside `.super-coder/` or `.git/`
is refused too. Add `--json` to any verb for one result object.

The VM is a disposable test box, so install, reconfigure, snapshot, and reset
are yours (decision #372). `bake` replaces the baseline every later session
resets to: that is the snapshot refresh decision #434 leaves to the owner, so
bake when the FnB asked for it or agreed.

Sharing rule: the broker''s mutation lock is the only concurrency control, so
read `status` first, do not start, reset, or snapshot a VM another shell is
visibly using, and finish your own session with `reset --off` while you still
have control. The engine supervises nothing after you stop (decision #101).

## Posture 2 session

```bash
./sc remote status <name>
./sc remote exec <name> -- <command>          # or --command-file <utf-8 file>
./sc remote push <name> <repo-file> <remote-path>
./sc remote pull <name> <remote-path> <local-path>
```

`push` sources resolve inside the repo; `pull` destinations resolve inside the
repo or `.sc-state/local/`. Treat the remote as shared: leave it as you found
it and say what you changed.

## Structured errors

Every verb returns `{ok, operation, error: {code, message}}` on failure. The
full vocabulary is the Structured errors table in
`.super-coder/docs/remote-seats.md`; read the row for a code not listed here.
The ones you act on:

| Code | Do |
|---|---|
| `broker_unreachable` | report the printed start command to the FnB |
| `vm_busy` | another call holds the mutation lock and nothing was attempted; wait, then retry |
| `remote_path_not_allowed` | move the file inside the repo or `.sc-state/local/` |
| `remote_unreachable`, `remote_exec_failed`, `exec_failed`, `push_failed`, `pull_failed` | read `stderr` or the message; report, do not retry blindly |
| `snapshot_live_unsupported` | normal on many hosts: `./sc vm stop`, snapshot, then start again |
| `snapshot_protected` | the baseline is redefined with `bake`, never deleted |
| `stop_timeout`, `reset_result_unknown`, `<operation>_timeout` | the final state is unconfirmed and the work may still be running; read `status`, report it, do not retry blindly |
| any `adopt_*` | adoption is the FnB''s host-only command (`adopt_sandboxed` says so from a sandbox); report the code and message verbatim. `adopt_host_key_changed` names a pin file and the `rm` that clears it — leave that removal to the FnB. After `adopt_broker_failed` the `vm` block IS written: the resume is `./sc vm-broker-up`, then adopt again |

Any other code names host configuration (keys, the saved block, the host''s
OpenSSH): report it. Do not hand-install keys, aliases, or known-hosts entries,
and do not open a raw `ssh` to a target the broker serves. If a target needs
something the broker does not give you, stop and report it.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'self_update',
  'Update the Subfloor engine in place — in a fork, fetch + materialize new code + migrations; in the source repo, reconcile the tracked engine after a merge. All memory intact; sound rollback. The shell hands off to its own next boot. Use when a Subfloor update is available or an engine change just merged.',
  'substrate',
  'sc update',
  0,
  '# self_update — laying a new floor under your own feet

The local shell updates its own substrate — no external rebuild. All state lives
in the DB and engine code is read live each session, so a code-only update
touches no data; a schema change applies as an in-place migration, never a
destructive rebuild. `current_state`, narrative, decisions, flags, seed, and
L&S all carry across. This is succession for the substrate: you handing off to
you.

## In the source repo

The procedure below is written for a tracking fork. In the Subfloor source
repo (your boot''s PROJECT vs ENGINE section says you are upstream) the engine
is tracked source, so there is nothing to fetch, materialize, or pin: after an
engine change merges, sync the main checkout with `origin/main`, then run
`sc update`, which reconciles the tracked engine and its managed catalogue in
place. Steps 1, 3, 4, and 6 apply as written; step 5 has nothing to commit,
because the engine arrived by merge and `.sc-state/engine.ref` is not the
delivery unit here.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update''s output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the Subfloor engine from its upstream remote
   (named `super-coder` in existing forks),
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <subfloor-url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`''s hermetic rebuild surfaces it. A red
     render-check here = a local mirror to regenerate. Pipeline + guard details:
     `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit only the public update.**
   Stage `.sc-state/engine.ref` (the pin), the root `sc` dispatcher if it
   changed, and other deliberately authored public files. Snapshot SQL and
   `_sc` renders remain ignored beneath `.sc-state/local/`; never force-add
   them. `.super-coder/` and `engine.ref.prev` are also gitignored in forks.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first, under its own
   `prerollback` prefix so it is never mistaken for a pre-update restore point
   — rollback is itself reversible. Where that copy is *written* is an ordered,
   fail-closed choice: `$SC_DB_BACKUP_DIR` when set and writable, else
   `~/db_backups/<repo-name>/` (keyed by this fork''s repo dir name — distinct
   from any `db_backups/` dir the fork''s app keeps at its repo root), else the
   gitignored repo-local `.sc-state/db_backups/`. Pruning keeps the five newest
   backups per lifecycle prefix per directory, so classes never evict each
   other;
2. restores the DB from the newest pre-update backup found across *every*
   candidate directory, not just the currently writable one — the writable
   destination can change between update and rollback, and discovery must not
   hide a restore point behind it;
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes (source repo): structural change -> new
migration file, additive where possible.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'snapshot',
  'Admin work — serialize the live DB to the gitignored local snapshot and refresh the flat renders with sc snapshot, sc render flat, and sc render-check. Load before a rebuild, after authoring skills or documents that must survive one, or when render-check is red. A working shell never runs it; generated instance state never enters Git.',
  'substrate',
  'sc snapshot',
  0,
  '# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and
`.sc-state/local/content.sql` on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
the shared instance''s gitignored local cache. `sc snapshot` and `sc render`
run from the main checkout; the dispatcher refuses them from a linked shell
worktree. The GUI **Save locally** button, `install`, `update`, and
the authorized maintenance flows run them for you. `render-check` only checks
for drift; it does not refresh the snapshot or mirror. A working shell does not run them; its writes
are captured when admin saves locally before a rebuild. The rest of this skill
= the admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/local/content.sql` | **this repo''s** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only, gitignored) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

Generated artifacts always live beneath `.sc-state/local/`. A legacy
`artifact_mode: tracked` setting is accepted only as upgrade input and resolves
to local; mode switching and Git publication are retired.

## When admin serializes

All commands run from the main checkout. The gate reads the caller''s shell
token: the Admin shell and the host operator''s terminal pass; any other
launched shell is refused.

1. `./sc snapshot` -> dumps the per-instance tables to the active
   local snapshot path. Deterministic DELETE-then-INSERT in PK order makes
   re-running byte-identical.

2. `./sc render flat` -> regenerates the flat `_sc` files
   (`renders/specs_sc/`, `renders/docs_sc/`, `renders/skills_sc/`,
   `renders/roadmap_sc.md`) beneath `.sc-state/local/`. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `./sc verify` -> rebuilds from local text in an
   isolated disposable checkout without replacing the live DB.
   `sc render-check` rebuilds the DB hermetically from text and fails if the
   local mirror drifts from that render. `./sc render flat` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`''s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. Do not stage the output. Generated snapshots and renders are gitignored.
   Only authored engine source and explicit migrations belong in Git.

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo''s roadmap/docs): edit the
  DB -> `./sc snapshot`. The local DB is primary; the ignored snapshot is its
  rebuild source.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && ./sc render flat`, then `sc render-check`. Commit the
    regenerated `migrations/0001_seed_skills.sql`; the mirror stays ignored.

Steps 1–3 are the local durability path. There is no generated-artifact
publication path.

## Related skills

This skill owns the render/snapshot pipeline + the `render-check` guard:

- `self_update` — `sc update` refreshes the same local `_sc` files.
- `fork_skill_design` — DB-canonical fork-local skills persist via the local
  snapshot.
- `engine_migrations` — a **content-seed** migration (skills, flavor defaults)
  changes what renders; rebuild + render + `render-check` after.
- Document bodies live in the DB, render to `docs_sc/` / `specs_sc/`;
  authored via `sc mem doc`, serialized here.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge under the Sprint grant once live authorization returns, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the
Developer''s steps. Your typed handoffs are `complete-unit`, `register-pr`,
`request-review`, and the merged handoff `send`; each follows the protocol''s
order.

## Route the entry

| Trigger | First read / action |
|---|---|
| Assignment, verdict, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; accept or handle the relevant message. Accepted assignment -> `sc context --work-unit <id>` next. |
| Self-describing engine-wide PR fact | Inspect the fact + registered PR directly. Do not manufacture a Sprint inbox item; the next typed handoff''s inbox check covers it. |
| Live FnB instruction | Preserve its authority; read only durable state needed for safe action. |

## Bound the lane

`sc context --work-unit <id>` is the default planning context: assignment,
expected output, linked tasks, bound revision id, active decisions,
dependencies, unit blockers, roles, worktree, lifecycle walls, resources. Read
the full bound revision or broader indexes only for an unresolved need. Own
one active unit; never start another lane or edit another shell''s worktree.
Resolve ambiguity to shippable in-scope work + rationale. Ask the Planner
before changing boundary, interface, deliverable, priority, or scope; ask the
Reviewer about review evidence. Use the relay''s unit question/blocker form and
stop at a decision boundary until the answer arrives.

A Developer does not pause the Sprint. Report blocker or integrity evidence to
the Planner, continue safe independent work, and stop at the unsafe boundary.
The Reviewer decides continue/replan/pause; the Planner executes the decision.

## Build and verify

Sync + branch; implement the smallest complete change. Per TESTING POSTURE
(`spec_execution`), finish code + run every available smallest affected gate. If the
selected interpreter, runner, or declared dependency cannot execute one,
record exact seat evidence; the registered PR supplies only that proof. Test
assertion/source collection red or incomplete code = failure. Optional browser
skip = non-failing. Keep external calls outside DB transactions; preserve
durable identities and append-only evidence. Record failures, anomalies,
retries, review friction, and departures for closeout.

## Report-only or no-code completion

Only an explicitly planned report/no-code lane may finish without a PR. Its
final handoff requires a durable completion receipt:

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
red/green/closed/merged wakes continue (the engine may already have discovered
the PR from your worktree branch; `register-pr` attaches it). Required checks:
pending -> native wake; red -> fix/push; green -> judge/request review; none or
untrustworthy watcher after one bounded read -> report + block. Follow
context: armed -> fix red + judge/pass green + merged -> post-merge handoff;
paused -> fix red now + judge green, review after resume; no active Sprint ->
fix red if needed, green -> merge only on a standing FnB directive naming the
PR, merged -> git skill after-merge cleanup. Planner/Reviewer get none.

If the same registered PR was externally closed, then reopened, rebased, and
pushed, replay the exact `register-pr` command. Require `created: false`, which
keeps identity/ownership and takes a fresh snapshot. This is the one exception
to the protocol''s inbox step: a single pre-handoff inbox check covers the
registration replay + the immediately following review request.
Do not wait for a second PR-fact wake: immediately request review. Green
proceeds; any other snapshot returns the watcher diagnostic without partial
handoff. Never register a replacement PR or ask the Planner to bypass observed
green.

Otherwise, when no local action remains, stop for the native PR fact. A stalled
gate permits one bounded read, then stop or report its evidence:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop.

## Review handoff and correction

Complete each round in order:

1. Finish readiness judgment + available local proof; require observed green.
2. Use `submit` first or `resubmit` after changes requested. The engine injects
   the PR URL, registered id, exact green head, and work-unit id into the
   Reviewer''s canonical bare one-line locator. Create no readiness file. Send
   no scope narrative, verification evidence, rationale, or review-focus
   steering in the request. The PR body carries the work-unit id and spec
   reference plus your rationale (decisions, rejected trade-offs): each verdict
   opens a fresh chat with only GitHub to read. Write no PR comments or
   annotations.
3. Final handoff:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --intent <submit|resubmit> --key <stable-key>
```

   Require the durable write + Reviewer wake, then await the native verdict
   wake.

Changes requested arrives as a fresh chat: orient from the PR body, diff, and
verdict; do not re-litigate choices the rationale explains. Apply every
blocking finding, re-establish green, and resubmit with a new review-round key.
Do not narrate cleared findings; the Reviewer verifies the full diff. Record
disagreements as judgment. Reviewer owns scope/severity; Planner executes
resulting action.

## Merge boundary

Immediately before merge, re-read live GitHub, grant, ownership, unit state,
and checks through:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the returned repository, PR, and head SHA. A refusal means not
green, not approved, or not yours; fix that, never bypass it. A rebase does not
undo approval. The FnB granted this merge by arming the Sprint; this command
verifies that grant live and is the only gate — never wait for a separate FnB
directive, and never merge on approval or green alone.

## Post-merge handoff

After the authorized merge:

1. Clean the worktree; put merged PR + SHA, unit result, verification,
   judgments, and departures in the handoff file.
2. Final handoff:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent handoff --key <stable-merged-handoff-key>
```

   Require the durable message + Planner wake. Automatic merge observation
   records the PR transition; this handoff releases the next wave.

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

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch and restructure lanes, change participant routes, and execute Reviewer decisions through durable pause, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the Planner''s steps after
`sprint_prep` arms the Sprint.

## What you can read

| Need | Read |
|---|---|
| Whole Sprint: lifecycle, participants with current routes, units, dependencies, PRs, health | `sc sprint show --sprint <id>` |
| Messages addressed to you | `sc sprint inbox --sprint <id>` |
| Exact bound spec body | `sc sprint spec-revision --sprint <id> --document <id> [--body-only]` |
| PR-watcher evidence behind a stalled gate | `sc sprint watcher-state --sprint <id>` |
| Post-Sprint cleanup evidence | `sc sprint cleanup-status --sprint <id>` |
| Bounded history packet: judgments, pauses, anomalies, reports | `sc sprint compile-report --sprint <id> --limit 50` |
| Candidate routes and what each supports | `sc models list [<harness>]`; preview one with `sc models resolve <harness> [<model>] [--effort <level>]` |
| Shell roster, feature, spec tasks, settled decisions | `sc mem get shells`, `sc mem get roadmap`, `sc mem get tasks --feature <id>`, `sc mem get decisions` |

`show` participants carry `shell_id`, `role`, `harness`, `model`, `effort`,
`binding_status`, and `route_revision`; units carry `developer`, `reviewer`,
`disposition`, `prerequisite_ids`, and `pull_requests`. A Thinking level
applies only to controlled routes: `null` model + `null` effort is Harness
default, and Vibe takes no effort.

## Route the entry

| Trigger | Route |
|---|---|
| Sprint decision, merged-work handoff, question, blocker, relay | Inspect `sc sprint inbox --sprint <id>` once; handle that message. |
| Engine-wide completion or cleanup receipt | Inspect receipt + terminal state directly; it is informational — do not run the Sprint inbox, accept it, or close again. |
| Live FnB instruction | Act under board override; name FnB authority in durable evidence. |

You receive no PR-event wakes; red/green/closed/merged facts go to Developers.

## Durable running loop

Read only trigger-required lifecycle, unit, dependency, route, PR, expectation,
and anomaly facts. Browser presence is not progress.

```text
sc sprint dispatch --sprint <id>
```

Dispatch every dependency-ready lane; returned ids are wake identities.
Disposition + messages are release facts. Stable assignment generations and
occupied lanes make dispatch repeat-safe.

- Keep dependencies as hard sequence; restructure current projection under
  Planner authority, record why, and never rewrite completed history.
- Developers own local/PR proof, review/fix/merge. Complete code + unavailable
  local gate -> registered CI: pending wait, red fix, green review; browser skip
  is non-failing. With fallback, Planner NEVER mutates packages/toolchains or
  runs repair. No checks/untrustworthy watcher after one read -> blocker.
  Reviewers own verdicts/conformance; do not proxy handoffs/judgments.
- Record Reviewer decision id + exact action + receipt; never rewrite rationale
  as Planner judgment.
- Reviewer-approved Planner/FnB spec rebind:
  pause -> `sc mem doc edit` -> `sc sprint rebind-spec --sprint <id>
  --document <id> --expected-revision <old-sha256> --reason <decision>` ->
  replan -> resume. Pass = old/new hashes + changed boolean; conflict -> reread.
- Relay Developer integrity evidence, impact, and recommendation to the
  Reviewer. Send required context before pausing: paused Sprint relay is
  unavailable.

## Reviewer decisions and Planner actions

For a required-reply Reviewer decision, keep this order:

1. Re-run `sc sprint inbox --sprint <id>`; verify the assigned Reviewer + retain the id.
2. Send a linked acknowledgement (`--intent information --reply-to <decision-message-id>`).
3. Require the reply command to confirm its durable message and wake; retry the
   same command/key if ambiguous.
4. `sc sprint accept --sprint <id> --message <decision-message-id>`.
5. Only after acceptance, execute the requested transition without
   re-adjudicating it. The linked reply must precede any pause or abort that
   makes the relay unavailable.

Record decision id + reply, acceptance, and action receipts. Clean conformance
closes atomically and sends an informational receipt; no reply/accept is
needed. If an action fails a lifecycle/authority/disposition precondition,
send the refusal + durable state to the Reviewer (or FnB for an override),
substitute nothing, and stop.

### Pause or resume

Pause for a Reviewer decision or safe Planner restructuring; preserve partial
artifacts, interrupt intent, judgment, and evidence:

```text
sc sprint pause --sprint <id> --reason <decision-or-restructure-reason>
```

Pause interrupts every live participant turn, and resume re-enters each
interrupted participant automatically with one re-enter wake into their
existing chat. For silence, read the board (`sc sprint show --sprint <id>`);
the protocol''s health rule forbids status-check messages.

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
new ownership generation before treating the Sprint as resumed. An exhausted
recovery wake = bounded manual evidence: preserve the unread message + failed
wake, involve FnB, create no recursive fallback. Drift informs but never
silently blocks resume.

Aborted-Sprint PR ownership repair belongs to the originating Planner. Keep
the replacement Sprint paused; establish old/new identity, then:

```text
sc sprint reconcile-pr --sprint <replacement-id> --repository <owner/repo> \
  --pr <number> --work-unit <replacement-unit-id> --reason <recovery-reason>
```

It refuses a live source or target Sprint, a non-originating Planner, an
invalid/owned target, and a closed-unmerged PR. Require a separate Reviewer
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

Never edit a released lane in place. Pause -> recall the unmerged lane
(`sc sprint recall-unit --sprint <id> --work-unit <id> --reason <reason>`) ->
replan -> resume. Recall preserves message/event history, returns only
unmerged work to planned, and refuses terminal/PR-bound work. PR-bound work
stays; plan replacement or use reconciliation. Resume creates a fresh
assignment generation. One spec task may govern repeated verification or
replacement lanes; each lane lists it once — do not duplicate the spec task.

Close a released lane terminal when its work finished out-of-band (a PR that
merged while paused) or its lane is abandoned — the PR-bound case recall
refuses:

```text
sc sprint resolve-unit --sprint <id> --work-unit <id> \
  --to completed|cancelled --reason <reason>
```

Paused-only; retires the lane''s open expectations, supersedes its PR links
(registration kept for reconcile-pr), and wakes both seats.

To change a future assignment or review route, pause the armed Sprint, take
each participant''s `shell_id` and current route from `sc sprint show`, preview
the replacement with `sc models resolve`, then replace the route and resume:

```text
sc sprint reroute-participant --sprint <id> --participant-shell <id> \
  --harness <harness> [--model <model>] [--effort <effort>] \
  [--route <display-route>]
```

Prepared Sprints may reroute directly. Reroute declared participants only. On
a decline, preserve the reason and choose a replacement from current capacity;
ask the Reviewer only if review/conformance judgment changes.

### Re-enter after conformance

The Reviewer decision names findings; governing tasks (existing for the same
scope, new title/description for new scope); and grouping, waves,
dependencies, routing, capacity. Preserve it; do not absorb extra or
post-Sprint scope or maximize occupancy.

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

Reuse a task for exact repair/repeat; add only genuinely new scope. Bind every
task, then confirm routes, dependency graph, and capacity plan match the
decision. Release ready lanes with `sc sprint dispatch --sprint <id>`. The
engine sends the next delivery-terminal wake; you do not initiate conformance.

### Conclude or abort

Clean `record-conformance` by the Reviewer atomically stores conformance,
the Reviewer-authored final report, completion, and your
informational receipt. On it, verify Sprint/reports/outcome/completed state.
Do not run `complete`; do not author a second report; do not manually close
peer chats.

The completion receipt is the only success wake: it names the Developer chats
the engine closed and the artifact directory it deletes. Your chat and every
Reviewer''s persist; no worktree is reset. Do not poll cleanup or reset
participant trees. A second wake arrives only if artifact deletion failed —
inspect once and retry only after correcting the named condition:

```text
sc sprint cleanup-status --sprint <id>
sc sprint cleanup --sprint <id> --key <stable-retry-key>
```

Require `created`, cleanup request id, action, exact target ids, and aggregate
projection. Reuse the key only for the same request. Abort only on a Reviewer
decision or FnB override; it is terminal and deletes nothing:

```text
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

## Handoffs and stop

Never dispatch the next wave from merge observation. The merged-work handoff
wake is the only normal next-wave dispatch trigger. On it:

1. Run `sc sprint inbox --sprint <id>`; inspect the merged handoff + unit/dependency state.
2. Handle earlier informational items and `accept` each, including the handoff.
3. Finish reconciliation and Planner bookkeeping; no work remains.
4. As the literal final action run `sc sprint dispatch --sprint <id>`.
5. Require durable assignments + wakes, then stop. Run no trailing command.
   Empty dispatch remains final; investigate only on a later durable wake.

On a clean completion receipt, verify the named Sprint is terminal and record
the closed Developer chats; run no close command. Then stop — a cleanup wake
arrives only if artifact deletion failed.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_protocol',
  'The shared Sprints v2 protocol every participant follows — lifecycle, wake types, inbox/accept/decline, the typed relay with stable keys, the one handoff order and body limit, artifact paths, receipt recovery, and the authority boundary. Load first in every Sprint turn, then your role skill.',
  'workflow',
  NULL,
  0,
  '# sprint_protocol — what every Sprint participant does the same way

Load this first in any Sprint turn, then your role skill (`sprint_prep`,
`sprint_pln`, `sprint_dev`, `sprint_rev`). Use the simplest path the current
durable state supports; treat authority, lifecycle preconditions, durable
writes, and typed handoffs as hard boundaries and use judgment inside them.
Repeat a read only when later activity could have changed it or the next
command requires live revalidation.

## Lifecycle

One Sprint binds one roadmap feature, exact governing spec revisions, a
participant set (one Planner, Developers, Reviewers) each on one
harness/model/effort route, and work units: editing lanes of spec tasks, each
with one Developer and one Reviewer, ordered by dependencies and waves.
`prepared` (editable) -> `armed` (ready lanes dispatch to Developers) <->
`paused` (relay off; restructure and reroute here) -> `completed` or `aborted`
(terminal; nothing deleted). One Sprint is armed at a time. A lane: dispatched
-> Developer builds, registers the PR, requests review -> Reviewer records a
verdict -> Developer merges under the Sprint grant once `authorize-merge`
returns live green + approved -> the merged handoff wakes the Planner, who
dispatches what became ready. After the last lane the conformance Reviewer
records the whole-Sprint report; the engine closes the Sprint, closes the
Developer chats, and deletes the Sprint artifact directory. Planner and
Reviewer chats persist; no worktree is reset.

## Wake types

Three literals name how a Sprint message reaches you:

| Type | Delivery |
|---|---|
| `new` | a fresh chat when the shell has none or its chat is idle; absorbed at the boundary of a live turn |
| `force-new` | never absorbed; waits for the live turn to end and a quiet gate, then closes the old chat and opens a fresh one |
| `re-enter` | resumes the existing chat at its next boundary |

Assignments, review requests, and verdicts arrive `force-new`; Planner-bound
results, decisions, and PR facts arrive `re-enter`. One engine wake names its
moment rather than a sender:

| Wake | Meaning |
|---|---|
| resume re-enter | a pause interrupted your live turn; on resume the engine queues this `re-enter` notification into your chat — continue that lane from its current state, do not re-accept the assignment |

You never choose a type, poll, boot a participant, or schedule a watcher; the
engine delivers. Health is reporting-only: nobody — engine or Planner — pings
a participant for silence; the board carries that signal. Stop after a
successful typed handoff and wait for the next wake.

## Inbox, accept, decline

```text
sc sprint show --sprint <id>
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Inspect the inbox once per trigger. Accepting an assignment starts ownership;
decline only with a concrete reason. After an informational message, `accept`
marks it read and changes no Sprint or work-unit state.

## Relay

Every message is one short body file. Unit question or blocker (requires a
reply):

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question|blocker --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Cross-unit, closeout, or external-authority rulings are Sprint-level:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the reply inherits its scope, so never add
`--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm the durable reply, then `accept` the incoming message. At a decision
boundary, stop until the required answer arrives; unread recovery re-wakes the
recipient, so send no duplicate reminder.

**Stable key** = recipient + exact body + intent + reply target + scope. Reuse
it only to retry the same failed or ambiguous write; when any field changes,
use a new key. Rejected or transport-failed -> correct and retry. Relay itself
unavailable -> give the FnB the attempted command, evidence, impact, and
recommendation; invent no alternate protocol.

## Typed handoffs

A typed handoff is a command that passes work to another seat: a relay `send`,
`complete-unit`, `register-pr`, `request-review`, `record-review`,
`record-conformance`. Every one follows this order; role skills name their
handoffs and add only what differs.

1. Finish the work, the bookkeeping, and every body file first.
2. Re-run `sc sprint inbox --sprint <id>` once; handle and `accept` new items.
3. Keep each body, result, verdict, and report file near 6,000 characters. The
   engine refuses anything over 8,000 and writes nothing, so check with
   `wc -m < <path>`.
4. Run the command. It is complete only when it exits successfully and
   confirms the durable write and the wake.
5. Do not run the inbox again. A handoff a role skill marks **final** ends the
   turn: it is the literal last action, with no trailing Git, Sprint, inbox,
   cleanup, or status command after it. Stop and wait for the next wake.

## Receipt recovery

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint: retry the exact command once, then use its normal read surface once to
prove the postcondition (for an informational `accept`: the message was in the
inbox and is now absent). Continue under that proof and name the receipt
defect in your next handoff. Never use this to infer assignment ownership,
review outcome, merge authorization, a lifecycle or work-unit transition, the
governing revision, PR head or green state, or cleanup authority; an unproved
postcondition stops.

## Artifacts

Working material — review notes, raw diffs, evidence packets, report drafts,
scratch proof — goes under the gitignored `shared/sprints/sprint-<n>/`; never
commit, branch, or PR it (a review-notes commit is a finding). Durable records
are DB rows: judgments via `record-review`, reports via `record-conformance`,
decisions in the relay.

## Authority

Reviewers own judgments: verdicts, conformance, re-enter and abort decisions.
The Planner owns plan structure — lanes, dependencies, waves, assignment,
routes, pause, resume, dispatch — and executes Reviewer decisions without
re-adjudicating them. A Developer owns one lane and its PR. The FnB may
override any of it from the GUI Sprints tab; name that authority in the
durable evidence when acting under it. A command that rejects a transition
returns the durable state to the deciding role; substitute nothing.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages. Inside an armed Sprint the recorded verdict replaces the Reviewer boot''s FnB-gated handoff.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, the typed-handoff order and body limit, artifact
paths, receipt recovery, and authority boundary. This skill holds only the
Reviewer''s steps: pre-declaration QA/QC, one work-unit review, or whole-Sprint
conformance. Your typed handoffs are `record-review`, `record-conformance`,
and a Sprint-level `decision` to the Planner; each is final and follows the
protocol''s order.

Inside an armed Sprint this verdict path stands in place of the Reviewer
boot''s FnB-gated handoff. Your boot''s REVIEW PROCEDURE has you draft the
handoff, propose it to the FnB, and send only on approval; here the FnB
already authorized the loop by arming the Sprint, so you record the verdict
or decision directly, with its findings in the body, and wait for no
per-review approval. Outside an armed Sprint the boot procedure governs.

## Route the entry

| Entry | Route |
|---|---|
| Explicit pre-declaration QA/QC request | Read and sign the exact current spec directly; there is no Sprint id or Sprint inbox yet. |
| Work-unit review / `sprint.delivery_terminal` | Inspect the Sprint inbox once; accept the actionable request. |
| Live FnB instruction | Preserve board-level authority; read only durable state needed for independent judgment. |

QA/QC precedes all Sprint inbox commands and is one write:

```text
sc mem doc qaqc <spec-document-id> --verdict pass|fail [--findings-doc <document-id>]
```

Reviewers never receive PR-event wakes.

## Conformance decisions and Planner controls

You own review, re-enter, abort, and conclude judgments. The Planner
independently owns operational plan structure: safe-edit pauses, recalling
unreleased work, lane changes/repeats, assignment/routing, unreleased-scope
cancellation, and validated resume. Never run standalone pause, replan,
recall, reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Base judgment on durable Sprint state, bound revisions, current work/PR facts,
progress-carrier evidence, and ratified judgments. A decision body names:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- Reviewer-owned evidence + rationale;
- exact Sprint/unit ids, reason, outcome, and complete action arguments;
- immediate safety impact for FnB.

The Planner verifies your identity and executes the transition without
surrendering plan authority. A rejected action requires a revised judgment
supported by returned durable state, never an improvised bypass. A live FnB
instruction is the FnB''s board-level override.

## Severity rubric

- **Critical** — active security/authority violation, destructive corruption,
  or unsafe continued operation.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or silently wedged delivery/recovery.
- **Medium** — concrete normal-use correctness/recovery gap, missing negative
  enforcement, or unreliable handoff.
- **Low** — bounded cleanup, clarity, test-depth, or resilience improvement;
  delivered behavior remains correct.

Critical/Major/Medium block unit approval; Low is a report note. At closeout,
severity does not decide timing: you judge whether each finding requires
in-Sprint patching or acceptable post-Sprint follow-up.

## Work-unit review

Accept the request and retain that exact message id. Its body is a bare
locator: intent, PR URL, registered PR id, exact head, work-unit id. Scope
narrative, verification, rationale, or focus steering is a protocol defect. PR
comments and annotations are forbidden; the PR body contains only unit id +
spec reference plus the Developer''s rationale.

Bind inspection/verdict to the accepted request''s message id, registered PR,
and work unit. Review the live PR head; a rebase since the locator''s head is
not a defect. Read the exact spec revision + full diff, then checks, tests,
relevant runtime facts, and ratified judgments. Each round is clean: no prior
Developer evidence or prose; prior findings clear only when the new head
proves it. Trace code paths, failure cases, and spec behavior rather than
names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable; the handoff remains green-only, without exception
or waiver: do not note the failure and approve anyway.

- In-scope failure -> record `changes_requested` so the Developer fixes them and restores green.
- Out-of-scope failure -> keep the lane unapproved and send the Planner a `replan`
  decision naming the failures; Planner widens the lane or cuts follow-up work.

Read cited and feature-scoped resolved flag evidence through memory
(`sc mem get flags <flag-id>`, `sc mem get flags --feature <id> --resolved`).

Each finding pins severity/title, violated invariant, exact location/evidence,
reproducible consequence, and fix boundary without unnecessary architecture.

Finish every inspection, finding, and the verdict body, then record the
verdict as the final handoff:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested|approved --body-file <path> --key <stable-key>
```

Require durable judgment evidence + the Developer wake.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the surface; an
unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode''s identity. Proceed only when the notification names this shell as the
selected conformance owner for its current ownership generation. A different
Reviewer accepts the informational notification if received and records no
conformance. Inspect inbox, lifecycle, and units first:

- Already completed/aborted -> `accept` notification and stop.
- Any non-terminal unit visible -> the wake is stale: `accept`, stop, and
  await a fresh delivery-terminal episode.
- Only an armed Sprint whose units are all terminal enters conformance.

Compile the bounded evidence packet first, yourself:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase only when truncation omitted needed evidence; maximum 200. Judge
integrated `main` against every bound/current revision + ratified judgment. All
units cancelled and nothing shipped -> `abort`, not `conclude`.

Choose one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision with every blocking finding; each spec
  task''s title and description; grouping, waves, dependencies, routing, and
  capacity rationale. State independent lanes, expected review overlap, useful
  reserve, and critical-path effect. After three re-entry episodes, escalate
  non-convergence to FnB.
- **Clean or post-Sprint-only findings.** Prepare conformance report,
  final report, reason, and outcome; submit the atomic close below. Send no
  conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify every requirement
`as-specced`, `deviated-intentionally` with ratified judgment,
`deviated-silently`, or `unimplemented`; the last two are findings. Include
spec document + work-unit ids when known.

For the clean branch, write findings directly in the conformance report.

Before recording conformance, author the final Sprint report. Name yourself as
author and cover governing scope/revisions, shipped units/PRs, judgments +
ratified deviations, failures/retries/recovery/anomalies, conclusion,
follow-ups, and evidence location. Preserve discrepancies. Both reports are
handoff bodies under the protocol''s limit.

Record one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

Require the receipt: conformance report id, final report id,
completed state, Planner message id, and Planner wake id. Closing ends the
Developer chats and schedules deletion of the Sprint artifact directory; your
chat persists and no worktree is reset. Do not poll cleanup, wait before
stopping, or manually close peer chats. Never reopen editing after recording;
a re-enter defers reports until new scope is terminal and a fresh
delivery-terminal wake arrives.

## Stop

Each ending is a final handoff in the protocol''s order:

- Unit review -> `record-review`.
- Clean conclude -> the atomic `record-conformance` command above, once every
  artifact and body is final. When it confirms completed state and all receipt
  identities, stop; the Planner is notified.
- Re-enter/abort -> the Sprint-level `decision` to the Planner (relay form in
  `sprint_protocol`); require durable write + Planner wake, and run nothing
  more until another native wake.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'tailscale_diagnostics',
  'Observe declared tailnet hosts through the host ts-broker with sc ts — tailnet status and a fixed read-only diagnostic verb table on readonly_hosts. Load before running anything on a tailnet host. Read-only applies to machines in real use; a test seat reached over the tailnet is still a test seat. Opt-in; a readonly_refused answer is a stop, never something to work around.',
  'substrate',
  'sc ts',
  0,
  '# tailscale_diagnostics — look, do not touch

## Posture

Read-only applies to machines in real use: workstations, servers, production.
They have no undo, so a shell observes there and changes nothing. That is a
precaution against accidents, not distrust. GitHub has history and test VMs
have snapshots, so mistakes there are recoverable; a test seat reached over
the tailnet is still a test seat. The class of machine decides, not the route:
on a test seat you work freely and may reset it to its snapshot at the end of
your own session without asking, while refreshing a shared snapshot stays the
owner''s call (decision #434; `remote_seats` holds the seat rules).

## The broker

The ts-broker runs on the host with the host''s Tailscale identity and serves
two verbs over a unix socket; `./sc ts` is the client. Keeping that identity
host-side is hygiene, not a mechanism that confines you. The fork''s `ts` block
names `readonly_hosts` (machines in real use: diagnostic verbs only) and
`allowed_hosts` (unrestricted exec as the configured `ssh_user`; a test seat
belongs here). A host in `readonly_hosts` is implicitly allowed; a host in
neither list is refused.

```bash
./sc ts status                        # tailnet backend, self, peers
./sc ts exec <host> -- <command>      # one policy-checked command
./sc ts init --ssh-user <user> [--allowed-host H]... [--readonly-host H]... [--tailscale-bin <path>]
```

`init` writes only the `ts` block and prints whether the broker is ready and
the command that starts it (`./sc ts-broker-up`). Every result is one JSON
object: `{ok, exit, stdout, stderr}` for `exec`.

## Read-only doctrine

A shell never reaches a read-only host with `tailscale` or `ssh` directly, on
bare metal or otherwise, even when the operator''s own tailnet identity or a
login would let it; the only path to a `readonly_hosts` entry is
`./sc ts exec`, and observation means observation — read state and logs,
report what you saw, and change nothing on that host by any route. The broker
enforces the verb table only on its own path; nothing on the device enforces
it, so this paragraph is the rest of the boundary (decision #354), kept by you
because that machine has no undo.

## What a read-only host accepts

The broker accepts a command on a `readonly_hosts` entry only when its first
tokens match this table, the string contains none of `; & | > < $ \` ( )` or a
newline or carriage return, and `sudo` appears nowhere. The table lives as data
in `ts.py` (`READONLY_COMMANDS`); trailing operands select a unit, container,
or file.

| Verb | Permitted forms |
|---|---|
| `systemctl` | `status`, `is-active`, `list-units`, `list-timers` |
| `journalctl` | any flags except `--vacuum*` and `--rotate` |
| `pm2` | `status`, `list`, `describe`, `logs --nostream` |
| `docker` | `ps`, `logs`, `inspect`, `stats --no-stream`, `images` |
| host facts | `df`, `free`, `uptime`, `ps`, `ss`, `ip`, `nproc`, `hostname`, `whoami`, `id` |
| files | `ls`, `stat`, `cat`, `head`, `tail`, `du` |

Pipes and redirections are refused, so ask for the raw output and filter it on
your side.

## On `readonly_refused`

The broker answers `{ok: false, error: "readonly_refused", token: "<what>"}`
and names the offending token. Stop. Report the host, the exact command, and
the token to the FnB or the assigning shell. Do not rephrase the command to
slip past the table, split it into permitted pieces that add up to a change,
run it from another host, or open your own session to the target. The broker
does not restrict a host that is only in `allowed_hosts`, so the class of
machine still decides there: a test seat is yours to change, and a machine in
real use is changed only when the FnB assigned that change.

Other refusals: `not in allowed_hosts or readonly_hosts` means the fork has
not declared the host — ask the FnB; `broker_unreachable` means the ts-broker
is down — report the printed start command.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'windows_testing',
  'Windows-only add-on to remote_seats — two-command guest adoption, the shell''s authority over the box, PowerShell exec conventions, capture, and the managed Windows-MCP GUI transport for a posture 1 Windows VM. Opt-in; load with remote_seats.',
  'substrate',
  'sc vm',
  0,
  '# windows_testing — the Windows add-on

`remote_seats` owns the posture, setup, the session shape, sharing, snapshots,
and errors. This skill adds only what a Windows guest changes. Linux guests
never need it.

The Windows guest is a test seat: mistakes there are recoverable from its
snapshot, so work freely and reset it at the end of your own session without
asking. Refreshing the shared baseline is the owner''s call (decision #434).

## Preparing a Windows guest

Bring-your-own-licence: the operator supplies a booted, licensed Windows 10 or
11 guest with a working network and a local administrator account. Adoption is
two commands. First, in an elevated PowerShell on the guest console:

```powershell
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=''Tls12''; irm https://raw.githubusercontent.com/jedbjorn/subfloor/<ref>/.super-coder/assets/winbox/bootstrap.ps1 | iex"
```

`<ref>` is the engine''s pinned commit — `.sc-state/engine.ref` when that file
exists, else `main`; `./sc vm adopt` prints the exact line. Then, on the host:

```bash
./sc vm adopt --domain <libvirt-domain> --ssh-user <account>
```

Adopt locates the guest, installs a host-held key behind one password prompt,
turns password auth off, pins the host key, provisions, verifies, writes the
`vm` block, brings the broker up and takes the baseline snapshot. The key is
generated on the host and never handed to a shell (decision #353) — key
hygiene, not a control on you; the password is read from the operator''s TTY,
never on argv, never persisted. Every phase is
idempotent, so re-running adopt after a toolchain change skips what is already
satisfied. `./sc vm init` still hand-links a guest prepared by other means.

Provisioning reads the fork-tracked `.subfloor/winbox.json`. Every key is
optional:

```json
{
  "winget_manifest": "winget-manifest.json",
  "checks": ["dotnet --version", "git --version"],
  "mcp": true,
  "mcp_port": 8000,
  "workspace": "C:\\SubfloorTest"
}
```

Defaults: `winget_manifest` is `winget-manifest.json` at the repo root when that
file exists, else none; `checks` empty; `mcp` true; `mcp_port` 8000; `workspace`
`C:\SubfloorTest`. The declared `checks` are also what `./sc vm status` runs as
the toolchain check — none declared means the check passes with "no checks
declared". Each entry is a command line run through the guest''s `cmd.exe`, so it
must not contain a double quote: cmd re-parses the line and the quotes arrive as
literal characters. Anything needing quotes goes in a `.cmd` file you push and
name instead. Changing the fork''s toolchain means editing `winbox.json` and
running `./sc vm adopt` again, or doing it by hand over `./sc vm exec` and then
`./sc vm bake` — both redefine the baseline, so both wait for the FnB''s word.

## Authority

The guest is a disposable test box: it holds no real data and no real accounts,
snapshots are free, and the FnB owns the physical host. So install software,
change settings, schedule tasks, snapshot and reset are all yours
(decision #372). Snapshot before a risky change and reset to it when the change
goes wrong. `./sc vm snapshot create <name>` is allowed while the domain is
running, but many hosts cannot checkpoint a live guest (a host-passthrough CPU
with non-migratable flags, UEFI pflash) and answer `snapshot_live_unsupported`.
That is the normal case, not a fault; take the snapshot offline instead:

1. Finish or save any in-guest work — an offline snapshot does not keep memory.
2. `./sc vm mcp down` if the tunnel is up, then `./sc vm stop`.
3. `./sc vm snapshot create <name>` (now offline), then `./sc vm start`.
4. Later, `./sc vm reset <name> --running` returns to that state booted, or
   `--off` leaves it powered down.

The baseline is the one snapshot `reset` depends on, and delete refuses it. It
is redefined only with `./sc vm bake [<name>]`, when the FnB asked for a new
baseline or agreed to one. Leave the box in a state the next shell can either
use or reset: finish with `./sc vm reset --off`, or say plainly what you
changed and which snapshot returns the box to the state you found it in. The
sharing rule in `remote_seats` still governs.

## PowerShell exec conventions

`./sc vm exec` runs under `cmd.exe`, so invoke PowerShell explicitly and
prefer a command file for anything longer than one line:

```bash
./sc vm exec -- powershell -NoProfile -NonInteractive -Command "Get-Service sshd | Select Status"
./sc vm exec --command-file run-tests.ps1.cmd      # exact UTF-8 contents, one command
```

The command file holds the exact text the guest receives; a typical body is
`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\SubfloorTest\run.ps1`
after `./sc vm push run.ps1` lands it in the guest workspace. Use Windows paths
on the guest side and `--json` when you need `exit_code`, `stdout`, and `stderr`
separately. A guest path for `push` and `pull` may be written with either
separator — `C:\SubfloorTest\out.bin` or `C:/SubfloorTest/out.bin` — because
the engine normalises it for scp''s SFTP mode; `exec` command text is yours and
is passed through untouched. The transfer path limits are in `remote_seats`.
Guest console output is decoded lossily; base64-encode guest-side
(`[Convert]::ToBase64String(...)`) when a result must be byte-exact, and write
large results to a file and `./sc vm pull` it rather than through stdout.

## Capture

`./sc vm capture [--output <path>]` saves a console screenshot as a mode-0600
file under `.sc-state/local/vm-captures/`; an explicit `--output` must stay
there. Installers and dialogs show state on screen, so capture before and after
a GUI step and after any timeout.

## Windows-MCP transport

```bash
./sc vm mcp status      # adapter, tunnel, relay, endpoint — read-only
./sc vm mcp up          # SSH-forward the guest''s MCP port to run/vm-mcp.sock, start the relay, probe the endpoint
./sc vm mcp down        # stop relay and tunnel; reports each separately
```

Supported harnesses receive a managed `windows-mcp` adapter definition;
`mcp up` is what brings that endpoint online (in a sandbox it also runs the
in-container relay on 127.0.0.1:18000). Do not add an MCP server by hand or
edit harness configuration. Drive the GUI through the MCP tools by element,
verify each step with a capture, and `mcp down` before `reset`.

Provisioning installs the guest side as the per-user login task
`windows-mcp-server`, listening on the guest''s loopback only; the tunnel is the
only route in. Because it is a LOGIN task, a guest with no interactive desktop
session has it installed and correct and never started — adoption reports that
as a warning and finishes, so a freshly adopted guest with no listener is
expected, not broken. `./sc vm mcp up` is what reports readiness. When it finds
no listener, log the adopting account in on the console, or re-run the install
yourself over exec:

```bash
./sc vm exec -- windows-mcp install --transport streamable-http --host 127.0.0.1 --port <mcp_port>
```

That is a legitimate repair under Authority above, not a workaround. Report what
you did.

## Session end

Finish as `remote_seats` says: `./sc vm mcp down` if you brought it up, then
`./sc vm reset --off`, and report test results separately from the reset
result. A `reset_result_unknown` answer is reported as-is; run `./sc vm status`
and stop rather than retrying the reset.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'planner', skill_id FROM skills
WHERE name='spec_authoring' AND is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'dev', skill_id FROM skills
WHERE name='spec_execution' AND is_deleted=0;

UPDATE skills SET description='Load before you run or diagnose sc deps, sc test, sc lint, or sc typecheck, when a hook reports any state but ready, or when you plan what this fork''s seats can prove. Holds the fork''s declared development hooks, execution seat, readiness states, evidence locations, and supported recovery surfaces. Planner-only on demand; Developer and Reviewer receive the same inventory in boot.',
  content='# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

`SC_SEAT=gui` wraps shell `test`, `lint`, and `typecheck` invocations in one
registered job. After the job id is confirmed, end the turn; its completion
wake tells you to inspect `sc job status <id>` and `sc job tail <id>` before
continuing. TUI and Admin hooks remain foreground without a completion wake
and return the child status; `deps` remains foreground in every seat. The wrapper marker is consumed by the
runner and removed from the hook''s environment, so nested hooks get their own
foreground run and return their real status without a completion wake. A
separate nesting marker prevents GUI wrapping inside a hook.

Every executed hook retains a log and adjacent `.receipt.json` under
`.sc-state/local/devkit-logs/<hook>/`. The receipt records declared argv with
appended arguments, checkout, host/container seat, starting commit and branch,
exit status, duration and log path. Test receipts parse pytest''s terminal
summary and failing ids; an absent summary is `null`, never zero failures.
Authenticated shell runs register before execution; wrapped hooks attach to
the existing run. Standalone CI and Admin retain local receipts without
claiming a shell run or completion wake. Receipts are local test evidence;
GitHub checks still own the merge gate.

Pruning retains the newest twenty completed log/receipt pairs per hook and
keeps ledger receipts with `evidence_pruned` set. If that acknowledgment is
unavailable, the files remain for a later pruning attempt. Submission outages
retain the run''s receipt and outcome for reconciliation, without rerunning the
hook. `SC_DEVKIT_OUTPUT=full` streams stdout and stderr while retaining the same
log and receipt. Provisioning/readiness evidence lives under
`.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork''s test assertions,
deployment ritual, database technique, or VM lifecycle.

## Run receipts

Attach a finished local run to its PR or work unit with
`sc job attach <id> --pr <number>|--work-unit <id>` (`sc job attach --help`).
The rules live in the Developer boot''s RUN RECEIPTS section; a receipt
supplements CI and never authorizes a merge.'
WHERE name='dev_kit' AND content='# dev_kit — read the fork development contract

The fork owns `.subfloor/dev-kit.json`. Subfloor validates the declaration,
selects the invoking checkout and host/container seat, runs exact hook argv,
and retains readiness evidence under `.sc-state/local/dev-kit/`. The engine
does not infer project policy from manifests or install privileged host tools.

## Hook inventory

The supported hook names are `deps`, `test`, `lint`, and `typecheck`:

```bash
sc deps [args...]
sc test [args...]
sc lint [args...]
sc typecheck [args...]
```

Read the declaration and its executable before running a hook. A configured
hook reports the selected checkout, cwd, seat, executable, and child status.
An absent hook is `unavailable`; do not reconstruct one from package metadata.

## Canonical states

| State | Meaning | Supported recovery |
|---|---|---|
| `absent` | No declaration exists; engine-baseline tools remain mechanisms, not project policy. | Add a tracked declaration only when the fork needs one. |
| `declared` | The declaration is valid; hook configuration is known, but execution/receipt evidence decides readiness. | Run the exact configured hook. |
| `invalid` | Declaration, path, mount, image, or invocation validation failed. | Correct the named tracked input and retry. |
| `ready` | The hook can execute on the active seat or the exact Docker receipt is current. | Continue. |
| `failed` | A declared hook or provisioning attempt ran and failed. | Inspect retained logs/evidence; retry the same supported surface. |
| `stale` | Docker provisioning or package evidence no longer matches the declaration, checkout, image, or labels. | From the host, run `sc launch`; use repair only after a failed attempt. |
| `advisory` | Engine baseline is runnable while a declared native-package candidate is degraded. | Inspect the named advisory evidence and submit a reviewed tracked remediation. |
| `repair` | A retained-container repair session is open without readiness. | Exit to the host, rerun `sc launch`, and require `ready`. |

Unavailable executable = exit 126; missing hook = exit 78; invalid
configuration = exit 64. A started child preserves its own status.

## Seats and evidence

Host hooks use the host checkout and toolchain. Container hooks use the
bind-mounted checkout, engine-baseline tools, declared sandbox extension, and
current provisioning receipt. `$SC_DEV_PORT` is loopback-bound on the host and
published from `0.0.0.0` in the container. A configured `$DATABASE_URL` reaches
the fork application sidecar; it never points at the engine memory DB.

`SC_SEAT=gui` wraps shell `test`, `lint`, and `typecheck` invocations in one
registered job. After the job id is confirmed, end the turn; its completion
wake tells you to inspect `sc job status <id>` and `sc job tail <id>` before
continuing. TUI and Admin hooks remain foreground without a completion wake
and return the child status; `deps` remains foreground in every seat. The wrapper marker is consumed by the
runner and removed from the hook''s environment, so nested hooks get their own
foreground run and return their real status without a completion wake. A
separate nesting marker prevents GUI wrapping inside a hook.

Every executed hook retains a log and adjacent `.receipt.json` under
`.sc-state/local/devkit-logs/<hook>/`. The receipt records declared argv with
appended arguments, checkout, host/container seat, starting commit and branch,
exit status, duration and log path. Test receipts parse pytest''s terminal
summary and failing ids; an absent summary is `null`, never zero failures.
Authenticated shell runs register before execution; wrapped hooks attach to
the existing run. Standalone CI and Admin retain local receipts without
claiming a shell run or completion wake. Receipts are local test evidence;
GitHub checks still own the merge gate.

Pruning retains the newest twenty completed log/receipt pairs per hook and
keeps ledger receipts with `evidence_pruned` set. If that acknowledgment is
unavailable, the files remain for a later pruning attempt. Submission outages
retain the run''s receipt and outcome for reconciliation, without rerunning the
hook. `SC_DEVKIT_OUTPUT=full` streams stdout and stderr while retaining the same
log and receipt. Provisioning/readiness evidence lives under
`.sc-state/local/dev-kit/`. Planner
uses this skill for pinch-hit development and capability design. It describes
the available surface and boundaries, not the fork''s test assertions,
deployment ritual, database technique, or VM lifecycle.

## RUN RECEIPTS

Attach completed local evidence by run id with `sc job attach <id> --pr <number>`
or `sc job attach <id> --work-unit <id>`. The run and target must share an owner;
a PR must already be registered. Use `--repository owner/name` if its number is
ambiguous. The Runs drawer and fleet offer the same attachment action.
Receipts show beside the observed GitHub check in Chats and the Sprint board.
A commit outside the PR head''s ancestry is stale; unavailable ancestry stays
unknown. Pruned logs retain their receipt. Receipts supplement CI evidence;
only GitHub checks and the existing merge grant authorize a merge.';

COMMIT;
