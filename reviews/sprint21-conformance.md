# CONFORMANCE: Sprint planner session control

sprint: doc #21 · spec: doc #20 (feature 14) · judged: main @ 2cc320ec4ef3d3d4e9c7aaf6ab01a1830a548165
reviewer: REV1 (conformance slot) · date: 2026-07-22 · kickoff: msg #274 (PLN1)
CI at SHA: tests ✅ · render-check ✅ (gh run list --commit 2cc320e)

Method: spec judged against the code on main at the pinned SHA only — no diffs, no
message trail. Narrative input limited to the 8 ratified judgement calls in the
kickoff. Hermetic verification confirmed by reading the test suites at the SHA +
green CI on the merge commit (pytest not runnable in this container — stale
materialized engine, already a known close-out dependency).

## Verdict table

| Spec section · requirement | Verdict | Evidence (main @2cc320e) |
|---|---|---|
| Data model: `shell_session_bindings` exact schema | as-specced | `migrations/0077` matches spec SQL column-for-column + one-managed-per-shell unique index |
| Data model: `session_wake_jobs` exact schema, dedupe (binding,message) | as-specced | `migrations/0077` |
| Data model: rebuild-from-text (schema.sql + migrations) | as-specced | frozen baseline + migration pattern; test_migrate + render-check green |
| Data model: tokens never in `control_endpoint`; 0600 runtime file; deleted on release | as-specced | kimi-session.py `write_private`; `_binding_credential_paths` strict-scope unlink in release (fail-closed) |
| State model: 7 states, dispatcher actions per state | as-specced | `session_control.py` `_NEXT_STATES`; claim_batch only from foreground/idle/dormant |
| State model: allowed edges (spec silent on exact set) | as-specced (ratified J1) | conservative set; foreground→dispatching present (Claude active watcher); error/released recover only via starting; self-refresh allowed |
| State model: ownership = PID + start ticks + command + worktree; PID reuse cannot authorize | as-specced | `session_supervisor.process_matches`; test_pid_reuse_is_stale… |
| State model: reconciliation at dispatcher start + before every lease claim | as-specced | `poll_once` reconciles per binding per cycle; `preflight_lease` + `claim_lease` re-validate |
| class4: no resume while a validated owner or active provider turn exists | as-specced | dormant+owner-vacant gate, `resume-fenced` guard, ProviderBusy defer; test_dormant_probe_cannot_resume_over_a_validated_live_owner |
| Provider contract: `create` | as-specced | adapter `session_control.launch` scripts; Claude supplies UUID via `--session-id` |
| Provider contract: `status` / `deliver` / `resume` | as-specced | all three adapters; deliver never steers an active turn |
| Provider contract: `interrupt` (operator-only) | **unimplemented — Low (F2)** | no interrupt operation in any adapter, CLI, or API |
| Provider contract: `release` | as-specced | API `release_session_control` + `sc session release` |
| Provider contract: capability probe records CLI version; unknown versions fail closed for active delivery, smoke-tested resume allowed | as-specced | probe_claude/probe_codex/probe_kimi; test_unknown_version_fails_* ×3 |
| Provider contract: no terminal-presentation scraping for session IDs | as-specced | IDs from `--session-id` supply, `thread/start` response, Kimi session API; register_native_session docstring enforces intent |
| Provider contract: model/provider/effort/worktree/permissions pinned from original archive; no silent model fallback; K3 stays K3 | as-specced (ratified J4: effort = config-effective at launch) | resume commands pin from launch-recorded settings/archive_model; binding_for_resume refuses model mismatch; Kimi resume refuses non-`kimi-code/k3` |
| Claude: watcher registers/heartbeats PID against binding, clears when it fires; live process without armed watcher not deliverable | as-specced | watch.py channel register/heartbeat; ClaudeAdapter.status → active (queue) when channel unarmed |
| Claude: dormant fallback `claude --resume <id> -p` | as-specced | resume_command; fenced via supervise+lease |
| Claude: ack-wait cannot wedge on dead owner | as-specced (SC-464 fixed) | `_wait_for_ack` 5s ownership re-check |
| Codex: per-binding unix socket app-server, `codex --remote` attach, `turn/start` only when idle, never `turn/steer`, `codex exec resume` fallback | as-specced | codex-session.py + CodexAdapter; test_idle_delivery_starts_one_turn_waits_and_never_steers; no `turn/steer` anywhere |
| Kimi: authenticated loopback session server, engine-side queueing, no steer, `kimi --session` fallback, web-client `./sc enter` surface | as-specced | KimiAdapter loopback+token validation; kimi-session.py prints/opens managed web URL |
| Other harnesses: capability-gated, no optimistic support; planner without session_control fails with actionable message | as-specced | opencode/vibe declare no session_control; no binding → manage fails actionably at arming (declaration step 2 of orchestration skill) |
| Arming validates approval posture + deliver-or-resume capability before workers kick off | as-specced (ratified J5, all 3 adapters) | manage_session_control: posture + capability + native-ID checks; provider-generic vocabulary |
| User workflow: boot summary shows both IDs | as-specced | run.py "session binding: <id> · <harness>=<native>" |
| User workflow: fixed injected prompt, bodies never in prompt | as-specced | WAKE_PROMPT verbatim match; no body ever crosses transport |
| User workflow: `./sc enter <planner>` attaches/resumes managed binding by default; `--new-session` refused until release | **unimplemented — Medium (F1)** | no managed-binding attach in run.py; `--new-session` flag does not exist |
| User workflow: sprint close releases binding; history stays resumable | as-specced | release keeps rows + native ID; orchestration skill close-out step |
| Skill re-arms watcher after every handled batch | **deviated-silently — Low (F3)** | orchestration skill says watcher "may keep armed"; no re-arm-after-batch instruction (runtime degrades safely to queued→dormant-resume) |
| Dispatch loop: 1s scan, zero model tokens, recovers rows from any path | as-specced | poll_once + reconstruct_wake_jobs each cycle |
| Dispatch loop: transactional single-claimer, coalesced batch, done only on `read_at`, never marks read on planner's behalf | as-specced | claim_batch/finish_batch; no write to shell_messages anywhere in dispatcher; test_two_dispatchers_claim_exactly_one_batch |
| Dispatch loop: messages arriving during turn — ledgered, done if acked, else queued | as-specced | watermark + `_reconstruct_turn_arrivals`; test_message_arriving_during_turn… |
| Dispatch loop: bounded sanitized attempt logs; dispatcher own heartbeat; `./sc launch` starts / `./sc down` stops | as-specced | AttemptLog 0600/200-line; daemon_heartbeats 'session-dispatcher'; service_supervisor under `sc serve` in launch container |
| Failure: retry 15s/60s/5m, terminal → error | as-specced (ratified J2: initial + 3 retries, terminal on 4th) | RETRY_DELAYS/MAX_ATTEMPTS; test_unacknowledged_failures_retry_then_enter_error_without_reading_message; spec wording contradiction = logged spec debt |
| Failure: API unavailable → no turn, local retry, no attempt burned | as-specced | api_ready gate; test_api_down_never_starts_or_consumes_an_attempt |
| Failure: busy/foreground → queue, never concurrent-resume; server loss → reconcile, resume only on vacant lease | as-specced | status=active skip; ProviderBusy defer without attempt cost; dormant+vacant gate |
| Failure: dispatcher crash mid-turn → exact reconcile, adopt live child or wait | as-specced | running-jobs + owner live/cleanup wait; recover_interrupted; test_crash_left_running_job_is_requeued_without_starting_a_second_writer |
| Failure: release with queued events → unread preserved, jobs cancelled with audit reason | as-specced | release sets state='cancelled', last_error='binding released'; test_release_cancels_queue_but_keeps_message_unread |
| Failure: never kill an unverified process | as-specced | orphan-group fences to error, no kill; terminate_group only on self-spawned child; #439 section below |
| Operator surfaces: `sc session status/manage/release/retry` semantics incl. release `--after-turn` | as-specced (ratified J3: internal endpoints U3, public CLI U7 — both on main) | session_cli.py + operator API; retry requeues failed-and-still-unread through starting |
| GUI: compact status + queued/error count, no tokens/transcript paths | as-specced | get_session_control_overview omits endpoints/capabilities/PIDs |
| Analytics: exact native-ID attribution before window inference, no double count | as-specced | analytics._attribute exact-match-first; test_native_binding_attribution_precedes_fallback_without_duplicate_rows |
| Surfaces: skills replace Claude-only planner recommendation + false wake claims | deviated-intentionally (ratified J8) | engine skills (sprint, sprint_orchestration) rewritten ✓; docs_sc/job-runner.md + docs/README.md still stale at SHA — unit 9 prepared, blocked on admin Publish gate |
| Verification: hermetic test list | as-specced | all listed scenarios matched to named tests (see Coverage); CI green at SHA |
| Verification: env-gated provider smokes + release gate (real sprint per provider) | deviated-intentionally (ratified J7) | deferred to close-out (stale engine lacks `./sc session`; provider spend unauthorized); spec #20 stays UNFROZEN until they pass |
| Non-goals: no Remote Control relay, no steer, no fresh-session fallback, no bodies in prompts, workers unchanged, no unsupported-harness optimism | as-specced | verified by absence at SHA; sc run worker path untouched (binding only for planner flavor) |

