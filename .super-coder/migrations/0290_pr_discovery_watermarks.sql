-- 0290 — PR discovery high-water mark, one row per repository.
-- Intent: the PR watcher subscribes every PR whose head commit carries a
-- shell's attribution trailer, so a shell that opens a PR always gets its
-- green/red/merged/closed wakes. An open PR is always eligible. A PR that is
-- already merged or closed is eligible only when its number is above this
-- mark, so a PR opened and merged between two discovery passes (or while the
-- engine was down) still wakes its owner, while the first pass on an upgraded
-- install never replays historic merges. The first pass for a repository
-- writes the mark; every later pass advances it. Runtime-derived: a rebuild
-- that loses it restarts from "open PRs only", so it stays out of snapshots.
-- Additive; idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS pr_discovery_watermarks (
    repository  TEXT PRIMARY KEY CHECK (trim(repository)<>''),
    pr_number   INTEGER NOT NULL CHECK (pr_number>=0),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

COMMIT;
