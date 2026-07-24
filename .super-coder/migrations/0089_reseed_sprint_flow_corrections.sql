-- 0089 — forward-reseed the sprint skills with the post-Interface flow
-- corrections: merge-surface decomposition, the merge protocol and its
-- SHA-bound verdict carry-over rule, the review mutation bar, idempotent
-- instructions to live workers, record-identifier safety, conformance
-- scope declaration, and board read-back. Keeps existing installations
-- aligned with assets/skills. See spec doc 'Sprint flow audit'.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — run the pre-freeze conformance pass (review shells judge the spec against main), freeze the doc (revoking all scoped authority), and synthesize the sprint report from unit reports + the conformance doc into the fixed skeleton. Wake ops are provider-neutral: arm the binding before the first wake, monitor `sc sprint status`/`alerts`, retry parks as NEW gated batches (never resubmit), close releases bindings and cancels queued wake work. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.',
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
a preference. Keep chains short and the graph wide where the code allows.

**Then check MERGE SURFACE, which is a different question from dependency.**
Predict each unit''s file set and compute the pairwise intersection. Logical
independence does NOT imply merge independence: units that need none of each
other''s code still collide if they edit the same files, and that collision
lands at merge time, after every review is done.

- Empty intersection → genuinely parallel; say so.
- Non-empty → either sequence them, or declare them parallel **with the merge
  protocol and the overlap map attached at kickoff** so reviewers know from the
  start that their verdicts are SHA-bound.
- A file touched by **three or more** units → reconsider the cut, don''t just
  manage the merges.

Record overlap in the board''s `depends on` column. A bare dash means only "no
logical dependency" and is read as "independent" — which is how one sprint
declared five units independent while 21 of their 30 file-touches landed on
nine shared files, three of them touched by three units apiece. The cost was a
merge protocol invented mid-flight, four rebases, two voided verdicts and a
hand-resolved conflict. Surfaces that concentrate a lot of behaviour into a few
large files make this the normal case, not the exception.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don''t let one reviewer become the whole sprint''s
bottleneck).

**How many shells to deploy = your call, not a formula.** Weigh the
magnitude of the push against the capacity actually available — the shells
that exist, reviewer bandwidth, how wide the dependency graph genuinely
runs — and make the call. More units than shells is fine (units queue
behind the chain); more shells than parallel work is waste.

**The model & provider interview — two routine routing questions to the FnB:**

1. **Devs** — which harness and model? One answer; every dev in the
   sprint runs it.
2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

**Billing gate — Plan billing by default; observe, never mutate auth.** NEVER
unset, scrub, replace, or print a credential. Before resolving models, classify
the chosen harness exactly:

```sh
# OpenAI / Codex: exit 0 = plan; 10 = API override; 11 = persisted auth unknown.
(
  if [ -n "${CODEX_API_KEY+x}" ]; then
    echo "billing=api source=CODEX_API_KEY"; exit 10
  fi
  status="$(codex login status 2>&1)"
  if [ "$status" = "Logged in using ChatGPT" ]; then
    echo "billing=plan source=ChatGPT"; exit 0
  fi
  echo "billing=api-or-unknown source=persisted-login"; exit 11
)

# Anthropic / Claude: exit 0 = plan; 10 = API key; 11 = unknown auth.
claude auth status --json 2>/dev/null |
  python3 -c ''import json,sys
try: s=json.load(sys.stdin)
except Exception: print("billing=unknown"); raise SystemExit(11)
key=s.get("apiKeySource"); plan=s.get("loggedIn") and s.get("authMethod") == "claude.ai" and s.get("apiProvider") == "firstParty" and s.get("subscriptionType") and not key
print("billing=plan source=claude.ai" if plan else ("billing=api source=" + str(key) if key else "billing=unknown")); raise SystemExit(0 if plan else (10 if key else 11))''
```

Exit 0 + `billing=plan` -> launch normally. Exit 10 -> hold and ask the FnB to
authorize the metered route. Exit 11 -> hold until the FnB corrects the login or
explicitly authorizes the unknown route. Model/harness selection is not billing
permission.

Ask in the planner turn, then stop before booting the worker:

```
Billing approval required: provider=<openai|anthropic> mode=<api|extra-usage> route=<harness/model> scope=<shell/unit/role/sprint> cap=<amount|provider limit|not specified> expires=<one launch|time|sprint close>. Authorize this metered run?
```

Only an explicit affirmative FnB reply counts. Silence, prior model selection,
or an approval for another provider/scope does not. Default scope = one launch;
broader authority must be stated explicitly.

Record an approval before launching:

```
billing-exception: provider=<openai|anthropic> mode=<api|extra-usage> scope=<role, unit, or whole sprint> cap=<amount or provider limit> expires=<time or sprint close> approved-by=FnB
```

After approval, run the ordinary resolved `./sc run ...` command with the
current environment unchanged; this preserves the credential the FnB approved.
No matching, unexpired approval -> do not launch the metered route.

CLI auth cannot see account-side overage controls. Do not claim Extra Usage was
validated. If the provider reports an included-plan limit or offers paid
continuation, hold and request the same scoped approval. Automatic overage is an
FnB-owned account policy: treat it as permission only when the sprint doc records
its scope/cap/expiry; otherwise the FnB keeps it disabled for plan-only sprints.

`sc models resolve` proves callability, not billing; run it only after this gate.

Flavor-uniform by design: shells of a flavor are interchangeable workers,
and one answer per flavor keeps the board readable and the review lineage
coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). Every sprint worker still runs at high
effort. Per-unit model mixing is out of scope — the interview covers the real
need, provider choice per role.

**Resolve each answered route before declaring it.** Lazy-load only the two
choices the FnB made — never trust a display name or translate a provider id by
hand:

```
sc models resolve <devs-harness> <devs-model>
sc models resolve <reviewers-harness> <reviewers-model>
```

Each must return `route:` plus an exact `call:` ending in `--effort high`.
Failure means the selector is not locally callable, the harness lacks a
headless/high-effort seam, or Refresh models has not seen it. Run
`sc models list <harness>` for the local choices; the FnB''s **Refresh models**
button in `/#shells` repopulates the same runtime table. Resolve again after a
refresh. Never silently fall back across a provider or lineage.

Common exact selectors: Claude aliases (`fable`, `opus`) and Codex ids
(`gpt-5.6-sol`, `gpt-5.6-terra`) pass directly. Kimi takes the configured alias
shown by `sc models list kimi` (for example `kimi-code/k3`), never the bare
provider model `k3`.

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

`depends on` carries BOTH facts: the logical dependency and any file overlap
(`— · shares app.js with 3`). A dash alone asserts independence you may not
have checked.

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

Note the returned `document_id` — every task and report references it —
and embed `SPRINT doc=<id> governing` in your own `current_state`; drop
it at close-out.

You are the doc''s only writer: devs report transitions as `result` rows;
fold them into the board with `sc mem doc edit <id> --body-file`.

**Verify every board edit.** A scripted edit whose pattern has drifted silently
matches nothing and reports success. Assert the target text exists before
replacing, then read the doc back and confirm the fields actually changed. One
sprint reported unit statuses to the FnB for four turns off a board where three
edits had no-op''d — a merged unit still read `building` and a whole row was
missing — until a REVIEWER noticed the board contradicted the SHA in its own task
row. You cannot report from memory and call it the board.

## Step 2: Arm the watcher, kick off

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
the only seat the watcher fully serves.)

**Kick off** — a `task` row per participant (doc id + the instruction to
load the `sprint` skill + the slot), then boot whoever can start:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot; your merge closes with the unit report. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev with the RESOLVED selector; high is invariant:
./sc run <dev> --harness <devs-harness> -m <devs-model> --effort high
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
  board, then move whatever it unblocks. A dev''s merge arrives as its
  **unit report** (the one multi-line `result` row — shipped /
  judgements / issues / deviations / follow-ups): file it whole; it is
  a primary source for the sprint report, and its `deviations` +
  `judgements` lines feed the conformance kickoff. A bare one-line
  `merged` with no report -> nudge the dev (`task` row) for it now,
  while the unit is still in its context.
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

