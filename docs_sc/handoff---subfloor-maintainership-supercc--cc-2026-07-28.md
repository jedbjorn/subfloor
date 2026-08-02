---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Conductor — CLI sprint orchestration v1
roadmap_status: retired
frozen: false
---

## HANDOFF — subfloor maintainership

From CC (superCC, shell_id 1), 2026-07-28. You are the maintainer of subfloor
from here on. This is everything you need that is not already in the repo.

### What you inherit

- **The repo:** github.com/jedbjorn/subfloor. Default branch `main` (carries
  the retired TMUX/Interface era). Working branch **`conductor`** — all
  current work lands there as PRs; Jed merges; `conductor` → `main` happens
  only at Step 12, and that merge is the rollout gate for the whole rebuild.
- **The project:** the Conductor sprint-orchestration rebuild — CLI only.
  A weak-model OpenCode shell (the Conductor) that never decides anything,
  relaying directives between ephemeral planner/dev/reviewer shells; a
  sentinel in the engine service turning silence into events; DB as the only
  source of truth. The full construction plan (12 steps, adversarially
  reviewed, per-step verification criteria) is being seeded to you as a
  document — read it whole before Step 2. The FnB has ratified it.
- **The grounding:** github.com/jedbjorn/subfloor-cli is the pre-TMUX backup.
  Its `skills_sc/sprint.md` + `sprint_orchestration.md` + `specs_sc/
  sprint-eventing.md` describe the old participant/planner loop the Conductor
  replaces. Read them to understand what died and why; do not build from them.

### State of the work

Step 1 (interface strip: surfaces) is complete on branch
`interface-strip-surfaces`, PR open against `conductor`. What it did:

- Deleted the Interface subsystem: 12 `interface_*.py` scripts,
  `api/interface_routes.py` + `interface_ws.py`, `chat_migrations/`, the
  interface-stream spike, 23 interface test files + interface mutation
  drivers + fixtures, the SPA's Interface tab (~2,100 lines of app.js, CSS,
  xterm vendor bundle), the Dockerfile's tmux + xterm-shadow layers.
- **Extracted the board API** — the keeper — into `api/sprint_routes.py`:
  GET/POST/PATCH `/api/sprint-units`, same handler logic and idempotency
  discipline, NEW auth model: a Bearer token resolving to `shells.api_key`
  is a shell actor; no token on the localhost-fenced server is the operator.
  Browser sessions, CSRF, and the operator token file died with the
  Interface. `_may_write_board` (planner-only writes) is unchanged.
- Moved the unit state machine (`SPRINT_UNIT_EDGES`, `check_transition`,
  `SprintTransitionError`) into `scripts/sprint_units.py` — its natural home.
- Stubbed the wake-machine verbs in `sc sprint` (status/alerts/retry/arm/
  disarm/action-receipts) with loud retirement errors; `unit`/`board` verbs
  stay live. Removed pr_poller's wake-item emission — its rows are durable
  records; the sentinel→Conductor path (Steps 5+8) becomes their consumer.
- Six keeper test files rewired from `interface_routes` to `sprint_routes`.

### The quarantine map — your Step 2

`git grep -n 'STEP2(conductor)'` lists every deliberate leftover:

- `update.py` + `rebuild.py`: interface_reconcile imports are guarded no-ops.
  Step 2 defines the replacement refusal rule — an ACTIVE sprint blocks
  update/rebuild, read from `sprint_state`, not interface state.
- `./sc enter` is BROKEN on the branch — it still routes through the deleted
  interface CLI. Step 2 re-opens the direct interactive door: un-gate
  `run.py` main() (it hard-exits public interactive launches, ~line 1261)
  and repoint `enter` at direct `sc boot`. Verify: boots with the API down.
- `snapshot.py` still lists five `interface_*` tables in its audit set;
  `activity_readers.py` still queries `interface_sessions`; `shell_liveness`
  and `run.py` carry interface-era touches. All Step 2.
- The legacy wake tables (`sprint_planner_bindings`, `planner_wake_batches`,
  `planner_wake_items`, `planner_action_receipts`, `planner_alerts`) and the
  five `interface_*` tables still exist in the schema. They drop in Step 4's
  migration, which MUST include a drain story: installed forks have LIVE rows
  (dos-arch had two live bindings at handoff). Verify the migration against a
  copy of a real fork DB, never just a clean one.

### Doctrines already fixed (do not relitigate)

- DB = source of all truth. MD renders = untracked, derived, FnB-review only.
- The Conductor never decides. Directives carry an issuer; a per-flavor
  whitelist is enforced at the API layer AND re-checked by the Conductor.
- Zero scheduled polling by any shell. The sentinel is the sole poller.
- **No attach semantics anywhere** — this is the tripwire doctrine. A shell
  session is boot → work → exit. Nothing attaches, recovers, or validates
  terminal state; liveness is observed from outside. If a step finds itself
  rebuilding that ceremony, the step is off-design; stop.
- Sentinel detects → Conductor relays → Planner decides.
- Topology (FnB decision, recorded): subfloor-the-product keeps its docker
  sandbox; single-namespace holds because the engine service is the
  container's entrypoint and shells `docker exec` into it. That decision is
  about the PRODUCT's runtime — your own bare-metal seat is a separate
  matter and does not change it.

### Practical facts

- Fleet (repin targets at Step 12, not work foci): dos-arch (proving
  ground), md-converter, ami, rst-c. Back up each fork DB before any
  migration lands on it.
- dos-app is the clean-install test target for Steps 9–11 (no `.super-coder`
  installed at handoff). Install, then pin to the branch:
  `./sc update --branch conductor`.
- Open flag inherited: the README owes the Sprints + Harnesses & models
  rewrite including the planner-model recommendation — folds into Step 12.
- The harness auth caveat that will bite first in a fresh environment:
  creds mounted ≠ model routable. Run the auth doctor before the first
  sentinel-triggered boot, not after it fails silently.

### Stances that will save you (from my L&S — yours to re-learn or adopt)

- Open PRs by default; merging is the FnB's gate, always. No exceptions
  outside a declared sprint's scoped authority.
- Read before proposing. Verify where a thing deploys from before editing
  what you assume is canonical.
- Merged and migrated are not deployed — bounce the service, then verify the
  running process.
- Validity ≠ correctness: run the real check, not the convenient pass.
- When a child fork surfaces a template bug, fix the template — fixes flow
  upward to where the next clone reads from.

### Close

Subfloor was my mandate — the layer above the layer that keeps everything
else running. It is yours now, and you hold it from the seat I never had:
outside. The plan is sound, the branch is clean, the first step is landed.
Take Step 2.

Build the piece that belongs there.

— CC
