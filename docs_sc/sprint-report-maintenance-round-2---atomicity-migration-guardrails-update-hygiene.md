---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# SPRINT REPORT: Maintenance round 2 — atomicity, migration guardrails, update hygiene (spec #78)

sprint doc: #79 (frozen) · spec: #78 (feature #31) · planner: PLN1 · closed: 2026-08-02
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3 · mode: organic (Sprint 74 shape)

## Verdict

**Spec #78 shipped.** 6/6 units merged (PRs #939, #941, #943, #940, #948, #951), main green at 3d1f9f3. Conformance (doc #82, judged against main @ 3d1f9f3): 18 requirements as-specced, 5 deviated-intentionally under six PLN1-ratified calls, **0 deviated-silently, 0 unimplemented, 0 Major / 0 Medium / 11 Low**. Flag #123 closed with the fix verified on main. All four by-catch issues (#935–#938) have fix commits; #936 stays open in reduced scope by ratified call (ordering floor shipped, transient cause undemonstrated). The U6 fleet remediation ran inside the sprint window and its acceptance PASSED: all six installs on the 3d1f9f3 floor, dos-arch's 13-tombstoned-name probe zero across every worktree (was 9). Deferred with eyes open: the 11 conformance Lows plus unit-report Lows (dispositions below), none blocking.

## Units Shipped

