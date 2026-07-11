---
name: sprint_orchestration
description: Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, declare the sprint doc, kick off the devs (every shell stands up one sprint tracker), monitor the board, unblock stalls, close out — freezing the doc revokes the devs' scoped merge authority and every tracker is torn down. Load when the FnB directs a coordinated multi-dev push. Companion to the dev-side `sprint` skill.
category: craft
common: false
---

# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run. You decompose the
push into units, sequence who builds on whom, kick off every participant,
watch the whole board, unblock stalls, and close it out. The dev-side loop
(watch dependency → PR → babysit CI → merge on green → hand off) is the
`sprint` skill — each participant runs it; you run this.

The two skills meet at one artifact: the **sprint doc**. Your declaration
turns the devs' scoped merge authority on; your close-out turns it off.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map the
dependency order — and be stingy with it: **a dependency edge is a real code
dependency, not a preference.** Units that don't touch each other run in
parallel; a chain is only as fast as its slowest link, so keep chains short
and the graph wide where the code allows.

Assign each unit to a shell, then write the board as a `documents` row:

```
sc mem doc add "SPRINT: <title>" --kind doc --body-file <draft.md>
```

Body contract (the `sprint` skill quotes the same one — keep it exact):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>

| seq | unit | shell | depends on | branch | pr | status |
```

Unit `status` walks: `waiting → building → pr-open → ci-red → merged`.
Note the returned `document_id` — every kickoff and report references it.

**You are the doc's only writer.** Devs report transitions by message; you
fold them into the board with `sc mem doc edit <id> --body-file`. One writer,
one board, no drift.

## Step 2: Kick off

Message every participant its slot — unit, what it depends on, who depends
on it, the doc id, and the instruction to load the `sprint` skill:

```
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell'> depends on you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>."
```

First-in-chain starts immediately; everyone else starts watching. From this
message on, each dev holds the `sprint` skill's scoped merge authority for
its own unit.

Then **stand up your own sprint tracker** — the same pattern the `sprint`
skill gives the devs, and the answer to "how does a cold shell know it's
time": one recurring watcher per shell in the harness scheduler, polling the
sprint's PRs (watch list derived live from the doc's branches, so mid-sprint
PRs join without edits) and notifying on **every green, red, and merge**.
Every participant runs exactly one for the sprint's duration; nobody
hand-builds ad-hoc triggers mid-sprint — if a tracker misses something, fix
its query, don't add a second. All trackers die at close-out (Step 5).

## Step 3: Monitor the board

Your tracker wakes you on every transition; between wakes, poll — don't wait
for reports:

```
gh pr list --state all             # who's open, merged, closed
gh pr checks <n>                   # the current bottleneck's CI state
sc mem message check               # dev transition reports
git fetch origin main              # what actually landed
```

At any moment you should be able to answer: **which link is the bottleneck?**
Fold every state change into the doc as it happens — the board is what the
FnB and any rebooted shell reads to re-orient mid-sprint. On a tracker wake,
that's the whole job: read the event, update the board, nudge whoever it
unblocks if their own tracker hasn't already.

## Step 4: Unblock

Stalls you'll meet, and the moves:

- **A dev wedged on red CI** (it reports after three failed fix attempts, per
  the `sprint` skill): decide — pair another shell onto it, re-scope the
  unit, or pull the failing part into a follow-up unit so the chain moves.
- **A unit growing past its scope**: split it; the piece downstream actually
  needs ships first, the rest becomes a new unit at the chain's tail.
- **A merge broke `main`**: message all devs to hold merges, insert a fix
  unit at the front of the chain, resume when green.
- **Re-sequencing**: when the plan meets reality, edit the board and message
  *every* affected dev with its new slot — a dev acting on a stale slot is
  worse than a paused one.
- **Judgment calls** — scope vs. deadline, cut a unit, change an interface
  another team reads: **escalate to the FnB immediately.** Sitting on a
  judgment call is the one stall you can't unblock yourself.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze the board:
   `sc mem doc freeze <doc-id>`. **Freezing is the revocation** — a frozen or
   `CLOSED` sprint doc is the signal that every participant's scoped merge
   authority is gone (the `sprint` skill checks exactly this).
2. Message every participant that the sprint is closed, default merge gates
   resume, and **kill your sprint tracker now — reply when it's dead.**
3. **Tear down the watchers.** Kill your own tracker, then collect the devs'
   confirmations. The sprint is not closed while any tracker lives — a
   watcher leaking into later sessions fires on unrelated PRs and erodes
   trust in the next sprint's signals. Chase silence like you'd chase red CI.
4. Report the outcome to the FnB: units shipped (PRs), anything cut or
   re-scoped, what it surfaced.
5. Settle the bookkeeping — close the sprint's flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- **Enforcement is advisory in v1.** Merge order and authority live in skill
  text and the board, not in a pre-commit check. That makes the board's
  accuracy *your* discipline — an out-of-date board is a false authority
  grant.
- **Monitor > interrogate.** `gh` and `git fetch` tell you the truth without
  costing a dev a context switch; messages are for what the tools can't see.
- **Escalate judgment, absorb mechanics.** Re-sequencing is yours; changing
  what the sprint *means* is the FnB's.
