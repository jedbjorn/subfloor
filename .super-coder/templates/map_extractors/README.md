# Map extractors — reference plug-ins

These are **reference** extractors. They are NOT run from here. Read the closest
reference, then author the adapted source at
`$SC_SHELL_WORKTREE/.sc-state/map_extractors/<name>.py` on the Cartographer's
branch. Install it only with:

```sh
sc map-extractor install "$SC_SHELL_WORKTREE/.sc-state/map_extractors/<name>.py"
```

Pass = output prints the canonical installed path + SHA-256 matching the
authored bytes. NEVER copy, move, redirect, or edit directly into
`$SC_ROOT/.sc-state/map_extractors/`.

The engine maps the generic 80% — files, languages, roles, dependencies, env
vars. Extractors add the semantic, per-repo dimensions the engine can't know
generically: HTTP **endpoints**, the app **DB schema**, UI **routes/components**.

## The contract

Each module defines one function:

```python
def extract(con, repo_root, cfg) -> str:
    ...
    return "N things"   # short summary for the map log
```

- `con` — the live **map db** connection (`.sc-state/local/map/map.db`). `dr_filepath` is
  already populated and committed when your extractor runs, so query it to find
  your inputs (don't re-walk the tree).
- `repo_root` — `pathlib.Path` to the repo root; read file bodies from here.
- `cfg` — the parsed `.sc-state/local/map/config.json` (dict). Per-extractor settings
  live under `cfg["extractors"]["<module_stem>"]` by convention.

Rules:
- **Inspect structure through the CLI.** Run `sc map-schema <dr_table>` before
  using unfamiliar columns; use `sc map-sql` only for data queries.
- **Own your table(s).** `DELETE` your rows then re-`INSERT` — the map is a
  derived cache, re-run on every `sc map`. Write only to the `dr_*` tables your
  dimension owns (`dr_endpoint`, `dr_db_table`/`dr_db_column`, `dr_route`,
  `dr_component`). Their columns are standardized in `map_schema.sql`.
- **Best-effort, not exhaustive.** Match the dominant pattern; expect to miss
  dynamically-registered routes, computed paths, ORM-built schemas. Return a
  count, and log (in the summary) anything you knowingly skipped — never imply
  100% coverage.
- **Never raise fatally.** Guard file reads/parses. `map_repo` rolls a failed
  plug-in's writes back and preserves the core map, but `sc map finalize` keeps
  Live map failed until the module succeeds.
- Files named `_*.py` are ignored (use for shared helpers).

## What ships here

| File | Stack | Fills |
|---|---|---|
| `fastapi_endpoints.py` | FastAPI / Flask-style decorators | `dr_endpoint` |
| `sqlite_schema.py` | SQL `CREATE TABLE`/`VIEW` files | `dr_db_table`, `dr_db_column` |
| `sveltekit_routes.py` | SvelteKit filesystem routing | `dr_route`, `dr_component` |

Adopt the one(s) matching your repo; rename the `framework` label and the file
filter as needed. For an uncovered stack (Django URLs, Express, Spring, Rails),
read the closest reference and author the new match in the Cartographer
worktree. Run `sc map`, verify rows through `sc map-schema` + `sc map-sql`, then
run `sc map finalize`; pass = every required row is `PASS` / `N/A`.
