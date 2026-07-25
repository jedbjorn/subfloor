-- 0092 — forward-reseed docs, spec and sprint_orchestration: reconcile the
-- skill deltas reverted by #550. PRs #510 (docs: "Anticipated User Activity"
-- authoring section; spec: the section as governing intent in Analyze +
-- Verification) and #506 (sprint_orchestration: a faulted worker has already
-- consumed its task row — confirm read receipts and re-send before booting)
-- both took migration number 0081, colliding with 0081_planner_wake, and were
-- reverted to restore a reproducible main. The deltas are re-applied here on
-- top of the CURRENT assets (post-0089/0090 sprint-flow corrections), as one
-- combined fresh-numbered reseed. UPSERT by name so skill_id + grants survive.

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

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration',
  'Planner-side governance of a multi-shell sprint — decompose the push, sequence the dependency chain, assign devs and reviewers, run the model & provider interview, declare the sprint doc, arm your inbox watcher, boot workers per task (./sc run), monitor the event stream (result + pr_event rows), unblock stalls, close out — run the pre-freeze conformance pass (review shells judge the spec against main), freeze the doc (revoking all scoped authority), and synthesize the sprint report from unit reports + the conformance doc into the fixed skeleton. Wake ops are provider-neutral: arm the binding before the first wake, monitor `sc sprint status`/`alerts`, retry parks as NEW gated batches (never resubmit), close releases bindings and cancels queued wake work. Zero scheduled polling by any shell. Load when the FnB directs a coordinated multi-dev push. Companion to the participant-side `sprint` skill.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration — governing a coordinated multi-shell push

The FnB declares *that* a sprint happens; you make it run: decompose the
push into units, sequence who builds on whom, assign a reviewer to every
unit, interview the FnB for the sprint''s models, boot each worker when its
turn comes, watch the event stream, unblock stalls, close out with a
report. The participant loop (build → PR + watch → CI → sprint review →
merge on green+clean → hand off, plus the reviewer slot) = the `sprint`
skill — devs and reviewers run it; you run this.

The skills meet at one artifact, the **sprint doc**: your declaration
turns the participants'' scoped authority ON (dev merge-on-green+clean,
reviewer direct handoffs); your close-out turns it OFF.

**The sprint is event-driven — nobody polls on a schedule.** Every
instruction and result is a `shell_messages` row: you send `task` rows and
boot workers headless; workers send `result` rows and register their PRs
with the watcher daemon, which sends you `pr_event` rows. Your inbox
watcher wakes you the moment any row lands. Workers are ephemeral,
per-task sessions; you are the one long-lived context in the loop — you
manage, you never load code. The full trail replays with
`SELECT * FROM shell_messages WHERE kind != ''shell'' ORDER BY created_at`.

## Step 1: Declare the sprint

Decompose the push into units a single shell can own end-to-end. Map
dependency order stingily: a dependency edge = a real code dependency, not
a preference. Keep chains short and the graph wide where the code allows.

**Then check MERGE SURFACE, which is a different question from dependency.**
Predict each unit''s file set and compute the pairwise intersection. Logical
independence does NOT imply merge independence: units that need none of each
other''s code still collide if they edit the same files, and that collision
lands at merge time, after every review is done.

- Empty intersection → genuinely parallel; say so.
- Non-empty → either sequence them, or declare them parallel **with the merge
  protocol and the overlap map attached at kickoff** so reviewers know from the
  start that their verdicts are SHA-bound.
- A file touched by **three or more** units → reconsider the cut, don''t just
  manage the merges.

Record overlap in the board''s `depends on` column. A bare dash means only "no
logical dependency" and is read as "independent" — which is how one sprint
declared five units independent while 21 of their 30 file-touches landed on
nine shared files, three of them touched by three units apiece. The cost was a
merge protocol invented mid-flight, four rebases, two voided verdicts and a
hand-resolved conflict. Surfaces that concentrate a lot of behaviour into a few
large files make this the normal case, not the exception.

Assign each unit a dev shell + a reviewer shell (one reviewer may gate
several units — don''t let one reviewer become the whole sprint''s
bottleneck).

**How many shells to deploy = your call, not a formula.** Weigh the
magnitude of the push against the capacity actually available — the shells
that exist, reviewer bandwidth, how wide the dependency graph genuinely
runs — and make the call. More units than shells is fine (units queue
behind the chain); more shells than parallel work is waste.

