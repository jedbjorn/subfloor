---
name: onboard
description: One-time, FnB-supervised ingest of a repo's EXISTING docs/specs into the DB + roadmap backfill. The only time content flows file→DB. Run once after bootstrap on a fork that has existing documentation. Planning shell's job.
category: substrate
common: false
---

# onboard — ingest the repo's existing docs (once, with the FnB)

After `bootstrap` (you've oriented), this brings the repo's *existing*
documentation **into the DB** so the GUI shows real content and the roadmap
reflects what's already there. This is the **one** legitimate file→DB direction
— a supervised, one-time import. After it, the DB owns content; it's DB→flat
only (or the drift we're killing comes back). `<self>` = your shell_id.

## 1. List what exists (from the map, not a blind walk)
```sql
SELECT path, lang, lines FROM dr_filepath WHERE role='doc' ORDER BY path;
```
These are the repo's real docs (README, `docs/`, `specs/`, guides). The `_sc`
dirs are OUR render output — never ingest those.

## 2. Read + classify, with the FnB
For each doc, read the file and decide together:
- **spec** — describes a feature / planned work → tied to a roadmap feature.
- **doc** — reference / guide / overview (README, CONTRIBUTING) → general, no
  feature.
Skip noise (changelogs, license, vendored docs) unless the FnB wants it.

## 3. Backfill the roadmap
Create a feature for each coherent area/initiative the docs imply; set status by
how built it is (`shipped` if done + documented, `near_term`/`brainstorm` if
planned):
```sql
INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary)
VALUES ('…', 'shipped', 0, <self>, '…');
```

## 4. Ingest into `documents` (DB owns the body)
```sql
-- general doc (no feature):
INSERT INTO documents (kind, seq, title, body, render_path)
VALUES ('doc', 1, 'README', '<file contents>', 'docs_sc/readme.md');
-- a feature's spec (link it):
INSERT INTO documents (feature_id, kind, seq, title, body, render_path)
VALUES (?, 'spec', 1, '…', '<file contents>', 'specs_sc/….md');
```
Paste the file's real contents into `body`. If a spec describes shipped work,
freeze it (`frozen=1, frozen_date`).

## 5. Render + persist
`./sc render` (writes the `_sc` copies + frontmatter) then `./sc snapshot` (the
bodies are per-instance content). Now the GUI's Docs/Roadmap tabs show the repo.

## 6. The host's original files — three exits (optional; coexist by default)
The DB now holds the canonical copy. Because we render to `_sc/`, the originals
never collide — so **coexist (freeze)** is the default. Offer the FnB:
- **freeze** — leave the original files as-is (default).
- **archive** — move them to an abandoned branch, drop from `main`.
- **delete** — remove them (the DB has them).

## Stance
Ingest is **once**. Don't re-ingest (drift). After onboarding, author via the
shell/GUI and render to flat — never edit the flat files or re-import them.
