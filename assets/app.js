"use strict";
const paths = {
  trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7',
  plus:'M12 5v14M5 12h14',grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  history:'M3 11a9 9 0 1 1 3 8M3 4v7h7M12 7v5l3 2',book:'M12 5v16M3 3l9 2 9-2v16l-9 2-9-2z',
  shield:'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7zM8 12l3 3 5-6',refresh:'M20 8a8 8 0 0 0-14-3L3 8M3 3v5h5M4 16a8 8 0 0 0 14 3l3-3M21 21v-5h-5',
  cloud:'M7 18a5 5 0 1 1 .5-10A6 6 0 0 1 19 11a3.5 3.5 0 0 1-1 7z',layers:'m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5',
  chip:'M6 6h12v12H6zM9 9h6v6H9zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3',lock:'M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4M12 14v3',
  spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',folder:'M3 6h7l2 2h9v12H3zM3 6V4h7l2 2h7v2',
  'arrow-right':'M4 12h16m-6-6 6 6-6 6','arrow-up':'M12 20V4m-6 6 6-6 6 6',activity:'M2 12h5l3-8 4 16 3-8h5',moon:'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11',
  settings:'M10 3h4l1 3 3 1 3 3v4l-3 1-1 3-3 3h-4l-1-3-3-1-3-3v-4l3-1 1-3zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  bulb:'M8 16a7 7 0 1 1 8 0v3H8zM9 22h6M9 19h6',terminal:'m5 6 6 6-6 6M13 18h7',close:'m6 6 12 12M6 18 18 6',check:'m5 12 4 4L19 6'
};
function icon(name){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 24 24');s.setAttribute('class','icon');s.setAttribute('fill','none');s.setAttribute('stroke','currentColor');s.setAttribute('stroke-width','1.6');s.setAttribute('stroke-linecap','round');s.setAttribute('stroke-linejoin','round');s.setAttribute('aria-hidden','true');const p=document.createElementNS(s.namespaceURI,'path');p.setAttribute('d',paths[name]||paths.spark);s.append(p);return s;}
document.querySelectorAll('[data-icon]').forEach(n=>n.replaceWith(icon(n.dataset.icon)));
const $=id=>document.getElementById(id), activeStates=['queued','running','stopping'];
let token=location.hash.slice(1)||sessionStorage.getItem('coding-hub-token')||'';
if(location.hash){sessionStorage.setItem('coding-hub-token',token);history.replaceState(null,'',location.pathname);}
let conversation=null,conversationProject='',treeSignature='',chatSignature='',pendingMessage=null,messageArchive=new Map(),archiveConversation=null;
let memoryProject=null,chatLayout=null,followChat=true,sidebarBusy=false;
let deleteTarget=null,deleteProjectTarget=null,deletingChat=false,historyEpoch=0;
let signinId=null,signinBusy=false,signinPollBusy=false,chosenModels={},modelSignature='';
let route='auto',selected=null,tasks=[],statusData={},folderPath='',folderParent='',lastOutput='',timer,refreshBusy=false,wasConnected=false;
const routeHelp={claude:'Uses your separate Claude subscription through its official CLI. Connect in Accounts first; your plan limits apply.',openai:'Uses your separate ChatGPT subscription through OpenCode. Connect in Accounts and choose a model; availability depends on your plan.',smart:'Choose the Antigravity manager model and up to 3 cloud workers for this same task. Independent project changes run in separate copies, then are integrated and checked. Local fallback stays serial. At most 2 bounded manager calls; token savings are measured, not guaranteed.',auto:'Antigravity → verified free models → local Qwen. Continues from partial work if a provider fails.',antigravity:'Uses Google’s native CLI and your Google sign-in. Choose Flash for speed or Pro for deeper reasoning.',free:'Verifies current zero-cost pricing before trying Space Bunny, LongCat, then Big Pickle. Provider quotas apply.',local:'Qwen 3 8B runs on this computer. Best for focused tasks, with a 16K context and thinking disabled for speed.'};
function positionTools(){
  const menu=$('tools-menu');if(!menu.matches(':popover-open'))return;
  const rect=$('tools-toggle').getBoundingClientRect(),above=rect.top>=innerHeight-rect.bottom;
  menu.style.left=Math.max(8,Math.min(rect.left,innerWidth-menu.offsetWidth-8))+'px';
  menu.style.top=above?'auto':(rect.bottom+8)+'px';
  menu.style.bottom=above?(innerHeight-rect.top+8)+'px':'auto';
  menu.style.maxHeight=Math.max(80,(above?rect.top:innerHeight-rect.bottom)-16)+'px';
}
$('tools-menu').addEventListener('toggle',e=>{$('tools-toggle').setAttribute('aria-expanded',String(e.newState==='open'));positionTools();});
window.addEventListener('resize',positionTools);
function toast(message,error=false){clearTimeout(timer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;timer=setTimeout(()=>$('toast').hidden=true,6000);}
async function api(path,body,signal){const options={signal,headers:{'X-CodeHub-Token':token}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const res=await fetch('/api/'+path,options);const data=await res.json();if(!res.ok){const error=new Error(data.error||'Request failed.');error.status=res.status;throw error;}return data;}
function view(name){if($('tools-menu').matches(':popover-open'))$('tools-menu').hidePopover();document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==name+'-view');document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===name));$('page-title').textContent={workspace:'Chats',history:'Task history',models:'Models & hardware',usage:'Usage & limits',guide:'Quick guide',accounts:'Accounts'}[name];if(name==='history')renderHistory(true);document.body.classList.toggle('chat-open',name==='workspace'&&!!conversation);}
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>view(el.dataset.view)));
function currentProject(){return conversation?conversationProject:$('project').value;}
function syncChatLayout(){
  const existing=!!conversation;
  document.body.classList.toggle('chat-open',existing&&!$('workspace-view').hidden);
  $('workspace-view').classList.toggle('existing-chat',existing);
  const general=$('task-scope').value==='general';
  $('scope-picker').hidden=existing;
  $('project-picker').hidden=existing||general;$('project').required=!general&&!existing;
  $('open-memory').hidden=general&&!existing;$('open-memory').textContent=general?'Memory':'Project memory';
  const ideas=[['Explain a command','Explain a Linux command and how to use it safely. Ask me which command.'],['Diagnose Linux','Help me diagnose a Linux problem. Ask about my symptoms first, then suggest focused read-only checks.'],['Plan a task','Help me plan a standalone task. Ask what outcome I want.']];
  document.querySelectorAll('[data-prompt]').forEach((button,index)=>{button.dataset.projectPrompt ||=button.dataset.prompt;button.dataset.projectLabel ||=button.textContent;button.dataset.prompt=general?ideas[index][1]:button.dataset.projectPrompt;button.textContent=general?ideas[index][0]:button.dataset.projectLabel;});
  $('edit-label').textContent=general?'Allow commands & changes':'Allow edits & commands';
  if(!existing)$('chat-subtitle').textContent=general?'Ask a question, diagnose Linux, or describe a standalone task.':'Choose a project and start a focused conversation.';
  $('project').readOnly=existing;$('browse').disabled=existing;
  $('composer-title').textContent=existing?'Reply':'Start a conversation';
  $('prompt-label').textContent=existing?'YOUR MESSAGE':'YOUR TASK';
  $('prompt').rows=existing?2:3;resizePrompt();
  $('prompt').setAttribute('aria-label',existing?'Your message':'Your task');
  $('prompt').placeholder=existing?'Continue the conversation…':'Describe what you want to build, fix, or understand…';
  if(chatLayout!==existing){$('chat-settings').open=!existing;$('activity-details').open=false;chatLayout=existing;}
}
function settingsSummary(){$('chat-settings-summary').textContent=({auto:'Automatic',smart:'Smart',antigravity:'Antigravity',free:'Free cloud',local:'Local Qwen',claude:'Claude account',openai:'ChatGPT account'}[route])+' · '+($('allow-edits').checked?'Edits enabled':'Analysis');}
function setRoute(value){route=value;document.querySelectorAll('[data-route]').forEach(el=>{const yes=el.dataset.route===value;el.classList.toggle('selected',yes);el.setAttribute('aria-pressed',yes);});$('route-help').textContent=routeHelp[value];$('quality').disabled=['free','local'].includes(value);settingsSummary();renderModelPicker();saveDraft();}
document.querySelectorAll('[data-route]').forEach(el=>el.addEventListener('click',()=>setRoute(el.dataset.route)));
function mode(){ $('mode-note').textContent=$('allow-edits').checked?'Commands enabled: the agent can run shell commands and make requested changes. Review the results.':'Analysis only: the agent explains without executing commands. Enable commands to perform a task.';settingsSummary();saveDraft();}
$('allow-edits').addEventListener('change',mode);
$('task-scope').addEventListener('change',()=>{syncChatLayout();saveDraft();});
function saveDraft(){try{localStorage.setItem('coding-hub-draft',JSON.stringify({scope:$('task-scope').value,project:currentProject(),prompt:$('prompt').value,route,quality:$('quality').value,edits:$('allow-edits').checked,conversation,conversationProject,chosenModels,workers:Number($('smart-workers').value)}));}catch{} }
['project','prompt','quality'].forEach(id=>$(id).addEventListener('input',()=>{$('char-count').textContent=$('prompt').value.length.toLocaleString()+' / 12,000';saveDraft();}));
try{const d=JSON.parse(localStorage.getItem('coding-hub-draft')||'{}');chosenModels=d.chosenModels||{};$('smart-workers').value=String([1,2,3].includes(d.workers)?d.workers:1);$('task-scope').value=d.scope|| (d.project?'project':'general');conversation=d.conversation||null;conversationProject=d.conversationProject||'';$('project').value=d.project||'';if(conversation){$('project').readOnly=true;$('browse').disabled=true;}$('prompt').value=d.prompt||'';$('quality').value=d.quality==='deep'?'deep':'fast';$('allow-edits').checked=!!d.edits;setRoute(routeHelp[d.route]?d.route:'auto');mode();$('char-count').textContent=$('prompt').value.length.toLocaleString()+' / 12,000';}catch{}
syncChatLayout();
document.querySelectorAll('[data-prompt]').forEach(el=>el.addEventListener('click',()=>{$('prompt').value=el.dataset.prompt;$('prompt').dispatchEvent(new Event('input'));$('prompt').focus();}));
function newTask(){followChat=true;$('chat-latest').hidden=true;view('workspace');messageArchive.clear();archiveConversation=null;conversation=null;conversationProject='';$('task-scope').value='general';pendingMessage=null;chatSignature='';$('chat-thread').hidden=true;$('chat-thread').replaceChildren();$('chat-title').textContent='New conversation';$('chat-subtitle').textContent='Choose a project and start a focused conversation.';$('chat-subtitle').title='';$('composer-title').textContent='Start a conversation';$('project').readOnly=false;$('browse').disabled=false;$('prompt').value='';selected=null;lastOutput='';syncChatLayout();$('activity-details').open=false;renderTask(null);$('prompt').dispatchEvent(new Event('input'));$('prompt').focus();treeSignature='';}

