---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# flags

Track blockers as flags — surface open ones, open new ones, edit long-lived ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.

**Category:** substrate

---

# flags — blockers & follow-ups

flag = open question / blocker. `--feature <id>` set -> the flag is that
feature's blocker (joined on the roadmap; shown on the Roadmap card + Flags
tab). `<self>` = your shell_id. All reads/writes go through `sc mem` (the
engine API) — there is no `sqlite3` path.

## Surface

```
sc mem get flags          # your open flags (id, name, priority, description)
sc mem get flags --json   # same, as JSON
```

Each flag carries its `feature_id`; cross-reference `sc mem get roadmap` for
the blocked feature's title.

## Open

```
sc mem flag open "[Area] what's blocked | Blocker for: X" --name SC-001 --priority Medium [--feature <id>]
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
| a blocker on another shell's work | **that shell** |
| an FnB decision / no shell owns it | **surface to the FnB** (no `send`) |

Message pairs with the *open* only: NEVER re-message a flag that is already
open; NEVER message on `close`.

## Edit

```
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
```

For long-lived tracker flags (one flag per arc, description updated
progressively as gates clear). `--description` replaces the whole text —
carry forward what still applies.

## Resolve

```
sc mem flag close <flag_id> --notes "…"
```

`--notes` states *how* it was resolved — that's the trail.

## Stance

Open a flag the moment something blocks or needs follow-up — don't hold it in
your head. Open flags on a feature = its blockers; clear them all before
calling the feature done. An opened flag with no message sent = a dropped
handoff.
