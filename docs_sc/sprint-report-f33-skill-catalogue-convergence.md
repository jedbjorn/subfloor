---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
title: "SPRINT REPORT: F33 Skill catalogue convergence"
tags: [sprint-report, f33, skills, sprint-70]
date: 2026-08-02
project: super-coder
purpose: Durable close-out record for sprint 70
---

# SPRINT REPORT: F33 Skill catalogue convergence

Sprint doc #70 (frozen) · spec doc #69 (frozen, one post-conformance ratification line) · conformance doc #71 · feature #33 · tasks #231–#237 all done · main @ `9668def`.

## Verdict

**Shipped, conforms-with-ratified-deviations.** 8 units / 8 PRs merged (7 planned + 1 inserted fix unit), main green, conformance clean: 28 as-specced, 1 deviated-intentionally (the J2/J4 foreign-preservation reading), 2 deviated-silently both Low (one ratified into the spec post-pass, one accepted as convention debt), 0 unimplemented, 0 Major/Medium findings. The dirty-fork fixture converges through update and rebuild twice, all 13 tombstones vanish from DB and disk, fork-local and foreign controls survive byte-for-byte, repeat convergence is a no-op. One deliberate process deviation: PR #924 was FnB-merged before review; its verification was completed post-merge (gate re-run + REV3 evidence sign-off) rather than pre-merge.

## Units Shipped

| seq | unit | dev | reviewer | pr | outcome |
|---|---|---|---|---|---|
| 1 | #231 baseline pin + dirty-fork fixture | DEV4 | REV1 | #911 | merged, 0 Maj/Med |
| 2 | #232 tombstone registry + DB reconciler | DEV5 | REV2 | #912 | merged, 0 Maj/Med |
| 3 | #233 migration/rebuild/update/snapshot convergence | DEV5 | REV2 | #913 | merged, 0 Maj/Med |
| 4 | #234 catalogue removals + rewrites | DEV6 | REV1 | #915 | merged, 0 Maj/Med |
| 5 | #235 curation governance | DEV6 | REV1 | #920 | merged, 0 Maj/Med |
| 6 | #236 projection reconciler | DEV5 | REV2 | #917 | merged after 2 fix loops (SC-040 Medium; flag #118 Major) |
| 7 | #237 adversarial verification + release gate | DEV4 | REV3 | #924 | FnB-merged pre-review; verification recovered post-merge (jobs 50/52/53 green, REV3 sign-off clean) |
| 8 | FIX flag #119 — foreign-preservation bound | DEV5 | REV2 | #928 | inserted pre-freeze; merged after 3 review loops (#119→#120→#121, each a real distinct hole) |

Planned order held throughout (1∥2 → 3 → 4→5 ∥ 6 → 7); unit 8 was inserted at the front under still-ACTIVE authority exactly as the pre-freeze design intends. Models: devs codex/gpt-5.6-sol, reviewers kimi/kimi-code/k3 (FnB interview).

## Judgements Made

