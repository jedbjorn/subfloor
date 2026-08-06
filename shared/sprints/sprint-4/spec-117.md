#117 spec seq 17 · feature 31 — Optional pre-Sprint QA/QC

---
title: Optional pre-Sprint QA/QC
tags: [sprints, qaqc, launch, schema]
date: 2026-08-05
project: super-coder
purpose: Make pre-Sprint review advisory
---
# Optional pre-Sprint QA/QC

## Objective

Remove pre-Sprint QA/QC from declaration and arming eligibility so the FnB may launch a Sprint with or without review evidence. The change is done when a Planner can bind a current spec directly, declare and arm with zero QA/QC rows, and the same path still succeeds when matching, stale, or failing review records exist.

> [!class1]
> Decision #104 governs: review is evidence only. Its absence, verdict, age, signer state, findings, or revision match never blocks Sprint declaration or arming.

Exact governing-spec identity and revision remain hard scope facts. A Sprint must bind at least one existing non-empty `spec` document belonging to its feature, and arming still refuses if the bound body changed after declaration.

## Prior Contract

Sprints v2 currently makes passing review a launch credential in three layers:

- `.super-coder/migrations/0146_sprint_v2_domain.sql` defines `sprint_specs.approval_id` as `NOT NULL`.
- `.super-coder/api/server.py` declares a Sprint only from `spec_approval_ids` and rejects fail, stale, wrong-feature, or inactive-reviewer records.
- `.super-coder/scripts/sprint_domain.py` joins every bound spec to a passing approval during arming.

The CLI makes `--spec-approval` required, `sprint_close.py` inner-joins the approval table, and `sprint_prep` teaches the same gate. This spec supersedes only those pre-Sprint review requirements in spec #46. It does not weaken exact spec binding, task membership, participant capacity, merge grants, work-unit review, green-check merge authorization, or closeout behavior.

No active decision directly owns the old pre-Sprint gate. Decision #104 is therefore additive rather than a parent-chain supersession. Decisions #93 and #96 concern implementation review and Sprint completion; they remain unchanged.

## Required Behavior

### Direct spec binding

`sc sprint declare` gains repeatable `--spec <document-id>` selectors. The authenticated declaration endpoint gains `spec_document_ids`, an array of document ids.

Inside the existing declaration transaction, the server:

1. Resolves the selected documents.
2. Requires at least one unique document after normalization.
3. Requires every selected document to be a non-empty `spec` belonging to the declared feature.
4. Hashes each current body and inserts that hash into `sprint_specs.bound_revision_sha256`.
5. Creates the prepared Sprint, participants, and declaration event atomically as today.

The client does not submit a revision hash. The server derives it from the body read in the same transaction that creates the binding.

### Review evidence

`sprint_spec_approvals` and `sc sprint record-qaqc` remain available. Existing review rows remain immutable historical evidence. Their verdict, revision, findings, signer, and time remain inspectable through existing memory and reporting surfaces.

Review evidence has no eligibility effect:

- zero records is valid;
- `fail` is valid;
- unresolved findings are valid;
- a review of an older body is valid historical evidence;
- a deleted or changed Reviewer shell does not affect launch;
- adding a review before or after declaration does not alter the bound revision.

`sprint_specs.approval_id` becomes nullable as a compatibility evidence pointer. Arming must not join through it or interpret it. Renaming the legacy table or column is outside this patch because it would add migration and downstream churn without changing policy.

### Compatibility input

The existing repeatable `--spec-approval` flag and `spec_approval_ids` payload remain accepted for compatibility. They are deprecated selectors, not approval gates:

- each known id resolves its document;
- the server binds the document's current body, not the review's historical hash;
- any verdict or revision age is accepted;
- the referenced row MUST be stored in nullable `sprint_specs.approval_id` for evidence;
- direct and compatibility selectors are deduplicated by document id;
- more than one compatibility review for the same document is rejected as ambiguous input, and the caller can use `--spec` instead.

An unknown document or review id remains an input error. That is resource validation, not review eligibility. At least one selector across `spec_document_ids` and `spec_approval_ids` is required.

### Arming

The arming transaction validates bound specs without consulting `sprint_spec_approvals`. It requires:

- at least one `sprint_specs` row;
- each document still exists, is a `spec`, belongs to the Sprint feature, and has a body;
- the current body hash equals `bound_revision_sha256`.

