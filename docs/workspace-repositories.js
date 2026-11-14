/* Local repository onboarding. Project identities are pinned by previews. */
(function(root){
'use strict';
function setupPayload(path,apply=false,manifest=null,name=''){
  if(apply&&!manifest)throw Error('Apply requires a preview manifest');
  const payload={root:String(path).trim(),apply};
  if(manifest)payload.manifest=JSON.parse(JSON.stringify(manifest));
  if(String(name).trim())payload.name=String(name).trim();
  return payload;
}
function conflictChoices(preview,selections){
  const choices={};for(const change of preview.changes||[])for(const [field,value] of Object.entries(change.fields||{}))if(value.conflict){
    const key=JSON.stringify([change.table,change.key,field]),choice=selections[key];
    if(!['local','remote'].includes(choice))throw Error('Choose a side for every conflicting field');choices[key]=choice;
  }return choices;
}
function create(options){
  const {api,notice,getProjectId,onConnected,onDirty}=options;
  const form=document.querySelector('#repositoryForm'),preview=document.querySelector('#repositoryPreview'),list=document.querySelector('#repositoriesList');
  let sequence=0,applying=false,uncertain=false,reviewing=false;
  function el(tag,text){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node}
  function lock(value){for(const input of form.querySelectorAll('input,button'))input.disabled=value}
  function post(payload){return api('/v1/repository-setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})}
  async function load(){
    const project=getProjectId(),mine=sequence;
    const response=await api('/v1/repositories?'+new URLSearchParams({project_id:project,limit:'100'}));
    const sharing=await Promise.all((response.items||[]).map(repository=>api('/v1/git-sync-status?'+new URLSearchParams({project_id:project,repository_id:repository.id})).catch(()=>null)));
    if(getProjectId()!==project||mine!==sequence||reviewing)return;
    list.replaceChildren();
    for(const [index,repository] of (response.items||[]).entries()){
      const sync=sharing[index];
      const box=el('article');box.className='card';box.append(el('h3',repository.root));
      for(const [label,value] of [['Branch',repository.branch||'No branch'],['Commit',repository.head_commit||'No commits'],['Working tree at last check',repository.dirty?'Changes present':'Clean'],['Source tracking',repository.source_state],['Shared research synchronization',sync?.state||repository.sync_state]])box.append(el('p',label+': '+value));
      if(repository.latest_update){const update=repository.latest_update,changes=update.changes;box.append(el('p','Last source update: '+update.created_at));if(changes.changed_paths.length){const paths=el('ul');for(const path of changes.changed_paths)paths.append(el('li',path));box.append(paths)}if(changes.omitted_paths)box.append(el('p',changes.omitted_paths+' additional paths retained in update history.'));if(changes.incomplete)box.append(el('p','Change comparison has gaps; earlier evidence remains available.'))}
      if(repository.last_error)box.append(el('p','Source check: '+repository.last_error));
      const refresh=el('button','Check Git changes now');refresh.type='button';refresh.onclick=async()=>{refresh.disabled=true;try{await api('/v1/repository-refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,repository_id:repository.id})});await load()}catch(error){notice('Source refresh could not be confirmed: '+error.message,'error')}finally{refresh.disabled=false}};box.append(refresh);
      if(sync)sharingControls(box,project,repository.id,sync);
      list.append(box);
    }
    if(!(response.items||[]).length)list.append(el('p','No repository is connected to this project.'));
  }
  function syncPost(action,project,identity,extra={}){return api('/v1/git-sync-'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,repository_id:identity,...extra})})}
  function sharingControls(box,project,identity,sync){
    box.append(el('p','Git sharing sends portable tasks and research to the connected project data branch. Source bytes, local paths and live claims stay local. Automatic checks use a five-minute interval and wait for editing to pause.'));
    if(sync.error)box.append(el('p',sync.error));
    if(sync.result?.missing_sources?.length)box.append(el('p',sync.result.missing_sources.length+' pinned sources have no bytes on this machine.'));
    const toggle=el('button',sync.enabled?'Turn Git sharing off':'Enable Git sharing');toggle.type='button';
    toggle.onclick=async()=>{toggle.disabled=true;try{await syncPost('configure',project,identity,{enabled:!sync.enabled,export_selected:!sync.enabled});await load()}catch(error){notice('Sharing setup was not confirmed: '+error.message,'error')}finally{toggle.disabled=false}};box.append(toggle);
    if(!sync.enabled)return;
    const now=el('button','Sync now');now.type='button';now.onclick=async()=>{now.disabled=true;try{await syncPost('now',project,identity);await load();notice('Git synchronization queued. Its status updates here.')}catch(error){notice(error.message,'error')}finally{now.disabled=false}};box.append(now);
    const review=el('button','Review incoming Git changes');review.type='button';review.onclick=async()=>{
      if(applying||uncertain||reviewing)return;reviewing=true;sequence++;lock(true);onDirty();review.disabled=true;
      try{
        const response=await syncPost('preview',project,identity);
        if(getProjectId()!==project)return;
        const editor=el('form');editor.className='git-merge-review';editor.append(el('h4','Review shared changes'),el('p','Choose each conflicting value. Compatible changes will combine. A changed project or remote requires a new preview.'));
        const selections=[];
        for(const change of response.preview.changes||[])for(const [field,alternative] of Object.entries(change.fields||{}))if(alternative.conflict){
          const key=JSON.stringify([change.table,change.key,field]),label=el('label',change.table+' · '+field);label.className='field';
          const values=el('pre','Local: '+(alternative.local_present?JSON.stringify(alternative.local):'(absent)')+'\nShared: '+(alternative.remote_present?JSON.stringify(alternative.remote):'(absent)'));values.style.whiteSpace='pre-wrap';
          const select=el('select');select.required=true;for(const [value,text] of [['','Choose a value'],['local','Keep local'],['remote','Use shared']]){const option=el('option',text);option.value=value;select.append(option)}
          label.append(values,select);editor.append(label);selections.push([key,select]);
        }
        if(!selections.length)editor.append(el('p','There are no conflicting fields. Review and apply the compatible incoming records.'));
        const save=el('button','Apply reviewed changes');save.type='submit';editor.append(save);box.append(editor);
        let payload=null;
        editor.onsubmit=async event=>{event.preventDefault();if(applying)return;applying=true;save.disabled=true;
          try{
            if(!payload)payload={choices:conflictChoices(response.preview,Object.fromEntries(selections.map(([key,input])=>[key,input.value]))),remote_tip:response.tip,bundle_id:response.bundle_id,expected_revision:response.local_revision};
            await syncPost('apply',project,identity,payload);uncertain=false;reviewing=false;lock(false);await onConnected(project);notice('Reviewed changes applied; Git synchronization queued.');
          }catch(error){uncertain=!error.status||error.status>=500;notice(uncertain?'Apply outcome is uncertain. Retry keeps the same reviewed choices and revision.':'Changes were not applied: '+error.message+'; discard this preview and review again.','error')}
          finally{applying=false;save.disabled=false;save.textContent=uncertain?'Retry same reviewed changes':'Apply reviewed changes'}
        };
      }catch(error){reviewing=false;lock(false);await onConnected(project);notice('Incoming review unavailable: '+error.message,'error')}
      finally{review.disabled=false}
    };box.append(review);
  }
  form.addEventListener('input',()=>{if(!applying&&!uncertain){sequence++;preview.replaceChildren()}});
  form.onsubmit=async event=>{
    event.preventDefault();if(applying||uncertain){notice('Confirm the pending connection outcome before starting another setup.','error');return}
    const draft=Object.fromEntries(new FormData(form)),project=getProjectId(),mine=++sequence;
    lock(true);preview.textContent='Inspecting repository and preparing setup preview…';
    try{
      const response=await post(setupPayload(draft.root,false,null,draft.name));
      if(mine!==sequence||project!==getProjectId())return;
      const manifest=JSON.parse(response.files['.research-workspace/project.json']);
      const body=setupPayload(draft.root,true,manifest,draft.name);
      preview.replaceChildren(el('h3','Review repository setup'),el('p','Project '+manifest.name+' · '+manifest.project_id));
      for(const [path,text] of Object.entries(response.files)){
        const details=el('details'),heading=el('summary',path),source=el('pre',text);source.className='docbody';details.append(heading,source);preview.append(details);
      }
      preview.append(el('p','Apply registers this connection and creates these project files. It does not commit or push them to Git.'));
      const status=el('p'),apply=el('button','Apply repository connection');apply.type='button';preview.append(status,apply);
      apply.onclick=async()=>{
        if(applying)return;applying=true;apply.disabled=true;lock(true);status.textContent='Connecting repository…';
        try{
          const result=await post(body);uncertain=false;applying=false;lock(false);preview.replaceChildren();form.reset();
          try{await onConnected(result.project_id);notice('Repository connected. Git synchronization status is shown below.','ok')}catch(error){notice('Repository connected, but the workspace view could not refresh. Reload to see its project: '+error.message,'error')}
        }catch(error){
          uncertain=!error.status||error.status>=500;
          status.textContent=uncertain?'Connection outcome is uncertain. Retry uses the same project identity and setup payload.':'Connection was not confirmed: '+error.message;
          apply.textContent=uncertain?'Retry same repository connection':'Try connection again';
          if(!uncertain)lock(false);
        }finally{applying=false;apply.disabled=false}
      };
      onDirty();
    }catch(error){if(mine===sequence&&project===getProjectId())preview.textContent='Setup preview unavailable: '+error.message}
    finally{if(!applying&&!uncertain)lock(false)}
  };
  return {load,discard(){if(applying||uncertain){notice('Retry or confirm the pending repository connection before discarding its outcome.','error');return false}sequence++;reviewing=false;for(const editor of list.querySelectorAll('.git-merge-review'))editor.remove();preview.replaceChildren();lock(false);return true}};
}
const model={setupPayload,conflictChoices,create};root.WorkspaceRepositories=model;
if(typeof module!=='undefined'&&module.exports)module.exports=model;
})(typeof globalThis!=='undefined'?globalThis:this);
