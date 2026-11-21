"""Read local owner installations using the reference dashboard's field projections."""
import os
import re
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from instance_state import active_database_path

USAGE_SQL = """
SELECT u.usage_id, u.harness, u.harness_session_ref, u.provider, u.model, u.title,
       u.started_at, u.ended_at, u.input_tokens, u.output_tokens,
       u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,
       u.status, u.captured_at, u.archive_id,
       COALESCE(sa.shortname, su.shortname) AS shell, a.session_id AS shell_session,
       su.flavor AS flavor
FROM session_token_usage u
LEFT JOIN shell_memory_archives a ON a.archive_id = u.archive_id
LEFT JOIN shells sa ON sa.shell_id = a.shell_id
LEFT JOIN shells su ON su.shell_id = u.shell_id
"""
FEATURES_SQL = """
SELECT r.feature_id, r.title, r.updated_at,
       EXISTS (SELECT 1 FROM documents d WHERE d.feature_id = r.feature_id
               AND d.kind = 'doc') AS has_doc
FROM roadmap r WHERE r.roadmap_status = 'shipped'
"""
SPECS_SQL = """
SELECT d.document_id, d.title, d.frozen_date, r.title AS feature_title
FROM documents d LEFT JOIN roadmap r ON r.feature_id = d.feature_id
WHERE d.kind = 'spec' AND d.frozen = 1
"""
# Shell activity for the "Shell Hours" view: every chat turn and every subagent
# run as a [start, end) interval. A live turn ends at its last heartbeat; a live
# subagent at extraction time. Rows without both ends are skipped.
TURNS_SQL = """
SELECT s.shortname, r.started_at, COALESCE(r.ended_at, r.heartbeat_at) AS ended_at
FROM conversation_runs r JOIN shells s ON s.shell_id = r.shell_id
WHERE r.started_at IS NOT NULL
"""
AGENTS_SQL = """
SELECT s.shortname, r.started_at,
       COALESCE(r.finished_at, CASE WHEN r.state = 'running' THEN ? END) AS ended_at
FROM runs r JOIN shells s ON s.shell_id = r.owner_shell_id
WHERE r.kind = 'agent' AND r.started_at IS NOT NULL
"""


def iso(ts):
    """Engine timestamps mix datetime('now') ('YYYY-MM-DD HH:MM:SS', UTC) and
    ISO-Z; emit ISO-Z so the browser never reads one as local time."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def activity(con, out):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spans = []
    for source, sql, args in (("turn", TURNS_SQL, ()), ("agent", AGENTS_SQL, (now,))):
        try:
            got = con.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            out["errors"].append(f"activity {source}: {exc}")
            return  # preserve the complete last good activity snapshot
        for r in got:
            s, e = iso(r[1]), iso(r[2])
            if s and e and e > s:
                spans.append({"source": source, "shell": r[0], "started_at": s, "ended_at": e})
    out["activity"] = spans


def git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args], capture_output=True,
        text=True, errors="replace", input=stdin, timeout=30, check=True,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})")


def engine_refs(repo, engine_repo):
    # Each pin-changing commit is an event, including a rollback to an old ref.
    patch = git(repo, "log", "--format=commit %H %ct", "--no-color", "--no-ext-diff",
                "--no-textconv", "--no-renames", "-p", "HEAD", "--", ".sc-state/engine.ref")
    refs = []
    for line in patch.splitlines():
        if line.startswith("commit "):
            _, commit, epoch = line.split()
        elif line.startswith("+") and SHA.fullmatch(line[1:].strip()):
            landed = datetime.fromtimestamp(int(epoch), timezone.utc)
            refs.append({"event": commit, "ref": line[1:].strip(), "landed_at": iso(landed.isoformat())})
    known = {(r["ref"], r["landed_at"]) for r in refs}
    current = None
    for name in ("engine.ref", "engine.ref.prev"):
        path = repo / ".sc-state" / name
        if path.resolve() != path or not path.is_file():
            continue
        ref = path.read_text().strip()
        if not SHA.fullmatch(ref):
            continue
        if name == "engine.ref":
            current = ref
        landed_at = iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
        if (ref, landed_at) not in known:
            refs.append({"event": f"pin:{ref}:{landed_at}", "ref": ref, "landed_at": landed_at})
            known.add((ref, landed_at))
    subjects = {}
    for checkout in dict.fromkeys((engine_repo, repo)):
        wanted = {r["ref"] for r in refs} - subjects.keys()
        if checkout is None or not wanted:
            continue
        # A fork may have none of the engine objects. Resolve all available
        # commits locally in two Git calls, independent of the history length.
        try:
            objects = git(checkout, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                          stdin="\n".join(wanted) + "\n")
        except subprocess.CalledProcessError:
            continue  # optional engine checkout may have moved

        commits = [line.split()[0] for line in objects.splitlines() if line.endswith(" commit")]
        if commits:
            log = git(checkout, "log", "--no-walk", "--no-notes", "--no-show-signature",
                      "--format=%H%x09%s", "--stdin", stdin="\n".join(commits) + "\n")
            subjects.update(line.split("\t", 1) for line in log.splitlines() if "\t" in line)
    for row in refs:
        row["subject"] = subjects.get(row["ref"])
    return refs, current


def file_version(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns


@contextmanager
def read_database(path):
    # mode=ro alone creates -wal and -shm on a stopped WAL database.
    # An immutable read of a checkpointed file leaves it untouched. Reject
    # that snapshot if a writer appeared or changed the file during the read.
    journals = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(journal.is_symlink() for journal in journals):
        raise ValueError("unexpected symlinked engine journal")
    wal, shm, _ = journals
    if wal.exists() != shm.exists():
        raise ValueError("engine WAL and shared-memory index are incomplete")
    version = file_version(path)
    quiet = not any(journal.exists() for journal in journals)
    uri = path.as_uri() + ("?mode=ro&immutable=1" if quiet else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("BEGIN")
        yield con
    if quiet and (file_version(path) != version or any(journal.exists() for journal in journals)):
        raise ValueError("engine database changed during collection")


def extract(repo, engine_repo=None):
    out = {"project": repo.name, "repo": str(repo), "errors": []}
    engine = repo / ".super-coder"
    if engine.resolve() != engine or repo.stat().st_uid != os.geteuid():
        raise ValueError("expected an owner-local primary checkout")
    try:
        out["engine_refs"], out["current_ref"] = engine_refs(repo, engine_repo)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        out["errors"].append(f"pin history: {exc}")
    try:
        read_project_database(engine, out)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        out["fatal"] = str(exc)
    return out


def read_project_database(engine, out):
    path = active_database_path(engine)
    if path.resolve() != path or not path.is_file() or path.stat().st_uid != os.geteuid():
        raise ValueError("engine database missing or outside its owner namespace")
    with read_database(path) as con:
        for key, sql in (("usage", USAGE_SQL), ("features", FEATURES_SQL), ("specs", SPECS_SQL)):
            try:
                out[key] = [dict(r) for r in con.execute(sql)]
            except sqlite3.Error as exc:
                if key == "usage":
                    out["fatal"] = f"usage read failed: {exc}"
                    return
                out["errors"].append(f"{key}: {exc}")
        activity(con, out)
