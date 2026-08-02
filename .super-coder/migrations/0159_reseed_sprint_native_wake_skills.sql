-- 0159 — publish bounded resolved-flag evidence and native Sprint role contracts.
--
-- Generated from the authoritative skill assets. Full-body UPSERTs converge
-- existing installations; 0001_seed_skills.sql remains the fresh-install seed.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'db_map',
  'Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.',
  'substrate',
  NULL,
  1,
  '# db_map — super-coder''s DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`). NEVER touch that file — read and write it only
through the engine API, via `sc mem`:

- **Read** = `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (`--json` for raw).
  `documents`/`tasks` take `--feature <id>` / `--doc <id>`; `--doc` on
  `documents` returns the one doc *with* its body. `flags` is open-only by
  default; `get flags <id>` includes one resolved row, while `get flags
  --feature <id> --resolved` returns bounded closure evidence.
- **Write** = `sc mem <cmd> …` (see `## Common writes`).

There is NO raw `sqlite3` path — not as a fallback, not for "ad-hoc" reads.
If the API isn''t wired, `sc mem` fails loud instead of writing the DB behind
its back. `sc mem` is already wired to this launched shell — the engine
resolves API identity for you; never name a shell in a write. Decisions read
FLEET-WIDE (every row, tagged `@shortname`) so cross-shell citations
resolve; every other identity surface reads as you.

**The `sc sql` lane** (read-only; `sc sql-rw` gated) is real and blessed for
what `sc mem` doesn''t cover: admin/reporting reads and sweep queries — the
flag_sweep / git_cleanup skills run it by design. The doctrine is one level
down: memory-surface reads and writes go through `sc mem`; `sc sql` is for
reporting ACROSS surfaces, never a write path for identity/memory (that is
what `sc mem` scopes and validates).

The table below = the data model behind those surfaces (what each `sc mem`
write touches), not a query cheatsheet. Lazy-load: `get` the one surface you
need, don''t bulk-read.

**Need a read/write `sc mem` doesn''t expose?** Report the gap, don''t reach for
the DB — the direct path is closed by design, and a fork can''t patch the
engine (`sc update` would overwrite it). Open a flag naming the data + the
use, surface it to the FnB (who carries it upstream); message a
planner-flavor shell too if the fork has one. Until it lands: do what you can
through the API, flag the rest — NEVER query the DB directly.

```
sc mem flag open "[Engine] need to <read|write> <what> — no sc mem surface for it | Blocker for: <your work>"
```

The repo map (`dr_*`) lives in its own db, `.sc-state/map.db` (see the
`surface_catalogue` skill). The `dr_*` tables also exist in `shell_db.db` but
are ALWAYS empty there — a `dr_*` query against `shell_db.db` silently returns
0 rows instead of erroring. Never query `dr_*` here; this map covers only
`shell_db.db` (memory/identity/content).

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — NEVER edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` = planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = off the board without shipping (decided-against / split / absorbed / replaced) — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | roadmap dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite lands first). Directed, kept acyclic (GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | content store — spec/doc bodies; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; NEVER edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `flavor_skills` / `shell_skills` | skill catalogue + shared packs for standard flavors + per-shell packs for Bespoke shells; `resolved_shell_skills` is the effective read view | managed by engine; name any standard shell to change its flavor pack, or a Bespoke shell to change only itself, via `sc skill grant/revoke` |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a work-stream that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

## Common writes

Each routes through the engine API to the live shared DB. `sc mem which`
orients; `sc mem <cmd> -h` shows flags. Writes always target your own shell —
the engine resolves API identity for you.

```
# current_state (rolling status, not a log — replaces in place):
sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
sc mem seed "…"            # sc mem lns "…" for a lesson
sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
sc mem decision "…" --rationale "…"

# roadmap: add a feature / move its horizon:
sc mem roadmap add "…" --status brainstorm --summary "…" [--project <shortname|id>]
sc mem roadmap status <feature_id> shipped

# roadmap grouping + sequencing (drive the GUI Flow view):
sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or ''none'' to clear)
sc mem roadmap depends <feature_id> --on <id> [--on <id>]   # set dependencies (replaces; omit --on to clear; refuses cycles)

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
sc mem doc freeze <document_id>

