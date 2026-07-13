-- 0060 — reseed: sprint eventing — the sprint skills stop polling
--
-- Sprint eventing (specs_sc/sprint-eventing.md, migration 0059) replaces the
-- per-shell scheduled PR trackers with the message bus: typed rows (task /
-- result / pr_event), the watched_prs registry + watcher daemon, headless
-- worker boots (./sc run), and the planner's zero-token inbox watcher
-- (./sc watch inbox). Three skills carry the rewire:
--
--   sprint               — dev registers the PR watch for the planner at PR
--                          open; trackers dropped; transitions are result rows
--   sprint_orchestration — declaration gains the two-question model &
--                          provider interview + the `models:` header line;
--                          kickoffs are task rows + ./sc run worker boots;
--                          planner tracker → inbox watcher
--   messaging            — send grows --kind; the kinds documented
--
-- Source assets updated in the same commit; this trailing forward reseed
-- (UPSERT by name; skill_id + grants preserved) carries them to installed
-- forks and fresh builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint',
  'Participant loop for a declared multi-shell sprint — dev or reviewer slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, report every transition as a result row. No scheduled polling — the planner and the watcher daemon wake you. Load when a sprint task message names you a participant.',
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

You are in a sprint ONLY when a planner `task` message names you a
participant and points at a sprint doc. No kickoff -> this skill is inert.

**You never poll on a schedule.** The sprint is event-driven: the planner
wakes you with `task` rows (often by booting you headless — `./sc run` —
with the task as your prompt), the GitHub watcher daemon turns your PR''s
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

The planner is the doc''s only writer. NEVER `sc mem doc edit` the sprint
doc — report state changes to the planner as `result` rows; the planner
updates the board.

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

**2. Prepare.** Run the `git` skill''s sync gate; cut your feature branch
from your base. Your unit needs upstream code that hasn''t merged -> branch
stacked on the upstream shell''s branch + accept the retarget duty in
step 4. Buildable against current `main` -> branch from `main`; stack only
for real code dependencies.

**3. Build.** Your dependency not yet merged? Build and commit locally,
but do NOT open your PR out of turn — the planner''s next `task` row (sent
on your upstream''s merge event) is your turn signal; a booted-headless
session simply ends here and the planner re-boots you when the chain
reaches you. Don''t schedule a watcher; don''t poll. Upstream visibly
stalls from where you sit -> `result` row to the planner; don''t sit
silent behind a stuck link.

**4. Take your turn** the moment your dependency is on `main` (your
kickoff said "start now", or a planner `task` row says so):

- stacked on the upstream branch -> retarget first: `gh pr edit <your-pr>
  --base main` if the PR exists, otherwise note your base is gone — same
  discipline as the `git` skill''s stacked-merge procedure;
- `git fetch origin && git rebase origin/main` on your feature branch;
- push, open your PR — then, in the SAME step:

```
./sc watch pr <owner/repo> <pr-number> --shell <planner-shortname>
sc mem message send <planner> "sprint <doc-id>: unit <seq> pr-open — PR #<n>" --kind result
```

The watch is what makes the loop event-driven: the daemon now turns every
CI conclusion, review, and merge on your PR into a `pr_event` row in the
planner''s inbox. Registration is explicit and happens at PR open — a PR
without a watch is invisible to the sprint.

**5. Babysit CI while live.** `gh pr checks <your-pr> --watch` blocks in
your session at zero scheduled cost — use it while you''re booted; if your
session ends first, the daemon''s red/green event reaches the planner and
a `task` row re-boots you. Never a cron, never a scheduled wake.

Triage before fixing: is the failure in something your diff touches? Does
`main` show the same failure? Does the log say timeout / runner died /
network / flaky test you never touched? Anomalous -> `gh run rerun
<run-id> --failed`, don''t patch healthy code. Anomalous red survives two
reruns -> `result` row to the planner (flaky suite, broken `main`, infra)
and hold — planner''s to fix as a unit, not yours to absorb. When a fix
needs a fix, suspect the diagnosis.

Real red -> read the failure, fix, push, watch again — your loop to run,
not the planner''s to chase. Three honest fix attempts without green ->
`result` row with what''s failing and what you''ve tried. Reruns of flakes
count neither as attempts nor as green: merge authority requires actual
green checks — "it''s just a flake" is never a merge.

**6. Pass sprint review.** CI green -> message your unit''s reviewer
`sprint <doc-id>: unit <seq> ready for review — PR #<n>, checks green`
(+ your open ambiguity calls) and tell the planner `in-review`
(--kind result). Major/Medium findings block: fix, push, re-request; keep
CI green across fix pushes. Low findings = notes for the sprint report,
not gates. Disagree with a severity call -> planner rules; don''t litigate
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

Gate the units the doc''s `reviewer` column assigns you. Method = the base
`review` skill (adversarial, verify-don''t-trust, review against the unit''s
scope); this overlay changes only pace and severity:

