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
from concurrent.futures import ThreadPoolExecutor
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


def output_key(event: dict,harness: str) -> str | None:
    """Engine identity for one observed complete chunk; native refs stay opaque."""
    ref=event.get('reference') or {}
    data=event['data']
    if (event['kind']!='output.final' or event['freshness']!='current' or event['partial']
            or event['grade'] in {'inconclusive','incompatible'}
            or not ref.get('root_id') or not ref.get('activity_id') or not event.get('request_id')
            or not (ref.get('thread_id')==ref.get('root_id') or harness=='claude' and ref.get('thread_id') is None)
            or not isinstance(data.get('text'),str) or len(data['text'])>4096):
        return None
    if harness=='claude':
        digest=data.get('text_digest')
        part=data.get('part')
        if (event['provenance'] not in {'claude:channel-reply','claude:owned-transcript-text','claude:MessageDisplay'}
                or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)
                or type(part) is not int or not 0<=part<128 or type(data.get('last')) is not bool):
            return None
        identity={'full_text_digest':digest,'part':part}
    else:
        offset=data.get('offset',0)
        if data.get('kind')!='assistant' or not ref.get('item_id') or type(offset) is not int or offset<0:
            return None
        identity={'native_item_id':ref['item_id'],'offset':offset}
    return payload_digest({'root_id':ref['root_id'],'activity_id':ref['activity_id'],
                           'request_id':event['request_id'],**identity})


def mirrored_output(con,cid: str,rid: Any,event: dict,harness: str) -> bool:
    key=output_key(event,harness)
    return bool(rid is not None and key and con.execute("SELECT 1 FROM conversation_events WHERE conversation_id=? AND run_id=? AND event_type='assistant.delta' AND json_extract(payload,'$.engine_output_key')=? LIMIT 1",
                        (cid,rid,key)).fetchone())


def project_event(con, cid: str, sequence: int, event: dict, *, primary: dict | None = None) -> None:
    """Same transaction as the controller watermark; replay never duplicates.

    Only an attributable GUI root command can finish a GUI foreground run.
    Autonomous and child activity remains independently visible. Terminal
    command truth is already reconciled by RuntimeStore before this callback.
    """
    row = con.execute('SELECT runtime_mode,runtime_projection,state,owner_user_id,shell_id FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
    if row is None or row['runtime_mode'] != 'native_experiment':
        raise RuntimeContractError('RUNTIME_NOT_OWNED','native projection requires opted-in chat')
    projection = json.loads(row['runtime_projection'])
    generation = projection.get('generation_id')
    if not generation:
        raise RuntimeContractError('GENERATION_INVALID','native projection has no captured generation')
    kind, ref = event['kind'], event.get('reference') or {}
    current = con.execute('SELECT state,close_intent,harness,cleanup_json FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=? AND owner_user_id=? AND shell_id=?',(generation,cid,row['owner_user_id'],row['shell_id'])).fetchone()
    if current is None:
        raise RuntimeContractError('GENERATION_INVALID','generation is outside chat projection')
    command = None
    if event.get('request_id'):
        command = con.execute('SELECT intent_json,state FROM conversation_runtime_commands WHERE generation_id=? AND command_id=? AND kind=\'submit\'',
                              (generation,event['request_id'])).fetchone()
    intent = json.loads(command['intent_json']) if command else {}
    mid, rid = intent.get('message_id'), intent.get('run_id')
    if mid is not None or rid is not None:
        if not all(str(value).isdecimal() and int(str(value))>0 for value in (mid,rid)):
            raise RuntimeContractError('COMMAND_INVALID','GUI intent has no attributable engine message/run')
        mid,rid=int(str(mid)),int(str(rid))
    root_activity = bool(ref.get('root_id') and (ref.get('thread_id')==ref['root_id'] or current['harness']=='claude' and ref.get('thread_id') is None))
    projection.update(controller_sequence=sequence,observed_at=event['observed_at'],
                      freshness=event['freshness'],partial=bool(projection.get('partial') or event['partial']))
    cleanup=json.loads(current['cleanup_json'])
    closed=(current['state']=='closed' and cleanup.get('outcome')=='complete'
            and cleanup.get('unit_verified_exited') is True and not cleanup.get('unresolved_work') and not cleanup.get('unresolved_definitions'))
    if closed:
        projection.update(state='closed',setup=None)
    elif current['close_intent']:
        projection.update(state='closing',setup=None)
    elif kind == 'runtime.setup' and current['state'] not in {'ready','closed','lost'}:
        eligible = event['freshness']=='current' and not event['partial'] and event['grade']!='inconclusive'
        projection.update(state='needs_consent' if eligible else 'setup_inconclusive',setup=event['data'] if eligible else None)
    elif kind == 'runtime.ready' and current['state'] not in {'closed','lost'}:
        eligible=(current['state']=='ready' and root_activity and event['freshness']=='current'
                  and not event['partial'] and event['grade'] in {'compatible','unverified'})
        observed={key:event['data'][key] for key in ('native_route','memory_policy','readiness_diagnostic') if key in event['data']}
        projection['latest_startup_observation']={'grade':event['grade'],'freshness':event['freshness'],
                                                 'partial':event['partial'],'data':observed}
        if eligible:
            projection.update(state='ready',setup=None,root_id=ref['root_id'],startup_evidence=observed)
        elif projection.get('state')!='ready':
            projection.update(state='setup_inconclusive',setup=None)
    elif kind in {'runtime.lost','ownership.failed'}:
        projection.update(state='lost',freshness='unknown',partial=True,setup=None)
    elif kind == 'capability.observed':
        # Only checker-bound grades installed by the owner admit GUI actions;
        # native observations are diagnostics, not a reusable certificate.
        projection['capability_observation'] = event['data']
    # Retained controller journal owns primary/terminal reconciliation, not
    # this individual event (which may be child, stale, late or replayed).
    projection['primary'] = None if closed else primary
    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(json.dumps(projection),cid))
    if (root_activity and mid is not None and rid is not None
            and event['freshness']=='current' and not event['partial']
            and event['grade'] not in {'inconclusive','incompatible'}):
        run = con.execute('SELECT state FROM conversation_runs WHERE run_id=? AND conversation_id=? AND trigger_message_id=?',(rid,cid,mid)).fetchone()
        if run is None:
            raise RuntimeContractError('COMMAND_INVALID','command run is outside chat')
        if kind in {'activity.started','activity.processed'} and run['state']=='starting':
            con.execute("UPDATE conversation_runs SET state='running' WHERE run_id=?",(rid,))
            con.execute("UPDATE conversation_messages SET state='running' WHERE message_id=? AND state IN ('accepted','queued')",(mid,))
            con.execute("UPDATE conversations SET state='running' WHERE conversation_id=? AND state='queued'",(cid,))
            append_event(con,cid,'run.started',{'native_activity_id':ref.get('activity_id')},message_id=mid,run_id=rid)
        elif kind == 'output.final' and run['state'] in {'starting','running'} and output_key(event,current['harness']):
            part=event['data'].get('part',event['data'].get('offset',0))
            key=output_key(event,current['harness'])
            prior=con.execute("SELECT 1 FROM conversation_events WHERE conversation_id=? AND run_id=? AND event_type='assistant.delta' AND json_extract(payload,'$.engine_output_key')=?",
                              (cid,rid,key)).fetchone()
            if prior is None:
                append_event(con,cid,'assistant.delta',
                             {'text':event['data'].get('text',''),'native_reference':ref,'native_final':True,'native_part':part,
                              'engine_output_key':key,
                              'native_controller_sequence':sequence},message_id=mid,run_id=rid)
        elif kind=='activity.terminal' and run['state'] in {'starting','running'}:
            status = event['data'].get('status')
            target = 'succeeded' if status in {'completed','complete','succeeded'} else 'cancelled' if status in {'interrupted','cancelled'} else 'failed' if status in {'failed','errored'} else 'unknown'
            if run['state']=='starting' and target=='succeeded':
                # A qualified terminal can be retained after a lost start
                # observation; success itself proves that input was processed.
                con.execute("UPDATE conversation_runs SET state='running' WHERE run_id=?",(rid,))
                con.execute("UPDATE conversation_messages SET state='running' WHERE message_id=? AND state IN ('accepted','queued')",(mid,))
            con.execute("UPDATE conversation_runs SET state=?,ended_at=datetime('now') WHERE run_id=?",(target,rid))
            message_state = 'completed' if target=='succeeded' else 'cancelled' if target=='cancelled' else 'failed'
            con.execute("UPDATE conversation_messages SET state=?,completed_at=datetime('now') WHERE message_id=?",(message_state,mid))
            queued = con.execute("SELECT 1 FROM conversation_outbox WHERE conversation_id=? AND state='pending'",(cid,)).fetchone()
            con.execute("UPDATE conversations SET state=?,last_activity_at=datetime('now') WHERE conversation_id=? AND state IN ('queued','running')",('queued' if queued else 'idle',cid))
            append_event(con,cid,'run.completed' if target=='succeeded' else 'run.interrupted' if target=='cancelled' else 'run.failed' if target=='failed' else 'run.unknown',
                         {'status':status,'runtime_alive':True},message_id=mid,run_id=rid)
    envelope = {'generation_id':generation,'controller_sequence':sequence,
                'engine_message_id':mid,'engine_run_id':rid,
                'engine_output_key':output_key(event,current['harness']),
                'engine_mirrored':mirrored_output(con,cid,rid,event,current['harness']),**event}
    append_event(con,cid,kind,envelope,message_id=mid,run_id=rid)


