#116 spec seq 16 · feature 31 — Sprint pickup reliability

---
title: Sprint pickup reliability
tags: [sprints-v2, recovery, harnesses]
date: 2026-08-05
project: super-coder
purpose: Prevent silent Sprint stalls
---

# Sprint pickup reliability

## Objective

Make every armed Sprint handoff converge visibly: a compatible participant turn
accepts the durable message, a bounded recovery wake remains live, or the engine
pauses with an actionable receipt. No assignment, review request, result,
re-entry, or conformance wake may leave a Sprint indefinitely `armed` with no
participant turn, no deliverable wake, and no recovery action.

Done means the dos-app Sprint 5 Kimi failure and kcsos Sprint 2 nullable-route
failure are covered by regression tests and can be recovered through public
lifecycle paths without a database edit or duplicate request. It also means an
unavailable Sprint runtime and an incompatible harness route fail before they can
masquerade as a healthy workflow stage, and a disposable installed dos-app canary
proves the exact candidate engine with real native turns and GitHub delivery.

> [!class1]
> Delivery is not pickup. A queued prompt is transport evidence; progress needs
> a live turn, an accepted Sprint message, a bounded recovery wake, or a durable
> pause receipt.

## Audit evidence

### Live failure

dos-app Sprint 5, work unit 5, PR #65:

| Time UTC | Durable fact |
|---|---|
| 19:05:56 | Review request message 74 created; unit moved to `in_review` |
| 19:06:09 | Wake 42 delivered to a new REV1 Kimi conversation |
| 19:06:22 | Run 93 ended `unknown`, `HARNESS_SESSION_DISCOVERY_FAILED` |
| 19:06:24 | Pickup reconciler created replacement wake 43 |
| 19:06:34 | Replacement delivered to a second REV1 conversation |
| 19:06:44 | Run 94 ended `unknown` with the same adapter error |
| after 19:06:44 | Sprint stayed `armed`; message 74 stayed unread and pending; `monitor` returned no outcome |

The installed Sprint runtime is Kimi 0.33.0, as reported from the launched
sandbox by `sc harness-status`. Its version-2 `state.json` contains an absolute
`cwd` and no `workDir`. The current adapter reads only `workDir`. The official
Kimi session layout still defines `state.json` and `agents/main/wire.jsonl` as
the durable session surfaces; the adapter must tolerate the supported metadata
schema rather than infer success.

Issue: https://github.com/jedbjorn/subfloor/issues/1055

### Default-route failure

kcsos Sprint 2 exposed a separate first-launch defect on the same current engine
revision. Its originating Planner participant declared `harness=codex` with
`model=NULL` and `effort=NULL`, intentionally requesting flavor defaults. After
the active Planner chat closed, wake 35 created a replacement Sprint-scoped chat.
`prepare_wake_conversation()` validated and returned the nullable route, and
`create_prepared_wake_conversation()` persisted those NULLs and hashed them.

At execution, canonical `prepare_launch()` correctly resolved the Planner model
and default Codex headless effort. `ConversationLaunchPreparer` then compared the
resolved values with the conversation's immutable NULL route and failed with
`HARNESS_ROUTE_MISMATCH`. Three turns failed before native execution, including
the initial wake, abort notice, and a later user prompt; no harness session was
created. Engine-wide shell wake creation already canonicalizes these defaults,
but the Sprint-specific path does not.

Issue: https://github.com/jedbjorn/subfloor/issues/1056

### Repeated failure class

This is the second current-floor adapter mismatch with the same Sprint outcome.
Issue #1044 showed Claude Re-enter turns becoming `unknown` after a legitimate
child-directory command. The Claude root defect is fixed, but its recovery
evidence showed the same pattern: delivered wake, poisoned conversation,
automatic replacement, second `unknown`, empty `monitor`, and manual closure of
the exact error chat before delivery could continue.

The relevant current recovery test explicitly preserves this behavior for
non-`SHELL_BUSY` failures: one replacement is created and a second failure is a
no-op. That test describes the observed stall; it is not the desired contract.

### Compatibility gap

All participant adapters expose `minimum_cli_version` and
`verified_cli_version`, but `ConversationAdapter._probe_result` enforces only
the minimum. Sprint arming validates supplied route strings but permits nullable model and
effort defaults. It neither canonicalizes those defaults nor proves that the
harness exists, has an adapter, or falls inside an explicitly supported protocol
range.

