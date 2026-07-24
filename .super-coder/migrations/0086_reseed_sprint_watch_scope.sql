-- 0086 — require sprint scope at watched-PR registration and repair the
-- participant procedure that omitted it. Existing unscoped legacy watches
-- remain readable/rebindable, but new registration cannot silently create
-- dormant state.

BEGIN;

UPDATE skills
SET content = replace(
      content,
      './sc watch pr <owner/repo> <pr-number> --shell <planner-shortname>',
      './sc watch pr <owner/repo> <pr-number> --shell <planner-shortname> --sprint <doc-id>'
    )
WHERE name = 'sprint'
  AND instr(
        content,
        './sc watch pr <owner/repo> <pr-number> --shell <planner-shortname> --sprint <doc-id>'
      ) = 0;

UPDATE skills
SET content = replace(
      content,
      'without a watch is invisible to the sprint.',
      'without a watch is invisible to the sprint. Sprint scope is mandatory:
registration without `--sprint`, or against a non-ACTIVE board, fails loudly
instead of creating a dormant watch.'
    )
WHERE name = 'sprint'
  AND instr(
        content,
        'without a watch is invisible to the sprint. Sprint scope is mandatory:'
      ) = 0;

COMMIT;
