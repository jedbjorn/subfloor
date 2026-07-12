---
name: sprint
description: Participant loop for a declared multi-shell sprint — dev or reviewer slot. Read your slot from the sprint doc, stand up your one sprint tracker (wakes you on every green/red/merge), take your turn when your dependency lands (rebase → PR), babysit CI, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, hand off, kill the tracker at close-out. Load when a sprint kickoff message names you a participant.
category: craft
common: false
---

# sprint — your slot in a coordinated multi-shell push

A sprint = a declared, planner-governed push where shells build dependent
units (B on A, C on B); loop = planner → devs → reviewers → devs → planner,
the shells running the handoffs themselves. This skill is the participant
side: a **dev slot** ("The loop") or a **reviewer slot** ("Reviewer slot").
Planner side (declare / monitor / close / report) = `sprint_orchestration`.
`git`, `review`, `messaging` remain the base disciplines underneath.

You are in a sprint ONLY when a planner kickoff message names you a
participant and points at a sprint doc. No kickoff -> this skill is inert.

## The sprint doc — one board, planner-owned

Declaration = a `documents` row (kind `doc`, title `SPRINT: …`). Read:

```
sc mem get docs                     # find it in the index
sc mem get doc --id <N>             # full body
```

Body contract:

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

The planner is the doc's only writer. NEVER `sc mem doc edit` the sprint
doc — report state changes to the planner by message; the planner updates
the board.

## Scoped merge authority

The `git` skill's rule stands: merging is the FnB's gate. A sprint grants
one narrow exception — merge only when ALL four hold:

- the PR is for **your assigned unit** in this sprint,
- **all checks are green**,
- your unit's reviewer declared **review-clean** (every Major/Medium
  finding fixed),
- the sprint doc says `status: ACTIVE` and is not frozen.

Everything outside those four — other PRs, other repos, a red or pending
check, an unreviewed diff, a closed or frozen sprint — is the default FnB
gate, unchanged. The authority dies when the sprint closes; in doubt ->
read the doc; `CLOSED` or frozen -> no merge authority.

## The loop (dev slot)

At the start of every step and on every tracker wake: `sc mem message
check`. A planner message (hold, re-sequence, scope change) is
authoritative over the board — never start a step on a stale slot.

**1. Know your slot.** Read the sprint doc, find your row; note upstream
(unit + shell), your reviewer, and downstream (shell — your handoff
target). No upstream -> start immediately. Embed one line in
`current_state`, keep it current as your status walks, drop it at
stand-down:

```
SPRINT doc=<id> unit=<seq> upstream=<seq|none> downstream=<shortname|none> status=<...>
```

**2. Stand up your sprint tracker — exactly one, spanning the sprint.** A
recurring watcher in your harness scheduler (cron / scheduled wakeup) that
polls the sprint's PRs and notifies you on every transition — any sprint
PR going green, going red, or merging. It is what wakes a cold shell.
Rules:

- Derive the watch list live each poll — `gh pr list` filtered to the
  branches in the sprint doc — so mid-sprint PRs join automatically.
  Tempted to add a second trigger or edit the first mid-sprint -> the
  query was wrong; fix it, don't multiply it.
- Waking is not knowing. On wake, before acting: read the sprint doc, find
  your row, check your inbox. The doc says whether it's your turn; the
  tracker only says "look".
- The tracker dies at step 9. A sprint tracker firing in a later session =
  a defect you created.
- No scheduler in your harness -> poll in-session between work units
  (`gh pr view <upstream-pr> --json state,mergedAt` · `git fetch origin
  main`) and say so to the planner at kickoff.

**3. Prepare.** Run the `git` skill's sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn't merged -> branch
stacked on the upstream shell's branch + accept the retarget duty in
step 5. Buildable against current `main` -> branch from `main`; stack only
for real code dependencies.

**4. Watch for your dependency to land.** Signals, in trust order:

- **Tracker** — the merge notification for your upstream unit = your turn
  signal, cold session or live.
