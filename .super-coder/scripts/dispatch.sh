#!/bin/sh
# super-coder dispatcher body — engine-owned, materialized by `./sc update`
# (spec #105, single-owner dispatcher). The tracked `sc` at the repo root is a
# thin bootstrap: it resolves the LIVE engine and execs this file with
# SC_CALLER_ROOT exported. Keeping the verb table here — inside the engine —
# means a stale committed bootstrap on an old shell branch can never hide a
# verb the live floor carries. Everything below the identity block is the
# dispatcher exactly as it lived in `sc`. Run via:  ./sc <command> [args]
set -e

# Caller identity arrives from the bootstrap. Invoked directly (tests, or a
# maintainer running an edited body without the bootstrap), fall back to the
# checkout holding this engine — one identity, same as a standalone root.
if [ -n "${SC_CALLER_ROOT:-}" ] && [ -d "${SC_CALLER_ROOT:-}" ]; then
  here="$(CDPATH= cd -- "$SC_CALLER_ROOT" && pwd)"
else
  here="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
fi
# SC_CALLER_ROOT is a one-hop bootstrap projection and is not part of nested
# command execution.
unset SC_CALLER_ROOT
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
S="$ENGINE/scripts"
MAPDB=""

sc_engine_db() {
  "$PY" "$S/instance_state.py" active-database "$ENGINE"
}

sc_require_supported_host() {
  SC_PLATFORM_KERNEL="$(command -p uname -s 2>/dev/null || true)"
  [ "$SC_PLATFORM_KERNEL" = Linux ] || sc_platform_unsupported
}

sc_platform_unsupported() {
  {
    echo '✗ subfloor refused: unsupported host.'
    echo "  detected kernel: ${SC_PLATFORM_KERNEL:-unknown}"
    echo '  subfloor runs on Linux.'
    echo '  Create a Linux VM, keep the checkout on the guest filesystem, then run ./sc install inside the guest.'
    echo '  The rejected command was not run and no native compatibility path exists.'
  } >&2
  exit 1
}

sc_python_recovery() {
  echo '  recovery: install Python 3.14.x with sqlite3, then:' >&2
  echo '            export SC_PYTHON=/absolute/path/to/python3' >&2
}

sc_python_probe() {
  requested="$PY"
  resolved="$(command -v "$requested" 2>/dev/null || true)"
  if [ -z "$resolved" ] || [ ! -x "$resolved" ]; then
    if [ -n "${SC_PYTHON:-}" ]; then
      echo "✗ host Python preflight: SC_PYTHON '$SC_PYTHON' is not executable." >&2
    else
      echo "✗ host Python preflight: python3 is not executable on PATH." >&2
    fi
    sc_python_recovery
    exit 1
  fi
  probe="$("$resolved" -c '
import os
import platform
import sys

executable = os.path.realpath(sys.executable)
version = platform.python_version()
if sys.version_info[:2] != (3, 14):
    print("Python 3.14.x required; {} reports {}".format(executable, version))
    raise SystemExit(2)
try:
    import sqlite3
except ImportError:
    print("{} ({}) cannot import sqlite3".format(executable, version))
    raise SystemExit(3)
print("{}|{}|{}".format(executable, version, sqlite3.sqlite_version))
' 2>&1)" || {
    echo "✗ host Python preflight failed for '$requested' ($resolved):" >&2
    printf '  %s\n' "$probe" >&2
    sc_python_recovery
    exit 1
  }
  SC_PYTHON_EXECUTABLE="${probe%%|*}"
  SC_PYTHON_RUNTIME="${probe#*|}"
  PY="$resolved"
  SC_PYTHON="$resolved"
  export PY SC_PYTHON SC_PYTHON_EXECUTABLE SC_PYTHON_RUNTIME
}

sc_mapdb() {
  if [ -z "$MAPDB" ]; then
    MAPDB="$("$PY" "$S/artifact_policy.py" path map-db)"
  fi
  printf '%s\n' "$MAPDB"
}

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

# ── Runtime selection (sandbox | host) ───────────────────────────────────────
# One instance.json key (`runtime`, scripts/runtime.py) decides which lifecycle
# the docker verbs drive. `host` runs the review server as a supervised host
# process (nohup + pidfile under .super-coder/run/) and boots shells directly
# on this host — no daemon, image, or container anywhere in the path. An absent
# key reads as `sandbox`, so every existing install keeps its behavior.
# `./sc install --runtime host` or `./sc runtime host` selects it.
sc_runtime() { "$PY" "$S/runtime.py" get 2>/dev/null || echo sandbox; }
sc_host_runtime() { [ "$(sc_runtime)" = host ]; }

HOST_SERVER_PID="$ENGINE/run/server.pid"
HOST_SERVER_LOG="$ENGINE/run/server.log"

