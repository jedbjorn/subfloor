---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
title: "CONFORMANCE: F33 skill catalogue convergence"
tags: [conformance, f33, skills, sprint-70]
date: 2026-08-02
project: super-coder
purpose: Spec-vs-main verdict record for sprint 70 close-out
---

# CONFORMANCE: F33 skill catalogue convergence

**Spec:** doc #69 (Skill Catalogue Convergence) · **Sprint:** doc #70 ·
**Judged against:** `main @ 9668def` (jedbjorn/subfloor) · **Judge:** REV3 ·
**Method:** spec requirements read against the integrated code on main at the
pinned SHA — never the diffs, never the message trail. Only narrative input:
ratified judgement calls J1–J4 plus the planner-declared snapshot.py heal
(unit-3 report). Evidence gathered via five parallel read-only audit passes
over a detached worktree at 9668def, with decisive claims re-verified by hand
(registry contents, reconciler, rebuild/update ordering, snapshot main(),
projection ownership gate, migration ledger keying, CLI verb surface, pg
feature grants).

**Result: 28 as-specced · 1 deviated-intentionally · 2 deviated-silently ·
0 unimplemented. Findings: 0 Major, 0 Medium, 5 Low (2 deviations + 3
observations).**

## Verdict table

### Ownership model

| # | Requirement | Verdict |
|---|---|---|
| 1 | Tracked, validated tombstone registry under `.super-coder/assets/` holding exactly the 13 names | as-specced — `assets/skill_tombstones.json` (verified byte-exact against the spec list) |
| 2 | Active namespace = 0001 seed names + tombstones; name cannot be both | as-specced — `validate_upstream_skill_namespace` (seed_skills.py:105-115) raises on any overlap; J1 tolerance fully removed; negative tests at test_skill_tombstones.py:92-113, 268-284 |
| 3 | Duplicate/blank/non-string/malformed entries fail loudly before writes | as-specced — loader raises before returning (seed_skills.py:64-102); six bad-shape tests |
| 4 | Fork-local asset/DB creation may not claim a tombstoned name | as-specced — `_validate_skill_specs_claimable` (seed_skills.py:118-128); DB-only creation surface removed with `sc skill add` |
| 5 | Tombstones permanent unless a reviewed migration reactivates | as-specced — nothing reactivates; registry is the sole authority |
| 6 | Fork-local skills absent from both sets untouched | as-specced — reconciler deletes by explicit name-IN-list only; tests pin exact local row/grants |
| 7 | `dev_kit` not a tombstone; `sprint_dev/pln/rev` active, not tombstoned | as-specced — verified in registry and 0001 seed |

### Database convergence

| # | Requirement | Verdict |
|---|---|---|
| 8 | One idempotent reconciliation function; shell_skills + flavor_skills children deleted before the skills row; returns changed names + grant count | as-specced — `reconcile_tombstoned_skills` (seed_skills.py:131-169), savepoint-guarded, early-return no-op, verified by hand |
| 9 | Trailing migration performs the same cleanup for in-place updates | as-specced — `0154_remove_tombstoned_skills.sql`, run-twice test at test_skill_lifecycle_convergence.py:123-128 |
| 10 | rebuild.py reconciles after snapshot load, before retire-list/key backfill/FK validation/publication; failure aborts candidate, preserves outgoing DB + backup | as-specced — rebuild.py:299-326 verified by hand; failure-atomicity test :136-145 |
| 11 | update.py reconciles after catalogue sync, before regrant/projection/snapshot | as-specced — update.py:932-942 + 1132-1149 verified by hand |
| 12 | Boot/render self-heal reconciles | as-specced — `sync_engine_skills` heals unconditionally (seed_skills.py:385) from run.py (boot, both launch paths) and render.py. Observation F4: boot heal is best-effort (swallows failure) |
| 13 | Snapshot never serializes tombstoned rows or their shell/flavor grants; fork-local rows reinserted | as-specced — exclusion filters in dump_local_skills/dump_shell_skills/dump_flavor_skills (snapshot.py:160-244); test :163-176 |
| 14 | Snapshot is a serializer of post-convergence state | **deviated-silently (Finding 1)** — `snapshot.py:570` heals the live DB in-place before serializing; undeclared expansion |

