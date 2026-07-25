-- 0097 — Provider Quota: drop account identity from the registry (spec doc 57, feature 17).
--
-- Spec 49 built a card per ACCOUNT, labelled with who is signed in. Decision
-- #75 replaced that with a card per PROVIDER showing its most recent reading
-- and the age of that reading, and nothing about the operator's session. This
-- migration removes what the old idea needed and nothing else.
--
-- ── Why the label goes, and it is not a redaction ────────────────────────────
--
-- The label was never going to be consistent across the three providers, which
-- is the fact the whole labelling chain (#66 redact → #67 full email → #69 the
-- Anthropic email path) was missing. Verified by capturing all three payloads
-- live: only OpenAI returns an email in its usage RESPONSE; Anthropic's comes
-- from ~/.claude.json, not the API at all; and Moonshot's payload carries no
-- email anywhere — user{} holds userId, region, membership and businessId, full
-- stop. So two of three cards always rendered an opaque identifier, and the
-- operator saw exactly that. A rule reasoned from the one provider that
-- cooperates is not a rule.
--
-- Dropping the column is therefore not a stricter redaction of the same design.
-- The design is gone: no probe collects an operator label of any kind, so there
-- is no value to redact, and the exposure class ceases to exist rather than
-- being defended.
--
-- ── CORRECTING 0096's COMMENT, which is why this file says all of the above ──
--
-- 0096 is landed and is NOT edited in place. Two things it states are now
-- wrong, and a future reader has no way to know that from 0096 alone:
--
-- 1. It states snapshot exclusion is LOAD-BEARING, on decision #67's ground
--    that account_label holds the operator's full email and exclusion is "the
--    ONLY thing between full operator emails and the repository". With no email
--    ever written, THAT RATIONALE IS RETIRED AT ITS SOURCE.
--
--    THE EXCLUSION ITSELF STAYS, AND SO DOES ITS TEST. The rationale simply
--    reverts to decision #65's original and still-sufficient one: these are
--    probe-rebuildable caches, exactly like session_token_usage (0071), and a
--    rebuild re-derives all of it from the next probe. Ordinary hygiene, which
--    was always reason enough. The test is not weakened — it is cheap, still
--    correct, and a test deleted because "the risk went away" is how the risk
--    comes back.
--
-- 2. Its account_label comment describes the label ladder as "FULL email
--    (OpenAI) else user.userId (Moonshot) else credential uuid first 8
--    (Anthropic)". That was ALREADY WRONG when 0096 landed — conformance SC-C2
--    found Anthropic's label was the full email under decision #69, not the
--    uuid's first 8 — and it is doubly wrong now that no ladder exists at all.
--
-- Also retired: 0096's second-order note that docs/images/analytics.png is a
-- committed screenshot needing a scrubbed label. With no label rendered there
-- is nothing in that shot to scrub.
--
-- ── is_current goes too: no reader AND no writer ─────────────────────────────
--
-- is_current existed to tell one account's card from another's — which account
-- the credential file resolves to NOW — and it had exactly two readers: the
-- API's exemption from the 7-day activity filter, and the UI's muted rendering
-- of a non-current account. Spec 57 removes both, along with the 7-day filter
-- itself and the upsert's pre-clear that maintained the column. A provider-level
-- panel shows the newest reading per provider and never has to answer "which
-- account", so nothing is left that could read it and nothing is left that
-- writes it. A column with neither is a question for the next person.
--
-- idx_hqa_last_seen goes with it: it existed solely to drive the 7-day filter.
-- The COLUMN last_seen stays — the writer still maintains it, and it is the
-- provenance of account_ref, which decision #75 explicitly keeps as an internal
-- upsert key so a repeated probe updates a row instead of duplicating it.
-- account_ref is provider-issued, is never an email, and is never rendered.
--
-- `plan` also stays. It is not identity (max / prolite / LEVEL_ADVANCED), it is
-- absent from the API response and from the card, and spec 57's verification
-- gate pins where each probe must read it from — which a dropped column could
-- not satisfy. Two of the three were reading it from the wrong place.

BEGIN;

ALTER TABLE harness_quota_account DROP COLUMN account_label;
ALTER TABLE harness_quota_account DROP COLUMN is_current;

DROP INDEX IF EXISTS idx_hqa_last_seen;

COMMIT;