sc_host_api_healthy() {
  curl -fsS "http://127.0.0.1:$(port)/api/health" >/dev/null 2>&1
}
# Print the pid from the pidfile only while it is alive AND still this fork's
# review server — a recycled pid belonging to something else is never ours to
# signal. Silent failure otherwise.
sc_host_server_pid() {
  [ -f "$HOST_SERVER_PID" ] || return 1
  host_pid="$(sed -n '1p' "$HOST_SERVER_PID" 2>/dev/null || true)"
  [ -n "$host_pid" ] && kill -0 "$host_pid" 2>/dev/null || return 1
  ps -p "$host_pid" -o args= 2>/dev/null | grep -q "api/server\.py" || return 1
  printf '%s\n' "$host_pid"
}
sc_host_server_alive() { sc_host_server_pid >/dev/null; }
sc_host_server_up() {
  "$PY" "$S/ports.py" ensure >/dev/null
  host_port="$(port)"
  if sc_host_server_alive; then
    echo "→ host review server already running (pid $(sc_host_server_pid)) · http://127.0.0.1:$host_port"
    return 0
  fi
  if sc_host_api_healthy; then
    echo "✗ something already answers http://127.0.0.1:$host_port/api/health but ./sc launch did not start it." >&2
    echo "  a sandbox from before the switch (./sc runtime sandbox; ./sc down) or a foreground" >&2
    echo "  ./sc serve — stop it, then retry ./sc launch" >&2
    return 1
  fi
  if ! mkdir -p "$ENGINE/run" 2>/dev/null || [ ! -w "$ENGINE/run" ]; then
    echo "✗ host-runtime: $ENGINE/run is not writable by $(id -un)" >&2
    echo "  fix: sudo chown -R $(id -u):$(id -g) '$ENGINE/run'" >&2
    return 1
  fi
  rm -f "$HOST_SERVER_PID"
  nohup env SC_BIND=127.0.0.1 PYTHONUNBUFFERED=1 \
    "$PY" "$ENGINE/api/server.py" --port "$host_port" >"$HOST_SERVER_LOG" 2>&1 &
  host_pid=$!
  printf '%s\n' "$host_pid" > "$HOST_SERVER_PID"
  attempts=0
  while [ "$attempts" -lt 40 ]; do
    if sc_host_api_healthy; then
      echo "→ host review server up (pid $host_pid) · http://127.0.0.1:$host_port · log $HOST_SERVER_LOG"
      return 0
    fi
    kill -0 "$host_pid" 2>/dev/null || break
    attempts=$((attempts + 1))
    sleep 0.25
  done
  echo "✗ host review server did not become healthy; see $HOST_SERVER_LOG" >&2
  kill "$host_pid" 2>/dev/null || true
  rm -f "$HOST_SERVER_PID"
  return 1
}
# Stops only what `launch` started (the pidfile). A server that answers on the
# port without our pidfile — a foreground `./sc serve`, a systemd unit — is
# reported and left alone, the same contract the brokers keep.
sc_host_server_down() {
  host_pid="$(sc_host_server_pid || true)"
  if [ -n "$host_pid" ]; then
    kill "$host_pid" 2>/dev/null || true
    attempts=0
    while kill -0 "$host_pid" 2>/dev/null && [ "$attempts" -lt 40 ]; do
      attempts=$((attempts + 1))
      sleep 0.25
    done
    if kill -0 "$host_pid" 2>/dev/null; then
      echo "✗ host review server (pid $host_pid) did not stop after SIGTERM" >&2
      return 1
    fi
    echo "→ host review server stopped"
  elif sc_host_api_healthy; then
    echo "→ a review server answers on 127.0.0.1:$(port) but ./sc launch did not start it — leaving it"
  else
    echo "→ host review server not running"
  fi
  rm -f "$HOST_SERVER_PID"
}
# Host-runtime entry: the shell boots on this host through run.py, the same
# primitive the sandbox runs inside the container. $1 = shortname ("" = picker).
sc_host_enter() {
  host_shortname="$1"
  shift
  if [ "${1:-}" = "--devkit-repair" ]; then
    echo "sc enter: --devkit-repair repairs sandbox image provisioning; this install's runtime is host" >&2
    exit 2
  fi
  if ! sc_host_api_healthy; then
    echo "✗ host review server is not answering on 127.0.0.1:$(port) — ./sc launch first" >&2
    exit 1
  fi
  sc_urls || true
  SC_DEV_PORT="$(devport)"
  export SC_DEV_PORT
  if [ -n "$host_shortname" ]; then
    exec "$PY" "$S/run.py" "$host_shortname" "$@"
  fi
  exec "$PY" "$S/run.py" "$@"
}

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
    echo "  Setup (one-time):  ./sc doctor      No docker:  ./sc boot" >&2
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