### Projection convergence

| # | Requirement | Verdict |
|---|---|---|
| 15 | One shared projection reconciler used by boot, update, CLI grant/revoke/rm/retire/unretire, feature enable/disable, GUI grant endpoints | as-specced — `skill_projection.py`; every listed call site confirmed (run.py:1113/1494, update.py:1136, skill.py:212/227/301/259/279, feature.py:221/271, server.py:3971/3988, render.py:98-103) |
| 16 | Managed roots = `.claude/skills` + adapter `skill_dirs` (`.agents/skills`, `.opencode/skills`); installer ignore/teardown inventories agree | as-specced — managed_skill_dirs (skill_projection.py:38-48) matches both inventories; pinned by test_skill_projection.py:60. Observation F3: inventories are hard-coded duplicates |
| 17 | Absolute paths, parent traversal, symlink roots, outside-checkout resolutions refused; cleanup never follows symlinks | as-specced — `_validated_relative`/`_bounded_root`/`_remove_managed_tree` guards; symlink tests :173-210 |
| 18 | Per-shell exact render: writes resolved grants, removes engine-managed dirs not in set, deletions in the render summary | as-specced — reconcile_root (skill_projection.py:126-174) returns `deleted`; boot prints count. Observation F5: `sc render skills`/`sc update` output does not list deleted paths |
| 19 | Bespoke grant change reconciles that shell; flavor change reconciles every live shell of that flavor | as-specced — reconcile_assignment_targets (:255-288), reconcile_flavors (:240-252); GUI tests test_flavor_skill_packs.py:237-277 |
| 20 | Update sweeps repo root + each existing direct `.sc-worktrees/<shortname>` checkout; creates no missing worktrees or unused native roots | as-specced — reconcile_existing_checkouts (:342-380); test :212 |
| 21 | Never delete unmarked foreign files/dirs — repo root and worktrees | deviated-intentionally — post-#928 gate `_remove_managed_tree` (:115-123, verified by hand) deletes only namespace-member or banner-owned dirs, foreign preserved at root and worktrees identically. Matches ratified J2+J4 (exactness over engine-managed dirs only; universal never-delete-foreign). Root render is bounded-sweep, not per-grant exact — the J2-ratified shape |
| 22 | Legacy `skills_sc`: banner-owned files deleted, dir removed when empty, foreign files in mixed dirs preserved | as-specced — cleanup_legacy_skills_sc (:316-339); test :291-312 + gate |
| 23 | Update report states running sessions retain loaded text until reboot | as-specced — update.py:1143-1146 prints exactly this |

### Catalogue cleanup

| # | Requirement | Verdict |
|---|---|---|
| 24 | Delete engine_surgery/test_authoring_pg/test_authoring_sqlite asset dirs; regenerate seed; 10 retired v1 assets remain absent | as-specced — zero tombstoned dirs under assets/skills; seed grep clean; 37 active skills |
| 25 | Source-mode compose pointer to engine_surgery removed; inline source-repo guidance retained | as-specced — compose.py:48-72 PROJECT_VS_ENGINE_SOURCE |
| 26 | test_authoring rewritten stack-neutral, no deleted-companion references | as-specced — grep over assets clean; SKILL.md defers fixtures downstream |
| 27 | pg feature grants query_authoring_pg only; sidecar retained | as-specced — feature.py:59-66 verified by hand; test_feature.py:100-104 |
| 28 | query_authoring_pg + docs/README.md promise infrastructure + diagnostic SQL, not fixtures | as-specced — SKILL.md:10-13; docs/README.md:811 |
| 29 | Deleted names replaced by neutral opt-in fixtures; negative assertions for every tombstone | as-specced — fixtures use dos_arch_testing/query_authoring_pg; tombstoned names appear only in negative/removal assertions |
| 30 | Sprint-removal manifests/inventories updated, historical migrations preserved | as-specced — manifest covers removed-asset assertions + retained mixed migrations (incl. 0090). Rolled into Finding 2's note: `allowed_reference_files` not exhaustive of post-0154 migrations |
| 31 | Engine does not rewrite fork-authored bodies | as-specced — byte-exact preservation pinned (gate :123-137, :305-313) |

