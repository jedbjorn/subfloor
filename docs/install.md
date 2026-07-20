---
title: super-coder — Install
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: Prerequisites, install & launch, installer internals, harness sign-in
---

# Install super-coder

## Quick start

> [!class2]
> **UI** Shells (your landing tab) · **Shells** your starting team — planner · 2×dev · reviewer · admin · cartographer

### Preparation

One-time host setup — get this right and the rest is `./sc install`. super-coder
runs the harness in a **docker sandbox**; the installer bootstraps everything
else. The host needs a container engine, a few base tools, and one signed-in
coding harness.

| Need | Arch Linux | macOS |
|---|---|---|
| **Container engine** | `sudo pacman -S docker`, then start a daemon — rootless default: `dockerd-rootless-setuptool.sh install && systemctl --user enable --now docker` | `brew install colima docker && colima start` (or Docker Desktop) |
| **Base tools** | `sudo pacman -S git curl python sqlite` (usually already present) | `xcode-select --install` (git/curl); python3 + sqlite3 ship with macOS |
| **Harness CLI** | installed for you by `./sc install` (`claude` · `opencode` · `codex` · `vibe` · `kimi`, native installers). Repair by hand: `curl -fsSL https://claude.ai/install.sh \| bash` | same — **and put `~/.local/bin` on your PATH** (a fresh macOS shell omits it) |
| **Harness account** | a plan for one of Claude Code · OpenCode · Codex · Vibe · Kimi Code; sign in once on the host (step 3) | same |

> [!class4]
> **The bar: a reachable docker daemon + a harness CLI on PATH.** `./sc doctor` reports the docker mode it finds (rootless / rootful) and the exact next command; `python3` + `sqlite3` are the only *hard* requirements (the engine runtime). **macOS PATH gotcha:** if `claude` reports *"missing or broken — run claude install to repair"*, the CLI installed fine but `~/.local/bin` isn't on your PATH. Add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile (`~/.zshrc`), open a new shell, then `claude install`. No docker at all? The `./sc serve` + `./sc boot` escape hatch runs the shell on the host.

### Install & launch

With the prerequisites in place, drop super-coder into an existing git repo and
boot a shell:

```bash
cd your-repo                                                  # an existing git repo

# 1. Pull in the engine + entry script (files only, no history merge):
git remote add super-coder https://github.com/jedbjorn/super-coder.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc

# 2. Bootstrap the fork — installs harness CLIs, builds the DB, seeds your starting team:
./sc install

# 3. Sign in to your harness once, on the HOST (not inside the sandbox):
claude                          # or:  opencode auth login  ·  codex login  ·  vibe --setup  ·  kimi login

# 4. Launch the sandbox (server + GUI) and attach a session:
./sc launch
./sc enter                      # auth + pick a shell + pick a harness + boot

# 5. Commit the install (engine is gitignored — only sc + .sc-state + config track):
git add -A && git commit -m "chore: install super-coder"
```

That's the happy path. Each step is covered in depth below — installer internals,
harness sign-in, the docker modes, and the localhost review GUI. For the full
arc from a fresh repo through ship-and-loop, see [*The loop*](the-loop.md).

## Install

> [!class2]
> **UI** Shells · Scripts · **Shells** seeds the starting team — planner · 2×dev · reviewer · admin · cartographer

super-coder installs **alongside** your code — it renders to `_sc` dirs, so it
never collides with your repo's own `/docs`, `/specs`, or skills. A fork
inherits the **system** (schema + the skill catalogue + the render chain), never
super-coder's own memory or roadmap.

> [!class4]
> **Requirements: `docker`.** The default run mode is a sandbox container, so the harness's "allow everything" is safe — the kernel is the boundary, and the container sees only this repo + your harness creds. The image bakes the rest: `python3`, `sqlite3`, `git`, `curl`, and the four harness CLIs. No docker? The `./sc serve` + `./sc boot` primitives run on the host with only `python3` + `sqlite3` (and a harness on `PATH`).

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

The commands are the five steps in the Quick start above — pull the engine in
via git (no history merge; super-coder never touches your repo's own
`Makefile`), `./sc install`, sign in, launch, commit.

