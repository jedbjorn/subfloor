"""Experimental API-side native runtime client and canonical projections.

Clients never own native descriptors or stop a unit when their lease expires.
The caller still applies normal Chats operator/origin authority; this store
independently binds every operation to conversation/shell/operator identity.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import socket
import struct
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import db_driver
from conversation_runtime_contract import (
    CONTRACT_REVISION,
    MAX_FRAME_BYTES,
    RuntimeContext,
    RuntimeContractError,
    RuntimeEvent,
    payload_digest,
    public_payload,
)
from conversation_runtime_controller import encoded, reference, start_ticks


class RuntimeStore:
    def __init__(self, database: str | Path, *, sensitive_values: tuple[str,...] = ()):
        self.database = str(database)
        self.secrets = sensitive_values

    def _owned(self, con, generation: str, owner: int, shell: int):
        row = con.execute("SELECT g.* FROM conversation_runtime_generations g JOIN conversations c USING(conversation_id) WHERE g.generation_id=? AND g.owner_user_id=? AND g.shell_id=? AND c.owner_user_id=? AND c.shell_id=?",(generation,owner,shell,owner,shell)).fetchone()
        if row is None:
            raise RuntimeContractError("RUNTIME_NOT_OWNED", "generation is outside conversation tenancy")
        return row

    def reserve(self, context: RuntimeContext, binding: Mapping[str,Any]) -> str:
        """Persist ownership/binary/boot/unit intent before supervisor launch.

        Binding contains public resource identity only. No full environment or
        auth material is allowed; boot bytes stay in the private prepared view.
        Fixture supervisor registration must finish before launching the unit.
        """
        con=db_driver.connect(self.database)
        try:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT shell_id,owner_user_id,state,harness,provider,model,effort,worktree FROM conversations WHERE conversation_id=?",(context.conversation_id,)).fetchone()
            if row is None or (row["shell_id"],row["owner_user_id"]) != (context.shell_id,context.owner_user_id) or row["state"]=="closed":
                raise RuntimeContractError("RUNTIME_NOT_OWNED", "open conversation ownership required")
            if any(row[key]!=getattr(context,key) for key in ("harness","provider","model","effort")) or Path(row["worktree"]).resolve()!=context.worktree.resolve():
                raise RuntimeContractError("GENERATION_CONFLICT","prepared route/worktree differs from immutable conversation")
            for previous in con.execute("SELECT cleanup_json FROM conversation_runtime_generations WHERE conversation_id=?",(context.conversation_id,)).fetchall():
                cleanup=json.loads(previous["cleanup_json"])
                if cleanup.get("unit_verified_exited") is not True or cleanup.get("outcome")!="complete" or cleanup.get("unresolved_definitions") or cleanup.get("unresolved_work"):
                    raise RuntimeContractError("CLEANUP_PENDING","previous generation ownership remains until unit and native definition cleanup are proved")
            captured={"context":{
                "generation_id":context.generation_id,"conversation_id":context.conversation_id,
                "shell_id":context.shell_id,"owner_user_id":context.owner_user_id,
                "harness":context.harness,"executable":{"path":str(context.executable.path),"sha256":context.executable.sha256,"version":context.executable.version},
                "driver_revision":context.driver_revision,"contract_revision":CONTRACT_REVISION,
                "boot_digest":context.boot_digest,"policy_digest":context.policy_digest,
                "model":context.model,"provider":context.provider,"effort":context.effort,
                "permission_mode":context.permission_mode,"worktree":str(context.worktree),
                "state_root":str(context.state_root),"controller_endpoint":str(context.controller_endpoint),
            },"supervision":public_payload(binding,sensitive_values=self.secrets)}
            now=time.time()
            con.execute("INSERT INTO conversation_runtime_generations(generation_id,conversation_id,shell_id,owner_user_id,harness,binding_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(context.generation_id,context.conversation_id,context.shell_id,context.owner_user_id,context.harness,encoded(captured),now,now))
            con.commit()
            return context.generation_id
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def attach(self, generation: str, owner: int, shell: int, consumer: str, *, lifetime: float=30) -> dict:
        if not 0<lifetime<=60 or not consumer:
            raise RuntimeContractError("LEASE_INVALID", "bounded named consumer required")
        con=db_driver.connect(self.database)
        try:
            con.execute("BEGIN IMMEDIATE")
            row=self._owned(con,generation,owner,shell)
            if row["consumer_id"] not in {None,consumer} and row["consumer_expires"]>time.time():
                raise RuntimeContractError("LEASE_BUSY", "another API consumer owns dispatch")
            fence=row["consumer_fence"]+(row["consumer_id"]!=consumer or row["consumer_expires"]<=time.time())
            expires=time.time()+lifetime
            con.execute("UPDATE conversation_runtime_generations SET consumer_id=?,consumer_fence=?,consumer_expires=?,updated_at=? WHERE generation_id=?",(consumer,fence,expires,time.time(),generation))
            con.commit()
            return {"consumer":consumer,"fence":fence,"expires":expires,"after":row["last_sequence"]}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def intent(self,generation: str,owner: int,shell: int,lease: Mapping[str,Any],cid: str,kind: str,payload: Mapping[str,Any]) -> dict:
        if kind not in {"submit","control","close"} or not cid or len(cid)>255:
            raise RuntimeContractError("COMMAND_INVALID", "stable bounded command identity required")
        con=db_driver.connect(self.database)
        try:
            con.execute("BEGIN IMMEDIATE")
            row=self._owned(con,generation,owner,shell)
            if row["consumer_id"]!=lease["consumer"] or row["consumer_fence"]!=lease["fence"] or row["consumer_expires"]<=time.time():
                raise RuntimeContractError("LEASE_FENCED", "API consumer cannot dispatch")
            previous=con.execute("SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",(generation,cid)).fetchone()
            ordinal=previous["command_sequence"] if previous else row["next_command_sequence"]
            id_key="request_id" if kind=="submit" else "control_id"
            if set(payload)&{id_key,"request_sequence","payload_digest"}:
                raise RuntimeContractError("COMMAND_INVALID", "caller cannot supply reserved identity fields")
            command=dict(payload)|{id_key:cid,"request_sequence":ordinal}
            digest=payload_digest(command)
            if previous:
                if previous["payload_digest"]!=digest or previous["kind"]!=("submit" if kind=="submit" else "control"):
                    raise RuntimeContractError("COMMAND_CONFLICT", "stable command payload changed")
                con.commit()
                return command|{"payload_digest":digest}
            if row["close_intent"]:
                raise RuntimeContractError("RUNTIME_CLOSING", "close intent blocks later commands")
            con.execute("INSERT INTO conversation_runtime_commands(generation_id,command_id,command_sequence,kind,payload_digest,intent_json) VALUES(?,?,?,?,?,?)",(generation,cid,ordinal,"submit" if kind=="submit" else "control",digest,encoded(public_payload(command,sensitive_values=self.secrets))))
            con.execute("UPDATE conversation_runtime_generations SET next_command_sequence=?,close_intent=MAX(close_intent,?),updated_at=? WHERE generation_id=?",(ordinal+1,int(kind=="close"),time.time(),generation))
            con.commit()
            return command|{"payload_digest":digest}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def receipt(self,generation: str,owner: int,shell: int,cid: str,result: Mapping[str,Any]) -> None:
        con=db_driver.connect(self.database)
        try:
            self._owned(con,generation,owner,shell)
            state=result.get("state","unknown")
            if state not in {"accepted","written","processed","terminal","unknown","not_written","unsupported","rejected"}:
                state="unknown"
            con.execute("UPDATE conversation_runtime_commands SET receipt_json=CASE WHEN state IN ('terminal','processed') THEN receipt_json ELSE ? END,state=CASE WHEN state IN ('terminal','processed') THEN state ELSE ? END WHERE generation_id=? AND command_id=?",(encoded(public_payload(result,sensitive_values=self.secrets)),state,generation,cid))
            con.commit()
        finally:
            con.close()

    def command_status(self,generation: str,owner: int,shell: int,cid: str) -> dict:
        con=db_driver.connect(self.database)
        try:
            self._owned(con,generation,owner,shell)
            row=con.execute("SELECT state,receipt_json FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",(generation,cid)).fetchone()
            if row is None:
                raise RuntimeContractError("COMMAND_UNKNOWN","canonical intent is missing")
            return {"state":row["state"],"receipt":json.loads(row["receipt_json"])}
        finally:
            con.close()

    def ingest(self,generation: str,owner: int,shell: int,lease: Mapping[str,Any],replay: Mapping[str,Any], *, project: Callable | None=None) -> int:
        """Commit event/projection/watermark together; optional Chats emitter
        receives this same transaction, never emits a second replay event.
        """
        con=db_driver.connect(self.database)
        try:
            con.execute("BEGIN IMMEDIATE")
            row=self._owned(con,generation,owner,shell)
            if row["consumer_id"]!=lease["consumer"] or row["consumer_fence"]!=lease["fence"] or row["consumer_expires"]<=time.time():
                raise RuntimeContractError("LEASE_FENCED", "stale ingestion consumer")
            last=row["last_sequence"]
            for item in replay["events"]:
                sequence=item["sequence"]
                if type(sequence) is not int or sequence<1:
                    raise RuntimeContractError("EVENT_INVALID", "positive controller sequence required")
                if sequence<=last:
                    continue
                if sequence!=last+1 and not replay.get("partial"):
                    raise RuntimeContractError("EVENT_GAP", "unexplained journal sequence gap")
                event=public_payload(item["event"],sensitive_values=self.secrets)
                normalized=RuntimeEvent(**(event|{"reference":reference(event.get("reference"))}))
                event=dataclasses.asdict(normalized)
                if event['kind']=='runtime.setup':
                    captured=json.loads(row['binding_json'])['context']
                    setup=event['data']
                    if (row['harness']!='claude' or setup['generation_id']!=generation
                            or setup['executable_sha256']!=captured['executable']['sha256']
                            or setup['driver_revision']!=captured['driver_revision']):
                        raise RuntimeContractError('SETUP_INVALID','startup event differs from captured Claude generation')
                    con.execute("UPDATE conversation_runtime_generations SET state='needs_consent' WHERE generation_id=? AND close_intent=0 AND state NOT IN ('closed','lost')",(generation,))
                elif event['kind']=='runtime.ready':
                    con.execute("UPDATE conversation_runtime_generations SET state='ready' WHERE generation_id=? AND close_intent=0 AND state NOT IN ('closed','lost')",(generation,))
                con.execute("INSERT INTO conversation_runtime_events VALUES(?,?,?)",(generation,sequence,encoded(event)))
                ref=event.get("reference") or {}
                if event["kind"].startswith("work."):
                    native_id=ref.get("work_id") or ref.get("native_process_id") or ref.get("thread_id")
                    if not native_id:
                        raise RuntimeContractError("EVENT_INVALID", "work projection lacks opaque key")
                    kind=event.get("data",{}).get("kind") or ("terminal" if ref.get("native_process_id") else "task" if ref.get("work_id") else "child")
                    key=encoded([ref.get("root_id"),ref.get("thread_id"),kind,native_id])
                    con.execute("INSERT INTO conversation_runtime_work VALUES(?,?,?,?) ON CONFLICT(generation_id,work_key) DO UPDATE SET projection_json=excluded.projection_json,last_sequence=excluded.last_sequence",(generation,key,encoded(event),sequence))
                cid=event.get("request_id") or event.get("control_id")
                if cid:
                    final_control=event['kind']=='control.outcome' and event['data'].get('outcome') in {'complete','failed','unsupported','rejected'}
                    state="terminal" if event["kind"]=="activity.terminal" or final_control else "processed" if event["kind"]=="activity.processed" else None
                    if state:
                        if final_control:
                            con.execute("UPDATE conversation_runtime_commands SET state='terminal',receipt_json=? WHERE generation_id=? AND command_id=? AND state<>'terminal'",
                                        (encoded({'state':'terminal',**event['data']}),generation,cid))
                        else:
                            con.execute("UPDATE conversation_runtime_commands SET state=? WHERE generation_id=? AND command_id=? AND (state<>'terminal' OR ?='terminal')",(state,generation,cid,state))
                if project is not None:
                    project(con,row["conversation_id"],sequence,event)
                last=sequence
            con.execute("UPDATE conversation_runtime_generations SET last_sequence=?,updated_at=? WHERE generation_id=?",(last,time.time(),generation))
            # Watermark deduplicates older replay after compacting resolved DB
            # events; live command/work projections remain independently intact.
            con.execute("DELETE FROM conversation_runtime_events WHERE generation_id=? AND sequence<?",(generation,max(0,last-2048)))
            con.commit()
            return last
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def status(self,generation: str,owner: int,shell: int) -> dict:
        con=db_driver.connect(self.database)
        try:
            row=self._owned(con,generation,owner,shell)
            return dict(row)|{"binding":json.loads(row["binding_json"]),"cleanup":json.loads(row["cleanup_json"])}
        finally:
            con.close()

    def state(self,generation: str,owner: int,shell: int,state: str,cleanup: Mapping[str,Any] | None=None) -> None:
        if state not in {"reserved","starting","ready","closing","closed","lost"}:
            raise RuntimeContractError("STATE_INVALID", "unknown generation state")
        con=db_driver.connect(self.database)
        try:
            row=self._owned(con,generation,owner,shell)
            if row["state"] in {"closed","lost"} and state not in {"closed","lost"}:
                raise RuntimeContractError("GENERATION_TERMINAL", "replacement requires a new generation")
            con.execute("UPDATE conversation_runtime_generations SET state=?,cleanup_json=?,updated_at=? WHERE generation_id=?",(state,encoded(public_payload(cleanup or {},sensitive_values=self.secrets)),time.time(),generation))
            con.commit()
        finally:
            con.close()


class RuntimeClient:
    def __init__(self,endpoint: Path,generation: str, *, controller_pid: int,controller_start_ticks: int,consumer: str | None=None):
        self.endpoint,self.generation=endpoint,generation
        self.controller_pid,self.controller_start_ticks=controller_pid,controller_start_ticks
        self.consumer=consumer or uuid.uuid4().hex
        self.lease: dict[str,Any]={}

    def request(self,op: str, *, timeout: float=10,**fields) -> dict:
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=120:
            raise RuntimeContractError('DEADLINE_INVALID','positive finite timeout at most 120 seconds required')
        value={"op":op,"generation":self.generation,"contract":CONTRACT_REVISION,**self.lease,**fields,"timeout":timeout}
        raw=(encoded(value)+"\n").encode()
        if len(raw)>MAX_FRAME_BYTES:
            raise RuntimeContractError("FRAME_TOO_LARGE", "private command exceeds frame bound")
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(min(timeout,120)+2)
            sock.connect(str(self.endpoint))
            pid,uid,_=struct.unpack("3i",sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.geteuid() or pid!=self.controller_pid or start_ticks(pid)!=self.controller_start_ticks:
                raise RuntimeContractError("OWNERSHIP_INVALID", "private server process differs from captured controller")
            sock.sendall(raw)
            response=bytearray()
            while b"\n" not in response:
                chunk=sock.recv(min(65536,MAX_FRAME_BYTES+1-len(response)))
                if not chunk or len(response)+len(chunk)>MAX_FRAME_BYTES:
                    raise RuntimeContractError("FRAME_INVALID", "private response missing or too large")
                response.extend(chunk)
            result=json.loads(bytes(response).split(b"\n",1)[0])
        if result.get("ok") is not True:
            raise RuntimeContractError(result.get("error","CONTROLLER_FAILED"),result.get("detail","private controller failed"))
        return result["result"]

    def attach(self,store: RuntimeStore,owner: int,shell: int) -> dict:
        self.lease=store.attach(self.generation,owner,shell,self.consumer)
        return self.request("attach",**self.lease)

    def open(self,context: RuntimeContext) -> dict:
        data=dataclasses.asdict(context)
        data["state_root"],data["worktree"]=str(context.state_root),str(context.worktree)
        data["controller_endpoint"]=str(context.controller_endpoint) if context.controller_endpoint else None
        data["executable"]["path"]=str(context.executable.path)
        data["managed_mcp_files"]=[str(p) for p in context.managed_mcp_files]
        return self.request("open",context=data,timeout=120)

    def submit(self,store: RuntimeStore,owner: int,shell: int,cid: str,**payload) -> dict:
        command=store.intent(self.generation,owner,shell,self.lease,cid,"submit",payload)
        try:
            result=self.request("submit",command=command)
        except RuntimeContractError as exc:
            if exc.code=="COMMAND_COMPACTED":
                result=store.command_status(self.generation,owner,shell,cid)|{"duplicate":True,"compacted":True}
            elif exc.code in {"BACKPRESSURE","LEASE_FENCED","RUNTIME_CLOSING","NATIVE_BUSY","DEADLINE_EXPIRED"}:
                result={"state":"not_written","code":exc.code,"detail":"controller proved refusal before native dispatch"}
            else:
                result={"state":"unknown","code":exc.code,"detail":"controller outcome unavailable; retain stable intent"}
        except OSError:
            result={"state":"unknown","detail":"controller response unavailable; stable intent must never be replayed as new"}
        if not result.get('compacted'):
            store.receipt(self.generation,owner,shell,cid,result)
        return result

    def control(self,store: RuntimeStore,owner: int,shell: int,cid: str, *, closing: bool=False,**payload) -> dict:
        command=store.intent(self.generation,owner,shell,self.lease,cid,"close" if closing else "control",payload)
        try:
            result=self.request("close" if closing else "control",command=command)
        except RuntimeContractError as exc:
            if exc.code!='COMMAND_COMPACTED':
                raise
            result=store.command_status(self.generation,owner,shell,cid)|{"duplicate":True,"compacted":True}
        if not result.get('compacted'):
            store.receipt(self.generation,owner,shell,cid,result)
        return result

    def subscribe(self,store: RuntimeStore,owner: int,shell: int, *, project: Callable | None=None) -> dict:
        after=store.status(self.generation,owner,shell)["last_sequence"]
        replay=self.request("subscribe",after=after)
        committed=store.ingest(self.generation,owner,shell,self.lease,replay,project=project)
        self.request("ack",sequence=committed)
        return replay