$('new-task').addEventListener('click',newTask);
$('new-project').addEventListener('click',()=>{newTask();$('task-scope').value='project';syncChatLayout();$('project').value='';saveDraft();$('browse').click();});
document.addEventListener('keydown',e=>{const typing=/INPUT|TEXTAREA|SELECT/.test(e.target.tagName);if(e.key.toLowerCase()==='n'&&!typing&&!e.metaKey&&!e.ctrlKey&&!$('folder-dialog').open){e.preventDefault();newTask();}if(e.target===$('prompt')&&(e.ctrlKey||e.metaKey)&&e.key==='Enter'&&!$('run-task').disabled){e.preventDefault();$('task-form').requestSubmit();}});
function badge(id,text,kind){$(id).textContent=text;$(id).className='badge '+kind;}
function renderStatus(s){statusData=s;renderAccounts(s);renderModelPicker();renderHardwareActivity(s);renderQuota(s.quota||{});renderFreeQuota(s.free_quota||{});if(s.checking)return;const programs=s.programs||{};badge('google-state',programs.agy?'Installed':'Install needed',programs.agy?'good':'bad');badge('free-state',s.pricing_error?'Check unavailable':s.free_models.length?'Price verified':'Unavailable',s.free_models.length&&!s.pricing_error?'good':'neutral');$('free-count').textContent=s.free_models.length+' verified free model'+(s.free_models.length===1?'':'s');$('free-description').textContent=s.pricing_error?'Pricing will be checked again when routing':'Fresh pricing checks. No credit purchases.';badge('local-state',s.local_ready?(s.gpu?'GPU ready':'CPU ready'):'Setup needed',s.local_ready?'good':'bad');$('local-description').textContent=s.gpu?'GPU acceleration available · private inference':'Private inference with Ollama';$('gpu-name').textContent=s.gpu?s.gpu.name.replace('NVIDIA GeForce ',''):'CPU inference';$('gpu-detail').textContent=s.gpu?'NVIDIA driver active':'GPU not detected or driver unavailable';$('gpu-memory').textContent=s.gpu?(s.gpu.used_mb/1024).toFixed(1)+' / '+(s.gpu.total_mb/1024).toFixed(0)+' GB':'—';$('gpu-meter').style.width=s.gpu?Math.min(100,s.gpu.used_mb/s.gpu.total_mb*100)+'%':'0%';$('ram-free').textContent=s.ram?s.ram.available_gb+' / '+s.ram.total_gb+' GB':'Not available';const local=(s.loaded||[]).find(m=>m.name===s.local_model||m.model===s.local_model)||(s.loaded||[])[0];$('gpu-detail').textContent=local?(local.size_vram?'GPU acceleration in use':'Local model running on CPU'):(s.gpu?'GPU available · local model sleeping':'Local inference uses CPU');$('offload').textContent=local?(local.size_vram?Math.round(local.size_vram/local.size*100)+'% GPU · rest CPU':'CPU only'):'Sleeping';$('unload').disabled=!local||tasks.some(t=>activeStates.includes(t.status));if(!$('project').value&&s.default_project){$('project').value=s.default_project;saveDraft();}}
function duration(t){const seconds=Math.max(0,Math.floor(((t.ended_at||Date.now()/1000)-(t.started_at||t.created_at))));return seconds<60?seconds+'s':Math.floor(seconds/60)+'m '+seconds%60+'s';}
const label={needs_review:'Needs review',queued:'Starting',running:'Running',stopping:'Stopping',completed:'Completed',failed:'Needs attention',canceled:'Canceled',interrupted:'Interrupted'};
function renderTask(t){if(t){tasks=tasks.map(item=>item.id===t.id?t:item);updatePending(t);}$('activity-empty').hidden=!!t;$('task-output').hidden=!t;$('task-meta').hidden=!t;$('copy-output').disabled=!t||!t.output;$('stop-task').hidden=!t||!activeStates.includes(t.status);$('stop-task').disabled=t?.status==='stopping';if(!t){badge('task-state','Ready','neutral');$('task-elapsed').textContent='';return;}badge('task-state',label[t.status]||t.status,activeStates.includes(t.status)?'busy':t.status==='completed'?'good':t.status==='failed'?'bad':'neutral');$('task-elapsed').textContent=duration(t);const out=$('task-output');const scroll=out.scrollHeight-out.scrollTop-out.clientHeight<70;lastOutput=t.output||'';out.textContent=lastOutput||(t.status==='queued'?'Preparing your task…':'Waiting for the agent’s first update…');if(scroll)out.scrollTop=out.scrollHeight;$('task-meta').textContent=t.project+'  ·  '+({auto:'Automatic',smart:'Smart',free:'Free cloud',local:'Local Qwen',antigravity:'Antigravity'}[t.backend])+'  ·  '+(t.mode==='build'?'Edits enabled':'Analysis only')+(t.status==='completed'?'  ·  Review changes and test results.':'');}
let historyOffset=0,historyRequest=0,historyTimer,historySignature='';
async function renderHistory(force=false){
  const params=new URLSearchParams({q:$('history-search').value,status:$('history-status').value,route:$('history-route').value,offset:historyOffset});
  const signature=params.toString()+JSON.stringify(tasks.map(t=>[t.id,t.status]))+historyEpoch;
  if(!force&&signature===historySignature)return;
  historySignature=signature;const request=++historyRequest;
  $('history-summary').textContent='Loading saved runs…';$('history-previous').disabled=true;$('history-next').disabled=true;
  try{
    const data=await api('history?'+params);
    if(request!==historyRequest)return;
    historyOffset=data.offset;
    $('history-summary').textContent=data.total?`${data.offset+1}–${data.offset+data.tasks.length} of ${data.total} runs`:'No matching runs';
    $('history-previous').disabled=!data.offset;$('history-next').disabled=!data.has_more;
    const list=$('history-list');list.replaceChildren();
    if(!data.tasks.length){const empty=document.createElement('p');empty.className='folder-empty';empty.textContent='Try a different search or clear the filters.';list.append(empty);}
    data.tasks.forEach(t=>{const b=document.createElement('button');b.className='history-item';const text=document.createElement('div'),h=document.createElement('h3'),p=document.createElement('p'),when=document.createElement('span'),state=document.createElement('span');h.textContent=t.prompt;p.textContent=(t.project.split('/').filter(Boolean).pop()||t.project)+' · '+t.backend+' · '+duration(t);p.title=t.project;text.append(h,p);when.className='history-time';when.textContent=new Date(t.created_at*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});state.className='badge '+(t.status==='completed'?'good':activeStates.includes(t.status)?'busy':t.status==='failed'?'bad':'neutral');state.textContent=label[t.status]||t.status;b.append(text,when,state,icon('arrow-right'));b.addEventListener('click',async()=>{view('workspace');try{if(t.conversation)await openChat(t.project,t.conversation);selected=t.id;renderTask(await api('tasks/'+selected));$('activity-details').open=true;$('activity-panel').scrollIntoView({behavior:'smooth',block:'center'});}catch(e){toast(e.message,true);}});list.append(b);});
  }catch(error){if(request!==historyRequest)return;historySignature='';$('history-summary').textContent='History unavailable: '+error.message;}
}
function filterHistory(){historyOffset=0;historySignature='';historyRequest++;clearTimeout(historyTimer);historyTimer=setTimeout(()=>renderHistory(),250);}
$('history-search').addEventListener('input',filterHistory);
for(const id of ['history-status','history-route'])$(id).addEventListener('change',filterHistory);
$('history-previous').addEventListener('click',()=>{historyOffset=Math.max(0,historyOffset-20);renderHistory(true);});
$('history-next').addEventListener('click',()=>{historyOffset+=20;renderHistory(true);});
async function refreshSidebar(){
  if(sidebarBusy||deletingChat||!token)return;
  sidebarBusy=true;const epoch=historyEpoch;
  try{
    const tree=await api('projects',undefined,AbortSignal.timeout(12000));
    if(epoch!==historyEpoch)return;
    renderTree(tree.projects);$('sidebar-status').hidden=true;
  }catch(error){
    if(epoch!==historyEpoch)return;
    $('sidebar-status').textContent='Saved chats could not load. Retrying…';$('sidebar-status').hidden=false;
  }finally{sidebarBusy=false;if(epoch!==historyEpoch&&!deletingChat)refreshSidebar();}
}
async function refresh(){
  refreshSidebar();
  if(refreshBusy||deletingChat||!token)return;
  refreshBusy=true;const epoch=historyEpoch;
  try{
    const [s,list]=await Promise.all([api('status'),api('tasks')]);
    if(epoch!==historyEpoch)return;
    tasks=list.tasks;
    $('run-task').disabled=tasks.some(t=>activeStates.includes(t.status));renderStatus(s);
    if(!selected){const running=tasks.find(t=>conversation&&t.conversation===conversation&&activeStates.includes(t.status));if(running)selected=running.id;}
    if(selected){const id=selected;try{const detail=await api('tasks/'+id);if(epoch!==historyEpoch)return;if(selected===id)renderTask(detail);}catch(error){if(epoch!==historyEpoch)return;if(error.status===404){if(selected===id){selected=null;renderTask(null);}}else throw error;}}
    if(conversation){
      const id=conversation;
      try{const data=await api('conversation?project='+encodeURIComponent(conversationProject)+'&id='+id);if(epoch!==historyEpoch)return;renderChat(data);}
      catch(error){if(epoch!==historyEpoch)return;if(error.status===400&&error.message.includes('Conversation does not belong')){if(conversation===id)newTask();}else throw error;}
    }
    if(!$('history-view').hidden)renderHistory();
    $('connection').classList.add('connected');$('connection').replaceChildren();const dot=document.createElement('i');$('connection').append(dot,document.createTextNode('Connected locally'));wasConnected=true;
  }catch(e){if(epoch!==historyEpoch)return;$('connection').classList.remove('connected');$('connection').textContent='Disconnected';if(wasConnected){toast('Connection lost. Reopen Coding Hub if the server has stopped.',true);wasConnected=false;}}
  finally{refreshBusy=false;if(epoch!==historyEpoch&&!deletingChat)refresh();}
}
function confirmDeleteChat(project,id,goal){
  if(deletingChat)return;deleteTarget={project,id};
  $('delete-chat-name').textContent=goal||'Untitled chat';$('delete-chat-error').textContent='';
  $('delete-chat-dialog').showModal();$('cancel-delete-chat').focus();
}
$('cancel-delete-chat').addEventListener('click',()=>$('delete-chat-dialog').close());
$('delete-chat-dialog').addEventListener('cancel',e=>{if(deletingChat)e.preventDefault();});
$('confirm-delete-chat').addEventListener('click',async()=>{
  if(!deleteTarget||deletingChat)return;
  const target=deleteTarget;deletingChat=true;historyEpoch++;
  $('confirm-delete-chat').disabled=true;$('cancel-delete-chat').disabled=true;
  try{
    await api('conversation/delete',target);historyEpoch++;
    tasks=tasks.filter(t=>t.conversation!==target.id);
    if(conversation===target.id)newTask();
    treeSignature='';renderHistory();$('delete-chat-dialog').close();toast('Chat deleted. Project files and shared memory kept.');
  }catch(error){$('delete-chat-error').textContent=error.message;}
  finally{deletingChat=false;$('confirm-delete-chat').disabled=false;$('cancel-delete-chat').disabled=false;await refresh();}
}
);

