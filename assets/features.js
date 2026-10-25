/* Workspace tools are loaded on demand; no background model requests. */
function featureElement(tag, text, className='') {
  const node=document.createElement(tag);node.textContent=text;node.className=className;return node;
}
function featureButton(text, handler) {
  const node=featureElement('button',text,'small-button');node.type='button';
  node.addEventListener('click',async()=>{node.disabled=true;try{await handler();}catch(e){toast(e.message,true);}finally{node.disabled=false;}});return node;
}
function featureField(parent,label,tag,value) {
  const wrap=featureElement('label',label,'feature-field'),input=document.createElement(tag);
  input.value=value;wrap.append(input);parent.append(wrap);return input;
}
let featuresGeneration=0;
async function workspaceFeature(name) {
  const generation=++featuresGeneration, dialog=$('features-dialog'),body=$('features-body');
  $('features-title').textContent={checks:'Project checks',queue:'Task queue',metrics:'Model observations'}[name];
  body.replaceChildren(featureElement('p','Loading…','subtle'));
  if($('tools-menu').matches(':popover-open'))$('tools-menu').hidePopover();
  if(!dialog.open)dialog.showModal();
  try {
    if(name==='queue') {
      const data=await api('queue');if(generation!==featuresGeneration)return;body.replaceChildren();
      body.append(featureElement('p',data.paused?'Queue paused. Waiting tasks are preserved across restarts.':'One task runs at a time. New messages join this queue.','subtle'));
      const actions=featureElement('div','','feature-actions');
      actions.append(featureButton(data.paused?'Resume queue':'Pause queue',async()=>{await api('queue',{action:data.paused?'resume':'pause'});await workspaceFeature(name);}),featureButton('Refresh',()=>workspaceFeature(name)));body.append(actions);
      if(!data.tasks.length)body.append(featureElement('p','No waiting or running tasks.'));
      for(const task of data.tasks) {
        const row=featureElement('section','','feature-card');row.append(featureElement('strong',task.prompt.slice(0,160)),featureElement('p',task.backend+' · '+task.status+' · '+task.project,'subtle'));
        const controls=featureElement('div','','feature-actions');
        if(task.status==='queued')for(const direction of ['up','down']) controls.append(featureButton(direction==='up'?'Move up':'Move down',async()=>{await api('queue',{action:'move',id:task.id,direction});await workspaceFeature(name);}));
        controls.append(featureButton(task.status==='queued'?'Cancel waiting task':'Stop task',async()=>{await api('stop',{id:task.id});await workspaceFeature(name);}));row.append(controls);body.append(row);
      }
    } else if(name==='metrics') {
      const data=await api('metrics');if(generation!==featuresGeneration)return;body.replaceChildren();
      body.append(featureElement('p',data.note,'subtle'),featureElement('p',data.local_resource.busy?'Local model slot: busy · '+(data.local_resource.owner||'another app'):'Local model slot: available'));
      body.append(featureElement('p','Catalog checked: '+(data.catalog.checked_at?new Date(data.catalog.checked_at*1000).toLocaleString():'not yet checked')+'. Only configured cooperating clients share the local slot.','subtle'));
      for(const candidate of data.catalog.candidates||[]) {
        const card=featureElement('section','','feature-card');card.append(featureElement('strong',candidate.model),featureElement('p',candidate.free?'Verified zero cost at last catalog check':'Unavailable / pricing not approved · '+candidate.status,'subtle'));body.append(card);
      }
      if(!data.observations.length)body.append(featureElement('p','No observed model calls in this version yet.'));
      for(const row of data.observations) {
        const card=featureElement('section','','feature-card');card.append(featureElement('strong',row.model+' · '+row.backend),featureElement('p',`${row.samples} attempts · ${row.successful} completed · ${row.average_seconds}s average · checks ${row.checks_passed} passed / ${row.checks_failed} failed`),featureElement('p','Last success: '+(row.last_success?new Date(row.last_success*1000).toLocaleString():'none'),'subtle'));body.append(card);
      }
      const cooldowns=featureElement('pre',JSON.stringify(data.cooldowns,null,2),'feature-output');body.append(featureElement('h3','Observed cooldowns'),cooldowns,featureButton('Refresh',()=>workspaceFeature(name)));
    } else {
      const project=currentProject();if(!project)throw new Error('Choose a project before configuring checks.');
      const config=await api('checks?project='+encodeURIComponent(project));if(generation!==featuresGeneration)return;body.replaceChildren();
      body.append(featureElement('p',project,'subtle'),featureElement('p','These commands run independently before and after coding tasks. Configure checks you trust. Each line is a command; shell operators need an explicit shell.','subtle'));
      const commands=featureField(body,'Check commands (one per line)','textarea',config.commands.join('\n'));commands.rows=3;
      const repairs=featureField(body,'Free repair attempts (0–2)','input',config.repairs);repairs.type='number';repairs.min='0';repairs.max='2';
      const timeout=featureField(body,'Timeout per check (seconds)','input',config.timeout);timeout.type='number';timeout.min='5';timeout.max='600';
      const url=featureField(body,'Browser test · running localhost URL','input',config.browser.url);url.placeholder='http://127.0.0.1:3000';
      const steps=featureField(body,'Browser steps (JSON)','textarea',JSON.stringify(config.browser.steps,null,2));steps.rows=5;steps.spellcheck=false;
      body.append(featureElement('p','Example: [{"action":"click","selector":"button[type=submit]"},{"action":"text","selector":"h1","value":"Welcome"}]. Optional Playwright installation is described in README. Traces may contain data displayed by your app.','subtle'));
      const output=featureElement('pre','','feature-output');output.setAttribute('role','status');
      const save=async()=>api('checks',{project,config:{commands:commands.value.split('\n').map(s=>s.trim()).filter(Boolean),timeout:Number(timeout.value),repairs:Number(repairs.value),browser:{url:url.value.trim(),steps:JSON.parse(steps.value)}}});
      const actions=featureElement('div','','feature-actions');
      actions.append(featureButton('Save checks',async()=>{await save();output.textContent='Saved.';}),featureButton('Run checks',async()=>{await save();output.textContent='Running independent checks…';const result=await api('verify',{project});output.textContent=result.status+'\n'+result.checks.map(c=>c.command+' · exit '+c.exit_code+'\n'+c.output).join('\n\n');}),featureButton('Test application',async()=>{await save();output.textContent='Running browser steps…';const result=await api('browser-test',{project});showBrowserResult(result);}));body.append(actions,output);
      function showBrowserResult(result) {
        output.textContent=`Browser: ${result.status}\n${result.steps?.length||0} steps passed\n${result.error||''}\n${(result.console_errors||[]).join('\n')}\nArtifacts: ${result.directory}`;
        for(const artifact of [...(result.screenshots||[]),...(result.trace?[result.trace]:[])]) body.append(featureButton('Download '+artifact,async()=>{
          const response=await fetch('/api/browser/artifact?project='+encodeURIComponent(project)+'&id='+result.id+'&name='+artifact,{headers:{'X-CodeHub-Token':token}});
          if(!response.ok)throw new Error('Artifact unavailable.');
          const objectURL=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=objectURL;link.download=artifact;link.click();setTimeout(()=>URL.revokeObjectURL(objectURL),1000);
        }));
      }
      if(config.browser_latest)showBrowserResult(config.browser_latest);
    }
  }catch(e){if(generation===featuresGeneration)body.replaceChildren(featureElement('p',e.message));}
}
document.querySelectorAll('[data-feature]').forEach(button=>button.addEventListener('click',()=>workspaceFeature(button.dataset.feature)));
$('features-close').addEventListener('click',()=>{$('features-dialog').close();featuresGeneration++;});
for(const action of ['apply','undo']) $('changes-'+action).addEventListener('click',async()=>{
  const button=$('changes-'+action),file=selectedReview?.files[Number($('changes-copy').dataset.index)];if(!file)return;
  button.disabled=true;
  try{await api('changes/action',{project:selectedReview.project,task:selectedReview.task,action,files:[file.path]});$('changes-dialog').close();await openChanges(selectedReview.task);toast(action==='apply'?'File applied.':'File restored.');}catch(e){toast(e.message,true);}finally{button.disabled=false;}
});
