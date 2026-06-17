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
_sc_find_manifests() {  # $1 = filename glob, e.g. 'requirements*.txt'
  find "$here" \
    \( -name node_modules -o -name .venv -o -name venv -o -name .super-coder \
       -o -name .sc-state -o -name .git -o -name __pycache__ \
       -o -name dist -o -name build -o -name vendor \) -prune -o \
    -name "$1" -type f -print
}

# Install the fork's deps into the bind-mounted repo: one repo-root .venv from every
# requirements*.txt (fork pins win), then an engine baseline test kit layered on with
# only-if-needed so `./sc test` works even if the fork's reqs omit pytest; plus
# `npm ci` for each package.json. Discovery is a glob walk (map-independent — runs on
# a fresh fork before `./sc map`). A map-backed fast path would read dr_filepath.path
# (dr_dependency.source_file is basename-only, so it can't locate a manifest's dir).
sc_deps() {
  rc=0
  venv="$here/.venv"
  reqs="$(_sc_find_manifests 'requirements*.txt')"
  base_reqs="$(printf '%s\n' "$reqs" | grep -v 'requirements-dev\.txt' || true)"
  if [ -n "$base_reqs" ]; then
    if [ ! -x "$venv/bin/python" ]; then
      echo "→ deps: creating $venv"
      "$PY" -m venv "$venv" || { echo "✗ deps: venv create failed" >&2; return 1; }
    fi
    # Fork pins first (authoritative), with a sibling requirements-dev.txt if present.
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
    echo "→ deps: no requirements*.txt or package.json found — nothing to install"
  fi
  [ "$rc" -eq 0 ] || { echo "✗ deps: one or more installs failed" >&2; return 1; }
  echo "✓ deps: done"
}

