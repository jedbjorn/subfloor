-- super-coder map catalogue (dr_*) — the host repo, mapped.
--
-- This is the schema for the MAP DB (`.sc-state/map.db`), a SEPARATE sqlite file
-- from the engine memory DB (`shell_db.db`). The map is a derived cache of the
-- host repo, owned by the cartographer and re-mappable any time; keeping it in
-- its own file means an engine memory-schema migration or rebuild never touches
-- it, and the cartographer can extend the map's schema locally without colliding
-- with the engine's. Populated by scripts/map_repo.py (`./sc map`).
--
-- Two layers live here: DERIVED (files/deps/env/repo — wiped + repopulated each
-- map) and AUTHORED (dr_section + dr_filepath.desc — cartographer-curated).
-- The authored layer is serialized to `.sc-state/map_content.sql` by snapshot.py
-- and reloaded on a fresh map DB; the derived layer is just re-mapped.

CREATE TABLE IF NOT EXISTS dr_repo (
    repo_id        INTEGER PRIMARY KEY,
    name           TEXT,
    root           TEXT,
    remote         TEXT,
    vcs            TEXT,
    default_branch TEXT,
    file_count     INTEGER,
    mapped_at      TEXT
);

CREATE TABLE IF NOT EXISTS dr_filepath (
    file_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL UNIQUE,    -- repo-relative; UNIQUE → map_repo UPSERTs by path
    ext      TEXT,
    lang     TEXT,                    -- inferred from extension
    role     TEXT,                    -- code / doc / config / test / asset / env
    bytes    INTEGER,
    lines    INTEGER,
    desc     TEXT                     -- ≤100 chars, cartographer-authored; NULL until described.
);                                    -- PRESERVED across the auto-remap (map_repo UPSERT keeps it).

-- Sectioned navigation over the file map (B5). Authored, stable, small (~10-20
-- rows) — NOT wiped by the remap. Files join to a section by path-prefix at
-- query/render time (no file ids stored), so a wiped+repopulated dr_filepath
-- never needs re-stitching and a new file auto-falls into its section. Seeded
-- from top-level dirs on first map; the cartographer renames / merges / curates.
CREATE TABLE IF NOT EXISTS dr_section (
    section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,          -- "API", "UI", "Docs", "Schema", …
    path_prefix  TEXT NOT NULL,          -- repo-relative prefix the section covers
    description  TEXT,                    -- one line, what this area is
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS dr_dependency (
    dep_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    manager     TEXT,                 -- npm / pip / poetry / go / cargo
    name        TEXT NOT NULL,
    version     TEXT,
    kind        TEXT,                 -- runtime / dev
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS dr_env (
    env_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    source_file TEXT
);

CREATE INDEX IF NOT EXISTS idx_dr_filepath_role ON dr_filepath(role);
CREATE INDEX IF NOT EXISTS idx_dr_filepath_lang ON dr_filepath(lang);
CREATE INDEX IF NOT EXISTS idx_dr_dependency_mgr ON dr_dependency(manager);

-- ── Tier-1 semantic dimensions ──────────────────────────────────────────────
-- Beyond files/deps/env, these capture the "what is this app" surface: HTTP
-- endpoints, the app's DB schema, and UI routes/components. They are DERIVED and
-- fork-specific: the COLUMNS are standardized here (so skills + the boot render
-- can rely on a stable shape across forks), but they stay EMPTY until the
-- cartographer wires an extractor for this repo's stack (see the `cartographer`
-- skill — `.sc-state/map_extractors/`). Each extractor owns its table: it
-- DELETEs then repopulates on every map, exactly like dr_dependency/dr_env.
-- Static extraction is best-effort ("most common, not 100%") — dynamically
-- registered routes etc. are expected to be missed; an extractor logs what it
-- could not parse rather than claiming completeness.

CREATE TABLE IF NOT EXISTS dr_endpoint (
    endpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    method      TEXT,                 -- GET / POST / PUT / PATCH / DELETE / …
    path        TEXT NOT NULL,        -- route path, e.g. /api/shells/{id}
    handler     TEXT,                 -- handler symbol or file:line
    framework   TEXT,                 -- fastapi / flask / express / … (which extractor)
    source_file TEXT                  -- repo-relative file the route is declared in
);

CREATE TABLE IF NOT EXISTS dr_db_table (
    db_table_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,        -- table / view name
    kind        TEXT,                 -- table / view
    source_file TEXT                  -- schema / migration / model file it is defined in
);

CREATE TABLE IF NOT EXISTS dr_db_column (
    db_column_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,       -- string ref to dr_db_table.name (cache; no FK)
    name         TEXT NOT NULL,
    type         TEXT,
    pk           INTEGER NOT NULL DEFAULT 0,
    not_null     INTEGER NOT NULL DEFAULT 0,   -- 'notnull' is a SQLite keyword
    source_file  TEXT
);

CREATE TABLE IF NOT EXISTS dr_route (
    route_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,        -- URL route derived from the file/router
    file        TEXT,                 -- repo-relative file backing the route
    kind        TEXT,                 -- page / endpoint / layout
    framework   TEXT                  -- sveltekit / nextjs / …
);

CREATE TABLE IF NOT EXISTS dr_component (
    component_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    path         TEXT,                -- repo-relative file
    framework    TEXT                 -- svelte / react / vue / …
);

CREATE INDEX IF NOT EXISTS idx_dr_endpoint_path ON dr_endpoint(path);
CREATE INDEX IF NOT EXISTS idx_dr_db_column_table ON dr_db_column(table_name);
CREATE INDEX IF NOT EXISTS idx_dr_route_path ON dr_route(path);
