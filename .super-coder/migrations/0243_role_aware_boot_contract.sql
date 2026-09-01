-- 0243 — publish the role-aware boot and skill contract.
-- Full-body skill UPSERTs converge upgraded installations; update.py separately
-- re-renders engine-owned standard-flavor prompts and preserves Bespoke rows.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'cartographer',
  'Own the repo map — configure mapping to THIS repo, wire auto-remap, install semantic extractors, curate authored navigation, and finish through one truthful finalization gate. Cartographer-only.',
  'substrate',
  'sc map-setup',
  0,
  '# cartographer — own the repo map

Working shells consume the `dr_*` catalogue and NEVER map. Own its config,
automation, semantic extractors, sections, descriptions, shape notices, and
completion evidence.

Map data = `.sc-state/local/map/map.db`, separate from engine memory. Use:

- `sc map-schema [dr_table]` for structure. Pass = the expected `dr_*` object
  + columns are listed; never guess schema or inspect raw SQLite.
- `sc map-sql "…"` for read-only data queries.
- `sc map-sql-rw "…"` only for the authored `dr_section` / `dr_filepath.desc`
  writes named below.
- `sc map` to refresh derived rows.
- `sc map finalize` to prove completion. Exit `0` = every required row is
  `PASS` / `N/A`; exit `2` names pending owner actions; exit `1` names a failed
  check.

## First boot / heal

Run this sequence on first boot, after a shape notice, or when the map drifts:

1. `sc map-schema` then `sc map-schema dr_repo`. Pass = map structure is
   inspectable through the supported surface.
2. Inspect live data:

   ```sql
   SELECT name, root, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath
   WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```

3. Tune `.sc-state/local/map/config.json` in the assigned worktree only where defaults are
   wrong. Config is per-clone runtime state and never a commit. All keys are
   optional; skip sets extend defaults and cannot re-include engine-owned
   paths:

   ```json
   {
     "skip_dirs": ["generated", "fixtures"],
     "skip_files": ["LICENSE"],
     "role_overrides": [
       {"prefix": "cmd/", "role": "code"},
       {"glob": "*.proto", "role": "code"},
       {"prefix": "docs/adr/", "role": "doc"}
     ]
   }
   ```

4. Run `sc map-setup`. Pass = `git config --get core.hooksPath` prints
   `.super-coder/hooks`, the declared hooks are executable, and `dr_repo`
   carries a current `mapped_at` + correct file count.
5. Curate sections + descriptions + semantic rows with the worklists below.
6. Resolve every notice-linked flag, then mark the notice read last.
7. Run `sc map finalize`. Complete Cartographer-owned actions; hand each
   Admin-owned snapshot/review action to Admin. Pass = a rerun exits `0`.
8. On first boot only, run `sc mem state "…"` then `sc mem oriented` after the
   finalizer is green.

Automation remains healthy when:

- `post-merge` / `post-checkout` / `post-rewrite` run `sc map` through
  `core.hooksPath`.
- Admin control-plane rebuilds remap through their supported lifecycle.
- pm2''s `sc-map-<repo>` one-shot cycles stopped -> online hourly while the
  stack is up. A repo without pm2 relies on hooks + manual `sc map`.

## Authored navigation

### Sections

`dr_section` is authored + snapshot-backed. Curate useful path prefixes; never
insert an empty prefix. Root files belong to the synthetic `Repository Root`
group and never enter `dr_section`.

```sql
-- Repository Root leaves; a non-empty result renders the synthetic group:
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, ''/'') = 0 ORDER BY path;

-- Authored sections + live counts:
SELECT s.name, s.path_prefix, s.description,
       (SELECT COUNT(*) FROM dr_filepath f
        WHERE f.path LIKE s.path_prefix || ''%'') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- WORKLIST: only nested unmatched files are real section gaps:
SELECT f.path FROM dr_filepath f
WHERE instr(f.path, ''/'') > 0
  AND NOT EXISTS (
    SELECT 1 FROM dr_section s
    WHERE f.path LIKE s.path_prefix || ''%''
  )
ORDER BY f.path;

-- STALE authored sections after a rename/removal:
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE NOT EXISTS (
  SELECT 1 FROM dr_filepath f
  WHERE f.path LIKE s.path_prefix || ''%''
)
ORDER BY s.name;
```

