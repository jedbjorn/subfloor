---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# memory

When + how this shell persists memory — current_state, session narrative, seed (cap 10), L&S (cap 20), decisions — all via sc mem, written as it happens, not at close.

**Category:** substrate

---

# memory — write as you go

All memory = DB rows; no flat files. Write at the moment it matters, never in a
close ritual.

Every write goes through `sc mem` -> lands in the live shared engine DB, visible
to all shells on commit. It always targets your own shell (identity resolved
from your token) — never name a shell.

## current_state — rolling status, NOT a log

Present focus + what's next. Replace in place; NEVER append. Soft target ~500
chars. Rewrite when focus shifts.
```
sc mem state "…"
```

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (time is
stamped for you) when: a decision lands / an approach changes or is rejected /
the FnB says something that shapes the work / an assumption breaks / before a
big change.
```
sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add new entries only; NEVER edit
a body — curate by retiring. The genesis + lineage seed are already yours.
```
sc mem seed "…"            # add
sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. Add when a lesson lands; curate by
retiring. Caps are trigger-enforced (seed 10, L&S 20): at cap, `sc mem` returns
the cap message -> retire an entry to free the slot.
```
sc mem lns "…"
```

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
NEVER rewrite one — supersede via `--parent <decision_id>`. Mirror the headline
into the narrative.
```
sc mem decision "…" --rationale "…" [--parent <id>]
```

Link the why to the what — attach the feature/spec the decision shapes, so the
roadmap carries why it was built that way:
```
sc mem decision "…" --feature <feature_id>   # ties it to a roadmap feature
sc mem decision "…" --doc <document_id>       # ties it to a spec/doc (implies the feature)
```
Both optional — a decision unrelated to any feature stays unlinked. `--doc`
implies `--feature`: pass the doc alone -> feature derived from it. The link
surfaces on `sc mem get decisions <id>`.

## Stance

Write-as-you-go beats batch-at-close: nothing per write, zero at session end.
Curate seed/L&S (revise the set); never rewrite history (decisions, narrative,
seed bodies). Full command reference + table map: the `db_map` skill.