function confirmDeleteProject(project,name){
  if(deletingChat)return;deleteProjectTarget=project;
  $('delete-project-name').textContent=(name||project)+'\n'+project;$('delete-project-error').textContent='';
  $('delete-project-dialog').showModal();$('cancel-delete-project').focus();
}
$('cancel-delete-project').addEventListener('click',()=>$('delete-project-dialog').close());
$('delete-project-dialog').addEventListener('cancel',e=>{if(deletingChat)e.preventDefault();});
$('confirm-delete-project').addEventListener('click',async()=>{
  if(!deleteProjectTarget||deletingChat)return;
  const project=deleteProjectTarget;deletingChat=true;historyEpoch++;
  $('confirm-delete-project').disabled=true;$('cancel-delete-project').disabled=true;
  try{
    await api('project/delete',{project});historyEpoch++;
    tasks=tasks.filter(t=>t.project!==project);
    if(currentProject()===project){newTask();$('project').value='';saveDraft();}
    if(memoryProject===project){memoryProject=null;$('pinned-notes').value='';$('memory-dialog').close();}
    treeSignature='';renderHistory();$('delete-project-dialog').close();toast('Project removed from Coding Hub. Your files are still on disk.');
  }catch(error){$('delete-project-error').textContent=error.message;}
  finally{deletingChat=false;$('confirm-delete-project').disabled=false;$('cancel-delete-project').disabled=false;await refresh();}
});

