-- 0129 — shell-facing API identity wording (feature #23).
--
-- Ordinary shell-facing skill text stops teaching bearer-token mechanics:
-- identity is described as already resolved by the engine for the launched
-- shell. Carry the db_map and memory corrections forward without replacing
-- the skill rows or their grants. Token terminology remains in
-- implementation comments, auth code, maintainer docs, and test-authoring
-- skills, where it is the subject.

BEGIN;

UPDATE skills
SET content = REPLACE(
  content,
  'its back. Your identity rides in your bearer token — the server resolves
token -> shell; never name a shell in a write. Decisions read FLEET-WIDE
(every row, tagged `@shortname`) so cross-shell citations resolve; every
other identity surface reads as you.',
  'its back. `sc mem` is already wired to this launched shell — the engine
resolves API identity for you; never name a shell in a write. Decisions read
FLEET-WIDE (every row, tagged `@shortname`) so cross-shell citations
resolve; every other identity surface reads as you.'
)
WHERE name = 'db_map';

UPDATE skills
SET content = REPLACE(
  content,
  'the server resolves it from your token.',
  'the engine resolves API identity for you.'
)
WHERE name = 'db_map';

UPDATE skills
SET content = REPLACE(
  content,
  'to all shells on commit. It always targets your own shell (identity resolved
from your token) — never name a shell.',
  'to all shells on commit. It always targets your own shell (the engine resolves
API identity for you) — never name a shell.'
)
WHERE name = 'memory';

COMMIT;
