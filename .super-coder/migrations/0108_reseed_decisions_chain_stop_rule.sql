-- 0108 — decisions: chain stop-rule on the always-loaded boot path
--
-- Follow-up to 0044/#274 (index/library split). The index already excludes
-- superseded rows, but shells — Opus seats especially — still walk decision
-- chains: a flag/spec/feature cites a decision, the citation resolves to a
-- superseded row, and the shell follows parent links outward "for context."
-- The citation-follow itself is fine (that IS the explicit direction); the
-- failure mode is the walk that continues past the first hop.
--
-- Fix (norm only, no read-path change): one stop-rule paragraph in the
-- always-loaded shell system prompt — active decisions load by subject;
-- superseded decisions are history, loaded only on explicit direction or
-- when auditing why a decision changed; parent chains are provenance, not
-- trails. Spliced into existing shells' prompts anchored on the 0044
-- "Read before you decide" paragraph, 0037/0044-style; the template covers
-- shells created from here on. Guarded idempotent.

BEGIN;

UPDATE shells
   SET system_prompt = REPLACE(system_prompt,
       'never silently re-litigate.',
       'never silently re-litigate.

**Chains are provenance, not trails.** Active decisions are working context —
load them by subject when they bear on the work. A citation in a flag, spec, or
feature may resolve to a superseded decision; that''s fine — read the superseding
row it points to and move on. Never walk parent chains as context-gathering;
load decision history only when explicitly directed, or when auditing why a
decision changed.')
 WHERE system_prompt LIKE '%never silently re-litigate.%'
   AND system_prompt NOT LIKE '%provenance, not trails%';

COMMIT;
