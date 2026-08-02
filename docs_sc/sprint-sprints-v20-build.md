---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: true
---

# SPRINT: Sprints v2.0 build
status: CLOSED                      # ACTIVE | CLOSED — closed 2026-08-01, all 13 units merged, C2 dual PASS, report doc pending id below
declared: 2026-07-31 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3

Governing spec: doc #46 (feature #31, Sprints v2.0 — collaboration loop), revision REV6 — QAQC PASS at REV4 (docs #54/#55); REV5 applied the verdicts' two prescribed Low one-liners (arming-eligible without a further round); REV6 is the recorded mid-Sprint R2 pill-precedence edit. Spec tasks #184–#193 map to units 1–10.
Work repo: ~/Repos/subfloor (github.com/jedbjorn/subfloor). Branch naming: `feat/s2-u<seq>-<slug>`.

## Protocol deviations (FnB-directed, 2026-07-31 — decision #38)

These override the participant `sprint` skill where they conflict; a planner task row is the ruling authority:

1. **Merges are PLN1's.** No dev merges — on review-clean + green, hand off by `result` row; PLN1 merges the PR. The v1 dev merge-on-green+clean authority is NOT granted this sprint.
2. **Wakes are PLN1-native — the substrate's sprint eventing is out of the loop.** No watch daemon, no `pr_event` rows, no `./sc watch inbox`. Devs do NOT register PR watches. PLN1 boots every worker as a background run whose completion is PLN1's wake, and runs its own GitHub poller for PR/CI transitions on `feat/s2-*` branches. `sc mem message` remains the durable task/result channel — workers drain their inbox at boot and file `result` rows as usual; PLN1 reads them on wake.
3. **Unit reviews are single-reviewer; only close-out conformance is 2x** (FnB ruling 2026-07-31). Each unit gets exactly one reviewer (REV1 gates DEV3's units, REV2 gates DEV4's — see the board). The final conformance pass alone runs both REV1 + REV2, sharded by spec half. Phase 0's double QAQC was a one-time spec-discovery measure, not the review pattern.
4. **Phase 0 gates everything:** spec #46's banner requires a Review-shell QAQC round to pass against the exact current revision before implementation tasks activate. Units 1+ stay `waiting` until phase 0 is PASS and Medium+ findings are resolved.

