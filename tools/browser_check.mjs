// Optional, deterministic browser checks. No model calls and no dynamic script evaluation.
import fs from 'node:fs';
import path from 'node:path';
const folder = process.argv[2];
const config = JSON.parse(fs.readFileSync(path.join(folder, 'config.json'), 'utf8'));
const result = {status:'failed', steps:[], console_errors:[], page_errors:[], screenshots:[]};
let browser, context;
try {
  const {chromium} = await import('playwright');
  browser = await chromium.launch({headless:true});
  context = await browser.newContext({viewport:{width:1365,height:900}});
  await context.tracing.start({screenshots:true,snapshots:true,sources:false});
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  page.on('console', msg => {if(msg.type()==='error' && result.console_errors.length<30) result.console_errors.push(msg.text().slice(0,1000));});
  page.on('pageerror', err => {if(result.page_errors.length<30) result.page_errors.push(err.message.slice(0,1000));});
  await page.goto(config.url, {waitUntil:'domcontentloaded',timeout:20000});
  for (const step of config.steps) {
    const locator = step.selector ? page.locator(step.selector) : null;
    if(step.action==='click') await locator.click();
    else if(step.action==='fill') await locator.fill(step.value);
    else if(step.action==='visible') await locator.waitFor({state:'visible'});
    else if(step.action==='text') {
      await locator.waitFor({state:'visible'});
      const start=Date.now();
      while(!(await locator.innerText()).includes(step.value)) {
        if(Date.now()-start>8000) throw new Error('Expected text was not found: '+step.value);
        await new Promise(resolve=>setTimeout(resolve,100));
      }
    } else if(step.action==='url') await page.waitForURL(url=>url.href.includes(step.value));
    result.steps.push({...step,status:'passed'});
  }
  await page.screenshot({path:path.join(folder,'final.png')});
  result.screenshots.push('final.png');
  result.status = result.page_errors.length || result.console_errors.length ? 'failed' : 'passed';
} catch(error) {
  result.error=String(error.message || error).slice(0,4000);
  if(context) {
    try {await context.pages()[0]?.screenshot({path:path.join(folder,'failure.png')});result.screenshots.push('failure.png');} catch {}
  }
} finally {
  if(context) {try {await context.tracing.stop({path:path.join(folder,'trace.zip')});result.trace='trace.zip';} catch {}}
  await browser?.close();
  fs.writeFileSync(path.join(folder,'result.json'),JSON.stringify(result,null,2),{mode:0o600});
}
console.log(`Browser checks: ${result.status} · ${result.steps.length}/${config.steps.length} steps`);
if(result.error) console.log(result.error);
process.exitCode=result.status==='passed'?0:1;