# spec_tasks (the plan): add a task / advance it / close it honestly:
sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
sc mem task start <task_id>     # sc mem task done <task_id>
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"   # split/re-scope — never mark unbuilt work done

# open / edit / close a flag:
sc mem get flags <flag_id>                         # exact, open or resolved
sc mem get flags --feature <feature_id> --resolved # bounded closure evidence
sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <shortname|id> "…"     # sc mem project status <…> paused

# inbox + first-run:
sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB on commit,
visible to every shell. Persisting to git is an admin/GUI step, not yours.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'flags',
  'Track blockers as flags — surface open ones, open new ones, edit long-lived ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.',
  'substrate',
  NULL,
  0,
  '# flags — blockers & follow-ups

flag = open question / blocker. `--feature <id>` set -> the flag is that
feature''s blocker (joined on the roadmap; shown on the Roadmap card + Flags
tab). `<self>` = your shell_id. All reads/writes go through `sc mem` (the
engine API) — there is no `sqlite3` path.

## Surface

```
sc mem get flags          # your open flags — `#<id> [<label>] (<priority>) <description>`
sc mem get flags --json   # same, as JSON
sc mem get flags <id>     # exact non-deleted row, open or resolved
sc mem get flags --feature <id> --resolved
                          # resolved non-deleted rows for one feature
```

Each flag carries its `feature_id`; cross-reference `sc mem get roadmap` for
the blocked feature''s title.

The default list forms are **open-only**. Exact and feature-scoped resolved
reads include numeric id, display name, owner, feature, priority, description,
opened date, resolved date, and closure notes in human and JSON output. Resolved
history without `--feature` is refused; there is no fleet-wide history read.

The exact CLI form reuses the authenticated single-row endpoint that protects
`flag close`:

```
GET /_sc/mem/flags/{id}
```

## Open

```
sc mem flag open "[Area] what''s blocked | Blocker for: X" --name SC-001 --priority Medium [--feature <id>]
```

- `--name` = short id, format `SC-###`.
- description format = `[Area] {what} | Blocker for: {what it blocks}`.
- `--priority` = High / Medium / Low. `--feature` = the feature it blocks (omit if none).

### Pair every open with a message

Every `flag open` -> a `message send` to whoever clears it (see the
`messaging` skill), so the work lands in their inbox on their next boot:

```
sc mem message send <shortname> "Opened SC-### — <one line> (Blocker for: <x>)."
```

Recipient = whoever the flag blocks:

| Flag is about | Message |
|---|---|
| docs pending after ship | the **planner** |
| a review failure on a diff | the **author dev** |
| a blocker on another shell''s work | **that shell** |
| an FnB decision / no shell owns it | **surface to the FnB** (no `send`) |

Message pairs with the *open* only: NEVER re-message a flag that is already
open; NEVER message on `close`.

## Edit

```
sc mem flag edit <flag_id> [--name SC-002] [--description "…"] [--append "…"] [--priority High] [--feature <id>]
```

For long-lived tracker flags (one flag per arc, description updated
progressively as gates clear).

- `--name` sets or corrects a `display_name` — including on a flag opened
  without one. An unnamed flag is referred to by bare integer, which is the
  precondition for the id/name collision `close` guards against.
- `--description` REPLACES the whole body — carry forward what still applies.
- `--append` grows the body server-side in one statement. Use it on a tracker
  flag: two shells doing fetch -> concatenate -> `--description` concurrently
  lose one edit. It concatenates raw — pass your own leading `\n` separator.
- `--description` + `--append` together -> the command refuses. Pick one.

## Resolve

```
sc mem flag close <flag_id> --notes "…"
```

`--notes` states *how* it was resolved — that''s the trail.

`close` prints the row it holds — id, label, priority, opened date, owner,
description — BEFORE it writes. **Read that line and confirm it names the flag
you meant.** SC-### display names and `flag_id`s are drawn from two counters
drifting through the same small-integer range, so a stale or mistranscribed
reference does not fail loudly: it resolves a different real record and closes
THAT.

Closing an already-resolved flag -> refused, and it prints the resolution notes
the write would have destroyed. A second close overwrites the notes of whoever
verified the flag.

## Stance