Messages are your steering wheel: a headless boot drains the inbox first
thing, and a dev checks it at each step start. Steer with `task` rows — holds,
re-sequencing, nudges, rulings on reported reds. The board records state;
messages change behavior; on conflict your latest message wins -> then
update the board to match.

**A message to a LIVE worker probably will not land before its next push.** A
long build has few step starts, so a ruling issued mid-build routinely arrives
after the work it was meant to change. Staleness runs BOTH ways: the worker is
also reporting against a snapshot of you that has moved, so it may tell you that
you don''t know something you ruled on half an hour ago.

Phrase instructions to live workers **idempotently** — "if you have not already
X, do X" — and state the **observed facts** they rest on ("main is at X", "the
intersection is empty"), not only the conclusion. A crossed message is then a
no-op or a confirmation rather than an order against reality, and a worker whose
state has moved can re-derive the right action. Three crossed messages in one
sprint cost nothing worse than a CI cycle for exactly that reason — the devs
reasoned from the facts. A fourth, phrased as a bare directive, would have
destroyed a record had it been obeyed literally.

**Never delegate a mutation by an identifier the tool does not take.** Give the
tool''s identifier and the human label together — "close flag_id 141 (SC-144)" —
and prefer mutating your own records yourself. Display names and row ids sit in
different counters that can overlap in range, so a name-only instruction can
resolve to a different real row and destroy it while reporting success.

Dev ambiguity reports (`ambiguity: … → chose …`) get a ruling on
receipt: overrule by `task` row while the unit is still un-merged, or
stay silent and the call stands. Either way log the call + outcome the
moment it arrives — the sprint report lists every one, and calls
reconstructed at close-out from old messages are calls lost.

## Wake operations (Interface-backed planner wake)

Provider-neutral operator workflow for the wake machinery — identical on
every harness (claude / codex / kimi); there are no provider-specific
steps. The operator surfaces are `sc sprint status` / `alerts` / `retry`
and the Interface tab''s Sprint wake panel; both read the same API
projection. None of it is scheduled polling — they are on-demand reads of
durable state, and the events still wake you.

- **Arm before the sprint''s first wake.** Once your Interface chat is
  live, start one arm attempt by generating an attempt nonce once:

  ```sh
  arm_attempt_id="$(python3 -c ''import secrets; print(secrets.token_hex(16))'')"
  ```

  Retain that value until the attempt ends, then arm the binding with the
  required idempotency header:

  ```http
  POST /api/interface/sprint-bindings
  Idempotency-Key: sprint-bind-<sprint-doc-id>-<planner-shell-id>-<arm-attempt-id>

  {"sprint_doc_id": <sprint-doc-id>, "planner_shell_id": <planner-shell-id>}
  ```

  Reuse that exact caller-stable key only for retries of this arm attempt,
  including after an ambiguous transport failure. A successful release or a
  conclusive refusal ends the attempt. Generate a new `arm_attempt_id` for
  every later arm or re-arm; reusing a released attempt''s key would replay its
  released binding and leave the sprint unarmed. Never generate a timestamp or
  random value separately for each transport retry. A shell may arm only
  itself; the operator may arm any planner. Arming is fail-closed: a frozen or
  non-ACTIVE doc, a mandatory-hook gap, or a second ACTIVE binding is refused.
  PR watches registered with `--sprint <doc-id>` ride the binding — an unarmed
  binding means `pr_event` rows arrive but nothing wakes you.
- **Monitor wake status.** `./sc sprint status` shows binding
  armed/released, the sprint doc ACTIVE/frozen, the derived wake state
  (armed/queued/submitting/running/parked), the current batch, the last
  wake outcome, and the park/quarantine reason. The Interface tab''s
  Sprint wake panel on your session shows the same projection.
- **Read the alerts.** `./sc sprint alerts` (+ the Interface alert
  panel) is the ONLY window into wake failures — session-loss,
  delivery_unknown parks, pre-send retries exhausted, quarantine,
  unmanaged-writer. Alerts are deduplicated while open; an open critical
  alert means the loop is NOT healthy no matter how quiet the inbox
  looks. Investigate the alert before concluding a stall is a shell''s
  fault.