**The model & provider interview — two routine routing questions to the FnB:**

1. **Devs** — which harness and model? One answer; every dev in the
   sprint runs it.
2. **Reviewers** — which harness and model? One answer; every reviewer
   runs it.

**Billing gate — Plan billing by default; observe, never mutate auth.** NEVER
unset, scrub, replace, or print a credential. Before resolving models, classify
the chosen harness exactly:

```sh
# OpenAI / Codex: exit 0 = plan; 10 = API override; 11 = persisted auth unknown.
(
  if [ -n "${CODEX_API_KEY+x}" ]; then
    echo "billing=api source=CODEX_API_KEY"; exit 10
  fi
  status="$(codex login status 2>&1)"
  if [ "$status" = "Logged in using ChatGPT" ]; then
    echo "billing=plan source=ChatGPT"; exit 0
  fi
  echo "billing=api-or-unknown source=persisted-login"; exit 11
)

# Anthropic / Claude: exit 0 = plan; 10 = API key; 11 = unknown auth.
claude auth status --json 2>/dev/null |
  python3 -c ''import json,sys
try: s=json.load(sys.stdin)
except Exception: print("billing=unknown"); raise SystemExit(11)
key=s.get("apiKeySource"); plan=s.get("loggedIn") and s.get("authMethod") == "claude.ai" and s.get("apiProvider") == "firstParty" and s.get("subscriptionType") and not key
print("billing=plan source=claude.ai" if plan else ("billing=api source=" + str(key) if key else "billing=unknown")); raise SystemExit(0 if plan else (10 if key else 11))''
```

Exit 0 + `billing=plan` -> launch normally. Exit 10 -> hold and ask the FnB to
authorize the metered route. Exit 11 -> hold until the FnB corrects the login or
explicitly authorizes the unknown route. Model/harness selection is not billing
permission.

Ask in the planner turn, then stop before booting the worker:

```
Billing approval required: provider=<openai|anthropic> mode=<api|extra-usage> route=<harness/model> scope=<shell/unit/role/sprint> cap=<amount|provider limit|not specified> expires=<one launch|time|sprint close>. Authorize this metered run?
```

Only an explicit affirmative FnB reply counts. Silence, prior model selection,
or an approval for another provider/scope does not. Default scope = one launch;
broader authority must be stated explicitly.

Record an approval before launching:

```
billing-exception: provider=<openai|anthropic> mode=<api|extra-usage> scope=<role, unit, or whole sprint> cap=<amount or provider limit> expires=<time or sprint close> approved-by=FnB
```

After approval, run the ordinary resolved `./sc run ...` command with the
current environment unchanged; this preserves the credential the FnB approved.
No matching, unexpired approval -> do not launch the metered route.

CLI auth cannot see account-side overage controls. Do not claim Extra Usage was
validated. If the provider reports an included-plan limit or offers paid
continuation, hold and request the same scoped approval. Automatic overage is an
FnB-owned account policy: treat it as permission only when the sprint doc records
its scope/cap/expiry; otherwise the FnB keeps it disabled for plan-only sprints.

`sc models resolve` proves callability, not billing; run it only after this gate.

Flavor-uniform by design: shells of a flavor are interchangeable workers,
and one answer per flavor keeps the board readable and the review lineage
coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). Every sprint worker still runs at high
effort. Per-unit model mixing is out of scope — the interview covers the real
need, provider choice per role.

**Resolve each answered route before declaring it.** Lazy-load only the two
choices the FnB made — never trust a display name or translate a provider id by
hand:

```
sc models resolve <devs-harness> <devs-model>
sc models resolve <reviewers-harness> <reviewers-model>
```

Each must return `route:` plus an exact `call:` ending in `--effort high`.
Failure means the selector is not locally callable, the harness lacks a
headless/high-effort seam, or Refresh models has not seen it. Run
`sc models list <harness>` for the local choices; the FnB''s **Refresh models**
button in `/#shells` repopulates the same runtime table. Resolve again after a
refresh. Never silently fall back across a provider or lineage.

Common exact selectors: Claude aliases (`fable`, `opus`) and Codex ids
(`gpt-5.6-sol`, `gpt-5.6-terra`) pass directly. Kimi takes the configured alias
shown by `sc models list kimi` (for example `kimi-code/k3`), never the bare
provider model `k3`.