$('task-form').addEventListener('submit',async e=>{e.preventDefault();$('run-task').disabled=true;try{const t=await api('tasks',{scope:$('task-scope').value,project:currentProject(),prompt:$('prompt').value,backend:route,model:['antigravity','claude','openai','free','local','smart'].includes(route)?chosenModels[route]||null:null,workers:route==='smart'?Number($('smart-workers').value):1,quality:$('quality').value,mode:$('allow-edits').checked?'build':'analysis',conversation});selected=t.id;conversation=t.conversation;conversationProject=t.project;$('task-scope').value=t.scope||'project';syncChatLayout();followChat=true;pendingMessage={id:t.id,prompt:t.prompt};$('activity-details').open=false;$('project').readOnly=true;$('browse').disabled=true;$('prompt').value='';$('char-count').textContent='0 / 12,000';saveDraft();renderTask(t);await refresh();scrollChatBottom();resizePrompt();$('prompt').focus();}catch(error){toast(error.message,true);$('run-task').disabled=false;}});
$('stop-task').addEventListener('click',async()=>{try{await api('stop',{id:selected});toast('Stopping the task. Partial changes are preserved.');await refresh();}catch(e){toast(e.message,true);}});
$('copy-output').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(lastOutput);toast('Output copied.');}catch{toast('Select the output text and copy it manually.',true);}});
$('refresh').addEventListener('click',async()=>{try{await api('refresh',{});toast('Refreshing hardware and model availability.');await refresh();}catch(e){toast(e.message,true);}});
$('unload').addEventListener('click',async()=>{$('unload').disabled=true;try{await api('unload',{model:(statusData.loaded||[]).find(m=>m.name===statusData.local_model||m.model===statusData.local_model)?.name||(statusData.loaded||[])[0]?.name||statusData.local_model});toast('Local model released. It will load again when needed.');await refresh();}catch(e){toast(e.message,true);}});
async function folders(path){try{const data=await api('folders?path='+encodeURIComponent(path));folderPath=data.path;folderParent=data.parent;$('folder-path').textContent=folderPath;const list=$('folder-list');list.replaceChildren();data.folders.forEach(f=>{const b=document.createElement('button');b.className='folder-entry';b.append(icon('folder'),document.createTextNode(f.name),icon('arrow-right'));b.addEventListener('click',()=>folders(f.path));list.append(b);});if(!data.folders.length){const p=document.createElement('p');p.className='folder-empty';p.textContent='No subfolders. You can use this folder.';list.append(p);}}catch(e){toast(e.message,true);}}
$('browse').addEventListener('click',async()=>{await folders($('project').value||statusData.home||'~');if(folderPath)$('folder-dialog').showModal();});$('close-browser').addEventListener('click',()=>$('folder-dialog').close());$('folder-up').addEventListener('click',()=>folders(folderParent));$('folder-home').addEventListener('click',()=>folders(statusData.home||'~'));$('folder-documents').addEventListener('click',()=>folders(statusData.default_project||'~/Documents'));$('select-folder').addEventListener('click',()=>{$('project').value=folderPath;saveDraft();$('folder-dialog').close();});
if(!token)toast('Open Coding Hub from its app icon to connect securely.',true);else refresh();
setInterval(()=>{if(!document.hidden)refresh();},3000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});

