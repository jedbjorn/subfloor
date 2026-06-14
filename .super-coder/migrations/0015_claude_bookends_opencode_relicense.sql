-- 0014 — Claude-default bookends + corrected/retuned OpenCode model ids
--
-- Two corrections, both to flavor_defaults (pure launch config — no FKs, no
-- per-instance memory — so UPDATEs converge fresh-rebuild and existing forks
-- alike; idempotent, re-runnable).
--
-- 1. BOOKEND PICKER DEFAULT → CLAUDE. planner and reviewer are low-volume,
--    high-leverage reasoning roles where the Claude lineage is preferred for
--    planning and adversarial review. planner now defaults to claude/sonnet
--    (was codex/gpt-5.5) and its claude model moves opus→sonnet. reviewer
--    already defaulted to claude/opus (unchanged). The middle/ops roles
--    (dev, cartographer, admin) keep codex as their picker default.
--
-- 2. OPENCODE MODEL IDS WERE STALE + RELICENSED. They were seeded as
--    `ollama/<model>:cloud`, but the live OpenCode provider is
--    `ollama-cloud/<model>` with NO suffix — none of the old ids resolved.
--    Corrected to the real provider prefix, and retuned to current open
--    weights under a hard constraint: open models MUST be MIT or Apache.
--      planner       ollama-cloud/deepseek-v4-pro     (MIT)     top long-horizon planner
--      reviewer      ollama-cloud/glm-5.1             (MIT)     #1 SWE-Bench Pro bug-finder
--      dev           ollama-cloud/qwen3-coder-next    (Apache)  fast/cheap agentic coder
--      cartographer  ollama-cloud/gpt-oss:20b         (Apache)  cheapest bulk mapper
--      admin         ollama-cloud/deepseek-v4-pro     (MIT)     premium ops reasoning
--    Excluded by the license gate: Kimi (Modified-MIT branding clause) and
--    MiniMax (license unresolved at time of writing) — both available on
--    ollama, neither cleanly MIT/Apache.

BEGIN;

-- 1. Bookend picker default → Claude (planner: codex→claude, opus→sonnet).
UPDATE flavor_defaults SET is_default = 0
    WHERE flavor = 'planner' AND harness = 'codex';
UPDATE flavor_defaults SET is_default = 1, model = 'sonnet'
    WHERE flavor = 'planner' AND harness = 'claude';
-- reviewer already claude/opus is_default=1 — no change needed.

-- 2. OpenCode ids: correct provider prefix + retune to MIT/Apache models.
UPDATE flavor_defaults SET model = 'ollama-cloud/deepseek-v4-pro'
    WHERE flavor = 'planner'      AND harness = 'opencode';
UPDATE flavor_defaults SET model = 'ollama-cloud/glm-5.1'
    WHERE flavor = 'reviewer'     AND harness = 'opencode';
UPDATE flavor_defaults SET model = 'ollama-cloud/qwen3-coder-next'
    WHERE flavor = 'dev'          AND harness = 'opencode';
UPDATE flavor_defaults SET model = 'ollama-cloud/gpt-oss:20b'
    WHERE flavor = 'cartographer' AND harness = 'opencode';
UPDATE flavor_defaults SET model = 'ollama-cloud/deepseek-v4-pro'
    WHERE flavor = 'admin'        AND harness = 'opencode';

COMMIT;
