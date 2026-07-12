---
name: onboard
description: One-time, FnB-supervised ingest of a repo's EXISTING docs/specs into the DB + roadmap backfill — the only time content flows file→DB. Run once after bootstrap on a fork with existing documentation. Planning shell's job.
category: substrate
common: false
---

# onboard — ingest the repo's existing docs (once, with the FnB)

Run once, after `bootstrap`, on a fork that has existing documentation —
FnB-supervised. Brings the repo's *existing* docs into the DB so the GUI shows
real content and the roadmap reflects what's already there. This is the ONE
legitimate file→DB direction; after it the DB owns content and the flow is
DB→flat only — re-importing = drift. `<self>` = your shell_id.

## 1. List what exists — from the map, not a blind walk
```sql
-- the map is its own db: sc map-sql "<query>"
SELECT path, lang, lines FROM dr_filepath WHERE role='doc' ORDER BY path;
```
These are the repo's real docs (README, `docs/`, `specs/`, guides). NEVER
ingest `_sc` dirs — those are OUR render output.

## 2. Read + classify, with the FnB
Read each doc; decide together:
- **spec** = describes a feature / planned work -> tie to a roadmap feature.
- **doc** = reference / guide / overview (README, CONTRIBUTING) -> general, no
  feature.
Skip noise (changelogs, license, vendored docs) unless the FnB wants it.

All writes below go through `sc mem` -> live shared engine DB; the import never
touches the app DB.

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
# a feature's spec (link it):
sc mem doc add "…" --kind spec --feature <id> --body-file ./path/to/spec.md --render-path specs_sc/….md
```
Spec describes shipped work -> freeze it: `sc mem doc freeze <document_id>`.

## 5. Persist
Each `sc mem` write is live in the shared engine DB immediately -> the GUI's
Docs/Roadmap tabs reflect the import as you go. Flat `_sc` copies + git commit
= an admin/GUI publish step, not part of onboarding.

## 6. The host's original files — three exits (optional; coexist by default)
The DB now holds the canonical copy; renders go to `_sc/`, so originals never
collide. Offer the FnB:
- **freeze** — leave the original files as-is (default).
- **archive** — move them to an abandoned branch, drop from `main`.
- **delete** — remove them (the DB has them).

## Stance
Ingest once. After onboarding: author via the shell/GUI, render DB→flat. NEVER
edit the flat files or re-import them.
