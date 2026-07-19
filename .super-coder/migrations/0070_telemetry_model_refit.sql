-- 0070 — flavor default refit from sprint success telemetry
--
-- Operator decision (2026-07-19): re-point the per-flavor defaults at what the
-- fleet's real sprint outcomes favor, not a-priori doctrine:
--
--   planner      claude  opus -> fable          (stays default harness)
--   reviewer     claude  opus -> fable          (stays default harness)
--   dev          codex   gpt-5.5 -> gpt-5.6-sol (stays default harness)
--   cartographer codex   gpt-5.4 -> gpt-5.6-terra, becomes default (was claude/sonnet)
--   admin        claude  sonnet -> opus, becomes default (was codex/gpt-5.5)
--
-- All three ids verified live against the signed-in plans before this refit
-- (ChatGPT-account codex accepts gpt-5.6-sol/-terra; `fable` is a Claude Code
-- CLI alias, self-tracking like opus/sonnet/haiku). devops untouched.
--
-- flavor_defaults is pure launch config (no FKs, no per-instance memory), so
-- plain UPDATEs converge an installed fork's live DB in place. NOTE — unlike
-- what the 0024/0045 headers claimed, flavor_defaults IS snapshotted into
-- content.sql these days (snapshot.py: fork GUI edits must win over the
-- engine baseline, content.sql loads AFTER migrations on rebuild). Two
-- consequences: (1) this commit ships the source repo's regenerated
-- content.sql alongside the migration, or a rebuild resurrects the old
-- matrix; (2) on forks, the in-place UPDATE lands on the live DB and their
-- next `./sc snapshot` persists it — an operator's own GUI-tuned cells for
-- these five (flavor, harness) rows are intentionally superseded.
-- Idempotent / re-runnable: each UPDATE targets one row by primary key.

BEGIN;

UPDATE flavor_defaults SET model = 'fable'
    WHERE flavor = 'planner' AND harness = 'claude';

UPDATE flavor_defaults SET model = 'fable'
    WHERE flavor = 'reviewer' AND harness = 'claude';

UPDATE flavor_defaults SET model = 'gpt-5.6-sol'
    WHERE flavor = 'dev' AND harness = 'codex';

-- Make codex the default harness for cartographer (was claude)
UPDATE flavor_defaults SET model = 'gpt-5.6-terra', is_default = 1
    WHERE flavor = 'cartographer' AND harness = 'codex';
UPDATE flavor_defaults SET is_default = 0
    WHERE flavor = 'cartographer' AND harness = 'claude';

-- Make claude the default harness for admin (was codex)
UPDATE flavor_defaults SET model = 'opus', is_default = 1
    WHERE flavor = 'admin' AND harness = 'claude';
UPDATE flavor_defaults SET is_default = 0
    WHERE flavor = 'admin' AND harness = 'codex';

COMMIT;
