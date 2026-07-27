-- 0112 — reseed sprint_orchestration_close: the board is a record, not a source.
-- Sprint 84 U6 (spec #76, H-23/H-24). Generated from
-- assets/skills/sprint_orchestration_close/SKILL.md; full-body upsert so
-- existing installations converge on the asset.
--
-- Single-skill reseed by planner ruling: U5's full-chain reseed (0114) is
-- authored later and is idempotent over this one. Editing the asset without
-- this migration is exactly the drift tests/test_skills_freshness.py detects.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_orchestration_close',
  'Close a sprint after every unit is terminal and main is green. Run an independent spec-to-main conformance pass, route findings while authority remains active, close and freeze the sprint document, verify wake and PR-watch cleanup, synthesize the report, and settle flags and roadmap state. Load only at the close trigger.',
  'craft',
  NULL,
  0,
  '# sprint_orchestration_close

Close from integrated evidence. Keep the sprint ACTIVE until conformance
findings are ruled.

Take every fact from structure. The `sprint_units`, `documents`, watch and
message rows are the record. A persisted render — a report body, a `status:`
line, a board pasted into a document — is display. `sc sprint board` and the
read-only GUI render the rows at call time, so they carry the record.

NEVER read a fact out of a persisted render, and NEVER add a close step that
parses one -> a step that reads a display passes while the record disagrees,
and the prose parsers this sprint removed all began as one convenient read.

## Confirm the close trigger

Read:

```sh
./sc sprint board --sprint <doc-id>
./sc sprint status
./sc watch list --all
```

Confirm:

- every unit is `merged` or `cancelled`;
- every merged unit has a structured unit report;
- every assigned review ended `review-clean` at a known head;
- all required checks on `main` are green;
- the sprint document remains unfrozen.

Confirm the conformance reviewer and its conflict set were declared at sprint
setup. The set names units authored, unit reviews performed, rulings supplied,
and any other overlap with conformance scope. Do not choose a reviewer at the
freeze gate after every available shell has reviewed its own evidence; return to
orchestration until an independent reviewer is reserved or the FnB explicitly
disposes each conflict.

If any condition fails, return to `sprint_orchestration` or
`sprint_orchestration_recover`.

## Run conformance

Assign an independent reviewer to compare the governing spec with integrated
`main` at one recorded SHA.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Send:

```sh
./sc mem message send <reviewer> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
./sc run <reviewer> --harness <review-harness> -m <review-model> --effort high
```

State the sections and units included. For decision-driven units without a spec,
name the alternate evidence: unit report, exact-head review, and mutation check.

Ask the tense question across the whole spec at the freeze SHA. Re-verify every
present-tense code claim, beginning with the problem statement and corrections.
Rewrite historical claims in past tense with their ref, and qualify current
provenance with the exact SHA it verifies.

The reviewer writes `CONFORMANCE: <sprint title>` with one verdict per
requirement:

- `as-specced`;
- `deviated-intentionally`;
- `deviated-silently`;
- `unimplemented`.

Route findings while the sprint is ACTIVE:

- Major: add a fix unit and rerun affected conformance.
- Medium: add a fix unit or record an explicit FnB-approved deferral.
- Low: add an actionable follow-up.

**Conformance pass condition:** every scoped requirement has a verdict and every
finding has a disposition.

## Revoke sprint authority

Drain the scoped inbox immediately before freezing the sprint document. An
earlier inbox check does not satisfy this gate.

Closing a sprint **is** freezing its doc. Freeze it, and edit nothing:

```sh
./sc mem doc freeze <doc-id>
```

NEVER close a sprint by editing its body to `status: CLOSED`. A sprint is live
iff its `documents` row has `kind=''doc''`, a `SPRINT:%` title, `frozen = 0`, and
at least one `sprint_units` row — the `status:` line is display prose that no
liveness reader consults. A body edit therefore releases no binding, cancels no
wake queue and retires no watch, while reading to a human as a closed sprint.
The freeze is what is atomic with binding release and queue cancellation.

Freezing ends sprint merge and direct-handoff authority -> verify:

```sh
./sc sprint status --all
./sc watch list --all
```

Every binding for the sprint must be released and every sprint PR watch retired.
Resolve any surviving watch by identifying its open or mis-scoped PR.

Send participants an ordinary `shell` message announcing closure and restoration
of default gates. Scoped task/result messages require an ACTIVE sprint, so closure
notices are intentionally unscoped.

## Write the sprint report

Create one `SPRINT REPORT: <title>` document. Synthesize the structured board,
unit reports, event trail, review verdicts, and conformance document into:

```markdown
## Verdict
## Final Board
## Units Shipped
## Judgements Made
## Spec Accuracy
## Issues Encountered
## Deferred & Follow-ups
## Spec Debt
## Metrics
```

Build `Final Board` from the `sprint_units` rows, one row per unit, columns
`seq · title · dev · reviewer · terminal state · PR · review head`. No live
board file exists on disk, so the frozen report is the only durable copy of the
board -> a unit missing from this table has no record anywhere.

NEVER assemble that table from a board render, a draft report or a unit report''s
prose. Those are display; the rows are the record, and the two diverge exactly
when a late board write lands after someone copied the display.

Use `Verdict` for the five-second answer: unit and PR count, conformance state,
green-main state, cancellations, and explicit deferrals. Cross-check unit
`deviations` against conformance verdicts and state the resulting synthesis.

Persist the report:

```sh
./sc mem doc add "SPRINT REPORT: <title>" --kind doc --body-file <report.md>
```

Add a shared copy only when the fork''s artifact policy or FnB requests one.

## Settle bookkeeping

- Before closing a flag by number, resolve and read back its exact `flag_id`.
  Display names and flag IDs share an integer range; never infer one from the
  other.
- Close flags resolved by the sprint with how-they-were-resolved notes.
- Advance the linked roadmap feature to its earned state.
- Open flags for actionable deferred work.
- Record spec debt where the implementation exposed a missing or wrong rule.
- Give each deferral an owner and an observation that will surface it again:
  query, metric, marker, failing test, or alert. Record an unmeasurable
  acceptance as a decision with rationale.
- Tell the FnB the sprint doc id, report doc id, conformance verdict, and
  remaining follow-ups.

**Close completion:** the sprint doc is frozen, bindings are released, watches
are retired, the report exists, and every finding or follow-up has an owner or
explicit disposition.',
  0)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
