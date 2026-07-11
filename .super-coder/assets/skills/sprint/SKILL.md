---
name: sprint
description: Dev-side loop for a declared multi-shell sprint — read your slot from the sprint doc, stand up your one sprint tracker (wakes you on every green/red/merge), take your turn when your dependency lands (rebase → PR), babysit CI, merge your own PR on green under scoped authority, hand off, and kill the tracker at close-out. Load when a sprint kickoff message names you a participant.
category: craft
common: false
---

# sprint — your slot in a coordinated multi-shell push

A **sprint** is a declared, planner-governed push where several shells build
dependent units of work: B builds on A, C on B. This skill is the dev-side
loop — self-running the handoffs the FnB used to orchestrate by hand. The
planner-side (declaring, monitoring, closing) is the `sprint_orchestration`
skill; `git` and `messaging` remain the base disciplines underneath.

You are in a sprint **only** when a kickoff message from the planner names you
a participant and points at a sprint doc. No kickoff, no sprint — this skill
is inert.

## The sprint doc — one board, planner-owned

The declaration lives in a `documents` row (kind `doc`, title `SPRINT: …`).
Read it with:

```
sc mem get docs                     # find it in the index
sc mem get doc --id <N>             # full body
```

Body contract (what you'll find):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>

| seq | unit | shell | depends on | branch | pr | status |
```

Unit `status` walks: `waiting → building → pr-open → ci-red → merged`.

**The planner is the doc's only writer.** You never `sc mem doc edit` it —
you report state changes to the planner by message, the planner updates the
board. One writer keeps the board coherent; your reports are the inputs.

## Scoped merge authority — the boundary, stated once

The `git` skill's rule stands: merging is the FnB's gate. A sprint grants a
**narrow exception**, and only this:

- **only** the PR for **your assigned unit** in this sprint,
- **only** when **all checks are green**,
- **only while** the sprint doc says `status: ACTIVE` and is not frozen.

Everything outside those three conditions — other PRs, other repos, a red or
pending check, a closed or frozen sprint — is the default FnB gate, unchanged.
Do **not** generalize this authority; it exists because the planner declared
it and dies when the sprint closes. When in doubt, check the doc; if it says
`CLOSED` or is frozen, you have no merge authority.

## The loop

One discipline spans every step: **check your inbox (`sc mem message check`)
at the start of each step and on every tracker wake.** The planner steers the
sprint by message — holds, re-sequencing, scope changes land there before the
board catches up — so a message is authoritative for your slot. Never start a
step on a stale slot.

**1. Know your slot — and write it down.** Read the sprint doc; find your
row. Note what you depend on (upstream unit + its shell) and what depends on
you (downstream shell — that's who you hand off to). No upstream → you start
immediately. Then embed one line in your `current_state`:

```
SPRINT doc=<id> unit=<seq> upstream=<seq|none> downstream=<shortname|none> status=<...>
```

This line is what survives a reboot: a cold session's inbox is already read
and its tracker may have died with it — the SPRINT line in your own state is
how the next boot knows it's mid-sprint, reloads this skill, and re-checks
the board (your dependency may have merged while you were down — then it's
simply your turn). Keep `status=` current as yours walks; recreate the
tracker if a reboot killed it.

**2. Stand up your sprint tracker — one, for the whole sprint.** A sprint is
mostly waiting for someone else's PR to go green and merge, and you won't be
sitting in a live session when it does. The tracker is what wakes a cold
shell: a recurring watcher in your harness's scheduler (cron / scheduled
wakeup) that polls the sprint's PRs and **notifies you on every transition —
any sprint PR going green, going red, or merging**. Every participant runs
one; so does the planner. Rules that make it not hurt:

- **Exactly one tracker per shell, spanning the sprint.** Derive the watch
  list live each poll — `gh pr list` filtered to the branches in the sprint
  doc — so PRs that open mid-sprint join automatically. If you're tempted to
  add a second trigger or edit the first mid-sprint, the tracker's query was
  wrong; fix it, don't multiply it.
- **Waking is not knowing.** A notification tells you *something* moved;
  re-orient before acting — read the sprint doc, find your row, check your
  inbox. The doc says whether it's your turn; the tracker only says "look".
- **The tracker dies with the sprint** (step 8). A sprint tracker still
  firing in a later session is a defect you created.
- No scheduler in your harness → fall back to in-session polling
  (`gh pr view <upstream-pr> --json state,mergedAt` · `git fetch origin
  main`) between work units, and say so to the planner at kickoff.

**3. Prepare.** Run the `git` skill's sync gate, cut your feature branch from
your base. If your unit needs upstream code that hasn't merged yet, branch
stacked on the upstream shell's branch — and accept the retarget duty in
step 5. If you can build against current `main`, do that instead; stacks are
for real code dependencies, not moral support.

**4. Watch for your dependency to land.** Signals, in order of trust:

- **Your tracker** — the merge notification for your upstream unit *is* your
  turn signal, cold session or live.
- **Inbox** — the upstream dev messages you on merge (that's *its* step 7).
  `sc mem message check` between work units and on every tracker wake.
- **Manual poll** — backup when live and impatient; never a reason to skip
  the tracker.

While waiting you can build and commit locally; you just can't open your PR
out of turn. If the upstream unit visibly stalls (red CI for hours, scope
ballooning), message the planner — don't sit silent behind a stuck link.

**5. Take your turn.** The moment your dependency merges:

- stacked on the upstream branch → **retarget first**: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise just note your base is gone —
  same discipline as the `git` skill's stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR, and message the planner that you're `pr-open`.

**6. Babysit CI.** `gh pr checks <your-pr> --watch` while live; your tracker
covers the cold gaps — a red on your PR is a wake-up call, not news you hear
from the planner.

**Not every red is your bug — triage before you fix.** Ask: is the failure
in something your diff touches? Does `main` show the same failure? Does the
log say timeout, runner died, network, a flaky test you never went near?
Anomalous → **re-run the failed checks** (`gh run rerun <run-id> --failed`),
don't patch healthy code. An anomalous red that survives two reruns is a
board problem — message the planner (flaky suite, broken `main`, infra) and
hold; it's the planner's to fix as a unit, not yours to absorb. When a fix
needs a fix, suspect the diagnosis.

A real red → read the failure, fix, push, watch again. This is your loop to
run, not the planner's to chase. **Three honest fix attempts without green →
message the planner** with what's failing and what you've tried; a wedged
link is a board problem, not a private shame. (Reruns of flakes don't count
as attempts — but neither do they count as green: **merge authority still
requires actual green checks.** "It's just a flake" is never a merge.)

**7. Merge on green, then hand off.** All checks green and the boundary above
satisfied:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
sc mem message send <planner-shortname> "sprint <doc-id>: unit <seq> merged (PR #<n>)."
```

No downstream (you're the last link) → the planner message is the handoff.
Then clean up local per the `git` skill (re-pin base, delete the branch).

**8. Stand down.** The planner's close-out message (or a frozen/`CLOSED`
sprint doc) ends the sprint: merge authority is gone, default gates resume,
and — **before anything else — kill your tracker, drop the SPRINT line from
your `current_state`,** and confirm both in your reply to the planner. If your unit is done early, help by reviewing
downstream PRs on request — but merging anyone else's PR was never yours to
do, sprint or not.

## Stance

- **The tracker watches, you decide.** One watcher per shell for the whole
  sprint; notifications wake you, the sprint doc tells you what it means.
- **Report state changes, not progress prose.** The planner needs
  `building → pr-open → ci-red → merged` transitions, one line each.
- **The boundary is load-bearing.** Merge-on-green is scoped authority inside
  a declared sprint, never a precedent outside one.