- **Retry a park — never resubmit it.** A parked (`delivery_unknown`)
  batch is never sent again: the parking invariant is law.
  `./sc sprint retry --binding <id>` closes the park as audit, returns
  its items to queued, and the coordinator forms a NEW batch that
  re-gates everything (idle, clean composer, quiet, hooks healthy,
  sprint ACTIVE) before a byte moves. When the input frame itself is
  parked, retry needs your verdict on what reached the pane:
  `--outcome delivered|not_delivered`. The Interface panel offers the
  same action (Retry wake / Retry — input landed / Retry — input lost).
- **Quarantine is yours to drain by hand.** An item that survives three
  completed wake turns quarantines and alerts without blocking newer
  work — read that message yourself and act on it; the wake machinery
  deliberately leaves it alone.
- **Close cleanly.** Setting `status: CLOSED` on the board (and the
  freeze after it) releases the binding and cancels queued wake work in
  the same transaction — no orphan armed binding, no stranded queued
  batch survives the close. Messages stay unread; the Interface chat is
  untouched. Verify with `./sc sprint status --all`: every binding of
  the sprint shows released.

## Merge protocol — declare it at kickoff, not at the first collision

Needed whenever any two units share a file. Declare it in the kickoff `task`
rows so reviewers know their verdicts are SHA-bound before they spend a pass.

1. Before merging: rebase onto current `origin/main`, confirm checks green on
   the **rebased** head, report that SHA.
2. After any unit merges: every remaining unit re-rebases and re-confirms.
3. Merge order is review-clean order unless you state otherwise.

**A reviewer verdict is bound to the exact SHA it was given.** The carry-over
rule: a verdict carries when the unit''s **own contribution is diff-identical**
and its hunks are **disjoint** from the incoming content — NOT when the reviewed
files are byte-identical. File-identity conflates "did my reviewed change
survive?" (answered exactly by diff-identity) with "did anything else touch this
file?" (irrelevant), and demanding it forces a full re-review every time two
units touch one file for unrelated reasons.

When either condition fails, re-confirmation is required — because disjointness
is a semantic claim, not a proof — but it is **narrowed to the interference
question**, and the dev supplies the evidence that scopes it. A dev that
hand-resolves a hunk names the line and leaves the mutation round trips to the
reviewer: a hand-resolved hunk is precisely what can silently unpin a test.

