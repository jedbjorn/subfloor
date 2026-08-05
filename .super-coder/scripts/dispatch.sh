#!/bin/sh
# super-coder dispatcher body — engine-owned, materialized by `./sc update`
# (spec #105, single-owner dispatcher). The tracked `sc` at the repo root is a
# thin bootstrap: it resolves the LIVE engine and execs this file with
# SC_CALLER_ROOT exported. Keeping the verb table here — inside the engine —
# means a stale committed bootstrap on an old shell branch can never hide a
# verb the live floor carries. Everything below the identity block is the
# dispatcher exactly as it lived in `sc`. Run via:  ./sc <command> [args]
# BARE-METAL SPECIALIZATION (this source fork): launch/enter/down/restart run
# directly on the host; docker remains available via sandbox-* commands.
set -e

# Caller identity arrives from the bootstrap. Invoked directly (tests, or a
# maintainer running an edited body without the bootstrap), fall back to the
# checkout holding this engine — one identity, same as a standalone root.
if [ -n "${SC_CALLER_ROOT:-}" ] && [ -d "${SC_CALLER_ROOT:-}" ]; then
  here="$(CDPATH= cd -- "$SC_CALLER_ROOT" && pwd)"
else
  here="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
fi
cd "$here"

# FOUR identities, never one ROOT (spec #68). The CALLER is the checkout holding
# the `sc` that was invoked; the LIVE instance is the MAIN worktree root, which
# owns `.super-coder/` and its gitignored DB. A linked worktree (a shell's
# `.sc-worktrees/<name>/`) has a tracked copy of the `sc` bootstrap — and, in
# the canonical repo where `.super-coder/` is tracked, even a DB-less engine
# copy —
# but never the live DB, map or runtime identity. Resolving the live root via
# git's common dir (its parent is the main worktree) is what makes `./sc mem`
# work from any worktree; it is ALSO what used to make `./sc migrate` silently
# maintain the shared instance from a worktree. So both values are kept, commands
# are CLASSIFIED against them below, and neither is a default for the other.
#
# We do NOT cd to the live root: cwd stays the caller's worktree so git ops +
# shell inference see it. `pwd -P` on both sides so a symlinked or relatively
# invoked caller compares as itself rather than as a second identity. If the
# git-common-dir resolution fails (no checkout, no engine there), the caller is a
# STANDALONE root — one identity, and we never guess a second target.
CALLER_ROOT="$(CDPATH= cd -- "$here" && pwd -P)"
CALLER_ENGINE="$CALLER_ROOT/.super-coder"
LIVE_ROOT="$CALLER_ROOT"
_root="$(cd "$here" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd -P || true)"
[ -n "$_root" ] && [ -d "$_root/.super-coder" ] && LIVE_ROOT="$_root"

# LINKED=1 means caller != live: this invocation can reach shared state it does
# not own. Compared as normalized paths — never a branch name, never the
# `.sc-worktrees` spelling (req 7), so a fork that keeps worktrees anywhere (or
# none at all) is judged by the same test.
LINKED=0
[ "$CALLER_ROOT" != "$LIVE_ROOT" ] && LINKED=1

ROOT="$LIVE_ROOT"          # every path below this line is the LIVE instance's
ENGINE="$ROOT/.super-coder"
PY="${SC_PYTHON:-python3}"
DB="$ENGINE/shell_db.db"
S="$ENGINE/scripts"
# Keep this bare-metal specialization self-contained on hosts where $HOME is
# read-only or shared. Operators can still override with an absolute path.
# Upstream's db_backup.py reads SC_DB_BACKUP_DIR; keep the historical
# SC_BACKUP_DIR name working by bridging one onto the other.
SC_BACKUP_DIR="${SC_BACKUP_DIR:-$ENGINE/run/db_backups/$(basename "$ROOT")}"
SC_DB_BACKUP_DIR="${SC_DB_BACKUP_DIR:-$SC_BACKUP_DIR}"
export SC_BACKUP_DIR SC_DB_BACKUP_DIR
MAPDB="$("$PY" "$S/artifact_policy.py" path map-db)"

# --- linked-worktree target safety (spec #68, decision #81) -------------------
# A command whose subject is the SHARED live instance refuses from a linked
# worktree, BEFORE it opens or deletes anything. The worktree has no DB, map,
# instance config or runtime identity of its own, so there is nothing local to
# act on instead: decision #81 declined a worktree-local runtime mode, because a
# partial instance replaces one ambiguity with another. A named refusal is the
# whole fix — `./sc migrate` from a worktree used to maintain the main
# checkout's live DB and say nothing about which DB it meant.
#
# `-h`/`--help` is not an action and must work from any checkout:
# parse first, refuse second, act third. Only the commands whose target
# actually implements help consult sc_help_form; for the rest every form is an
# action form and refuses.
sc_help_form() {
  for _a in "$@"; do
    case "$_a" in -h|--help) return 0 ;; esac
  done
  return 1
}

# $1 = the command as the operator typed it · $2 = the live target declined.
# stderr + exit 1: nothing ran, so nothing may read as having run.
sc_refuse_linked() {
  [ "$LINKED" -eq 1 ] || return 0
  {
    echo "✗ ./sc $1 refused: this is a linked worktree, not the live instance."
    echo "    caller worktree : $CALLER_ROOT"
    echo "    live instance   : $LIVE_ROOT"
    echo "    declined target : $2"
    echo "  ./sc $1 acts on the shared live instance above, which this worktree"
    echo "  does not own. Nothing was opened, written or deleted."
    echo "  For live maintenance, run it from the main checkout:"
    echo "      cd $LIVE_ROOT && ./sc $1"
  } >&2
  exit 1
}

port() { "$PY" "$S/ports.py" port; }
devport() { "$PY" "$S/ports.py" devport; }

# The two localhost URLs an operator needs, derived from this fork's ports —
# never a fixed 8800, because every fork lands on its own offset (ports.py).
# One printer, three callers (`url`, `enter`, `enter-<shortname>`): entry
# restates the links before handing the terminal to the harness, and `./sc url`
# / `make dos-url` is the recall path once they have scrolled away.
sc_urls() {
  # Under `set -e` a failed derivation would abort the caller, so it is a
  # return here and `enter` ignores it: an operator who cannot be told the
  # URL still gets their shell. `url` propagates it — a recall command that
  # printed nothing and exited 0 would read as "this fork has no GUI".
  sc_url_gui="$(port)" && sc_url_dev="$(devport)" || {
    echo "✗ could not derive this fork's ports — is ${PY} able to run $S/ports.py?" >&2
    return 1
  }
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    printf '  \033[1mReview GUI  \033[36mhttp://127.0.0.1:%s\033[0m\n' "$sc_url_gui"
  else
    printf '  Review GUI  http://127.0.0.1:%s\n' "$sc_url_gui"
  fi
  printf '  dev server  http://127.0.0.1:%s\n' "$sc_url_dev"
}

# Host-side docker orchestration (raw docker — no compose plugin dependency).
# The sandbox runs as you (uid/gid → no root-owned files), bind-mounts this repo
# at its host path + your harness creds rw, and publishes this fork's derived
# port to 127.0.0.1 only. The in-container primitives (`serve`, `boot`) need no
# docker and so run the same whether on the host or inside the container.
IMG=super-coder-sandbox
CNAME="sc-$(basename "$here")"   # unique per fork, like the pm2 name
# Shared inter-fork network. Sandbox containers join it so a shell in one fork
# can reach another fork's API by container name (http://sc-<repo>:<port>) — see
# dnet(). Override with SC_NET to isolate a fork onto its own network.
SC_NET="${SC_NET:-sc-net}"
# Optional Postgres sidecar (per-fork, app-only). Named container + data volume
# tied to this fork. The engine DB is SQLite always (db_driver is SQLite-only);
# this sidecar exists purely so a shell can develop + test the fork's *app*
# against real Postgres inside the sandbox, isolated from any host PG.
PGNAME="sc-pg-$(basename "$here")"
PGVOL="sc-pg-$(basename "$here")-data"
# /dev/shm for the sidecar. Docker's 64MB default is too small for postgres's
# posix DSM (parallel-query segments) — concurrent suites exhaust it and trip a
# postmaster crash-reinit that kills every connection (#298). tmpfs, allocated
# on use, so a generous cap is cheap. Override with SC_PG_SHM.
SC_PG_SHM="${SC_PG_SHM:-1g}"

# Fail fast with the fix if the docker daemon isn't reachable, instead of a
# cryptic build/run error. Host setup is one-time and lives in `./sc doctor` /
# `./sc install` — it needs sudo + a re-login, so it can't fold into launch.
dcheck() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "✗ docker daemon not reachable — the sandbox needs it." >&2
    echo "  Setup (one-time): ./sc doctor — or use the primary bare-metal path: ./sc launch + ./sc enter" >&2
    exit 1
  fi
}

# Ensure the harness cred mount-sources exist as the RIGHT TYPE before docker
# bind-mounts them. A missing DIR source is harmless (docker makes a dir), but a
# missing FILE source (~/.claude.json) gets auto-created as a directory and
# breaks claude — so seed it with empty json. Real creds come from a one-time
# host login (`./sc doctor` guides it); this just keeps the mounts valid.
dcreds() {
  mkdir -p "$HOME/.claude" "$HOME/.config/opencode" "$HOME/.local/share/opencode" "$HOME/.codex" "$HOME/.vibe" "$HOME/.kimi-code" 2>/dev/null || true
  [ -e "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
}

# Which in-container uid writes the bind-mounted repo as YOU on the host.
# Rootless docker maps container-root → host-you, so run as root (it is not real
# root — just your user inside the namespace). Rootful maps uid 1:1, so run as
# your uid. Get this wrong and the mount is read-only-ish (EACCES on write).
duser() {
  if docker info 2>/dev/null | grep -qi rootless; then echo "0:0"
  else echo "$(id -u):$(id -g)"; fi
}

# Ensure the shared inter-fork network exists (idempotent — created once, reused
# by every fork). Sandbox containers join it so a shell in one fork can reach
# another fork's API by container name (http://sc-<repo>:<port>, e.g.
# sc-dos-arch:8804) — Docker's embedded DNS resolves container names on a
# user-defined network, which the default bridge does NOT do. This is
# container<->container only: host port publishing stays 127.0.0.1-bound, so no
# new host exposure. A fork that wants isolation sets SC_NET to its own name.
dnet() {
  docker network inspect "$SC_NET" >/dev/null 2>&1 \
    || docker network create "$SC_NET" >/dev/null
}

# Sandbox harness freshness. The harness CLIs are baked into the image (their
# binaries are host-ABI artifacts — see install.py's harness-epoch note for why
# host state mounts must never choose them), and docker serves those installer
# layers from cache forever. The epoch is their cache key: rolling it re-runs
# the installers on the next build, and nothing else above the harness seam is
# invalidated.
# install.py owns the value + its file; these are thin readers so there is one
# implementation of it, not two that can disagree.
harness_epoch()      { "$PY" "$S/install.py" --harness-epoch; }
harness_epoch_roll() { "$PY" "$S/install.py" --roll-harness-epoch; }

# What the CURRENT image was actually built with, read back from the label the
# Dockerfile stamps. Empty for an image built before this seam existed (or none
# at all) — callers treat that as "unknown", never as "current".
harness_epoch_built() {
  docker image inspect "$IMG" --format '{{index .Config.Labels "sc.harness_epoch"}}' 2>/dev/null \
    | sed 's/^<no value>$//' || true
}

# Build the env image (the repo is bind-mounted at run time, never baked — see
# .dockerignore: the build context is empty). Cheap to re-run; layers cache —
# including the harness layers, unless the epoch below has been rolled since.
dbuild() {
  docker build -t "$IMG" -f "$ENGINE/Dockerfile" \
    --build-arg SC_USER="$(id -un)" \
    --build-arg SC_UID="$(id -u)" \
    --build-arg SC_GID="$(id -g)" \
    --build-arg SC_HARNESS_EPOCH="$(harness_epoch)" \
    "$here"
}

# ── host-native review server (the primary lifecycle) ────────────────────────
# Bare metal is deliberate for this source fork: shells need the host toolchain
# and direct access to the substrate they maintain. Keep the review server
# detached with the same small pidfile pattern used by the watcher/brokers.
HOST_SERVER_PID="$ENGINE/run/server.pid"
HOST_SERVER_LOG="$ENGINE/run/server.log"

sc_host_server_alive() {
  [ -f "$HOST_SERVER_PID" ] || return 1
  pid="$(cat "$HOST_SERVER_PID" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -q "api/server\\.py"
}

# A checkout move can strand a live server after its pidfile disappears. Find
# only listeners that are unmistakably this checkout's server: same configured
# port, server.py command, and kernel-resolved cwd. This keeps restart from
# killing an unrelated service that happens to own the port.
sc_host_server_orphan_pids() {
  command -v fuser >/dev/null 2>&1 || return 0
  p="$(port)"
  root_real="$(readlink -f "$ROOT" 2>/dev/null || printf '%s' "$ROOT")"
  for candidate in $(fuser -n tcp "$p" 2>/dev/null || true); do
    args="$(ps -p "$candidate" -o args= 2>/dev/null || true)"
    cwd="$(readlink -f "/proc/$candidate/cwd" 2>/dev/null || true)"
    case "$args" in
      *".super-coder/api/server.py"*"--port $p"*)
        [ "$cwd" = "$root_real" ] && printf '%s\n' "$candidate"
        ;;
    esac
  done
}