Use `sc map-sql-rw` to `INSERT` / `UPDATE` / `DELETE` the exact rows identified
by these queries. Pass = nested unmatched + stale-section worklists return no
rows; root files remain queryable through `instr(path, ''/'') = 0`.

### Descriptions

Set `dr_filepath.desc` to an adequate one-line description (<=100 chars): say
what the file does/holds, not its kind or filename. Descriptions survive remap
in the live DB but are not snapshot durability; refill after a fresh rebuild.

```sql
WITH f AS (
  SELECT path, role, desc,
         replace(path, rtrim(path, replace(path,''/'','''')), '''') AS base
  FROM dr_filepath
), g AS (
  SELECT *, CASE WHEN instr(base,''.'') > 0
    THEN substr(base, 1, instr(base,''.'')-1) ELSE base END AS stem
  FROM f
)
SELECT path, role, desc FROM g
WHERE desc IS NULL
   OR (length(stem) >= 5 AND (
       lower(substr(desc, -length(base))) = lower(base)
       OR lower(substr(desc, -length(stem))) = lower(stem)
   ))
ORDER BY (desc IS NULL) DESC, role, path;
```

Update only rows verified against the file. Pass = the worklist is empty +
spot checks per section describe behavior that the path alone cannot reveal.

### Product DB

Tag the host application''s schema/migrations as product DB, never engine
memory. The live app `.db` is often ignored; tracked schema + migrations are
the durable map anchors.

```sql
UPDATE dr_filepath
SET desc=''Product DB schema — the APP database (NOT engine memory)''
WHERE path=''<app schema file>'';

UPDATE dr_filepath
SET desc=''Product DB migration — change the app schema here''
WHERE path LIKE ''<app migrations dir>/%'';
```

Create an authored section when those files form a real area. Pass = working
shells can identify the app DB definition without confusing it with Subfloor
control-plane state. No product DB -> `N/A`.

## Semantic extractors

Extractors implement `extract(con, repo_root, cfg) -> str` and own only their
semantic `dr_*` rows. They DELETE + repopulate their own derived tables, guard
unparseable files, report best-effort omissions, and never claim exhaustive
coverage.

Adopt an extractor:

1. Inspect stack dependencies/file mix with `sc map-sql`.
2. Author `.sc-state/map_extractors/<name>.py` in the assigned worktree against
   the extractor contract above.
3. Run `sc map-extractor install
   ".sc-state/map_extractors/<name>.py"`. Pass = output
   prints the installed canonical path + SHA-256 matching the authored bytes.
4. NEVER `cp`, `mv`, redirect, or use a file-edit tool into
   another checkout''s `.sc-state/map_extractors/`. The guarded installer is the only
   supported cross-worktree write.
5. Run `sc map`, inspect structure with `sc map-schema <dr_table>`, then query
   rows with `sc map-sql`. Pass = expected semantic rows exist + the map log
   has no extractor failure.
6. Commit + push the authored worktree source. Hand Admin the source path for
   review/merge when finalization names that action. Generated map DB, status,
   receipts, and snapshots stay local-only.

An extractor failure rolls its plug-in writes back while preserving the core
map. Pass = `sc map finalize` reports no failed module and every installed
extractor has matching receipt/source/Admin evidence.

## Shape notices

Sender = the dev/coder shell on merge, not Planner. Open blocking map-quality flags before sending
one notice to the `cartographer` role alias:

```text
shape: <what landed> — paths: <region/>; ref: <feature/doc/PR>
flags: <numeric_id>=<SC-name>[, <numeric_id>=<SC-name>] | none
curate; verify and close each flag; mark this notice read last.
```