Write the board as a `documents` row:

```
sc mem doc add "SPRINT: <title>" --kind doc --body-file <draft.md>
```

Body contract (the `sprint` skill quotes the same one — keep it exact):

```
# SPRINT: <title>
status: ACTIVE                      # ACTIVE | CLOSED
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>

| seq | unit | shell | reviewer | depends on | branch | pr | status |
```

`depends on` carries BOTH facts: the logical dependency and any file overlap
(`— · shares app.js with 3`). A dash alone asserts independence you may not
have checked.

Unit `status` walks `waiting → building → pr-open → in-review → fixing →
merged`; `fixing` loops back to `in-review` until clean; `ci-red` can
interleave anywhere from `pr-open` on.

Note the returned `document_id` — every task and report references it —
and embed `SPRINT doc=<id> governing` in your own `current_state`; drop
it at close-out.

You are the doc''s only writer: devs report transitions as `result` rows;
fold them into the board with `sc mem doc edit <id> --body-file`.

**Verify every board edit.** A scripted edit whose pattern has drifted silently
matches nothing and reports success. Assert the target text exists before
replacing, then read the doc back and confirm the fields actually changed. One
sprint reported unit statuses to the FnB for four turns off a board where three
edits had no-op''d — a merged unit still read `building` and a whole row was
missing — until a REVIEWER noticed the board contradicted the SHA in its own task
row. You cannot report from memory and call it the board.

## Step 2: Arm the watcher, kick off

**Arm your inbox watcher first** — the zero-token wake-up that replaces
every scheduled tracker. On the claude harness, run it as a background
task (it blocks until any message row lands for you, then exits — the
exit is your wake-up):

```
./sc watch inbox        # background it via your harness''s background-task tool
```

**Interactive sessions only.** A harness background task is
session-scoped: in a headless (`-p`) boot it dies with the session,
silently — six sprint stalls traced to exactly this. A headless planner
turn arms nothing: drain the inbox, act, end the turn — the next event
row boots you again. The watcher belongs to the long-lived interactive
planner seat, nowhere else.

Re-arm it every time you finish draining your inbox. On other harnesses
the watcher isn''t available — check your inbox at every task boundary
instead; correctness is identical, latency degrades gracefully. (Strong
recommendation, not a gate: the planner seat runs best on claude/Fable —
the one long-lived, low-volume, high-leverage context in the loop, and
the only seat the watcher fully serves.)

**Kick off** — a `task` row per participant (doc id + the instruction to
load the `sprint` skill + the slot), then boot whoever can start:

```
# devs — unit, dependencies, reviewer:
sc mem message send <dev> "SPRINT <doc-id>: you own unit <seq> — <one line>. Depends on unit <k> (<shell>); <shell''> depends on you; <reviewer> reviews you. Load the sprint skill and take your slot; your merge closes with the unit report. First move: <start now | build locally, wait for unit <k>>." --kind task

# reviewers — assigned units, the severity bar:
sc mem message send <reviewer> "SPRINT <doc-id>: you review units <seq,seq> — Major/Medium block, Low goes to the report. Load the sprint skill (reviewer slot). Review requests come to you directly as units go green." --kind task

# boot each first-in-chain dev with the RESOLVED selector; high is invariant:
./sc run <dev> --harness <devs-harness> -m <devs-model> --effort high
```

`./sc run` renders the shell''s boot doc and drains its inbox
non-interactively — the `task` row you just sent is what it acts on. The
default prompt is exactly that ("check your inbox and act"); pass
`-p` only to say something the task row doesn''t. A shell with a live
session refuses to boot (one shell, one session) — a live session reads
the same `task` row at its next inbox check.

Keep `task` bodies model-neutral and constraint-explicit: point at the
sprint doc, the unit, the spec, and the skill — don''t restate them in
your own phrasing. Constraints live in specs, which every lineage reads
the same way.

This kickoff activates each dev''s scoped merge authority and each
reviewer''s direct-handoff authority for its assigned units.

## Step 3: Monitor the event stream

Your watcher wakes you on every row. On wake, drain the inbox and act:

- **`result` rows** (dev/reviewer transitions — pr-open, in-review,
  review-clean, merged, ambiguity calls, stall reports): fold into the
  board, then move whatever it unblocks. A dev''s merge arrives as its
  **unit report** (the one multi-line `result` row — shipped /
  judgements / issues / deviations / follow-ups): file it whole; it is
  a primary source for the sprint report, and its `deviations` +
  `judgements` lines feed the conformance kickoff. A bare one-line
  `merged` with no report -> nudge the dev (`task` row) for it now,
  while the unit is still in its context.