Never let a unit merge on a verdict attached to a superseded SHA. Two sprints
have lost cycles to it; in the second, reviewers caught it twice and the planner
missed it both times.

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
  -m <reviewers-model> --effort high`; its inbox holds the review request. Still stuck
  -> reassign the unit to another reviewer. Severity dispute (dev says
  Low, reviewer says Medium) -> rule by message immediately — a chain
  waiting on a classification argument is pure loss. Dispute about what
  the unit *should do* -> FnB.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.
  The bottleneck question in Step 3 is what surfaces a dead link.
- **Re-sequencing**: edit the board + `task` row to *every* affected dev
  with its new slot — a dev acting on a stale slot is worse than a paused
  one.
- **Every worker boot failing at once**: check provider auth and spend
  limits BEFORE debugging the engine — a monthly cap presents as a
  fleet-wide boot failure and costs an hour of misdiagnosis. Pause at a
  clean gate (units green, nothing mid-merge), surface to the FnB (auth
  switch is theirs), resume where the board says you stopped.
- **CI queue clogged at the tail**: a queued verify whose commit a later
  stack head already supersedes is pure queue time — cancel it (`gh run
  cancel`) and let the head''s run stand for the stack. Cancelling
  anything to protect a measurement run is allowed but logged: rationale
  in the board or a `result` row, and re-run the cancelled check after.
  Green means green — cancellation never substitutes for a verdict on
  what still needs one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can''t unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. **Run the conformance pass — before the freeze.** "All units merged"
   and "the spec shipped" are different claims; this is where the second
   one gets checked. Boot review shell(s) — reviewer lineage, the
   sprint''s reviewer harness/model; one shell by default, shard by spec
   section only when the spec genuinely exceeds one context:

   ```
   sc mem message send <reviewer> "SPRINT <doc-id>: conformance pass — spec doc <spec-id>, main @ <merge-sha><, sections <scope> if sharded>. Ratified judgement calls: <list — the only narrative input>. Load the sprint skill (conformance slot)." --kind task
   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model> --effort high
   ```

   The shell judges the spec against the code on `main` — never the
   diffs, never the trail — and files four-way verdicts (`as-specced` /
   `deviated-intentionally` / `deviated-silently` / `unimplemented`) as
   a `CONFORMANCE: <title>` doc + a one-line `result` pointer.

   **Declare the SCOPE before you boot the pass, and name which units it does
   NOT cover.** A pass judges a spec, so it certifies only the units built to
   that spec. Decision-driven units — no spec doc, built from a decision or a
   flag — cannot be judged by it, and a verdict that appears to bless them is a
   false certification. Their bar is their unit reports, their reviewer verdicts
   at exact heads, and the mutation round trips. Put the split in the report''s
   Verdict so freezing cannot be read as certifying everything. Assign the pass
   to a reviewer that did NOT review the unit being certified, and hand over any
   DECLARED deviation up front so the pass judges whether the declaration is
   honest rather than discovering it as a gap.

   **Rule on the findings** — they route like any sprint event:
   - **Major** -> insert a fix unit at the front of the chain under
     still-ACTIVE authority (this is exactly why the pass runs before
     the freeze — a reopened sprint re-grants nothing); re-run the pass
     scoped to the fix when it merges.
   - **Medium** -> your judgment: fix unit now, or defer with the FnB
     told explicitly in the report''s Verdict.
   - **Low** -> Deferred & Follow-ups; never holds the close.
2. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant''s scoped authority ends with it.
3. Message every participant (`task` row): sprint closed, default merge
   gates resume.
4. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
5. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Fixed skeleton — fill it by **reasoning over the unit reports and the
   conformance doc against each other** (a dev''s `deviations: none`
   meeting a `deviated-silently` finding on its unit is exactly what the
   report exists to say), not by pasting either verbatim:

   | Section | Primary source |
   |---|---|
   | `## Verdict` | your synthesis — five-second answer: N units / N PRs, conformance state (conforms / conforms-with-deviations / gaps-found), main green, anything deferred-with-eyes-open |
   | `## Units Shipped` | the board — final table, planned vs. actual order |
   | `## Judgements Made` | unit reports (`judgements:`) + your rulings + severity disputes; every call with its final state |
   | `## Spec Accuracy` | conformance doc — verdict table + findings, cross-checked against unit reports'' `deviations:` |
   | `## Issues Encountered` | unit reports (`issues:`) + the `pr_event`/stall trail — CI fights, anomalous reds, re-scopes, unblocks |
   | `## Deferred & Follow-ups` | unit reports (`follow-ups:`) + reviewers'' Lows + conformance Lows + anything cut — one actionable backlog, the next sprint''s seed list |
   | `## Spec Debt` | judgement calls that should be written back into the spec + places the spec was silent, wrong, or contradictory — the input to the spec-update pass |
   | `## Metrics` (optional) | mechanical from the trail: review cycles per unit, CI reds, boots per shell, planned vs. actual merge order |

   The `kind != ''shell''` message trail remains the in-order backbone;
   the CONFORMANCE doc stays alongside as the report''s evidence trail.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
6. Settle the bookkeeping — close the sprint''s flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- Local long work rides `./sc job` (see the `sprint` skill) — a job''s
  completion is a `result` row like any other wake-up. A hand-rolled
  nohup/poll waiter anywhere in the sprint is a defect: one sprint''s
  hand-rolled waiter carried a self-match bug that masked a dead bench.
- You manage; you never load code. Your context grows at coordination
  density — the workers'' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- The conformance shell files verdicts, never rulings — Major/Medium/Low
  routing stays yours; what the sprint *means* stays the FnB''s.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint',
  'Participant loop for a declared multi-shell sprint — dev, reviewer, or conformance slot. Read your slot from the task message + sprint doc, take your turn when your dependency lands, open your PR and register its watch for the planner, babysit CI while live, pass sprint review (Major/Medium fixed), merge your own PR on green+clean under scoped authority, close your unit with a structured unit-report result row, report every transition as a result row. Conformance slot: judge the spec against main pre-freeze, four-way verdicts. No scheduled polling — the planner and the watcher daemon wake you. Local long work (suites/benches) rides ./sc job, never a harness background task. Wake ops (status, alerts, retry) are provider-neutral reads/recovery on the planner''s binding — a parked batch is never resubmitted, only requeued as a NEW gated batch. Load when a sprint task message names you a participant.',
  'craft',
  NULL,
  0,
  '# sprint — your slot in a coordinated multi-shell push

