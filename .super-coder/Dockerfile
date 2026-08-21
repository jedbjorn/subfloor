# super-coder sandbox image — the *environment*, not the code.
#
# The repo is bind-mounted at runtime (see `./sc launch`); this image carries
# what the harness needs to run: python3 + sqlite3, git/curl/ripgrep, gh, and the
# harness CLIs (claude / opencode / codex / vibe / kimi) baked in via their official
# native installers (the same ones install.py uses — keep the URLs in sync with
# scripts/install.py HARNESS_INSTALL).
# It also carries the dev-kit TOOLCHAIN super-coder develops against — node LTS for
# Svelte/Vite, plus a baked Playwright+Chromium browser for E2E tests — but NOT the
# fork's app deps: those install per-fork into the bind-mounted repo's own .venv /
# node_modules via `./sc deps`, so versions track the fork's pins, not the image.
#
# Why bake the binaries but mount the creds: claude installs as a symlink into
# ~/.local/share/claude/versions/<v> (awkward to bind-mount); opencode is a fat
# self-contained binary; Codex's standalone package lives under ~/.codex and
# must be relocated after install because that state home is mounted at runtime.
# Baking them sidesteps host ABI differences and the symlink dance.
# Auth/config (~/.claude, ~/.claude.json, ~/.config/opencode,
# ~/.local/share/opencode, ~/.codex, ~/.vibe, ~/.kimi-code) is mounted rw from
# the host so the container reuses the login you already did — no in-container
# `auth login`.
#
# The container runs as your uid (compose `user:`) with HOME=/home/<user> equal
# to the host, so paths match host==container and Path.home() finds everything.
ARG SC_PARENT_IMAGE=python:3.12-slim
FROM ${SC_PARENT_IMAGE}

ARG SC_USER=j3d1
ARG SC_UID=1000
ARG SC_GID=1000
ARG SC_GITHUB_HOST_TRUST_B64
ARG SC_GITHUB_HOST_TRUST_SHA256