class NativeChatsService:
    """Bounded API consumer slots. Shutdown releases consumers only.

    Launch preparation/check factories are fixture-owned, never an arbitrary
    executable endpoint. A restart attaches captured live generations instead
    of reconstructing their context or replaying ambiguous submissions.
    """
    def __init__(self, database: Path, root: Path, supervisor, *, prepare_context=None, resolve_route=None, probe_delegate=None):
        self.database, self.root, self.supervisor = database, root, supervisor
        self.store = RuntimeStore(database)
        self.prepare_context = prepare_context
        self.route_resolver = resolve_route
        self.probe_delegate = probe_delegate
        self.consumer = uuid.uuid4().hex
        self.clients: dict[str,tuple[RuntimeClient,int,int]] = {}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.starting: set[str] = set()
        self.starts=ThreadPoolExecutor(max_workers=2,thread_name_prefix='native-chat-start')
        self.thread = threading.Thread(target=self.run,name='native-chats-consumer',daemon=True)

    def start(self) -> None:
        self.thread.start()

    def notify(self) -> None:
        self.wake.set()

    def dispatch_queued(self,generation: str,cid: str,client: RuntimeClient,owner: int,shell: int) -> None:
        """One FIFO intent per GUI message; only proved no-write can retry.

        Run and outbox claim precede the durable native intent. A restart may
        reconcile that same accepted command with the controller journal;
        written, unknown and terminal commands are never submitted again.
        Native autonomous activity may win after this idle observation: its
        pre-edge busy refusal retains the original GUI intent and run.
        """
        con=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_chat.dispatch_intent'):
                chat=con.execute('SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=? AND shell_id=?',(cid,owner,shell)).fetchone()
                runtime=json.loads(chat['runtime_projection']) if chat else {}
                captured=con.execute('SELECT * FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=?',(generation,cid)).fetchone()
                if (chat is None or chat['state']=='closed' or runtime.get('role')=='probe'
                        or runtime.get('generation_id')!=generation or runtime.get('state')!='ready'
                        or runtime.get('primary') or captured is None or captured['state']!='ready' or captured['close_intent']):
                    return
                self.require_capability(runtime.get('capabilities',{}),'submission')
                message=con.execute("SELECT m.*,o.outbox_id FROM conversation_outbox o JOIN conversation_messages m USING(message_id) WHERE o.conversation_id=? AND o.state IN ('pending','claimed') ORDER BY o.outbox_id LIMIT 1",(cid,)).fetchone()
                if message is None:
                    return
                command_id='gui-message:'+str(message['message_id'])
                retained=con.execute('SELECT state FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(generation,command_id)).fetchone()
                if retained and retained['state'] not in {'accepted','not_written'}:
                    return # Ambiguous/known native write is never replayed.
                if con.execute("SELECT 1 FROM conversation_runtime_commands WHERE generation_id=? AND kind='submit' AND command_id!=? AND state IN ('accepted','written','processed','unknown') LIMIT 1",(generation,command_id)).fetchone():
                    return
                if con.execute("SELECT 1 FROM conversation_runs WHERE conversation_id=? AND trigger_message_id!=? AND state IN ('leased','starting','running') LIMIT 1",(cid,message['message_id'])).fetchone():
                    return
                prior=con.execute('SELECT * FROM conversation_runs WHERE trigger_message_id=? ORDER BY attempt DESC LIMIT 1',(message['message_id'],)).fetchone()
                if prior and prior['state'] not in {'starting','running'}:
                    return
                rid=prior['run_id'] if prior else con.execute("INSERT INTO conversation_runs(conversation_id,shell_id,trigger_message_id,state,lease_owner,lease_expires_at,started_at,heartbeat_at) VALUES(?,?,?,'starting',?,datetime('now','+60 seconds'),datetime('now'),datetime('now'))",
                    (cid,shell,message['message_id'],self.consumer)).lastrowid
                con.execute("UPDATE conversation_outbox SET state='claimed',claim_owner=?,claimed_at=datetime('now'),lease_expires_at=datetime('now','+60 seconds') WHERE outbox_id=? AND state IN ('pending','claimed')",(self.consumer,message['outbox_id']))
                con.execute("UPDATE conversation_messages SET state='queued' WHERE message_id=? AND state='accepted'",(message['message_id'],))
                con.execute("UPDATE conversations SET state='queued' WHERE conversation_id=? AND state='idle'",(cid,))
            result=client.submit(self.store,owner,shell,command_id,text=message['body'],
                                 message_id=str(message['message_id']),run_id=str(rid),
                                 source='gui' if message['sender_kind']=='user' else 'system')
            with db_driver.write_transaction(con,'native_chat.dispatch_receipt'):
                # Close can cancel this outbox while native dispatch returns.
                # Late receipts cannot restore a cancelled/dispatched slot.
                if result.get('state') in {'written','processed','terminal','unknown'}:
                    con.execute("UPDATE conversation_outbox SET state='dispatched',run_id=?,dispatched_at=datetime('now') WHERE outbox_id=? AND state='claimed'",(rid,message['outbox_id']))
                elif result.get('state') in {'rejected','unsupported'}:
                    con.execute("UPDATE conversation_outbox SET state='cancelled',claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL WHERE outbox_id=? AND state='claimed'",(message['outbox_id'],))
                    con.execute("UPDATE conversation_runs SET state='failed',ended_at=datetime('now'),error_code='NATIVE_SUBMISSION_REFUSED' WHERE run_id=? AND state='starting'",(rid,))
                    con.execute("UPDATE conversation_messages SET state='failed',completed_at=datetime('now') WHERE message_id=? AND state IN ('accepted','queued')",(message['message_id'],))
                    queued=con.execute("SELECT 1 FROM conversation_outbox WHERE conversation_id=? AND state='pending'",(cid,)).fetchone()
                    con.execute("UPDATE conversations SET state=? WHERE conversation_id=? AND state='queued'",('queued' if queued else 'idle',cid))
            conversation_events.notify(cid)
        finally:
            con.close()

    def resolve_route(self,harness,model,effort):
        if self.route_resolver is None:
            raise RuntimeContractError('NATIVE_ROUTE_INCONCLUSIVE','owned native account/options observation is not ready')
        return self.route_resolver(harness,model,effort)

    def schedule_starts(self) -> None:
        if self.prepare_context is None or self.stopped.is_set():
            return
        con=db_driver.connect(str(self.database))
        try:
            candidates=con.execute("SELECT conversation_id,runtime_projection FROM conversations c WHERE runtime_mode='native_experiment' AND state!='closed' AND NOT EXISTS(SELECT 1 FROM conversation_runtime_generations g WHERE g.conversation_id=c.conversation_id) ORDER BY created_at LIMIT 4").fetchall()
            for chat in candidates:
                with self.lock:
                    cid=chat['conversation_id']
                    if cid in self.starting or len(self.starting)>=2:
                        continue
                    runtime=json.loads(chat['runtime_projection'])
                    if runtime.get('role')=='probe':
                        continue # The finite checker alone owns probe startup.
                    if runtime.get('state') in {'preparation_inconclusive','lost','closing'}:
                        if runtime.get('state')=='closing' and runtime.get('preparation_owner'):
                            self.recover_preparation(cid,runtime)
                        continue
                    if runtime.get('preparation_owner'):
                        self.recover_preparation(cid,runtime)
                        continue
                    generation=runtime.get('generation_id') or uuid.uuid4().hex
                    preparation_owner=self.supervisor.preparation_identity()
                    with db_driver.write_transaction(con,'native_chat.start_intent'):
                        current=con.execute("SELECT state,runtime_projection FROM conversations WHERE conversation_id=?",(cid,)).fetchone()
                        if (current['state']=='closed' or current['runtime_projection']!=chat['runtime_projection']
                                or con.execute('SELECT 1 FROM conversation_runtime_generations WHERE conversation_id=?',(cid,)).fetchone()):
                            continue
                        runtime=json.loads(current['runtime_projection'])
                        runtime.update(generation_id=generation,state='preparing',capabilities={},setup=None,partial=True,freshness='unknown',
                                       preparation_owner=preparation_owner,preparation_cleanup=None)
                        con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                        append_event(con,cid,'capability.observed',{'generation_id':generation,'grade':'inconclusive','phase':'canonical preparation pending'})
                    self.starting.add(cid)
                    self.starts.submit(self.open_generation,cid,generation)
                    conversation_events.notify(cid)
        finally:
            con.close()

    def open_generation(self,cid: str,generation: str) -> None:
        """Prepare/open outside the consumer loop so setup and Close stay usable.

        The normal context receives only fingerprint-bound cache admission;
        named probe grants belong to the independent finite checker factory.
        Existing captured generations are attached, never prepared again.
        """
        reserved=False
        try:
            context,fingerprint,native=self.prepare_context(cid,generation)
            if self.stopped.is_set():
                raise RuntimeContractError('API_STOPPING','API release fences new native allocation')
            self.require_capability(context.capability_evidence,'submission')
            con=db_driver.connect(str(self.database))
            try:
                chat=con.execute('SELECT state,runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                runtime=json.loads(chat['runtime_projection'])
                if chat['state']=='closed' or runtime.get('state')=='closing' or runtime.get('generation_id')!=generation:
                    raise RuntimeContractError('RUNTIME_CLOSING','Close fences completion of canonical preparation')
            finally:
                con.close()
            binding=native|{'implementation_digest':fingerprint.implementation_digest,'fingerprint':fingerprint.key,
                            'captured_capabilities':dict(context.capability_evidence)}
            self.store.reserve(context,binding)
            reserved=True
            closing=False
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_chat.captured_projection'):
                    chat=con.execute('SELECT runtime_projection,state,version FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                    if chat['state']=='closed':
                        raise RuntimeContractError('RUNTIME_CLOSING','chat closed during canonical preparation')
                    runtime=json.loads(chat['runtime_projection'])
                    if runtime.get('generation_id')!=generation:
                        raise RuntimeContractError('GENERATION_INVALID','startup was replaced before captured launch')
                    closing=runtime.get('state')=='closing'
                    runtime.update(state='closing' if closing else 'starting',capabilities=dict(context.capability_evidence),partial=False,
                                   preparation_owner=None,
                                   captured_identity={'executable':{'path':str(context.executable.path),'sha256':context.executable.sha256,'version':context.executable.version},'implementation_digest':fingerprint.implementation_digest,'fingerprint':fingerprint.key})
                    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                if closing:
                    self.request_close(con,cid,context.owner_user_id,chat['version']+1)
            finally:
                con.close()
            # reserve() precedes launch. The unit persists independently of API
            # release and the consumer ingests setup while this call is pending.
            if self.store.status(generation,context.owner_user_id,context.shell_id)['close_intent']:
                # The fixed supervisor has not launched this registered unit.
                # No native process/definition existed; record that limited
                # fact, then independently verify OS cleanup before release.
                cleanup={'outcome':'complete','never_started':True}
                self.store.receipt(generation,context.owner_user_id,context.shell_id,'close:'+generation,
                                   {'state':'rejected','native_cleanup':cleanup})
                self.finish_cleanup(generation,cid,context.owner_user_id,context.shell_id,cleanup)
                return
            self.supervisor.launch(generation)
            client,_,_=self.attach(generation)
            started=client.open(context)
            if started.get('state') not in {'ready','needs_consent'}:
                raise RuntimeContractError('NATIVE_START_INCONCLUSIVE','native readiness remains inconclusive; Close retains ownership')
            self.notify()
        except (RuntimeContractError,OSError,ValueError,RuntimeError,SystemExit) as exc:
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_chat.start_inconclusive'):
                    chat=con.execute('SELECT runtime_projection,state FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                    if chat and chat['state']!='closed':
                        runtime=json.loads(chat['runtime_projection'])
                        if runtime.get('generation_id')==generation and runtime.get('state')!='closing':
                            runtime.update(state='setup_inconclusive' if reserved else 'preparation_inconclusive',partial=True,freshness='unknown',setup=None,
                                           diagnostic=getattr(exc,'code','PREPARATION_INCONCLUSIVE'))
                            con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                            append_event(con,cid,'capability.observed',{'generation_id':generation,'grade':'inconclusive','detail':runtime['diagnostic']})
            finally:
                con.close()
            if not reserved:
                # No native process may launch without canonical reservation.
                # A registered preparation root still counts until verified stop.
                self.finish_preparation(cid,generation)
        finally:
            with self.lock:
                self.starting.discard(cid)
            conversation_events.notify(cid)

    def recover_preparation(self,cid: str,runtime: dict) -> None:
        """No new preparation after API death; verify the old worker exited."""
        if self.supervisor.preparation_exited(runtime['preparation_owner']):
            self.finish_preparation(cid,runtime['generation_id'])

    def finish_preparation(self,cid: str,generation: str) -> None:
        """Only a never-launched generation can release provisional ownership."""
        try:
            owned=next((n for n in self.supervisor.inventory() if n['generation_id']==generation),None)
            if owned is not None:
                if owned['status'] not in {'registered','stopped'}:
                    return  # Any possible launch requires canonical Close/cleanup.
                stopped=self.supervisor.stop(generation)
                if stopped.get('os_cleanup',{}).get('complete') is not True:
                    return
            con=db_driver.connect(str(self.database))
            try:
                with db_driver.write_transaction(con,'native_chat.preparation_cleanup'):
                    if con.execute('SELECT 1 FROM conversation_runtime_generations WHERE generation_id=?',(generation,)).fetchone():
                        return
                    chat=con.execute('SELECT state,runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
                    runtime=json.loads(chat['runtime_projection'])
                    if runtime.get('generation_id')!=generation:
                        return
                    closing=runtime.get('state')=='closing'
                    runtime.update(preparation_owner=None,preparation_cleanup={'unit_verified_exited':True,'never_launched':True},
                                   state='closed' if closing else 'preparation_inconclusive',setup=None,partial=True)
                    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                    if closing:
                        self._finish_chat(con,cid)
            finally:
                con.close()
        except (OSError,RuntimeError):
            return  # Unverified ownership remains retained for explicit cleanup.

    def control(self,con,cid: str,owner: int,key: str,body: dict) -> dict:
        """Operator-authorized finite actions against stored native targets.

        HTTP idempotency binds the whole requested version/scope. Replays of
        written/unknown controls return retained truth without another write.
        Neither a client-provided native reference nor a broad capability
        grade can select an unproved target kind.
        """
        if body.get('action') not in {'enable_local_channel','stop_reply','stop_work','stop_automation'}:
            raise RuntimeContractError('CONTROL_INVALID','unknown finite native action')
        request_hash = payload_digest(body)
        with db_driver.write_transaction(con,'native_chat.control_intent'):
            chat = con.execute("SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=? AND runtime_mode='native_experiment'",(cid,owner)).fetchone()
            if chat is None:
                raise RuntimeContractError('RUNTIME_NOT_OWNED','native chat is outside operator tenancy')
            prior = con.execute('SELECT * FROM conversation_runtime_http_requests WHERE conversation_id=? AND request_key=?',(cid,key)).fetchone()
            if prior:
                if prior['request_hash']!=request_hash:
                    raise RuntimeContractError('CONTROL_IDEMPOTENCY_CONFLICT','control key was reused with a different request')
                command_id, generation = prior['command_id'], prior['generation_id']
                command_row = con.execute('SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(generation,command_id)).fetchone()
                if command_row['state'] not in {'accepted','not_written'}:
                    return {'control_id':command_id,'state':command_row['state'],'receipt':json.loads(command_row['receipt_json']),'duplicate':True}
            else:
                if chat['version']!=body['version']:
                    raise RuntimeContractError('CONVERSATION_VERSION_CONFLICT','conversation changed before control')
                runtime = json.loads(chat['runtime_projection'])
                generation = runtime.get('generation_id')
                if not generation or generation!=body['generation_id'] or runtime.get('state') in {'closing','closed','lost'}:
                    raise RuntimeContractError('GENERATION_INVALID','control is stale or outside the current native generation')
                if runtime.get('role')=='probe' and runtime.get('check_deadline',0)<=time.time():
                    raise RuntimeContractError('PROBE_EXPIRED','finite probe action expired; Close remains available')
                generation_row = con.execute('SELECT * FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=? AND owner_user_id=?',(generation,cid,owner)).fetchone()
                if generation_row is None or generation_row['close_intent']:
                    raise RuntimeContractError('RUNTIME_CLOSING','Close fences subsequent controls')
                action = body['action']
                caps = runtime.get('capabilities',{})
                options: dict[str,Any] = {}
                expected = None
                target = None
                if action=='enable_local_channel':
                    setup = runtime.get('setup')
                    if runtime.get('state')!='needs_consent' or not setup or setup['setup_id']!=body.get('setup_id'):
                        raise RuntimeContractError('SETUP_INVALID','documented startup choice is no longer current')
                    options = {name:setup[name] for name in ('setup_id','configuration_sha256')}
                elif action=='stop_reply':
                    target = runtime.get('primary')
                    expected = body.get('expected_activity_id')
                    if not target or target.get('activity_id')!=expected:
                        raise RuntimeContractError('CONTROL_STALE','foreground activity changed before control')
                    self.require_capability(caps,'stop_reply')
                else:
                    work = con.execute('SELECT projection_json FROM conversation_runtime_work WHERE generation_id=? AND work_key=?',(generation,body.get('work_key'))).fetchone()
                    if work is None:
                        raise RuntimeContractError('WORK_NOT_OWNED','work target is outside this generation')
                    observed = json.loads(work['projection_json'])
                    target = observed['reference']
                    kind = observed['data'].get('kind') or ('terminal' if target.get('native_process_id') else 'task' if target.get('work_id') else 'child')
                    if observed['partial'] or observed['freshness'] in {'unknown','stale'}:
                        raise RuntimeContractError('WORK_INCONCLUSIVE','target inventory requires current attributable reconciliation')
                    if action=='stop_automation':
                        if kind!='automation':
                            raise RuntimeContractError('WORK_NOT_OWNED','automation control requires a native definition target')
                        self.require_capability(caps,'automation')
                    else:
                        if kind not in {'terminal','child'}:
                            raise RuntimeContractError('CAPABILITY_INCONCLUSIVE','this native work kind has no demonstrated stop coverage')
                        self.require_capability(caps,'stop_work')
                        self.require_capability(caps,'stop_work_'+kind)
                        expected = body.get('expected_activity_id')
                        if kind=='child' and (not expected or target.get('activity_id')!=expected):
                            raise RuntimeContractError('CONTROL_STALE','child activity changed before control')
                command_id = 'gui-control:'+payload_digest({'conversation':cid,'key':key})
                command = {'control_id':command_id,'request_sequence':generation_row['next_command_sequence'],
                           'action':action,'target':target,'expected_activity_id':expected,'options':options}
                digest = payload_digest(command)
                con.execute("INSERT INTO conversation_runtime_commands(generation_id,command_id,command_sequence,kind,payload_digest,intent_json) VALUES(?,?,?,'control',?,?)",(generation,command_id,command['request_sequence'],digest,encoded(command)))
                con.execute('UPDATE conversation_runtime_generations SET next_command_sequence=next_command_sequence+1 WHERE generation_id=?',(generation,))
                con.execute('INSERT INTO conversation_runtime_http_requests VALUES(?,?,?,?,?)',(cid,key,request_hash,generation,command_id))
                command_row = con.execute('SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(generation,command_id)).fetchone()
        edge=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=? AND owner_user_id=?',(cid,owner)).fetchone()
        edge_runtime=json.loads(edge[0]) if edge else {}
        if edge_runtime.get('role')=='probe' and edge_runtime.get('check_deadline',0)<=time.time():
            raise RuntimeContractError('PROBE_EXPIRED','finite probe action expired before dispatch; Close remains available')
        client,actual_owner,shell = self.attach(generation)
        if actual_owner!=owner:
            raise RuntimeContractError('RUNTIME_NOT_OWNED','controller consumer differs from operator tenancy')
        command = json.loads(command_row['intent_json'])|{'payload_digest':command_row['payload_digest']}
        try:
            result = client.request('control',command=command,timeout=5)
        except (OSError,RuntimeContractError) as exc:
            result = {'state':'unknown','detail':getattr(exc,'code','CONTROLLER_UNAVAILABLE')}
        self.store.receipt(generation,owner,shell,command_id,result)
        self.notify()
        return {'control_id':command_id,**result}

    @staticmethod
    def require_capability(caps: dict,capability: str) -> None:
        if caps.get(capability)!='compatible':
            verdict = 'CAPABILITY_INCOMPATIBLE' if caps.get(capability)=='incompatible' else 'CAPABILITY_INCONCLUSIVE'
            raise RuntimeContractError(verdict,f'{capability} has no matching compatible native proof coverage')

    def observe_wake(self, con, cid: str) -> dict:
        """Fresh private observation; callers perform RPC outside write transactions."""
        if con.in_transaction:
            raise RuntimeError('wake observation cannot run inside a write transaction')
        chat=con.execute('SELECT * FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
        if chat is None or chat['runtime_mode']!='native_experiment':
            raise RuntimeContractError('RUNTIME_NOT_OWNED','wake requires an owned native chat')
        runtime=json.loads(chat['runtime_projection'])
        generation=runtime.get('generation_id')
        if not generation or runtime.get('role')!='ordinary':
            raise RuntimeContractError('WAKE_NOT_QUIET','native preparation/probe is not a quiet ordinary generation')
        client,owner,shell=self.attach(generation)
        if owner!=chat['owner_user_id'] or shell!=chat['shell_id']:
            raise RuntimeContractError('RUNTIME_NOT_OWNED','wake controller differs from chat owner')
        client.request('snapshot',timeout=1) # Root occupancy reconciliation precedes quiet placement.
        status=client.request('status',timeout=1)
        return {'conversation_id':cid,'generation_id':generation,'owner_user_id':owner,'shell_id':shell,
                'observed_at':time.time(),'consumer_fence':client.lease['fence'],
                'status':status}

    def request_close(self,con,cid: str,owner: int,version: int, *, wake_id: int | None = None,
                      observation: dict | None = None, quiet_seconds: int = 0) -> None:
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
                runtime=json.loads(chat['runtime_projection'])
                if runtime.get('preparation_owner'):
                    runtime.update(state='closing',setup=None)
                    con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                    append_event(con,cid,'conversation.close.requested',{'generation_id':runtime['generation_id'],'phase':'preparation cleanup pending'})
                    return
                self._finish_chat(con,cid)
                return
            gid = generation['generation_id']
            projection=json.loads(chat['runtime_projection'])
            if projection.get('generation_id')!=gid:
                raise RuntimeContractError('GENERATION_INVALID','Close must match the retained chat generation')
            probe=projection.get('role')=='probe'
            control_id = 'probe-close' if probe else 'close:'+gid
            options={}
            if wake_id is not None:
                from sprint_native_wakes import current_observation
                if (observation is None or not current_observation(con,self,chat,observation)
                        or observation['generation_id']!=gid or generation['close_intent']):
                    raise RuntimeContractError('WAKE_NOT_QUIET','wake ownership or quiet observation changed before Close')
                quiet=observation['status']['quiet']
                options={'wake_quiet_epoch':quiet['epoch'],'wake_quiet_seconds':quiet_seconds}
                control_id=f'wake-close:{wake_id}:{gid}:{quiet["epoch"]}'
                projection['wake_rotation']={'wake_id':wake_id,'epoch':quiet['epoch']}
                projection['close_control_id']=control_id
            else:
                # An explicit operator Close supersedes a conditional wake
                # attempt. Its unconditional fence may never be cleared by a
                # delayed proved-no-write response from that older attempt.
                projection.pop('wake_rotation',None)
                projection.pop('close_control_id',None)
            existing = con.execute('SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(gid,control_id)).fetchone()
            if existing is not None and wake_id is not None:
                previous=json.loads(existing['intent_json'])
                if existing['state']!='not_written' or previous.get('options')!=options:
                    raise RuntimeContractError('CLEANUP_PENDING','wake Close has a retained written or unknown intent')
                con.execute("UPDATE conversation_runtime_commands SET state='accepted' WHERE generation_id=? AND command_id=?",(gid,control_id))
            if existing is None:
                command = {'control_id':control_id,'request_sequence':generation['next_command_sequence'],'action':'close'}
                if not probe:
                    command.update(target=None,expected_activity_id=None,options=options)
                digest = payload_digest(command)
                con.execute("INSERT INTO conversation_runtime_commands(generation_id,command_id,command_sequence,kind,payload_digest,intent_json) VALUES(?,?,?,'control',?,?)",(gid,control_id,command['request_sequence'],digest,encoded(command)))
                con.execute("UPDATE conversation_runtime_generations SET next_command_sequence=next_command_sequence+1 WHERE generation_id=?",(gid,))
            if generation['state']!='closed':
                con.execute("UPDATE conversation_runtime_generations SET close_intent=1,state='closing',updated_at=? WHERE generation_id=?",(time.time(),gid))
                projection.update(state='closing',setup=None)
                con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(projection),cid))
                append_event(con,cid,'conversation.close.requested',{'generation_id':gid,'scope':'owned native generation','cleanup':'pending'})

    def close_id(self,generation: str,cid: str) -> str:
        con=db_driver.connect(str(self.database))
        try:
            row=con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
            runtime=json.loads(row[0]) if row else {}
            if runtime.get('generation_id')!=generation:
                raise RuntimeContractError('GENERATION_INVALID','Close is outside the retained chat generation')
            return runtime.get('close_control_id') or ('probe-close' if runtime.get('role')=='probe' else 'close:'+generation)
        finally:
            con.close()

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
        control_id=self.close_id(generation,cid)
        con = db_driver.connect(str(self.database))
        try:
            row = con.execute("SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",(generation,control_id)).fetchone()
        finally:
            con.close()
        if row is None or row['state']!='accepted':
            return
        command = json.loads(row['intent_json'])|{'payload_digest':row['payload_digest']}
        try:
            native_cleanup = client.request('close',command=command,timeout=5)
        except RuntimeContractError as exc:
            native_cleanup = ({'state':'not_written','detail':exc.code} if exc.code in {'BACKPRESSURE','LEASE_FENCED','DEADLINE_EXPIRED'}
                              else {'outcome':'inconclusive','delivery_unknown':True,'detail':'native cleanup deadline/transport inconclusive'})
        except OSError:
            native_cleanup = {'outcome':'inconclusive','delivery_unknown':True,'detail':'native cleanup deadline/transport inconclusive'}
        if native_cleanup.get('state')=='not_written' and command.get('options'):
            self.store.receipt(generation,owner,shell,control_id,native_cleanup)
            self.release_wake_close(generation,cid,owner,shell,control_id,client.lease['fence'])
            return
        # Record ambiguity before any OS fallback. Never rerun an unknown
        # control under a fresh identity after API death or a lost reply.
        self.store.receipt(generation,owner,shell,control_id,{'state':'unknown' if native_cleanup.get('delivery_unknown') else 'written','native_cleanup':native_cleanup})
        try:
            client.subscribe(self.store,owner,shell,project=project_event)
        except (OSError,RuntimeContractError):
            pass
        retained = self.store.command_status(generation,owner,shell,control_id)['receipt']
        if retained.get('state')=='terminal' and retained.get('outcome'):
            native_cleanup = retained
        self.finish_cleanup(generation,cid,owner,shell,native_cleanup)

    def release_wake_close(self,generation: str,cid: str,owner: int,shell: int,control_id: str,fence: int) -> None:
        retained=db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(retained,'native_chat.wake_close_no_write'):
                current=retained.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=? AND owner_user_id=? AND shell_id=?',(cid,owner,shell)).fetchone()
                runtime=json.loads(current[0]) if current else {}
                owned=retained.execute('SELECT * FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=? AND owner_user_id=? AND shell_id=?',(generation,cid,owner,shell)).fetchone()
                command=retained.execute('SELECT state,intent_json,receipt_json FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?',(generation,control_id)).fetchone()
                if (owned and command and command['state']=='not_written'
                        and json.loads(command['intent_json']).get('options')
                        and runtime.get('generation_id')==generation and runtime.get('close_control_id')==control_id
                        and owned['consumer_fence']==fence and owned['consumer_id']==self.consumer
                        and owned['consumer_expires']>time.time() and owned['state']=='closing'):
                    state='ready' if json.loads(command['receipt_json']).get('runtime_ready') is True else 'lost'
                    runtime.update(state=state)
                    runtime.pop('close_control_id',None)
                    runtime.pop('wake_rotation',None)
                    retained.execute("UPDATE conversation_runtime_generations SET close_intent=0,state=? WHERE generation_id=?",(state,generation))
                    retained.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(runtime),cid))
                    append_event(retained,cid,'control.acknowledged',{'generation_id':generation,'control_id':control_id,'state':'not_written','detail':'wake quiet condition changed'})
        finally:
            retained.close()
        conversation_events.notify(cid)

    def recover_close(self,generation: str,cid: str,owner: int,shell: int) -> None:
        """Recovery never invents/replays native writes after ambiguous delivery.

        A persisted native receipt can finish independent verified OS cleanup;
        absent native proof remains pending even when the unit is now gone.
        """
        try:
            retained = self.store.command_status(generation,owner,shell,self.close_id(generation,cid))['receipt']
        except RuntimeContractError:
            retained = {}
        native_cleanup = retained if retained.get('state')=='terminal' else retained.get('native_cleanup',{})
        self.finish_cleanup(generation,cid,owner,shell,native_cleanup)

    def finish_cleanup(self,generation: str,cid: str,owner: int,shell: int,native_cleanup: dict) -> None:
        retained = self.store.status(generation,owner,shell)
        if retained['consumer_id'] not in {None,self.consumer} and retained['consumer_expires']>time.time():
            raise RuntimeContractError('LEASE_FENCED','a replacement consumer owns cleanup')
        fence = retained['consumer_fence']
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
        self._commit_cleanup(generation,cid,owner,shell,cleanup,expected_fence=fence)

    def _commit_cleanup(self,generation: str,cid: str,owner: int,shell: int,cleanup: dict, *, expected_fence: int | None = None) -> None:
        # Generation truth and chat finalization share a transaction. API
        # interruption can leave both pending, never a released generation
        # whose chat cannot be selected for completion after restart.
        complete = bool(cleanup.get('outcome')=='complete' and cleanup.get('unit_verified_exited') is True
                        and not cleanup.get('unresolved_work') and not cleanup.get('unresolved_definitions'))
        con = db_driver.connect(str(self.database))
        try:
            with db_driver.write_transaction(con,'native_chat.cleanup_projection'):
                owned = con.execute('SELECT * FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=? AND owner_user_id=? AND shell_id=?',(generation,cid,owner,shell)).fetchone()
                chat = con.execute('SELECT runtime_projection FROM conversations WHERE conversation_id=? AND owner_user_id=?',(cid,owner)).fetchone()
                if owned is None or chat is None:
                    raise RuntimeContractError('RUNTIME_NOT_OWNED','cleanup is outside operator tenancy')
                if expected_fence is not None and owned['consumer_fence']!=expected_fence:
                    raise RuntimeContractError('LEASE_FENCED','cleanup consumer changed before finalization')
                previous = json.loads(owned['cleanup_json'])
                if previous.get('outcome')=='complete' and previous.get('unit_verified_exited') is True and not previous.get('unresolved_work') and not previous.get('unresolved_definitions'):
                    # Verified terminal cleanup cannot regress when an older
                    # API worker later returns an ambiguous/pending result.
                    cleanup,complete = previous,True
                projection = json.loads(chat['runtime_projection'])
                if projection.get('generation_id')!=generation:
                    raise RuntimeContractError('GENERATION_INVALID','cleanup is outside the captured chat generation')
                cleanup = public_payload(cleanup,sensitive_values=self.store.secrets)
                con.execute('UPDATE conversation_runtime_generations SET state=?,cleanup_json=?,updated_at=? WHERE generation_id=?',('closed' if complete else 'lost',encoded(cleanup),time.time(),generation))
                projection.update(state='closed' if complete else 'lost',cleanup=cleanup,setup=None,primary=None)
                con.execute('UPDATE conversations SET runtime_projection=?,version=version+1 WHERE conversation_id=?',(encoded(projection),cid))
                append_event(con,cid,'control.outcome',{'generation_id':generation,'control_id':projection.get('close_control_id') or ('probe-close' if projection.get('role')=='probe' else 'close:'+generation),**cleanup})
                if complete:
                    self._finish_chat(con,cid)
        finally:
            con.close()
        conversation_events.notify(cid)

    def shutdown(self) -> None:
        self.stopped.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(6)
        self.starts.shutdown(wait=False,cancel_futures=True)
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
            self.schedule_starts()
            con = db_driver.connect(str(self.database))
            try:
                provisional=con.execute("SELECT c.conversation_id,j.generation_id FROM conversation_runtime_probe_jobs j JOIN conversations c USING(conversation_id) WHERE j.status!='complete' AND c.runtime_mode='native_experiment' AND json_extract(c.runtime_projection,'$.role')='probe'").fetchall()
                rows = con.execute("SELECT g.generation_id,g.conversation_id,g.state,g.cleanup_json,g.owner_user_id,g.shell_id,c.runtime_projection FROM conversation_runtime_generations g JOIN conversations c USING(conversation_id) WHERE c.runtime_mode='native_experiment' AND (g.state!='closed' OR c.state!='closed') ORDER BY g.created_at LIMIT 4").fetchall()
            finally:
                con.close()
            if self.probe_delegate:
                for row in provisional:
                    try:
                        self.probe_delegate(row['conversation_id'],row['generation_id'])
                    except (RuntimeContractError,OSError):
                        pass # Retain ownership when reconciliation is unavailable.
            for row in rows:
                if self.stopped.is_set():
                    break
                try:
                    if (json.loads(row['runtime_projection']).get('role')=='probe' and self.probe_delegate
                            and self.probe_delegate(row['conversation_id'],row['generation_id'])):
                        continue # One factory owns its continuous reader/ack.
                    if row['state']=='closed':
                        # Retained verified evidence also repairs a chat from
                        # older interrupted finalization; no controller attach
                        # or native write is needed for an exited generation.
                        cleanup = json.loads(row['cleanup_json'])
                        if cleanup.get('outcome')=='complete' and cleanup.get('unit_verified_exited') is True and not cleanup.get('unresolved_work') and not cleanup.get('unresolved_definitions'):
                            self._commit_cleanup(row['generation_id'],row['conversation_id'],row['owner_user_id'],row['shell_id'],cleanup)
                        continue
                    client,owner,shell = self.attach(row['generation_id'])
                    generation_state = self.store.status(row['generation_id'],owner,shell)
                    if generation_state['close_intent']:
                        command_state = self.store.command_status(row['generation_id'],owner,shell,self.close_id(row['generation_id'],row['conversation_id']))['state']
                        if command_state=='not_written':
                            self.release_wake_close(row['generation_id'],row['conversation_id'],owner,shell,self.close_id(row['generation_id'],row['conversation_id']),client.lease['fence'])
                        elif command_state=='accepted':
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
                    else:
                        self.dispatch_queued(row['generation_id'],row['conversation_id'],client,owner,shell)
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


def projection(conversation: Any, *, con=None) -> dict | None:
    if conversation['runtime_mode']!='native_experiment':
        return None
    runtime=public_payload(json.loads(conversation['runtime_projection']))
    runtime.update(work=[],controls=[],activity=[],work_partial=False,controls_partial=False,activity_partial=False)
    if con is None or not runtime.get('generation_id'):
        return runtime
    generation=runtime['generation_id']
    owned=con.execute('SELECT 1 FROM conversation_runtime_generations WHERE generation_id=? AND conversation_id=? AND owner_user_id=? AND shell_id=?',
                      (generation,conversation['conversation_id'],conversation['owner_user_id'],conversation['shell_id'])).fetchone()
    if owned is None:
        return runtime # Provisional preparation has no native work to expose.
    work=con.execute('SELECT work_key,projection_json FROM conversation_runtime_work WHERE generation_id=? ORDER BY last_sequence DESC LIMIT 257',(generation,)).fetchall()
    runtime['work']=[{'work_key':row['work_key'],**json.loads(row['projection_json'])} for row in work[:256]]
    runtime['work_partial']=len(work)>256
    controls=con.execute("SELECT command_id,state,intent_json,receipt_json FROM conversation_runtime_commands WHERE generation_id=? AND kind='control' ORDER BY command_sequence DESC LIMIT 65",(generation,)).fetchall()
    runtime['controls']=[{'control_id':row['command_id'],'state':row['state'],'action':json.loads(row['intent_json']).get('action'),
                         'receipt':json.loads(row['receipt_json'])} for row in reversed(controls[:64])]
    runtime['controls_partial']=len(controls)>64
    events=con.execute('SELECT sequence,event_json FROM conversation_runtime_events WHERE generation_id=? ORDER BY sequence DESC LIMIT 129',(generation,)).fetchall()
    for row in reversed(events[:128]):
        event=json.loads(row['event_json'])
        request=event.get('request_id')
        intent=con.execute("SELECT intent_json FROM conversation_runtime_commands WHERE generation_id=? AND command_id=? AND kind='submit'",(generation,request)).fetchone() if request else None
        engine=json.loads(intent[0]) if intent else {}
        runtime['activity'].append({'generation_id':generation,'controller_sequence':row['sequence'],
            'engine_run_id':engine.get('run_id'),'engine_message_id':engine.get('message_id'),
            'engine_output_key':output_key(event,conversation['harness']),
            'engine_mirrored':mirrored_output(con,conversation['conversation_id'],engine.get('run_id'),event,conversation['harness']),**event})
    runtime['activity_partial']=len(events)>128
    if _SERVICE is not None:
        runtime['onboarding']={'canonical_main_root':str(_SERVICE.root.resolve()),
                               'worktree':conversation['worktree'],'scope':'linked_worktrees',
                               'initial_setup':'native_tui','local_channel_setup':'scoped_gui_action'}
    return public_payload(runtime)
