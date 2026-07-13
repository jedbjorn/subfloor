---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# sprint_orchestration

Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — freeze the doc (revoking all scoped authority) and write the sprint report. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.

**Category:** craft

---

# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, interview the FnB for the sprint's models, boot each worker when its
turn comes, watch the event stream, unblock stalls, close out with a
report. The participant loop (build → PR + watch → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) = the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the watcher daemon, which sends you `pr_event` rows. Your inbox
watcher wakes you the moment any row lands. Workers are ephemeral,
per-task sessions; you are the one long-lived context in the loop — you
manage, you never load code. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != 'shell' ORDER BY created_at`.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Units that don't touch each other run in parallel; keep
chains short and the graph wide where the code allows.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don't let one reviewer become the whole sprint's
bottleneck).

**How many shells to deploy = your call, not a formula.** Weigh the
magnitude of the push against the capacity actually available — the shells
that exist, reviewer bandwidth, how wide the dependency graph genuinely
runs — and make the call. More units than shells is fine (units queue
behind the chain); more shells than parallel work is waste.

**The model & provider interview — exactly two questions to the FnB:**

1. **Devs** — which harness and model? One answer; every dev in the
   sprint runs it.
2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

Flavor-uniform by design: shells of a flavor are interchangeable workers,
and one answer per flavor keeps the board readable and the review lineage
coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). The answers parameterize every
`./sc run` you issue for this sprint. Per-unit model mixing is out of
scope — the interview covers the real need, provider choice per role.

Write the board as a `documents` row:

```
sc mem doc add "SPRINT: <title>" --kind doc --body-file <draft.md>
```

Body contract (the `sprint` skill quotes the same one — keep it exact):

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

Note the returned `document_id` — every task and report references it —
and embed `SPRINT doc=<id> governing` in your own `current_state`; drop
it at close-out.

You are the doc's only writer: devs report transitions as `result` rows;
fold them into the board with `sc mem doc edit <id> --body-file`.

## Step 2: Arm the watcher, kick off

**Arm your inbox watcher first** — the zero-token wake-up that replaces
every scheduled tracker. On the claude harness, run it as a background
task (it blocks until any message row lands for you, then exits — the
exit is your wake-up):

```
./sc watch inbox        # background it via your harness's background-task tool
```

Re-arm it every time you finish draining your inbox. On other harnesses
the watcher isn't available — check your inbox at every task boundary
instead; correctness is identical, latency degrades gracefully. (Strong
recommendation, not a gate: the planner seat runs best on claude/Fable —
the one long-lived, low-volume, high-leverage context in the loop, and
the only seat the watcher fully serves.)

**Kick off** — a `task` row per participant (doc id + the instruction to
load the `sprint` skill + the slot), then boot whoever can start:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell'> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev headless, with the sprint's models:
./sc run <dev> --harness <devs-harness> -m <devs-model>
```

`./sc run` renders the shell's boot doc and drains its inbox
non-interactively — the `task` row you just sent is what it acts on. The
default prompt is exactly that ("check your inbox and act"); pass
`-p` only to say something the task row doesn't. A shell with a live
session refuses to boot (one shell, one session) — a live session reads
the same `task` row at its next inbox check.

Keep `task` bodies model-neutral and constraint-explicit: point at the
sprint doc, the unit, the spec, and the skill — don't restate them in
your own phrasing. Constraints live in specs, which every lineage reads
the same way.

This kickoff activates each dev's scoped merge authority and each
reviewer's direct-handoff authority for its assigned units.

## Step 3: Monitor the event stream

Your watcher wakes you on every row. On wake, drain the inbox and act:

- **`result` rows** (dev/reviewer transitions — pr-open, in-review,
  review-clean, merged, ambiguity calls, stall reports): fold into the
  board, then move whatever it unblocks.
- **`pr_event` rows** (daemon ground truth — checks green/red, review
  submitted, merged, closed): the wake-up for transitions no worker is
  live to report. Green on an in-review unit -> nothing (the reviewer
  gate holds); red -> re-task the unit's dev (`task` row + `./sc run`);
  merged -> boot the downstream dev whose turn it is.
- Mark rows read as you fold them; then **re-arm the watcher**.

A worker self-report is never the verdict — green checks + the reviewer
gate are the only ground truth; the `pr_event` stream is what makes a
"done" checkable without a context switch. `gh pr checks <n>` /
`gh pr list` remain your on-demand detail reads — detail lives in `gh`,
the message is the wake-up.

At any moment, be able to answer: which link is the bottleneck? The board
is what the FnB and any rebooted shell reads to re-orient mid-sprint —
fold every state change in as it happens. The board + message table ARE
the sprint's state: a rebooted planner replays the rows and loses
nothing.

Messages are your steering wheel: every dev checks its inbox at each
step start, and a headless boot drains it first thing — your `task` row
is read before that dev's next move. Steer with `task` rows — holds,
re-sequencing, nudges, rulings on reported reds. The board records state;
messages change behavior; on conflict your latest message wins -> then
update the board to match.

Dev ambiguity reports (`ambiguity: … → chose …`) get a ruling on
receipt: overrule by `task` row while the unit is still un-merged, or
stay silent and the call stands. Either way log the call + outcome the
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
  `task` row when the dev may proceed. Don't count phantom reds against
  the dev's fix attempts — and don't let anyone merge over one; green
  means green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain's tail.
- **Merge broke `main`**: `task` row to all devs to hold merges, insert a
  fix unit at the front of the chain, resume when green.
- **Review stall** (unit sitting `in-review` while its reviewer is idle):
  boot the reviewer — `./sc run <reviewer> --harness <reviewers-harness>
  -m <reviewers-model>`; its inbox holds the review request. Still stuck
  -> reassign the unit to another reviewer. Severity dispute (dev says
  Low, reviewer says Medium) -> rule by message immediately — a chain
  waiting on a classification argument is pure loss. Dispute about what
  the unit *should do* -> FnB.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it —
  `./sc run <shortname>` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.
  The bottleneck question in Step 3 is what surfaces a dead link.
- **Re-sequencing**: edit the board + `task` row to *every* affected dev
  with its new slot — a dev acting on a stale slot is worse than a paused
  one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can't unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant's scoped authority ends with it.
2. Message every participant (`task` row): sprint closed, default merge
   gates resume.
3. Verify the watches are gone: `./sc watch list` — every sprint PR's
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don't leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
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
   surfaced about the process itself. The `kind != 'shell'` message
   trail is your primary source — it is the sprint, in order.

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
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- You manage; you never load code. Your context grows at coordination
  density — the workers' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB's.
