-- 0096 — Account Analytics: harness quota registry + window snapshots (spec doc 49, feature 17, sprint doc 52).
--
-- Feature #17's collector answers *what did we spend* by parsing what each
-- harness writes to disk after the fact. It cannot answer *how much is left*:
-- no harness writes its remaining allotment anywhere, so allotment lives behind
-- an authenticated endpoint per provider. These two tables are where the probes
-- in scripts/quota_probes/ land what those endpoints return.
--
-- Two parts:
--   1. harness_quota_account — the durable registry. One row per account ever
--      seen. A credential file holds exactly ONE account, so "show all known
--      accounts" is only possible if we remember the ones we have seen; this is
--      that memory. Not a timeseries — one current row per account.
--   2. harness_quota_window — last snapshot per window, overwritten every probe.
--      No history is kept, so the panel stays a glance surface.
--
-- ── NEITHER TABLE GOES IN PER_INSTANCE_TABLES (scripts/snapshot.py) ──────────
--
-- They are probe-rebuildable caches, exactly like session_token_usage (0071):
-- nothing here is authored memory, and a rebuild re-derives all of it from the
-- next probe. That alone would be reason enough to leave them out.
--
-- But the consequence of getting it wrong is no longer merely "a cache got
-- committed". account_label holds the operator's FULL email address. An earlier
-- ruling redacted it to the local part; decision #67 WITHDREW that redaction,
-- because redaction was defending a path that does not exist (the engine DB is
-- gitignored, and session_token_usage — a cache of exactly this class — appears
-- zero times in the tracked content.sql on origin/main) while costing real
-- information: jed@gmail.com and jed@work.com both reduce to "jed", making two
-- cards indistinguishable in precisely the multi-account case this feature
-- exists to show.
--
-- That withdrawal promoted snapshot exclusion from hygiene to load-bearing: it
-- is now the ONLY thing between full operator emails and the repository. Adding
-- either table to PER_INSTANCE_TABLES would put operator PII into a git-tracked
-- file — the same defect shape already recorded as a conformance Medium for the
-- plaintext lease token.
--
-- This comment exists because a future reader has no other way to know the
-- exclusion is deliberate; an allowlist records what IS in it, never what was
-- kept out or why. tests/test_quota_snapshot_exclusion.py asserts the property
-- against a freshly rendered content.sql rather than against the allowlist, so
-- the guarantee survives a refactor of how the snapshot decides what to dump.
--
-- Second-order, and no allowlist can catch it: docs/images/analytics.png is a
-- committed screenshot of this very tab. Whoever captures a shot of this
-- section scrubs the label or shoots a placeholder account.

BEGIN;

CREATE TABLE IF NOT EXISTS harness_quota_account (
    account_pk    INTEGER PRIMARY KEY,
    provider      TEXT    NOT NULL,       -- anthropic/openai/moonshot (one module per PROVIDER,
                                          -- not per harness: quota is an account property, and
                                          -- five shells on Claude share one bucket)
    account_ref   TEXT    NOT NULL,       -- provider's stable id: OpenAI account_id, Moonshot
                                          -- user.userId, or the Anthropic credential-file uuid
    account_label TEXT,                   -- FULL email (OpenAI) else user.userId (Moonshot) else
                                          -- credential uuid first 8 (Anthropic). Never truncated —
                                          -- see decision #67 and the exclusion note above.
    plan          TEXT,                   -- max / prolite / LEVEL_ADVANCED — verbatim from provider
    first_seen    TEXT    NOT NULL,       -- ISO UTC; survives an account going quiet and coming back
    last_seen     TEXT    NOT NULL,       -- ISO UTC; drives the fixed 7-day activity window
    is_current    INTEGER NOT NULL DEFAULT 0,  -- 1 = the account the credential file resolves to now
    UNIQUE (provider, account_ref)
);