A sprint = a declared, planner-governed push where shells build dependent
units (B on A, C on B); loop = planner → devs → reviewers → devs → planner,
the shells running the handoffs themselves. This skill is the participant
side: a **dev slot** ("The loop"), a **reviewer slot** ("Reviewer slot"),
or a **conformance slot** ("Conformance slot" — the close-out spec-vs-main
pass). Planner side (declare / monitor / close / report) =
`sprint_orchestration`.
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

**A premise that looks WRONG is reported BEFORE you build it, not at merge.**
A spec, board or ruling can rest on a factual claim about the code that simply
isn''t true. Test it, then say so — do not silently cut the deliverable, and do
not silently ship against a premise you believe false. Both outcomes are
recoverable when the planner hears it early; neither is after merge. One dev
proved a board deliverable could not reach the operator at all and reported it
pre-build, so an invisible feature was replaced rather than shipped. Another
re-verified a planner ruling against the parser source before complying with it
and confirmed it at a level the planner had not checked.

Comply after checking, not instead of checking. A planner instruction phrased as
a bare directive can be wrong about the world — including destructively so, if it
names a record by an identifier that resolves to a different row.

**Resolve a record''s identifier before you mutate it.** Display names and row ids
live in different counters that can overlap in range, so "close SC-144" may
resolve to a row that is not SC-144. Look the row up and read it before writing.
If it is already resolved by another shell, leave it — re-closing overwrites that
shell''s verification notes with yours.

## Local long work — suites, benches, builds

A harness background task is session-scoped: in a headless boot it dies
with the session, silently — "the harness will wake me" is false there.
Never park a suite, bench, build, or watcher on one. Long local work
goes through `./sc job`, two patterns:

- **Fire-and-wake (default):** `./sc job start [--label <x>]
  [--timeout <s>] -- <cmd>` — the job survives your session; completion
  lands in YOUR inbox as a `result` row, and the normal event loop (your
  next boot''s inbox drain) acts on it. If the sprint waits on the
  outcome, report the job id to the planner, then end the turn.
- **Wait-slice (the result decides THIS turn''s next step):**
  `./sc job wait <id>` blocks ≤550s in the foreground — exit 0 =
  finished · 2 = still running. Between slices drain your inbox
  (`sc mem message check`) and act on what landed — a planner hold read
  only after your suite finished was a stale-slot build — then slice
  again.

Set `--timeout` on anything that can wedge: a deadlocked suite becomes
a bounded failure with a completion row, not a four-hour hole in the
sprint.

**Measurements:** a local bench is exploratory only. A perf number that
gates a merge or decides a design is CI-vs-CI on the same runner, in
one run — local numbers die with sessions and double-launches; they
have contaminated a sprint decision before.

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
- drain your inbox once more immediately before pushing — a ruling issued while
  you were building will not have interrupted you, and pushing an approach that
  was already overruled costs a cycle;
- push, open your PR — then, in the SAME step:

```
./sc watch pr <owner/repo> <pr-number> --shell <planner-shortname> --sprint <doc-id>
sc mem message send <planner> "sprint <doc-id>: unit <seq> pr-open — PR #<n>" --kind result
```

The watch is what makes the loop event-driven: the daemon now turns every
CI conclusion, review, and merge on your PR into a `pr_event` row in the
planner''s inbox. Registration is explicit and happens at PR open — a PR
without a watch is invisible to the sprint. Sprint scope is mandatory:
registration without `--sprint`, or against a non-ACTIVE board, fails loudly
instead of creating a dormant watch.

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

**7. Merge on green + clean, file your unit report, hand off.** All
checks green + reviewer declared review-clean + boundary above satisfied.

