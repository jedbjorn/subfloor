-- 0045 — planner claude model → opus
--
-- Operator decision (2026-07-04): planning shells default to Opus. 0015 moved
-- planner's claude row opus→sonnet when it made claude the picker default;
-- this restores opus as planner's claude model — planning is a low-volume,
-- high-leverage reasoning role and the stronger model is worth it there. The
-- picker default stays claude (unchanged since 0015). Pure launch config
-- (flavor_defaults has no FKs, no per-instance memory), so a plain UPDATE
-- converges fresh rebuilds and existing forks alike; idempotent, re-runnable.

BEGIN;

UPDATE flavor_defaults SET model = 'opus'
    WHERE flavor = 'planner' AND harness = 'claude';

COMMIT;
