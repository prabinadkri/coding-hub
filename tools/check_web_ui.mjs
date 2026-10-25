// End-to-end tests against the real HTTP API, with isolated synthetic state.
import {chromium} from 'playwright';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {once} from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
const output=process.env.CODING_HUB_WEB_REPORT || fs.mkdtempSync(path.join(os.tmpdir(),'hub-web-report-'));
fs.mkdirSync(output,{recursive:true});
const server=spawn(process.env.PYTHON || 'python3',['tools/web_fixture.py'],{cwd:process.cwd(),stdio:['ignore','pipe','inherit']});
const lines=createInterface({input:server.stdout});
let browser,context;
const deadline=setTimeout(()=>{server.kill();process.exitCode=1;console.error('Web test timed out');browser?.close();},90000);
try {
  const [line]=await once(lines,'line'), fixture=JSON.parse(line);
  browser=await chromium.launch({headless:true});context=await browser.newContext({viewport:{width:1365,height:900}});
  await context.tracing.start({screenshots:true,snapshots:true,sources:false});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(10000);
  await page.goto(fixture.url);
  await page.locator('#connection').getByText('Connected locally',{exact:true}).waitFor();
  await page.locator('#task-scope').selectOption('project');await page.locator('#project').fill(fixture.project);
  await page.locator('[data-route="smart-free"]').click();
  assert.equal(await page.locator('#smart-workers-row').isVisible(),true);
  await page.locator('#account-model').selectOption('beam-free');
  await page.locator('#preview-edits').check();assert.equal(await page.locator('#allow-edits').isChecked(),true);
  await page.screenshot({path:path.join(output,'new-chat-light.png')});
  await page.locator('#theme-toggle').click();await page.screenshot({path:path.join(output,'new-chat-dark.png')});
  await page.locator('#theme-toggle').click();
  async function open(name) {await page.locator('#tools-toggle').click();await page.locator(`[data-feature="${name}"]`).click();}
  await open('checks');await page.getByLabel('Check commands (one per line)').fill('python3 -c "print(123)"');
  await page.getByRole('button',{name:'Run checks',exact:true}).click();
  await page.locator('.feature-output').filter({hasText:'passed'}).waitFor();
  await page.getByLabel('Browser test · running localhost URL').fill(fixture.url);
  await page.getByLabel('Browser steps (JSON)').fill(JSON.stringify([{action:'visible',selector:'#chat-title'},{action:'text',selector:'#chat-title',value:'New conversation'},{action:'url',value:'127.0.0.1'}]));
  await page.getByRole('button',{name:'Test application',exact:true}).click();
  await page.locator('.feature-output').filter({hasText:'Browser: passed'}).waitFor({timeout:30000});
  await page.getByRole('button',{name:'Download trace.zip',exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'project-checks.png')});await page.locator('#features-close').click();
  await open('queue');await page.getByRole('button',{name:'Pause queue',exact:true}).click();
  await page.getByRole('button',{name:'Resume queue',exact:true}).waitFor();await page.locator('#features-close').click();
  await page.locator('#prompt').fill('First synthetic queued task');await page.locator('#run-task').click();
  await page.waitForFunction(()=>!document.getElementById('run-task').disabled && !document.getElementById('prompt').value);
  await page.locator('#prompt').fill('Second synthetic queued task');await page.locator('#prompt').press('Control+Enter');
  await page.waitForFunction(()=>!document.getElementById('run-task').disabled && !document.getElementById('prompt').value);
  await open('queue');
  await page.getByText('Second synthetic queued task',{exact:true}).waitFor();
  const rows=page.locator('#features-body .feature-card');assert.equal(await rows.count(),2);
  await rows.nth(1).getByRole('button',{name:'Move up'}).click();
  await page.waitForFunction(()=>document.querySelector('#features-body .feature-card strong')?.textContent==='Second synthetic queued task');
  await page.screenshot({path:path.join(output,'task-queue.png')});
  await page.locator('#features-body .feature-card').first().getByRole('button',{name:'Cancel waiting task'}).click();
  await page.waitForFunction(()=>document.querySelectorAll('#features-body .feature-card').length===1);
  await page.getByRole('button',{name:'Resume queue',exact:true}).click();await page.locator('#features-close').click();
  await open('metrics');await page.getByText('Observed attempts, not a benchmark.',{exact:false}).waitFor();
  await page.screenshot({path:path.join(output,'model-observations.png')});await page.locator('#features-close').click();
  // Use the UI's navigation function to select seeded history, then real review buttons.
  await page.evaluate(async({project,conversation})=>openChat(project,conversation),fixture);
  await page.getByRole('button',{name:'View changes',exact:true}).click();
  await page.getByRole('button',{name:'Apply this file',exact:true}).click();
  await page.getByRole('button',{name:'Undo this file',exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'changes-applied.png')});
  await page.getByRole('button',{name:'Undo this file',exact:true}).click();
  await page.getByRole('button',{name:'Apply this file',exact:true}).waitFor();
  assert.equal(fs.readFileSync(path.join(fixture.project,'source.py'),'utf8'),'answer = 1\n');
  assert.deepEqual(errors,[]);
  console.log('Web UI passed: routes, model selection, checks, queue, shortcut, apply/undo, themes, no JS errors.');
} catch(error) {
  if(context)await context.pages()[0]?.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
  console.error(error);process.exitCode=1;
} finally {
  clearTimeout(deadline);
  if(context)await context.tracing.stop({path:path.join(output,'trace.zip')});
  await browser?.close();lines.close();server.kill();await once(server,'exit');
  console.log('Browser artifacts: '+output);
}