| Harness | Manifest verified | Installed runtime |
|---|---:|---:|
| Claude | 2.1.220 | 2.1.222 |
| Codex | 0.145.0 | 0.146.1 |
| OpenCode | 1.18.9 | 1.18.13 |
| Kimi | 0.30.0 | 0.33.0 |

Harness refresh deliberately installs current vendor releases. Without an
upper compatibility boundary, a successful restart can activate a native
protocol the engine has never verified and Sprint arming still reports the
route as eligible.

### Recovery gap

Existing recovery is sound at two edges:

- wake transport failure: three attempts, then atomic system pause;
- `SHELL_BUSY`: bounded backoff through five attempts, then atomic system pause.

The middle edge is incomplete. Once transport queued the conversation prompt,
the outbox is `delivered` even if the native run later fails or becomes
unprovable. Delivered-unread reconciliation creates one generic replacement,
then suppresses every later recovery because the replacement key starts with
`sprint-recovery:`. No liveness expectation exists before the recipient accepts
the Sprint message, so liveness cannot close the gap. Resume resets only a
`SHELL_BUSY` contention episode; it cannot reset this generic exhausted episode.

### Runtime visibility gap

The conversation broker and PR watcher write durable service heartbeats. The
five-second Sprint runtime does not. A deliverable wake can therefore remain
`pending` with zero attempts while the board shows only the work-unit stage.
Issue #1036 records that exact shape on another fork. The board also reduces an
adapter `run.unknown` to the unit's ordinary Review column and projects only the
replacement wake id, not the stable failure code or required recovery action.

## Settled constraints

This work honors the current decisions rather than reopening them:

- **#66:** wake type resolves at delivery; complete coalesced drain remains the
  delivery rule; automatic pause preserves coordinate mode.
- **#67:** Reviewer owns ordinary pause/cancel/conclude judgment and Planner
  executes it. Existing system integrity pauses remain valid; FnB override is
  unchanged.
- **#79:** paused-Sprint ride-along drain remains allowed; do not filter the
  drain by lifecycle.
- **#85:** sanctioned quiet, liveness timing, and CI-stalled backstop remain
  unchanged.
- **#89 and #94:** Force-new remains the sole assignment/review freshness
  mechanism and only undelivered Force-new messages are hidden from inbox
  acceptance.
- **#96:** clean Reviewer conformance approval still completes atomically.

Unknown native execution is conservative: because the prompt may have run, an
`unknown` outcome is never replayed automatically. The engine pauses, preserves
evidence, closes only the exact dead error conversation, and lets an authorized
resume create a new pickup episode after repair.

## Failure contract

| Boundary | Evidence | System action |
|---|---|---|
| Route preflight | canonical route unresolved, adapter absent, binary missing, or version outside declared range | Refuse arming before any lifecycle/message/wake write |
| Wake transport | prompt was not queued | Existing three-attempt backoff, then pause |
| Shell contention | no turn started because the shell slot is busy | Existing five-attempt backoff, then pause |
| Native outcome unknown | prompt may have started; outcome unprovable | No replay; pause immediately with exact evidence |
| Native run failed | terminal failure is proven and Sprint message remains unread | One replacement; pause if replacement also terminalizes unread |
| Turn succeeded but ignored inbox | Sprint message remains unread after terminal turn | One replacement; pause if replacement also terminalizes unread |
| Live pickup turn | queued/running turn can still read the message | Wait; create no duplicate wake |
| Runtime unavailable | pending/delivering work plus missing/stale runtime heartbeat | Surface unhealthy runtime; launch refuses an unstarted runtime |
| Message read | recipient accepted or handled the durable item | Resolve the pickup episode; cancel any unused recovery wake |

Every pause in this table is atomic with a pause report, event, exact
conversation cleanup, Planner/FnB notice, and retained unread messages. No
automatic branch, PR, work-unit, review, or message disposition is inferred.

## Unit 1 — Kimi v2 session state

Change `conversation_adapters/kimi.py` so `_state_worktree` recognizes both
supported schemas:

