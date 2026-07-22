---
name: sprint
description: Participant loop for a declared multi-shell sprint — dev, reviewer, or conformance slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, close your unit with a structured unit-report result row, report every transition as a result row. Conformance slot: judge the spec against main pre-freeze, four-way verdicts. No scheduled polling — the managed session dispatcher wakes the planner, and the planner explicitly boots workers. Local long work (suites/benches) rides ./sc job, never a harness background task. Load when a sprint task message names you a participant.
category: craft
common: false
---

# sprint — your slot in a coordinated multi-shell push

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

**You never poll on a schedule.** The planner sends durable `task` rows and
explicitly boots workers (often headless with `./sc run`). The GitHub watcher
daemon turns your PR transitions into `pr_event` rows; the session dispatcher
delivers those unread events only to the planner's managed binding. You report
every state change as a `result` row. A message does not boot an ordinary
worker by itself; a live inbox check or an explicit planner boot starts your
next turn. Memory, archives, and messages persist across those turns.

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

## Local long work — suites, benches, builds

A harness background task is session-scoped: in a headless boot it dies
with the session, silently — "the harness will wake me" is false there.
Never park a suite, bench, build, or watcher on one. Long local work
goes through `./sc job`, two patterns:

- **Detached completion:** `./sc job start [--label <x>]
  [--timeout <s>] -- <cmd>` — the job survives your session; completion
  lands in YOUR inbox as a `result` row. That row is durable, but it does
  not autonomously boot an ordinary worker. Use this only when no immediate
  continuation depends on the result; otherwise use wait-slice or arrange an
  explicit planner re-boot rather than ending the turn on an assumed wake.
- **Wait-slice (the result decides THIS turn's next step):**
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

**2. Prepare.** Run the `git` skill's sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn't merged -> branch
stacked on the upstream shell's branch + accept the retarget duty in
step 4. Buildable against current `main` -> branch from `main`; stack only
for real code dependencies.

**3. Build.** Your dependency not yet merged? Build and commit locally,
but do NOT open your PR out of turn — the planner's next `task` row (sent
after the managed planner receives your upstream's merge event) is your turn
signal; a booted-headless session simply ends here and the planner explicitly
re-boots you when the chain reaches you. Don't schedule a watcher; don't poll.
Upstream visibly stalls from where you sit -> `result` row to the planner; don't sit
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
session ends first, the daemon's red/green event reaches the managed planner.
The planner sends any needed `task` row and explicitly re-boots you. Never a
cron, never a scheduled model wake.

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
sc mem message send <planner-shortname> "$(cat <<'EOF'
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <what the unit does now, 1-2 lines — the claim, not the diff>
judgements: <ambiguity calls incl. final state (ratified/overruled); 'none'>
issues: <CI reds (real vs anomalous), fix loops, stalls, review friction; 'none'>
deviations: <known departures from the spec's reading + why; 'none'>
follow-ups: <Lows deferred, TODOs left, cleanup owed; 'none'>
EOF
)" --kind result
```

One report per unit, at merge, mandatory — written NOW, while the unit's
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

Gate the units the doc's `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don't-trust, review against the unit's
scope); this overlay changes only pace and severity:

1. **Wake = an explicit review turn.** A dev's `ready for review` message is
   durable; a live inbox check or the planner booting you headless starts the
   next-in-queue work. A waiting review stalls the chain exactly like
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

## Conformance slot

The sprint's final gate: after every unit is merged and `main` is green,
*before* the freeze, the planner boots you to answer the one question no
unit reviewer is positioned to answer — **does what shipped on `main`
actually match the spec?** Unit reviewers gated diffs against unit
scopes; you read the integrated whole. Cross-unit seams — one unit's
interface drifting from what another assumed, a requirement that fell
between two units — are yours to catch.

1. **Wake = the planner's kickoff.** Its `task` row carries exactly: the
   spec doc id, the sprint doc id, the merge SHA of `main`, your section
   scope (if the pass is sharded), and the planner's list of **ratified
   judgement calls**. That list is your only narrative input — it is what
   lets you tell an intentional deviation from a silent one. Everything
   else is artifact: judge the spec against the code on `main` at that
   SHA — never the diffs, never the message trail, never the devs'
   reasoning.
2. **Verdicts.** Every spec requirement in scope gets exactly one:
   - `as-specced` — code matches the spec's reading;
   - `deviated-intentionally` — matches a ratified judgement call;
   - `deviated-silently` — departs from spec, nobody declared it;
   - `unimplemented` — spec requires it, nothing on `main` does it.
   The last two are findings: attach spec section, code location, and
   Major/Medium/Low — the sprint's severity bar, same meanings.
3. **Output.** Write a `documents` row — `CONFORMANCE: <sprint title>`,
   kind `doc` (`sc mem doc add`) — holding the verdict table + findings,
   then send the planner ONE line pointing at it:
   `sprint <doc-id>: conformance done — doc <id>, N findings (x Major, y
   Medium, z Low)` (--kind result). Detail in the doc, wake-up in the
   message.
4. **No authority.** You file verdicts; you rule on nothing. Fix units,
   deferrals, and severity disputes are the planner's; anything that
   changes what the sprint *means* is the FnB's. Same escalation ladder
   as every other slot.
5. **Stand down** when the planner confirms receipt (a re-run on fix
   units arrives as a fresh scoped `task` row).

## Stance

- No scheduled model polling, ever: only a live inbox check or explicit
  `./sc run` starts a worker turn; unread events wake the managed planner
  binding. The sprint doc tells every turn what it means.
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
  conformance slot exists because only the first is otherwise checked.