-- The registry's identity key. The spec states "one row per account ever seen"
-- but declares no key for it; without this a re-probe mints a duplicate instead
-- of advancing last_seen, and "switching back restores the original first_seen"
-- becomes unimplementable. account_ref is NOT NULL so a drifted payload that
-- carries no identifier fails loudly here rather than accumulating anonymous
-- rows — a provider we cannot identify has no card to render anyway.

-- Drives the panel's only filter: last_seen inside 7 days. Rows are never
-- pruned, they simply stop rendering, so this index reads a growing table.
CREATE INDEX IF NOT EXISTS idx_hqa_last_seen ON harness_quota_account(last_seen);

CREATE TABLE IF NOT EXISTS harness_quota_window (
    window_pk     INTEGER PRIMARY KEY,
    account_pk    INTEGER NOT NULL REFERENCES harness_quota_account(account_pk) ON DELETE CASCADE,
    window_kind   TEXT    NOT NULL,       -- session/five_hour/weekly/weekly_scoped/short — and see below
    scope         TEXT,                   -- model or feature the window applies to; NULL = account-wide
    used_percent  REAL,                   -- 0-100, NULL when underivable (never back-computed into a count)
    used          INTEGER,                -- absolute counts, NULL when the provider gives none
    limit_value   INTEGER,                -- "limit" is a SQL reserved word — see note below
    resets_at     TEXT,                   -- ISO UTC
    captured_at   TEXT    NOT NULL,       -- ISO UTC — the card always renders its age
    status        TEXT    NOT NULL DEFAULT 'ok'
                  CHECK (status IN ('ok', 'stale', 'unauth', 'na', 'error')),
    probe_version TEXT                    -- drift pin, per probe module
);

-- window_kind is deliberately NOT constrained by a CHECK. The spec requires
-- that a window mapping to no known duration is "rendered under its raw
-- duration, not dropped" — a CHECK over the five known kinds would reject that
-- insert, which drops exactly the row the spec says to keep. These endpoints
-- are private, undocumented and expected to drift; the constraint would convert
-- drift into data loss. status IS constrained, because its five values are ours
-- and closed.
--
-- limit_value, not "limit": the spec's Data Model names this column `limit`,
-- which is a SQL reserved word — `SELECT used, limit FROM harness_quota_window`
-- is a syntax error, so every reader downstream would have to quote it forever.
-- Renamed here and declared to the planner rather than quoted at every call
-- site. Meaning is unchanged: the absolute allotment, NULL when the provider
-- reports percentage only. limit_value = 0 yields a NULL percent and an "n/a"
-- card — never a division by zero.

-- ── The upsert key, and why it is an expression index ────────────────────────
--
-- The spec declares UNIQUE (account_pk, window_kind, scope) as the idempotency
-- key AND declares scope NULL = account-wide. Under SQLite those two clauses
-- contradict each other: NULLs compare DISTINCT in a UNIQUE index, so a plain
-- UNIQUE(account_pk, window_kind, scope) does not constrain account-wide rows
-- at all — two probes of the same window insert two rows, no violation raised.
-- That is the common path, not an edge case: session, weekly and five_hour are
-- all account-wide, so the panel's principal rows would duplicate on every
-- probe and the spec's own "concurrent probes: upsert is idempotent on the
-- UNIQUE key" would be false.
--
-- Folding NULL to '' in the index restores the declared behaviour while leaving
-- scope NULLable, so the shape U3 and U4 read is exactly the one the spec gave
-- them. The cost is that the upsert must name this expression as its conflict
-- target verbatim:
--
--   INSERT INTO harness_quota_window (...) VALUES (...)
--   ON CONFLICT(account_pk, window_kind, COALESCE(scope, ''))
--   DO UPDATE SET used_percent=excluded.used_percent, ...;
--
CREATE UNIQUE INDEX IF NOT EXISTS idx_hqw_key
    ON harness_quota_window(account_pk, window_kind, COALESCE(scope, ''));

COMMIT;
