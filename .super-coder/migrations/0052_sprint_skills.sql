-- 0052 — sprint skills: self-orchestrated multi-shell pushes (issues #325, #326)
--
-- Two companion craft skills for the loop planner → devs → reviewers →
-- devs → planner. `sprint` (participant-side, dev + reviewer slots): read
-- your slot from the planner's sprint doc, run one sprint tracker, take
-- your turn when your dependency lands (rebase + PR), babysit CI, pass
-- sprint review (Major/Medium block, Low goes to the report), merge your
-- own PR on green+clean under scoped authority, hand off.
-- `sprint_orchestration` (planner-side): decompose + sequence the push,
-- assign devs + reviewers, declare the sprint doc (a documents row,
-- kind='doc'), kick off, monitor the board, unblock stalls, close out —
-- freeze the doc (revoking all scoped authority), tear down every tracker,
-- write the sprint report. Enforcement is advisory (skill text) in v1.
--
-- Self-contained on purpose (same reason as 0049): at update time `migrate`
-- runs BEFORE the catalogue sync, so the grants below cannot rely on the
-- sync having inserted the skill rows — the migration carries the bodies
-- itself (UPSERT by name; skill_id + existing grants preserved). The sync
-- re-asserts the same content harmlessly afterward. 0001 is regenerated from
-- assets for fresh builds; this forward reseed carries the same bodies to
-- installed forks.
--
-- Grants: existing dev + reviewer shells get `sprint` (dev slot / reviewer
-- slot of the same participant skill), existing planner shells get
-- `sprint_orchestration`; NEW shells get them from
-- templates/shells/{dev,reviewer,planner}.json.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint',
  'Participant loop for a declared multi-shell sprint — dev or reviewer slot. Read your slot from the sprint doc, stand up your one sprint tracker (wakes you on every green/red/merge), take your turn when your dependency lands (rebase → PR), babysit CI, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, hand off, kill the tracker at close-out. Load when a sprint kickoff message names you a participant.',
  'craft',
  NULL,
  0,
  '# sprint — your slot in a coordinated multi-shell push

A **sprint** is a declared, planner-governed push where several shells build
dependent units of work: B builds on A, C on B. The loop is planner → devs →
reviewers → devs → planner: every unit is built, reviewed, fixed, and merged
by the shells themselves — self-running the handoffs the FnB used to
orchestrate by hand. This skill is the participant side, and your slot is
either a **dev slot** (you build a unit — "The loop" below) or a **reviewer
slot** (you gate units — "Your slot as reviewer" below). The planner side
(declaring, monitoring, closing, the sprint report) is the
`sprint_orchestration` skill; `git`, `review`, and `messaging` remain the
base disciplines underneath.

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

Body contract (what you''ll find):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

Unit `status` walks: `waiting → building → pr-open → in-review → fixing →
merged` (`fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on).

**The planner is the doc''s only writer.** You never `sc mem doc edit` it —
you report state changes to the planner by message, the planner updates the
board. One writer keeps the board coherent; your reports are the inputs.

## Scoped merge authority — the boundary, stated once

The `git` skill''s rule stands: merging is the FnB''s gate. A sprint grants a
**narrow exception**, and only this:

- **only** the PR for **your assigned unit** in this sprint,
- **only** when **all checks are green**,
- **only** after your unit''s reviewer declared it **review-clean**
  (every Major/Medium finding fixed),
- **only while** the sprint doc says `status: ACTIVE` and is not frozen.

Everything outside those four conditions — other PRs, other repos, a red or
pending check, an unreviewed diff, a closed or frozen sprint — is the default
FnB gate, unchanged.
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
row. Note what you depend on (upstream unit + its shell), who reviews you,
and what depends on you (downstream shell — that''s who you hand off to). No
upstream → you start immediately. Embed one line in your `current_state` —
your slot at a glance, kept current as your status walks, dropped at
stand-down:

```
SPRINT doc=<id> unit=<seq> upstream=<seq|none> downstream=<shortname|none> status=<...>
```

**2. Stand up your sprint tracker — one, for the whole sprint.** A sprint is
mostly waiting for someone else''s PR to go green and merge, and you won''t be
sitting in a live session when it does. The tracker is what wakes a cold
shell: a recurring watcher in your harness''s scheduler (cron / scheduled
wakeup) that polls the sprint''s PRs and **notifies you on every transition —
any sprint PR going green, going red, or merging**. Every participant runs
one; so does the planner. Rules that make it not hurt:

- **Exactly one tracker per shell, spanning the sprint.** Derive the watch
  list live each poll — `gh pr list` filtered to the branches in the sprint
  doc — so PRs that open mid-sprint join automatically. If you''re tempted to
  add a second trigger or edit the first mid-sprint, the tracker''s query was
  wrong; fix it, don''t multiply it.
- **Waking is not knowing.** A notification tells you *something* moved;
  re-orient before acting — read the sprint doc, find your row, check your
  inbox. The doc says whether it''s your turn; the tracker only says "look".
- **The tracker dies with the sprint** (step 9). A sprint tracker still
  firing in a later session is a defect you created.
- No scheduler in your harness → fall back to in-session polling
  (`gh pr view <upstream-pr> --json state,mergedAt` · `git fetch origin
  main`) between work units, and say so to the planner at kickoff.

**3. Prepare.** Run the `git` skill''s sync gate, cut your feature branch from
your base. If your unit needs upstream code that hasn''t merged yet, branch
stacked on the upstream shell''s branch — and accept the retarget duty in
step 5. If you can build against current `main`, do that instead; stacks are
for real code dependencies, not moral support.

**4. Watch for your dependency to land.** Signals, in order of trust:

- **Your tracker** — the merge notification for your upstream unit *is* your
  turn signal, cold session or live.
- **Inbox** — the upstream dev messages you on merge (that''s *its* step 8).
  `sc mem message check` between work units and on every tracker wake.
- **Manual poll** — backup when live and impatient; never a reason to skip
  the tracker.

While waiting you can build and commit locally; you just can''t open your PR
out of turn. If the upstream unit visibly stalls (red CI for hours, scope
ballooning), message the planner — don''t sit silent behind a stuck link.

**5. Take your turn.** The moment your dependency merges:

- stacked on the upstream branch → **retarget first**: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise just note your base is gone —
  same discipline as the `git` skill''s stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR, and message the planner that you''re `pr-open`.

**6. Babysit CI.** `gh pr checks <your-pr> --watch` while live; your tracker
covers the cold gaps — a red on your PR is a wake-up call, not news you hear
from the planner.

**Not every red is your bug — triage before you fix.** Ask: is the failure
in something your diff touches? Does `main` show the same failure? Does the
log say timeout, runner died, network, a flaky test you never went near?
Anomalous → **re-run the failed checks** (`gh run rerun <run-id> --failed`),
don''t patch healthy code. An anomalous red that survives two reruns is a
board problem — message the planner (flaky suite, broken `main`, infra) and
hold; it''s the planner''s to fix as a unit, not yours to absorb. When a fix
needs a fix, suspect the diagnosis.

A real red → read the failure, fix, push, watch again. This is your loop to
run, not the planner''s to chase. **Three honest fix attempts without green →
message the planner** with what''s failing and what you''ve tried; a wedged
link is a board problem, not a private shame. (Reruns of flakes don''t count
as attempts — but neither do they count as green: **merge authority still
requires actual green checks.** "It''s just a flake" is never a merge.)

**7. Pass sprint review.** CI green → message your unit''s reviewer that the
PR is ready (`sprint <doc-id>: unit <seq> ready for review — PR #<n>,
checks green`) and tell the planner you''re `in-review`. The reviewer answers
with findings, **Major/Medium only as blockers** — fix those, push,
re-request; CI re-runs on your push, so keep it green while you go. Low
findings arrive as notes, not gates — they land in the sprint report, not in
your critical path. Disagree with a severity call → planner rules; don''t
litigate in the thread while the chain waits.

**8. Merge on green + clean, then hand off.** All checks green, reviewer
declared review-clean, boundary above satisfied:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
sc mem message send <planner-shortname> "sprint <doc-id>: unit <seq> merged (PR #<n>)."
```

No downstream (you''re the last link) → the planner message is the handoff.
Then clean up local per the `git` skill (re-pin base, delete the branch).

**9. Stand down.** The planner''s close-out message (or a frozen/`CLOSED`
sprint doc) ends the sprint: merge authority is gone, default gates resume,
and — **before anything else — kill your tracker, drop the SPRINT line from
your `current_state`,** and confirm both in your reply to the planner.

## Your slot as reviewer

A reviewer slot gates the units the doc''s `reviewer` column assigns you. The
base `review` skill is your method — adversarial, verify-don''t-trust, review
against the unit''s scope; this overlay changes only pace and severity:

1. **Same tracker, same ledger.** Stand up your one sprint tracker at
   kickoff and a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. Your wake signal is a `ready for review` message or an
   assigned unit''s PR going green — a review request is next-in-queue work,
   not eventually-work; a waiting review is a stalled chain, exactly like
   red CI.
2. **Major/Medium block; Low informs.** A sprint runs on velocity with a
   quality gate, not a full-polish gate. Findings that are wrong-behavior,
   data-loss, security, spec-violation (Major) or will-bite-soon (Medium) →
   the dev fixes them now, and you re-review on the fix push. Style, naming,
   nice-to-have refactors (Low) → one summary note to the planner for the
   sprint report; they don''t block merge and you don''t re-litigate them.
3. **Handoffs go direct — a scoped relaxation, same shape as the merge
   authority.** The base `review` skill gates handoffs behind the FnB.
   Inside an ACTIVE sprint, for your assigned units only, you message the
   author dev your findings directly and copy the planner one line
   (`unit <seq>: N major, M medium — with <dev>` or `unit <seq>:
   review-clean`). The FnB gate is unchanged for everything else, and it
   returns the moment the doc freezes.
4. **Clean is a declaration.** `review-clean` to the dev + planner is what
   unlocks the dev''s merge — say it explicitly, never leave it implied.
5. **Stand down like everyone else.** Close-out message → kill your tracker,
   drop your SPRINT line, confirm to the planner.

## Stance

- **The tracker watches, you decide.** One watcher per shell for the whole
  sprint; notifications wake you, the sprint doc tells you what it means.
- **Report state changes, not progress prose.** The planner needs
  `building → pr-open → in-review → fixing → merged` transitions, one line
  each.
- **The boundary is load-bearing.** Merge-on-green+clean and direct review
  handoffs are scoped authority inside a declared sprint, never a precedent
  outside one.',
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

The FnB declares *that* a sprint happens; you make it run. The loop is
planner → devs → reviewers → devs → planner: you decompose the push into
units, sequence who builds on whom, assign a reviewer to every unit, kick
off every participant, watch the whole board, unblock stalls, and close it
out with a report. The participant loop (build → PR → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) is the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact: the **sprint doc**. Your declaration turns
the participants'' scoped authority on (dev merge-on-green+clean, reviewer
direct handoffs); your close-out turns it off.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map the
dependency order — and be stingy with it: **a dependency edge is a real code
dependency, not a preference.** Units that don''t touch each other run in
parallel; a chain is only as fast as its slowest link, so keep chains short
and the graph wide where the code allows.

Assign each unit a dev shell **and a reviewer shell** (one reviewer can gate
several units — just don''t let one reviewer become the whole sprint''s
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

**You are the doc''s only writer.** Devs report transitions by message; you
fold them into the board with `sc mem doc edit <id> --body-file`. One writer,
one board, no drift.

## Step 2: Kick off

Message every participant its slot — the doc id, the instruction to load the
`sprint` skill, and what its slot is:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>."

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green."
```

First-in-chain starts immediately; everyone else starts watching. From this
message on, each dev holds the scoped merge authority and each reviewer the
direct-handoff authority for its assigned units.

Then **stand up your own sprint tracker** — the same pattern the `sprint`
skill gives the devs, and the answer to "how does a cold shell know it''s
time": one recurring watcher per shell in the harness scheduler, polling the
sprint''s PRs (watch list derived live from the doc''s branches, so mid-sprint
PRs join without edits) and notifying on **every green, red, and merge**.
Every participant runs exactly one for the sprint''s duration; nobody
hand-builds ad-hoc triggers mid-sprint — if a tracker misses something, fix
its query, don''t add a second. All trackers die at close-out (Step 5).

## Step 3: Monitor the board

Your tracker wakes you on every transition; between wakes, poll — don''t wait
for reports:

```
gh pr list --state all             # who''s open, merged, closed
gh pr checks <n>                   # the current bottleneck''s CI state
sc mem message check               # dev transition reports
git fetch origin main              # what actually landed
```

At any moment you should be able to answer: **which link is the bottleneck?**
Fold every state change into the doc as it happens — the board is what the
FnB and any rebooted shell reads to re-orient mid-sprint. On a tracker wake,
that''s the whole job: read the event, update the board, nudge whoever it
unblocks if their own tracker hasn''t already.

**Messages are your steering wheel.** The `sprint` skill has every dev check
its inbox at the start of each step and on every tracker wake — so a message
from you is guaranteed to be read before that dev''s next move. Steer with it:
holds, re-sequencing, nudges, rulings on reported reds. The board records
state; messages change behavior. When they''d conflict, your latest message
wins — then update the board to match.

## Step 4: Unblock

Stalls you''ll meet, and the moves:

- **A dev wedged on red CI** (it reports after three failed fix attempts, per
  the `sprint` skill): decide — pair another shell onto it, re-scope the
  unit, or pull the failing part into a follow-up unit so the chain moves.
- **An anomalous red** — the dev reports a failure that isn''t its bug (flaky
  test, runner death, `main` red underneath): the dev''s job was to rerun and
  report, not to patch healthy code. Yours is to fix the cause as its own
  unit (or hold the chain while infra recovers) and rule by message when the
  dev may proceed. Don''t count phantom reds against a dev''s fix attempts —
  and don''t let anyone merge over one either; green means green.
- **A unit growing past its scope**: split it; the piece downstream actually
  needs ships first, the rest becomes a new unit at the chain''s tail.
- **A merge broke `main`**: message all devs to hold merges, insert a fix
  unit at the front of the chain, resume when green.
- **A review stall** — a unit sitting `in-review` while its reviewer works
  something else: nudge the reviewer; still stuck → reassign the unit to
  another reviewer. A severity dispute (dev says Low, reviewer says Medium)
  → **you rule, by message, immediately** — a chain waiting on a
  classification argument is pure loss. When the dispute is genuinely about
  what the unit *should do*, that''s a judgment call: FnB.
- **A link gone quiet** — no transition report, no tracker-visible movement,
  no reply: nudge by message; a live shell reads it at its next step
  boundary, a dead one never will. A second nudge met with silence →
  **escalate to the FnB: only the FnB boots shells.** Ask for the shell to
  be booted or the unit reassigned. A dead link is invisible unless you''re
  counting heartbeats — the bottleneck question in Step 3 is what surfaces
  it.
- **Re-sequencing**: when the plan meets reality, edit the board and message
  *every* affected dev with its new slot — a dev acting on a stale slot is
  worse than a paused one.
- **Judgment calls** — scope vs. deadline, cut a unit, change an interface
  another team reads: **escalate to the FnB immediately.** Sitting on a
  judgment call is the one stall you can''t unblock yourself.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze the board:
   `sc mem doc freeze <doc-id>`. **Freezing is the revocation** — a frozen or
   `CLOSED` sprint doc is the signal that every participant''s scoped merge
   authority is gone (the `sprint` skill checks exactly this).
2. Message every participant that the sprint is closed, default merge gates
   resume, and **kill your sprint tracker now — reply when it''s dead.**
3. **Tear down the watchers.** Kill your own tracker, then collect the devs''
   confirmations. The sprint is not closed while any tracker lives — a
   watcher leaking into later sessions fires on unrelated PRs and erodes
   trust in the next sprint''s signals. Chase silence like you''d chase red CI.
4. **Write the sprint report** — one `documents` row, the sprint''s durable
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
5. Settle the bookkeeping — close the sprint''s flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- **Enforcement is advisory in v1.** Merge order and authority live in skill
  text and the board, not in a pre-commit check. That makes the board''s
  accuracy *your* discipline — an out-of-date board is a false authority
  grant.
- **Monitor > interrogate.** `gh` and `git fetch` tell you the truth without
  costing a dev a context switch; messages are for what the tools can''t see.
- **Escalate judgment, absorb mechanics.** Re-sequencing is yours; changing
  what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

-- grant to existing shells by flavor (no-op where already granted)
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id
FROM shells s, skills k
WHERE COALESCE(s.is_deleted, 0) = 0
  AND s.flavor IN ('dev', 'reviewer')
  AND k.name = 'sprint' AND k.is_deleted = 0;

INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id
FROM shells s, skills k
WHERE COALESCE(s.is_deleted, 0) = 0
  AND s.flavor = 'planner'
  AND k.name = 'sprint_orchestration' AND k.is_deleted = 0;

COMMIT;