Name the durable ref + exact path region. Pair every flag''s numeric DB ID with
its display name. Write `flags: none` when no flag exists. Pass = one notice
carries every map-quality flag opened for that shape change.

On receipt:

1. Parse all three lines. Missing/malformed `flags`, missing flag, or ID/name mismatch -> surface
   the exact defect + leave the notice unread.
2. Run the nested-section + stale-section + description + semantic worklists
   scoped to the named region. Pass = every scoped result is clean.
3. For each pair, run `sc mem get flags <numeric_id>` and confirm the display
   name. An already-resolved row passes only when its notes name the verified
   map result. Otherwise run `sc mem flag close <numeric_id> --notes "<what
   was verified>"`; pass = the exact row is resolved with adequate notes.
4. Run `--message mark-read <message_id>` last. Pass = scoped worklists + every
   named flag passed before the notice became read. Send no closure reply.

## Persistence boundary

Map config, live descriptions, derived rows, install receipts, and generated
status are local-only. Sections persist only after the GUI Snapshot action or
Admin runs `sc snapshot`. NEVER run plain `sc snapshot` from Cartographer; it
is refused. Pass = `sc map finalize` reports Authored sections `PASS` after
Admin acts, without Cartographer mutating snapshot/Git/message/flag state on
their behalf.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'db_map',
  'Supported Subfloor control-plane surfaces, ownership, scope, and `sc mem` commands. Check before reading or writing identity, decisions, roadmap, documents, tasks, or flags.',
  'substrate',
  NULL,
  1,
  '# db_map — use the supported control-plane surfaces

Subfloor control-plane state is already wired to the launched shell. Read and
write it through `sc mem`; the service resolves shell identity and enforces
ownership, caps, immutability, and durable transactions. `sc mem which`
confirms the active identity and API reachability.

The repository catalogue is separate: inspect its `dr_*` objects with
`sc map-schema` and query them with `sc map-sql`. Product runtime data is also
separate and follows the fork application''s code, migrations, dev-kit commands,
and app database connection.

## Read surfaces

```text
sc mem get state
sc mem get seed
sc mem get lns
sc mem get decisions [<id>]
sc mem get flags [<id>] [--feature <id>] [--resolved]
sc mem get narrative
sc mem get messages
sc mem get roadmap
sc mem get projects
sc mem get documents [--feature <id> | --doc <id>]
sc mem get tasks [--feature <id> | --doc <id>]
sc mem get shells
```

Identity surfaces (`state`, `seed`, `lns`, narrative, and messages) resolve as
the calling shell. Decisions are fleet-visible and tagged by author. Planning,
document, task, project, and shell reads are shared coordination surfaces.
Use `--json` only when a supported command consumer needs structured output.

## Write surfaces

Each successful command confirms a durable API write:

```text
sc mem state "…"
sc mem seed "…"
sc mem lns "…" --new
sc mem lns "…" --supersedes <ids>
sc mem retire <entry_id>
sc mem decision "…" --rationale "…" [--parent <id>] [--feature <id> | --doc <id>]

sc mem roadmap add "…" --status <status> --summary "…" [--project <id|shortname>]
sc mem roadmap status <feature_id> <status>
sc mem roadmap project <feature_id> <id|shortname|none>
sc mem roadmap depends <feature_id> [--on <feature_id>]…

sc mem doc add "…" --kind <spec|doc> --feature <id> --body-file <path> --render-path <path>
sc mem doc freeze <document_id>
sc mem task add "…" --feature <id> --doc <id> --seq <n> [--desc "…"]
sc mem task start <task_id>
sc mem task done <task_id>
sc mem task cancel <task_id> --notes "…"

sc mem flag open "…" --name <name> [--priority <priority>] [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority <priority>] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <id|shortname> "…"
sc mem message send <shortname> "…"
sc mem oriented
```

Load the owning skill before a governed write: `memory` for state/identity,
`spec` for roadmap tasks, `docs` for documents, `flags` for blockers, and
`messaging` for shell coordination. Frozen documents require a new document
revision; decisions are superseded with `--parent`; seed entries are retired
rather than rewritten.