function renderTree(projects){const signature=JSON.stringify(projects)+conversation;if(signature===treeSignature)return;treeSignature=signature;const tree=$('project-tree'),open=new Set([...tree.querySelectorAll('details[open]')].map(el=>el.dataset.project));tree.replaceChildren();if(!projects.length){const p=document.createElement('p');p.className='tree-empty';p.textContent='Start a chat to add its project here.';tree.append(p);return;}projects.forEach(project=>{const group=document.createElement('details');group.className='project-group';group.dataset.project=project.project;group.open=open.has(project.project)||project.project===conversationProject||projects.length===1;const title=document.createElement('summary');const name=document.createElement('span');name.className='project-name';name.textContent=project.name;title.append(name);if(project.scope!=='general'){const remove=document.createElement('button');remove.type='button';remove.className='chat-delete project-delete';remove.title='Remove project: '+project.name;remove.setAttribute('aria-label',remove.title);remove.append(icon('trash'));remove.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();confirmDeleteProject(project.project,project.name);});title.append(remove);}title.title=project.scope==='general'?'Standalone conversations':project.project;group.append(title);project.conversations.forEach(chat=>{const button=document.createElement('button');button.className='chat-link'+(chat.id===conversation?' active':'');button.textContent=chat.goal||'Untitled chat';button.title=chat.goal;button.addEventListener('click',()=>openChat(project.project,chat.id));const row=document.createElement('div');row.className='chat-row';const remove=document.createElement('button');remove.className='chat-delete';remove.title='Delete chat: '+(chat.goal||'Untitled chat');remove.setAttribute('aria-label',remove.title);remove.append(icon('trash'));remove.addEventListener('click',()=>confirmDeleteChat(project.project,chat.id,chat.goal));row.append(button,remove);group.append(row);});const add=document.createElement('button');add.className='chat-link';add.textContent='+ New chat';add.addEventListener('click',()=>{newTask();$('task-scope').value=project.scope||'project';syncChatLayout();$('project').value=project.project;saveDraft();});group.insertBefore(add,title.nextSibling);tree.append(group);});}
async function openChat(project,id){const epoch=historyEpoch;try{followChat=true;const data=await api('conversation?project='+encodeURIComponent(project)+'&id='+id);if(epoch!==historyEpoch)return;conversation=id;conversationProject=project;$('task-scope').value=data.scope||'project';selected=null;pendingMessage=null;lastOutput='';chatSignature='';$('project').value=project;$('project').readOnly=true;$('browse').disabled=true;$('prompt').value='';syncChatLayout();$('activity-details').open=false;renderTask(null);renderChat(data);view('workspace');saveDraft();treeSignature='';await refresh();}catch(e){toast(e.message,true);}}
function message(role,text,status='',turn=null){const box=document.createElement('article');box.className='chat-message '+role;const label=document.createElement('div');label.className='message-role';label.textContent=role==='user'?'You':'Coding Hub';const content=document.createElement('div');content.className='message-text';renderMessageText(content,text);box.append(label,content);if(role==='assistant'&&turn){const actions=document.createElement('div');actions.className='message-actions';const copy=document.createElement('button');copy.type='button';copy.textContent='Copy reply';copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(text);toast('Reply copied.');}catch{toast('Select the reply text to copy it.',true);}});actions.append(copy);if(turn.has_review){const review=document.createElement('button');review.type='button';review.textContent='View changes';review.addEventListener('click',()=>openChanges(turn.task));actions.append(review);}if(turn.details){const details=document.createElement('button');details.type='button';details.textContent='View details';details.addEventListener('click',()=>{renderMessageText($('task-details-content'),turn.details);$('task-details-dialog').showModal();});actions.append(details);}box.append(actions);}if(status){const meta=document.createElement('div');meta.className='message-status';meta.textContent=status;box.append(meta);}return box;}
function renderChat(data){if(data.id!==conversation)return;if(archiveConversation!==data.id){messageArchive.clear();archiveConversation=data.id;}data.turns.forEach(t=>messageArchive.set(t.rowid,t));const allTurns=[...messageArchive.values()].sort((a,b)=>a.rowid-b.rowid);const active=tasks.find(t=>t.conversation===conversation&&activeStates.includes(t.status));const signature=JSON.stringify(allTurns)+data.total_turns+(active?.id||'');if(signature===chatSignature){if(active)updatePending(active);return;}chatSignature=signature;$('chat-title').textContent=data.goal.split(/\n|\.\s/)[0].slice(0,80);$('chat-subtitle').textContent=(data.scope==='general'?'General task':data.project.split(/[\\/]/).filter(Boolean).pop())+' · Saved conversation';$('chat-subtitle').title=data.scope==='general'?'':data.project;syncChatLayout();const thread=$('chat-thread'),oldTop=thread.scrollTop,oldHeight=thread.scrollHeight;const earlier=allTurns.length&&data.turns.length&&data.turns.at(-1).rowid<allTurns.at(-1).rowid;thread.hidden=false;thread.replaceChildren();if(allTurns.length&&data.total_turns>allTurns.length){const p=document.createElement('button');p.className='small-button chat-more';p.textContent='Load earlier messages';p.addEventListener('click',async()=>{try{renderChat(await api('conversation?project='+encodeURIComponent(conversationProject)+'&id='+conversation+'&before='+allTurns[0].rowid));}catch(e){toast(e.message,true);}});thread.append(p);}allTurns.forEach(turn=>{thread.append(message('user',turn.request),message('assistant',turn.result||'No final response was saved.',turn.status==='completed'?'':turn.status.replaceAll('_',' '),turn));});if(active){thread.append(message('user',active.prompt));const pending=document.createElement('article');pending.className='pending-message';pending.id='pending-status';pending.dataset.task=active.id;pending.setAttribute('role','status');const head=document.createElement('div');head.className='pending-heading';const spinner=document.createElement('span');spinner.className='thinking-spinner';spinner.setAttribute('aria-hidden','true');const title=document.createElement('strong');title.id='pending-title';const detail=document.createElement('p');detail.id='pending-detail';head.append(spinner,title);pending.append(head,detail);thread.append(pending);updatePending(active);}else pendingMessage=null;requestAnimationFrame(()=>{if(followChat&&!earlier)scrollChatBottom();else thread.scrollTop=oldTop+(earlier?thread.scrollHeight-oldHeight:0);});}
function renderQuotaGroup(data,holderId,ageId){const holder=$(holderId);holder.replaceChildren();if(!data.groups?.length){holder.textContent=data.refreshing?'Checking official usage…':data.error||'Quota has not been reported yet.';}else data.groups.forEach(group=>group.buckets.forEach(bucket=>{const box=document.createElement('div');box.className='quota-bucket';const row=document.createElement('div');row.className='quota-name';const name=document.createElement('span'),amount=document.createElement('strong');name.textContent=group.name;amount.textContent=bucket.remaining_percent.toFixed(1)+'% left';row.append(name,amount);const meter=document.createElement('div');meter.className='meter';const fill=document.createElement('span');fill.style.width=bucket.remaining_percent+'%';meter.append(fill);const reset=document.createElement('div');reset.className='quota-reset';if(bucket.reset_at){const secs=Math.max(0,Math.floor(bucket.reset_at-Date.now()/1000));reset.textContent='Resets '+new Date(bucket.reset_at*1000).toLocaleString()+' · '+Math.floor(secs/86400)+'d '+Math.floor(secs%86400/3600)+'h '+Math.floor(secs%3600/60)+'m';}else reset.textContent='Reset time not reported';box.append(row,meter,reset);holder.append(box);}));$(ageId).textContent=(data.checked_at?'Checked '+new Date(data.checked_at*1000).toLocaleTimeString():'')+(data.stale?' · Cached / needs refresh':'')+(data.error?' · '+data.error:'');$('quota-refresh').disabled=!!data.refreshing;}
function renderQuota(data){renderQuotaGroup(data,'quota-groups','quota-age');renderQuotaGroup(data,'usage-quota-groups','usage-quota-age');$('usage-quota-refresh').disabled=!!data.refreshing;}
$('quota-refresh').addEventListener('click',async()=>{try{renderQuota(await api('quota/refresh',{}));}catch(e){toast(e.message,true);}});
$('open-memory').addEventListener('click',async()=>{
  const project=currentProject();
  if(!project){toast('Choose a project first.',true);return;}
  try{
    const data=await api('project?path='+encodeURIComponent(project));
    if(project!==currentProject())return;
    memoryProject=project;
    $('memory-project').textContent=project;
    $('pinned-notes').value=data.requirements;
    $('guidance-files').textContent='Project instructions: '+(data.guidance_files.map(f=>f.name+(f.included?'':' (read on demand)')).join(', ')||'No instruction file yet.');
    $('memory-stats').textContent=data.indexed_files+' / '+data.eligible_files+' source files indexed · '+data.turns+' saved turns';
    $('memory-dialog').showModal();
  }catch(e){toast(e.message,true);}
});
$('close-memory').addEventListener('click',()=>$('memory-dialog').close());
$('save-memory').addEventListener('click',async()=>{if(!memoryProject)return;try{await api('project/notes',{project:memoryProject,requirements:$('pinned-notes').value});$('memory-dialog').close();toast('Requirements saved for all chats in this project.');}catch(e){toast(e.message,true);}});
$('init-rules').addEventListener('click',async()=>{if(!memoryProject)return;try{const result=await api('project/init',{project:memoryProject});toast(result.created?'Created CODING_HUB.md. Edit it with your project conventions.':'CODING_HUB.md already exists and was preserved.');$('guidance-files').textContent='Project instructions: CODING_HUB.md';}catch(e){toast(e.message,true);}});