## Findings

**F1 · Medium · unimplemented — `./sc enter` against a managed binding neither attaches by default nor refuses `--new-session`.**
Spec (User workflow): "`./sc enter <planner>` against a managed binding resumes or attaches to that binding by default instead of opening another engine archive. An explicit `--new-session` is refused until the managed binding is released."
On main, run.py reuses a binding only via the explicit `--session-binding <id>` flag; a bare interactive boot opens a new archive and `ensure_binding` creates a fresh binding row; the `--new-session` flag does not exist in the tree. Scenario: the FnB reboots the planner mid-sprint with a bare `./sc enter pln1` while the managed binding is dormant (shell_liveness only guards against a *live* process) — the boot silently opens conversation N+1 while the dispatcher keeps resuming managed conversation N. Two conversations for one shell — precisely the split this requirement exists to make impossible ("visible rather than relying on convention"). Location: `.super-coder/scripts/run.py` main() boot flow.

**F2 · Low · unimplemented — provider-contract `interrupt` operation absent.**
Spec (Provider contract): adapters "must provide" `interrupt` (operator-only; never used by normal event delivery). No adapter, CLI, or API surface implements it. Normal delivery correctly never needs it; the gap is operator ergonomics — no sanctioned way to interrupt a stuck managed turn short of verified process kill.