## Failure behavior

An unavailable API stops control-plane work; ordinary shells do not fall back
to files or arbitrary queries. A missing supported read/write projection is an
engine gap: report the exact data and use needed, then stop at that boundary.
Admin storage, backup, rebuild, and repair internals live in the Admin-only
`engine_database` skill.',
  0
) ON CONFLICT(name) DO UPDATE SET
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
  `make dos-r`.
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
  'engine_migrations',
  'Maintain Subfloor''s schema baseline, ordered migration ledger, live-DB backup boundary, rebuild/update compatibility, and source-repository migration files. Admin-only by default.',
  'substrate',
  NULL,
  0,
  '# engine_migrations — maintain Subfloor''s database floor

Subfloor owns `.super-coder/schema.sql` as the current baseline and
`.super-coder/migrations/*.sql` as ordered additive deltas. The
`schema_migrations` ledger applies each delta once. `sc rebuild` creates the
baseline, applies every migration, then restores instance content; `sc update`
materializes source and reconciles migrations before the next boot.

## Author in the source repository

Allocate migrations through the collision-safe source command:

```bash
./sc migration new <lowercase_snake_case_slug>
```

Pass = it reports the created next-numbered path and its source-removal
allowlist entry. Keep historical migrations append-only and change `schema.sql`
only when the current baseline itself must describe a new schema object. Never
fold an already shipped delta into the baseline in a way that makes rebuild
apply it twice.

`0001_seed_skills.sql` is the generated exception: update authoritative global
skill assets, run `./sc seed-skills`, and commit the regenerated 0001 body with
the trailing reconciliation migration. Do not hand-edit 0001 or regenerate it
for fork-local skills.

For seeded system content, update the authoritative asset or generator and add
a trailing reconciliation migration. Preserve per-instance rows carried by the
snapshot. Pass = fresh build, in-place migration, and rebuild from an older
snapshot converge to the same state.

## Protect the live instance

The Admin boot names the private instance-state directory. Before an authorized
live migration, load `engine_database` and independently resolve the canonical
database through the state resolver. Require that path to match the boot''s
private state, then use the supported backup-and-apply surface:

```bash
./sc migrate
```

Require its first line, `migrate: db         <absolute-path>`, to match the
independently resolved canonical database exactly. The command then reports the migration
source, creates a WAL-safe backup with a `premigrate` restore point for an
existing DB, and reports each applied filename plus the final count (or
`nothing pending`). Pass = the backup receipt names its restore path before the
first migration applies. A DB-path mismatch stops the operation. The FnB owns
the restart and cutover boundary. Never point engine work at `$DATABASE_URL`;
that variable is for the fork application''s database.

## Verify compatibility

Run the migration on a dirty fixture containing the stale rows it must
reconcile, then run it again. Require:

- one application recorded in `schema_migrations`;
- identical desired state after repeated migration and rebuild;
- preserved shell memory and genuine fork-local content;
- no stale grant, projection, or system row restored by an older snapshot; and
- the running engine healthy after the authorized restart.

Stop before live application when the backup, exact DB path, compatibility
fixture, or FnB maintenance authority is absent.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'fork_skill_design',
  'Design and maintain DB-canonical fork-local skills that describe the fork''s real systems, tools, testing seats, and core processes. Planner-only; use when a capability needs durable shell guidance without becoming global doctrine.',
  'substrate',
  NULL,
  0,
  '# fork_skill_design — describe fork capabilities

Use a fork-local skill when shells need durable knowledge specific to this
repository, stack, host, VM, deployment surface, database, or core fork
process. Keep global skills limited to Subfloor itself, supplied tools and
testing environments, and core Subfloor processes.

## Discover the real capability

Read the repo map, tracked configuration, declared dev-kit hooks, and current
readiness evidence before drafting. Identify:

