-- 0201 — durable successful-Sprint cleanup targets.

BEGIN;

CREATE TABLE sprint_cleanup_targets (
    cleanup_target_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id             INTEGER NOT NULL REFERENCES sprints(sprint_id),
    shell_id              INTEGER REFERENCES shells(shell_id),
    target_kind           TEXT NOT NULL
                          CHECK (target_kind IN ('worktree','artifact_dir')),
    canonical_path        TEXT NOT NULL
                          CHECK (trim(canonical_path)<>''
                                 AND substr(canonical_path,1,1)='/'),
    repository_root       TEXT NOT NULL
                          CHECK (trim(repository_root)<>''
                                 AND substr(repository_root,1,1)='/'),
    git_common_dir        TEXT NOT NULL
                          CHECK (trim(git_common_dir)<>''
                                 AND substr(git_common_dir,1,1)='/'),
    expected_base_branch  TEXT,
    state                 TEXT NOT NULL DEFAULT 'pending'
                          CHECK (state IN
                            ('pending','running','succeeded','failed')),
    attempt_count         INTEGER NOT NULL DEFAULT 0
                          CHECK (attempt_count >= 0),
    claim_generation      INTEGER NOT NULL DEFAULT 0
                          CHECK (claim_generation >= 0),
    lease_owner           TEXT,
    lease_expires_at      TEXT,
    waiting_reason        TEXT CHECK (
                            waiting_reason IS NULL
                            OR length(waiting_reason) <= 120
                          ),
    before_evidence       TEXT CHECK (
                            before_evidence IS NULL
                            OR (json_valid(before_evidence)
                                AND json_type(before_evidence)='object')
                          ),
    after_evidence        TEXT CHECK (
                            after_evidence IS NULL
                            OR (json_valid(after_evidence)
                                AND json_type(after_evidence)='object')
                          ),
    last_error_code       TEXT CHECK (
                            last_error_code IS NULL
                            OR length(last_error_code) <= 120
                          ),
    last_error_detail     TEXT CHECK (
                            last_error_detail IS NULL
                            OR length(last_error_detail) <= 2000
                          ),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at            TEXT,
    completed_at          TEXT,
    CHECK (
      (target_kind='worktree'
       AND shell_id IS NOT NULL
       AND expected_base_branch GLOB 'shell/*')
      OR
      (target_kind='artifact_dir'
       AND shell_id IS NULL
       AND expected_base_branch IS NULL)
    ),
    CHECK (
      (state='running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
      OR state<>'running'
    ),
    UNIQUE (sprint_id, target_kind, canonical_path)
);

CREATE UNIQUE INDEX idx_sprint_cleanup_one_worktree_per_shell
    ON sprint_cleanup_targets(sprint_id, shell_id)
    WHERE target_kind='worktree';
CREATE INDEX idx_sprint_cleanup_claimable
    ON sprint_cleanup_targets(state, lease_expires_at, sprint_id,
                              cleanup_target_id);
CREATE INDEX idx_sprint_cleanup_shell_gate
    ON sprint_cleanup_targets(shell_id, state, sprint_id)
    WHERE target_kind='worktree';

CREATE TRIGGER trg_sprint_cleanup_completed_only
BEFORE INSERT ON sprint_cleanup_targets
WHEN (SELECT lifecycle FROM sprints WHERE sprint_id=NEW.sprint_id)<>'completed'
BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup targets require completed lifecycle');
END;

CREATE TRIGGER trg_sprint_cleanup_identity_immutable
BEFORE UPDATE OF sprint_id,shell_id,target_kind,canonical_path,repository_root,
                 git_common_dir,expected_base_branch
ON sprint_cleanup_targets
WHEN NEW.sprint_id IS NOT OLD.sprint_id
  OR NEW.shell_id IS NOT OLD.shell_id
  OR NEW.target_kind IS NOT OLD.target_kind
  OR NEW.canonical_path IS NOT OLD.canonical_path
  OR NEW.repository_root IS NOT OLD.repository_root
  OR NEW.git_common_dir IS NOT OLD.git_common_dir
  OR NEW.expected_base_branch IS NOT OLD.expected_base_branch
BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup target identity is immutable');
END;

CREATE TRIGGER trg_sprint_cleanup_counters_monotonic
BEFORE UPDATE OF attempt_count,claim_generation ON sprint_cleanup_targets
WHEN NEW.attempt_count < OLD.attempt_count
  OR NEW.claim_generation < OLD.claim_generation
BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup counters cannot decrease');
END;

CREATE TRIGGER trg_sprint_cleanup_success_terminal
BEFORE UPDATE OF state ON sprint_cleanup_targets
WHEN OLD.state='succeeded' AND NEW.state<>'succeeded'
BEGIN
  SELECT RAISE(ABORT, 'Succeeded Sprint cleanup targets are terminal');
END;

CREATE TRIGGER trg_sprint_cleanup_no_delete
BEFORE DELETE ON sprint_cleanup_targets BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup targets are durable evidence');
END;

COMMIT;
