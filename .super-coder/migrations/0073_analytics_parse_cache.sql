-- 0073 — analytics parse cache: per-harness incremental parse state (CC-145).
--
-- The pre-session sweep (doc #11) is mtime-gated per FILE, but the claude
-- parser's dedupe is dir-scoped: any changed transcript re-parses its whole
-- project dir. A live session keeps its dir permanently hot, so every boot
-- re-paid a full multi-hundred-MB parse — the 10-15s stall between harness
-- pick and boot summary.
--
-- One row per harness: a JSON payload of parser-owned derived state (per-file
-- byte offsets + accumulated parse aggregates) that turns the re-parse into a
-- tail-delta read. The payload is a disposable cache, never a source of
-- truth: it is version-pinned to the parser (a PARSER_VERSION bump discards
-- it), rebuilt from the transcripts on any mismatch, and lives in the DB so a
-- rebuilt DB drops rows and cache together — they can never disagree.

BEGIN;

CREATE TABLE IF NOT EXISTS analytics_parse_cache (
    harness        TEXT PRIMARY KEY,       -- claude/opencode/codex/vibe/kimi
    parser_version TEXT NOT NULL,          -- pin: mismatch = cache miss
    payload        TEXT NOT NULL,          -- JSON, parser-owned shape
    updated_at     TEXT
);

COMMIT;