## Board

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 0a | QAQC round on spec #46 rev3 (independent) | REV1 | — | — | — | — | done — FAIL: 2 Medium (flags #67/#68), 5 Low; verdict doc #53 |
| 0b | QAQC round on spec #46 rev3 (independent) | REV2 | — | — | — | — | done — FAIL: 3 Medium (flags #64/#65/#66), 5 Low; verdict doc #52 |
| 0c | Re-round: verify all 5 Medium fixes in spec #46 REV4 + sweep changed text | REV1 | — | 0a,0b | — | — | done — PASS (doc #54); flags #67/#68 closed |
| 0d | Re-round: verify all 5 Medium fixes in spec #46 REV4 + sweep changed text | REV2 | — | 0a,0b | — | — | done — PASS (doc #55); flags #64/#65/#66 closed |
| 1 | Stage 1 — Sprint domain schema + lifecycle + armed-service switch (task #184, done) | DEV3 | REV1 | 0a,0b | feat/s2-u1-domain-lifecycle | #850 | merged → main 471ce2b; flag #69 fixed+closed; 7 Lows to report |
| 2 | Stage 2 — participant conversations, amber pills, FnB entry (task #185, done) | DEV4 | REV2 | 1 (merged) | feat/s2-u2-conversations-pills | #849 | merged → main 6abc593; #71 fixed+closed; re-review PASS 0 findings |
| 3 | Stage 3 — sprint_messages domain + wake outbox + 3-attempt auto-pause (task #186, done) | DEV3 | REV1 | 1 (merged) | feat/s2-u3-messages-wakes | #858 | merged → main 13c599c; review-clean 0M/0M; 4 Lows (#479) — L2/L3 wiring gates pinned to unit 5; side-finding subfloor#859 |
| 4 | Stage 5 — PR registration + armed watcher (task #187) | DEV4 | REV2 | 1, 3 (merged) | feat/s2-u4-pr-watcher | #861 | merged → main e6ac3d5; review-clean 0M/0M, 2 Lows (#514); mechanical u5-conflict rebase, no delta re-review needed |
| 5 | Stage 4 — work units, dependencies, waves + dispatcher (task #188) | DEV3 | REV1 | 1, 3 (merged) | feat/s2-u5-workunits-dispatch | #860 | merged → main eb9d8e2; review-clean 0M/0M; closed u3-L2/L3 wiring gates + u1-Low#5; 4 Lows (#510) |
| 6 | Stage 6 — dev/review loop wiring (task #189) | DEV4 | REV2 | 2, 3, 4, 5 (all merged) | feat/s2-u6-loop-wiring | #863 | merged → main 171ee5c; re-review PASS, SC-028 fixed+closed; mechanical post-#862 rebase; Lows: pause-race + R5 → unit 8, replay UX + grant_bypassed test = accepted residual |
| 7 | Stage 7 — liveness monitor, grace/nudge/escalation policy (task #190) | DEV3 | REV1 | 3 (merged) | feat/s2-u7-liveness | #862 | merged → main c1770fb; re-review clean, SC-029/030/031 fixed+closed (#73-75); R4 implemented; decision #42 ratified; 2 report notes (#558) |
| 8 | Stage 8 — pause/resume/recovery + reconciliation (+ u6-L1 merge-observation pause-race reconciliation; + R5/REV8 closed-without-merge review-expectation resolution) | DEV4 | REV2 | 1, 3, 4 (merged) | feat/s2-u8-pause-recovery | #867 | merged → main 0f60c3b; review-clean 0M/0M; 4 Lows (#585); u6-L1 + R5 implemented; decision #44 (no new migration) |
| 9 | Stage 9 — conformance follow-ups + report compiler + five sprint skills + **shell-facing sprint command surface** (production callers for request_review/record_review/authorize_merge, dispatch/monitor entrypoints — REV2 seam note #539: units 3–6 are store-level only, unit 10 needs this) | DEV3 | REV1 | 6 (merged), 8 (building — parallel-build pattern) | feat/s2-u9-close-skills-surface | #868 | merged → main b7e77d7; review-clean 0M/0M; 3 Lows (#593, incl. L2 planner-replacement-authority spec-clarification candidate); side-flag SC-033 (low) |
| 10 | Stage 10 — live vertical proof (one serial + one parallel Sprint) + adversarial acceptance sweep | DEV4 | REV2 | 9 (merged) | feat/s2-u10-live-proof | #869 | merged → main cc33349; review-clean 0M/0M; 4 Lows (#609); external claims independently verified by REV2; side-issue subfloor#872 |
| C | Close-out conformance: spec #46 vs main @ cc33349 | REV1+REV2 | — | 10 (merged) | — | — | done — DUAL FAIL. Shard A doc #57: 4M/7Md/14L (M1 QAQC write path, M2 sprint inbox surface, M3 merge auto-complete inversion, M4 shell completion path). Shard B doc #58: 2M/9Md/14L (C-M1 final report write path + completion gate, C-M2 follow-up disposition) + 12 spec-clarification candidates. Internals verified solid both shards; failure theme = store-deep/no production caller + one spec-internal contradiction (M3). Remediation plan proposed to FnB: U11 surface/close-out completion (all Majors + replan), U12 behavior fixes (M3-as-R7, C-m1/C-m2 wedge, C-m5 stranded wakes, Md5 conversation close), rest → follow-up flags; scoped re-run by opposite reviewer; FnB dispositioned 2026-08-01 → decision #45, spec REV9, units 11/12 below |
| 11 | Remediation A — production surface completion: QAQC write+signer, inbox/accept/decline, completion (report-only, cancelled), replan, final-report path (advisory), follow-up disposition (task #198) | DEV3 | REV2 | C (dispositioned) | feat/s2-u11-surface-completion | #877 | merged → main 6973f49; review-clean 0M/0M; 3 Lows (#627, incl. cancelled-unit-dependents clarification candidate); side-flag SC-034 / subfloor#876 |
| 12 | Remediation B — R7 completion semantics, watcher (state,head) dedupe + auto delta re-review, stranded-wake resume, conversation lifecycle close (task #199) | DEV4 | REV1 | 11 (serial — shared files) | feat/s2-u12-behavior-fixes | #879 | merged → main b5f5c29; first round 2 Md (SC-035/SC-036) fixed+closed, re-review CLEAN @ 7a98a7a; 1 Low + 1 audit-gap observation (unapproved-head merge while merge_ready completes silently — follow-up candidate) banked (#644); side-issues subfloor#878, #769 |
| 13 | FnB board UI follow-up — spec #59, tasks #200–#204 (U13 read API, U14 header/routing Chats→Sprints→Shells, U15 work-unit board + audit modal, U16 FnB lifecycle actions/feeds, U17 adversarial+visual proof) as ONE implementation PR; visual ref shared/SprintBoard.png; FnB directive #625 confirmed in-session 2026-08-01 | DEV3 | REV2 | 12 (merged) — runs parallel with C2 | feat/s2-u13-fnb-board-ui | #881 | merged → main 012c40a; 3 review rounds: SC-037/038/039 all fixed+closed (#81-83 resolved), final residual re-review clean @ ae6307a; 4 Lows banked; PLN2 notified (#692) for its Chat-view rebase |
| C2 | Scoped conformance re-run: Majors + touched Mediums only, opposite eyes (REV2←shard A areas, REV1←shard B areas) | REV1+REV2 | — | 12 (merged) | — | — | done — DUAL PASS. Shard B doc #60: 6/6 CLOSED, no regressions, 6 Low obs. Shard A doc #61: 7/7 CLOSED (M1-M4/Md1/Md4/Md5), 4 Low obs. Conformance settled @ main b5f5c29. Process note (REV2): main checkout dirty with uncommitted sprint_board work during observation — attributed to in-flight unit 13 build, watching |

Waves (intent, not prohibition): W0 = 0a+0b · W1 = 1, 2 · W2 = 3, 4 · W3 = 5, 7 · W4 = 6, 8 · W5 = 9 · W6 = 10 · W7 = C.
Unit decomposition beyond phase 0 is provisional until QAQC passes; spec tasks (`sc mem task`) are created only after the phase-0 PASS, per the spec banner.

## Rulings

- **R7 (2026-08-01, decision #45, spec REV9):** merged observation completes only `merge_ready` units — the completion judgment happens at authorize_merge; observation executes it. Grant-bypassed merges notify the Planner and never auto-complete. Settles conformance A-M3 (spec-internal contradiction between the loop section and Work-and-Parallelism). Same disposition (FnB, 2026-08-01): remediation as serial U11 (surfaces) → U12 (behavior); close-out completeness is ADVISORY by explicit FnB stance — the engine surfaces gaps, never gates; head-move recovery is automatic; C-m3 and remaining Mediums/Lows → follow-up flags; REV9 wrote all of this plus disposition semantics, QAQC signer rule, replacement-Planner authority, and merge-execution vocabulary into spec #46 before the fix units build.
- **R6 (2026-08-01, msg #599):** unit 10's live proof surfaced that no authenticated shell surface exists for sprint lifecycle (declare/plan, arm, pause/resume, complete/abort) or PR registration — unit 9's surface list stopped at dispatch/monitor/review/merge/conformance/report, and shipped role skills reference the missing surface. Ruled option A: unit 10 builds the minimal surface as thin callers over production stores, mirroring unit 9's auth pattern, then re-drives the proof shell-driven. GitHub merge boundary: fixture PRs against a throwaway base branch only (never main), engine production merge path allowed on those, cleanup in-unit. DEV4's isolation call (#595) approved.
- **R5 (2026-07-31, spec REV8):** closed-without-merge watcher observation resolves the owning unit's outstanding review-request liveness expectations (REV2 unit-6 re-review Low — dead-lane nudge/escalation). Implementation owned by unit 8.
- **R4 (2026-07-31, msg #544):** bare run-state `unknown` is not proven terminal failure — escalate-immediately is exactly the spec's proven list; `unknown` alone takes the silence path and is named among unreadable signals in the escalation; a co-occurring proven signal escalates, the label never does. Also ruled: SprintLivenessMonitor.resolve() wiring from the review outcome path is a unit-6 gate (msg #545).
- **R3 (2026-07-31, msg #543, spec REV7):** a registered PR has exactly one owning work unit; registration rejects multi-unit sets. Settles SC-028 — Terms' "one or more work units" contradicted the review loop's per-lane invariant; per-lane wins, fan-out rejected as complexity without a use the editing-lane rule permits.
- **R2 (2026-07-31, msg #477, spec REV6):** pill precedence for a shell with multiple live participations — armed wins (unique by single-armed invariant), else most recently paused; all participations enterable from history. Recorded mid-Sprint spec edit settling REV2's spec-silence note and flag #71's fix direction.
- **Incident I2 (2026-07-31):** DEV4 reported "REV2 review requested" for unit 2 (#471) but no message row to REV2 ever landed — REV2 booted into an empty inbox and exited without acting. PLN1 verified via the message table, sent the review request itself (#469), re-booted REV2. For the report: worker self-reports verified against durable rows; dev handoffs must be `sc mem message send` deliveries. (Also: PLN1 destroyed REV2's first run log by deleting before reading — lesson logged.)
- **Incident I1 (2026-07-31):** host /tmp tmpfs (7.6G) filled by 30,843 leaked engine-test temp dirs (6.2 GB, TC.json/planted.json fixtures) — ENOSPC killed sprint run logging and the PLN1 poller mid-wave-1; no work lost (DEV3/DEV4 progress verified via result rows + GitHub). Stale dirs purged, poller re-armed, defect filed upstream as subfloor#853. Unit reports unaffected.
- **R1 (2026-07-31, msgs #439/#440):** the feature #29 v1-removal guard tests (`tests/test_sprint_removal_manifest.py`) flag all new v2 sprint modules (first hit: PR #849 `tests` red — "removed module still imports: sprint_conversations"). Standard resolution for every unit: extend the removal manifest/allowlist deliberately for new v2 files; never weaken the v1 assertions; never reuse a v1 module name listed as removed (rename colliding v2 modules). Each dev records its manifest extension as a unit-report judgement.

## Notes

- Unit reports on file (result rows, inputs to the sprint report): u1 #494 · u2 #493 · u3 #495 · u4 #521 · u5 #568 · u6 #566 · u7 #569. Owed: u8, u9, u10 on their merges.
- Thin-vertical rule from the spec holds: stages 2+3 prove the foundation, stage 5 extends it — no broad orchestration before those gates pass.
- Adversarial-acceptance scenarios distribute into each unit's verification gate; the sweep in unit 10 is the final check, not the first.
- cc is mid-flight on subfloor features #24/#25/#26 in the same repo — devs sync/rebase before every unit and before every review handoff.
- Board maintained by PLN1 only; devs report transitions as `result` rows.
