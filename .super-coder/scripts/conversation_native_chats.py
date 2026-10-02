"""Opt-in Chats projections and API consumers; controllers own native lifetime.

Only the disposable fixture installs this service. Ordinary API startup and
the legacy broker remain independent. Native identities are opaque and their
scope is retained in every projected observation and control request.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import conversation_events
import db_driver
from conversation_runtime import RuntimeClient, RuntimeStore
from conversation_runtime_contract import (
    RuntimeContractError,
    payload_digest,
    public_payload,
)
from conversation_runtime_controller import encoded

_SERVICE: NativeChatsService | None = None


def append_event(con, cid: str, kind: str, payload: dict, *, message_id=None, run_id=None) -> None:
    sequence = con.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events WHERE conversation_id=?', (cid,)).fetchone()[0]
    con.execute('INSERT INTO conversation_events(conversation_id,sequence,event_type,payload,message_id,run_id) VALUES(?,?,?,?,?,?)',
                (cid,sequence,conversation_events.require_event_type(kind),json.dumps(payload),message_id,run_id))


def project_event(con, cid: str, sequence: int, event: dict, *, primary: dict | None = None) -> None:
    """Same transaction as the controller watermark; replay never duplicates.

    Only an attributable GUI root command can finish a GUI foreground run.
    Autonomous and child activity remains independently visible. Terminal
    command truth is already reconciled by RuntimeStore before this callback.
    """
    row = con.execute('SELECT runtime_mode,runtime_projection,state FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
    if row is None or row['runtime_mode'] != 'native_experiment':
        raise RuntimeContractError('RUNTIME_NOT_OWNED','native projection requires opted-in chat')
    projection = json.loads(row['runtime_projection'])
    generation = projection.get('generation_id')
    if not generation:
        raise RuntimeContractError('GENERATION_INVALID','native projection has no captured generation')
    kind, ref = event['kind'], event.get('reference') or {}
    current = con.execute('SELECT state,close_intent FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=?',(generation,cid)).fetchone()
    if current is None:
        raise RuntimeContractError('GENERATION_INVALID','generation is outside chat projection')
    command = None
    if event.get('request_id'):
        command = con.execute('SELECT intent_json,state FROM conversation_runtime_commands WHERE generation_id=? AND command_id=? AND kind=\'submit\'',
                              (generation,event['request_id'])).fetchone()
    intent = json.loads(command['intent_json']) if command else {}
    mid, rid = intent.get('message_id'), intent.get('run_id')
    root_activity = bool(ref.get('root_id') and ref.get('thread_id') == ref.get('root_id'))
    projection.update(controller_sequence=sequence,observed_at=event['observed_at'],
                      freshness=event['freshness'],partial=bool(projection.get('partial') or event['partial']))
    if current['close_intent']:
        projection.update(state='closing',setup=None)
    elif kind == 'runtime.setup' and current['state'] not in {'ready','closed','lost'}:
        eligible = event['freshness']=='current' and not event['partial'] and event['grade']!='inconclusive'
        projection.update(state='needs_consent' if eligible else 'setup_inconclusive',setup=event['data'] if eligible else None)
    elif kind == 'runtime.ready' and current['state'] not in {'closed','lost'}:
        projection.update(state='ready',setup=None)
        if ref:
            projection['root_id'] = ref['root_id']
    elif kind in {'runtime.lost','ownership.failed'}:
        projection.update(state='lost',freshness='unknown',partial=True,setup=None)
    elif kind == 'capability.observed':
        # Only checker-bound grades installed by the owner admit GUI actions;
        # native observations are diagnostics, not a reusable certificate.
        projection['capability_observation'] = event['data']
    # Retained controller journal owns primary/terminal reconciliation, not
    # this individual event (which may be child, stale, late or replayed).
    projection['primary'] = primary
    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(json.dumps(projection),cid))
    envelope = {'generation_id':generation,'controller_sequence':sequence,**event}
    append_event(con,cid,kind,envelope,message_id=mid,run_id=rid)
    if root_activity and mid is not None and rid is not None:
        run = con.execute('SELECT state FROM conversation_runs WHERE run_id=? AND conversation_id=? AND trigger_message_id=?',(rid,cid,mid)).fetchone()
        if run is None:
            raise RuntimeContractError('COMMAND_INVALID','command run is outside chat')
        if kind in {'activity.started','activity.processed'} and run['state']=='starting':
            con.execute("UPDATE conversation_runs SET state='running' WHERE run_id=?",(rid,))
            con.execute("UPDATE conversation_messages SET state='running' WHERE message_id=? AND state IN ('accepted','queued')",(mid,))
            con.execute("UPDATE conversations SET state='running' WHERE conversation_id=? AND state='queued'",(cid,))
            append_event(con,cid,'run.started',{'native_activity_id':ref.get('activity_id')},message_id=mid,run_id=rid)
        elif kind == 'output.delta' and run['state'] in {'starting','running'}:
            append_event(con,cid,'assistant.delta',
                         {'text':event['data'].get('text',''),'native_reference':ref},message_id=mid,run_id=rid)
        elif kind=='activity.terminal' and run['state'] in {'starting','running'}:
            status = event['data'].get('status')
            target = 'succeeded' if status in {'completed','complete','succeeded'} else 'cancelled' if status in {'interrupted','cancelled'} else 'failed' if status in {'failed','errored'} else 'unknown'
            con.execute("UPDATE conversation_runs SET state=?,ended_at=datetime('now') WHERE run_id=?",(target,rid))
            message_state = 'completed' if target=='succeeded' else 'cancelled' if target=='cancelled' else 'failed'
            con.execute("UPDATE conversation_messages SET state=?,completed_at=datetime('now') WHERE message_id=?",(message_state,mid))
            queued = con.execute("SELECT 1 FROM conversation_outbox WHERE conversation_id=? AND state='pending'",(cid,)).fetchone()
            con.execute("UPDATE conversations SET state=?,last_activity_at=datetime('now') WHERE conversation_id=? AND state IN ('queued','running')",('queued' if queued else 'idle',cid))
            append_event(con,cid,'run.completed' if target=='succeeded' else 'run.interrupted' if target=='cancelled' else 'run.failed' if target=='failed' else 'run.unknown',
                         {'status':status,'runtime_alive':True},message_id=mid,run_id=rid)


class NativeChatsService:
    """Bounded API consumer slots. Shutdown releases consumers only.

    Launch preparation/check factories are fixture-owned, never an arbitrary
    executable endpoint. A restart attaches captured live generations instead
    of reconstructing their context or replaying ambiguous submissions.
    """
    def __init__(self, database: Path, root: Path, supervisor, *, prepare_context=None, resolve_route=None):
        self.database, self.root, self.supervisor = database, root, supervisor
        self.store = RuntimeStore(database)
        self.prepare_context = prepare_context
        self.route_resolver = resolve_route
        self.consumer = uuid.uuid4().hex
        self.clients: dict[str,tuple[RuntimeClient,int,int]] = {}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run,name='native-chats-consumer',daemon=True)

    def start(self) -> None:
        self.thread.start()

    def notify(self) -> None:
        self.wake.set()

    def resolve_route(self,harness,model,effort):
        if self.route_resolver is None:
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','owned native account/options observation is not ready')
        return self.route_resolver(harness,model,effort)

    def request_close(self,con,cid: str,owner: int,version: int) -> None:
        """Persist the Close fence atomically with the operator/version check.

        The API returns the persisted chat immediately. Its independent
        consumer dispatches one retained control; API death cannot unfence
        queued/new sends or make the shell available to a replacement.
        """
        with db_driver.write_transaction(con,'native_chat.close_intent'):
            chat = con.execute('SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=? AND runtime_mode=\'native_experiment\'',(cid,owner)).fetchone()
            if chat is None:
                raise RuntimeContractError('RUNTIME_NOT_OWNED','native chat is outside operator tenancy')
            if chat['version']!=version:
                raise RuntimeContractError('CONVERSATION_VERSION_CONFLICT','conversation version changed before Close')
            generation = con.execute('SELECT * FROM conversation_runtime_generations WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1',(cid,)).fetchone()
            if generation is None:
                self._finish_chat(con,cid)
                return
            gid = generation['generation_id']
            control_id = 'close:'+gid
            existing = con.execute('SELECT 1 FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(gid,control_id)).fetchone()
            if existing is None:
                command = {'control_id':control_id,'request_sequence':generation['next_command_sequence'],'action':'close','target':None,'expected_activity_id':None,'options':{}}
                digest = payload_digest(command)
                con.execute("INSERT INTO conversation_runtime_commands(generation_id,command_id,command_sequence,kind,payload_digest,intent_json) VALUES(?,?,?,'control',?,?)",(gid,control_id,command['request_sequence'],digest,encoded(command)))
                con.execute("UPDATE conversation_runtime_generations SET close_intent=1,state='closing',next_command_sequence=next_command_sequence+1,updated_at=? WHERE generation_id=?",(time.time(),gid))
                projection = json.loads(chat['runtime_projection'])
                projection.update(state='closing',setup=None)
                con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(projection),cid))
                append_event(con,cid,'conversation.close.requested',{'generation_id':gid,'scope':'owned native generation','cleanup':'pending'})

    @staticmethod
    def _finish_chat(con,cid: str) -> None:
        # Preserve foreground uncertainty: unit exit alone does not prove an
        # earlier native interruption caused the root turn's terminal outcome.
        con.execute("UPDATE conversation_runs SET state='unknown',ended_at=datetime('now') WHERE conversation_id=? AND state IN ('leased','starting','running')",(cid,))
        con.execute("UPDATE conversation_messages SET state='cancelled',completed_at=datetime('now') WHERE conversation_id=? AND state IN ('accepted','queued','running')",(cid,))
        con.execute("UPDATE conversation_outbox SET state='cancelled',claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL WHERE conversation_id=? AND state IN ('pending','claimed')",(cid,))
        row = con.execute('SELECT state FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
        if row['state']=='queued':
            con.execute("UPDATE conversations SET state='idle' WHERE conversation_id=?",(cid,))
        elif row['state']=='running':
            con.execute("UPDATE conversations SET state='error' WHERE conversation_id=?",(cid,))
        if row['state']!='closed':
            con.execute("UPDATE conversations SET state='closed',closed_at=datetime('now'),version=version+1 WHERE conversation_id=?",(cid,))
            append_event(con,cid,'conversation.closed',{'status':'closed','reason':'native generation cleanup verified'})

    def close_generation(self,generation: str,cid: str,client: RuntimeClient,owner: int,shell: int) -> None:
        con = db_driver.connect(str(self.database))
        try:
            row = con.execute("SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",(generation,'close:'+generation)).fetchone()
        finally:
            con.close()
        if row is None or row['state']!='accepted':
            return
        command = json.loads(row['intent_json'])|{'payload_digest':row['payload_digest']}
        try:
            native_cleanup = client.request('close',command=command,timeout=5)
        except (OSError,RuntimeContractError):
            native_cleanup = {'outcome':'inconclusive','delivery_unknown':True,'detail':'native cleanup deadline/transport inconclusive'}
        # Record ambiguity before any OS fallback. Never rerun an unknown
        # control under a fresh identity after API death or a lost reply.
        self.store.receipt(generation,owner,shell,'close:'+generation,{'state':'unknown' if native_cleanup.get('delivery_unknown') else 'written','native_cleanup':native_cleanup})
        try:
            client.subscribe(self.store,owner,shell,project=project_event)
        except (OSError,RuntimeContractError):
            pass
        retained = self.store.command_status(generation,owner,shell,'close:'+generation)['receipt']
        if retained.get('state')=='terminal' and retained.get('outcome'):
            native_cleanup = retained
        self.finish_cleanup(generation,cid,owner,shell,native_cleanup)

    def recover_close(self,generation: str,cid: str,owner: int,shell: int) -> None:
        """Recovery never invents/replays native writes after ambiguous delivery.

        A persisted native receipt can finish independent verified OS cleanup;
        absent native proof remains pending even when the unit is now gone.
        """
        try:
            retained = self.store.command_status(generation,owner,shell,'close:'+generation)['receipt']
        except RuntimeContractError:
            retained = {}
        native_cleanup = retained if retained.get('state')=='terminal' else retained.get('native_cleanup',{})
        self.finish_cleanup(generation,cid,owner,shell,native_cleanup)

    def finish_cleanup(self,generation: str,cid: str,owner: int,shell: int,native_cleanup: dict) -> None:
        try:
            stopped = self.supervisor.stop(generation)
            os_exited = stopped.get('os_cleanup',{}).get('complete') is True
        except (OSError,RuntimeError):
            os_exited = False
        complete = bool(os_exited and native_cleanup.get('outcome')=='complete'
                        and not native_cleanup.get('unresolved_work') and not native_cleanup.get('unresolved_definitions'))
        cleanup = {'outcome':'complete' if complete else 'pending','unit_verified_exited':os_exited,
                   'native_outcome':native_cleanup.get('outcome','inconclusive'),
                   'unresolved_work':native_cleanup.get('unresolved_work',[]),
                   'unresolved_definitions':native_cleanup.get('unresolved_definitions',[]),
                   'detail':native_cleanup.get('detail','')}
        self.store.state(generation,owner,shell,'closed' if complete else 'lost',cleanup)
        con = db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_chat.cleanup_projection'):
                chat = con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                projection = json.loads(chat['runtime_projection'])
                projection.update(state='closed' if complete else 'lost',cleanup=cleanup,setup=None,primary=None)
                con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(projection),cid))
                append_event(con,cid,'control.outcome',{'generation_id':generation,'control_id':'close:'+generation,**cleanup})
                if complete:
                    self._finish_chat(con,cid)
        finally:
            con.close()
        conversation_events.notify(cid)

    def shutdown(self) -> None:
        self.stopped.set()
        self.wake.set()
        self.thread.join(6)
        # Native descriptors/controllers remain owned by their finite units.

    def attach(self, generation: str) -> tuple[RuntimeClient,int,int]:
        with self.lock:
            if generation not in self.clients:
                native = next((n for n in self.supervisor.inventory() if n['generation_id']==generation and n.get('status')=='active'),None)
                if native is None:
                    raise RuntimeContractError('CLEANUP_PENDING','captured controller is not available; ownership remains retained')
                con = db_driver.connect(str(self.database))
                try:
                    row = con.execute('SELECT owner_user_id,shell_id FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone()
                finally:
                    con.close()
                if row is None:
                    raise RuntimeContractError('RUNTIME_NOT_OWNED','controller has no canonical generation')
                client = RuntimeClient(Path(native['endpoint']),generation,controller_pid=native['main_pid'],controller_start_ticks=native['main_pid_start_ticks'],consumer=self.consumer)
                self.clients[generation] = client,int(row['owner_user_id']),int(row['shell_id'])
            value = self.clients[generation]
        value[0].attach(self.store,value[1],value[2])
        return value

    def run(self) -> None:
        while not self.stopped.is_set():
            con = db_driver.connect(str(self.database))
            try:
                rows = con.execute("SELECT g.generation_id,g.conversation_id FROM conversation_runtime_generations g JOIN conversations c USING(conversation_id) WHERE c.runtime_mode='native_experiment' AND g.state NOT IN ('closed') ORDER BY g.created_at LIMIT 4").fetchall()
            finally:
                con.close()
            for row in rows:
                if self.stopped.is_set():
                    break
                try:
                    client,owner,shell = self.attach(row['generation_id'])
                    generation_state = self.store.status(row['generation_id'],owner,shell)
                    if generation_state['close_intent']:
                        command_state = self.store.command_status(row['generation_id'],owner,shell,'close:'+row['generation_id'])['state']
                        if command_state=='accepted':
                            self.close_generation(row['generation_id'],row['conversation_id'],client,owner,shell)
                        else:
                            self.recover_close(row['generation_id'],row['conversation_id'],owner,shell)
                        continue
                    replay = client.request('subscribe',after=self.store.status(row['generation_id'],owner,shell)['last_sequence'])
                    identity = client.request('status').get('identity') or {}
                    activity = replay.get('primary')
                    primary = {'root_id':identity['root_id'],'thread_id':identity['root_id'],'activity_id':activity} if activity and identity.get('root_id') else None
                    committed = self.store.ingest(row['generation_id'],owner,shell,client.lease,replay,
                        project=lambda con,cid,seq,event,primary=primary: project_event(con,cid,seq,event,primary=primary))
                    client.request('ack',sequence=committed)
                    if replay['events']:
                        conversation_events.notify(row['conversation_id'])
                    if self.store.status(row['generation_id'],owner,shell)['close_intent']:
                        self.close_generation(row['generation_id'],row['conversation_id'],client,owner,shell)
                except (RuntimeContractError,OSError):
                    # Lease/reconnection failure is not verified native exit.
                    # Keep canonical unresolved ownership and stable commands.
                    retained = db_driver.connect(str(self.database))
                    try:
                        owned = retained.execute('SELECT owner_user_id,shell_id,close_intent,cleanup_json FROM conversation_runtime_generations WHERE generation_id=?',(row['generation_id'],)).fetchone()
                    finally:
                        retained.close()
                    if owned and owned['close_intent'] and not json.loads(owned['cleanup_json']).get('unit_verified_exited'):
                        self.recover_close(row['generation_id'],row['conversation_id'],owned['owner_user_id'],owned['shell_id'])
                    continue
            self.wake.wait(1)
            self.wake.clear()


def projection(conversation: Any) -> dict | None:
    if conversation['runtime_mode']!='native_experiment':
        return None
    return public_payload(json.loads(conversation['runtime_projection']))
