# REVIEW — sprint 25 seq-11 fix unit · PR #526 (flag #61, Interface worktree-exec)

- PR: #526 `fix(interface): provision shell worktree at reserve, never assume it` (base main, +213/-25)
- Reviewer: REV1 (Kimi) · task #573 from PLN1 · 2026-07-23
- CI: 6/6 green (analyze actions+python, CodeQL, render-check, tests, verify) at review time; mergeable.
- Files: `api/interface_routes.py`, `scripts/interface_exec.py`, `scripts/run.py`, tests ×3.

## Verdict: REVIEW-CLEAN — 0 Major, 0 Medium, 3 Low (notes only)

## Scrutiny points (per the task)

1. **Shared resolver** — PASS. `run.shell_work_dir(shortname, flavor)` (run.py:253) is
   the single rule; all three boot paths route through it: interactive `main()`
   (run.py:1282), headless `prepare_launch` (run.py:956), and the Interface via
   `_worktree_for` (interface_routes.py:293, lazy `import run` — safe: scripts dir is
   on sys.path at interface_routes.py:47). Semantics are byte-identical to the inline
   logic it replaced in both CLI sites (`shortname and flavor != "admin"` → worktree,
   else repo root); no divergent copy remains anywhere.

2. **Provision ordering** — PASS. In `_create_session.produce()` the resolve+provision
   sits after the occupied/unmanaged 409 checks and BEFORE the generations insert,
   sessions insert, token write, and spawn. Failure returns the actionable 500
   `worktree_provision_failed` (`FATAL:` stripped, `git worktree list` / `./sc enter`
   guidance). Test asserts the no-leak triple: 0 rows in `interface_sessions`, no
   `launch-*.json`, no spawn call. Admin flavor short-circuits (`path == REPO_ROOT` →
   no-op, no provision).

3. **Concurrency / idempotency** — PASS (no lock; bounded race, clean failure). The
   occupied check at the top of `produce()` makes the second New chat 409 once the
   first reservation commits. A true simultaneous race has both callers reach
   `ensure_worktree`; git refuses the duplicate `worktree add`, the loser gets the
   clean actionable 500 — before any row/token/pane, so no partial state either way.
   Re-provision of an existing worktree is a proven no-op (`is_dir` short-circuit,
   `ensure_wt.assert_not_called()`); reopening a live chat never touches the
   provision path. `ensure_worktree` idempotency proven with real git
   (EnsureWorktreeTest: second call no-op, branch + worktree list correct).

4. **interface_exec missing-worktree** — PASS. The hard exit-2 is gone; a missing
   token worktree falls through to `prepare_launch` (the same shared resolver, which
   itself provisions for non-admin), and the process chdirs to the authoritative
   `plan.cwd` before exec. Token stays single-use (unlinked on parse); bad-token
   refusal before any archive row is intact (test_missing_fields_refuse_before_archive
   unchanged and green).

5. **Tests red/green** — PROVEN. On the PR checkout: test_interface_api 38 OK,
   test_interface_exec 11 OK, test_worktree_sync 9 OK. Against unfixed `origin/main`
   source with the PR's tests overlaid (temp worktree): 3 named failures in
   test_interface_api (`provisions_missing_worktree`, `admin_resolves_repo_root`,
   `provision_failure_is_actionable`), 1 failure in test_interface_exec
   (`missing_worktree_self_heals_via_prepare_launch`), ImportError in
   test_worktree_sync (`shell_work_dir` absent). The suite genuinely guards the fix.

## Lows (notes for the sprint report — non-blocking)

- L1: the double-provision race's loser gets a 500 rather than a 409 — clean and
  actionable, no leak; acceptable, could be polished to retry-then-409 later.
- L2: `_provision_worktree` catches only `SystemExit`; an `OSError` (e.g. git binary
  missing) would surface as an uncurated 500. Still pre-insert, so no leak; the host
  always has git.
- L3: `path.is_dir()` vs `ensure_worktree`'s `exists()` — a non-dir file at the
  worktree path would no-op provisioning and fail later at the pane `cd`. Degenerate,
  pre-existing shape.

## Recommendation

Merge. DEV4 has scoped merge authority (sprint ACTIVE, checks green, review-clean
declared). The end-to-end fork proof (Interface on ami for pln1 provisions + execs)
remains PLN1's post-merge acceptance test.