function renderMessageText(target,text){
  function inline(node,value){value.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).forEach(part=>{let el;if(part.startsWith('**')&&part.endsWith('**')){el=document.createElement('strong');el.textContent=part.slice(2,-2);}else if(part.startsWith('`')&&part.endsWith('`')){el=document.createElement('code');el.textContent=part.slice(1,-1);}else el=document.createTextNode(part);node.append(el);});}
  const pieces=String(text).split(/(```[\s\S]*?(?:```|$))/g);
  pieces.forEach(piece=>{
    if(piece.startsWith('```')){
      const value=piece.replace(/^```[^\n]*\n?/,'').replace(/```$/,'');
      const wrap=document.createElement('div'),head=document.createElement('div'),language=document.createElement('span'),copy=document.createElement('button'),pre=document.createElement('pre'),code=document.createElement('code');
      wrap.className='reply-code-block';head.className='reply-code-heading';language.textContent=piece.slice(3).split('\n')[0]||'Code';copy.type='button';copy.textContent='Copy';copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(value);copy.textContent='Copied';setTimeout(()=>copy.textContent='Copy',1600);}catch{toast('Select the code to copy it.',true);}});code.textContent=value;head.append(language,copy);pre.append(code);wrap.append(head,pre);target.append(wrap);
    }else{
      let paragraph=[],list=null;
      function flush(){if(paragraph.length){const p=document.createElement('p');inline(p,paragraph.join('\n'));target.append(p);paragraph=[];}list=null;}
      for(const line of piece.split('\n')){
        const heading=line.match(/^#{1,6}\s+(.+)/),bullet=line.match(/^\s*[-*+]\s+(.+)/),number=line.match(/^\s*\d+[.)]\s+(.+)/);
        if(!line.trim()){flush();continue;}
        if(heading){flush();const h=document.createElement('h3');inline(h,heading[1]);target.append(h);}
        else if(bullet||number){if(paragraph.length)flush();const tag=number?'OL':'UL';if(!list||list.tagName!==tag){list=document.createElement(tag.toLowerCase());target.append(list);}const li=document.createElement('li');inline(li,(bullet||number)[1]);list.append(li);}
        else if(/^\s*[-*_]{3,}\s*$/.test(line)){flush();}
        else if(line.startsWith('> ')){flush();const quote=document.createElement('blockquote');inline(quote,line.slice(2));target.append(quote);}
        else{list=null;paragraph.push(line);}
      }
      flush();
    }
  });
}

