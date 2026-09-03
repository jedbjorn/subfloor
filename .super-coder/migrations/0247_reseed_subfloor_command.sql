-- 0247 — publish the subfloor command in shell-facing skill text.
-- The make dos-* operator aliases are retired for `subfloor <verb>` (a bash +
-- fish function ./sc install writes and ./sc update refreshes) and the visible
-- product name is Subfloor. No schema change: full-body skill UPSERTs converge
-- upgraded installations on the same text a fresh seed produces.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'admin_git',
  'Admin-only Git procedure for the repository root — identify main, fast-forward safely, commit fork engine pins, merge only approved PRs, and preserve every foreign worktree. Use before Admin performs Git maintenance or an authorized merge.',
  'substrate',
  NULL,
  0,
  '# admin_git — maintain the repository root

Admin owns the root checkout and its `main` branch. Use this procedure for a
specific update, reconciliation, or approved merge; it is not a standing
cleanup pass. The FnB merge gate and the preservation rule remain in force.

## Orient before writing

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short --branch
git worktree list
```

Proceed only when the top level matches the repository named by the boot
document and the root checkout is on `main`. A dirty root, detached head, or
diverged main is a decision boundary: show the exact state to the FnB before
changing it.

Every other worktree belongs to its shell. Never switch its branch, stash,
reset, clean, move, or remove it. When the FnB explicitly asks for repository-
wide cleanup, load `git_cleanup`; otherwise leave foreign worktrees untouched.

## Fast-forward main

```bash
git fetch origin main
git pull --ff-only origin main
```

Success leaves `main` clean and at the fetched remote head. If `--ff-only`
refuses, stop and report the local/remote commits; never create a merge bubble
or reset main to make the command pass.

## Commit a fork engine pin

In a tracking fork, `.super-coder/` is a materialized dependency and remains
gitignored. After `self_update` succeeds, stage only the durable public update:

```bash
git add .sc-state/engine.ref
git status --short
SC_SHELL_FLAVOR=admin git commit -m "chore: update subfloor engine pin"
```

Set the marker on this commit command even inside an Admin shell. The update
may have replaced the pre-commit hook during a session launched by the old
floor, whose inherited environment cannot contain the new Admin exemption.
The marker makes that one post-update handoff explicit without bypassing hooks.

Add the root `sc` dispatcher or another public file only when the update
deliberately changed it. Never force-add `.super-coder/`, local snapshots,
rendered `_sc` state, or `.sc-state/engine.ref.prev`. Push the resulting main
commit only within the operator''s requested update workflow.

## Merge an approved PR

Merge only after the FnB names or explicitly authorizes the PR. Re-read live
state immediately before acting:

```bash
gh pr view <number> --json url,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup
```

Require the expected repository, `baseRefName=main`, the reviewed head, a
mergeable state, and successful required checks. Use the repository''s approved
merge method, then `git pull --ff-only origin main`. A changed head, red/pending
check, or merge refusal invalidates the authorization; stop and return the live
evidence instead of overriding it.

For a stack, retarget each remaining PR to `main` before merging the PR above
the one that landed. Never rely on automatic retargeting after a base branch is
deleted.

## Source-repository exception

```bash
git ls-files --error-unmatch .super-coder/schema.sql
```

Exit 0 means this repository authors Subfloor itself: `.super-coder/` is
tracked source, not a dependency, and `.sc-state/engine.ref` is not the delivery
unit. Engine implementation still arrives through a Developer branch and PR;
Admin fast-forwards main and merges only the exact approved PR. Apply live
migrations or restart the engine only through their dedicated procedures and
operator-owned recovery window.

## Stop conditions

- No approval -> do not merge.
- Foreign worktree activity -> preserve it and surface it.
- Main cannot fast-forward -> report divergence; do not reset.
- Target repository, PR head, or checks differ from the authorization -> stop.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'docs',
  'Author or review docs & specs in Subfloor. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.',
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
feature-to-feature links; related work within one mental model is another spec
under the same feature. A genuinely new era may split into a fresh feature by
moving its unfrozen active spec through the guarded workflow below. Freeze =
the ship-time record of what was built to; it never gates the feature''s other
specs.

| state | test | meaning |
|---|---|---|
| **shipped** | `frozen = 1` | delivered; immutable record |
| **active** | unfrozen + has rows in `spec_tasks` | the spec being built now |
| **backlog** | unfrozen, no task plan | the pile, ordered by `seq` |

The **doc** (`kind=''doc''`) = the feature''s readable face — write it when the
first spec ships, under the same `feature_id`. Sibling of the specs, not a
parent.

## Split an active era from feature history

When accumulated history makes a feature''s active context misleading or a new
era has become a separate mental model, preserve the old feature as history and
move the existing unfrozen active spec intact:

1. Create the fresh feature with its correct work-stream, status, and summary.
2. Run `sc mem doc move <document_id> --feature <target_feature_id>`.
3. Re-read the document and task ledger under the target feature; the same ids,
   task states, and document-linked decisions must now project there.
4. Edit the historical feature''s title/summary to name the split, then set its
   truthful terminal status (`shipped` for delivered history, `retired` for
   abandoned history).

The move assigns the spec''s next target-feature sequence and is atomic across
the document, its tasks, and document-linked decisions. It refuses frozen
specs, ordinary docs, terminal targets, and any spec already bound to a Sprint.
Do not duplicate the spec or cancel/recreate its tasks when this move applies.

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

## Establish posture, then challenge

Before writing — don''t duplicate, don''t re-litigate, and don''t transcribe the
request uncritically:
```
sc mem get documents      # every control-plane spec/doc (kind, seq, frozen, task_count)
sc mem get decisions      # active-decision index (<id> = full row + rationale; --all incl. superseded)
sc map-sql "SELECT path FROM dr_filepath WHERE role=''doc'';"   # repo''s own docs (map db)
```

Spec touches a recorded decision -> honor it, or supersede explicitly: say so
in the spec + record `sc mem decision "…" --parent <old_id>`. NEVER silently
re-decide a settled choice.

Read the documentation for every related system the change relies on or alters.
Documentation is the preferred account of intended posture. If it is absent,
ambiguous, or plausibly stale, use the repo map to locate and verify the narrow
code path; name that fallback in the spec. Documentation and code disagree ->
surface both as an inconsistency to the FnB. Do not silently choose one as truth.

Walk the proposed workflow end to end. Challenge contradictions, missing
boundaries, hidden assumptions, audience mismatches, partial failure, concurrent
use, permissions, and the unhappy path. Ask the FnB to resolve anything that
could change implementation or acceptance. Finalize only after each material
question is either resolved or explicitly deferred; a deferral belongs in
**Out of Scope**, with the boundary it leaves behind. Editorial uncertainty that
cannot change the build does not block authoring.

The conversation is input; the spec is the resolved contract. Paraphrase or
synthesize settled FnB decisions beside the design clause they govern. Preserve
the requirement and consequential rationale, not the dialogue. Never promote an
unconfirmed suggestion to a requirement or hide an unresolved choice in fluent
prose.

## Required spec contract

Every newly authored spec, and every substantive revision to an unfrozen spec,
must make these boundaries explicit:

| section | holds |
|---|---|
| `## Current Posture` | related systems and intended behavior before this change; name the documents and decisions consulted, plus any code paths used because documentation was missing, ambiguous, or stale |
| `## Scope` / `### In Scope` | behavior this delivery promises to add, change, or remove |
| `## Scope` / `### Out of Scope` | adjacent behavior deliberately excluded from this delivery, including explicit deferrals and the boundary retained |
| relevant design sections | synthesized FnB decisions and rationale placed beside the requirement they constrain |
| `## Anticipated User Activity` | actors, per-surface audience posture, reach, process curation, safety hardening, tenancy, and behavior beyond intention |