### Curation governance

| # | Requirement | Verdict |
|---|---|---|
| 32 | `sc skill add` removed — parser, help, author-resolution, DB write path; no hidden/deprecated alias; grant/revoke/rm/retire/unretire retain scope | as-specced — skill.py:311-335 six verbs only, verified by hand; absence test test_lns_curation.py:339-353 |
| 33 | curate rewritten: cluster → issue dedup → `skills: recommend <topic>` with six required elements → keep one compressed L&S until a reviewed skill ships and is granted | as-specced — curate/SKILL.md:59-93, all elements present |
| 34 | issue_reporting permits the recommendation route without the FnB-first gate; unavailable-search fallback (surface to FnB, keep L&S, no local skill) | as-specced — issue_reporting/SKILL.md:95-118 |
| 35 | Boot curation summary reflects recommendation-only policy | as-specced — templates/boot.md:143-165 |
| 36 | Deliberate fork authoring remains admin-owned via local_skill_management | as-specced — boot.md:147-153; test :378-383 |

### Mutation semantics

| # | Requirement | Verdict |
|---|---|---|
| 37 | DB mutation commits first; projection after; projection failure reported as partial failure naming the committed change + remedy; no rollback, no silent success | as-specced — CLI/API/feature all commit-then-reconcile with `partial_failure_message` (skill_projection.py:383-388); API returns `committed: true` on 500 |
| 38 | Concurrent reconcilers converge; per-file atomic writes; idempotent deletion | as-specced — atomic_write_text; gate proves retry-to-noop convergence (gate :263-317) |

### Verification gate

| # | Requirement | Verdict |
|---|---|---|
| 39 | Focused tests cover: tombstone validation, dirty migration, snapshot resurrection, grant serialization, CLI removal, curation wording, feature grants, GUI/CLI projection triggers, native mirrors, legacy flat cleanup, symlink confinement, local-skill preservation | as-specced — every area has named coverage (test_skill_tombstones.py, test_skill_lifecycle_convergence.py, test_skill_projection.py, test_lns_curation.py, test_feature.py, test_flavor_skill_packs.py, test_skill_convergence_release_gate.py). Gap rolled into Finding 3 |
| 40 | Adversarial gate: dirty fixture (13 retired rows, both grant types, stale snapshot, dormant worktrees, native mirrors, legacy skills_sc, fork-local skill, unmarked foreign file) exercised through update AND rebuild | as-specced — skill_convergence_fixtures.py + test_skill_convergence_release_gate.py; both paths proven non-vacuous then byte-identical noop |

## Findings

### Finding 1 — deviated-silently · Low — snapshot.py main() heals the live DB

