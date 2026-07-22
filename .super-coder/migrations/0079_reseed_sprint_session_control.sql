-- 0079 — reseed sprint workflow for provider-neutral managed planner wake.
--
-- Sprint eventing previously taught that Claude's inbox watcher was the
-- planner control plane and that durable message rows woke ordinary workers.
-- Session control now provides the actual boundary: a managed binding wakes
-- only the planner's existing native conversation; the planner explicitly
-- boots workers. These exact replacements keep existing forks and fresh
-- rebuilds aligned with the authoritative source assets.

BEGIN;

UPDATE skills SET description =
'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, manage the planner''s provider-neutral session binding, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — run the pre-freeze conformance pass (review shells judge the spec against main), freeze the doc (revoking all scoped authority), release managed wake, and synthesize the sprint report from unit reports + the conformance doc into the fixed skeleton. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.'
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the watcher daemon, which sends you `pr_event` rows. Your inbox
watcher wakes you the moment any row lands. Workers are ephemeral,
per-task sessions; you are the one long-lived context in the loop — you
manage, you never load code. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != ''shell'' ORDER BY created_at`.',
'**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the GitHub watcher daemon, which sends you `pr_event` rows. A managed
planner binding turns each unread row into a deduplicated wake job: deliver
to the bound conversation when idle, leave it queued while busy, or resume
that same native conversation when dormant. Workers are still booted
explicitly; a row addressed to an ordinary worker does not start a model
turn by itself. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != ''shell'' ORDER BY created_at`.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'## Step 2: Arm the watcher, kick off

**Arm your inbox watcher first** — the zero-token wake-up that replaces
every scheduled tracker. On the claude harness, run it as a background
task (it blocks until any message row lands for you, then exits — the
exit is your wake-up):

```
./sc watch inbox        # background it via your harness''s background-task tool
```

**Interactive sessions only.** A harness background task is
session-scoped: in a headless (`-p`) boot it dies with the session,
silently — six sprint stalls traced to exactly this. A headless planner
turn arms nothing: drain the inbox, act, end the turn — the next event
row boots you again. The watcher belongs to the long-lived interactive
planner seat, nowhere else.

Re-arm it every time you finish draining your inbox. On other harnesses
the watcher isn''t available — check your inbox at every task boundary
instead; correctness is identical, latency degrades gracefully. (Strong
recommendation, not a gate: the planner seat runs best on claude/Fable —
the one long-lived, low-volume, high-leverage context in the loop, and
the only seat the watcher fully serves.)',
'## Step 2: Manage the planner session, kick off

**Enable managed wake before kicking off a worker:**

```
./sc session manage <planner-shortname> --sprint <doc-id>
./sc session status <planner-shortname>
```

`manage` is idempotent, binds this sprint to the planner''s existing engine
archive + native harness conversation, and fails closed unless that binding
can deliver live or resume dormant with its pinned model, effort, worktree,
permissions, and sandbox posture. Do not kick off on `starting`, `released`,
or `error`; fix the reported capture/auth/posture problem, then run
`./sc session retry <planner-shortname>` for failed unread work.

The binding lifecycle is the control boundary:

- `starting` queues only until the native ID/control endpoint is confirmed;
- `foreground` or `idle` uses the provider''s validated active transport;
- an active turn is `dispatching`, so new messages stay queued — never steer;
- `dormant` resumes the same native ID only after exact owner/lease checks;
- `error` preserves unread rows for diagnosis/retry; `released` disables wake.

The dispatcher coalesces queued rows into the fixed inbox prompt. You drain
the inbox, act on every row, and mark each handled row read; `read_at` is the
only delivery acknowledgement. Infrastructure never marks mail read for you.
A Claude interactive planner may keep `./sc watch inbox` armed as its
provider-local active channel, but that watcher is not the cross-provider
correctness model and it never belongs in a headless turn.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'Your watcher wakes you on every row. On wake, drain the inbox and act:',
'The managed binding delivers a turn for queued unread rows. On every delivered
turn, drain the inbox and act:')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'- Mark rows read as you fold them; then **re-arm the watcher**.',
'- Mark rows read only after you fold and act on them. Any row left unread
  remains queued for a later delivery.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.',
'You boot workers; neither the GitHub watcher nor the session dispatcher does
that. The watcher writes planner events; the dispatcher wakes only the managed
planner binding. The FnB is pulled in only for judgment or provider posture
that the engine cannot resolve safely.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'4. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).',
'4. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then release the
   planner binding with `./sc session release <planner-shortname>` so new
   ordinary mail stays unread without autonomous delivery. If status is
   `dispatching`, do not wait on the turn that is currently executing; have
   the operator run `./sc session release <planner-shortname> --after-turn`
   after this turn returns. Release preserves the archive/native conversation.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.',