**F3 · Low · deviated-silently — no re-arm-after-batch instruction in the sprint skills.**
Spec (Claude decision): "The sprint skill re-arms after every handled batch; a missing watcher becomes a visible queued/error condition." The orchestration skill only says the watcher "may keep armed"; neither skill nor the wake prompt instructs re-arming after each handled batch. Runtime degrades safely (unarmed channel → status `active` → queue → dormant resume after exit), so this is a doc/skill gap, not a correctness one.

## Issue #454 proof — provider-neutral planner wake

The #454 scenario (a Codex or Kimi planner sits on unread work forever because
only Claude's background-task watcher ever woke a planner) is closed on main:

1. **Provider-independent capture.** Any message addressed to a managed planner
   (`result`, `pr_event`, `task`, `shell`) becomes a durable wake job via
   `reconstruct_wake_jobs` (managed=1 ∧ read_at IS NULL, INSERT OR IGNORE),
   re-run every 1s dispatcher cycle — so rows written by any path are recovered
   even if the row-writer never signals anyone.
2. **Provider-specific delivery.** Codex: `turn/start` on the per-binding
   app-server socket when the thread is idle; busy threads queue (never steer).
   Kimi: authenticated loopback deliver when idle; busy queues. Claude: armed
   inbox-watcher channel. Dormant sessions on all three resume headlessly in the
   same native conversation (`claude --resume -p` / `codex exec resume` /
   `kimi --session`), lease-fenced.
3. **Acknowledgement, not exit-zero.** Jobs finish only when the planner sets
   `read_at`; a turn that exits without acknowledging retries 15s/60s/5m and then
   surfaces `error` on the binding — unread work can no longer be silently lost.
