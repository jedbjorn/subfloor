-- 0048 — reseed memory skill: the why-audit decision link (#0047 companion).
--
-- 0047 added shell_decisions.feature_id + document_id. This carries the skill
-- prose that documents them to already-installed forks, and — critically —
-- makes the memory skill the migration ledger's LAST word on its own content.
--
-- Why a migration and not just the asset edit: 0001_seed_skills.sql is
-- regenerated from assets/skills/ (fresh builds get the new body), but a later
-- reseed (0028) still carries an inline, now-older memory body. On a fresh
-- rebuild — schema.sql, then every migration in order — 0028 would overwrite
-- 0001's fresh body with its stale one, leaving the DB out of sync with the
-- asset (the skills-freshness tripwire). Re-stating the current body here, after
-- 0028, restores "last write wins = the asset" and heals existing forks in the
-- same pass. Body is the verbatim assets/skills/memory/SKILL.md content;
-- regenerate this file if the asset changes again.
--
-- Plain SQL: migrate.py owns the transaction and the schema_migrations row.

UPDATE skills SET content = '# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual.

**Write through `sc mem`.** The write lands in the live engine DB — shared by
every shell, durable + visible to all the moment it commits. It always targets
your own shell: the server resolves your identity from your token, so you never
name a shell.

## current_state — rolling status, NOT a log

Your present focus + what''s next. **Replaces in place; never a log.** Soft target
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
narrative, seed bodies). Full command reference + table map: the `db_map` skill.'
  WHERE name = 'memory' AND is_deleted = 0;
