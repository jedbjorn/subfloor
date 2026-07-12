-- 0057 — reseed: sprint + sprint_orchestration — ambiguity calls
--
-- Devs hitting a spec ambiguity mid-unit call it themselves (sprint-scoped
-- judgment, same shape as the merge authority: how the unit meets its spec,
-- never what the unit is) and report each call to the planner in one line.
-- The planner rules on receipt (overrule while un-merged; silence ratifies),
-- logs every call, lists them all in the sprint report, and drops a copy of
-- the report in the fork's shared/ dir at close-out. Source assets updated
-- in the same commit; this trailing forward reseed (UPSERT by name;
-- skill_id + grants preserved) carries it to installed forks and fresh
-- builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint',
  'Participant loop for a declared multi-shell sprint — dev or reviewer slot. Read your slot from the sprint doc, stand up your one sprint tracker (wakes you on every green/red/merge), take your turn when your dependency lands (rebase → PR), babysit CI, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, hand off, kill the tracker at close-out. Load when a sprint kickoff message names you a participant.',
  'craft',
  NULL,
  0,
  '# sprint — your slot in a coordinated multi-shell push

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
sc mem get doc --doc <N>            # full body
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

The planner is the doc''s only writer. NEVER `sc mem doc edit` the sprint
doc — report state changes to the planner by message; the planner updates
the board.

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

Every call is reported, never silent: with your next transition message
to the planner, one line per call —
`ambiguity: <what the spec left open> → chose <reading> — <why>`. No
planner overrule -> your call stands; an overrule arrives by message and
is worked like a review finding. Repeat your open calls in the review
request (step 7) so the reviewer gates against your reading, not its
own guess.

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
polls the sprint''s PRs and notifies you on every transition — any sprint
PR going green, going red, or merging. It is what wakes a cold shell.
Rules:

- Derive the watch list live each poll — `gh pr list` filtered to the
  branches in the sprint doc — so mid-sprint PRs join automatically.
  Tempted to add a second trigger or edit the first mid-sprint -> the
  query was wrong; fix it, don''t multiply it.
- Waking is not knowing. On wake, before acting: read the sprint doc, find
  your row, check your inbox. The doc says whether it''s your turn; the
  tracker only says "look".
- The tracker dies at step 9. A sprint tracker firing in a later session =
  a defect you created.
- No scheduler in your harness -> poll in-session between work units
  (`gh pr view <upstream-pr> --json state,mergedAt` · `git fetch origin
  main`) and say so to the planner at kickoff.

**3. Prepare.** Run the `git` skill''s sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn''t merged -> branch
stacked on the upstream shell''s branch + accept the retarget duty in
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
message the planner; don''t sit silent behind a stuck link.

**5. Take your turn** the moment your dependency merges:

- stacked on the upstream branch -> retarget first: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise note your base is gone — same
  discipline as the `git` skill''s stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR, message the planner `pr-open`.

**6. Babysit CI.** `gh pr checks <your-pr> --watch` while live; the
tracker covers cold gaps — a red on your PR is your wake-up, not news from
the planner.

Triage before fixing: is the failure in something your diff touches? Does
`main` show the same failure? Does the log say timeout / runner died /
network / flaky test you never touched? Anomalous -> `gh run rerun
<run-id> --failed`, don''t patch healthy code. Anomalous red survives two
reruns -> message the planner (flaky suite, broken `main`, infra) and
hold — planner''s to fix as a unit, not yours to absorb. When a fix needs a
fix, suspect the diagnosis.

Real red -> read the failure, fix, push, watch again — your loop to run,
not the planner''s to chase. Three honest fix attempts without green ->
message the planner with what''s failing and what you''ve tried. Reruns of
flakes count neither as attempts nor as green: merge authority requires
actual green checks — "it''s just a flake" is never a merge.

**7. Pass sprint review.** CI green -> message your unit''s reviewer
`sprint <doc-id>: unit <seq> ready for review — PR #<n>, checks green` +
tell the planner `in-review`. Major/Medium findings block: fix, push,
re-request; keep CI green across fix pushes. Low findings = notes for the
sprint report, not gates. Disagree with a severity call -> planner rules;
don''t litigate in the thread while the chain waits.

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

Gate the units the doc''s `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don''t-trust, review against the unit''s
scope); this overlay changes only pace and severity:

1. **Same tracker, same ledger.** Stand up your one sprint tracker at
   kickoff + a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. Wake signal = a `ready for review` message or an
   assigned unit''s PR going green. A review request is next-in-queue work;
   a waiting review stalls the chain exactly like red CI.
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
   review-clean`). The FnB gate is unchanged everywhere else and returns
   the moment the doc freezes.
4. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev''s merge; never leave it implied.
5. **Stand down** on close-out: kill your tracker, drop your SPRINT line,
   confirm to the planner.

## Stance

- One tracker per shell for the whole sprint; notifications wake you, the
  sprint doc tells you what it means.
- Report state transitions (`building → pr-open → in-review → fixing →
  merged`), one line each — not progress prose.
