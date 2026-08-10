#!/bin/sh
# Bare-metal lifecycle seam for sc-cachy; all other verbs delegate upstream.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENGINE="$ROOT/.super-coder"
PY="${SC_PYTHON:-python3}"
S="$ENGINE/scripts"
PIDFILE="$ENGINE/run/server.pid"
LOGFILE="$ENGINE/run/server.log"
DB="$ENGINE/shell_db.db"
CMD="${1:-}"
[ $# -gt 0 ] && shift

port() { "$PY" "$S/ports.py" port; }
devport() { "$PY" "$S/ports.py" devport; }

server_alive() {
  [ -f "$PIDFILE" ] || return 1
  pid="$(sed -n '1p' "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -q "\.super-coder/api/server\.py"
}

orphan_pids() {
  command -v fuser >/dev/null 2>&1 || return 0
  serve_port="$(port)"
  root_real="$(readlink -f "$ROOT" 2>/dev/null || printf '%s' "$ROOT")"
  for candidate in $(fuser -n tcp "$serve_port" 2>/dev/null || true); do
    args="$(ps -p "$candidate" -o args= 2>/dev/null || true)"
    cwd="$(readlink -f "/proc/$candidate/cwd" 2>/dev/null || true)"
    case "$args" in
      *".super-coder/api/server.py"*"--port $serve_port"*)
        [ "$cwd" = "$root_real" ] && printf '%s\n' "$candidate"
        ;;
    esac
  done
}

server_up() {
  "$PY" "$S/ports.py" ensure >/dev/null
  serve_port="$(port)"
  if server_alive; then
    echo "→ host server already running (pid $(sed -n '1p' "$PIDFILE")) · Review GUI http://127.0.0.1:$serve_port"
    return 0
  fi
  mkdir -p "$ENGINE/run"
  rm -f "$PIDFILE"
  nohup env SC_BIND=127.0.0.1 PYTHONUNBUFFERED=1 \
    "$PY" "$ENGINE/api/server.py" --port "$serve_port" >"$LOGFILE" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$PIDFILE"
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:$serve_port/api/health" >/dev/null 2>&1; then
      echo "→ bare-metal server up (pid $pid) · Review GUI http://127.0.0.1:$serve_port"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    attempts=$((attempts + 1))
    sleep 0.1
  done
  echo "host_sc: server failed to become healthy; see $LOGFILE" >&2
  rm -f "$PIDFILE"
  return 1
}

server_down() {
  if server_alive; then
    kill "$(sed -n '1p' "$PIDFILE")"
    echo "→ bare-metal server stopped"
  else
    pids="$(orphan_pids)"
    if [ -n "$pids" ]; then
      for pid in $pids; do kill "$pid"; done
      echo "→ orphaned bare-metal server stopped"
    else
      echo "→ bare-metal server not running"
    fi
  fi
  rm -f "$PIDFILE"
}

case "$CMD" in
  launch) server_up ;;
  enter)
    export SC_TRUSTED_HOST=1 SC_DEV_PORT="$(devport)"
    exec "$ROOT/sc" boot "$@"
    ;;
  enter-*)
    export SC_TRUSTED_HOST=1 SC_DEV_PORT="$(devport)"
    exec "$ROOT/sc" "boot-${CMD#enter-}" "$@"
    ;;
  down) server_down ;;
  restart)
    "$PY" "$S/db_backup.py" backup "$DB" "$ROOT" prerestart
    server_down
    server_up
    ;;
  logs)
    [ -f "$LOGFILE" ] || {
      echo "host_sc: no host server log yet; run make dos-l" >&2
      exit 1
    }
    exec tail -f "$LOGFILE"
    ;;
  *) exec "$ROOT/sc" "$CMD" "$@" ;;
esac
