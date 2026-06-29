-- super-coder — PostgreSQL schema (full current baseline).
--
-- Postgres equivalent of schema.sql. Applied by rebuild.py when DATABASE_URL
-- is set. All migrations (0001–current) are baked in; rebuild stamps them all
-- as applied so future `./sc update` only runs new ones.
--
-- Column types: TIMESTAMP/DATE in place of SQLite's TEXT; the db_driver layer
-- coerces them back to ISO strings so Python callers see the same format.
-- Sequences: SERIAL allows explicit ID inserts (from content.sql); rebuild.py
-- calls reset_sequences() afterward to sync them with the loaded data.

-- ── Migration ledger ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Identity ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    user_id       SERIAL PRIMARY KEY,
    username      TEXT      NOT NULL UNIQUE,
    email         TEXT,
    initials      TEXT,
    password_hash TEXT,
    password_salt TEXT,
    is_active     INTEGER   NOT NULL DEFAULT 1,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shells (
    shell_id          SERIAL PRIMARY KEY,
    display_name      TEXT      NOT NULL,
    shortname         TEXT,
    partner           TEXT,
    role              TEXT,
    mandate           TEXT,
    system_prompt     TEXT      NOT NULL,
    current_state     TEXT,
    connections       TEXT,
    workspace         TEXT,
    lineage_seed      TEXT,
    flavor            TEXT,
    has_identity      INTEGER   NOT NULL DEFAULT 0,
    bootstrapped      INTEGER   NOT NULL DEFAULT 0,
    active_archive_id INTEGER,
    user_id           INTEGER   REFERENCES users(user_id),
    is_shared         INTEGER   NOT NULL DEFAULT 0,
    is_deleted        INTEGER   NOT NULL DEFAULT 0
);

-- Singleton cartographer guard
CREATE OR REPLACE FUNCTION _trg_singleton_cartographer_fn()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.flavor = 'cartographer' AND (
        SELECT COUNT(*) FROM shells
        WHERE flavor = 'cartographer' AND is_deleted = 0
    ) >= 1 THEN
        RAISE EXCEPTION 'cartographer is a singleton — this fork already has one';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_singleton_cartographer
BEFORE INSERT ON shells
FOR EACH ROW EXECUTE FUNCTION _trg_singleton_cartographer_fn();

CREATE TABLE IF NOT EXISTS flavor_defaults (
    flavor     TEXT    NOT NULL,
    harness    TEXT    NOT NULL,
    model      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flavor, harness)
);

CREATE TABLE IF NOT EXISTS shell_memory_archives (
    archive_id     SERIAL PRIMARY KEY,
    shell_id       INTEGER   NOT NULL REFERENCES shells(shell_id),
    session_id     TEXT,
    date           DATE      NOT NULL,
    full_narrative TEXT
);

