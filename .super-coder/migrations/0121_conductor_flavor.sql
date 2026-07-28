-- 0121 — Conductor Step 8 flavor route.
--
-- The flavor itself is a tracked shell template.  Installed databases also
-- need one deterministic launch default so an ephemeral Conductor cannot fall
-- through to the fork's ambient harness/model.  No flavor_skills row is added:
-- zero skills is part of the Conductor authority boundary.

BEGIN;

INSERT OR IGNORE INTO flavor_defaults
    (flavor, harness, model, is_default)
VALUES
    ('conductor', 'opencode', 'ollama-cloud/gpt-oss:20b', 1);

COMMIT;
