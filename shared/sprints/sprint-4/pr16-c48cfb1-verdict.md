# Review verdict — Sprint 4, work-unit 19, registered PR 16

**Verdict: approved**

PR: https://github.com/jedbjorn/subfloor/pull/1065
Reviewed head: c48cfb188605b811ead74f5a65aea3d791778383 (fetched and matched the
registered head exactly; diff taken as `origin/main...c48cfb1`).
Governing spec: document #117 (seq 17) — Optional pre-Sprint QA/QC.
Checks at head: tests, verify, render-check, CodeQL, Analyze (actions),
Analyze (python) all SUCCESS; PR mergeable. Readiness body was a bare locator;
PR body carries only work-unit id and spec reference — protocol conformant.

## What was verified (traced, not trusted)

**Declaration (server.py).** `spec_document_ids` is canonical; both selectors
normalized as positive-int lists with dedup. Server reads each document body and
hashes it inside the declaration transaction; the client never submits a hash.
Compatibility selector resolves the approval's document and binds the *current*
body, retaining the approval id as nullable evidence; duplicate compatibility
selections for one document are rejected as ambiguous (400); mixed
direct+compat selection of one document dedups to exactly one `sprint_specs`
row with the evidence pointer kept (test proves it). Unknown document/approval
→ KeyError → 404; non-spec, empty-body, NULL-feature, or wrong-feature →
SprintInvariantError → 409; no selector → ValueError → 400. Error mapping
confirmed in `_sprint_error`. The declared-event payload adds
`spec_document_ids` and retains `spec_approval_ids` only when supplied —
matches the API contract table row-for-row.

**Migration 0185.** Rebuilds `sprint_specs` preserving PK, both FKs, the hash
CHECK, and `included_at`, with `approval_id` now nullable; straight row-for-row
copy. `schema.sql` untouched. The one trigger referencing `sprint_specs`
(`trg_sprint_followups_spec_scope` from 0150) is dropped and recreated
verbatim — I diffed the definitions. No other trigger/index/view references
the table. Runs under the `-- migrate: foreign-keys-off` runner contract;
failure-before-commit rollback, no-reapply under normal discovery, and
populated-pre-change preservation are each tested. Fresh-null insert and
`foreign_key_check` verified in tests.

**Arming (sprint_domain.py).** The approval join and all verdict/revision/
signer/flavor/deleted checks are gone; arming now requires ≥1 bound row whose
document still exists (LEFT JOIN + NULL detect), is a `spec`, belongs to the
feature, has a non-blank body, and whose current hash equals
`bound_revision_sha256`. Post-declaration drift still refuses. I grepped the
whole engine surface for remaining `sprint_spec_approvals` joins: only the
compatibility resolver, the snapshot allowlist, the close LEFT JOIN, and the
`record-qaqc` writer itself remain — no forgotten eligibility gate.

**Reports/projections.** `sprint_close.py` uses the LEFT JOIN and surfaces
verdict, reviewed revision, signer, findings reference, and timestamp as
evidence; NULL-evidence packet contents asserted by test. Board whitelist
exposes `spec_document_ids` while keeping historical events readable. Snapshot
roundtrip with NULL evidence asserts rows are preserved without zero/omission
conversion.

**Guidance.** `sprint_prep` asset rewritten to FnB-decides, evidence-not-
authorization, `--spec` canonical; gating language ("qualifying approval",
"use fail until", "lacks Review-shell QAQC approval") removed and asserted
absent. Reseed rides inside 0185 as the spec's single-migration instruction
requires; seed-convergence test compares granted row to the asset. The
`POLISHED_SPRINT_SKILLS` exclusion of `sprint_prep` is the correct
reseed-chain pattern (0184's historical content no longer matches the updated
asset), not a masked regression — 0185's own convergence test covers it.

**Adversarial matrix.** The spec's verification gate is implemented as tests:
no-row / pass / fail / unresolved-findings / stale / deleted-signer /
mixed-selectors all reach `armed`; counter-tests reject no bound spec, unknown
document, wrong-feature, non-spec, empty body, and pre-arm drift. Legacy
`--spec-approval`-only declaration shape still works end to end.

## Low findings (report notes only — non-blocking)

1. **FK-check result discarded (Low).** 0185 ends with
   `PRAGMA foreign_key_check;`, but the runner (`migrate.apply`) and direct
   `executescript` both discard its rows, so a violation would not abort the
   migration. The straight-copy rebuild plus post-migration
   `foreign_key_check` assertions in tests make a real violation implausible,
   and prior rebuild migrations (0144, 0168) ship no check at all — this is
   strictly above convention. If the engine ever wants the check to bite, the
   runner must assert an empty result.
2. **Declaration invariant error does not name the failing document (Low).**
   A multi-spec declaration that fails kind/feature/body validation reports
   only "declaration requires non-empty spec documents for its feature";
   the caller must bisect which selector failed. One-line diagnosability
   improvement, no behavior change.

## Conclusion

Implementation matches spec #117 on every required-behavior clause I could
trace, the launch gate is genuinely gone from all three former layers, scope
integrity (kind, feature, non-empty body, exact revision, at-least-one-spec,
drift refusal) is preserved and tested, migration integrity is proven for the
populated/fresh/rebuild/rollback/idempotency cases, and checks are green at
the reviewed head. No Critical/Major/Medium finding remains. Approved.