Open a flag the moment something blocks or needs follow-up — don''t hold it in
your head. Open flags on a feature = its blockers; clear them all before
calling the feature done. An opened flag with no message sent = a dropped
handoff.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_close',
  'Close or abort a Sprints v2 run — boot whole-Sprint conformance, compile the bounded evidence packet, synthesize the final report, preserve follow-ups, and transition terminally without deleting history.',
  'workflow',
  NULL,
  0,
  '# sprint_close — synthesize and finish

Use as the owning Planner when delivery work is terminal, or when abort has
been chosen. Close-out supplies meaning; the compiler supplies facts.
On entry or any wake, load `sprint_close`, run `sc sprint inbox --sprint <id>`,
inspect the durable message, and accept or decline it only when actionable.

```text
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or context request in a short body
file, then send it to the conformance Reviewer or participant who owns the fact:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send`, confirm that write, then mark the
handled question read with `accept`. For a blocker, relay the evidence, impact,
and exact action needed to every directly affected Sprint participant, and
surface the exceptional recovery need separately to FnB. Continue safe
synthesis, but stop at a decision boundary when the answer is required. Do not
send duplicate reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. This skill is Planner-owned: the Planner or FnB decides
whether an integrity threat warrants pause. Send any needed participant context
before pausing; an active relay is not available after the lifecycle becomes
paused.

Treat an exhausted recovery wake as bounded manual-recovery evidence for FnB;
preserve the unread message and failed wake, and do not create recursive
fallbacks.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

## Delivery-complete gate

Before conformance, re-read work units, dependencies, registered PRs, checks,
merge observations, task membership, pending actionable messages, and active
native runs. Normally every planned unit is completed/cancelled with an
explicit disposition, and code units have their real PR outcome. Close-out is
advisory: the packet surfaces gaps but never prevents the owning Planner or FnB
from making and reporting a completion judgment. Do not infer code completion
from PR state alone.

Request an independent Reviewer through the durable Sprint relay, using the
`sc sprint send` command above with the bound spec revision hashes, integrated
main SHA, ratified judgment list, and `sprint_rev` conformance mode. Confirm the
write and wake receipt, then stop and await the native conformance-result wake.
Give the Reviewer recorded judgments, not unit authors'' narrative; conformance
judges artifacts.

## Conformance boundary

The Reviewer records its report and findings with `sc sprint
record-conformance`. Every finding becomes a pending follow-up for FnB review.
No conformance finding is fixed inside this Sprint at any severity. A safety
finding may demand immediate operator action, but it still remains follow-up
evidence rather than a silently reopened editing lane.

Verify report id, follow-up ids, author identity, and idempotent replay before
synthesis.

FnB records one terminal disposition per follow-up. `accepted` acknowledges
ship-as-is; `resolved` and `dismissed` require a resolution file.

Keep a resolution at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and require a successful durable disposition.

```text
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition accepted
sc sprint disposition-followup --sprint <id> --followup <id> \
  --disposition resolved --resolution-file <path>
```

## Compile bounded evidence

Generate the packet through the authenticated production surface:

```text
sc sprint compile-report --sprint <id> --limit 50 > evidence.json
```

Increase the per-section bound only when the default truncation counters show
that synthesis needs more detail; the maximum is 200. Follow the packet''s full
timeline and participant-conversation links for raw history. Never paste the
entire event stream into the final report.

The packet supplies:

- scope and lifecycle times;
- exact bound spec revisions and recorded mid-Sprint edits;
- planned versus actual work;
- PR outcomes and links;
- judgments and deviations;
- pause, resume, recovery, interrupt, and drift evidence;
- wake states, attempts, liveness aggregates, nudges, and escalations;
- anomalies with bounded detail;
- unresolved units, actionable messages, and follow-ups; and
- links to the complete timeline and every participant conversation.

The compiler does not decide whether a deviation was wise or an anomaly was
acceptable. That is your synthesis.

## Final report

Write a concise report that answers:

1. What scope and exact revisions governed the Sprint?
2. What was planned, what actually shipped, and which PRs produced it?
3. Which judgments and intentional deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which unresolved items and follow-ups now require FnB disposition?
7. Where can the complete evidence be inspected?

Name discrepancies; do not smooth them into a success narrative. A recovered
stall can be a successful Sprint when the failure stayed durable, visible, and
contained.

## Pause and abort reports

