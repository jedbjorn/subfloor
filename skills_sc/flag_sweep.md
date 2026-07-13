---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# flag_sweep

Admin's every-session flag reconciliation — auto-close flags whose gating work is provably done, open ship flags for implemented-but-unshipped specs and docs-pending flags for shipped features that lack a doc (message the planner), surface judgment calls to the FnB. Step 1 of the admin standing pass; run before git_cleanup.

**Category:** substrate

---

# flag_sweep — reconcile flags against state

Admin-only. Leg 1 of the standing every-session pass -> then `git_cleanup` ->
then optional `local_skill_management`. Working shells close the flags their
own work clears (boot doc, "Finish before you stop"); this sweep is the
backstop for the stragglers they dropped + the docs nobody opened a flag for.
Two directions: close what's provably resolved, open what's provably missing.

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
        WHERE d.feature_id = f.feature_id AND d.frozen=1) AS frozen_docs
FROM flags f
LEFT JOIN roadmap r ON r.feature_id = f.feature_id
WHERE f.resolved=0 AND COALESCE(f.is_deleted,0)=0
ORDER BY f.priority, f.flag_id;
```

`frozen_docs` counts ANY frozen document on the feature — kind='spec' AND
kind='doc' both qualify (#319: forks that freeze kind='doc' rows for shipped
docs got false "undocumented" positives every sweep under a spec-only count).

Sort every open flag into exactly one bucket (Step 2 / Step 4). Auto-close
only on unambiguous evidence — any doubt -> Step 4, not a close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note MUST cite the
evidence.

**A. Docs-pending flag, doc now exists** = `[Docs] … docs pending` flag on a
feature with `frozen_docs > 0`:
```
sc mem flag close <flag_id> --notes "Auto: frozen spec doc now exists for feature #<id> (flag_sweep)."
```

**B. Ship-blocker, feature now shipped** = flag of the form
`… | Blocker for: <X>` + linked feature's `roadmap_status` is `shipped` (or
later) + the flag text is about that feature shipping / becoming available. A
separate concern that merely hangs off the same feature does NOT qualify:
```
sc mem flag close <flag_id> --notes "Auto: blocking feature #<id> (<title>) now shipped (flag_sweep)."
```

**C. Ship-drift flag, now shipped AND documented** = `[Ship] … not marked
shipped` flag (opened by Step 3A) covers two halves — mark shipped + reconcile
the doc — so close only when BOTH hold: `roadmap_status` is `shipped` (or
later) + `frozen_docs > 0`. Shipped-but-undocumented -> leave open:
```
sc mem flag close <flag_id> --notes "Auto: feature #<id> (<title>) now shipped with a frozen doc (flag_sweep)."
```

NEVER message on close (per the `flags` skill — messages pair with `open`).
NEVER reopen a flag. A close whose evidence you had to infer -> Step 4.

---

## Step 3: Open the flags nobody opened

Two gaps drop silently, in sequence: 3A (done but never marked shipped)
precedes 3B (shipped but undocumented) — a feature exits 3A before 3B can
apply. Pick `SC-###` for any open below = next free id
(`SELECT display_name FROM flags ORDER BY flag_id DESC LIMIT 5;`).

### 3A — Implemented but not marked shipped (ship-drift)

The dev flips the horizon to `shipped` when Verification passes (`spec` skill,
hand-off step) — the flip sometimes gets missed. Deterministic signal = spec's
**Verification task `done`** + feature **not** `shipped`. Open a durable
`[Ship]` flag — it governs both halves of the dropped hand-off (mark shipped +
reconcile the doc to the spec) and stays open until a planner does both.

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

Per row: open + message the planner (no planner -> surface to the FnB) — same
contract as the `flags` skill:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) — Verification done but still <status>; SC-### opened to mark shipped + reconcile docs to spec."
```

### 3B — Shipped but undocumented (docs-pending)

Devs open a docs-pending flag when they ship — sometimes skipped. Find
`shipped` features with no frozen doc + no open docs-pending flag; open one
per row. (Finished-but-not-shipped is 3A's job, not this one.)

```sql
-- shipped features with no frozen doc and no open docs-pending flag:
SELECT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
WHERE r.roadmap_status = 'shipped'
  AND NOT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.feature_id = r.feature_id AND d.frozen=1)
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND f.description LIKE '%docs pending%');
```

Per row: open + message the planner (no planner -> surface to the FnB) — same
contract as the `flags` skill:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) is shipped with no doc — SC-### opened, ready to freeze + document."
```

---

## Step 4: Surface the rest — don't guess

Everything that isn't a clean Step-2 close / Step-3 open -> short list to the
FnB (no `send` unless a specific shell owns it): review-failure flags (author
dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can't verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn't auto-act*

The FnB or the owning shell closes these with a real note. Auto-act ONLY on
unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB + cited in the note,
  or it surfaces. A wrongly-closed live blocker is worse than a straggler.
- **Backstop, not owner.** The shell that did the work closes its own flag
  with the richer "how" note; don't race to close a flag whose owner is still
  active on that feature.
- **Both directions, every session.** An implemented-but-unshipped spec and an
  undocumented shipped feature are dropped handoffs; the signal is already in
  the DB (a `done` Verification task, a missing frozen doc) — surfacing them
  is deterministic.
- **Then `git_cleanup`.** flag_sweep is leg 1 of the pass, not the whole pass.
