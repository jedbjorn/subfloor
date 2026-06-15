-- 0016 — flat files are renders, not sources.
--
-- Shells were spending effort auditing local .md / git-tracked files for drift,
-- staleness, and stale dates — treating them as authoritative. They are not:
-- every flat file is rendered from the DB by `./sc render`. The system prompt
-- now says so. templates/shell_system_prompt.md (forked shells) and
-- scripts/seed_dogfood.py (the maintainer prompt) carry the new paragraph for
-- shells created from here on; this migration rewrites the prompt of shells
-- already created under the old wording.
--
-- Injected before the `## MANDATE` section, which terminates the MEMORY
-- ARCHITECTURE block in every shell shape (maintainer + all flavors).
--
-- Idempotent: the NOT LIKE guard makes a re-run (or a fork already carrying the
-- paragraph) a no-op.
UPDATE shells SET system_prompt = REPLACE(
    system_prompt,
    '

## MANDATE',
    '

**Flat files are renders, not sources.** Every local `.md` and git-tracked file
— docs, specs, skills, this `CLAUDE.md`/`AGENTS.md` — is rendered from the DB by
`./sc render`. They are derived artifacts: a photograph of a DB row, not the row.
Do not audit them for drift, staleness, or a stale date, and never edit or delete
a file to change its content. If one looks wrong or out of date, fix the DB (`./sc
mem` or the owning table) and re-render — the divergence is a render that hasn''t
run, not a file to hand-correct. The DB is the authoritative content; the tree is
its projection.

## MANDATE'
) WHERE system_prompt NOT LIKE '%Flat files are renders%';