4. **Hermetic proof at the SHA:** test_session_dispatcher (coalesce/ack-by-read_at,
   API-down no-attempt, crash requeue without second writer, two-dispatcher race),
   test_codex_session_control (idle one-turn delivery never steers; unknown version
   fails closed), test_kimi_session_control (busy race queues without steer),
   test_session_control (reconstruction idempotent; manage makes pre-existing
   unread reconstructible). CI green at 2cc320e.
5. **Residual (ratified J7):** live disposable-session smokes + one real sprint per
   provider are deferred to close-out — deviated-intentionally; spec #20 remains
   unfrozen until they pass. The #454 closure claim is therefore *hermetically*
   proven now and *operationally* proven at close-out.

## Issue #439 proof — liveness / no parent-only authority

The #439 scenario (a parent-only process scan grants ownership while an orphaned
harness group survives → concurrent writers or unverified kills) cannot recur:

1. **Ownership is exact.** Every decision validates recorded PID + Linux start
   ticks + kernel cmdline identity + worktree (`process_matches`); PID reuse is
   classified stale and an old lease generation cannot release a newer owner
   (test_pid_reuse_is_stale_and_old_generation_cannot_release_new_owner).
2. **Group scan, not parent scan.** `process_group_members` walks all of /proc
   for surviving members of the recorded group; a dead leader with live members
   fences the binding into `error` with the survivor PIDs recorded — ownership is
   never transferred and nothing is killed
   (test_orphaned_process_group_fails_closed).
3. **Supervised launch.** run.py no longer execs the harness: `supervise()` runs
   it in its own process group, forwards SIGINT/TERM/HUP/QUIT (including the
   fork/exec window), and reaps the group after the leader exits so a daemonized
   descendant cannot recreate #439 under a new PID
   (test_cancelling_supervisor_terminates_real_descendant_group — asserts the
   signal reaching only the leader would "recreate #439").
4. **Healthy cleanup ≠ orphan.** Migration 0078's supervisor identity lets
   reconciliation distinguish a live supervisor reaping its group (`cleanup`,
   claim refused) from a true orphan race.
5. **Every resume is fenced.** Dispatcher reconciles before decisions, runs
   `preflight_lease` before spawn, and the spawned resume claims its own
   generation post-spawn (test_dormant_resume_runs_lease_preflight_before_adapter,
   test_dormant_probe_cannot_resume_over_a_validated_live_owner). The Claude
   ack-wait re-checks delivery ownership every 5s (SC-464) so a dead owner cannot
   wedge the dispatcher.

## Ratified judgements applied

J1 U1 edge set → as-specced. J2 retry counts → as-specced, wording = spec debt.
J3 U3/U7 endpoint/CLI boundary → moot at SHA (both shipped). J4 effort pinning =
config-effective at launch → as-specced. J5 arming-time posture validation, all
three adapters → as-specced. J6 kimi token cleanup boundary (U6 server-exit /
U7 generic release) → as-specced, release path verified fail-closed. J7 live
gates deferred → deviated-intentionally (spec stays unfrozen). J8 stale
docs_sc/job-runner.md + docs/README.md pending publish-gated unit 9 →
deviated-intentionally.

## Coverage notes

- Read at SHA: migrations 0077/0078, session_control.py, session_supervisor.py,
  session_dispatcher.py, session_cli.py, service_supervisor.py, api/server.py
  session-control handlers, all three adapter session_control.py + launch
  scripts + probes, run.py boot/exec path, watch.py channel wiring, analytics
  attribution, sprint + sprint_orchestration skills, sc launch/down.
- Not verified here: live provider transports (J7 deferral); GUI rendering beyond
  the API payload shape; U7 Low 4 (unauthenticated local session-control POST
  routes) remains an open FnB escalation on the board — pre-existing, not
  re-filed.
- Suite not re-run locally (no pytest in the stale materialized engine —
  known close-out dependency); relied on green CI at the merge SHA instead.

**Summary: 3 findings (0 Major, 1 Medium, 2 Low). #454 and #439 scenarios proven
covered hermetically; operational proof rides the deferred close-out gates. Spec
#20 correctly remains unfrozen pending J7 live gates, unit 9 publish, and the
Medium fix decision.**