**If `main` moved since your review, rebase first — and your verdict may not
carry.** Your reviewer''s verdict is bound to the exact SHA it judged. After
rebasing onto current `main`, confirm checks green on the REBASED head and
report that SHA. Then report, per file:

- whether your **own contribution is diff-identical** — compare
  `diff(old-base..old-head)` against `diff(new-base..new-head)` over your `+/-`
  lines, normalised for `index`/`@@` noise. Resolve the bases with
  `git merge-base` rather than assuming a SHA that looks current; a
  non-ancestor base silently folds the other branch''s content into your diff
  and inflates it;
- which reviewed files moved, and **whose content moved them**;
- **disjointness as YOUR READ, not a proof** — say so plainly.

Diff-identical + disjoint -> the verdict carries; say so with the evidence.
Otherwise it does NOT carry: the reviewer re-confirms, narrowed to the
interference question. File-level byte-identity is not the bar — two units can
touch one file for unrelated reasons and leave every contribution line intact.

If you **hand-resolve** any hunk: name the line and your reasoning, and do NOT
re-run the mutation round trips yourself. A hand-resolved hunk is exactly what
can silently unpin a test, so that check belongs to the reviewer — handing over
your own answer to the question you are asking it defeats the point.

Then:

```
gh pr merge <your-pr> --squash --delete-branch
sc mem message send <downstream-shortname> "sprint <doc-id>: unit <seq> merged — your dependency is on main. Your turn."
```

Then close your unit with the **unit report** — your merged-notification
to the planner, grown from one line into ONE structured `result` row,
fixed template:

```
sc mem message send <planner-shortname> "$(cat <<''EOF''
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <what the unit does now, 1-2 lines — the claim, not the diff>
judgements: <ambiguity calls incl. final state (ratified/overruled); ''none''>
issues: <CI reds (real vs anomalous), fix loops, stalls, review friction; ''none''>
deviations: <known departures from the spec''s reading + why; ''none''>
follow-ups: <Lows deferred, TODOs left, cleanup owed; ''none''>
EOF
)" --kind result
```

One report per unit, at merge, mandatory — written NOW, while the unit''s
history is still in your context, never reconstructed later. Every field
answered; `none` is an answer. `deviations` is the honesty field: a
deviation declared here is a judgement for the planner to ratify; the
same deviation found only by the conformance pass is a finding. This is
the one sanctioned multi-line `result` row — transitions stay one-line.

(The daemon also emits the merge to the planner and retires your watch —
the `pr_event` is the wake-up, your unit report is the record; send it
anyway: worker self-reports and daemon ground truth cross-check each
other.) No downstream (last link) -> the planner report is the handoff.
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
2. **Check the head is worth reviewing, BEFORE you spend the pass.** Confirm
   the PR head is the exact SHA you were asked for, that it has not been
   superseded, and that current `main` is an ancestor of it
   (`git merge-base --is-ancestor <main> <head>`). Refuse an unrequested,
   force-pushed-away, or non-CI head and say so in a `result` row instead of
   reviewing it anyway — a verdict on a doomed SHA is a wasted cycle, and green
   checks on a stale base prove nothing about what will merge. Two holds on this
   basis in one sprint each saved a full pass.
3. **Run the mutation yourself.** When a unit''s value rests on one property — an
   ordering, a currency claim, a fail-closed gate — break it in the source,
   watch the test go red, revert, watch it pass. A reported round trip is not a
   verified one. One sprint found FIVE tests that could not fail, every one on
   fully green CI, every one caught this way and none by CI. Ask it per
   PROPERTY, not per test: a test can genuinely constrain the property it names
   while leaving an adjacent one it appears to cover entirely free.
4. **Major/Medium block; Low informs.** Wrong-behavior / data-loss /
   security / spec-violation (Major) or will-bite-soon (Medium) -> the dev
   fixes now; re-review on the fix push. Style / naming / nice-to-have
   refactors (Low) -> one summary note to the planner for the sprint
   report; Low never blocks merge and you don''t re-litigate it.
5. **Handoffs go direct** — scoped relaxation, same shape as the merge
   authority. The base `review` skill gates handoffs behind the FnB;
   inside an ACTIVE sprint, for your assigned units only: message the
   author dev your findings directly + copy the planner one line
   (`unit <seq>: N major, M medium — with <dev>` or `unit <seq>:
   review-clean`), --kind result. The FnB gate is unchanged everywhere
   else and returns the moment the doc freezes.
