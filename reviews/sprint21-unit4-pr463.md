# Sprint 21 · Unit 4 review — PR #463 vs spec doc #20 (task #53)

- **PR:** #463 `feat(session): add Claude session control` @397b173e (branch `feat/claude-session-control`, base `main`)
- **Dev:** DEV4 (shell #6) · **Reviewer:** REV2 · ambiguity calls declared: none
- **Scope:** Claude vertical slice (delivery plan step 3): supplied-UUID controlled launch, inbox-watcher active channel, lease-fenced dormant `--resume -p` fallback, `read_at`-only acknowledgement.
- **Verdict:** 1 Medium (SC-464, flag #21) blocks; 6 Lows for the sprint report. No Majors.

## Findings

### Medium — SC-464 (flag #21): deliver ack-wait has no liveness re-check
`adapters/claude/session_control.py` — `ClaudeAdapter.deliver` → `_wait_for_ack`
polls only `session_wake_jobs(running) ⋈ shell_messages(read_at IS NULL)` every
0.2 s until `ACK_TIMEOUT = 4h`. It never re-reads the binding: if the planner
process dies after `claim_batch` (SIGKILL/OOM/operator closes the terminal —
the watcher dies with the harness group, so nobody will ever mark the messages
read), the dispatcher — which is single-threaded (`poll_once` blocks in
`adapter.deliver`) — wedges for the full 4 hours. Every other managed binding
is starved, and the binding's own recovery (reconcile → dormant → fenced
resume, which works) is deferred until the timeout expires. Crash-mid-turn is
a spec-emphasized scenario; the sibling path (resume exits before ack) is
correctly bounded by `finish_batch`'s 15s/60s/5m ladder, so this wait is the
one unbounded-in-practice hole.
**Fix shape:** inside the wait loop, every few seconds re-read the binding and
bail (raise, so `finish_batch` requeues with error) when the lease is vacated
*and* the active-channel heartbeat is stale — the dormant path then recovers on
the next cycle. Backing the 0.2 s poll off to ~1–2 s at the same time also
resolves L1.

### Lows
- **L1** — `_running_unread` opens a fresh SQLite connection per 0.2 s poll
  (~72k connections across a wedged wait). Fold into the SC-464 fix.
- **L2** — deliver path returns the binding `dispatching → idle` while a
  foreground interactive client still owns the conversation (claim was from
  `foreground`). Harmless to this adapter (status derives from lease+channel),
  but `sc session status` (unit 7) will render a foreground session as `idle`.
- **L3** — `resume_environment` forges `IS_SANDBOX=1` whenever the recorded
  permission is `bypassPermissions`, wherever the dispatcher runs (host). It
  mirrors the sandbox-only launch env in `adapter.json`, but on the host it
  defeats claude's root-refusal guard — and the path is doubly dead anyway: a
  sandbox-born binding's transcripts don't exist on the host, and
  `register_active_channel`'s host-`/proc` cwd validation already keeps
  in-container watchers from ever registering. Suggest gating on `SC_SANDBOX`
  or a comment stating the intent. (Precedent note: codex host-side resume with
  `danger-full-access` merged in unit 5, so this is parity, not novel scope.)
- **L4** — `./sc watch inbox --interval` > 90 makes the heartbeat gap exceed
  `CHANNEL_HEARTBEAT_MAX_AGE=90`: the channel flaps not-ready between beats and
  delivery degrades to watcher-fired-only. Heartbeat cadence should be
  `min(interval, ~30)` or the interval validated at register.
- **L5** — test gaps: `_wait_for_ack`'s TimeoutError path, the watcher's
  clear-on-timeout / clear-on-API-down paths, and `resume_environment` are
  untested. The rest of the suite is well-shaped (argv pinning, fence
  preflight, posture refusal, register/heartbeat/clear identity).
- **L6** — a displaced watcher (second register overwrote identity) sees its
  heartbeat 409 folded into `_ApiDown` and dies after 10 loops as
  "API unreachable" — misleading; the 409 body says "active channel identity
  changed" and could be surfaced as such.

## Verified sound (traced, not trusted)
- **Posture, empirically:** `claude --permission-mode auto -p` auto-approves
  Bash headless (ran it against the installed 2.1.216) — the dormant-resume
  posture works despite run.py's "headless auto-denies" comment (that comment
  describes default mode). The U5 arming ruling is satisfied: claude records
  `settings.permission_mode`, and this PR widens the shared validator's
  vocabulary to `auto|yolo|bypassPermissions` — so `sc session manage` gates
  claude bindings exactly as ruled. The widening nominally lets other providers
  record `bypassPermissions`, but none do; noted, not a defect.
- **Probe/version gate:** installed CLI 2.1.216 ⇒ `(2,1) ∈ TESTED_VERSIONS`;
  unknown versions fail active delivery closed but keep flag-tested resume —
  exactly the spec's "smoke-tested resume command" allowance.
- **#439 fencing:** `_run_fenced_resume` is structurally identical to the
  kimi pattern reviewed in unit 6 — preflight before spawn, claim with exact
  pid/start-ticks after, release with rc on exit; `supervise` group-terminates
  descendants. No adapter-side kill of unverified processes.
- **Ack discipline:** delivery completes only via `read_at` (deliver waits on
  it; resume defers to `finish_batch`); nothing marks messages read on the
  planner's behalf; `deliver` ignores the prompt by design (spec sanctions the
  background-task notification as v1 active transport, `_prompt` unused).
- **Watcher channel:** registers before blocking, heartbeats each loop
  (default 30 s < 90 s window), clears in `finally` on fire/timeout/give-up;
  identity is server-derived start-ticks, heartbeat/clear are CAS on
  (pid, ticks). `_api` on main already accepts POST payloads.
- **Pinning:** first launch records model/effort from `SC_SESSION_*`, relaunch
  prefers stored settings over env (`--resume` + recorded route) — U5/L3
  config-effective-at-launch ruling honored; resume falls back to
  `archive_model`, never a silent different model.
- **Dispatcher fit:** status vocabulary maps correctly — live-without-watcher
  → `active` (queue, per "never assumed deliverable"); vacant lease →
  `dormant`; pre-registration → `starting`. `foreground → dispatching` claim
  matches the U1 ratified graph. ProviderBusy on fired-watcher race defers
  without burning an attempt; already-read batches complete without a re-armed
  watcher. Token cleanup (SC-463 analog): N/A — claude has no control endpoint
  or token file.
- **Non-goals respected:** no steer path, no fresh-session substitution
  (`register_native_session` refuses a different id; resume requires the
  recorded native id), no inbox bodies in any prompt.

## Recommendation
Fix SC-464 (bounded, in-adapter), push, re-request review. Lows are report
notes, not gates. Unit 7 should inherit L2 (status rendering) and L4 (interval
validation) context.
