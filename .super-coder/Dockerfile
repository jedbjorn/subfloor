# super-coder sandbox image — the *environment*, not the code.
#
# The repo is bind-mounted at runtime (see `./sc launch`); this image carries
# what the harness needs to run: python3 + sqlite3, git/curl/ripgrep, gh, and the
# harness CLIs (claude / opencode / codex / vibe) baked in via their official
# native installers (the same ones install.py uses — keep the URLs in sync with
# scripts/install.py HARNESS_INSTALL).
# It also carries the dev-kit TOOLCHAIN super-coder develops against — node LTS for
# Svelte/Vite, plus a baked Playwright+Chromium browser for E2E tests — but NOT the
# fork's app deps: those install per-fork into the bind-mounted repo's own .venv /
# node_modules via `./sc deps`, so versions track the fork's pins, not the image.
#
# Why bake the binaries but mount the creds: claude installs as a symlink into
# ~/.local/share/claude/versions/<v> (awkward to bind-mount); opencode + codex
# are fat self-contained binaries. Baking them sidesteps the symlink dance.
# Auth/config (~/.claude, ~/.claude.json, ~/.config/opencode,
# ~/.local/share/opencode, ~/.codex) is mounted rw from the host so the container
# reuses the login you already did — no in-container `auth login`.
#
# The container runs as your uid (compose `user:`) with HOME=/home/<user> equal
# to the host, so paths match host==container and Path.home() finds everything.
FROM python:3.12-slim

ARG SC_USER=j3d1
ARG SC_UID=1000
ARG SC_GID=1000

# sqlite3 CLI + git + curl + bash + ca-certs (installers pipe-to-bash over https).
# ripgrep (rg) — the fast code search shells reach for when working a repo; the
# mapping flow and a shell grepping its tree both expect `rg` on PATH.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sqlite3 git curl bash ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh) — the in-container push/PR path: the GUI "publish" flow
# (snapshot → render → commit → push → PR) and shells that open their own PRs.
# Not in Debian's default repos, so add the official gh apt source. Auth is NOT
# baked: the token arrives at runtime via GH_TOKEN (`./sc launch` forwards the
# host's `gh auth token`, or a repo-scoped SC_GH_TOKEN) — never in the image.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Node.js LTS — a *project* runtime, not a harness dep. Forked repos increasingly
# ship a JS/TS dev surface (vite/SvelteKit/etc); without node baked in, a shell
# can run `npm` only by installing into the ephemeral container FS, which
# evaporates on the next rebuild. Baked here it persists like python3. The
# "no npm" note on the harness install below still holds — that's about how
# claude/opencode get installed, not a ban on node for project work. NodeSource's
# apt repo pins a clean LTS major (same pipe-to-bash idiom as the installers).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Playwright E2E browser — baked at the IMAGE level on purpose, unlike the per-fork
# app deps (those land in the fork's .venv / node_modules via `./sc deps`). It stays
# baked because: (1) Chromium needs a pile of OS libraries (`--with-deps` shells out
# to apt), which only root can install and which don't belong in a venv; (2) the E2E
# runner drives the *running app over HTTP*, not via the app's venv, so it must be
# present whichever fork is mounted. Install browsers to a world-readable shared path
# so the unprivileged ${SC_USER} (the container's real uid) finds them — the default
# /root/.cache/ms-playwright is unreadable to that user. PLAYWRIGHT_BROWSERS_PATH is
# inherited by the running container, so a fork's own playwright / @playwright/test
# runner (in its .venv / node_modules) resolves the SAME baked browser. Pin the
# version: a runner only drives the browser build it shipped with, so an unpinned
# client + an older cached browser silently mismatch — keep this >= any fork's pin.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir "playwright==1.60.0" \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright

# Dev-kit fallback tools. `./sc lint` / `./sc typecheck` prefer the fork's .venv
# copy (its pins + config), but a host-managed .venv (pinned out-of-tree
# interpreter mounted by launch) is pip-skipped in the sandbox — so bake the
# PATH fallback here or the tools are unobtainable from inside the box
# (dos-arch QAQC-02). _sc_devtool in ./sc resolves .venv → PATH in that order.
RUN pip install --no-cache-dir ruff mypy

# A user matching the host uid/gid so bind-mounted files aren't root-owned.
# A host GID may already exist in the base image — notably macOS's primary GID 20
# (`staff`) collides with Debian's pre-existing `dialout` group — so creating it
# unconditionally fails the build there. Reuse an existing GID; only create when
# absent. The group's *name* is irrelevant; only the numeric GID must match the
# host for bind-mounted ownership to line up.
RUN if ! getent group ${SC_GID} >/dev/null; then groupadd -g ${SC_GID} ${SC_USER}; fi \
 && useradd -m -u ${SC_UID} -g ${SC_GID} -s /bin/bash ${SC_USER}

USER ${SC_USER}
ENV HOME=/home/${SC_USER}
# The dirs the official installers drop binaries into — on PATH so execvpe and
# run.py's ensure_harness_path() agree (it derives the same dirs from Path.home).
ENV PATH=/home/${SC_USER}/.local/bin:/home/${SC_USER}/.opencode/bin:${PATH}

# Harness CLIs — official native installers, no npm (mirrors install.py).
# codex drops into ~/.local/bin (already on PATH above), same as claude. vibe's
# installer self-bootstraps uv (astral.sh) then `uv tool install`s into ~/.local/bin.
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && curl -fsSL https://opencode.ai/install | bash \
 && curl -fsSL https://chatgpt.com/codex/install.sh | sh \
 && curl -LsSf https://mistral.ai/vibe/install.sh | bash

# Safe in-container git auth — structurally prevents a token in a URL. The
# sandbox has no ssh, so `git@github.com:` remotes (e.g. a fork's default
# origin) fail and tempt a `https://x-access-token:$TOK@github.com/...` fallback
# that leaks the token to logs / `push -u` upstream output. Two configs close
# that: (1) rewrite GitHub SSH remotes to HTTPS so plain `git push origin`
# works; (2) hand credentials to git via `gh`, which serves them from the
# runtime GH_TOKEN on demand — never on the command line, never in a URL. With
# this, `git push origin <branch>` and `gh pr create` need zero manual token
# handling; absent a token they fail closed (no prompt-loop, no leak).
RUN git config --global url."https://github.com/".insteadOf "git@github.com:" \
 && git config --global credential."https://github.com".helper "!gh auth git-credential"

WORKDIR /home/${SC_USER}/super-coder

# Foreground stdlib server (api + static UI on one port). compose overrides the
# port; SC_BIND=0.0.0.0 (set in compose) lets docker publish it.
CMD ["./sc", "serve"]
