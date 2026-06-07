-- 0008 — align existing shell flavors to the canonical planner/reviewer vocab.
--
-- Early shell templates emitted flavor 'planning'/'review', but flavor_defaults
-- (0006/0007) and the schema comment use 'planner'/'reviewer' — matching the
-- role-noun pattern of 'dev'/'cartographer'. The mismatch meant a planning/
-- review shell never matched a flavor_default row: blank picker default and no
-- model pin (it fell through to the harness's built-in model). The templates
-- are now renamed to planner.json/reviewer.json (the filename stem IS the
-- flavor); this migration fixes shells already created under the old vocab.
--
-- Idempotent: a no-op once flavors are canonical (or on a fork with none).
UPDATE shells SET flavor = 'planner'  WHERE flavor = 'planning';
UPDATE shells SET flavor = 'reviewer' WHERE flavor = 'review';
