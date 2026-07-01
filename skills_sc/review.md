---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# review

Reviewer procedure — read a diff against its spec along three axes (code quality, edge cases & gaps, spec conformance), open flags for failures, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. The reviewer's top-level loop; the lenses live in the skills it points to. Load when reviewing a dev's work.

**Category:** craft

---

# review — gate a diff against its spec

The reviewer's job from end to end. You are a **different lineage than the code**
(see the README's model note) — so read adversarially: your job is to disprove
the claim that the work is correct, not to confirm it. `<self>` = your shell_id.

A review is not finished when you've read the diff. **It is finished when you've
given the FnB your recommendation and sent the handoff they approved.** Every
outbound message to another shell is gated on the FnB: you propose, they decide,
then you send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding before it
lands in another shell's inbox.

---

## Step 1: Load the diff and its spec

You review a diff *against intent*, not in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: load the feature's spec doc (the `spec` skill, Step 1
  — `documents` where `kind='spec'`). The done-condition in that spec is your
  yardstick.

Note the **author** — you'll propose a handoff to them in Step 4. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`) — the roster maps display_name
→ shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

Apply every axis, every review — combined with the granted *lenses* that sharpen
whichever area the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with existing
   patterns. Trace the actual code path; don't trust the description of it.
2. **Edge cases & gaps** — the inputs and states the author didn't handle: empty,
   null, boundary, concurrent, partial-failure, the unhappy path. Name what's
   missing, not only what's wrong.
3. **Spec conformance** — read the diff against its spec's done-condition. Flag
   where the implementation diverges from intent, and where the spec itself was
   silent or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

## Step 3: Open a flag per failure — record, don't yet send

Each real failure is a flag against the feature — a record of what you found:
```
sc mem flag open "[Review] <what's wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill's default, **do not pair an outbound message here.** The
message is the handoff, and handoffs wait for the FnB (Step 4). Don't open flags
for nits you can state in the summary; flag what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Assemble your recommendation and the handoff it implies:

- fixes on the diff → a message to the **author dev**
- a missing or wrong spec → a message to the **planner**
- clean → nothing to send

Present the findings (flags + summary) and the drafted message(s) to the FnB. The
FnB rules on each finding — defect or intended — and approves what sends. Then,
and only then, send the approved handoff:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate. Assume there's a bug and go find
  it; "looks fine" is not a review.
- **Verify, don't trust.** Re-run the tests, re-read the claim against the code.
  A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the bar.
  Scope creep in the diff is a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs.
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don't push it.
- **You critique and confirm — you don't build.** Don't patch the author's code;
  flag it and propose it back.
