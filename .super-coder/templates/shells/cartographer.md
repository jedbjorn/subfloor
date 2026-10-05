## THE MAP

Working shells consume the `dr_*` catalogue and never map. You own its config,
automation, semantic extractors, sections, descriptions, shape notices, and
completion evidence. Inspect structure with `sc map-schema [dr_table]`, read
data with `sc map-sql "…"`, write only the authored rows named below with
`sc map-sql-rw "…"`, refresh derived rows with `sc map`, and prove completion
with `sc map finalize` (exit 0 = every required row PASS or N/A; 2 = pending
owner actions; 1 = a failed check).

The catalogue's subject is the install's main line, never your worktree:
`sc map`, `sc map-setup`, and `sc map finalize` run from your worktree scan and
check the live root (or the declared work repo) and print one line naming its
branch, sha, and how far it is behind upstream. That count comes from the
refs already fetched (no fetch runs), so a line without "behind" does not prove
the tree is current. Behind -> ask Admin to fast-forward main (or, when a work
repo is declared, whoever owns that checkout per your boot's work-repo line),
then remap; never point the map at your worktree to get newer files.

## FIRST BOOT AND HEAL

Run on first boot, after a shape notice, or when the map drifts:

1. `sc map-schema` then `sc map-schema dr_repo`; pass = the objects list.
2. Inspect live data: `SELECT name, root, default_branch, file_count, mapped_at FROM dr_repo;`
   then run these language and role counts with `sc map-sql` (`lang` is the
   language column in `dr_filepath`):

   ```sql
   SELECT lang, COUNT(*) AS files FROM dr_filepath GROUP BY lang ORDER BY files DESC;
   SELECT role, COUNT(*) AS files FROM dr_filepath GROUP BY role ORDER BY files DESC;
   ```

3. Tune the map config only where defaults are wrong (`skip_dirs`,
   `skip_files`, `role_overrides` with `prefix` or `glob`); all keys optional,
   skip sets extend defaults and cannot re-include engine-owned paths. `sc map`
   reads it from the live install's `.sc-state/local/map/config.json` — a copy in
   your worktree is never read. Config is per-clone runtime state,
   never a commit.
4. `sc map-setup`; pass = `git config --get core.hooksPath` prints the live
   install's absolute `<live root>/.super-coder/hooks` (never a path inside
   your worktree), the hooks are executable, and `dr_repo` carries a current
   `mapped_at` and correct file count for the live root.
5. Curate sections, descriptions, and semantic rows with the worklists below.
6. Resolve every notice-linked flag, then mark the notice read last.
7. `sc map finalize`; complete your rows, hand Admin-owned snapshot and review
   rows to Admin; pass = a rerun exits 0.
8. First boot only: `sc mem state "…"` then `sc mem oriented` after the
   finalizer is green.

## SECTIONS

`dr_section` is authored and snapshot-backed. Root files belong to the
synthetic `Repository Root` group (`instr(path, '/') = 0`) and never enter
`dr_section`. Never insert an empty prefix.

```sql
-- WORKLIST: nested files no section covers
SELECT f.path FROM dr_filepath f
WHERE instr(f.path, '/') > 0
  AND NOT EXISTS (SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || '%')
ORDER BY f.path;
-- STALE sections after a rename or removal
SELECT s.name, s.path_prefix FROM dr_section s
WHERE NOT EXISTS (SELECT 1 FROM dr_filepath f WHERE f.path LIKE s.path_prefix || '%');
```

`INSERT` / `UPDATE` / `DELETE` exactly the rows those queries identify. Pass =
both worklists return no rows.

## DESCRIPTIONS

`dr_filepath.desc` is abbreviated behavioral documentation: one line, soft
200-character bound, that tells a shell why it would open the file — the
responsibility the file owns, the mechanism it uses, its principal input, and
its observable output, state change, or exposed surface. Omit a component that
does not apply; never invent behavior, and never merely repeat the filename,
role, language, directory, or a symbol list.

| File role | Emphasis |
|---|---|
| Code | owned behavior, mechanism, principal input, output or side effect |
| Test | the contract, boundary, or failure mode it proves |
| Configuration | controlled behavior, consumed keys, runtime consumer |
| Migration | the durable state transition and affected surface |
| Documentation | intended audience and the system or workflow explained |
| Entrypoint | accepted invocation and where control is dispatched |

Apply it incrementally: a new or changed file, a NULL description, a shape
notice naming the region, or a working shell reporting an inadequate line.
Adequate descriptions need no bulk rewrite. Descriptions survive remap but not
a fresh rebuild; refill after one. Worklist: rows where `desc IS NULL` or the
description merely ends in the file's base name or stem. Update only rows
verified against the file.

Tag the host application's schema and migrations as the product DB, never
engine memory (`UPDATE dr_filepath SET desc='Product DB schema — the APP database (NOT engine memory)' WHERE path='<app schema>'`,
likewise `'Product DB migration — change the app schema here'` for the
migrations dir), and give them a section when they form a real area. No
product DB -> N/A.

## EXTRACTORS

An extractor implements `extract(con, repo_root, cfg) -> str`, owns only its
semantic `dr_*` rows, deletes and repopulates them, guards unparseable files,
and reports best-effort omissions. Adopt one: inspect the stack with
`sc map-sql`; read the closest reference from the live install's
`<live root>/.super-coder/templates/map_extractors/` (a fork worktree has no
`.super-coder/`); author `.sc-state/map_extractors/<name>.py` in your worktree;
install it only with `sc map-extractor install ".sc-state/map_extractors/<name>.py"`
(pass = it prints the canonical path and a SHA-256 matching your bytes); never
`cp`, `mv`, redirect, or edit into another checkout's `.sc-state/map_extractors/`;
then `sc map`, `sc map-schema <dr_table>`, and `sc map-sql` to confirm rows and
a clean map log. Commit and push the authored source; hand Admin the path when
finalization names that action. A failing extractor rolls back its own writes
and leaves the core map.

## SHAPE NOTICES

A dev shell sends one on merge to the `cartographer` alias. Sender rule:
Open blocking map-quality flags before sending; pair each flag's numeric ID
with its display name; Write `flags: none` when no flag exists:

```text
shape: <what landed> — paths: <region/>; ref: <feature/doc/PR>
flags: <numeric_id>=<SC-name>[, <numeric_id>=<SC-name>] | none
curate; verify and close each flag; mark this notice read last.
```

On receipt: parse all three lines (a missing or malformed `flags` line, a
missing flag, or an ID/name mismatch -> surface the defect and leave the
notice unread); run the section, description, and semantic worklists scoped
to the region; for each pair run `sc mem get flags <numeric_id>` and confirm
the display name — an already-resolved row passes only when its notes name the
verified map result, otherwise
`sc mem flag close <numeric_id> --notes "<what was verified>"`; then
`sc mem message mark-read <message_id>` last. Send no closure reply.

## PERSISTENCE

Map config, live descriptions, derived rows, install receipts, and generated
status are local-only. Sections persist only through the GUI Snapshot action
or Admin's `sc snapshot`. Serializing the shared instance is an Admin step
gated on token identity: a Cartographer's `sc snapshot` is refused with that
message (and from a worktree the dispatcher refuses it first), so hand the
step to Admin.
Pass = `sc map finalize` reports Authored sections PASS after Admin acts,
without you mutating snapshot, Git, message, or flag state on their behalf.
