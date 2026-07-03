#!/bin/sh
# super-coder entry point — the dispatcher. All engine logic lives here so it
# travels with a fork (install.py checks out .super-coder + sc). Host commands
# (launch/enter/down) drive a docker sandbox; the in-container primitives
# (serve/boot) need only python3 + sqlite3 and double as the no-docker host
# escape hatch. Run from the repo root:  ./sc <command> [args]   ·   ./sc help
set -e
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$here"

# The engine (`.super-coder/`) and its gitignored live DB sit at the MAIN worktree
# root. A linked worktree (a shell's `.sc-worktrees/<name>/`) has a tracked copy of
# THIS script — and, in the canonical repo where `.super-coder/` is tracked, even a
# DB-less engine copy — but never the live DB. So always resolve the engine at the
# main root via git's common dir (its parent is the main worktree), so `./sc` works
# from any worktree. We do NOT cd there: cwd stays the caller's worktree so git ops
# + shell inference see it. Fall back to $here outside a git checkout.
ROOT="$here"
_root="$(cd "$here" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd || true)"
[ -n "$_root" ] && [ -d "$_root/.super-coder" ] && ROOT="$_root"

ENGINE="$ROOT/.super-coder"
PY="${SC_PYTHON:-python3}"
DB="$ENGINE/shell_db.db"
MAPDB="$ROOT/.sc-state/map.db"
S="$ENGINE/scripts"

port() { "$PY" "$S/ports.py" port; }
devport() { "$PY" "$S/ports.py" devport; }

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

# Fail fast with the fix if the docker daemon isn't reachable, instead of a
# cryptic build/run error. Host setup is one-time and lives in `./sc doctor` /
# `./sc install` — it needs sudo + a re-login, so it can't fold into launch.
dcheck() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "✗ docker daemon not reachable — the sandbox needs it." >&2
    echo "  Setup (one-time):  ./sc doctor      No docker:  ./sc serve + ./sc boot" >&2
    exit 1
  fi
}

