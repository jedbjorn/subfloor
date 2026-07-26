-- 0101 — forward-reseed `memory` + seed `curate`: L&S self-curation (0100's
-- companion, the prose half).
--
-- 0100 added the mechanism — the curation stamp and the two hard length caps.
-- A cap nobody was told about is just a wall in the dark, so the skills that
-- teach the write path have to reach already-installed forks in the same pass.
--
-- Why a migration and not only the asset edit: 0001_seed_skills.sql is
-- regenerated from assets/skills/ (fresh builds get the new bodies), but a
-- LATER reseed — 0048 for `memory` — still carries an inline, now-older body.
-- On a fresh rebuild (schema.sql, then every migration in order) 0048 would
-- overwrite 0001's fresh body with its stale one and leave the DB out of sync
-- with the asset, which is exactly what the skills-freshness tripwire exists to
-- catch. Re-stating the current body HERE, after 0048, restores "last write
-- wins = the asset" and heals existing forks in the same pass.
--
-- `curate` is new in 0001 and needs no ordering fix, but a fork that only runs
-- `./sc migrate` never re-executes the already-stamped 0001 — so it lands here
-- too, idempotently, by name.
--
-- Bodies are the verbatim assets/skills/<name>/SKILL.md content; regenerate
-- this file if either asset changes again.
--
-- Plain SQL: migrate.py owns the transaction and the schema_migrations row.

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'curate',
  'The periodic L&S sweep. Run when the STATUS L&S line says "curation due" — resolve contradictions, merge entries stating one rule, promote a recurring process to a skill, move environment facts out, then stamp `sc mem curated`. Yours alone; never delegate it.',
  'substrate',
  NULL,
  1,
  '# curate — the L&S sweep

Write-time triage (`--supersedes` / `--new`) catches contradiction and
restatement pairwise, at the moment of writing. It **cannot** catch the
emergent cluster: five entries can each be a valid distinct rule and only in
aggregate be five instances of one principle. That is this pass''s job, along
with promotion, category drift, and size drift.

**Yours alone.** Law 3 and Law 7 reserve curation to the shell. Never hand this
to a subagent, never let another shell run it for you, never accept a proposed
retirement from anyone else. Read your own set; decide yourself.

Trigger: `## STATUS` says `L&S: … — curation due`. Nothing else fires it.

## Load the set

```
sc mem get lns          # entry ids + bodies — the active set, all of it
```

Read every entry before deciding anything. This is one cheap read; the whole
set is already in your boot doc anyway.

## Pass 1 — Consistency

Find entries that **contradict** each other. One of them is the newer
understanding; the other is superseded and still rendering as live guidance.

```
sc mem lns "<the surviving rule>" --supersedes <old_id>
```

Write-time triage should prevent most of these from ever forming. What you find
here predates the loop or crossed in while two sessions ran.

## Pass 2 — Cluster

Group entries that state **one rule**. Merge each group to a single imperative
rule:

```
sc mem lns "<the one rule>" --supersedes 30,33,34,37,38
```

Three or more members is the bar. Two statements of a rule are often
legitimately two rules — merging at two is usually wrong.

The incidents behind the entries are already in the narrative. They do not need
a second home, and the merged rule must not try to carry them: an entry is the
rule, ≤500 chars, hard-enforced.

## Pass 3 — Promote

A cluster of three or more that keeps **recurring across sessions** is a
*process*, not a lesson. Author it as a skill and keep one L&S rule pointing at
it:

```
sc skill add <you>_<topic> --file <path.md> --desc "<one line>"
```

Local skills are DB-only and namespaced by your shortname — the command
enforces both. This is the pressure valve that makes a hard budget survivable:
knowledge relocates to a lazy surface instead of being deleted.

## Pass 4 — Category

An entry that is an **environment fact** (a routing quirk, a term to avoid, a
path) is not an operating principle. It belongs in a skill, not in L&S. Retire
it and put the fact where it is looked up.

```
sc mem retire <entry_id>
```

## Stamp

```
sc mem curated
```

**Stamp even if you retired nothing.** A clean set is a legitimate outcome; if
an honest sweep left the counter running, the advisory would stand forever and
you would learn to ignore it. The stamp says "I looked," not "I cut."

## Stance

Curate the set toward ~12–14 entries, not toward the cap. Cap 20 is a ceiling
never to reach — if you ever hit it, this sweep is not running.

The trigger firing often does not mean the threshold is wrong; it means entries
are being written faster than they are reconciled. Fix that at write time, with
`--supersedes`.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
  'memory',
  'When + how this shell persists memory — current_state (≤300), session narrative, seed (cap 10), L&S (cap 20, ≤500/entry, --supersedes|--new), decisions — all via sc mem, written as it happens, not at close.',
  'substrate',
  NULL,
  1,
  '# memory — write as you go

All memory = DB rows; no flat files. Write at the moment it matters, never in a
close ritual.

Every write goes through `sc mem` -> lands in the live shared engine DB, visible
to all shells on commit. It always targets your own shell (identity resolved
from your token) — never name a shell.

## current_state — rolling status, NOT a log

Present focus + what''s next. Replace in place; NEVER append. **300 chars, hard
— the write is rejected over it.** Rewrite when focus shifts.
```
sc mem state "…"
```

**Point, do not reproduce.** The overrun is never verbosity, it is restatement:
a decision''s reasoning, a spec''s gate, a flag''s argument all pasted inline when
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
seed bodies). Full command reference + table map: the `db_map` skill.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;