# Run the fork's test suites: backend (the .venv's pytest, honoring the fork's
# pytest.ini; else the engine's own stdlib-unittest suite) + UI (npm run test /
# vitest in any package.json dir that declares a test script). Non-zero if any fail.
sc_test() {
  rc=0
  venv="$here/.venv"
  if [ -x "$venv/bin/pytest" ]; then
    echo "→ test: $venv/bin/pytest"
    ( cd "$here" && "$venv/bin/pytest" "$@" ) || rc=1
  elif ls "$here"/tests/test_*.py >/dev/null 2>&1; then
    echo "→ test: python3 -m unittest discover (stdlib)"
    ( cd "$here" && "$PY" -m unittest discover -s tests -p 'test_*.py' ) || rc=1
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

# Lint + format-check the fork's python with the .venv's ruff (honors the fork's
# [tool.ruff] config). Defaults to the repo root; pass paths/flags through. The
# tool is available, not enforced — a fork opts in by running this. Needs `./sc deps`.
sc_lint() {
  venv="$here/.venv"
  [ -x "$venv/bin/ruff" ] || { echo "✗ lint: no .venv/bin/ruff — run ./sc deps first" >&2; return 1; }
  echo "→ lint: $venv/bin/ruff check"
  ( cd "$here" && "$venv/bin/ruff" check "$@" )
}

# Type-check the fork's python with the .venv's mypy (honors the fork's
# [tool.mypy] config). Same available-not-enforced stance as lint. Needs `./sc deps`.
sc_typecheck() {
  venv="$here/.venv"
  [ -x "$venv/bin/mypy" ] || { echo "✗ typecheck: no .venv/bin/mypy — run ./sc deps first" >&2; return 1; }
  echo "→ typecheck: $venv/bin/mypy"
  if [ $# -gt 0 ]; then ( cd "$here" && "$venv/bin/mypy" "$@" )
  else ( cd "$here" && "$venv/bin/mypy" . ); fi
}

# ── Windows VM broker (HOST-side; drives the test VM for sandboxed forks) ──────
# A separate host process — the sandbox server can't hold the ssh key or reach
# libvirt. It listens on a unix socket in the bind-mounted engine dir so
# windows_devkit (in the container) can curl it without a route or a key. Refuses
# to run in the sandbox (vm_broker.py guards on SC_SANDBOX). Supervise it however
# the host prefers; vm-broker-up is a dependency-free nohup+pidfile default.
VM_BROKER_PID="$ENGINE/run/vm-broker.pid"
sc_vm_broker_up() {
  mkdir -p "$ENGINE/run"
  if [ -f "$VM_BROKER_PID" ] && kill -0 "$(cat "$VM_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    echo "→ vm-broker already running (pid $(cat "$VM_BROKER_PID"))"; return 0
  fi
  nohup "$PY" "$ENGINE/api/vm_broker.py" >"$ENGINE/run/vm-broker.log" 2>&1 &
  echo $! > "$VM_BROKER_PID"
  echo "→ vm-broker up (pid $!) · socket $("$PY" "$S/vm.py" sock) · log $ENGINE/run/vm-broker.log"
}
sc_vm_broker_down() {
  if [ -f "$VM_BROKER_PID" ] && kill -0 "$(cat "$VM_BROKER_PID" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$VM_BROKER_PID")" && echo "→ vm-broker stopped"
  else
    echo "→ vm-broker not running"
  fi
  rm -f "$VM_BROKER_PID"
}

cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-docker ;;
  update)            exec "$PY" "$S/update.py" "$@" ;;
  update-harnesses) exec "$PY" "$S/install.py" --update-harnesses ;;
  rollback)     exec "$PY" "$S/rollback.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  rebuild)      exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      exec "$PY" "$S/migrate.py" "$DB" ;;
  snapshot)     exec "$PY" "$S/snapshot.py" ;;
  mem)          exec "$PY" "$S/mem.py" "$@" ;;
  render)       [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  render-check) exec "$PY" "$S/render_check.py" ;;
  map)          exec "$PY" "$S/map_repo.py" ;;
  map-setup)    exec "$PY" "$S/map_setup.py" ;;
  seed-skills)  exec "$PY" "$S/seed_skills.py" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  preview)      exec "$PY" "$S/preview.py" "$@" ;;
  # ── in-container primitives (no docker; also the host escape hatch) ──
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  # ── Windows VM broker (HOST-side primitive — runs where virsh + the key live) ──
  vm-broker)      exec "$PY" "$ENGINE/api/vm_broker.py" "$@" ;;
  vm-broker-up)   sc_vm_broker_up ;;
  vm-broker-down) sc_vm_broker_down ;;
  vm-broker-sock) exec "$PY" "$S/vm.py" sock ;;
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
    git_name="$(git -C "$here" config user.name 2>/dev/null || true)"
    git_email="$(git -C "$here" config user.email 2>/dev/null || true)"
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
    docker run -d --name "$CNAME" --restart unless-stopped \
      --network "$SC_NET" \
      --user "$(duser)" \
      -e HOME="$HOME" -e SC_BIND=0.0.0.0 -e SC_PYTHON=python3 -e PYTHONUNBUFFERED=1 \
      -e SC_SANDBOX=1 -e SC_DEV_PORT="$dp" \
      -e GH_TOKEN="$gh_token" $mistral_env \
      -e GIT_AUTHOR_NAME="$git_name" -e GIT_AUTHOR_EMAIL="$git_email" \
      -e GIT_COMMITTER_NAME="$git_name" -e GIT_COMMITTER_EMAIL="$git_email" \
      -w "$here" \
      -v "$here:$here" \
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
    echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)" ;;
  enter)        exec docker exec -it "$CNAME" ./sc boot "$@" ;;
  enter-*)      exec docker exec -it "$CNAME" ./sc boot "${cmd#enter-}" "$@" ;;
  down)         docker rm -f "$CNAME" >/dev/null 2>&1 && echo "→ sandbox stopped" || echo "→ not running" ;;
  restart)      "$0" down; exec "$0" launch "$@" ;;
  build)        dcheck; dbuild ;;
  logs)         exec docker logs -f "$CNAME" ;;
  verify)
    "$PY" "$S/rebuild.py"
    "$PY" "$S/render.py" flat
    RENDER_ONLY=1 exec "$PY" "$S/run.py" --first ;;
  health)       curl -s "http://127.0.0.1:$(port)/api/health" && echo "" ;;
  clean-db)     rm -f "$DB" "$DB-wal" "$DB-shm" && echo "removed $DB (rebuild with: ./sc rebuild)" ;;
  help|-h|--help)
    cat <<'EOF'
super-coder — forkable shell substrate

  ./sc install             first-launch bootstrap for a fork (requirements, harness, first shell)
  ./sc ensure-harness      install claude + opencode + codex if missing (official native installers, no npm)
  ./sc doctor              sandbox readiness: docker (rootless/rootful) + harness login
  ./sc update              fetch + materialize the engine (gitignored dep) + reconcile IN PLACE (migrate, sync skills, map); --no-fetch to skip fetch
  ./sc update-harnesses    update claude + opencode + codex + vibe to latest (force-reruns official installers)
  ./sc rollback            sound undo of a bad update — restore the DB + engine (engine.ref.prev) together
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc snapshot            dump per-instance tables -> .sc-state/content.sql
  ./sc mem <cmd> [args]    safe engine-DB writes for a shell's own memory (state/seed/lns/decision/flag/roadmap/doc/narrative);
                             resolves + guards the engine DB (refuses product DBs & 0-byte stubs), then snapshots+renders. `./sc mem which` to orient
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
  holds the ssh key + virsh so the fork never does. See docs/windows-vm-broker.md):
  ./sc vm-broker           run the broker in the foreground (unix socket)
  ./sc vm-broker-up        start it in the background (nohup + pidfile)
  ./sc vm-broker-down      stop the backgrounded broker
  ./sc vm-broker-sock      print the broker's socket path

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