- **`pr_event` rows** (daemon ground truth — checks green/red, review
  submitted, merged, closed): the wake-up for transitions no worker is
  live to report. Green on an in-review unit -> nothing (the reviewer
  gate holds); red -> re-task the unit''s dev (`task` row + `./sc run`);
  merged -> boot the downstream dev whose turn it is.
- Mark rows read as you fold them; then **re-arm the watcher**.

A worker self-report is never the verdict — green checks + the reviewer
gate are the only ground truth; the `pr_event` stream is what makes a
"done" checkable without a context switch. `gh pr checks <n>` /
`gh pr list` remain your on-demand detail reads — detail lives in `gh`,
the message is the wake-up.

At any moment, be able to answer: which link is the bottleneck? The board
is what the FnB and any rebooted shell reads to re-orient mid-sprint —
fold every state change in as it happens. The board + message table ARE
the sprint''s state: a rebooted planner replays the rows and loses
nothing.

Messages are your steering wheel: a headless boot drains the inbox first
thing, and a dev checks it at each step start. Steer with `task` rows — holds,
re-sequencing, nudges, rulings on reported reds. The board records state;
messages change behavior; on conflict your latest message wins -> then
update the board to match.

**A message to a LIVE worker probably will not land before its next push.** A
long build has few step starts, so a ruling issued mid-build routinely arrives
after the work it was meant to change. Staleness runs BOTH ways: the worker is
also reporting against a snapshot of you that has moved, so it may tell you that
you don''t know something you ruled on half an hour ago.

Phrase instructions to live workers **idempotently** — "if you have not already
X, do X" — and state the **observed facts** they rest on ("main is at X", "the
intersection is empty"), not only the conclusion. A crossed message is then a
no-op or a confirmation rather than an order against reality, and a worker whose
state has moved can re-derive the right action. Three crossed messages in one
sprint cost nothing worse than a CI cycle for exactly that reason — the devs
reasoned from the facts. A fourth, phrased as a bare directive, would have
destroyed a record had it been obeyed literally.

**Never delegate a mutation by an identifier the tool does not take.** Give the
tool''s identifier and the human label together — "close flag_id 141 (SC-144)" —
and prefer mutating your own records yourself. Display names and row ids sit in
different counters that can overlap in range, so a name-only instruction can
resolve to a different real row and destroy it while reporting success.

Dev ambiguity reports (`ambiguity: … → chose …`) get a ruling on
receipt: overrule by `task` row while the unit is still un-merged, or
stay silent and the call stands. Either way log the call + outcome the
moment it arrives — the sprint report lists every one, and calls
reconstructed at close-out from old messages are calls lost.

## Wake operations (Interface-backed planner wake)

Provider-neutral operator workflow for the wake machinery — identical on
every harness (claude / codex / kimi); there are no provider-specific
steps. The operator surfaces are `sc sprint status` / `alerts` / `retry`
and the Interface tab''s Sprint wake panel; both read the same API
projection. None of it is scheduled polling — they are on-demand reads of
durable state, and the events still wake you.

- **Arm before the sprint''s first wake.** Once your Interface chat is
  live, start one arm attempt by generating an attempt nonce once:

  ```sh
  arm_attempt_id="$(python3 -c ''import secrets; print(secrets.token_hex(16))'')"
  ```

  Retain that value until the attempt ends, then arm the binding with the
  required idempotency header:

  ```http
  POST /api/interface/sprint-bindings
  Idempotency-Key: sprint-bind-<sprint-doc-id>-<planner-shell-id>-<arm-attempt-id>

  {"sprint_doc_id": <sprint-doc-id>, "planner_shell_id": <planner-shell-id>}
  ```

  Reuse that exact caller-stable key only for retries of this arm attempt,
  including after an ambiguous transport failure. A successful release or a
  conclusive refusal ends the attempt. Generate a new `arm_attempt_id` for
  every later arm or re-arm; reusing a released attempt''s key would replay its
  released binding and leave the sprint unarmed. Never generate a timestamp or
  random value separately for each transport retry. A shell may arm only
  itself; the operator may arm any planner. Arming is fail-closed: a frozen or
  non-ACTIVE doc, a mandatory-hook gap, or a second ACTIVE binding is refused.
  PR watches registered with `--sprint <doc-id>` ride the binding — an unarmed
  binding means `pr_event` rows arrive but nothing wakes you.