Out of scope means "not in this delivery," never "will never be built." Do not
use it to park an unresolved requirement that changes the in-scope design. Mixed
surfaces may have different audiences and controls; specify them separately.
Frozen historical specs are immutable and need no backfill.

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
reached, how much process guidance each surface needs, which safety controls its
exposure and impact require, whose data it holds, and what it does not intend to
allow. Soft vocabulary, hard invariants — the nouns stay gentle, every statement
stays checkable from code ("a Valid User only ever sees rows tied to their own
account"), because review + Verification test the build against this section.

Shape (H3s under the section H2):

| H3 | holds |
|---|---|
| `### Vocabulary` | the cast — roles from the shared roster below + any feature-specific ones, each defined in one line |
| `### Expected Activity` | per role: what they do, what they see, what they can change |
| `### Reach` | where the feature meets the world — pages, endpoints, jobs, files it adds or alters, and which roles can arrive at each |
| `### Audience and Assurance` | for each reachable surface: intended audience posture(s), process curation, and concrete safety hardening derived from exposure, authority, data sensitivity, reversibility, and blast radius |
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

Audience postures describe the expected user of a surface; they do not replace
the activity roles above and are not mutually exclusive:

| posture | design consequence |
|---|---|
| **Unknown User** | identity and competence are not established at entry; public/anonymous reach needs the most tolerance of unanticipated input, guidance, safe defaults, and bounded disclosure |
| **Authenticated User** | identity is established, but trust is not assumed; keep account boundaries, validation, recovery, and clear feedback explicit |
| **Operational User** | a trained user performs a recurring workflow; optimize safe repetition, observable state, partial-failure recovery, and low ambiguity |
| **Technical User** | a user understands technical concepts or automation; instructional curation may be lighter, while contracts, validation, precise errors, and compatibility stay rigorous |
| **Administrator** | a user holds broad authority; instructional curation may be light, while authorization, consequential-action confirmation, auditability, reversibility, and recovery reflect the larger blast radius |

Separate **process curation** from **safety hardening**. Expertise can reduce
explanation and hand-holding; it never waives correctness, input validation,
authorization, tenancy, or safe failure. Derive hardening from the surface''s
reach and consequence, not from a single audience ranking. An Unknown User
demands the strongest unanticipated-input posture; an Administrator may demand
the strongest authority and recovery controls. State concrete obligations, not
only a posture label.

An Unknown User is expected anonymous/public traffic. An Unexpected Participant
is activity outside the stated contract, whether or not identity is known. For
machine-only behavior, name `System` or `Shell`, say that there is no human
audience, and still specify reach and assurance.

Language — soft by design. Specs never use: threat model, attack or attack
surface, adversary, exploit, abuse case, vulnerability, breach, privilege
escalation, exfiltration, malicious. Say it in roster words instead: threat
model -> anticipated activity · attacker -> Unexpected Participant · abuse
case -> Beyond Intention · access matrix -> Expected Activity · attack
surface -> Reach · isolation -> tenancy. Describe behavior and boundaries,
never hostility.

Internal-only feature -> the section still ships; identify its operational,
technical, or administrator surface, reach, authority controls, and tenancy
posture. Whole section ≤ ~60 lines — it frames the build, it does not enumerate
it.

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
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'engine_database',
  'Admin-only map of Subfloor''s private instance database, schema, tables, backups, snapshots, rebuild path, SQL diagnosis, and repair boundaries.',
  'substrate',
  NULL,
  0,
  '# engine_database — inspect and repair the control plane

Admin only. The boot `ENGINE MAINTENANCE` block names the active engine floor
and private instance-state directory. Resolve the canonical database again
before any repair:

```bash
python3 .super-coder/scripts/instance_state.py active-database .super-coder
```

Require the printed absolute path to sit under the boot''s private instance
state. The private directory owns the live `shell_db.db` plus WAL/SHM sidecars,
local control-plane snapshot, verified backups, relocation receipt, maintenance
lease, and DB-generation evidence. The repository catalogue remains a separate
map store; a product database remains the fork application''s concern.

## Source and rebuild model

In the Subfloor source repository, `.super-coder/schema.sql` is the current
baseline and `.super-coder/migrations/*.sql` are ordered, ledger-tracked deltas.
Installed downstream floors materialize the same engine source. `sc rebuild`
creates a candidate from that source plus the private instance snapshot,
verifies it, and publishes only through the maintenance cutover. Load
`engine_migrations` before changing the baseline or migrations and `snapshot`
before serializing instance content.

## Data model

| Surface | Storage |
|---|---|
| Shell core | `shells` — role, flavor, mandate, system prompt, current state, active session/archive identity |
| Seed and L&S | `shell_identity_entries` — capped identity entries with retirement |
| Decisions | `shell_decisions` — append-only decisions and supersession links |
| Narrative | `shell_memory_archives` — per-session narrative |
| Planning | `roadmap`, `feature_blockers`, `projects`, `project_shells`, `spec_tasks` |
| Documents | `documents` — revisioned spec/doc bodies and freeze state |
| Flags | `flags` — open/resolved work linked to features |
| Skills | `skills`, `flavor_skills`, `shell_skills`, `resolved_shell_skills` |
| Coordination | message, wake, conversation, Sprint, PR-subscription, and liveness tables |

Normal reads and writes still use `sc mem` and bounded APIs. The table map is
for diagnosis, migration authoring, and recovery—not ordinary shell work.

## SQL and mutation boundary

`sc sql` is the Admin read-only diagnostic lane and remains available from the
host Admin seat when the API is down. `sc sql-rw` is an overt escape hatch and
must refuse outside a named procedure satisfying all of these gates:

- managed runtime stopped;
- exclusive maintenance lease held;
- WAL-safe backup verified before mutation;
- exact canonical target independently matched;
- candidate and ledger verified before publication;
- restart health and rollback evidence retained.

Prefer the typed maintenance command (`sc migrate`, `sc rebuild`, `sc update`,
`sc rollback`, or the named recovery procedure) over direct SQL. Keep external
calls outside transactions. A path mismatch, unresolved private state,
conflicting legacy/private copies, failed backup, or absent authority stops the
operation with the runtime down.

## Recovery routing

- API down, database healthy: use host Admin `sc health`, `sc logs`, and
  read-only `sc sql`, then restore the managed service with `sc restart` /
  `subfloor restart`.
- Migration or rebuild work: load `engine_migrations` and require its backup,
  candidate, ledger, and restart receipts.
- Snapshot or render repair: load `snapshot`; do not hand-edit serialized or
  rendered state.
- Update/rollback failure: load `self_update`; preserve the engine/database
  generation pair.
- Ambiguous or damaged canonical state: keep the runtime stopped and present
  the exact database, backup, generation, and relocation evidence to the FnB.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git conventions for a Subfloor shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK), attribute commits per-shell. Use before any git work.',
  'substrate',
  NULL,
  0,
  '# git — version control, the Subfloor way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in Subfloor. NEVER commit or edit anything under `.super-coder/`.

