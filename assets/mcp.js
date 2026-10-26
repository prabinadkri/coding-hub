/* Project connections: explicit discovery, no provider or AI calls on panel open. */
async function openMcp() {
  let project=currentProject();const generation=++featuresGeneration,scope=!conversation&&$('task-scope').value==='general'?'general':'project';
  const dialog=$('features-dialog'),body=$('features-body');
  releaseBrowserImages();$('features-title').textContent='MCP connections';body.replaceChildren();
  if($('tools-menu').matches(':popover-open'))$('tools-menu').hidePopover();
  if(!dialog.open)dialog.showModal();
  const current=()=>generation===featuresGeneration;
  try {
    if(!project&&scope!=='general')throw new Error('Choose a workspace first.');
    const data=await api('mcp?project='+encodeURIComponent(project)+'&scope='+scope);if(!current())return;project=data.project;
    body.append(featureElement('p',project,'file-path'),featureElement('p',data.note,'subtle'));
    const output=featureElement('pre','','feature-output');output.setAttribute('role','status');
    const change=async payload=>{await api('mcp',{project,...payload});if(current())await openMcp();};
    const actions=featureElement('div','','feature-actions');
    if(!data.connections.some(c=>c.name==='github'))actions.append(featureButton('Connect GitHub',()=>change({action:'save',connection:{name:'github',type:'github'}})));
    actions.append(featureButton('Add custom server',()=>edit()),featureButton('Refresh',()=>openMcp()));body.append(actions);
    if(!data.connections.length)body.append(featureElement('p','No connections yet. Add GitHub or a trusted MCP server.','subtle'));
    for(const item of data.connections){
      const card=featureElement('section','','feature-card');
      card.append(featureElement('strong',item.name),featureElement('p',`${item.enabled?'Enabled':'Disabled'} · ${item.type==='github'?'GitHub · read-only':item.type+' · edits & commands mode'}${item.tools.length?' · '+item.tools.length+' allowed tools':''}`,'subtle'));
      const controls=featureElement('div','','feature-actions');
      controls.append(featureButton('Test connection',async()=>{
        output.textContent='Connecting and discovering tools…';
        try{const result=await api('mcp',{project,action:'test',name:item.name});if(current())output.textContent=`Connected to ${item.name}${result.identity?' as '+result.identity:''} · ${result.tools.length} tools\n`+result.tools.map(t=>t.name+' — '+t.description).join('\n');}
        catch(e){if(current())output.textContent=e.message;}
      }),featureButton(item.enabled?'Disable':'Enable',()=>change({action:item.enabled?'disable':'enable',name:item.name})),
      featureButton('Edit',()=>edit(item)),featureButton('Delete',async()=>{if(confirm('Remove connection '+item.name+' from this workspace?'))await change({action:'delete',name:item.name});}));
      card.append(controls);body.append(card);
    }
    body.append(output);
    const editor=featureElement('section','','feature-card');editor.hidden=true;body.append(editor);
    function edit(item){
      editor.hidden=false;editor.replaceChildren(featureElement('h3',item?'Edit connection':'Custom MCP server'));
      editor.append(featureElement('p','Paste one connection as JSON. Use command as an argument array for stdio, or url for HTTP. env and headers map to environment variable names, never secret values. An empty tools list permits all server tools; discovery still loads only five schemas at a time.','subtle'));
      const examples=featureElement('div','','feature-actions');editor.append(examples);
      const field=featureField(editor,'Connection JSON','textarea',JSON.stringify(item||{name:'my-server',type:'stdio',command:['/absolute/path/to/mcp-server'],env:{},tools:[]},null,2));
      field.rows=9;field.spellcheck=false;
      if(!item){examples.append(featureButton('Local example',()=>{field.value=JSON.stringify({name:'my-server',type:'stdio',command:['/absolute/path/to/mcp-server'],env:{SERVER_API_KEY:'MY_SERVER_KEY'},tools:[]},null,2);}),featureButton('HTTP example',()=>{field.value=JSON.stringify({name:'my-server',type:'http',url:'https://example.com/mcp',headers:{Authorization:'MY_MCP_AUTH_HEADER'},tools:[]},null,2);}));}
      const save=featureElement('div','','feature-actions');save.append(featureButton('Save connection',async()=>{
        const connection=JSON.parse(field.value);if(item&&connection.name!==item.name)throw new Error('Keep the existing name, or create a separate connection.');
        await change({action:'save',connection});
      }),featureButton('Cancel',()=>{editor.hidden=true;}));editor.append(save);field.focus();
    }
  }catch(e){if(current())body.replaceChildren(featureElement('p',e.message));}
}
