---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT: Interface-backed planner wake
status: ACTIVE
declared: 2026-07-22 · planner: PLN1
models: devs=kimi/kimi-code/k3 · reviewers=kimi/kimi-code/k3 (switched from codex/gpt-5.6-sol on 2026-07-22 — Codex cyber content-filter refuses Interface ws/PTY/auth/broker review; decision #21)
spec: doc #20 (feature #14) · gate unit: seq 3 (feasibility spike)

| seq | unit | task | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|---|
| 1 | `sc mem task edit` verb — resolves SC-010 | new | DEV4 | REV1 | — | feat/sc-mem-task-edit | #492 | MERGED @c5abaa1 (SC-010 resolved) |
| 2 | Doc render/snapshot pipeline fix (subfloor#434) — resolves SC-008 | new | DEV4 | REV1 | main | fix/doc-render-serialize; fix/doc-render-single-writer | #493 ✅; #494 ✅ | ✅ DONE. #493 @6b8f93c (SC-008 headless render) + #494 @134e3d5 (safe single-writer, SC-014 race code reverted). main green @134e3d5. SC-012/013/014 → decision #20 / roadmap #21. Engine foot-guns → flag #32. (#495 strip cancelled by planner — #494 final; my late redirect, not dev error.) |
| 3 | Stream + input-broker feasibility spike — HARD GATE | #79 | DEV3 | REV1 | — | feat/interface-stream-spike | #496 | ✅ DONE — GATE PASSED + MERGED @81756b1 (squash). REV1 (Kimi) review-clean 0/0: fence proofs re-run 12/12, both defect fixes verified, CI 6/6, stack license-clean. RATIFIED DEVIATION: crash-window/delivery_unknown parking proof → seq 4 (decision #22); hard condition: seq 4 implements+proves parking before any wake/retry (seq 6) ships. 4 Lows → report. Pinned stack + DESIGN.md/RESULTS.md on main as seq-4 reference. |
| 4 | Session schema + state machines | #80 | DEV4 | REV1 | 3 | feat/interface-session-schema | #500 | ✅ MERGED @6a2b8ec. Flags #33-37 closed, CI 6/6. Decision-#22 crash-window re-proof HOLDS under corrected fence. 4 ambiguities accepted (decision #23). Spec-debt: hook-auth-on-generations, incomplete edge list. W1 OPEN. |
| 5 | One-shell Interface vertical slice | #81 | DEV3 | REV1 | 3,4 | — | — | BUILDING (W1; off main @6a2b8ec). DEV3 unblocked — 2 zombie codex orphans (921/10637+child) reaped from dev3 worktree (flag #38; tooling → roadmap #22). |
| 6 | CLI parity + full Interface workflow | #82 | DEV3 | REV1 | 5 | — | — | waiting |
| 7 | Cross-harness lifecycle adapters | #83 | DEV4 | REV2 | 4,5 | — | — | waiting |
| 8 | Transactional brokered planner wake | #84 | DEV3 | REV2 | 4,5,6,7 | — | — | waiting |
| 9 | Watched-PR polling + daemon cutover | #85 | DEV4 | REV2 | 4 | — | — | BUILDING (W1; off main @6a2b8ec) |
| 10 | Operator/sprint workflow + skills | #86 | DEV4 | REV2 | 6,7,8,9 | — | — | waiting |
| 11 | Conformance + real-sprint gate | #87 | REV1+REV2 | — | 1–10 | — | — | waiting |

## Gate discipline

Seq 3 (feasibility spike, task #79) is a **ship-gate**. It runs alone; seq 4–11
are not booted until it proves green. Any silent loss, duplicate, bypass, or
interleaved input on the stream/broker stack stops the build and returns spec
#20 to the planner for rescope — dev does not work around it.

Phase-0 tooling (seq 1–2) has no code dependency on the gate and runs in
parallel with it. Seq 4 (schema) waits on both the gate green **and** seq 1
landed, so its QA contract is aligned via `sc mem task edit` before build.

## Parallel windows

- W0 (immediately): seq 3 spike (DEV3) ∥ seq 1→2 tooling (DEV4)
- W1 (after seq 4): seq 5 (DEV3) ∥ seq 9 (DEV4)
- W2 (after seq 5): seq 6 (DEV3) ∥ seq 7 (DEV4)
- Converge: seq 8 → seq 10 → seq 11

Critical path: 3 → 4 → 5 → 6 → 8 → 10 → 11.

## Status legend

waiting → building → pr-open → in-review → fixing → merged (ci-red interleaves
from pr-open on).

