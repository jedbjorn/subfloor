-- 0026 — shells: add api_key, api_key_hash, api_key_rotated_at.
--
-- Every shell gets a random 256-bit Bearer token at creation. The middleware
-- (CC-127) resolves shell_id from the hashed token so API endpoints are
-- token-scoped with no shell_id in the path. The plaintext is stored alongside
-- the hash (alpha simplification — the API is loopback-bound, low value).
--
-- Three-column add:
--   api_key           — plaintext urlsafe token (secrets.token_urlsafe(32))
--   api_key_hash      — SHA-256 hex of api_key; indexed + UNIQUE for fast lookup
--   api_key_rotated_at — ISO timestamp of last mint/rotate; NULL until keyed
--
-- Backfill for existing shells cannot be done in plain SQL (requires Python's
-- secrets module for cryptographic token generation). After applying this
-- migration, run:
--
--   python3 .super-coder/scripts/backfill_shell_api_keys.py <path-to-db>
--
-- New shells receive keys at creation time via shell_factory.py — no manual
-- backfill needed for shells created after this migration runs.
--
-- Plain SQL: migrate.py owns the transaction and the schema_migrations row.

ALTER TABLE shells ADD COLUMN api_key            TEXT;
ALTER TABLE shells ADD COLUMN api_key_hash       TEXT;
ALTER TABLE shells ADD COLUMN api_key_rotated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shells_api_key_hash ON shells(api_key_hash);
