# super-coder sandbox image — the *environment*, not the code.
#
# The repo is bind-mounted at runtime (see `./sc launch`); this image carries
# what the harness needs to run: python3 + sqlite3, git/curl, gh, and the two
# harness CLIs baked in via their official native installers (the same ones
# install.py uses — keep the URLs in sync with scripts/install.py HARNESS_INSTALL).
# It also carries the common *project* runtimes a forked repo's dev surface needs
# (node LTS), so `npm`/dev servers work without an ephemeral in-container install.
#
# Why bake the binaries but mount the creds: claude installs as a symlink into
# ~/.local/share/claude/versions/<v> (awkward to bind-mount); opencode is a fat
# self-contained binary. Baking both sidesteps the symlink dance. Auth/config
# (~/.claude, ~/.claude.json, ~/.config/opencode, ~/.local/share/opencode) is
# mounted rw from the host so the container reuses the login you already did —
# no in-container `auth login`.
#
# The container runs as your uid (compose `user:`) with HOME=/home/<user> equal
# to the host, so paths match host==container and Path.home() finds everything.
FROM python:3.12-slim

ARG SC_USER=j3d1
ARG SC_UID=1000
ARG SC_GID=1000

# sqlite3 CLI + git + curl + bash + ca-certs (installers pipe-to-bash over https).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sqlite3 git curl bash ca-certificates \
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

# A user matching the host uid/gid so bind-mounted files aren't root-owned.
RUN groupadd -g ${SC_GID} ${SC_USER} \
 && useradd -m -u ${SC_UID} -g ${SC_GID} -s /bin/bash ${SC_USER}

USER ${SC_USER}
ENV HOME=/home/${SC_USER}
# The dirs the official installers drop binaries into — on PATH so execvpe and
# run.py's ensure_harness_path() agree (it derives the same dirs from Path.home).
ENV PATH=/home/${SC_USER}/.local/bin:/home/${SC_USER}/.opencode/bin:${PATH}

# Harness CLIs — official native installers, no npm (mirrors install.py).
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && curl -fsSL https://opencode.ai/install | bash

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
