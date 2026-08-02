---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: Maintenance round 2 — atomicity, migration guardrails, update hygiene
status: CLOSED
declared: 2026-08-02 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3

spec: doc #78 (feature #31) · tasks #242–#247 · decision #61 (backup + sweep policy)
mode: organic (FnB-directed, Sprint 74 shape) — PLN1 boots all workers and runs its own inbox watcher.

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | U1 verdict atomicity (flag #123) | DEV3 | REV1 | — | fix/sprint-review-verdict-atomicity | #939 | merged |
| 2 | U2 migration guardrails + premigrate backup | DEV4 | REV2 | — | feat/migration-guardrails | #941 | merged |
| 4 | U4 test-depth sweep | DEV5 | REV2 | — | chore/sprint79-u4-test-depth | #940 | merged |
| 3 | U3 deferral doctrine (F1) | DEV3 | REV1 | unit 1 (DEV3) | chore/sprint79-deferral-doctrine | #943 | merged |
| 5 | U5 update by-catch #935–#938 | DEV4 | REV1 | unit 2 (DEV4) | fix/sprint79-update-bycatch | #948 | merged |
| 6 | U6 skills sweep on forks | DEV5 | REV2 | unit 5 (DEV4) — shared update.py surface | feat/sprint79-skills-sweep | #951 | merged |

Wave 1 (parallel): units 1, 2, 4. Wave 2: unit 3 after 1 (same liveness surface, same dev); unit 5 after 2 (rides the U2 scaffold); unit 6 after 5 (both edit update.py — serialized to avoid conflicts).

Post-merge inside the sprint window (PLN1, not a dev lane): U6 fleet remediation — run the fixed update across installed forks; acceptance = dos-arch worktrees probe clean for all 13 tombstoned skill names, evidence captured durably.

FLEET REMEDIATION: DONE — all six installs (ami, dos-app, dos-arch, md-converter, rst-c, subfloor-marketing) on 3d1f9f3. **U6 acceptance PASS**: dos-arch 13-name probe = 0 across all worktrees + main (was 9). Live validation: #935 fix proven on subfloor-marketing; #936 ordering proven on 4 genuine first-attempt successes. By-catch: 2 refusals (ami, rst-c) exited 0 and were not clean aborts — filed subfloor #953; recovered by re-running from repin branches. Repin PRs: ami#33, rst-c#122, dos-app#59 advanced to 3d1f9f3; dos-arch#1024 + md-converter#77 opened fresh (their f442bb2 PRs merged mid-remediation); subfloor-marketing#5 opened (sixth install, FnB decision). Evidence: shared/SPRINT79_U6_fleet_remediation.md.

Judgements: (1) U4/F5 ambiguity (DEV5, msg #1109) — no-behavior-change vs "cover reassignment edge"; chose boundary test (stale old-assignee notification not resolved by new assignee handoff). PLN1 RATIFIED (msg #1110); reseed-numbering coordination with U2 noted, U4 told not to block on the scaffold. (2) U2 ambiguity (DEV4, msg #1124) — spec #78 Unit 2 says "re-rendered skills_sc/ mirror", stale since PR #726 retired tracked renders; DEV4 chose local-only render policy + hermetic render-check. PLN1 RATIFIED (msg #1129); recorded as spec debt on spec #78 (do not edit the sentence mid-sprint — conformance judges against the ratified call).

Unit 1 report (msg #1125): shipped = verdict persistence + notification resolution commit atomically, fresh and replay; judgements/issues/deviations/follow-ups: none.

Unit 6 report (msg #1211): shipped = ref-keyed fresh-process managed-skill projection sweep across installed forks incl. dormant worktrees, marker recorded only after success, idempotent rerun; review 0M/0M/0L; judgements/issues/deviations/follow-ups: none.

Unit 5 report (msg #1201): shipped = exact engine-remote selection (#935), atomic engine.ref publish after successful migrate/map/snapshot (#936 floor), clean-worktree dispatcher reconciliation with named dirty skips (#937), venv-runnability rebuild before pytest selection (#938); judgements = both PLN1-ratified (msgs #1181, #1192); issues = #947 filed, #809 updated, SC-049/SC-050 cured in one loop, no PR CI reds; deviations none; follow-up Lows = #938 unittest-discover fallback spec-silence, dead source-branch assignment, .sc-worktrees vs git-worktree-list scope inconsistency, residual mid-reconcile crash window after prev-rotation, non-atomic dispatcher write_bytes.

Unit 2 report (msg #1176): shipped = migration guardrails with caller-source dispatch, local-only render + hermetic render-check, premigrate backup coverage; judgements = stale mirror requirement → local-only policy (ratified msg #1129); deviations = no tracked skills_sc mirror re-rendered (retired by PR #726, ratified); issues = SC-048 Medium cured in one loop, mutant-verified, no CI reds; follow-up Lows = backup-test hermeticity (SC_DB_BACKUP_DIR), CALLER_ENGINE guard on migration dispatch.

Unit 4 report (msg #1162): shipped = maintenance-depth regressions (cancellation wording, reassignment ownership, pip/npm help purity, short-circuit discovery with runtime+source guards, actionable-kind centralization, flags human-output via reseed 0161); 1 review Medium cured in one loop; no CI reds; Lows = deps-help probe misses pip3/python -m pip shapes (F7 literal satisfied), PR #941 adjacency rebase risk (PLN1 warned DEV4, msg #1163).

Unit 3 report (msg #1156): shipped = FnB-completion deferral pinned to developer-run ownership with regression coverage (planner/developer close intent deferred while developer runs) + doctrine doc #81; judgements = doc-vehicle ambiguity ratified (msg #1136); deviations none; follow-up Low = doc #81 title mismatch (cosmetic).

Judgement (3): U3 doc-vehicle ambiguity (DEV3, msg #1133) — no editable Sprints v2 feature doc exists; chose a new concise feature-scoped doctrine doc (kind='doc', feature 31 → doc #81), frozen history preserved. PLN1 RATIFIED (msg #1136); doc seeds the feature doc owed at ship, PLN1 folds at close.

Judgement (4): U4 review Medium (REV2, msg #1142) — string-occurrence guard test replaced, not maintained. PLN1 RULED restore (msg #1143): spec item 4 is short-circuit PLUS guard test, both. Unit 4 → fixing; 2 Lows to the report. Fix pushed, green @ 1cbd09d — awaiting REV2 re-review.

Unit 3 review-clean (REV1, msg #1151): 0M/0M; Low for report — doc #81 DB title vs front-matter title cosmetic mismatch. Flag #123 closed (msg #1146), closure verified by REV1 against main @ aa4e9ca1 (resolve_in_transaction inside sprint.review.outcome txn + abort-after-commit test).

Unit 2 review (REV2, msg #1155): 0M/1Med/2Low. Medium SC-048 — misnamed CLI test claims linked-worktree dispatch coverage it lacks (cwd=ROOT); real worktree-target case requested; DEV4 fixing. Lows for report: premigrate backup tests write real ~/db_backups/main (no SC_DB_BACKUP_DIR override, non-hermetic); 'migration' dispatch lacks the CALLER_ENGINE existence guard render-check/seed-skills carry. Rest conformant to U2 reqs 1-4 + gate.

Unit 2 re-review (REV2, msg #1174): review-clean 0M/0M — SC-048 cured, mutant-verified red, flag #132 closed. Verdict quotes fix commit ee2bb5f; PR head is the rebased 1ad0613 (rebase-only delta, 0160/0161 adjacency, CI green at head; verdict postdates the re-request — PLN1 accepts). 2 first-round Lows stand for the report. Merge unlocked (msg #1175: DEV4 merges then starts unit 5).

Judgement (5): U5 #937 mechanism (DEV4, msg #1180) — update-time reconciliation of clean tracked worktree dispatchers + update_compat seam + prior engine.ref recognition; local dispatcher edits preserved (old launchers cannot self-detect on first update). PLN1 RATIFIED (msg #1181) with sharpening: skipped dirty-dispatcher worktrees must be named in update output, never silent.

Issues (baseline, not sprint-caused): U5 verification found 2 main-baseline test failures (test_local_skill_persistence, missing resolved_shell_skills fixture view) at zero diff from origin/main — filed subfloor #947; render-check local mirror drift from merged skill units — pre-existing remedy defect #809 updated. Unit 5 diff touches neither; hosted PR CI canonical. No phantom reds counted.

Judgement (6): U5 review Mediums (REV1, msg #1191) — SC-049 crash-window overlay unrecognizable: PLN1 ordered fix + crash regression. SC-050 #936 root cause undemonstrated: PLN1 RATIFIED the spec's at-minimum floor (engine.ref never advances past a failed step; ordering regression required; subfloor #936 stays open in reduced scope, annotated shipped-vs-remaining) — msg #1192. 3 U5 review Lows to the report. Unit 5 → fixing.

Severity bar: Major/Medium block, Low goes to the report. Conformance pass before freeze, against the spec (doc #78) on main at the final merge sha.

CONFORMANCE (doc #82, main @ 3d1f9f3): PASS — 18 as-specced, 5 deviated-intentionally (all six ratified calls accounted), 0 deviated-silently, 0 unimplemented; 0 Major / 0 Medium / 11 Low observations (dispositioned in the sprint report). Note: REV1's first two conformance boots were externally killed (cross-install kill, subfloor #954); third run under signal-catcher instrumentation completed clean.

CLOSED 2026-08-02 — freeze follows this edit; all scoped authority revoked.
