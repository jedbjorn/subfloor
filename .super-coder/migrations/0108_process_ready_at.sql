-- 0108 — first-turn-gated harness readiness (sprint 84 U7, flag #303,
-- decisions #98/#99).
--
-- WHY A SECOND COLUMN RATHER THAN A SECOND MEANING FOR provider_ready_at.
--
-- `provider_ready_at` (migration 0081) means one thing: the harness's OWN
-- session_start hook arrived, so the provider handshaked and will answer.
-- Codex 0.145.0 never sends that hook until a human submits the first turn
-- (measured on a live TUI seat — flag #303), so a codex seat that exists to
-- BE woken deadlocks: the wake path waits on a hook that only the thing the
-- wake path exists to do can trigger.
--
-- `process_ready_at` records the strictly WEAKER proof the entrypoint already
-- has: the pane is live and interface_exec is about to exec the harness with
-- its hooks installed. That proves the PROCESS is up. It does NOT prove the
-- provider will answer.
--
-- Stamping the weak proof into `provider_ready_at` would make that column mean
-- two different things depending on harness, and every existing reader that
-- trusts it as "provider handshaked" would become silently wrong for codex —
-- the assert-more-than-you-enforce defect decision #61 records. Hence a
-- distinct column, so the distinction survives in the record:
--
--   process_ready_at set, provider_ready_at NULL -> process ready, provider
--                                                   UNPROVEN
--   provider_ready_at set                        -> provider handshaked
--
-- Both are stamped for every harness. Only a harness whose capability
-- readiness class is 'first_turn_gated' (codex) is PROMOTED to idle on the
-- weak proof; claude and kimi still wait for their native hook exactly as
-- before, and for them this column is a redundant record of occupied_at.
--
-- The trade is deliberate and is an upgrade path, not a lowered bar: a codex
-- seat proceeds on weak proof and upgrades to strong proof when the first real
-- turn fires session_start. If the provider is in fact down, the submit goes
-- out and FAILS — loudly, via the persistent-gate-failure alert — instead of
-- the status quo of never arming at all, silently, forever.
--
-- Nullable, no default: every pre-existing interface_sessions row stays valid
-- and reads as "no process-readiness stamp recorded", which is true of them.

BEGIN;

ALTER TABLE interface_sessions ADD COLUMN process_ready_at TEXT;

COMMIT;
