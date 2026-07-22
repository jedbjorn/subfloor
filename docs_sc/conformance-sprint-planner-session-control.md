---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint eventing — GitHub→inbox daemon + headless worker boot
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE (amended, seq 2): Sprint planner session control — F1 re-run

sprint: doc #21 · spec: doc #20 (feature 14) · judged: main @ 90866a6afb6047937dba10b4b7d9368b36c773b6 (unit 10, PR #469)
reviewer: REV1 (conformance slot) · date: 2026-07-22 · kickoff: msg #299 (PLN1) — scoped re-run, F1 only + SC-466
CI at SHA: tests ✅ · render-check ✅ (gh run list --commit 90866a6)
delta bound: 2cc320e..90866a6 touches only run.py, session_supervisor.py, sc usage text, README, tests — every other doc #22 seq 1 verdict stands unchanged.

Method: spec judged against the code on main at the pinned SHA only — no diffs, no
message trail. Diff range used solely to bound scope; verdict evidence read at the SHA.

## Amended verdict

| Spec section · requirement | Prior (seq 1 @2cc320e) | Now (@90866a6) | Evidence |
|---|---|---|---|
| User workflow: `./sc enter <planner>` attaches/resumes managed binding by default; `--new-session` refused until release | **unimplemented — Medium (F1)** | **as-specced — F1 closed** | see proof below |

## F1 proof — managed enter attach/resume

Requirement (spec doc #20, User workflow): "`./sc enter <planner>` against a
managed binding resumes or attaches to that binding by default instead of
opening another engine archive. An explicit `--new-session` is refused until
the managed binding is released."

1. **Attach by default.** `session_supervisor.binding_for_enter` (session_supervisor.py:207)
   selects the shell's `managed=1` binding on every interactive boot (run.py:930,
   bare shortname and picker paths alike; headless `sc run` keeps ephemeral
   behavior — matches the spec's workers-unchanged non-goal). The binding routes
   through `binding_for_resume` (shell/harness validated, released refused), the
   archive is **reused, not re-opened** (`open_session(..., reuse_archive_id=…)` —
   test proves same session_id/archive_id, archive count unchanged), the model is
   pinned from the archive (flag mismatch refused), and the boot summary prints
   the binding line.
2. **The native conversation is actually resumed**, not just the engine archive:
   Claude launcher issues `--resume <native_id>` when the binding names one,
   `--session-id <fresh uuid>` only when it doesn't (claude-session.py:60-63);
   Codex issues `thread/resume` on the per-binding app-server and attaches the
   TUI via `codex --remote` (codex-session.py:126-132); Kimi re-opens the
   existing session's authenticated web URL (kimi-session.py:95-98).
3. **`--new-session` exists and is refused while managed.** Parsed in run.py
   (usage line + main()); with a managed binding present, `binding_for_enter`
   raises "`--new-session requires releasing managed binding <id> first`" and
   run.py exits **before `open_session`** — hermetic tests assert archive count
   and `active_archive_id` untouched (test_session_supervisor.py:
   test_new_session_is_refused_while_managed_binding_exists,
   test_new_session_flag_is_refused_by_main_before_archive_open).
4. **Refusal is scoped "until release."** `release_session_control` sets
   `managed=0` + state `released` in one transaction (api/server.py:1444); after
   release, `binding_for_enter` returns None and `--new-session` proceeds with
   `force_new=True` opening archive N+1
   (test_released_new_session_reaches_open_with_force_new,
   test_new_session_opens_new_archive_after_binding_release).
5. **No second writer on attach.** Lease preflight runs pre-Popen
   (`on_pre_spawn`); a live owner raises LeaseConflict and the launch is refused
   before spawn (test_preflight_refuses_before_popen). `--new-session` +
   `--session-binding` combined is refused outright (run.py:926).
6. **Operator surface documents it**: `sc` usage now reads "managed bindings
   resume by default" / "new session requires release".

The seq 1 failure scenario — bare `./sc enter pln1` mid-sprint silently opening
conversation N+1 while the dispatcher resumes conversation N — is now
impossible: the bare enter resumes conversation N itself, and the only path to
a second conversation is an explicit release.

## SC-466 correction — verified

Requirement: error-state managed enter must fail **before archive
creation/spawn** with actionable retry/release guidance, without funneling
operators into queue-cancelling release.

- `binding_for_enter` raises on `state='error'` with: "managed binding <id> is
  in error; run `./sc session retry <shortname>` to recover it, or
  `./sc session release <shortname>` before starting a new session"
  (session_supervisor.py:225-230). **Retry is the primary remedy**; release is
  framed only as the deliberate new-session path — the operator is not steered
  into the queue-cancelling release to recover.
- The raise happens in run.py **before `open_session`** and before any spawn.
  Hermetic proof: test_error_binding_refuses_before_archive_open_with_remedies
  asserts the exact message, archive count still 1, `active_archive_id` still
  NULL, and the binding row (state/managed/lease/last_error) untouched.
- Guidance is honest: `retry_session_control` accepts exactly the `error` state
  (api/server.py) and `release_session_control` is the sanctioned new-session
  path — both verified as-specced in seq 1 and untouched in this delta.

## Out of scope, unchanged

F2 (provider-contract `interrupt` absent — Low) and F3 (no re-arm-after-batch
skill instruction — Low) were not in this re-run's scope and remain open as
filed in seq 1. J7 live gates remain deferred (spec #20 stays unfrozen until
they pass).

**Summary: F1 closed — as-specced at 90866a6. SC-466 correction verified. 0 new
findings. Remaining open from seq 1: 2 Lows (F2, F3) + deferred J7 live gates.**
