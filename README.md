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

> Requirements: `docker` — the default run mode is a sandbox container, so the
> harness's "allow everything" is safe (the kernel is the boundary; the
> container sees only this repo + your harness creds). The image bakes the rest:
> `python3`, `sqlite3`, `git`, `curl`, and the harness CLIs (`claude` +
> `opencode`, via their official native installers, no npm). `make` targets wrap
> the common commands; `./sc <cmd>` works without `make`. No docker? The
> `./sc serve` + `./sc boot` primitives run on the host with only `python3` +
> `sqlite3` (and a harness on `PATH`).

**Docker mode — rootful vs rootless.** `./sc doctor` checks your docker and
prints setup guidance. Both work (the launcher's `duser()` adapts), but
**rootful is the smoother default**: bind-mounts map 1:1 to your uid and the
harness runs as a normal user. Under rootless the sandbox runs the container as
root (which maps to you), which works but makes `claude` run as root inside
(its `--dangerously-skip-permissions` flag is blocked — the sandbox replaces the
need for it). `./sc install` runs this preflight for you.

```bash
cd your-repo                    # an existing git repo

# 1. Pull the engine + entry script in via git (no history merge — just the
#    files; super-coder never touches your repo's own Makefile):
git remote add super-coder https://github.com/jedbjorn/super-coder.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc

# 2. Bootstrap the fork — one command:
./sc install

# 3. Commit the install, then boot:
git add -A && git commit -m "chore: install super-coder"
./sc launch                     # build + start the sandbox (server + GUI)
./sc enter                      # attach: auth + pick shell + pick harness + boot
```

`./sc install` does the rest: checks requirements, **installs the harness CLIs**
(`claude` + `opencode`, via their official native installers — no npm — if either
is missing; `--skip-harness-install` to detect only), wires your `.gitignore`,
**strips super-coder's own
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

After `./sc enter` you're talking to the shell, working your repo. Author
memory, roadmap, and specs into the DB; `./sc snapshot` (+ `./sc render`)
serializes back to the text git tracks.

## Update a fork

Ship an improvement to super-coder, pull it into each fork — **in place**, with
no loss of memory. The shell updates its own substrate: it pulls the new engine,
applies new migrations under its own feet, and the next boot stands on the new
floor with every row intact. (The shell-facing version of this is the
`self_update` skill — same procedure, framed as the handoff it is.)

```bash
./sc update                     # self-fetch + reconcile in place
git add -A && git commit -m "chore: update super-coder"
```

`./sc update` self-fetches the engine from the `super-coder` remote (one
canonical path list — code, schema, migrations, skills; your `snapshot/`, DB,
and `instance.json` are never touched), backs up the live DB, **applies pending
migrations in place** (never a rebuild-from-snapshot — your unsnapshotted
in-session writes survive), syncs the skills catalogue (id-stable, so grants
stay valid), re-grants any new common skills, refreshes the repo map, and
re-snapshots the live state. Then restart the session to boot onto the new floor.

- `./sc update --no-fetch` reconciles against an already-checked-out engine
  (offline / dev). `--branch <name>` to track a non-`main` engine branch.
- Missing remote? `git remote add super-coder https://github.com/jedbjorn/super-coder.git`

**The contract:** every schema change *after* a fork exists ships as a
`migrations/NNNN_*.sql` file, never an edit to `schema.sql` — the migration
ledger is what carries a delta across to an existing fork. Additive where you
can make it.

## Run (everyday)

```bash
./sc launch              # build + start the sandbox container (server + GUI), 127.0.0.1 only
./sc enter               # attach a session: auth + pick a shell + pick a harness + boot
./sc enter-<shortname>   # attach + boot one shell directly, skip the shell picker
./sc down                # stop + remove the sandbox container
./sc logs                # tail the sandbox server logs
./sc rebuild             # rebuild .super-coder/shell_db.db from schema + migrations + snapshot
./sc render              # regenerate the tracked flat _sc files from the DB
./sc snapshot            # serialize per-instance tables → snapshot/content.sql
./sc verify              # rebuild + flat render + headless boot (no exec) — the proof
./sc help                # all commands
```

**Choosing a harness.** The boot artifact is dual-written every launch
(`CLAUDE.md` for Claude Code, `AGENTS.md` for the rest), so any installed harness
can boot the same shell. At launch, after you pick a shell: `--harness <name>` or
`HARNESS=<name>` forces one; otherwise, when more than one harness is on `PATH`,
you're prompted (default = your fork's `instance.json` harness). The pick is
per-launch and never written back — so two terminals can run the **same** shell on
different harnesses at once (one Claude Code, one OpenCode). A fork with a single
harness on `PATH` skips the prompt.

**`make`.** The source repo ships a thin root `Makefile` that delegates to
`./sc` (`make launch` / `make enter` / `make enter s=cc` / `make down`). It is
source-repo ergonomics only — `install.py` checks out `.super-coder` + `sc`, not
the `Makefile`, so it never propagates to a fork or clobbers a fork's own
`Makefile`. In a fork, use `./sc <cmd>` (or alias it in your own `Makefile`).

## Review layer (localhost GUI)

A zero-dependency localhost GUI to review the substrate — shells, roadmap,
flags. One stdlib Python server serves both the JSON API and a static UI; no
venv, no npm, no build step.

The server runs **inside the sandbox container** as its foreground process, so
`./sc launch` brings it up and `./sc down` stops it. The port publishes to
`127.0.0.1` only.

```bash
./sc launch    # start the sandbox — server + GUI come up on this fork's derived port
./sc health    # curl /api/health
./sc down      # stop the sandbox (and the server with it)
./sc logs      # tail the server logs
./sc serve     # run the server in the foreground on the host (no docker)
./sc ports     # show this fork's derived port
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

**One container, two access paths** — `./sc launch` starts the server (GUI) as
the container's foreground process and prints its URL; `./sc enter` then attaches
an interactive harness session into that same container via `docker exec`, so the
shell and the GUI run side by side, sharing the one bind-mounted repo + creds.

The live `.super-coder/shell_db.db` is **gitignored and rebuilt** from
git-tracked text. See `.super-coder/README.md` for the full model.

> Spec: the founding design lives in the roadmap (`super-coder` feature row) and
> renders to `specs_sc/`. Build plan + log: tracked in superCC
> `shared/super-coder-impl-plan.md` during bring-up.