- the capability and the shells that need it;
- its tracked declaration or owning source;
- the seat, host, VM, service, or database it reaches;
- readiness states and evidence locations;
- authority, recovery, and data-tenancy boundaries; and
- one observable success receipt.

Pass = every operational claim names evidence available in this fork. Do not
infer package managers, test policy, credentials, hosts, or deployment steps.

## Apply the purpose test

Keep a line only when it explains this fork, a supplied tool or testing
environment, or a core fork process. Use an imperative only when variation
would break shared state, authority, compatibility, or recovery. Remove generic
planning, coding, API, test, database, deployment, VM, and troubleshooting
method.

## Draft and persist

Write a Planner-owned draft with a lowercase underscore name and
`common: false`:

```yaml
---
name: repo_capability
description: State the capability and when it fires.
category: substrate
common: false
---
```

Describe locations, commands, states, boundaries, and receipts. A testing-seat
skill identifies the runner, fixtures, reach, readiness, and evidence; it does
not choose assertions. A VM or host skill identifies the supplied control
surface and reset boundary; it does not invent a lifecycle. A deployment or
database skill records the fork''s tracked procedure and authority; it does not
teach generic deployment or SQL technique.

Persist and grant through the supported DB-canonical surface:

```bash
sc skill put --file <path/to/SKILL.md>
sc skill grant <skill_name> <shell>...
sc skill list
```

`put` succeeds only after DB, local snapshot, flat catalogue, and managed skill
projections reconcile. Naming a standard shell changes its shared flavor pack;
naming a Bespoke shell changes only that shell. Creation grants nothing.

A launched Planner seat runs under the restricted execution view and cannot
open the engine DB directly; `sc skill put` then falls back to the engine API''s
Planner-owned skill lane, which runs the identical validation and persistence
server-side. `retire`/`unretire` remain Admin-local: they write the fork''s
tracked retire manifest on the host.

## Update, retire, and recover

```bash
sc skill put --file <path/to/SKILL.md>
sc skill revoke <skill_name> <shell>...
sc skill rm <skill_name>
```

Retry the exact command after fixing a reported snapshot, render, or projection
path. Pass = the full persistence receipt returns and the projected body
matches `sc skill list` plus the intended grant. On a launched seat the same
receipt rides the API fallback, so a failure names which of the four layers
(DB, snapshot, flat render, projection) is still outstanding. `rm` is only for
fork-local names; retire an upstream skill with `sc skill retire <name>` and
restore it with `sc skill unretire <name>` — both from an Admin host seat,
which owns the fork''s tracked retire list.

Keep fork-local skill bodies on the supported `sc skill` surface; do not place
them under engine assets, regenerate the engine seed for them, or set them
common.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git conventions for a super-coder shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK), attribute commits per-shell. Use before any git work.',
  'substrate',
  NULL,
  0,
  '# git — version control, the super-coder way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in super-coder. NEVER commit or edit anything under `.super-coder/`.

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
  'memory',
  'When + how this shell persists memory — current_state (≤300), session narrative, seed (cap 10), L&S (cap 20, ≤500/entry, --supersedes|--new), decisions — all via sc mem, written as it happens, not at close.',
  'substrate',
  NULL,
  1,
  '# memory — write as you go

All memory uses the Subfloor control-plane service; managed flat files are not
a write surface. Write at the moment it matters, never in a close ritual.

Every write goes through `sc mem` and becomes durably visible to other shells.
The service resolves your shell identity; never name a shell in an identity
write.

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
Feature #29 task #171 gate — see doc #44.
Blocked on flag #200. Next: task #172 after the blocker clears.
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
wrote it; if the text opens with an incident timestamp, it is a narrative entry.

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
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'messaging',
  'Shell-to-shell messaging — ordinary `sc mem message` inbox mail by default, with wake delivery only when explicitly instructed. Use to send, check, verify, or mark ordinary messages and to follow a named wake-producing workflow.',
  'substrate',
  'sc mem message',
  1,
  '# messaging — ordinary inbox + explicit wakes