## GitHub capability boundary

`sc launch` and `sc restart` re-resolve Git transport and GitHub API
capabilities from the host on every invocation, including `--no-build` forms.
`build`, `enter`, and an already running sandbox do not refresh auth. Pass = the
lifecycle summary says `ready` for the operation you need; `unavailable` and
`unverified` are NEVER readiness claims.

Preserve the configured `origin` transport. For SSH, fix the host agent and
load an authorized GitHub identity; NEVER copy or mount private keys. For
HTTPS/API, fix a scoped host `SC_GH_TOKEN` or the host `gh` OAuth login. Then
run `sc launch` or `sc restart`; the running sandbox remains unchanged
until that refresh. NEVER rewrite the remote or start an interactive login
inside the sandbox to work around a missing capability.

## Sync before you start — hard pre-code gate

Run the gate every session + before each new unit of work. `shell/<shortname>` = a moving base pinned to `origin/main`, not a content branch — cut feature branches from it. A stale base -> you read code that no longer exists + your PRs conflict on arrival.

The launcher auto-syncs at boot when provably nothing can be lost (on base branch + clean tree + no local-only commits). Read the `sync:` line in ACTIVE SESSION: auto-synced + nothing done since -> current, carry on. Says **NOT auto-synced** / you''re mid-session about to start new work -> run:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` -> record remote freshness; continue through the branch/target gate even when the count is 0.
2. Compare `git rev-parse --show-toplevel` + `git branch --show-current` with ACTIVE SESSION before any destructive command. A mismatch -> stop + surface it.
3. Exact `shell/<shortname>` base -> discard local-only commits, tracked changes, and non-ignored untracked files without asking: `git reset --hard origin/main && git clean -fd`. Durable coordination belongs in the control plane and code belongs on a pushed remote branch with a PR. Pass = `git status --short` is empty + `git rev-parse HEAD` equals `git rev-parse origin/main`.
4. NEVER reset or clean a feature branch / open PR. Clean stale feature branch -> `git rebase origin/main`. Dirty or unpushed feature work -> list it + ask the FnB to land / stash / discard.
5. NEVER `git pull`/merge on the base — merge bubbles accumulate + squash-merged work replays as conflicts.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to the default branch. Branch first: `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs). *Admin-shell exception:* it boots at the repo root on `main`, exempt from the branch-guard; committing to main is its mandate (engine updates, migrations, approved patches) and it starts each session with `git pull --ff-only`. Every other shell branches, always.
2. Commit in logical units. End every message with your shell''s trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open a PR -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## Finish before you stop

