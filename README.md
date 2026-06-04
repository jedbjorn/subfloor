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

# 2. Bootstrap the fork — one command:
make install

# 3. Commit the install, then boot:
git add -A && git commit -m "chore: install super-coder"
make launch                     # starts the review GUI + boots your shell
```

`make install` does the rest: checks requirements, detects your harness
(`claude` / `opencode`), wires your `.gitignore`, **strips super-coder's own
per-instance content** (a fork inherits the *system* — schema + skill catalogue
+ render chain — never the memory or roadmap), builds the system DB, seeds your
fork's **first shell** (your user + a shell carrying the CC Lineage Seed and its
own genesis seed), and renders. It refuses to run in the super-coder source repo
or on an already-installed fork (guarding against content loss).

Interactive by default (prompts for the shell's name/role/mandate); pass flags
to script it:

```bash
python3 .super-coder/scripts/install.py \
    --username Jed --name Dev --shortname dev \
    --role "Dev shell" --mandate "Build and maintain this repo."
```

After `make launch` you're talking to the shell, working your repo. Author
memory, roadmap, and specs into the DB; `make snapshot` (+ `make render`)
serializes back to the text git tracks.

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
(linear status buckets, with toggle-filters) and **non-frozen** documents; create
and resolve flags. **seed and L&S are read-only** — the laws say the shell
curates them, so the API ships no endpoint to write them at all. A `snapshot ⤓`
button re-serializes + renders after edits (the manual precursor to the B6
commit→PR automation).

The **Scripts** tab lists the maintenance scripts (snapshot, render, seed-skills,
migrate, rebuild) — each with a description and a **run** button, so the common
chores work from the GUI without dropping to a terminal (rebuild prompts first,
since it discards un-snapshotted DB edits).

**`make launch` brings the GUI up too** — it starts the review layer (if not
already running) and prints its URL *before* handing off to the harness, so the
shell and the GUI run side by side from one command.

The live `.super-coder/shell_db.db` is **gitignored and rebuilt** from
git-tracked text. See `.super-coder/README.md` for the full model.

> Spec: the founding design lives in the roadmap (`super-coder` feature row) and
> renders to `specs_sc/`. Build plan + log: tracked in superCC
> `shared/super-coder-impl-plan.md` during bring-up.
