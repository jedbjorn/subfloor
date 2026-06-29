-- 0027 — shells: add api_key + api_key_rotated_at.
--
-- Every shell gets a random 256-bit Bearer token at creation. The middleware
-- resolves shell_id from the token directly — no hash needed, since the
-- plaintext is already stored (loopback-bound, low value).
--
-- Two-column add:
--   api_key            — plaintext urlsafe token (secrets.token_urlsafe(32)); UNIQUE
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
ALTER TABLE shells ADD COLUMN api_key_rotated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shells_api_key ON shells(api_key);
