---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE: Sprints v2.0 build — shard A re-run (addendum to doc #57)

- **Sprint:** doc #51 — SPRINT: Sprints v2.0 build
- **Trigger:** planner task msg #646 — scoped C2 re-run after remediation U11 (PR #877) + U12 (PR #879)
- **Spec judged:** #46 (feature 31), REV9 body (R7 completion semantics, head-change rule, advisory close-out)
- **Judged against:** `main @ b5f5c29` in ~/Repos/subfloor (committed HEAD verified via `git show HEAD:`; working tree noted dirty — see Observations)
- **Scope:** shard A remediated findings from doc #57 only — M1, M2, M3, M4, Md1, Md4, Md5 — plus immediately-adjacent regressions. NOT a full re-pass; shard B and all other doc #57/#58 findings remain as previously recorded.
- **Reviewer:** REV2 (shell 8), conformance slot
- **Ratified judgement calls:** none supplied with the re-run task.

## Verdict: PASS (scoped) — 7 closed / 0 partial / 0 open

| Finding | Verdict | Basis |
|---|---|---|
| M1 — QAQC write path | **closed** | `POST /_sc/sprint/qaqc` (server.py:2160 @HEAD), token-bound signer; store `SprintSpecApprovalStore.record()` binds reviewer/verdict/timestamp/findings/exact revision sha256 (sprint_domain.py:113-184); CLI `sc sprint record-qaqc`; `sc mem doc qaqc` rewired to the live route (mem.py:790-796), dead `/api/spec-qaqc-reviews` 404s; declare + arm gates read the same table. |
| M2 — sprint inbox/accept/decline | **closed** | CLI `sc sprint inbox\|accept\|decline` (sprint_cli.py:113-140) → authenticated endpoints `GET /_sc/sprint/<id>/inbox`, `POST /_sc/sprint/inbox-read`, `POST /_sc/sprint/inbox-decline` (server.py:2126,2263,2272) → store methods wrapping the trigger-enforced acceptance semantics; decline requires `--reason` and routes unit back + notifies planner atomically; skills (sprint_dev/rev/pln) name the commands. |
| M3 — completion semantics (R7) | **closed** | Merged observation completes only `merge_ready` units (sprint_domain.py:1776-1820); grant-bypass records `merge.grant_bypassed`, does NOT complete, and actively wakes the Planner via message + pending wake outbox row (sprint_domain.py:1833-1895); head-change rule implemented — stale approval voided, unit back to `in_review`, Reviewer actively woken for delta review (sprint_pr_watcher.py:535-617); stale approvals unusable downstream (sprint_review_loop.py:179-181, 247-251). |
| M4 — shell-judged + report-only completion | **closed** | `output_kind` + `completion_result` columns (migration 0152); `POST /_sc/sprint/complete-unit` (server.py:2244) + `sc sprint complete-unit --result-file`; store enforces assigned-Developer authority, non-code-only, code units rejected with "complete only through the merge judgment chain"; result carried durably in unit row AND `work_unit.completed` event payload; dependents unblock on `disposition='completed'` path-agnostically. |
| Md1 — signer verification | **closed** | Recording surface rejects non-reviewers (`SprintAuthorityError` → 403) unless `shells.flavor='reviewer'` (sprint_domain.py:124-132); declare and arm re-verify signer flavor independently, so hand-seeded non-reviewer rows still block arming. |
| Md4 — replan surface | **closed** | `POST /_sc/sprint/replan-unit` (server.py:2221-2243) behind `_sprint_planner_proxy` (owning planner or FnB/admin) + `sc sprint replan-unit`; store guards intact — planned-only, completed history untouchable, append-only before/after `work_unit.replanned` event (sprint_domain.py:1540-1608). |
| Md5 — conversation lifecycle close | **closed** | `close_for_terminal_lifecycle` (sprint_participant_chats.py:151-238) called inside the lifecycle transaction on BOTH completion (sprint_domain.py:307-314) and abort (519-525); conversations closed (`state='closed'` + `closed_at` + `conversation.closed` event), retained not deleted; pill projection excludes terminal sprints; CLI picker `state!='closed'` filter cured. Test loops both terminal states. |

## Test verification

Focused suites run green on main: `test_sprint_v2_domain.py::SpecApprovalTest`, `test_sprint_cli.py` (end-to-end HTTP round-trips with per-shell tokens), `test_sprint_message_delivery.py`, `test_sprint_review_loop.py` (head-change, grant-bypass, positive paths), `test_sprint_work_dispatch.py` (replan guards, no-code completion unblocks dependents), `test_sprint_close.py`, `test_sprint_skills.py` (skill/command drift), `test_sprint_entrypoint_removal.py` (dead route 404s). Tests assert meaningfully — reverting each remediated behavior turns its test red.

## Low observations (not re-openings; for planner disposition)

- **L-a:** `sprint_rev` skill never names `sc sprint decline`, though the store treats `review_request` as declinable and routes declines to the planner. If reviewers are meant to decline review requests, the skill doesn't say how; if not, worth a one-line confirmation of intent.
- **L-b:** `complete_from_merge_in_transaction` returns silently when lifecycle is not armed/paused (sprint_domain.py:1759-1760) — a bypass merge observed while `prepared` leaves no `merge.grant_bypassed` trace. Narrow window; units cannot be `merge_ready` then, so informational only.
- **L-c:** Test hygiene — `ProductionPulseTest::test_server_startup_wires_broker_before_sprint_runtime` doesn't mock `sprint_pr_watcher.start_service`, leaking a real watcher thread (stderr noise) since #877. Not a product defect.
- **L-d:** `mem.py:795` builds the qaqc idempotency key with `uuid.uuid4()` per call — HTTP-level replay dedup can never hit via `mem doc qaqc`; store-level UNIQUE dedup still holds. Dead decoration.

## Process observation (outside conformance scope, reported not judged)

The shared work tree ~/Repos/subfloor on `main` was DIRTY during this pass: uncommitted `sprint_board` changes (modified server.py, ui/app.js, ui/index.html, ui/style.css, tests/test_sprint_entrypoint_removal.py, fixtures manifest; untracked sprint_board.py + test_sprint_board_api.py). Looks like a dev's in-progress unit left on the default branch — finish-gate violation for whoever owns it. Verdicts above were verified against committed `b5f5c29` (via `git show HEAD:` where files were dirty), so the dirt did not contaminate the judgment.
