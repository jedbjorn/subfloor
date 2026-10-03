// Fixed accounting expectations; immutable peer defect scripts remain separate.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require(process.env.SC_PLAYWRIGHT_MODULE||'playwright');
const ui=path.join(process.argv[2],'.super-coder/ui');
const server=http.createServer((req,res)=>{const file=req.url==='/'?'index.html':req.url.split('?')[0].slice(1);if(!['index.html','app.js','style.css','vendor/marked.umd.js','vendor/purify.min.js'].includes(file)){res.writeHead(404);res.end();return;}res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(path.join(ui,file)));});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;const receipt={cases:[],native:0,posts:0};try{
 browser=await chromium.launch({headless:true});
 for(const mode of ['saved_reference_selection_disposal_settlement','latest_pending_capacity_coalescing_settlement']){
  const page=await browser.newPage();page.setDefaultTimeout(5000);const held=new Map(),errors=[];let gets=0,active=0,maxActive=0,posts=0;
  page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/api/**',async route=>{const req=route.request(),u=new URL(req.url());if(req.method()!=='GET')posts++;const send=body=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});if(u.pathname.startsWith('/api/conversations/native-checks/')){gets++;active++;maxActive=Math.max(maxActive,active);const id=u.pathname.split('/').pop();await new Promise(resolve=>held.set(id,resolve));held.delete(id);active--;return send({state:'complete',check_id:id,selection:{harness:'codex',model:'gpt-6.1-sol',effort:'high'},admissible:false});}if(u.pathname==='/api/health')return send({repo:'source'});if(u.pathname==='/api/shells')return send({shells:[]});return send({});});
  try{
   await page.goto(`http://127.0.0.1:${server.address().port}/#map`,{waitUntil:'networkidle'});errors.length=0;
   await page.evaluate(()=>{window.removeEventListener('hashchange',routeFromHash);window.removeEventListener('popstate',routeFromHash);chatReadController=new AbortController();window.testHost=document.createElement('div');document.body.replaceChildren(testHost);});
   const mount=async()=>page.evaluate(async()=>{const refs=prefix=>Array.from({length:4},(_,i)=>({check_id:'nc_'+(prefix+i).toString(16).padStart(32,'0'),origin:'operator',state:'retained',scope:'retained_check'}));await chatNativeNewForm(testHost,{shell_id:1},{candidates:[{harness:'codex',model:'gpt-6.1-sol',effort:'high',label:'Codex',retained_checks:refs(10),retained_checks_partial:false},{harness:'claude',model:'claude-sonnet-5-5',effort:'high',label:'Claude',retained_checks:refs(20),retained_checks_partial:false}]});});
   const waitUntil=async fn=>{const end=Date.now()+3000;while(!fn()){assert.ok(Date.now()<end,'bounded held HTTP observation');await page.waitForTimeout(10);}};
   if(mode.startsWith('saved')){
    await mount();for(let i=0;i<4;i++)await page.locator('.chat-native-reference').nth(i).getByRole('button',{name:'Read saved check',exact:true}).click();await waitUntil(()=>gets===4);
    await page.getByLabel('Native route to check').selectOption('1');
    assert.equal(await page.locator('.chat-native-reference button').filter({hasText:'Read saved check'}).evaluateAll(rows=>rows.every(row=>row.disabled)),true);
    await page.evaluate(()=>[...document.querySelectorAll('.chat-native-reference button')].filter(x=>x.textContent==='Read saved check').forEach(x=>x.onclick()));await page.waitForTimeout(50);assert.equal(gets,4);
    await page.evaluate(()=>{testHost.remove();window.testHost=document.createElement('div');document.body.replaceChildren(testHost);});await mount();
    await page.evaluate(()=>[...document.querySelectorAll('.chat-native-reference button')].filter(x=>x.textContent==='Read saved check').forEach(x=>x.onclick()));await page.waitForTimeout(50);assert.equal(gets,4);
    for(const release of held.values())release();await waitUntil(()=>active===0);await page.waitForFunction(()=>chatNativeReferenceReads.size===0);
    await page.evaluate(()=>{const button=[...document.querySelectorAll('.chat-native-reference button')].find(x=>x.textContent==='Read saved check');button.onclick();button.onclick();});await waitUntil(()=>gets===5);
    assert.equal(await page.evaluate(()=>chatNativeReferenceReads.size),1);assert.ok(maxActive<=4);
   }else{
    const id=i=>'nc_'+i.toString(16).padStart(32,'0');
    const render=async i=>page.evaluate(id=>chatNativeRuntimePanel(testHost,{conversation_id:'cv_captured',version:1,state:'idle',runtime_mode:'native_experiment',route:{harness:'codex',model:'gpt-6.1-sol',effort:'high'},runtime:{generation_id:'captured',state:'ready',partial:false,capabilities:{submission:'compatible'},latest_installed_identity:{fingerprint:'fp',capability_grade:'inconclusive',check_ref:{check_id:id,origin:'installed_change',state:'running'}}}},{control:()=>{throw Error('unexpected mutation')},refresh:()=>{}}),id(i));
    for(let i=0;i<65;i++){await render(i);await page.waitForTimeout(5);}await waitUntil(()=>gets===64);
    await render(0);await page.evaluate(()=>document.querySelector('.chat-native-latest button').onclick());await page.waitForTimeout(50);assert.equal(gets,64);assert.equal(await page.evaluate(()=>chatNativeLatestReads.size),64);
    held.get(id(0))();await waitUntil(()=>active===63);await page.waitForFunction(()=>[...chatNativeLatestReads.values()].some(row=>!row.pending));
    await render(64);await waitUntil(()=>gets===65);await render(64);await page.waitForTimeout(50);assert.equal(gets,65);assert.equal(await page.evaluate(()=>chatNativeLatestReads.size),64);assert.ok(maxActive<=64);
   }
   assert.equal(posts,0);assert.deepEqual(errors,[]);receipt.cases.push({mode,gets,maxActive});
  }finally{for(const release of held.values())release();await page.close();}
 }
 console.log(JSON.stringify(receipt));
 }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}})().catch(e=>{console.error(e.stack);process.exitCode=1;});
