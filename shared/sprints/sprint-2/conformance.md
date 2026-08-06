# Sprint 2 conformance — Windows VM Driving Hardening (feature #38)

Reviewer: REV1. Judged against spec document #108 at bound revision
`4b1b2cf76b75e38eddcc436c99e85ed9048103a0003c69029e02c60665a626e4`, which is
still the document's current revision; zero mid-Sprint spec edits are recorded.
Integrated target: `jedbjorn/subfloor` `origin/main` @ `f9dbc9c`, the tip after
all seven Sprint PRs merged. Pre-Sprint base for diffing: `07f1c0b`.

## Method

I compiled the evidence packet myself (`compile-report --limit 50`, no
truncation counters set on any section), read the spec body in full, diffed
`07f1c0b..origin/main`, traced the code paths named in each acceptance clause,
and ran the suite on a clean checkout at `f9dbc9c`.

Test result on integrated main: **1939 passed, 945 subtests passed** (132s),
full `tests/` suite. The six VM/adapter/skill files alone: 178 passed, 39
subtests. No skips, no xfails, no red.

## Requirement classification

**as-specced** — verified in code at `f9dbc9c`, not taken from PR prose:

- **#1020** relay `up` reaches a verified listener or returns bind/exit
  evidence; stale or reused PID state cannot signal an unrelated process.
  `test_bind_exit_returns_bounded_log_and_cleans_all_state`,
  `test_recycled_container_pid_fails_start_identity_check`,
  `test_mismatched_record_is_never_signaled`.
- **#1021 / #1023** adapter-declared MCP injection, no Claude-only command, no
  duplicate registration, stable across repeat launches.
  `run.py:managed_mcp_injection` consumes only adapter-supplied `launch_args` /
  `merge_json`, so no harness-name switch exists in the launcher.
  `test_capability_matrix_is_explicit`,
  `test_supported_adapter_recipes_match_harness_goldens`,
  `test_repeat_opencode_launch_merge_preserves_siblings_without_duplicates`.
- **#1022** off and already-running domains reach bounded SSH-ready results
  through `start`, attempts and last error preserved on timeout.
  `test_start_powers_on_off_domain_once_then_waits`,
  `test_start_timeout_preserves_attempts_and_last_error`.
- **#1024** multiline PowerShell with quotes, `$` variables, spaced paths,
  pipes, backticks, and Unicode reaches `exec` unchanged — proven at the wire,
  not just the API: `test_broker_wire_payload_round_trips_complex_command_as_
  utf8_json` asserts the exact UTF-8 JSON body on the socket. See the ratified
  deviation below for the return leg.
- **#1025** capture writes a viewable atomic mode-0600 artifact and removes
  partial/oversized output; path-escape and symlink outputs are refused.
  `test_oversized_capture_preserves_existing_target_and_creates_no_partial`,
  `test_failed_atomic_replace_removes_partial_and_preserves_existing_target`.
- **#1026** powered-off reset carries a client budget exceeding the broker's,
  returns a parseable result and observed domain state, and an uncertain result
  becomes `reset_result_unknown` and is never auto-retried.
  `test_reset_json_matches_golden_shape_and_exceeds_broker_budget`,
  `test_reset_malformed_response_is_unknown_and_never_retried`.
- **SC-467** relay state, identity validation, logs, and shutdown on host and
  Docker seats. `test_recycled_container_pid_fails_start_identity_check`,
  `test_down_removes_namespace_stale_state_without_signaling`.
- **SC-468** one guarded response per broker request with bounded, payload-safe
  logs. `SingleResponseTests` covers disconnect, handler exception on
  GET/PUT/POST, and the malformed-reset body that must not fall through to a
  default running reset.

**deviated-intentionally (ratified)** — guest console stdout transliterates
non-ASCII on the return leg (`—`→`-`, `ø`→`o`), observed in both acceptance
runs. Unit 15 / PR #1048 resolved this by documenting it in `windows_devkit`
with a base64 workaround rather than by changing transport. I hold that as
correct: the acceptance clause binds the inbound leg ("reaches `exec`
unchanged"), which is proven byte-exact on the socket, and the corruption is
the Windows guest console codepage, outside the engine's boundary. Ratified by
REV2, judgment #56, unit 15.

**unimplemented / unproven** — see findings 1 and 2. Nothing is
`deviated-silently`.

## The unproven acceptance gates, and why no code fixes them

Spec line 182 sets a minimum — a live Codex launch must show the Windows MCP
tools on its first turn — and the downstream gate (line 188) requires `mcp up`,
one GUI observation/action, and `mcp down` in one `rst-c` session. Two
acceptance runs (unit 12 @ 12:24, unit 14 @ 13:29, the latter after the unit
13/15 re-entry fixes merged) both failed the same way: `mcp up` returned
`mcp_adapter_unsupported` / `SC_HARNESS is not set`, and no GUI action was
possible or claimed.

I traced the cause rather than accept the symptom. `SC_HARNESS` is set only by
`run.py` at launch and is new in this Sprint (absent at `07f1c0b`, added in
#1035, merged 11:04). `managed_mcp_injection` is additionally gated on
`linked_vm_configured()`. I checked both repos directly: **subfloor has no `vm`
block; rst-c does.** Both acceptance runs were performed by subfloor-launched
shells that reached rst-c by `cd`, so they could not carry `SC_HARNESS` from a
pre-#1035 session and would never receive an injected MCP definition even if
relaunched in subfloor — that gating is correct and is itself covered by
`test_unlinked_fork_has_no_managed_recipe_for_supported_adapters`.

So the engine behaved exactly as specced, including refusing `up` before any
transport mutation (`vm.py:1774`) and emitting the
`recovery: relaunch_through_engine` hint added by unit 13. The gate is unmet
because the observation requires a shell **launched inside rst-c** on an engine
pinned to `f9dbc9c` — rst-c is currently pinned to `e4778ec`, which is not an
ancestor of subfloor's main. That is an operator/deployment act, not a delivery
lane. Cutting another work unit would repeat the pattern that already consumed
one re-entry episode without closing the gap, which is why I route these as
post-Sprint follow-ups rather than in-Sprint patching.

Both Developers correctly reported these as unconfirmed and claimed no success,
satisfying spec line 192.

## Conclusion

All nine planned units are terminal and completed; seven PRs merged; every
automated acceptance clause in spec #108 is met and green on integrated main.
The two live-session acceptance gates remain unproven for an environmental
reason with no code remedy in scope, and are recorded as follow-ups for FnB
disposition. Conformance: **pass with follow-ups.**