- Merge-on-green+clean and direct review handoffs are scoped authority
  inside a declared sprint, never precedent outside one.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, declare the sprint doc, kick everyone off (every shell stands up one sprint tracker), monitor the board, unblock stalls, close out — freeze the doc (revoking all scoped authority), tear down every tracker, and write the sprint report. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, kick off every participant, watch the whole board, unblock stalls,
close out with a report. The participant loop (build → PR → CI → sprint
review → merge on green+clean → hand off, plus the reviewer slot) = the
`sprint` skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants'' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Units that don''t touch each other run in parallel; keep
chains short and the graph wide where the code allows.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don''t let one reviewer become the whole sprint''s
bottleneck). Write the board as a `documents` row:

```
sc mem doc add "SPRINT: <title>" --kind doc --body-file <draft.md>
```

Body contract (the `sprint` skill quotes the same one — keep it exact):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

Note the returned `document_id` — every kickoff and report references
it — and embed `SPRINT doc=<id> governing` in your own `current_state`;
drop it at close-out.

You are the doc''s only writer: devs report transitions by message; fold
them into the board with `sc mem doc edit <id> --body-file`.

## Step 2: Kick off

Message every participant its slot — the doc id, the instruction to load
the `sprint` skill, and the slot:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>."

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green."
```

First-in-chain starts immediately; everyone else starts watching. This
message activates each dev''s scoped merge authority and each reviewer''s
direct-handoff authority for its assigned units.

Then stand up your own sprint tracker — the same pattern the `sprint`
skill gives the devs: one recurring watcher per shell in the harness
scheduler, polling the sprint''s PRs (watch list derived live from the
doc''s branches, so mid-sprint PRs join without edits), notifying on every
green, red, and merge. Exactly one per shell for the sprint''s duration —
a tracker misses something -> fix its query, don''t add a second. All
trackers die at close-out (Step 5).

## Step 3: Monitor the board

Your tracker wakes you on every transition; between wakes, poll — don''t
wait for reports:

```
gh pr list --state all             # who''s open, merged, closed
gh pr checks <n>                   # the current bottleneck''s CI state
sc mem message check               # dev transition reports
git fetch origin main              # what actually landed
```

At any moment, be able to answer: which link is the bottleneck? Fold every
state change into the doc as it happens — the board is what the FnB and
any rebooted shell reads to re-orient mid-sprint. On a tracker wake: read
the event, update the board, nudge whoever it unblocks if their own
tracker hasn''t already.

Messages are your steering wheel: the `sprint` skill has every dev check
its inbox at each step start and every tracker wake, so your message is
read before that dev''s next move. Steer with messages — holds,
re-sequencing, nudges, rulings on reported reds. The board records state;
messages change behavior; on conflict your latest message wins -> then
update the board to match.

Dev ambiguity reports (`ambiguity: … → chose …`) get a ruling on
receipt: overrule by message while the unit is still un-merged, or stay
silent and the call stands. Either way log the call + outcome the
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
  message when the dev may proceed. Don''t count phantom reds against the
  dev''s fix attempts — and don''t let anyone merge over one; green means
  green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain''s tail.
- **Merge broke `main`**: message all devs to hold merges, insert a fix
  unit at the front of the chain, resume when green.
- **Review stall** (unit sitting `in-review` while its reviewer works
  something else): nudge the reviewer; still stuck -> reassign the unit to
  another reviewer. Severity dispute (dev says Low, reviewer says Medium)
  -> rule by message immediately — a chain waiting on a classification
  argument is pure loss. Dispute about what the unit *should do* -> FnB.
- **Link gone quiet** (no transition report, no tracker-visible movement,
  no reply): nudge by message — a live shell reads it at its next step
  boundary, a dead one never will. Second nudge met with silence ->
  escalate to the FnB: only the FnB boots shells; ask for a boot or a
  reassignment. The bottleneck question in Step 3 is what surfaces a dead
  link.
- **Re-sequencing**: edit the board + message *every* affected dev its new
  slot — a dev acting on a stale slot is worse than a paused one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can''t unblock yourself.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant''s scoped authority ends with it.
2. Message every participant: sprint closed, default merge gates resume,
   kill your sprint tracker now — reply when it''s dead.
3. Tear down the watchers: kill your own tracker, then collect every dev''s
   confirmation. The sprint is not closed while any tracker lives — a
   leaked watcher fires on unrelated PRs in later sessions. Chase silence
   like you''d chase red CI.
4. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Cover: units shipped (PRs, planned vs. actual order); review outcomes
   (Major/Medium found and fixed per unit; reviewers'' Low notes — they
   land here, as the post-sprint cleanup list); every ambiguity call —
   what the spec left open, what the dev chose, ratified or overruled
   (this list is where spec debt surfaces); stalls hit and how each
   was unblocked; anything cut or re-scoped and why; what the sprint
   surfaced about the process itself.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
5. Settle the bookkeeping — close the sprint''s flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Monitor > interrogate: `gh` and `git fetch` cost no dev a context
  switch; messages are for what the tools can''t see.
- Escalate judgment, absorb mechanics: re-sequencing is yours; changing
  what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
