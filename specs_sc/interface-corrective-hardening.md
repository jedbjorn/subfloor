---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Interface chats and interactive planner wake
roadmap_status: in_progress
frozen: false
title: Interface corrective hardening
tags: [interface, tmux, sprint, admin, recovery, qaqc]
date: 2026-07-23
project: super-coder
purpose: Close AMI runtime findings
---

# Interface corrective hardening

## Objective

Harden the merged Interface and sprint-orchestration floor against every finding
from AMI's real-fork QA pass at engine `10d1bdd`. Feature #14 remains unfrozen
until all sixteen open findings are fixed, their exact reproductions pass on
AMI's restricted Admin seat, and one cross-harness sprint completes without
manual database repair or an undocumented recovery command.

The existing architecture remains: one API-managed tmux generation per shell,
one ordered input broker, browser and CLI clients over the same API, and durable
sprint work in engine tables. This stage repairs lifecycle convergence,
persistence boundaries, host-seat operability, and operator truth. It does not
replace tmux, add provider resume, or create a second control plane.

> [!class4]
> A green hermetic suite alone does not close an AMI finding. Each issue needs a
> regression test plus its reported host or real-tmux workflow rerun.

## Scope

| Track | Findings | Required outcome |
|---|---|---|
| Lifecycle correctness | [#519](https://github.com/jedbjorn/subfloor/issues/519), [#523](https://github.com/jedbjorn/subfloor/issues/523), [#532](https://github.com/jedbjorn/subfloor/issues/532) | Cancel stuck starts, converge concurrent end signals, and return truthful errors |
| Client state truth | [#522](https://github.com/jedbjorn/subfloor/issues/522), [#527](https://github.com/jedbjorn/subfloor/issues/527), [#534](https://github.com/jedbjorn/subfloor/issues/534), [#535](https://github.com/jedbjorn/subfloor/issues/535) | Attach, controls, alerts, and model defaults follow exact lifecycle, identity, generation, and catalogue state |
| Update and snapshot | [#528](https://github.com/jedbjorn/subfloor/issues/528), [#529](https://github.com/jedbjorn/subfloor/issues/529), [#533](https://github.com/jedbjorn/subfloor/issues/533) | Refusal is pre-mutation, ended rows do not block, snapshots preserve referential closure |
| Admin API and CLI | [#516](https://github.com/jedbjorn/subfloor/issues/516), [#518](https://github.com/jedbjorn/subfloor/issues/518) | Host Admin memory and HTTP-only Interface commands work without hidden dependencies |
| Restricted supervision | [#530](https://github.com/jedbjorn/subfloor/issues/530), [#531](https://github.com/jedbjorn/subfloor/issues/531) | Backup and launch work when home and Docker config are read-only |
| Diagnostics and map | [#517](https://github.com/jedbjorn/subfloor/issues/517), [#524](https://github.com/jedbjorn/subfloor/issues/524) | Cleanup never claims unproved safety; linked worktrees never enter the repo map |
| Operator workflow | FnB corrective additions | Restore the rich terminal launcher, expose the supported command surface through Make, and make browser sign-in practical |
| Stranded-shell recovery | Roadmap #22 and flag #38 | Browser and CLI inspect and safely release stale locks or exact orphan processes without direct database or process manipulation |

Closed runtime fixes [#520](https://github.com/jedbjorn/subfloor/issues/520),
[#521](https://github.com/jedbjorn/subfloor/issues/521), and
[#525](https://github.com/jedbjorn/subfloor/issues/525), plus merged worktree
provisioning [#526](https://github.com/jedbjorn/subfloor/pull/526), are the
baseline. Their tests remain green and their real-runtime paths join the final
matrix. The three low follow-ups from #526 are also in scope: a reservation race
returns the existing owner as `409`, provisioning failures curate all expected
launcher exceptions, and path validation distinguishes missing, non-directory,
and unusable worktrees.

## Requirements

1. Session termination is a convergent operation. An operator request, provider
   `session_end` hook, pane exit, startup reconciliation, and a repeated request
   may race in any order; once process absence is proved they produce one ended
   session, one ended generation, revoked leases, terminal input state, and an
   idempotent success response.
2. A reserved generation can be cancelled. If no pane or harness identity was
   established, cancellation records `cancelled_before_spawn` and makes the
   shell available. If identity is live, the normal verified stop path runs. If
   spawn outcome is uncertain, the session becomes unreconciled and requires
   absence proof; it is never silently ended.
3. No termination path performs a terminal-to-nonterminal lifecycle transition.
   A hook that wins the race cannot strand `occupancy=occupied` with
   `lifecycle=ended`; the losing operation observes the terminal result and
   completes idempotently.
4. Route parsing errors, state conflicts, and unexpected handler failures remain
   distinct. Bad path identifiers return `404` or `422`; legal-route state
   conflicts return `409` with a stable code; unexpected failures return a
   sanitized `500` with a server-side correlation record. A broad `ValueError`
   handler never converts internal failures into `no_such_route`.
5. `sc enter`, browser attachment, writer acquisition, certification, takeover,
   and termination controls require a compatible occupancy/lifecycle pair and
   verified live identity. Cached output from an ended or identity-missing pane
   is never presented as a writable terminal.
6. The browser treats only the exact writer-conflict response as another client
   holding control. A reserved, ended, lost, or unreconciled generation displays
   its own state and only actions legal for that state.
7. `sc update` evaluates the current engine's live-state refusal guard before
   changing `sc`, `.super-coder/`, workflow files, `.gitignore`,
   `engine.ref.prev`, or `engine.ref`. A refusal leaves a byte-for-byte unchanged
   working floor. Remote fetch objects may be refreshed because they do not
   change the installed engine.
8. Live-state refusal considers only state that can still act or requires
   operator reconciliation. A durably ended and closed session cannot block
   update because of stale composer, delivery, lease, or pending-input rows.
   Closure terminalizes or removes volatile children in the same transaction.
9. A snapshot is referentially closed. A `planner_alerts` row is serialized only
   when every non-null session, binding, message, or watch reference it carries
   is also serialized. Session-scoped alerts for excluded live sessions are
   discarded. Rebuild runs a foreign-key check before replacing the outgoing DB
   and refuses with the exact offending table and row when the snapshot is not
   closed.
10. Existing orphan alert references are cleaned by migration or startup repair
    before new numeric IDs can reuse them. Rebuild never lets an alert from an
    old generation attach to a new session with the same integer ID.
11. A host Admin seat without injected API variables has a supported API-only
    bootstrap. The supervised service emits an owner-readable, non-snapshotted
    runtime credential artifact for each Admin shell. `sc mem` may discover the
    unique Admin artifact when both API variables are absent; it still calls the
    API and never reads or writes memory tables directly. Ambiguous Admin
    identity refuses and asks for an explicit shortname.
12. HTTP-only Interface commands use the standard Python runtime and do not
    require `websockets`. Only terminal attach/view/take-control code loads and
    checks the stream dependency. Host `status`, `start`, `stop`, and `reconcile`
    operate against the published sandbox API.
13. Database backup selection is deterministic:
    `SC_DB_BACKUP_DIR` when set and writable, then the existing home backup
    directory when writable, then a gitignored repo-local
    `.sc-state/db_backups/`. Directory creation and a writable probe happen
    before any supervised process stops. Backups remain WAL-safe and retain the
    current keep-five policy.
14. `sc launch --no-build` starts from the existing named sandbox image without
    invoking Buildx. It refuses before changing runtime state if the image is
    absent. `sc restart --no-build` validates the image and backup destination
    before `down`, forwards the mode to launch, and cannot leave a running fork
    down because of a known preflight failure.
15. Liveness is conservative. `safe_to_clean_all=true` requires positive
    evidence for the current Admin harness plus no live other shell and no
    indeterminate process. When self identity is absent, output says
    `admin_presence=indeterminate` and cleanup remains unsafe.
16. `.sc-worktrees` is an unconditional repo-map skip directory. Map file,
    dependency, environment, language, and count projections contain only the
    selected checkout once; linked worktrees cannot duplicate the project.
17. Every command failure names the next supported action. No acceptance flow
    uses direct DB edits, manual `docker run`, or a service restart merely to
    make an inconsistent session reconcilable.
18. The desktop terminal viewport is large by default and grows with available
    space up to exactly `1300px` wide by `850px` tall. The Interface page widens
    beyond the shared `1000px` review-page cap so the `230px` shell rail does not
    consume the terminal allowance. Smaller desktop and mobile viewports remain
    fluid, never overflow the screen, and preserve the existing stable minimum
    height. Every resulting size change refits xterm and reports the new rows and
    columns to the live pane without reconnecting.
19. Alerts expose human-readable meaning, session/generation provenance, and a
    supported next action. Capability degradation is informational rather than
    styled as a warning. A `turn_failure` resolves after a later successful turn
    in the same generation; a `reservation_expired` resolves when that
    generation is durably closed; every remaining session-scoped alert resolves
    on session end. Resolved audit rows do not contribute to the rail count or
    current-warning panel. Current alert queries are generation-scoped rather
    than shell-history-scoped. Expected optional-hook limitations render as
    generation capability information outside the warning count. Any remaining
    dismissible current alert has an API-backed acknowledgement action that
    records who and when without deleting its audit row.
20. Model choice uses one shared searchable list in Default Models and
    Interface New chat. The operator first chooses a harness, then focuses the
    model search field. Focus opens the existing correctly sized result window
    with the full model list for that harness. Typing filters that list; clicking
    a model selects it and closes the window. The harness is the only model
    prefilter. `Harness default` remains the first selectable option, so model
    choice is an optional override. Family chips/selectors and the hidden `all`
    query are removed. Empty search means the full list, not an instruction
    screen. The UI never offers or persists a raw search string: Enter chooses
    the highlighted exact result, and a query with no result changes nothing.
    Family-null local catalogue entries such as Codex Sol, Terra, and Luna are
    ordinary first-class results. The defaults API accepts only `null` for
    Harness default or an exact currently resolvable route for that harness;
    invalid and cross-harness values return a stable `422 invalid_model_route`.
    An already stored route that becomes unavailable is shown as stale and
    blocks launch with a named choose-model-or-default action rather than being
    passed unchanged to the harness.
21. `sc enter` without a shell argument restores the former Rich terminal
    launcher presentation: shells grouped by flavor, stable color-coded state,
    and columns for display name, shortname, status, and default
    harness/model. Selection remains keyboard-friendly and a direct
    `sc enter <shortname>` remains available. The launcher reads state and
    performs actions only through the Interface API; restoring its presentation
    does not restore direct database access or the old boot path.
22. The root Makefile remains a thin, fork-compatible include while
    `.super-coder/aliases.mk` becomes the documented operator command surface.
    Existing short aliases remain compatible. Named targets cover help, setup,
    service lifecycle, build/logs/health/ports, update/rollback, verification,
    map/render/snapshot, Interface enter/status/start/view/take/stop/recover,
    model refresh and selection, sprint/watch/job commands, and browser token
    retrieval. Targets that require `s=<shortname>` fail with their exact usage
    before invoking `sc`; `make dos ARGS="..."` remains the complete escape
    hatch. Every target delegates to `./sc` rather than duplicating behavior.
    `make dos-r ARGS="..."` delegates to the full `./sc restart`, not a
    server-only reload: after its existing confirmation, backup, and preflight,
    it bounces every applicable engine-managed service and verifies the expected
    inventory before returning success.
23. `./sc token` and the exact alias `make dos-token` print the current
    browser operator token, and only that token, to stdout for paste into the
    browser sign-in prompt. They do not rotate it, put it in command arguments,
    or write it to logs. A missing, unreadable, or insecurely permissioned
    runtime artifact refuses on stderr with the supported service action.
    Help labels the value as an operator capability and does not print it.
24. Stranded-shell recovery is one API-owned workflow used by browser and CLI.
    A preview classifies the shell as available, stale durable lock, exact idle
    orphan, verified live, or indeterminate and returns the evidence plus an
    opaque observation ID. Execution requires that fresh observation; changed
    state returns `409 recovery_observation_stale` and makes the client preview
    again. Standard recovery preserves all worktree files, closes only
    absence-proved locks or exact idle orphans, and makes the shell available
    atomically. Terminating a verified process requires force confirmation.
    Discarding local file changes is a separate optional escalation requiring
    the shell shortname as confirmation; it is never implied by recover or
    force. Recovery never uses a broad process match, silently drops commits,
    or bypasses the API with direct database edits.

## Lifecycle Contract

The API owns one closure helper and one transaction boundary. Every close
producer calls it rather than composing lifecycle and occupancy transitions
independently.

| Observed state | End or cancel result |
|---|---|
| `reserved/starting`, no identity ever established | End as `cancelled_before_spawn`; return success |
| `reserved/starting`, verified identity live | Signal the exact generation; converge through normal closure |
| `reserved/starting`, spawn outcome uncertain | Set unreconciled with exact reason; require absence proof |
| `occupied` with nonterminal lifecycle and verified identity | Graceful stop, then current force-after-timeout contract |
| `occupied/ended` or terminal lifecycle with proved absence | Complete durable closure idempotently; never transition back to stopping |
| Any state with identity mismatch | Refuse process signaling; set unreconciled/lost and preserve diagnostics |
| Already `ended` with terminal children | Return the original terminal result without state churn |

The helper atomically records end reason/time, terminalizes occupancy and
lifecycle, ends the matching generation, revokes active leases, resolves or
parks session-scoped wake state according to existing ambiguity rules, and
removes active input blockers. Provider hooks can acknowledge an already-ended
generation without reopening it. Repeated idempotency keys return the original
result; a fresh key against an already-ended session returns the same semantic
success.

## Client Workflow

1. Shell rail state is derived from a valid state pair, not occupancy alone.
2. `available` offers New chat.
3. `reserved/starting` shows startup state and Cancel start. It does not show
   Take-over, certify clean, or process-only End chat.
4. `occupied` plus a nonterminal lifecycle and verified pane identity may attach.
   Writer acquisition failure becomes read-only only for `writer_held`.
5. `lost`, `unreconciled`, terminal lifecycle, or missing identity shows
   diagnostics and Reconcile. It never opens a writable stream.
6. Reconcile with proved absence offers or performs durable close. Once closed,
   New chat is available without restarting the service.
7. On a sufficiently large desktop, the terminal opens at its `1300px` by
   `850px` cap. On smaller screens it consumes the available width and safe
   viewport height; the rail collapses on mobile and the terminal remains usable
   without horizontal page scrolling.
8. The alert panel labels current actionability. It explains `hooks_degraded`
   as reduced optional lifecycle detail with ordinary chat still working,
   explains `turn_failure` as one failed provider turn with recovery status, and
   ties `reservation_expired` to the exact generation that needs reconciliation.
   Historical resolved rows live behind an explicit history view; current
   counts include only the active generation. Dismissible current rows expose
   Acknowledge, while optional-hook capability information is not a warning.
9. Model selection is harness-first and list-first: select the harness, click
   the search field, and choose from all models shown for that harness. Search
   narrows the open list in place. Selecting `Harness default` clears an
   override; changing harness in New chat starts on that harness's default until
   the operator picks another model. Escape or an outside click closes without
   changing the current model; keyboard focus, arrows, and Enter provide the
   equivalent accessible selection path. The Shells tab uses the same picker
   behavior for each harness row. Search text is never a selectable model card
   and cannot be submitted as a default.

CLI and browser consume the same stable error codes. UI labels and controls are
projections of server state; clients do not infer a writer conflict from HTTP
status alone.

## Update And Snapshot

Update has an explicit read-only preflight phase before materialization. It
loads the installed guard, reports every blocking session/binding/batch/input
condition, and exits without installed-file or pin changes when any exists.
Only a passing preflight advances to fetch/materialize, migration, render, map,
and snapshot.

Closure and guard queries share the same definition of active state. Tests pin
that every terminal combination is nonblocking and every actionable live or
uncertain combination blocks with a usable remediation.

Snapshot selection treats parent and child rows as one projection. It never
serializes a child merely because its table is generally durable. After loading
schema, migrations, and content into a candidate DB, rebuild validates foreign
keys and required Interface invariants before the atomic replacement. Failure
keeps the outgoing DB and its backup intact.

## Restricted Admin

The restricted host Admin seat is a supported operating mode:

- Repo paths and `/tmp` may be writable while `$HOME` and Docker Buildx config
  are read-only.
- The API is healthy inside the sandbox and published on the fork port.
- Runtime credentials are mode `0600`, excluded from snapshot/render, refreshed
  on key rotation, and accepted only under the existing local trust boundary.
- HTTP-only recovery works without a host `websockets` install.
- Repo-local DB backups are gitignored and survive engine materialization.
- `--no-build` is explicit. It never silently selects an unknown or missing
  image, and ordinary `launch` retains its current build-first behavior.

This mode does not weaken WAL-safe backup, process identity checks, API-only
memory access, or the requirement to preflight before stopping a healthy stack.

## Operator Commands

`./sc` remains canonical. Make provides memorable entry points for the common
host workflow without becoming a second implementation:

| Workflow | Make target | Delegated command |
|---|---|---|
| Rich shell chooser | `make dos-enter` | `./sc enter` |
| Enter one shell | `make dos-enter s=DEV1` | `./sc enter DEV1` |
| Interface state | `make dos-status [s=DEV1]` | `./sc interface status [DEV1]` |
| Start shell | `make dos-start s=DEV1 ARGS="..."` | `./sc interface start DEV1 ...` |
| View/take control | `make dos-view s=DEV1`; `make dos-take s=DEV1` | Matching Interface stream command |
| Stop shell | `make dos-stop s=DEV1 ARGS="..."` | `./sc interface stop DEV1 ...` |
| Recover shell | `make dos-recover s=DEV1 ARGS="..."` | `./sc interface recover DEV1 ...` |
| Browser token | `make dos-token` | `./sc token` |
| Models | `make dos-models ARGS="..."`; `make dos-models-refresh` | Matching `./sc models` commands |
| Sprint/watch/job | `make dos-sprint`, `make dos-watch`, `make dos-job` with `ARGS` | Matching `./sc` command families |

The existing `dos-e`, `dos-l`, `dos-r`, `dos-d`, `dos-u`, `dos-t`, and `dos-h`
hot aliases remain. Help groups lifecycle, Interface, orchestration,
maintenance, and advanced commands and shows required variables adjacent to
each target. Automated help coverage prevents a documented target from
disappearing or dispatching a different command.

`dos-r` forwards `ARGS` so `--yes` and `--no-build` retain their canonical
semantics. The full restart inventory is the sandbox container (which owns the
HTTP API, browser UI, Interface runtime, wake coordinator, and PR poller), every
configured VM, tailnet, PM2, and database broker, and the configured Postgres
sidecar. Pidfile- and systemd-managed brokers both reload current engine code;
the latter are restarted through their supervisor instead of being left alive.
The retired legacy watch daemon remains stopped. Restart does not restart the
external application processes observed by the PM2 broker, a linked VM, or the
tailnet platform itself. A final health summary names every applicable service
as restarted, skipped because unconfigured, or failed; any expected unhealthy
service makes the command nonzero.

Token retrieval reads the same owner-only runtime artifact provisioned by the
supervised service. It verifies regular-file ownership and mode before reading,
emits no decorative prefix on stdout, and provides a distinct nonzero result
for service-not-running versus unsafe permissions. Browser authentication
continues to accept a pasted token; this requirement does not add token material
to a URL, page source, snapshot, or client persistence.

## Shell Recovery

Recovery starts with
`GET /_sc/interface/shells/{shell_id}/recovery`. Its response includes the
durable Interface session and generation, active archive relation, sprint
binding, pane PID/start ticks, tmux target, process-group evidence, unread
message count, and advisory Git facts for the shell worktree. Secrets and
terminal content are excluded. The server derives one classification and the
legal actions; the client does not infer safety from the fields.

`POST /_sc/interface/shells/{shell_id}/recovery` accepts the opaque
`observation_id`, a mode of `recover` or `force`, `preserve_worktree=true` by
default, and an idempotency key. `force` is legal only when the preview names
the exact verified process identity and the request carries the scoped
confirmation. The server sends `SIGTERM` to that exact process group, waits the
bounded existing grace period, and uses `SIGKILL` only if the same PID/start
ticks still identify it. An identity mismatch or unreadable process state
returns an indeterminate result and performs no signal.

On proven process absence, one transaction ends the matching Interface session
and generation, revokes writer leases, terminalizes queued input, closes the
matching archive, clears `active_archive_id` only when it still points to that
archive, resolves session alerts, and releases only generation-bound sprint
state whose ownership is unambiguous. Ambiguous wake or delivery state is
parked with a named next action. Unread inbox messages remain unread. The
result reports every changed durable object and the shell's resulting
availability.

The browser shows Preview recovery, then Recover or Force recover only when the
server lists that action. Diagnostics and confirmation name the shell, session,
process, and whether the worktree is clean. The CLI equivalent is
`./sc interface recover <shortname> [--force] [--discard-worktree] [--yes]
[--json]`; without `--yes`, force confirms the exact process identity.
`--discard-worktree` is an advanced, independently confirmed action that may
remove tracked and untracked file changes only in the exact shell worktree. It
refuses when unpushed commits exist and never deletes a worktree or branch.

## Delivery Plan

1. **Lifecycle convergence.** Repair cancellation, shared closure, hook/stop
   races, API error mapping, and the three #526 low follow-ups. Covers #519,
   #523, and #532. Verify with deterministic interleavings plus real tmux.
2. **Client state truth and size.** Gate CLI/browser attachment and
   state-dependent controls on lifecycle, identity, and exact error codes.
   Widen the Interface layout and make the terminal fluid up to `1300px` by
   `850px`, with xterm refit and pane resize propagation. Add alert explanations,
   provenance, severity styling, and current-versus-resolved projection.
   Simplify the shared model picker to harness-first plus one focus-opened,
   searchable full list, with `Harness default` as the first/reset option and no
   family controls or `all` mode. Apply it consistently to tmux New chat and the
   Shells tab. Covers #522, #527, #534, and #535 plus the FnB sizing,
   warning-clarity, and model-picker requirements. Depends on step 1.
3. **Update and snapshot integrity.** Move update refusal before mutation,
   align ended-state drain semantics, close snapshot references, clean existing
   orphans, and validate candidate rebuilds. Covers #528, #529, and #533.
   Depends on step 1's terminal-state contract.
4. **Admin API and CLI parity.** Add runtime Admin credential discovery and
   lazy stream dependency loading. Covers #516 and #518. Parallelizable with
   steps 1 and 3.
5. **Restricted supervision.** Add backup fallback plus `launch/restart
   --no-build` with preflight-before-down, and make restart bounce and verify
   every applicable engine-managed service across pidfile and systemd
   supervision. Covers #530 and #531 plus the FnB full-bounce requirement.
   Parallelizable with steps 1, 3, and 4.
6. **Diagnostics and map polish.** Make liveness uncertainty fail closed and
   exclude linked worktrees from mapping. Covers #517 and #524.
   Parallelizable with steps 1 through 5.
8. **Unified shell recovery.** Add the shared preview/execution API, exact
   process verification, atomic stale-state closure, worktree-preserving
   browser controls, and the equivalent CLI command. Absorbs roadmap #22 and
   flag #38. Depends on steps 1, 4, and 6.
9. **Rich CLI, Make, and token.** Restore the grouped Rich chooser on the
   API-backed `sc enter`, rebuild aliases/help around the supported operator
   workflow, and add `./sc token` plus `make dos-token`. Depends on steps 4 and
   8 for the final command surface; launcher presentation and tests may begin in
   parallel.
10. **AMI acceptance and close.** Run the consolidated matrix on the restricted
   AMI seat, close all sixteen issues with evidence, repeat the Claude/Codex/Kimi
   sprint path, update conformance and sprint report, then freeze only after a
   clean review. Depends on steps 1 through 9.

Each implementation step is one reviewable unit. Steps 4, 5, and 6 may run in
parallel after contracts are accepted. Step 2 waits for lifecycle error codes;
step 3 waits for terminal closure semantics; sequence 7 remains as the cancelled
acceptance row that this expansion replaced. Step 8 consumes the lifecycle and
liveness contracts; step 9 exposes only accepted API operations. Integration is
last only as a release gate, while every unit carries its own exact regression.

## Edge Cases

- Two End chat requests arrive while `session_end` and pane EOF fire.
- Cancel start arrives before spawn, after pane creation but before identity
  hook, and after identity hook but before provider readiness.
- The termination response is lost after durable closure and the client retries
  with the same or a new idempotency key.
- A session says occupied while lifecycle is terminal, pane identity is absent,
  or a reused PID fails start-tick validation.
- Writer acquisition returns `writer_held`, `not_occupied`, `stale_generation`,
  or an unexpected server error.
- Update is run with an ended session whose composer is unknown, with a live
  generation but ended session, and with no Interface tables on an older fork.
- Snapshot contains an alert with only a session reference, multiple references,
  a preserved ended-session reference, and an excluded live-session reference.
- Rebuild loads a legacy snapshot with an orphan reference or reused integer ID.
- No API variables exist; one Admin credential exists, multiple Admin
  credentials exist, the runtime file is stale, or the API has rotated keys.
- Host Python lacks `websockets`; status and stop still work, while attach gives
  a dependency-specific remediation.
- Home backup is writable, read-only, absent, or overridden to an unwritable
  path. No valid destination means restart refuses before down.
- `--no-build` sees an existing image, no image, a stopped fork, or an already
  running fork.
- Full restart sees every combination of configured/unconfigured pidfile and
  systemd brokers, a Postgres sidecar, a retired legacy watcher, and one service
  that fails its post-start health check. No old engine process survives a
  successful result.
- The current harness cannot be identified, another shell is live, `/proc`
  entries are unreadable, or only the Admin harness is positively identified.
- `.sc-worktrees` contains clean, dirty, nested, or broken linked worktrees;
  none appear in any map projection.
- Desktop viewport space is larger than, equal to, and smaller than the
  `1300px` by `850px` terminal cap; browser zoom and rail/header wrapping do not
  cause overflow or hide terminal rows.
- A failed turn is followed by a successful turn; optional hooks are degraded;
  a reservation is reconciled closed; and a session ends with open
  session-scoped alerts. Current counts and resolved audit history remain
  distinct in every case.
- A harness has zero, one, sixty, and more than sixty catalogue models; the
  focus-opened list, scroll window, filtering, current selection, keyboard
  navigation, outside close, and harness change remain deterministic without
  family controls or a special query.
- Catalogue entries have a null family, duplicate display labels, unavailable
  state, or exact Codex Sol/Terra/Luna selectors. Bare search terms, stale saved
  routes, and a route from another harness are never forwarded to launch.
- Alert history contains an ended session, a reused numeric session ID, an old
  generation for the same shell, a current actionable alert, a recovered turn
  failure, optional-hook capability information, and an acknowledged current
  row. Only current actionable state contributes to warning counts.
- The rich chooser sees no shells, one shell, long display/model names, unknown
  state, a reserved start, an orphan, and a terminal narrower than its columns;
  it remains readable and direct shortname entry still works.
- The browser token artifact is missing, a symlink, owned by another user,
  group/world readable, or valid. Failure text goes to stderr and stdout remains
  empty.
- Recovery is previewed, then the pane exits, the PID is reused, another client
  recovers first, or a new generation starts before execution. Every changed
  observation refuses without signaling or closing the new owner.
- A stranded shell has no process, an exact idle process, a verified active
  process, unreadable `/proc`, an active sprint delivery, unread messages, dirty
  tracked files, untracked files, and unpushed commits. Default recovery
  preserves the tree; discard remains separate and refuses unpushed commits.

## Verification

Unit and integration coverage must include:

- A table-driven legal/illegal lifecycle matrix and deterministic hook,
  termination, pane-exit, retry, and cancellation interleavings.
- API envelope tests proving route, validation, state-conflict, and server-error
  categories remain distinct.
- CLI and browser state tests for every occupancy/lifecycle pair, exact
  writer-conflict handling, and control visibility.
- Browser layout assertions at wide desktop, compact desktop, and mobile sizes:
  the terminal reaches but never exceeds `1300px` by `850px`, remains within the
  viewport when space is smaller, and sends updated rows/columns after resize.
- Alert lifecycle tests for failure then recovery, capability information,
  reservation close, session end, provenance after ID reuse, rail counts, and
  human-readable UI copy, acknowledgement audit, and current-generation query
  scoping.
- Shared model-picker tests in Default Models and New chat: focus with an empty
  query shows `Harness default` plus the harness's full list in the existing
  window, typing filters, click and keyboard selection persist the exact route,
  default clears the override, changing harness swaps the list and begins on its
  default, family-null local routes render normally, and neither family
  controls, `all`, raw-search cards, nor unvalidated writes are possible.
- Defaults API and launch tests reject bare, unavailable, and cross-harness
  routes; accept exact available Sol/Terra/Luna routes; and block a previously
  valid route that becomes stale with the named remediation.
- A mutation sentinel around update refusal proving hashes and mtimes for `sc`,
  engine paths, workflow paths, ignore rules, and both pin files do not change.
- Guard tests for live, ended, uncertain, pre-Interface, and partially migrated
  databases.
- Snapshot/rebuild tests with ID reuse, parent filtering, legacy orphan cleanup,
  foreign-key validation, and outgoing-DB preservation on failure.
- Host Admin tests with API variables absent and runtime credential discovery,
  plus multi-Admin ambiguity and stale credential refusal.
- CLI tests with no importable `websockets`: HTTP verbs pass and stream verbs
  fail with the exact dependency action.
- Backup and launch tests using read-only home and Docker config fixtures,
  missing images, successful image reuse, restart preflight ordering, complete
  configured-service restart, systemd reload, and aggregate health failure.
- Make dispatch coverage proving `dos-r` forwards `ARGS` to full `sc restart`
  and cannot select a server-only reload.
- Liveness fixtures for positive Admin identity, absent identity, other live
  shells, and unreadable processes.
- Repo-map fixtures containing `.sc-worktrees` with duplicate manifests and
  source; file and dependency projections stay singular.
- Rich-launcher snapshots and dispatch tests for grouping, status labels,
  default harness/model, narrow terminals, cancellation, and direct shell
  selection, with API calls asserted as the only state/action source.
- Make contract tests covering every documented target, required `s` failures,
  argument forwarding, legacy aliases, and the generic `dos ARGS` escape hatch.
- Token tests proving exact stdout, silent help, no rotation, file validation,
  and distinct missing-service and unsafe-artifact failures.
- Recovery table tests for every classification and legal action, stale
  observations, PID reuse, exact process-group signaling, repeated idempotency
  keys, atomic closure, ambiguous sprint state, and worktree preservation.
- Browser and CLI parity tests proving both render the server's evidence and
  legal actions, force requires scoped confirmation, discard is independent,
  and no unpushed commit can be removed.

The real AMI gate repeats each of the sixteen issue reproductions on the latest
merged engine, then:

1. Start, attach, detach, cancel-start, gracefully end, force-after-timeout, and
   reconcile-close real tmux sessions for Admin, planner, reviewer, and dev.
2. Race a real harness `session_end` hook against End chat and prove one clean
   terminal record with New chat immediately available.
3. Refuse update on live state and compare installed hashes; drain cleanly and
   complete update without direct DB changes.
4. Snapshot, clean, and rebuild; start new sessions and prove no old alert is
   attached through integer ID reuse.
5. From the restricted host Admin seat, run `sc mem which`, Interface HTTP
   status/stop/reconcile, WAL-safe restart, and `restart --no-build`.
6. Map the fork with linked shell worktrees and prove no duplicated files or
   dependencies.
7. Run one task through CI red, CI green, review, merge, and sprint close across
   Claude, Codex, and Kimi while recording all wake and action receipts.
8. Use the Rich chooser and direct Make targets to inspect, start, enter, stop,
   and recover shells; retrieve the browser token with `make dos-token` and
   authenticate without exposing it in command arguments or logs.
9. Produce stale-lock, exact-idle-orphan, verified-live, PID-reuse, dirty-tree,
   and unpushed-commit recovery cases; prove browser and CLI converge to the
   same result and default recovery preserves worktree content.
10. Run `make dos-r` with configured pidfile and systemd brokers plus the
    Postgres sidecar, then prove the sandbox and every applicable engine process
    has a new identity, current code, and passing health result.

## Non Goals

- Replacing tmux, the terminal library, or the API-owned input broker.
- Persisting terminal bytes, drafts, output, or provider conversation IDs.
- Automatic provider resume, automatic process respawn, or a second planner.
- Supporting arbitrary remote hosts or separate untrusting local user accounts.
- Silently skipping backups, builds, identity checks, snapshot validation, or
  live-state refusal.
- Redesigning general publish/snapshot concurrency tracked by roadmap #21.
- Automatically discarding shell worktrees, deleting branches, or deleting
  unpushed commits during recovery.
- Replacing `./sc` with Make or maintaining command behavior in two places.

## Done Condition

All sixteen AMI issues are closed with merged regression coverage and exact
real-environment evidence. Every session can start, cancel, attach, end, and
recover without an impossible state or service restart. Update refusal is
pre-mutation; cleanly ended state does not block; snapshot/rebuild cannot
reattach orphan children through ID reuse.

The restricted Admin seat can reach memory and HTTP recovery through the API,
create a WAL-safe repo-local backup when home is read-only, and restart from an
existing image without Buildx. `make dos-r` bounces and health-checks every
applicable engine-managed service, including systemd-managed brokers. Liveness
never authorizes cleanup without positive evidence, and repo mapping excludes
every linked worktree.

An operator can use the restored grouped terminal chooser, the documented Make
surface, and `make dos-token` without hidden dependencies. Browser and CLI
preview the same recovery evidence and can release every absence-proved stale
lock or exact confirmed orphan without service restart, direct database
changes, or loss of worktree content by default.

The final AMI matrix and cross-harness sprint pass on merged main. Feature #14
then receives updated conformance and sprint reports, its remaining open
feature flags are resolved, the corrective spec and completed parent spec are
frozen, and the roadmap may move to shipped.
