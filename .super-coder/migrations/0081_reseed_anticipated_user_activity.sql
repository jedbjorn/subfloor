-- 0081 — reseed: docs + spec skills — Anticipated User Activity.
--
-- Specs now carry an "## Anticipated User Activity" section: a soft-vocabulary
-- posture statement (Vocabulary / Expected Activity / Reach / Data Tenancy /
-- Beyond Intention, on a shared role roster incl. System and Shell) that
-- review + Verification test the build against. The docs skill gains the
-- authoring requirement (shape, roster, soft-language map); the spec skill
-- gains the consumption (analyze against it; Verification checks the build
-- against it). Source assets updated in the same commit; this trailing
-- forward reseed (UPSERT by name; skill_id + grants preserved) carries it to
-- installed forks and fresh builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'docs',
  'Author or review docs & specs in super-coder. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.',
  'substrate',
  NULL,
  0,
  '# docs — author & review documents

The DB owns document bodies: a `documents` row is the source — NEVER author a
loose `.md` file as the canonical body. `sc render` writes the read-only flat
copy to `specs_sc/` / `docs_sc/`; the GUI opens it rendered in md-converter.

| kind | lives on | meaning |
|---|---|---|
| `spec` | the **Roadmap** (the dev cycle) | working spec for a feature; a feature can hold several at once; **freezes on ship** |
| `doc` | the **Docs** tab | documentation; not part of the spec lifecycle |

`<self>` = your shell_id.

## One feature, many specs

Feature = the `roadmap` row; exists from `brainstorm` onward, before any spec.
Specs hang off the feature, not off each other: several unfrozen specs per
feature, each a `documents (kind=''spec'')` row, ordered by `seq`. No
feature-to-feature links; no second roadmap row for related work — related
work = another spec under the same feature. Freeze = the ship-time record of
what was built to; it never gates the feature''s other specs.

| state | test | meaning |
|---|---|---|
| **shipped** | `frozen = 1` | delivered; immutable record |
| **active** | unfrozen + has rows in `spec_tasks` | the spec being built now |
| **backlog** | unfrozen, no task plan | the pile, ordered by `seq` |

The **doc** (`kind=''doc''`) = the feature''s readable face — write it when the
first spec ships, under the same `feature_id`. Sibling of the specs, not a
parent.

## Assess the work-stream on every feature

A feature attaches to a work-stream (`projects` row) via `roadmap.project_id`.
The GUI Flow view groups on it; `NULL` shows as Ungrouped = invisible to the
grouping. On every feature create / spec author / spec update, assess the
work-stream in the same act:

```
sc mem get projects   # existing work-streams — pick the fit
sc mem get roadmap    # this feature''s current project_id
```

| case | action |
|---|---|
| new feature | create pre-assigned: `sc mem roadmap add "<title>" --project <shortname>` |
| existing + Ungrouped | `sc mem roadmap project <feature_id> <shortname>` |
| no fitting stream | `sc mem project add <shortname> "<title>" --purpose "…"` -> then assign |
| already correctly assigned | no-op — don''t churn |

Auto-assign when only one plausible fit / it clearly belongs to an existing
stream. Surface to the FnB only when ambiguous — several streams fit, or a
new stream you''re unsure how to name. Exempt (as with stages): work that
isn''t a feature/spec (a quick fix) needs no work-stream.

## Review first

Before writing — don''t duplicate, don''t re-litigate:
```
sc mem get documents      # every spec/doc in the engine DB (kind, seq, frozen, task_count)
sc mem get decisions      # active-decision index (<id> = full row + rationale; --all incl. superseded)
sc map-sql "SELECT path FROM dr_filepath WHERE role=''doc'';"   # repo''s own docs (map db)
```

Spec touches a recorded decision -> honor it, or supersede explicitly: say so
in the spec + record `sc mem decision "…" --parent <old_id>`. NEVER silently
re-decide a settled choice.

## Author