Choose delivery before sending:

| Mode | Use when | Surface |
|---|---|---|
| Normal message | Default for every shell-to-shell message. | `sc mem message` |
| Wake message | The operator or a loaded workflow explicitly instructs a wake. | The exact wake-producing command named by that instruction/workflow. |

Urgency, message kind, or a desire for a prompt response does not authorize a
wake. An explicit wake instruction with no supported command -> surface the
missing capability and stop. Use only the supported message commands; do not
call internal Python or send both modes unless the instruction requires both.

## Normal messages — the shell inbox

Normal messages are shell-to-shell markdown driven by `sc mem message`.
Sender = you; recipient addressed by `shortname`; body preserved verbatim.
The recipient discovers the message on its next boot via the `## STATUS`
`Inbox:` count. `sc mem message send` always sends this normal mode; it never
wakes or rotates a chat.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> [--kind k] | sent | mark-read <id>`

## Message kinds

Every message carries a `kind`, so ordinary mail, delegated tasks, and
completion evidence remain independently filterable:

- `shell` — ordinary shell-to-shell mail (the default; what `send` does
  unless told otherwise).
- `task` — a bounded instruction for another shell.
- `result` — worker → planner completion or transition report.

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

Read-only — it does NOT auto-mark-read. Non-`shell` rows show their kind
inline. Surface the body to the operator (reply if warranted — a reply is
itself a `send`), then `mark-read` the inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>" [--kind shell|task|result]
```

