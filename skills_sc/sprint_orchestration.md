---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# sprint_orchestration

Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — run the pre-freeze conformance pass (review shells judge the spec against main), freeze the doc (revoking all scoped authority), and synthesize the sprint report from unit reports + the conformance doc into the fixed skeleton. Wake ops are provider-neutral: arm the binding before the first wake, monitor `sc sprint status`/`alerts`, retry parks as NEW gated batches (never resubmit), close releases bindings and cancels queued wake work. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.

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
  python3 -c 'import json,sys
try: s=json.load(sys.stdin)
except Exception: print("billing=unknown"); raise SystemExit(11)
key=s.get("apiKeySource"); plan=s.get("loggedIn") and s.get("authMethod") == "claude.ai" and s.get("apiProvider") == "firstParty" and s.get("subscriptionType") and not key
print("billing=plan source=claude.ai" if plan else ("billing=api source=" + str(key) if key else "billing=unknown")); raise SystemExit(0 if plan else (10 if key else 11))'
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
`sc models list <harness>` for the local choices; the FnB's **Refresh models**
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

**Interactive sessions only.** A harness background task is
session-scoped: in a headless (`-p`) boot it dies with the session,
silently — six sprint stalls traced to exactly this. A headless planner
turn arms nothing: drain the inbox, act, end the turn — the next event
row boots you again. The watcher belongs to the long-lived interactive
planner seat, nowhere else.

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
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell'> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot; your merge closes with the unit report. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev with the RESOLVED selector; high is invariant:
./sc run <dev> --harness <devs-harness> -m <devs-model> --effort high
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
  board, then move whatever it unblocks. A dev's merge arrives as its
  **unit report** (the one multi-line `result` row — shipped /
  judgements / issues / deviations / follow-ups): file it whole; it is
  a primary source for the sprint report, and its `deviations` +
  `judgements` lines feed the conformance kickoff. A bare one-line
  `merged` with no report -> nudge the dev (`task` row) for it now,
  while the unit is still in its context.
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

## Wake operations (Interface-backed planner wake)

Provider-neutral operator workflow for the wake machinery — identical on
every harness (claude / codex / kimi); there are no provider-specific
steps. The operator surfaces are `sc sprint status` / `alerts` / `retry`
and the Interface tab's Sprint wake panel; both read the same API
projection. None of it is scheduled polling — they are on-demand reads of
durable state, and the events still wake you.

- **Arm before the sprint's first wake.** Once your Interface chat is
  live, arm the binding: `POST /api/interface/sprint-bindings` with
  `sprint_doc_id` + `planner_shell_id` (a shell may arm only itself; the
  operator may arm any planner). Arming is fail-closed: a frozen or
  non-ACTIVE doc, a mandatory-hook gap, or a second ACTIVE binding is
  refused. PR watches registered with `--sprint <doc-id>` ride the
  binding — an unarmed binding means `pr_event` rows arrive but nothing
  wakes you.
- **Monitor wake status.** `./sc sprint status` shows binding
  armed/released, the sprint doc ACTIVE/frozen, the derived wake state
  (armed/queued/submitting/running/parked), the current batch, the last
  wake outcome, and the park/quarantine reason. The Interface tab's
  Sprint wake panel on your session shows the same projection.
