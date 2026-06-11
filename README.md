# super-coder

A **forkable shell substrate for a single code repository.** You install it into
a project repo; it brings the shell system — DB-backed identity, memory, seed/L&S,
decisions, flags, a roadmap, and spec/doc content — and runs that repo through
whatever coding harness you point at it — **Claude Code, OpenCode, Codex, and
Mistral Vibe**, all sandbox-integrated (or run on the no-docker host path).

The bet: **we build the data layer, we rent the harness.** The agent loop, the
tools, the model API are the harness's job. We own identity + memory + content
and render a boot artifact the harness reads natively.

This repo is also **dogfood**: super-coder maintains super-coder. Its own
`.super-coder/` engine manages the maintainer shell that builds it.

## Layout

```
.super-coder/         the engine — a gitignored, materialized DEPENDENCY in a
                      fork (see .super-coder/README.md); tracked only in this
                      source repo, where the engine IS the project
.sc-state/            fork-owned, tracked: content.sql (DB serialization / memory)
                      + engine.ref (the upstream SHA the engine is pinned at)
specs_sc/ docs_sc/    rendered from the DB, read-only (the _sc suffix = provenance)
skills_sc/ roadmap_sc.md
.claude/skills/       per-shell skills, rendered at boot — gitignored
CLAUDE.md / AGENTS.md boot artifact — gitignored, rebuilt at launch
```

A fork's git surfaces show **only its project** — the engine is a dependency,
not committed source, exactly like `node_modules/`. The one fork-owned artifact
that must survive is its DB, serialized to the tracked `.sc-state/content.sql`.

## Quick start

Drop super-coder into an existing git repo and boot a shell. Requires `docker`
(rootless is fine) and an account for one coding harness — Claude Code, OpenCode,
or Codex.

```bash
cd your-repo                                                  # an existing git repo

# 1. Pull in the engine + entry script (files only, no history merge):
git remote add super-coder https://github.com/jedbjorn/super-coder.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc

# 2. Bootstrap the fork — installs harness CLIs, builds the DB, seeds your first shell:
./sc install

# 3. Sign in to your harness once, on the HOST (not inside the sandbox):
claude                          # or:  opencode auth login  ·  codex login

# 4. Launch the sandbox (server + GUI) and attach a session:
./sc launch
./sc enter                      # auth + pick a shell + pick a harness + boot

# 5. Commit the install (engine is gitignored — only sc + .sc-state + config track):
git add -A && git commit -m "chore: install super-coder"
```

That's the happy path. Each step is covered in depth below — installer internals,
harness sign-in, the docker modes, and the localhost review GUI.

## Install into an existing repo

super-coder installs **alongside** your code — it renders to `_sc` dirs, so it
never collides with your repo's own `/docs`, `/specs`, or skills. A fork
inherits the **system** (schema + the skill catalogue + the render chain), never
super-coder's own memory or roadmap.

> Requirements: `docker` — the default run mode is a sandbox container, so the
> harness's "allow everything" is safe (the kernel is the boundary; the
> container sees only this repo + your harness creds). The image bakes the rest:
> `python3`, `sqlite3`, `git`, `curl`, and the harness CLIs (`claude` +
> `opencode` + `codex` + `vibe`, via their official native installers, no npm). `make` targets wrap
> the common commands; `./sc <cmd>` works without `make`. No docker? The
> `./sc serve` + `./sc boot` primitives run on the host with only `python3` +
> `sqlite3` (and a harness on `PATH`).

