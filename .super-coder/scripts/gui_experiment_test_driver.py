"""Finite fixture-only native-like pipe transport, never a harness replacement.

Used to prove controller/API independence and scoped unit cleanup. No login,
provider inference, scheduling or native compatibility claim is possible.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from conversation_runtime_contract import (
    DriverStart,
    NativeCleanup,
    NativeReference,
    NativeSnapshot,
    ProcessIdentity,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    WriteReceipt,
)
from conversation_runtime_controller import start_ticks

_CHILD="""
import json,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
for line in sys.stdin:
 value=json.loads(line)
 for kind in ['activity.started','activity.processed','output.final','activity.terminal']:
  print(json.dumps({'kind':kind,'request_id':value['request_id'],'activity_id':'fixture-'+value['request_id'],'text':value['text']}),flush=True)
while True:time.sleep(1)
"""


class TestDriver(RuntimeDriver):
    harness='codex'
    revision='fixture-pipe-v1'
    def __init__(self):
        self.process: subprocess.Popen | None=None
        self.root=''
        self.closing=False
        self.lock=threading.Lock()
        self.identity: RuntimeIdentity | None=None
    def start(self,context,emit,*,deadline):
        # The controller CLI test option is itself restricted to a marked
        # fixture. The context must stay under that same retained root.
        fixture_root=context.state_root.parent.parent
        if not (fixture_root/'.gui-experiment-owner.json').is_file() or fixture_root not in context.worktree.parents:
            return DriverStart('unavailable',detail='test transport requires marked fixture worktree')
        self.root='fixture-'+context.generation_id
        self.process=subprocess.Popen([sys.executable,'-u','-c',_CHILD],cwd=context.worktree,
                         env=context.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL,text=True,start_new_session=True)
        self.identity=RuntimeIdentity(self.root,process=ProcessIdentity(self.process.pid,start_ticks(self.process.pid)))
        process=self.process
        def reader():
            assert process.stdout
            for line in process.stdout:
                value=json.loads(line)
                emit(RuntimeEvent(value['kind'],NativeReference(self.root,thread_id=self.root,activity_id=value['activity_id']),
                      request_id=value['request_id'],provenance='fixture-test-transport',
                      data={'text':value['text']} if value['kind']=='output.final' else {}))
        threading.Thread(target=reader,daemon=True).start()
        return DriverStart('ready',self.identity,capabilities={'submission':'compatible'})
    def submit(self,command,*,deadline):
        with self.lock:
            if self.closing or not self.process or not self.process.stdin:
                return WriteReceipt('not_written')
            self.process.stdin.write(json.dumps({'request_id':command.request_id,'text':command.text})+'\n')
            self.process.stdin.flush()
            return WriteReceipt('written',True)
    def inventory(self,*,deadline):
        return NativeSnapshot(self.identity,None,partial=True,provenance='fixture-test-transport')
    def control(self,command,*,deadline):
        return WriteReceipt('unsupported',detail='test transport proves no native controls')
    def cleanup(self,*,deadline):
        with self.lock:
            self.closing=True
            process=self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(.01,min(.2,deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                return NativeCleanup('inconclusive',detail='resistant fixture descendant requires owned unit backstop')
        return NativeCleanup('complete')


def create_driver() -> RuntimeDriver:
    return TestDriver()
