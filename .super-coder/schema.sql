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
    connections       TEXT,                          -- authored "where things live" notes; rendered in ## CONNECTIONS (B5)
    workspace         TEXT,                          -- RETIRED (B5): superseded by connections; unrendered, unauthored, kept to avoid a table rebuild

    lineage_seed      TEXT,
    flavor            TEXT,                          -- planning / dev / review (NULL = bespoke, e.g. maintainer)
    has_identity      INTEGER NOT NULL DEFAULT 0,
    bootstrapped      INTEGER NOT NULL DEFAULT 0,   -- 1 once the shell has run first-run orientation

    active_archive_id INTEGER,
    user_id           INTEGER REFERENCES users(user_id),
    is_shared         INTEGER NOT NULL DEFAULT 0,
    is_deleted        INTEGER NOT NULL DEFAULT 0
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
);

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

-- ── Repo catalogue (dr_*) — the host repo, mapped ───────────────────────────
-- super-coder is dropped INTO a host repo; these tables are how the shell reads
-- that repo without grepping blind. Structure is system (ships in the baseline);
-- the ROWS are a derived cache of the host repo, populated by scripts/map_repo.py
-- (`make map`, run at install) — NOT snapshotted, re-mappable when the repo
-- changes. v1 maps files / deps / env; semantic tables (api/db/page) come later.

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