**Docker mode — rootless is the default.** `./sc doctor` checks your docker.
Both modes work (the launcher's `duser()` adapts), and **rootless is the chosen
default: zero setup, same function.** Under rootless the sandbox runs the
container as root, which maps to *you*, so repo writes come out owned by you —
no phantom-uid problem (verified). Its only wart: `claude` runs as root inside,
so its `--dangerously-skip-permissions` flag is blocked — the sandbox replaces
the need for it. **Rootful is optional**, purely to drop that wart (1:1
bind-mounts, harness runs as a normal user); it costs a one-time sudo + re-login.

**Setup is one-time per machine (and rootless needs none).** `./sc launch` only
checks the daemon is reachable and points you here if not — it never does setup.

- **Rootless (default) — nothing to do.** If rootless docker runs as your user,
  `./sc launch` works as-is.
- **Rootful (optional upgrade).** Needs sudo + a re-login (a new `docker` group
  only applies to a fresh session — which is exactly why it can't fold into
  `launch`):

  ```bash
  sudo usermod -aG docker $USER            # 1. join the docker group
  sudo systemctl enable --now docker.socket # 2. start the system daemon
  # 3. LOG OUT and back in (the group only applies to a new session)
  docker context use default                # 4. point the CLI at the system daemon
  systemctl --user disable --now docker.service  # 5. optional: stop rootless
  ./sc doctor                               # verify → "docker ✓ rootful"
  ```

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
git add -A && git commit -m "chore: install super-coder"   # engine is gitignored; commits sc + .sc-state + config
./sc launch                     # build + start the sandbox (server + GUI)
./sc enter                      # attach: auth + pick shell + pick harness + boot
```

`./sc install` does the rest: checks requirements, **installs the harness CLIs**
(`claude` + `opencode` + `codex` + `vibe`, via their official native installers — no
npm — if any are missing; `--skip-harness-install` to detect only), wires your `.gitignore`,
**makes the engine a gitignored dependency** (`git rm -r --cached .super-coder` —
files stay on disk; pins its upstream SHA in `.sc-state/engine.ref`), **strips
super-coder's own per-instance content** (a fork inherits the *system* — schema +
skill catalogue + render chain — never the memory or roadmap), builds the system
DB, seeds your fork's **first shell** (your user + a shell carrying the CC Lineage
Seed and its own genesis seed), and renders. So after install your git surfaces
show only your project — the engine no longer appears in `git status`. It refuses
to run in the super-coder source repo or on an already-installed fork (guarding
against content loss).

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

## Sign in to your harness (on the host)

The harnesses are just CLIs — `./sc install` (and `./sc update`, `./sc
ensure-harness`) install the binaries, but you authenticate each **once, on the
host**, with your own account/subscription:

```bash
claude                      # Claude Code — prompts to sign in on first run
opencode auth login         # OpenCode
codex login                 # Codex (OpenAI / ChatGPT account)
vibe --setup                # Mistral Vibe — stores the API key (or export MISTRAL_API_KEY)
```

`./sc launch` bind-mounts each harness's credential dir into the sandbox
(`~/.claude` + `~/.claude.json`, `~/.config/opencode` + `~/.local/share/opencode`,
`~/.codex`, `~/.vibe`), so host auth flows straight into the container — **you
never sign in inside the sandbox.** Authenticate on the host, then `./sc enter`.

> **Vibe creds.** `vibe --setup` stores your key under `~/.vibe`, which the
> sandbox now mounts — so Vibe works inside the container like the others. If you
> instead use the env-var path, `export MISTRAL_API_KEY` on the host before
> `./sc launch` and it's forwarded in (only when set). Re-run `./sc launch` after
> first authenticating, so the mount picks up `~/.vibe`.

> **⚠ Sign in on the host, not inside the sandbox.** OAuth logins spin up a
> localhost callback server (Codex uses `:1455`). Run the login on the **host** so
> your browser's callback reaches it. Logging in from *inside* the sandbox fails —
> that port isn't published, so the browser gets `ERR_CONNECTION_REFUSED`.

> **OpenCode is the exception.** Its `opencode auth login` for **API-key**
> providers is a paste-the-key prompt, not an OAuth callback, so it works at
> **either level** — run it on the host *or* from inside the container (`./sc
> enter`). Because `~/.config/opencode` + `~/.local/share/opencode` are bind-mounted
> read-write, a key entered on either side lands in the same `auth.json` and is
> available to both. (OAuth-based OpenCode providers still follow the host rule
> above.)

A note on Codex models: driven by a **ChatGPT account** (not an API key), Codex
exposes `gpt-5.5` and `gpt-5.4-mini` — the flavor defaults are set to those.
Plain API-only ids (e.g. `gpt-5.4`) return a 400 on a ChatGPT account.

## Harnesses, plans & model choice

### Prefer a subscription plan over a raw API key

Agentic coding burns **huge** token volume — multi-step loops, large context,
constant re-reads. Metered per-token API billing scales with every one of those
tokens and gets expensive fast. A flat **subscription plan** is generally far
cheaper *and* predictable for this workload, so we recommend running each harness
against its plan rather than its pay-as-you-go API:

| Harness | Provider | Recommended plan |
|---|---|---|
| **Claude Code** | Anthropic | [Claude Pro / Max](https://claude.com/pricing) |
| **Codex** | OpenAI | [ChatGPT Plus / Pro](https://openai.com/chatgpt/pricing/) |
| **Vibe** | Mistral | [Mistral plans](https://mistral.ai/pricing) |
| **OpenCode** → open-weights | Ollama | [Ollama Cloud (or run local, free)](https://ollama.com/) |

Codex exists for exactly this reason — a ChatGPT account bills **flat, with no
per-token metering**. OpenCode with a raw API key stays the **metered catch-all**:
reach for it when you need a model no plan covers, accepting per-token cost. Ollama
goes one further — open-weights models you can run **locally for free** on your own
hardware, or on Ollama Cloud's plan.

### Why each role defaults to the model it does

Every shell has a **flavor** (its role); each flavor ships an advisory model
default per harness (the `flavor_defaults` table — the picker pre-selects it;
`--harness` / `-m` / the picker override). The doctrine:

| Flavor | Job | Codex | Claude | OpenCode (open-weights) |
|---|---|---|---|---|
| **planner** | architecture, plans | `gpt-5.5` ★ | `opus` | `deepseek-v4-pro` |
| **reviewer** | adversarial review | `gpt-5.5` | `opus` ★ | `kimi-k2.6` |
| **dev** | write the code | `gpt-5.4-mini` ★ | `sonnet` | `qwen3-coder:480b` |
| **cartographer** | map the repo | `gpt-5.4-mini` ★ | `haiku` | `gpt-oss:20b` |
| **admin** | own the substrate, maintain `main` | `gpt-5.5` ★ | `sonnet` | `qwen3-coder:480b` |

★ = the harness the picker pre-selects for that flavor.

The logic, three rules:

- **Bookends premium, middle fast.** Planner and reviewer are *low-volume,
  high-leverage reasoning* — one good plan or one sharp review pays for the
  premium model. Dev and cartographer are *high-volume mechanical work* (bulk
  code, file mapping), where a fast, cheap, coding-tuned model wins on
  cost-per-token because the volume is highest.
- **The reviewer runs a different lineage than the code it reviews.** It defaults
  to **Claude (Opus)** so it isn't blind to the same mistakes a GPT model made
  authoring the code — adversarial *diversity*, not a second opinion from the same
  brain.
- **Three lineages, always.** Every flavor offers Codex (OpenAI), Claude
  (Anthropic), and OpenCode (open-weights via Ollama) — pick any provider for any
  role at launch.
- **Admin is the one shell on `main`.** Every other shell boots into an isolated
  git worktree (`.sc-worktrees/<shortname>/`, branch `shell/<shortname>`) and
  lands work via PRs; the admin shell boots in the **repo root** and maintains
  the default branch directly — engine updates, rollbacks, migrations, applying
  approved patches, fork-local skills. The branch-guard exempts it (and only
  it). Its decisions carry real risk (a wrong rollback is data loss), so it
  defaults premium on Codex.

> **Vibe sits outside this matrix.** Mistral Vibe takes no model from the launch
> seam — it selects its own via `active_model` in `~/.vibe/config.toml`
> (`vibe --setup`) or `VIBE_ACTIVE_MODEL`. It's a fourth harness option, not a
> fourth column here.

## Update a fork

Ship an improvement to super-coder, pull it into each fork — **in place**, with
no loss of memory. The shell updates its own substrate: it pulls the new engine,
applies new migrations under its own feet, and the next boot stands on the new
floor with every row intact. (The shell-facing version of this is the
`self_update` skill — same procedure, framed as the handoff it is.)

```bash
./sc update                     # fetch + materialize the engine, reconcile in place
git add -A && git commit -m "chore: update super-coder"   # commits only .sc-state/ + _sc
```

`./sc update` fetches the engine from the `super-coder` remote and
**materializes** it into the gitignored `.super-coder/` dir (the engine is a
dependency — code, schema, migrations, skills; your `.sc-state/`, DB, and
`instance.json` are never touched), **pins** the new upstream SHA in
`.sc-state/engine.ref` (keeping the prior one as `engine.ref.prev`), backs up the
live DB, **applies pending migrations in place** (never a rebuild-from-snapshot —
your unsnapshotted in-session writes survive), syncs the skills catalogue
(id-stable, so grants stay valid), re-grants any new common skills, refreshes the
repo map, and re-snapshots the live state. Nothing under `.super-coder/` is
committed — you commit only `.sc-state/` (refreshed `content.sql` + bumped
`engine.ref`) and any `_sc` renders. Then restart the session to boot onto the
new floor.

- `./sc update --no-fetch` reconciles against the current working tree (offline /
  dev) — engine + `engine.ref` unchanged. `--branch <name>` to track a non-`main`
  engine branch.
- Missing remote? `git remote add super-coder https://github.com/jedbjorn/super-coder.git`

### Roll back a bad update

```bash
./sc rollback                   # restore the DB + engine together, then reboot
```

`./sc rollback` is a **sound pair-restore**: because engine code is read live and
a migration exists *because new code expects the new schema*, it restores both —
it backs up the current DB first (rollback is itself reversible), restores the DB
from the most recent pre-update backup, and re-materializes the engine at
`.sc-state/engine.ref.prev`. Whole-restore, not a per-step schema reversal; the
only data lost is anything written between the update and the rollback.

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
./sc snapshot            # serialize per-instance tables → .sc-state/content.sql
./sc update              # fetch + materialize the engine, reconcile in place
./sc rollback            # sound undo of a bad update (restore DB + engine)
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

## Dev kit

Every sandbox bakes a **toolchain** — `rg`, `sqlite3`, `curl`, Node 22 / `npm`,
and a Playwright + Chromium browser for E2E — but deliberately **not** your
project's dependencies. Those you install per fork with `./sc deps`, which builds
a repo-root `.venv` from every `requirements*.txt` (your pins are authoritative)
and runs `npm ci`/`install` for each `package.json`. Because the install lands in
the **bind-mounted repo** rather than the image, it survives rebuilds — built in
the container, run in the container, persisted in the mount. Run it first in a
fresh sandbox; a "module not found" is almost always just deps-not-yet-installed.
On top of your deps it layers a small engine baseline — `pytest`, `httpx`,
`coverage`, `ruff`, `mypy`, `datasette` — with pip's `only-if-needed` strategy, so
it never overrides a fork's pin or its `[tool.ruff]` / `[tool.mypy]` config.
**Available, not enforced:** opt into whichever pieces you want, fork by fork.

```bash
./sc deps          # install fork deps into the bind-mount (.venv + npm) — run first
./sc test          # backend (.venv pytest / stdlib unittest) + UI (npm test / vitest)
./sc lint [paths]  # ruff check  (.venv/bin/ruff format to apply formatting)
./sc typecheck     # mypy
```

One boundary trips people up: **you work inside the sandbox container**, and the
app the FnB watches in their browser is a *separate*, host-supervised instance. To
see your own changes, start a dev server **inside** the container on
`0.0.0.0:$SC_DEV_PORT` — the launcher publishes it to `http://127.0.0.1:$SC_DEV_PORT`
on the host — and use `datasette <db.sqlite>` the same way to browse a SQLite DB in
a web GUI. Never restart the host stack from inside the sandbox; run your own
instance instead. (The boot doc's `RUNNING THE APP` section and the `dev_kit` skill
carry the full detail.)

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

## License

[MIT](LICENSE) © 2026 jedbjorn.
