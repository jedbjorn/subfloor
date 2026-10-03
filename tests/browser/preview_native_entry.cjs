// Actual app, ephemeral synthetic HTTP/Chromium only; no harness or native turn.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const {chromium}=require(process.env.SC_PLAYWRIGHT_MODULE||'playwright');
const ui=path.join(process.argv[2],'.super-coder/ui');
const requests=[];const errors=[];
let profile='submission-preview',enabled=true;
const selection={harness:'codex',model:'gpt-6.1-sol',effort:'high'};
const checkId='nc_'+'a'.repeat(32);
const shell={shell_id:1,shortname:'ui',display_name:'Preview',flavor:'dev'};
const harnesses=['claude','codex','kimi','opencode','vibe'];
const defaults={flavors:{dev:[{harness:'codex',model:'gpt-5.6-terra',is_default:true}]},harnesses,
 harness_status:Object.fromEntries(harnesses.map(h=>[h,{installed:false,unavailable_reason:'HARNESS_UNAVAILABLE',surfaces:{browser:true}}]))};
const server=http.createServer((req,res)=>{
 const file=req.url==='/'?'index.html':req.url.split('?')[0].slice(1);
 if(!['index.html','app.js','style.css','vendor/marked.umd.js','vendor/purify.min.js'].includes(file)){res.writeHead(404);res.end();return;}
 res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');
 res.end(fs.readFileSync(path.join(ui,file)));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();page.setDefaultTimeout(7000);page.on('pageerror',e=>errors.push(String(e)));
  await page.addInitScript(()=>{window.EventSource=class{addEventListener(){}close(){}};});
  const fulfill=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  await page.route('**/api/**',async route=>{
   const req=route.request(),p=new URL(req.url()).pathname,m=req.method();requests.push({p,m});
   if(p==='/api/health')return fulfill(route,{repo:'synthetic-preview'});
   if(p==='/api/shells')return fulfill(route,{shells:[shell],repo_root:'/synthetic'});
   if(p==='/api/shells/1')return fulfill(route,shell);
   if(p==='/api/flavor-defaults')return fulfill(route,defaults);
   if(p==='/api/models')return fulfill(route,{harnesses:{},sources:[],stale:true});
   if(p==='/api/conversations/native-config')return fulfill(route,{enabled,preview_profile:profile,candidates:[{...selection,label:'Codex · gpt-6.1-sol · high',proof_state:'requested_candidate',grades:{}}]});
   if(p==='/api/conversations/native-checks'&&m==='POST')return fulfill(route,{check_id:checkId,state:'accepted',selection,admissible:false,probe:null},202);
   if(p==='/api/conversations/native-checks/'+checkId)return fulfill(route,{check_id:checkId,state:'complete',selection,admissible:false,grades:{submission:'inconclusive'},diagnostics:[{code:'UNOBSERVED',grade:'inconclusive'}],probe:null});
   if(p==='/api/conversations')return fulfill(route,{items:[],next_cursor:null});
   return fulfill(route,{});
  });
  const url='http://127.0.0.1:'+server.address().port;
  await page.goto(url+'/#interface/ui/configure');
  await page.getByRole('heading',{name:'Start a native preview chat'}).waitFor();
  assert.equal(await page.getByLabel('Native route to check').locator('option').count(),1);
  assert.match(await page.getByLabel('Native route to check').textContent(),/gpt-6\.1-sol/);
  assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isVisible(),false);
  assert.equal(await page.getByRole('button',{name:'Start native chat',exact:true}).isDisabled(),true);
  assert.equal(requests.filter(r=>r.m==='POST').length,0);
  await page.getByRole('button',{name:'Check native compatibility',exact:true}).click();
  await page.locator('.chat-native-check-status').filter({hasText:'complete · submission: inconclusive'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Start native chat',exact:true}).isDisabled(),true);
  assert.equal(requests.filter(r=>r.m==='POST').length,1);
  assert.equal(requests.filter(r=>r.m==='POST'&&r.p==='/api/conversations').length,0);
  await page.goto(url+'/#shells-default-models');
  await page.getByText('Legacy launch settings are separate from this isolated native preview.',{exact:false}).waitFor();
  await page.getByRole('link',{name:'Open native preview chat'}).click();
  await page.getByRole('heading',{name:'Start a native preview chat'}).waitFor();
  // The old/full fixture still requires deliberate opt-in, even with unavailable legacy launchers.
  profile=undefined;await page.reload();
  await page.getByLabel('Keep native runtime open (experiment)').waitFor();
  assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isChecked(),false);
  assert.equal(await page.getByRole('heading',{name:'Start a chat with Preview'}).isVisible(),true);
  profile=true;await page.reload();
  await page.getByLabel('Keep native runtime open (experiment)').waitFor();
  assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isChecked(),false);
  enabled=false;profile='submission-preview';await page.reload();
  await page.getByRole('heading',{name:'Start a chat with Preview'}).waitFor();
  assert.equal(await page.getByLabel('Keep native runtime open (experiment)').isVisible(),false);
  assert.equal(requests.filter(r=>r.m==='POST').length,1);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({synthetic_browser:true,native_processes:0,preview_default_native:true,generic_default_ephemeral:true,malformed_no_promotion:true,start_inconclusive_disabled:true,posts:1,creates:0}));
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
