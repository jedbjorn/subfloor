-- 0100 — L&S self-curation: the curation stamp + two hard length caps.
--
-- The boot doc is dominated by one section. For a planner shell at cap,
-- LESSONS & STANCES was 42% of the rendered CLAUDE.md (19,284 of 45,491
-- chars) and grew without bound, because the only cap counts ENTRIES while
-- the cost is in CHARACTERS: that shell sat at 20/20 for six days while the
-- average entry went 297 -> 2,325 chars. Curating at the cap even NET-ADDED
-- chars (one 364-char entry retired to admit a 2,325-char one), so any
-- signal that resets on "a retirement happened" is gameable by exactly the
-- behaviour observed.
--
-- Three objects, one theme — make the unit of the cap the unit of the cost:
--
--   lns_curated_at             the stamp a curation sweep writes. The boot
--                              render counts entries created after it; >= 5
--                              renders a STATUS advisory pointing at the
--                              `curate` skill. Stamping is unconditional —
--                              a sweep that retires NOTHING still clears the
--                              counter, or a legitimately clean set would
--                              leave a standing reminder forever.
--
--   trg_sie_len_lns            an L&S entry is the RULE, imperative. The
--                              incident belongs in the narrative, where it
--                              is already written.
--
--   trg_shells_current_state_len   current_state is the second boot-rendered
--                              surface with a soft target and no enforcement,
--                              and it drifted the same way (fleet: 3.7x-10.3x
--                              over the documented ~300). The overrun is not
--                              verbosity, it is RESTATEMENT — reproducing
--                              decisions, spec gates and flags inline when
--                              each is a live row one query away.
--
-- ENFORCE, DO NOT ADVISE. A soft target gets over-run by a shell that
-- believes it complied — shells do many things well; counting is not one of
-- them. The rejection IS the feedback mechanism, which is why every other cap
-- here (seed 10, L&S 20, singleton cartographer) is a trigger. Both messages
-- route the overflow rather than only refusing: a refusal that only refuses
-- produces a truncated lesson.
--
-- 500 is a judgment call, not a reading of the data. Across the fleet's 49
-- active entries there is no empty band to place a threshold in (the clean
-- 517-948 gap is one shell's). 500 admits the disciplined-era entries and
-- rejects every incident report fleet-wide; rejecting most of the existing
-- corpus is the INTENT, not a side effect — that corpus is the defect.
--
-- BEFORE INSERT / BEFORE UPDATE, so nothing existing is touched: the entries
-- and states already in place stay readable and renderable. The caps
-- constrain new writes; the periodic sweep clears the legacy. Two mechanisms,
-- different halves, neither needs to know about the other.
--
-- The count-20 cap (trg_sie_cap_lns) stays. With the sweep running it becomes
-- a backstop that should never fire — if it does, the sweep is not running.
-- 20 entries x 500 chars is now a mathematical ceiling of 10,000 chars, which
-- is why this ships no character-budget trigger: the per-entry cap bounds the
-- total by construction, so there is one threshold to reason about, not two.

BEGIN;

ALTER TABLE shells ADD COLUMN lns_curated_at TEXT;   -- NULL = never swept

-- One string literal per RAISE: SQLite has no implicit adjacent-literal
-- concatenation, so a message split across source lines is a syntax error.
--
-- The created_at window is what keeps the engine REBUILDABLE. `.sc-state/
-- content.sql` replays every identity entry as an INSERT, so an unconditional
-- BEFORE INSERT cap would abort the rebuild of any fork already holding a
-- legacy 2,914-char entry — and those entries cannot be edited away first:
-- Laws 3 and 7 reserve curation to the shell that owns them. A replayed row
-- carries its ORIGINAL created_at and is a restore of already-accepted state,
-- not a new write; a live write's created_at is the column default, i.e. now.
-- Five minutes is far outside any clock jitter between the default and this
-- WHEN clause, and — since no oversized row can be created after this
-- migration — every replayed offender is older than the window by construction.
-- Old content.sql artifacts need no regeneration, which a marker-table scheme
-- would have required (and SQLite cannot read temp schema from a trigger
-- anyway: it resolves the name against `main`).
CREATE TRIGGER trg_sie_len_lns
BEFORE INSERT ON shell_identity_entries
WHEN NEW.kind = 'lns' AND LENGTH(NEW.body) > 500
     AND NEW.created_at >= datetime('now', '-300 seconds')
BEGIN
  SELECT RAISE(ABORT, 'L&S entry over 500 chars — an L&S entry is the RULE; the incident goes to the narrative (sc mem narrative), then state the rule alone');
END;

CREATE TRIGGER trg_shells_current_state_len
BEFORE UPDATE OF current_state ON shells
WHEN LENGTH(COALESCE(NEW.current_state, '')) > 300
BEGIN
  SELECT RAISE(ABORT, 'current_state over 300 chars — name what is in flight and point at the row (doc/feature/flag/decision id), do not reproduce it');
END;

COMMIT;
