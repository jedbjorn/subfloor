-- 0009 — fix codex model ids for ChatGPT-account use.
--
-- 0007 seeded the codex workhorse rows (dev, cartographer) with model 'gpt-5.4'.
-- That id is API-only — Codex driven by a ChatGPT subscription (which is the
-- whole reason the codex harness exists: flat billing, no per-token metering)
-- rejects it: HTTP 400 "The 'gpt-5.4' model is not supported when using Codex
-- with a ChatGPT account." The two ids codex+ChatGPT actually exposes are
-- 'gpt-5.5' (premium — already on planner/reviewer) and 'gpt-5.4-mini' (the
-- fast/cheap step below it). Repoint the workhorse rows to the mini, which
-- preserves the intended tiering (bookends premium, middle roles cheaper).
--
-- Idempotent: targets the dead id, so a no-op once corrected (or on a fresh
-- install whose 0007 already seeds 'gpt-5.4-mini').
UPDATE flavor_defaults SET model = 'gpt-5.4-mini'
 WHERE harness = 'codex' AND model = 'gpt-5.4';
