---
name: sprint
description: Participant loop for a declared multi-shell sprint — dev or reviewer slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, report every transition as a result row. No scheduled polling — the planner and the watcher daemon wake you. Load when a sprint task message names you a participant.
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

You are in a sprint ONLY when a planner `task` message names you a
participant and points at a sprint doc. No kickoff -> this skill is inert.

**You never poll on a schedule.** The sprint is event-driven: the planner
wakes you with `task` rows (often by booting you headless — `./sc run` —
with the task as your prompt), the GitHub watcher daemon turns your PR's
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

The planner is the doc's only writer. NEVER `sc mem doc edit` the sprint
doc — report state changes to the planner as `result` rows; the planner
updates the board.

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

## Ambiguity calls

A spec ambiguity mid-unit — more than one defensible reading and the
spec doesn't pick — is yours to call inside a sprint: pick the reading
that keeps your unit shippable and keep building; don't stall the chain
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

**2. Prepare.** Run the `git` skill's sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn't merged -> branch
stacked on the upstream shell's branch + accept the retarget duty in
step 4. Buildable against current `main` -> branch from `main`; stack only
for real code dependencies.

**3. Build.** Your dependency not yet merged? Build and commit locally,
but do NOT open your PR out of turn — the planner's next `task` row (sent
on your upstream's merge event) is your turn signal; a booted-headless
session simply ends here and the planner re-boots you when the chain
reaches you. Don't schedule a watcher; don't poll. Upstream visibly
stalls from where you sit -> `result` row to the planner; don't sit
silent behind a stuck link.

**4. Take your turn** the moment your dependency is on `main` (your
kickoff said "start now", or a planner `task` row says so):

- stacked on the upstream branch -> retarget first: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise note your base is gone — same
  discipline as the `git` skill's stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR — then, in the SAME step:

```
./sc watch pr <owner/repo> <pr-number> --shell <planner-shortname>
sc mem message send <planner> "sprint <doc-id>: unit <seq> pr-open — PR #<n>" --kind result
```

The watch is what makes the loop event-driven: the daemon now turns every
CI conclusion, review, and merge on your PR into a `pr_event` row in the
planner's inbox. Registration is explicit and happens at PR open — a PR
without a watch is invisible to the sprint.

**5. Babysit CI while live.** `gh pr checks <your-pr> --watch` blocks in
your session at zero scheduled cost — use it while you're booted; if your
session ends first, the daemon's red/green event reaches the planner and
a `task` row re-boots you. Never a cron, never a scheduled wake.

Triage before fixing: is the failure in something your diff touches? Does
`main` show the same failure? Does the log say timeout / runner died /
network / flaky test you never touched? Anomalous -> `gh run rerun
<run-id> --failed`, don't patch healthy code. Anomalous red survives two
reruns -> `result` row to the planner (flaky suite, broken `main`, infra)
and hold — planner's to fix as a unit, not yours to absorb. When a fix
needs a fix, suspect the diagnosis.

Real red -> read the failure, fix, push, watch again — your loop to run,
not the planner's to chase. Three honest fix attempts without green ->
`result` row with what's failing and what you've tried. Reruns of flakes
count neither as attempts nor as green: merge authority requires actual
green checks — "it's just a flake" is never a merge.

**6. Pass sprint review.** CI green -> message your unit's reviewer
`sprint <doc-id>: unit <seq> ready for review — PR #<n>, checks green`
(+ your open ambiguity calls) and tell the planner `in-review`
(--kind result). Major/Medium findings block: fix, push, re-request; keep
CI green across fix pushes. Low findings = notes for the sprint report,
not gates. Disagree with a severity call -> planner rules; don't litigate
in the thread while the chain waits.

**7. Merge on green + clean, then hand off.** All checks green + reviewer
declared review-clean + boundary above satisfied:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
sc mem message send <planner-shortname> "sprint <doc-id>: unit <seq> merged (PR #<n>)." --kind result
```

(The daemon also emits the merge to the planner and retires your watch —
the `pr_event` is the wake-up, your `result` row is the report; send it
anyway: worker self-reports and daemon ground truth cross-check each
other.) No downstream (last link) -> the planner message is the handoff.
Then clean up local per the `git` skill (re-pin base, delete the branch).

**8. Stand down.** Planner close-out message / frozen or `CLOSED` sprint
doc = sprint over: merge authority gone, default gates resume. Drop the
SPRINT line from `current_state` and confirm in a final `result` row.
Your PR watches retired themselves at merge/close — nothing to tear down.

## Reviewer slot

Gate the units the doc's `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don't-trust, review against the unit's
scope); this overlay changes only pace and severity:

1. **Wake = a review request.** A dev's `ready for review` message — or a
   planner `task` row booting you headless with the request as prompt —
   is next-in-queue work; a waiting review stalls the chain exactly like
   red CI. Keep a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. No trackers, no scheduled polls.
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
   review-clean`), --kind result. The FnB gate is unchanged everywhere
   else and returns the moment the doc freezes.
4. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev's merge; never leave it implied.
5. **Stand down** on close-out: drop your SPRINT line, confirm to the
   planner in a final `result` row.

## Stance

- No scheduled polling, ever: `task` rows and headless boots wake you;
  `pr_event` rows wake the planner; the sprint doc tells you what a wake
  means.
- Register the watch in the same step that opens the PR — an unwatched PR
  is a silent link, and silent links revert the sprint to polling.
- Report state transitions (`building → pr-open → in-review → fixing →
  merged`) as `result` rows, one line each — not progress prose.
- Merge-on-green+clean and direct review handoffs are scoped authority
  inside a declared sprint, never precedent outside one.