Bookend to the sync gate. At end of session: `git status` (uncommitted) + `git rev-list origin/<base>..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don''t skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git restore` / `git stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.

Pass = tree clean, or on a pushed branch with a PR. A dirty/unpushed tree forces the admin''s `git_cleanup` to map attribution, check liveness, and commit on your behalf.

## After a merge — clean up local

Only after the PR is merged:

A managed worktree whose Sprint is already `completed` is the exception: the
Sprint cleanup service owns its reset after live turns exit. Do not race that
service with manual Git cleanup. A pending or failed cleanup makes the slot
unavailable until `sc sprint cleanup-status --sprint <id>` reports succeeded;
the originating Planner or FnB uses the Sprint retry surface.

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Never commit the engine or derived files

- `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `.claude/skills/`, `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.
- Exception: in the Subfloor source repo, tracked engine database source is project source; identify exact files through the repository catalogue.

## After DB work

A confirmed `sc mem` write lands in the shared control plane immediately. The
Admin/API persistence path owns generated serialization and renders; they are
not a Developer commit or Publish PR.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main` (see the sync gate). Worktree isolation is automatic — no shared cwd. Admin shell = the one exception: repo root on `main`.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'issue_reporting',
  'Report engine defects upstream — the moment a sc command fails or lies, a skill contradicts your reality, the API blocks a documented workflow, or you work around the engine to proceed. File a GitHub issue on Subfloor; your repo''s app bugs stay in the fork.',
  'substrate',
  NULL,
  1,
  '# issue_reporting — the backwards flow

