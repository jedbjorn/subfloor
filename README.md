# super-coder

A **forkable shell substrate for a single code repository.** You install it into
a project repo; it brings the shell system — DB-backed identity, memory, seed/L&S,
decisions, flags, a roadmap, and spec/doc content — and runs that repo through
whatever coding harness you point at it (Claude Code today, OpenCode next).

The bet: **we build the data layer, we rent the harness.** The agent loop, the
tools, the model API are the harness's job. We own identity + memory + content
and render a boot artifact the harness reads natively.

This repo is also **dogfood**: super-coder maintains super-coder. Its own
`.super-coder/` engine manages the maintainer shell that builds it.

## Layout

```
.super-coder/         the engine (see .super-coder/README.md)
specs_sc/ docs_sc/    rendered from the DB, read-only (the _sc suffix = provenance)
skills_sc/ roadmap_sc.md
.claude/skills/       per-shell skills, rendered at boot — gitignored
CLAUDE.md / AGENTS.md boot artifact — gitignored, rebuilt at launch
```

## Install into an existing repo

super-coder installs **alongside** your code — it renders to `_sc` dirs, so it
never collides with your repo's own `/docs`, `/specs`, or skills. A fork
inherits the **system** (schema + the skill catalogue + the render chain), never
super-coder's own memory or roadmap.

> Requirements: `python3`, `sqlite3`, `make`, and your harness CLI (`claude` or
> `opencode`) on `PATH`.

```bash
cd your-repo                    # an existing git repo

# 1. Pull the engine + Makefile in via git (no history merge — just the files):
git remote add super-coder https://github.com/jedbjorn/super-coder.git
git fetch super-coder
git checkout super-coder/main -- .super-coder Makefile

# 2. A fork takes the SYSTEM, not super-coder's per-instance content — drop it:
rm .super-coder/snapshot/content.sql \
   .super-coder/assets/seed/super-coder-founding-spec.md
git remote remove super-coder   # optional; re-add when you want updates (below)

# 3. Add these lines to your .gitignore (the .db + boot artifact + skill render
#    are all rebuilt — never commit them):
cat >> .gitignore <<'EOF'

# super-coder — rebuilt from git-tracked text; never commit
/.super-coder/shell_db.db
/.super-coder/shell_db.db-wal
/.super-coder/shell_db.db-shm
/CLAUDE.md
/AGENTS.md
/.claude/skills/
EOF

# 4. Build the system DB (schema + skill-catalogue migration; no content yet):
make rebuild

# 5. Seed this fork's first shell — your user + a shell carrying the CC lineage
#    seed and its own genesis seed (interactive prompts, or pass flags):
make init
#   non-interactive: python3 .super-coder/scripts/init_fork.py \
#       --username Jed --name Dev --shortname dev --role "Dev shell" \
#       --mandate "Build and maintain this repo."

# 6. Serialize that shell to git-tracked text, then commit the install:
make snapshot
git add .super-coder Makefile .gitignore && git commit -m "chore: install super-coder"

# 7. Boot it through your harness:
make launch                     # username auth → pick shell → render boot → exec
```

After step 7 you're talking to the shell, working your repo. Author memory,
roadmap, and specs into the DB; `make snapshot` (+ `make render` for
docs/roadmap/skills) serializes back to the text git tracks.

> **What `make init` does** (the fork-identity step): provisions your local user
> and the fork's first shell, writes the canonical CC Lineage Seed into it, and
> plants the shell's own genesis seed. This is the minimal v1 bootstrap — a
> richer installer (requirements check, harness auto-detect, slot-filled
> system-prompt template) is the next phase.

## Update a fork

Ship an improvement to super-coder, pull it into each fork — the system
propagates via migrations; your per-instance content (the snapshot) is
untouched.

```bash
git fetch super-coder           # (re-add the remote first if you removed it)
git checkout super-coder/main -- \
    .super-coder/schema.sql .super-coder/migrations .super-coder/scripts \
    .super-coder/render .super-coder/templates .super-coder/assets/skills
make rebuild                    # re-applies your snapshot + any NEW system migrations
```

The migration ledger (`schema_migrations`) skips already-applied migrations, so
`rebuild` only lays down what's new. Your shells, memory, and roadmap come back
from your own `snapshot/content.sql`.

## Run (everyday)

```bash
make launch              # auth + pick a shell + render boot + exec the harness
make launch-<shortname>  # boot one shell directly, skip the picker
make rebuild             # rebuild .super-coder/shell_db.db from schema + migrations + snapshot
make render              # regenerate the tracked flat _sc files from the DB
make snapshot            # serialize per-instance tables → snapshot/content.sql
make verify              # rebuild + flat render + headless boot (no exec) — the proof
make help                # all targets
```

## Review layer (localhost GUI)

A zero-dependency localhost GUI to review the substrate — shells, roadmap,
flags. One stdlib Python server serves both the JSON API and a static UI; no
venv, no npm, no build step.

```bash
make up        # start it (pm2) on this fork's derived port
make health    # curl /api/health
make down      # stop it
make serve     # run it in the foreground (no pm2)
make ports     # show this fork's derived port
```

**Ports are derived per repo**, never fixed — because a fork runs *inside* a
host repo that may have its own dev server, and several forks can run at once.
Each fork hashes its path to a stable port in the `88xx` band (clear of superCC
8000 / dos-arch 8001 and common host ports), persisted to a gitignored
`.super-coder/instance.json` you can hand-edit. Two forks won't collide.

What you can do in the GUI: read everything; edit a shell's operational fields
(`current_state`, `connections`, `workspace`) and skill grants; edit the roadmap
and **non-frozen** documents; create and resolve flags. **seed and L&S are
read-only** — the laws say the shell curates them, so the API ships no endpoint
to write them at all. A `snapshot ⤓` button re-serializes + renders after edits
(the manual precursor to the B6 commit→PR automation).

The live `.super-coder/shell_db.db` is **gitignored and rebuilt** from
git-tracked text. See `.super-coder/README.md` for the full model.

> Spec: the founding design lives in the roadmap (`super-coder` feature row) and
> renders to `specs_sc/`. Build plan + log: tracked in superCC
> `shared/super-coder-impl-plan.md` during bring-up.