# Rootless Docker maps container-root to the host user. The GitHub launch
# adapter owns the exact --user selection because it also validates whether an
# agent socket can be forwarded for that mapping.
drootless() {
  docker info 2>/dev/null | grep -qi rootless
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

sc_devkit_image_name() {
  epoch="$1"
  "$PY" "$S/sandbox_devkit.py" image-name \
    "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)"
}

sc_devkit_ready() {
  epoch="$(harness_epoch)"
  "$PY" "$S/sandbox_devkit.py" ready \
    "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)" \
    "$CNAME"
}

sc_devkit_cutover() {
  epoch="$(harness_epoch)"
  "$PY" "$S/sandbox_devkit.py" cutover \
    "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)"
}

# What the CURRENT image was actually built with, read back from the label the
# Dockerfile stamps. Empty for an image built before this seam existed (or none
# at all) — callers treat that as "unknown", never as "current".
harness_epoch_built() {
  epoch="$(harness_epoch)"
  IMG="$(sc_devkit_image_name "$epoch")" || return 1
  docker image inspect "$IMG" --format '{{index .Config.Labels "sc.harness_epoch"}}' 2>/dev/null \
    | sed 's/^<no value>$//' || true
}

# Build the env image (the repo is bind-mounted at run time, never baked — see
# .dockerignore: the build context is empty). Cheap to re-run; layers cache —
# including the harness layers, unless the epoch below has been rolled since.
dbuild() {
  epoch="$(harness_epoch)"
  IMG="$(sc_devkit_image_name "$epoch")" || return 1
  "$PY" "$S/sandbox_devkit.py" build \
    "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)" \
    "$CNAME" || return 1
  IMG="$(sc_devkit_image_name "$epoch")" || return 1
}

dimage_preflight() {
  epoch="$(harness_epoch)"
  IMG="$(sc_devkit_image_name "$epoch")" || return 1
  "$PY" "$S/sandbox_devkit.py" preflight \
    "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)" \
    "$CNAME" || return 1
  IMG="$(sc_devkit_image_name "$epoch")" || return 1
}

drunning() { [ "$(docker inspect -f '{{.State.Running}}' "$CNAME" 2>/dev/null || echo false)" = true ]; }

# Which harness CLIs the shells will actually run, and whether the image owes a
# build. Answers from INSIDE the sandbox when one is up, because that is the
# runtime shells get — the host's own CLIs are not mounted in and do not decide
# anything on the docker path. Never fatal: this is a status surface, and a
# probe that cannot run should say so rather than take a launch down with it.
sc_harness_status() {
  # In-container, host runtime, or no docker at all: this process IS the runtime.
  if [ -n "${SC_SANDBOX:-}" ] || sc_host_runtime || ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
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
    echo "harness CLIs (runtime and compatibility):"
    echo "  runtime:   sandbox '$CNAME' · not running"
    echo "  adapters:  unavailable until ./sc launch starts that runtime"
  fi
  echo "harness epoch: image built with ${built:-<none — predates the epoch seam>} · stored ${stored}"
  # A rolled-but-unbuilt epoch is the actionable state: the operator asked for
  # fresh harnesses and the image has not caught up yet. Say the command.
  if [ "$stored" != "0" ] && [ "$stored" != "$built" ]; then
    echo "  ! the image predates the stored epoch — ./sc restart (or ./sc build) to bake fresh harnesses"
  fi
}

# ── fork-declared dev-kit hooks — exact current-seat execution ────────────────
# Hooks run in the current host or Docker environment against the invoking Git
# checkout. The engine owns validation and exact execution, never project policy.

# Fork lifecycle policy lives only in .subfloor/dev-kit.json.  Keep the shell
# adapter deliberately thin: exact one-argument help is engine-owned, a leading
# separator is removed, and every other argument reaches the Python runner as a
# literal argv element.
sc_devkit_hook() {  # $1 = hook name; remaining args append to declared argv
  hook="$1"
  shift
  if sc_devkit_help_form "$@"; then
    echo "Usage: ./sc $hook [-h|--help]"
    return 0
  fi
  if [ "${1:-}" = "--" ]; then shift; fi
  "$PY" "$S/devkit.py" run "$CALLER_ROOT" "$hook" "$@"
}

sc_devkit_help_form() {
  [ "$#" -eq 1 ] || return 1
  case "$1" in -h|--help) return 0 ;; esac
  return 1
}

# Shared preflight for every host-side broker's `up`. The brokers write
# pid/log/sock into $ENGINE/run — a bind-mounted dir that a sudo restart
# (root-owned) or sandbox-side write (container-mapped uid) can leave
# unreachable for the invoking user. That used to fail the log redirect and
# pid write with "Permission denied" while `up` still printed "up"; restart
# health then reported the cryptic "live broker has no recognized supervisor".
# Clear the stale artifacts, prove run/ is writable, and fail fast with the
# remediation instead of lying about the start.
sc_broker_preflight() {  # $1 = label, $2 = pidfile, $3 = logfile, $4 = sock
  if ! mkdir -p "$ENGINE/run" 2>/dev/null || [ ! -w "$ENGINE/run" ]; then
    echo "✗ $1: $ENGINE/run is not writable by $(id -un) — stale from a sudo restart or a container-mapped write" >&2
    echo "  fix: sudo chown -R $(id -u):$(id -g) '$ENGINE/run'" >&2
    return 1
  fi
  if ! rm -f "$2" "$3" "$4" 2>/dev/null; then
    echo "✗ $1: stale broker artifacts in $ENGINE/run are not removable by $(id -un) — stale from a sudo restart or a container-mapped write" >&2
    echo "  fix: sudo rm -f '$2' '$3' '$4' && sudo chown -R $(id -u):$(id -g) '$ENGINE/run'" >&2
    return 1
  fi
}

