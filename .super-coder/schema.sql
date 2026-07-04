-- super-coder — SQLite schema (full current baseline).
--
-- Forkable shell substrate for a single repo. Derived from superCC's substrate
-- schema, inverted to the one-repo model and extended with the roadmap index +
-- content store (spec §Data Model).
--
-- The live shell_db.db is GITIGNORED and rebuilt from this file + migrations/ +
-- snapshot/. A fresh build applies this whole file; existing forks catch up via
-- ordered migrations/*.sql (recorded in schema_migrations).
--
-- Auth note (v1): the launcher is username-only — no password challenge. The
-- password_hash/password_salt columns are kept nullable for forward-compat but
-- are unused at v1.

-- ── Migration ledger ────────────────────────────────────────────────────────
-- Records which migrations/*.sql files have been applied. A fresh build stamps
-- every existing migration as the baseline (squash); updates apply only the
-- unstamped ones.

CREATE TABLE schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Identity ────────────────────────────────────────────────────────────────

CREATE TABLE users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    email         TEXT,
    initials      TEXT,
    password_hash TEXT,                 -- unused at v1 (no-password launcher)
    password_salt TEXT,                 -- unused at v1
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE shells (
    shell_id          INTEGER PRIMARY KEY,
    display_name      TEXT    NOT NULL,
    shortname         TEXT,
    partner           TEXT,
    role              TEXT,
    mandate           TEXT,
    system_prompt     TEXT    NOT NULL,
    current_state     TEXT,
    connections       TEXT,                          -- RETIRED (B5): authored "where things live" layer; nothing prompted shells to fill it so it sat empty — ## CONNECTIONS is now wholly derived from the dr_* map. Unrendered, unauthored, kept to avoid a table rebuild
    workspace         TEXT,                          -- RETIRED (B5): superseded by connections (itself since retired); unrendered, unauthored, kept to avoid a table rebuild

    lineage_seed      TEXT,
    flavor            TEXT,                          -- dev / planner / reviewer / cartographer (NULL = bespoke, e.g. maintainer); launch defaults in flavor_defaults
    has_identity      INTEGER NOT NULL DEFAULT 0,
    bootstrapped      INTEGER NOT NULL DEFAULT 0,   -- 1 once the shell has run first-run orientation

    active_archive_id INTEGER,
    user_id           INTEGER REFERENCES users(user_id),
    is_shared         INTEGER NOT NULL DEFAULT 0,
    is_deleted        INTEGER NOT NULL DEFAULT 0
);

-- Singleton guard: a fork has exactly one cartographer — it owns the repo map
-- and no other shell maps, so a second one is incoherent. Mirrors the seed/L&S
-- cap triggers (RAISE(ABORT) below the line). is_deleted=0 so a deleted
-- cartographer frees the slot. shell_factory pre-checks for a friendly error;
-- this is the DB backstop that also catches direct writes / the API path.
CREATE TRIGGER trg_singleton_cartographer
BEFORE INSERT ON shells
WHEN NEW.flavor = 'cartographer' AND (
  SELECT COUNT(*) FROM shells
  WHERE flavor = 'cartographer' AND is_deleted = 0
) >= 1
BEGIN
  SELECT RAISE(ABORT, 'cartographer is a singleton — this fork already has one');
END;

-- Per-flavor launch defaults: the harness + model a shell of this flavor boots
-- with. ADVISORY ONLY — overridable per launch (--harness / -m / the picker);
-- A (flavor, harness) matrix: each flavor offers a model per harness, so the
-- operator picks the harness at launch and gets that harness's model. run.py
-- reads these to resolve the launch model + annotate the picker; is_default marks
-- the picker's pre-selected harness for a flavor. model is harness-specific (codex
-- bare id "gpt-5.4" / claude alias "sonnet" / opencode "provider/model"); NULL =
-- let the harness pick its own. Reshaped + reseeded in migrations/0007.
CREATE TABLE flavor_defaults (
    flavor     TEXT    NOT NULL,
    harness    TEXT    NOT NULL,
    model      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flavor, harness)
);

CREATE TABLE shell_memory_archives (
    archive_id     INTEGER PRIMARY KEY,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    session_id     TEXT,
    date           DATE    NOT NULL,
    full_narrative TEXT
);

-- ── Seed + L&S (table-backed, cap-enforced) ─────────────────────────────────

CREATE TABLE shell_identity_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id    INTEGER NOT NULL REFERENCES shells(shell_id),
    kind        TEXT    NOT NULL CHECK (kind IN ('seed', 'lns')),
    entry_date  TEXT,
    source_tag  TEXT,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    retired_at  TEXT,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER trg_sie_cap_seed
BEFORE INSERT ON shell_identity_entries
WHEN NEW.kind = 'seed' AND (
  SELECT COUNT(*) FROM shell_identity_entries
  WHERE shell_id = NEW.shell_id AND kind='seed'
    AND is_deleted=0 AND retired_at IS NULL
) >= 10
BEGIN
  SELECT RAISE(ABORT, 'seed cap (10) reached for this shell — retire an entry first');
END;

CREATE TRIGGER trg_sie_cap_lns
BEFORE INSERT ON shell_identity_entries
WHEN NEW.kind = 'lns' AND (
  SELECT COUNT(*) FROM shell_identity_entries
  WHERE shell_id = NEW.shell_id AND kind='lns'
    AND is_deleted=0 AND retired_at IS NULL
) >= 20
BEGIN
  SELECT RAISE(ABORT, 'L&S cap (20) reached for this shell — retire an entry first');
END;

-- ── Decisions ───────────────────────────────────────────────────────────────

CREATE TABLE shell_decisions (
    decision_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id           INTEGER NOT NULL REFERENCES shells(shell_id),
    decision_date      DATE    NOT NULL,
    priority           TEXT    NOT NULL DEFAULT 'M' CHECK(priority IN ('M','m')),
    decision           TEXT    NOT NULL,
    rationale          TEXT,
    parent_decision_id INTEGER REFERENCES shell_decisions(decision_id),
    is_deleted         INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    -- feature_id  INTEGER REFERENCES roadmap(feature_id)   — the feature this
    -- document_id INTEGER REFERENCES documents(document_id) — decision shaped
    -- (the why-audit link), both added by migration 0047. Kept out of this
    -- baseline CREATE on purpose: ADD COLUMN can't be IF NOT EXISTS and rebuild
    -- applies migrations after schema.sql, so inlining would double-define.
    -- See migrations/0047_decisions_feature_link.sql.
);

-- ── Roadmap (NEW — the feature index) ───────────────────────────────────────
-- One row per planned feature. The DB *is* the index that kills "where does the
-- spec for X live." Status is a planning horizon (a column, not a folder).

CREATE TABLE roadmap (
    feature_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    roadmap_status TEXT    NOT NULL DEFAULT 'brainstorm'
                   -- funnel order: idea inlet → most-active committed work →
                   -- done (shipped) → taken-off-the-board (retired). shipped
                   -- means we delivered; retired means we chose not to.
                   CHECK (roadmap_status IN
                       ('brainstorm','in_progress','next','near_term',
                        'long_term','shipped','retired')),
    sort_order     INTEGER NOT NULL DEFAULT 0,   -- ordering within a bucket
    owning_shell   INTEGER REFERENCES shells(shell_id),
    summary        TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    -- project_id INTEGER REFERENCES projects(project_id) — work-stream this
    -- feature belongs to (NULL = unassigned), added by migration 0018. Kept out
    -- of this baseline CREATE on purpose: ADD COLUMN can't be IF NOT EXISTS and
    -- rebuild applies migrations after schema.sql, so inlining it would
    -- double-define the column. See migrations/0018_roadmap_project.sql.
);

-- ── Feature blockers (the roadmap's sequencing edges) ───────────────────────
-- A directed many-to-many self-relation on roadmap. One row = one dependency:
-- `feature_id` is blocked by `blocked_by` (blocked_by must land first). A feature
-- may be blocked by many. The flowchart view renders these as arrows. Cycle
-- prevention is app-level (server.py) so the graph stays a DAG; the table guards
-- only against self-blocks and duplicates. Edges among brainstorm/retired
-- features are simply not drawn (those stages don't sequence yet).

CREATE TABLE feature_blockers (
    feature_id  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    blocked_by  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feature_id, blocked_by),
    CHECK (feature_id <> blocked_by)
);
CREATE INDEX idx_feature_blockers_blocked_by ON feature_blockers(blocked_by);

-- ── Documents (NEW — the content store) ─────────────────────────────────────
-- DB owns the body, always. A feature accumulates MULTIPLE specs over its life:
-- each stage's spec freezes on ship (frozen=1, immutable), the feature lives on,
-- the next stage opens a new spec. One feature : many docs, each freezable.

CREATE TABLE documents (
    document_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id   INTEGER REFERENCES roadmap(feature_id),  -- NULL = general doc (not tied to a feature)
    kind         TEXT    NOT NULL DEFAULT 'spec' CHECK (kind IN ('spec','doc')),
    seq          INTEGER NOT NULL DEFAULT 1,     -- lineage within (feature, kind)
    title        TEXT,
    frozen       INTEGER NOT NULL DEFAULT 0,     -- 1 = frozen on ship, immutable
    frozen_date  TEXT,
    body         TEXT,                           -- canonical markdown, lives here
    render_path  TEXT,                           -- repo-relative flat-file target
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(feature_id, kind, seq)
);

-- ── Flags (substrate task tracking; link to a feature) ──────────────────────

CREATE TABLE flags (
    flag_id          INTEGER PRIMARY KEY,
    display_name     TEXT,
    priority         TEXT    NOT NULL DEFAULT 'Medium'
                     CHECK(priority IN ('High','Medium','Low')),
    description      TEXT,
    created_date     DATE    NOT NULL DEFAULT (date('now')),
    resolved_date    DATE,
    resolved         INTEGER NOT NULL DEFAULT 0,
    shell_id         INTEGER REFERENCES shells(shell_id),
    feature_id       INTEGER REFERENCES roadmap(feature_id),  -- a feature's blockers
    resolution_notes TEXT,
    parent_flag_id   INTEGER REFERENCES flags(flag_id),
    is_deleted       INTEGER NOT NULL DEFAULT 0
);

-- ── Spec tasks (per-instance — implementation plan for a spec) ──────────────
-- One row per task. Seq 0 = Preparation, last seq = Verification, middle = impl
-- steps. Status drives current_state updates (last done + next pending).

CREATE TABLE spec_tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id     INTEGER NOT NULL REFERENCES roadmap(feature_id),
    document_id    INTEGER NOT NULL REFERENCES documents(document_id),
    seq            INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','in_progress','done')),
    completed_date DATE,
    shell_id       INTEGER REFERENCES shells(shell_id),
    created_date   DATE    NOT NULL DEFAULT (date('now')),
    UNIQUE(document_id, seq)
);

-- ── Shell Inbox (inter-shell messaging) ─────────────────────────────────────
-- A shell writes a markdown message to another shell; the recipient discovers it
-- on its next boot (the `## STATUS` Inbox count + the `messaging` skill's `check`
-- verb) and marks it read by UPDATE-ing `read_at`. No API layer in v1 — the
-- `messaging` skill runs parameterized SQL directly (single-user, localhost). The
-- only enforcement v1 has is at the DB layer: FK on from/to, NOT NULL, body CHECK.
-- See migrations/0004_shell_messages.sql (convergent — carries an existing fork).

CREATE TABLE shell_messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    from_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    to_shell_id   INTEGER NOT NULL REFERENCES shells(shell_id),
    body          TEXT    NOT NULL CHECK (length(body) > 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at       TEXT                          -- NULL = unread
);

-- ── Skills (system content — seeded from assets/, propagates) ────────────────

CREATE TABLE skills (
    skill_id    INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    category    TEXT,
    content     TEXT,
    command     TEXT,
    common      INTEGER NOT NULL DEFAULT 1,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE shell_skills (
    shell_skill_id  INTEGER PRIMARY KEY,
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    skill_id        INTEGER NOT NULL REFERENCES skills(skill_id),
    UNIQUE(shell_id, skill_id)
);

-- ── Projects (per-shell project standing) ───────────────────────────────────

CREATE TABLE projects (
    project_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    shortname    TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    purpose      TEXT,
    standing     TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK(status IN ('active','inactive','paused')),
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_shells (
    project_shell_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(project_id),
    shell_id         INTEGER NOT NULL REFERENCES shells(shell_id),
    role             TEXT,
    added_date       DATE NOT NULL DEFAULT (date('now')),
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, shell_id)
);

-- ── Repo catalogue (dr_*) — VESTIGIAL, transition-only ──────────────────────
-- The map moved to its OWN db (`.sc-state/map.db`, schema in `map_schema.sql`)
-- so engine memory-schema changes never touch it. These definitions remain in
-- shell_db.db for ONE release purely so a pre-split `.sc-state/content.sql`
-- (which still carries `INSERT INTO dr_section …`) can load on a rebuild without
-- erroring. map_repo no longer writes here; map_db.seed_authored() lifts any
-- rows that land here into map.db on the first post-split map. Remove in a later
-- release once all forks have re-snapshotted (dr_section → map_content.sql).

CREATE TABLE dr_repo (
    repo_id        INTEGER PRIMARY KEY,
    name           TEXT,
    root           TEXT,
    remote         TEXT,
    vcs            TEXT,
    default_branch TEXT,
    file_count     INTEGER,
    mapped_at      TEXT
);

CREATE TABLE dr_filepath (
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
CREATE TABLE dr_section (
    section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,          -- "API", "UI", "Docs", "Schema", …
    path_prefix  TEXT NOT NULL,          -- repo-relative prefix the section covers
    description  TEXT,                    -- one line, what this area is
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE dr_dependency (
    dep_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    manager     TEXT,                 -- npm / pip / poetry / go / cargo
    name        TEXT NOT NULL,
    version     TEXT,
    kind        TEXT,                 -- runtime / dev
    source_file TEXT
);

CREATE TABLE dr_env (
    env_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    source_file TEXT
);

-- ── Indexes ─────────────────────────────────────────────────────────────────

CREATE INDEX idx_flags_parent   ON flags(parent_flag_id);
CREATE INDEX idx_flags_feature  ON flags(feature_id);
CREATE INDEX idx_decisions_shell ON shell_decisions(shell_id, decision_date);
CREATE INDEX idx_roadmap_status ON roadmap(roadmap_status, sort_order);
CREATE INDEX idx_documents_feature ON documents(feature_id, kind, seq);
CREATE INDEX idx_sie_shell_kind_active
    ON shell_identity_entries(shell_id, kind)
    WHERE is_deleted = 0 AND retired_at IS NULL;
CREATE INDEX idx_shell_messages_to_unread ON shell_messages(to_shell_id, read_at);
CREATE INDEX idx_dr_filepath_role ON dr_filepath(role);
CREATE INDEX idx_dr_filepath_lang ON dr_filepath(lang);
CREATE INDEX idx_dr_dependency_mgr ON dr_dependency(manager);