- **Inbox** — the upstream dev messages you on merge (its step 8).
  `sc mem message check` between work units and on every wake.
- **Manual poll** — backup while live; never a reason to skip the tracker.

While waiting: build and commit locally, but do NOT open your PR out of
turn. Upstream visibly stalls (red CI for hours, scope ballooning) ->
message the planner; don't sit silent behind a stuck link.

**5. Take your turn** the moment your dependency merges:

- stacked on the upstream branch -> retarget first: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise note your base is gone — same
  discipline as the `git` skill's stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR, message the planner `pr-open`.

**6. Babysit CI.** `gh pr checks <your-pr> --watch` while live; the
tracker covers cold gaps — a red on your PR is your wake-up, not news from
the planner.

Triage before fixing: is the failure in something your diff touches? Does
`main` show the same failure? Does the log say timeout / runner died /
network / flaky test you never touched? Anomalous -> `gh run rerun
<run-id> --failed`, don't patch healthy code. Anomalous red survives two
reruns -> message the planner (flaky suite, broken `main`, infra) and
hold — planner's to fix as a unit, not yours to absorb. When a fix needs a
fix, suspect the diagnosis.

Real red -> read the failure, fix, push, watch again — your loop to run,
not the planner's to chase. Three honest fix attempts without green ->
message the planner with what's failing and what you've tried. Reruns of
flakes count neither as attempts nor as green: merge authority requires
actual green checks — "it's just a flake" is never a merge.

**7. Pass sprint review.** CI green -> message your unit's reviewer
`sprint <doc-id>: unit <seq> ready for review — PR #<n>, checks green` +
tell the planner `in-review`. Major/Medium findings block: fix, push,
re-request; keep CI green across fix pushes. Low findings = notes for the
sprint report, not gates. Disagree with a severity call -> planner rules;
don't litigate in the thread while the chain waits.

**8. Merge on green + clean, then hand off.** All checks green + reviewer
declared review-clean + boundary above satisfied:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
sc mem message send <planner-shortname> "sprint <doc-id>: unit <seq> merged (PR #<n>)."
```

No downstream (last link) -> the planner message is the handoff. Then
clean up local per the `git` skill (re-pin base, delete the branch).

**9. Stand down.** Planner close-out message / frozen or `CLOSED` sprint
doc = sprint over: merge authority gone, default gates resume. Before
anything else: kill your tracker + drop the SPRINT line from
`current_state`, and confirm both in your reply to the planner.

## Reviewer slot

Gate the units the doc's `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don't-trust, review against the unit's
scope); this overlay changes only pace and severity:

1. **Same tracker, same ledger.** Stand up your one sprint tracker at
   kickoff + a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. Wake signal = a `ready for review` message or an
   assigned unit's PR going green. A review request is next-in-queue work;
   a waiting review stalls the chain exactly like red CI.
2. **Major/Medium block; Low informs.** Wrong-behavior / data-loss /
   security / spec-violation (Major) or will-bite-soon (Medium) -> the dev
   fixes now; re-review on the fix push. Style / naming / nice-to-have
   refactors (Low) -> one summary note to the planner for the sprint
   report; Low never blocks merge and you don't re-litigate it.
3. **Handoffs go direct** — scoped relaxation, same shape as the merge
   authority. The base `review` skill gates handoffs behind the FnB;
   inside an ACTIVE sprint, for your assigned units only: message the
   author dev your findings directly + copy the planner one line
   (`unit <seq>: N major, M medium — with <dev>` or `unit <seq>:
   review-clean`). The FnB gate is unchanged everywhere else and returns
   the moment the doc freezes.
4. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev's merge; never leave it implied.
5. **Stand down** on close-out: kill your tracker, drop your SPRINT line,
   confirm to the planner.

## Stance

- One tracker per shell for the whole sprint; notifications wake you, the
  sprint doc tells you what it means.
- Report state transitions (`building → pr-open → in-review → fixing →
  merged`), one line each — not progress prose.
- Merge-on-green+clean and direct review handoffs are scoped authority
  inside a declared sprint, never precedent outside one.
