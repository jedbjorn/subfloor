#!/usr/bin/env python3
"""One experimental generation's private controller and bounded local journal.

No API imports, shell memory, global daemon, production startup or auto-restart.
The enclosing owned user-systemd unit is the independent final cleanup backstop.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import importlib.util
import json
import math
import os
import queue
import socket
import sqlite3
import stat
import struct
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from conversation_runtime_contract import (
    CONTRACT_REVISION,
    MAX_FRAME_BYTES,
    OUTPUT_KINDS,
    ExecutableBinding,
    NativeControl,
    NativeReference,
    NativeSubmission,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    StartupConsent,
    WriteReceipt,
    payload_digest,
    public_payload,
)


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def private_directory(root: Path) -> None:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeContractError("OWNERSHIP_INVALID", "controller requires its owner-only state directory")


def start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


class Journal:
    """Finite event/outcome bridge with non-expiring uncertain command identity.

    Resolved contiguous command prefixes compact into an ordinal rejection
    floor. Live/unknown commands never compact. Essential events compact only
    after canonical API acknowledgement; ephemeral output may drop explicitly.
    """
    def __init__(self, path: Path, *, max_events: int = 2048, max_commands: int = 512,
                 max_bytes: int = 4 * 1024 * 1024, reserve: int = 32,
                 sensitive_values: tuple[str, ...] = ()):
        private_directory(path.parent)
        if path.exists() and (path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise RuntimeContractError("OWNERSHIP_INVALID", "unsafe controller journal")
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,ordinal INTEGER UNIQUE,
            digest TEXT,kind TEXT,state TEXT,receipt TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY,kind TEXT,payload TEXT);
        """)
        self.lock = threading.RLock()
        self.max_events, self.max_commands, self.max_bytes, self.reserve = max_events, max_commands, max_bytes, reserve
        self.secrets = sensitive_values
        defaults: dict[str,Any] = {"sequence":0,"floor":0,"acked":0,"partial":False,
                           "primary":None,"close":False,"lease":None,"root_activities":{},
                           "setup":None,"setup_confirmation":None}
        for key,value in defaults.items():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, encoded(value)))

    def get(self, key: str) -> Any:
        return json.loads(self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0])

    def set(self, key: str, value: Any) -> None:
        self.db.execute("UPDATE meta SET value=? WHERE key=?", (encoded(value), key))

    def _compact(self) -> None:
        floor = self.get("floor")
        for row in self.db.execute("SELECT ordinal,state FROM commands ORDER BY ordinal").fetchall():
            if row["ordinal"] != floor + 1 or row["state"] not in {"terminal", "rejected", "unsupported"}:
                break
            floor += 1
        self.set("floor", floor)
        self.db.execute("DELETE FROM commands WHERE ordinal<=?", (floor,))
        self.db.execute("DELETE FROM events WHERE sequence<=?", (self.get("acked"),))

    def reserve_command(self, cid: str, ordinal: int, digest: str, kind: str, *, closing: bool = False) -> dict | None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM commands WHERE id=?", (cid,)).fetchone()
                if row:
                    if row["digest"] != digest or row["ordinal"] != ordinal or row["kind"] != kind:
                        raise RuntimeContractError("COMMAND_CONFLICT", "stable command identity changed")
                    if row["state"] != "not_written":
                        self.db.execute("COMMIT")
                        return {"state": row["state"], "receipt": json.loads(row["receipt"]), "duplicate":True}
                    if self.get("close") and not closing:
                        raise RuntimeContractError("RUNTIME_CLOSING", "close intent blocks retry of proved no-write")
                    count,size=self.db.execute("SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM events").fetchone()
                    if not closing and (count>=self.max_events-self.reserve or size>=self.max_bytes-MAX_FRAME_BYTES):
                        raise RuntimeContractError("BACKPRESSURE", "journal evidence capacity must recover before dispatch retry")
                    # Explicit no-write may retry the SAME intent/ordinal.
                    # Unknown/written/processed never enter this path.
                    self.db.execute("UPDATE commands SET state='accepted' WHERE id=?",(cid,))
                    self.db.execute("COMMIT")
                    return None
                if ordinal <= self.get("floor"):
                    raise RuntimeContractError("COMMAND_COMPACTED", "resolved ordinal cannot be replayed")
                self._compact()
                count = self.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
                events, size = self.db.execute("SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM events").fetchone()
                limit = self.max_commands if closing else self.max_commands - self.reserve
                if count >= limit or (not closing and (events >= self.max_events - self.reserve or size >= self.max_bytes - MAX_FRAME_BYTES)):
                    raise RuntimeContractError("BACKPRESSURE", "journal retains live or unacknowledged evidence; dispatch refused")
                if self.get("close") and not closing:
                    raise RuntimeContractError("RUNTIME_CLOSING", "close intent blocks new native commands")
                if not closing and len(self.get("root_activities")) >= self.max_commands-self.reserve:
                    raise RuntimeContractError("BACKPRESSURE", "retained native activity identities exhausted dispatch capacity")
                self.db.execute("INSERT INTO commands(id,ordinal,digest,kind,state) VALUES(?,?,?,?,'accepted')", (cid, ordinal, digest, kind))
                if closing:
                    self.set("close", True)
                self.db.execute("COMMIT")
                return None
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def receipt(self, cid: str, result: WriteReceipt) -> None:
        with self.lock:
            clean = public_payload(dataclasses.asdict(result), sensitive_values=self.secrets)
            # Native events may race the bounded RPC receipt. Never regress a
            # proved terminal/processed event to merely written/unknown.
            self.db.execute("UPDATE commands SET receipt=?,state=CASE WHEN state IN ('terminal','processed') THEN state ELSE ? END WHERE id=?", (encoded(clean), result.state, cid))

    def emit(self, event: RuntimeEvent) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._compact()
                count, size = self.db.execute("SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM events").fetchone()
                clean = public_payload(dataclasses.asdict(event), sensitive_values=self.secrets)
                payload = encoded(clean)
                if event.kind in OUTPUT_KINDS and (count >= self.max_events-self.reserve or size+len(payload) >= self.max_bytes-MAX_FRAME_BYTES):
                    self.set("partial", True)
                    self.db.execute("COMMIT")
                    return
                if count >= self.max_events or size+len(payload) > self.max_bytes:
                    self.set("partial", True)
                    self.db.execute("COMMIT")
                    raise RuntimeContractError("JOURNAL_EXHAUSTED", "essential evidence cannot be retained; native dispatch must stop")
                seq = self.get("sequence")+1
                self.db.execute("INSERT INTO events VALUES(?,?,?)", (seq, event.kind, payload))
                self.set("sequence", seq)
                ref = event.reference
                root_activity = ref is not None and ref.thread_id in {None,ref.root_id}
                activities=self.get("root_activities")
                already_terminal=bool(ref and activities.get(ref.activity_id)=="terminal")
                if root_activity and ref and ref.activity_id and event.kind in {"activity.started","activity.terminal"}:
                    if ref.activity_id not in activities and len(activities)>=self.max_commands:
                        raise RuntimeContractError("JOURNAL_EXHAUSTED", "bounded native activity identity retention exhausted")
                    activities[ref.activity_id]="terminal" if event.kind=="activity.terminal" or already_terminal else "active"
                    self.set("root_activities",activities)
                if event.kind == "activity.started" and root_activity and ref and ref.activity_id and not already_terminal:
                    self.set("primary", ref.activity_id)
                if event.kind == "activity.terminal" and root_activity and ref and (
                        ref.activity_id == self.get("primary") or
                        (event.request_id and self.get("primary") == "request:"+event.request_id)):
                    self.set("primary", None)
                cid = event.request_id or event.control_id
                if cid:
                    if event.kind == "activity.terminal" or (event.kind == "control.outcome" and clean["data"].get("outcome") in {"complete", "failed", "unsupported", "rejected"}):
                        self.db.execute("UPDATE commands SET state='terminal' WHERE id=?", (cid,))
                    elif event.kind == "activity.processed":
                        self.db.execute("UPDATE commands SET state='processed' WHERE id=? AND state<>'terminal'", (cid,))
                self.db.execute("COMMIT")
            except Exception:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def replay(self, after: int, limit: int = 128) -> dict:
        with self.lock:
            events: list[dict[str,Any]] = []
            size = 0
            for row in self.db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (after,min(limit,128))):
                if events and size + len(row["payload"]) > MAX_FRAME_BYTES//2:
                    break
                events.append({"sequence":row["sequence"],"event":json.loads(row["payload"])})
                size += len(row["payload"])
            return {"events":events,
                    "sequence":self.get("sequence"),"partial":self.get("partial"),"primary":self.get("primary"),"closing":self.get("close"),"rejection_floor":self.get("floor")}

    def ack(self, sequence: int) -> None:
        with self.lock:
            if sequence < self.get("acked") or sequence > self.get("sequence"):
                raise RuntimeContractError("ACK_INVALID", "acknowledgement is outside journal range")
            self.set("acked", sequence)
            self._compact()

    def lease(self, consumer: str, fence: int, expires: float) -> None:
        with self.lock:
            current = self.get("lease")
            if expires <= time.time() or expires > time.time()+60 or fence < 1:
                raise RuntimeContractError("LEASE_INVALID", "consumer lease must be bounded")
            if current and (fence < current["fence"] or (fence == current["fence"] and consumer != current["consumer"])):
                raise RuntimeContractError("LEASE_FENCED", "stale API consumer")
            self.set("lease", {"consumer":consumer,"fence":fence,"expires":expires})

    def check_lease(self, consumer: str, fence: int) -> None:
        with self.lock:
            lease = self.get("lease")
            if not lease or lease["consumer"] != consumer or lease["fence"] != fence or lease["expires"] <= time.time():
                raise RuntimeContractError("LEASE_FENCED", "API consumer lease expired or replaced")


