-- 0022 — stacked-PR retarget rule into the always-loaded system prompt.
--
-- The git skill carries the full stacked-merge procedure, but a shell told
-- "merge it" may act without reopening the skill — and that mistake is costly
-- (a deleted base ref orphans the stacked PR; recovery is ~5k tokens of churn).
-- So the short rule now lives on the always-loaded path: the shell system-prompt
-- template (templates/shell_system_prompt.md) gained one line, for shells created
-- from here on. This migration splices the SAME line into shells already created
-- under the old template.
--
-- Anchored on repo-INDEPENDENT text ("one cwd — no cross-repo confusion." →
-- "## MEMORY ARCHITECTURE"), so it is fork-safe — the {{repo}}-templated lines
-- are untouched. All flavors (cartographer included). Idempotent: once the line
-- is spliced the old anchor no longer matches, so a re-run is a no-op; a shell
-- whose prompt predates the anchor simply isn't matched (no harm).

BEGIN;

UPDATE shells SET system_prompt = REPLACE(
  system_prompt,
  'one cwd — no cross-repo confusion.

## MEMORY ARCHITECTURE',
  'one cwd — no cross-repo confusion.

**Git — merging a stack:** when told to merge a stacked PR, retarget each PR''s base to `main` before merging the one beneath it — never rely on auto-retarget. Full procedure (bottom-up + recovery): the `git` skill.

## MEMORY ARCHITECTURE'
);

COMMIT;
