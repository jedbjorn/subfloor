-- 0081 — Transactional brokered planner wake (sprint 25 seq 8, spec #20
-- task #84, HARD requirement #49 from decisions #28/#31).
--
-- provider_ready_at stamps REAL provider readiness: the provider-native
-- session_start hook (never the entrypoint's pre-exec identity claim). The
-- wake gate's quiet debounce measures from this stamp — a >3s claude/codex
-- boot can no longer let a queued wake submit into an unpainted TUI just
-- because the pre-exec occupied_at baseline already aged past the debounce
-- (flag #49). NULL = the provider has not proven readiness yet; the gate
-- then falls back to occupied_at/created_at exactly as before.

ALTER TABLE interface_sessions ADD COLUMN provider_ready_at TEXT;