# ── Windows VM broker (HOST-side; drives the test VM for sandboxed forks) ──────
# A separate host process — the sandbox server can't hold the ssh key or reach
# libvirt. It listens on a unix socket in the bind-mounted engine dir so
# the typed client (in the container) can curl it without a route or key. Refuses
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
  sc_broker_preflight vm-broker "$VM_BROKER_PID" "$ENGINE/run/vm-broker.log" "$("$PY" "$S/vm.py" sock)" || return 1
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
  sc_broker_preflight ts-broker "$TS_BROKER_PID" "$ENGINE/run/ts-broker.log" "$("$PY" "$S/ts.py" sock)" || return 1
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
  sc_broker_preflight pm2-broker "$PM2_BROKER_PID" "$ENGINE/run/pm2-broker.log" "$("$PY" "$S/pm2.py" sock)" || return 1
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
  sc_broker_preflight db-broker "$DB_BROKER_PID" "$ENGINE/run/db-broker.log" "$("$PY" "$S/dbq.py" sock)" || return 1
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
  else
    "$PY" "$S/instance_state.py" config-set "$f" db '{"dsn_env":"SC_RO_DSN","allow_tables":["skill_runs","tool_call_attempts","models"],"row_cap":1000,"statement_timeout_ms":5000}' || return 1
    echo "→ db: added to $f"
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
  if [ -f "$f" ] && "$PY" -c "import json,sys; d=json.load(open('$f')); sys.exit(0 if 'pg' in d else 1)" 2>/dev/null; then
    echo "→ pg: already configured in $f"; return 0
  else
    "$PY" "$S/instance_state.py" config-set "$f" pg '{}' || return 1
    echo "→ pg: added to $f"
  fi
  echo "  next: ./sc pg-up   (or ./sc launch — pg starts automatically)"
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
  database="$(sc_engine_db)"
  if [ -n "$destination" ]; then
    "$PY" "$S/db_backup.py" backup "$database" "$ROOT" "$prefix" "$destination"
  else
    "$PY" "$S/db_backup.py" backup "$database" "$ROOT" "$prefix"
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
  if sc_host_runtime; then
    if [ "$launch_rc" -eq 0 ] && sc_wait_until sc_host_api_healthy; then
      echo "  host server: restarted"
    else
      echo "  host server: failed (launch or health)"
      SC_RESTART_FAILED=1
    fi
  elif [ "$launch_rc" -eq 0 ] && sc_wait_until sc_sandbox_alive; then
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

# Bare and explicit help remain readable on every host. Every other command is
# an action surface and must refuse before Python, Docker, Git, or engine code.
case "$cmd" in
  help|-h|--help) ;;
  *) sc_require_supported_host ;;
esac

# Capability-check every dispatcher route that can execute host Python before
# it reaches imports or mutation. Shell-implemented help (deps/map/admin/
# launch/restart) stays dependency-free; script-owned help (remove/rebuild/
# migrate) probes first because it executes host Python. Container entry
# deliberately remains a Docker handoff rather than a host-runtime gate.
case "$cmd" in
  install|ensure-harness|doctor|update|update-harnesses|harness-status|docker-cache-gc|rollback|feature|runtime|artifact-mode|eject|remove|init|rebuild|migrate|migration|snapshot|mem|pr|token|persist|job|visual-qa|sql|sql-rw|map-sql|map-sql-rw|map-schema|map-extractor|render|render-check|map|map-setup|analytics|models|seed-skills|skill|ports|url|preview|serve|vm|vm-broker|vm-bake|vm-broker-up|vm-broker-down|vm-broker-sock|vm-mcp-relay|vm-broker-install|vm-broker-uninstall|ts-broker|ts-broker-up|ts-broker-down|ts-broker-sock|ts-broker-install|ts-broker-uninstall|pm2-broker|pm2-broker-up|pm2-broker-down|pm2-broker-sock|pm2-broker-install|pm2-broker-uninstall|db-broker|db-broker-up|db-broker-down|db-broker-sock|db-broker-install|db-broker-uninstall|db-init|pg-init|pg-up|pg-down|admin|boot|boot-*|run|deps|test|lint|typecheck|launch|down|restart|build|verify|health|clean-db)
    case "$cmd" in
      deps|test|lint|typecheck)
        sc_devkit_help_form "$@" || sc_python_probe ;;
      launch|restart|admin|map)
        sc_help_form "$@" || sc_python_probe ;;
      *) sc_python_probe ;;
    esac ;;