When closing after a pause, include the integrity threat, deterministic state at
pause, interrupt delivery, reconciliation, spec drift, judgment, and resume
outcome. Keep this section behind the pause/recovery evidence seam; missing
optional pause facts must not prevent compiling an otherwise valid packet.

Abort is terminal and history-preserving. Its report names reason, completed
work, partial artifacts, outstanding work, active interruption outcome, and
recovery disposition. A prepared Sprint may abort with a stub report; delete
nothing.

## Terminal handoff

Pass the final synthesis to `complete`; the surface commits the append-only
`final` report before attempting the lifecycle transition. Omitting the report
is permitted under advisory close-out, but the evidence packet records the gap.
Abort only under Planner or FnB authority. Terminal state stops Sprint services
and removes live pills while retaining conversations, messages, events, PR
evidence, reports, and follow-ups.

Immediately before `complete`, re-run `sc sprint inbox --sprint <id>` to drain
newly arrived messages, mark every handled informational message read with
`accept`, and confirm the final report file is the intended synthesis. This is
the last pre-terminal evidence read.

Keep the final report at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` before the typed terminal handoff, then require
the successful report receipt and lifecycle transition.

```text
sc sprint complete --sprint <id> --reason <summary> --outcome <outcome> \
  --report-file <path> --key <stable-key>
sc sprint abort --sprint <id> --reason <reason> [--outcome <outcome>]
```

After `complete` succeeds, emit one bounded final response from its receipt:
final report id, follow-up list, integrated SHA, and evidence links. Run no
further Sprint command; close intent terminalizes the owning conversation and
Sprint-scoped authority is over.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.',
  'workflow',
  NULL,
  0,
  '# sprint_dev — own one editing lane

Use for an actionable work-unit assignment in an armed Sprint. Marking that
Sprint message read is acceptance and starts work immediately. If you cannot
accept, decline with a concrete reason; never leave it unread and waking.
On every wake or re-entry, load `sprint_dev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Orient and bound the lane

Read the assignment, expected output, bound spec revision, dependencies,
assigned Reviewer, repository/worktree, merge grant, and prior judgments. One
Developer shell may own one active work unit in the Sprint. Do not start a
second editing lane or edit another shell''s worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit''s scope, record the choice and rationale, and continue. Escalate changes to
the unit boundary, interfaces another unit consumes, deliverable cuts, or scope
growth to the Planner.

## Questions, answers, blockers, and failures

Write a concrete question, answer, blocker, or useful context to a short body
file, then send it durably to the participant who can act:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Ask the Planner about scope, priority, or cross-unit decisions; ask the assigned
Reviewer about review evidence. Answer an incoming question through `send` so it
wakes the asker, confirm that write, then mark the handled question read with
`accept`. For a blocker or integrity concern, send the Planner concise evidence,
impact, the exact action needed, and your recommendation. Continue safe
independent work, but stop at a decision boundary when the answer is required.
No immediate response is not a reason to send duplicates: the durable message
and recovery reconciler own re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Developer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit''s independent stage gate and realistic
failure paths. A local exploratory number is not merge evidence. Record real CI
failures, anomalous infrastructure failures, retries, review friction, and
known departures for the final report.

An explicitly planned report-only or no-code lane completes with its durable
result instead of a PR. Code lanes cannot use this path; they complete only
after merge authorization and observation.

Keep the result at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submitting, then require a successful command and
durable completion receipt.

```text
sc sprint complete-unit --sprint <id> --work-unit <id> \
  --result-file <path>
```

Register the PR through the authoritative Sprint surface and retain ownership
until it is green. After `register-pr` succeeds, the native registered-PR
watcher supplies red/green facts and their durable wakes. On red, diagnose and
fix the PR. On green, judge readiness rather than forwarding mechanically.

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

When no local implementation action remains, stop and await the native PR-fact
wake. Use Sprint-native wakes for coordination. Do not start a recurring shell
loop, scheduled job, manual watcher daemon, or external PR watcher to track the
registered PR.

## Review handoff

Put the readiness claim in a file, then use one stable retry key:

