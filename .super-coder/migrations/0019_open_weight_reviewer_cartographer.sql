-- 0019 — Open-weight retune: reviewer GLM-5.1→5.2, cartographer gpt-oss:20b→qwen
--
-- Two UPDATEs to flavor_defaults (pure launch config — no FKs, no per-instance
-- memory — so they converge fresh-rebuild and existing forks alike; idempotent,
-- re-runnable). Both stay inside the MIT/Apache license gate (0015) — no gate
-- change, MiniMax/Kimi remain excluded.
--
--   reviewer      ollama-cloud/glm-5.2          (MIT)     GLM-5.2 supersedes 5.1;
--                                                         stronger SWE-Bench Pro bug-finding.
--   cartographer  ollama-cloud/qwen3-coder-next (Apache)  was gpt-oss:20b — too small now that
--                                                         the cartographer authors script/map
--                                                         updates, not just bulk file mapping.
--                                                         qwen3-coder-next (already the dev pick)
--                                                         is coding-tuned and well above a 20B.
--
-- Provider prefix stays `ollama-cloud/` (the live OpenCode provider, no suffix).

BEGIN;

UPDATE flavor_defaults SET model = 'ollama-cloud/glm-5.2'
    WHERE flavor = 'reviewer'     AND harness = 'opencode';

UPDATE flavor_defaults SET model = 'ollama-cloud/qwen3-coder-next'
    WHERE flavor = 'cartographer' AND harness = 'opencode';

COMMIT;