- legacy non-empty absolute `workDir`;
- version-2 non-empty absolute `cwd`.

If both fields exist, resolve both and require equality. Reject missing,
non-string, empty, relative, conflicting, or unreadable values with
`HARNESS_SESSION_INSPECTION_FAILED`. Keep exact resolved-path comparison to the
assigned worktree and keep `agents/main/wire.jsonl` as the only run-evidence
stream. Do not guess a worktree from the directory hash.

Update the Kimi manifest and capability fixture from a real launched-runtime
probe. The verified floor must name Kimi 0.33.0 and the supported range must
include the legacy 0.30 format and the proven 0.33 format while refusing the
next unverified protocol interval.

Verification covers legacy-only, v2-only, equal dual-field, conflicting fields,
relative paths, malformed JSON, new-turn discovery, resume, inspect, reconcile,
interrupt, and exact prompt-slice behavior. A sanitized v2 fixture must match
the observed key shape; no live user session file is committed.

## Unit 2 — Canonical routes and compatibility

### Manifest contract

Each browser-conversation adapter declares an explicit closed-open compatibility
range alongside its verified version. The upper bound is adapter-authored, not
derived mechanically from SemVer; vendor versioning policies differ and a minor
release may change persistence or wire protocols.

`ProbeResult` returns installed version, minimum, exclusive maximum, verified
version, and compatibility state. `ConversationAdapter._probe_result` refuses a
version below the minimum or at/above the exclusive maximum with stable
`HARNESS_VERSION_UNSUPPORTED` evidence naming harness, installed version, and
supported range.

### Canonical route snapshot

Resolve every participant route before constructing `PreparedSprintWake`, hashing
the creation request, or inserting a conversation. Use one shared headless-route
resolver with the same semantics as canonical launch preparation:

- a missing model resolves from the shell flavor's model for the selected harness;
- a missing effort resolves from `default_headless_effort(adapter)`;
- provider derives from the resolved harness and model;
- explicit model and effort remain byte-for-byte unchanged after validation.

Persist and hash only the resolved harness, provider, model, effort, and worktree.
Later launch preparation is an equality assertion over that canonical snapshot,
not the first place defaults are applied. Engine-wide and Sprint wake creation
must call the same resolver rather than maintain parallel fallback logic. A route
that cannot be fully resolved fails before any chat is created.

### Arm boundary

Before `arm` opens its write transaction:

1. Read one immutable snapshot of the prepared participant launch selections.
2. Probe each distinct harness once in the actual server runtime.
3. Reject an unknown adapter, missing binary, failed version probe, or
   out-of-range version.
4. Open the arm transaction, re-read the plan, and require the exact selection
   fingerprint to match the preflight snapshot before lifecycle/message/wake
   writes.

No subprocess runs while the engine holds a database write transaction. A plan
changed during probing returns HTTP 409 and may be retried from a fresh
snapshot. Failed preflight leaves the Sprint `prepared` with zero new wakes.

`sc harness-status` reports runtime provenance and compatibility together, so
the sandbox/host distinction cannot be inferred from a separate host binary.
Normal browser conversations and Sprint arming use the same adapter probe;
Sprints refuse unsupported routes rather than accepting a known-unverified
native protocol.

## Unit 3 — Pickup exhaustion state machine

Replace the non-busy one-shot suppression branch in
`SprintLifecycleStore._reconcile_unread_wakes_in_transaction` with a complete
episode state machine over existing durable rows.

### Classification

For each relevant unread Sprint message attached to a terminal wake, inspect
the latest conversation message and run:

- `turn_live=true`: no-op;
- `run_state=unknown`: exhaust immediately, without replacement;
- first proven failed or succeeded-but-unread turn: create/adopt one replacement;
- terminal replacement with the message still unread: exhaust;
- `SHELL_BUSY`: retain the existing distinct contention chain;
- missing or contradictory attempt/run evidence: exhaust as an integrity
  anomaly rather than guessing.

### Exhaustion transition

Exhaustion atomically:

- emits `wake.pickup_exhausted` with Sprint, participant, shell, role,
  work-unit/message/wake ids, conversation id, run state, stable error code,
  failure class, and attempt count;
- pauses with reason `wake_pickup_unknown`, `wake_pickup_failed`,
  `wake_pickup_unread`, or `wake_pickup_evidence_invalid`;
