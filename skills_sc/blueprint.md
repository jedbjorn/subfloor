---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# blueprint

Turn a one-line objective into a sequenced construction plan — decompose into steps, find the dependency order, mark what can run in parallel, name the verification gate. Use before multi-step builds.

**Category:** craft

---

# blueprint — objective → sequenced plan

Catalogue skill (opt-in). Run before any build spanning more than a couple of
steps. Output = the seven items below, in order.

1. **Objective** — one sentence + done-condition (the observable check that
   ends the build).
2. **Prior decisions** — `sc mem get decisions` (active-decision index;
   `sc mem get decisions <id>` = full row + rationale). A recorded decision
   constrains the plan: honor it, or supersede it explicitly with
   `sc mem decision "…" --parent <old_id>`. NEVER silently re-litigate a
   settled decision.
3. **Decompose** — concrete steps, each verifiable on its own.
4. **Order** — dependency sequence (what must precede what); steps with no
   dependency on each other -> mark **parallelizable**.
5. **Per step** — the change + files/areas touched (ground in the real repo
   via `surface_catalogue`, not memory) + its verification (test / run /
   review).
6. **Risks / unknowns** — list what could break the plan; spike the riskiest
   unknown first, not last.
7. **Gate** — adversarial pass before calling it done: each step's
   verification proves the done-condition, or the plan fails the gate.

## Stance

- Plan to the next solid checkpoint, not the whole universe — re-plan as
  reality lands.
- Sequence a thin slice that works end-to-end early, then deepen — never
  build all the pieces and integrate last.
- In super-coder, land the plan as a **spec** on the roadmap (`docs` skill):
  feature row + `spec` document -> reviewable, freezes on ship.
