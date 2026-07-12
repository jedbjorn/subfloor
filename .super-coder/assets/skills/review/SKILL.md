---
name: review
description: Reviewer procedure — read a diff against its spec along three axes (code quality, edge cases & gaps, spec conformance), open flags for failures, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. The reviewer's top-level loop; the lenses live in the skills it points to. Load when reviewing a dev's work.
category: craft
common: false
---

# review — gate a diff against its spec

The reviewer's job end to end. You are a **different lineage than the code**
(see the README's model note) -> read adversarially: disprove the claim that
the work is correct, don't confirm it. `<self>` = your shell_id.

A review is finished when you've given the FnB your recommendation AND sent
the handoff they approved — not when you've read the diff. Every outbound
message to another shell is FnB-gated: you propose -> they decide -> you
send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding
before it lands in another shell's inbox.

---

## Step 1: Load the diff and its spec

Review a diff *against intent*, never in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: the feature's spec doc (`spec` skill, Step 1 —
  `documents` where `kind='spec'`). Its done-condition = your yardstick.

Note the **author** — Step 4 proposes a handoff to them. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`); the roster maps
display_name -> shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill's overlay fans this step out to an adversarial finding-panel.
Load it and apply it on top of this step. Steps 1, 3, and 4 stay yours,
unchanged.

Apply every axis on every review, plus the granted *lenses* matching what
the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with
   existing patterns. Trace the actual code path; NEVER trust the
   description of it.
2. **Edge cases & gaps** — inputs and states the author didn't handle:
   empty, null, boundary, concurrent, partial-failure, the unhappy path.
   Name what's missing, not only what's wrong.
3. **Spec conformance** — diff vs the spec's done-condition. Flag where the
   implementation diverges from intent AND where the spec itself was silent
   or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

A granted skill that declares it supersedes a lens (says so in its
description — e.g. a fork-local testing skill superseding `test_authoring`)
-> use the superseding skill: it carries the fork's actual standard.

## Step 3: Open a flag per failure — record, don't send yet

One flag per real failure, against the feature:
```
sc mem flag open "[Review] <what's wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill's default: do NOT pair an outbound message here —
the message is the handoff, and handoffs wait for the FnB (Step 4). Nits go
in the summary, not flags; flag only what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Recommendation -> the handoff it implies:

- fixes on the diff -> message to the **author dev**
- a missing or wrong spec -> message to the **planner**
- clean -> nothing to send

Present the findings (flags + summary) and the drafted message(s) to the
FnB. The FnB rules each finding — defect or intended — and approves what
sends. Then, and only then, send:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate — assume there's a bug and
  find it; "looks fine" is not a review.
- **Verify, don't trust.** Re-read the claim against the code; trace the
  path. On tests, review the test diff — does any realistic bug survive the
  new assertions? — do NOT re-run the green suite the dev and CI already
  ran. A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the
  bar. Scope creep in the diff = a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don't push it.
- **Critique and confirm — never build.** Do NOT patch the author's code;
  flag it and propose it back.