def reference(value: Any) -> NativeReference | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("root_id"),str) or not value["root_id"]:
        raise RuntimeContractError("TARGET_INVALID", "control target requires native root identity")
    fields = {f.name for f in dataclasses.fields(NativeReference)}
    if set(value)-fields:
        raise RuntimeContractError("TARGET_INVALID", "unknown target identity fields")
    data = dict(value)
    if data.get("os_process"):
        data["os_process"] = ProcessIdentity(**data["os_process"])
    return NativeReference(**data)


def runtime_context(value: dict) -> RuntimeContext:
    data = dict(value)
    for key in ("state_root","worktree","controller_endpoint"):
        if data.get(key) is not None:
            data[key] = Path(data[key])
    binding = data["executable"]
    data["executable"] = ExecutableBinding(Path(binding["path"]),binding["sha256"],binding["version"])
    data["managed_mcp_files"] = tuple(Path(p) for p in data.get("managed_mcp_files",[]))
    data["execution_prefix"] = tuple(data.get("execution_prefix",[]))
    data["managed_mcp_args"] = tuple(data.get("managed_mcp_args",[]))
    data["probe_capabilities"] = tuple(data.get("probe_capabilities",[]))
    return RuntimeContext(**data)


class Controller:
    def __init__(self, generation: str, root: Path, driver: RuntimeDriver, *, journal: Journal | None = None):
        self.generation, self.root, self.driver = generation, root, driver
        self.journal = journal or Journal(root/"journal.sqlite")
        self.ready = False
        self.identity: RuntimeIdentity | None = None
        self.context_digest = ""
        self.context: RuntimeContext | None = None
        self.dispatch_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.lost = False
        self.native_slots = threading.BoundedSemaphore(3)
        self.cleanup_slot = threading.BoundedSemaphore(1)

    def call(self, operation, *, deadline: float, closing: bool = False):
        """Bound callers even if an installed driver violates its deadline.

        Hung operations retain their finite slot; no growing future backlog.
        Close has its own slot and scoped OS cleanup remains independent.
        """
        slot = self.cleanup_slot if closing else self.native_slots
        if not slot.acquire(blocking=False):
            raise RuntimeContractError("NATIVE_BUSY", "bounded native operation capacity exhausted")
        results: queue.Queue = queue.Queue(maxsize=1)
        def run():
            try:
                if time.monotonic()>=deadline:
                    raise RuntimeContractError('DEADLINE_EXPIRED','native operation expired before dispatch')
                results.put((True,operation()))
            except (RuntimeError,OSError,ValueError,TypeError) as exc:
                results.put((False,exc))
            finally:
                slot.release()
        threading.Thread(target=run,daemon=True).start()
        try:
            ok,result=results.get(timeout=max(.001,deadline-time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError("native operation deadline exceeded") from exc
        if not ok:
            raise result
        return result

    @contextlib.contextmanager
    def admission(self,lock,deadline):
        if not lock.acquire(timeout=max(0,deadline-time.monotonic())):
            raise RuntimeContractError('DEADLINE_EXPIRED','command expired waiting for dispatch admission')
        try:
            if time.monotonic()>=deadline:
                raise RuntimeContractError('DEADLINE_EXPIRED','command expired before dispatch admission')
            yield
        finally:
            lock.release()

    def emit(self, event: RuntimeEvent) -> None:
        try:
            if self.identity and event.reference and event.reference.root_id != self.identity.root_id:
                raise RuntimeContractError("OWNERSHIP_INVALID", "event is outside captured native root")
            self.journal.emit(event)
            if event.kind=='runtime.setup' and not self.journal.get('close'):
                self.capture_setup(StartupConsent(**event.data))
            elif (event.kind=='runtime.ready' and not self.journal.get('close') and not self.lost
                  and self.identity and event.reference and event.reference.root_id==self.identity.root_id):
                self.ready=True
                self.journal.set('setup',None)
        except RuntimeContractError:
            self.lost = True
            raise

    def capture_setup(self,setup: StartupConsent) -> None:
        if (self.context is None or self.context.harness!='claude'
                or setup.generation_id!=self.generation
                or setup.executable_sha256!=self.context.executable.sha256
                or setup.driver_revision!=self.context.driver_revision):
            raise RuntimeContractError('SETUP_INVALID','startup descriptor differs from captured Claude generation')
        self.journal.set('setup',public_payload(dataclasses.asdict(setup),sensitive_values=self.journal.secrets))
        self.ready=False

    def handle(self, value: Mapping[str, Any], *, peer: ProcessIdentity | None = None) -> dict:
        if value.get("generation") != self.generation or value.get("contract") != CONTRACT_REVISION:
            raise RuntimeContractError("GENERATION_INVALID", "private protocol generation/contract mismatch")
        op = value.get("op")
        timeout=value.get('timeout',30 if op=='open' else 5)
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=120:
            raise RuntimeContractError('DEADLINE_INVALID','positive finite timeout at most 120 seconds required')
        deadline=time.monotonic()+timeout
        if op == "attach":
            self.journal.lease(str(value["consumer"]),int(value["fence"]),float(value["expires"]))
            return self.status()
        if op == "asset":
            if peer is None or not owned_peer(peer):
                raise RuntimeContractError("PEER_INVALID", "native asset peer is outside controller cgroup")
            return dict(self.driver.asset(value["payload"],peer=peer,deadline=time.monotonic()+min(float(value.get("timeout",1)),5)))
        self.journal.check_lease(str(value.get("consumer","")),int(value.get("fence",0)))
        if op == "open":
            with self.admission(self.dispatch_lock,deadline):
                self.journal.check_lease(str(value.get("consumer","")),int(value.get("fence",0)))
                if self.context is not None:
                    if payload_digest(value["context"]) != self.context_digest:
                        raise RuntimeContractError("GENERATION_CONFLICT", "prepared generation binding changed")
                    return self.status()
                context = runtime_context(value["context"])
                if context.generation_id != self.generation or context.state_root.resolve() != self.root.resolve() or context.harness != self.driver.harness:
                    raise RuntimeContractError("OWNERSHIP_INVALID", "driver context differs from reserved generation")
                self.context = context
                self.context_digest = payload_digest(value["context"])
                # Only ephemeral known auth/private env values; never persist
                # this set or the full environment in the journal/receipt.
                self.journal.secrets = tuple(v for k,v in context.env.items() if v and any(
                    s in k.upper() for s in ("TOKEN","SECRET","API_KEY","PASSWORD","CREDENTIAL","AUTHORIZATION")))
                started = self.call(lambda:self.driver.start(context,self.emit,deadline=deadline),deadline=deadline)
                self.identity = started.identity
                self.ready = started.state == "ready" and self.identity is not None and not self.journal.get('close')
                if started.setup is not None and not self.journal.get('close'):
                    self.capture_setup(started.setup)
                    self.emit(RuntimeEvent('runtime.setup',data=dataclasses.asdict(started.setup)))
                return public_payload(dataclasses.asdict(started),sensitive_values=self.journal.secrets)
        if op == "status":
            return self.status()
        if op == "subscribe":
            return self.journal.replay(int(value.get("after",0)))
        if op == "ack":
            self.journal.ack(int(value["sequence"]))
            return {"acknowledged":value["sequence"]}
        if op == "snapshot":
            deadline=min(deadline,time.monotonic()+5)
            snapshot=self.call(lambda:self.driver.inventory(deadline=deadline),deadline=deadline)
            return public_payload(dataclasses.asdict(snapshot),sensitive_values=self.journal.secrets)
        if op not in {"submit","control","close"}:
            raise RuntimeContractError("OP_INVALID", "unknown private controller operation")
        command = value.get("command")
        if not isinstance(command,dict):
            raise RuntimeContractError("COMMAND_INVALID", "command object required")
        cid = command.get("request_id") if op == "submit" else command.get("control_id")
        ordinal, digest = command.get("request_sequence"),command.get("payload_digest")
        if not isinstance(cid,str) or not cid or len(cid)>255 or type(ordinal) is not int or ordinal<1 or not isinstance(digest,str) or len(digest)!=64:
            raise RuntimeContractError("COMMAND_INVALID", "stable identity/ordinal/digest required")
        body = {k:v for k,v in command.items() if k!="payload_digest"}
        if payload_digest(body) != digest:
            raise RuntimeContractError("COMMAND_INVALID", "command payload digest mismatch")
        closing = op == "close"
        if closing and command.get("action") != "close":
            raise RuntimeContractError("COMMAND_INVALID", "Close requires explicit close action")
        if op == "submit":
            if command.get("source","gui") not in {"gui","native_completion","automation","reconciliation","system"} or not isinstance(command.get("text"),str):
                raise RuntimeContractError("COMMAND_INVALID", "bounded user text and valid source required")
            submission = NativeSubmission(**command)
        else:
            if command.get("action") not in {"stop_reply","stop_work","stop_automation","close","enable_local_channel"} or (not closing and command["action"]=="close"):
                raise RuntimeContractError("COMMAND_INVALID", "invalid scoped native control")
            native_control = NativeControl(**(command|{"target":reference(command.get("target"))}))
        # Close is independent of the regular dispatch lock/native RPC future.
        lock = threading.Lock() if closing else self.dispatch_lock
        with self.admission(lock,deadline):
            self.journal.check_lease(str(value.get("consumer","")),int(value.get("fence",0)))
            duplicate = self.journal.reserve_command(cid,ordinal,digest,"submit" if op=="submit" else "control",closing=closing)
            if duplicate:
                return duplicate
            if closing:
                self.ready=False
                cleanup_deadline=min(deadline,time.monotonic()+5)
                try:
                    cleanup=self.call(lambda:self.driver.cleanup(deadline=cleanup_deadline),deadline=cleanup_deadline,closing=True)
                except (RuntimeError,OSError,TimeoutError):
                    from conversation_runtime_contract import NativeCleanup
                    cleanup=NativeCleanup("inconclusive",detail="native cleanup timed out; owned-unit cleanup remains required")
                receipt = WriteReceipt("written",detail="native cleanup requested; independent owned-unit verification required")
                self.journal.receipt(cid,receipt)
                result = public_payload(dataclasses.asdict(cleanup),sensitive_values=self.journal.secrets)
                self.emit(RuntimeEvent("control.outcome",control_id=cid,data=result))
                return result
            setup_action=op=='control' and command['action']=='enable_local_channel'
            if self.lost or (not self.ready and not setup_action):
                result = WriteReceipt("not_written",detail="native ownership/readiness unavailable")
            elif setup_action:
                setup=self.journal.get('setup')
                confirmation=self.journal.get('setup_confirmation')
                if (self.ready or self.context is None or self.context.harness!='claude' or not setup
                        or command['options']['setup_id']!=setup['setup_id']
                        or command['options']['configuration_sha256']!=setup['configuration_sha256']
                        or confirmation is not None
                        or (native_control.target is not None and (self.identity is None or native_control.target.root_id!=self.identity.root_id))):
                    result=WriteReceipt('rejected',detail='startup choice is stale, duplicated, or outside captured Claude phase')
                else:
                    # Retain before the finite choice write. Ambiguous/partial
                    # confirmation cannot be replayed under another control ID.
                    self.journal.set('setup_confirmation',{'setup_id':setup['setup_id'],'control_id':cid})
                    try:
                        def setup_edge():
                            self.journal.check_lease(str(value.get('consumer','')),int(value.get('fence',0)))
                            if self.journal.get('close') or time.monotonic()>=deadline or self.journal.get('setup')!=setup:
                                return WriteReceipt('not_written',detail='Close, deadline, or phase change fenced setup choice')
                            return self.driver.control(native_control,deadline=deadline)
                        result=self.call(setup_edge,deadline=deadline)
                    except RuntimeContractError as exc:
                        if exc.code not in {'DEADLINE_EXPIRED','NATIVE_BUSY','LEASE_FENCED'}:
                            raise
                        result=WriteReceipt('not_written',detail='controller refused before startup choice: '+exc.code)
                    except (RuntimeError,OSError,TimeoutError):
                        result=WriteReceipt('unknown',detail='finite startup confirmation outcome ambiguous; no replay')
                    if result.state in {'not_written','rejected','unsupported'}:
                        self.journal.set('setup_confirmation',None)
            elif op == "submit":
                if command.get("source","gui") not in {"gui","native_completion","automation","reconciliation","system"}:
                    raise RuntimeContractError("COMMAND_INVALID", "unknown activity source")
                earlier=self.journal.db.execute("SELECT 1 FROM commands WHERE kind='submit' AND ordinal<? AND state NOT IN ('terminal','rejected','unsupported') LIMIT 1",(ordinal,)).fetchone()
                if self.journal.get("primary") is not None or earlier:
                    result = WriteReceipt("not_written",detail="primary slot occupied; retain API outbox order")
                else:
                    # Reserve before native write; processing replaces this
                    # request placeholder with attributable native activity.
                    self.journal.set("primary","request:"+cid)
                    try:
                        def submit_edge():
                            self.journal.check_lease(str(value.get("consumer","")),int(value.get("fence",0)))
                            if self.journal.get('close') or time.monotonic()>=deadline:
                                return WriteReceipt('not_written',detail='Close or deadline fenced pending native write')
                            return self.driver.submit(submission,deadline=deadline)
                        result = self.call(submit_edge,deadline=deadline)
                    except RuntimeContractError as exc:
                        if exc.code not in {'DEADLINE_EXPIRED','NATIVE_BUSY','LEASE_FENCED'}:
                            raise
                        result=WriteReceipt('not_written',detail='controller refused before native dispatch: '+exc.code)
                    except (RuntimeError, OSError, TimeoutError):
                        result = WriteReceipt("unknown",detail="native write outcome ambiguous; no replay")
                    if result.state in {"not_written","rejected","unsupported"} and self.journal.get("primary")=="request:"+cid:
                        self.journal.set("primary",None)
            else:
                if command.get("action") not in {"stop_reply","stop_work","stop_automation","close"} or command["action"]=="close":
                    raise RuntimeContractError("COMMAND_INVALID", "invalid scoped native control")
                target = reference(command.get("target"))
                if not target or not self.identity or target.root_id != self.identity.root_id:
                    result = WriteReceipt("rejected",detail="control target differs from owned native root")
                elif command["action"]=="stop_reply" and command.get("expected_activity_id") != self.journal.get("primary"):
                    result = WriteReceipt("rejected",detail="primary activity changed; stale stop refused")
                else:
                    try:
                        def control_edge():
                            self.journal.check_lease(str(value.get("consumer","")),int(value.get("fence",0)))
                            if self.journal.get('close') or time.monotonic()>=deadline:
                                return WriteReceipt('not_written',detail='Close or deadline fenced pending native control')
                            return self.driver.control(native_control,deadline=deadline)
                        result = self.call(control_edge,deadline=deadline)
                    except RuntimeContractError as exc:
                        if exc.code not in {'DEADLINE_EXPIRED','NATIVE_BUSY','LEASE_FENCED'}:
                            raise
                        result=WriteReceipt('not_written',detail='controller refused before native dispatch: '+exc.code)
                    except (RuntimeError, OSError, TimeoutError):
                        result = WriteReceipt("unknown",detail="native control outcome ambiguous")
            self.journal.receipt(cid,result)
            return public_payload(dataclasses.asdict(result),sensitive_values=self.journal.secrets)

    def status(self) -> dict:
        return {"generation":self.generation,"contract":CONTRACT_REVISION,"ready":self.ready,
                "lost":self.lost,"identity":dataclasses.asdict(self.identity) if self.identity else None,
                "setup":self.journal.get('setup'),"setup_confirmation":self.journal.get('setup_confirmation'),
                **self.journal.replay(self.journal.get("sequence"))}


def owned_peer(peer: ProcessIdentity) -> bool:
    try:
        return start_ticks(peer.pid)==peer.start_ticks and Path(f"/proc/{peer.pid}/cgroup").read_text()==Path("/proc/self/cgroup").read_text()
    except (OSError,ValueError,IndexError):
        return False


class PrivateServer:
    def __init__(self, controller: Controller, endpoint: Path):
        self.controller, self.endpoint = controller, endpoint
        self.slots = threading.BoundedSemaphore(16)

    def connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(125)
            pid,uid,_ = struct.unpack("3i",conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid != os.geteuid():
                raise RuntimeContractError("PEER_INVALID", "private peer uid differs")
            peer = ProcessIdentity(pid,start_ticks(pid))
            raw = bytearray()
            while b"\n" not in raw:
                chunk = conn.recv(min(65536,MAX_FRAME_BYTES+1-len(raw)))
                if not chunk or len(raw)+len(chunk)>MAX_FRAME_BYTES:
                    raise RuntimeContractError("FRAME_INVALID", "private frame missing or too large")
                raw.extend(chunk)
            value = json.loads(bytes(raw).split(b"\n",1)[0])
            if not isinstance(value,dict):
                raise RuntimeContractError("FRAME_INVALID", "private request must be object")
            result = {"ok":True,"result":self.controller.handle(value,peer=peer)}
        except RuntimeContractError as exc:
            result = {"ok":False,"error":exc.code,"detail":str(exc)}
        except (ValueError, TypeError, KeyError, OSError):
            result = {"ok":False,"error":"REQUEST_INVALID","detail":"invalid private request"}
        try:
            conn.sendall((encoded(result)+"\n").encode())
        except OSError:
            pass
        finally:
            conn.close()
            self.slots.release()

    def serve(self) -> None:
        private_directory(self.endpoint.parent)
        if self.endpoint.exists() or self.endpoint.is_symlink():
            raise RuntimeContractError("ENDPOINT_CONFLICT", "controller endpoint already exists")
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.bind(str(self.endpoint))
            os.chmod(self.endpoint,0o600)
            endpoint_identity=self.endpoint.lstat()
            sock.listen(16)
            sock.settimeout(.5)
            try:
                while not self.controller.shutdown.is_set():
                    try:
                        conn,_=sock.accept()
                    except TimeoutError:
                        continue
                    if not self.slots.acquire(blocking=False):
                        conn.close()
                        continue
                    threading.Thread(target=self.connection,args=(conn,),daemon=True).start()
            finally:
                if self.endpoint.exists():
                    current=self.endpoint.lstat()
                    if (current.st_dev,current.st_ino)==(endpoint_identity.st_dev,endpoint_identity.st_ino):
                        self.endpoint.unlink()


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation",required=True)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--endpoint",type=Path)
    parser.add_argument("--harness",choices=("codex","claude"),required=True)
    parser.add_argument("--test-transport",action="store_true")
    args=parser.parse_args(argv)
    private_directory(args.root)
    if args.test_transport:
        # No test transport outside an independently marked fixture state.
        fixture_root=args.root.parent.parent
        bootstrap=fixture_root/"fixture_bootstrap.py"
        spec=importlib.util.spec_from_file_location("fixture_supervisor",bootstrap)
        if not spec or not spec.loader:
            raise RuntimeContractError("OWNERSHIP_INVALID","fixture bootstrap missing")
        fixture=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        fixture.verified_bootstrap_root(fixture_root,bootstrap)
        record=fixture.read_json(fixture.ledger_path(fixture.read_json(fixture_root/fixture.MARKER)["fixture_id"]))
        native=next((n for n in record.get("native_units",[]) if n["generation_id"]==args.generation),None)
        if not native or not native.get("test_transport") or native["root"]!=str(args.root) or native["harness"]!=args.harness or native["endpoint"]!=str(args.endpoint):
            raise RuntimeContractError("OWNERSHIP_INVALID","test transport was not pre-registered")
        fixture.verify_native(record,native)
        module=importlib.import_module("gui_experiment_test_driver")
    else:
        module=importlib.import_module(f"conversation_adapters.{args.harness}_runtime")
    driver=module.create_driver()
    if args.test_transport:
        driver.harness=args.harness
    controller=Controller(args.generation,args.root,driver)
    PrivateServer(controller,args.endpoint or args.root/"controller.sock").serve()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
