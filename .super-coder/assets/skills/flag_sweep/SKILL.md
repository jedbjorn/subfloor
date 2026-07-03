---
name: flag_sweep
description: Admin's every-session flag reconciliation — auto-close flags whose gating work is provably done, open ship flags for implemented-but-unshipped specs and docs-pending flags for shipped features that lack a doc (and message the planner), and surface the judgment calls to the FnB. Step 1 of the admin standing pass; run it before git_cleanup.
category: substrate
common: false
---

# flag_sweep — reconcile flags against state

Admin-only. The first leg of your standing every-session pass (then `git_cleanup`,
then optional `local_skill_management`). Working shells close the flags *their own*
work clears (boot doc, "Finish before you stop"); this sweep is the backstop — it
catches the stragglers they dropped and the docs nobody opened a flag for. It runs
in **two directions**: close what's provably resolved, open what's provably missing.

`<self>` = your shell_id. Resolve the planner once up front:

```sql
SELECT shortname FROM shells WHERE flavor='planner' AND COALESCE(is_deleted,0)=0;
-- no planner in this fork → surface to the FnB instead of messaging.
```

---

## Step 1: Load the open flags with their state

```sql
SELECT f.flag_id, f.display_name, f.priority, f.description,
       f.feature_id, r.title AS feature, r.roadmap_status,
       (SELECT COUNT(*) FROM documents d
        WHERE d.feature_id = f.feature_id AND d.kind='spec' AND d.frozen=1) AS frozen_docs
FROM flags f
LEFT JOIN roadmap r ON r.feature_id = f.feature_id
WHERE f.resolved=0 AND COALESCE(f.is_deleted,0)=0
ORDER BY f.priority, f.flag_id;
```

Sort each open flag into exactly one bucket below. **Auto-close only on unambiguous
evidence** — when in doubt, surface, don't close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note must cite the
evidence — that is the whole point of doing it here instead of guessing.

**A. Docs-pending flag, doc now exists.** A `[Docs] … docs pending` flag on a
feature whose `frozen_docs > 0`:
```
sc mem flag close <flag_id> --notes "Auto: frozen spec doc now exists for feature #<id> (flag_sweep)."
```

**B. Ship-blocker, feature now shipped.** A flag of the form `… | Blocker for: <X>`
whose linked feature's `roadmap_status` is `shipped` (or later) **and** whose text
is about that feature shipping / becoming available (not a separate concern that
merely happens to hang off the same feature):
```
sc mem flag close <flag_id> --notes "Auto: blocking feature #<id> (<title>) now shipped (flag_sweep)."
```

**C. Ship-drift flag, now shipped *and* documented.** A `[Ship] … not marked
shipped` flag (Step 3A) covers both halves — mark shipped *and* reconcile the doc —
so only close it once **both** are true: `roadmap_status` is `shipped` (or later)
**and** `frozen_docs > 0`. Shipped-but-still-undocumented leaves it open (the doc
half isn't done):
```
sc mem flag close <flag_id> --notes "Auto: feature #<id> (<title>) now shipped with a frozen doc (flag_sweep)."
```

Do **not** message on close (per the `flags` skill — messages pair with `open`, not
`close`). Do **not** reopen anything. Do **not** close a flag whose evidence you had
to infer — that goes to Step 4.

---

## Step 3: Open the flags nobody opened

Two upstream gaps drop silently — work that finished but was never marked shipped,
and shipped work with no doc. They're sequential: a feature climbs out of 3A (gets
marked shipped) before 3B can apply. Pick `SC-###` for any open below as the next
free id (`SELECT display_name FROM flags ORDER BY flag_id DESC LIMIT 5;`).

### 3A — Implemented but not marked shipped (ship-drift)

The dev is supposed to flip the horizon to `shipped` when Verification passes (the
`spec` skill, hand-off step) — but the spec sometimes gets built and the flip gets
missed, so the feature lingers `in_progress` with its work actually done. The
deterministic signal is a spec whose **Verification task is `done`** while the
feature is **not** `shipped`. Open a durable `[Ship]` flag — it governs both halves
of the dropped hand-off (mark shipped **and** reconcile the doc to the spec) and
lingers until a planner does them.

```sql
-- specs finished (Verification done) on features still short of shipped, with no open ship/docs flag:
SELECT DISTINCT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
JOIN documents d   ON d.feature_id = r.feature_id AND d.kind='spec'
JOIN spec_tasks t  ON t.document_id = d.document_id AND t.title='Verification' AND t.status='done'
WHERE r.roadmap_status NOT IN ('shipped','retired')
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND (f.description LIKE '%not marked shipped%' OR f.description LIKE '%docs pending%'));
```

For each row, open the flag and message the planner (or surface to the FnB if there
is no planner) — same contract as the `flags` skill:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) — Verification done but still <status>; SC-### opened to mark shipped + reconcile docs to spec."
```

### 3B — Shipped but undocumented (docs-pending)

Devs are supposed to open a docs-pending flag when they ship — but they sometimes
skip it. Find `shipped` features with no frozen doc **and** no open docs-pending
flag, and open one so they don't ship silently undocumented. (Work that's finished
but not yet shipped is 3A's job, not this one — it surfaces there first.)

```sql
-- shipped features with no frozen doc and no open docs-pending flag:
SELECT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
WHERE r.roadmap_status = 'shipped'
  AND NOT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.feature_id = r.feature_id AND d.kind='spec' AND d.frozen=1)
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND f.description LIKE '%docs pending%');
```

For each row, open the flag and message the planner (or surface to the FnB if there
is no planner) — same contract as the `flags` skill:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) is shipped with no doc — SC-### opened, ready to freeze + document."
```

---

## Step 4: Surface the rest — don't guess

Everything that isn't a clean Step-2 close or Step-3 open goes to the FnB as a
short list (no `send` unless a specific shell owns it): review-failure flags (the
author dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can't verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn't auto-act*

The FnB or the owning shell closes these with a real note. You only ever auto-act
on unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB, cited in the note, or it
  surfaces. A wrongly-closed live blocker is worse than a straggler.
- **You are the backstop, not the owner.** The shell that did the work should close
  its own flag with the richer "how" note; you sweep what they dropped. Don't race
  to close a flag whose owner is still active on that feature.
- **Both directions, every session.** Close what's resolved; open what's missing.
  An implemented-but-unshipped spec and an undocumented shipped feature are each as
  much a dropped handoff as an unclosed flag — and the signal is already in the DB
  (a `done` Verification task, a missing frozen doc), so surfacing them is
  deterministic, not a guess.
- **Then move on to `git_cleanup`.** flag_sweep is leg 1 of the pass, not the whole
  pass.