# sqlite3 CLI + git + curl + bash + ca-certs (installers pipe-to-bash over https).
# openssh-client is transport-only: runtime may forward a validated agent socket,
# but no private key or host ~/.ssh tree is copied into the image.
# ripgrep (rg) — the fast code search shells reach for when working a repo; the
# mapping flow and a shell grepping its tree both expect `rg` on PATH.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sqlite3 git curl bash ca-certificates ripgrep openssh-client \
 && rm -rf /var/lib/apt/lists/*

# GitHub host trust is image-owned and pinned to GitHub's published host keys.
# The build context is deliberately empty. sandbox_devkit.py supplies the
# tracked assets/github_known_hosts bytes and digest as build arguments. A key
# rotation ships as a reviewed source change + rebuilt image; --no-build
# intentionally retains the keys from its selected image.
# UserKnownHostsFile=/dev/null prevents an ad-hoc per-user acceptance from
# widening this host-specific policy. Strict checking never prompts or falls
# back to trust-on-first-use.
RUN install -d -m 0755 /etc/ssh/ssh_config.d \
 && test -n "${SC_GITHUB_HOST_TRUST_B64}" \
 && test -n "${SC_GITHUB_HOST_TRUST_SHA256}" \
 && printf '%s' "${SC_GITHUB_HOST_TRUST_B64}" | base64 -d > /etc/ssh/ssh_known_hosts \
 && printf '%s  %s\n' "${SC_GITHUB_HOST_TRUST_SHA256}" /etc/ssh/ssh_known_hosts | sha256sum -c - \
 && chmod 0644 /etc/ssh/ssh_known_hosts \
 && printf '%s\n' \
      'Host github.com' \
      '    StrictHostKeyChecking yes' \
      '    GlobalKnownHostsFile /etc/ssh/ssh_known_hosts' \
      '    UserKnownHostsFile /dev/null' \
      > /etc/ssh/ssh_config.d/99-super-coder-github.conf \
 && chmod 0644 /etc/ssh/ssh_config.d/99-super-coder-github.conf

LABEL sc.github_host_trust_sha256="${SC_GITHUB_HOST_TRUST_SHA256}"

# websockets 16.1.1 (BSD-3-Clause) — the sans-io WS layer for the one-port
# HTTP+WS transport (api/transport.py; WS upgrades currently answer
# unavailable). Baked into the IMAGE python
# because the engine server runs on the image's interpreter, not the fork's
# .venv.
RUN pip install --no-cache-dir "websockets==16.1.1"

# Python project tooling — generic sandbox mechanisms, not fork dependency
# policy. A fork's declared dev-kit hook and project environment still own its
# exact test stack. Pin both tools so rebuilding after an engine update restores
# a deterministic baseline instead of inheriting whatever the host happens to
# provide.
RUN pip install --no-cache-dir "uv==0.11.26" "pytest==9.1.1" \
 && uv --version \
 && python -m pytest --version

# GitHub CLI (gh) — shells open their own feature PRs from the sandbox.
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

# DeepSeek Harness has a Python 3.10+ floor. Its lifecycle carrier is built from
# one digest-pinned upstream archive plus the reviewed narrow patch, then kept in
# a dedicated venv. The recipe itself pins the build Node and pnpm versions; the
# image's Node is only the bootstrap for that isolated tool invocation. Unsupported
# architectures still receive the tested marker instead of a partial carrier.
ENV SC_DEEPSEEK_CARRIER_PYTHON=/opt/super-coder/deepseek-runtime/bin/python
COPY .super-coder/scripts/deepseek_runtime.py /opt/super-coder/deepseek-bootstrap/scripts/deepseek_runtime.py
COPY .super-coder/scripts/build_deepseek_carrier.py /opt/super-coder/deepseek-bootstrap/scripts/build_deepseek_carrier.py
COPY .super-coder/scripts/deepseek_carrier_worker.py /opt/super-coder/deepseek-bootstrap/scripts/deepseek_carrier_worker.py
COPY .super-coder/scripts/deepseek_inference_canary.py /opt/super-coder/deepseek-bootstrap/scripts/deepseek_inference_canary.py
COPY .super-coder/scripts/cli_entry.py /opt/super-coder/deepseek-bootstrap/scripts/cli_entry.py
COPY .super-coder/assets/deepseek /opt/super-coder/deepseek-bootstrap/assets/deepseek
RUN python /opt/super-coder/deepseek-bootstrap/scripts/deepseek_runtime.py \
      --install-container-carrier /opt/super-coder/deepseek-runtime "$(uname -m)" \
      --json

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

# Durable bare `sc` on PATH for EVERY harness. run.py prepends the repo root to
# PATH at exec, but that only survives into a harness that hands its env verbatim
# to each command shell: claude's Bash tool does, so bare `sc` works there; codex
# spawns a fresh login-ish shell per call that drops the inherited PATH, so bare
# `sc` from a worktree hit `sc: command not found` (dos-arch #291, #292). A real
# file on PATH fixes it independently of inherited env. It resolves the checkout
# from the CALLER's cwd via git — never from $0 (a symlink would misresolve) and
# never from env — then exec's that checkout's own tracked ./sc, which finds the
# engine at the main worktree root and stays in cwd. /usr/local/bin is on PATH for
# every shell; run.py's REPO_ROOT prepend still shadows this for claude (same wrapper).
RUN printf '%s\n' \
      '#!/bin/sh' \
      '# Durable bare `sc` (baked in the sandbox image — see Dockerfile). Resolves the' \
      '# checkout from the caller cwd via git and execs its tracked ./sc. Never uses' \
      '# $0 or inherited env, so it survives whatever shell a harness spawns.' \
      'top=$(git rev-parse --show-toplevel 2>/dev/null) || {' \
      '  echo "sc: current directory is not inside a Git checkout; use ./sc from a subfloor checkout" >&2' \
      '  exit 127' \
      '}' \
      'git -C "$top" ls-files --error-unmatch -- sc >/dev/null 2>&1 || {' \
      '  echo "sc: Git checkout has no tracked sc launcher: $top/sc; use ./sc after installing subfloor in this checkout" >&2' \
      '  exit 127' \
      '}' \
      '[ -x "$top/sc" ] || {' \
      '  echo "sc: tracked launcher is not executable: $top/sc; repair it, then use ./sc" >&2' \
      '  exit 127' \
      '}' \
      'exec "$top/sc" "$@"' \
      'exit 127' \
      > /usr/local/bin/sc \
 && chmod 0755 /usr/local/bin/sc

# ── Harness freshness seam ───────────────────────────────────────────────────
# The harness CLIs are BAKED (here + below), never mounted from the host: their
# binaries are host-ABI artifacts, not portable config. A foreign-ABI binary in
# a Linux container is fatal (see kimi's note below), vibe's entry point is a
# script with an absolute shebang into a host uv interpreter, and glibc baselines
# differ across the distros we support. Auth travels; binaries do not.
#
# Baking alone froze them, though: docker serves the installer RUNs from layer
# cache forever, so `./sc launch|restart` — which DO build — could never make a
# harness newer, and a new model (Opus 5, in claude 2.1.219) stayed invisible to
# every shell. This ARG is the expiry. Rolling it invalidates the two harness
# layers and nothing above them (node/playwright/pip stay cached); it is
# REFERENCED inside each RUN so the bust is guaranteed, not merely implied by
# docker's arg-cache heuristics.
#
# Who rolls it: every normal `./sc restart`, plus `./sc update` (dos-u),
# `./sc update-harnesses`, and `./sc build --harnesses` — see sc's
# harness_epoch()/harness_epoch_roll(). Each explicit refresh gets a unique UTC
# token; routine launch/plain build reuse the stored value and stay cache-warm.
# `restart --no-build` deliberately reuses the existing image.
ARG SC_HARNESS_EPOCH=0

# Kimi Code — baked HERE (root stage) with its binary DELIBERATELY redirected to
# /usr/local/bin (root-writable, on every PATH), unlike the $HOME installs below.
# Its default home, ~/.kimi-code, holds bin/ AND config in one dir, and
# `./sc launch` bind-mounts the host's ~/.kimi-code for creds — installing there
# would let the mount shadow the baked binary with an incompatible host binary.
# KIMI_NO_MODIFY_PATH skips its shell-rc edit.
RUN echo "harness epoch: ${SC_HARNESS_EPOCH}" \
 && curl -fsSL https://code.kimi.com/kimi-code/install.sh \
      | env KIMI_INSTALL_DIR=/usr/local KIMI_NO_MODIFY_PATH=1 bash

# A user matching the host uid/gid so bind-mounted files aren't root-owned.
# A host GID or UID may already exist in the base image — sudo builds pass
# UID/GID 0, which the image's root already owns — so creating either
# unconditionally can fail the build. Reuse existing IDs; only create when
# absent. The *names* are irrelevant; only the numeric IDs must match the host
# for bind-mounted ownership to line up. HOME is /home/${SC_USER} regardless
# (see ENV below), so ensure that dir even when the user pre-existed (root's
# real home is /root).
RUN if ! getent group ${SC_GID} >/dev/null; then groupadd -g ${SC_GID} ${SC_USER}; fi \
 && if ! getent passwd ${SC_UID} >/dev/null; then useradd -m -u ${SC_UID} -g ${SC_GID} -s /bin/bash ${SC_USER}; fi \
 && mkdir -p /home/${SC_USER} \
 && chown ${SC_UID}:${SC_GID} /home/${SC_USER}

USER ${SC_USER}
ENV HOME=/home/${SC_USER}
# The dirs the official installers drop binaries into — on PATH so execvpe and
# run.py's ensure_harness_path() agree (it derives the same dirs from Path.home).
ENV PATH=/home/${SC_USER}/.local/bin:/home/${SC_USER}/.opencode/bin:${PATH}

# Harness CLIs — official native installers, no npm (mirrors install.py).
# Codex drops a launcher in ~/.local/bin whose standalone target is under
# ~/.codex/packages. Runtime mounts the host's whole ~/.codex so auth, sessions,
# goals, plugins, and other durable state survive; without relocation that mount
# also masks the image's newer package (#775). Copy the resolved standalone
# executable and its code-mode helper into image-owned ~/.local/libexec, then
# repoint the launcher there.
# Vibe's installer self-bootstraps uv (astral.sh) then `uv tool install`s into
# ~/.local/bin.
# (kimi is baked earlier, in the root stage — see the comment there.)
# SC_HARNESS_EPOCH is referenced so a rolled epoch re-runs these installers; they
# resolve "latest" themselves, so the epoch is an expiry, not a version pin.
RUN echo "harness epoch: ${SC_HARNESS_EPOCH}" \
 && curl -fsSL https://claude.ai/install.sh | bash \
 && curl -fsSL https://opencode.ai/install | bash \
 && curl -fsSL https://chatgpt.com/codex/install.sh | sh \
 && codex_installed="$(readlink -f "$HOME/.local/bin/codex")" \
 && codex_code_mode_host="$(dirname "$codex_installed")/codex-code-mode-host" \
 && test -x "$codex_installed" \
 && test -x "$codex_code_mode_host" \
 && mkdir -p "$HOME/.local/libexec" \
 && install -m 0755 "$codex_installed" "$HOME/.local/libexec/codex" \
 && install -m 0755 "$codex_code_mode_host" "$HOME/.local/libexec/codex-code-mode-host" \
 && ln -sfn "$HOME/.local/libexec/codex" "$HOME/.local/bin/codex" \
 && "$HOME/.local/bin/codex" --version \
 && curl -LsSf https://mistral.ai/vibe/install.sh | bash

# Stamp the epoch the harness layers were built with, so an image can be asked
# what it is rather than guessed at: `./sc harness-status` reads this label and
# says so when the image predates the stored epoch (i.e. a build is owed).
LABEL sc.harness_epoch="${SC_HARNESS_EPOCH}"

# Safe in-container HTTPS auth — `gh` serves the validated runtime GH_TOKEN to
# Git without putting it in a remote URL, Git config, or process argv. Missing
# credentials fail promptly rather than opening a terminal prompt. SSH remotes
# are deliberately not rewritten: the repository's configured transport is an
# operator choice and remains byte-for-byte unchanged.
ENV GIT_TERMINAL_PROMPT=0
RUN git config --global credential."https://github.com".helper "!gh auth git-credential"

WORKDIR /home/${SC_USER}/super-coder

# Foreground stdlib server (api + static UI on one port). compose overrides the
# port; SC_BIND=0.0.0.0 (set in compose) lets docker publish it.
CMD ["./sc", "serve"]
