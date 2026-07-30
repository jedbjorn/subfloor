-- 0142 — durable Git/PR review identities for browser conversations.
--
-- Local observations are deliberately small: the read-only review service
-- added by later Feature #26 steps enriches these rows with PR evidence and
-- bounded patch artifacts.  PR number becomes immutable once associated so a
-- reused branch name can never repurpose an older review target.

BEGIN;

CREATE TABLE conversation_git_targets (
    target_id              TEXT PRIMARY KEY
                           DEFAULT ('gt_' || lower(hex(randomblob(16))))
                           CHECK (
                             length(target_id)=35
                             AND substr(target_id,1,3)='gt_'
                             AND substr(target_id,4)
                                 NOT GLOB '*[^0-9a-f]*'
                           ),
    conversation_id        TEXT NOT NULL
                           REFERENCES conversations(conversation_id),
    branch_name            TEXT NOT NULL
                           CHECK (
                             length(trim(branch_name)) BETWEEN 1 AND 1024
                           ),
    base_ref               TEXT
                           CHECK (
                             base_ref IS NULL
                             OR length(trim(base_ref)) BETWEEN 1 AND 1024
                           ),
    first_head_sha         TEXT NOT NULL
                           CHECK (
                             length(first_head_sha) IN (40,64)
                             AND first_head_sha NOT GLOB '*[^0-9a-f]*'
                           ),
    latest_head_sha        TEXT NOT NULL
                           CHECK (
                             length(latest_head_sha) IN (40,64)
                             AND latest_head_sha NOT GLOB '*[^0-9a-f]*'
                           ),
    pr_number              INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
    pr_head_sha            TEXT
                           CHECK (
                             pr_head_sha IS NULL
                             OR (
                               length(pr_head_sha) IN (40,64)
                               AND pr_head_sha NOT GLOB '*[^0-9a-f]*'
                             )
                           ),
    pr_state               TEXT
                           CHECK (
                             pr_state IS NULL
                             OR pr_state IN ('OPEN','MERGED','CLOSED')
                           ),
    merge_sha              TEXT
                           CHECK (
                             merge_sha IS NULL
                             OR (
                               length(merge_sha) IN (40,64)
                               AND merge_sha NOT GLOB '*[^0-9a-f]*'
                             )
                           ),
    merged_at              TEXT,
    pr_url                 TEXT
                           CHECK (
                             pr_url IS NULL OR length(pr_url) <= 2048
                           ),
    pr_title               TEXT
                           CHECK (
                             pr_title IS NULL OR length(pr_title) <= 500
                           ),
    first_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    remote_refreshed_at    TEXT,
    patch_artifact         TEXT
                           CHECK (
                             patch_artifact IS NULL
                             OR (
                               length(patch_artifact) BETWEEN 1 AND 2048
                               AND substr(patch_artifact,1,1) <> '/'
                             )
                           ),
    patch_sha256           TEXT
                           CHECK (
                             patch_sha256 IS NULL
                             OR (
                               length(patch_sha256)=64
                               AND patch_sha256 NOT GLOB '*[^0-9a-f]*'
                             )
                           ),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (
      pr_number IS NOT NULL
      OR (
        pr_head_sha IS NULL
        AND pr_state IS NULL
        AND merge_sha IS NULL
        AND merged_at IS NULL
        AND pr_url IS NULL
        AND pr_title IS NULL
        AND remote_refreshed_at IS NULL
        AND patch_artifact IS NULL
        AND patch_sha256 IS NULL
      )
    ),
    CHECK (
      (patch_artifact IS NULL AND patch_sha256 IS NULL)
      OR
      (patch_artifact IS NOT NULL AND patch_sha256 IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_conversation_git_targets_pr
    ON conversation_git_targets(conversation_id, pr_number)
    WHERE pr_number IS NOT NULL;
CREATE UNIQUE INDEX idx_conversation_git_targets_local
    ON conversation_git_targets(
      conversation_id, branch_name, first_head_sha
    )
    WHERE pr_number IS NULL;
CREATE INDEX idx_conversation_git_targets_recent
    ON conversation_git_targets(
      conversation_id, last_seen_at DESC, target_id
    );
CREATE INDEX idx_conversation_git_targets_head
    ON conversation_git_targets(
      conversation_id, branch_name, latest_head_sha
    );
CREATE INDEX idx_conversation_git_targets_pr_lookup
    ON conversation_git_targets(pr_number)
    WHERE pr_number IS NOT NULL;

CREATE TRIGGER trg_conversation_git_targets_identity_immutable
BEFORE UPDATE OF
    target_id, conversation_id, branch_name, first_head_sha, first_seen_at
ON conversation_git_targets
BEGIN
  SELECT RAISE(ABORT, 'conversation Git target identity is immutable');
END;

CREATE TRIGGER trg_conversation_git_targets_pr_immutable
BEFORE UPDATE OF pr_number ON conversation_git_targets
WHEN OLD.pr_number IS NOT NULL
  AND NEW.pr_number IS NOT OLD.pr_number
BEGIN
  SELECT RAISE(ABORT, 'conversation Git target PR identity is immutable');
END;

COMMIT;
