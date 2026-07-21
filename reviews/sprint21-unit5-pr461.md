# Review — Sprint 21, Unit 5: Codex app-server adapter (PR #461)

- **Reviewer:** REV1 · **Author:** DEV3 · **Spec:** doc #20 (feature 14), task #54 · **Sprint:** doc #21
- **Head reviewed:** `34b44f2` (`feat/codex-session-control`) — all 6 checks green
- **Verdict (initial):** **1 blocking finding** — 1 Medium (flag #19 / SC-462); 7 Lows for the report. Merge blocked until the Medium is fixed. No Major found.
- Dev's declared ambiguity calls: none.

## Scope reviewed

New: `codex-session.py` (per-binding app-server + remote-TUI launcher),
`codex_rpc.py` (stdlib WebSocket/JSON-RPC client, version probe),
`adapters/codex/session_control.py` (status/deliver/resume adapter,
lease-fenced `codex exec resume`), `defer_busy_batch` + `ProviderBusy`
dispatcher path, run.py controlled-launch wiring (planner-only binding
creation, `SC_SESSION_BINDING_ID`/`SC_SESSION_MODEL` env), 448 test lines.

Spec conformance verified positively: app-server over per-binding Unix
socket in a 0700 runtime dir, no TCP, endpoint stores no credentials;
`thread/start`/`thread/resume` supplies the native ID (no TTY scrape;
mismatched ID fails closed in `register_native_session`); `turn/start` only
when idle; no `turn/steer` anywhere (tested); busy races requeue via
`ProviderBusy` with the claim's attempt-count increment undone (tested);
server-loss fallback is `codex exec resume <id> <prompt>` behind a
re-probe + preflight + exact-identity lease (`_run_fenced_resume`); version
gate fails active control closed off 0.144.x while keeping smoke-probed
resume; capabilities + cli_version recorded on the binding. One-writer
invariants hold end to end: the launcher runs under `supervise` with
`preflight_lease` (double-launch refused before the socket unlink),
`command_matches` validates the python3 launcher as the codex owner via
path components, group-survivor scans fence orphaned servers, and
`terminate_group` reaps a leaderless group (#439 shape).

## Findings

### Medium — managed wake breaks fail-slow on approval-gated Codex configs (flag #19, SC-462)

The wake-turn delivery path has no answer to a server-initiated approval
request. On the designed no-docker host path (`SC_SANDBOX` unset — run.py:
"the no-docker host path keeps the harness's normal prompts"), the launcher
pins the thread's `approvalPolicy` from `config/read` — for a default Codex
config that is an approval-prompting posture. The injected wake prompt makes
the planner execute commands (`sc mem message check` at minimum), so the
**first** wake turn raises `item/commandExecution/requestApproval` on the
dispatcher's connection; `wait_notification` fails closed
(`CodexProtocolError`), the server-side turn is left to die with its client,
and the batch burns its 15s/60s/5m retry budget into a terminal `error`
binding — or worse, cycles busy-defer against a turn stuck `active` awaiting
an approval no client will ever answer. Nothing at controlled launch, arming
(`manage`), or delivery validates the posture, and the failure surfaces only
as an opaque protocol error after ~6 minutes of retries.

Fail-closed is right; fail-closed **late and unreadable** is the defect.
Sandboxed launches are immune (`danger-full-access`/`never` pinned), which
is why CI and the dev's live probe never see this.

**Proposed fix (small, unit-scoped):** the recorded settings already carry
`approval_policy` + `sandbox`. In `CodexAdapter.deliver` (and/or at launcher
registration), if the effective posture is approval-prompting
(`approval_policy` not `never`/`on-failure`-equivalent and sandbox not
`danger-full-access`), raise a clean `RuntimeError` naming the posture and
the remediation ("release, or relaunch sandboxed / with approval_policy=
never") so the binding errors immediately with an actionable
`last_error` instead of a protocol-error retry loop. Add the test. The
deeper question — whether spec #20 should *require* arming-time posture
validation for every provider (Claude unit 4 and Kimi unit 6 have the same
class of exposure) — is spec debt for the planner, noted in my result row.

## Lows (report notes — non-blocking)

- **L1** `probe_codex` propagates `FileNotFoundError`/`TimeoutExpired` when
  the codex binary is absent/wedged instead of returning a fail-closed
  capability dict: the launcher dies with a traceback rather than its clean
  `SystemExit` message; a dispatcher-side resume probe burns a retry on the
  raw error. One `except (OSError, subprocess.TimeoutExpired)` per probe call
  fixes both.
- **L2** `resume_command` pins `model_reasoning_effort` only in the
  non-`danger-full-access` branch — a sandboxed dormant resume drops the
  effort pin and re-reads it from live config, so a config edit between
  launch and resume drifts effort on exactly the posture the engine runs.
  Hoist the effort `-c` out of the else.
- **L3** Effort pinning reads the spec loosely: spec says effort is pinned
  "from the original archive"; the implementation pins the *config-derived
  effective* effort at launch (`SC_SESSION_MODEL` is exported, effort is
  not). It matches pre-existing interactive-codex behavior and stays
  consistent across active/dormant, but it is an undeclared reading — dev
  declared "ambiguity calls: none". Should be ratified or corrected.
- **L4** `wait_notification` narrows the socket timeout to the wait
  remainder and never restores `self.timeout` (harmless today — last op
  before close — a trap for reuse). Related: a `turn/start` rejected by the
  server on a concurrent-turn race surfaces as `CodexRpcError` → burns a
  retry, where the pre-check race maps to free `ProviderBusy` defer.
- **L5** Test gaps: run.py's new planner-only narrowing of binding creation
  is untested; the launcher's `thread/resume` (existing native id) path is
  untested; no test covers `probe_codex` with the binary missing (L1);
  `wait_notification`'s fail-closed-on-server-request branch is untested
  (only `request()`'s is).
- **L6** The adapter dir ships `session_control.py` — same module name as
  `scripts/session_control.py`. Exception identity for `ProviderBusy`
  currently survives on sys.path insertion order + `sys.modules` caching;
  the file is loaded by explicit path anyway, so a rename removes the trap.
- **L7** The launcher opens its per-binding server log with `O_TRUNC`,
  discarding the previous session's crash evidence on relaunch (spec asks
  for bounded logs, not zero history — append + size-cap instead). And
  during a long wake turn the single-threaded dispatcher blocks in
  `wait_notification` (up to 3600s) without heartbeating, so the
  `daemon_heartbeats` surface can misreport a healthy dispatcher as dead;
  other managed bindings starve for the duration. Structural, pre-existing
  shape from unit 3 — materialized by codex's blocking deliver; fine for
  one-planner sprints, worth a note in unit 7's status surface.

## Deferred-risk note (not a finding)

The dev's live verification covered handshake, `thread/list`, and
`config/read` against Codex 0.144.6 — not `thread/read` status shapes,
`turn/start` delivery, or `turn/completed` notification shape, which are
exercised only against fixtures here. That matches the delivery plan (the
opt-in provider smoke + conformance gates land in unit 8), but the
status-mapping surface (`notLoaded`→idle, unknown→error) is where a point
release will bite first.