sc_host_server_up() {
  "$PY" "$S/ports.py" ensure >/dev/null
  p="$(port)"
  if sc_host_server_alive; then
    echo "→ host server already running (pid $(cat "$HOST_SERVER_PID")) · Review GUI http://127.0.0.1:$p"
    return 0
  fi
  mkdir -p "$ENGINE/run"
  rm -f "$HOST_SERVER_PID"
  nohup env SC_BIND=127.0.0.1 PYTHONUNBUFFERED=1 \
    "$PY" "$ENGINE/api/server.py" --port "$p" >"$HOST_SERVER_LOG" 2>&1 &
  pid=$!
  echo "$pid" > "$HOST_SERVER_PID"
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:$p/api/health" >/dev/null 2>&1; then
      echo "→ bare-metal server up (pid $pid) · Review GUI http://127.0.0.1:$p"
      echo "  boot a shell: ./sc enter   (or ./sc enter-<shortname>)"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
    i=$((i + 1))
  done
  echo "✗ host server failed to become healthy — see $HOST_SERVER_LOG" >&2
  rm -f "$HOST_SERVER_PID"
  return 1
}
sc_host_server_down() {
  if sc_host_server_alive; then
    kill "$(cat "$HOST_SERVER_PID")"
    echo "→ bare-metal server stopped"
  else
    orphan_pids="$(sc_host_server_orphan_pids)"
    if [ -n "$orphan_pids" ]; then
      for orphan_pid in $orphan_pids; do
        kill "$orphan_pid"
      done
      # Give ThreadingHTTPServer's signal handler a moment to close the socket
      # before restart attempts to bind the same port.
      i=0
      while [ "$i" -lt 30 ]; do
        still_alive=""
        for orphan_pid in $orphan_pids; do
          kill -0 "$orphan_pid" 2>/dev/null && still_alive=1
        done
        [ -z "$still_alive" ] && break
        sleep 0.1
        i=$((i + 1))
      done
      echo "→ orphaned bare-metal server stopped"
    else
      echo "→ bare-metal server not running"
    fi
  fi
  rm -f "$HOST_SERVER_PID"
}

dimage_preflight() {
  if docker image inspect "$IMG" >/dev/null 2>&1; then
    return 0
  fi
  echo "✗ --no-build: sandbox image '$IMG:latest' is missing; nothing was stopped." >&2
  echo "  Run ./sc build, then retry with --no-build." >&2
  return 1
}

drunning() { [ "$(docker inspect -f '{{.State.Running}}' "$CNAME" 2>/dev/null || echo false)" = true ]; }

# Which harness CLIs the shells will actually run, and whether the image owes a
# build. Answers from INSIDE the sandbox when one is up, because that is the
# runtime shells get — the host's own CLIs are not mounted in and do not decide
# anything on the docker path. Never fatal: this is a status surface, and a
# probe that cannot run should say so rather than take a launch down with it.
sc_harness_status() {
  # In-container (or no docker at all): this process IS the runtime.
  if [ -n "${SC_SANDBOX:-}" ] || ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "harness CLIs (this runtime):"
    "$PY" "$S/harness_versions.py" || true
    return 0
  fi
  stored="$(harness_epoch)"
  built="$(harness_epoch_built)"
  if drunning; then
    echo "harness CLIs (in the sandbox — what shells run):"
    # python3, not $PY: the container's interpreter is its own (the image's), and
    # a host SC_PYTHON pointing at a host venv would not exist in there. $S is a
    # host absolute path that resolves identically inside — launch bind-mounts
    # the repo at its own path, which is what makes this exec work at all.
    docker exec "$CNAME" python3 "$S/harness_versions.py" 2>/dev/null \
      || echo "  (could not probe $CNAME)"
  else
    echo "harness CLIs: sandbox '$CNAME' is not running — ./sc launch to start it."
  fi
  echo "harness epoch: image built with ${built:-<none — predates the epoch seam>} · stored ${stored}"
  # A rolled-but-unbuilt epoch is the actionable state: the operator asked for
  # fresh harnesses and the image has not caught up yet. Say the command.
  if [ "$stored" != "0" ] && [ "$stored" != "$built" ]; then
    echo "  ! the image predates the stored epoch — ./sc restart (or ./sc build) to bake fresh harnesses"
  fi
}

# ── dev kit (deps + test) — in-container primitives, like serve/boot ──────────
# A shell runs these from INSIDE the sandbox, where pip/npm act directly on the
# bind-mounted repo: the .venv / node_modules they create live in the mount and so
# persist across image rebuilds (the point of installing per-fork instead of baking
# into the image). They run in the CURRENT environment — no docker re-exec — so on
# the no-docker host path they use host python3/node, exactly like serve/boot.

# List a fork's manifests, pruning the install-artifact + engine + vcs trees that
# map_repo.py's SKIP_DIRS also excludes (never descend an installed/engine tree).
# Also prunes `vendor/` — installing has a stronger reason to skip it than mapping
# does: a vendored/submodule package.json (e.g. ui/vendor/md-converter) is built by
# its parent, and a stray `npm ci`/`pip install` there runs third-party lifecycle
# scripts we don't own. We install only the manifests the fork itself authors.
# `.sc-worktrees/` is pruned too — each shell worktree is a sibling checkout of
# the SAME repo, so descending it would install/test every manifest N× (QAQC-02).
_sc_find_manifests() {  # $1 = filename glob, e.g. 'requirements*.txt'
  find "$here" \
    \( -name node_modules -o -name .venv -o -name venv -o -name .super-coder \
       -o -name .sc-state -o -name .sc-worktrees -o -name .git -o -name __pycache__ \
       -o -name dist -o -name build -o -name vendor \) -prune -o \
    -name "$1" -type f -print
}