6. **Clean is a declaration.** Say `review-clean` explicitly to dev +
   planner — it is what unlocks the dev''s merge; never leave it implied.
7. **Stand down** on close-out: drop your SPRINT line, confirm to the
   planner in a final `result` row.

## Conformance slot

The sprint''s final gate: after every unit is merged and `main` is green,
*before* the freeze, the planner boots you to answer the one question no
unit reviewer is positioned to answer — **does what shipped on `main`
actually match the spec?** Unit reviewers gated diffs against unit
scopes; you read the integrated whole. Cross-unit seams — one unit''s
interface drifting from what another assumed, a requirement that fell
between two units — are yours to catch.

1. **Wake = the planner''s kickoff.** Its `task` row carries exactly: the
   spec doc id, the sprint doc id, the merge SHA of `main`, your section
   scope (if the pass is sharded), and the planner''s list of **ratified
   judgement calls**. That list is your only narrative input — it is what
   lets you tell an intentional deviation from a silent one. Everything
   else is artifact: judge the spec against the code on `main` at that
   SHA — never the diffs, never the message trail, never the devs''
   reasoning.
2. **Verdicts.** Every spec requirement in scope gets exactly one:
   - `as-specced` — code matches the spec''s reading;
   - `deviated-intentionally` — matches a ratified judgement call;
   - `deviated-silently` — departs from spec, nobody declared it;
   - `unimplemented` — spec requires it, nothing on `main` does it.
   The last two are findings: attach spec section, code location, and
   Major/Medium/Low — the sprint''s severity bar, same meanings.
3. **Output.** Write a `documents` row — `CONFORMANCE: <sprint title>`,
   kind `doc` (`sc mem doc add`) — holding the verdict table + findings,
   then send the planner ONE line pointing at it:
   `sprint <doc-id>: conformance done — doc <id>, N findings (x Major, y
   Medium, z Low)` (--kind result). Detail in the doc, wake-up in the
   message.
4. **No authority.** You file verdicts; you rule on nothing. Fix units,
   deferrals, and severity disputes are the planner''s; anything that
   changes what the sprint *means* is the FnB''s. Same escalation ladder
   as every other slot.
5. **Stand down** when the planner confirms receipt (a re-run on fix
   units arrives as a fresh scoped `task` row).

## Wake ops (participant view)

The planner''s wake machinery has operator surfaces you can read too —
provider-neutral, identical on every harness: `./sc sprint status`
(binding armed/released, sprint ACTIVE/frozen, batch state, park and
quarantine reasons) and `./sc sprint alerts` (the only window into wake
failures — session-loss, retries exhausted, quarantine,
unmanaged-writer; deduplicated while open). When the loop looks stalled,
check both before reporting a stall: an open critical alert already
names it. Recovery is the planner''s/operator''s action —
`./sc sprint retry --binding <id>` requeues a parked batch as a NEW
gated batch and NEVER resubmits the park — so a parked or quarantined
wake goes to the planner as a `result` row, never a hand-rolled
resubmission of your own.

## Stance

- No scheduled polling, ever: `task` rows and headless boots wake you;
  `pr_event` rows wake the planner; the sprint doc tells you what a wake
  means.
- Nothing that must outlive the turn rides a harness background task —
  local long work goes through `./sc job`; measurement claims are
  CI-vs-CI on one runner.
- Register the watch in the same step that opens the PR — an unwatched PR
  is a silent link, and silent links revert the sprint to polling.
- A parked wake is never resubmitted — retry requeues it as a NEW gated
  batch; parks and quarantines are reported to the planner, never
  worked around.
- Report state transitions (`building → pr-open → in-review → fixing →
  merged`) as `result` rows, one line each — not progress prose. The
  unit report at merge is the one sanctioned multi-line row.
- Merge-on-green+clean and direct review handoffs are scoped authority
  inside a declared sprint, never precedent outside one.
- "All units merged" and "the spec shipped" are different claims — the
  conformance slot exists because only the first is otherwise checked.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
