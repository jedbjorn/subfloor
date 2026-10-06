"""Optional real Chromium A12: attachment API, PR panel and Sprint receipt cards."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import test_run_attachments as attachment_fixture

ROOT = Path(__file__).resolve().parents[1]
PLAYWRIGHT = ROOT / '.super-coder/browser/node_modules/playwright'
pytestmark = pytest.mark.skipif(not PLAYWRIGHT.exists() or not shutil.which('chromium'), reason='optional local Chromium seat')


def test_receipt_attachment_browser():
    case = attachment_fixture.AttachmentsTest()
    case.setUp()
    try:
        current = case.receipt(case.base)
        stale = case.receipt()
        case.attachments.attach(stale, 1, {'work_unit': case.unit})
        case.observe(case.base)
        payload = {'base': f'http://127.0.0.1:{case.httpd.server_port}', 'sprint': case.fixture.sprint_id,
                   'unit': case.unit, 'current': current, 'out': os.environ.get('SC_RUNS_SCREENSHOTS')}
        script = r'''
const {chromium} = require(process.argv[1]);
const config = JSON.parse(process.argv[2]);
(async () => {
 const browser = await chromium.launch({headless:true, executablePath:process.argv[3]});
 try {
  const page = await browser.newPage({viewport:{width:1500,height:1100}});
  const errors=[]; page.on('pageerror', e => errors.push(String(e)));
  const shells=[{shell_id:1,shortname:'DEV1',display_name:'Receipt owner',flavor:'dev',skills:[]}];
  await page.route('**/api/**', async route => {
   const path=new URL(route.request().url()).pathname;
   if(path.startsWith('/api/runs') || path.startsWith('/api/sprints')) return route.continue();
   let data={items:[]};
   if(path==='/api/health') data={repo:'A12 isolated fixture'};
   else if(path==='/api/shells') data={shells};
   else if(path.startsWith('/api/shells/')) data=shells[0];
   else if(path==='/api/shell-templates') data={templates:[]};
   else if(path==='/api/flavor-defaults') data={flavors:{},harness_status:{codex:{installed:true,enabled:true,healthy:true,surfaces:{browser:true}}}};
   else if(path==='/api/models') data={harnesses:{}};
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto(config.base+'/#interface/DEV1');
  await page.locator('.chat-pr-panel summary').click();
  await page.locator('.chat-pr-row').waitFor();
  if(!(await page.locator('.chat-pr-panel').innerText()).includes('Stale:')) throw Error('stale receipt missing in PR panel');
  await page.locator('.runs-drawer summary').click();
  await page.getByRole('article',{name:`Run ${config.current}`,exact:true}).getByRole('button',{name:'Attach receipt'}).click();
  await page.getByRole('combobox',{name:'Receipt target'}).selectOption({label:'fixture/project#42'});
  await page.getByRole('button',{name:'Attach',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.chat-pr-row .run-receipt').length===2);
  if(config.out) await page.screenshot({path:config.out+'/a12-pr-receipts.png',fullPage:true});
  await page.goto(config.base+'/#sprints');
  await page.locator(`[data-unit-id="${config.unit}"] .run-receipt`).first().waitFor();
  const card=page.locator(`[data-unit-id="${config.unit}"]`);
  const text=await card.innerText();
  for(const expected of ['Stale:', 'Ancestor of PR head','3 passed','green']) if(!text.includes(expected)) throw Error('card missing '+expected+': '+text);
  if(await card.locator('a[href$="/tail"]').count()!==2) throw Error('receipt log links missing');
  if(config.out) await page.screenshot({path:config.out+'/a12-sprint-receipts.png',fullPage:true});
  if(errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed:true,run:config.current,unit:config.unit,sprint:config.sprint}));
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(PLAYWRIGHT), json.dumps(payload), shutil.which('chromium')],
                                capture_output=True, text=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)['passed']
    finally:
        case.doCleanups()