**Spec:** "Snapshot generation must never serialize tombstoned skill rows or
their shell or flavor grants" (Database Convergence). The spec models snapshot
as a serializer of already-converged state; reconciliation owns the mutation
role in every other path.
**Code:** `snapshot.py:561-580` — `main()` opens the **live** DB and calls
`reconcile_tombstoned_skills(con)` before serializing; the savepoint RELEASE
commits the deletes (`seed_skills.py:135-137` docstring, sqlite legacy
isolation). Verified by hand at 9668def.
**Why it matters:** a command named "snapshot" silently mutates the DB it
reads — write semantics in a read-named surface, no flag to opt out, and it
does not re-apply the fork retire list the way its sibling heal
(`sync_engine_skills`) does. The unit-3 report declared it "intentional but
undeclared"; it is not among the ratified judgement calls, so it files as
deviated-silently. Impact is bounded: admin-gated (`require_admin`), reported
on stdout when it fires, no-op post-convergence, and the mutation is the same
convergent one every other path performs.
**Recommendation:** planner ratifies it (→ deviated-intentionally; add one
line to the spec's snapshot paragraph) or gates the heal behind an explicit
flag. Not a merge blocker either way.

### Finding 2 — deviated-silently · Low — duplicate migration number 0155

**Spec/sprint contract:** "A new trailing migration" per unit; sprint doc
sequencing note: "each unit that adds one numbers it against main at branch
time and renumbers on rebase if a sibling landed first."
**Code:** both `.super-coder/migrations/0155_reseed_catalogue_cleanup.sql`
(F33 unit 4) and `.super-coder/migrations/0155_sprint_conversation_generations.sql`
(sibling sprint work, #916) exist on main.
**Why it matters (and why only Low):** the ledger keys on full filename
(`migrate.py:40-44`, `filename TEXT PRIMARY KEY`) and applies in sorted
filename order (:49), so both apply deterministically — verified by hand. This
is a cross-sprint numbering collision, not a functional defect: exactly the
class of seam the conformance pass exists to catch. Convention debt only;
the next migration author should not treat 0156 as "current + 1" proof of
uniqueness. Related sub-note: the sprint-removal manifest's
`allowed_reference_files` inventory does not list 0155_reseed_catalogue_cleanup
or 0156 (no test failure — neither matches the reference pattern — but the
inventory is no longer exhaustive).
**Recommendation:** planner notes it for the sprint report; consider a
renumber or a manifest/lint guard against duplicate numeric prefixes.

### Finding 3 — Low (observation) — gate fixture lacks a native-root foreign control in the dormant worktree

The adversarial fixture plants unmarked foreign controls in legacy `skills_sc`
at both checkouts and in a native root at the **repo root only**
(test_skill_projection.py:232-289); `_populate_projections`
(skill_convergence_fixtures.py:304-330) writes only managed bodies into
worktree native roots, so foreign-dir preservation inside a dormant
worktree's `.claude/.agents/.opencode/skills` is never exercised by the gate.
The protection itself is structural (one `_remove_managed_tree` gate shared by
every checkout, verified by hand), so risk is low — but the #928 defect class
(deletion reaching foreign dirs) is exactly what this control would pin.
Carried forward from my unit-7 review note (#1003). Recommendation: one
foreign dir + sentinel file in the fixture worktree's native root.

### Finding 4 — Low (observation) — boot self-heal swallows reconciliation failure

`run.py:1308-1313` and :1012-1019 make the boot heal best-effort: a
reconciliation exception is rolled back and boot continues. Spec wants
self-heal so a missed path "cannot remain live indefinitely" — a persistently
failing heal would leave stale authority live across boots, surfaced only as
a boot-line warning. Render and update fail loudly, so convergence is still
guaranteed on those paths. Documented as deliberate; noted, no action
requested.

### Finding 5 — Low (observation) — install/teardown inventories are hard-coded duplicates

`install.py:137-150` + `:548-568` restate the managed-root set as literals
rather than deriving it from `managed_skill_dirs()`. Currently in exact
agreement and pinned by test_skill_projection.py:60, but a future adapter
adding a `skill_dirs` root drifts silently until that test trips. No action
requested.

## Judgement-call accounting

- **J1** (staged disjointness → strict final): main is strict; tolerance code
  absent; unit-4 negative tests present. Verdicts 2 above. CLOSED as ratified.
- **J2 + J4** (engine-managed-only exactness; universal foreign preservation):
  verdict 21 — deviated-intentionally, matches the ratified reading.
- **J3** (root-sweep foreign deletion = code defect, fixed in #928): the fix
  is on main and behaves as ruled (verified `_remove_managed_tree` gate).
  CLOSED.
- **snapshot.py live-DB heal** (unit-3 declared expansion): judged — Finding
  1, deviated-silently / Low, planner to ratify or scope.

## What was NOT re-verified

- Full test suite not executed in this pass (static inspection + targeted
  hand-verification only); suite-green evidence rides the unit-7 gate jobs
  (50/52/53 @ 9668def) already signed off.
- Runtime behavior of GUI endpoints exercised via their tests, not a live
  server.