-- ── Seed + L&S ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shell_identity_entries (
    entry_id    SERIAL  PRIMARY KEY,
    shell_id    INTEGER NOT NULL REFERENCES shells(shell_id),
    kind        TEXT    NOT NULL CHECK (kind IN ('seed', 'lns')),
    entry_date  TEXT,
    source_tag  TEXT,
    body        TEXT    NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retired_at  TEXT,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

-- Seed cap (10 per shell)
CREATE OR REPLACE FUNCTION _trg_sie_cap_seed_fn()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.kind = 'seed' AND (
        SELECT COUNT(*) FROM shell_identity_entries
        WHERE shell_id = NEW.shell_id AND kind = 'seed'
          AND is_deleted = 0 AND retired_at IS NULL
    ) >= 10 THEN
        RAISE EXCEPTION 'seed cap (10) reached for this shell — retire an entry first';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_sie_cap_seed
BEFORE INSERT ON shell_identity_entries
FOR EACH ROW EXECUTE FUNCTION _trg_sie_cap_seed_fn();

-- L&S cap (20 per shell)
CREATE OR REPLACE FUNCTION _trg_sie_cap_lns_fn()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.kind = 'lns' AND (
        SELECT COUNT(*) FROM shell_identity_entries
        WHERE shell_id = NEW.shell_id AND kind = 'lns'
          AND is_deleted = 0 AND retired_at IS NULL
    ) >= 20 THEN
        RAISE EXCEPTION 'L&S cap (20) reached for this shell — retire an entry first';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_sie_cap_lns
BEFORE INSERT ON shell_identity_entries
FOR EACH ROW EXECUTE FUNCTION _trg_sie_cap_lns_fn();

-- ── Decisions ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shell_decisions (
    decision_id        SERIAL  PRIMARY KEY,
    shell_id           INTEGER NOT NULL REFERENCES shells(shell_id),
    decision_date      DATE    NOT NULL,
    priority           TEXT    NOT NULL DEFAULT 'M' CHECK(priority IN ('M','m')),
    decision           TEXT    NOT NULL,
    rationale          TEXT,
    parent_decision_id INTEGER REFERENCES shell_decisions(decision_id),
    is_deleted         INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Roadmap ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS roadmap (
    feature_id     SERIAL  PRIMARY KEY,
    title          TEXT    NOT NULL,
    roadmap_status TEXT    NOT NULL DEFAULT 'brainstorm'
                   CHECK (roadmap_status IN
                       ('brainstorm','in_progress','next','near_term',
                        'long_term','shipped','retired')),
    sort_order     INTEGER NOT NULL DEFAULT 0,
    owning_shell   INTEGER REFERENCES shells(shell_id),
    summary        TEXT,
    project_id     INTEGER,   -- FK added below after projects table
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Feature blockers ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS feature_blockers (
    feature_id  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    blocked_by  INTEGER NOT NULL REFERENCES roadmap(feature_id),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (feature_id, blocked_by),
    CHECK (feature_id <> blocked_by)
);
CREATE INDEX IF NOT EXISTS idx_feature_blockers_blocked_by ON feature_blockers(blocked_by);

-- ── Documents ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS documents (
    document_id  SERIAL  PRIMARY KEY,
    feature_id   INTEGER REFERENCES roadmap(feature_id),
    kind         TEXT    NOT NULL DEFAULT 'spec' CHECK (kind IN ('spec','doc')),
    seq          INTEGER NOT NULL DEFAULT 1,
    title        TEXT,
    frozen       INTEGER NOT NULL DEFAULT 0,
    frozen_date  TEXT,
    body         TEXT,
    render_path  TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(feature_id, kind, seq)
);

-- ── Flags ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS flags (
    flag_id          SERIAL  PRIMARY KEY,
    display_name     TEXT,
    priority         TEXT    NOT NULL DEFAULT 'Medium'
                     CHECK(priority IN ('High','Medium','Low')),
    description      TEXT,
    created_date     DATE    NOT NULL DEFAULT CURRENT_DATE,
    resolved_date    DATE,
    resolved         INTEGER NOT NULL DEFAULT 0,
    shell_id         INTEGER REFERENCES shells(shell_id),
    feature_id       INTEGER REFERENCES roadmap(feature_id),
    resolution_notes TEXT,
    parent_flag_id   INTEGER REFERENCES flags(flag_id),
    is_deleted       INTEGER NOT NULL DEFAULT 0
);

-- ── Spec tasks ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS spec_tasks (
    task_id        SERIAL  PRIMARY KEY,
    feature_id     INTEGER NOT NULL REFERENCES roadmap(feature_id),
    document_id    INTEGER NOT NULL REFERENCES documents(document_id),
    seq            INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','in_progress','done')),
    completed_date DATE,
    shell_id       INTEGER REFERENCES shells(shell_id),
    created_date   DATE    NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE(document_id, seq)
);

-- ── Shell inbox ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shell_messages (
    message_id    SERIAL  PRIMARY KEY,
    from_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    to_shell_id   INTEGER NOT NULL REFERENCES shells(shell_id),
    body          TEXT    NOT NULL CHECK (length(body) > 0),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at       TEXT
);

-- ── Skills ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS skills (
    skill_id    SERIAL  PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    category    TEXT,
    content     TEXT,
    command     TEXT,
    common      INTEGER NOT NULL DEFAULT 1,
    is_deleted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shell_skills (
    shell_skill_id  SERIAL  PRIMARY KEY,
    shell_id        INTEGER NOT NULL REFERENCES shells(shell_id),
    skill_id        INTEGER NOT NULL REFERENCES skills(skill_id),
    UNIQUE(shell_id, skill_id)
);

-- ── Projects ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS projects (
    project_id   SERIAL  PRIMARY KEY,
    shortname    TEXT    NOT NULL UNIQUE,
    title        TEXT    NOT NULL,
    purpose      TEXT,
    standing     TEXT,
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK(status IN ('active','inactive','paused')),
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_shells (
    project_shell_id SERIAL  PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES projects(project_id),
    shell_id         INTEGER NOT NULL REFERENCES shells(shell_id),
    role             TEXT,
    added_date       DATE    NOT NULL DEFAULT CURRENT_DATE,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, shell_id)
);

-- Add project_id FK now that projects exists
ALTER TABLE roadmap ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(project_id);

-- ── Repo catalogue (dr_* — vestigial, transition-only) ───────────────────────
-- Kept for compatibility with content.sql from pre-map-split forks.

CREATE TABLE IF NOT EXISTS dr_repo (
    repo_id        SERIAL PRIMARY KEY,
    name           TEXT,
    root           TEXT,
    remote         TEXT,
    vcs            TEXT,
    default_branch TEXT,
    file_count     INTEGER,
    mapped_at      TEXT
);

CREATE TABLE IF NOT EXISTS dr_filepath (
    file_id  SERIAL PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    ext      TEXT,
    lang     TEXT,
    role     TEXT,
    bytes    INTEGER,
    lines    INTEGER,
    desc     TEXT
);

CREATE TABLE IF NOT EXISTS dr_section (
    section_id   SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    path_prefix  TEXT NOT NULL,
    description  TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS dr_dependency (
    dep_id      SERIAL PRIMARY KEY,
    manager     TEXT,
    name        TEXT NOT NULL,
    version     TEXT,
    kind        TEXT,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS dr_env (
    env_id      SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    source_file TEXT
);

-- ── Indexes ──────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_flags_parent      ON flags(parent_flag_id);
CREATE INDEX IF NOT EXISTS idx_flags_feature     ON flags(feature_id);
CREATE INDEX IF NOT EXISTS idx_decisions_shell   ON shell_decisions(shell_id, decision_date);
CREATE INDEX IF NOT EXISTS idx_roadmap_status    ON roadmap(roadmap_status, sort_order);
CREATE INDEX IF NOT EXISTS idx_documents_feature ON documents(feature_id, kind, seq);
CREATE INDEX IF NOT EXISTS idx_sie_shell_kind_active
    ON shell_identity_entries(shell_id, kind)
    WHERE is_deleted = 0 AND retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_shell_messages_to_unread ON shell_messages(to_shell_id, read_at);
CREATE INDEX IF NOT EXISTS idx_dr_filepath_role  ON dr_filepath(role);
CREATE INDEX IF NOT EXISTS idx_dr_filepath_lang  ON dr_filepath(lang);
CREATE INDEX IF NOT EXISTS idx_dr_dependency_mgr ON dr_dependency(manager);
