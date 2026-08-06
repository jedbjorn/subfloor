# Sprint 4 — final Sprint report

Author: Review-01 (REV1), shell 7, Sprint Reviewer.

## 1. Governing scope and revisions

Sprint 4, feature #31 (Sprints v2.0 — collaborative orchestration). One bound
spec: document #117 (seq 17, "Optional pre-Sprint QA/QC"), bound revision
sha256 8c826c2e76be2b535ed31eb59deed39e030290d1b98b8960f511b3b1a02f0f80.
The bound revision equals the current revision at closeout — no mid-Sprint
edits. Declaration bound the spec through the new direct `--spec` selector
with approval #11 retained as nullable compatibility evidence, itself a live
exercise of the shipped feature. Sprint armed 2026-08-06 03:03:24.

## 2. What shipped

One work unit, one PR, one wave:
- Work unit 19 "Optional pre-Sprint QA/QC implementation" (tasks 347–351,
  Developer DEV6, wave 1, no dependencies), disposition completed 03:55:47.
- PR jedbjorn/subfloor#1065, registered Sprint PR 16, head
  c48cfb188605b811ead74f5a65aea3d791778383, merged as squash commit d704cc0.
  Diff of approved head against integrated main is empty — main carries
  exactly the reviewed tree.
- Scope: migration 0185 (nullable `sprint_specs.approval_id` rebuild), direct
  current-spec declaration in server.py + sprint_cli.py, review-independent
  arming in sprint_domain.py, nullable-evidence projections in sprint_close.py
  and sprint_board.py, sprint_prep guidance rewrite with reseed migration, and
  the adversarial-matrix + full-suite test gate. 12 files, +~1,000 lines.

## 3. Judgments and ratified deviations

- Pre-declaration QAQC: approval #11 (pass, 2026-08-06 02:57:23, REV1) signed
  the exact bound revision after one fix round — flag #220 (schema.sql
  instruction contradicted the frozen-baseline migration contract) was
  corrected in the spec body and all three advisory nits adopted (findings
  doc #120). No deviation shipped.
- Work-unit review: judgment recorded approved at the exact registered head
  after a full adversarial pass (declaration transaction, migration 0185
  row-for-row preservation, arming validator, error mapping, event payload,
  projections, guidance). No Critical/Major/Medium finding remained; no
  changes-requested round was needed.
- No intentional deviations were ratified during the Sprint.

## 4. Failures, retries, pauses, anomalies

None. Zero pause/resume cycles, zero cancelled units, zero failed wakes, zero
anomaly events, zero open liveness expectations at closeout (2 resolved).
Wake health: 6 delivered, 0 escalations, 0 nudges. The Sprint ran clean end
to end in under one hour from declare to delivery-terminal.

## 5. Conformance conclusion

Every requirement of spec #117 classifies as-specced; the classification and
evidence are in the conformance report submitted with this close. Integrated
main (d704cc0) passed the focused Sprint suites (78 passed, 92 subtests) and
the full repository gate `pytest tests/ -q` (2009 passed, 988 subtests,
142.9s) run in a detached worktree at the integrated commit. Review evidence is now advisory across declaration and
arming, while spec binding, drift refusal, participant, and merge-grant
invariants are preserved and counter-tested. Findings: none. Decision:
conclude, outcome accepted.

## 6. Follow-ups for FnB disposition

None. No follow-up items were created; the findings array is empty and no
open flags attach to this Sprint's scope.

## 7. Evidence location

- Durable: bounded evidence packet via `sc sprint compile-report --sprint 4`;
  conformance report and this final report in `sprint_reports`; review
  judgments on work unit 19; approval #11 on document #117.
- Working copies (gitignored): `shared/sprints/sprint-4/` — evidence.json,
  spec-117.txt, conformance-report.md, findings.json, final-report.md.
- Code: origin/main at d704cc0 in jedbjorn/subfloor; PR #1065.
