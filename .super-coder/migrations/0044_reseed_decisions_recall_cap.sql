-- 0044 — decisions recall: index/library split + boot-doc read-side line (#274)
--
-- Follow-up to 0043/#267: the read-trigger pulled the WHOLE decision log —
-- every row, full rationale, no limit — every planning session, and the log
-- grows unbounded (dos-arch: 91 rows ≈ 17k tokens/recall). The API/CLI now
-- default to an index (active rows only, no rationale, newest-first, capped
-- with an explicit footer); `sc mem get decisions <id>` is the library half
-- (full row + rationale); `--all` is the full log incl. superseded.
--
-- This reseed carries the fork-side halves:
--   1. docs + blueprint skill bodies — the read-trigger hint now teaches the
--      index-default + <id>-detail form (UPSERT by name; grants preserved).
--   2. shell system prompts — the always-loaded boot doc gains a read-side
--      "Read before you decide" paragraph (write-side was nudged in three
--      places; read-side had none outside the two skills), and the Decisions
--      table row's confusing "never supersede, add a new one" is reworded.
--      Spliced into existing shells' prompts, 0037-style; the template covers
--      shells created from here on. Guarded idempotent.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'blueprint',
  'Turn a one-line objective into a sequenced construction plan — decompose into steps, find the dependency order, mark what can run in parallel, name the verification gate. Use before multi-step builds.',
  'craft',
  NULL,
  0,
  '# blueprint — objective → sequenced plan

Catalogue skill (opt-in). Use before a build that spans more than a couple of
steps, so the work has a shape before you start cutting.

## Produce

1. **Restate the objective** in one sentence + the done-condition (how you''ll
   know it''s finished).
2. **Re-surface prior decisions** — `sc mem get decisions`: has any part of
   this already been settled? (Index of active decisions; `sc mem get
   decisions <id>` pulls one with its rationale.) A recorded decision
   constrains the plan; honor it, or supersede it explicitly
   (`sc mem decision "…" --parent <old_id>`) — never silently re-litigate.
3. **Decompose** into concrete steps — each a unit you could verify on its own.
4. **Order by dependency** — what must precede what. Mark steps with no
   dependency on each other as **parallelizable**.
5. **Per step**: the change, the files/areas it touches (use `surface_catalogue`
   to ground this in the real repo), and its **verification** (test, run,
   review).
6. **Risks / unknowns** — what could break the plan; resolve the riskiest
   unknown first (spike it) rather than last.
7. **Gate** — the adversarial check before calling it done: does each step''s
   verification actually prove the done-condition?

## Stance
- Plan to the **next solid checkpoint**, not the whole universe — re-plan as
  reality lands. A plan that survives contact is short and concrete.
- Sequence so something **works end-to-end early** (a thin slice), then deepen —
  beats building all the pieces and integrating last.
- In super-coder, land the plan as a **spec** on the roadmap (the `docs` skill):
  a feature row + a `spec` document, so the plan is reviewable and freezes on
  ship.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'docs',
  'Author or review docs & specs in super-coder. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.',
  'substrate',
  NULL,
  0,
  '# docs — author & review documents

In super-coder the **DB owns document bodies** — never loose `.md` files. A
`documents` row is the source; `sc render` writes the read-only flat copy to
`specs_sc/` / `docs_sc/`, and the GUI opens it rendered in md-converter.

| kind | lives on | meaning |
|---|---|---|
| `spec` | the **Roadmap** (the dev cycle) | a working spec for a feature; a feature can hold several at once; **freezes on ship** |
| `doc` | the **Docs** tab | documentation; not part of the spec lifecycle |

`<self>` = your shell_id.

## One feature, many specs

A feature (the `roadmap` row) is the umbrella, and it exists from `brainstorm`
onward — before any spec is written. **Specs hang off the feature, not off each
other:** a feature can hold several unfrozen specs at once (the working pile),
each a `documents (kind=''spec'')` row, ordered by `seq`. There are no
feature-to-feature links and no second roadmap row for related work — related
work is just another spec under the same feature.

A spec stays unfrozen until it ships; freeze is the ship-time record of what we
built to, and it never gates the feature''s other specs. So at any moment a
feature''s specs are in one of three states:

| state | how to tell | meaning |
|---|---|---|
| **shipped** | `frozen = 1` | delivered; immutable record |
| **active** | unfrozen **and** has rows in `spec_tasks` | the spec being built now |
| **backlog** | unfrozen, no task plan yet | the pile, ordered by `seq` |

The **doc** (`kind=''doc''`) is the feature''s readable face — write it when the
first spec ships, under the same `feature_id`. It is a sibling of the specs, not
a parent they point at.

## Every feature belongs to a work-stream — assess it

A feature attaches to a **work-stream** (a `projects` row) via
`roadmap.project_id`; the GUI **Flow view groups on it**, and `NULL` shows as
**Ungrouped**. An unassigned feature is invisible to the Flow view''s grouping —
so a roadmap of Ungrouped features is a roadmap with no flows. **Whenever you
create a feature or author/update a spec, assess the work-stream** as part of the
same act (it''s a planning decision, like the stage):

```
# existing work-streams (pick the one this feature belongs to):
sc mem get projects
# is this feature already assigned? — read its row''s project_id:
sc mem get roadmap
```

Then:
- **New feature** → create it already assigned:
  `sc mem roadmap add "<title>" --project <shortname>`
- **Existing + Ungrouped** → assign it:
  `sc mem roadmap project <feature_id> <shortname>`
- **No fitting work-stream exists yet** → create one, then assign:
  `sc mem project add <shortname> "<title>" --purpose "…"`
- **Already correctly assigned** → **no-op**; don''t churn it.

**Auto-assign when the stream is obvious** (only one plausible fit, or it clearly
belongs to an existing stream). **Surface to the FnB only when ambiguous** —
several streams could fit, or the feature implies a new stream you''re unsure how
to name. Exempt, as with stages: work that isn''t a feature/spec (a quick fix)
needs no work-stream.

## Review first

Before writing, see what exists — don''t duplicate, and don''t re-litigate:
```
sc mem get documents      # every spec/doc in the engine DB (kind, seq, frozen, task_count)
sc mem get decisions      # active-decision index — already settled? (<id> = full row + rationale; --all incl. superseded)
sc map-sql "SELECT path FROM dr_filepath WHERE role=''doc'';"  -- repo''s own docs (map db)
```

If the spec you''re about to write touches a recorded decision, honor it or
supersede it **explicitly** — say so in the spec, and record the new decision
with `sc mem decision "…" --parent <old_id>`. Never silently re-decide a
settled choice.

## Author

Write through `sc mem doc add` — it routes through the engine API, `--body-file` reads
the markdown from a file (no shell-escaping a long body), `--seq` auto-increments
within `(feature, kind)`, and it renders + snapshots for you (the render/snapshot
pipeline this rides on is the `snapshot` skill):
```
# a doc against a feature (kind=''doc''); DB owns the body:
sc mem doc add "…" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/….md

# a feature''s next spec stage (kind=''spec''); seq auto-advances:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
```

## Revise before freeze

While a doc is still unfrozen, revise it in place — no new row, no seq bump.
Pass any of `--title` / `--body-file` / `--render-path`; it renders + snapshots
like `add`. Refused once frozen (open a new spec instead — see below):
```
sc mem doc edit <document_id> --body-file ./draft.md
sc mem doc edit <document_id> --title "New title" --render-path specs_sc/….md
```

## Freeze + document on ship — the planner''s handoff

Shipping a feature is a **two-shell** act, and the split keeps `shipped` honest:

- the **dev** flips the feature to `roadmap_status = shipped` and opens a
  **docs-pending** flag (see the `spec` skill, Step 5) — so `shipped` never
  silently claims a doc that doesn''t exist yet;
- the **planner** picks up that flag and does the paperwork: **freeze the spec,
  write the doc, close the flag.**

As the planner, on a docs-pending flag (it arrived in your inbox per the `flags`
skill):

1. **Freeze the shipped spec** — records what we built to, immutable thereafter.
   The feature''s other specs stay unfrozen and unaffected; never edit a frozen one
   (open a new spec under the same feature instead). The GUI and render layer both
   refuse edits to frozen docs:
   ```
   sc mem doc freeze <document_id>
   ```
2. **Read the shipped code, then write the doc.** The doc is written from
   interpretation of the code as it actually shipped — not from the spec body.
   Drift happens during production: decisions get made, scope adjusts, edge cases
   land differently than planned. The spec captures the intent; the code is the
   truth. Read the implementation first, then write what it does:
   ```
   sc mem doc add "<feature> — how it works" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/<slug>.md
   ```
3. **Close the docs-pending flag** with a note pointing at the doc:
   ```
   sc mem flag close <flag_id> --notes "Spec frozen; doc <document_id> written → docs_sc/<slug>.md"
   ```

Until step 3, `shipped` + the open flag is the truthful interim state: delivered,
doc pending.

## View

Open any doc rendered: the GUI''s "open in md-converter ↗" (Roadmap card or Docs
tab) — the body rides in the URL, no upload. For long-form authoring, write the
markdown to the `body` and let the render + md-converter handle presentation.

---

# Authoring format (themed-markdown)

The `body` you write **is** themed-markdown — the format md-converter renders.
**Your job is structure; styling is the renderer''s job.** Never write visual
instructions (colors, fonts, sizes, themes). Apply the four semantic classes;
the theme picks the actual colors.

Use **only** the constructs below. Anything else either drops silently or breaks
the render.

`req` = required · `opt` = optional · `≤N` = soft character cap (over-cap wraps
awkwardly or overflows a fixed UI slot).

## Frontmatter

Author these in the body''s frontmatter:

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

`date`/`project`/`purpose` → footer meta cards. **`sc render` injects
`feature`, `roadmap_status`, `frozen`, `rendered_by`, `source` on top of these
— don''t write those yourself.** Never use comma-separated tags (`tags: a, b`);
always a YAML list.

## Structure

| Syntax | Role | Cap |
|---|---|---|
| `# Title` | doc title (opt; falls back to `frontmatter.title`) | — |
| `## Section` | sidebar tab | ≤28 |
| `### Heading` | subsection → `<h3>` | ≤80 |

H4–H6 ⛔.

**Tab rule:** every H2 = one tab. Content between two H2s belongs to the first.
Content between H1 and the first H2 is **silently dropped** — put intro under an
H2 (e.g. "Overview"). Single-section docs may omit H2s (whole doc = one tab).

**Doc scale:** the app renders every section up-front and re-renders every
Mermaid on each tab switch. Aim for ≤25 sections and ≤15 Mermaid diagrams; split
larger material.

## Inline · lists · tables · images · code

- Inline: `**bold**` · `*italic*` · `~~strike~~` · `` `code` `` · `[text](url)`
- Lists: `-` unordered · `1.` ordered · `- [ ]` / `- [x]` tasks
- Tables: standard GFM pipe tables
- Images: `![alt](https://url/img.png)` — absolute URLs only, descriptive alt
- Video: a bare video URL **alone on its own line** renders as a player —
  a `github.com/user-attachments/assets/<id>` URL (paste a video into a GitHub
  issue/PR to mint one) or any absolute URL ending `.mp4`/`.webm`/`.mov`/`.ogg`.
  Don''t wrap it in `![]()` or `[]()` — bare is what triggers the player.
- Code: fenced with a language hint (```` ```python ````)

## Color classes

`class1`–`class4`, available on callouts, stat cards, mermaid nodes, and linear
steps. **You choose which class fits each piece by meaning** — the theme decides
the color. Keep one class per semantic role across the doc (e.g. `class1` =
primary, `class2` = supporting, `class3` = positive/done, `class4` =
caution/warning). Consistency > specific choice.

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
| `value` | req | ≤12 | Short token: `87%`, `1.2M`. Not sentences. |
| `label` | req | ≤28 | One short noun phrase. |
| `description` | opt | one short line | Omit if no signal. |

Layout: 2 per row; trailing odd card spans the row.

## Mermaid

````
```mermaid
graph LR
  A[Start]:::class1 --> B[Middle]:::class2 --> C[End]:::class3
```
````

Class via `:::classN` on nodes. The app injects `classDef` — **don''t** write
`classDef`, `fill:`, or any style directive. Node label cap ≤24 (Mermaid
auto-sizes nodes; long labels balloon them).

**Quote labels with special characters.** Unquoted node text is parsed as
Mermaid grammar, not literal text. Any label containing `/`, `(`, `)`, `*`, `[`,
`]`, `{`, `}`, `<`, `>`, `#`, `:`, `;`, or a quote **must** be wrapped in double
quotes inside the brackets — otherwise the diagram throws *"Syntax error in
text"* and renders nothing. Notably `A[/text/]` is the parallelogram shape, so a
literal path like `/lease/mail/*` breaks unless quoted.

```
GOOD:  AD["/admin/user-credentials/"]:::class3
       N["count > 0"]:::class2
BAD:   AD[/admin/user-credentials/]      (parsed as a parallelogram shape → error)
       N[count > 0]                      (> is a grammar token → error)
```

Cylinder/stadium shapes are fine as-is — `DB[(secrets.db)]`, `X([ready])` —
quote only the inner *text*, not the shape brackets.

## Linear

````
```linear
Step 1 :::class1 -> Step 2 :::class2 -> Step 3 :::class3
```
````
Steps separated by `->`, optional class via `:::classN`. Steps render
**vertically** — one per row, top→bottom (never horizontal). Step text cap ≤48.

## Never

- H4–H6 · blockquotes (except callouts) · footnotes · raw HTML
- Color / font / size / theme / visual mentions (the theme owns styling)
- Content between H1 and the first H2 (silently dropped — use an H2)
- Comma-separated `tags` (must be a YAML list)
- `classDef` / `fill:` / style directives inside Mermaid
- Unquoted Mermaid labels containing special characters

## Open in md-converter

A doc whose `body` lives in the DB already opens in the app from the GUI
("open in md-converter ↗" on the Roadmap/Docs card) — nothing to author there.

When you instead **commit a standalone themed-markdown file** to the repo (a
README, or a rendered `docs_sc/` page meant to be read on GitHub), drop a
one-click badge in its preamble — between `# Title` and the first `##`, so it
shows on GitHub but is dropped from the render (preamble rule):

```markdown
[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/<owner>/<repo>/blob/<branch>/<path>)
```

Fill `<owner>/<repo>/<branch>/<path>` with the file''s GitHub location (any
subdirectory depth). Public repos only — the badge fetches the raw file in the
reader''s browser (no server/auth). Destination unknown → keep the placeholders
and tell the user to fill them.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

-- 2a. Decisions table row: "never supersede, add a new one" → supersede-with-new.
UPDATE shells
   SET system_prompt = REPLACE(system_prompt,
       '| Decisions | `shell_decisions` — major decisions; never supersede, add a new one |',
       '| Decisions | `shell_decisions` — major decisions; never edit a row — supersede with a new one (`--parent`) |')
 WHERE system_prompt LIKE '%never supersede, add a new one%';

-- 2b. Read-side awareness paragraph, anchored after the writes paragraph.
UPDATE shells
   SET system_prompt = REPLACE(system_prompt,
       '`memory` and `db_map` skills.',
       '`memory` and `db_map` skills.

**Read before you decide.** Settled choices constrain new work — before any
architectural or approach decision, lazy-load the log: `sc mem get decisions`
(index of active decisions; `sc mem get decisions <id>` for the full row with
rationale). Honor a prior decision or supersede it explicitly (`--parent`) —
never silently re-litigate.')
 WHERE system_prompt LIKE '%`memory` and `db_map` skills.%'
   AND system_prompt NOT LIKE '%Read before you decide%';

COMMIT;
