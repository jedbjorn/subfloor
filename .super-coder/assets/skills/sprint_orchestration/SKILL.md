---
name: sprint_orchestration
description: Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, declare the sprint doc, kick everyone off (every shell stands up one sprint tracker), monitor the board, unblock stalls, close out — freeze the doc (revoking all scoped authority), tear down every tracker, and write the sprint report. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.
category: craft
common: false
---

# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run. The loop is
planner → devs → reviewers → devs → planner: you decompose the push into
units, sequence who builds on whom, assign a reviewer to every unit, kick
off every participant, watch the whole board, unblock stalls, and close it
out with a report. The participant loop (build → PR → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) is the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact: the **sprint doc**. Your declaration turns
the participants' scoped authority on (dev merge-on-green+clean, reviewer
direct handoffs); your close-out turns it off.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map the
dependency order — and be stingy with it: **a dependency edge is a real code
dependency, not a preference.** Units that don't touch each other run in
parallel; a chain is only as fast as its slowest link, so keep chains short
and the graph wide where the code allows.

Assign each unit a dev shell **and a reviewer shell** (one reviewer can gate
several units — just don't let one reviewer become the whole sprint's
bottleneck), then write the board as a `documents` row:

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

Unit `status` walks: `waiting → building → pr-open → in-review → fixing →
merged` (`fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on). Note the returned `document_id` —
every kickoff and report references it — and embed `SPRINT doc=<id>
governing` in your own `current_state`; drop it at close-out.

**You are the doc's only writer.** Devs report transitions by message; you
fold them into the board with `sc mem doc edit <id> --body-file`. One writer,
one board, no drift.

## Step 2: Kick off

Message every participant its slot — the doc id, the instruction to load the
`sprint` skill, and what its slot is:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell'> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>."

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green."
```

First-in-chain starts immediately; everyone else starts watching. From this
message on, each dev holds the scoped merge authority and each reviewer the
direct-handoff authority for its assigned units.

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

**Messages are your steering wheel.** The `sprint` skill has every dev check
its inbox at the start of each step and on every tracker wake — so a message
from you is guaranteed to be read before that dev's next move. Steer with it:
holds, re-sequencing, nudges, rulings on reported reds. The board records
state; messages change behavior. When they'd conflict, your latest message
wins — then update the board to match.

## Step 4: Unblock

Stalls you'll meet, and the moves:

- **A dev wedged on red CI** (it reports after three failed fix attempts, per
  the `sprint` skill): decide — pair another shell onto it, re-scope the
  unit, or pull the failing part into a follow-up unit so the chain moves.
- **An anomalous red** — the dev reports a failure that isn't its bug (flaky
  test, runner death, `main` red underneath): the dev's job was to rerun and
  report, not to patch healthy code. Yours is to fix the cause as its own
  unit (or hold the chain while infra recovers) and rule by message when the
  dev may proceed. Don't count phantom reds against a dev's fix attempts —
  and don't let anyone merge over one either; green means green.
- **A unit growing past its scope**: split it; the piece downstream actually
  needs ships first, the rest becomes a new unit at the chain's tail.
- **A merge broke `main`**: message all devs to hold merges, insert a fix
  unit at the front of the chain, resume when green.
- **A review stall** — a unit sitting `in-review` while its reviewer works
  something else: nudge the reviewer; still stuck → reassign the unit to
  another reviewer. A severity dispute (dev says Low, reviewer says Medium)
  → **you rule, by message, immediately** — a chain waiting on a
  classification argument is pure loss. When the dispute is genuinely about
  what the unit *should do*, that's a judgment call: FnB.
- **A link gone quiet** — no transition report, no tracker-visible movement,
  no reply: nudge by message; a live shell reads it at its next step
  boundary, a dead one never will. A second nudge met with silence →
  **escalate to the FnB: only the FnB boots shells.** Ask for the shell to
  be booted or the unit reassigned. A dead link is invisible unless you're
  counting heartbeats — the bottleneck question in Step 3 is what surfaces
  it.
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
4. **Write the sprint report** — one `documents` row, the sprint's durable
   record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Cover: units shipped (PRs, planned vs. actual order), review outcomes
   (Major/Medium found and fixed per unit; the Low notes reviewers filed —
   this is where they land, as the post-sprint cleanup list), stalls hit and
   how each was unblocked, anything cut or re-scoped and why, and what the
   sprint surfaced about the process itself. Message the FnB: sprint closed,
   report at doc `<id>`.
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