- Multi-word body = one quoted argument; markdown preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send plan1 "feature 12 task 3 complete (PR #41)" --kind result`
- `cartographer` is a **role alias**: when no shell has that literal
  shortname, it resolves to the fork''s cartographer shell whatever its
  shortname (e.g. `CART1`). Address the map-keeper as `cartographer` — no
  shortname lookup needed. An exact shortname match always wins.
- Unknown / deleted recipient -> `mem: recipient shortname ''<x>'' unknown`;
  empty body -> `mem: body is empty`. Surface either to the operator plainly.
- Sends are idempotent under load: each invocation carries a dedupe key, so
  a timed-out send retries itself and can never write a duplicate. Do NOT
  re-run a timed-out send by hand — the retry already happened; if it still
  died, check `sent` first.

## sent — your outbound view

```
sc mem message sent           # latest 50 you sent, newest first, read receipts
```

Verify delivery after an ambiguous failure (a send that died after its
retries) before ever resending. A row present = delivered; absent = safe
to resend.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Pass the `message_id` that `check` surfaced. Only messages addressed to you
clear — another shell''s message = no-op; re-marking a read message = no-op.

## Wake messages — active chat delivery

A wake message creates durable delivery intent for the recipient''s active
chat. Pending wakes coalesce per receiver; one wake turn drains every
undelivered wake message for that shell. A wake does not enter the normal
`sc mem message` inbox or `sent` view.

Use only the wake type selected by the instruction/workflow:

| Recipient state | Delivery result |
|---|---|
| Verified live turn | Re-enter at the turn''s natural boundary. |
| Idle registered chat | Any coalesced New rotates; all Re-enter resumes. |
| No registered chat | Create a chat and deliver as New. |

Engine-wide wakes need no Sprint. Sprint-scoped wakes deliver only while that
Sprint is armed; a producer may create delivery intent earlier and leave it
queued. Typed Sprint commands and engine producers return their durable
message/wake receipt. Receipt present = complete; do not add a normal-message
duplicate.

## Stance

- On boot, `Inbox:` non-zero -> run `--message check` and surface the first
  item before continuing.
- No threading: a reply = a new `send`; include `Re: <topic>` in the body if
  it matters.
- `mark-read` only after you have actually acted on the message.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'onboard',
  'One-time, FnB-supervised ingest of a repo''s EXISTING docs/specs into the DB + roadmap backfill — the only time content flows file→DB. Run once after bootstrap on a fork with existing documentation. Planning shell''s job.',
  'substrate',
  NULL,
  0,
  '# onboard — ingest the repo''s existing docs (once, with the FnB)

Run once, after `bootstrap`, on a fork that has existing documentation —
FnB-supervised. Brings the repo''s *existing* docs into the DB so the GUI shows
real content and the roadmap reflects what''s already there. This is the ONE
legitimate file→DB direction; after it the DB owns content and the flow is
DB→flat only — re-importing = drift. `<self>` = your shell_id.

## 1. List what exists — from the map, not a blind walk
```sql
-- the map is its own db: sc map-sql "<query>"
SELECT path, lang, lines FROM dr_filepath WHERE role=''doc'' ORDER BY path;
```
These are the repo''s real docs (README, `docs/`, `specs/`, guides). NEVER
ingest `_sc` dirs — those are OUR render output.

## 2. Read + classify, with the FnB
Read each doc; decide together:
- **spec** = describes a feature / planned work -> tie to a roadmap feature.
- **doc** = reference / guide / overview (README, CONTRIBUTING) -> general, no
  feature.
Skip noise (changelogs, license, vendored docs) unless the FnB wants it.

All writes below go through `sc mem` to durable shared control-plane state; the
import never touches the app DB.

## 3. Backfill the roadmap
Create one feature per coherent area/initiative the docs imply; status by how
built it is: `shipped` = done + documented, `near_term`/`brainstorm` = planned.
```
sc mem roadmap add "…" --status shipped --summary "…"
```

## 4. Ingest into `documents` (DB owns the body)
`--body-file` reads the real file straight into the body — no pasting:
```
# general doc (no feature):
sc mem doc add "README" --kind doc --body-file ./README.md --render-path docs_sc/readme.md
# a feature''s spec (link it):
sc mem doc add "…" --kind spec --feature <id> --body-file ./path/to/spec.md --render-path specs_sc/….md
```
Spec describes shipped work -> freeze it: `sc mem doc freeze <document_id>`.

## 5. Persist
Each confirmed `sc mem` write is live in the shared control plane immediately -> the GUI''s
Docs/Roadmap tabs reflect the import as you go. Flat `_sc` copies + git commit
= an admin/GUI publish step, not part of onboarding.

## 6. The host''s original files — three exits (optional; coexist by default)
The DB now holds the canonical copy; renders go to `_sc/`, so originals never
collide. Offer the FnB:
- **freeze** — leave the original files as-is (default).
- **archive** — move them to an abandoned branch, drop from `main`.
- **delete** — remove them (the DB has them).

## Stance
Ingest once. After onboarding: author via the shell/GUI, render DB→flat. NEVER
edit the flat files or re-import them.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'surface_catalogue',
  'Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Query first, lazy-load the few files it points at. Use to orient in an unfamiliar repo fast.',
  'substrate',
  NULL,
  1,
  '# surface_catalogue — read the repo from the map, not by grepping

The `dr_*` catalogue is a scan of the host repo. Query it first to orient, not
the tree. It is separate from Subfloor control-plane memory and from the
product''s runtime database. Inspect structure with `sc map-schema`; query data
with `sc map-sql "…"`.

NEVER map the repo yourself. The map stays fresh automatically (git hooks
re-map on pull / branch-switch / rebase) and is owned by the **cartographer**
shell. Empty / stale / wrong map -> flag the cartographer, don''t re-map.

| Table | Holds |
|---|---|
| `dr_repo` | the repo: name, root, remote, vcs, default_branch, file_count, mapped_at |
| `dr_section` | the navigational index: `name`, `path_prefix`, `description` — "UI here / API here / docs here". Rendered in the boot `## CONNECTIONS` block; start here. |
| `dr_filepath` | one row per file: `path`, `ext`, `lang`, `role` (code/doc/config/test/asset/env), `bytes`, `lines`, `desc` (cartographer one-liner, NULL until curated) |
| `dr_dependency` | deps from the manifests: `manager` (npm/pip/poetry/go/cargo), `name`, `version`, `kind`, `source_file` |
| `dr_env` | env-var names found in `.env.*` example files: `name`, `source_file` |
| `dr_endpoint` | HTTP routes: `method`, `path`, `handler` (file:line), `framework`, `source_file` |
| `dr_db_table` / `dr_db_column` | the app DB schema: tables/views + their columns (`type`, `pk`, `not_null`) |
| `dr_route` / `dr_component` | UI routes (`path`, `kind`) + components (`name`, `path`) |