Keep the readiness claim at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and condense before the typed handoff. The handoff
exists only after the command succeeds and confirms its durable write and wake.

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --readiness-file <path> --key <stable-key>
```

The assigned Reviewer receives an actionable request. After `request-review`
succeeds, stop and await the native verdict wake. A changes-requested verdict
opens a fresh linked fix conversation and makes it current. Apply every
blocking finding, re-establish green, and hand back with a new stable review
key. Record disagreements as judgment; the Planner resolves scope/severity
disputes.

## Merge boundary

Approval alone is stale evidence. Immediately before merge, ask the engine to
re-read live GitHub state and revalidate the armed grant, ownership, work-unit
state, approved head, and checks:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the exact repository, PR number, and head SHA returned. If the
command refuses, do not work around it; wait for the watcher or return to the
appropriate loop. After merge, clean the worktree, submit the unit result and
judgments, and let automatic merge observation advance dependencies.

## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or an unrecoverable environment to the Planner
with evidence, impact, and a recommendation. Stop at the unsafe boundary while
the Planner decides whether to continue, re-plan, or pause.

Stop when the unit is merged and reported, declined, awaiting Planner/FnB
recovery, or returned to review. Before stopping, re-run `sc sprint inbox
--sprint <id>`, act on newly arrived messages, mark every handled informational
message read with `accept`, and confirm the final typed handoff succeeded. Ask
the Planner for later work only after the current editing lane is terminal.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes, respond to durable evidence and escalations, re-plan honestly, and coordinate pause/resume without becoming a transition bottleneck.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; you decide scope, sequencing, and recovery.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

The armed runtime owns scheduled dispatch, unread wake recovery, liveness
evaluation, and registered-PR observation. React to its durable inbox and wake
facts; use the Planner turn for decisions, re-plans, escalation, and close-out.

Read the Sprint inbox, lifecycle, bound spec revisions, work-unit graph,
participant routes, active conversations, registered PRs, unresolved
expectations, and recent anomalies. Viewing a participant conversation is
observation, not activity; never manufacture progress from browser presence.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Release every dependency-ready lane through the production surface:

```text
sc sprint dispatch --sprint <id>
```

The returned ids are wake identities. Work-unit disposition and messages are
the authoritative release facts. Dispatch is safe to repeat: occupied Developer
lanes and stable assignment generations prevent double booking.

Accept or decline only when the inbox item is actionable. After acting on an
informational question, answer, blocker, or evidence message, run `accept` for
that message. For informational messages it only marks the message read; it does
not change Sprint or work-unit state.

## Running loop

- Keep dependencies as the only hard sequence. Re-plan assignments, waves, or
  dependencies when reality changes, but never rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts. Do not proxy routine handoffs.
- Consume passive system facts without waking yourself into every transition.
  Act on decisions, escalations, re-plans, pauses, and terminal synthesis.
- Record scope calls, spec edits, ratified deviations, and their rationale as
  judgment evidence while context is live.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. The running Sprint remains bound to
  its approved revision unless an explicit recorded judgment says otherwise.

The armed runtime evaluates liveness on its five-second pulse. A one-shot
diagnostic/evaluation is available when evidence requires it:

```text
sc sprint monitor --sprint <id>
```

Run `monitor` once for concrete evidence, then return control to native
delivery. It evaluates only due accepted expectations and its
nudge/escalation identities are durable. Use Sprint-native wakes for
coordination. Do not start a recurring shell loop, scheduled job, manual
participant boot, or external PR watcher to track Sprint state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, decision, blocker, or useful context in a short
body file and address the participant who owns the needed fact or action:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`.
For a cross-unit blocker, send evidence, impact, and the exact action needed to
every directly affected participant. Continue safe independent governance, but
stop at a decision boundary when an answer is required. Do not spam duplicates
when no response is immediate; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. When a Developer or Reviewer reports an integrity concern,
evaluate its evidence, impact, and recommendation. Decide whether to continue,
re-plan, or pause. Send any needed participant context before pausing; an active
relay is not available after the lifecycle becomes paused.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

