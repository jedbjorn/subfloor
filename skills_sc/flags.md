---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# flags

Track blockers as flags — surface open ones, open new ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.

**Category:** substrate

---

# flags — blockers & follow-ups

A flag is an open question or blocker. Linking it to a `feature_id` makes it that
feature's blocker (joined on the roadmap + shown on the Roadmap card and the
Flags tab). `<self>` = your shell_id.

## Surface

```
./sc mem get flags          # your open flags (id, name, priority, description) — via the API
./sc mem get flags --json   # same, as JSON
```

(Need the roadmap-feature title joined in? That join isn't in the `get` surface —
fall back to a read-only `sqlite3 .super-coder/shell_db.db "SELECT f.flag_id,
f.display_name, f.priority, f.description, r.title AS feature FROM flags f LEFT
JOIN roadmap r ON r.feature_id=f.feature_id WHERE f.resolved=0 AND
COALESCE(f.is_deleted,0)=0 ORDER BY f.priority, f.flag_id;"`.)

## Open

Write through `./sc mem` (it guards the engine DB + snapshots):
```
./sc mem flag open "[Area] what's blocked | Blocker for: X" --name SC-001 --priority Medium [--feature <id>]
```
- `--name`: short id (`SC-###`).
- the description is `[Area] {what} | Blocker for: {what it blocks}`.
- `--priority`: High / Medium / Low. `--feature`: the feature it blocks (or omit).

### Always pair the open with a message

A flag sitting in the DB that no one is told about isn't a handoff — it's a note
to yourself. **Every flag you open also sends a message to whoever clears it**, so
the work lands in their inbox on their next boot (see the `messaging` skill):

```
./sc mem message send <shortname> "Opened SC-### — <one line> (Blocker for: <x>)."
```

Resolve the recipient by what the flag blocks:

| Flag is about | Message |
|---|---|
| docs pending after ship | the **planner** |
| a review failure on a diff | the **author dev** |
| a blocker on another shell's work | **that shell** |
| an FnB decision, or no shell owns it | **surface to the FnB** (no `send`) |

Idempotent: pair the message with the *open*. Don't re-message a flag that's
already open, and don't message on `close`.

## Resolve

```
./sc mem flag close <flag_id> --notes "…"
```

(equivalent raw write, for reference: `UPDATE flags SET resolved=1,
resolved_date=date('now'), resolution_notes='…' WHERE flag_id=?;`)

## Stance

Open a flag the moment something is blocked or needs follow-up — don't hold it in
your head. Resolve with a note saying *how*, so the trail is legible. Open flags
on a feature are its blockers; clear them before calling the feature done. **An
opened flag always rides out on a message** to whoever clears it — a flag nobody
is told about is a dropped handoff.
