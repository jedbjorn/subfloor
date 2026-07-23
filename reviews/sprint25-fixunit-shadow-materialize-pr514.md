# Review — sprint 25 seq-11 fix unit · PR #514 (fix/shadow-materialize-manifest @7f5b4da)

Reviewer: REV1 (Kimi K3) · 2026-07-23 · flag #59 · feature #14
Verdict: **review-clean — 0 Major / 0 Medium / 0 Low**. CI 6/6 green pre-review.

## Scope

`engine_manifest.py` +1 (`".super-coder/shadow"` in ENGINE_PATHS);
`tests/test_update_materialize.py` +47 (per-file recurrence guard + opt-out set
+ shadow pin test + docstring rewrite).

## Scrutiny points (planner's list) — all verified, not trusted

1. **Correctness** — `.super-coder/shadow` is a directory pathspec; `git archive
   ref -- .super-coder/shadow` emits the whole tree, so all four tracked files
   (`dump.js`, `grid.js`, `package.json`, `sidecar.js` — confirmed via
   `git ls-tree` at the PR ref) ship to forks. `interface_runtime.py:51,438`
   resolves the sidecar as `ENGINE / "shadow" / "sidecar.js"` =
   `.super-coder/shadow/sidecar.js` — exactly the path now materialized.
2. **No over-inclusion** — diff is scoped to the single pathspec add. Verified
   none of `.sc-state/` (repo-root, never in ENGINE_PATHS), `shell_db.db*`,
   `instance.json`, `engine.ref`, `engine.manifest`, or `assets/shells/` are
   git-tracked under `.super-coder/`, so the new per-file guard has nothing
   fork-owned to trip on, and the materialize set stays engine-only.
   `assets/seed/` is tracked-but-stripped-on-install; the test's
   `NOT_MATERIALIZED` opt-out matches the ENGINE_PATHS comment.
3. **Hash-guard coverage** — `update.py:295-296` calls
   `write_manifest(_engine_paths_at(sha), files=_engine_files_at(sha))` after
   each materialize; `_engine_files_at` is `git ls-tree` over the same
   ENGINE_PATHS, so the manifest now records the shadow files and
   `local_edits()` blocks an update over a locally-modified one. Guard and
   materialize set agree by construction (both derive from ENGINE_PATHS).
4. **Recurrence guard is real** — ran `tests/test_update_materialize.py` in a
   detached worktree at the PR ref: 4/4 green. Replayed its coverage logic
   against the pre-fix manifest (ENGINE_PATHS minus the shadow line): it
   reports exactly the 4 shadow files as missing — the test goes RED pre-fix,
   GREEN post-fix. `_covered` (exact or dir-prefix with `/` boundary) has no
   false-prefix hole; the guard keys off `git ls-files` (the upstream-owned
   set), so it is neither vacuous nor over-broad.

## Findings

None. Diff is minimal, matches the bug (flag #59: `interface_unavailable:
shadow sidecar exited` on fresh forks), and the test guards the whole class
(new engine subdir absent from the manifest), not just this instance.

## Notes

- End-to-end fork proof (re-materialize ami, Interface boots) is the planner
  acceptance test post-merge — correctly out of scope here.
- Handoff per sprint reviewer slot (direct, ACTIVE sprint): DEV4 merges under
  scoped authority + files the unit report.
