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
-- ── EVERY OTHER COLUMN GOES TOO: each one answered "WHICH ACCOUNT" ───────────
--
-- The registry is left as (account_pk, provider, account_ref) and nothing else.
-- Each dropped column had its own reason, and they converge on one:
--
-- is_current told one account's card from another's — which account the
-- credential file resolves to NOW. Exactly two readers: the API's exemption
-- from the 7-day activity filter, and the UI's muted rendering of a
-- non-current account. Spec 57 removes both, plus the 7-day filter itself and
-- the upsert's pre-clear that maintained the column. Nothing reads it and
-- nothing writes it; a column with neither is a question for the next person.
--
-- first_seen and last_seen lost their last reader to a BUILD DECISION, which is
-- why they were not in this migration's first draft. Selecting the newest
-- reading per provider needs an ordering key, and last_seen looks like it. But
-- the panel selects on the WINDOW's captured_at instead — a row with no windows
-- then has no reading and cannot outrank one with numbers, which is both what
-- the spec literally asks for and what makes flag #196's stale guessed-ref rows
-- unable to win and impossible to see, with no data migration to hunt them
-- down. That choice removes last_seen's only reader; first_seen never had a
-- stronger one.
--
-- plan is the one that looked harmless enough to keep: not identity in the way
-- an email is (max / prolite / LEVEL_ADVANCED), and each probe could read it
-- correctly. But decision #75 displays no plan and the API returns none, so
-- every read fed a column no one queried — which is precisely why TWO OF THE
-- THREE PROBES WERE READING IT FROM THE WRONG PLACE, undetected: moonshot from
-- the top level instead of user.membership, anthropic from a payload key the
-- wire has never sent. A column nothing queries cannot report that it is wrong.
--
-- An earlier draft of spec 57 kept a verification item requiring plan to be
-- read from user.membership.level. That item was carried over from the defect
-- list written BEFORE the simplification ruling and contradicted the ruling
-- itself; it is withdrawn, and the column goes with it.
--
-- ── WHY "cheap provenance" DOES NOT SAVE ANY OF THEM ─────────────────────────
--
-- The instinct to keep a timestamp because it costs nothing is the one this
-- migration deliberately refuses. THESE TABLES ARE PROBE-REBUILDABLE CACHES:
-- provenance that a single probe regenerates is not provenance. If first_seen
-- is wanted later it is added deliberately and refills on the next probe.
--
-- The table is NOT collapsed. account_ref earns its keep as the upsert key that
-- stops a repeated probe duplicating rows; it is provider-issued, is never an
-- email, and is never rendered.
--
-- idx_hqa_last_seen goes because it existed solely to drive the 7-day filter,
-- and because SQLite refuses to drop an indexed column — hence the order below:
-- the index first, then the column it indexed.

BEGIN;

DROP INDEX IF EXISTS idx_hqa_last_seen;

ALTER TABLE harness_quota_account DROP COLUMN account_label;
ALTER TABLE harness_quota_account DROP COLUMN is_current;
ALTER TABLE harness_quota_account DROP COLUMN plan;
ALTER TABLE harness_quota_account DROP COLUMN first_seen;
ALTER TABLE harness_quota_account DROP COLUMN last_seen;

COMMIT;
