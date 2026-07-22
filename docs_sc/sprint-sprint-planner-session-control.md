---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: Sprint planner session control
status: CLOSED
declared: 2026-07-21 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=claude/fable

Spec: doc #20 (feature 14) · decision #4 · fixes issue #454 · #439 liveness correction mandatory (unit 2).

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Session-control schema + state machine (task #50) | DEV3 | REV1 | — | feat/session-control-state | #455 | merged |
| 2 | Session supervisor + ownership leases, incl. #439 (task #51) | DEV4 | REV2 | 1 | feat/session-supervisor-leases | #456 | merged |
| 3 | Wake dispatcher + control API (task #52) | DEV3 | REV1 | 2 | feat/session-wake-dispatcher-api | #460 | merged |
| 4 | Claude session-control adapter (task #53) | DEV4 | REV2 | 3 | feat/claude-session-control | #463 | merged |
| 5 | Codex app-server adapter (task #54) | DEV3 | REV1 | 3 | feat/codex-session-control | #461 | merged |
| 6 | Kimi K3 session-control adapter (task #55) | DEV3 | REV2 | 3 | feat/kimi-session-control | #462 | merged |
| 7 | Session status + analytics integration (task #56) | DEV3 | REV1 | 4,5,6 | feat/session-status-analytics | #464 | merged |
| 8 | Sprint workflow + provider conformance (task #57) | DEV4 | REV2 | 7 | chore/sprint-provider-conformance | #465 | merged |
| 9 | Doc cleanup: purge stale wake claims (U8 L1/L2; docs-only, no spec task) | DEV4 | REV2 | 8 | docs/sprint21-wake-claims | #466 (draft) | blocked: publish gate |
| 10 | Conformance F1 fix: sc enter managed-binding attach + --new-session refusal (run.py) | DEV4 | REV2 | conformance | fix/managed-enter-binding | #469 | merged |

Judgements:
- U1/DEV3 (2026-07-21): spec names binding states but not allowed edges → chose conservative lifecycle transitions: self-refresh allowed; idle/dormant/foreground (via active channel) may enter dispatching; error/released recover only through starting before delivery. Ruling: stands (consistent with spec dispatch table + pre-lease reconciliation). Wording corrected per REV1 L1 — foreground→dispatching is in the implemented graph, required for Claude active-watcher delivery.

- U3/DEV3 (2026-07-21): spec retry wording contradictory (delays 15s/60s/5m vs "third failure terminal") → chose initial attempt + three delayed retries, terminal on fourth failure, preserving every stated delay. Ruling: stands; retry wording logged as spec debt.
- U3/DEV3 (2026-07-21): boundary — internal session-control endpoints ship in unit 3; public `sc session` operator CLI remains unit 7 (per tasks #52/#56). Ruling: stands.

- U5/REV1+PLN1 (2026-07-21): effort pinning "from the original archive" ratified as config-effective at launch (L3). Spec debt: make explicit in #20.
- U5/PLN1 ruling (2026-07-21): arming-time approval-posture validation is in-spec intent (arming verifies deliverability) — SC-462 fix in unit 5; SAME REQUIREMENT applies to Claude (unit 4) and Kimi (unit 6) adapters, to be stated in their kickoffs. Spec debt: #20 silent on posture validation at arming.

- U6/DEV3 (2026-07-21): boundary — SC-463 fix covers server-exit token cleanup in unit 6; release-while-server-lives cleanup belongs to unit 7's generic release surface. Ruling: stands (unit 7 owns sc session release); unit 7 kickoff must name it explicitly.

Follow-ups (Lows):
- U1/REV1 L2: reconstruction tests skip the one-shell-two-bindings (managed+unmanaged) case — coverage nicety. Review notes: reviews/sprint21-unit1-pr455.md on shell/rev1.
- U3/REV1: 1 Major (SC-460 retry stranded a binding in starting) + 1 Medium (SC-461 PATCH could unfence dispatching) — fixed at 6883ca2, flags #17/#18 closed. Notes: reviews/sprint21-unit3-pr460.md @7eb39a2 on shell/rev1. Earlier real CI red: CodeQL on dynamic-field SQL, fixed 3987995.
- U5/REV1: 1 Medium (SC-462 approval-gated codex config breaks managed wake fail-slow, no posture validation) + 7 Lows: reviews/sprint21-unit5-pr461.md @8a07b91 on shell/rev1.
- U6/REV2: 1 Medium (SC-463 flag #20: kimi control token not deleted on release — explicit spec requirement) + 6 Lows: reviews/sprint21-unit6-pr462.md @2eeb5df on shell/rev2. DEV3's three judgement calls verified sound.
- U4/REV2: 1 Medium (SC-464 flag #21: claude deliver ack-wait lacks liveness re-check — planner crash could wedge dispatcher ~4h; fixed 14dc36a, verified, flag closed) + 7 Lows (incl. L7 ack-wait residual under whole-group kill -9): reviews/sprint21-unit4-pr463.md @3d3b1a3 on shell/rev2.
- U7/REV1: 1 Medium (SC-465 flag #22: sc session release AttributeError on shells without a binding) + 7 Lows: reviews/sprint21-unit7-pr464.md @6013dae on shell/rev1. U6 credential-cleanup handoff verified fail-closed; analytics attribution verified no-double-count.
- U7 Low 4 ESCALATED TO FNB (2026-07-22): unauthenticated local /api/session-control POST routes let any local process release another shell's managed binding (cancels queued wakes, deletes kimi token). Pattern-consistent with existing /api surface but first route with cross-shell sprint-liveness side effects. Awaiting FnB posture ruling; non-blocking for U7 merge.
- U8/REV2: 0 Major/0 Medium/3 Lows: L1 docs_sc/job-runner.md false completion-result-row wake claim; L2 docs/README.md stale Claude-planner recommendation + watcher-as-planner-wake (~L428-432, 634-638, 791); L3 wrap-width nit. L1/L2 → unit 9 (PLN1 ruling: fix under ACTIVE authority before conformance, else reads deviated-silently); L3 → report. Notes: reviews/sprint21-unit8-pr465.md @bd33d98 on shell/rev2.
- U10/REV2: 1 Medium (SC-466 flag #23: bare sc enter refused for managed binding in state=error — error→foreground invalid transition — and refusal funnels operator to release, cancelling queued wake jobs) — fixed @12a6792, verified, flag closed; re-review clean, merge unlocked. 3 Lows for report: L1 harness-mismatch msg/precedence untested; L2 bare headless run of errored-managed shell now also refused (fail-closed, actionable); L3 harness-mismatch path untested. Notes: reviews/sprint21-unit10-pr469.md @68578cc on shell/rev2.
- U2/REV2: 2 Mediums (lease lifecycle races: cleanup false-orphan fencing; pre-spawn live-owner refusal) fixed at a054855, flags #15/#16 closed. 9 Lows for the report (L1-L6 original + 3 residual): reviews/sprint21-unit2-pr456.md @722924c on shell/rev2.

Notes:
- HOLD RELEASED 2026-07-22: DEV4 returned by FnB; unit 4 kicked off.
- Units 4/5/6 are parallel after unit 3. RESEQUENCED 2026-07-21: unit 6 reassigned DEV4→DEV3 (DEV4 on FnB hold); unit 4 remains DEV4 pending hold release.
- U5 re-review note for units 4/6 reviewers: validate_managed_wake_posture in manage is provider-generic — providers recording settings.approval_policy/sandbox opt into the codex vocabulary; absent keys pass untouched.
- Unit 8 freezes spec #20 only after the pre-freeze review proves the #454 and #439 scenarios.
- Engine staleness: `./sc models` missing from materialized engine (predates PR #453) — routes read from DB `flavor_defaults`; surfaced to FnB, `./sc update` is theirs.
- U9 PUBLISH GATE (2026-07-22): canonical doc #10 updated via sc mem; matching repo-doc diffs prepared by DEV4. Landing them requires committing the .sc-state/content.sql snapshot — admin/GUI Publish only (git/docs skills), and sc mem doc edit does not render (known subfloor#434, see flag SC-007/#14). PLN1 ruling: DEV4 holds (no hand-committed renders), branch pushed as draft; unit 9 waits on FnB Publish handoff. Conformance pass proceeds with these surfaces pre-declared deviated-intentionally. Draft PR #466 (docs/sprint21-wake-claims @f608a1e) pushed; render-check red IS the publish gate (render changed, content.sql intentionally absent) — expected, not anomalous, not counted; goes green once FnB Publish commits the snapshot.
- CONFORMANCE PASS launched 2026-07-22: REV1 (claude/fable), spec doc 20 vs main @ 2cc320ec, ratified judgements + #454/#439 proof required in kickoff (msg to REV1).
- SCOPED CONFORMANCE RE-RUN CLEAN 2026-07-22 (doc #23, seq 2 of #22): F1 as-specced at main @ 90866a6; SC-466 correction verified (error-state enter fails pre-archive/spawn, retry-first guidance); 0 new findings. F2/F3 Lows + J7 live gates unchanged.
- SPRINT CLOSED 2026-07-22: all code units (1-8, 10) merged, conformance clean. All scoped authority revoked by this CLOSED status + freeze. Unit 9 (docs-only, draft PR #466) hands to FnB: run Publish, then merge under default gate — render-check goes green once content.sql snapshot is committed. PR #466 watch deliberately kept so the eventual merge lands as an inbox row. Spec #20 remains UNFROZEN pending live release gates (J7) — a separate freeze from this doc's.
- CONFORMANCE VERDICT (doc #22, 2026-07-22): 0 Major / 1 Medium / 2 Low; #454 + #439 proven hermetically (operational proof rides deferred close-out gates, J7). PLN1 routing: F1 Medium (sc enter managed-binding attach + --new-session refusal unimplemented in run.py) → fix unit 10 NOW under ACTIVE authority, scoped conformance re-run after merge; F2 Low (provider interrupt op absent) → report backlog; F3 Low (skills lack re-arm-after-batch instruction; runtime degrades safely) → report backlog.
- FNB CLOSE-OUT DEPENDENCIES: (1) ./sc update (stale engine lacks ./sc session + ./sc models); (2) provider spend authorization for live gates; (3) admin Publish handoff for unit 9; (4) U7 Low 4 auth-posture ruling.
- U8 RELEASE CONSTRAINTS (PLN1 ruling, msg #261): unit 8 merges on its hermetic scope (all hermetic provider gates + capability probes + render checks green, full suite 641/641). Live gates — per-provider disposable-session smokes + one real sprint each on Claude/Codex/Kimi K3 — deferred to close-out; blocked on FnB `./sc update` (stale engine lacks `./sc session`) and provider spend authorization. Spec #20 stays UNFROZEN until they pass. Documented in PR #465 body.