esac

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-docker ;;
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
    if sc_host_runtime; then
      echo "→ runtime host — updating this host's harness CLIs (the host IS the runtime)"
      "$PY" "$S/install.py" --update-harnesses
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
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
  docker-cache-gc) exec "$PY" "$S/docker_cache.py" "$@" ;;
  rollback)     exec "$PY" "$S/rollback.py" "$@" ;;
  feature)      exec "$PY" "$S/feature.py" "$@" ;;
  runtime)      exec "$PY" "$S/runtime.py" "$@" ;;
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
  rebuild)      sc_help_form "$@" || sc_refuse_linked rebuild "$(sc_engine_db)"
                exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      sc_help_form "$@" || sc_refuse_linked migrate "$(sc_engine_db)"
                exec "$PY" "$S/migrate.py" "$(sc_engine_db)" "$@" ;;
  migration)    exec "$PY" "$CALLER_ENGINE/scripts/migration.py" "$@" ;;
  # snapshot/render name the artifact they would overwrite as well as the DB
  # they read; resolving that path costs a subprocess, so only the refusing
  # branch pays for it.
  snapshot)     if [ "$LINKED" -eq 1 ]; then
                  sc_refuse_linked snapshot \
                    "$(sc_engine_db) -> $("$PY" "$S/artifact_policy.py" path content)"
                fi
                exec "$PY" "$S/snapshot.py" ;;
  mem)          exec "$PY" "$S/mem.py" "$@" ;;
  pr)           exec "$PY" "$S/pr_cli.py" "$@" ;;
  sprint)       sc_python_probe; exec "$PY" "$S/sprint_cli.py" "$@" ;;
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
  # General engine SQL is an Admin maintenance capability. The helper resolves
  # the bearer token through the API, with a canonical-DB fallback solely for
  # API-down host Admin diagnosis; caller-set flavor/path strings never grant it.
  # Repository-map SQL is separate and retains its Cartographer authority.
  sql)          exec "$PY" "$S/engine_sql.py" read-only "$@" ;;
  map-sql)      exec sqlite3 -readonly "$(sc_mapdb)" "$@" ;;
  sql-rw)       exec "$PY" "$S/engine_sql.py" read-write "$@" ;;
  map-sql-rw)   exec sqlite3 "$(sc_mapdb)" "$@" ;;
  map-schema)   exec "$PY" "$S/map_schema_cli.py" "$@" ;;
  map-extractor) exec "$PY" "$S/map_extractor_install.py" "$@" ;;
  render)       if [ "$LINKED" -eq 1 ]; then
                  sc_refuse_linked render \
                    "$(sc_engine_db) -> $("$PY" "$S/artifact_policy.py" path renders)"
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
                  -h|--help) echo "usage: ./sc map [finalize [--json]] — refresh the dr_* catalogue or report Cartographer completion"
                             exit 0 ;;
                  finalize)  shift
                             exec "$PY" "$S/map_finalize.py" "$@" ;;
                  ?*)        echo "sc map: unknown argument '$1' (-h for usage)" >&2
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
  admin)
    if sc_help_form "$@"; then
      echo "usage: ./sc admin [admin-shortname] [--harness <h>] [--model <route>]"
      echo "Boot the sole active Admin directly on the host; no Docker or API is required."
      exit 0
    fi
    if [ -n "${SC_SANDBOX:-}" ]; then
      echo "sc admin: host Admin launch is unavailable inside the sandbox; run make dos-admin from a host terminal" >&2
      exit 1
    fi
    exec "$PY" "$S/run.py" --host-admin "$@" ;;
  boot)         exec "$PY" "$S/run.py" "$@" ;;
  boot-*)       exec "$PY" "$S/run.py" "${cmd#boot-}" "$@" ;;
  # Headless boot: same render-then-exec path as boot, minus the picker and
  # the TTY. Also used by the no-docker host path.
  run)          exec "$PY" "$S/run.py" --headless "$@" ;;
  deps)         sc_devkit_hook deps "$@" ;;
  test)         sc_devkit_hook test "$@" ;;
  lint)         sc_devkit_hook lint "$@" ;;
  typecheck)    sc_devkit_hook typecheck "$@" ;;
  # ── docker sandbox (host-side; the default way to run) ──
  launch)
    no_build=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --no-build) no_build=1 ;;
        -h|--help)
          echo "usage: ./sc launch [--no-build]"
          echo "  default     build the image, refresh host GitHub capabilities, then launch"
          echo "  --no-build  reuse the labeled image but still refresh host GitHub capabilities"
          exit 0 ;;
        *)
          echo "sc launch: unknown argument '$1' (usage: ./sc launch [--no-build])" >&2
          exit 2 ;;
      esac
      shift
    done
    if sc_host_runtime; then
      [ -z "$no_build" ] || echo "→ runtime host: --no-build is implied (there is no image)"
      sc_host_server_up || exit 1
      echo "  dev server:    \$SC_DEV_PORT=$(devport) → http://127.0.0.1:$(devport)"
      echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)"
      sc_vm_broker_up || true
      sc_ts_broker_up || true
      sc_pm2_broker_up || true
      sc_db_broker_up || true
      # The Postgres sidecar is a docker container even under the host runtime
      # (app-only, opt-in); it is started only when the fork linked it.
      if sc_pg_configured; then sc_pg_up || true; fi
      exit 0
    fi
    dcheck
    if [ -n "$no_build" ]; then dimage_preflight; else dbuild; fi
    dcreds
    "$PY" "$S/ports.py" ensure >/dev/null
    p="$(port)"
    dp="$(devport)"
    dnet
    github_auth_rootless=""
    drootless && github_auth_rootless="--rootless"
    # Forward a Mistral key for vibe's API-key auth path — ONLY when set, so an
    # empty value can't shadow the mounted ~/.vibe creds (vibe --setup stores its
    # key + .env there; the mount below carries them in like every other harness).
    mistral_env=""
    [ -n "${MISTRAL_API_KEY:-}" ] && mistral_env="-e MISTRAL_API_KEY=${MISTRAL_API_KEY}"
    disabled_harnesses_env=""
    [ -n "${SC_DISABLED_HARNESSES:-}" ] && disabled_harnesses_env="-e SC_DISABLED_HARNESSES"
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
    # A private-state install keeps its engine DB outside the checkout.  The
    # repo bind below therefore cannot make that state visible to the sandbox,
    # and mounting the whole owner-local state collection would expose sibling
    # instances.  Bind only this installation's resolved state directory into
    # the image's private, pre-created state namespace.  The instance ID stays
    # identical, so the in-container resolver selects this exact directory even
    # when the host uses a non-default XDG_STATE_HOME.
    state_dir="$(dirname "$(sc_engine_db)")"
    state_mount=""
    state_namespace_mounts=""
    if [ "$state_dir" != "$ENGINE" ]; then
      state_target="$HOME/.local/state/subfloor/instances/$(basename "$state_dir")"
      state_mount="-v $state_dir:$state_target"
      # Rootless Docker maps container root to the invoking host user.  The
      # selected bind leaf therefore appears root-owned inside, while the image
      # namespace was built for the host numeric uid.  Overlay only the empty
      # namespace ancestors with private root-owned tmpfs mounts so the strict
      # resolver sees one coherent owner.  The selected instance remains the
      # sole host-state bind; rootful launch keeps the image-owned uid tree.
      if [ -n "$github_auth_rootless" ]; then
        state_namespace_mounts="--tmpfs $HOME/.local/state:uid=0,gid=0,mode=0700 --tmpfs $HOME/.local/state/subfloor:uid=0,gid=0,mode=0700 --tmpfs $HOME/.local/state/subfloor/instances:uid=0,gid=0,mode=0700"
      fi
    fi
    # Pinned-interpreter passthrough. When the fork's .venv was built from an
    # out-of-tree interpreter — a uv-managed standalone CPython under $HOME, used
    # to pin the app's Python independent of the host's rolling system python —
    # the bind-mounted .venv's bin/python + script shebangs point at that
    # interpreter by absolute path. Mount it read-only at the SAME path so the
    # shared .venv runs end-to-end inside the sandbox on the *identical* binary:
    # same ABI as the wheels the host installed (psycopg etc. import with zero
    # rebuild), and every declared .venv tool resolves. Engine
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
    # Docker's init shim is PID 1 so orphaned harness/worker subprocesses are
    # reaped. Without it the Python API server becomes PID 1, never wait()s on
    # reparented children, and long-running multi-shell work exhausts the
    # container's PID limit with zombies (flag #323).
    epoch="$(harness_epoch)"
    provision_rc=0
    "$PY" "$S/github_auth.py" discover --repo-root "$CALLER_ROOT" | \
      "$PY" "$S/sandbox_github_auth.py" $github_auth_rootless \
        --image "$IMG" --uid "$(id -u)" --gid "$(id -g)" -- \
        "$PY" "$S/sandbox_devkit.py" launch-container \
        "$CALLER_ROOT" "$ENGINE" "$epoch" "$(id -un)" "$(id -u)" "$(id -g)" \
        "$CNAME" -- \
        -d --name "$CNAME" --restart unless-stopped --init \
        --network "$SC_NET" \
        SC_GITHUB_AUTH_ARGS \
        -e HOME="$HOME" -e SC_BIND=0.0.0.0 -e SC_PYTHON=python3 -e PYTHONUNBUFFERED=1 \
        -e SC_SANDBOX=1 -e SC_DEV_PORT="$dp" \
        $mistral_env $disabled_harnesses_env $pg_env \
        -e GIT_AUTHOR_NAME="$git_name" -e GIT_AUTHOR_EMAIL="$git_email" \
        -e GIT_COMMITTER_NAME="$git_name" -e GIT_COMMITTER_EMAIL="$git_email" \
        -w "$here" \
        -v "$here:$here" \
        $state_namespace_mounts \
        $state_mount \
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
        SC_DEVKIT_MOUNTS \
        "$IMG" ./sc serve --port "$p" || provision_rc=$?
    if [ "$provision_rc" -ne 0 ]; then
      echo "✗ dev-kit state: failed — retained sandbox '$CNAME' and local evidence." >&2
      echo "  retry:  ./sc launch --no-build" >&2
      echo "  repair: ./sc enter --devkit-repair" >&2
      exit "$provision_rc"
    fi
    if ! sc_wait_until sc_sandbox_alive; then
      echo "✗ sandbox launch failed: review API did not become healthy; retained '$CNAME' for inspection." >&2
      echo "  inspect: ./sc logs" >&2
      echo "  retry:   ./sc launch --no-build" >&2
      exit 1
    fi
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
      printf '\033[1m→ sandbox up\033[0m · \033[1mReview GUI  \033[36mhttp://127.0.0.1:%s\033[0m\n' "$p"
    else
      echo "→ sandbox up · review GUI at http://127.0.0.1:$p"
    fi
    echo "  dev server:    bind 0.0.0.0:$dp inside (\$SC_DEV_PORT) → http://127.0.0.1:$dp"
    echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)"
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
  enter)
    if sc_host_runtime; then sc_host_enter "" "$@"; fi
    if [ "${1:-}" = "--devkit-repair" ]; then
      shift
      echo "! dev-kit state: repair — provisioning is not ready; no readiness claim is made." >&2
      echo "  inspect .sc-state/local/dev-kit/ and run the declared hook explicitly." >&2
      sc_urls || true
      exec docker exec -it -e SC_DEVKIT_REPAIR=1 "$CNAME" ./sc boot "$@"
    fi
    sc_devkit_ready || {
      echo "✗ dev-kit state: stale — normal entry blocked until fork provisioning is ready." >&2
      echo "  retry:  ./sc launch --no-build" >&2
      echo "  repair: ./sc enter --devkit-repair" >&2
      exit 1
    }
    sc_urls || true
    exec docker exec -it "$CNAME" ./sc boot "$@" ;;
  enter-*)
    if sc_host_runtime; then sc_host_enter "${cmd#enter-}" "$@"; fi
    if [ "${1:-}" = "--devkit-repair" ]; then
      echo "sc ${cmd}: repair posture is available only as ./sc enter --devkit-repair" >&2
      exit 2
    fi
    sc_devkit_ready || {
      echo "✗ dev-kit state: stale — normal entry blocked until fork provisioning is ready." >&2
      echo "  retry:  ./sc launch --no-build" >&2
      echo "  repair: ./sc enter --devkit-repair" >&2
      exit 1
    }
    sc_urls || true
    exec docker exec -it "$CNAME" ./sc boot "${cmd#enter-}" "$@" ;;
  down)         if sc_host_runtime; then
                  down_rc=0
                  sc_host_server_down || down_rc=1
                  sc_vm_broker_down
                  sc_ts_broker_down
                  sc_pm2_broker_down
                  sc_db_broker_down
                  if sc_pg_configured; then sc_pg_down || down_rc=1; fi
                  exit "$down_rc"
                fi
                docker rm -f "$CNAME" >/dev/null 2>&1 && echo "→ sandbox stopped" || echo "→ not running"
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
  restart)
    assume_yes=""
    no_build=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -y|--yes) assume_yes=1 ;;
        --no-build) no_build=1 ;;
        -h|--help)
          echo "usage: ./sc restart [-y|--yes] [--no-build]"
          echo "  default     refresh harness CLIs and host GitHub capabilities, rebuild, then bounce"
          echo "  --no-build  reuse the image but still refresh host GitHub capabilities"
          exit 0 ;;
        *)
          echo "sc restart: unknown argument '$1' (usage: ./sc restart [-y|--yes] [--no-build])" >&2
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
    if sc_host_runtime; then
      # No image to refresh or cut over: backup, stop what launch started,
      # start it again, then prove health — the same shape, minus docker.
      [ -z "$no_build" ] || echo "→ runtime host: --no-build is implied (there is no image)"
      backup_dir="$(sc_db_backup_preflight)"
      sc_db_backup prerestart "$backup_dir"
      if ! "$0" down; then
        echo "✗ restart stopped: teardown did not complete; no replacement services were launched." >&2
        exit 1
      fi
      launch_rc=0
      "$0" launch || launch_rc=$?
      sc_restart_health_summary "$launch_rc"
      exit $?
    fi
    dcheck
    if [ -n "$no_build" ]; then
      dimage_preflight
    else
      epoch="$(harness_epoch_roll)"
      echo "→ refresh harnesses for restart (epoch $epoch)"
      dbuild
    fi
    cutover="$(sc_devkit_cutover)" || exit 1
    if [ "$cutover" = "unchanged" ] && drunning; then
      echo "→ restart preserved healthy sandbox '$CNAME' — native package capability remains advisory"
      exit 0
    fi
    backup_dir="$(sc_db_backup_preflight)"
    sc_db_backup prerestart "$backup_dir"
    if ! "$0" down; then
      echo "✗ restart stopped: teardown did not complete; no replacement services were launched." >&2
      exit 1
    fi
    launch_rc=0
    "$0" launch --no-build || launch_rc=$?
    sc_restart_health_summary "$launch_rc" ;;
  # --harnesses expires the baked harness CLIs first, so the build re-runs their
  # installers instead of serving them from a cache that has no expiry of its own.
  build)
    if sc_host_runtime; then
      echo "sc build: runtime is host — there is no sandbox image to build (./sc runtime sandbox to switch)" >&2
      exit 2
    fi
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
  logs)
    if sc_help_form "$@"; then
      echo "usage: ./sc logs"
      echo "  Tail the managed server logs until interrupted."
      exit 0
    fi
    if sc_host_runtime; then
      [ -f "$HOST_SERVER_LOG" ] || {
        echo "sc logs: no host review server log yet — ./sc launch first" >&2
        exit 1
      }
      exec tail -f "$HOST_SERVER_LOG"
    fi
    exec docker logs -f "$CNAME" ;;
  verify)
    database="$(sc_engine_db)"
    sc_refuse_linked verify "$database"
    # Destructive by design: rebuild.py REPLACES the DB below. Say which DB and
    # which source before that happens — a footer printed after the fact is a
    # disclosure a crash can skip, and this is the command that eats unsnapshotted
    # memory when it is pointed at an instance the caller did not mean.
    echo "→ verify: about to REBUILD $database"
    echo "          from engine source $ENGINE"
    "$PY" "$S/rebuild.py"
    # The engine source intentionally carries no per-instance snapshot in local
    # artifact mode. Exercise the real fresh-fork initialization path before
    # the headless boot when rebuild therefore produced an empty instance.
    if "$PY" - "$database" <<'PY'
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
  clean-db)     database="$(sc_engine_db)"
                sc_refuse_linked clean-db "$database"
                rm -f "$database" "$database-wal" "$database-shm" && echo "removed $database (rebuild with: ./sc rebuild)" ;;
  help|-h|--help)
    cat <<'EOF'
