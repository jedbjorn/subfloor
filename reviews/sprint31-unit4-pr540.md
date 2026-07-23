# Review — Sprint 31 (doc #31) Unit 4 · PR #540

- **Unit:** Admin API + CLI parity — runtime Admin credential discovery (#516, spec #30 req 11) + lazy websockets (#518, req 12)
- **Author:** DEV6 (Code-04) · **Reviewer:** REV2 · **Branch:** `feat/s31-admin-api-cli-parity` → `main` @ `05db571`
- **CI:** green (tests / verify / render-check / CodeQL / Analyze) — verified via `gh pr checks`, not the dev's claim.

## Verdict: REVIEW-CLEAN — 0 Major / 0 Medium / 4 Low

## Axis 1 — Code quality

- `server.py:2714` call site: `port` is resolved at the top of `main()` (argv or `ports_mod.resolve()`), so the artifact's `api_base` is the real bound port. Placement directly after `backfill_shell_api_keys.backfill` means a boot-time rotation is picked up by the rewrite. Verified against FETCH_HEAD, not the PR description.
- `mem_credentials.provision`: correct guard set — unkeyed shells skipped, path-traversal shortnames (`/`, leading `.`) refused, unconditional `chmod 0600` repairs a weakened artifact (O_CREATE mode only applies at creation — the comment is accurate), stale sweep covers gone/demoted/deleted/unkeyed shells.
- `mem.py` discovery: env-wins short-circuit first, discovery only when BOTH vars absent (partial wiring still dies with the precise missing-var message — spec-conformant). `_api` reads module globals at call time, so adopted wiring takes effect; 401 + discovered credential dies with the stale-credential remediation instead of a generic HTTP error.
- `sc` `ifpy`: preference-not-gate, falls back to `$PY`; sole caller is the `interface` verb. `exec "$(ifpy)"` can't exec an empty string anymore since ifpy now always succeeds.
- Import-chain claim verified adversarially: `interface_cli → run → {compose, flat, db_driver, install, git_prune → git_hygiene}` plus `style`/`ports` — all stdlib-only at module level. The PR's "stdlib-clean — verified" statement is true.
- `_ws_connect` reached only via `run_stream` (attach/view/take-control/enter-reattach); missing package dies with EXIT_API_DOWN=3 naming `./sc deps` / `./sc build` and the HTTP verbs that still work.

## Axis 2 — Edge cases & gaps

Probed: partial env wiring (dies correctly), zero artifacts (original unwired death preserved + new path named), multiple artifacts (ambiguous → SC_MEM_AS), SC_MEM_AS unknown/case-insensitive, non-regular/wrong-owner/wrong-mode artifact (refused), malformed JSON / missing keys (refused), rotated token (401 → stale remediation), dir unreadable (returns False → unwired death). All covered by tests that run the real `server.Handler` auth path — a realistic bug (env losing to artifact, weakened perms accepted, sweep dropping live artifacts) would turn these red.

Lows (to sprint report, non-blocking):

1. **Boot-rewrite race:** provision rewrites artifacts with O_TRUNC at boot; a concurrent host `sc mem` reading mid-rewrite gets the "malformed — restart" refusal. Transient (retry succeeds) but the message points at restart rather than retry. Low.
2. **Symlink following in provision:** `os.open(O_TRUNC)` follows a planted symlink in `run/mem/` and would truncate/write its target. Dir is 0700 owner-only and the local trust boundary already assumes a non-hostile same-user — consistent with the operator-token precedent — but `O_NOFOLLOW` would be cheap. Low.
3. **Discovery checks the artifact file's mode, not the parent dir's 0700.** A weakened dir leaks shortnames (filenames) only; file content stays 0600. Acceptable under the trust boundary. Low.
4. **watch.py keeps mem's old unwired gate** (verified at FETCH_HEAD: same both-vars check, no discovery). Dev declared this as a deliberate follow-up; spec req 11 scopes discovery to `sc mem`. Ratified as follow-up — a host Admin seat running `./sc watch` unwired still dies without the artifact path named. Low.

## Axis 3 — Spec conformance (doc #30)

Req 11 — conformant: supervised service provisions one 0600/0700 artifact per live keyed Admin at every boot ✓; discovery only when both API vars absent ✓; still API-only, never direct-DB ✓; ambiguity refuses and asks for an explicit shortname ✓; artifact gitignored (`/.super-coder/run/`, .gitignore:31) and never snapshotted (snapshot serializes DB tables only) ✓; rotation refresh at boot ✓; accepted only under the local trust boundary (regular file, owner, no group/world bits) ✓.

Req 12 — conformant: HTTP verbs run on stdlib python ✓; only stream verbs load/check websockets ✓; status/start/stop/reconcile unchanged against the published API ✓; missing package names the exact dependency action ✓.

Ambiguity calls (dev-declared, reviewer-ratified): `SC_MEM_AS` env selector over a CLI flag (zero argparse surface, matches env wiring model — reasonable); artifact carries `api_base` (self-describing, same URL both sides of the bind mount — reasonable); watch.py gate deferred (out of spec scope — recorded as Low #4).

## Recommendation

review-clean. Dev may merge under scoped sprint authority (green CI + this declaration + ACTIVE doc). Lows 1–4 to the sprint report; Low 4 is the natural follow-up unit candidate.
