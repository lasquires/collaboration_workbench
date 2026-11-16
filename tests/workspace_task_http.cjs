'use strict';
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const {resolve}=require('node:path');
const workflows=require('../docs/workspace-task-workflows.js');
global.WorkspaceTaskSession=require('../docs/workspace-task-session.js');

class Node {
 constructor(tag,doc){this.tagName=tag;this.doc=doc;this.children=[];this.dataset={};this.value='';this.textContent='';this.disabled=false;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 setAttribute(k,v){this[k]=v;}
 setCustomValidity(v){this.validationMessage=v;}
 reportValidity(){return true;}
 focus(){this.doc.activeElement=this;}
 setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
 querySelectorAll(selector){const key=selector==='[data-task-field]'?'taskField':'other';return this.children.flatMap(n=>[...(n.dataset[key]?[n]:[]),...n.querySelectorAll(selector)]);}
}
const all=n=>[n,...n.children.flatMap(all)];
const py=process.env.WORKBENCH_TEST_PYTHON||(process.platform==='win32'?'python':'python3');
const child=spawn(py,['-B','-X','utf8','tests/task_http_fixture.py'],{cwd:resolve(__dirname,'..'),stdio:['pipe','pipe','inherit']});
const lines=readline.createInterface({input:child.stdout});
function fixtureConfig(){return new Promise((resolveConfig,reject)=>{
 const timer=setTimeout(()=>finish(Error('HTTP fixture startup timed out')),10000);
 function finish(error,value){clearTimeout(timer);lines.removeListener('line',onLine);child.removeListener('error',onError);child.removeListener('exit',onExit);error?reject(error):resolveConfig(value);}
 function onLine(line){try{finish(null,JSON.parse(line));}catch(e){finish(Error('HTTP fixture emitted invalid configuration JSON: '+e.message));}}
 function onError(e){finish(Error('HTTP fixture could not start: '+e.message));}
 function onExit(code){finish(Error(`HTTP fixture exited before startup (code ${code})`));}
 lines.once('line',onLine);child.once('error',onError);child.once('exit',onExit);
});}
async function main(){
 const config=await fixtureConfig();
 const actor={name:'reviewer'};
 const doc={activeElement:null,createElement(tag){return new Node(tag,this);}};
 const mount=new Node('main',doc),stored=new Map();
 async function api(path,options={}){
  const response=await fetch(config.base+path,{...options,headers:{...(options.headers||{}),Authorization:'Bearer '+config.tokens[actor.name]}});
  const data=await response.json();if(!response.ok){const e=Error(data.error||data.message||`HTTP ${response.status}`);e.status=response.status;e.data=data;throw e;}return data;
 }
 async function postRaw(path,body){return fetch(config.base+path,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+config.tokens[actor.name]},body:JSON.stringify(body)});}
 const context={projectId:config.project,principalId:config.principals.reviewer,taskId:config.parent,activePanel:'task-workflows'};
 const renderer=workflows.create({mount,document:doc,getContext:()=>context,storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api});
 const btn=(label)=>all(mount).find(n=>n.tagName==='button'&&n.textContent===label);
 async function act(label){const b=btn(label);assert.ok(b,`missing ${label}`);assert.equal(b.disabled,false,`${label} unexpectedly disabled`);await b.onclick();}
 function formValue(label,value){const input=all(mount).find(n=>n['aria-label']===label);assert.ok(input,`missing field ${label}; have ${all(mount).map(n=>n['aria-label']).filter(Boolean).join(',')}`);input.value=value;input.oninput();}
 async function submit(){const form=all(mount).find(n=>n.tagName==='form');assert.ok(form,'task editor form visible');await form.onsubmit({preventDefault(){}});}
 try {
  await renderer.load();
  assert.equal(btn('Complete task').disabled,true,'pending required child prevents parent acceptance at the UI');
  const initialParent=await api(`/v1/tasks/${config.parent}/context?project_id=${config.project}`);
  const blocked=await postRaw('/v1/transactions',{project_id:config.project,actor:{principal_id:config.principals.reviewer},expected_revision:initialParent.revision,request_id:require('node:crypto').randomUUID(),operations:[{op:'complete_task',task_id:config.parent,expected_record_revision:initialParent.context.task.record_revision,rationale:'Probe blocked parent acceptance'}]});
  assert.equal(blocked.status,409,'shared HTTP transaction rejects parent completion while a required child is incomplete');
  assert.equal((await api(`/v1/tasks/${config.parent}/context?project_id=${config.project}`)).revision,initialParent.revision,'rejected parent completion leaves the workspace revision unchanged');
  // The required child is rendered from the server's task-children collection.
  await btn('Subtasks').onclick();
  const childLink=all(mount).find(n=>n.tagName==='a'&&n.textContent==='Open subtask workflow');
  assert.ok(childLink,'required child is rendered from the real HTTP collection');
  // The public renderer keeps its session private; reload in a child-scoped surface.
  const childMount=new Node('main',doc),childContext={...context,taskId:config.child};
  const childRenderer=workflows.create({mount:childMount,document:doc,getContext:()=>childContext,storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api});
  const childAll=()=>all(childMount),childBtn=label=>childAll().find(n=>n.tagName==='button'&&n.textContent===label);
  await childRenderer.load();
  assert.equal(childBtn('Complete task').disabled,true,'an unvalidated child cannot complete');
  await childBtn('Record independent validation').onclick();
  const field=(label,value)=>{const i=childAll().find(n=>n['aria-label']===label);assert.ok(i,`missing ${label}; have ${childAll().map(n=>n['aria-label']).filter(Boolean).join(',')}`);i.value=value;i.oninput();};
  field('Independent result','passed');field('Inputs','{}');field('Outputs','{"fixture":"http"}');field('Pinned validation sources',JSON.stringify([{artifact_version_id:config.evidence,method:'real HTTP fixture',limitations:'Workflow only.'}]));field('Rationale','Validate required child');
  await childAll().find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
  await childRenderer.load();await childBtn('Complete task').onclick();
  const completeField=childAll().find(n=>n['aria-label']==='Rationale');completeField.value='Explicitly complete validated child';completeField.oninput();await childAll().find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
  let childCtx=await api(`/v1/tasks/${config.child}/context?project_id=${config.project}`);
  assert.equal(childCtx.context.task.status,'done',`child completion requires and records its explicit gate; session=${JSON.stringify(childRenderer.state())}`);
  await renderer.load();
  assert.equal(btn('Complete task').disabled,true,'completed child does not substitute for independent parent gate');
  await btn('Overview').onclick();
  await act('Declare task scope');
  formValue('Scope completeness','true');formValue('Rationale','Confirm parent decomposition is complete');await submit();
  await act('Record independent validation');
  // Fill the actual parent gate form and submit through the renderer/session.
  formValue('Independent result','passed');formValue('Inputs','{}');formValue('Outputs','{"fixture":"http"}');formValue('Pinned validation sources',JSON.stringify([{artifact_version_id:config.evidence,method:'real HTTP fixture',limitations:'Workflow only.'}]));formValue('Rationale','Validate parent independently');await submit();
  await renderer.load();await act('Complete task');formValue('Rationale','Explicitly complete parent after its own gate');await submit();
  const parentCtx=await api(`/v1/tasks/${config.parent}/context?project_id=${config.project}`);
  assert.equal(parentCtx.context.task.status,'done',`explicit parent completion succeeds after its own current gate; ui=${renderer.state().phase}:${renderer.state().error}`);
  assert.equal((await api(`/v1/tasks/${config.child}/context?project_id=${config.project}`)).context.task.status,'done','parent completion does not implicitly alter child status');

  // Claim ownership and reservation conflicts use the actual authenticated transaction endpoint.
  actor.name='reviewer';context.taskId=config.claim_task;
  const claimMount=new Node('main',doc),claimContext={...context,taskId:config.claim_task};
  const claimRenderer=workflows.create({mount:claimMount,document:doc,getContext:()=>claimContext,storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api});
  const claimAll=()=>all(claimMount),claimBtn=label=>claimAll().find(n=>n.tagName==='button'&&n.textContent===label);
  await claimRenderer.load();await claimBtn('Claim this task').onclick();
  const claimField=(label,value)=>{const i=claimAll().find(n=>n['aria-label']===label);assert.ok(i,`missing ${label}`);i.value=value;i.oninput();};
  claimField('Reserved paths','[{"path":"owned/tree","kind":"tree"}]');claimField('Lease seconds','900');claimField('Rationale','Reserve integration fixture paths');await claimAll().find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
  actor.name='alternate';
  const altMount=new Node('main',doc),altContext={...context,principalId:config.principals.alternate,taskId:config.claim_task};
  const altRenderer=workflows.create({mount:altMount,document:doc,getContext:()=>altContext,storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api});
  const altAll=()=>all(altMount),altBtn=label=>altAll().find(n=>n.tagName==='button'&&n.textContent===label);
  await altRenderer.load();assert.equal(altBtn('Claim this task').disabled,true,'another principal cannot overwrite a protected claim');
  assert.equal(altBtn('Release claim').disabled,true,'wrong principal cannot silently release a claim');
  const claimBefore=await api(`/v1/tasks/${config.claim_task}/context?project_id=${config.project}`);
  assert.equal(claimBefore.context.progress.claim.principal_id,config.principals.reviewer);
  const overlapMount=new Node('main',doc),overlapContext={...altContext,taskId:config.overlap_task};
  const overlapRenderer=workflows.create({mount:overlapMount,document:doc,getContext:()=>overlapContext,storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api});
  const overlapAll=()=>all(overlapMount),overlapBtn=label=>overlapAll().find(n=>n.tagName==='button'&&n.textContent===label);
  await overlapRenderer.load();await overlapBtn('Claim this task').onclick();
  const overlapField=(label,value)=>{const i=overlapAll().find(n=>n['aria-label']===label);assert.ok(i,`missing ${label}`);i.value=value;i.oninput();};
  const requestedPaths=[{path:'owned/tree/child.js',kind:'file'}];
  overlapField('Reserved paths',JSON.stringify(requestedPaths));overlapField('Lease seconds','900');overlapField('Rationale','Attempt overlapping reservation on independent task');await overlapAll().find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
  assert.ok(overlapRenderer.state().draft,'server rejection retains the authored claim draft');assert.equal(overlapRenderer.state().phase,'rejected');
  assert.deepEqual(overlapRenderer.state().draft.fields.reserved_paths,requestedPaths,'rejection preserves the exact requested overlapping paths');
  const claimAfterConflict=await api(`/v1/tasks/${config.claim_task}/context?project_id=${config.project}`);
  assert.equal(claimAfterConflict.context.progress.claim.principal_id,config.principals.reviewer,'overlap rejection retains the original claim owner');
  assert.deepEqual(claimAfterConflict.context.reservations,claimBefore.context.reservations,'overlap rejection retains the original reservation');
  overlapRenderer.discard();
  actor.name='reviewer';await claimRenderer.load();await claimBtn('Hand off claim').onclick();
  claimField('Recipient principal',config.principals.alternate);claimField('Lease seconds','900');claimField('Rationale','Explicitly hand off protected work');await claimAll().find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
  const handoffCtx=await api(`/v1/tasks/${config.claim_task}/context?project_id=${config.project}`);
  assert.equal(handoffCtx.context.progress.claim.principal_id,config.principals.alternate,'explicit handoff updates server claim ownership');
  actor.name='alternate';await altRenderer.load();assert.equal(altBtn('Release claim').disabled,false,'explicit handoff transfers ownership');await altBtn('Release claim').onclick();
  const releaseForm=altAll().find(n=>n.tagName==='form');assert.ok(releaseForm,'release requires an explicit rationale form');
  const releaseRationale=altAll().find(n=>n['aria-label']==='Rationale');releaseRationale.value='Explicitly release handed-off work';releaseRationale.oninput();await releaseForm.onsubmit({preventDefault(){}});
  const released=await api(`/v1/tasks/${config.claim_task}/context?project_id=${config.project}`);
  assert.equal(released.context.progress.claim.state,'unclaimed','explicit release clears the server claim');
  assert.deepEqual(released.context.reservations,[],'explicit release clears path reservations');
  console.log('workspace task real HTTP integration checks passed');
 } finally {child.stdin.end('\n');await new Promise(resolveExit=>child.exitCode!==null?resolveExit():child.once('exit',resolveExit));}
}
main().catch(e=>{console.error(e);child.kill();process.exitCode=1;});