- stores the same bounded facts in the pause report;
- closes only the exact participant conversation when it is `error`/terminal
  and has no verified live process, preserving its transcript;
- leaves every Sprint message unread and actionable;
- sends one actionable Planner/FnB notice through the existing pause path.

Raw adapter detail remains in the internal pause report but is bounded and is
not used as an idempotency key. Stable ids and error codes drive deduplication.
Repeated pulses after the pause write nothing.

## Unit 4 — Resume and legacy recovery

Generalize contention-only resume reset into pickup-episode reset. On an
authorized resume, for each exhausted participant episode whose messages remain
unread:

- preserve the terminal wake, attempts, run, events, and closed conversation;
- create or adopt one new participant wake with a fresh resume-episode key;
- move the unread message mappings to that wake and clear their transport
  delivery stamp for the new episode;
- coalesce multiple unread messages for the same receiver into one deliverable
  wake;
- honor each message's declared type at delivery;
- report the new wake ids in `lifecycle.reconciled` and the resume receipt.

Messages handled while paused are not requeued. Repeating the same resume is a
no-op. If the repaired route fails again, it creates a new bounded episode and
pauses again; it never recurses indefinitely.

Startup and the normal five-second pulse must recognize legacy current-floor
inert state: an armed Sprint with an unread message attached to an exhausted
generic recovery wake and no live pickup turn. The first reconciliation after
update converts it into the new atomic pause. This is the migration path for
dos-app Sprint 5; no direct row rewrite or data migration is needed.

## Unit 5 — Runtime and operator visibility

### Runtime heartbeat

Reuse `daemon_heartbeats` with the name `sprint-runtime`. Write a heartbeat only
after a successful startup/pulse cycle completes pickup reconciliation,
dependency dispatch, liveness evaluation, and wake delivery. A failing cycle
must not refresh a healthy-looking timestamp.

Server startup waits for the runtime's first successful cycle and refuses to
advertise a ready service if the thread never starts or dies. Live state uses a
documented threshold derived from the recorded interval. The PR watcher and
conversation broker heartbeat contracts remain unchanged.

### Monitor response

Keep `POST /_sc/sprint/monitor` and its existing `outcomes` array. Add fields
backward-compatibly:

```json
{
  "outcomes": [],
  "pickup": {
    "action": "none|requeued|paused",
    "requeued_wake_ids": [],
    "pause_reason": null
  },
  "runtime": {
    "state": "live|stale|missing",
    "beat_at": null,
    "interval_seconds": 5
  }
}
```

The command remains idempotent. It may perform the same pickup reconciliation
as the native pulse; it does not accept messages, adjudicate work, or deliver a
second copy. A pause result is returned in the same 200 response because the
requested evaluation succeeded.

### Board projection

Add runtime health and current recovery state to the existing Sprint detail
projection; do not add a second lifecycle endpoint. An exhausted pickup shows
the stable error code, affected shell/role, work unit, message, wake, attempts,
and the recovery instruction. The timeline projection includes the bounded
`wake.pickup_exhausted` fields. It does not reduce this state to the ordinary
Dev/Review column or require opening the participant transcript to learn the
cause.

## Verification gate

### Adapter and preflight

- Kimi 0.30 legacy and 0.33 v2 state fixtures pass start/resume/inspect/reconcile.
- Conflicting or relative state fields fail conservatively.
- Every adapter range accepts its verified/current test version and rejects the
  exact lower/upper boundaries correctly.
- Missing/unknown/out-of-range harnesses refuse arm before writes.
- A participant-selection race between probe and arm returns 409 with no
  lifecycle or wake mutation.
- `harness-status` proves the runtime binary, not a host-side executable.
- A nullable Planner model/effort is resolved before request hashing and storage;
  the replacement chat's first turn cannot self-fail `HARNESS_ROUTE_MISMATCH`.
- Explicit participant model/effort values remain unchanged.
- Closing the originating Planner chat and delivering its Re-enter wake exercises
  the same canonical route as an already-open Planner chat.

### Pickup and recovery

- Unknown first turn pauses immediately and creates no replacement.
- Proven first failure creates exactly one replacement; second failure pauses.
- Successful turns that never read the inbox receive one replacement; second
  terminal unread turn pauses.
