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

Catalogue skill (opt-in). Use before a build that spans more than a couple of
steps, so the work has a shape before you start cutting.

## Produce

1. **Restate the objective** in one sentence + the done-condition (how you'll
   know it's finished).
2. **Re-surface prior decisions** — `sc mem get decisions`: has any part of
   this already been settled? A recorded decision constrains the plan; honor
   it, or supersede it explicitly (`sc mem decision "…" --parent <old_id>`) —
   never silently re-litigate.
3. **Decompose** into concrete steps — each a unit you could verify on its own.
4. **Order by dependency** — what must precede what. Mark steps with no
   dependency on each other as **parallelizable**.
5. **Per step**: the change, the files/areas it touches (use `surface_catalogue`
   to ground this in the real repo), and its **verification** (test, run,
   review).
6. **Risks / unknowns** — what could break the plan; resolve the riskiest
   unknown first (spike it) rather than last.
7. **Gate** — the adversarial check before calling it done: does each step's
   verification actually prove the done-condition?

## Stance
- Plan to the **next solid checkpoint**, not the whole universe — re-plan as
  reality lands. A plan that survives contact is short and concrete.
- Sequence so something **works end-to-end early** (a thin slice), then deepen —
  beats building all the pieces and integrating last.
- In super-coder, land the plan as a **spec** on the roadmap (the `docs` skill):
  a feature row + a `spec` document, so the plan is reviewable and freezes on
  ship.
