// Actual app.js in fresh Chromium; synthetic HTTP only, no native/account.
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
  const browser=await chromium.launch({headless:true});
  const receipt={native_processes:0,inference_turns:0,cases:[]};
  try{
    for(const mode of ['converge','immediate','error','expiry','selection','dispose','replacement','terminal_race']){
      const page=await browser.newPage();page.setDefaultTimeout(7000);
      let posts=0,gets=0,active=0,maxActive=0,release,held=false;
      const waiting=new Promise(resolve=>release=resolve);
      const selection={harness:'codex',model:'gpt-6.1-sol',effort:'high'};
      const accepted={check_id:'nc_'+'a'.repeat(32),state:'accepted',selection,probe:null,admissible:false,grades:{}};
      const probing={...accepted,state:'running',probe:{conversation_id:'cv_'+'b'.repeat(32),generation_id:'owned-probe'}};
      const complete={...probing,state:'complete',admissible:true,grades:{submission:'compatible'}};
      const errors=[];page.on('pageerror',e=>errors.push(String(e)));
      await page.addInitScript(()=>{
        const realNow=Date.now;window.clockAdvance=0;Date.now=()=>realNow()+window.clockAdvance;
        class Source{constructor(url){this.url=url;this.listeners={};(window.sources||=[]).push(this);queueMicrotask(()=>this.onopen?.());}addEventListener(k,v){(this.listeners[k]||=[]).push(v);}close(){this.closed=true;}}
        window.EventSource=Source;
      });
      const fulfill=(r,v,status=200)=>r.fulfill({status,contentType:'application/json',body:JSON.stringify(v)});
      await page.route('**/api/**',async route=>{
        const req=route.request(),u=new URL(req.url());
        if(u.pathname==='/api/conversations/native-checks'&&req.method()==='POST'){
          posts++;assert.deepEqual(req.postDataJSON(),selection);
          return fulfill(route,mode==='immediate'?complete:accepted,202);
        }
        if(u.pathname.startsWith('/api/conversations/native-checks')){
          gets++;active++;maxActive=Math.max(maxActive,active);
          try{
            if(['selection','dispose','replacement','terminal_race'].includes(mode)&&gets===1){held=true;await waiting;}
            if(mode==='error')return await fulfill(route,{error:{code:'READ_UNAVAILABLE',message:'synthetic'}},503);
            if(mode==='replacement'&&gets>1)return await fulfill(route,{...probing,admissible:false});
            return await fulfill(route,gets===1&&mode==='converge'?probing:complete);
          }catch{ /* Navigation/selection aborts the captured request. */ }
          finally{active--;}
          return;
        }
        if(u.pathname==='/api/health')return fulfill(route,{repo:'source-fixture'});
        if(u.pathname==='/api/shells')return fulfill(route,{shells:[]});
        return fulfill(route,{});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/#map`,{waitUntil:'networkidle'});
      errors.length=0;
      await page.evaluate(async()=>{
        chatReadController=new AbortController();window.testHost=document.createElement('div');document.body.replaceChildren(window.testHost);
        await chatNativeNewForm(window.testHost,{shell_id:1},{candidates:[{harness:'codex',model:'gpt-6.1-sol',effort:'high',label:'Sol high'},{harness:'codex',model:'other',effort:'high',label:'Other'}]});
      });
      await page.getByRole('button',{name:'Check native compatibility',exact:true}).click();
      if(mode==='immediate'||mode==='converge'){
        await page.waitForFunction(()=>!document.querySelector('.chat-native-check .primary').disabled);
        assert.equal(posts,1);assert.equal(gets,mode==='immediate'?0:2);
        if(mode==='converge')assert.equal(await page.evaluate(()=>window.sources.filter(x=>x.url.includes('cv_')).length),1);
      }else if(mode==='error'){
        await page.getByText(/readback inconclusive/).waitFor();const before=gets;await page.waitForTimeout(1300);assert.equal(gets,before);assert.equal(posts,1);
        assert.equal(await page.getByRole('button',{name:'Start native chat'}).isDisabled(),true);
      }else if(mode==='expiry'){
        await page.evaluate(()=>window.clockAdvance=180000);
        await page.getByText(/Check discovery pending \/ inconclusive/).waitFor();assert.equal(gets,0);assert.equal(posts,1);
      }else{
        while(!held)await page.waitForTimeout(25);
        if(mode==='selection')await page.getByLabel('Native route to check').selectOption('1');
        if(mode==='dispose')await page.evaluate(()=>window.testHost.remove());
        if(mode==='replacement')await page.evaluate(async()=>{
          chatReadController.abort();chatReadController=new AbortController();
          await chatNativeNewForm(window.testHost,{shell_id:1},{candidates:[{harness:'codex',model:'gpt-6.1-sol',effort:'high',label:'Sol high'}]});
        });
        if(mode==='terminal_race'){
          // A manual GET coalesces with the already in-flight discovery read.
          await page.getByRole('button',{name:'Refresh check'}).click();assert.equal(gets,1);
        }
        release();await page.waitForTimeout(400);
        if(mode==='selection')assert.equal(await page.getByRole('button',{name:'Start native chat'}).isDisabled(),true);
        if(mode==='dispose'){const count=gets;await page.waitForTimeout(1300);assert.equal(gets,count);}
        if(mode==='replacement')assert.equal(await page.getByRole('button',{name:'Start native chat'}).isDisabled(),true);
        if(mode==='terminal_race')await page.waitForFunction(()=>!document.querySelector('.chat-native-check .primary').disabled);
        assert.equal(posts,1);
      }
      // The stale request can overlap its replacement only during cancellation;
      // each live form has one native-check GET at a time.
      if(mode!=='replacement')assert.equal(maxActive<=1,true);
      assert.deepEqual(errors,[]);
      receipt.cases.push(mode);release();await page.close();
    }
    console.log(JSON.stringify(receipt));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