- Live queued/running turn prevents duplicate recovery.
- `SHELL_BUSY` and transport exhaustion retain their existing budgets.
- Pause closes only the exact dead error chat and preserves transcript/events.
- Authorized resume creates exactly one new wake per affected participant;
  repeated resume and repeated pulses are no-ops.
- A read message before resume is not requeued.
- Existing complete-drain, Force-new gate, coordinate mode, authority split,
  sanctioned quiet, and automatic conformance tests remain green.

### Runtime and UI

- Successful pulses advance `sprint-runtime` heartbeat; failed pulses do not.
- Missing/stale runtime is visible in monitor and board projections.
- Pending zero-attempt wake plus stale runtime is not presented as an ordinary
  healthy waiting stage.
- Monitor reports requeue/pause action even when liveness `outcomes` is empty.
- UI/API tests prove stable bounded evidence without raw stack traces.

### Installed-fork canary

The existing `test_sprint_live_proof.py` and cross-harness release gate remain the
fast compositional tests, but they use temporary databases, a fake GitHub reader,
and deterministic adapter doubles. They do not constitute native Sprint proof.

Add one source-repo-only, FnB-invoked unattended canary command as the
downstream-promotion gate for an exact engine revision. It must:

1. Create a disposable copy of dos-app at its current base revision with an
   isolated engine database, runtime namespace, ports, and worktrees. Never use
   the live dos-app Sprint database.
2. Materialize the candidate engine revision and report the launched sandbox's
   adapter/harness versions before arming. Host binaries are not evidence.
3. Create one canary feature, reviewed spec, and task only in the disposable DB.
   Declare Planner Codex with nullable model/effort, Developer Codex, and Reviewer
   Kimi so both #1056 default resolution and #1055 Kimi v2 discovery are exercised.
4. Start inside the originating Planner chat, arm, then close that chat to force a
   fresh Planner Re-enter conversation. Require its first run to accept the
   durable message with a canonical non-null route.
5. Let the real Developer create a deterministic canary file on an ephemeral head
   branch and open a real PR against an ephemeral base branch in dos-app. Neither
   branch may target or merge into `main`.
6. Require actual assignment pickup, PR registration and green observation,
   Force-new Kimi review pickup, verdict, merge authorization, merge into the
   ephemeral base, merge observation, conformance, and `lifecycle.completed`. No
   synthetic message-read, run, review, or PR-state rows may be inserted.
7. Enforce per-stage and whole-run deadlines. On failure, stop advancing and emit
   a sanitized receipt containing engine ref, launched harness versions, canonical
   participant routes, runtime heartbeat, timeline, messages, wakes, conversations,
   run states/error codes, PR identity, stage durations, and the exact next action.
8. Close/delete leftover canary PR and remote branches, stop the isolated runtime,
   and remove its temporary state. Cleanup failure makes the canary fail. Preserve
   the bounded receipt outside the disposable install.

This command lives in subfloor maintainer tooling outside `ENGINE_PATHS`; it is
not a distributed `sc` verb, is never materialized into downstream forks, and
controls the disposable fork from the source checkout. It runs only on an
FnB-controlled host after merge and before updating the real dos-app install. It is never triggered by a public pull
request and never executes untrusted PR code on a credentialed house runner. A candidate is not
promoted to dos-app until its exact-ref receipt is green.

### Canary risks

- Missing native credentials, unavailable harness binaries, dirty target state,
  active live dos-app Sprints, or remote-branch name collisions fail preflight
  before the disposable DB or remote branches are created.
- GitHub API/CI unavailability is reported as `canary_infrastructure_failed`,
  distinct from a Sprint lifecycle, adapter, or participant failure.
- Agent duration and token use are bounded by the minimal deterministic task,
  per-stage deadlines, and one whole-run deadline; timeout is a failed receipt,
  never an indefinite wait.
- Every resource uses a run id. Cleanup is idempotent and can be rerun from the
  receipt without guessing which process, directory, PR, or branch belongs to it.
- Secrets, full prompts, model reasoning, and native session bodies are excluded
  from receipts; exact ids, stable codes, versions, timings, and bounded event
  evidence remain sufficient for diagnosis.

### End to end

