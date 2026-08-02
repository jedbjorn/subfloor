---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
title: Issue Backlog Triage 2026-08-02
tags: [triage, backlog, issues]
date: 2026-08-02
project: subfloor
purpose: Disposition of 126 open issues
---

# Issue Backlog Triage — 2026-08-02

## Overview

Full triage of the 126 open issues on jedbjorn/subfloor as of 2026-08-02. Every issue was read and verified against current `origin/main` source by five read-only investigation passes, one per defect family. No issue was closed — this doc is the disposition record; closures await FnB approval.

```stats
:::class1
value: 126
label: Open issues triaged
:::class2
value: 73
label: Recommended close
description: 30 fixed, 28 obsolete, 15 duplicates
:::class3
value: 49
label: Confirmed still legit
:::class4
value: 4
label: Need re-verification
```

Timeline anchors used for obsolescence calls: Interface/TMUX strip ~Jul 27–28 (ed636a4, 8c5efc7), browser chat/diff migration Jul 28–31, Sprint v1 + Conductor rip-out Jul 30–31 (8a1c580, 659a789, migration 0144), Sprints v2 Jul 31–Aug 1 (#850–#879), skill-catalogue convergence Aug 1–2 (067cf9f…9668def), hardening wave Aug 1–2 (dc93234 #909, aca4696 #931, 446dc39 #904, f442bb2 #934).

## Close — Fixed (30)

Verified fixed in current main source; close each citing the fixing commit.

| Issue | Fixed by |
|---|---|
| 331 API 500 database-is-locked under load | 5a7edfe #768 bounded tx policy + 2563ea2 #790 WAL retry |
| 490 verify from worktree rebuilds shared DB | a06ce84 #666 — `sc_refuse_linked verify` |
| 565 render-check dispatches stale engine | a06ce84 #666 — render-check is source-pure |
| 569 migrate targets main-checkout DB | a06ce84 #666 — `sc_refuse_linked migrate` |
| 547 rebuild --help destructive | e6a94ae #663 — args parsed before state |
| 697 + 716 make banners break supervision asserts | de26f8f #751 — MAKELEVEL stripped |
| 705 no documented sprint-board creation | Sprints v2 `sc sprint declare` (sprint_prep skill) |
| 723 sprint_pln ./sc fails from worktree | 60c8ace #707 — canonical sc addressing |
| 727 update prints retired commit instructions | 7c6880e #726 |
| 728 opencode headless Skill resolution | 1dd2653 #753 — native skill tree render |
| 731 models resolve opens canonical DB | dc93234 #909 — API-routed reads |
| 747 Interface/TMUX still advertised in docs | cleaned on main (README + docs/) |
| 750 PLN1 worktree dangling gitdir | 385a557 #889 — self-heal on `sc update` |
| 758 schema comment references removed helper | removed from schema.sql |
| 773 verify refuses active sprint post-update | 9dea86b #921 — sprint state preserved |
| 774 nested Python suite reported absent | aca4696 #931 — nested manifest walk |
| 792 + 800 SSE redaction races event commit | 36fd867 #822 — single transaction |
| 818 get flags ignores --feature | f442bb2 #934 |
| 835 migration 0144 append-only trigger failure | b35cd61 #837 + a4dfac1 #838 |
| 894 complete-unit 8000-char limit undocumented | 446dc39 #904 |
| 895 resume cannot re-wake unread assignment | 446dc39 #904 — wake reconcile on resume |
| 897 reviewers sent to generic inbox | 446dc39 #904 — sprint-scoped wake prompt |
| 922 closure notes invisible to reviewers | f442bb2 #934 — resolution_notes surfaced |
| 925 pre-declaration QAQC needs Sprint id | f442bb2 #934 — sprint_rev entry condition |
| 926 deps --help mutates (.venv) | aca4696 #931 — help-form guard |
| 733 + 872 + 919 seed-skills runs shared-checkout source | c7b4761 #924 — caller-engine required |

## Close — Obsolete (28)

Target surface no longer exists on main, or the complaint is out of scope for the subfloor tracker. Close as not-planned.

**Interface/TMUX strip** (ed636a4 #684, 8c5efc7 #687): 498, 544, 555, 682, 689 — spike suites, tmux liveness, pane recovery: all deleted with `spikes/` and the Interface runtime.

**Sprint v1 + Conductor rip-out** (8a1c580 #829, 659a789 #830, migration 0144): 454, 561, 638, 677, 678, 683, 696, 706, 754, 795, 801, 820, 821, 823, 824 — wake bindings, sprint-bindings endpoint, Conductor CLIs/roster/config, v1 PR poller, one-shot runtime: all excised. Sprints v2 replaced these surfaces wholesale (its own defects tracked separately below).

**Self-resolved residue**: 715 (drifted `sprint_cond` artifact no longer exists — skill retired), 876 (`sprint_ref` column dropped by 0144; one-time dirty-upgrade hazard already handled by #837).

**Skill catalogue retirement** (067cf9f #915): 848, 864 (test_authoring_sqlite deleted), 866 (test_authoring reworded; premise also inaccurate — pytest is the preferred runner).

**Works as designed**: 874 — Sprints v2 spec-approval writer exists (`sc sprint record-qaqc`, reviewer-flavor gated); reporter invoked it from a Planner shell.

**Out of subfloor scope**: 832 + 901 — the source-maintenance skill is fork-local (home substrate), not a subfloor asset. Real contradiction, wrong tracker: becomes a home-substrate flag for the FnB.

## Close — Duplicates (15)

Close pointing at the surviving canonical.

| Duplicates | Canonical | Family |
|---|---|---|
| 718, 748, 905 | **699** | update --help executes full workflow — `update.py:main()` has no help handling |
| 752 | **737** | --ref rejects abbreviated SHAs — raw string passed to `git fetch` |
| 854, 865, 878, 797, 890, 900, 927 | **785** | `sc lint` has no ruff config anywhere in history — bare defaults, repo-wide, no baseline |
| 734 | **720** | dev-kit `SC_ROOT`/PATH design makes bare `sc` escape to main checkout |
| 719, 809 | **729** | render-check prints `./sc rebuild && ./sc render flat` without the required `SC_ADMIN=1` |
| 739 | **537** | `sc engine-ref` path invalid in the source repo (no `.sc-state/engine.ref`) |

## Keep — Confirmed (49)

All verified still present in current main source, with file:line evidence on the issue-level agent findings.

### High priority

| Issue | Why it matters |
|---|---|
| 365 (P1) | `sc mem doc edit` with a zero-byte body-file silently destroys a live document — no emptiness guard anywhere in the path |
| 448 (P1) + 738 | task INSERT has no guard on seq collision or invalid document_id — uncaught IntegrityError → HTTP 500. Same root cause; merge 738 into 448 |
| 859 | `unittest discover` leaks mutated `DB_PATH`/API globals across modules — full test discovery can roll back the live engine DB |
| 937 | linked worktrees keep their branch's stale tracked `sc` dispatcher; no heal path rewrites it, so every dispatcher fix (e.g. 926) resurrects in worktrees |
| 907 | materialize never prunes upstream-deleted files — ghost migrations break hermetic replay, ghost skill assets resurrect retired skills. Docstring admits the gap; no doctor sweep exists |
| 629 | render_path confinement violation aborts the whole render pass; frozen doc cannot be repaired and no unfreeze exists — blocks all publishing. Compound of 376 |
| 769 | `sc verify` unconditionally refuses linked worktrees while source-maintenance and git skills require working from one — actively reproduced through Aug 2, no resolution path on main |
| 853 | test suite leaks one mkdtemp dir per run (5 call sites, no cleanup) — filled a 7.6G tmpfs |

### Medium

- **Update/materialize**: 699 (canonical --help mutation), 737 (canonical --ref SHA), 935 (`super_coder_remote()` substring match picks fork's own origin), 886 (canonical transient migration FK-then-succeeds) + 936 (broader: adds distinct snapshot.py first-attempt leg — keep both), 581 (local-edit guard compares disk to manifest, never to git HEAD), 906 (render-check hermetic build ignores fork retire list)
- **Worktree/dispatch**: 720 (canonical dev-kit escape), 729 (canonical remedy-omits-admin-gate), 702 (./sc absent from worktree — same family as 720), 709 (cartographer skill: extractor source path lacks `$SC_ROOT` prefix), 685 (map-sql raw sqlite error in clean clone), 732 (headless claude hangs on untrusted workspace — codex has trust handling, claude has none)
- **Lint/test**: 785 (canonical: no ruff config has ever existed), 851 (explicit pytest targets silently fall back to unittest), 938 (venv self-heal gates on pytest existence, not runnability; stdlib fallback hardcodes `-s tests`)
- **Memory API**: 376 (freeze irreversible), 509 (no doc delete/soft-delete), 882 (no task reopen; cancelled rows burn seq slot), 813 (boot render + messaging skill still advertise removed `--message` form; live command is `sc mem message`)
- **Sprints v2**: 898 (complete-unit resolves --result-file against engine cwd, not the worktree — only surviving v2 defect)
- **Misc**: 836 (teardown reports partial on empty `.sc-worktrees`), 537 (engine-ref invalid in source repo)

### Low

392 (roadmap depends unreadable), 482 (task start mutates under frozen spec), 670 (narrative has no redaction path — PII permanent), 681 (db_map says ~500 chars, trigger caps 300), 789 (worktree has no provisioned ruff — arguably by design), 552 (sandbox image lacks make), 784 (Interface-slug substring assertion), 819 (IPv6 websocket CSP console noise), 445 (git skill: gh merge cleanup fails in linked worktree), 451 (shortname case mismatch in branch naming)

### Docker sandbox seat cluster

383, 385, 386, 388, 389, 395, 396 (Jul 17–18, filed from a Docker-sandbox fork). All verified still-real defects. **FnB ruling (decision #62): docker is PRIMARY — all forks run docker; bare metal exists to maintain docker.** The whole cluster is therefore mainline keeper work, batched as flag SC-045 (High). 552 (sandbox image lacks make) rides in the test-hygiene batch for the same reason.

## Needs Repro (4)

| Issue | Situation |
|---|---|
| 831 | flag close 404s by id — current GET/PATCH routes are symmetric with no owner scoping; the reported behavior has no explanation in current code. Re-verify; close if unreproducible |
| 679 | `REV1_mutation_verification` has zero hits in all of subfloor history — fork-local boot drift, not a subfloor defect. Close here; re-file at home if it recurs |
| 690 | backup-dir override originates in the sc-cachy managed-worktree launcher, not subfloor source — out of scope here; verify home-side |
| 450 | kcsos-fork mypy dual-module layout; mechanism plausible (`sc typecheck` lacks `--explicit-package-bases`) but not reproducible in subfloor's own tree |

## Flag Batches

Per FnB direction (2026-08-02), all 49 keepers are batched into eight flags, each scoped so one spec produces one patch:

| Flag | Priority | Issues | Patch shape |
|---|---|---|---|
| SC-040 update.py hygiene | Medium | 699, 737, 935, 886+936, 581 | update.py + sc dispatcher |
| SC-041 materialize + render-check integrity | Medium | 907, 906 | materialization/hermetic build |
| SC-042 mem-API write-safety | High | 365, 448+738, 376+629, 509, 882, 482, 392, 670 | server.py + mem.py |
| SC-043 test + lint hygiene | Medium | 859, 853, 785, 789, 851, 938, 784, 552, rider 450 | test runner, suite fixtures, ruff config, Dockerfile |
| SC-044 worktree dispatch + addressing | High | 937, 720, 702, 769, 729, 709, 685, 732 | dispatcher heal + skill/remedy text |
| SC-045 docker sandbox seat | High | 383, 385, 386, 388, 389, 395, 396 | run.py/job.py/db access on the docker seat |
| SC-046 skill + boot text residue | Low | 813, 537, 445, 451, 681 | text-only PR |
| SC-047 sprints-v2 + misc small fixes | Low | 898, 836, 819 | small mechanical PR |

Cross-links: 383 overlaps 938 (venv self-heal — implement once, close both); 629 depends on the unfreeze/repair verb specced with 376; 769 also tracked by existing flag SC-019.

## Remaining Actions

1. **FnB approves the closure list** → 73 closures executed with per-issue comments citing fixing commit / removing commit / canonical duplicate; needs-repro items (831, 679, 690, 450-as-standalone) close as unreproducible-here with a re-file note.
2. **Caution on 331**: verified fixed (5a7edfe + 2563ea2), but existing flag SC-023 claims a post-fix recurrence under Sprint load (BROKER_RUN_ERROR). Verify SC-023's incident post-dates the fix before closing 331.
3. **Existing flags resolvable by this triage** (their owners / FnB to close): SC-011 (#773 fixed by #921), SC-021 (#820 obsolete), SC-022 (#821 obsolete), SC-019 (folds into SC-044 / home-side 832 follow-up).
4. **Home-substrate follow-ups** (not subfloor PRs): 832/901 source-maintenance contradiction; 690 launcher override; 813's fix lands in subfloor but the home install picks it up only on the next engine update (currently under SC-005 rollout HOLD).
