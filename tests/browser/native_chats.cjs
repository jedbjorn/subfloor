// Fresh Chromium, actual app.js, fixed synthetic API projections. No harness.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(process.env.SC_PLAYWRIGHT_MODULE || 'playwright');
const root = process.argv[2];
const ui = path.join(root, '.super-coder/ui');
const cid = 'cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const probeId = 'cv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const cv = {conversation_id:cid,state:'idle',version:7,title:'Native UI source fixture',scope:'interactive',
  shell:{shell_id:1,shortname:'ui',display_name:'UI'},route:{harness:'claude',model:'selected-model',effort:'high'},
  runtime_mode:'native_experiment',created_at:'2026-10-02 10:00:00',close_requested_at:null,
  runtime:{generation_id:'root-gen',root_id:'root',state:'ready',partial:false,freshness:'current',observed_at:1790935200,
    primary:{root_id:'root',activity_id:'turn-A'},capabilities:{submission:'compatible',stop_reply:'compatible',stop_work:'compatible',stop_work_terminal:'compatible'},
    onboarding:{canonical_main_root:'/synthetic/main',scope:'linked_worktrees'},controls:[],
    work:[{work_key:'stored-terminal-key',kind:'work.observed',partial:false,freshness:'last_observed',observed_at:1790935200,grade:'compatible',provenance:'claude:Stop-snapshot',
      reference:{root_id:'root',work_id:'opaque-task',activity_id:'observed-turn'},data:{kind:'terminal',state:'running',snapshot_complete:true}},
      {work_key:'unknown-key',kind:'work.observed',partial:false,freshness:'current',data:{kind:'task'}},
      {work_key:'partial-key',kind:'work.observed',partial:true,freshness:'current',data:{kind:'terminal'}}],
    activity:[{generation_id:'root-gen',controller_sequence:8,kind:'output.final',source:'native_completion',freshness:'current',partial:false,
      observed_at:1790935200,data:{text:'Autonomous native completion'}},
      {generation_id:'root-gen',controller_sequence:10,kind:'output.final',source:'system',freshness:'current',partial:false,data:{text:'Setup nonce belongs to system activity'}},
      {generation_id:'root-gen',controller_sequence:12,engine_run_id:1,engine_mirrored:false,kind:'output.final',reference:{root_id:'root',thread_id:'child',item_id:'child-output'},data:{kind:'assistant',text:'Child output with engine run survives'}},
      {generation_id:'root-gen',controller_sequence:13,engine_run_id:1,engine_mirrored:false,kind:'output.final',reference:{root_id:'root',thread_id:'root',item_id:'command-output'},data:{kind:'terminal',text:'Terminal output with engine run survives'}},
      ...[16,17].map(controller_sequence=>({generation_id:'root-gen',controller_sequence,engine_run_id:1,engine_output_key:'stored-itemless-final',engine_mirrored:false,kind:'output.final',freshness:'current',partial:false,reference:{root_id:'root',activity_id:'turn-A'},data:{text:'Itemless final appears once'}})),
      {generation_id:'old-gen',controller_sequence:11,kind:'output.final',source:'native_completion',data:{text:'Foreign generation must not display'}},
      {generation_id:'root-gen',controller_sequence:9,engine_run_id:1,engine_mirrored:true,kind:'output.final',reference:{root_id:'root',thread_id:'root',item_id:'mirrored'},data:{text:'Legacy duplicate must not display'}}]}};
const probe = {...cv,conversation_id:probeId,runtime:{...cv.runtime,generation_id:'probe-gen',role:'probe',state:'needs_consent',primary:null,
  setup:{generation_id:'probe-gen',setup_id:'stored-setup',phase:'local_channel_development_consent'},work:[],activity:[]}};