- **J1** (msg #834/#835): staged active/tombstone disjointness — registry carried all 13 tombstones from day one while strict validation tolerated the 3 seed-owned overlaps until unit 4 regenerated the seed. Bound: tolerance mechanically scoped to generated-0001 membership, removable without other code change. CLOSED by unit 4 with negative coverage; conformance verdict 2 confirms strict-final.
- **J2** (msg #901/#903): repo-root projection is bounded-sweep, not per-grant exact — no resolved-grant owner exists at the root. UPHELD with the never-delete-foreign bound.
- **J3** (flag #119, ruled #954): merged unit-6 code deleted foreign root dirs, violating J2's bound. Ruled code-defect (J2 not amended); fix unit 8 inserted; the missing gate control landed in #928's regressions.
- **J4** (msg #995/#997): exactness ranges over engine-managed directories only — unmarked foreign dirs preserved at root AND in shell worktrees; REV3's earlier worktree-removal expectation overruled by spec text. Conformance verdict 21 files this as the sprint's one deviated-intentionally.
- **Flag #120** (ruled #964): the exact-render path for a shell rooted at the repo root bypassed the ownership bound — same invariant moved into the shared deletion primitive so no caller can bypass it.
- **Flag #121** (ruled #977): the bound over-corrected — DB-known fork-local names are engine-managed too; revocation must still converge on disk. Final invariant: delete iff name ∈ (active namespace ∪ tombstones ∪ DB-known skills) or banner-owned; foreign = never-rendered, no row.
- **Finding 1 ratification** (post-conformance): snapshot.py's live-DB heal ratified as intentional; declared in spec #69's snapshot paragraph before freeze.
- **Process**: PR #924 FnB-merged pre-review (confirmed by FnB in-session); planner converted the review to post-merge evidence sign-off rather than reverting.
- **Severity disputes:** none.

## Spec Accuracy

Conformance doc #71 (REV3, spec vs `main@9668def`, diffs and trail excluded): 28/31 requirement rows as-specced with named evidence; the 3 non-as-specced rows are the J2/J4 ratified reading (intentional), the snapshot heal (silently-deviated → ratified into the spec at close), and the dual-0155 migration numbering (accepted convention debt — ledger keys on full filename, both apply deterministically). Cross-check against unit reports: unit 3 *did* declare the snapshot heal in its deviations line — the pass filed it deviated-silently only because it was declared-but-not-ratified; no unit report claimed `deviations: none` over a real deviation.

## Issues Encountered

- Both first CI runs red on the same manifest-coverage invariant (new files referencing retired names must be inventoried) — hidden locally while files were untracked; both devs fixed in one loop.
- Unit 6 took the sprint's only mid-sprint Majors: flag #118 (test escaped isolation and truncated live `instance.json`) — caught by REV2's re-review, fixed same session.
- Unit 7's gate exposed a real `seed-skills` linked-worktree dispatcher bug (dispatched to the shared checkout, verified the wrong catalogue) — fixed inside the unit with a worktree-target regression.
- One anomalous red: clean-source verify's first attempt used a git archive and was correctly refused by the engine-floor guard; standalone-clone retry passed. Not counted against fix attempts.
- Environment: three worker/background kills mid-run (REV2 twice mid-review, REV3 once two lines into conformance) — all recovered by re-boot with no lost rows; cause external to the sprint (host-side), worth watching.
- Non-sprint traffic interleaved cleanly (#909, #910, #916, #921 merged mid-sprint); the only collision was #916 taking migration number 0155 in parallel with unit 4 (Finding 2).

## Deferred & Follow-ups

Report-only Lows, the next maintenance seed list:
1. Gate fixture lacks a foreign-dir control inside a dormant worktree's native roots (F3 + REV3 #1003) — one sentinel dir/file in the fixture closes the class.
2. Hard-deleted skill rows (snapshot/reseed `DELETE FROM skills`) can leave projected dirs on disk — the DB-registry predicate can't see them (REV2 #986 Low 1).
3. Operator dirs whose names collide with DB-registered skills are treated engine-managed and deleted from managed roots — namespace broadening since #120; deserves one line in F33 docs (REV2 #986 Low 2).
4. Projection summaries double-report deletions as writes in some paths / `sc render skills` doesn't list deleted paths (REV2 + F5-adjacent).
5. Dual-0155 migration prefixes on main + `allowed_reference_files` inventory no longer exhaustive — consider a duplicate-prefix lint (F2).
6. Boot self-heal is best-effort and swallows reconcile failure (F4) — documented-deliberate, noted.
7. Install/teardown inventories restate managed roots as literals instead of deriving from `managed_skill_dirs()` (F5).
8. Fixture Lows from unit 1: grant-replay assertion gap; baseline-pin drift guard.
9. `test_lns_curation.py` asserts an exact line-wrapped string from the curate asset — cosmetic reflow turns it red (REV1).
10. Update leg of the gate mocks the service-cutover migration chain; dirty-migration coverage rides the focused lifecycle suite (REV3).

## Spec Debt

- Snapshot heal line: PAID — written into spec #69 pre-freeze (Finding 1 ratification).
- The spec's "known shell worktrees stay exact" phrasing invited the J4 ambiguity; future projection specs should say "exact over engine-managed directories" explicitly.
- Cross-sprint migration-numbering coordination is unspecified anywhere; the sprint doc's renumber-on-rebase note only binds one sprint's units.

## Metrics

- 8 units, 8 PRs, 8/8 merged; 5 units clean on first review, 2 needed fix loops (unit 6 ×2, unit 8 ×3), unit 7 verified post-merge.
- Review findings across the sprint: 4 Major (all found by kimi reviewers, all fixed + verified), 1 Medium, ~15 Lows (all in this report). CI reds: 3 real (one shared root cause ×2, one gate-exposed dispatcher bug), 1 anomalous.
- Cross-family review (Sol devs × Kimi reviewers) earned its keep again: every Major came from a reviewer or reviewer probe, none from CI.
- Zero scheduled polling by any shell; planner event plumbing = inbox watcher + planner-owned GitHub poller (FnB-directed), both retired at close.
