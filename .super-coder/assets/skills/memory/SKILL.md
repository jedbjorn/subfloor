---
name: memory
description: How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.
category: substrate
common: true
---

# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual. The `.db` is a cache — after writing, `./sc snapshot` serializes to
the text git tracks (see the `snapshot` skill). `<self>` = your shell_id.

## current_state — rolling status, NOT a log

Your present focus + what's next. **UPDATE in place; never append.** Soft target
~500 chars. Rewrite when focus shifts.
```sql
UPDATE shells SET current_state='…' WHERE shell_id=<self>;
```

## Session narrative — append at inflection points

One row per session (`shell_memory_archives`, the active one is
`active_archive_id`). Append `[HH:MM] {1–2 lines}` when: a decision lands, an
approach changes or is rejected, the FnB says something that shapes the work, an
assumption breaks, or before a big change. Edit the H1 when the session's
headline crystallizes.
```sql
UPDATE shell_memory_archives
SET full_narrative = full_narrative || char(10) || '[HH:MM] …' || char(10)
WHERE archive_id = (SELECT active_archive_id FROM shells WHERE shell_id=<self>);
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. INSERT to add; **never edit a body**
(curate by setting `retired_at`). The genesis + lineage seed are already yours.
```sql
INSERT INTO shell_identity_entries (shell_id, kind, entry_date, body)
VALUES (<self>, 'seed', date('now'), '…');
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. INSERT when a lesson lands; curate via
`retired_at`. Caps are trigger-enforced (seed 10, L&S 20) — retire to free a slot.
```sql
INSERT INTO shell_identity_entries (shell_id, kind, entry_date, body)
VALUES (<self>, 'lns', date('now'), '…');
```

## Decisions — Major only

INSERT a row on a Major decision (architecture, approach, a path chosen over
another). Never edit a prior row; supersede via `parent_decision_id`. Mirror it
into the narrative.
```sql
INSERT INTO shell_decisions (shell_id, decision_date, priority, decision, rationale)
VALUES (<self>, date('now'), 'M', '…', '…');
```

## Stance

Write-as-you-go beats batch-at-close: appending costs nothing per write and zero
at session end. Curate seed/L&S (revise the set), never rewrite history
(decisions, narrative, seed bodies).
