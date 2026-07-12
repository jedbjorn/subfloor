-- 0054 — authoring_syntax: the house authoring standard joins the catalogue
--
-- New craft skill `authoring_syntax`: house syntax for AI-consumed text
-- (skill bodies, shell focus/mandate text, agent prompts) — directives with
-- success conditions, specific prohibitions, light operators, need-to-know,
-- plus the Self-test for "syntax check X" requests. Ported from the superCC
-- original that governed the 0053 catalogue rewrite.
--
-- Self-contained on purpose (0049 pattern): at update time `migrate` runs
-- BEFORE the catalogue sync, so the grant below cannot rely on the sync
-- having inserted the skill row — this migration carries the body itself
-- (UPSERT by name; skill_id + existing grants preserved). 0001 is
-- regenerated from assets for fresh builds.
--
-- Grants: existing admin shells get it here; NEW admin shells get it from
-- templates/shells/admin.json.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'authoring_syntax',
  'House syntax for text authored for AI consumption — skill bodies, shell focus/mandate text, agent prompts. Directives with success conditions, specific prohibitions, light operators, need-to-know. Fires when authoring or editing one, or on "syntax check X".',
  'craft',
  NULL,
  0,
  '# authoring_syntax — write for the shell that loads it

House syntax for text authored for AI consumption — skill bodies, shell
focus/mandate text, agent prompts. Governs how the text reads; lifecycle
mechanics (create / seed / grant / persist) stay in `local_skill_management`.

Retrofit = touch-time: an existing doc converts when next edited. No mass
pass. Engine skill bodies are upstream-owned — a syntax fix to one is an
upstream PR (`issue_reporting` boundary), never a fork edit.

## Fires when

- authoring / editing any AI-consumed doc
- "syntax check X" -> run Self-test against X, return findings ranked, no
  rewrite

## Rules

- directive + success condition. every instruction = action + observable
  pass. "bounce the api -> `make health` returns ok" not "make sure the api
  is running".
- prohibitions are specific. bar the exact action: "NEVER `echo $SECRET`"
  not "be careful with secrets". no compliance test = cut or sharpen.
- operators, light: `->` then / leads-to. `=` is / defined-as. `/` or. `+`
  and. use only where compression loses nothing; else prose.
- need-to-know. cut the why unless it changes the action taken. NEVER name a
  tool / path / failure mode the reader would not otherwise encounter.
- CAPS = emphasis. rare — caps on every rule = caps on none.
- imperative voice. "validate input" not "you should validate input".
- format: prose = reasoning. tables / short bullets = structured data.
  `path:line` = code.
- one source of truth. state a constraint once, where it''s used; restate
  only adjacent to a distant use.

## Self-test

- per line: could the reader comply and still fail? -> add the success
  condition.
- per line: delete it — does behavior change? no -> stays deleted.
- per doc: anything explaining rather than directing? -> cut or convert.

This skill obeys itself. Every edit to it passes the Self-test before write.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

-- grant to existing admin shells (no-op where already granted)
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id
FROM shells s, skills k
WHERE COALESCE(s.is_deleted, 0) = 0
  AND s.flavor = 'admin'
  AND k.name = 'authoring_syntax' AND k.is_deleted = 0;

COMMIT;
