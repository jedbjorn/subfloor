-- 0013 — planner/reviewer shells may write and commit; worktrees for all shells.
--
-- The planner/reviewer templates used to bake "Git is read-only: diff, log,
-- and read files only. Never checkout, restore, stash, or commit." into the
-- shell's system_prompt at creation. That instruction is gone: every shell now
-- boots into its own git worktree on shell/<shortname> (run.py no longer gates
-- worktrees to the dev flavor), and planner/reviewer shells NEED git writes —
-- they commit their own artifacts (specs, snapshots, .sc-state) on their
-- branch. The templates (templates/shells/planner.json, reviewer.json) carry
-- the new wording for shells created from here on; this migration rewrites the
-- prompt of shells already created under the old wording.
--
-- Idempotent: REPLACE is a no-op once the old sentence is gone (or on a fork
-- with no planner/reviewer shells).
UPDATE shells SET system_prompt = REPLACE(
    system_prompt,
    'keep those lanes clean. Git is read-only: diff, log, and read files only. Never checkout, restore, stash, or commit.',
    'keep those lanes clean. You work in your own worktree on your shell branch: write and commit your artifacts (specs, snapshots, state) there; leave feature code to dev.'
) WHERE flavor = 'planner';

UPDATE shells SET system_prompt = REPLACE(
    system_prompt,
    'you don''t build. Git is read-only: diff, log, and read files only. Never checkout, restore, stash, or commit.',
    'you don''t build features. You work in your own worktree on your shell branch: write and commit your artifacts (review notes, snapshots, state) there.'
) WHERE flavor = 'reviewer';