First five = mapped on EVERY repo. Last three = the semantic layer, populated
only when the cartographer wired an extractor for this repo''s stack (see the
`cartographer` skill). Empty `dr_endpoint` = no extractor wired, NOT "no
endpoints" — check before relying on it; flag the cartographer if a dimension
you need is missing.

## Orient fast

Boot `## CONNECTIONS` already shows the section index. Flow: pick a section
there -> query that section''s leaves (file names + descriptions) -> read the
one or two files you need. Section-first, one cheap query deep — never a full
preload.

Run `sc map-schema` before the first structural query; pass = it lists the
expected `dr_*` object. Run `sc map-schema <dr_table>` before using unfamiliar
columns; pass = ordinal/name/type/nullability/default/PK + indexes are explicit.
Use `sc map-sql` only for data queries.

```sql
-- all of these run against the map db:  sc map-sql "<query>"
-- the section index (same as boot CONNECTIONS) — where to start:
SELECT name, path_prefix, description FROM dr_section ORDER BY sort_order, name;

-- a chosen section''s leaves — the descriptions tell you which file to open:
SELECT path, desc, lines FROM dr_filepath
WHERE path LIKE ''shell_core/api/%'' ORDER BY path;

-- the synthetic Repository Root group (not an authored dr_section row):
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, ''/'') = 0 ORDER BY path;

-- what is this repo + how big:
SELECT name, default_branch, file_count, mapped_at FROM dr_repo;

-- language mix:
SELECT lang, COUNT(*) n, SUM(lines) lines FROM dr_filepath
WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;

-- where the code lives (skip docs/config/assets):
SELECT path, lang, lines FROM dr_filepath WHERE role=''code'' ORDER BY lines DESC;

-- find files by area (the map is the index; grep only what it points at):
SELECT path FROM dr_filepath WHERE path LIKE ''%auth%'';

-- stack + config surface:
SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
SELECT name, source_file FROM dr_env ORDER BY name;

-- semantic layer (only if an extractor is wired for this repo — see cartographer):
SELECT method, path, handler FROM dr_endpoint ORDER BY path;            -- the API surface
SELECT name, kind, source_file FROM dr_db_table ORDER BY name;          -- the app DB schema
-- table_name is a string ref (cache; no FK): schema + migration files each
-- contribute their own copy of a table''s columns — select source_file and
-- read one source''s rows, or expect duplicates:
SELECT source_file, name, type, pk, not_null FROM dr_db_column
WHERE table_name=''users'' ORDER BY source_file;
SELECT path, kind, file FROM dr_route ORDER BY path;                    -- UI routes
```

## Stance

- **Map first, grep second.** Query `dr_filepath` for the handful of files
  that matter, then read those — NEVER `grep -r` the whole tree.
- **Lazy-load.** Pull a file''s contents only once the map points at it. Carry
  the map, not the territory.
- **Map looks wrong?** Empty, stale (repo changed since `mapped_at`),
  mis-classified, a nested file under "other / unsectioned", or a `desc IS
  NULL` where you needed one -> Cartographer worklist item. Root files belong
  to `Repository Root`, not the unsectioned worklist. Flag the gap; don''t
  author the map yourself.
- **Semantic layer when wired.** Endpoints / DB schema / UI routes let you
  jump straight to the API surface or schema; a dimension is empty -> fall
  back to section + descriptions. Symbol-level semantics (functions/classes)
  are a later pass.',
  0
) ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT 'admin', skill_id FROM skills WHERE name='engine_database' AND is_deleted=0;

COMMIT;