- **Monitor wake status.** `./sc sprint status` shows binding
  armed/released, the sprint doc ACTIVE/frozen, the derived wake state
  (armed/queued/submitting/running/parked), the current batch, the last
  wake outcome, and the park/quarantine reason. The Interface tab''s
  Sprint wake panel on your session shows the same projection.
- **Read the alerts.** `./sc sprint alerts` (+ the Interface alert
  panel) is the ONLY window into wake failures — session-loss,
  delivery_unknown parks, pre-send retries exhausted, quarantine,
  unmanaged-writer. Alerts are deduplicated while open; an open critical
  alert means the loop is NOT healthy no matter how quiet the inbox
  looks. Investigate the alert before concluding a stall is a shell''s
  fault.
- **Retry a park — never resubmit it.** A parked (`delivery_unknown`)
  batch is never sent again: the parking invariant is law.
  `./sc sprint retry --binding <id>` closes the park as audit, returns
  its items to queued, and the coordinator forms a NEW batch that
  re-gates everything (idle, clean composer, quiet, hooks healthy,
  sprint ACTIVE) before a byte moves. When the input frame itself is
  parked, retry needs your verdict on what reached the pane:
  `--outcome delivered|not_delivered`. The Interface panel offers the
  same action (Retry wake / Retry — input landed / Retry — input lost).
- **Quarantine is yours to drain by hand.** An item that survives three
  completed wake turns quarantines and alerts without blocking newer
  work — read that message yourself and act on it; the wake machinery
  deliberately leaves it alone.
- **Close cleanly.** Setting `status: CLOSED` on the board (and the
  freeze after it) releases the binding and cancels queued wake work in
  the same transaction — no orphan armed binding, no stranded queued
  batch survives the close. Messages stay unread; the Interface chat is
  untouched. Verify with `./sc sprint status --all`: every binding of
  the sprint shows released.

## Merge protocol — declare it at kickoff, not at the first collision

Needed whenever any two units share a file. Declare it in the kickoff `task`
rows so reviewers know their verdicts are SHA-bound before they spend a pass.

1. Before merging, check whether anything merged since the head was cut touches
   THIS unit''s files. Overlapping -> rebase onto current `origin/main`, confirm
   checks green on the **rebased** head, report that SHA. Empty intersection ->
   the head stands; say so with the evidence and merge. Do not rebase reflexively:
   with disjoint file sets a rebase is ceremony that costs a CI cycle and buys
   nothing, and it invites a fresh review for no reason.
2. After any unit merges, every remaining unit re-applies step 1 — which for a
   disjoint unit means re-checking the intersection, not necessarily re-rebasing.
3. Merge order is review-clean order unless you state otherwise.

**A reviewer verdict is bound to the exact SHA it was given.** The carry-over
rule: a verdict carries when the unit''s **own contribution is diff-identical**
and its hunks are **disjoint** from the incoming content — NOT when the reviewed
files are byte-identical. File-identity conflates "did my reviewed change
survive?" (answered exactly by diff-identity) with "did anything else touch this
file?" (irrelevant), and demanding it forces a full re-review every time two
units touch one file for unrelated reasons.

When either condition fails, re-confirmation is required — because disjointness
is a semantic claim, not a proof — but it is **narrowed to the interference
question**, and the dev supplies the evidence that scopes it. A dev that
hand-resolves a hunk names the line and leaves the mutation round trips to the
reviewer: a hand-resolved hunk is precisely what can silently unpin a test.

Never let a unit merge on a verdict attached to a superseded SHA. Two sprints
have lost cycles to it; in the second, reviewers caught it twice and the planner
missed it both times.

## Step 4: Unblock

Stalls and the moves:

- **Dev wedged on red CI** (it reports after three failed fix attempts,
  per the `sprint` skill): pair another shell onto it / re-scope the
  unit / pull the failing part into a follow-up unit so the chain moves.
- **Anomalous red** (flaky test, runner death, `main` red underneath — the
  dev''s job was to rerun and report, not patch healthy code): fix the
  cause as its own unit, or hold the chain while infra recovers; rule by
  `task` row when the dev may proceed. Don''t count phantom reds against
  the dev''s fix attempts — and don''t let anyone merge over one; green
  means green.
