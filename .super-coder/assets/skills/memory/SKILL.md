---
name: memory
description: When + how this shell persists memory — current_state (≤300), session narrative, seed (cap 10), L&S (cap 20, ≤500/entry, --supersedes|--new), decisions — all via sc mem, written as it happens, not at close.
category: substrate
common: true
---

# memory — write as you go

All memory = DB rows; no flat files. Write at the moment it matters, never in a
close ritual.

Every write goes through `sc mem` -> lands in the live shared engine DB, visible
to all shells on commit. It always targets your own shell (identity resolved
from your token) — never name a shell.

## current_state — rolling status, NOT a log

Present focus + what's next. Replace in place; NEVER append. **300 chars, hard
— the write is rejected over it.** Rewrite when focus shifts.
```
sc mem state "…"
```

**Point, do not reproduce.** The overrun is never verbosity, it is restatement:
a decision's reasoning, a spec's gate, a flag's argument all pasted inline when
each is a live row one query away. Name what is in flight and carry the id:

```
Sprint 59 U0 gate — see doc #46, feature #29.
Blocked on flag #200. Next: U3 shape once U0 answers.
```

Not the argument, the ruling, or the rationale — those have rows, and a reader
who needs them runs `sc mem get`. Same principle the boot doc already applies
to decisions: carry the pointer, lazy-load the payload.

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

Operating lessons, imperative voice. An entry is **the RULE** — **≤500 chars,
hard**. The incident that taught it goes in the narrative, where you already
wrote it; if the text opens with "Sprint 38:", it is a narrative entry.

**Exactly one of `--supersedes` / `--new` is required.** Your active set is
already rendered in your boot doc, so checking a new rule against it costs no
extra read — and this flag is where that check lands:
```
sc mem lns "…" --supersedes 29,36   # contradicts or refines those — retires them, adds this
sc mem lns "…" --new                # checked against the set, genuinely unrelated
```
`--supersedes` works at 20/20: it frees the slot it uses.

Caps are trigger-enforced (seed 10, L&S 20, L&S body 500, current_state 300) —
a rejected write is the feedback, and the message routes the fix.

Periodic sweep: when `## STATUS` says `L&S: … — curation due`, run the `curate`
skill, then `sc mem curated` to stamp it — even if you retired nothing. Cap 20
is a ceiling never to reach, not a target; curation holds the set near 12–14.

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
