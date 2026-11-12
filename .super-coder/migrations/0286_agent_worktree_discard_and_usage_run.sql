-- 0286 — subagent worktree discard and usage run tag.
-- Intent (Feature #96, spec #273 "Inspect, continue, stop" and "Usage"):
-- `sc job clean --discard` records that a kept subagent worktree was removed
-- with work in it, so a later continue is refused; and the analytics sweep
-- tags a usage row with the subagent run whose worktree the session ran in.
-- Both columns are empty on every existing row.
ALTER TABLE run_agents ADD COLUMN worktree_discarded_at TEXT;
ALTER TABLE session_token_usage ADD COLUMN run_id INTEGER REFERENCES runs(run_id);
CREATE INDEX IF NOT EXISTS idx_stu_run ON session_token_usage(run_id)
    WHERE run_id IS NOT NULL;
