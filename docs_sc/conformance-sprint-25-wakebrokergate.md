---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Interface chats and interactive planner wake
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE: Sprint 25 wake/broker/gate

**Sprint:** doc #25 (Interface-backed planner wake) · **Spec:** doc #20 · **Judged against:** `main @13f5405` (all 10 units merged, green)
**Shard (REV2):** Requirements, Occupancy Model, Tmux Runtime, Input Broker, Harness Hooks, Wake Delivery, Retry Policy, Data Model
**Method:** spec clauses read against the code on main at the SHA — never the diffs, never the message trail. Code verified in a detached worktree at 13f5405; the full Interface/wake hermetic suite was run in-pass: **319 passed, 4 tmux-gated skips, 0 failures** (test_interface_{schema,transitions,crash_window,wake,wake_submit,sprint_ops,hooks,reconcile_guard,snapshot,api,runtime,exec,cli} + test_sprint_eventing).
**Narrative input (only):** ratified decisions #15, #16, #19, #22, #23, #28, #30, #31, #32, #33, #34.

**Verdict key:** as-specced · deviated-intentionally (matches a ratified judgement call) · deviated-silently (FINDING) · unimplemented (FINDING).

**Result: 0 Major, 2 Medium, 5 Low.** No finding reopens the sprint; the two Mediums are bounded, fail-closed deviations worth a fix unit or an explicit ratification before freeze.

## Recorded per planner instruction (not findings)