The automated canary is the happy-path release proof. Focused fault-injection tests
then prove `unknown` pause/no-replay and bounded failed/unread replacement behavior.
After the candidate is installed in dos-app, recover Sprint 5 through the same
public update, automatic-pause, repair, and resume path.

The gate fails if any nonterminal state requires SQLite edits, an out-of-band
participant boot, a duplicate `request-review`, inference from narration, manual
stage advancement, or a change to dos-app `main`.

## Delivery plan

| Unit | Change surface | Dependency | Verification |
|---|---|---|---|
| 1. Kimi v2 compatibility | Kimi adapter, manifest, capability/test fixtures | none | focused adapter suite + host smoke |
| 2. Route canonicalization and harness preflight | shared route resolver, Sprint chats, adapter registry, arm service/API, harness status | none; integrates Unit 1 range | nullable/explicit route + probe/race/API tests |
| 3. Pickup exhaustion | Sprint domain, pause evidence, recovery tests | settled failure contract | state-machine tests |
| 4. Resume and legacy repair | Sprint domain resume/startup reconciliation | Unit 3 | resume/idempotency/current-floor fixture |
| 5. Runtime visibility | runtime heartbeat, monitor, board API/UI | Unit 3 receipt shape | runtime/API/UI tests |
| 6. Installed-fork canary | source-only maintainer controller, isolated dos-app install, real native turns and ephemeral-base GitHub PR | Units 1–5 | unattended exact-ref receipt + live Sprint 5 recovery |

Units 1 and 3 can run in parallel. Unit 2 can begin in parallel after the range
shape is agreed. Unit 4 depends on Unit 3. Unit 5 depends on the Unit 3 receipt
shape. Unit 6 follows Units 1–5 and is the installed cross-contract promotion
gate; hermetic CI remains required but cannot substitute for its native receipt.

## Explicit exclusions

This spec records but does not absorb unrelated open defects:

- database lock contention and client timeout/idempotency (#331);
- PR check-set truth and poll transport semantics (#955 and existing watcher
  flags);
- model-scoped quota interpretation (#1029);
- stale recovery narration versus actual lifecycle (#1052);
- Sprint-message sent/history read surface (flag 204);
- conformance reviewer election, exclusivity, plan lock, and completion gate
  (flags 207–210);
- retired Sprint-v1 binding/composer issues (#638, #678, #683).

Those defects keep their own evidence and ownership. This build guarantees that
when one of them causes participant pickup to fail, the current Sprint becomes
actionably paused or visibly unhealthy instead of silently inert.

## Anticipated User Activity

### Vocabulary

**Shell** means a Planner, Developer, or Reviewer participating in a Sprint.
**System** means the adapter, broker, Sprint runtime, recovery reconciler, and
board projection. **Valid Privileged User** means the FnB supervising through
the local UI or CLI. **Unexpected Participant** means a shell or caller outside
the named Sprint and its granted role.

### Expected Activity

Shells receive and accept only their durable Sprint messages. A Shell does not
repair rows or repeat an uncertain action. System validates native compatibility,
delivers one bounded pickup episode, pauses on uncertainty/exhaustion, and
preserves evidence. The Valid Privileged User inspects the receipt, repairs the
named route or service, and authorizes resume.

### Reach

Existing browser-conversation creation, `sc harness-status`, Sprint arm,
runtime pulse, `sc sprint monitor`, pause/resume, and the Sprints board are
altered. No public network surface or product application database is added.

### Data Tenancy

All session, wake, run, report, heartbeat, and event evidence remains inside one
install's engine database and native harness state home. Board/API reads retain
their existing operator or participant scope.

### Beyond Intention

No Shell may bypass an unsupported adapter, accept another participant's
message, replay an unproven native action, resume a Sprint without existing
authority, or use a host binary as proof of the launched runtime. Raw database
repair and silent deletion of failed conversations remain outside intention.

## Open questions

None blocking. Defaults chosen for review:

- `unknown` pauses immediately with no automatic replay;
- an installed version outside the adapter-authored range refuses Sprint arm;
- compatibility probing occurs outside the arm write transaction and is
  revalidated by an exact selection fingerprint;
- this spec owns the inert-state spine only; the explicit exclusions remain
  separate work.






