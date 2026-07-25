-- 0093 — chat title + launch effort on interface_sessions (spec #43 U4).
--
-- Two columns, ONE migration, deliberately: U7's +Chat reuses the launch
-- triple (harness, model_route, effort) of the session it ends, and effort
-- rides only in the transient launch-<id>.json token today — so "same model"
-- would silently drop a chosen effort. Splitting the columns across two
-- migrations would land U7's dependency behind a second deploy for no gain.
--
-- title: minted ONCE per session by the CLIENT at the first successful
-- composer send — first line of that message, capped at 60 chars, never
-- updated after (set-if-unset). NULL = no composer message has been sent
-- yet; a session driven only by raw terminal typing keeps no title. The
-- input broker stays content-blind (decision #16 byte privacy): it cannot
-- tell a composer send from a raw keystroke, so a broker-side rule would
-- mint one-character titles. This column is the one NAMED exception —
-- exactly one operator-visible 60-char line from the composer path.
--
-- launch_effort: the effort the session was LAUNCHED with, written at
-- session creation from the request body. NULL on pre-migration rows and
-- on launches that named no effort → +Chat sends no effort → harness
-- default. Like model_route this records the launch route, never a claim
-- about what the harness is running now (decision #55).

ALTER TABLE interface_sessions ADD COLUMN title TEXT;
ALTER TABLE interface_sessions ADD COLUMN launch_effort TEXT;
