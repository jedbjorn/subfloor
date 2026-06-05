# super-coder sandbox image — the *environment*, not the code.
#
# The repo is bind-mounted at runtime (see `./sc launch`); this image carries
# only what the harness needs to run: python3 + sqlite3, git/curl, and the two
# harness CLIs baked in via their official native installers (the same ones
# install.py uses — keep the URLs in sync with scripts/install.py HARNESS_INSTALL).
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

WORKDIR /home/${SC_USER}/super-coder

# Foreground stdlib server (api + static UI on one port). compose overrides the
# port; SC_BIND=0.0.0.0 (set in compose) lets docker publish it.
CMD ["./sc", "serve"]