- **Read the alerts.** `./sc sprint alerts` (+ the Interface alert
  panel) is the ONLY window into wake failures — session-loss,
  delivery_unknown parks, pre-send retries exhausted, quarantine,
  unmanaged-writer. Alerts are deduplicated while open; an open critical
  alert means the loop is NOT healthy no matter how quiet the inbox
  looks. Investigate the alert before concluding a stall is a shell's
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
  -m <reviewers-model> --effort high`; its inbox holds the review request. Still stuck
  -> reassign the unit to another reviewer. Severity dispute (dev says
  Low, reviewer says Medium) -> rule by message immediately — a chain
  waiting on a classification argument is pure loss. Dispute about what
  the unit *should do* -> FnB.
- **Worker faulted mid-task** (rate-limit cutoff, provider error, session
  died): its `task` row is already consumed — a worker marks the row read
  when it starts acting, so a fault leaves a read row and an unfinished
  unit. Re-launching alone drains an empty inbox and the worker idles on
  the default prompt. **Confirm the row's state at runtime before you
  boot** — `sc mem message sent` carries read receipts; a task row showing
  read means re-send it (same unit, plus where the work stopped and what
  is already on the branch), *then* `./sc run`. A re-boot is not a
  re-task.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. Check `sent` first, though — a read task row
  means the link faulted rather than stalled, and the boot has nothing to
  act on. The liveness guard refusing (session already
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
  cancel`) and let the head's run stand for the stack. Cancelling
  anything to protect a measurement run is allowed but logged: rationale
  in the board or a `result` row, and re-run the cancelled check after.
  Green means green — cancellation never substitutes for a verdict on
  what still needs one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can't unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. **Run the conformance pass — before the freeze.** "All units merged"
   and "the spec shipped" are different claims; this is where the second
   one gets checked. Boot review shell(s) — reviewer lineage, the
   sprint's reviewer harness/model; one shell by default, shard by spec
   section only when the spec genuinely exceeds one context:

   ```
   sc mem message send <reviewer> "SPRINT <doc-id>: conformance pass — spec doc <spec-id>, main @ <merge-sha><, sections <scope> if sharded>. Ratified judgement calls: <list — the only narrative input>. Load the sprint skill (conformance slot)." --kind task
   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model> --effort high
   ```

   The shell judges the spec against the code on `main` — never the
   diffs, never the trail — and files four-way verdicts (`as-specced` /
   `deviated-intentionally` / `deviated-silently` / `unimplemented`) as
   a `CONFORMANCE: <title>` doc + a one-line `result` pointer.

   **Rule on the findings** — they route like any sprint event:
   - **Major** -> insert a fix unit at the front of the chain under
     still-ACTIVE authority (this is exactly why the pass runs before
     the freeze — a reopened sprint re-grants nothing); re-run the pass
     scoped to the fix when it merges.
   - **Medium** -> your judgment: fix unit now, or defer with the FnB
     told explicitly in the report's Verdict.
   - **Low** -> Deferred & Follow-ups; never holds the close.
2. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant's scoped authority ends with it.
3. Message every participant (`task` row): sprint closed, default merge
   gates resume.
4. Verify the watches are gone: `./sc watch list` — every sprint PR's
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don't leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
5. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Fixed skeleton — fill it by **reasoning over the unit reports and the
   conformance doc against each other** (a dev's `deviations: none`
   meeting a `deviated-silently` finding on its unit is exactly what the
   report exists to say), not by pasting either verbatim:

   | Section | Primary source |
   |---|---|
   | `## Verdict` | your synthesis — five-second answer: N units / N PRs, conformance state (conforms / conforms-with-deviations / gaps-found), main green, anything deferred-with-eyes-open |
   | `## Units Shipped` | the board — final table, planned vs. actual order |
   | `## Judgements Made` | unit reports (`judgements:`) + your rulings + severity disputes; every call with its final state |
   | `## Spec Accuracy` | conformance doc — verdict table + findings, cross-checked against unit reports' `deviations:` |
   | `## Issues Encountered` | unit reports (`issues:`) + the `pr_event`/stall trail — CI fights, anomalous reds, re-scopes, unblocks |
   | `## Deferred & Follow-ups` | unit reports (`follow-ups:`) + reviewers' Lows + conformance Lows + anything cut — one actionable backlog, the next sprint's seed list |
   | `## Spec Debt` | judgement calls that should be written back into the spec + places the spec was silent, wrong, or contradictory — the input to the spec-update pass |
   | `## Metrics` (optional) | mechanical from the trail: review cycles per unit, CI reds, boots per shell, planned vs. actual merge order |

   The `kind != 'shell'` message trail remains the in-order backbone;
   the CONFORMANCE doc stays alongside as the report's evidence trail.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
6. Settle the bookkeeping — close the sprint's flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- Local long work rides `./sc job` (see the `sprint` skill) — a job's
  completion is a `result` row like any other wake-up. A hand-rolled
  nohup/poll waiter anywhere in the sprint is a defect: one sprint's
  hand-rolled waiter carried a self-match bug that masked a dead bench.
- You manage; you never load code. Your context grows at coordination
  density — the workers' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- The conformance shell files verdicts, never rulings — Major/Medium/Low
  routing stays yours; what the sprint *means* stays the FnB's.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB's.