- **Unit growing past scope**: split it — the piece downstream needs ships
  first; the rest becomes a new unit at the chain''s tail.
- **Merge broke `main`**: `task` row to all devs to hold merges, insert a
  fix unit at the front of the chain, resume when green.
- **Review stall** (unit sitting `in-review` while its reviewer is idle):
  boot the reviewer — `./sc run <reviewer> --harness <reviewers-harness>
  -m <reviewers-model> --effort high`; its inbox holds the review request. Still stuck
  -> reassign the unit to another reviewer. Severity dispute (dev says
  Low, reviewer says Medium) -> rule by message immediately — a chain
  waiting on a classification argument is pure loss. Dispute about what
  the unit *should do* -> FnB.
- **Worker faulted mid-task** (rate-limit cutoff, provider error, session
  died): its `task` row is already consumed — a worker marks the row read
  when it starts acting, so a fault leaves a read row and an unfinished
  unit. Re-launching alone drains an empty inbox and the worker idles on
  the default prompt. **Confirm the row''s state at runtime before you
  boot** — `sc mem message sent` carries read receipts; a task row showing
  read means re-send it (same unit, plus where the work stopped and what
  is already on the branch), *then* `./sc run`. A re-boot is not a
  re-task.
- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in
  an event-driven sprint. Check `sent` first, though — a read task row
  means the link faulted rather than stalled, and the boot has nothing to
  act on. The liveness guard refusing (session already
  live) + still silent -> escalate to the FnB with the worktree state.
  The bottleneck question in Step 3 is what surfaces a dead link.
- **Re-sequencing**: edit the board + `task` row to *every* affected dev
  with its new slot — a dev acting on a stale slot is worse than a paused
  one.
- **Every worker boot failing at once**: check provider auth and spend
  limits BEFORE debugging the engine — a monthly cap presents as a
  fleet-wide boot failure and costs an hour of misdiagnosis. Pause at a
  clean gate (units green, nothing mid-merge), surface to the FnB (auth
  switch is theirs), resume where the board says you stopped.
- **CI queue clogged at the tail**: a queued verify whose commit a later
  stack head already supersedes is pure queue time — cancel it (`gh run
  cancel`) and let the head''s run stand for the stack. Cancelling
  anything to protect a measurement run is allowed but logged: rationale
  in the board or a `result` row, and re-run the cancelled check after.
  Green means green — cancellation never substitutes for a verdict on
  what still needs one.
- **Judgment calls** (scope vs. deadline, cutting a unit, changing an
  interface another team reads): escalate to the FnB immediately — the one
  stall you can''t unblock yourself.

You boot workers; the daemon never does (it only writes rows), and the
FnB is only pulled in for judgment. Autonomous wake stays a deliberate
non-goal.

## Step 5: Close out

When every unit is `merged` and `main` is green:

1. **Run the conformance pass — before the freeze.** "All units merged"
   and "the spec shipped" are different claims; this is where the second
   one gets checked. Boot review shell(s) — reviewer lineage, the
   sprint''s reviewer harness/model; one shell by default, shard by spec
   section only when the spec genuinely exceeds one context:

   ```
   sc mem message send <reviewer> "SPRINT <doc-id>: conformance pass — spec doc <spec-id>, main @ <merge-sha><, sections <scope> if sharded>. Ratified judgement calls: <list — the only narrative input>. Load the sprint skill (conformance slot)." --kind task
   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model> --effort high
   ```

   The shell judges the spec against the code on `main` — never the
   diffs, never the trail — and files four-way verdicts (`as-specced` /
   `deviated-intentionally` / `deviated-silently` / `unimplemented`) as
   a `CONFORMANCE: <title>` doc + a one-line `result` pointer.

   **Declare the SCOPE before you boot the pass, and name which units it does
   NOT cover.** A pass judges a spec, so it certifies only the units built to
   that spec. Decision-driven units — no spec doc, built from a decision or a
   flag — cannot be judged by it, and a verdict that appears to bless them is a
   false certification. Their bar is their unit reports, their reviewer verdicts
   at exact heads, and the mutation round trips. Put the split in the report''s
   Verdict so freezing cannot be read as certifying everything. Assign the pass
   to a reviewer that did NOT review the unit being certified, and hand over any
   DECLARED deviation up front so the pass judges whether the declaration is
   honest rather than discovering it as a gap.

   **Rule on the findings** — they route like any sprint event:
   - **Major** -> insert a fix unit at the front of the chain under
     still-ACTIVE authority (this is exactly why the pass runs before
     the freeze — a reopened sprint re-grants nothing); re-run the pass
     scoped to the fix when it merges.
   - **Medium** -> your judgment: fix unit now, or defer with the FnB
     told explicitly in the report''s Verdict.
   - **Low** -> Deferred & Follow-ups; never holds the close.