Write through `sc mem doc add` (routes through the engine API): `--body-file`
reads the markdown from a file (no shell-escaping a long body); `--seq`
auto-increments within `(feature, kind)`; it renders + snapshots for you
(pipeline = the `snapshot` skill). The render+snapshot is serialized by one
in-process API lock — sufficient because these artifacts only ever come from
manual admin-shell or GUI actions (single writer by design; cross-process
concurrency is out of scope for v1, decision #20 / roadmap #21).
```
# a doc against a feature (kind=''doc''); DB owns the body:
sc mem doc add "…" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/….md

# a feature''s next spec stage (kind=''spec''); seq auto-advances:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
```

## Specs carry "Anticipated User Activity"

Every spec (`kind=''spec''`) ships an `## Anticipated User Activity` section —
the feature''s posture statement: who is expected to touch it, where it can be
reached, whose data it holds, and what it does not intend to allow. Soft
vocabulary, hard invariants — the nouns stay gentle, every statement stays
checkable from code ("a Valid User only ever sees rows tied to their own
account"), because review + Verification test the build against this section.

Shape (H3s under the section H2):

| H3 | holds |
|---|---|
| `### Vocabulary` | the cast — roles from the shared roster below + any feature-specific ones, each defined in one line |
| `### Expected Activity` | per role: what they do, what they see, what they can change |
| `### Reach` | where the feature meets the world — pages, endpoints, jobs, files it adds or alters, and which roles can arrive at each |
| `### Data Tenancy` | whose data the feature touches; what stays within one account; what, if anything, is deliberately shared |
| `### Beyond Intention` | activity the feature does not intend to accommodate — anything observed here in review is a finding, not a nuance |

Shared roster (always available; same meaning in every spec):

| role | means |
|---|---|
| **Valid Privileged User** | signed-in user with an operator/admin role, acting within what that role allows |
| **Valid User** | signed-in user acting inside their own account and their own data |
| **Visitor** | expected traffic that has not signed in (public/shared surfaces) |
| **Future Potential User** | a role anticipated later, not built now — the design must not wall it out |
| **System** | the product acting on a schedule or trigger — daemons, jobs, watchers |
| **Shell** | an AI agent shell acting through its granted tools — its activity is messages, memory writes, file edits |
| **Unexpected Participant** | anyone acting outside the roles above — where the spec says what must never be reachable |

Language — soft by design. Specs never use: threat model, attack or attack
surface, adversary, exploit, abuse case, vulnerability, breach, privilege
escalation, exfiltration, malicious. Say it in roster words instead: threat
model -> anticipated activity · attacker -> Unexpected Participant · abuse
case -> Beyond Intention · access matrix -> Expected Activity · attack
surface -> Reach · isolation -> tenancy. Describe behavior and boundaries,
never hostility.

Internal-only feature -> the section still ships, one line ("All activity is
by Valid Privileged Users; no tenancy boundary"). Whole section ≤ ~40 lines —
it frames the build, it does not enumerate it.

## Revise before freeze

Unfrozen -> edit in place: no new row, no seq bump. Pass any of `--title` /
`--body-file` / `--render-path`; renders + snapshots like `add`. Frozen ->
refused; open a new spec under the same feature instead:
```
sc mem doc edit <document_id> --body-file ./draft.md
sc mem doc edit <document_id> --title "New title" --render-path specs_sc/….md
```

## Freeze + document on ship — the planner''s handoff

Shipping is a two-shell act (keeps `shipped` honest):

- **dev**: flips `roadmap_status = shipped` + opens a **docs-pending** flag
  (`spec` skill, Step 5) — `shipped` never silently claims a doc that doesn''t
  exist yet.
- **planner**: on that flag (arrives in your inbox per the `flags` skill), do
  the paperwork:

1. **Freeze the shipped spec** — immutable thereafter; the feature''s other
   specs stay unfrozen and unaffected. NEVER edit a frozen spec (open a new
   spec under the same feature); the GUI and render layer both refuse edits
   to frozen docs:
   ```
   sc mem doc freeze <document_id>
   ```
2. **Read the shipped code, then write the doc** — from the code as it
   actually shipped, NOT from the spec body. The spec is intent; the code is
   truth (drift lands during production). Read the implementation first,
   write what it does:
   ```
   sc mem doc add "<feature> — how it works" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/<slug>.md
   ```
3. **Close the docs-pending flag** pointing at the doc:
   ```
   sc mem flag close <flag_id> --notes "Spec frozen; doc <document_id> written → docs_sc/<slug>.md"
   ```

Until step 3, `shipped` + open flag = the truthful interim state: delivered,
doc pending.

## View

GUI "open in md-converter ↗" (Roadmap card / Docs tab) opens any doc rendered
— the body rides in the URL, no upload. Long-form authoring: write the
markdown to `body`; render + md-converter own presentation.

---

# Authoring format (themed-markdown)

The `body` you write IS themed-markdown — the format md-converter renders.
Your job = structure; styling = the renderer''s job. NEVER write visual
instructions (colors, fonts, sizes, themes) — apply the four semantic
classes; the theme picks colors.

Use ONLY the constructs below — anything else drops silently or breaks the
render.

`req` = required · `opt` = optional · `≤N` = soft character cap (over-cap
wraps awkwardly / overflows a fixed UI slot).

## Frontmatter

```
---
title: Document Title
tags: [tag1, tag2]
date: YYYY-MM-DD
project: Project Name
purpose: Brief description
---
```

| Field | Status | Cap |
|---|---|---|
| `title` | req | ≤40 |
| `tags` | req (YAML list; `[]` ok) | — |
| `date` | opt | `YYYY-MM-DD` |
| `project` | opt | ≤40 |
| `purpose` | opt | ≤40 |

`date`/`project`/`purpose` -> footer meta cards. `sc render` injects
`feature`, `roadmap_status`, `frozen`, `rendered_by`, `source` on top —
NEVER write those yourself. Tags = YAML list only; comma-separated
(`tags: a, b`) breaks.

## Structure

| Syntax | Role | Cap |
|---|---|---|
| `# Title` | doc title (opt; falls back to `frontmatter.title`) | — |
| `## Section` | sidebar tab | ≤28 |
| `### Heading` | subsection -> `<h3>` | ≤80 |

H4–H6 ⛔.

**Tab rule:** every H2 = one tab; content between two H2s belongs to the
first. Content between H1 and the first H2 is silently dropped — put intro
under an H2 (e.g. "Overview"). Single-section docs may omit H2s (whole doc =
one tab).

**Doc scale:** ≤25 sections + ≤15 Mermaid diagrams (every section renders
up-front; every Mermaid re-renders per tab switch) — split larger material.

## Inline · lists · tables · images · code

- Inline: `**bold**` · `*italic*` · `~~strike~~` · `` `code` `` · `[text](url)`
- Lists: `-` unordered · `1.` ordered · `- [ ]` / `- [x]` tasks
- Tables: standard GFM pipe tables
- Images: `![alt](https://url/img.png)` — absolute URLs only, descriptive alt
- Video: a bare video URL alone on its own line renders as a player — a
  `github.com/user-attachments/assets/<id>` URL (paste a video into a GitHub
  issue/PR to mint one) or any absolute URL ending `.mp4`/`.webm`/`.mov`/`.ogg`.
  NEVER wrap it in `![]()` / `[]()` — bare triggers the player.
- Code: fenced with a language hint (```` ```python ````)

## Color classes

`class1`–`class4` — on callouts, stat cards, mermaid nodes, linear steps.
Choose the class by meaning; the theme decides the color. Keep one class per
semantic role across the doc (e.g. `class1` = primary, `class2` = supporting,
`class3` = positive/done, `class4` = caution/warning). Consistency >
specific choice.

## Callouts

```
> [!class1]
> Callout content.
```
Cap ≤280 (one short paragraph). class1–class4.

## Stat cards

````
```stats
:::class1
value: 87%
label: User satisfaction
description: Up 12% from last quarter
:::class2
value: 1.2M
label: Active users
```
````

| Field | Status | Cap | Notes |
|---|---|---|---|
| `value` | req | ≤12 | short token (`87%`, `1.2M`) — not sentences |
| `label` | req | ≤28 | one short noun phrase |
| `description` | opt | one short line | omit if no signal |

Layout: 2 per row; trailing odd card spans the row.

## Mermaid

````
```mermaid
graph LR
  A[Start]:::class1 --> B[Middle]:::class2 --> C[End]:::class3
```
````

Class via `:::classN` on nodes. The app injects `classDef` — NEVER write
`classDef`, `fill:`, or any style directive. Node label cap ≤24 (long labels
balloon auto-sized nodes).

**Quote labels with special characters** — unquoted node text is parsed as
Mermaid grammar. Any label containing `/`, `(`, `)`, `*`, `[`, `]`, `{`, `}`,
`<`, `>`, `#`, `:`, `;`, or a quote MUST be double-quoted inside the brackets
-> else *"Syntax error in text"* and nothing renders. Notably `A[/text/]` =
the parallelogram shape, so a literal path like `/lease/mail/*` breaks unless
quoted.

```
GOOD:  AD["/admin/user-credentials/"]:::class3
       N["count > 0"]:::class2
BAD:   AD[/admin/user-credentials/]      (parsed as a parallelogram shape → error)
       N[count > 0]                      (> is a grammar token → error)
```

Cylinder/stadium shapes are fine as-is — `DB[(secrets.db)]`, `X([ready])` —
quote only the inner text, not the shape brackets.

## Linear

````
```linear
Step 1 :::class1 -> Step 2 :::class2 -> Step 3 :::class3
```
````
Steps separated by `->`, optional `:::classN`. Renders vertically — one step
per row, top→bottom (never horizontal). Step text cap ≤48.

## Never

- H4–H6 · blockquotes (except callouts) · footnotes · raw HTML
- Color / font / size / theme / visual mentions (the theme owns styling)
- Content between H1 and the first H2 (silently dropped — use an H2)
- Comma-separated `tags` (must be a YAML list)
- `classDef` / `fill:` / style directives inside Mermaid
- Unquoted Mermaid labels containing special characters

## Open in md-converter

A doc whose `body` lives in the DB already opens in the app from the GUI
("open in md-converter ↗" on the Roadmap/Docs card) — author nothing there.

When committing a **standalone** themed-markdown file to the repo (a README,
or a rendered `docs_sc/` page meant to be read on GitHub), drop a one-click
badge in its preamble — between `# Title` and the first `##` (shows on
GitHub, dropped from the render by the preamble rule):

```markdown
[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/<owner>/<repo>/blob/<branch>/<path>)
```

Fill `<owner>/<repo>/<branch>/<path>` with the file''s GitHub location (any
subdirectory depth). Public repos only — the badge fetches the raw file in
the reader''s browser (no server/auth). Destination unknown -> keep the
placeholders and tell the user to fill them.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'spec',
  'Execute a spec across sessions — analyze viability, surface blockers and unclear items, break into tasks (Preparation → impl steps → Verification), and track progress in spec_tasks. Updates current_state at every step. Load when starting, implementing, or building any feature, spec, or roadmap item — before writing code.',
  'craft',
  NULL,
  0,
  '# spec — analyze and execute a spec

Load at the start of any session that builds or implements a feature, whether
or not the work is framed as a "spec". A spec governs the work -> this skill
executes it; one should exist but doesn''t -> the `docs` skill authors it first.
Run **Analyze** before touching any code. Blockers / unclear items you can''t
resolve alone -> pause for the FnB.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill).
NEVER auto-pick "the latest" — list the feature''s open specs and choose the
target explicitly:

```
# the feature''s documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec''s task plan (empty = no plan yet):
sc mem get tasks --doc <doc_id>
```

`get documents --feature <id>` lists every spec/doc with `kind`, `seq`,
`frozen`, `task_count`. Active spec = the unfrozen one with `task_count > 0`
— resume it. `task_count = 0` = backlog; starting it (Step 3) makes it
active. More than one open spec and the target unclear -> ask the FnB.

Tasks already exist -> skip to **Step 4** (Track).

Read the entire spec body before going further. Do not skim.

---

## Step 2: Analyze

Surface all three before any planning or code:

### Viability
- Session-completable? Bounded + clear entry points = yes. Multiple layers /
  migrations / unknown dependencies = no -> say so + propose a session-sized
  slice.
- No stated done-condition in the spec -> that is the first unclear item.

### Anticipated User Activity
The spec''s `## Anticipated User Activity` section is governing intent: its
roles, reach, and tenancy invariants shape the plan — access and tenancy
checks are planned tasks, not afterthoughts. Older specs predate the section;
absence there is not a blocker.

### Unclear items
Anything you cannot act on without guessing:
- Ambiguous between two interpretations
- Missing a critical detail (which table? which endpoint? which component?)
- Implies knowledge not stated in the spec

List them and ask the FnB before writing the plan.

### Blockers
Hard stops — prior work not shipped, missing environment state, unresolved
external dependency. Open one flag per blocker:

```
sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

NEVER open a flag for an unclear item resolvable by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec = engaging it to build, so the feature''s `roadmap_status`
(loaded in Step 1) must catch up to reality. Stages:
`brainstorm · long_term · near_term · next · in_progress · shipped`.

- At `brainstorm`/`long_term`/`near_term` + building this session ->
  `sc mem roadmap status <feature_id> in_progress`
- Planning ahead only (no build this session) -> move it to `next`.
- Already at `in_progress` (or further) -> no-op; don''t churn it.

The transition fires because you *act on* the spec — reading one for
reference moves nothing. No spec governing the work (quick UI fix, minor
migration) -> skip all stage handling (see Stance).

### Confirm the work-stream too

Check the feature''s work-stream (`roadmap.project_id` — the Flow-view
grouping). Ungrouped -> assign now so the feature shows in a flow:

```
sc mem roadmap project <feature_id> <shortname>   # ''none'' to clear
```

Stream obvious -> assign; ambiguous -> surface to the FnB; already assigned
-> no-op. Full create/assess procedure (new streams, new features) = the
`docs` skill; this is only the engage-time confirmation.

### Write the task plan

Analysis clear + blockers resolved or accepted -> generate the task list.
Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, check the build against the spec''s Anticipated User Activity section, snapshot + render |

Add one task per seq with `sc mem task add` — each write is live in the
shared DB immediately:

```
sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>"
sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, check the build against the spec''s Anticipated User Activity section, snapshot + render"
```

Then set `current_state` — nothing done yet, next = Preparation:

```
sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill''s overlay replaces this step''s one-task-at-a-time loop with
adjudicated waves. Load it and apply it on top of this step.

At each work session''s start, load the plan:

```
sc mem get tasks --doc <doc_id>
```

Find the first `pending` task -> mark it in progress:

```
sc mem task start <task_id>
```

Work ONLY that task. When done:

```
sc mem task done <task_id>
```

A planned task overtaken by a feature split or re-scope (its work moved to
another feature/spec, never built here) is cancelled, not done:

```
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
```

NEVER mark unbuilt work `done` and NEVER leave it `pending` under a shipped
feature — the task ledger is how a planner answers "is this feature actually
finished."

Re-read the plan (`sc mem get tasks --doc <doc_id>`) and resolve from it:
`last_done` = highest-`seq` `done` task; `next_up` = lowest-`seq` `pending`.
Advance `current_state`:

```
sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

`next_up` NULL = all tasks done -> set current_state to reflect that.

---

## Step 5: Hand off on completion

Verification task passes (`next_up` NULL — the existing done-line) = feature
delivered. As the dev: flip the horizon + hand the paperwork to the planner.
Do NOT freeze the spec or write the doc — that''s the planner (`docs` skill).

1. **Flip the horizon to shipped:**
   ```
   sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag + message the planner with full instructions.**
   `shipped` + an open flag = the honest interim state; the message carries
   everything the planner needs without digging:
   ```
   sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   sc mem message send <planner-shortname> "**[Docs pending] <feature_title> (feature <feature_id>)**

   Spec <doc_id> shipped. Flag SC-### is open — your action required:

   1. **Read the shipped code first.** Write the doc from what actually shipped, not from the spec. Drift happens and decisions get made in production — the spec captures the intent, the code is the truth.
   2. Freeze the spec: \`sc mem doc freeze <doc_id>\`
   3. Write the doc (\`kind=''doc''\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec +
   write the doc." The planner closes the flag when the doc lands.

No planner-flavor shell in this fork -> message nobody; surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

Mid-build, the work grows past the spec''s stated what/why:

- **Small growth** (same mental model, a few more tasks) -> the unfrozen spec
  is living; edit it (`sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the
  granularity test in the `docs` skill) -> do NOT quietly absorb it.
  Recommend a **new spec** to the FnB, authored by the planner against its
  own feature. Significant creep = planning event, not dev improvisation.

---

## Stance

- **Analyze before acting.** Analysis finds the gap between what the spec
  says and what the code does.
- **One task at a time.** Start task N+1 only after task N is verified +
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **Anticipated User Activity is intent.** Verification checks the build
  against the spec''s section — a capability beyond its stated roles, or data
  crossing a tenancy line it states, is a finding, not a nuance.
- **Spec too large for one session** -> scope a slice at Preparation: cover
  steps 1–K verifiable now, leave K+1–N pending. NEVER start work that can''t
  be verified before the session ends.
- **current_state always reflects the plan.** Update after every task
  completion — last done + next up. The next session resumes from it without
  reading the full task list first.
- **The stage tracks reality — spec''d work only.** Engaging a spec ->
  `in_progress`; finishing -> `shipped`; already matching -> no-op, don''t
  churn. Work with no spec (quick UI tweaks, minor migrations) is exempt
  entirely: no promotion, no handoff, no creep check. Stage discipline never
  blocks small things.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
