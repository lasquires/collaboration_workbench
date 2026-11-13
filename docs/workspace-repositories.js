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
function create(options){
  const {api,notice,getProjectId,onConnected,onDirty}=options;
  const form=document.querySelector('#repositoryForm'),preview=document.querySelector('#repositoryPreview'),list=document.querySelector('#repositoriesList');
  let sequence=0,applying=false,uncertain=false;
  function el(tag,text){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node}
  function lock(value){for(const input of form.querySelectorAll('input,button'))input.disabled=value}
  function post(payload){return api('/v1/repository-setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})}
  async function load(){
    const project=getProjectId();
    const response=await api('/v1/repositories?'+new URLSearchParams({project_id:project,limit:'100'}));
    if(getProjectId()!==project)return;
    list.replaceChildren();
    for(const repository of response.items||[]){
      const box=el('article');box.className='card';box.append(el('h3',repository.root));
      for(const [label,value] of [['Branch',repository.branch||'No branch'],['Commit',repository.head_commit||'No commits'],['Working tree at last check',repository.dirty?'Changes present':'Clean'],['Synchronization',repository.sync_state]])box.append(el('p',label+': '+value));
      list.append(box);
    }
    if(!(response.items||[]).length)list.append(el('p','No repository is connected to this project.'));
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
  return {load,discard(){if(applying||uncertain){notice('Retry or confirm the pending repository connection before discarding its outcome.','error');return false}sequence++;preview.replaceChildren();lock(false);return true}};
}
const model={setupPayload,create};root.WorkspaceRepositories=model;
if(typeof module!=='undefined'&&module.exports)module.exports=model;
})(typeof globalThis!=='undefined'?globalThis:this);
