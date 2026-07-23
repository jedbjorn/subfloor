---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT: Interface corrective hardening (spec 30)
status: ACTIVE
declared: 2026-07-23 · planner: PLN1

models:
- default lane — devs=codex/gpt-5.6-sol · reviewers=codex/gpt-5.6-sol
- security-adjacent units (1, 4, 8, 9b) — devs=kimi/kimi-code/k3 · reviewers=kimi/kimi-code/k3
- fallback ladder for kimi units (FnB Kimi budget draining; per decision #39): L0 kimi dev / kimi review → L1 (kimi wall) opus dev / sol review → L2 (sol refuses as reviewer) opus dev / terra review

Governing spec: doc #30 (feature 14). AMI = restricted host Admin seat; step 10 acceptance is the FnB-coordinated gate, not a booted worker. Reviewers: Major/Medium block, Low goes to the report.

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Lifecycle convergence — cancel start, shared closure helper, hook/stop races, API error mapping, #526 low follow-ups (#519 #523 #532) · **kimi** | DEV5 | REV2 | — | fix/lifecycle-convergence | #541 | **merged** @f54faad (head af27925; SC-064/065 fixed; 2 Low → report) |
| 2 | Client state + layout + model picker — attach/control gating, terminal fluid to 1300×850, alert clarity, harness-first picker (#522 #527 #534 #535) · sol | DEV3 | REV1 | 1 ✅ | fix/client-state-layout-model-picker | #546 | pr-open · **ci-red INHERITED from main aa7a460** (own tests/verify green); HOLDING for main repair |
| 3 | Update + snapshot integrity — pre-mutation refusal, ended-drain, FK-closed snapshot, orphan cleanup (#528 #529 #533) · sol | DEV4 | REV1 | 1 ✅ | — | #545 | pr-open |
| 4 | Admin API + CLI parity — runtime credential discovery, lazy websockets loading (#516 #518) · **kimi** | DEV6 | REV2 | — | feat/s31-admin-api-cli-parity | #540 | **merged** @40bf072 (4 Low → report) |
| 5 | Restricted supervision — backup fallback, launch/restart --no-build preflight, full `dos-r` bounce + health (#530 #531) · sol | DEV3 | REV1 | — | fix/restricted-supervision | #539 | **merged** (SC-063 fixed; 3 Low → report) |
| 6 | Diagnostics + map — liveness fail-closed, `.sc-worktrees` map skip (#517 #524) · sol | DEV4 | REV1 | — | — | #538 | **merged** (2 Low → report) |
| 8 | Unified shell recovery — preview/execute API, exact process-group verify, atomic stale closure, worktree-preserving (roadmap #22, flag #38) · **kimi** | DEV5 | REV2 | 1 ✅, 4 ✅, 6 ✅ | feat/unified-shell-recovery | #551 | in-review (40 new tests, suite green) · **ci-red INHERITED from main**; HOLDING |
| 9a | Rich launcher + Make surface — grouped chooser on API-backed `sc enter`, `aliases.mk` operator surface + help coverage (#521-adjacent) · sol | DEV3 | REV1 | 4, 8 | — | — | waiting |
| 9b | `./sc token` + `make dos-token` — owner-only runtime credential read to stdout (#516/#518 token) · **kimi** | DEV6 | REV2 | 4 ✅ | feat/s31-sc-token | #542 | **merged** @62548b8 (CodeQL suppression FnB-approved + conformant; 2 Low → report) |
| 10 | AMI acceptance + close — 16-issue reproduction matrix on restricted seat, cross-harness (Claude/Codex/Kimi) sprint path, conformance + report, freeze | AMI/FnB | — | 1–9 | — | — | waiting |

## Sequencing notes

- **Boot now (no deps):** U1, U4 (kimi), U5, U6 (sol) — one per dev shell.
- **U2, U3** build locally now, gate on U1's lifecycle error codes / terminal-closure semantics before merge.
- **U8** consumes U1's lifecycle + U6's liveness + U4's credential surface.
- **U9a** launcher presentation + tests may begin in parallel; final command wiring waits on U4 + U8.
- **U9b** waits on U4's runtime credential artifact.
- **U10** is the release gate — AMI restricted-seat matrix + cross-harness sprint, run with the FnB; conformance pass precedes freeze.
- **Carry-forward into U9a:** REV2 watch-item from the unit-9b review — pin automated help-coverage for `dos-token` in the Make surface tests.
- **Sprint convention (3/3 units tripped it):** AMI issues use `Refs #NNN`, never `Closes` — they close only at U10. Logged as Spec Debt for the report.
- **For the conformance pass — spec debt:** req 23's "distinct nonzero result" (service-not-running vs unsafe permissions) was implemented message-level only, both exit 1. Reviewer scoped it Low; conformance should adjudicate whether the requirement means distinct *exit codes*, and the spec should say so explicitly either way.
- **INCIDENT (main red, ~30 min, 3 units blocked):** external merges #506 (aa7a460) + #510 (00f168e) landed a `sprint_orchestration` skill whose seeded body disagreed with its asset, turning **main itself** red on `render-check` (skills_sc mirror drift) + `tests` (`test_skills_freshness`). Units 2, 3 and 8 all inherited the failure; all three devs correctly diagnosed it as external and held rather than absorbing it (DEV3 reverted an out-of-scope repair it had started). Root-cause chain: the main checkout was a day stale (81756b1) so GUI-published fixes (#549) carried an ancient base; `.sc-state/content.sql` loads AFTER migrations and silently overrides a reseed migration. Resolved by FnB reverting both merges → main green at 5d15510. Engine defects surfaced: `./sc rebuild --help` EXECUTES the rebuild (issue #547, cost DEV3 a live-DB restore); planner `sc mem doc edit` snapshots into whatever branch the shared main checkout is on, and gutted migration 0081 (444→5 lines) when that checkout sat on a PR branch.
- **Flake watch (3 events):** U6 `test_interface_wake` SQLite `-shm` teardown race; U1 `test_bounded_buffers` slow-consumer stall; U9b silent `git-remote-https` push hang. All anomalous, none patched as code defects — pattern noted for the report's Issues.

Bottleneck to watch: U1 is the critical-path foundation (U2, U3, U8 all wait on it). Kimi burn on U1+U4 running in parallel — if Kimi strains, U1 holds priority and U4 drops to the L1 ladder rung first.
