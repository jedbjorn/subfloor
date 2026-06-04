---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# docs

Author or review docs & specs in super-coder. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.

**Category:** substrate

---

# docs — author & review documents

In super-coder the **DB owns document bodies** — never loose `.md` files. A
`documents` row is the source; `./sc render` writes the read-only flat copy to
`specs_sc/` / `docs_sc/`, and the GUI opens it rendered in md-converter.

| kind | lives on | meaning |
|---|---|---|
| `spec` | the **Roadmap** (the dev cycle) | the working/founding spec for a feature; **freezes on ship** |
| `doc` | the **Docs** tab | documentation; not part of the spec lifecycle |

`<self>` = your shell_id.

## Review first

Before writing, see what exists — don't duplicate:
```sql
SELECT document_id, feature_id, kind, seq, title, frozen FROM documents ORDER BY feature_id, kind, seq;
SELECT path FROM dr_filepath WHERE role='doc';   -- the repo's own docs (surface_catalogue)
```

## Author

```sql
-- a doc against a feature (kind='doc'); DB owns the body:
INSERT INTO documents (feature_id, kind, seq, title, body, render_path)
VALUES (?, 'doc', 1, '…', '…', 'docs_sc/….md');

-- a feature's next spec stage (kind='spec', new seq):
INSERT INTO documents (feature_id, kind, seq, title, body, render_path)
VALUES (?, 'spec', 2, '…', '…', 'specs_sc/….md');
```
Then `./sc render` (writes the `_sc` file + injects feature/roadmap_status/frozen
into its frontmatter) and `./sc snapshot` (the body is per-instance content).

## Freeze on ship

A spec freezes when its stage ships — immutable thereafter; open the **next** seq
for the next stage, never edit a frozen one:
```sql
UPDATE documents SET frozen=1, frozen_date=date('now') WHERE document_id=?;
```
The GUI and the render layer both refuse edits to frozen docs.

## View

Open any doc rendered: the GUI's "open in md-converter ↗" (Roadmap card or Docs
tab) — the body rides in the URL, no upload. For long-form authoring, write the
markdown to the `body` and let the render + md-converter handle presentation.