# Ensure the harness cred mount-sources exist as the RIGHT TYPE before docker
# bind-mounts them. A missing DIR source is harmless (docker makes a dir), but a
# missing FILE source (~/.claude.json) gets auto-created as a directory and
# breaks claude — so seed it with empty json. Real creds come from a one-time
# host login (`./sc doctor` guides it); this just keeps the mounts valid.
dcreds() {
  mkdir -p "$HOME/.claude" "$HOME/.config/opencode" "$HOME/.local/share/opencode" "$HOME/.codex" "$HOME/.vibe" 2>/dev/null || true
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

# Build the env image (the repo is bind-mounted at run time, never baked — see
# .dockerignore: the build context is empty). Cheap to re-run; layers cache.
dbuild() {
  docker build -t "$IMG" -f "$ENGINE/Dockerfile" \
    --build-arg SC_USER="$(id -un)" \
    --build-arg SC_UID="$(id -u)" \
    --build-arg SC_GID="$(id -g)" \
    "$here"
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

# Resolve a dev-kit tool (ruff / mypy): the fork's .venv copy wins (its pins +
# config), else the image/PATH copy (baked into the sandbox for exactly this
# case), else fail with the honest fix. A host-managed .venv (pinned out-of-tree
# interpreter mounted by launch) is pip-skipped in the sandbox BY DESIGN, so
# "run ./sc deps first" was a closed loop there — the tool was unobtainable from
# inside the box (dos-arch QAQC-02). Say what is actually wrong and where the
# fix runs instead.
_sc_devtool() {  # $1 = tool name → prints the executable path, or fails
  venv="$here/.venv"
  if [ -x "$venv/bin/$1" ]; then printf '%s\n' "$venv/bin/$1"; return 0; fi
  if command -v "$1" >/dev/null 2>&1; then command -v "$1"; return 0; fi
  hostmanaged=""
  if [ -n "${SC_SANDBOX:-}" ] && [ -e "$venv/bin/python" ]; then
    case "$(readlink -f "$venv/bin/python" 2>/dev/null || true)" in
      "$here"/*) : ;;                       # sandbox-built venv — deps can provision it
      *) hostmanaged=1 ;;                   # pinned host interpreter — pip skipped here
    esac
  fi
  if [ -n "$hostmanaged" ]; then
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
  # Self-heal: a fork with python tests but no .venv/bin/pytest is an unprovisioned
  # worktree (the .venv is only populated by the first `./sc deps`), NOT a fork that
  # opted out of pytest. Provision the dev kit + fork deps rather than silently
  # downgrading to stdlib unittest — under which a pytest-based suite fails with
  # ModuleNotFoundError (pytest / the fork's own libs) that reads as a real test
  # failure. We gate on the pytest binary, not sc_deps' exit code, so a partial
  # provision (e.g. npm leg fails) still runs pytest if it landed.
  if [ ! -x "$venv/bin/pytest" ] && ls "$here"/tests/test_*.py >/dev/null 2>&1; then
    echo "→ test: $venv/bin/pytest missing — provisioning first (./sc deps)"
    sc_deps || echo "→ test: provisioning incomplete — continuing" >&2
  fi
  rc=0
  if [ -x "$venv/bin/pytest" ]; then
    echo "→ test: $venv/bin/pytest"
    ( cd "$here" && "$venv/bin/pytest" "$@" ) || rc=1
  elif ls "$here"/tests/test_*.py >/dev/null 2>&1; then
    # pytest still unavailable after provisioning (venv create failed, or a
    # host-managed sandbox interpreter that skips pip). A fork that *declares*
    # pytest must not be green-washed through stdlib unittest — fail loud with the
    # fix. Only a fork with no pytest config keeps the legacy stdlib fallback.
    if _sc_wants_pytest; then
      echo "✗ test: pytest required (pytest config present) but unavailable in $venv — run ./sc deps to provision it" >&2
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
  systemctl --user enable --now "$VM_BROKER_UNIT"
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
  systemctl --user enable --now "$TS_BROKER_UNIT"
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
  systemctl --user enable --now "$PM2_BROKER_UNIT"
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
sc_pg_up() {
  if ! sc_pg_configured; then
    echo "→ pg: no \`pg\` key in instance.json — skipping (run: ./sc pg-init)"; return 0
  fi
  if sc_pg_alive; then echo "→ pg already running ($PGNAME)"; return 0; fi
  dnet
  docker volume create "$PGVOL" >/dev/null
  docker run -d --name "$PGNAME" --restart unless-stopped \
    --network "$SC_NET" \
    -e POSTGRES_USER=sc \
    -e POSTGRES_PASSWORD=sc \
    -e POSTGRES_DB=sc \
    -v "$PGVOL:/var/lib/postgresql/data" \
    postgres:17 >/dev/null
  echo "→ pg 17 up ($PGNAME on $SC_NET) · DATABASE_URL=postgresql://sc:sc@$PGNAME:5432/sc"
}
sc_pg_down() {
  if docker inspect "$PGNAME" >/dev/null 2>&1; then
    docker rm -f "$PGNAME" >/dev/null 2>&1 && echo "→ pg stopped (volume $PGVOL retained)" || true
  fi
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
  echo "  next: ./sc pg-up   (or ./sc launch — pg starts automatically)"
}


cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-docker ;;
  update)            exec "$PY" "$S/update.py" "$@" ;;
  update-harnesses) exec "$PY" "$S/install.py" --update-harnesses ;;
  rollback)     exec "$PY" "$S/rollback.py" "$@" ;;
  feature)      exec "$PY" "$S/feature.py" "$@" ;;
  eject)        exec "$PY" "$S/eject.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  rebuild)      exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      exec "$PY" "$S/migrate.py" "$DB" ;;
  snapshot)     exec "$PY" "$S/snapshot.py" ;;
  mem)          exec "$PY" "$S/mem.py" "$@" ;;
  # Raw read passthrough to the engine + map DBs, resolved by absolute path so no
  # skill example ever needs a cwd-relative `sqlite3 .super-coder/…` (which pulls a
  # shell into `cd`-ing to the root — the cwd trap). Read-only is ENFORCED
  # (sqlite3 -readonly), matching the label: writes go via `sc mem`
  # (triggers/caps). The -rw variants are the explicit escape hatch for the few
  # procedures with no API surface (skill grants, cartographer map authoring) —
  # only use one where a skill names it.
  sql)          exec sqlite3 -readonly "$DB" "$@" ;;
  map-sql)      exec sqlite3 -readonly "$MAPDB" "$@" ;;
  sql-rw)       exec sqlite3 "$DB" "$@" ;;
  map-sql-rw)   exec sqlite3 "$MAPDB" "$@" ;;
  render)       [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  render-check) exec "$PY" "$S/render_check.py" ;;
  map)          case "${1:-}" in
                  -h|--help) echo "usage: ./sc map — rescan the host repo into the dr_* catalogue (.sc-state/map.db); takes no arguments"
                             exit 0 ;;
                  ?*)        echo "sc map: unknown argument '$1' (takes none; -h for usage)" >&2
                             exit 2 ;;
                esac
                exec "$PY" "$S/map_repo.py" ;;
  map-setup)    exec "$PY" "$S/map_setup.py" ;;
  seed-skills)  exec "$PY" "$S/seed_skills.py" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  preview)      exec "$PY" "$S/preview.py" "$@" ;;
  # ── in-container primitives (no docker; also the host escape hatch) ──
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  # ── Windows VM broker (HOST-side primitive — runs where virsh + the key live) ──
  vm-broker)         exec "$PY" "$ENGINE/api/vm_broker.py" "$@" ;;
  # Bake/re-bake the clean snapshot — HOST-side, deliberately NOT a broker verb:
  # the snapshot is the trust anchor every test reverts to; a sandboxed shell may
  # run against it but must never redefine it. vm.py bake self-guards on SC_SANDBOX.
  vm-bake)           exec "$PY" "$S/vm.py" bake ;;
  vm-broker-up)      sc_vm_broker_up ;;
  vm-broker-down)    sc_vm_broker_down ;;
  vm-broker-sock)    exec "$PY" "$S/vm.py" sock ;;
  # In-sandbox half of the GUI seam (#263): TCP→unix relay so `claude mcp add
  # --transport http` can reach the broker's vm-mcp.sock tunnel. Runs IN the
  # container; the broker-side half is `POST /mcp/up` on the vm-broker socket.
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
  # ── Postgres sidecar (app-only) ──
  pg-init)      sc_pg_init ;;
  pg-up)        sc_pg_up ;;
  pg-down)      sc_pg_down ;;
  boot)         exec "$PY" "$S/run.py" "$@" ;;
  boot-*)       exec "$PY" "$S/run.py" "${cmd#boot-}" "$@" ;;
  deps)         sc_deps "$@" ;;
  test)         sc_test "$@" ;;
  lint)         sc_lint "$@" ;;
  typecheck)    sc_typecheck "$@" ;;
  # ── docker sandbox (host-side; the default way to run) ──
  launch)
    dcheck
    dcreds
    "$PY" "$S/ports.py" ensure >/dev/null
    p="$(port)"
    dp="$(devport)"
    dbuild
    dnet
    # Forward GitHub auth for the in-container push/PR path (GUI publish + shells
    # opening their own PRs). Prefer a repo-scoped SC_GH_TOKEN; else reuse the
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
    docker run -d --name "$CNAME" --restart unless-stopped \
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
      -p "127.0.0.1:$p:$p" \
      -p "127.0.0.1:$dp:$dp" \
      "$IMG" ./sc serve --port "$p" >/dev/null
    echo "→ sandbox up · review GUI at http://127.0.0.1:$p"
    echo "  dev server:    bind 0.0.0.0:$dp inside (\$SC_DEV_PORT) → http://127.0.0.1:$dp"
    echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)"
    # Bring the VM broker up alongside the sandbox when a VM is linked (self-skips
    # otherwise, and no-ops if systemd already owns it). The shells need it to
    # drive the VM; this keeps it from being a forgotten manual step.
    sc_vm_broker_up || true
    # Same for the tailnet broker — self-skips when no `ts` block is linked.
    sc_ts_broker_up || true
    # Same for the pm2 broker — self-skips when no `pm2` block is linked.
    sc_pm2_broker_up || true
    # Start the PG sidecar when configured — self-skips otherwise.
    sc_pg_up || true ;;
  enter)        exec docker exec -it "$CNAME" ./sc boot "$@" ;;
  enter-*)      exec docker exec -it "$CNAME" ./sc boot "${cmd#enter-}" "$@" ;;
  down)         docker rm -f "$CNAME" >/dev/null 2>&1 && echo "→ sandbox stopped" || echo "→ not running"
                sc_vm_broker_down
                sc_ts_broker_down
                sc_pm2_broker_down
                sc_pg_down ;;
  restart)      "$0" down; exec "$0" launch "$@" ;;
  build)        dcheck; dbuild ;;
  logs)         exec docker logs -f "$CNAME" ;;
  verify)
    "$PY" "$S/rebuild.py"
    SC_ADMIN=1 "$PY" "$S/render.py" flat
    RENDER_ONLY=1 exec "$PY" "$S/run.py" --first ;;
  health)       curl -s "http://127.0.0.1:$(port)/api/health" && echo "" ;;
  clean-db)     rm -f "$DB" "$DB-wal" "$DB-shm" && echo "removed $DB (rebuild with: ./sc rebuild)" ;;
  help|-h|--help)
    cat <<'EOF'
super-coder — forkable shell substrate

  ./sc install             first-launch bootstrap for a fork (requirements, harness, first shell)
  ./sc ensure-harness      install claude + opencode + codex if missing (official native installers, no npm)
  ./sc doctor              sandbox readiness: docker (rootless/rootful) + harness login
  ./sc update              fetch + materialize the engine (gitignored dep) + reconcile IN PLACE (migrate, sync skills, map);
                             --no-fetch skips the fetch · --ref <tag|sha> pins a version · blocks on local engine edits (--force discards them)
  ./sc update-harnesses    update claude + opencode + codex + vibe to latest (force-reruns official installers)
  ./sc rollback            sound undo of a bad update — restore the DB + engine (engine.ref.prev) together
  ./sc feature             list the opt-in features (pg · windows · tailnet) and the state of both halves (config block + skill grants)
  ./sc feature enable <f>  enable one: grant its skills to the owning flavors + create/point-at its instance.json block (disable reverses)
  ./sc eject               ONE-WAY: stop tracking upstream and own the engine — un-gitignore + stage .super-coder/ as fork source (confirm-gated)
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc snapshot            dump per-instance tables -> .sc-state/content.sql
  ./sc mem <cmd> [args]    a shell's own memory, over the engine API (get/state/seed/lns/decision/flag/roadmap/doc/narrative);
                             identity is the shell's token, server-resolved — no DB path, no direct-DB fallback. `./sc mem which` to orient
  sc sql "<query>"         read-only passthrough to the engine DB (schema/skills/flags) — absolute path, cwd-independent (no `cd` to root)
  sc map-sql "<query>"     read-only passthrough to the repo-map DB (dr_* catalogue) — absolute path, cwd-independent
  sc sql-rw / map-sql-rw   read-WRITE passthroughs — bypass the API's triggers/caps; `sc mem` is the write path.
                             Only for procedures with no API surface (skill grants, map authoring) where a skill names it
  ./sc render              render tracked flat _sc files (specs/docs/skills/roadmap)
  ./sc render-check        fail if committed flat _sc files drift from the DB render (CI guard; rebuild first for a hermetic check)
  ./sc map                 scan the host repo into the dr_* catalogue (re-runnable)
  ./sc map-setup           wire the auto-remap git hooks (core.hooksPath) + map — the cartographer's one-shot
  ./sc seed-skills         regenerate the skills seed migration from assets/skills/
  ./sc init                seed a fresh fork's first user + shell (run once after install)

  Sandbox (docker — the default way to run; allow-everything is safe because the
  container only sees this repo + your harness creds):
  ./sc launch              build + start the sandbox container (server + GUI), 127.0.0.1 only
  ./sc enter               attach an interactive session: auth + pick shell + pick harness + boot
  ./sc enter-<shortname>   attach + boot that shell directly (skip the shell picker)
                             harness: --harness <name> or HARNESS=<name> forces it; else when
                             >1 harness is on PATH you're prompted (per-launch, not persisted)
  ./sc down                stop + remove the sandbox container
  ./sc restart             down + launch — recreate the sandbox fresh
  ./sc build               (re)build the sandbox image
  ./sc logs                tail the sandbox server logs

  Primitives (run inside the container; also the no-docker host escape hatch):
  ./sc serve               run the review layer (api + static UI) in the foreground
  ./sc boot [shortname]    auth + pick shell + pick harness + boot (no container, no GUI)
  ./sc deps                install this fork's python (.venv) + node (node_modules) deps into the bind-mount
                             (plus an only-if-needed dev kit: pytest httpx coverage ruff mypy datasette)
  ./sc test                run backend (.venv pytest or stdlib unittest) + UI (vitest) suites; non-zero on any failure
  ./sc lint [paths]        ruff check the fork's python (.venv ruff; honors [tool.ruff]) — available, not enforced
  ./sc typecheck [paths]   mypy the fork's python (.venv mypy; honors [tool.mypy]) — available, not enforced

  Windows VM broker (run on the HOST — drives the test VM for sandboxed forks;
  holds the ssh key + virsh so the fork never does. See .super-coder/docs/windows-vm-broker.md).
  `launch` brings it up automatically when a VM is linked; `down` stops it:
  ./sc vm-broker           run the broker in the foreground (unix socket)
  ./sc vm-bake             HOST-side: graceful shutdown + (re)bake the clean snapshot after provisioning
                             (deliberately NOT a broker verb — the sandbox must never redefine 'clean')
  ./sc vm-broker-up        start it in the background (nohup + pidfile); self-skips if unlinked/already up
  ./sc vm-broker-down      stop the backgrounded broker
  ./sc vm-broker-sock      print the broker's socket path
  ./sc vm-broker-install   supervise via a systemd --user unit (survives logout/reboot)
  ./sc vm-broker-uninstall remove the systemd unit
  ./sc vm-mcp-relay        in-SANDBOX half of the GUI seam: up [port] / down / status —
                             TCP 127.0.0.1:18000 → the broker's vm-mcp.sock tunnel, so
                             `claude mcp add --transport http` reaches the guest's Windows-MCP
                             (broker half: POST /mcp/up on the vm-broker socket)

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

  Postgres sidecar (app-only; docker container on SC_NET, data in a named volume).
  For developing/testing the fork's APP against real Postgres in the sandbox — the
  engine DB stays SQLite. One-time: ./sc pg-init (adds "pg" to instance.json).
  `launch` starts it + forwards DATABASE_URL (override with SC_DATABASE_URL); `down` stops it:
  ./sc pg-init             add the "pg" key to instance.json (enables the sidecar)
  ./sc pg-up               start the postgres:17 container; self-skips if unconfigured/already up
  ./sc pg-down             stop + remove the container (data volume retained)

  ./sc verify              rebuild + flat render + render-only boot (headless proof)
  ./sc health              curl the review layer's /api/health
  ./sc ports               show this fork's derived port
  ./sc preview             live-preview every dev shell's worktree UI on one port,
                             routed by subdomain (http://<shortname>.localhost:<dev_port>/)
  ./sc clean-db            remove the rebuilt .db (text serializations untouched)
EOF
    ;;
  *) echo "sc: unknown command '$cmd' (try ./sc help)" >&2; exit 2 ;;
esac