- **Decision #26 (operator-cap bootstrap exchange)** is **direction-superseded by #30** (personal-machine trust boundary). The shipped seq-5 impl STANDS; the absence of automatic same-origin bootstrap is deliberately NOT flagged.
- **Frozen-CANCEL** (a frozen sprint doc cancels queued wake like a close, stronger than the spec minimum): **deviated-intentionally**, declared by seq 8/seq 10. Verified in code: `submit_wake_batch` cancels on frozen/not-ACTIVE (`interface_broker.py:807-816`), and `_close_sprint_wake` fires on BOTH the status:CLOSED edit and freeze (`api/server.py:757-771, 2032, 2055-2056` — decision #33(3)).
- **Spec-debt carried from decision #23:** hook token hash + sequence live on `interface_generations`, not `sprint_planner_bindings` as the spec's Data Model table says; lifecycle edges `lost|error→ended` and `starting→ended` extend the spec's literal list. Both recorded below as deviated-intentionally. Spec #20's Data Model row and edge list should be updated post-sprint.

## Requirements (1–15)

| # | Clause | Verdict | Evidence / note |
|---|---|---|---|
| 1 | Every nondeleted shell represented with exact availability + lifecycle | as-specced | `_list_shells` / `_availability` (`interface_routes.py:251,500`); UI rail out of shard |
| 2 | At most one live generation; browser/terminal loss ≠ end; unmanaged harness blocks New chat | as-specced | Partial unique index (`0078:90-91`); detach preserves chat (`interface_runtime.py:1134-1143`); `shell_liveness` backstop in `_create_session` (`interface_routes.py:342-350`) |
| 3 | New chat only when no live/unreconciled owner; normal harness/model/effort/permission/worktree/render/boot/archive path | as-specced | 409 occupied gate (`interface_routes.py:328-338`); `interface_exec` rides `run.prepare_launch` (`interface_exec.py:169-180`) |
| 4 | CLI and web call the same API/state machine; no private side path | as-specced | `interface_cli.py` is an API client throughout; no direct DB/tmux writes |
| 5 | One per-generation broker serializes all writable input; `sc enter` is a broker client; raw launch requires generation capability; no bytes persisted | as-specced | `accept_human_input` two-phase; `sc enter → sc interface enter` (`sc:1088`); raw interactive launch refuses before archive creation (`run.py:1106-1119`); metadata-only rows (`0078:120-140`) |
| 6 | Wake requires supported harness, idle, clean, ≥3s quiet, no conflicting writer/unmanaged client | as-specced | Full gate in `submit_wake_batch` (`interface_broker.py:849-896`) — see Retry Policy rows for the two timing deviations (F3/F4) |
| 7 | Busy/approval/user_input/dirty/races/uncertainty queue safely; unacked human frame parks delivery_unknown, never blind-retried | as-specced | Gate-fails are state-preserving; crash-window parking (`interface_broker.py:86-95, 230-245`; `interface_reconcile.py:60-66`); no replay path exists anywhere |
| 8 | Only sprint-scoped task/result/pr_event wake, only while doc unfrozen + ACTIVE | as-specced | `maybe_create_wake_item` (`interface_wake.py:38-82`) — kind, sprint_doc_id, frozen, status, binding, generation all checked in the insert txn |
| 9 | Every event durable before notification; failure never deletes/reads/blind-replays | as-specced | Message + wake item commit atomically; coordinator signals post-commit (`interface_wake.py:5-10, 38-82`) |
| 10 | Claude/Codex/Kimi share one session+input protocol; adapters supply hooks only | as-specced | Single contract in `interface_hooks.py`; per-harness installers only merge hook config |
| 11 | No scheduled model poll / provider resume / second harness / webhook / event content in prompt | as-specced | Coordinator is event-driven, one-shot debounce timer only (`interface_wake.py:183-201`); fixed prompt constant (`interface_broker.py:28`); GitHub polling scope = REV1 shard |
| 12 | Browser/OS/focus/terminal environment does not affect correctness | as-specced | No focus/screen/Wayland/X11 dependencies in any code path reviewed; alerts are DB rows + UI |
| 13 | Linux sandbox + declared tmux + pinned deps; non-Linux no-sandbox reports Interface unavailable | **deviated-silently (F5, Low)** | Version gate + pins present (`interface_runtime.py:57,451-464`; Dockerfile); **no platform check** — a non-Linux host with tmux reports available, then /proc-based identity fails closed at spawn |
| 14 | Restart reconciles DB + tmux; snapshot excludes volatile; rebuild/update refuse while live | as-specced | `startup_reconcile` + `reattach_all`; volatile tables/columns excluded (`snapshot.py:79-94`); `live_refusal_reasons` wired into `rebuild.py:166` and `update.py:327` |
| 15 | Operator authority; hook tokens generation-scoped, callback-route-only | as-specced (this half) | Hook token calls only `_hook_callback` (`interface_routes.py:1330-1436`); browser/operator auth surfaces = REV1 shard; trust boundary per decision #30 |

## Occupancy Model

| Clause | Verdict | Evidence / note |
|---|---|---|
| Five orthogonal dimensions (occupancy / lifecycle / composer / client / wake) with the spec's state sets | as-specced | `0078:72-78,125-128,193-196,218-221`; client + wake derived (`_wake_state`, `interface_routes.py:805`) |
| `available` derived only after no live/uncertain generation; busy ≠ available; disconnected browser ≠ available | as-specced | Availability = absence of non-ended row; detach keeps occupancy |
| Occupancy edge list | as-specced | Triggers + app maps match spec exactly (`0078:317-326`, `interface_state.py:23-28`) |
| Lifecycle edge list | as-specced + **Low note (F6)** | Spec edges all present; `lost\|error→ended`, `starting→ended` ratified (#23) = deviated-intentionally; **`starting→error` is an extra unratified edge** (fail-closed direction); spawn-failure path leaves `lifecycle='starting'` on an occupancy-`ended` session (cosmetic) (`interface_state.py:31`; `interface_routes.py:400-417`) |
| Composer: unknown at fresh start → clean only via ready callback + zero accepted human seq; dirty before forwarding; dirty→clean only via fenced submit or certification; ambiguity → unknown | as-specced | `record_hook` session_start (`interface_broker.py:344-371`); phase-1 dirty commit (`:217-223`); fenced submit (`:372-414`); `certify_clean` (`:260-266`) |
| One non-ended session per shell; reservation lease + PID start ticks fence concurrent starts; startup reconciliation repairs expired reservations; liveness scan precedes reservation | as-specced | `0078:89-91`; 60s reservation TTL + expiry → unreconciled (`interface_reconcile.py:96-107`); `shell_liveness` check in produce (`interface_routes.py:342-350`) |

## Tmux Runtime

| Clause | Verdict | Evidence / note |
|---|---|---|
| Private tmux server on instance-scoped mode-0700 socket; no dependence on another server | as-specced | Own `-S` socket under mode-0700 run_dir (`interface_runtime.py:434-436`) |
| **Uses engine configuration, a distinct prefix** | **deviated-silently (F1, Medium)** | Bare `new-session` — **no `-f` engine config**, so the server sources `/etc/tmux.conf` + `~/.tmux.conf`, contra "does not depend on ~/.tmux.conf"; no distinct prefix set. A user/system tmux.conf (e.g. `remain-on-exit on`) can alter pane behavior the death-detection assumes — fail-closed (stuck occupied + alert), never unsafe. `interface_runtime.py:643-652, 734-751` |
| **One named session per interactive chat** | **deviated-silently (F2, Low)** | One session `sc-interface` with one **window** per chat (`interface_runtime.py:66, 733-755`). Pane-level identity fencing preserves per-chat isolation; organizational deviation only |
| Declared, version-gated tmux in image/installer | as-specced | Dockerfile pin + `TMUX_MIN_VERSION` startup gate; unavailable = review UI only |
| Generation-capability entrypoint; `sc enter` cannot invoke the raw path; direct launch refuses before archive | as-specced | `interface_exec.py` token flow (single-use, mode 0600); `run.py:1106-1119` refusal |
| Broker owns the writable tmux path; read-only diagnostic client tolerated | as-specced | Only broker calls send-keys; probe tolerates read-only clients (`interface_runtime.py:557-575`) |
| Unmanaged writable client → composer unknown, wake disarmed, alert, removal + certification to rearm | as-specced | Gate probe + verdict (`interface_broker.py:862-877`); decision #32's fail-open-on-unreachable ratified and writer-preflight compensation verified total (`interface_runtime.py:528-555`) |
| PID presence never authority; start ticks/identity validation; fail closed; never kill uncertain process | as-specced | `_verify_identity`, `terminate` re-verifies before SIGKILL, `prove_absence` (`interface_runtime.py:792-930`) |

## Input Broker

| Clause | Verdict | Evidence / note |
|---|---|---|
| Ordered per-generation queue; one unacknowledged frame per writer; bounded payload | as-specced | Generation queue (`interface_runtime.py:347-357`); pending_seq check; 64 KiB bound (`interface_broker.py:27,173-175,208-211`) |
| Two-phase metadata-only accept: pending commit (dirty first) → forward once → forwarded commit → ack | as-specced | `accept_human_input` (`interface_broker.py:146-257`); bytes never stored |
| Exact duplicate returns prior ack, never re-forwards; gap rejects pre-state-change | as-specced | `interface_broker.py:192-196, 212-215`; lease reseed keeps session-scoped dedupe across takeover (`:106-143`) |
| Crash between commits → park unknown, revoke writer, alert, NEVER replay | as-specced | Live write-failure park + startup park; `reconcile_input` is the only exit and never re-sends (`interface_broker.py:230-245, 269-298`; `interface_reconcile.py:60-66`) |
| UTF-8/paste/control/mouse/escape same path; resize ordered, non-dirtying; local copy never reaches broker | as-specced | Single send-keys -H path; resize queue item touches no composer state |
| prompt_submit clears dirty → busy; clean only if no later human seq (fence); turn_stop → idle; erased draft stays dirty; certify-clean records writer+seq | as-specced | Fenced submit incl. batch-evidence fence (`interface_broker.py:372-414`); certification audit columns |
| 3s quiet is debounce only; zero forbidden; monotonic clock; fresh full debounce after restart | **deviated-silently (F3 Medium, F4 Low)** | Zero-forbidden enforced (`interface_broker.py:777-778`). **F3: the debounce computes on wall-clock `datetime('now')` text, not a monotonic clock** (`:883-896`) — a backward clock step compresses the quiet window; clean+lock+fence bound the blast radius. **F4: the post-restart debounce rides the `service_restart` lease-revoke stamp — a generation with NO current lease at restart has no stamp and owes no fresh debounce** (`:885-890`; `interface_reconcile.py:109-114`) |
| Quiet interval "configurable within a small bounded range" | **deviated-silently (F7, Low)** | `quiet_s` is a constructor parameter only; no operator-facing configuration exists (`interface_wake.py:101-105`). Zero-forbidden holds |
| Wake uses same lock/queue; human-first cancels attempt; wake-first is indivisible; no interleaving | as-specced | Lock = `submitting` batch row; BEGIN IMMEDIATE serialization both directions (`interface_broker.py:45-57, 197-207, 769-776`) |

## Harness Hooks

| Clause | Verdict | Evidence / note |
|---|---|---|
| Event table: session_start / prompt_submit / turn_stop / session_end / approval_wait / approval_result / user_input_wait / interrupt+failure | as-specced | All mapped incl. interrupt/failure as terminal-turn reconciliation (`interface_broker.py:448-459`); per-harness capability table is honest about gaps (`interface_hooks.py:100-128`) |
| Start-ready, submit, stop, end mandatory; missing approval/user-input = degraded, stays busy (safe); unsupported mandatory hooks block arming, never the chat | as-specced | `MANDATORY` + capability gate at arm and submit (`interface_routes.py:883-889`; `interface_broker.py:856-861`); degraded alerts (`_hook_capability_alerts`) |
| Pre-prompt provider event does not satisfy start-ready | deviated-intentionally | claude/codex SessionStart fires pre-prompt; ratified resolution (#28/#31, flag #49): readiness keyed to the REAL provider session_start stamp (`provider_ready_at`, migration 0081) + quiet baseline + submit-hook fence absorbing the residual window (`interface_broker.py:353-362`; `interface_hooks.py:93-98`) |
| Hook config merged, never replacing fork/user hooks | as-specced | claude per-session overlay, codex group merge, kimi marker-fenced block (`interface_hooks.py:166-331`) |
| Callbacks discard prompt/tool/transcript/terminal content | as-specced | Emitter redirects stdin to /dev/null, posts only event/session/generation/seq/PID/token (`interface_hook.py:155-186`) |
| Wrong tokens, stale generations, replayed sequences, illegal transitions, PID mismatch rejected AND audited | as-specced | Every rejection path `_log`s — flag #51 verified landed, incl. replay/stale seq (409), 422s, 404, identity mismatch (`interface_routes.py:1341-1434`); commit-order flock through POST (#50) (`interface_hook.py:98-118`) |

## Wake Delivery

| Clause | Verdict | Evidence / note |
|---|---|---|
| One wake item per unread eligible message; coalesced fixed-prompt batch; one live batch per generation | as-specced | `UNIQUE(binding_id,message_id)`; `form_batch`; partial live-batch index (`0078:206-229`) |
| Pre-submit proof: lock, idle, clean, ≥3s quiet, no pending human frame, exact identity, hook health, API health, active sprint; uncertainty queues without a byte | as-specced | `submit_wake_batch` gate in full (`interface_broker.py:783-896`) — timing clauses per F3/F4 |
| Submit callback acks delivery → running; stop callback reconciles: read→done, ambiguous→reconcile, unread→queued+wake-count, in-turn read→done | as-specced | `record_hook` prompt_submit/turn_stop; `_complete_batch`/`_reconcile_item` (`interface_broker.py:504-578`) |
| Infrastructure never marks messages read; uncertain external action parks before retry | as-specced | Item reconciliation reads `read_at` only; receipt intent/unknown → item `reconcile` + alert |
| Unread after 3 completed wakes → quarantined + alert; newer work unblocked | as-specced | `MAX_COMPLETED_WAKES=3`; quarantine is per-item, never batch-blocking (`interface_broker.py:565-578`) |
| Restart: submitting/running batch → delivery_unknown UNLESS durable hook-seq evidence proves the transition; never blindly resubmitted | as-specced | `startup_reconcile` evidence ladder (stop stamp → complete; submit stamp → running; neither → parked) (`interface_reconcile.py:68-93`) |
| Parked work recovery is operator-gated | as-specced | `resolve_batch` closes parked batch as audit, items requeue for a NEW batch; retry route resolves EVERY parked batch (SC-015 fix verified present) and clears only the alerts it remedied, with re-arm via dedupe-while-open (#33(2)) (`interface_routes.py:1153-1259`) |

## Retry Policy (table walk)

| Spec row | Verdict | Note |
|---|---|---|
| Busy/approval/user_input/dirty/unknown/quiet → no attempt, await event | as-specced | State-preserving gate-fails; quiet schedules one event-reset re-attempt |
| Human input wins broker order → cancel attempt, preserve queue | as-specced | Pending-frame gate-fail; batch stays queued |
| Wake holds lock first → indivisible submission; later input ordered after | as-specced | Input lock refuses frames while `submitting` |
| Broker fails with pending human seq → unknown, revoke writer, never replay | as-specced | Crash-window protocol, proven in test_interface_crash_window |
| Definite pre-send failure → queued; bounded 1s/5s/30s retries, then stop | as-specced | `PreSendError` + `RETRY_DELAYS_S`; exhaustion alerts, batch stays queued (in-mem counters ratified #32) |
| Prompt may be sent, submit hook missing → delivery_unknown, never auto-retry | as-specced | Ambiguous writer failure + restart rule both park; no auto-replay path exists |
| Browser/CLI disconnect → release lease, preserve chat/draft/queue | as-specced | Fenced liveness revoke (`interface_runtime.py:1134-1143, 1227-1261`) |
| Writer heartbeat expires → revoke, preserve dirty, allow takeover | as-specced | Reaper sweep (40s liveness / 60s stale) |
| Unmanaged writable client → unknown, disarm, alert, removal + certification | as-specced | Gate-time probe; detection latency between submits rides the next gate (fail-closed) |
| Broker/API unavailable → no input/wake accepted; work stays queued | as-specced | 503s, no direct-DB/tmux fallback anywhere |
| PID/pane/worktree/shell/archive/generation mismatch → refuse, never kill | as-specced | Identity proofs fail closed; generation fencing throughout |
| Harness/pane/tmux/container/supervisor lost → lost/unreconciled, queue + alert | as-specced | `_on_unexpected_exit` + `wake_session_ended` alert (SC-011); batch stays queued for a future generation |
| Sprint closes → cancel wake items, messages unread, chat running | as-specced | `release_binding`/`release_bindings_for_sprint`; frozen-CANCEL recorded deviated-intentionally |
| Snapshot while live → exclude volatile, continue | as-specced | `snapshot.py:79-94` row filters + SENSITIVE_COLUMNS |
| Rebuild/update with live state → refuse with drain guidance | as-specced (+Low note) | `live_refusal_reasons` wired in; reasons name the state, not literal CLI commands — cosmetic |
| External action uncertain → park and inspect before retry | as-specced | Receipt intent/unknown blocks read-marking; reconcile path explicit |
| 3 completed wakes unread → quarantine + alert, newer work continues | as-specced | Verified above |
| Debounce event-reset, not polling; watch intervals the only steady timer | as-specced | One-shot per-binding timer (ratified #32); PR poller = REV1 shard |

## Data Model

| Spec surface | Verdict | Note |
|---|---|---|
| `interface_sessions` (+ one non-ended per shell) | as-specced | `0078:55-91` |
| `interface_writer_leases` (one current writer) | as-specced | `0078:96-112` |
| `interface_input_state` (metadata only, no bytes) | as-specced | `0078:120-140` |
| `interface_idempotency_keys` (unique actor/op/key) | as-specced | `0078:145-157` |
| `sprint_planner_bindings` (one unreleased per planner + per sprint) | as-specced + recorded deviation | Table + both partial indexes present; hook token/seq live on `interface_generations` instead — ratified #23 spec-debt (deviated-intentionally) |
| `planner_wake_items` (unique binding/message) | as-specced | `0078:213-232` |
| `planner_wake_batches` (one live batch; seq fence; hook acks) | as-specced | `0078:188-209` |
| `planner_action_receipts` (unique op key) | as-specced | `0078:236-248` |
| `pr_poll_runs` / `pr_poll_observations` | as-specced + recorded deviation | `run_id` deliberately not FK (observations outlive volatile runs) — ratified #23 (deviated-intentionally) |
| `planner_alerts` (deduped while open) | as-specced | `0078:283-296` |
| Nullable `sprint_doc_id` on shell_messages + watched_prs; existing rows unwoken | as-specced | `0078:304-310`; watched_prs uniqueness rebuild landed with seq 9 (`0080_pr_polling_cutover.sql`) as #23 scheduled |
| `provider_ready_at` (0081) | deviated-intentionally | Post-spec column implementing ratified #28/#31 (flag #49) |
| Snapshot exclusions + rebuild/update refusal | as-specced | Verified above |

## Findings

- **F1 (Medium, deviated-silently) — Tmux Runtime: no engine tmux configuration / distinct prefix.** The private server is started bare; it sources `/etc/tmux.conf` + `~/.tmux.conf`, contra the spec's "uses engine configuration, a distinct prefix … does not depend on ~/.tmux.conf". User/system tmux options (e.g. `remain-on-exit`) can break the pane-death detection the runtime assumes. Fail-closed (stuck occupied + alert), never unsafe. `interface_runtime.py:643-652, 734-751`. Fix: start the server with `-f` an engine-shipped tmux.conf (explicit prefix, sane defaults).
- **F2 (Low, deviated-silently) — Tmux Runtime: one session + windows, not one named session per chat.** `interface_runtime.py:66, 733-755`. Isolation holds at pane level; organizational deviation.
- **F3 (Medium, deviated-silently) — Input Broker/Retry Policy: quiet debounce uses wall clock, not a monotonic clock.** `interface_broker.py:883-896` computes `julianday(now)-julianday(baseline)` over `datetime('now')` text; a backward wall-clock step compresses the 3s window the spec names as monotonic. Bounded by the clean+lock+fence layers, but the debounce is one of the four decision-#15 gate legs. Fix: stamp baselines from a monotonic source (or store epoch from `time.monotonic()` anchored at write time) for the quiet comparison.
- **F4 (Low, deviated-silently) — Input Broker: post-restart fresh debounce only owed when a writer lease existed at restart.** The restart baseline is the `service_restart` lease-revoke stamp; a generation with no attached client has no lease, so a wake can submit <3s after service restart, contra "after service restart every otherwise-clean generation waits a fresh full debounce". `interface_broker.py:885-890`; `interface_reconcile.py:109-114`. Fix: record a restart stamp per runtime boot (e.g. a singleton table) and include it in the baseline.
- **F5 (Low, deviated-silently) — Requirement 13: no non-Linux platform gate.** `_check_available` checks tmux/node presence + version only; a non-Linux no-sandbox host with tmux installed reports Interface available, then /proc-based identity proofing fails closed at spawn. Spec wants "reports Interface unavailable". `interface_runtime.py:451-464`. Fix: one `sys.platform` check in `_check_available`.
- **F6 (Low, deviated-silently) — Occupancy: unratified extra lifecycle edge `starting→error`; spawn-failure sessions keep `lifecycle='starting'` on an `ended` occupancy.** Edge beyond spec + #23's ratified additions (fail-closed direction); cosmetic state inconsistency on the definite-spawn-failure path. `interface_state.py:31`; `interface_routes.py:400-417`.
- **F7 (Low, deviated-silently) — Input Broker: quiet interval not operator-configurable.** Only a constructor default; spec calls for configurability within a bounded range (zero-forbidden holds). `interface_wake.py:101-105`.

## Safety-critical hunt (the planner's named targets)

- **Decision-#15 gate (idle+clean+3s-quiet+no-unmanaged-writer, one fenced order):** PRESENT and correctly serialized (BEGIN IMMEDIATE both directions). Two leg-level timing deviations: F3/F4 above.
- **Parking invariant (delivery_unknown never auto-replayed):** HOLDS. No replay/resubmit path exists for parked human frames or parked batches anywhere in the tree; the only exits are operator verdicts (`reconcile_input`, `resolve_batch` → NEW batch through the full gate).
- **Generation-fenced input order:** HOLDS (generation FK + lease/token/generation fencing + durable hook_seq fence).
- **Hook rejection auditing (#51):** HOLDS — every route rejection path audited.
- **delivery_unknown no-auto-replay:** HOLDS, incl. the restart evidence ladder and the retry route's resolve-every-parked-batch shape (SC-015 verified fixed in-tree).

**Recommendation:** no Majors — the sprint is not reopened by this shard. F1 and F3 are Medium: either land a small fix unit (engine tmux.conf + monotonic quiet baseline) or have the planner ratify them as accepted deviations before freeze. F2/F4/F5/F6/F7 are report-level Lows.
