# Review — Sprint 31 (doc #31) Unit 9b · PR #542 @ef70af6

- **Unit:** `./sc token` + `make dos-token` — owner-only runtime credential read to stdout (spec #30 req 23)
- **Author:** DEV6 · **Reviewer:** REV2 · **Branch:** `feat/s31-sc-token` → `main`, 3 commits, +195/−3
- **CI:** green 6/6 per dev report; CodeQL alert #18 dismissed won't-fix (FnB-approved false positive — req 23 mandates the stdout print; inline suppression sits on the flagged line with a req-23 justification comment after the ef70af6 placement fix).

## Verdict: REVIEW-CLEAN — 0 Major / 0 Medium / 2 Low (+1 watch-item for 9a)

## Axis 1 — Code quality

- `operator_token.py` reuses `mem._discover_runtime_credential` / `_load_runtime_credential` instead of duplicating the trust-boundary logic — the right call: one security check, one home. The `mem._PROG` override makes refusals name `sc token`, not `mem`. Named `operator_token.py` to avoid shadowing stdlib `token` — documented in the module docstring.
- `sc` dispatcher line is the same one-line exec pattern as every other verb; help text labels the value an operator capability and never prints it. `aliases.mk`: `dos-token` is an exact alias (`$(SC) token`, no args), in `.PHONY`, in the LONG-ONLY list, and has a `dos-help` row.
- Verified in `mem.py`: `_load_runtime_credential` enforces regular-file + owner-euid + no group/world bits before reading; malformed JSON refuses; `_discover_runtime_credential` refuses ambiguity (multiple Admin artifacts) unless `SC_MEM_AS` names one. `sc token` inherits all of it.
- Artifact-only confirmed by reading the code: `sc token` calls discovery unconditionally, which overwrites the module globals from the artifact — an injected `SC_API_TOKEN` is never printed, and a missing artifact refuses even when env is fully wired (pinned by `test_env_wiring_never_substitutes_for_the_artifact`).

## Axis 2 — Edge cases & gaps

- Missing artifact → refusal naming `./sc restart` / `make dos-r` ✓ (pinned). Unreadable/malformed/insecure-mode → distinct refusals, none leak the value (asserted: token absent from the refusal text) ✓. Ambiguous multi-Admin → refusal naming `SC_MEM_AS` ✓. Selection among Admins works ✓.
- Stdout purity pinned: output is exactly `token + "\n"`, both in-process and via a real-interpreter subprocess run.
- **Low 1 — "distinct nonzero result" satisfied at message level only.** Spec #30 (Make-surface section, token retrieval paragraph) asks for "a distinct nonzero result for service-not-running versus unsafe permissions". Both refusals exit 1 via `die()` — distinct stderr diagnoses, identical exit code. If the FnB reads "distinct" as distinct exit codes (scripting), that's a planner/conformance call; the message-level distinction is arguably the plain reading. Low.
- **Low 2 — junk argv silently prints.** `sc token anything-but-help` ignores the argument and prints the token. Harmless for a read-only one-shot, but a usage refusal would be tidier. Low.
- **Watch-item (not a 9b defect):** spec's "automated help coverage prevents a documented target from disappearing or dispatching a different command" is unit 9a's surface; no test pins the new `dos-help` row / `dos-token` dispatch yet. 9a's reviewer should confirm the coverage test picks up the new target.

## Axis 3 — Spec conformance (doc #30 req 23)

- Prints the current browser operator token and only that token to stdout ✓ (purity pinned twice).
- Never rotates, never in command arguments, never in logs ✓ (no write path exists in the command).
- Missing/unreadable/insecurely-permissioned artifact refuses on stderr with the supported service action ✓ (sys.exit string → stderr, exit 1, names `./sc restart` / `make dos-r`).
- Help labels the value as an operator capability and does not print it ✓ (`--help`, `sc` help, `dos-help` all label without the value; pinned).
- Token-retrieval paragraph: reads the same owner-only artifact ✓, verifies ownership/mode before reading ✓, no decorative prefix ✓, no token material added to URL/page source/snapshot/client persistence ✓ (diff touches no browser-auth code).

### Ambiguity calls (dev-declared, reviewer-ratified)

1. Artifact-only read — env never substitutes — **ratified**: the spec names the artifact as the source; env substitution would bypass the permission contract.
2. No freshness API call — artifact current by construction — **ratified**: provisioning refreshes at every boot including key rotation (pinned by `test_refresh_picks_up_key_rotation`); a stale artifact means the service is down, which the refusal path already covers.

## Tests (test_authoring lens)

Strong: stdout purity is asserted exactly (`== TOKEN + "\n"`), refusal tests assert the token does NOT leak into the refusal text (a leak turns red), env-no-substitute is pinned against a fully-wired env, and the end-to-end test runs the real interpreter as a subprocess. 23/23 green locally (this review, temp worktree @ef70af6, `python -m unittest tests.test_runtime_credentials`).

## Recommendation

**Review-clean — clear to merge on green.** Lows 1–2 + the 9a watch-item to the sprint report.