1. **Wake = a review request.** A dev''s `ready for review` message — or a
   planner `task` row booting you headless with the request as prompt —
   is next-in-queue work; a waiting review stalls the chain exactly like
   red CI. Keep a `SPRINT doc=<id> reviewing=<seq,seq,…>` line in
   `current_state`. No trackers, no scheduled polls.
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
   review-clean`), --kind result. The FnB gate is unchanged everywhere
   else and returns the moment the doc freezes.
4. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev''s merge; never leave it implied.
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
  inside a declared sprint, never precedent outside one.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — freeze the doc (revoking all scoped authority) and write the sprint report. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, interview the FnB for the sprint''s models, boot each worker when its
turn comes, watch the event stream, unblock stalls, close out with a
report. The participant loop (build → PR + watch → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) = the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants'' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the watcher daemon, which sends you `pr_event` rows. Your inbox
watcher wakes you the moment any row lands. Workers are ephemeral,
per-task sessions; you are the one long-lived context in the loop — you
manage, you never load code. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != ''shell'' ORDER BY created_at`.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Units that don''t touch each other run in parallel; keep
chains short and the graph wide where the code allows.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don''t let one reviewer become the whole sprint''s
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

You are the doc''s only writer: devs report transitions as `result` rows;
fold them into the board with `sc mem doc edit <id> --body-file`.

## Step 2: Arm the watcher, kick off

**Arm your inbox watcher first** — the zero-token wake-up that replaces
every scheduled tracker. On the claude harness, run it as a background
task (it blocks until any message row lands for you, then exits — the
exit is your wake-up):

```
./sc watch inbox        # background it via your harness''s background-task tool
```

Re-arm it every time you finish draining your inbox. On other harnesses
the watcher isn''t available — check your inbox at every task boundary
instead; correctness is identical, latency degrades gracefully. (Strong
recommendation, not a gate: the planner seat runs best on claude/Fable —
the one long-lived, low-volume, high-leverage context in the loop, and
the only seat the watcher fully serves.)

**Kick off** — a `task` row per participant (doc id + the instruction to
load the `sprint` skill + the slot), then boot whoever can start:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev headless, with the sprint''s models:
./sc run <dev> --harness <devs-harness> -m <devs-model>
```

`./sc run` renders the shell''s boot doc and drains its inbox
non-interactively — the `task` row you just sent is what it acts on. The
default prompt is exactly that ("check your inbox and act"); pass
`-p` only to say something the task row doesn''t. A shell with a live
session refuses to boot (one shell, one session) — a live session reads
the same `task` row at its next inbox check.

Keep `task` bodies model-neutral and constraint-explicit: point at the
sprint doc, the unit, the spec, and the skill — don''t restate them in
your own phrasing. Constraints live in specs, which every lineage reads
the same way.

This kickoff activates each dev''s scoped merge authority and each
reviewer''s direct-handoff authority for its assigned units.

## Step 3: Monitor the event stream

Your watcher wakes you on every row. On wake, drain the inbox and act:

- **`result` rows** (dev/reviewer transitions — pr-open, in-review,
  review-clean, merged, ambiguity calls, stall reports): fold into the
  board, then move whatever it unblocks.
- **`pr_event` rows** (daemon ground truth — checks green/red, review
  submitted, merged, closed): the wake-up for transitions no worker is
  live to report. Green on an in-review unit -> nothing (the reviewer
  gate holds); red -> re-task the unit''s dev (`task` row + `./sc run`);
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
the sprint''s state: a rebooted planner replays the rows and loses
nothing.

Messages are your steering wheel: every dev checks its inbox at each
step start, and a headless boot drains it first thing — your `task` row
is read before that dev''s next move. Steer with `task` rows — holds,
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
  dev''s job was to rerun and report, not patch healthy code): fix the
  cause as its own unit, or hold the chain while infra recovers; rule by
  `task` row when the dev may proceed. Don''t count phantom reds against
  the dev''s fix attempts — and don''t let anyone merge over one; green
  means green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain''s tail.
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
  stall you can''t unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant''s scoped authority ends with it.
2. Message every participant (`task` row): sprint closed, default merge
   gates resume.
3. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
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
   surfaced about the process itself. The `kind != ''shell''` message
   trail is your primary source — it is the sprint, in order.

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
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- You manage; you never load code. Your context grows at coordination
  density — the workers'' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'messaging',
  'Shell-to-shell inbox — send a markdown message to another shell (typed: shell/task/result; pr_event is daemon-emitted), check your unread inbox, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.',
  'substrate',
  NULL,
  1,
  '# messaging — the shell inbox

Shell-to-shell markdown messages, driven by `sc mem message`. Sender = you;
recipient addressed by `shortname`. Body = markdown, preserved verbatim.
Recipient discovers it on its next boot via the `## STATUS` `Inbox:` count.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | mark-read <id>`

## Message kinds

Every message carries a `kind` — the trail stays filterable
(`SELECT * FROM shell_messages WHERE kind != ''shell''` replays a sprint''s
whole coordination history):

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — planner → worker instruction (a sprint kickoff / re-task).
- `result` — worker → planner completion or transition report.
- `pr_event` — GitHub watcher daemon → shell PR transition (checks
  green/red, review submitted, merged, closed). Daemon-emitted only:
  `send` refuses it — a forged PR event would poison the wake loop''s
  ground truth. Detail lives in `gh`; the row is the wake-up, not the
  payload.

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

Read-only — it does NOT auto-mark-read. Non-`shell` rows show their kind
inline. Surface the body to the operator (reply if warranted — a reply is
itself a `send`), then `mark-read` the inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>" [--kind shell|task|result]
```

- Multi-word body = one quoted argument; markdown preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send plan1 "sprint 12: unit 3 merged (PR #41)" --kind result`
- Unknown / deleted recipient -> `mem: recipient shortname ''<x>'' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Pass the `message_id` that `check` surfaced. Only messages addressed to you
clear — another shell''s message = no-op; re-marking a read message = no-op.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
