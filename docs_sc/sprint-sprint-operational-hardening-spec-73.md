---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: Sprint operational hardening (spec #73)
status: CLOSED
declared: 2026-08-02 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3

QAQC gate: satisfied — FnB certified the review of spec #73 directly at declaration (2026-08-02).

Mode: **organic sprint** (FnB directive) — PLN1 boots every worker and runs its own inbox watcher; the native sprint machinery (`sc sprint …`) is the subject under repair in this spec and is NOT the coordinator of this sprint. Participants run the message-row `sprint` skill loop: build → PR + watch → CI → sprint review → merge on green+clean → unit report.

Spec: doc #73 (feature #31). Issues closed: #923 #929 #925 #926 #922 #774.

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Terminal completion deferral + in_review liveness resolution (spec Unit 1) | DEV3 | REV1 | — | fix/sprint-terminal-liveness | #933 | merged |
| 2 | Resolved-flag reads + five sprint role skills + final skill audit (spec Unit 2) | DEV4 | REV2 | 1 | fix/sprint74-native-role-evidence | #934 | merged |
| 3 | CLI hygiene: `sc deps --help` read-only + nested Python test discovery (spec Unit 3) | DEV5 | REV1 | — | fix/sprint74-cli-hygiene | #931 | merged |
| 4 | Downstream-style proof sprint — report-only (spec Unit 4) | PLN2 (dos-app native seats PLN2/DEV1/REV1; home REV3 evidence gate) | REV3 | 2, 3 | — | dos-app #59, #60 | merged (PASS — evidence doc #75 + supplement, flag #122 closed) |
| A | ADOPTED: admin shells CLI-only in browser (CC's work, outside spec #73) | CC | FnB-certified, merge overseen by PLN1 | — | feat/admin-cli-only | #932 | merged |

Event log:
- 2026-08-02 08:30 · Conformance done (REV1, doc #76): 0 Major / 0 Medium / 7 Low; spec #73 SHIPPED — 25× as-specced, 2× deviated-intentionally (both PLN1-ratified: F2 spec-text error on schema shipping; U4 step-4 structural proof). F3 flagged #123. Sprint CLOSED and frozen; scoped authority revoked.
- 2026-08-02 ~07:30 · FnB-directed fleet update: all 6 local forks (dos-app, md-converter, rst-c, subfloor-marketing, ami, dos-arch) pinned to f442bb2. dos-app turned out to be PLN2's unit-4 proof surface — PLN2 had already repinned it (dos-app PR #59); double-lay disclosed to PLN2 (task #1081), dos-app excluded from further sweep. Engine defects found and filed: subfloor #935 (remote matcher substring bug), #936 (transient first-attempt update failures). ami/dos-arch engine servers still on pre-update floor pending FnB-approved restart (live sessions would be killed).
- 2026-08-02 07:13 · PLN2 seat mapping for unit 4 RATIFIED: dos-app native seats PLN2/DEV1/REV1; home DEV6/REV3 evidence roles; interview routes inherited.
- 2026-08-02 06:52 · Unit 2 merged @ f442bb2. Report discrepancy logged: DEV4's unit report says "No CI reds" but the trail shows one red (pr_event #1065 — removal-manifest miss for migration 0159, fixed in loop 1 @ 434c7cf). Trail is authoritative; goes to sprint report.
- 2026-08-02 05:57 · FnB directed adoption of CC's PR #932 (the shell behind DEV3's checkout collision). Zero file overlap with #933 confirmed; all checks green; squash-merged @ 44983b6 under PLN1 oversight. #933 unaffected.
- 2026-08-02 05:39 · PR #931 checks green @ dcd1048 (pr_event #1028); REV1 tasked (#1029) and booted.
- 2026-08-02 05:39 · DEV3 call (stands): shared subfloor checkout found switched by a non-sprint shell to feat/admin-cli-only with unrelated dirty API/UI work — DEV3 moved unit 1 to a dedicated worktree. Anomaly noted for the report; no sprint impact.

Notes:
- Unit 2's final skill audit (six-dimension matrix) is part of Unit 2's gate: DEV4 supplies the audit matrix, REV2 independently verifies it against diff, command help, role authority, and generated skill seed. A failed audit returns Unit 2 for correction before Unit 4.
- Unit 4 produces no code PR — durable evidence + report only, per spec Ratified decision 3.
- Adversarial gate + focused-suite ordering per spec §Adversarial gate; final verification (`./sc render-check`, `./sc verify`, migration rebuild) runs before Unit 4.
