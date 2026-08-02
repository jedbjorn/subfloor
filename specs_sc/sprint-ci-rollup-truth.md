---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: CI-truthful check rollup normalization
tags: [sprint, watcher, github, defect-fix]
date: 2026-08-02
project: super-coder
purpose: Never project green while checks run
---

# CI-truthful check rollup normalization

## Overview

> [!class4]
> The PR watcher can project **green** while the gating CI suite is still queued or running. Decision #36 makes green an *active* wake to the owning Developer — so this defect hands a dev an actionable merge signal before pytest has run. Observed live in dos-arch Sprint 3 (U7: pytest run 30749547786 queued→in_progress while the unit sat merge_ready); the Reviewer held the merge back by hand.

This spec fixes the defect at its single source: the check-rollup normalization seam in `github_pull_requests.py`. One function changes; the watcher, transitions, and routing above it are untouched.

**Governing principle: uncertainty projects pending, never green.** A rollup item the normalizer cannot positively classify as terminal-success must hold the PR out of `green`.

## Defect

`_check_state()` (`.super-coder/scripts/github_pull_requests.py:137-155`) derives each rollup item's verdict from `item.get("conclusion") or item.get("state")`.

GitHub returns two item shapes inside `statusCheckRollup`:

| Shape | Fields carried | Used by |
|---|---|---|
| `StatusContext` | `state` (EXPECTED / ERROR / FAILURE / PENDING / SUCCESS) | legacy commit statuses |
| `CheckRun` | `status` (QUEUED / IN_PROGRESS / COMPLETED / WAITING / PENDING / REQUESTED) + `conclusion` (null until COMPLETED) | **GitHub Actions** |

A `CheckRun` has **no `state` key**, and `conclusion` is null until it completes. So for a queued or in-progress Actions run the expression evaluates falsy and the item **silently drops out of the computation**. Two failure shapes follow:

- **Mixed rollup** — fast checks done SUCCESS, pytest queued: surviving states are all SUCCESS → `("SUCCESS", False)` → `normalize_state` projects `green` → the watcher routes an active green wake to the owner (`sprint_pr_watcher.py:509-510`). This is the premature green.
- **All-queued rollup** — states list empty → `(None, False)` → `created`, masking that checks exist and are owed.

The tell that this is a field mix-up, not design: the pending set at line 150 already lists `"QUEUED"` and `"IN_PROGRESS"` — values that only ever appear in the CheckRun `status` field the code never reads. Those entries are dead letters today.

## Evidence

Verified live against subfloor PR #951 (`gh pr list --json statusCheckRollup`, 2026-08-02): every CheckRun item carries exactly `__typename, completedAt, conclusion, detailsUrl, name, startedAt, status, workflowName` — no `state` key. The engine's own deterministic fixtures (`tests/fixtures/review/github_prs.json`, `tests/test_review_contract_fixtures.py:286-291`) use the CheckRun shape too — but always with **terminal conclusions**, which is why the bug never surfaced in the suite: no fixture anywhere exercises a queued/in-progress CheckRun, and none exercises a `state`-only StatusContext item.

## Design

Per-item verdict chain becomes: **`conclusion` → `state` → `status`** (first non-empty wins, uppercased).

Classification sets:

| Verdict | States | Change |
|---|---|---|
| failed | ACTION_REQUIRED, CANCELLED, ERROR, FAILURE, STALE, STARTUP_FAILURE, TIMED_OUT | unchanged |
| pending | PENDING, EXPECTED, IN_PROGRESS, QUEUED, WAITING, REQUESTED | +WAITING, +REQUESTED |
| success | SUCCESS, NEUTRAL, SKIPPED | unchanged |

Tightened rules, in precedence order:

1. Any item in the failed set → `("FAILURE", True)` — unchanged.
2. Any item in the pending set → `("PENDING", False)`.
3. `COMPLETED` surfacing as a verdict (status reached the chain because conclusion was null on a completed run — defensive; not observed) → **pending**, never success.
4. Any **unrecognized** non-empty verdict → **pending**. This replaces the current `return (states[0], False)` passthrough, which lets an unknown state string reach `normalize_state` and fall to `created`.
5. All items positively in the success set → `("SUCCESS", False)`.
6. Empty rollup (no checks attached) → `(None, False)` → `created` — unchanged.

`green` therefore requires every rollup item to be positively terminal-successful. `normalize_state()` itself does not change.

## Implementation

- `.super-coder/scripts/github_pull_requests.py` — `_check_state()` only; extract the two state sets as module constants next to `_FAILED_CHECKS`.
- No schema change, no watcher change, no API change.
- **Downstream effect:** on deploy, any registered PR currently showing `green` with a queued/running CheckRun will transition `green → pending → green` as the suite completes. Transitions are append-only and keyed on (state, head_sha); the extra transition rows and notifications are correct behavior, not churn to suppress. Note it in the PR description so reviewers expect it in fork timelines.

## Edge cases

- **Failure precedence** — a failed item wins over pending items in the same rollup (rule order 1 before 2): a red suite must not read pending.
- **Mixed StatusContext + CheckRun rollup** — both shapes classified by the same chain; StatusContext continues to resolve via `state`.
- **CheckRun WAITING / REQUESTED** (deployment-protection holds, requested-not-created) — pending.
- **StatusContext compatibility** — the chain keeps `state` in second position, so legacy commit statuses classify unchanged in production. No existing fixture exercises `state`-only items: the dev **adds** StatusContext-shaped fixture entries rather than assuming coverage exists.

## Verification

- **Unit** (`tests/test_git_review.py` + fixture additions in `tests/fixtures/review/github_prs.json` and the contract fixtures): queued CheckRun + SUCCESS CheckRun → PENDING; all-queued → PENDING; COMPLETED with null conclusion → PENDING; unknown state string → PENDING; failed + queued → FAILURE; all COMPLETED/SUCCESS → SUCCESS; plus **new** StatusContext-shaped fixture items (`state`-only: PENDING, SUCCESS, FAILURE) and a mixed StatusContext+CheckRun rollup.
- **Watcher-level** (`tests/test_sprint_pr_watcher.py`): a rollup with one queued CheckRun and one successful one produces a `pending` transition and **no** green wake to the owner; the green wake fires only after the last run concludes SUCCESS.

## Out of scope

- **Required-checks awareness** (branch protection): green still means "every *attached* check succeeded". The post-push window where a required workflow's run has not yet attached (empty rollup → `created`) remains; closing it needs the branch-protection API and is a separate feature if wanted.
- **`pr.poll_failed` backoff persistence** — the watcher's failure backoff is in-memory and resets on engine restart (`sprint_pr_watcher.py:236`). Same subsystem, different defect class; tracked as a follow-up flag, not this spec.
