"""Maintainer fixture-only canonical boot/CLI/MCP readiness, never native launch.

Imported only by the marked disposable bootstrap. The fixture MCP transport
exercises canonical injection/auth/tenancy; it provides no browser capability.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import db_driver
import run
from conversation_boot import BootDirective, content_digest

_LOCK=threading.Lock()


def prepared_plan(*, database: Path, root: Path, conversation_id: str,
                  shell_id: int, harness: str) -> run.LaunchPlan:
    """Retrieve an existing synthetic conversation's canonical resume plan.

    Call inside a fixture-isolated process after selecting the copied engine
    and synthetic DB. The returned env/boot remain private ephemeral launch
    inputs; neither belongs in public evidence. This does not launch a driver
    or grant native startup consent or capability evidence.
    """
    if (harness not in {'codex','claude'} or shell_id not in {1,2}
            or not conversation_id.startswith('cv_fixture_')
            or run.ENGINE.resolve()!=root.resolve()/'.super-coder'
            or database.resolve()!=root.resolve()/'.super-coder/shell_db.db'
            or Path(run.DB_PATH).resolve()!=database.resolve()):
        raise ValueError('canonical launcher is outside the synthetic fixture')
    con=db_driver.connect(str(database))
    try:
        row=con.execute('SELECT c.*,s.user_id AS shell_owner FROM conversations c JOIN shells s ON s.shell_id=c.shell_id WHERE c.conversation_id=?',
                        (conversation_id,)).fetchone()
    finally:
        con.close()
    if (row is None or row['shell_id']!=shell_id or row['owner_user_id']!=1
            or row['shell_owner']!=1 or row['harness']!=harness or row['state']=='closed'
            or row['provider']!=run.session_provider(harness,row['model'])):
        raise ValueError('synthetic conversation route or ownership differs')
    worktree=Path(row['worktree']).resolve()
    if root.resolve() not in worktree.parents:
        raise ValueError('synthetic conversation worktree escaped fixture')
    plan=run.prepare_launch(shell_id=shell_id,harness=harness,model=row['model'],effort=row['effort'],
                            headless_prompt='fixture readiness; never dispatch',conversation_owned=True,
                            boot=BootDirective(conversation_id,'resume'))
    if (plan.argv or Path(plan.cwd).resolve()!=worktree or plan.harness!=harness
            or plan.model!=row['model'] or plan.effort!=row['effort']
            or plan.env.get('SC_SHELL_ID')!=str(shell_id)):
        raise ValueError('canonical preparation differs from synthetic binding')
    return plan


def prepare(*,database: Path,root: Path,fixture_id: str,harness: str,shell_id: int | None=None) -> dict:
    shell_id = shell_id if shell_id is not None else (1 if harness=='codex' else 2)
    if harness not in {'codex','claude'} or shell_id not in {1,2}:
        raise ValueError('synthetic harness/shell required')
    if run.ENGINE.resolve()!=root/'.super-coder' or Path(run.DB_PATH).resolve()!=database.resolve():
        raise ValueError('canonical launcher is outside the synthetic fixture')
    with _LOCK:
        cid='cv_fixture_'+uuid.uuid4().hex
        con=db_driver.connect(str(database))
        try:
            shell=con.execute('SELECT shortname,user_id FROM shells WHERE shell_id=?',(shell_id,)).fetchone()
            if shell is None or shell['user_id']!=1:
                raise ValueError('fixture operator does not own shell')
            worktree=run.shell_work_dir(shell['shortname'],'dev')
            model='gpt-6.1' if harness=='codex' else 'claude-sonnet-4-6'
            effort='high'
            provider=run.session_provider(harness,model)
            con.execute('INSERT INTO conversations(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,worktree,creation_idempotency_key,creation_request_hash) VALUES(?,?,1,?,?,?,?,?,?,?)',
                        (cid,shell_id,harness,provider,model,effort,str(worktree),cid,hashlib.sha256(cid.encode()).hexdigest()))
            con.commit()
        finally:
            con.close()
        plan=run.prepare_launch(shell_id=shell_id,harness=harness,model=model,effort=effort,
                                headless_prompt='fixture readiness; never dispatch',conversation_owned=True,
                                boot=BootDirective(cid,'start'))
        first_digest=content_digest(plan.boot_content)
        resumed=prepared_plan(database=database,root=root,conversation_id=cid,shell_id=shell_id,harness=harness)
        if content_digest(resumed.boot_content)!=first_digest or plan.argv or resumed.argv:
            raise ValueError('conversation boot binding or native-owned preparation changed')
        if plan.env.get('SC_API_BASE')!=f'http://127.0.0.1:{run.ports_mod.resolve()["port"]}' or plan.env.get('SC_SHELL_ID')!=str(shell_id):
            raise ValueError('prepared CLI identity differs')
        selected=Path(plan.cwd).resolve()
        if selected!=worktree.resolve() or root.resolve() not in selected.parents:
            raise ValueError('prepared worktree escaped synthetic root')
        for name in ('CLAUDE.md','AGENTS.md'):
            if (selected/name).read_text()!=plan.boot_content:
                raise ValueError('canonical boot bytes not retained in discovery files')
        if 'NEVER use the harness' not in plan.boot_content:
            raise ValueError('no-memory boot instruction missing')
        commands=[]
        for args in (['mem','which'],['mem','state',f'fixture-readiness-{fixture_id}-{harness}']):
            result=subprocess.run(plan.execution_view.command([str(selected/'sc'),*args]),
                                  cwd=selected,env=plan.env,timeout=15,capture_output=True,text=True,check=False)
            if result.returncode:
                raise ValueError('copied CLI/execution view readiness failed')
            if plan.env['SC_API_TOKEN'] in result.stdout+result.stderr:
                raise ValueError('CLI output exposed synthetic credential')
            commands.append({'command':args[:2],'exit':result.returncode})
        con=db_driver.connect(str(database))
        try:
            state=con.execute('SELECT current_state FROM shells WHERE shell_id=?',(shell_id,)).fetchone()[0]
            snapshot=con.execute('SELECT content_sha256 FROM conversation_boot_snapshots WHERE conversation_id=?',(cid,)).fetchone()[0]
        finally:
            con.close()
        if state!=f'fixture-readiness-{fixture_id}-{harness}' or snapshot!=first_digest:
            raise ValueError('CLI write or canonical boot snapshot hit wrong state')
        adapter=run.load_adapter(harness)
        mcp=run.managed_mcp_injection(adapter,shell['shortname'])
        if not mcp or mcp['name']!='browser' or mcp['url']!=f'{plan.env["SC_API_BASE"]}/mcp/{shell["shortname"].upper()}':
            raise ValueError('canonical MCP recipe is not fixture-routed')
        # Native memory/credentials/login/channel consent are separate native
        # driver acceptance. This lane proves preparation without inference.
        report={'fixture_id':fixture_id,'conversation_id':cid,'harness':harness,'shell_id':shell_id,
                'worktree':str(selected),'shell_branch':subprocess.check_output(['git','-C',str(selected),'branch','--show-current'],text=True).strip(),
                'boot_sha256':first_digest,'snapshot_sha256':snapshot,'boot_discovery_files_match':True,
                'no_memory_boot_instruction':True,'model':plan.model,'effort':plan.effort,'provider':provider,
                'execution_view':plan.execution_view.mode,'policy_source':'canonical adapter unrestricted',
                'api_base':plan.env['SC_API_BASE'],'engine_token_fixture_row_verified':True,
                'cli':commands,'mcp_transport':'fixture-test-only','managed_mcp':mcp,
                'native_launch_proven':False,'native_memory_config_proven':False,'browser_capability_proven':False}
        # No context/env/boot bytes written to a public readiness receipt.
        (root/'readiness').mkdir(mode=0o700,exist_ok=True)
        path=root/'readiness'/f'{harness}-{shell_id}.json'
        path.write_text(json.dumps(report,indent=2));path.chmod(0o600)
        return report


def mcp_response(*,method: str,path: str,body: bytes,database: Path,fixture_id: str,dispatch) -> tuple:
    """Stateless MCP JSON-RPC fixture test transport on the already-owned API."""
    headers=[('Content-Type','application/json')]
    if method!='POST':
        return 405,headers,b'{}'
    value=json.loads(body)
    mid=value.get('id')
    operation=value.get('method')
    short=path.removeprefix('/mcp/')
    con=db_driver.connect(str(database))
    try:
        row=con.execute('SELECT shell_id,api_key FROM shells WHERE UPPER(shortname)=? AND user_id=1',(short,)).fetchone()
    finally:
        con.close()
    if row is None:
        return 403,headers,b'{}'
    if operation=='initialize':
        result: dict[str,Any]={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'subfloor-fixture-test-transport','version':'1'}}
    elif operation=='notifications/initialized':
        return 202,headers,b''
    elif operation=='tools/list':
        result={'tools':[{'name':'fixture_identity','description':'Synthetic control-plane identity only','inputSchema':{'type':'object','properties':{}}},
                         {'name':'fixture_state','description':'Write a bounded marker to this synthetic shell only','inputSchema':{'type':'object','properties':{'marker':{'type':'string','maxLength':200}},'required':['marker']}}]}
    elif operation=='tools/call':
        params=value.get('params',{})
        name=params.get('name')
        if name not in {'fixture_identity','fixture_state'}:
            raise ValueError('unsupported fixture MCP tool')
        args=params.get('arguments',{})
        marker=args.get('marker','')
        if name=='fixture_state' and (not isinstance(marker,str) or not 1<=len(marker)<=200):
            raise ValueError('bounded marker required')
        api_path='/_sc/mem/whoami' if name=='fixture_identity' else '/_sc/mem/state'
        api_body=b'' if name=='fixture_identity' else json.dumps({'body':marker}).encode()
        status,_,raw=dispatch('GET' if name=='fixture_identity' else 'POST',api_path,
                              'Authorization: Bearer '+row['api_key']+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(api_body))+'\r\n',
                              api_body)
        if status!=200:
            raise ValueError('synthetic MCP auth routing refused')
        actual=json.loads(raw)
        if name=='fixture_identity' and actual.get('shell_id')!=row['shell_id']:
            raise ValueError('MCP identity differs from synthetic shell')
        if name=='fixture_state':
            con=db_driver.connect(str(database))
            try:
                observed=con.execute('SELECT current_state FROM shells WHERE shell_id=?',(row['shell_id'],)).fetchone()[0]
            finally:
                con.close()
            if observed!=marker:
                raise ValueError('MCP write did not reach the synthetic row')
        result={'content':[{'type':'text','text':json.dumps({'fixture_id':fixture_id,'shell_id':row['shell_id'],'ok':True})}]}
    else:
        return 200,headers,json.dumps({'jsonrpc':'2.0','id':mid,'error':{'code':-32601,'message':'fixture method unavailable'}}).encode()
    return 200,headers,json.dumps({'jsonrpc':'2.0','id':mid,'result':result}).encode()
