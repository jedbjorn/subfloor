-- 0242 — publish flag_sweep's Planner-only bounded delivery-audit client.

BEGIN;

UPDATE skills SET
  description='Planner-owned periodic or on-demand delivery reconciliation — auto-close flags whose gating work is provably done, open missing ship/docs handoffs, and surface judgment calls to the FnB. Use for a requested sweep or when delivery state needs reconciliation.',
  category='substrate',
  command=NULL,
  common=0,
  content='# flag_sweep — reconcile flags against state

Planner-owned. Run periodically or when the FnB asks for delivery-state
reconciliation; never make it a boot ritual. Working shells close the flags
their own work clears (boot doc, "Finish before you stop"); this sweep is the
backstop for dropped handoffs + shipped work nobody documented. Two directions:
close what''s provably resolved, open what''s provably missing.

---

## Step 1: Load the bounded delivery audit

Run `sc mem delivery-audit`. The Planner-only API response contains
`recent_flag_names`, `open_flags`, `implemented_but_unshipped`, and
`shipped_but_undocumented`. It preserves the deterministic queries and dedup
guards below without granting arbitrary engine SQL.

`frozen_docs` counts ANY frozen document on the feature — kind=''spec'' AND
kind=''doc'' both qualify (#319: forks that freeze kind=''doc'' rows for shipped
docs got false "undocumented" positives every sweep under a spec-only count).

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
`… | Blocker for: <X>` + linked feature''s `roadmap_status` is `shipped` (or
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
apply. Pick `SC-###` from the highest numbered value in `recent_flag_names`.

### 3A — Implemented but not marked shipped (ship-drift)

The dev flips the horizon to `shipped` when Verification passes (`spec` skill,
hand-off step) — the flip sometimes gets missed. Deterministic signal = spec''s
**Verification task `done`** + feature **not** `shipped`. Open a durable
`[Ship]` flag — it governs both halves of the dropped hand-off (mark shipped +
reconcile the doc to the spec) and stays open until a planner does both.

Use the `implemented_but_unshipped` rows. The projection includes only specs
whose Verification task is done, whose feature is not shipped/retired, and
whose open `[Ship]`/`[Docs]` or organic ship/docs-pending handoff does not
already cover the feature.

Per row, open the flag in Planner''s own queue. Do not message yourself:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
```

### 3B — Shipped but undocumented (docs-pending)

Devs open a docs-pending flag when they ship — sometimes skipped. Find
`shipped` features with no frozen doc + no open docs-pending flag; open one
per row. (Finished-but-not-shipped is 3A''s job, not this one.)

Use the `shipped_but_undocumented` rows. The projection includes only shipped
features with no frozen document and no open `[Docs]` or organic docs-pending
handoff.

The dedup guards match the `[Docs]`/`[Ship]` tag at position zero FIRST — the
templates below mint "doc pending" (singular) and legacy hand-written flags say
"feature doc pending", so a prose-only `''%docs pending%''` pattern matched
neither and every later sweep re-listed already-flagged rows (found session
ADM1/0003, seven covered rows re-surfaced). The `''%doc%pending%''` fallback
catches untagged organic wordings; its over-breadth only ever SKIPS an open —
the conservative direction.

Per row, open the flag in Planner''s own queue. Do not message yourself:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
```

---

## Step 4: Surface the rest — don''t guess

Everything that isn''t a clean Step-2 close / Step-3 open -> short list to the
FnB (no `send` unless a specific shell owns it): review-failure flags (author
dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can''t verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn''t auto-act*

The FnB or the owning shell closes these with a real note. Auto-act ONLY on
unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB + cited in the note,
  or it surfaces. A wrongly-closed live blocker is worse than a straggler.
- **Backstop, not owner.** The shell that did the work closes its own flag
  with the richer "how" note; don''t race to close a flag whose owner is still
  active on that feature.
- **Both directions, every sweep.** An implemented-but-unshipped spec and an
  undocumented shipped feature are dropped handoffs; the signal is already in
  the DB (a `done` Verification task, a missing frozen doc) — surfacing them
  is deterministic.',
  is_deleted=0
WHERE name='flag_sweep';

COMMIT;