# One map-independent presence gate for Python suites. Reuse the manifest walk
# so tests inherit its engine, worktree, environment, dependency, and build
# pruning; pytest still owns actual recursive collection from the repo root.
_sc_has_python_tests() {
  _sc_find_manifests 'test_*.py' | (
    while IFS= read -r test_file; do
      relative_test=${test_file#"$here"/}
      case "$relative_test" in tests/*|*/tests/*) exit 0 ;; esac
    done
    exit 1
  )
}

# Is the repo .venv's tooling runnable BY THE INTERPRETER THAT RESOLVES HERE?
# Existence is not runnability. A .venv is bind-mounted into the sandbox from
# the host, and `python -m venv` records its interpreter as an UNVERSIONED
# symlink (.venv/bin/python3 -> /usr/bin/python3). That path exists in both
# places and resolves to a DIFFERENT minor version in each whenever the host's
# python3 and the image's python3 disagree. The venv's packages live in
# lib/python<X.Y>/site-packages for the host's X.Y, so under the image's
# interpreter every .venv/bin/* shim is still present and executable and
# imports NOTHING — `ModuleNotFoundError: No module named '_pytest'`, which
# reads as a broken tool or a failing suite rather than as the version skew it
# is. Probe the interpreter against its own site-packages before trusting the
# shims. (The launch-time py_mount passthrough solves this properly for a venv
# built from an out-of-tree interpreter under $HOME; it deliberately declines
# to shadow /usr, so a system-python venv lands here instead.)
_sc_venv_runnable() {
  venv="$here/.venv"
  [ -x "$venv/bin/python" ] || return 1
  vv="$("$venv/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || return 1
  [ -n "$vv" ] && [ -d "$venv/lib/python$vv/site-packages" ]
}

# Resolve a dev-kit tool (ruff / mypy): the fork's .venv copy wins (its pins +
# config) WHEN IT RUNS HERE, else the image/PATH copy (baked into the sandbox
# for exactly this case), else fail with the honest fix. A host-managed .venv
# (pinned out-of-tree interpreter mounted by launch) is pip-skipped in the
# sandbox BY DESIGN, so "run ./sc deps first" was a closed loop there — the
# tool was unobtainable from inside the box (dos-arch QAQC-02). Say what is
# actually wrong and where the fix runs instead.
_sc_devtool() {  # $1 = tool name → prints the executable path, or fails
  venv="$here/.venv"
  if [ -x "$venv/bin/$1" ] && _sc_venv_runnable; then printf '%s\n' "$venv/bin/$1"; return 0; fi
  if command -v "$1" >/dev/null 2>&1; then command -v "$1"; return 0; fi
  hostmanaged=""
  if [ -n "${SC_SANDBOX:-}" ] && [ -e "$venv/bin/python" ]; then
    case "$(readlink -f "$venv/bin/python" 2>/dev/null || true)" in
      "$here"/*) : ;;                       # sandbox-built venv — deps can provision it
      *) hostmanaged=1 ;;                   # pinned host interpreter — pip skipped here
    esac
  fi
  if [ -x "$venv/bin/$1" ] && ! _sc_venv_runnable; then
    # Present but unimportable: name the skew, not a phantom missing package.
    echo "✗ $1: $venv/bin/$1 exists but its interpreter cannot import it — this .venv was built by a different python minor than the one resolving here ($("$venv/bin/python" -V 2>&1), packages under $(ls -d "$venv"/lib/python* 2>/dev/null | tr '\n' ' '))." >&2
    echo "  Fix: \`./sc build\` to bake $1 into the image as the PATH fallback — or on the HOST rebuild .venv from an interpreter launch can mount (a uv-managed one under \$HOME), which carries the SAME binary into the sandbox." >&2
  elif [ -n "$hostmanaged" ]; then
    echo "✗ $1: unavailable, and this .venv is host-managed (pinned out-of-tree interpreter) — in-sandbox pip is skipped by design, so \`./sc deps\` cannot provision it here." >&2
    echo "  Fix on the HOST: install $1 into the pinned venv (e.g. uv pip install $1) — or \`./sc build\` to refresh the sandbox image, which bakes $1 as the PATH fallback." >&2
  else
    echo "✗ $1: not provisioned — run ./sc deps first" >&2
  fi
  return 1
}

# Install the fork's deps into the bind-mounted repo: always a repo-root .venv with
# the engine baseline dev kit (pytest/ruff/mypy/...), plus every requirements*.txt
# layered in first with fork pins winning; plus `npm ci` for each package.json.
# Discovery is a glob walk (map-independent — runs on a fresh fork before `./sc map`).
# A map-backed fast path would read dr_filepath.path (dr_dependency.source_file is
# basename-only, so it can't locate a manifest's dir).
sc_deps() {
  if sc_help_form "$@"; then
    echo "Usage: ./sc deps [-h|--help]"
    return 0
  fi
  rc=0
  venv="$here/.venv"
  # In the sandbox, a .venv whose interpreter lives OUTSIDE the repo is host-built
  # and host-managed — a pinned out-of-tree CPython (uv standalone) that `./sc
  # launch` mounts in, with deps already installed against it. Recreating it with
  # the image's python or pip-ing into it here would clobber that shared tree, so
  # leave python deps to the host (`make install` / `uv`). Node deps still install.
  skip_py=""
  if [ -n "${SC_SANDBOX:-}" ] && [ -e "$venv/bin/python" ]; then
    case "$(readlink -f "$venv/bin/python" 2>/dev/null || true)" in
      "$here"/*) : ;;       # sandbox-built venv inside the repo — manage normally
      *) skip_py=1 ;;       # host-managed pinned interpreter — don't touch it
    esac
  fi
  reqs="$(_sc_find_manifests 'requirements*.txt')"
  base_reqs="$(printf '%s\n' "$reqs" | grep -v 'requirements-dev\.txt' || true)"
  if [ -n "$skip_py" ]; then
    echo "→ deps: python deps are host-managed (pinned interpreter mounted by launch) — skipping venv/pip in the sandbox"
    # Never green-lie the skip (#314/#324/#339): the pinned tree carries what
    # the host installed at launch time, which silently lags the fork's
    # declared pins (a branch adds requirements → deps says done → 12 mystery
    # ModuleNotFoundError test failures). Verify every declared pin resolves
    # in the mounted tree; a missing one is a hard failure with the fix named,
    # not a ✓. (Installing from in here stays off-limits by design — the tree
    # is host-managed and shared.)
    if [ -n "$base_reqs" ]; then
      missing="$(printf '%s\n' "$base_reqs" | "$venv/bin/python" -c '
import importlib.metadata as md, re, sys
missing = []
for path in (l.strip() for l in sys.stdin):
    if not path:
        continue
    try:
        lines = open(path).read().splitlines()
    except OSError:
        continue
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        # skip options (-r/-e/--hash), URL/path requirements — only plain pins verify
        if not line or line.startswith("-") or "://" in line or line.startswith((".", "/")):
            continue
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip()
        if not name:
            continue
        try:
            md.version(name)
        except md.PackageNotFoundError:
            missing.append(line)
print("\n".join(missing))
')"
      if [ -n "$missing" ]; then
        echo "✗ deps: host-managed venv is missing declared python deps:" >&2
        printf '%s\n' "$missing" | sed 's/^/    /' >&2
        echo "  Fix: install the pins above into the pinned venv — on the host (e.g. uv pip install …)," >&2
        echo "  or \`$venv/bin/pip install <pins>\` (the mounted tree accepts installs; ownership lands root)." >&2
        rc=1
      else
        echo "→ deps: host-managed tree verified — every declared python pin present"
      fi
    fi
  else
  # The engine dev kit lives in this .venv, so create it unconditionally — a fork
  # that declares no requirements*.txt still needs pytest on hand for `./sc test`
  # and for shells writing tests. (Previously the venv + kit were gated on $base_reqs,
  # which left pure-JS and undeclared-dep forks with no pytest at all.)
  if [ ! -x "$venv/bin/python" ]; then
    echo "→ deps: creating $venv"
    "$PY" -m venv "$venv" || { echo "✗ deps: venv create failed" >&2; return 1; }
  fi
  # Fork pins first (authoritative), with a sibling requirements-dev.txt if present.
  if [ -n "$base_reqs" ]; then
    printf '%s\n' "$base_reqs" | while IFS= read -r req; do
      [ -n "$req" ] || continue
      echo "→ deps: pip install -r $req"
      "$venv/bin/pip" install -q -r "$req" || exit 1
      dev="$(dirname "$req")/requirements-dev.txt"
      if [ -f "$dev" ]; then
        echo "→ deps: pip install -r $dev"
        "$venv/bin/pip" install -q -r "$dev" || exit 1
      fi
    done || rc=1
  fi
  # Engine baseline dev kit — test (pytest/httpx/coverage), lint+format (ruff),
  # type-check (mypy), SQLite GUI (datasette). only-if-needed never overrides a
  # fork's pin or its [tool.ruff]/[tool.mypy] config — available, not enforced.
  echo "→ deps: engine dev kit (pytest httpx coverage ruff mypy datasette, only-if-needed)"
  "$venv/bin/pip" install -q --upgrade-strategy only-if-needed pytest httpx coverage ruff mypy datasette || rc=1
  fi
  pkgs="$(_sc_find_manifests 'package.json')"
  if [ -n "$pkgs" ]; then
    printf '%s\n' "$pkgs" | while IFS= read -r pkg; do
      [ -n "$pkg" ] || continue
      d="$(dirname "$pkg")"
      if [ -f "$d/package-lock.json" ]; then
        echo "→ deps: npm ci in $d"; ( cd "$d" && npm ci ) || exit 1
      else
        echo "→ deps: npm install in $d"; ( cd "$d" && npm install ) || exit 1
      fi
    done || rc=1
  fi
  if [ -z "$base_reqs" ] && [ -z "$pkgs" ]; then
    echo "→ deps: no fork requirements*.txt or package.json found — engine dev kit only"
  fi
  [ "$rc" -eq 0 ] || { echo "✗ deps: one or more installs failed" >&2; return 1; }
  echo "✓ deps: done"
}

# True if the fork declares a pytest config — pytest.ini, a [tool.pytest…] table in
# pyproject.toml, or [tool:pytest] in setup.cfg. The discriminator between "this
# fork opted out of pytest" (→ stdlib unittest fallback) and "this fork needs
# pytest but the .venv is unprovisioned" (→ hard error, never a silent downgrade).
_sc_wants_pytest() {
  [ -f "$here/pytest.ini" ] && return 0
  [ -f "$here/pyproject.toml" ] && grep -q '\[tool\.pytest' "$here/pyproject.toml" 2>/dev/null && return 0
  [ -f "$here/setup.cfg" ] && grep -q '\[tool:pytest\]' "$here/setup.cfg" 2>/dev/null && return 0
  return 1
}

# Run the fork's test suites: backend (the .venv's pytest, honoring the fork's
# pytest.ini; else the engine's own stdlib-unittest suite) + UI (npm run test /
# vitest in any package.json dir that declares a test script). Non-zero if any fail.
sc_test() {
  venv="$here/.venv"
  python_tests=""
  _sc_has_python_tests && python_tests=1
  # Self-heal an unprovisioned OR unrunnable host venv before selecting a test
  # runner. A dangling interpreter leaves the pytest shim executable, so -x is
  # not evidence that the venv can run. On the host the repo-local .venv is ours
  # to rebuild; in the sandbox a host-managed venv stays untouched and sc_deps'
  # existing ownership check chooses the safe PATH fallback.
  provision_reason=""
  if [ -n "$python_tests" ]; then
    if [ ! -x "$venv/bin/pytest" ]; then
      provision_reason=missing
    elif ! _sc_venv_runnable; then
      provision_reason=unrunnable
    fi
  fi
  if [ -n "$provision_reason" ]; then
    if [ "$provision_reason" = unrunnable ] && [ -z "${SC_SANDBOX:-}" ]; then
      echo "→ test: $venv is not runnable — rebuilding before provisioning"
      ( cd "$here" && rm -rf -- .venv )
    else
      echo "→ test: $venv/bin/pytest unavailable — provisioning first (./sc deps)"
    fi
    sc_deps || echo "→ test: provisioning incomplete — continuing" >&2
  fi
  # Which pytest can we actually RUN? The .venv copy wins (fork pins + config)
  # only when its interpreter can import it — see _sc_venv_runnable. Running a
  # present-but-unimportable shim anyway fails the ENTIRE suite with
  # ModuleNotFoundError, which reads as a real test failure and sends whoever
  # sees it hunting a bug that is not there. The image bakes pytest as the
  # fallback for exactly this case; it is a real pytest, not the stdlib
  # downgrade the branch below guards against, so a fork's own missing deps
  # still fail loudly rather than passing green.
  pytest_bin=""
  if [ -x "$venv/bin/pytest" ] && _sc_venv_runnable; then
    pytest_bin="$venv/bin/pytest"
  elif command -v pytest >/dev/null 2>&1; then
    pytest_bin="$(command -v pytest)"
    [ -x "$venv/bin/pytest" ] && echo "→ test: $venv/bin/pytest is not importable by its interpreter ($("$venv/bin/python" -V 2>&1)) — using $pytest_bin" >&2
  fi
  rc=0
  if [ -n "$pytest_bin" ]; then
    echo "→ test: $pytest_bin"
    ( cd "$here" && "$pytest_bin" "$@" )
    prc=$?
    if [ "$prc" -eq 5 ] && [ $# -eq 0 ]; then
      # pytest exit 5 = collected nothing. On a bare `./sc test` in a fork with
      # no python tests (JS-only forks: vitest is the real suite) that is not a
      # failure — counting it as one left every JS-only fork permanently red
      # (#310). With explicit args a collection miss stays red: a typo'd path
      # must not pass green.
      echo "→ test: pytest collected no python tests — not counted as a failure (JS-only fork)"
    elif [ "$prc" -ne 0 ]; then
      rc=1
    fi
  elif [ -n "$python_tests" ]; then
    # pytest still unavailable after provisioning (venv create failed, or a
    # host-managed sandbox interpreter that skips pip). A fork that *declares*
    # pytest must not be green-washed through stdlib unittest — fail loud with the
    # fix. Only a fork with no pytest config keeps the legacy stdlib fallback.
    if _sc_wants_pytest; then
      echo "✗ test: pytest required (pytest config present) but unavailable — neither $venv nor PATH has a runnable copy; run ./sc deps to provision it (or ./sc build to bake the image fallback)" >&2
      rc=1
    else
      echo "→ test: python3 -m unittest discover (stdlib)"
      ( cd "$here" && "$PY" -m unittest discover -s tests -p 'test_*.py' ) || rc=1
    fi
  else
    echo "→ test: no python tests found"
  fi
  pkgs="$(_sc_find_manifests 'package.json')"
  if [ -n "$pkgs" ]; then
    printf '%s\n' "$pkgs" | while IFS= read -r pkg; do
      [ -n "$pkg" ] || continue
      d="$(dirname "$pkg")"
      # Only run where a "test" script is declared (else npm errors "missing script").
      if "$PY" -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('scripts',{}).get('test') else 1)" "$pkg" 2>/dev/null; then
        echo "→ test: npm run test in $d"; ( cd "$d" && npm run test ) || exit 1
      fi
    done || rc=1
  fi
  [ "$rc" -eq 0 ] || { echo "✗ test: one or more suites failed" >&2; return 1; }
  echo "✓ test: all suites passed"
}

# Lint + format-check the fork's python with ruff (the .venv copy when present —
# honors the fork's [tool.ruff] config — else the image's baked fallback).
# Defaults to the repo root; pass paths/flags through. The tool is available,
# not enforced — a fork opts in by running this.
sc_lint() {
  tool="$(_sc_devtool ruff)" || return 1
  echo "→ lint: $tool check"
  ( cd "$here" && "$tool" check "$@" )
}

# Type-check the fork's python with mypy (.venv copy → image fallback, same
# resolution as lint; honors the fork's [tool.mypy] config when the .venv copy
# runs). Same available-not-enforced stance as lint.
sc_typecheck() {
  tool="$(_sc_devtool mypy)" || return 1
  echo "→ typecheck: $tool"
  if [ $# -gt 0 ]; then ( cd "$here" && "$tool" "$@" )
  else ( cd "$here" && "$tool" . ); fi
}

# ── Windows VM broker (HOST-side; drives the test VM for sandboxed forks) ──────
# A separate host process — the sandbox server can't hold the ssh key or reach
# libvirt. It listens on a unix socket in the bind-mounted engine dir so
# windows_devkit (in the container) can curl it without a route or a key. Refuses
# to run in the sandbox (vm_broker.py guards on SC_SANDBOX).
#
# Supervision: `launch` brings it up (and `down` stops it) automatically when the
# fork has linked a VM, so it tracks the sandbox lifecycle with no extra step. For
# reboot-survival independent of launch, `vm-broker-install` writes a systemd
# --user unit. The two coexist: `up` no-ops when the socket already answers (so a
# launch after a systemd start is harmless), and `down` only stops what IT started
# (the pidfile) — it never kills a systemd-managed broker.
VM_BROKER_PID="$ENGINE/run/vm-broker.pid"
VM_BROKER_UNIT="sc-vm-broker-$(basename "$here").service"

# Is the broker already answering on its socket? (true regardless of who started
# it — pidfile nohup or systemd — so `up` is idempotent across both mechanisms.)
sc_vm_broker_alive() {
  sock="$("$PY" "$S/vm.py" sock)"
  [ -S "$sock" ] || return 1
  curl -s --unix-socket "$sock" http://vm/health 2>/dev/null | grep -q '"ok": true'
}
sc_vm_broker_up() {
  if ! "$PY" "$S/vm.py" configured; then
    echo "→ vm-broker: no VM linked (instance.json has no \`vm\` block) — nothing to serve"; return 0
  fi
  if sc_vm_broker_alive; then echo "→ vm-broker already serving $("$PY" "$S/vm.py" sock)"; return 0; fi
  mkdir -p "$ENGINE/run"
  nohup "$PY" "$ENGINE/api/vm_broker.py" >"$ENGINE/run/vm-broker.log" 2>&1 &
  echo $! > "$VM_BROKER_PID"
  echo "→ vm-broker up (pid $!) · socket $("$PY" "$S/vm.py" sock) · log $ENGINE/run/vm-broker.log"
}
sc_vm_broker_down() {
  if [ -f "$VM_BROKER_PID" ] && kill -0 "$(cat "$VM_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$VM_BROKER_PID")" && echo "→ vm-broker stopped"
  elif sc_vm_broker_alive; then
    echo "→ vm-broker is running but not from \`vm-broker-up\` (systemd?) — leaving it; use vm-broker-uninstall"
  else
    echo "→ vm-broker not running"
  fi
  rm -f "$VM_BROKER_PID"
}
# Install a systemd --user unit so the broker survives logout/reboot without a
# launch. enable-linger lets it run with no active session; Restart=on-failure
# covers crashes. Idempotent — rewrites + re-enables.
sc_vm_broker_install() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ vm-broker-install: systemd (systemctl) not found on this host" >&2; return 1; }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/$VM_BROKER_UNIT" <<UNIT
[Unit]
Description=super-coder vm-broker ($(basename "$here")) — host-side Windows VM broker
After=network.target libvirtd.service

[Service]
ExecStart=$PY $ENGINE/api/vm_broker.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  # A pidfile-managed broker would hold the socket; stop it so systemd owns it.
  sc_vm_broker_down >/dev/null 2>&1 || true
  # `enable --now` does not restart an already-active unit after ExecStart was
  # rewritten (notably when the fork moved). Enable, then restart explicitly so
  # the live process always executes the path in the freshly written unit.
  systemctl --user enable "$VM_BROKER_UNIT"
  systemctl --user restart "$VM_BROKER_UNIT"
  echo "→ vm-broker installed as systemd --user unit: $VM_BROKER_UNIT (enabled, started, linger on)"
  echo "  status: systemctl --user status $VM_BROKER_UNIT   ·   logs: journalctl --user -u $VM_BROKER_UNIT"
}
sc_vm_broker_uninstall() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ vm-broker-uninstall: systemd not found" >&2; return 1; }
  systemctl --user disable --now "$VM_BROKER_UNIT" 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$VM_BROKER_UNIT"
  systemctl --user daemon-reload
  echo "→ vm-broker systemd unit removed ($VM_BROKER_UNIT)"
}

# ── Tailnet broker (HOST-side; drives the tailnet for sandboxed forks) ─────────
# Sibling of the vm-broker: the sandbox can't join the tailnet (no route, no TUN,
# no NET_ADMIN) and must not hold a tailnet credential. This host process owns the
# already-`tailscale up` node and listens on a unix socket in the bind-mounted
# engine dir so the `tailscale` skill (in the container) can curl it without a
# route or a key. Refuses to run in the sandbox (ts_broker.py guards on SC_SANDBOX).
# Same supervision model as vm-broker: `launch` brings it up / `down` stops it when
# a tailnet is linked; `ts-broker-install` writes a systemd --user unit for
# reboot-survival. `up` no-ops when the socket already answers; `down` only stops
# what IT started (the pidfile), never a systemd-managed broker.
TS_BROKER_PID="$ENGINE/run/ts-broker.pid"
TS_BROKER_UNIT="sc-ts-broker-$(basename "$here").service"

sc_ts_broker_alive() {
  sock="$("$PY" "$S/ts.py" sock)"
  [ -S "$sock" ] || return 1
  curl -s --unix-socket "$sock" http://ts/health 2>/dev/null | grep -q '"ok": true'
}
sc_ts_broker_up() {
  if ! "$PY" "$S/ts.py" configured; then
    echo "→ ts-broker: no tailnet linked (instance.json has no \`ts\` block) — nothing to serve"; return 0
  fi
  if sc_ts_broker_alive; then echo "→ ts-broker already serving $("$PY" "$S/ts.py" sock)"; return 0; fi
  mkdir -p "$ENGINE/run"
  nohup "$PY" "$ENGINE/api/ts_broker.py" >"$ENGINE/run/ts-broker.log" 2>&1 &
  echo $! > "$TS_BROKER_PID"
  echo "→ ts-broker up (pid $!) · socket $("$PY" "$S/ts.py" sock) · log $ENGINE/run/ts-broker.log"
}
sc_ts_broker_down() {
  if [ -f "$TS_BROKER_PID" ] && kill -0 "$(cat "$TS_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$TS_BROKER_PID")" && echo "→ ts-broker stopped"
  elif sc_ts_broker_alive; then
    echo "→ ts-broker is running but not from \`ts-broker-up\` (systemd?) — leaving it; use ts-broker-uninstall"
  else
    echo "→ ts-broker not running"
  fi
  rm -f "$TS_BROKER_PID"
}
sc_ts_broker_install() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ ts-broker-install: systemd (systemctl) not found on this host" >&2; return 1; }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/$TS_BROKER_UNIT" <<UNIT
[Unit]
Description=super-coder ts-broker ($(basename "$here")) — host-side tailnet broker
After=network.target tailscaled.service

[Service]
ExecStart=$PY $ENGINE/api/ts_broker.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  # A pidfile-managed broker would hold the socket; stop it so systemd owns it.
  sc_ts_broker_down >/dev/null 2>&1 || true
  systemctl --user enable "$TS_BROKER_UNIT"
  systemctl --user restart "$TS_BROKER_UNIT"
  echo "→ ts-broker installed as systemd --user unit: $TS_BROKER_UNIT (enabled, started, linger on)"
  echo "  status: systemctl --user status $TS_BROKER_UNIT   ·   logs: journalctl --user -u $TS_BROKER_UNIT"
}
sc_ts_broker_uninstall() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ ts-broker-uninstall: systemd not found" >&2; return 1; }
  systemctl --user disable --now "$TS_BROKER_UNIT" 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$TS_BROKER_UNIT"
  systemctl --user daemon-reload
  echo "→ ts-broker systemd unit removed ($TS_BROKER_UNIT)"
}

# ── pm2 broker (HOST-side; observes + manages the host's pm2 stack) ───────────
# Third sibling of the vm/ts brokers: the sandbox has no pm2 binary and no route
# to the host's 127.0.0.1-bound ports, but an admin shell owns the fork's infra
# and needs to see + bounce the pm2-supervised app (deploy confirmation). This
# host process owns pm2 and listens on a unix socket in the bind-mounted engine
# dir so the `pm2` skill (in the container) can curl it. Every verb is
# fail-closed on the `pm2` block's `processes` allowlist. Refuses to run in the
# sandbox (pm2_broker.py guards on SC_SANDBOX). Same supervision model as its
# siblings: `launch` brings it up / `down` stops it when a stack is linked;
# `pm2-broker-install` writes a systemd --user unit for reboot-survival. `up`
# no-ops when the socket already answers; `down` only stops what IT started.
PM2_BROKER_PID="$ENGINE/run/pm2-broker.pid"
PM2_BROKER_UNIT="sc-pm2-broker-$(basename "$here").service"

sc_pm2_broker_alive() {
  sock="$("$PY" "$S/pm2.py" sock)"
  [ -S "$sock" ] || return 1
  curl -s --unix-socket "$sock" http://pm2/health 2>/dev/null | grep -q '"ok": true'
}
sc_pm2_broker_up() {
  if ! "$PY" "$S/pm2.py" configured; then
    echo "→ pm2-broker: no process stack linked (instance.json has no \`pm2\` block) — nothing to serve"; return 0
  fi
  if sc_pm2_broker_alive; then echo "→ pm2-broker already serving $("$PY" "$S/pm2.py" sock)"; return 0; fi
  mkdir -p "$ENGINE/run"
  nohup "$PY" "$ENGINE/api/pm2_broker.py" >"$ENGINE/run/pm2-broker.log" 2>&1 &
  echo $! > "$PM2_BROKER_PID"
  echo "→ pm2-broker up (pid $!) · socket $("$PY" "$S/pm2.py" sock) · log $ENGINE/run/pm2-broker.log"
}
sc_pm2_broker_down() {
  if [ -f "$PM2_BROKER_PID" ] && kill -0 "$(cat "$PM2_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$PM2_BROKER_PID")" && echo "→ pm2-broker stopped"
  elif sc_pm2_broker_alive; then
    echo "→ pm2-broker is running but not from \`pm2-broker-up\` (systemd?) — leaving it; use pm2-broker-uninstall"
  else
    echo "→ pm2-broker not running"
  fi
  rm -f "$PM2_BROKER_PID"
}
sc_pm2_broker_install() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ pm2-broker-install: systemd (systemctl) not found on this host" >&2; return 1; }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/$PM2_BROKER_UNIT" <<UNIT
[Unit]
Description=super-coder pm2-broker ($(basename "$here")) — host-side pm2 broker
After=network.target

[Service]
ExecStart=$PY $ENGINE/api/pm2_broker.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  # A pidfile-managed broker would hold the socket; stop it so systemd owns it.
  sc_pm2_broker_down >/dev/null 2>&1 || true
  systemctl --user enable "$PM2_BROKER_UNIT"
  systemctl --user restart "$PM2_BROKER_UNIT"
  echo "→ pm2-broker installed as systemd --user unit: $PM2_BROKER_UNIT (enabled, started, linger on)"
  echo "  status: systemctl --user status $PM2_BROKER_UNIT   ·   logs: journalctl --user -u $PM2_BROKER_UNIT"
}
sc_pm2_broker_uninstall() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ pm2-broker-uninstall: systemd not found" >&2; return 1; }
  systemctl --user disable --now "$PM2_BROKER_UNIT" 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$PM2_BROKER_UNIT"
  systemctl --user daemon-reload
  echo "→ pm2-broker systemd unit removed ($PM2_BROKER_UNIT)"
}

# ── db broker (HOST-side; read-only diagnostic access to the LIVE app DB) ─────
# A host-side broker that shells out to `psql` where the live DSN + route
# resolve, exposing ONE narrow verb (a single allowlisted, capped, read-only
# SELECT) over a unix socket in the bind-mounted engine dir so the `db_query`
# skill (in the container) can curl it. The sandbox holds no DSN and no route.
# Read-only twice: the DSN must point at a read-only PG role AND dbq.py rejects
# any non-SELECT before psql runs. Refuses to run in the sandbox (db_broker.py
# guards on SC_SANDBOX). Same lifecycle model as its siblings: `up` no-ops when
# the socket already answers; `down` only stops what IT started.
DB_BROKER_PID="$ENGINE/run/db-broker.pid"
DB_BROKER_UNIT="sc-db-broker-$(basename "$here").service"

sc_db_broker_alive() {
  sock="$("$PY" "$S/dbq.py" sock)"
  [ -S "$sock" ] || return 1
  curl -s --unix-socket "$sock" http://db/health 2>/dev/null | grep -q '"ok": true'
}
sc_db_broker_up() {
  if ! "$PY" "$S/dbq.py" configured; then
    echo "→ db-broker: no live DB linked (instance.json has no \`db\` block) — nothing to serve"; return 0
  fi
  if sc_db_broker_alive; then echo "→ db-broker already serving $("$PY" "$S/dbq.py" sock)"; return 0; fi
  mkdir -p "$ENGINE/run"
  nohup "$PY" "$ENGINE/api/db_broker.py" >"$ENGINE/run/db-broker.log" 2>&1 &
  echo $! > "$DB_BROKER_PID"
  echo "→ db-broker up (pid $!) · socket $("$PY" "$S/dbq.py" sock) · log $ENGINE/run/db-broker.log"
}
sc_db_broker_down() {
  if [ -f "$DB_BROKER_PID" ] && kill -0 "$(cat "$DB_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$DB_BROKER_PID")" && echo "→ db-broker stopped"
  elif sc_db_broker_alive; then
    echo "→ db-broker is running but not from \`db-broker-up\` (systemd?) — leaving it; use db-broker-uninstall"
  else
    echo "→ db-broker not running"
  fi
  rm -f "$DB_BROKER_PID"
}
sc_db_broker_install() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ db-broker-install: systemd (systemctl) not found on this host" >&2; return 1; }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  # The DSN is read from the host env at query time; a systemd unit has no login
  # shell, so point it at an EnvironmentFile the operator controls (host-side,
  # never mounted). SC_RO_ENVFILE overrides the default path.
  envfile="${SC_RO_ENVFILE:-$HOME/.config/$(basename "$here")/db-broker.env}"
  cat > "$unit_dir/$DB_BROKER_UNIT" <<UNIT
[Unit]
Description=super-coder db-broker ($(basename "$here")) — host-side read-only DB broker
After=network.target

[Service]
EnvironmentFile=-$envfile
ExecStart=$PY $ENGINE/api/db_broker.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  sc_db_broker_down >/dev/null 2>&1 || true
  systemctl --user enable "$DB_BROKER_UNIT"
  systemctl --user restart "$DB_BROKER_UNIT"
  echo "→ db-broker installed as systemd --user unit: $DB_BROKER_UNIT (enabled, started, linger on)"
  echo "  DSN env-file: $envfile (create it host-side with: SC_RO_DSN=postgresql://…)"
  echo "  status: systemctl --user status $DB_BROKER_UNIT   ·   logs: journalctl --user -u $DB_BROKER_UNIT"
}
sc_db_broker_uninstall() {
  command -v systemctl >/dev/null 2>&1 || { echo "✗ db-broker-uninstall: systemd not found" >&2; return 1; }
  systemctl --user disable --now "$DB_BROKER_UNIT" 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$DB_BROKER_UNIT"
  systemctl --user daemon-reload
  echo "→ db-broker systemd unit removed ($DB_BROKER_UNIT)"
}
sc_db_init() {
  f="$ENGINE/instance.json"
  if [ -f "$f" ] && "$PY" -c "import json,sys; d=json.load(open('$f')); sys.exit(0 if 'db' in d else 1)" 2>/dev/null; then
    echo "→ db: already configured in $f"
  elif [ -f "$f" ]; then
    "$PY" -c "
import json,pathlib
p=pathlib.Path('$f')
d=json.loads(p.read_text())
d['db']={'dsn_env':'SC_RO_DSN','allow_tables':['skill_runs','tool_call_attempts','models'],'row_cap':1000,'statement_timeout_ms':5000}
p.write_text(json.dumps(d,indent=2)+'\n')
print('-> db: added to $f')
"
  else
    printf '{\"db\":{\"dsn_env\":\"SC_RO_DSN\",\"allow_tables\":[\"skill_runs\",\"tool_call_attempts\",\"models\"],\"row_cap\":1000,\"statement_timeout_ms\":5000}}\n' > "$f"
    echo "→ db: created $f with db block"
  fi
  echo "  Host-side setup (the sandbox never sees the credential):"
  echo "    1. Provision a read-only role on the live DB, e.g.:"
  echo "         CREATE ROLE sc_ro LOGIN PASSWORD '…';"
  echo "         GRANT CONNECT ON DATABASE <db> TO sc_ro;"
  echo "         GRANT USAGE ON SCHEMA public TO sc_ro;"
  echo "         GRANT SELECT ON skill_runs, tool_call_attempts, models TO sc_ro;"
  echo "    2. Export its DSN for the broker's environment:"
  echo "         export SC_RO_DSN=postgresql://sc_ro:…@<host>:5432/<db>"
  echo "    3. Start it host-side: ./sc db-broker-up   (or ./sc db-broker-install)"
  echo "  Widen allow_tables (+ the role's GRANTs) to expose more; content tables stay gated."
}

# ── Postgres sidecar (HOST-side Docker container on $SC_NET — APP-ONLY) ───────
# A named postgres:17 container alongside the sandbox on SC_NET. The sandbox
# reaches it by hostname ($PGNAME) with DATABASE_URL forwarded in, so the fork's
# *app* (its own db layer) can run + be tested against real Postgres. The engine
# never reads DATABASE_URL — its DB is SQLite, full stop — so this cannot affect
# the review GUI (that was the #207 regression; it stays fixed). Data persists in
# a named Docker volume ($PGVOL) across restarts + image rebuilds. Enabled
# per-fork by a "pg" key in .super-coder/instance.json (./sc pg-init adds it).
# Creds are local-sandbox-only (sc/sc/sc) — never published to the host.
sc_pg_configured() {
  test -f "$ENGINE/instance.json" || return 1
  "$PY" -c "import json,sys; d=json.load(open('$ENGINE/instance.json')); sys.exit(0 if 'pg' in d else 1)" 2>/dev/null
}
sc_pg_alive() {
  docker inspect --format '{{.State.Running}}' "$PGNAME" 2>/dev/null | grep -q true
}
sc_pg_absent() {
  names="$(docker ps -a --filter "name=^/${PGNAME}$" --format '{{.Names}}')" || return 1
  [ -z "$names" ]
}
sc_pg_up() {
  if ! sc_pg_configured; then
    echo "→ pg: no \`pg\` key in instance.json — skipping (run: ./sc pg-init)"; return 0
  fi
  if sc_pg_alive; then echo "→ pg already running ($PGNAME)"; return 0; fi
  dnet
  docker volume create "$PGVOL" >/dev/null
  docker run -d --name "$PGNAME" --restart unless-stopped \
    --network "$SC_NET" \
    --shm-size "$SC_PG_SHM" \
    -e POSTGRES_USER=sc \
    -e POSTGRES_PASSWORD=sc \
    -e POSTGRES_DB=sc \
    -v "$PGVOL:/var/lib/postgresql/data" \
    postgres:17 >/dev/null
  echo "→ pg 17 up ($PGNAME on $SC_NET) · DATABASE_URL=postgresql://sc:sc@$PGNAME:5432/sc"
}
sc_pg_down() {
  remove_rc=0
  docker rm -f "$PGNAME" >/dev/null 2>&1 || remove_rc=$?
  if sc_pg_absent; then
    if [ "$remove_rc" -eq 0 ]; then
      echo "→ pg stopped (volume $PGVOL retained)"
    fi
    return 0
  fi
  echo "✗ postgres teardown could not verify removal of '$PGNAME'." >&2
  echo "  Fix Docker access, run ./sc pg-down, then retry ./sc restart." >&2
  return 1
}
sc_pg_init() {
  f="$ENGINE/instance.json"
  if [ -f "$f" ]; then
    if "$PY" -c "import json,sys; d=json.load(open('$f')); sys.exit(0 if 'pg' in d else 1)" 2>/dev/null; then
      echo "→ pg: already configured in $f"; return 0
    fi
    "$PY" -c "
import json,pathlib
p=pathlib.Path('$f')
d=json.loads(p.read_text())
d['pg']={}
p.write_text(json.dumps(d,indent=2)+'\n')
print('-> pg: added to $f')
"
  else
    printf '{\"pg\":{}}\n' > "$f"
    echo "→ pg: created $f with pg block"
  fi
  echo "  next: ./sc pg-up   (or ./sc sandbox-launch — pg starts automatically)"
}


# ── persist: reboot-proof every applicable host-side daemon in one verb ───────
# The #359 incident shape: a host reboot kills the nohup'd daemons while the
# docker sandbox resurrects itself — the fork looks healthy with nobody
# polling. One idempotent verb installs the systemd --user unit for each
# daemon that applies to this fork (skips the rest with a reason); linger is
# enabled by the installs, so units start at boot with no login.
sc_persist() {
  command -v systemctl >/dev/null 2>&1 || {
    echo "✗ persist: systemd (systemctl) not found — nohup + \`./sc launch\` is the only supervision on this host" >&2; return 1; }
  if "$PY" "$S/vm.py" configured;  then sc_vm_broker_install;  else echo "→ persist: no VM linked — vm-broker skipped"; fi
  if "$PY" "$S/ts.py" configured;  then sc_ts_broker_install;  else echo "→ persist: no tailnet linked — ts-broker skipped"; fi
  if "$PY" "$S/pm2.py" configured; then sc_pm2_broker_install; else echo "→ persist: no pm2 stack linked — pm2-broker skipped"; fi
  if "$PY" "$S/dbq.py" configured; then sc_db_broker_install;  else echo "→ persist: no live DB linked — db-broker skipped"; fi
  echo "→ persist: done — units survive reboot + logout; remove per daemon with ./sc <name>-uninstall"
}


# Resolve + write-probe the destination before a restart changes any runtime
# state. db_backup.py is also used by rebuild/rollback, keeping one deterministic
# override → home → repo-local fallback contract across every engine backup.
sc_db_backup_preflight() {
  "$PY" "$S/db_backup.py" select "$ROOT"
}
sc_db_backup() {
  prefix="${1:-manual}"
  destination="${2:-}"
  if [ -n "$destination" ]; then
    "$PY" "$S/db_backup.py" backup "$DB" "$ROOT" "$prefix" "$destination"
  else
    "$PY" "$S/db_backup.py" backup "$DB" "$ROOT" "$prefix"
  fi
}

sc_systemd_unit_loaded() {
  command -v systemctl >/dev/null 2>&1 || return 1
  [ "$(systemctl --user show "$1" -p LoadState --value 2>/dev/null)" = "loaded" ]
}

sc_wait_until() {
  check="$1"
  attempts=0
  while [ "$attempts" -lt 20 ]; do
    "$check" && return 0
    attempts=$((attempts + 1))
    sleep 0.25
  done
  return 1
}

sc_sandbox_alive() {
  docker inspect --format '{{.State.Running}}' "$CNAME" 2>/dev/null | grep -q true \
    && curl -fsS "http://127.0.0.1:$(port)/api/health" >/dev/null 2>&1
}

sc_pg_healthy() {
  sc_pg_alive && docker exec "$PGNAME" pg_isready -U sc -d sc >/dev/null 2>&1
}

sc_vm_broker_configured() { "$PY" "$S/vm.py" configured; }
sc_ts_broker_configured() { "$PY" "$S/ts.py" configured; }
sc_pm2_broker_configured() { "$PY" "$S/pm2.py" configured; }
sc_db_broker_configured() { "$PY" "$S/dbq.py" configured; }

# Restart one configured broker through its actual supervisor. launch has
# already recreated pidfile-managed brokers; systemd-managed brokers remain
# alive across down by design, so restart them explicitly to load current code.
sc_restart_broker() {
  label="$1"
  configured="$2"
  alive="$3"
  up="$4"
  down="$5"
  pidfile="$6"
  unit="$7"
  if ! "$configured"; then
    echo "  $label: skipped (unconfigured)"
    return 0
  fi
  supervisor="pidfile"
  if sc_systemd_unit_loaded "$unit"; then
    supervisor="systemd"
    # A loaded-but-previously-inactive unit may have let launch create a
    # pidfile process. Remove that exact process before handing ownership back
    # to systemd; an already-active systemd process is deliberately left alone
    # by the broker's down helper and then restarted by its supervisor.
    "$down" >/dev/null 2>&1 || true
    if ! systemctl --user restart "$unit"; then
      echo "  $label: failed (systemd restart)"
      SC_RESTART_FAILED=1
      return 0
    fi
  elif ! "$up"; then
    echo "  $label: failed (start)"
    SC_RESTART_FAILED=1
    return 0
  elif [ ! -f "$pidfile" ]; then
    echo "  $label: failed (live broker has no recognized supervisor)"
    SC_RESTART_FAILED=1
    return 0
  fi
  if sc_wait_until "$alive"; then
    echo "  $label: restarted ($supervisor)"
  else
    echo "  $label: failed (unhealthy after $supervisor restart)"
    SC_RESTART_FAILED=1
  fi
}

sc_restart_health_summary() {
  launch_rc="$1"
  SC_RESTART_FAILED=0
  echo "→ restart health"
  if [ "$launch_rc" -eq 0 ] && sc_wait_until sc_sandbox_alive; then
    echo "  sandbox: restarted"
  else
    echo "  sandbox: failed (launch or health)"
    SC_RESTART_FAILED=1
  fi
  sc_restart_broker "vm-broker" \
    sc_vm_broker_configured sc_vm_broker_alive sc_vm_broker_up \
    sc_vm_broker_down "$VM_BROKER_PID" "$VM_BROKER_UNIT"
  sc_restart_broker "ts-broker" \
    sc_ts_broker_configured sc_ts_broker_alive sc_ts_broker_up \
    sc_ts_broker_down "$TS_BROKER_PID" "$TS_BROKER_UNIT"
  sc_restart_broker "pm2-broker" \
    sc_pm2_broker_configured sc_pm2_broker_alive sc_pm2_broker_up \
    sc_pm2_broker_down "$PM2_BROKER_PID" "$PM2_BROKER_UNIT"
  sc_restart_broker "db-broker" \
    sc_db_broker_configured sc_db_broker_alive sc_db_broker_up \
    sc_db_broker_down "$DB_BROKER_PID" "$DB_BROKER_UNIT"
  if sc_pg_configured; then
    if sc_wait_until sc_pg_healthy; then
      echo "  postgres: restarted"
    else
      echo "  postgres: failed (unhealthy after restart)"
      SC_RESTART_FAILED=1
    fi
  else
    echo "  postgres: skipped (unconfigured)"
  fi
  [ "$SC_RESTART_FAILED" -eq 0 ]
}


cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-host ;;
  update)            exec "$PY" "$S/update.py" "$@" ;;
  # Refresh the harness CLIs the SHELLS run — which, on the docker path, means
  # the image and nothing else. Running the installers on the host here is what
  # this command used to do, and it reported success while changing nothing:
  # the container mounts harness state homes but image-owned launchers must
  # resolve image-owned binaries, and every launch `docker rm -f`s the writable
  # layer that an in-container install would land in. So: roll the epoch,
  # rebuild, and name the no-rebuild bounce that activates exactly this image.
  # Without docker the host IS the runtime, so the installers are correct there.
  update-harnesses)
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
      epoch="$(harness_epoch_roll)"
      echo "→ harness epoch rolled to $epoch"
      dbuild
      echo "→ image rebuilt with fresh harness CLIs"
      sc_harness_status || true
      echo "  running sandboxes keep the OLD image until they restart: ./sc restart --no-build"
    else
      echo "→ no docker — updating this host's harness CLIs (the no-docker runtime)"
      "$PY" "$S/install.py" --update-harnesses
    fi ;;
  harness-status)  sc_harness_status ;;
  rollback)     exec "$PY" "$S/rollback.py" "$@" ;;
  feature)      exec "$PY" "$S/feature.py" "$@" ;;
  artifact-mode) exec "$PY" "$S/artifact_policy.py" "$@" ;;
  eject)        exec "$PY" "$S/eject.py" "$@" ;;
  remove)       if sc_help_form "$@"; then
                  exec "$PY" "$CALLER_ENGINE/scripts/remove.py" "$@"
                fi
                sc_refuse_linked remove "$ROOT"
                exec "$PY" "$S/remove.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  # rebuild/migrate: the script owns the whole argument contract (help, unknown
  # tokens), so the dispatcher forwards VERBATIM and only inserts the refusal —
  # after the help question, before the action.
  rebuild)      sc_help_form "$@" || sc_refuse_linked rebuild "$DB"
                exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      sc_help_form "$@" || sc_refuse_linked migrate "$DB"
                exec "$PY" "$S/migrate.py" "$DB" "$@" ;;
  migration)    exec "$PY" "$CALLER_ENGINE/scripts/migration.py" "$@" ;;
  # snapshot/render name the artifact they would overwrite as well as the DB
  # they read; resolving that path costs a subprocess, so only the refusing
  # branch pays for it.
  snapshot)     if [ "$LINKED" -eq 1 ]; then
                  sc_refuse_linked snapshot \
                    "$DB -> $("$PY" "$S/artifact_policy.py" path content)"
                fi
                exec "$PY" "$S/snapshot.py" ;;
  mem)          exec "$PY" "$S/mem.py" "$@" ;;
  pr)           exec "$PY" "$S/pr_cli.py" "$@" ;;
  sprint)       exec "$PY" "$S/sprint_cli.py" "$@" ;;
  token)        exec "$PY" "$S/operator_token.py" "$@" ;;
  engine-ref)   sc_engine_ref_path="$LIVE_ROOT/.sc-state/engine.ref"
                if [ ! -r "$sc_engine_ref_path" ]; then
                  echo "✗ sc engine-ref: no readable engine pin at $sc_engine_ref_path" >&2
                  exit 1
                fi
                IFS= read -r sc_engine_ref < "$sc_engine_ref_path" || true
                case "$sc_engine_ref" in
                  ""|*[!0-9a-f]*)
                    echo "✗ sc engine-ref: invalid engine pin at $sc_engine_ref_path" >&2
                    exit 1 ;;
                esac
                if [ "${#sc_engine_ref}" -ne 40 ]; then
                  echo "✗ sc engine-ref: invalid engine pin at $sc_engine_ref_path" >&2
                  exit 1
                fi
                printf '%s\n' "$sc_engine_ref" ;;
  # ── persist (HOST-side): reboot-proof all applicable daemons via systemd ──
  persist)           sc_persist ;;
  # ── session-surviving local jobs: detached supervised one-shots whose
  # completion posts a result row to the starting shell's inbox ──
  job)               exec "$PY" "$S/job.py" "$@" ;;
  # Advisory viewport screenshots for fork apps (CI + local capture + init).
  visual-qa)         exec "$PY" "$S/visual_qa.py" "$@" ;;
  # Raw read passthrough to the engine + map DBs, resolved by absolute path so no
  # skill example ever needs a cwd-relative `sqlite3 .super-coder/…` (which pulls a
  # shell into `cd`-ing to the root — the cwd trap). Read-only is ENFORCED
  # (sqlite3 -readonly), matching the label: writes go via `sc mem`
  # (triggers/caps). The -rw variants are the explicit escape hatch for the few
  # procedures with no dedicated surface (direct skill INSERTs, cartographer
  # map authoring) — only use one where a skill names it. Skill grants have
  # their own surface now: `./sc skill`.
  sql)          exec sqlite3 -readonly "$DB" "$@" ;;
  map-sql)      exec sqlite3 -readonly "$MAPDB" "$@" ;;
  sql-rw)       exec sqlite3 "$DB" "$@" ;;
  map-sql-rw)   exec sqlite3 "$MAPDB" "$@" ;;
  render)       if [ "$LINKED" -eq 1 ]; then
                  sc_refuse_linked render \
                    "$DB -> $("$PY" "$S/artifact_policy.py" path renders)"
                fi
                [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  # render-check is SOURCE-PURE: it builds a throwaway DB from tracked text and
  # diffs the mirror, touching no live state. So it runs the CALLER's engine —
  # that is the source the caller is about to commit, and running the main
  # checkout's copy verified the wrong tree from every worktree. A missing caller
  # engine fails naming that path; falling back to live source would answer a
  # question nobody asked.
  render-check) if [ ! -f "$CALLER_ENGINE/scripts/render_check.py" ]; then
                  echo "✗ ./sc render-check: no engine source at $CALLER_ENGINE" >&2
                  echo "  render-check verifies THIS checkout's tracked sources; it does not" >&2
                  echo "  fall back to the live instance at $LIVE_ROOT." >&2
                  exit 1
                fi
                exec "$PY" "$CALLER_ENGINE/scripts/render_check.py" ;;
  map)          case "${1:-}" in
                  -h|--help) echo "usage: ./sc map — rescan the host repo into the dr_* catalogue ($MAPDB); takes no arguments"
                             exit 0 ;;
                  ?*)        echo "sc map: unknown argument '$1' (takes none; -h for usage)" >&2
                             exit 2 ;;
                esac
                exec "$PY" "$S/map_repo.py" ;;
  map-setup)    exec "$PY" "$S/map_setup.py" ;;
  # Token & session analytics — sweep each harness's on-disk usage data for
  # THIS repo into session_token_usage (incremental, idempotent; doc #11).
  analytics)    exec "$PY" "$S/analytics.py" "$@" ;;
  models)       exec "$PY" "$S/models.py" "$@" ;;
  # Like render-check, seed generation authors the CALLER's tracked engine
  # source. A linked source worktree must never regenerate the main checkout's
  # 0001 from a different branch's assets or upsert that shared live DB.
  seed-skills)  if [ ! -f "$CALLER_ENGINE/scripts/seed_skills.py" ]; then
                  echo "✗ ./sc seed-skills: no engine source at $CALLER_ENGINE" >&2
                  echo "  seed-skills authors THIS checkout's tracked catalogue; it does not" >&2
                  echo "  fall back to the live instance at $LIVE_ROOT." >&2
                  exit 1
                fi
                exec "$PY" "$CALLER_ENGINE/scripts/seed_skills.py" ;;
  # Skill catalogue write surface — grants/retirement by name, loud on a miss
  # (the raw-SQL grant's silent no-op class). Snapshot is still the persist step.
  skill)        exec "$PY" "$S/skill.py" "$@" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  url)          sc_urls ;;
  preview)      exec "$PY" "$S/preview.py" "$@" ;;
  # ── in-container primitives (no docker; also the host escape hatch) ──
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  # ── Windows VM broker (HOST-side primitive — runs where virsh + the key live) ──
  vm)                exec "$PY" "$S/vm.py" client "$@" ;;
  vm-broker)         exec "$PY" "$ENGINE/api/vm_broker.py" "$@" ;;
  # Bake/re-bake the clean snapshot — HOST-side, deliberately NOT a broker verb:
  # the snapshot is the trust anchor every test reverts to; a sandboxed shell may
  # run against it but must never redefine it. vm.py bake self-guards on SC_SANDBOX.
  vm-bake)           exec "$PY" "$S/vm.py" bake ;;
  vm-broker-up)      sc_vm_broker_up ;;
  vm-broker-down)    sc_vm_broker_down ;;
  vm-broker-sock)    exec "$PY" "$S/vm.py" sock ;;
  # In-sandbox half of the GUI seam (#263): TCP→unix relay used by managed
  # adapter injection after `./sc vm mcp up` brings the endpoint online. Runs
  # IN the container; the broker-side half is `POST /mcp/up` on vm-broker.
  vm-mcp-relay)      exec "$PY" "$S/vm_mcp_relay.py" "$@" ;;
  vm-broker-install)   sc_vm_broker_install ;;
  vm-broker-uninstall) sc_vm_broker_uninstall ;;
  # ── Tailnet broker (HOST-side primitive — runs where the tailnet node lives) ──
  ts-broker)         exec "$PY" "$ENGINE/api/ts_broker.py" "$@" ;;
  ts-broker-up)      sc_ts_broker_up ;;
  ts-broker-down)    sc_ts_broker_down ;;
  ts-broker-sock)    exec "$PY" "$S/ts.py" sock ;;
  ts-broker-install)   sc_ts_broker_install ;;
  ts-broker-uninstall) sc_ts_broker_uninstall ;;
  # ── pm2 broker (HOST-side primitive — runs where pm2 + the app live) ──
  pm2-broker)         exec "$PY" "$ENGINE/api/pm2_broker.py" "$@" ;;
  pm2-broker-up)      sc_pm2_broker_up ;;
  pm2-broker-down)    sc_pm2_broker_down ;;
  pm2-broker-sock)    exec "$PY" "$S/pm2.py" sock ;;
  pm2-broker-install)   sc_pm2_broker_install ;;
  pm2-broker-uninstall) sc_pm2_broker_uninstall ;;
  # ── db broker (HOST-side primitive — runs where the live DSN + route live) ──
  db-broker)         exec "$PY" "$ENGINE/api/db_broker.py" "$@" ;;
  db-broker-up)      sc_db_broker_up ;;
  db-broker-down)    sc_db_broker_down ;;
  db-broker-sock)    exec "$PY" "$S/dbq.py" sock ;;
  db-broker-install)   sc_db_broker_install ;;
  db-broker-uninstall) sc_db_broker_uninstall ;;
  db-init)      sc_db_init ;;
  # ── Postgres sidecar (app-only) ──
  pg-init)      sc_pg_init ;;
  pg-up)        sc_pg_up ;;
  pg-down)      sc_pg_down ;;
  boot)         exec "$PY" "$S/run.py" "$@" ;;
  boot-*)       exec "$PY" "$S/run.py" "${cmd#boot-}" "$@" ;;
  # Headless boot: same render-then-exec path as boot, minus the picker and
  # the TTY. In-container primitive like boot — the planner calls it to stand
  # up an ephemeral worker; also the no-docker host path (trusted bare metal).
  run)          export SC_TRUSTED_HOST=1 SC_DEV_PORT="$(devport)"
                exec "$PY" "$S/run.py" --headless "$@" ;;
  deps)         sc_deps "$@" ;;
  test)         sc_test "$@" ;;
  lint)         sc_lint "$@" ;;
  typecheck)    sc_typecheck "$@" ;;
  # ── bare-metal lifecycle (primary) ──
  # (The v1 standalone watch daemon is gone: sprint v2's PR watcher runs inside
  # the API server itself — sc_host_server_up is the whole host lifecycle.)
  launch)       sc_host_server_up ;;
  enter)        export SC_TRUSTED_HOST=1 SC_DEV_PORT="$(devport)"
                exec "$0" boot "$@" ;;
  enter-*)      export SC_TRUSTED_HOST=1 SC_DEV_PORT="$(devport)"
                exec "$0" boot "${cmd#enter-}" "$@" ;;
  down)         sc_host_server_down ;;
  restart)      sc_db_backup prerestart
                "$0" down
                exec "$0" launch "$@" ;;
  logs)         [ -f "$HOST_SERVER_LOG" ] || {
                  echo "✗ no host server log yet — run ./sc launch" >&2; exit 1; }
                exec tail -f "$HOST_SERVER_LOG" ;;

  # ── docker sandbox (optional compatibility lifecycle) ──
  sandbox-launch)
    no_build=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --no-build) no_build=1 ;;
        -h|--help)
          echo "usage: ./sc sandbox-launch [--no-build]"
          echo "  --no-build  reuse the existing $IMG:latest image; refuse if absent"
          exit 0 ;;
        *)
          echo "sc sandbox-launch: unknown argument '$1' (usage: ./sc sandbox-launch [--no-build])" >&2
          exit 2 ;;
      esac
      shift
    done
    dcheck
    if [ -n "$no_build" ]; then dimage_preflight; else dbuild; fi
    dcreds
    "$PY" "$S/ports.py" ensure >/dev/null
    p="$(port)"
    dp="$(devport)"
    dnet
    # Forward GitHub auth for shells opening their own feature PRs. Prefer a
    # repo-scoped SC_GH_TOKEN; else reuse the
    # host's gh login. NOTE: this widens the sandbox — anything in the container
    # can act as you on GitHub within the token's scope. A fine-grained,
    # single-repo PAT in SC_GH_TOKEN is the tighter option.
    gh_token="${SC_GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
    # Forward a Mistral key for vibe's API-key auth path — ONLY when set, so an
    # empty value can't shadow the mounted ~/.vibe creds (vibe --setup stores its
    # key + .env there; the mount below carries them in like every other harness).
    mistral_env=""
    [ -n "${MISTRAL_API_KEY:-}" ] && mistral_env="-e MISTRAL_API_KEY=${MISTRAL_API_KEY}"
    # Forward DATABASE_URL into the sandbox when a pg sidecar is configured, so
    # the fork's APP can connect to it. Default tracks the sidecar's container
    # name + baked sc/sc/sc creds (one source of truth); SC_DATABASE_URL overrides
    # for a fork whose sidecar differs. The hostname is the CONTAINER name (DNS on
    # SC_NET) — NOT 127.0.0.1, which inside the sandbox is its own loopback. The
    # engine ignores this var (SQLite-only); only the app reads it. Sidecar is
    # started after the sandbox (sc_pg_up below); the app connects lazily, so order
    # doesn't matter.
    pg_env=""
    sc_pg_configured && pg_env="-e DATABASE_URL=${SC_DATABASE_URL:-postgresql://sc:sc@$PGNAME:5432/sc}"
    git_name="$(git -C "$here" config user.name 2>/dev/null || true)"
    git_email="$(git -C "$here" config user.email 2>/dev/null || true)"
    # Pinned-interpreter passthrough. When the fork's .venv was built from an
    # out-of-tree interpreter — a uv-managed standalone CPython under $HOME, used
    # to pin the app's Python independent of the host's rolling system python —
    # the bind-mounted .venv's bin/python + script shebangs point at that
    # interpreter by absolute path. Mount it read-only at the SAME path so the
    # shared .venv runs end-to-end inside the sandbox on the *identical* binary:
    # same ABI as the wheels the host installed (psycopg etc. import with zero
    # rebuild), and .venv/bin/{python,pytest,ruff,mypy} all resolve. Engine
    # python (the image's own python3, SQLite-only) is untouched — this is the
    # product app's interpreter, a separate concern. Skipped when the venv's
    # interpreter is a system path (don't shadow /usr) or already inside the repo
    # mount; a fork with a plain `python3 -m venv` host venv gets nothing here.
    py_mount=""
    if [ -e "$here/.venv/bin/python" ]; then
      pybin="$(readlink -f "$here/.venv/bin/python" 2>/dev/null || true)"
      case "$pybin" in
        "$here"/*) : ;;                         # already under the repo bind-mount
        "$HOME"/*)
          # The venv's bin/python is symlinked to its interpreter by absolute
          # path, but that path is usually a minor-version ALIAS dir that
          # readlink -f collapses away (uv: cpython-3.14-… → cpython-3.14.5-…;
          # the venv pins the alias so it floats across patch bumps). Mounting
          # just the resolved dir leaves the alias path missing in the container
          # and .venv/bin/python dangles. So mount the interpreter REGISTRY — the
          # parent of the version dir, e.g. ~/.local/share/uv/python — so both the
          # alias symlink and the real dir are present and every venv symlink
          # resolves. A flat standalone (no version dir under $HOME) has no usable
          # registry, so fall back to mounting its root directly.
          pyver_dir="$(dirname "$(dirname "$pybin")")"   # <registry>/<versiondir>/bin/python → <versiondir>
          pyreg="$(dirname "$pyver_dir")"                # <registry> (holds the alias + the real dir)
          if [ -d "$pyreg" ] && [ "$pyreg" != "$HOME" ]; then
            py_mount="-v $pyreg:$pyreg:ro"
          elif [ -d "$pyver_dir" ]; then
            py_mount="-v $pyver_dir:$pyver_dir:ro"
          fi ;;
      esac
    fi
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
    # Docker's init shim is PID 1 so orphaned harness/worker subprocesses are
    # reaped. Without it the Python API server becomes PID 1, never wait()s on
    # reparented children, and long-running multi-shell work exhausts the
    # container's PID limit with zombies (flag #323).
    docker run -d --name "$CNAME" --restart unless-stopped --init \
      --network "$SC_NET" \
      --user "$(duser)" \
      -e HOME="$HOME" -e SC_BIND=0.0.0.0 -e SC_PYTHON=python3 -e PYTHONUNBUFFERED=1 \
      -e SC_SANDBOX=1 -e SC_DEV_PORT="$dp" \
      -e GH_TOKEN="$gh_token" $mistral_env $pg_env \
      -e GIT_AUTHOR_NAME="$git_name" -e GIT_AUTHOR_EMAIL="$git_email" \
      -e GIT_COMMITTER_NAME="$git_name" -e GIT_COMMITTER_EMAIL="$git_email" \
      -w "$here" \
      -v "$here:$here" \
      $py_mount \
      -v "$HOME/.claude:$HOME/.claude" \
      -v "$HOME/.claude.json:$HOME/.claude.json" \
      -v "$HOME/.config/opencode:$HOME/.config/opencode" \
      -v "$HOME/.local/share/opencode:$HOME/.local/share/opencode" \
      -v "$HOME/.codex:$HOME/.codex" \
      -v "$HOME/.vibe:$HOME/.vibe" \
      -v "$HOME/.kimi-code:$HOME/.kimi-code" \
      -p "127.0.0.1:$p:$p" \
      -p "127.0.0.1:$dp:$dp" \
      "$IMG" ./sc serve --port "$p" >/dev/null
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
      printf '\033[1m→ sandbox up\033[0m · \033[1mReview GUI  \033[36mhttp://127.0.0.1:%s\033[0m\n' "$p"
    else
      echo "→ sandbox up · review GUI at http://127.0.0.1:$p"
    fi
    echo "  dev server:    bind 0.0.0.0:$dp inside (\$SC_DEV_PORT) → http://127.0.0.1:$dp"
    echo "  boot a shell:  ./sc sandbox-enter   (or ./sc sandbox-enter-<shortname>)"
    # One line naming the claude build the shells got. It is the version that
    # decides which models an alias like `opus` can resolve to, and until this
    # line existed nothing anywhere reported it — a sandbox stuck one release
    # behind a new model looked identical to a current one. Best-effort: a probe
    # that cannot answer must not fail a launch that otherwise succeeded.
    claude_v="$(docker exec "$CNAME" claude --version 2>/dev/null | head -1 || true)"
    [ -n "$claude_v" ] && echo "  harnesses:     claude $claude_v   (all: ./sc harness-status)"
    # Bring the VM broker up alongside the sandbox when a VM is linked (self-skips
    # otherwise, and no-ops if systemd already owns it). The shells need it to
    # drive the VM; this keeps it from being a forgotten manual step.
    sc_vm_broker_up || true
    # Same for the tailnet broker — self-skips when no `ts` block is linked.
    sc_ts_broker_up || true
    # Same for the pm2 broker — self-skips when no `pm2` block is linked.
    sc_pm2_broker_up || true
    # Same for the read-only DB broker — it was previously omitted from the
    # sandbox lifecycle, so a restart could leave configured diagnostics down.
    sc_db_broker_up || true
    # Start the PG sidecar when configured — self-skips otherwise.
    sc_pg_up || true ;;
  sandbox-enter)        sc_urls || true; exec docker exec -it "$CNAME" ./sc boot "$@" ;;
  sandbox-enter-*)      sc_urls || true; exec docker exec -it "$CNAME" ./sc boot "${cmd#sandbox-enter-}" "$@" ;;
  sandbox-down)         docker rm -f "$CNAME" >/dev/null 2>&1 && echo "→ sandbox stopped" || echo "→ not running"
                sc_vm_broker_down
                sc_ts_broker_down
                sc_pm2_broker_down
                sc_db_broker_down
                sc_pg_down ;;
  # restart is a hard bounce — down runs `docker rm -f`, which SIGKILLs every
  # live session inside the sandbox along with whatever those sessions had not
  # yet written to the DB. Too easy to reach by accident (dos-r sits next to
  # dos-e), so: typed confirmation (only YES / Yes / yes proceed — anything
  # else, including a closed stdin, aborts) + a WAL-safe DB backup BEFORE
  # anything is torn down. --yes/-y skips the prompt for scripted callers.
  # --no-build validates and deliberately reuses the existing image. The
  # default path gives every harness installer a fresh cache key, then completes
  # that build before down, so a network/install failure cannot strand a
  # healthy fork offline.
  sandbox-restart)
    assume_yes=""
    no_build=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -y|--yes) assume_yes=1 ;;
        --no-build) no_build=1 ;;
        -h|--help)
          echo "usage: ./sc sandbox-restart [-y|--yes] [--no-build]"
          echo "  default     refresh harness CLIs, rebuild the image, then bounce"
          echo "  --no-build  reuse the existing $IMG:latest image; preflight before down"
          exit 0 ;;
        *)
          echo "sc sandbox-restart: unknown argument '$1' (usage: ./sc sandbox-restart [-y|--yes] [--no-build])" >&2
          exit 2 ;;
      esac
      shift
    done
    if [ -z "$assume_yes" ]; then
      echo "restart recreates the sandbox — live sessions inside it are killed."
      printf "ARE YOU SURE YOU WANT TO RESTART? (YES/no): "
      ans=""; read -r ans || true
      case "$ans" in
        YES|Yes|yes) ;;
        *) echo "→ restart aborted (nothing touched)"; exit 1 ;;
      esac
    fi
    dcheck
    if [ -n "$no_build" ]; then
      dimage_preflight
    else
      epoch="$(harness_epoch_roll)"
      echo "→ refresh harnesses for restart (epoch $epoch)"
      dbuild
    fi
    backup_dir="$(sc_db_backup_preflight)"
    sc_db_backup prerestart "$backup_dir"
    if ! "$0" sandbox-down; then
      echo "✗ restart stopped: teardown did not complete; no replacement services were launched." >&2
      exit 1
    fi
    launch_rc=0
    "$0" sandbox-launch --no-build || launch_rc=$?
    sc_restart_health_summary "$launch_rc" ;;
  # --harnesses expires the baked harness CLIs first, so the build re-runs their
  # installers instead of serving them from a cache that has no expiry of its own.
  build|sandbox-build)
    dcheck
    case "${1:-}" in
      --harnesses)
        epoch="$(harness_epoch_roll)"
        echo "→ harness epoch rolled to $epoch"
        shift ;;
      "") : ;;
      *) echo "sc build: unknown argument '$1' (usage: ./sc build [--harnesses])" >&2; exit 2 ;;
    esac
    dbuild ;;
  sandbox-logs) exec docker logs -f "$CNAME" ;;
  verify)
    sc_refuse_linked verify "$DB"
    # Destructive by design: rebuild.py REPLACES the DB below. Say which DB and
    # which source before that happens — a footer printed after the fact is a
    # disclosure a crash can skip, and this is the command that eats unsnapshotted
    # memory when it is pointed at an instance the caller did not mean.
    echo "→ verify: about to REBUILD $DB"
    echo "          from engine source $ENGINE"
    "$PY" "$S/rebuild.py"
    # The engine source intentionally carries no per-instance snapshot in local
    # artifact mode. Exercise the real fresh-fork initialization path before
    # the headless boot when rebuild therefore produced an empty instance.
    if "$PY" - "$DB" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
try:
    populated = con.execute(
        "SELECT EXISTS(SELECT 1 FROM users WHERE is_active=1) "
        "AND EXISTS(SELECT 1 FROM shells WHERE COALESCE(is_deleted,0)=0)"
    ).fetchone()[0]
finally:
    con.close()
raise SystemExit(0 if populated else 1)
PY
    then
      :
    else
      "$PY" "$S/init_fork.py" --username verify
    fi
    SC_ADMIN=1 "$PY" "$S/render.py" flat
    RENDER_ONLY=1 exec "$PY" "$S/run.py" --first ;;
  health)       curl -s "http://127.0.0.1:$(port)/api/health" && echo "" ;;
  clean-db)     sc_refuse_linked clean-db "$DB"
                rm -f "$DB" "$DB-wal" "$DB-shm" && echo "removed $DB (rebuild with: ./sc rebuild)" ;;
  help|-h|--help)
    cat <<'EOF'
super-coder — forkable shell substrate

  ./sc install             first-launch bootstrap for a fork (requirements, harness, first shell)
  ./sc ensure-harness      install claude + opencode + codex + vibe + kimi if missing (official native installers, no npm)
  ./sc doctor              bare-metal readiness: host tools + harness login
  ./sc update              fetch + materialize the engine (gitignored dep) + reconcile IN PLACE (migrate, sync skills, map);
                             --no-fetch skips the fetch · --ref <tag|sha> pins a version · blocks on local engine edits (--force discards them)
                             first runs git pull --ff-only for any tracked checkout; source repos then reconcile FROM that tree.
                             Advisory, never blocking: an unsafe/offline pull WARNS and engine update continues from the current
                             checkout. Update never merges, rebases or resets. --no-fetch skips checkout and engine network sync.
  ./sc update-harnesses    refresh the harness CLIs the SHELLS run: rolls the harness epoch + rebuilds the sandbox image
                             (they are image-owned — activate that exact build with ./sc restart --no-build)
                             without docker, updates this host's CLIs instead — there the host IS the runtime
  ./sc harness-status      report the harness CLI versions inside the sandbox + whether the image owes a harness rebuild
                             (a model the shells cannot reach is nearly always this — see .super-coder/docs/harness-freshness.md)
  ./sc rollback            sound undo of a bad update — restore the DB + engine (engine.ref.prev) together
                             --engine-only repairs a new-engine / unchanged-old-DB half floor without restoring a DB backup
  ./sc feature             list the opt-in features (pg · windows · tailnet · pm2 · app-deploy) and the state of both halves (config block + skill grants)
  ./sc feature enable <f>  enable one: grant its skills to the owning flavors + create/point-at its instance.json block (disable reverses)
  ./sc eject               ONE-WAY: stop tracking upstream and own the engine — un-gitignore + stage .super-coder/ as fork source (confirm-gated)
  ./sc remove              safely uninstall subfloor from this repo after a verified DB backup
                             --dry-run previews; --yes skips confirmation, never safety gates
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc migration new <slug>
                           allocate the next free migration number, write the standard skeleton, and update the source removal-test allowlist
  ./sc snapshot            dump per-instance tables to gitignored .sc-state/local/
                             live-state commands (rebuild · migrate · verify · snapshot · render · clean-db) act on the SHARED live
                             instance at the main checkout, so they REFUSE from a linked worktree rather than substitute it, naming
                             the target declined (decision #81); -h/--help still answers from any checkout. render-check is the
                             source-pure one: it verifies the CALLER's engine sources and local artifacts, and names the checkout it read.
  ./sc mem <cmd> [args]    a shell's own memory, over the engine API (get/state/seed/lns/decision/flag/roadmap/doc/narrative);
                             already wired to this launched shell, identity resolved by the engine — no DB path, no direct-DB fallback. `./sc mem which` to orient
  ./sc pr subscribe --repository <owner/name> --pr <number>
                           subscribe the authenticated Developer shell to engine-wide PR event wakes
  ./sc sprint <cmd>        authenticated Sprints v2 actions (run without a command for the full verb list)
                             caller identity is resolved by the engine; report and review bodies use files, and mutating retries carry stable keys where required
  ./sc token               print the browser sign-in operator token (an operator capability: the Admin runtime
                             credential from the owner-only artifact .super-coder/run/mem/<shortname>.json, mode 0600)
                             — stdout carries ONLY the token, for paste into the browser sign-in prompt. Never
                             rotates; a missing/unreadable/insecure artifact refuses on stderr with the service
                             action (`./sc restart` / `make dos-r`). A recovery path — the browser attaches its
                             own credential, so pasting a token by hand is no longer the everyday sign-in
  sc engine-ref            print the full engine pin from the canonical live checkout — safe from any shell
                             worktree; stdout is the 40-character SHA only
  ./sc job start -- <cmd>  run a long local command (suite/bench/build) detached + supervised — it
                             survives your session; completion lands in YOUR inbox as a result row
                             (--label <slug> names it, --timeout <s> kills the wedged process group)
  ./sc job wait <id>       bounded foreground wait, ≤550s slice — exit 0 done · 2 still running
                             (drain your inbox between slices); list/status/tail/kill complete the set
  ./sc models refresh      refresh local model routes (same action as Shells → Refresh models)
  ./sc models resolve <h> <model> [--shell <shortname>]
                             print one exact, locally runnable high-effort call; list [harness] shows routes
  ./sc visual-qa <mode>    viewport screenshot QA: ci boots/captures · run captures a local app · init scaffolds config
  sc sql "<query>"         read-only passthrough to the engine DB (schema/skills/flags) — absolute path, cwd-independent (no `cd` to root)
  sc map-sql "<query>"     read-only passthrough to the repo-map DB (dr_* catalogue) — absolute path, cwd-independent
  sc sql-rw · sc map-sql-rw
                           read-WRITE passthroughs — bypass the API's triggers/caps; `sc mem` is the write path.
                             Only for procedures with no API surface (map authoring) where a skill names it
  ./sc skill <cmd>         skill catalogue surface: list · grant <name> <shell>... · revoke <name> <shell>... · rm <name> · retire <name> · unretire <name>
                             shells by id or shortname; rm refuses engine skills — retire/unretire manages the fork retire
                             list (active tracked/local retire path, rides updates); snapshot after writes to persist
  ./sc artifact-mode       inspect the local-only artifact paths (mode switching is retired)
  ./sc render              render flat _sc files under the active artifact policy
  ./sc render-check        fail if the active flat _sc files drift from the DB render (hermetic check)
  ./sc analytics sweep     parse each harness's on-disk token usage for this repo into session_token_usage
                             (incremental + idempotent; --harness <name> · --quiet · --full re-parses everything).
                             Also runs at boot and behind the GUI Analytics tab
  ./sc map                 scan the host repo into the dr_* catalogue (re-runnable)
  ./sc map-setup           wire the auto-remap git hooks (core.hooksPath) + map — the cartographer's one-shot
  ./sc seed-skills         upsert assets/skills/ into the live DB (+ regenerate the seed migration — source repo only)
  ./sc init                seed a fresh fork's first user + shell (run once after install)

  Bare metal (the primary lifecycle; harnesses run trusted on the host):
  ./sc launch              start the host review server + GUI, 127.0.0.1 only
  ./sc enter               start an interactive session: auth + pick shell + pick harness + boot
  ./sc enter-<shortname>   attach + boot that shell directly (skip the shell picker)
                             harness: --harness <name> or HARNESS=<name> forces it; else when
                             >1 harness is on PATH you're prompted (per-launch, not persisted)
  make dos-help            supported operator aliases for lifecycle, models,
                             job, maintenance, browser token, and generic ./sc forwarding
  ./sc run <shortname>     headless boot: render + exec the harness NON-interactively (claude · codex ·
                             opencode · kimi) to drain the shell's inbox and act; -p "<prompt>" overrides the
                             default prompt · --harness <h> · -m <model> (else flavor_defaults);
                             --effort defaults to high; refuses a shell that already has a live session
  ./sc down                stop the host review server
  ./sc restart             DB backup, then restart the host services
  ./sc logs                tail the host review-server log

  Optional Docker compatibility:
  ./sc sandbox-launch      build + start the Docker sandbox
  ./sc sandbox-enter       attach + boot inside the Docker sandbox
  ./sc sandbox-down        stop + remove the Docker sandbox
  ./sc sandbox-restart     confirm (YES) + DB backup, then recreate the sandbox
  ./sc sandbox-build       (re)build the sandbox image
  ./sc sandbox-logs        tail the sandbox server logs

  Primitives:
  ./sc serve               run the review layer (api + static UI) in the foreground
  ./sc boot [shortname]    direct interactive launch (host/no-docker primitive)
  ./sc deps                install this fork's python (.venv) + node (node_modules) deps into the bind-mount
                             (plus an only-if-needed dev kit: pytest httpx coverage ruff mypy datasette)
  ./sc test                run backend (.venv pytest or stdlib unittest) + UI (vitest) suites; non-zero on any failure
  ./sc lint [paths]        ruff check the fork's python (.venv ruff; honors [tool.ruff]) — available, not enforced
  ./sc typecheck [paths]   mypy the fork's python (.venv mypy; honors [tool.mypy]) — available, not enforced

  Windows VM broker (run on the HOST — drives the test VM for sandboxed forks;
  holds the ssh key + virsh so the fork never does. See .super-coder/docs/windows-vm-broker.md).
  `launch` brings it up automatically when a VM is linked; `down` stops it:
  ./sc vm status [--json] read broker, VM, SSH, and MCP-tunnel state without mutation;
                           includes relay, endpoint, and active-adapter state
  ./sc vm start [--json]  start only when off, then wait within a bounded SSH-readiness budget
  ./sc vm push SRC [DEST] [--json]
                           stage a permitted local artifact through the configured transfer directory
  ./sc vm exec [--command-file FILE] [--json] -- COMMAND...
                           execute one guest command through SSH without caller-built JSON
  ./sc vm capture [--output PATH] [--json]
                           save a validated screenshot artifact and return viewable metadata
  ./sc vm mcp status|up|down [--json]
                           inspect, start+verify, or stop the managed MCP tunnel and relay
  ./sc vm reset --off [--json]
                           restore the testing snapshot and confirm the VM is powered off
  ./sc vm-broker           run the broker in the foreground (unix socket)
  ./sc vm-bake             HOST-side: graceful shutdown + (re)bake the clean snapshot after provisioning
                             (deliberately NOT a broker verb — the sandbox must never redefine 'clean')
  ./sc vm-broker-up        start it in the background (nohup + pidfile); self-skips if unlinked/already up
  ./sc vm-broker-down      stop the backgrounded broker
  ./sc vm-broker-sock      print the broker's socket path
  ./sc vm-broker-install   supervise via a systemd --user unit (survives logout/reboot)
  ./sc vm-broker-uninstall remove the systemd unit
  ./sc vm-mcp-relay        in-SANDBOX half of the GUI seam: up [port] / down / status —
                             TCP 127.0.0.1:18000 → the broker's vm-mcp.sock tunnel;
                             managed adapter injection supplies the harness definition and
                             `./sc vm mcp up` brings the endpoint online

  Tailnet broker (run on the HOST — drives the tailnet for sandboxed forks; holds
  the already-`tailscale up` node so the fork never holds a tailnet credential.
  See .super-coder/docs/tailscale-broker.md). `launch` brings it up when a tailnet is linked:
  ./sc ts-broker           run the broker in the foreground (unix socket)
  ./sc ts-broker-up        start it in the background (nohup + pidfile); self-skips if unlinked/already up
  ./sc ts-broker-down      stop the backgrounded broker
  ./sc ts-broker-sock      print the broker's socket path
  ./sc ts-broker-install   supervise via a systemd --user unit (survives logout/reboot)
  ./sc ts-broker-uninstall remove the systemd unit

  pm2 broker (run on the HOST — lets a sandboxed shell observe + manage the
  host's pm2-supervised app stack: status, health, logs, restart — fail-closed
  on the `pm2` block's `processes` allowlist. See .super-coder/docs/pm2-broker.md).
  `launch` brings it up when a stack is linked:
  ./sc pm2-broker          run the broker in the foreground (unix socket)
  ./sc pm2-broker-up       start it in the background (nohup + pidfile); self-skips if unlinked/already up
  ./sc pm2-broker-down     stop the backgrounded broker
  ./sc pm2-broker-sock     print the broker's socket path
  ./sc pm2-broker-install  supervise via a systemd --user unit (survives logout/reboot)
  ./sc pm2-broker-uninstall remove the systemd unit

  db broker (run on the HOST — read-only diagnostic reads of the fork's LIVE app
  DB for a sandboxed shell, without handing it a DSN or a route. Shells out to
  psql host-side; SELECT-only + table allowlist + row cap; the DSN must be a
  read-only role. One-time: ./sc db-init. See .super-coder/docs/db-broker.md.
  ./sc db-init             add the "db" block to instance.json + print host setup steps
  ./sc db-broker           run the broker in the foreground (unix socket)
  ./sc db-broker-up        start it in the background (nohup + pidfile); self-skips if unlinked/already up
  ./sc db-broker-down      stop the backgrounded broker
  ./sc db-broker-sock      print the broker's socket path
  ./sc db-broker-install   supervise via a systemd --user unit (survives logout/reboot)
  ./sc db-broker-uninstall remove the systemd unit

  Persist (HOST-side — reboot-proof the fork in one verb; #359): installs the
  systemd --user unit for every daemon that applies here (vm/ts/pm2/db brokers
  when linked), enables linger, skips the rest with a reason. Idempotent:
  ./sc persist             install + enable --now every applicable unit

  Postgres sidecar (app-only; docker container on SC_NET, data in a named volume).
  For developing/testing the fork's APP against real Postgres in the sandbox — the
  engine DB stays SQLite. One-time: ./sc pg-init (adds "pg" to instance.json).
  `launch` starts it + forwards DATABASE_URL (override with SC_DATABASE_URL); `down` stops it:
  ./sc pg-init             add the "pg" key to instance.json (enables the sidecar)
  ./sc pg-up               start the postgres:17 container; self-skips if unconfigured/already up
  ./sc pg-down             stop + remove the container (data volume retained)
                             (recreate via pg-down→pg-up to change --shm-size / SC_PG_SHM)

  ./sc verify              rebuild + flat render + render-only boot (headless proof)
  ./sc health              curl the review layer's /api/health
  ./sc ports               show this fork's derived port
  ./sc url                 print this fork's review GUI + dev-server URLs (derived, never a fixed 8800)
                             — the recall path when the boot summary has scrolled away. Alias: make dos-url
  ./sc preview             live-preview every dev shell's worktree UI on one port,
                             routed by subdomain (http://<shortname>.localhost:<dev_port>/)
  ./sc clean-db            remove the rebuilt .db (text serializations untouched)
EOF
    ;;
  *) echo "sc: unknown command '$cmd' (try ./sc help)" >&2; exit 2 ;;
esac
