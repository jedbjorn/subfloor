-- 0124 — explicit Planner → Conductor activation.

BEGIN;

INSERT OR IGNORE INTO directive_kinds (issuer_flavor, kind)
VALUES ('planner', 'handoff');

COMMIT;
