---
name: review
description: Reviewer procedure — read a diff against its governing intent, identify material failures, open flags for merge blockers, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. Load when reviewing a dev's work.
category: craft
common: false
---

# review — gate a diff against its spec

The reviewer's job end to end. You are a **different lineage than the code**
— reviewer shells are deliberately booted on a different model family than
the authoring dev, so the review doesn't share the author's blind spots. Use
that independence to test consequential claims against the actual diff and
its governing intent. `<self>` = your shell_id.

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
  `documents` where `kind='spec'`). Its Current Posture, In Scope promises,
  Out of Scope exclusions, done-condition, and Anticipated User Activity = your
  yardstick.

Note the **author** — Step 4 proposes a handoff to them. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`); the roster maps
display_name -> shortname:
```
sc mem get shells
```

## Step 2: Review what matters for this change

Choose the lenses that materially bear on the diff; do not manufacture
coverage to complete a checklist:

- **Implementation** — correctness, clarity, error handling, and fit with
  existing patterns where they affect the change.
- **Behavior under relevant conditions** — states, boundaries, and failures
  significant to the feature's risk and intended use.
- **Intent** — the diff against the spec's current posture, scope,
  done-condition, and any audience or assurance promises that apply.

| Diff touches | Lens |
|---|---|
| a redline / UI change | `redline_review` |

A matching fork-local skill carries the fork's actual environment, tools, and
process boundary.

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

- **Match skepticism to the work.** Follow the evidence and the project's
  posture; do not assume either correctness or defect.
- **Verify, don't trust.** Re-read the claim against the code; trace the
  path. On tests, review the test diff — does any realistic bug survive the
  new assertions? — do NOT re-run the green suite the dev and CI already
  ran. A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** In Scope promises and the
  done-condition are the bar. Out of Scope work or an audience/assurance
  mismatch in the diff = a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don't push it.
- **Critique and confirm — never build.** Do NOT patch the author's code;
  flag it and propose it back.
