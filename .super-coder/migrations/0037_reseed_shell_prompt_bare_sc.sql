-- 0037 — finish the cwd-independent sweep (0036) in the shell system prompt.
--
-- 0036 rewrote the db_map/memory/messaging/spec SKILLS from the cwd-relative
-- `./sc …` form to bare `sc …` (on PATH from any cwd, so a shell never `cd`s to
-- the main root and silently retargets later bare git/grep). But it missed the
-- always-loaded shell system-prompt template — which still spelled its memory
-- writes as `./sc mem`. The template (templates/shell_system_prompt.md) is now
-- fixed for shells created from here on; this splices the same change into
-- shells already created under the old template.
--
-- Surgical + idempotent: REPLACE `./sc mem` → `sc mem` (also covers
-- `./sc mem which`, a superstring). Once replaced the pattern no longer matches,
-- so a re-run is a no-op. All flavors. The maintainer prompt (seed_dogfood.py)
-- runs from the main root where `./sc` also resolves, so touching it here is
-- harmless — bare `sc` is on PATH for every shell post-#225.

BEGIN;

UPDATE shells
   SET system_prompt = REPLACE(system_prompt, './sc mem', 'sc mem')
 WHERE system_prompt LIKE '%./sc mem%';

COMMIT;
