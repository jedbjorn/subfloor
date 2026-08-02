---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Conductor — CLI sprint orchestration v1
roadmap_status: retired
frozen: false
title: Conductor Alpha Hardening
tags: [conductor, sprint, update, ports]
date: 2026-07-29
project: super-coder
purpose: Repair AMI alpha findings
---

# Conductor Alpha Hardening

## Objective

Make an existing fork update into a sprint-ready Conductor installation without
manual shell creation, skill surgery, port reassignment, or accidental
activation. Done means a legacy fork updates, restarts, waits at the FnB gate,
then completes a one-unit sprint through close.

## Scope

- Fix #740: API and dev ports are globally collision-free across installed
  forks, regardless of which side of the pair already owns a number.
- Fix #743: update and fresh install idempotently provision exactly one `CON1`
  shell from the Conductor template.
- Fix #744: `inherit_common_skills: false` is honored by initial seeding,
  update-time regrant, and later catalogue sync; Conductor receives exactly
  `sprint_cond` and no direct grants.
- Fix #745: a pending initial `planner:handoff` on a declared sprint never
  triggers the sentinel. Only the explicit FnB `sc run CON1 ...` boot activates
  it. Automatic wakes begin only after the sprint is active.

> [!class4]
> Retired Interface/TMUX sprint state has no compatibility guarantee. It may be
> deleted or reset when simplifying the repair. No user production data depends
> on it; preserve unrelated current engine memory.

## Design

### Port allocation

Build one occupied-port set from every discovered fork's API port and dev port,
plus ports already bound on the host. Allocate the requested pair against that
union and against each other. A sibling API port and dev port are not separate
namespaces.

### Conductor provisioning

Create one idempotent Python reconciliation function shared by fresh install and
update. It uses `shell_factory`, creates role-only identity with a generated API
key, applies the shipped `CON1` shortname and OpenCode/Luna defaults, and refuses
ambiguous duplicate live Conductor shells rather than guessing.

### Skill invariant

Drive common-skill inheritance from the flavor template's
`inherit_common_skills` value. Reconciliation removes common flavor grants from
opt-out flavors before applying their explicit pack. Repeated update, seed, and
restart operations must converge to the same rows.

### Activation fence

The sentinel may wake Conductor only for eligible pending directives belonging
to an active sprint. A declared sprint's initial handoff remains visible but
ineligible. The manually booted Conductor can act that handoff, atomically move
the sprint active, and release ready workers; subsequent pending directives are
then sentinel-eligible.

## Construction Order

1. Add regression fixtures for a pre-Conductor fork, cross-namespace port
   collision, declared handoff, and polluted Conductor flavor grants.
2. Implement #740 independently in the port allocator and installer tests.
3. Implement shared Conductor provisioning and wire fresh install plus update
   reconciliation (#743).
4. Make flavor-pack reconciliation honor common-skill opt-out and clean existing
   Conductor grants (#744).
5. Gate sentinel eligibility on active sprint state while retaining manual
   access to the declared handoff (#745).
6. Delete/reset retired TMUX sprint state where it complicates the new
   invariants; do not build compatibility translation.
7. Run focused tests, the full suite, render-check, verify, and downstream AMI
   update/restart.
8. Repeat the AMI one-unit sprint from reviewed spec through frozen close with no
   manual port, shell, skill, or wake intervention.

## Verification

- Two forks cannot receive the same number across either API/dev port field.
- Updating a pre-Conductor fixture creates one `CON1`; rerunning update creates
  none.
- `CON1` is role-only, keyed, OpenCode-only, Luna-defaulted, and has exactly the
  `sprint_cond` flavor skill.
- Enabled Conductor passes startup doctor immediately after update.
- Restart with a declared handoff launches no Conductor and leaves the sprint
  declared.
- The explicit FnB boot acts the handoff exactly once and moves the sprint
  active.
- After activation, ready-for-review, review-clean, merge, report, conformance,
  and close directives wake automatically.
- Legacy TMUX sprint rows may be absent after reconciliation; unrelated roadmap,
  documents, identity, and current sprint rows remain intact.

## Gate

The repair fails if AMI requires any manual JSON edit, API shell creation, skill
toggle, service-restart workaround, direct database write, or manual worker
relay. The final evidence is a clean update plus a completely closed AMI sprint.
