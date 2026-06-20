---
name: review
description: Reviewer procedure — read a diff against its spec through the review lenses, open flags for failures, and ALWAYS message the author dev with findings (flags or clean). The reviewer's top-level loop; the lenses live in the skills it points to. Load when reviewing a dev's work.
category: craft
common: false
---

# review — gate a diff against its spec

The reviewer's job from end to end. You are a **different lineage than the code**
(see the README's model note) — so read adversarially: hunt the bug the author
missed, verify rather than trust. `<self>` = your shell_id.

A review is not finished when you've read the diff. **It is finished when the
author dev has been told what you found** — flags or clean. That message is the
handoff; skipping it leaves the author waiting on a review they can't see.

---

## Step 1: Load the diff and its spec

You review a diff *against intent*, not in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: load the feature's spec doc (the `spec` skill, Step 1
  — `documents` where `kind='spec'`). The done-condition in that spec is your
  yardstick.

Note the **author** — you'll message them in Step 4. Resolve their shortname from
the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`):
```sql
SELECT shortname, display_name FROM shells WHERE display_name='<from trailer>' AND is_deleted=0;
```

## Step 2: Run the lenses

The review *lenses* live in the skills you're granted — apply each that the diff
touches, don't re-derive them:

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

Across all of them, the cross-cutting checks: does it meet the spec's
done-condition? Does it do what the diff *claims*? What did the author not test?
What breaks at the edges?

## Step 3: Open a flag per failure

Each real failure is a flag against the feature. Per the `flags` skill, **opening
a flag also messages the party who clears it — here, the author dev**, so each
failure lands in their inbox:
```
./sc mem flag open "[Review] <what's wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
./sc mem message send <author-shortname> "Opened SC-### — <one line>."
```
Don't open flags for nits you can state in the summary; flag what blocks merge.

## Step 4: Always message the author — flags or clean

This is the step that closes the loop, and it fires **every** review, including a
clean one (a clean review the author never hears about is indistinguishable from
no review):

```
# failures found:
./sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# clean:
./sc mem message send <author-shortname> "Review of <feature> done — clean, no flags. Good to go on the FnB's merge gate."
```

Then report the same to the FnB.

---

## Stance

- **Adversarial by default.** You are the gate. Assume there's a bug and go find
  it; "looks fine" is not a review.
- **Verify, don't trust.** Re-run the tests, re-read the claim against the code.
  A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the bar.
  Scope creep in the diff is a flag, not a silent pass.
- **The author always hears back.** Flags ride out on messages (Step 3); the
  summary always does (Step 4). A review nobody was told about didn't happen.
- **You critique and confirm — you don't build.** Don't patch the author's code;
  flag it and send it back.
