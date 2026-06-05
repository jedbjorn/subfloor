#!/bin/sh
# super-coder entry point — a thin dispatcher so super-coder never owns the host
# repo's root Makefile. Needs only python3 + sqlite3 (+ pm2 for the GUI). Run
# from the repo root:  ./sc <command> [args]   ·   ./sc help
set -e
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$here"

ENGINE=.super-coder
PY="${SC_PYTHON:-python3}"
DB="$ENGINE/shell_db.db"
ECO="$ENGINE/ecosystem.config.cjs"
S="$ENGINE/scripts"

port() { "$PY" "$S/ports.py" port; }
gui_up() {
  "$PY" "$S/ports.py" ensure >/dev/null
  pm2 start "$ECO" >/dev/null 2>&1 || true
  echo "→ review GUI at http://127.0.0.1:$(port)"
}

cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  update)       exec "$PY" "$S/update.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  rebuild)      exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      exec "$PY" "$S/migrate.py" "$DB" ;;
  snapshot)     exec "$PY" "$S/snapshot.py" ;;
  render)       [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  map)          exec "$PY" "$S/map_repo.py" ;;
  seed-skills)  exec "$PY" "$S/seed_skills.py" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  launch)       gui_up; exec "$PY" "$S/run.py" "$@" ;;
  launch-*)     gui_up; exec "$PY" "$S/run.py" "${cmd#launch-}" ;;
  verify)
    "$PY" "$S/rebuild.py"
    "$PY" "$S/render.py" flat
    RENDER_ONLY=1 exec "$PY" "$S/run.py" --first ;;
  up)
    "$PY" "$S/ports.py" ensure >/dev/null
    pm2 start "$ECO" && echo "→ review layer up at http://127.0.0.1:$(port)" ;;
  down)         pm2 delete "$ECO" 2>/dev/null && echo "→ review layer stopped" || echo "→ not running" ;;
  restart)      exec pm2 restart "$ECO" ;;
  health)       curl -s "http://127.0.0.1:$(port)/api/health" && echo "" ;;
  clean-db)     rm -f "$DB" "$DB-wal" "$DB-shm" && echo "removed $DB (rebuild with: ./sc rebuild)" ;;
  help|-h|--help)
    cat <<'EOF'
super-coder — forkable shell substrate

  ./sc install             first-launch bootstrap for a fork (requirements, harness, first shell)
  ./sc ensure-harness      install claude + opencode if missing (official native installers, no npm)
  ./sc update              self-fetch the engine + reconcile IN PLACE (migrate, sync skills, map); --no-fetch to skip fetch
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc snapshot            dump per-instance tables -> .super-coder/snapshot/content.sql
  ./sc render              render tracked flat _sc files (specs/docs/skills/roadmap)
  ./sc map                 scan the host repo into the dr_* catalogue (re-runnable)
  ./sc seed-skills         regenerate the skills seed migration from assets/skills/
  ./sc init                seed a fresh fork's first user + shell (run once after install)
  ./sc launch              start the GUI (prints its URL) + auth + pick shell + pick harness + boot
  ./sc launch-<shortname>  boot that shell directly (skip shell picker); also starts the GUI
                             harness: --harness <name> or HARNESS=<name> forces it; else when
                             >1 harness is on PATH you're prompted (per-launch, not persisted)
  ./sc verify              rebuild + flat render + render-only boot (headless proof)
  ./sc up / down / restart start/stop the localhost review layer (pm2, per-fork port)
  ./sc serve               run the review layer in the foreground (no pm2)
  ./sc health              curl the review layer's /api/health
  ./sc ports               show this fork's derived port
  ./sc clean-db            remove the rebuilt .db (text serializations untouched)
EOF
    ;;
  *) echo "sc: unknown command '$cmd' (try ./sc help)" >&2; exit 2 ;;
esac