A post-declaration body edit still blocks first arming because the planned scope changed. The Planner may abort and redeclare, or the implementation may expose a prepared-plan rebind path only if one already exists; this patch does not invent silent rebinding.

Resume behavior is unchanged: an already armed Sprint continues against its bound revision and reports later drift.

## API Contract

The existing `POST /_sc/sprint/declare` route remains the declaration resource action. This patch adds fields rather than versioning or renaming the endpoint.

```json
{
  "feature_id": 31,
  "planner_shell_id": 10,
  "spec_document_ids": [117],
  "spec_approval_ids": [],
  "participants": [
    {"shell_id": 10, "role": "planner", "harness": "codex"},
    {"shell_id": 11, "role": "developer", "harness": "codex"},
    {"shell_id": 12, "role": "reviewer", "harness": "codex"}
  ],
  "merge_grant_enabled": true
}
```

`spec_document_ids` is canonical. `spec_approval_ids` is optional and deprecated. Unknown fields retain the endpoint's current boundary policy.

Response and error behavior:

| Condition | Result |
|---|---|
| Valid direct or compatibility selector set | Existing success response |
| Neither selector contains an id | `400` bad input |
| Unknown document or review id | `404` not found |
| Non-spec, empty-body, or wrong-feature document | `409` invariant conflict |
| Duplicate compatibility reviews for one document | `400` ambiguous input |
| Any review verdict, staleness, findings, or signer state | No error |

The `sprint.declared` event adds canonical `spec_document_ids` while retaining `spec_approval_ids` when compatibility input was supplied. The board's event whitelist must expose the new field; historical events remain readable.

## Data Migration

Add the next migration, numbered `0185`; it alone carries this post-baseline change. Do not modify `.super-coder/schema.sql`: `sprint_specs` is introduced by migration `0146`, and the additive migration chain must remain its sole definition path.

The migration rebuilds `sprint_specs` with the same primary key, foreign keys, hash check, and `included_at`, but permits `approval_id IS NULL`. Existing rows and approval references are copied unchanged. The migration must run transactionally with foreign-key integrity restored and verified.

Required migration cases:

- a fresh database creates the nullable schema;
- a populated pre-change database preserves every Sprint binding and review pointer;
- rebuild-from-snapshot succeeds with reviewed and unreviewed bindings;
- rerunning normal migration discovery does not reapply or partially rebuild the table;
- a failure before commit leaves the original table intact.

No existing review row is deleted or rewritten.

## Projections and Reports

`.super-coder/scripts/sprint_close.py` must use a left join for optional evidence. Every bound spec appears in the evidence packet whether or not `approval_id` exists. When evidence exists, report its verdict, reviewed revision, signer, findings reference, and timestamp without labeling it an eligibility result.

`.super-coder/scripts/sprint_board.py` continues to show every bound document and revision. Its declared-event projection accepts `spec_document_ids`. A larger board redesign or review-status badge is not required; the document QA/QC surface already exposes the historical records.

Snapshot allowlists retain both tables. Optional foreign-key values must serialize and rebuild without being converted to zero or omitted rows.

## Guidance Changes

Update the authoritative `.super-coder/assets/skills/sprint_prep/SKILL.md` and reseed it through a migration.

The skill must teach:

- FnB decides whether pre-Sprint QA/QC is requested;
- review is optional evidence and never an eligibility blocker;
- the Planner binds specs with `--spec` and may proceed regardless of recorded verdicts or findings;
- direct current-spec hashing replaces approval-derived binding;
- the final check still verifies exact spec revision, task coverage, participants, routes, capacity, single-armed constraints, and merge grant;
- the handoff reports whether review was performed and summarizes available evidence without treating it as authorization.

Remove wording such as "qualifying approval," "use fail until," and "refuse arming when approval is absent." Preserve all work-unit review and conformance instructions outside pre-Sprint QA/QC.

Historical seed migrations remain immutable. Only the authoritative asset and a new reseed migration change.

## Construction Plan

Implementation base is `main` including merged PR #1057; do not stack this change on an unmerged #1057 head.

```linear
Migrate nullable evidence link :::class1 -> Bind specs directly :::class1 -> Remove arm gate :::class2 -> Repair reports :::class2 -> Reseed guidance :::class3 -> Run adversarial gate :::class3
```

