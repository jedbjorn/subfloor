---
name: memory
description: How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.
category: substrate
common: true
---

# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual.

**Write through `sc mem`.** The write lands in the live engine DB — shared by
every shell, durable + visible to all the moment it commits. It always targets
your own shell: the server resolves your identity from your token, so you never
name a shell.

## current_state — rolling status, NOT a log

Your present focus + what's next. **Replaces in place; never a log.** Soft target
~500 chars. Rewrite when focus shifts.
```
sc mem state "…"
```

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (the time is
stamped for you) when: a decision lands, an approach changes or is rejected, the
FnB says something that shapes the work, an assumption breaks, or before a big
change.
```
sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add a new entry; **never edit a
body** (curate by retiring). The genesis + lineage seed are already yours.
```
sc mem seed "…"            # add
sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. Add when a lesson lands; curate by retiring.
Caps are trigger-enforced (seed 10, L&S 20) — `sc mem` reports the cap message;
retiring frees a slot.
```
sc mem lns "…"
```

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
Never rewritten; supersede via `--parent <decision_id>`. Mirror the headline into
the narrative.
```
sc mem decision "…" --rationale "…" [--parent <id>]
```

**Link the why to the what.** Most decisions shape a feature or come out of a
spec — attach that link so the roadmap carries not just what was built and how,
but why it was built that way:
```
sc mem decision "…" --feature <feature_id>   # ties it to a roadmap feature
sc mem decision "…" --doc <document_id>       # ties it to a spec/doc (implies the feature)
```
Both optional — a decision unrelated to any feature stays unlinked. `--doc` is a
refinement of `--feature`: pass the doc alone and the feature is derived from it.
The link surfaces on `sc mem get decisions <id>`.

## Stance

Write-as-you-go beats batch-at-close: it costs nothing per write and zero at
session end. Curate seed/L&S (revise the set), never rewrite history (decisions,
narrative, seed bodies). Full command reference + table map: the `db_map` skill.