`./sc install` does the rest: checks requirements, **installs the harness CLIs**
(`claude` + `opencode` + `codex` + `vibe` + `kimi`, via their official native installers — no
npm — if any are missing; `--skip-harness-install` to detect only), wires your `.gitignore`,
**makes the engine a gitignored dependency** (`git rm -r --cached .super-coder` —
files stay on disk; pins its upstream SHA in `.sc-state/engine.ref`), **strips
super-coder's own per-instance content** (a fork inherits the *system* — schema +
skill catalogue + render chain — never the memory or roadmap), builds the system
DB, seeds your fork's **starting team** (your user + a planner-flavor *primary*
carrying the CC Lineage Seed and its own genesis seed, plus two `dev`, a
`reviewer`, the `admin` that owns `main`, and the singleton **Cartographer**
repo-map owner), and renders. So after install
your git surfaces show only your project — the engine no longer appears in
`git status`. It refuses to run in the super-coder source repo or on an
already-installed fork (guarding against content loss).

Interactive by default (prompts for your **primary** shell's name/role/mandate —
the rest of the team is auto-named); pass flags to script it. `--flavor` picks
which roster slot is your primary (default `planner`):

```bash
python3 .super-coder/scripts/install.py \
    --username Jed --name Lead --shortname lead \
    --role "Planning lead" --mandate "Scope and steer the work in this repo."
```

After `./sc enter` you're talking to the shell, working your repo. Author
memory, roadmap, and specs into the DB; `./sc snapshot` (+ `./sc render`)
serializes back to the text git tracks.

## Harness sign-in

> [!class2]
> **UI** — host auth, no GUI · **Shells** any (the harness is a per-launch pick)

The harnesses are just CLIs — `./sc install` (and `./sc update`, `./sc
ensure-harness`) install the binaries, but you authenticate each **once, on the
host**, with your own account/subscription:

```bash
claude                      # Claude Code — prompts to sign in on first run
opencode auth login         # OpenCode
codex login                 # Codex (OpenAI / ChatGPT account)
vibe --setup                # Mistral Vibe — stores the API key (or export MISTRAL_API_KEY)
kimi login                  # Kimi Code — device-code OAuth against your Kimi membership
```

`./sc launch` bind-mounts each harness's credential dir into the sandbox
(`~/.claude` + `~/.claude.json`, `~/.config/opencode` + `~/.local/share/opencode`,
`~/.codex`, `~/.vibe`, `~/.kimi-code`), so host auth flows straight into the
container — **you never sign in inside the sandbox.** Authenticate on the host,
then `./sc enter`.

> [!class4]
> **Sign in on the host, not inside the sandbox.** OAuth logins spin up a localhost callback server (Codex uses `:1455`). Run the login on the **host** so your browser's callback reaches it — from *inside* the sandbox that port isn't published, so the browser gets `ERR_CONNECTION_REFUSED`.

> [!class2]
> **Vibe creds.** `vibe --setup` stores your key under `~/.vibe`, which the sandbox now mounts — so Vibe works inside the container like the others. Prefer the env-var path? `export MISTRAL_API_KEY` on the host before `./sc launch` and it's forwarded in (only when set). Re-run `./sc launch` after first authenticating, so the mount picks up `~/.vibe`.

> [!class2]
> **Kimi creds.** `kimi login` (device-code OAuth) stores its state under `~/.kimi-code`, which the sandbox mounts — host auth flows in. Note kimi does **not** read keys from shell env vars (`export KIMI_API_KEY=…` does nothing); provider keys live in `~/.kimi-code/config.toml`. Re-run `./sc launch` after first authenticating, so the mount picks it up.

> [!class2]
> **OpenCode is the exception.** Its `opencode auth login` for **API-key** providers is a paste-the-key prompt, not an OAuth callback, so it works at **either level** — host or inside the container (`./sc enter`). Because `~/.config/opencode` + `~/.local/share/opencode` are bind-mounted read-write, a key entered on either side lands in the same `auth.json`. (OAuth-based OpenCode providers still follow the host rule above.)

A note on Codex models: driven by a **ChatGPT account** (not an API key), Codex
exposes the `gpt-5.6` line (`gpt-5.6-sol`, `gpt-5.6-terra`) and `gpt-5.5` — the
flavor defaults are set from those. Plain API-only ids return a 400 on a
ChatGPT account.
