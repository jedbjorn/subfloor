#!/bin/sh
# super-coder entry point — the dispatcher. All engine logic lives here so it
# travels with a fork (install.py checks out .super-coder + sc). The shell runs
# directly on the host (no container): `enter`/`boot` boot a harness in this repo
# with allow-all permissions; `launch` backgrounds the optional review GUI. Needs
# only python3 + sqlite3. Run from the repo root:  ./sc <command> [args] · ./sc help
set -e
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$here"

ENGINE=.super-coder
PY="${SC_PYTHON:-python3}"
DB="$ENGINE/shell_db.db"
S="$ENGINE/scripts"

port() { "$PY" "$S/ports.py" port; }

# The review GUI (api + static UI) runs as a backgrounded host process; its pid
# + log live under .super-coder/run/ so `down` can stop it and `logs` can tail
# it. Booting a harness needs none of this — `enter`/`boot` just exec run.py on
# the host, in this repo, with allow-all permissions (the host IS the trust
# boundary now; SC_TRUST=0 reverts to normal prompts).
RUN="$ENGINE/run"
PIDFILE="$RUN/serve.pid"
LOGFILE="$RUN/serve.log"

cmd="${1:-help}"; [ $# -gt 0 ] && shift

case "$cmd" in
  install)         exec "$PY" "$S/install.py" "$@" ;;
  ensure-harness)  exec "$PY" "$S/install.py" --ensure-harness ;;
  doctor)          exec "$PY" "$S/install.py" --check-host ;;
  update)       exec "$PY" "$S/update.py" "$@" ;;
  init)         exec "$PY" "$S/init_fork.py" "$@" ;;
  rebuild)      exec "$PY" "$S/rebuild.py" "$@" ;;
  migrate)      exec "$PY" "$S/migrate.py" "$DB" ;;
  snapshot)     exec "$PY" "$S/snapshot.py" ;;
  render)       [ $# -gt 0 ] && exec "$PY" "$S/render.py" "$@" || exec "$PY" "$S/render.py" flat ;;
  map)          exec "$PY" "$S/map_repo.py" ;;
  map-setup)    exec "$PY" "$S/map_setup.py" ;;
  seed-skills)  exec "$PY" "$S/seed_skills.py" ;;
  ports)        exec "$PY" "$S/ports.py" show ;;
  # ── boot a harness on the host (the way to run) ──
  serve)        exec "$PY" "$ENGINE/api/server.py" "$@" ;;
  boot)         exec "$PY" "$S/run.py" "$@" ;;
  boot-*)       exec "$PY" "$S/run.py" "${cmd#boot-}" "$@" ;;
  enter)        exec "$PY" "$S/run.py" "$@" ;;
  enter-*)      exec "$PY" "$S/run.py" "${cmd#enter-}" "$@" ;;
  # ── review GUI (optional; backgrounded host process, 127.0.0.1 only) ──
  launch)
    "$PY" "$S/ports.py" ensure >/dev/null
    p="$(port)"
    mkdir -p "$RUN"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "→ review GUI already up (pid $(cat "$PIDFILE")) · http://127.0.0.1:$p"
    else
      nohup "$PY" "$ENGINE/api/server.py" --port "$p" >"$LOGFILE" 2>&1 &
      echo $! > "$PIDFILE"
      echo "→ review GUI up (pid $(cat "$PIDFILE")) · http://127.0.0.1:$p"
    fi
    echo "  boot a shell:  ./sc enter   (or ./sc enter-<shortname>)" ;;
  down)
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
      rm -f "$PIDFILE"; echo "→ review GUI stopped"
    else
      rm -f "$PIDFILE"; echo "→ not running"
    fi ;;
  logs)
    if [ -f "$LOGFILE" ]; then exec tail -f "$LOGFILE"
    else echo "→ no log yet — run ./sc launch first"; exit 1; fi ;;
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
  ./sc doctor              host readiness: harness installed + harness login
  ./sc update              self-fetch the engine + reconcile IN PLACE (migrate, sync skills, map); --no-fetch to skip fetch
  ./sc rebuild             build the .db from schema + migrations + snapshot
  ./sc migrate             apply pending migrations to an existing .db
  ./sc snapshot            dump per-instance tables -> .super-coder/snapshot/content.sql
  ./sc render              render tracked flat _sc files (specs/docs/skills/roadmap)
  ./sc map                 scan the host repo into the dr_* catalogue (re-runnable)
  ./sc map-setup           wire the auto-remap git hooks (core.hooksPath) + map — the cartographer's one-shot
  ./sc seed-skills         regenerate the skills seed migration from assets/skills/
  ./sc init                seed a fresh fork's first user + shell (run once after install)

  Run on the host (allow-all permissions — no container; you trust the model +
  this repo. Off-switch: SC_TRUST=0 keeps normal permission prompts):
  ./sc enter               boot a shell: auth + pick shell + pick harness + boot
  ./sc enter-<shortname>   boot that shell directly (skip the shell picker)
                             harness: --harness <name> or HARNESS=<name> forces it; else when
                             >1 harness is on PATH you're prompted (per-launch, not persisted)
  ./sc boot [shortname]    alias of enter (the in-engine primitive name)

  Review GUI (optional; backgrounded host process, 127.0.0.1 only):
  ./sc launch              start the review layer (api + static UI) in the background
  ./sc serve               run the review layer in the foreground instead
  ./sc down                stop the backgrounded review layer
  ./sc logs                tail the review layer log

  ./sc verify              rebuild + flat render + render-only boot (headless proof)
  ./sc health              curl the review layer's /api/health
  ./sc ports               show this fork's derived port
  ./sc clean-db            remove the rebuilt .db (text serializations untouched)
EOF
    ;;
  *) echo "sc: unknown command '$cmd' (try ./sc help)" >&2; exit 2 ;;
esac
