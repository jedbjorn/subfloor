---
name: flag_sweep
description: Planner-owned periodic or on-demand delivery reconciliation — auto-close flags whose gating work is provably done, open missing ship/docs handoffs, and surface judgment calls to the FnB. Use for a requested sweep or when delivery state needs reconciliation.
category: substrate
common: false
---

# flag_sweep — reconcile flags against state

Planner-owned. Run periodically or when the FnB asks for delivery-state
reconciliation; never make it a boot ritual. Working shells close the flags
their own work clears; this sweep is the
backstop for dropped handoffs + shipped work nobody documented. Two directions:
close what's provably resolved, open what's provably missing.

---

## Step 1: Load the bounded delivery audit

Run `sc mem delivery-audit` (Planner-only). It returns `recent_flag_names`,
`open_flags` (each with its feature's `roadmap_status` and `frozen_docs`),
`implemented_but_unshipped`, and `shipped_but_undocumented`. These are the
sweep's whole input; run no engine SQL of your own.

`frozen_docs` counts ANY frozen document on the feature — kind='spec' AND
kind='doc' both qualify, so a fork that freezes its shipped `doc` rows is
never reported as undocumented.

Sort every open flag into exactly one bucket (Step 2 / Step 4). Auto-close
only on unambiguous evidence — any doubt -> Step 4, not a close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note MUST cite the
evidence.

**A. Docs-pending flag, doc now exists** = `[Docs]`-tagged doc-pending flag
(however worded — "doc pending", "docs pending", "feature doc pending") on a
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

NEVER message on close. NEVER reopen a flag. A close whose evidence you had to infer -> Step 4.

---

## Step 3: Open the flags nobody opened

The audit already computed both gaps and applied the dedup guards, so open
one flag per returned row and re-derive nothing. A feature exits 3A before 3B
can apply. Pick `SC-###` from the highest numbered value in
`recent_flag_names`. Open each flag in Planner's own queue.
Do not message yourself.

### 3A — Implemented but not marked shipped (ship-drift)

Rows: `implemented_but_unshipped` — the spec's Verification task is `done`,
the feature is not shipped or retired, and no open `[Ship]`/`[Docs]` or
organic ship/docs-pending handoff covers it. The dev's flip to `shipped` was
missed. The `[Ship]` flag governs both halves of the dropped handoff (mark
shipped + reconcile the doc to the spec) and stays open until a planner does
both:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
```

### 3B — Shipped but undocumented (docs-pending)

Rows: `shipped_but_undocumented` — a shipped feature with no frozen document
and no open `[Docs]` or organic docs-pending handoff. The dev's docs-pending
flag was skipped:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
```

The guards match the `[Docs]`/`[Ship]` tag at position zero, then fall back to
`'%doc%pending%'` for untagged wordings; the fallback's over-breadth only ever
SKIPS an open — the conservative direction.

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
- **Both directions, every sweep.** An implemented-but-unshipped spec and an
  undocumented shipped feature are dropped handoffs; the signal is already in
  the DB (a `done` Verification task, a missing frozen doc) — surfacing them
  is deterministic.
