-- 0126 — fresh-install shell command and cartographer root contract.
--
-- 0122 established bare `sc` as the canonical worktree command but generated
-- boot text still exposed `./sc`, and the cartographer skill still assigned
-- canonical-root assets to an isolated worktree commit. Carry the corrected
-- cartographer guidance forward without replacing the skill row or its grants.

BEGIN;

UPDATE skills
SET content = REPLACE(
  content,
  '2. **Author the active map config** — `.sc-state/map.config.json` in tracked
   mode or `.sc-state/local/map/config.json` in local mode. It is per-instance
   and survives `sc update`. All keys optional; each merges over `map_repo.py`
   defaults:',
  '2. **Author the active map config at the canonical live root** —
   `$SC_ROOT/.sc-state/map.config.json` in tracked mode or
   `$SC_ROOT/.sc-state/local/map/config.json` in local mode. The mapper
   deliberately reads the shared live checkout, not your shell worktree. It is
   per-instance and survives `sc update`. All keys optional; each merges over
   `map_repo.py` defaults:'
)
WHERE name = 'cartographer';

UPDATE skills
SET content = REPLACE(
  content,
  '6. **Commit** the config + hooks (`git` skill) -> `sc mem state "…"` ->
   `sc mem oriented` (sets `bootstrapped=1` — the write is live in the
   shared DB; it does NOT snapshot).',
  '6. **Persist by mode.** Hook wiring is per-clone runtime state, never a commit.
   In tracked mode, use the `messaging` skill to send admin the exact canonical
   path (`.sc-state/map.config.json`) and verification result; only admin may
   commit the main checkout. In local mode, the config is intentionally ignored
   and needs no commit. Do not branch or commit the main checkout from the
   cartographer shell. Then `sc mem state "…"` -> `sc mem oriented` (sets
   `bootstrapped=1` — the write is live in the shared DB; it does NOT snapshot).'
)
WHERE name = 'cartographer';

UPDATE skills
SET content = REPLACE(
  content,
  '2. Edit `.sc-state/map.config.json` to match (step 2).',
  '2. Edit the active canonical-root config from step 2 to match.'
)
WHERE name = 'cartographer';

UPDATE skills
SET content = REPLACE(
  content,
  '7. Commit.',
  '7. Persist by mode as in first-boot step 6.'
)
WHERE name = 'cartographer';

UPDATE skills
SET content = REPLACE(
  content,
  '2. **Copy the matching reference** from the engine''s
   `.super-coder/templates/map_extractors/` into `.sc-state/map_extractors/`:',
  '2. **Copy the matching reference** from the engine''s
   `.super-coder/templates/map_extractors/` into
   `$SC_ROOT/.sc-state/map_extractors/`:'
)
WHERE name = 'cartographer';

UPDATE skills
SET content = REPLACE(
  content,
  '4. **Commit** `.sc-state/map_extractors/`. (Snapshotting the authored layer =
   the admin/GUI step above — not yours to run.)',
  '4. **Hand off persistence** to admin via the `messaging` skill, naming each
   changed `.sc-state/map_extractors/` path and the verification result. These
   canonical-root files are normal tracked files; snapshotting the authored DB
   layer remains the separate admin/GUI step above.'
)
WHERE name = 'cartographer';

COMMIT;
