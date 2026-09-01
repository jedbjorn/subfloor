-- 0245 — teach shells the supported active-spec feature split workflow.
-- The API/CLI implementation needs no schema change: documents, spec_tasks,
-- and document-linked decisions already carry the ownership columns. These
-- idempotent replacements run after 0243's full-body skill publication, so the
-- old text is a stable migration input on every upgrade path.

BEGIN;

UPDATE skills SET content=REPLACE(
  content,
  'sc mem doc add "…" --kind <spec|doc> --feature <id> --body-file <path> --render-path <path>
sc mem doc freeze <document_id>
sc mem task add "…" --feature <id> --doc <id> --seq <n> [--desc "…"]',
  'sc mem doc add "…" --kind <spec|doc> --feature <id> --body-file <path> --render-path <path>
sc mem doc freeze <document_id>
sc mem doc move <document_id> --feature <target_feature_id>
sc mem task add "…" --feature <id> --doc <id> --seq <n> [--desc "…"]'
)
WHERE name='db_map';

UPDATE skills SET content=REPLACE(
  content,
  'revision; decisions are superseded with `--parent`; seed entries are retired
rather than rewritten.',
  'revision; decisions are superseded with `--parent`; seed entries are retired
rather than rewritten. `doc move` preserves one unfrozen spec''s identity,
tasks, and document-linked decisions while reassigning them atomically to an
active feature; it refuses frozen, ordinary-doc, terminal-target, and
Sprint-bound moves.'
)
WHERE name='db_map';

UPDATE skills SET content=REPLACE(
  content,
  'feature, each a `documents (kind=''spec'')` row, ordered by `seq`. No
feature-to-feature links; no second roadmap row for related work — related
work = another spec under the same feature. Freeze = the ship-time record of
what was built to; it never gates the feature''s other specs.',
  'feature, each a `documents (kind=''spec'')` row, ordered by `seq`. No
feature-to-feature links; related work within one mental model is another spec
under the same feature. A genuinely new era may split into a fresh feature by
moving its unfrozen active spec through the guarded workflow below. Freeze =
the ship-time record of what was built to; it never gates the feature''s other
specs.'
)
WHERE name='docs';

UPDATE skills SET content=REPLACE(
  content,
  'The **doc** (`kind=''doc''`) = the feature''s readable face — write it when the
first spec ships, under the same `feature_id`. Sibling of the specs, not a
parent.

## Assess the work-stream on every feature',
  'The **doc** (`kind=''doc''`) = the feature''s readable face — write it when the
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

## Assess the work-stream on every feature'
)
WHERE name='docs';

UPDATE skills SET content=REPLACE(
  content,
  'sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
sc mem state "[<feature>] — last: <last_done>. next: <next_up>."
```

Final Verification follows',
  'sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
sc mem state "[<feature>] — last: <last_done>. next: <next_up>."
```

Cancellation applies when work is copied or replanned into a different spec.
When the existing unfrozen spec itself starts a fresh feature era, use
`sc mem doc move <document_id> --feature <target_feature_id>` instead: the
document, its task ledger, and its document-linked decisions move atomically,
so their identities and statuses remain intact. Follow the `docs` split
workflow to verify the target and annotate the historical feature.

Final Verification follows'
)
WHERE name='spec';

COMMIT;
