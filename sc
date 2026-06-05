#!/bin/sh
# super-coder entry point — the dispatcher. All engine logic lives here so it
# travels with a fork (install.py checks out .super-coder + sc). Host commands
# (launch/enter/down) drive a docker sandbox; the in-container primitives
# (serve/boot) need only python3 + sqlite3 and double as the no-docker host
# escape hatch. Run from the repo root:  ./sc <command> [args]   ·   ./sc help
set -e
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$here"

ENGINE=.super-coder
PY="${SC_PYTHON:-python3}"
DB="$ENGINE/shell_db.db"
S="$ENGINE/scripts"

port() { "$PY" "$S/ports.py" port; }

# Host-side docker orchestration (raw docker — no compose plugin dependency).
# The sandbox runs as you (uid/gid → no root-owned files), bind-mounts this repo
# at its host path + your harness creds rw, and publishes this fork's derived
# port to 127.0.0.1 only. The in-container primitives (`serve`, `boot`) need no
# docker and so run the same whether on the host or inside the container.
IMG=super-coder-sandbox
CNAME="sc-$(basename "$here")"   # unique per fork, like the pm2 name

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
  mkdir -p "$HOME/.claude" "$HOME/.config/opencode" "$HOME/.local/share/opencode" 2>/dev/null || true
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

# Build the env image (the repo is bind-mounted at run time, never baked — see
# .dockerignore: the build context is empty). Cheap to re-run; layers cache.
dbuild() {
  docker build -t "$IMG" -f "$ENGINE/Dockerfile" \
    --build-arg SC_USER="$(id -un)" \
    --build-arg SC_UID="$(id -u)" \
    --build-arg SC_GID="$(id -g)" \
    "$here"
}

cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-docker ;;
  update)       exec "$PY" "$S/update.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  rebuild)      exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      exec "$PY" "$S/migrate.py" "$DB" ;;
  snapshot)     exec "$PY" "$S/snapshot.py" ;;
  render)       [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  map)          exec "$PY" "$S/map_repo.py" ;;
  seed-skills)  exec "$PY" "$S/seed_skills.py" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  # ── in-container primitives (no docker; also the host escape hatch) ──
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  boot)         exec "$PY" "$S/run.py" "$@" ;;
  boot-*)       exec "$PY" "$S/run.py" "${cmd#boot-}" "$@" ;;
  # ── docker sandbox (host-side; the default way to run) ──
  launch)
    dcheck
    dcreds
    "$PY" "$S/ports.py" ensure >/dev/null
    p="$(port)"
    dbuild
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
    docker run -d --name "$CNAME" --restart unless-stopped \
      --user "$(duser)" \
      -e HOME="$HOME" -e SC_BIND=0.0.0.0 -e SC_PYTHON=python3 -e PYTHONUNBUFFERED=1 \
      -e SC_SANDBOX=1 \
      -w "$here" \
      -v "$here:$here" \
      -v "$HOME/.claude:$HOME/.claude" \
      -v "$HOME/.claude.json:$HOME/.claude.json" \
      -v "$HOME/.config/opencode:$HOME/.config/opencode" \
      -v "$HOME/.local/share/opencode:$HOME/.local/share/opencode" \
      -p "127.0.0.1:$p:$p" \
      "$IMG" ./sc serve --port "$p" >/dev/null
    echo "→ sandbox up · review GUI at http://127.0.0.1:$p"
    echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)" ;;
  enter)        exec docker exec -it "$CNAME" ./sc boot "$@" ;;
  enter-*)      exec docker exec -it "$CNAME" ./sc boot "${cmd#enter-}" "$@" ;;
  down)         docker rm -f "$CNAME" >/dev/null 2>&1 && echo "→ sandbox stopped" || echo "→ not running" ;;
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
  ./sc ensure-harness      install claude + opencode if missing (official native installers, no npm)
  ./sc doctor              sandbox readiness: docker (rootless/rootful) + harness login
  ./sc update              self-fetch the engine + reconcile IN PLACE (migrate, sync skills, map); --no-fetch to skip fetch
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc snapshot            dump per-instance tables -> .super-coder/snapshot/content.sql
  ./sc render              render tracked flat _sc files (specs/docs/skills/roadmap)
  ./sc map                 scan the host repo into the dr_* catalogue (re-runnable)
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
  ./sc build               (re)build the sandbox image
  ./sc logs                tail the sandbox server logs

  Primitives (run inside the container; also the no-docker host escape hatch):
  ./sc serve               run the review layer (api + static UI) in the foreground
  ./sc boot [shortname]    auth + pick shell + pick harness + boot (no container, no GUI)

  ./sc verify              rebuild + flat render + render-only boot (headless proof)
  ./sc health              curl the review layer's /api/health
  ./sc ports               show this fork's derived port
  ./sc clean-db            remove the rebuilt .db (text serializations untouched)
EOF
    ;;
  *) echo "sc: unknown command '$cmd' (try ./sc help)" >&2; exit 2 ;;
esac