1. **Schema foundation** — add migration `0185` for the transactional `sprint_specs` rebuild without changing `schema.sql`, and add migration/snapshot tests. Verify reviewed rows survive and new null evidence rows rebuild.
2. **Direct declaration slice** — add `spec_document_ids`, CLI `--spec`, canonical current-body hashing, selector deduplication, and the deprecated compatibility adapter in `server.py` and `sprint_cli.py`. Verify zero-review declaration end to end through the authenticated CLI.
3. **Arming and reporting** — remove review joins and verdict checks from `_validate_arm_plan`; make close evidence tolerate nullable review links; extend declared-event projection. Verify direct declarations arm while spec drift still refuses. This depends on steps 1 and 2.
4. **Guidance** — revise `sprint_prep` and add its reseed migration. This is parallelizable with step 3 after the contract in step 2 is fixed.
5. **Regression sweep** — update fixtures that insert `sprint_specs`, replace tests asserting review-gated launch, and run the focused Sprint domain, CLI, board, close, snapshot, skill, live-proof, liveness, message-delivery, recovery, review-loop, and dispatch suites. Then run the repository's full test gate.

Implementation should land as one coherent PR because the schema, declaration path, and arming validator are unsafe in mixed versions. Within the branch, steps 3 and 4 may proceed in parallel after step 2.

## Risks and Failure Modes

| Risk | Required response |
|---|---|
| Migration drops a historical approval link | Migration fixture compares all old and new binding fields row-for-row |
| API binds a client-supplied or stale hash | Hash only the current body inside the declaration transaction |
| Legacy caller breaks | Keep `--spec-approval` and `spec_approval_ids`; test the old command shape |
| Review still gates through one forgotten join | Test fail, stale, deleted-reviewer, unresolved-findings, and no-review cases at declaration and arm |
| Removing review also removes scope integrity | Keep kind, feature, non-empty body, at-least-one-spec, and drift checks explicit |
| Report silently loses unreviewed specs | Left-join test asserts an unreviewed binding appears in close evidence |
| Mixed selectors bind a document twice | Normalize by document id before insertion |
| Concurrent body edit races declaration | Read, hash, and insert under the existing declaration write transaction |
| Guidance and engine disagree after update | Seed convergence test compares the granted skill to the authoritative asset |

## Verification Gate

The feature fails acceptance if any review property changes launch eligibility.

The adversarial matrix must declare and arm the same valid spec with:

- no QA/QC row;
- one current `pass`;
- one current `fail`;
- unresolved findings;
- only a stale review;
- a review signed by a shell later marked deleted;
- the same document selected through both direct `--spec` and legacy `--spec-approval` inputs.

Every case must reach `armed`. The mixed-selector case must create exactly one `sprint_specs` binding for the document. Counter-tests must still reject no bound spec, unknown document, wrong-feature document, non-spec document, empty body, body drift before first arm, invalid participant plan, missing merge grant, and conflicting armed capacity.

Acceptance additionally requires migration preservation, close-report visibility with null evidence, legacy CLI compatibility, skill-seed convergence, and the full suite green.

## Anticipated User Activity

### Vocabulary

- **Valid Privileged User**: the FnB choosing whether review is useful and observing the resulting Sprint.
- **Shell**: a Planner declaring and arming, or a Reviewer optionally recording QA/QC evidence.
- **System**: the engine binding spec bodies, validating launch invariants, and projecting evidence.
- **Unexpected Participant**: a caller without the authenticated Sprint authority already required by the endpoint.

### Expected Activity

- The FnB may request review, consider any verdict, or launch without review.
- A Planner selects current governing specs and launches when the non-review plan invariants pass.
- A Reviewer may record exact-revision findings without granting or withholding launch authority.
- The System preserves review history while deriving launch scope from the selected current documents.

### Reach

The change is reachable through `sc sprint declare`, `sc sprint record-qaqc`, `POST /_sc/sprint/declare`, Sprint arming, close evidence, board projections, migrations, and the `sprint_prep` skill. Existing authentication and role checks remain in force.

### Data Tenancy

All data remains inside the installation's engine database and existing Sprint/feature scope. No new account boundary or external data flow is introduced.

### Beyond Intention

This feature does not permit launching without a governing spec, bypass participant or merge controls, turn pre-Sprint QA/QC into implementation review, delete review history, or allow unauthenticated declaration.

## Non-Goals

- Removing `record-qaqc` or historical QA/QC data
- Renaming `sprint_spec_approvals` or `approval_id`
- Changing work-unit review, PR approval, green-check, merge, conformance, or completion rules
- Automatically selecting whether the FnB should request review
- Silently rebinding a spec edited after declaration
- Redesigning the Sprint board


