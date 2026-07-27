-- 0111 — reseed flags: the verbs shipped in S84 U11 (PR #672, squash 7c82dbc).
-- Generated from assets/skills/flags/SKILL.md; full-body upsert so existing
-- installations converge on the asset.
--
-- Slot re-allocated to this commit by planner ruling (S84 U11): the skill text
-- follows the code onto main rather than describing verbs still in flight.
-- Editing the asset without this migration is exactly the drift
-- tests/test_skills_freshness.py detects.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'flags',
  'Track blockers as flags — surface open ones, open new ones, edit long-lived ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.',
  'substrate',
  NULL,
  0,
  '# flags — blockers & follow-ups

flag = open question / blocker. `--feature <id>` set -> the flag is that
feature''s blocker (joined on the roadmap; shown on the Roadmap card + Flags
tab). `<self>` = your shell_id. All reads/writes go through `sc mem` (the
engine API) — there is no `sqlite3` path.

## Surface

```
sc mem get flags          # your open flags — `#<id> [<label>] (<priority>) <description>`
sc mem get flags --json   # same, as JSON
```

Each flag carries its `feature_id`; cross-reference `sc mem get roadmap` for
the blocked feature''s title.

Both list forms are **open-only**. One flag by id, resolved rows included:

```
GET /_sc/mem/flags/{id}   # no CLI verb — `flag close` reads it before it writes
```

## Open

```
sc mem flag open "[Area] what''s blocked | Blocker for: X" --name SC-001 --priority Medium [--feature <id>]
```

- `--name` = short id, format `SC-###`.
- description format = `[Area] {what} | Blocker for: {what it blocks}`.
- `--priority` = High / Medium / Low. `--feature` = the feature it blocks (omit if none).

### Pair every open with a message

Every `flag open` -> a `message send` to whoever clears it (see the
`messaging` skill), so the work lands in their inbox on their next boot:

```
sc mem message send <shortname> "Opened SC-### — <one line> (Blocker for: <x>)."
```

Recipient = whoever the flag blocks:

| Flag is about | Message |
|---|---|
| docs pending after ship | the **planner** |
| a review failure on a diff | the **author dev** |
| a blocker on another shell''s work | **that shell** |
| an FnB decision / no shell owns it | **surface to the FnB** (no `send`) |

Message pairs with the *open* only: NEVER re-message a flag that is already
open; NEVER message on `close`.

## Edit

```
sc mem flag edit <flag_id> [--name SC-002] [--description "…"] [--append "…"] [--priority High] [--feature <id>]
```

For long-lived tracker flags (one flag per arc, description updated
progressively as gates clear).

- `--name` sets or corrects a `display_name` — including on a flag opened
  without one. An unnamed flag is referred to by bare integer, which is the
  precondition for the id/name collision `close` guards against.
- `--description` REPLACES the whole body — carry forward what still applies.
- `--append` grows the body server-side in one statement. Use it on a tracker
  flag: two shells doing fetch -> concatenate -> `--description` concurrently
  lose one edit. It concatenates raw — pass your own leading `\n` separator.
- `--description` + `--append` together -> the command refuses. Pick one.

## Resolve

```
sc mem flag close <flag_id> --notes "…"
```

`--notes` states *how* it was resolved — that''s the trail.

`close` prints the row it holds — id, label, priority, opened date, owner,
description — BEFORE it writes. **Read that line and confirm it names the flag
you meant.** SC-### display names and `flag_id`s are drawn from two counters
drifting through the same small-integer range, so a stale or mistranscribed
reference does not fail loudly: it resolves a different real record and closes
THAT.

Closing an already-resolved flag -> refused, and it prints the resolution notes
the write would have destroyed. A second close overwrites the notes of whoever
verified the flag.

## Stance

Open a flag the moment something blocks or needs follow-up — don''t hold it in
your head. Open flags on a feature = its blockers; clear them all before
calling the feature done. An opened flag with no message sent = a dropped
handoff.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