Revise only a still-planned lane; name its complete new projection so the
before/after event is reviewable. Cancel only an unreleased lane, with the
reason retained as its terminal result:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  --developer-shell <id> --reviewer-shell <id> --wave <n> \
  [--depends-on <work-unit-id>] [--output-kind code|report-only|no-code]
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reason>
```

## Escalation judgment

Read the evidence packet before acting: last strong evidence, supporting
signals, unreadable signals, failure identity, nudge history, route/quota state,
and current work facts. A proven failure may justify re-plan, route recovery, or
pause. Ambiguous silence does not justify corrupting a work-unit disposition.

If a Developer or Reviewer declines, preserve the reason, return the lane or
review request to the eligible pool, and issue a fresh assignment identity.
Never edit the declined message into a different instruction.

## Pause and resume

Developer and Reviewer participants report integrity concerns; the Planner or
FnB decides whether to pause. When pause is warranted, transition durably, stop
external Sprint services, persist interrupt intent, preserve every partial
artifact, and retain the judgment and evidence for FnB recovery.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
An exhausted recovery wake is one bounded fallback, not a retry loop. Preserve
the unread message and failed wake as evidence, involve FnB for manual recovery,
and do not create recursive fallbacks.
Drift informs; it never silently blocks resume. Record the exact revision facts
and choose continue, re-plan, or abort.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
sc sprint resume --sprint <id> [--reason <reconciliation-judgment>]
```

Abort only when continuing would be dishonest or unsafe. It is terminal and
deletes nothing.

## Handoffs and stop

Assign ready work in the Developer''s persistent Sprint conversation. Review
outcomes move the Developer to fresh fix/merge conversations automatically;
the next work assignment returns it to the persistent lane.

When all planned delivery work is terminal and merged or explicitly no-code,
re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
every handled informational message is marked read with `accept`, confirm the
final typed transition succeeded, stop dispatching, and hand control to
`sprint_close`. Close-out conformance findings become follow-ups rather than
new editing lanes in this Sprint.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_prep',
  'Prepare and arm a Sprints v2 run — bind reviewed spec revisions, shape work units and dependencies, assign routes and capacity, and refuse arming until the durable plan is eligible.',
  'workflow',
  NULL,
  0,
  '# sprint_prep — declare the riverbed

Use as the owning Planner while a Sprint is `prepared`. Preparation ends at one
atomic arming decision; it does not launch participants piecemeal.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and their qualifying QAQC approvals;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one primary harness/model/effective effort per participant plus eligible
  Planner fallback capacity;
- a committed Sprint merge grant; and
- enough local/GitHub capacity to execute the plan.

The arming transaction creates every participant conversation, the initial
assignment messages and wake intents, and the armed transition together.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, QAQC records, shell roster,
model routes, quota state, repository access, and worktree availability. Record
the exact revision hash you inspected; a title or document id is not a revision.

The Review shell records its verdict against the current exact body through the
authenticated Sprint surface:

```text
sc sprint record-qaqc --document <spec-document-id> --verdict pass \
  [--findings-document <document-id>]
```

Use `fail` until every blocking finding is resolved. A body edit changes the
revision hash and therefore needs a fresh signed record.

Refuse arming when any of these is true:

- a selected current revision lacks Review-shell QAQC approval;
- any Medium-or-higher QAQC finding is unresolved;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint; or
- the merge grant was not committed as part of the final plan.

Deficiencies remain editable in `prepared`. Do not weaken an invariant merely
to get to `armed`; surface the missing fact or capacity to the FnB.

## Shape work, do not script behavior

A work unit is one coherent editing lane and may group related spec tasks. Use
dependencies only for hard prerequisites. Waves express intent and later report
comparison; they do not forbid safe out-of-order completion. Reviews are not
editing lanes.

Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell''s implementation
steps into the durable plan when its role skill and judgment can decide them.

For every participant, record role, route, model, effective effort, persistent
conversation ownership, and fallback facts the plan actually depends on. Never
pretend a native session can resume across harnesses.

Declare the prepared envelope from a JSON array of participant objects, then
add each editing lane from existing spec tasks:

