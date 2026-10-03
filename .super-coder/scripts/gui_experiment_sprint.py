"""Fixed, fixture-only Sprint bootstrap; no account discovery or native launch."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sprint_runtime import SprintRuntimeService

PURPOSE = 'native-sprint'
PLANNER_ID = 4


class SprintFixtureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def worker_status(raw: dict) -> dict:
    """Fixed local receipt vocabulary; no caller/native payload projection."""
    states={'starting','ready','stopped','cleanup_inconclusive'}
    codes={'SPRINT_RUNTIME_JOIN_PENDING','SPRINT_CLEANUP_JOIN_PENDING','SPRINT_PLANNER_CLOSE_PENDING'}
    if not isinstance(raw,dict) or not isinstance(raw.get('state'),str) or raw['state'] not in states:
        raise SprintFixtureError('SPRINT_STATUS_INVALID')
    allowed={'state','code','planner_model','planner_effort','planner_execution','model_pickup_proved',
             'runtime_joined','cleanup_worker_joined','planner_chats_closed','whole_unit_cleanup_verified'}
    if set(raw)-allowed:
        raise SprintFixtureError('SPRINT_STATUS_INVALID')
    for key,value in raw.items():
        if (key=='code' and (not isinstance(value,str) or value not in codes)
                or key in {'planner_model','planner_effort'} and value is not None
                or key=='planner_execution' and value!='operator_only'
                or key in {'model_pickup_proved','whole_unit_cleanup_verified'} and value is not False
                or key in {'runtime_joined','cleanup_worker_joined'} and value is not True
                or key=='planner_chats_closed' and (type(value) is not int or not 0<=value<=32)):
            raise SprintFixtureError('SPRINT_STATUS_INVALID')
    return dict(raw)


def executable_identity(path: Path, *, deadline: float) -> dict:
    """Hash a captured executable without following a mutable file descriptor."""
    if time.monotonic() >= deadline or not path.is_absolute():
        raise SprintFixtureError('SPRINT_EXECUTABLE_INVALID')
    resolved = path.resolve(strict=True)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111
                or before.st_size > 256 * 1024 * 1024):
            raise SprintFixtureError('SPRINT_EXECUTABLE_INVALID')
        digest = hashlib.sha256()
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise SprintFixtureError('SPRINT_METADATA_DEADLINE')
            part = os.read(fd, 65536)
            if not part:
                break
            size += len(part)
            if size > before.st_size:
                raise SprintFixtureError('SPRINT_EXECUTABLE_CHANGED')
            digest.update(part)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if size != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
            raise SprintFixtureError('SPRINT_EXECUTABLE_CHANGED')
        return {'sha256': digest.hexdigest(), 'device': before.st_dev, 'inode': before.st_ino}
    finally:
        os.close(fd)


class MetadataAlias:
    """Only the fixture API child resolves this actual captured Codex binary."""
    def __init__(self, root: Path, executable: Path, expected_sha256: str):
        self.root = root.resolve(strict=True)
        self.executable = executable.resolve(strict=True)
        self.expected_sha256 = expected_sha256
        self.directory = self.root / 'sprint-metadata-bin'
        self.alias = self.directory / 'codex'

    def prepare(self, *, deadline: float) -> str:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SprintFixtureError('SPRINT_ALIAS_INVALID')
        if not self.alias.is_symlink() and not self.alias.exists():
            self.alias.symlink_to(self.executable)
        self.verify(deadline=deadline)
        return str(self.directory) + os.pathsep + os.defpath

    def verify(self, *, deadline: float) -> None:
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or not self.alias.is_symlink()
                or os.readlink(self.alias) != str(self.executable)):
            raise SprintFixtureError('SPRINT_ALIAS_INVALID')
        if executable_identity(self.executable, deadline=deadline)['sha256'] != self.expected_sha256:
            raise SprintFixtureError('SPRINT_EXECUTABLE_CHANGED')


def role_rows(fixture_id: str) -> tuple[tuple[int, str, str, int], ...]:
    prefix = 'fx' + fixture_id[:10]
    return ((1, prefix + 'a', 'dev', 1), (2, prefix + 'b', 'reviewer', 1),
            (3, prefix + 'other', 'dev', 2), (PLANNER_ID, prefix + 'pln', 'planner', 1))


def seed_roles(con, fixture_id: str) -> tuple[tuple[int, str, str, int], ...]:
    """Only fresh synthetic rows, never imported shell credentials or routes."""
    rows = role_rows(fixture_id)
    if con.execute('SELECT 1 FROM shells LIMIT 1').fetchone() is not None:
        raise SprintFixtureError('SPRINT_ROLE_CONFLICT')
    for sid, short, flavor, owner in rows:
        con.execute('INSERT INTO shells(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (sid, short, short, flavor, 'Isolated Sprint fixture', owner, secrets.token_hex(32)))
    return rows


def validate_roles(con, fixture_id: str) -> None:
    for sid, short, flavor, owner in role_rows(fixture_id):
        row = con.execute('SELECT shortname,flavor,user_id,is_deleted,api_key FROM shells WHERE shell_id=?', (sid,)).fetchone()
        if (row is None or tuple(row[:4]) != (short, flavor, owner, 0)
                or not isinstance(row[4], str) or len(row[4]) != 64):
            raise SprintFixtureError('SPRINT_ROLE_CONFLICT')


def close_operator_chats(database: Path, fixture_id: str, *, deadline: float) -> int:
    """Canonical Close of undispatched operator Planner wake chats, after join."""
    import conversation_routes
    import db_driver
    con = db_driver.connect(database)
    try:
        con.execute('PRAGMA busy_timeout='+str(max(1, min(50, int((deadline-time.monotonic())*1000)))))
        validate_roles(con, fixture_id)
        rows = con.execute('SELECT c.conversation_id,c.version,c.state,c.runtime_mode,c.owner_user_id,c.model,c.effort '
                           'FROM conversations c JOIN sprint_participant_conversations link USING(conversation_id) '
                           'JOIN sprint_participants p ON p.participant_id=link.sprint_participant_id '
                           "WHERE p.shell_id=? AND p.role='planner' AND c.shell_id=p.shell_id LIMIT 33", (PLANNER_ID,)).fetchall()
        if len(rows) > 32:
            raise SprintFixtureError('SPRINT_PLANNER_OBLIGATION_UNKNOWN')
        closed = 0
        for row in rows:
            if time.monotonic() >= deadline:
                raise SprintFixtureError('SPRINT_SHUTDOWN_DEADLINE')
            if (row['runtime_mode'] != 'ephemeral' or row['owner_user_id'] != 1
                    or row['model'] is not None or row['effort'] is not None):
                raise SprintFixtureError('SPRINT_PLANNER_OBLIGATION_UNKNOWN')
            cid = row['conversation_id']
            if con.execute("SELECT 1 FROM conversation_runs WHERE conversation_id=?", (cid,)).fetchone() or con.execute('SELECT 1 FROM active_shell_chats WHERE chat_id=? AND process_pid IS NOT NULL', (cid,)).fetchone():
                raise SprintFixtureError('SPRINT_PLANNER_EXECUTION_UNKNOWN')
            if row['state'] != 'closed':
                conversation_routes._patch_conversation(con, {'user_id': 1}, cid,
                                                        {'version': row['version'], 'state': 'closed'})
                closed += 1
        return closed
    finally:
        con.close()


def require_durable_cleanup(database: Path, *, deadline: float) -> None:
    """Read only this retained synthetic DB after unit exit; never repair it."""
    if time.monotonic()>=deadline or database.is_symlink():
        raise SprintFixtureError('SPRINT_DURABLE_CLEANUP_UNKNOWN')
    con=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=max(.001,min(.05,deadline-time.monotonic())))
    try:
        if con.execute("SELECT 1 FROM sprint_cleanup_targets WHERE target_kind='artifact_dir' AND state<>'succeeded' LIMIT 1").fetchone():
            raise SprintFixtureError('SPRINT_DURABLE_CLEANUP_UNKNOWN')
        for state,raw in con.execute('SELECT state,cleanup_json FROM conversation_runtime_generations LIMIT 65'):
            cleanup=json.loads(raw)
            if (state!='closed' or cleanup.get('outcome')!='complete'
                    or cleanup.get('unit_verified_exited') is not True
                    or cleanup.get('unresolved_work')!=[] or cleanup.get('unresolved_definitions')!=[]):
                raise SprintFixtureError('SPRINT_DURABLE_CLEANUP_UNKNOWN')
        if con.execute('SELECT COUNT(*) FROM conversation_runtime_generations').fetchone()[0]>64 or time.monotonic()>=deadline:
            raise SprintFixtureError('SPRINT_DURABLE_CLEANUP_UNKNOWN')
    except (sqlite3.Error,ValueError,TypeError,AttributeError):
        raise SprintFixtureError('SPRINT_DURABLE_CLEANUP_UNKNOWN') from None
    finally:
        con.close()


class SprintSeat:
    """Own only existing Sprint runtime and its non-daemon cleanup worker."""
    def __init__(self, database: Path, fixture_id: str, *, record):
        self.database, self.fixture_id, self.record = database, fixture_id, record
        self.service: SprintRuntimeService | None = None

    def start(self, *, deadline: float) -> None:
        import sprint_runtime
        if time.monotonic() >= deadline or sprint_runtime.service() is not None:
            raise SprintFixtureError('SPRINT_SERVICE_CONFLICT')
        self.record({'state': 'starting', 'planner_execution': 'operator_only'})
        self.service = sprint_runtime.start_service(self.database)
        if (not self.service.wait_ready(max(0, min(5, deadline - time.monotonic())))
                or not self.service.is_alive() or time.monotonic() >= deadline):
            self.service.stop()
            raise SprintFixtureError('SPRINT_SERVICE_UNAVAILABLE')
        self.record({'state': 'ready', 'planner_model': None, 'planner_effort': None,
                     'planner_execution': 'operator_only', 'model_pickup_proved': False})

    def shutdown(self, *, deadline: float) -> None:
        if self.service is None:
            return
        self.service.stop()
        self.service.join(max(0, deadline - time.monotonic()))
        if self.service.is_alive():
            self.record({'state': 'cleanup_inconclusive', 'code': 'SPRINT_RUNTIME_JOIN_PENDING'})
            raise SprintFixtureError('SPRINT_RUNTIME_JOIN_PENDING')
        # Joining main first fences creation of another cleanup worker.
        worker = self.service._cleanup_thread
        if worker is not None:
            worker.join(max(0, deadline - time.monotonic()))
            if worker.is_alive():
                self.record({'state': 'cleanup_inconclusive', 'code': 'SPRINT_CLEANUP_JOIN_PENDING'})
                raise SprintFixtureError('SPRINT_CLEANUP_JOIN_PENDING')
        try:
            closed = close_operator_chats(self.database, self.fixture_id, deadline=deadline)
        except Exception:  # noqa: BLE001 - static code retains all failed cleanup obligations
            self.record({'state': 'cleanup_inconclusive', 'code': 'SPRINT_PLANNER_CLOSE_PENDING'})
            raise SprintFixtureError('SPRINT_PLANNER_CLOSE_PENDING') from None
        self.record({'state': 'stopped', 'runtime_joined': True, 'cleanup_worker_joined': True,
                     'planner_chats_closed': closed, 'whole_unit_cleanup_verified': False})