'- Zero scheduled model polling by any shell: managed session delivery wakes
  the planner, the planner boots workers, and PR watches retire themselves.
  A scheduled model tracker anywhere in the sprint is a defect.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'- Local long work rides `./sc job` (see the `sprint` skill) — a job''s
  completion is a `result` row like any other wake-up. A hand-rolled
  nohup/poll waiter anywhere in the sprint is a defect: one sprint''s
  hand-rolled waiter carried a self-match bug that masked a dead bench.',
'- Local long work rides `./sc job` (see the `sprint` skill) — a job''s
  completion is a durable `result` row, not an autonomous worker boot. A
  hand-rolled nohup/poll waiter anywhere in the sprint is a defect: one
  sprint''s hand-rolled waiter carried a self-match bug that masked a dead bench.')
WHERE name='sprint_orchestration';

UPDATE skills SET description =
'Participant loop for a declared multi-shell sprint — dev, reviewer, or conformance slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, close your unit with a structured unit-report result row, report every transition as a result row. Conformance slot: judge the spec against main pre-freeze, four-way verdicts. No scheduled polling — the managed session dispatcher wakes the planner, and the planner explicitly boots workers. Local long work (suites/benches) rides ./sc job, never a harness background task. Load when a sprint task message names you a participant.'
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'**You never poll on a schedule.** The sprint is event-driven: the planner
wakes you with `task` rows (often by booting you headless — `./sc run` —
with the task as your prompt), the GitHub watcher daemon turns your PR''s
transitions into `pr_event` rows for the planner, and you report every
state change back as a `result` row. A session that has nothing left to
act on ends; the next event boots the next one. Your memory, archives,
and messages accrete across boots — an ephemeral session is still you.',
'**You never poll on a schedule.** The planner sends durable `task` rows and
explicitly boots workers (often headless with `./sc run`). The GitHub watcher
daemon turns your PR transitions into `pr_event` rows; the session dispatcher
delivers those unread events only to the planner''s managed binding. You report
every state change as a `result` row. A message does not boot an ordinary
worker by itself; a live inbox check or an explicit planner boot starts your
next turn. Memory, archives, and messages persist across those turns.')
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'- **Fire-and-wake (default):** `./sc job start [--label <x>]
  [--timeout <s>] -- <cmd>` — the job survives your session; completion
  lands in YOUR inbox as a `result` row, and the normal event loop (your
  next boot''s inbox drain) acts on it. If the sprint waits on the
  outcome, report the job id to the planner, then end the turn.',
'- **Detached completion:** `./sc job start [--label <x>]
  [--timeout <s>] -- <cmd>` — the job survives your session; completion
  lands in YOUR inbox as a `result` row. That row is durable, but it does
  not autonomously boot an ordinary worker. Use this only when no immediate
  continuation depends on the result; otherwise use wait-slice or arrange an
  explicit planner re-boot rather than ending the turn on an assumed wake.')
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'but do NOT open your PR out of turn — the planner''s next `task` row (sent
on your upstream''s merge event) is your turn signal; a booted-headless
session simply ends here and the planner re-boots you when the chain
reaches you. Don''t schedule a watcher; don''t poll. Upstream visibly
stalls from where you sit -> `result` row to the planner; don''t sit
silent behind a stuck link.',
'but do NOT open your PR out of turn — the planner''s next `task` row (sent
after the managed planner receives your upstream''s merge event) is your turn
signal; a booted-headless session simply ends here and the planner explicitly
re-boots you when the chain reaches you. Don''t schedule a watcher; don''t poll.
Upstream visibly stalls from where you sit -> `result` row to the planner; don''t sit
silent behind a stuck link.')
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'**5. Babysit CI while live.** `gh pr checks <your-pr> --watch` blocks in
your session at zero scheduled cost — use it while you''re booted; if your
session ends first, the daemon''s red/green event reaches the planner and
a `task` row re-boots you. Never a cron, never a scheduled wake.',
'**5. Babysit CI while live.** `gh pr checks <your-pr> --watch` blocks in
your session at zero scheduled cost — use it while you''re booted; if your
session ends first, the daemon''s red/green event reaches the managed planner.
The planner sends any needed `task` row and explicitly re-boots you. Never a
cron, never a scheduled model wake.')
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'1. **Wake = a review request.** A dev''s `ready for review` message — or a
   planner `task` row booting you headless with the request as prompt —
   is next-in-queue work; a waiting review stalls the chain exactly like',
'1. **Wake = an explicit review turn.** A dev''s `ready for review` message is
   durable; a live inbox check or the planner booting you headless starts the
   next-in-queue work. A waiting review stalls the chain exactly like')
WHERE name='sprint';

UPDATE skills SET content = replace(content,
'- No scheduled polling, ever: `task` rows and headless boots wake you;
  `pr_event` rows wake the planner; the sprint doc tells you what a wake
  means.',
'- No scheduled model polling, ever: only a live inbox check or explicit
  `./sc run` starts a worker turn; unread events wake the managed planner
  binding. The sprint doc tells every turn what it means.')
WHERE name='sprint';

COMMIT;
