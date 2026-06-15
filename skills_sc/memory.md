---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# memory

How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.

**Category:** substrate

---

# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual.

**Write through `./sc mem`, never raw `sqlite3`.** Two DBs are in reach (your
engine DB and the app's product DB) and their table names overlap — a raw
`INSERT INTO shell_decisions …` against the wrong one *succeeds silently*.
`./sc mem` resolves + guards *this* engine DB, refuses the app DB or an empty
stub, and snapshots the change so it survives a rebuild (no separate
`./sc snapshot`). `./sc mem which` shows the resolved DB; raw `sqlite3` is for
SELECT only. Writes default to your shell; pass `--shell <id|name>` to be explicit.

## current_state — rolling status, NOT a log

Your present focus + what's next. **Replaces in place; never a log.** Soft target
~500 chars. Rewrite when focus shifts.
```
./sc mem state "…"
```

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (the time is
stamped for you) when: a decision lands, an approach changes or is rejected, the
FnB says something that shapes the work, an assumption breaks, or before a big
change.
```
./sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add a new entry; **never edit a
body** (curate by retiring). The genesis + lineage seed are already yours.
```
./sc mem seed "…"            # add
./sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. Add when a lesson lands; curate by retiring.
Caps are trigger-enforced (seed 10, L&S 20) — `./sc mem` reports the cap message;
retiring frees a slot.
```
./sc mem lns "…"
```

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
Never rewritten; supersede via `--parent <decision_id>`. Mirror the headline into
the narrative.
```
./sc mem decision "…" --rationale "…" [--parent <id>]
```

## Stance

Write-as-you-go beats batch-at-close: it costs nothing per write and zero at
session end. Curate seed/L&S (revise the set), never rewrite history (decisions,
narrative, seed bodies). The underlying tables are documented in `db_map` — read
them with raw SELECT, write them with `./sc mem`.