function resetText(stamp,prefix){if(!stamp)return prefix+': not reported';const secs=Math.max(0,Math.floor(stamp-Date.now()/1000));return prefix+' '+new Date(stamp*1000).toLocaleString()+' · '+Math.floor(secs/86400)+'d '+Math.floor(secs%86400/3600)+'h '+Math.floor(secs%3600/60)+'m';}
function usageLine(label,value){const row=document.createElement('div');row.className='usage-line';const key=document.createElement('span'),amount=document.createElement('strong');key.textContent=label;amount.textContent=value;row.append(key,amount);return row;}
function amount(value){return typeof value==='number'?value.toLocaleString():'Unavailable';}
function renderFreeQuota(data){
  const models=$('free-quota-models');
  $('free-quota-coverage').textContent=data.coverage||'Reading local OpenCode observations…';
  const reset=data.expected_reset_at?resetText(data.expected_reset_at,'Expected daily reset')+' (00:00 UTC)':'Expected reset: checking…';
  $('free-expected-reset').textContent=reset;$('free-reset-summary').textContent=reset;
  const signature=JSON.stringify(data.models||[]);
  if(models.dataset.signature!==signature){
  const expanded=new Set([...models.querySelectorAll('details[open]')].map(el=>el.dataset.model));models.replaceChildren();models.dataset.signature=signature;
  (data.models||[]).forEach(model=>{
    const card=document.createElement('article');card.className='panel usage-model';
    const title=document.createElement('h3');title.textContent=model.name;card.append(title);
    const state=document.createElement('span');state.className='badge '+(model.status==='rate_limited'?'bad':'neutral');
    state.textContent={not_reported:'Allowance unknown',rate_limited:'Rate limit reported',retry_elapsed:'Retry window passed',limit_observed:'Limit observed'}[model.status]||'Unknown';card.append(state);
    card.append(usageLine('Remaining','Not reported'),usageLine('AI responses today',amount(model.usage?.responses)),usageLine('Tokens observed today',amount(model.usage?.total_tokens)));
    const detail=document.createElement('p');detail.className='subtle';const tokens=model.usage?.tokens;
    detail.textContent=tokens?'Input '+amount(tokens.input)+' · Output '+amount(tokens.output)+' · Reasoning '+amount(tokens.reasoning)+' · Cache read/write '+amount(tokens.cache_read)+' / '+amount(tokens.cache_write):'Local usage unavailable';const breakdown=document.createElement('details'),summary=document.createElement('summary');breakdown.className='token-breakdown';breakdown.dataset.model=model.id;breakdown.open=expanded.has(model.id);summary.textContent='Token breakdown';breakdown.append(summary,detail);card.append(breakdown);
    if(model.observation){const observation=document.createElement('p');observation.className='usage-observation';observation.textContent=(model.observation.kind==='free_limit'?'Free allowance limit reported. ':'Provider rate limit reported. ')+(model.observation.retry_at?resetText(model.observation.retry_at,'Provider retry time'):'Retry time not reported.')+' Observed '+new Date(model.observation.observed_at*1000).toLocaleString()+'.'+(model.status==='retry_elapsed'?' Access has not been rechecked.':'');card.append(observation);}
    models.append(card);
  });
  }
  $('free-quota-age').textContent=(data.checked_at?'Local data checked '+new Date(data.checked_at*1000).toLocaleTimeString():'Checking local data…')+(data.refreshing?' · Refreshing…':'')+(data.error?' · '+data.error:'')+(data.clock_warning?' · '+data.clock_warning:'');
  $('free-quota-refresh').disabled=!!data.refreshing;
  $('local-usage').textContent='Today: '+amount(data.local?.usage?.responses)+' AI responses · '+amount(data.local?.usage?.total_tokens)+' observed tokens.';
}
$('free-quota-refresh').addEventListener('click',async()=>{try{renderFreeQuota(await api('free-quota/refresh',{}));}catch(e){toast(e.message,true);}});
$('usage-quota-refresh').addEventListener('click',async()=>{try{renderQuota(await api('quota/refresh',{}));}catch(e){toast(e.message,true);}});

function resizePrompt(){const p=$('prompt');p.style.height='auto';p.style.height=Math.min(112,Math.max(conversation?48:72,p.scrollHeight))+'px';}
function scrollChatBottom(){const el=$('chat-thread');followChat=true;el.scrollTop=el.scrollHeight;$('chat-latest').hidden=true;}
$('prompt').addEventListener('input',resizePrompt);
$('chat-latest').addEventListener('click',scrollChatBottom);
$('chat-thread').addEventListener('scroll',()=>{const el=$('chat-thread');followChat=el.scrollHeight-el.scrollTop-el.clientHeight<48;$('chat-latest').hidden=followChat;});
function renderModelPicker(){$('smart-workers-row').hidden=route!=='smart';$('model-label').textContent=route==='smart'?'Manager model (Antigravity account)':'Model';const choices=route==='free'?(statusData.free_models||[]).map(id=>({id,name:id})):route==='local'?(statusData.models||[]).map(id=>({id,name:id})):statusData.provider_models?.[route==='smart'?'antigravity':route]||[];const saved=chosenModels[route]||'';$('quality').disabled=!['auto','antigravity','smart'].includes(route)||!!saved;const options=saved&&!choices.some(m=>m.id===saved)?[{id:saved,name:saved},...choices]:choices;const signature=JSON.stringify([route,options,saved]);$('account-model-row').hidden=!['antigravity','claude','openai','free','local','smart'].includes(route);if(signature===modelSignature)return;modelSignature=signature;$('account-model').replaceChildren(new Option(route==='openai'?'Choose a model':'Use route default',''),...options.map(m=>new Option(m.name,m.id)));$('account-model').value=saved;}
$('smart-workers').addEventListener('change',saveDraft);
$('account-model').addEventListener('change',()=>{chosenModels[route]=$('account-model').value;renderModelPicker();saveDraft();});
function renderHardwareActivity(s){const gpu=s.gpu?.utilization,cpu=s.cpu;$('gpu-load').textContent=gpu==null?'Not available':gpu+'% active';$('cpu-load').textContent=cpu==null?'Waiting for sample':Math.round(cpu)+'% active';$('gpu-load-meter').style.width=Math.max(0,Math.min(100,gpu||0))+'%';$('cpu-load-meter').style.width=Math.max(0,Math.min(100,cpu||0))+'%';$('hardware-age').textContent=s.sampled_at?'Live · sampled '+new Date(s.sampled_at*1000).toLocaleTimeString()+' · refreshes about every 3–6 seconds':'';}
function renderAccounts(s){const states=s.account_status||{};for(const entry of s.accounts||[]){let state=states[entry.id];if(entry.id==='antigravity')state=s.quota?.available&&!s.quota?.error&&!s.quota?.stale?'connected':'unknown';$('account-'+entry.id+'-state').textContent=!entry.installed?'CLI not installed on this computer':({connected:'Connected',saved:'Sign-in saved · provider confirms access when used',sign_in:'Sign in to connect',unknown:'Installed · sign in or check access'}[state]||'Installed · checking access');const b=document.querySelector('[data-signin="'+entry.id+'"]');b.disabled=!entry.installed||!!signinId||signinBusy;}}
async function startSignin(provider,reconnect=false){if(signinBusy||signinId)return;signinBusy=true;renderAccounts(statusData);try{const s=await api('accounts/start',{provider,reconnect});signinId=s.id;$('signin-panel').hidden=false;$('signin-title').textContent={antigravity:'Antigravity · Google OAuth',claude:'Claude subscription',openai:'ChatGPT subscription'}[provider];showSignin(s);$('signin-panel').scrollIntoView({block:'start',behavior:'smooth'});}catch(e){toast(e.message,true);}finally{signinBusy=false;renderAccounts(statusData);}}
function showSignin(s){if(s.id!==signinId)return;$('signin-screen').textContent=s.screen||'Opening the provider’s sign-in flow…';const holder=$('signin-links');if(holder.dataset.links!==JSON.stringify(s.links)){holder.dataset.links=JSON.stringify(s.links);holder.replaceChildren();for(const url of s.links){const a=document.createElement('a');a.href=url;a.textContent='Open provider sign-in in browser ↗';a.target='_blank';a.rel='noopener noreferrer';holder.append(a);}}if(!s.running)$('signin-title').textContent=s.exit_code===0?'Sign-in command finished · choose Done to refresh access':'Sign-in ended · close and try again if access is not connected';}
async function signinInput(data){if(!signinId)return;try{await api('accounts/input',{id:signinId,...data});}catch(e){toast(e.message,true);}}
document.querySelectorAll('[data-signin]').forEach(b=>b.addEventListener('click',()=>startSignin(b.dataset.signin)));
document.querySelectorAll('[data-signin-key]').forEach(b=>b.addEventListener('click',()=>signinInput({key:b.dataset.signinKey})));
$('google-reconnect').addEventListener('click',()=>startSignin('antigravity',true));
$('signin-form').addEventListener('submit',e=>{e.preventDefault();const text=$('signin-input').value;$('signin-input').value='';if(text)signinInput({text});});
$('signin-close').addEventListener('click',async()=>{if(!signinId)return;try{await api('accounts/close',{id:signinId});signinId=null;$('signin-panel').hidden=true;$('signin-screen').textContent='';$('signin-links').replaceChildren();$('signin-links').dataset.links='';$('signin-input').value='';await refresh();}catch(e){toast(e.message,true);}});
setInterval(async()=>{if(!signinId||signinPollBusy||document.hidden)return;signinPollBusy=true;try{showSignin(await api('accounts/session?id='+signinId));}catch(e){$('signin-title').textContent=e.message;}finally{signinPollBusy=false;}},600);
function setTheme(dark){document.documentElement.classList.toggle('dark',dark);$('theme-toggle').setAttribute('aria-pressed',String(dark));$('theme-toggle').textContent=dark?'Light mode':'Dark mode';try{localStorage.setItem('coding-hub-dark',String(dark));}catch{}}
$('theme-toggle').addEventListener('click',()=>setTheme(!document.documentElement.classList.contains('dark')));
try{setTheme(localStorage.getItem('coding-hub-dark')==='true');}catch{}

