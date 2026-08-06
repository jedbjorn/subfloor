# Sprint 4 — whole-Sprint conformance report

Author: Review-01 (REV1), shell 7. Sprint 4 (feature #31), one work unit (19),
one PR (#1065), governing spec document #117 (seq 17, "Optional pre-Sprint
QA/QC"). Bound revision sha256 8c826c2e…02f0f80 equals the current revision —
no mid-Sprint edits, no drift.

## Method

Integrated main was judged, not the unit diff. The approved head
c48cfb188605b811ead74f5a65aea3d791778383 was fetched and diffed against the
squash-merge commit d704cc0 on origin/main: the diff is empty, so integrated
main carries exactly the reviewed tree. A detached worktree at d704cc0 ran the
focused Sprint suites (test_sprint_cli, test_sprint_close,
test_sprint_board_api, test_sprint_skills: 78 passed + 92 subtests) and the
full repository gate `pytest tests/ -q`: 2009 passed, 988 subtests, 142.9s.
The bounded evidence packet (compile-report, limit 50, no truncation counters
fired) supplied durable Sprint facts.

## Requirement classification

Every requirement of spec #117 classifies **as-specced**. No
deviated-intentionally, deviated-silently, or unimplemented items.

1. **Direct spec binding — as-specced.** `spec_document_ids` is canonical on
   `POST /_sc/sprint/declare`; repeatable `--spec` on the CLI. The server
   resolves documents, requires ≥1 unique non-empty `spec` of the declared
   feature, hashes each current body, and inserts the hash inside the
   declaration transaction; the client never submits a hash. Verified by code
   trace at the approved head and by the adversarial CLI tests on main.
2. **Review evidence optional — as-specced.** `record-qaqc` and
   `sprint_spec_approvals` survive as immutable evidence;
   `sprint_specs.approval_id` is a nullable evidence pointer; arming does not
   join or interpret it. Zero/fail/stale/findings/deleted-signer review states
   all reach `armed` in the adversarial matrix.
3. **Compatibility input — as-specced.** `--spec-approval` /
   `spec_approval_ids` remain accepted, bind the document's *current* body,
   store the approval id as nullable evidence, dedup mixed selectors by
   document id (exactly one binding row — tested), and reject duplicate
   compatibility reviews for one document as ambiguous (400).
4. **Arming — as-specced.** `_validate_arm_plan` validates bound rows,
   existence, kind, feature, non-empty body, and current-body hash equality
   without consulting approvals; post-declaration drift still refuses first
   arming; resume behavior unchanged.
5. **API contract — as-specced.** Error table verified row-for-row: no
   selector → 400, unknown id → 404, non-spec/empty/wrong-feature → 409,
   duplicate compat → 400, any review property → no error. The
   `sprint.declared` event adds `spec_document_ids`, retains
   `spec_approval_ids` only when supplied, and the board whitelist exposes it.
6. **Data migration 0185 — as-specced.** Transactional `sprint_specs` rebuild
   preserving PK, both FKs, hash CHECK, and `included_at`, with nullable
   `approval_id`; `schema.sql` untouched; trigger
   `trg_sprint_followups_spec_scope` dropped and recreated verbatim (diffed).
   Required cases tested: fresh DB, populated pre-change preservation,
   rebuild-from-snapshot, discovery idempotence, pre-commit failure atomicity.
7. **Projections and reports — as-specced.** `sprint_close.py` left-joins
   optional evidence so unreviewed bindings appear in the packet (tested);
   `sprint_board.py` projects `spec_document_ids`; snapshot allowlists retain
   both tables and null FK values round-trip.
8. **Guidance — as-specced.** `sprint_prep/SKILL.md` teaches optional,
   FnB-gated QA/QC with gating wording removed; a reseed migration
   redistributes it; the seed-convergence test compares grant to asset.
9. **Verification gate — as-specced.** The adversarial matrix (no row, pass,
   fail, unresolved findings, stale, deleted signer, mixed selectors) reaches
   `armed`; counter-tests still reject empty scope, unknown/wrong-feature/
   non-spec/empty documents, drift, invalid participants, missing merge grant,
   and conflicting capacity. Full suite green on integrated main.

## Findings

None. The findings file is an empty JSON array. Pre-declaration spec defect
flag #220 (schema.sql instruction contradiction) was fixed in the bound spec
body before declaration and closed with verification; it is not a shipped
deviation. No pauses, cancellations, anomalies, failed wakes, or unresolved
follow-ups exist in the durable record.