super-coder — forkable shell substrate

  Host support: Linux-only — Ubuntu LTS, stable Fedora, and Arch-compatible Linux (including CachyOS) are tested examples.

  ./sc install             first-launch bootstrap for a fork (requirements, harness, first shell)
  ./sc ensure-harness      install claude + opencode + codex + vibe + kimi if missing (official native installers, no npm)
  ./sc doctor              runtime readiness: docker (rootless/rootful) + harness login — or, under
                             runtime host, the host process contract + harness login
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
  ./sc docker-cache-gc     remove unused host-global Docker build cache older than seven days
                             --until <duration> changes the age · --all removes all unused cache
  ./sc rollback            Admin-only undo of a bad update — restore the paired control-plane + engine generation
                             --engine-only repairs a new-engine / unchanged-state half floor
  ./sc feature             list optional infrastructure (pg · windows · tailnet · pm2) and its instance.json state
  ./sc feature enable <f>  create or point at one instance.json block (disable removes it)
  ./sc eject               ONE-WAY: stop tracking upstream and own the engine — un-gitignore + stage .super-coder/ as fork source (confirm-gated)
  ./sc remove              safely uninstall subfloor from this repo after a verified DB backup
                             --dry-run previews; --yes skips confirmation, never safety gates
  ./sc rebuild             Admin-only verified rebuild of the private control plane
  ./sc migrate             Admin-only application of pending engine changes
  ./sc migration new <slug>
                           allocate the next free migration number, write the standard skeleton, and update the source removal-test allowlist
  ./sc snapshot            Admin-only serialization of private instance content
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
  ./sc token               print the browser sign-in operator token — an Admin/operator recovery capability;
                             stdout carries only the token and failure names the supported service action
  sc engine-ref            print the full engine pin from the canonical live checkout — safe from any shell
                             worktree; stdout is the 40-character SHA only
  ./sc job start -- <cmd>  run a long local command (suite/bench/build) detached + supervised — it
                             survives your session; completion lands in YOUR inbox as a result row
                             (--label <slug> names it, --timeout <s> kills the wedged process group)
  ./sc job wait <id>       bounded foreground wait, ≤550s slice — exit 0 done · 2 still running
                             (drain your inbox between slices); list/status/tail/kill complete the set
  ./sc models refresh      refresh local model routes (same action as Shells → Refresh models)
  ./sc models resolve <h> [<model>] [--effort <level>] [--shell <shortname>]
                             print one exact, locally runnable high-effort call; list [harness] shows routes
  ./sc visual-qa <mode>    viewport screenshot QA: ci boots/captures · run captures a local app · init scaffolds config
  sc map-sql "<query>"     read-only query of the repository catalogue (`dr_*`)
  sc map-schema [dr_table] list live dr_* objects or stable column/index metadata — read-only, no arbitrary SQL
  sc map-sql-rw            Cartographer-only catalogue authoring when its skill names the exact procedure
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
  ./sc map finalize [--json]
                           refresh + report live/snapshot/install/source/Admin/notice/flag evidence; never owns those actions
  ./sc map-extractor install <worktree-file>
                           validate + atomically install one Cartographer-authored extractor and write its SHA-256 receipt
  ./sc map-setup           wire the auto-remap git hooks (core.hooksPath) + map — the cartographer's one-shot
  ./sc seed-skills         upsert assets/skills/ into the live DB (+ regenerate the seed migration — source repo only)
  ./sc init                seed a fresh fork's first user + shell (run once after install)

  Sandbox (docker — the default way to run; allow-everything is safe because the
  container only sees this repo + your harness creds):
  ./sc runtime [mode]      show or select the lifecycle runtime: sandbox (docker, the default) · host
                             (review server as a supervised host process + shells booted on this host;
                             no docker anywhere). launch/enter/down/restart/logs/build/update-harnesses/
                             doctor/update follow the selection; ./sc install --runtime host sets it at install
  ./sc launch              build the exact base/fork image, start the sandbox, and run declared provisioning
                             (runtime host: start the host review server + configured brokers; no image)
                             states: absent · invalid · failed · stale · ready; failed setup retains container + evidence
                             every launch refreshes configured-origin Git + GitHub API capabilities;
                             --no-build reuses only a ready labeled image but still refreshes auth
  ./sc admin               boot the sole active Admin directly on the host (no Docker or API required)
  ./sc enter               boot an interactive shell only when declared provisioning is ready
                             --devkit-repair enters state repair without claiming readiness
  ./sc enter-<shortname>   enter that shell directly when ready (skip the shell picker)
                             harness: --harness <name> or HARNESS=<name> forces it; else when
                             >1 harness is on PATH you're prompted (per-launch, not persisted)
  make dos-help            supported operator aliases for lifecycle, models,
                             job, maintenance, browser token, and generic ./sc forwarding
  ./sc run <shortname>     headless boot: render + exec the harness NON-interactively (claude · codex ·
                             opencode · kimi); -p "<prompt>" · --harness <h> · -m <model> · --effort;
                             refuses a shell that already has a live session
  ./sc down                stop + remove the sandbox container
  ./sc restart             confirm + WAL-safe backup, fully bounce, then health-check managed services
                             default refreshes image-owned harnesses; every restart refreshes host GitHub capabilities
                             --yes skips the prompt · --no-build reuses the image without skipping auth refresh
  ./sc build               (re)build the sandbox image · --harnesses also expires the baked harness CLIs so they reinstall
  ./sc logs                tail the sandbox server logs

  Primitives (run inside the container; also the no-docker host escape hatch):
  ./sc serve               run the review layer (api + static UI) in the foreground
  ./sc boot [shortname]    direct interactive launch (host/no-docker primitive)
  ./sc deps [args]         run the fork-declared deps argv; exit 78 when not configured
  ./sc test [args]         run the fork-declared test argv exactly
  ./sc lint [args]         run the fork-declared lint argv exactly
  ./sc typecheck [args]    run the fork-declared typecheck argv exactly
                             all four validate .subfloor/dev-kit.json, preserve child output/status,
                             and never infer a manifest, tool, file set, or fallback

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
