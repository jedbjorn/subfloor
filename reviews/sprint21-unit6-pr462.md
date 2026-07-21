# Sprint 21 · Unit 6 — PR #462 review

**Unit:** Kimi K3 session-control adapter (task #55) · **Dev:** DEV3 · **Reviewer:** REV2
**Diff:** `feat/kimi-session-control` @ d8d06f0 vs `main` · **Spec:** doc #20 (feature 14) · **Checks:** all green

## Scope of the diff

New: `adapters/kimi/kimi-session.py` (controlled launcher: spawn authenticated
loopback server, capture native session ID, pin route/effort/posture, register
binding), `adapters/kimi/kimi_http.py` (REST client + CLI capability probe),
`adapters/kimi/session_control.py` (status/deliver/resume transport, fenced CLI
resume). Modified: provider-generic `validate_managed_wake_posture` gains the
Kimi-native `permission_mode ∈ {auto, yolo}` rule; `run.py` exports
`SC_SESSION_EFFORT`; adapter.json declares `session_control.launch`. Tests:
494-line hermetic suite + arming-rejection API test.

## Verified (adversarial checks that came back clean)

- **CLI resume posture (chased hard, refuted).** The resume command
  `kimi --session <id> --model kimi-code/k3 --prompt <p>` carries no
  `--auto`/`--yolo`. Verified against the installed Kimi 0.27.0 CLI and the
  official command reference: `-p` mode runs "under the `auto` permission
  policy" by definition, `--prompt` *conflicts* with `--auto`/`--yolo` (flag
  error), and a resumed session retains its saved permission mode. The built
  command is exactly right; adding a posture flag would break it.
- **U5/PLN1 ruling applied correctly.** Posture validation is provider-generic:
  Kimi records native `permission_mode`; absent keys pass untouched; no Codex
  `approval_policy`/`sandbox` keys invented. Enforced at arming (API test
  proves rejection without mutation), at deliver, and at resume.
- **Config-effective-at-launch (ratified L3).** Launcher round-trips the
  profile through the server, records the *server-reported* effective
  model/effort/permission, and hard-fails on drift before binding
  registration. Every injected prompt reasserts model/effort/permission_mode.
- **Dispatcher contract.** `binding.get("shortname"/"flavor"/"archive_model")`
  all exist in the dispatcher's SELECT (b.*, s.shortname, s.flavor,
  a.model AS archive_model). `session_effort` binds in run.py. Supervisor
  calls (register_native_session, preflight/claim/release_lease, supervise,
  read_process, expected_worktree) match unit-2 signatures. Fenced resume is a
  faithful mirror of the codex adapter's lease choreography.
- **No steer, engine-queue policy.** No steer endpoint anywhere; busy →
  `ProviderBusy` (queue stays in engine); test asserts no `:steer` URL.
- **Version gate.** 0.27/0.28 minor-tuple gate; unknown version → no
  server_command → launcher refuses (fail closed); resume separately probed
  live at resume time. `-m kimi-code/k3` matches the proven headless
  convention (adapter.json `model_flag: -m`).
- **Auth hygiene.** Endpoint validation rejects non-loopback, credentials,
  query, fragment (stricter than codex); token file 0600 in 0700 dir, exact
  content tested; banner and API-error redaction tested; endpoint stored
  without token.

## Findings

### M1 (Medium, blocks) — control token is never deleted; spec says "deleted on release"

Spec #20, data model: "Tokens live in a mode-0600 runtime file or inherited
environment **and are deleted on release**." This unit introduces the engine's
first bearer-token runtime file (`run/session-control/kimi-<binding>.token`) —
and nothing ever unlinks it. The launcher's `finally` terminates the server
but leaves the token; the generic `release_session_control` (unit 3) has no
provider-cleanup hook; the adapter exposes no release operation (consistent
with codex, which needs none — its socket dies with the server).

Failure scenario: sprint closes / FnB releases the binding → a valid bearer
token for a possibly still-running loopback server persists on disk
indefinitely. Loopback + 0600 bounds the real risk, but it is an explicit
spec sentence going silently unimplemented in the unit that makes it live.

Proposed minimal fix in unit-6 scope: unlink `token_path(binding_id)` in the
launcher's `finally` (server death invalidates the token anyway, so
launcher-exit deletion covers the dominant path). Release-while-server-lives
deletion needs a hook at the release surface — if PLN1 rules that half into
unit 7 (`sc session release`), note it there; the launcher-side unlink is
still owed here. Flag SC-463.

### Lows (sprint-report notes, non-blocking)

- **L1 — wait_prompt infers completion indirectly.** Once the prompt id
  leaves active/queued it decides on global `busy` + `last_turn_reason`.
  (a) If enqueue were async, a first poll could miss the prompt and read a
  stale `last_turn_reason` — false fail (duplicate delivery later; tolerable
  under at-least-once) or false success. (b) A foreground turn started right
  after our wake completes keeps `deliver` blocked until global idle (up to
  4 h) and can end in TimeoutError for a delivery that succeeded. Self-healing
  via read_at acking, but returning as soon as our prompt id disappears (or a
  per-prompt status read, if the API offers one) would be tighter. Codex gets
  exact per-turn completion via `turn/completed`; Kimi's REST may not offer an
  equivalent — worth one line in the code saying so.
- **L2 — hard-coded K3.** Launcher and `resume_command` refuse any route ≠
  `kimi-code/k3` literally, while spec says K3 holds "unless the FnB
  explicitly changes the route." An FnB route change cannot flow through;
  failure is loud, and task #55 says preserve K3, so defensible — but it's a
  latent contradiction with the spec sentence. Spec debt candidate.
- **L3 — banner parsing untested; 0.28 fixture-validated only.** No unit test
  covers `wait_for_server`'s `_URL_PATTERN`/`_TOKEN_PATTERN`; the real smoke
  ran on 0.27 (`server run` tree), so 0.28's `kimi web` banner format is
  assumed. Fails closed if wrong; conformance sprint (unit 8) will exercise it.
- **L4 — deliver's model fallback is looser than resume's.** `deliver` submits
  `settings.model or archive_model or ""` with no K3/non-empty validation,
  while `resume_command` hard-validates. Guarded upstream by the
  `active_delivery` capability gate, so near-contrived — but inconsistent
  strictness.
- **L5 — status() maps a missing/invalid `control_endpoint` to `error`** (via
  ValueError) even when the truth may be `dormant`. Narrow: registration
  always sets the endpoint.
- **L6 — test nits.** `client_factory=lambda ...: (self.assertEqual(...) or
  client)` hides an assertion in a factory expression; no test covers
  `deliver` refusing when recorded `active_delivery` is false.

## Verdict

**1 Medium (SC-463), 0 Major.** Fix M1, push, re-request — re-review will be
quick. Lows go to the sprint report; none block. Everything else checked out
under an adversarial pass that specifically tried to break resume posture,
route/effort preservation, lease fencing, and auth handling — and failed.