2. Set `status: CLOSED` in the body, then freeze:
   `sc mem doc freeze <doc-id>`. Freezing IS the revocation — a frozen or
   `CLOSED` sprint doc is exactly what the `sprint` skill checks before
   any merge; every participant''s scoped authority ends with it.
3. Message every participant (`task` row): sprint closed, default merge
   gates resume.
4. Verify the watches are gone: `./sc watch list` — every sprint PR''s
   watch retired itself at merge/close; a survivor means an unmerged PR
   or a mis-registered watch — resolve it, don''t leave it. Then stop
   re-arming your inbox watcher (a running one just times out — it holds
   no authority and wakes nothing that matters).
5. Write the sprint report — one `documents` row, the durable record:

   ```
   sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
   ```

   Fixed skeleton — fill it by **reasoning over the unit reports and the
   conformance doc against each other** (a dev''s `deviations: none`
   meeting a `deviated-silently` finding on its unit is exactly what the
   report exists to say), not by pasting either verbatim:

   | Section | Primary source |
   |---|---|
   | `## Verdict` | your synthesis — five-second answer: N units / N PRs, conformance state (conforms / conforms-with-deviations / gaps-found), main green, anything deferred-with-eyes-open |
   | `## Units Shipped` | the board — final table, planned vs. actual order |
   | `## Judgements Made` | unit reports (`judgements:`) + your rulings + severity disputes; every call with its final state |
   | `## Spec Accuracy` | conformance doc — verdict table + findings, cross-checked against unit reports'' `deviations:` |
   | `## Issues Encountered` | unit reports (`issues:`) + the `pr_event`/stall trail — CI fights, anomalous reds, re-scopes, unblocks |
   | `## Deferred & Follow-ups` | unit reports (`follow-ups:`) + reviewers'' Lows + conformance Lows + anything cut — one actionable backlog, the next sprint''s seed list |
   | `## Spec Debt` | judgement calls that should be written back into the spec + places the spec was silent, wrong, or contradictory — the input to the spec-update pass |
   | `## Metrics` (optional) | mechanical from the trail: review cycles per unit, CI reds, boots per shell, planned vs. actual merge order |

   The `kind != ''shell''` message trail remains the in-order backbone;
   the CONFORMANCE doc stays alongside as the report''s evidence trail.

   Then drop a copy at the repo root: write the same body to
   `shared/SPRINT_REPORT_<slug>.md` (`mkdir -p shared` — the dir may
   not exist yet). Message the FnB: sprint closed, report at doc
   `<id>` + the `shared/` file.
6. Settle the bookkeeping — close the sprint''s flags, advance roadmap /
   feature status, note docs-pending.

## Stance

- Enforcement is advisory in v1 — merge order and authority live in skill
  text and the board, not a pre-commit check. An out-of-date board = a
  false authority grant; board accuracy is your discipline.
- Zero scheduled polling by any shell: rows wake you, you boot workers,
  watches retire themselves. A scheduled tracker anywhere in the sprint
  is a defect.
- Local long work rides `./sc job` (see the `sprint` skill) — a job''s
  completion is a `result` row like any other wake-up. A hand-rolled
  nohup/poll waiter anywhere in the sprint is a defect: one sprint''s
  hand-rolled waiter carried a self-match bug that masked a dead bench.
- You manage; you never load code. Your context grows at coordination
  density — the workers'' grows at code density and is discarded per task.
- Monitor > interrogate: `pr_event` rows and `gh` reads cost no dev a
  context switch; `task` rows are for changing behavior.
- The conformance shell files verdicts, never rulings — Major/Medium/Low
  routing stays yours; what the sprint *means* stays the FnB''s.
- Escalate judgment, absorb mechanics: re-sequencing and worker boots are
  yours; changing what the sprint *means* is the FnB''s.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
