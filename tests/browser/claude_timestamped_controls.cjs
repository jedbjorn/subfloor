// Actual app panel with normalized server projection; no native transport.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require(process.env.SC_PLAYWRIGHT_MODULE||'playwright');
const root=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),ui=path.join(root,'.super-coder/ui');
const server=http.createServer((req,res)=>{const name=req.url==='/'?'index.html':req.url.split('?')[0].slice(1);if(!['index.html','app.js','style.css','vendor/marked.umd.js','vendor/purify.min.js'].includes(name)){res.writeHead(404);res.end();return;}res.end(fs.readFileSync(path.join(ui,name)));});
(async()=>{let browser;try{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));browser=await chromium.launch({headless:true});const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));await page.route('**/api/**',r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({repo:'source',shells:[]})}));
 await page.goto(`http://127.0.0.1:${server.address().port}/#map`,{waitUntil:'networkidle'});
 await page.evaluate(cv=>{window.removeEventListener('hashchange',routeFromHash);window.removeEventListener('popstate',routeFromHash);window.sourceCV=cv;window.requests=[];window.panel=document.createElement('div');document.body.replaceChildren(panel);window.paint=()=>chatNativeRuntimePanel(panel,sourceCV,{connection:'connected',refresh:()=>paint(),control:async(body,key)=>{requests.push({body,key});throw Error('SOURCE_LOST_RESPONSE');}});paint();},input);
 const stop=page.getByRole('button',{name:'Request stop',exact:true}).first();assert.equal(await stop.isEnabled(),true);
 assert.match(await page.locator('.chat-native-work-item').first().innerText(),/last_observed/);
 assert.match(await page.locator('.chat-native-work-item').first().innerText(),/snapshot timestamp/);
 await stop.click();await page.getByText('Outcome unknown. Refresh evidence or Close.').waitFor();
 assert.equal(await page.getByRole('button',{name:'Request stop',exact:true}).first().isDisabled(),true);
 await page.evaluate(()=>paint());assert.equal(await page.getByRole('button',{name:'Request stop',exact:true}).first().isDisabled(),true);
 assert.equal(await page.evaluate(()=>requests.length),1);
 // Reset view intent only for independent malformed fixtures, never in product.
 const failures=await page.evaluate(()=>{const good=structuredClone(sourceCV.runtime.work[0]);const tests=[
  row=>{row.observed_at=true;},row=>{row.observed_at='1';},row=>{row.observed_at=null;},
  row=>{delete row.data.snapshot_complete;},row=>{row.data.snapshot_complete='true';},
  row=>{row.reference.root_id='foreign';},row=>{row.reference.thread_id='child';},
  row=>{row.partial=true;},row=>{row.freshness='current';},row=>{row.data.state='completed';},
  row=>{row.data.kind='child';},row=>{row.provenance='claude:SubagentStop-snapshot';}];
  return tests.map(change=>{const row=structuredClone(good);change(row);return chatNativeControlBody(sourceCV,'stop_work',row);});});
 assert.deepEqual(failures,Array(12).fill(null));assert.deepEqual(errors,[]);
 console.log(JSON.stringify({source_only:true,native_launches:0,unknown_request_count:1,duplicate_writes:0,negative_cases:12,timestamp_visible:true}));
 }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}})().catch(e=>{console.error(e.stack);process.exitCode=1;});
