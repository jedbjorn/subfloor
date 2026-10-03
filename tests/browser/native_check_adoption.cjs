// Actual app in ephemeral Chromium. Synthetic HTTP only; no native/account.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require(process.env.SC_PLAYWRIGHT_MODULE||'playwright');
const ui=path.join(process.argv[2],'.super-coder/ui');
const server=http.createServer((req,res)=>{
  const file=req.url==='/'?'index.html':req.url.split('?')[0].slice(1);
  if(!['index.html','app.js','style.css','vendor/marked.umd.js','vendor/purify.min.js'].includes(file)){res.writeHead(404);res.end();return;}
  res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(path.join(ui,file)));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  const receipt={native_processes:0,inference_turns:0,cases:[]};
  try{
    browser=await chromium.launch({headless:true});
    for(const mode of ['automatic','automatic_discovery','adopted_stale','adopted_dispose','manual_unknown','retained_prefix','retained_malformed','saved_reconnect','retained_creation','selection','malformed_reference','probe_binding','probe_wrong_generation','probe_wrong_role','admission_false','latest_installation']){
      const page=await browser.newPage();page.setDefaultTimeout(7000);
      const selection={harness:'codex',model:'gpt-6.1-sol',effort:'high'};
      const id='nc_'+'a'.repeat(32),probeId='cv_'+'b'.repeat(32),known='nc_'+'e'.repeat(32);
      const result={check_id:id,state:'complete',selection,admissible:mode!=='admission_false',retry_allowed:false,
        grades:{submission:'compatible',stop_work_terminal:'compatible',stop_work_child:'inconclusive'},
        probe:{conversation_id:probeId,generation_id:'probe-gen'}};
      const cv={conversation_id:probeId,shell:{shell_id:2,shortname:'PROBE'},runtime_mode:'native_experiment',
        route:selection,runtime:{role:mode==='probe_wrong_role'?'ordinary':'probe',generation_id:mode==='probe_wrong_generation'?'foreign':'probe-gen'}};
      const requests=[],errors=[];let reads=0,release,held=false;
      const waiting=new Promise(resolve=>release=resolve);page.on('pageerror',e=>errors.push(e.stack||String(e)));
      await page.addInitScript(()=>{class Source{constructor(url){this.url=url;this.listeners={};(window.sources||=[]).push(this);queueMicrotask(()=>this.onopen?.());}addEventListener(k,v){(this.listeners[k]||=[]).push(v);}close(){this.closed=true;}}window.EventSource=Source;});
      await page.route('**/api/**',async route=>{
        const req=route.request(),u=new URL(req.url());requests.push([req.method(),u.pathname,u.search]);
        const send=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
        if(u.pathname.startsWith('/api/conversations/native-checks')){
          if(u.searchParams.has('request_key'))return send({error:{code:'CHECK_UNKNOWN',message:'pending'}},404);
          reads++;
          if(['adopted_stale','adopted_dispose'].includes(mode)){held=true;await waiting;}
          const checkId=u.pathname.split('/').pop();
          if(mode==='automatic_discovery'&&reads<3)return send({...result,check_id:checkId,state:reads===1?'accepted':'running',admissible:false,probe:reads===1?null:result.probe});
          return send({...result,check_id:checkId});
        }
        if(u.pathname===`/api/conversations/${probeId}`)return send(cv);
        if(u.pathname==='/api/health')return send({repo:'source-fixture'});
        if(u.pathname==='/api/shells')return send({shells:[]});
        return send({});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/#map`,{waitUntil:'networkidle'});errors.length=0;requests.length=0;
      await page.evaluate(async({mode,selection,id,known})=>{
        // This bounded form test owns its host, not the application router.
        // Probe navigation is observed as a hash without mounting a second UI.
        window.removeEventListener('hashchange',routeFromHash);
        window.removeEventListener('popstate',routeFromHash);
        chatReadController=new AbortController();window.testHost=document.createElement('div');document.body.replaceChildren(testHost);
        const ref={check_id:id,origin:'installed_change',state:'complete'};
        const candidate={...selection,label:'Sol high',automatic_check:mode==='malformed_reference'?{...ref,check_id:'foreign'}:ref,
          retained_checks:Array.from({length:16},(_,i)=>({check_id:'nc_'+i.toString(16).padStart(32,'0'),origin:'operator',state:'retained',scope:'retained_check'})),retained_checks_partial:mode==='retained_prefix'};
        if(mode==='manual_unknown')sessionStorage.setItem('native-check:'+JSON.stringify([1,selection]),JSON.stringify({key:'manual-stable',selection,create:{key:'manual-create',state:'unknown'}}));
        if(mode==='saved_reconnect')sessionStorage.setItem('native-check-adopted:'+JSON.stringify([1,selection]),JSON.stringify({read_only:true,check_id:known,origin:'operator',selection}));
        if(mode==='retained_creation'){
          sessionStorage.setItem('native-check:'+JSON.stringify([1,selection]),JSON.stringify({key:'manual-stable',check_id:id,selection}));
          sessionStorage.setItem('native-check-adopted:'+JSON.stringify([1,selection]),JSON.stringify({read_only:true,check_id:known,origin:'operator',selection,create:{key:'adopted-create',state:'unknown'}}));
        }
        if(mode==='retained_malformed'){candidate.retained_checks='unknown';candidate.retained_checks_partial=false;}
        await chatNativeNewForm(testHost,{shell_id:1},{candidates:[candidate,{harness:'claude',model:'claude-sonnet-5-5',effort:'high',label:'Sonnet high',retained_checks:[],retained_checks_partial:false}]});
      },{mode,selection,id,known});
      const start=page.getByRole('button',{name:'Start native chat',exact:true});
      if(['adopted_stale','adopted_dispose'].includes(mode)){
        while(!held)await page.waitForTimeout(20);
        if(mode==='adopted_stale')await page.getByLabel('Native route to check').selectOption('1');
        else await page.evaluate(()=>testHost.remove());
        release();await page.waitForTimeout(200);
        if(mode==='adopted_stale')assert.equal(await start.isDisabled(),true);
        assert.equal(reads,1);
      }else if(mode==='malformed_reference'){
        await page.waitForTimeout(200);assert.equal(requests.filter(r=>r[1].includes('native-checks')).length,0);assert.equal(await start.isDisabled(),true);
      }else if(mode==='manual_unknown'){
        await page.getByText(/readback inconclusive/).waitFor();
        assert.equal(await start.isDisabled(),true);
        assert.equal(await page.locator('.chat-native-reference').first().getByRole('button',{name:'Use saved check result'}).isDisabled(),true);
        const stored=await page.evaluate(selection=>JSON.parse(sessionStorage.getItem('native-check:'+JSON.stringify([1,selection]))),selection);
        assert.equal(stored.key,'manual-stable');assert.equal(stored.create.key,'manual-create');
        assert.ok(requests.some(r=>r[2]==='?request_key=manual-stable'));assert.ok(requests.some(r=>r[1].endsWith(id)));
      }else{
        await page.getByRole('button',{name:'Open finite probe setup / Close',exact:true}).waitFor();
        if(mode==='automatic_discovery')await page.waitForFunction(()=>!document.querySelector('.chat-native-check .primary').disabled);
        assert.equal(await start.isDisabled(),['admission_false','retained_creation'].includes(mode));
        if(mode==='automatic_discovery'){
          assert.equal(reads,3);assert.equal(await page.evaluate(()=>sources.filter(x=>x.url.includes('cv_')).length),1);
        }
        if(mode==='retained_malformed')await page.getByText(/Retained checks are only partially listed/).waitFor();
        if(mode==='retained_creation'){
          await page.getByText(/Another retained chat creation/).waitFor();
          assert.equal(await page.getByRole('button',{name:'Check native compatibility',exact:true}).isDisabled(),true);
          const stored=await page.evaluate(selection=>JSON.parse(sessionStorage.getItem('native-check-adopted:'+JSON.stringify([1,selection]))),selection);
          assert.equal(stored.create.key,'adopted-create');
        }
        if(mode==='retained_prefix'){
          assert.equal(await page.locator('.chat-native-reference').count(),17);
          await page.getByText(/Retained checks are only partially listed/).waitFor();
          await page.locator('.chat-native-reference').nth(1).getByRole('button',{name:'Read saved check',exact:true}).click();
          await page.locator('.chat-native-reference').nth(1).getByRole('button',{name:'Open this probe setup / Close'}).waitFor();
        }
        if(mode==='saved_reconnect'){
          assert.ok(requests.some(r=>r[1].endsWith(known)));
          assert.equal(await page.locator('.chat-native-reference').count(),18);
        }
        if(mode==='selection'){
          await page.getByLabel('Native route to check').selectOption('1');
          assert.equal(await start.isDisabled(),true);assert.equal(await page.locator('.chat-native-reference').count(),0);
        }
        if(mode.startsWith('probe_')){
          await page.getByRole('button',{name:'Open finite probe setup / Close',exact:true}).click();
          if(mode==='probe_binding')await page.waitForFunction(()=>location.hash.includes('cv_'));
          else{await page.waitForTimeout(200);assert.equal(new URL(page.url()).hash,'#map');}
        }
        if(mode==='latest_installation'){
          await page.evaluate(({selection,id})=>{
            const host=document.createElement('div');document.body.replaceChildren(host);
            const conversation={conversation_id:'cv_old',version:1,state:'idle',runtime_mode:'native_experiment',route:selection,
              runtime:{generation_id:'old-gen',state:'ready',partial:false,primary:{activity_id:'old-turn'},
              capabilities:{submission:'compatible',stop_reply:'compatible'},
              latest_installed_identity:{capability_grade:'inconclusive',check_ref:{check_id:id,origin:'installed_change',state:'complete'}}}};
            window.oldControl=chatNativeControlBody(conversation,'stop_reply');
            chatNativeRuntimePanel(host,conversation,{control:()=>{throw Error('unexpected control')},refresh:()=>{}});
          },{selection,id});
          await page.getByRole('button',{name:'Read latest installation check'}).click();
          await page.getByText(/Observed check: complete/).waitFor();
          assert.equal(await page.evaluate(()=>oldControl.generation_id),'old-gen');
          await page.getByText(/New runtime submission: inconclusive/).waitFor();
        }
      }
      assert.equal(requests.filter(r=>r[0]!=='GET').length,0);assert.deepEqual(errors,[],mode);
      receipt.cases.push(mode);release();await page.close();
    }
    console.log(JSON.stringify(receipt));
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
