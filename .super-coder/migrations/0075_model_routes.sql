-- 0075 — runtime model-route catalogue.
--
-- Model availability is host/account state, not fork memory: Refresh models
-- rebuilds these rows from locally authoritative CLI/config caches plus
-- advisory public catalogues. The table is deliberately absent from
-- snapshot.py's PER_INSTANCE_TABLES, so a rebuild starts empty and self-heals
-- on the next refresh instead of carrying one machine's entitlements forward.

BEGIN;

CREATE TABLE IF NOT EXISTS model_routes (
    harness               TEXT NOT NULL,
    selector              TEXT NOT NULL,
    provider              TEXT,
    provider_model        TEXT,
    display_name          TEXT,
    family                TEXT,
    source                TEXT NOT NULL,
    availability          TEXT NOT NULL CHECK (
        availability IN ('available', 'advisory', 'fallback')
    ),
    headless_supported     INTEGER NOT NULL DEFAULT 0,
    high_effort_supported  INTEGER NOT NULL DEFAULT 0,
    default_effort         TEXT,
    supported_efforts      TEXT,
    cli_version            TEXT,
    last_seen_at           TEXT NOT NULL,
    stale                  INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT,
    PRIMARY KEY (harness, selector)
);

CREATE INDEX IF NOT EXISTS idx_model_routes_runnable
    ON model_routes(harness, availability, headless_supported,
                    high_effort_supported, stale);

COMMIT;
