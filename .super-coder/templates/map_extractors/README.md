# Map extractors — reference plug-ins

These are **reference** extractors. They are NOT run from here. To activate one,
the cartographer copies it into the fork's `.sc-state/map_extractors/` (tracked,
fork-owned, survives `./sc update`) and adapts it to the repo's stack.

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

- `con` — the live **map db** connection (`.sc-state/map.db`). `dr_filepath` is
  already populated and committed when your extractor runs, so query it to find
  your inputs (don't re-walk the tree).
- `repo_root` — `pathlib.Path` to the repo root; read file bodies from here.
- `cfg` — the parsed `.sc-state/map.config.json` (dict). Per-extractor settings
  live under `cfg["extractors"]["<module_stem>"]` by convention.

Rules:
- **Own your table(s).** `DELETE` your rows then re-`INSERT` — the map is a
  derived cache, re-run on every `./sc map`. Write only to the `dr_*` tables your
  dimension owns (`dr_endpoint`, `dr_db_table`/`dr_db_column`, `dr_route`,
  `dr_component`). Their columns are standardized in `map_schema.sql`.
- **Best-effort, not exhaustive.** Match the dominant pattern; expect to miss
  dynamically-registered routes, computed paths, ORM-built schemas. Return a
  count, and log (in the summary) anything you knowingly skipped — never imply
  100% coverage.
- **Never raise fatally.** `map_repo` guards each extractor, but keep file reads
  and parses defensive; a crash just means your dimension is empty that run.
- Files named `_*.py` are ignored (use for shared helpers).

## What ships here

| File | Stack | Fills |
|---|---|---|
| `fastapi_endpoints.py` | FastAPI / Flask-style decorators | `dr_endpoint` |
| `sqlite_schema.py` | SQL `CREATE TABLE`/`VIEW` files | `dr_db_table`, `dr_db_column` |
| `sveltekit_routes.py` | SvelteKit filesystem routing | `dr_route`, `dr_component` |

Adopt the one(s) matching your repo; rename the `framework` label and the file
filter as needed. For a stack none of these cover (Django URLs, Express, Spring,
Rails), copy the closest one as a skeleton and rewrite the match.
