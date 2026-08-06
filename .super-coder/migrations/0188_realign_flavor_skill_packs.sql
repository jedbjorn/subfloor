-- 0188 — realign Admin, Planner, and Dev skill ownership.
--
-- Full-body UPSERTs publish the new Admin Git procedure and Planner-owned flag
-- reconciliation. Targeted pack changes converge existing installations while
-- leaving Bespoke shell_skills and unrelated operator opt-ins untouched.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'admin_git',
  'Admin-only Git procedure for the repository root — identify main, fast-forward safely, commit fork engine pins, merge only approved PRs, and preserve every foreign worktree. Use before Admin performs Git maintenance or an authorized merge.',
  'substrate',
  NULL,
  0,
  '# admin_git — maintain the repository root

Admin owns the root checkout and its `main` branch. Use this procedure for a
specific update, reconciliation, or approved merge; it is not a standing
cleanup pass. The FnB merge gate and the preservation rule remain in force.

## Orient before writing

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short --branch
git worktree list
```

Proceed only when the top level matches the repository named by the boot
document and the root checkout is on `main`. A dirty root, detached head, or
diverged main is a decision boundary: show the exact state to the FnB before
changing it.

Every other worktree belongs to its shell. Never switch its branch, stash,
reset, clean, move, or remove it. When the FnB explicitly asks for repository-
wide cleanup, load `git_cleanup`; otherwise leave foreign worktrees untouched.

## Fast-forward main

```bash
git fetch origin main
git pull --ff-only origin main
```

Success leaves `main` clean and at the fetched remote head. If `--ff-only`
refuses, stop and report the local/remote commits; never create a merge bubble
or reset main to make the command pass.

## Commit a fork engine pin

In a tracking fork, `.super-coder/` is a materialized dependency and remains
gitignored. After `self_update` succeeds, stage only the durable public update:

```bash
git add .sc-state/engine.ref
git status --short
git commit -m "chore: update super-coder engine pin"
```

Add the root `sc` dispatcher or another public file only when the update
deliberately changed it. Never force-add `.super-coder/`, local snapshots,
rendered `_sc` state, or `.sc-state/engine.ref.prev`. Push the resulting main
commit only within the operator''s requested update workflow.

## Merge an approved PR

Merge only after the FnB names or explicitly authorizes the PR. Re-read live
state immediately before acting:

```bash
gh pr view <number> --json url,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup
```

Require the expected repository, `baseRefName=main`, the reviewed head, a
mergeable state, and successful required checks. Use the repository''s approved
merge method, then `git pull --ff-only origin main`. A changed head, red/pending
check, or merge refusal invalidates the authorization; stop and return the live
evidence instead of overriding it.

For a stack, retarget each remaining PR to `main` before merging the PR above
the one that landed. Never rely on automatic retargeting after a base branch is
deleted.

## Source-repository exception

```bash
git ls-files --error-unmatch .super-coder/schema.sql
```

Exit 0 means this repository authors super-coder itself: `.super-coder/` is
tracked source, not a dependency, and `.sc-state/engine.ref` is not the delivery
unit. Engine implementation still arrives through a Developer branch and PR;
Admin fast-forwards main and merges only the exact approved PR. Apply live
migrations or restart the engine only through their dedicated procedures and
operator-owned recovery window.

## Stop conditions

- No approval -> do not merge.
- Foreign worktree activity -> preserve it and surface it.
- Main cannot fast-forward -> report divergence; do not reset.
- Target repository, PR head, or checks differ from the authorization -> stop.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'flag_sweep',
  'Planner-owned periodic or on-demand delivery reconciliation — auto-close flags whose gating work is provably done, open missing ship/docs handoffs, and surface judgment calls to the FnB. Use for a requested sweep or when delivery state needs reconciliation.',
  'substrate',
  NULL,
  0,
  '# flag_sweep — reconcile flags against state

Planner-owned. Run periodically or when the FnB asks for delivery-state
reconciliation; never make it a boot ritual. Working shells close the flags
their own work clears (boot doc, "Finish before you stop"); this sweep is the
backstop for dropped handoffs + shipped work nobody documented. Two directions:
close what''s provably resolved, open what''s provably missing.

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
apply. Pick `SC-###` for any open below = next free id
(`SELECT display_name FROM flags ORDER BY flag_id DESC LIMIT 5;`).

### 3A — Implemented but not marked shipped (ship-drift)

The dev flips the horizon to `shipped` when Verification passes (`spec` skill,
hand-off step) — the flip sometimes gets missed. Deterministic signal = spec''s
**Verification task `done`** + feature **not** `shipped`. Open a durable
`[Ship]` flag — it governs both halves of the dropped hand-off (mark shipped +
reconcile the doc to the spec) and stays open until a planner does both.

```sql
-- specs finished (Verification done) on features still short of shipped, with no open ship/docs flag:
SELECT DISTINCT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
JOIN documents d   ON d.feature_id = r.feature_id AND d.kind=''spec''
JOIN spec_tasks t  ON t.document_id = d.document_id AND t.title=''Verification'' AND t.status=''done''
WHERE r.roadmap_status NOT IN (''shipped'',''retired'')
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND (f.description LIKE ''[Ship]%'' OR f.description LIKE ''[Docs]%''
           OR f.description LIKE ''%not marked shipped%'' OR f.description LIKE ''%doc%pending%''));
```

Per row, open the flag in Planner''s own queue. Do not message yourself:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
```

### 3B — Shipped but undocumented (docs-pending)

Devs open a docs-pending flag when they ship — sometimes skipped. Find
`shipped` features with no frozen doc + no open docs-pending flag; open one
per row. (Finished-but-not-shipped is 3A''s job, not this one.)

```sql
-- shipped features with no frozen doc and no open docs-pending flag:
SELECT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
WHERE r.roadmap_status = ''shipped''
  AND NOT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.feature_id = r.feature_id AND d.frozen=1)
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND (f.description LIKE ''[Docs]%'' OR f.description LIKE ''%doc%pending%''));
```

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
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

DELETE FROM flavor_skills
WHERE (flavor='admin' AND skill_id IN (
    SELECT skill_id FROM skills
    WHERE name IN ('git','flag_sweep','local_skill_management')
)) OR (flavor IN ('planner','dev') AND skill_id IN (
    SELECT skill_id FROM skills WHERE name='admin_git'
));

WITH required_grants(flavor, skill_name) AS (
    VALUES
        ('admin','admin_git'),
        ('admin','authoring_syntax'),
        ('planner','flag_sweep'),
        ('planner','local_skill_management'),
        ('planner','authoring_syntax'),
        ('dev','authoring_syntax')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT grants.flavor, skills.skill_id
FROM required_grants grants
JOIN skills ON skills.name=grants.skill_name
WHERE skills.is_deleted=0;

COMMIT;
