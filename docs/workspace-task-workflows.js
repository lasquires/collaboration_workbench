/* Typed task edits keep workflow acceptance distinct from forensic proof. */
(function(root){
'use strict';
const clone=value=>JSON.parse(JSON.stringify(value));
const schemas={
 set_task_scope:['area_id','decomposition_complete'],move_task:['parent_id','required_for_parent','expected_parents'],attach_child:['parent_id','required_for_parent','expected_parents'],detach_child:['expected_parents'],
 archive_task:['expected_parents'],reopen_task:[],claim_task:['reserved_paths','lease_seconds'],heartbeat_claim:['lease_seconds'],handoff_claim:['to_principal_id','lease_seconds'],release_claim:[],
 record_attempt:['boundary_id','outcome','evidence'],resolve_attempt:['attempt_id'],record_validation:['outcome','inputs','outputs','evidence'],complete_task:[],record_blocker:['kind','target_id'],resolve_blocker:['blocker_id'],
 merge_tasks:['absorbed_id','preview_guard','choices','metadata_choices','source_references'],reverse_merge:['merge_id','preview_guard'],compensate_merge:['merge_id','preview_guard','choices','source_references']
};
function required(value,label){if(typeof value!=='string'||!value.trim())throw Error(label+' is required.');return value;}
function object(value){return value&&typeof value==='object'&&!Array.isArray(value);}
function operation(kind,context,fields={}){
 if(!Object.hasOwn(schemas,kind))throw Error('Unknown task operation.');
 const task=context?.task;if(!task||!Number.isSafeInteger(task.record_revision)||task.record_revision<1)throw Error('Read current task identity and revision first.');
 required(task.id,'Task identity');required(task.project_id,'Project identity');
 if(fields.task_id!==undefined&&fields.task_id!==task.id)throw Error('Task target identity cannot change.');
 if(fields.expected_record_revision!==undefined&&fields.expected_record_revision!==task.record_revision)throw Error('Read and explicitly reconcile the changed record guard.');
 if(fields.op!==undefined&&fields.op!==kind)throw Error('Recovered operation kind does not match the draft.');
 const allowed=new Set([...schemas[kind],'op','task_id','expected_record_revision','rationale']);
 if(Object.keys(fields).some(key=>!allowed.has(key)))throw Error('Task operation contains unsupported fields.');
 const op={op:kind,task_id:task.id,expected_record_revision:task.record_revision,rationale:required(fields.rationale,'Operation rationale')};
 for(const key of schemas[kind])if(Object.hasOwn(fields,key))op[key]=clone(fields[key]);
 if(['move_task','attach_child','detach_child','archive_task'].includes(kind)&&!object(op.expected_parents))throw Error('Read and review the affected parent structure guards first.');
 if(['move_task','attach_child'].includes(kind)){required(op.parent_id,'Destination parent');if(typeof op.required_for_parent!=='boolean')throw Error('Choose explicitly whether the new parent requires this task.');}
 if(['record_attempt','record_validation'].includes(kind)&&(!Array.isArray(op.evidence)||!op.evidence.length))throw Error('Recorded evidence is required.');
 if(kind==='record_attempt'){
  required(op.boundary_id,'Attempt boundary');if(!['failed','passed','blocked','inconclusive'].includes(op.outcome))throw Error('Choose an attempt outcome.');
 }
 if(kind==='record_validation'){
  if(!['passed','failed'].includes(op.outcome)||!object(op.inputs)||!object(op.outputs))throw Error('Independent validation requires outcome, inputs and outputs.');
  for(const source of op.evidence){required(source?.artifact_version_id,'Pinned artifact version');required(source?.method,'Validation method');required(source?.limitations,'Validation limitations');}
 }
 if(kind==='complete_task'&&(context.acceptance_state!=='passed'||context.validation?.current!==true||context.validation.outcome!=='passed'||context.validation.gate_revision!==context.gate_revision))throw Error('Completion requires this task’s current independent validation gate.');
 if(kind==='record_blocker'){
  if(!['question','prerequisite','review'].includes(op.kind))throw Error('Choose a typed blocker.');
  if(op.kind!=='review')required(op.target_id,'Blocker target');else if(op.target_id==='')op.target_id=null;
 }
 for(const key of ['attempt_id','blocker_id','to_principal_id','absorbed_id','merge_id','preview_guard'])if(schemas[kind].includes(key))required(op[key],key);
 if(kind==='set_task_scope'&&!Object.hasOwn(op,'area_id')&&!Object.hasOwn(op,'decomposition_complete'))throw Error('Choose the task scope change explicitly.');
 if(Object.hasOwn(op,'decomposition_complete')&&typeof op.decomposition_complete!=='boolean')throw Error('Scope completeness must be explicit.');
 if(Object.hasOwn(op,'lease_seconds')&&(!Number.isSafeInteger(op.lease_seconds)||op.lease_seconds<60||op.lease_seconds>86400))throw Error('Lease duration must be60–86400seconds.');
 if(['merge_tasks','compensate_merge'].includes(kind)&&(!Array.isArray(op.source_references)||!op.source_references.length))throw Error('Merge decisions require reviewed source references.');
 return op;
}

function mergeDecision(kind,context,preview,fields){
 if(!['merge_tasks','reverse_merge','compensate_merge'].includes(kind)||preview?.survivor_id!==context?.task?.id)throw Error('Read the exact selected survivor preview first.');
 if(preview.guards?.[context.task.id]?.record_revision!==undefined&&preview.guards[context.task.id].record_revision!==context.task.record_revision)throw Error('Selected task revision changed since the preview.');
 required(preview.guard_digest,'Current preview guard');
 const identityKey=kind==='merge_tasks'?'absorbed_id':'merge_id';if(fields[identityKey]!==undefined&&fields[identityKey]!==preview[identityKey])throw Error('Displayed target identity changed; read its exact preview again.');
 function choices(values,keys,options){
  if(!object(values)||Object.keys(values).length!==keys.length||keys.some(key=>!Object.hasOwn(values,key)||!options.includes(values[key])))throw Error('Choose explicitly for every preview conflict or action.');return clone(values);
 }
 const next={...fields,preview_guard:preview.guard_digest};
 if(kind==='reverse_merge'){
  if(!preview.can_reverse)throw Error('Intervening edits require compensation; this merge cannot reverse safely.');
  next.merge_id=required(preview.merge_id,'Merge identity');
 }else{
  if(!preview.apply_available)throw Error('Resolve the preview problems before applying this decision.');
  if(!Array.isArray(fields.source_references)||!fields.source_references.length||fields.source_references.length>100)throw Error('Record reviewed merge source references.');
  fields.source_references.forEach(value=>required(value,'Merge source reference'));
  if(kind==='merge_tasks'){
   next.absorbed_id=required(preview.absorbed_id,'Absorbed task');
   next.choices=choices(fields.choices,Object.keys(preview.field_conflicts||{}),['survivor','absorbed']);
   next.metadata_choices=choices(fields.metadata_choices,Object.keys(preview.metadata_conflicts||{}),['survivor','absorbed']);
  }else{
   next.merge_id=required(preview.merge_id,'Merge identity');
   next.choices=choices(fields.choices,(preview.actions||[]).map(row=>row.key),['original','current']);
  }
 }
 return operation(kind,context,next);
}
function affectedParentIds(context,destination=null){
 const ids=(context.ancestors||[]).map(row=>row.id);
 if(destination){
  if(destination.task?.project_id!==context.task.project_id)throw Error('Destination scope belongs to another project.');
  const next=[destination.task.id,...(destination.ancestors||[]).map(row=>row.id)];
  if(next.includes(context.task.id))throw Error('Destination creates a containment cycle.');
  ids.push(...next);
 }
 const unique=Array.from(new Set(ids));if(unique.length>200)throw Error('Review this large parent scope through the agent API before moving it.');return unique;
}
function parentGuardMap(project,revision,ids,responses){
 const guards={};for(const result of responses){const task=result?.context?.task;
  if(result?.project_id!==project||result.revision!==revision||task?.project_id!==project||!ids.includes(task.id)||result.redirect||!Number.isSafeInteger(task.structure_revision)||task.structure_revision<1)throw Error('Parent scope snapshot or revision changed; reread before reviewing guards.');
  guards[task.id]=task.structure_revision;
 }
 if(ids.some(id=>!Object.hasOwn(guards,id)))throw Error('Read every affected parent structure guard.');return guards;
}
const formDefinitions={
 merge_tasks:[['absorbed_id','Incoming task UUID'],['source_references','Reviewed source references','json']],reverse_merge:[['merge_id','Merge identity']],compensate_merge:[['merge_id','Merge identity'],['source_references','Reviewed source references','json']],
 record_attempt:[['boundary_id','Attempt boundary'],['outcome','Outcome',['failed','passed','blocked','inconclusive']],['evidence','Evidence references','json']],
 resolve_attempt:[['attempt_id','Attempt identity']],
 record_blocker:[['kind','Blocker type',['question','prerequisite','review']],['target_id','Target identity']],
 resolve_blocker:[['blocker_id','Blocker identity']],
 record_validation:[['outcome','Independent result',['passed','failed']],['inputs','Inputs','json'],['outputs','Outputs','json'],['evidence','Pinned validation sources','json']],
 move_task:[['parent_id','Destination parent UUID'],['required_for_parent','Required by new parent',['true','false']]],detach_child:[],archive_task:[],
 complete_task:[],set_task_scope:[['area_id','Area identity, blank clears'],['decomposition_complete','Scope completeness',['true','false']]],
 reopen_task:[],claim_task:[['reserved_paths','Reserved paths','json'],['lease_seconds','Lease seconds','number']],heartbeat_claim:[['lease_seconds','Lease seconds','number']],handoff_claim:[['to_principal_id','Recipient principal'],['lease_seconds','Lease seconds','number']],release_claim:[]
};
const labels={merge_tasks:'Review task merge',reverse_merge:'Review merge reversal',compensate_merge:'Review merge compensation',move_task:'Move task',detach_child:'Detach task',archive_task:'Archive task',record_attempt:'Record an attempt',record_blocker:'Record a blocker',record_validation:'Record independent validation',complete_task:'Complete task',set_task_scope:'Declare task scope',reopen_task:'Reopen task',claim_task:'Claim this task',heartbeat_claim:'Renew local/service claim',handoff_claim:'Hand off claim',release_claim:'Release claim'};
function canAct(kind,state,context){
 const granted=cap=>state.capabilities?.authenticated!==true||state.capabilities.granted?.includes(cap)===true;
 if(!granted('contribute'))return false;
 if(context?.task?.archived_at&&!['reopen_task','resolve_attempt','release_claim'].includes(kind))return false;
 if(context?.task?.status==='done'&&['move_task','attach_child','detach_child','archive_task','set_task_scope'].includes(kind))return false;
 const claim=context?.progress?.claim||context?.task?.progress?.claim;
 const protectedClaim=claim&&claim.state!=='unclaimed'&&claim.principal_id;
 if(kind==='record_validation')return granted('review')&&context?.task?.created_by!==state.principalId&&(!protectedClaim||claim.principal_id!==state.principalId);
 if(kind==='handoff_claim')return Boolean(protectedClaim)&&granted('manage_claims');
 if(['heartbeat_claim','release_claim'].includes(kind))return Boolean(protectedClaim)&&(claim.principal_id===state.principalId||granted('manage_claims'));
 if(kind==='claim_task')return !protectedClaim;
 if(kind==='complete_task')return granted('review')&&context?.acceptance_state==='passed'&&context?.task?.status!=='done';
 return true;
}
function create({api,getContext,onDirty=()=>{},notice=()=>{},mount=null,storage,document:doc=root.document}={}){
 const host=mount||doc?.querySelector('#taskWorkspace');if(!host)throw Error('Task workflow mount is required.');
 const Session=root.WorkspaceTaskSession||(typeof require==='function'?require('./workspace-task-session.js'):null);
 let session,error='',resource='attempts',activeSection='overview',helpFilter='',invalid=new Map(),readingScopes=false,mergePreview=null,previewPage=0,lastMergeId=null,sectionPages={};
 const node=(tag,value)=>{const n=doc.createElement(tag);if(value!==undefined)n.textContent=String(value);return n;};
 async function run(fn){try{error='';await fn();}catch(e){error=e.message;notice(error,'error');}render(session.state());}
 function button(label,fn,disabled=false){const b=node('button',label);b.type='button';b.disabled=disabled;b.onclick=()=>run(fn);return b;}
 function detail(parent,label,value){const box=node('details'),title=node('summary',label);box.append(title,node('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));parent.append(box);}
 function contextOf(state){return state.context?.context||state.context;}
 function allowed(state,cap){return state.capabilities?.authenticated!==true||state.capabilities.granted?.includes(cap);}
 function start(kind,seed={}){
  const state=session.state(),context=contextOf(state);invalid.clear();mergePreview=null;previewPage=0;
  const initial={expected_record_revision:context.task.record_revision,rationale:'',...seed};
  if(kind==='merge_tasks')Object.assign(initial,{absorbed_id:'',choices:{},metadata_choices:{},source_references:[]});
  if(['reverse_merge','compensate_merge'].includes(kind))initial.merge_id=lastMergeId||'';
  if(kind==='compensate_merge')Object.assign(initial,{choices:{},source_references:[]});
  if(kind==='record_attempt')Object.assign(initial,{boundary_id:'',outcome:'',evidence:[]});
  if(kind==='record_blocker')Object.assign(initial,{kind:'',target_id:''});
  if(kind==='record_validation')Object.assign(initial,{outcome:'',inputs:{},outputs:{},evidence:[]});
  if(['claim_task','heartbeat_claim','handoff_claim'].includes(kind))initial.lease_seconds=900;
  if(kind==='claim_task')initial.reserved_paths=[];
  session.edit(kind,initial);
 }
 function field(parent,key,label,type,value,onSave=null){
  const wrap=node('label'),caption=node('span',label);wrap.className='field';let input;
  if(Array.isArray(type)){input=node('select');const empty=node('option','Choose explicitly…');empty.value='';input.append(empty);for(const choice of type){const o=node('option',choice);o.value=choice;input.append(o);}}
  else{input=node('textarea');if(type==='number'){input=node('input');input.type='number';}}
  input.setAttribute('aria-label',label);input.dataset.taskField=key;input.value=type==='json'?JSON.stringify(value??{},null,2):value??'';
  if(invalid.has(key)){input.value=invalid.get(key).value;input.setCustomValidity(invalid.get(key).error);}
  input.oninput=()=>{try{let next=type==='json'?JSON.parse(input.value):type==='number'?Number(input.value):input.value;if(['decomposition_complete','required_for_parent'].includes(key)){if(!['true','false'].includes(next))throw Error('Choose complete or incomplete scope.');next=next==='true';}if(key==='area_id'&&next==='')next=null;invalid.delete(key);if(onSave)onSave(next);else session.change(key,next);if(['absorbed_id','merge_id'].includes(key))mergePreview=null;if(key==='parent_id')session.change('expected_parents',null);}catch(e){invalid.set(key,{value:input.value,error:e.message});input.setCustomValidity(e.message);error=e.message;}};
  wrap.append(caption,input);parent.append(wrap);
 }
 function draftOperation(state,latest=false){const fields=clone(state.draft.fields),context=clone(contextOf(state));if(latest)delete fields.expected_record_revision;else if(fields.expected_record_revision)context.task.record_revision=fields.expected_record_revision;if(['merge_tasks','reverse_merge','compensate_merge'].includes(state.draft.kind)){if(!mergePreview||mergePreview.kind!==state.draft.kind)throw Error('Read the current merge preview before reviewing this action.');return mergeDecision(state.draft.kind,context,mergePreview.value,fields);}return operation(state.draft.kind,context,fields);}
 async function readParentScopes(){
  const state=session.state(),draft=state.draft,context=contextOf(state);
  if(readingScopes||state.busy||state.pendingBody||!draft)throw Error('Finish the active task action before reading parent scopes.');
  const moving=draft.kind==='move_task'||draft.kind==='attach_child',target=moving?required(draft.fields.parent_id,'Destination parent UUID'):null;
  const binding=JSON.stringify([getContext(),draft.kind,target,draft.fields.expected_record_revision,state.revision]);
  function current(){const now=session.state(),d=now.draft;return d&&JSON.stringify([getContext(),d.kind,moving?d.fields.parent_id:null,d.fields.expected_record_revision,now.revision])===binding&&!now.pendingBody;}
  readingScopes=true;render(session.state());
  try{
   let destination=null,destinationResponse=null;
   if(target){destinationResponse=await api('/v1/tasks/'+encodeURIComponent(target)+'/context?project_id='+encodeURIComponent(state.projectId));if(!current())throw Error('Task context changed during parent scope read.');if(destinationResponse.context?.task?.id!==target||destinationResponse.redirect)throw Error('Choose the current destination task explicitly.');destination=destinationResponse.context;}
   const ids=affectedParentIds(context,destination),responses=[];
   for(const id of ids){const response=id===target?destinationResponse:await api('/v1/tasks/'+encodeURIComponent(id)+'/context?project_id='+encodeURIComponent(state.projectId));if(!current())throw Error('Task context changed during parent scope read.');responses.push(response);}
   const guards=parentGuardMap(state.projectId,state.revision,ids,responses);if(!current())throw Error('Task context changed during parent scope read.');session.change('expected_parents',guards);
  }finally{readingScopes=false;render(session.state());}
 }
 async function readMergePreview(){
  const state=session.state(),draft=state.draft;if(readingScopes||state.busy||state.pendingBody||!draft)throw Error('Finish the active task action before reading a preview.');
  const kind=draft.kind,id=kind==='merge_tasks'?required(draft.fields.absorbed_id,'Incoming task UUID'):required(draft.fields.merge_id,'Merge identity');
  const captured=JSON.stringify([getContext(),kind,id,state.revision]);
  const query=new URLSearchParams({project_id:state.projectId});let route;
  if(kind==='merge_tasks'){route='merge-preview';query.set('survivor_id',state.taskId);query.set('absorbed_id',id);}else{route=kind==='reverse_merge'?'merge-reversal-preview':'merge-compensation-preview';query.set('merge_id',id);}
  readingScopes=true;render(session.state());
  try{
   const result=await api('/v1/tasks/'+route+'?'+query),current=session.state(),now=current.draft;
   if(!now||current.pendingBody||captured!==JSON.stringify([getContext(),now.kind,kind==='merge_tasks'?now.fields.absorbed_id:now.fields.merge_id,current.revision]))throw Error('Task context changed while reading the preview.');
   const value=result?.preview;if(result?.project_id!==state.projectId||result.revision!==state.revision||value?.survivor_id!==state.taskId||(kind==='merge_tasks'?value.absorbed_id!==id:value.merge_id!==id))throw Error('Preview identity or snapshot changed; reread selected task context.');
   mergePreview={kind,value:clone(value)};previewPage=0;sectionPages={};session.change('preview_guard',value.guard_digest);
   if(kind!=='reverse_merge')session.change('choices',{});if(kind==='merge_tasks')session.change('metadata_choices',{});
  }finally{readingScopes=false;render(session.state());}
 }
 function previewControls(form,state){
  const kind=state.draft.kind;if(!['merge_tasks','reverse_merge','compensate_merge'].includes(kind))return;
  form.append(node('p','Merging invalidates acceptance and returns affected work to review. Every conflict requires an explicit choice.'));form.append(button('Read current merge preview',readMergePreview,readingScopes||state.busy));
  if(!mergePreview||mergePreview.kind!==kind)return;const preview=mergePreview.value;
  detail(form,'Scope and preview summary',{survivor_id:preview.survivor_id,absorbed_id:preview.absorbed_id,guarded_task_count:Object.keys(preview.guards||{}).length,problem_count:preview.problems?.length||0,claim_conflict_count:preview.claim_conflicts?.length||0,apply_available:preview.apply_available,can_reverse:preview.can_reverse});
  function section(name,rows){if(!rows?.length)return;const count=Math.ceil(rows.length/20),page=Math.min(sectionPages[name]||0,count-1);detail(form,name.replaceAll('_',' ')+` (${page+1}/${count})`,rows.slice(page*20,page*20+20));form.append(button('Previous '+name.replaceAll('_',' '),()=>{sectionPages[name]=page-1;},page===0),button('Next '+name.replaceAll('_',' '),()=>{sectionPages[name]=page+1;},page===count-1));}
  section('guarded_tasks',Object.entries(preview.guards||{}).map(([id,guards])=>({id,...guards})));section('problems',preview.problems);section('claim_conflicts',preview.claim_conflicts);
  if(kind==='reverse_merge'){form.append(node('p',preview.can_reverse?'Current scope can be reversed without overwriting later edits.':'Later edits prevent safe reversal. Discard this action and review compensation instead.'));return;}
  const rows=kind==='merge_tasks'?[...Object.entries(preview.field_conflicts||{}).map(([key,value])=>({key,value,group:'choices'})),...Object.entries(preview.metadata_conflicts||{}).map(([key,value])=>({key,value,group:'metadata_choices'}))]:(preview.actions||[]).map(value=>({key:value.key,value,group:'choices'}));
  const pages=Math.max(1,Math.ceil(rows.length/20));previewPage=Math.min(previewPage,pages-1);form.append(node('p',`Preview choices page ${previewPage+1} of ${pages}. Every conflict/action requires an explicit choice.`));
  for(const row of rows.slice(previewPage*20,previewPage*20+20)){
   const scope=row.value.task_id===preview.survivor_id?'selected task':row.value.task_id===preview.absorbed_id?'incoming task':'related task';
   const label=kind==='compensate_merge'?(row.value.field||row.key)+' ('+scope+')':row.key;
   detail(form,(row.group==='metadata_choices'?'Task metadata: ':'Decision: ')+label,row.value);
   field(form,'decision-'+row.group+'-'+row.key,'Choose '+label,kind==='merge_tasks'?['survivor','absorbed']:['original','current'],state.draft.fields[row.group]?.[row.key]||'',value=>session.change(row.group,{...session.state().draft.fields[row.group],[row.key]:value}));
  }
  form.append(button('Previous preview choices',()=>{previewPage--;},previewPage===0),button('Next preview choices',()=>{previewPage++;},previewPage===pages-1));
  for(const name of ['proposed_children','proposed_links','source_union'])section(name,preview[name]);
 }
 async function save(op,retry=false){const result=retry?await session.retry():await session.submit(op);if(session.state().pendingBody){notice('Confirm recovery cleanup before continuing.','error');return result;}notice('Task action confirmed saved.','ok');lastMergeId=result.changed_records?.find(row=>row.merge_id)?.merge_id||lastMergeId;mergePreview=null;await session.load();return result;}
 function editor(parent,state){
  const d=state.draft||(state.pendingBody?{kind:JSON.parse(state.pendingBody).operations[0].op,fields:{}}:null);if(!d)return;
  const form=node('form');form.className='card';form.append(node('h3',labels[d.kind]||d.kind.replaceAll('_',' ')));
  if(state.pendingBody){detail(form,'Exact pending request',JSON.parse(state.pendingBody));form.append(button('Retry exact task request',()=>save(null,true),state.busy));parent.append(form);return;}
  for(const [key,label,type] of formDefinitions[d.kind]||[])field(form,key,label,type,d.fields[key]);
  if(['move_task','attach_child','detach_child','archive_task'].includes(d.kind)){form.append(node('p','This changes task scope and invalidates affected acceptance. Review every current parent guard first.'));detail(form,'Reviewed parent structure guards',d.fields.expected_parents||{});form.append(button('Read affected parent scopes',readParentScopes,readingScopes||state.busy));}
  previewControls(form,state);
  field(form,'rationale','Rationale',null,d.fields.rationale);
  const status=node('p',error);status.setAttribute('role','alert');form.append(status);
  if(d.needsReconcile){
   detail(form,'Current selected task context',contextOf(state));detail(form,'Retained authored action',d.fields);
   form.append(button('Read latest task context',()=>session.refreshContext(),state.busy));
   const wrap=node('label'),ack=node('input');ack.type='checkbox';ack.setAttribute('aria-label','I reviewed the current task and retained action');wrap.append(ack,node('span','I reviewed the current task and retained action. No automatic field merge.'));form.append(wrap);
   form.append(button('Adopt reviewed task context',()=>{if(!ack.checked)throw Error('Explicit current-context review is required.');if(!form.reportValidity())throw Error('Correct unfinished fields first.');return session.reconcile(draftOperation(session.state(),true),true);},state.busy));
  }
  const submit=node('button','Save task action');submit.type='submit';submit.disabled=readingScopes||state.busy||d.needsReconcile||!canAct(d.kind,state,contextOf(state));form.append(submit,button('Discard task draft',()=>session.discard(),state.busy));
  form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;return run(()=>save(draftOperation(session.state())));};parent.append(form);
 }
 function taskLink(label,state,id,panel='task-workflows',entity=null){
  const link=node('a',label),query=new URLSearchParams({panel,project_id:state.projectId,task_id:id});
  if(entity){query.set('entity_type',entity.type);query.set('entity_id',entity.id);}link.href='/?'+query;return link;
 }
 function overview(parent,state,context){
  const task=context.task;parent.append(node('h4','Overview'),node('p',task.summary||context.fields?.summary||'No task summary recorded.'));
  parent.append(node('p','Workflow status: '+(task.status||'not reported')+' · own acceptance: '+(context.acceptance_state||'unknown')));
  parent.append(node('p','Declared scope: '+(task.decomposition_complete===undefined?'not reported':task.decomposition_complete?'complete':'incomplete')+' · Area: '+(task.area_id===undefined?'not reported':task.area_id||'unassigned')));
  const claimScope=state.capabilities?.claim_scope;parent.append(node('p',claimScope==='local-only'?'Task claims coordinate this local copy. Shared-host leases are managed in Live collaboration.':'Task claims coordinate this service. Shared-host leases are managed separately in Live collaboration.'));
  parent.append(node('h4','Acceptance criteria'));for(const text of Array.isArray(context.acceptance)?context.acceptance:context.acceptance?[context.acceptance]:[])parent.append(node('p',text));
  parent.append(node('h4','Instructions'));if(!(context.instructions||[]).length)parent.append(node('p','No scoped instructions reported.'));for(const instruction of context.instructions||[]){parent.append(node('p',instruction.body||''));if(instruction.source_path)parent.append(node('small',instruction.source_path));}
  if(context.ancestors?.length){parent.append(node('h4','Parent scopes'));for(const task of context.ancestors)parent.append(taskLink((task.alias||task.id)+' · '+(task.acceptance_state||'unknown'),state,task.id));}
  if(context.dependencies?.length){parent.append(node('h4','Prerequisites'));for(const dependency of context.dependencies)parent.append(taskLink(dependency.alias||dependency.id,state,dependency.id));}
  parent.append(taskLink('Open research for this task',state,state.taskId,'research'));
  detail(parent,'Current acceptance evidence',context.validation||'No independent validation recorded.');detail(parent,'Full task context and provenance',context);
 }
 function sections(parent,state,context){
  const nav=node('nav');nav.setAttribute('aria-label','Selected task sections');nav.className='actions';
  for(const [name,title,res] of [['overview','Overview',null],['subtasks','Subtasks','task-children'],['knowledge','Knowledge','task-findings'],['questions','Questions','task-questions'],['discussion','Discussion','history'],['history','History','history']]){
   const b=button(title,()=>{activeSection=name;if(res){resource=res;return session.readPage(res,{task_id:state.taskId});}},state.busy);b.setAttribute('aria-pressed',String(activeSection===name));nav.append(b);
  }
  parent.append(nav);
  if(activeSection==='overview')overview(parent,state,context);
  else{parent.append(node('h4',activeSection[0].toUpperCase()+activeSection.slice(1)));if(activeSection==='discussion')parent.append(node('p','The task journal includes authored messages and workflow audit events. Original history and attribution are retained.'));page(parent,state);}
 }
 function page(parent,state){
  const p=state.pages?.[resource];if(!p)return;
  parent.append(node('p',`${p.items.length} ${resource} in this page. ${p.next_cursor?'More records remain.':'End of collection.'}`));
  for(const row of p.items){const card=node('article');card.className='card';card.append(node('h4',row.title||row.claim||row.text||row.boundary_id||row.kind||row.id||row.event_id));
   if(resource==='task-children'){card.append(node('p',(row.status||'unknown')+' · '+(row.required_for_parent===undefined?'scope relationship not reported':row.required_for_parent?'required by parent':'optional for parent')),taskLink('Open subtask workflow',state,row.id));}
   if(resource==='task-findings')card.append(node('p','Classification: '+row.classification+' · authored assertion, not forensic acceptance'),taskLink('Open finding',state,state.taskId,'research',{type:'finding',id:row.id}));
   if(resource==='task-questions')card.append(node('p','Question state: '+row.state),taskLink('Open question',state,state.taskId,'research',{type:'question',id:row.id}));
   if(resource==='history'){const text=row.details?.text||row.details?.event?.text;if(text)card.append(node('p',text));card.append(node('p',(row.recorded_at||'')+' · '+(row.actor?.label||row.actor?.name||'Recorded contributor')));}
   detail(card,'Recorded details',row);
   const locked=Boolean(readingScopes||state.draft||state.pendingBody||state.busy);
   if(resource==='attempts'&&row.resolution_state==='unresolved')card.append(button('Resolve this attempt',()=>start('resolve_attempt',{attempt_id:row.id}),locked));
   if(resource==='blockers'&&!row.resolved_at)card.append(button('Resolve this blocker',()=>start('resolve_blocker',{blocker_id:row.id}),locked));
   parent.append(card);
  }
  parent.append(button('Restart '+resource,()=>session.readPage(resource,{task_id:state.taskId})),button('Next 20 '+resource,()=>session.readPage(resource,{task_id:state.taskId},p.next_cursor),!p.next_cursor||state.busy));
 }
 function render(state){
  if(!state.draft)invalid.clear();const active=doc.activeElement,key=active?.dataset?.taskField,caret=key?{value:active.value,start:active.selectionStart,end:active.selectionEnd}:null;
  const box=node('div');box.className='task-workflows';box.append(node('h2','Selected task workflow'));const status=node('p',error||state.error||state.phase);status.setAttribute('role','status');box.append(status);
  if(state.selection?.redirect){const redirect=state.selection.redirect;box.append(node('p','This task was merged. Retained drafts and pending requests keep their original target.'));const link=node('a','Open surviving task');link.href='/?panel=task-workflows&project_id='+encodeURIComponent(state.projectId)+'&task_id='+encodeURIComponent(redirect.to);box.append(link);}
  const context=contextOf(state);if(context?.task){box.append(node('h3',context.task.title||context.fields?.title||context.task.id),node('p','Task acceptance: '+(context.acceptance_state||'unknown')+'. Workflow acceptance does not establish forensic behavior.'));
   sections(box,state,context);const filter=node('input');filter.type='search';filter.setAttribute('aria-label','Filter task actions and help');filter.placeholder='Find an action or help topic';filter.value=helpFilter;filter.dataset.taskField='help-search';filter.oninput=()=>{helpFilter=filter.value;render(session.state());};box.append(filter,node('p','Actions preserve history. Validation requires an independent reviewer; completion checks this task and its required children. No workflow status proves native behavior.'));const actions=node('div');actions.className='actions';const locked=Boolean(readingScopes||state.draft||state.pendingBody||state.busy)||!allowed(state,'contribute');
   for(const [kind,label] of Object.entries(labels).filter(([kind,label])=>(kind+' '+label).toLowerCase().includes(helpFilter.toLowerCase())))actions.append(button(label,()=>start(kind),locked||!canAct(kind,state,context)));box.append(actions);
   const nav=node('div');nav.className='actions';for(const name of ['attempts','blockers','validations','claims'])nav.append(button(name,()=>{activeSection='records';resource=name;return session.readPage(name,{task_id:state.taskId});},state.busy));box.append(nav);
  }
  editor(box,state);host.replaceChildren(box);
  if(key){const input=Array.from(host.querySelectorAll('[data-task-field]')).find(n=>n.dataset.taskField===key);if(input){input.value=caret.value;input.focus();if(typeof input.setSelectionRange==='function'&&caret.start!==null)input.setSelectionRange(caret.start,caret.end);}}
 }
 session=Session.createSession({api,getContext,onDirty,onChange:state=>{if(session)render(state);},storage:storage===undefined?root.localStorage:storage});render(session.state());return session;
}
const api={operation,create,canAct,affectedParentIds,parentGuardMap,mergeDecision};root.WorkspaceTaskWorkflows=api;if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:this);