An engine defect fixed upstream reaches every fork via `sc update`; worked
around silently, every fork re-derives the workaround. File the issue while
the failure is on screen — NEVER batch to session end.

A workaround IS a report: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; file it now.

## Boundary — engine vs fork

| Where | What |
|---|---|
| **Upstream — file it** | anything the engine materializes/owns: `.super-coder/`, `sc` + every subcommand, engine skills (this catalogue), the boot doc render, the sandbox / dev kit, `sc update` + migrations, the `_sc` API + `sc mem` |
| **Fork — don''t** | the repo''s app code, DB-canonical fork-local skills, operator-owned host config |

Unsure -> "would the same problem hit any other fork?" yes = upstream.

## Triggers

Each row = a real engine defect filed by a fork shell doing ordinary work.
Match the left column -> file.

| You hit | Real case |
|---|---|
| A `sc` command fails out of the box | `sc verify` always aborted — its own render step needed `SC_ADMIN` it never set (#227) |
| A command exits green without doing the work | `sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `sc lint` said "run `sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths your seat doesn''t have | a sandbox skill drove raw host-only `ssh`/`virsh` paths (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `sc`, then bare git hit the wrong tree, "my edits vanished" (#225) |
| The sandbox can reach something it shouldn''t | `do_push` src/dest weren''t contained — sandbox→host escape (#228) |

Stale guidance (skill says X, engine does Y) files the same as a crash.

## Capture — while the failure is on screen

- **engine ref** = `sc engine-ref` — first line of every report
- **staleness** = compare that ref to upstream head:
  `git ls-remote https://github.com/jedbjorn/subfloor HEAD` — write
  `current` or `behind head <sha7>`. Behind + the symptom is a missing
  command or a skill/engine mismatch -> the fix may already be shipped:
  ask your FnB for `sc update` first, and file only if the defect
  survives the update (or updating isn''t an option — then the staleness
  note carries that caveat). Triage reads this line to tell a live
  engine defect from a stale fork build.
- **fork + seat**: repo name, shell flavor, sandbox/host
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The issue is public: NEVER paste api keys, tokens, secrets, or private paths.

## File it

```bash
# 1. dedup — someone may have hit it first
gh issue list --repo jedbjorn/subfloor --search "<symptom keywords>" --state all

# 2. file — title: [<fork>] <area>: <one-line symptom>
gh issue create --repo jedbjorn/subfloor \
  --title "[<fork>] <area>: <symptom>" \
  --body "$(cat <<''EOF''
- engine ref: <sha from .sc-state/engine.ref> · <current | behind head <sha7>>
- fork/seat: <repo> · <shell flavor> · <sandbox|host>

**Ran / followed:** <command or skill+step>
**Expected:** <what the docs/skill promise>
**Actual:** <exact trimmed output>
**Workaround:** <what unblocked you, or "blocked">
EOF
)"
```

`jedbjorn/subfloor` = engine upstream; confirm: `git remote get-url super-coder`.

Dedup hit -> comment your engine ref + repro on the existing issue; do NOT
file a duplicate.

No `gh` / no network from your seat -> save the identical body as a fork flag:
`sc mem flag open "[Engine] <symptom> | Blocker for: <x>" --name UP-###`, then
message the **admin** shell to relay it upstream (see `messaging`).

## Authorized curation recommendation

The `curate` skill has one FnB-authorized exception to the normal enhancement
gate below. When a recurring L&S cluster may warrant a reusable upstream skill,
the curating shell may search and file the recommendation directly without
asking the FnB first.

Search all upstream issues before opening anything. Add evidence to a matching
recommendation, or open one titled `skills: recommend <topic>` containing the
trigger, repeated incidents, proposed ownership boundary, expected users, why
existing skills do not cover it, and a compact candidate procedure.

This route recommends; it never creates or promotes a skill. Keep one compressed
L&S entry until a reviewed upstream skill ships and is granted. If issue search
or creation is unavailable, surface the failure to the FnB, keep the L&S, and
create no local skill or asset. Deliberate fork-specific authoring remains the
Planner-owned workflow in `fork_skill_design`.

## Rules

- One defect per issue. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to your FnB first, except the authorized curation
  recommendation route above.
- Filing ≠ unblocked: defect blocks work -> also open a fork flag linking the
  issue URL.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'self_update',
  'Update this fork''s Subfloor engine in place — fetch + materialize new code + migrations, all memory intact; sound rollback. The shell hands off to its own next boot. Use when a Subfloor update is available.',
  'substrate',
  'sc update',
  0,
  '# self_update — laying a new floor under your own feet

The local shell updates its own substrate — no external rebuild. All state lives
in the DB and engine code is read live each session, so a code-only update
touches no data; a schema change applies as an in-place migration, never a
destructive rebuild. `current_state`, narrative, decisions, flags, seed, and
L&S all carry across. This is succession for the substrate: you handing off to
you.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update''s output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the engine from the `super-coder` remote,
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`''s hermetic rebuild surfaces it. A red
     render-check here = a local mirror to regenerate. Pipeline + guard details:
     `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit only the public update.**
   Stage `.sc-state/engine.ref` (the pin), the root `sc` dispatcher if it
   changed, and other deliberately authored public files. Snapshot SQL and
   `_sc` renders remain ignored beneath `.sc-state/local/`; never force-add
   them. `.super-coder/` and `engine.ref.prev` are also gitignored in forks.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first — rollback is itself
   reversible;
2. restores the DB from the most recent pre-update backup in
   `~/db_backups/<repo-name>/` (keyed by this fork''s repo dir name — distinct
   from any `db_backups/` dir the fork''s app keeps at its repo root);
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes: structural change -> new migration file,
additive where possible.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