const check = {check_id:'nc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',state:'running',selection:{harness:'claude',model:'selected-model',effort:'high'},admissible:false,grades:{submission:'inconclusive'},probe:{conversation_id:probeId,generation_id:'probe-gen'}};
const requests = [], errors = [];
let checkPosts = 0, createPosts = 0, controlPosts = 0, messagePosts = 0, enabled = true, message = null, messageReadback = false, creationKey = null;
const server = http.createServer((req,res) => {
  const file = req.url === '/' ? 'index.html' : req.url.split('?')[0].slice(1);
  if (!['index.html','app.js','style.css','vendor/marked.umd.js','vendor/purify.min.js'].includes(file)) {res.writeHead(404);res.end();return;}
  res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');
  res.end(fs.readFileSync(path.join(ui,file)));
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1200,height:900}});
    page.setDefaultTimeout(7000);
    page.on('pageerror',e=>errors.push(String(e)));
    await page.addInitScript(() => {
      class Source {
        constructor(url) {this.url=url;this.listeners={};(window.sources ||= []).push(this);queueMicrotask(()=>this.onopen?.());}
        addEventListener(type,callback) {(this.listeners[type] ||= []).push(callback);}
        close() {this.closed=true;}
      }
      window.EventSource=Source;
      window.emitNative=(type,payload) => {for(const source of window.sources || []) if(!source.closed) for(const cb of source.listeners[type] || []) cb({data:JSON.stringify(payload)});};
    });
    const fulfill = (route,value,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    await page.route('**/api/**',async route => {
      const req=route.request(), url=new URL(req.url()), p=url.pathname, method=req.method();
      const body=req.postDataJSON(); requests.push({p,method,body,key:req.headers()['idempotency-key']});
      if(p==='/api/health') return fulfill(route,{repo:'source-fixture'});
      if(p==='/api/shells') return fulfill(route,{repo_root:'/synthetic/main',shells:[{...cv.shell,flavor:'dev'}]});
      if(p==='/api/flavor-defaults') return fulfill(route,{flavors:{dev:[{harness:'claude',model:'selected-model',effort:'high',is_default:true}]},harness_status:{claude:{installed:true,enabled:true,healthy:true,surfaces:{browser:true}}}});
      if(p==='/api/models') return fulfill(route,{harnesses:{claude:{models:[{id:'selected-model',name:'Server model',efforts:['high']}]}}});
      if(p==='/api/conversations/native-config') return fulfill(route,{enabled,candidates:[{harness:'claude',model:'selected-model',effort:'high',label:'Requested native route',proof_state:'unverified'}],onboarding:{canonical_main_root:'/synthetic/main'}});
      if(p==='/api/conversations/native-checks' && method==='POST') {checkPosts++;return route.abort('failed');}
      if(p.startsWith('/api/conversations/native-checks')) return fulfill(route,check);
      if(p==='/api/conversations') {
        if(method==='POST') {creationKey=req.headers()['idempotency-key'];createPosts++;assert.equal(body.runtime_mode,'native_experiment');assert.equal(body.model,'selected-model');cv.state='idle';cv.close_requested_at=null;cv.version++;cv.runtime={...cv.runtime,generation_id:'new-chat-gen',state:'needs_consent',primary:null,controls:[],work:[],activity:[],cleanup:null,setup:{generation_id:'new-chat-gen',setup_id:'new-chat-setup',phase:'local_channel_development_consent'}};return route.abort('failed');}
        if(url.searchParams.has('request_key')) return fulfill(route,{items:url.searchParams.get('request_key')===creationKey?[{...cv,request_key:creationKey}]:[],next_cursor:null});
        return fulfill(route,{items:url.searchParams.get('starred')==='true'?[]:[cv,probe],next_cursor:null});
      }
      const chat=p.includes(probeId)?probe:cv;
      if(p.endsWith('/runtime-controls')) {
        controlPosts++;
        if(body.action==='enable_local_channel') {
          assert.equal(body.generation_id,'probe-gen');assert.equal(body.setup_id,'stored-setup');
          probe.runtime.state='ready';probe.runtime.setup=null;return fulfill(route,{control_id:'enable1',state:'written'});
        }
        assert.equal(body.work_key,'stored-terminal-key');
        cv.runtime.controls=[{control_id:'control1',state:'written',action:'stop_work',receipt:{state:'written'}}];
        return route.abort('failed'); // The server recorded it, but the caller lost the reply.
      }
      if(p.endsWith('/transcript')) return fulfill(route,{projection_version:3,conversation_id:chat.conversation_id,items:[],through_sequence:0,
        controls:{conversation_version:chat.version,conversation_state:chat.state,active_run_id:null,close_requested_at:chat.close_requested_at},assistant_cursor:null,older_cursor:null});
      if(p.endsWith('/messages')) {
        if(method==='POST') {messagePosts++;message={message_id:10,conversation_id:cid,request_key:req.headers()['idempotency-key'],body:body.text,state:'queued',message_kind:'prompt'};return route.abort('failed');}
        return fulfill(route,{items:messageReadback && url.searchParams.get('request_key')===message?.request_key?[message]:[]});
      }
      if(p===`/api/conversations/${cid}` || p===`/api/conversations/${probeId}`) {
        if(method==='PATCH' && body.state==='closed') {
          chat.state='closed';chat.close_requested_at=true;chat.runtime.state='closed';chat.runtime.cleanup={outcome:'complete',unit_verified_exited:true,unresolved_work:[],unresolved_definitions:[]};
          if(chat===probe) {check.state='complete';check.admissible=true;check.grades.submission='compatible';}
        }
        return fulfill(route,chat);
      }
      return fulfill(route,{});
    });
    const base=`http://127.0.0.1:${server.address().port}`;
    await page.goto(`${base}/#interface/ui/${cid}`,{waitUntil:'networkidle'});
    await page.getByLabel('Native runtime').waitFor();
    assert.match(await page.locator('.chat-native-panel').innerText(),/Autonomous native completion/);
    assert.doesNotMatch(await page.locator('.chat-native-panel').innerText(),/Legacy duplicate|Foreign generation/);
    assert.match(await page.locator('.chat-native-activity-item').filter({hasText:'Setup nonce'}).innerText(),/System \/ setup/);
    assert.equal(await page.locator('.chat-native-output').filter({hasText:'Itemless final appears once'}).count(),1);
    assert.match(await page.locator('.chat-native-panel').innerText(),/Child output with engine run survives/);
    assert.match(await page.locator('.chat-native-panel').innerText(),/Terminal output with engine run survives/);
    cv.runtime.activity.push({generation_id:'root-gen',controller_sequence:14,engine_run_id:1,engine_mirrored:false,
      kind:'output.delta',source:'gui',reference:{root_id:'root',thread_id:'root',item_id:'streamed'},data:{kind:'assistant',text:'Native streaming before final'}});
    await page.evaluate(()=>window.emitNative('output.delta',{sequence:1,event_type:'output.delta',payload:{generation_id:'root-gen',controller_sequence:14}}));
    await page.locator('.chat-native-output').filter({hasText:'Native streaming before final'}).waitFor();
    cv.runtime.activity.push({generation_id:'root-gen',controller_sequence:15,engine_run_id:1,engine_mirrored:true,
      kind:'output.final',source:'gui',reference:{root_id:'root',thread_id:'root',item_id:'streamed'},data:{kind:'assistant',text:'Final now mirrored to legacy'}});
    await page.evaluate(()=>window.emitNative('output.final',{sequence:2,event_type:'output.final',payload:{generation_id:'root-gen',controller_sequence:15}}));
    await page.waitForFunction(()=>!document.querySelector('.chat-native-panel').textContent.includes('Native streaming before final'));
    assert.doesNotMatch(await page.locator('.chat-native-panel').innerText(),/Final now mirrored to legacy/);
    const bounds=await page.evaluate(()=>{const panel=document.querySelector('.chat-native-host'),composer=document.querySelector('.chat-composer');return {panel:panel.getBoundingClientRect().height,composer:composer.getBoundingClientRect().bottom,height:innerHeight};});
    assert.ok(bounds.panel<=321);assert.ok(bounds.composer<=bounds.height);
    await page.setViewportSize({width:390,height:780});
    assert.ok(await page.evaluate(()=>document.querySelector('.chat-composer').getBoundingClientRect().bottom<=innerHeight));
    await page.setViewportSize({width:1200,height:900});
    const stops=page.getByRole('button',{name:'Request stop',exact:true});
    assert.equal(await stops.count(),3);assert.equal(await stops.nth(1).isDisabled(),true);assert.equal(await stops.nth(2).isDisabled(),true);
    await stops.nth(0).click();await page.getByText('Outcome unknown. Refresh evidence or Close.').waitFor();
    assert.equal(controlPosts,1);assert.equal(await page.getByRole('button',{name:'Close',exact:true}).isEnabled(),true);
    await page.reload({waitUntil:'networkidle'});
    assert.equal(await page.getByRole('button',{name:'Request stop',exact:true}).nth(0).isDisabled(),true);assert.equal(controlPosts,1);
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.getByText('Cleanup: complete · owned unit exit verified').waitFor();
    assert.equal(await page.locator('.chat-composer-input').isDisabled(),true);
    await page.evaluate(()=>{location.hash='#interface/ui/configure';});
    await page.getByLabel('Keep native runtime open (experiment)').waitFor();
    assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isChecked(),false);
    await page.getByLabel('Keep native runtime open (experiment)').check();
    assert.equal(await page.getByRole('button',{name:'Start native chat'}).isDisabled(),true);
    await page.getByRole('button',{name:'Check native compatibility'}).click();
    await page.getByText(/Use Refresh check; this check was not replayed/).waitFor();
    await page.getByRole('button',{name:'Refresh check'}).click();
    await page.getByRole('button',{name:'Open finite probe setup / Close'}).click();
    await page.getByRole('button',{name:'Enable local channel'}).waitFor();
    assert.equal(await page.locator('.chat-composer-input').isDisabled(),true);
    assert.equal(controlPosts,1); // No automatic consent.
    await page.getByRole('button',{name:'Enable local channel'}).click();
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.getByText('Cleanup: complete · owned unit exit verified').waitFor();
    await page.evaluate(()=>{location.hash='#interface/ui/configure';});
    await page.getByLabel('Keep native runtime open (experiment)').check();
    await page.getByRole('button',{name:'Start native chat'}).waitFor();
    await page.waitForFunction(()=>!document.querySelector('.chat-native-check .primary').disabled);
    assert.equal(checkPosts,1); // Reentry used GET check_id.
    await page.getByRole('button',{name:'Start native chat'}).click();
    await page.getByText('Chat creation outcome unknown. Inspect shell history; this creation was not replayed.').waitFor();
    assert.equal(createPosts,1);
    await page.getByRole('button',{name:'Refresh check'}).click();
    await page.getByRole('button',{name:'Open created chat / Close'}).click();
    await page.getByRole('button',{name:'Enable local channel'}).waitFor();
    assert.equal(await page.locator('.chat-composer-input').isDisabled(),true);
    assert.equal(createPosts,1);assert.equal(controlPosts,2); // Probe consent did not transfer.
    cv.runtime.state='ready';cv.runtime.setup=null;
    await page.evaluate(()=>window.emitNative('runtime.ready',{sequence:1,event_type:'runtime.ready'}));
    await page.waitForFunction(()=>!document.querySelector('.chat-composer-input').disabled);
    await page.locator('.chat-composer-input').fill('Finite source fixture message');
    await page.getByRole('button',{name:'Send',exact:true}).click();
    await page.getByText('Send outcome unknown. Refresh evidence or Close; this send was not replayed.').waitFor();
    assert.equal(messagePosts,1);assert.equal(await page.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);
    await page.reload({waitUntil:'networkidle'});
    assert.equal(await page.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);
    messageReadback=true;
    await page.getByRole('button',{name:'Refresh evidence'}).click();
    await page.waitForFunction(()=>!document.querySelector('.chat-compose-actions .primary').disabled);
    assert.equal(messagePosts,1); // Exact request-key GET resolved the ambiguity.
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.getByText('Cleanup: complete · owned unit exit verified').waitFor();
    await page.evaluate(()=>{location.hash='#interface/ui/configure';});
    await page.getByLabel('Keep native runtime open (experiment)').check();
    await page.waitForFunction(()=>!document.querySelector('.chat-native-check .primary').disabled);
    assert.equal(createPosts,1); // A known cleaned-up creation permits a distinct New Chat.
    check.admissible=false;check.retry_allowed=true;check.state='complete';
    await page.getByRole('button',{name:'Refresh check'}).click();
    await page.waitForFunction(()=>document.querySelector('.chat-native-check .primary').disabled);
    assert.equal(await page.getByRole('button',{name:'Check native compatibility'}).isEnabled(),true);
    await page.getByRole('button',{name:'Check native compatibility'}).click();
    await page.getByText(/Use Refresh check; this check was not replayed/).waitFor();
    assert.equal(checkPosts,2); // Fresh key only after explicit server cleanup-safe retry.
    enabled=false;
    const productionConfig=page.waitForResponse(response=>response.url().endsWith('/api/conversations/native-config'));
    await page.reload({waitUntil:'networkidle'});
    await page.getByRole('heading',{name:'Start a chat with UI'}).waitFor();
    await productionConfig;
    assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isVisible(),false);
    assert.equal(checkPosts,2);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({native_processes:0,inference_turns:0,controls_unknown_no_replay:true,close_available:true,
      cold_check_readback:true,creation_readback_no_replay:true,cleanup_bound_new_check:true,send_readback_no_replay:true,separate_chat_consent:true,production_opt_in_hidden:true,composer_reachable:true,default_ephemeral:true,checks_post:checkPosts,controls_post:controlPosts,creates_post:createPosts,browser_errors:errors.length}));
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