```text
sc sprint declare --feature <feature-id> \
  --spec-approval <approval-id> --participants-file <path> --merge-grant
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

The participant file contains `shell_id`, `role`, and `harness`, with optional
`model`, `effort`, and `route`. FnB may add `--planner-shell <id>` when declaring
for the originating Planner. Keep the Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, re-read the spec revision hashes, QAQC records,
participant capacity, single-armed invariant, repository access, and merge
grant. The final read and durable plan commit belong to the authoritative
arming transaction; external harness and GitHub work occurs after it commits.

Arming succeeds only when the first assignments and wake intents are durable.
A process crash after commit is outbox recovery; a crash before commit exposes
no partial Sprint.

```text
sc sprint arm --sprint <id>
```

After `arm` succeeds, participant pickup belongs to native delivery. The armed
runtime dispatches ready work and wake recovery reconciles unread pickup; the
preparing Planner does not manually boot participants or create a second wake
path.

## Handoff

Once armed, hand control to `sprint_pln` and stop preparation work. Give the FnB
a compact declaration:
Sprint id, feature, exact spec revisions, participants/routes, work-unit graph,
planned waves, merge-grant state, and known accepted risks.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — apply the Medium-and-above gate, record precise verdicts through the authenticated surface, and route conformance findings only to post-Sprint follow-ups.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. Pre-declaration QAQC is a third entry condition,
before a Sprint exists. The evidence differs; independence does not.

## Entry and durable state

Pre-declaration QAQC begins from an explicit Planner or FnB request. Read the
exact current spec document and sign that body directly; there is no Sprint id
or Sprint inbox to inspect yet:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, every review or conformance entry arrives through its
durable wake/inbox. On every wake or re-entry, load `sprint_rev`, inspect the
message, and accept the actionable request before beginning:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or useful context in a short body file
and send it to the participant who can act. Ask the Developer for missing PR
evidence and the Planner for scope or severity decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`. A
blocker or integrity concern goes to the Planner with concise evidence, impact,
the exact action needed, and your recommendation. Continue independent safe
review, but stop at a decision boundary when the answer is required. Do not send
duplicate reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Reviewer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.

## Severity rubric

This skill owns severity. The governing spec intentionally does not.

- **Critical** — active security/authority violation, destructive corruption,
  or a condition that makes continued operation unsafe.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or a loop/recovery path that can silently wedge delivery.
- **Medium** — a concrete correctness or recovery gap likely to bite normal
  use soon, including missing negative enforcement or an unreliable handoff.
- **Low** — bounded cleanup, clarity, test depth, or resilience improvement that
  does not make the delivered behavior wrong now.

During a work-unit review, Critical/Major/Medium block approval; Low is a
report note. During close-out conformance, every severity becomes a follow-up
and none is fixed inside the Sprint.

## Work-unit review

Accept the actionable review request, then inspect the exact bound spec
revision, readiness claim, PR head, diff, checks, tests, relevant runtime
evidence, and prior judgment calls. Review code quality, edge cases/failure
paths, and spec conformance. Trace the real path; do not trust names or PR prose.

Read resolved closure evidence through the authenticated memory surface; no SQL
or mutation is needed. Use the exact form for a cited flag and the scoped form
to audit every resolved flag attached to the feature:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

Findings must state:

- severity and concise title;
- violated behavior or invariant;
- exact code/evidence location;
- a reproducible consequence; and
- the fix boundary, without prescribing unnecessary architecture.

Put the verdict body in a file and record it through the authenticated surface:

Keep the verdict at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submission. The typed review handoff exists only
after the command succeeds and confirms its durable write and Developer wake.

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence, opens the correct fresh Developer conversation, and
resolves the review liveness expectation. Do not message around this surface;
an unrecorded verdict cannot unlock merge.

## Whole-Sprint conformance

Judge integrated `main` against every governing bound revision, plus the exact
recorded mid-Sprint revision facts and ratified judgments. Review the integrated
system, not unit diffs. Classify each requirement as:

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
Write the narrative report and a JSON findings array:

```json
[
  {
    "severity": "Major",
    "title": "Integrated seam diverges",
    "body": "Evidence and consequence.",
    "spec_document_id": 46,
    "work_unit_id": 9
  }
]
```

Then record both atomically:

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission. Require the successful report and
follow-up receipt before stopping.

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --key <stable-pass-key>
```

This creates append-only conformance evidence and pending follow-ups for FnB
disposition. It must not create a fix lane, reopen a completed work unit, or
send findings to a Developer for in-Sprint repair — including Critical ones.
Surface immediate safety risk to the FnB, but preserve the close-out rule.

## Stop

For either mode, re-run `sc sprint inbox --sprint <id>` and act on newly arrived
messages before stopping. For unit review, stop after the durable verdict is
recorded and every handled informational message is marked read with `accept`.
For conformance, also require the report and all findings to replay
idempotently and give the Planner their report/follow-up ids. The typed receipt
completes the handoff; stop until another native wake arrives.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