let selectedReview = null;
async function openChanges(task){
  const dialog=$('changes-dialog');dialog.showModal();$('changes-summary').textContent='Loading task changes…';$('changes-note').textContent='';$('changes-files').replaceChildren();$('changes-code').replaceChildren();$('changes-file-title').textContent='';$('changes-copy').disabled=true;
  try{const review=await api('changes?project='+encodeURIComponent(conversationProject)+'&task='+encodeURIComponent(task));selectedReview=review;
    const added=review.files.reduce((n,f)=>n+f.added,0),removed=review.files.reduce((n,f)=>n+f.removed,0);
    $('changes-summary').textContent=review.files.length+' files · +'+added+' −'+removed+(review.limited?' · Partial review':'');
    $('changes-note').textContent=review.note+(review.limited?' Some files or diffs exceeded the review limits.':'');
    review.files.forEach((file,index)=>{const b=document.createElement('button'),name=document.createElement('span'),stats=document.createElement('small');name.textContent=file.path;stats.textContent=file.status+' · +'+file.added+' −'+file.removed;b.append(name,stats);b.addEventListener('click',()=>selectChange(index));$('changes-files').append(b);});
    if(review.files.length)selectChange(0);else $('changes-code').textContent='No captured source changes.';
  }catch(e){$('changes-summary').textContent='Review unavailable';$('changes-note').textContent=e.message;}
}
function selectChange(index){const file=selectedReview.files[index];$('changes-file-title').textContent=file.path;$('changes-copy').disabled=false;$('changes-copy').dataset.index=index;[...$('changes-files').children].forEach((b,i)=>b.classList.toggle('selected',i===index));const code=$('changes-code');code.replaceChildren();file.diff.split('\n').forEach(line=>{const span=document.createElement('span');span.className=line.startsWith('@@')?'diff-hunk':line.startsWith('+')&&!line.startsWith('+++')?'diff-added':line.startsWith('-')&&!line.startsWith('---')?'diff-removed':'diff-context';span.textContent=line||' ';code.append(span);});}
$('changes-close').addEventListener('click',()=>$('changes-dialog').close());
$('changes-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(selectedReview.files[Number($('changes-copy').dataset.index)].diff);toast('Diff copied.');}catch{toast('Select the diff to copy it.',true);}});

function updatePending(task){const node=$('pending-status');if(!node||node.dataset.task!==task.id)return;const progress=task.progress||{label:task.status==='queued'?'Preparing task…':'Thinking / waiting for model…',detail:'Waiting for the next agent update'};$('pending-title').textContent=progress.label;$('pending-detail').textContent=duration(task)+' · '+progress.detail;}

let diagnosticsText='';
$('run-diagnostics').addEventListener('click',async()=>{
  $('run-diagnostics').disabled=true;$('diagnostics-report').hidden=false;$('diagnostics-report').textContent='Checking your system…';
  try{const data=await api('diagnostics');diagnosticsText=data.text;$('diagnostics-report').textContent=data.text;$('discuss-diagnostics').hidden=false;}
  catch(error){$('diagnostics-report').textContent='Could not check the system: '+error.message;}
  finally{$('run-diagnostics').disabled=false;}
});
$('discuss-diagnostics').addEventListener('click',()=>{newTask();$('allow-edits').checked=false;mode();$('prompt').value='Help me diagnose my Linux laptop. Explain these observations and ask about symptoms when needed. Start with read-only checks; propose exact fixes for review before changing the system.\n\n'+diagnosticsText.slice(0,4800);$('prompt').dispatchEvent(new Event('input'));$('prompt').focus();});

$('close-task-details').addEventListener('click',()=>$('task-details-dialog').close());