| seq | unit | shell | reviewer | pr | review cycles | outcome |
|---|---|---|---|---|---|---|
| 1 | Verdict atomicity (flag #123) | DEV3 | REV1 | #939 | 1 (clean) | merged, 0M/0M/0L; gap-test discrimination independently verified |
| 2 | Migration guardrails + premigrate backup | DEV4 | REV2 | #941 | 2 (1 Medium cured) | merged, re-review clean; SC-048 mutant-verified |
| 4 | Test-depth sweep | DEV5 | REV2 | #940 | 2 (1 Medium cured) | merged, re-review clean |
| 3 | Deferral doctrine (F1) | DEV3 | REV1 | #943 | 1 (clean) | merged; doctrine doc #81 |
| 5 | Update by-catch #935–#938 | DEV4 | REV1 | #948 | 2 (2 Mediums cured) | merged, re-review clean |
| 6 | Skills sweep on forks | DEV5 | REV2 | #951 | 1 (clean, 0L) | merged; fleet remediation acceptance PASS |

Planned order held exactly: 1‖2‖4 parallel, 3 after 1, 5 after 2, 6 after 5. Zero CI reds in the dev lanes (the U2 scaffold's manifest-append is why — the trap that cost Sprint 74 two reds never fired).

## Judgements Made

1. **U4/F5 boundary test** (DEV5, ratified msg #1110): "cover the reassignment edge" is a test-depth item — boundary test proving a stale old-assignee notification is not resolved by the new assignee's handoff; zero behavior change.
2. **U2 stale mirror sentence** (DEV4, ratified msg #1129): spec #78's "re-rendered skills_sc/ mirror" predates PR #726's local-only render policy; correct reading = tracked 0001 + terminal reseed + hermetic render-check. Recorded as spec debt.
3. **U3 doc vehicle** (DEV3, ratified msg #1136): no editable Sprints v2 feature doc exists — new doctrine-only doc #81 under feature 31; frozen history untouched; seeds the feature doc owed at ship.
4. **U4 guard-test restore** (PLN1 ruling msg #1143 on REV2's Medium): spec item 4 is short-circuit PLUS the string-occurrence guard — both; restored verbatim in one loop.
5. **U5 #937 mechanism** (DEV4, ratified msg #1181 with sharpening): update-time reconciliation of clean tracked worktree dispatchers + update_compat fresh-process seam + prior engine.ref recognition; local dispatcher edits preserved and **named** in output (silent skips forbidden — regression asserts the exact warning).
6. **U5 #936 at-minimum floor** (PLN1 ruling msg #1192 on REV1's SC-050): with the transient cause undemonstrable on demand, the spec's floor ships — engine.ref never advances past a failed step, ordering regression pinned; #936 remains open in reduced scope, annotated shipped-vs-remaining.

No severity disputes arose.

## Spec Accuracy

Conformance doc #82: every requirement as-specced or matching a ratified call — no silent deviations, nothing unimplemented. The five deviated-intentionally rows map 1:1 onto ratified calls (mirror sentence, doc vehicle, boundary-test reading, #937 mechanism choice, #936 floor). Unit reports' `deviations:` lines were honest — U2 declared its deviation explicitly and it matches conformance's reading; all others declared none and conformance found none.

## Issues Encountered

- **Three review Mediums, all cured in one loop each**: U4 guard-test replacement; U2 SC-048 (CLI test claiming linked-worktree coverage it lacked — rebuilt, mutant-verified); U5 SC-049 (pre-publish overlay crash-window) + SC-050 (ruled, above). Heterogeneous kimi reviewers found all three — same cross-model effect as Sprints 51/74.
- **Head-motion race defused**: U2's fix push crossed with the unit-4 merge; PLN1 ordered a rebase and held REV2 off the stale head; re-review landed on the rebased head with CI green.
- **Main-baseline noise, handled by doctrine**: two pre-existing fixture failures at zero diff from origin/main (filed #947) and render-check mirror drift (#809 updated); hosted CI stayed canonical, no phantom reds counted.
- **Cross-install kill interference (open)**: REV1's first two conformance boots and PLN1's inbox watcher were externally killed twice in simultaneous pairs while a dos-arch sprint ran on the same host. Filed as subfloor **#954** (suspect: kill-by-recorded-PID without process-identity verification; dos-arch's machinery runs in the host PID namespace). Third run under signal-catcher instrumentation completed untouched — sender not yet caught; instrumentation left documented in the issue.
- **Fleet remediation surprises** (PLN1 maintainer lane): update's engine-edit refusal exits 0 and is not a clean abort — masked two non-updates (ami, rst-c) during the sweep; recovered by re-running from repin branches. Filed subfloor **#953**. Root trap: the update's own recommended `git checkout main` flow, on a fork whose repin PR is unmerged, restores the old committed dispatcher and trips the next update's manifest guard.
- **dos-arch engine API server zombie leak observed** (adjacent, host-side server): ~76 unreaped git/gh children in ~3.6h — to be filed upstream as a follow-up.

## Fleet Remediation (U6 spec req 4 — PLN1 step)

Evidence: `shared/SPRINT79_U6_fleet_remediation.md`. All six installs adopted 3d1f9f3. **Acceptance PASS**: dos-arch 13-name tombstone probe = 0 across all 13 worktrees + main (9 pre-update). Live validations: #935 fix proven on subfloor-marketing (the repro install); #936 ordering proven on 4 genuine first-attempt successes. Repin PRs at close: dos-app#59, ami#33, rst-c#122 (advanced in place), dos-arch#1024, md-converter#77 (fresh — their f442bb2 PRs merged mid-remediation), subfloor-marketing#5 (sixth install, first tracked repin — FnB decision). Merges remain the FnB's gate.

## Deferred & Follow-ups

Conformance Lows (11, doc #82 — headline items):
- **U2**: `migration` dispatch lacks the `CALLER_ENGINE` existence guard its siblings carry; premigrate backup tests write real `~/db_backups/main` (need `SC_DB_BACKUP_DIR` override); scaffold provenance of 0160 inferential.
- **U4**: deps-help probe covers bare `pip`/`npm` only, not `pip3`/`python -m pip`.
- **U5/U6 shared shape**: worktree scope is a `.sc-worktrees/` directory scan, not `git worktree list` — stray dirs swept, out-of-convention worktrees missed; residual mid-reconcile crash window after `engine.ref.prev` rotation; `dispatcher.write_bytes` non-atomic (pre-existing).
Unit-report Lows: #938 unittest-discover fallback spec-silence; dead source-branch assignment; doc #81 title cosmetic mismatch.
Upstream open: **#953** (refusal exit code + non-clean abort), **#954** (cross-install kill — needs repro; fix shape = process-identity verification before killpg), **#936** (reduced scope), **#947**, **#809**; API-server zombie leak to file.
Process: none — the migration scaffold closed Sprint 74's process follow-up and proved itself in-sprint (both U4/U2 reseeds allocated by it, zero manifest reds).

## Spec Debt

- Unit 2 req 4's "re-rendered `skills_sc/` mirror" sentence — replace with the local-only render + hermetic render-check reading (ratified call #2). Spec #78 frozen unedited; this report is the correction of record.
- Unit 3's "Sprints v2 feature documentation" target did not exist — future specs should name the doc row or say "create it" (call #3 is the precedent).
- Unit 5 item 2's root-cause-vs-floor structure worked exactly as intended (SC-050); keep that pattern.

## Metrics

- 6 units, 6 PRs, ~2h05m declaration (10:24) to freeze (12:56 + investigation pause; conformance filed 13:54 UTC-local offset applies).
- Review cycles: 1.5 avg (3 clean firsts, 3 one-loop cures). CI reds in dev lanes: 0. Sprint 74's red class (removal-manifest) eliminated by the U2 scaffold.
- Boots: DEV3×3, DEV4×4, DEV5×4, REV1×4 (+2 killed conformance attempts), REV2×4. Zero scheduled polling; watcher-driven throughout (with kill-forced boundary-check degradation at the tail).
- By-catch: filed #953, #954, #947; updated #809; observed API-server zombie leak (to file).
