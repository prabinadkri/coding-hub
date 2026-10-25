// Optional, deterministic browser checks. No model calls and no dynamic script evaluation.
import fs from 'node:fs';
import path from 'node:path';
const folder = process.argv[2];
const config = JSON.parse(fs.readFileSync(path.join(folder, 'config.json'), 'utf8'));
const result = {status:'failed', steps:[], pages:[], console_errors:[], page_errors:[], http_errors:[], layout_errors:[], screenshots:[]};
let browser, context;
try {
  const {chromium} = await import('playwright');
  browser = await chromium.launch({headless:true});
  context = await browser.newContext({viewport:{width:1365,height:900}});
  await context.tracing.start({screenshots:true,snapshots:true,sources:false});
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const origin=new URL(config.url).origin;
  await context.route('**/*', route=>{
    const request=route.request();
    if(request.isNavigationRequest() && request.frame()===page.mainFrame() && new URL(request.url()).origin!==origin) {
      result.http_errors.push('Navigation left the configured local application: '+new URL(request.url()).origin);
      return route.abort();
    }
    return route.continue();
  });
  page.on('console', msg => {if(msg.type()==='error' && result.console_errors.length<30) result.console_errors.push(msg.text().slice(0,1000));});
  page.on('pageerror', err => {if(result.page_errors.length<30) result.page_errors.push(err.message.slice(0,1000));});
  page.on('response', response=>{
    if(response.status()>=400 && new URL(response.url()).origin===origin && ['document','script','stylesheet','fetch','xhr'].includes(response.request().resourceType()) && result.http_errors.length<30)
      result.http_errors.push(response.status()+' '+new URL(response.url()).pathname);
  });
  async function visit(url) {
    const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:20000});
    if(response && response.status()>=400)throw new Error('Page returned HTTP '+response.status());
    await page.locator('body').waitFor({state:'visible'});
    // Hydration and initial network requests can finish after DOMContentLoaded.
    await page.waitForLoadState('networkidle',{timeout:3000}).catch(()=>{});
    if(!(await page.locator('body').innerText()).trim() && await page.locator('body canvas, body img, body video, body svg').count()===0)
      throw new Error('The page has no visible content.');
  }
  async function capturePage(url, index) {
    await page.setViewportSize({width:1365,height:900});await visit(url);
    const entry={url:page.url(),title:await page.title(),viewports:[]};
    for(const [size,viewport] of [['desktop',{width:1365,height:900}],['mobile',{width:390,height:844}]]) {
      await page.setViewportSize(viewport);
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+4);
      if(overflow)result.layout_errors.push('Horizontal overflow at '+size+' size: '+new URL(page.url()).pathname);
      const filename=`page-${index}-${size}.png`;
      await page.screenshot({path:path.join(folder,filename)});result.screenshots.push(filename);
      entry.viewports.push({size,horizontal_overflow:overflow});
    }
    result.pages.push(entry);
  }
  await capturePage(config.url,1);
  // Only ordinary same-origin links without query strings, downloads or action-like paths.
  // Never guess form values or click arbitrary buttons during automatic discovery.
  if(!config.steps.length) {
    const links=await page.locator('a[href]').evaluateAll(nodes=>nodes.slice(0,150).map(n=>({href:n.href,download:n.hasAttribute('download')})));
    const destinations=[];
    for(const link of links) {
      const url=new URL(link.href);
      if(link.download || url.origin!==origin || url.search || url.hash || url.pathname===new URL(page.url()).pathname || /(?:logout|signout|delete|remove|checkout|purchase|unsubscribe|reset|confirm|activate|callback|auth|api)(?:[/.\-_]|$)/i.test(url.pathname) || /\.(?:pdf|zip|csv|exe|dmg|png|jpg|svg)$/i.test(url.pathname)) continue;
      if(!destinations.includes(url.href))destinations.push(url.href);
      if(destinations.length===2)break;
    }
    for(const [index,url] of destinations.entries())await capturePage(url,index+2);
  }
  await page.setViewportSize({width:1365,height:900});
  if(page.url()!==config.url)await visit(config.url);
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
  result.status = result.page_errors.length || result.console_errors.length || result.http_errors.length || result.layout_errors.length ? 'failed' : 'passed';
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
