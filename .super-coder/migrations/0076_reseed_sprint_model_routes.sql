-- 0076 — reseed sprint_orchestration with model-route resolution.
--
-- The source asset is authoritative. These exact, idempotent replacements
-- carry the delta across a fresh rebuild (0001's older seed) and existing
-- forks; update.py's skill sync converges any already-divergent live row from
-- the same asset.

BEGIN;

UPDATE skills SET content = replace(content,
'coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). The answers parameterize every
`./sc run` you issue for this sprint. Per-unit model mixing is out of
scope — the interview covers the real need, provider choice per role.',
'coherent — reviewers stay a different lineage from the code they gate,
chosen per sprint instead of per boot. No answer -> `flavor_defaults`,
unchanged (omit the `models:` line). Every sprint worker still runs at high
effort. Per-unit model mixing is out of scope — the interview covers the real
need, provider choice per role.

**Resolve each answered route before declaring it.** Lazy-load only the two
choices the FnB made — never trust a display name or translate a provider id by
hand:

```
sc models resolve <devs-harness> <devs-model>
sc models resolve <reviewers-harness> <reviewers-model>
```

Each must return `route:` plus an exact `call:` ending in `--effort high`.
Failure means the selector is not locally callable, the harness lacks a
headless/high-effort seam, or Refresh models has not seen it. Run
`sc models list <harness>` for the local choices; the FnB''s **Refresh models**
button in `/#shells` repopulates the same runtime table. Resolve again after a
refresh. Never silently fall back across a provider or lineage.

Common exact selectors: Claude aliases (`fable`, `opus`) and Codex ids
(`gpt-5.6-sol`, `gpt-5.6-terra`) pass directly. Kimi takes the configured alias
shown by `sc models list kimi` (for example `kimi-code/k3`), never the bare
provider model `k3`.')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'# boot each first-in-chain dev headless, with the sprint''s models:
./sc run <dev> --harness <devs-harness> -m <devs-model>',
'# boot each first-in-chain dev with the RESOLVED selector; high is invariant:
./sc run <dev> --harness <devs-harness> -m <devs-model> --effort high')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'- **Review stall** (unit sitting `in-review` while its reviewer is idle):
  boot the reviewer — `./sc run <reviewer> --harness <reviewers-harness>
  -m <reviewers-model>`; its inbox holds the review request. Still stuck',
'- **Review stall** (unit sitting `in-review` while its reviewer is idle):
  boot the reviewer — `./sc run <reviewer> --harness <reviewers-harness>
  -m <reviewers-model> --effort high`; its inbox holds the review request. Still stuck')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it —
  `./sc run <shortname>` drains its inbox and acts; that IS the nudge in',
'- **Link gone quiet** (no `result` row, no `pr_event` movement): boot it with
  its declared sprint route — `./sc run <shortname> --harness <role-harness>
  -m <role-model> --effort high` drains its inbox and acts; that IS the nudge in')
WHERE name='sprint_orchestration';

UPDATE skills SET content = replace(content,
'   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model>',
'   ./sc run <reviewer> --harness <reviewers-harness> -m <reviewers-model> --effort high')
WHERE name='sprint_orchestration';

COMMIT;
