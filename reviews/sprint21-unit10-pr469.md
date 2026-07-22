# Review — Sprint 21 unit 10: PR #469 vs conformance doc #22 F1 / spec doc #20

- PR: #469 `fix(session): attach managed enter bindings` (fix/managed-enter-binding @6b54ac8, DEV4)
- Scope: conformance F1 (Medium) — `./sc enter <planner>` managed-binding attach by
  default + `--new-session` refusal until release, in run.py.
- Checks at head: tests ✅ verify ✅ render-check ✅ CodeQL ×3 ✅
- Dev's declared ambiguity calls: none.
- Verdict: **1 Medium, 3 Low** — not review-clean; fix + re-review.

## What the diff does (verified, not trusted)

- `session_supervisor.binding_for_enter(con, shell_id, new_session=)` — selects the
  shell's `managed=1` binding (one-managed-per-shell unique index makes `fetchone`
  sound); returns None when absent; raises actionable ValueError on `--new-session`
  while managed.
- run.py main(): bare interactive enter feeds that binding into the pre-existing
  `binding_for_resume` → `open_session(reuse_archive_id=…)` → controlled-launch →
  lease preflight/claim machinery (the path `--session-binding` already used and
  units 3/4/5/6 + conformance already verified). Headless discards it (dispatcher
  names bindings explicitly) — consistent with its comment. `--new-session` +
  `--session-binding` refused; `--new-session` after release passes
  `force_new=True` so `open_session` skips unused-stub reuse (genuinely new
  archive; tested).
- Flag plumbing verified end to end: `sc enter --new-session` → `docker exec ./sc
  boot --new-session` → `run.py`; parser can't mistake `--new-session` for the
  shortname positional. Help text matches behavior.
- Release path (`release_session_control`) clears `managed=0` in the same
  transaction as `state='released'` — no released-but-still-managed wedge; a
  released binding can never be re-selected by `binding_for_enter` (and
  `binding_for_resume` refuses `released` as belt-and-braces).
- Model pinning honored on the attach path (`archive_model` overrides, explicit
  `-m` mismatch refused). Boot summary prints the binding line on attach. On
  harness exit, `release_lease` returns the binding to `dormant` (native id
  present), so the dispatcher keeps waking it — managed lifecycle preserved.
- Tests: helper-level (select/refuse/release+force_new) + main-flow routing tests
  that drive the real `main()` to the `open_session` call and assert
  `reuse_archive_id`/`force_new`/lifecycle. `test_style_spinner` fixture updated
  for the new lookup. Good shape; gaps below.

## Findings

### M1 (Medium) — bare enter is refused outright for a managed binding in state `error`, with a state-machine-internals message; the discoverable escape cancels queued wake jobs

Path (all verified at 6b54ac8):

1. `binding_for_enter` selects `managed=1` with **no state filter**;
   `binding_for_resume` refuses only `released`. An `error`-state binding — the
   spec's own designed terminal outcome ("terminal → error"; retries exhausted,
   owner-exit without native id, orphan-group fence) — flows through.
2. `preflight_lease` passes (`reconcile_binding` reports the *owner* vacant and
   deliberately keeps `state='error'` at error, session_supervisor.py:494-495).
3. The harness child is spawned, then `claim_lease` calls
   `transition_binding(expected='error', target='foreground')` →
   `_NEXT_STATES["error"] == {starting, released}` → `InvalidStateTransition`.
   `supervise`'s BaseException handler kills the child group cleanly (no orphan,
   no #439 residue), `InvalidStateTransition` IS a RuntimeError so main catches
   it, and the boot dies with:
   `session launch refused: invalid session binding transition: error -> foreground`.
4. The operator's next move, `--new-session`, is also refused ("requires
   releasing managed binding N first"). Neither message names the sanctioned
   recovery (`sc session retry <shortname>`, error→starting, after which enter
   works: starting→foreground is an allowed edge). The only remedy either
   message *does* name is release — and `release_session_control` cancels all
   queued/failed wake jobs. So the realistic mid-sprint outcome is the operator
   releasing the managed conversation to get a prompt, destroying the queue —
   operator-induced, but the design funnels them there.

Why Medium, not Low: `error` is exactly the state in which the FnB will want to
enter the planner (the binding surfaced `error` because deliveries failed), the
spec's F1 sentence ("against a managed binding resumes or attaches … by
default") carves out no state, and ratified J1 already names the recovery edge
("error … recover only through starting"). Pre-PR the explicit
`--session-binding` path had the same behavior, but this PR makes it the
*default* boot path for every managed planner. Not Major: fail-closed, no data
loss, child cleanly killed, recoverable by an operator who knows `retry`.

Proposed fix (dev's choice of mechanism): route the interactive claim from
`error` through `starting` (error→starting→foreground, matching J1), or have
`binding_for_enter`/main refuse *early* (before spawn) with a message that names
`sc session retry <shortname>` and release as the two exits. Either way, add a
regression test for enter-on-error.

Flag: SC-466 (feature 14).

### L1 (Low) — harness-override mismatch refusal is context-free, and env `HARNESS` outranks the binding's pinned harness

Resolution order is `--harness` / `HARNESS` env → binding harness → picker. An
operator with `HARNESS=codex` exported (a legitimate standing preference) doing
a bare enter of a claude-pinned managed planner gets
`session resume refused: session binding belongs to a different shell or harness`
— which doesn't say a managed binding was auto-selected, which harness it pins,
or how to proceed. Fail-closed is correct (a claude conversation can't resume
under codex); the message and precedence deserve a look. No test covers this
path.

### L2 (Low) — `--new-session` semantics leak into headless

`binding_for_enter` runs (and its `--new-session` refusal fires) before the
`if headless: enter_binding = None` reset, so `./sc run <planner> --new-session`
is refused while a managed binding exists, and absent one, `force_new=True`
skips stub reuse headless too — despite the adjacent comment saying headless
keeps its existing ephemeral behavior and the attach contract is
interactive-only. Harmless in practice (nothing passes the flag to `sc run`);
noted for the report.

### L3 (Low) — test gaps on the refusal edges

New tests cover the happy attach, the managed refusal, and post-release
`force_new`, but not enter-on-`error` (M1) or the harness-mismatch refusal (L1).
Fold into the M1 fix.

## Edge cases traced and found sound

- `dispatching`/live-owner at enter: `preflight_lease` → LeaseConflict → clean
  refusal before spawn; transient, retryable. Interactive live-session guard
  (`confirm_live`) also fires earlier for live cases.
- Fresh planner (no binding): unchanged path, `ensure_binding` after
  `open_session`; no duplicate binding on attach (ensure skipped when
  `resume_binding` set).
- Non-planner shells: no binding rows → `binding_for_enter` returns None →
  behavior unchanged; one cheap indexed query per boot.
- Managed binding with missing archive row: JOIN drops it → None (FK makes this
  unreachable anyway).
- `--new-session` message names the binding id and the release requirement —
  actionable. Refusal happens before any archive is opened (tested).
- Picker path (bare `./sc enter`, no shortname) resolves shell first, then
  binding — attach works from the picker too.
