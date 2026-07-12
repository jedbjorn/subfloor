---
name: sprint_orchestration
description: Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, declare the sprint doc, kick everyone off (every shell stands up one sprint tracker), monitor the board, unblock stalls, close out — freeze the doc (revoking all scoped authority), tear down every tracker, and write the sprint report. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.
category: craft
common: false
---

# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, kick off every participant, watch the whole board, unblock stalls,
close out with a report. The participant loop (build → PR → CI → sprint
review → merge on green+clean → hand off, plus the reviewer slot) = the
`sprint` skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Units that don't touch each other run in parallel; keep
chains short and the graph wide where the code allows.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don't let one reviewer become the whole sprint's
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

You are the doc's only writer: devs report transitions by message; fold
them into the board with `sc mem doc edit <id> --body-file`.

## Step 2: Kick off

Message every participant its slot — the doc id, the instruction to load
the `sprint` skill, and the slot:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell'> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>."

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green."
```

First-in-chain starts immediately; everyone else starts watching. This
message activates each dev's scoped merge authority and each reviewer's
direct-handoff authority for its assigned units.

Then stand up your own sprint tracker — the same pattern the `sprint`
skill gives the devs: one recurring watcher per shell in the harness
scheduler, polling the sprint's PRs (watch list derived live from the
doc's branches, so mid-sprint PRs join without edits), notifying on every
green, red, and merge. Exactly one per shell for the sprint's duration —
a tracker misses something -> fix its query, don't add a second. All
trackers die at close-out (Step 5).

## Step 3: Monitor the board

Your tracker wakes you on every transition; between wakes, poll — don't
wait for reports:

```
gh pr list --state all             # who's open, merged, closed
gh pr checks <n>                   # the current bottleneck's CI state
sc mem message check               # dev transition reports
git fetch origin main              # what actually landed
```

At any moment, be able to answer: which link is the bottleneck? Fold every
state change into the doc as it happens — the board is what the FnB and
any rebooted shell reads to re-orient mid-sprint. On a tracker wake: read
the event, update the board, nudge whoever it unblocks if their own
tracker hasn't already.

Messages are your steering wheel: the `sprint` skill has every dev check
its inbox at each step start and every tracker wake, so your message is
read before that dev's next move. Steer with messages — holds,
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
  dev's job was to rerun and report, not patch healthy code): fix the
  cause as its own unit, or hold the chain while infra recovers; rule by
  message when the dev may proceed. Don't count phantom reds against the
  dev's fix attempts — and don't let anyone merge over one; green means
  green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain's tail.
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
  stall you can't unblock yourself.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant's scoped authority ends with it.
2. Message every participant: sprint closed, default merge gates resume,
   kill your sprint tracker now — reply when it's dead.
3. Tear down the watchers: kill your own tracker, then collect every dev's
   confirmation. The sprint is not closed while any tracker lives — a
   leaked watcher fires on unrelated PRs in later sessions. Chase silence
   like you'd chase red CI.
4. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Cover: units shipped (PRs, planned vs. actual order); review outcomes
   (Major/Medium found and fixed per unit; reviewers' Low notes — they
   land here, as the post-sprint cleanup list); every ambiguity call —
   what the spec left open, what the dev chose, ratified or overruled
   (this list is where spec debt surfaces); stalls hit and how each
   was unblocked; anything cut or re-scoped and why; what the sprint
   surfaced about the process itself.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
5. Settle the bookkeeping — close the sprint's flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Monitor > interrogate: `gh` and `git fetch` cost no dev a context
  switch; messages are for what the tools can't see.
- Escalate judgment, absorb mechanics: re-sequencing is yours; changing
  what the sprint *means* is the FnB's.
