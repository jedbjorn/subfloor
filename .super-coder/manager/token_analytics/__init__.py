"""Token Analytics mounted by the host user's manager.

Mount STATIC_DIR at /analytics/ and route GET /api/analytics/<name> to
read(name, parse_qs(query)). The manager authenticates and checks the origin
before dispatching POST /api/analytics/refresh to refresh(). Start one
scheduler with the manager and close it on shutdown.
"""
import logging
import sqlite3
import subprocess
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .extract import extract

STATIC_DIR = Path(__file__).with_name("static")
READS = {
    "filters": store.filters,
    "sessions": store.sessions,
    "usage": store.usage,
    "activity": store.activity,
    "engine-updates": store.engine_updates,
}
INTERVAL = 6 * 3600
RETRY = 15 * 60


class Analytics:
    def __init__(self, state_dir, repositories, *, engine_repo=None):
        """repositories() returns validated absolute checkout Paths, including stopped installs."""
        cache_dir = Path(state_dir) / "analytics"
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = cache_dir / "tokens.sqlite3"
        self.repositories = repositories
        self.engine_repo = engine_repo
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        with closing(store.connect(self.database)) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(store.SCHEMA)

    def read(self, name, query=None):
        with closing(store.connect(self.database)) as con:
            con.execute("BEGIN")
            if name == "status":
                return store.status(con)
            return READS[name](con, query or {})

    def _collect(self):
        started = store.now()
        errors = []
        with closing(store.connect(self.database)) as con:
            try:
                repos = list(self.repositories())
                for repo in repos:
                    repo = Path(repo)
                    try:
                        project = extract(repo, self.engine_repo)
                        with con:
                            store.ingest(con, project)
                    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                        project = {"project": repo.name, "repo": str(repo), "fatal": str(exc)}
                        with con:
                            store.ingest(con, project)
                    if project.get("fatal") or project.get("errors"):
                        errors.append(repo.name)
                error = "Collection incomplete for " + ", ".join(errors) if errors else None
            except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
                # Discovery can fail before returning any projects. Keep the
                # durable schedule and cached dashboard usable after restart.
                error = f"Collection failed: {exc}"
            with con:
                con.execute("INSERT INTO pulls(started_at,finished_at,ok,error) VALUES (?,?,?,?)",
                            (started, store.now(), error is None, error))
        return {"ok": False, "error": error} if error else {"ok": True}

    def refresh(self):
        with self._lock:
            return self._collect()

    def seconds_until_due(self):
        with closing(store.connect(self.database)) as con:
            last = con.execute("SELECT ok, finished_at FROM pulls ORDER BY pull_id DESC LIMIT 1").fetchone()
        if last is None:
            return 0
        finished = datetime.fromisoformat(last["finished_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - finished).total_seconds()
        return max(0, (INTERVAL if last["ok"] else RETRY) - age)

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    wait = self.seconds_until_due()
                    if wait <= 0 and not self._stop.is_set():
                        self._collect()
                        wait = self.seconds_until_due()
            except (OSError, sqlite3.Error):
                logging.getLogger(__name__).exception("Token Analytics cache unavailable")
                wait = RETRY
            # Recheck after manual refreshes, which can move the next attempt.
            self._stop.wait(min(wait, RETRY))

    def start(self):
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="token-analytics", daemon=True)
            self._thread.start()

    def close(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
