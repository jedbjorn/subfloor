---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# onboard

One-time, FnB-supervised ingest of a repo's EXISTING docs/specs into the DB + roadmap backfill. The only time content flows file→DB. Run once after bootstrap on a fork that has existing documentation. Planning shell's job.

**Category:** substrate

---

# onboard — ingest the repo's existing docs (once, with the FnB)

After `bootstrap` (you've oriented), this brings the repo's *existing*
documentation **into the DB** so the GUI shows real content and the roadmap
reflects what's already there. This is the **one** legitimate file→DB direction
— a supervised, one-time import. After it, the DB owns content; it's DB→flat
only (or the drift we're killing comes back). `<self>` = your shell_id.

## 1. List what exists (from the map, not a blind walk)
```sql
-- the map is its own db: sc map-sql "<query>"
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

All writes here go through `sc mem` (routed through the engine API, which writes
to the live shared DB — the import never touches the app DB).

## 3. Backfill the roadmap
Create a feature for each coherent area/initiative the docs imply; set status by
how built it is (`shipped` if done + documented, `near_term`/`brainstorm` if
planned):
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
If a spec describes shipped work, freeze it: `sc mem doc freeze <document_id>`.

## 5. Persist
Each `sc mem` write is live in the shared engine DB immediately, so the GUI's
Docs/Roadmap tabs reflect the import as you go. The flat `_sc` copies and the git
commit are an admin/GUI publish step — not part of onboarding.

## 6. The host's original files — three exits (optional; coexist by default)
The DB now holds the canonical copy. Because we render to `_sc/`, the originals
never collide — so **coexist (freeze)** is the default. Offer the FnB:
- **freeze** — leave the original files as-is (default).
- **archive** — move them to an abandoned branch, drop from `main`.
- **delete** — remove them (the DB has them).

## Stance
Ingest is **once**. Don't re-ingest (drift). After onboarding, author via the
shell/GUI and render to flat — never edit the flat files or re-import them.
