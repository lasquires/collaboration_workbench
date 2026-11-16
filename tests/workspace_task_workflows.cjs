'use strict';
const assert=require('node:assert/strict');
const tasks=require('../docs/workspace-task-workflows.js');
const context={task:{id:'task-uuid',project_id:'p',record_revision:4},acceptance_state:'pending',gate_revision:2,validation:null};
assert.throws(()=>tasks.operation('complete_task',context,{rationale:'Declare success'}),/gate|validation/i);
assert.throws(()=>tasks.operation('record_attempt',context,{task_id:'another',rationale:'Wrong target',boundary_id:'b',outcome:'failed',evidence:['trace']}),/target|identity/i);
const failed=tasks.operation('record_attempt',context,{boundary_id:'camera',outcome:'failed',evidence:['trace'],rationale:'Observed mismatch'});
assert.equal(failed.expected_record_revision,4);assert.equal(failed.task_id,'task-uuid');assert.equal(failed.outcome,'failed');
assert.throws(()=>tasks.operation('move_task',context,{parent_id:'new-parent',rationale:'Rescope'}),/parent|guard/i);
assert.throws(()=>tasks.operation('record_attempt',context,{boundary_id:'camera',outcome:'passed',evidence:[],rationale:'Unchecked'}),/evidence/i);
assert.throws(()=>tasks.operation('record_validation',context,{outcome:'passed',inputs:{},outputs:{},evidence:[{artifact_version_id:'v',method:'Read',limitations:''}],rationale:'Review'}),/limitations/i);
assert.throws(()=>tasks.operation('unknown',context,{rationale:'Unknown'}),/operation/i);
const accepted={...context,acceptance_state:'passed',validation:{current:true,outcome:'passed',gate_revision:2}};
assert.equal(tasks.operation('complete_task',accepted,{rationale:'Current independent gate passed'}).expected_record_revision,4);
console.log('task workflow operation checks passed');
assert.equal(typeof tasks.create,'function','selected task renderer must expose integration entry');
assert.equal(tasks.operation('resolve_attempt',context,{op:'resolve_attempt',attempt_id:'attempt',expected_record_revision:4,rationale:'Reviewed'}).attempt_id,'attempt','canonical recovered operations can reopen in the editor');
class Node {
 constructor(tag,doc){this.tagName=tag;this.doc=doc;this.children=[];this.dataset={};this.value='';this.textContent='';}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 setAttribute(k,v){this[k]=v;}
 setCustomValidity(v){this.validationMessage=v;}
 reportValidity(){return true;}
 focus(){this.doc.activeElement=this;}
 setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
 querySelectorAll(selector){const key=selector==='[data-task-field]'?'taskField':'other';return this.children.flatMap(n=>[...(n.dataset[key]?[n]:[]),...n.querySelectorAll(selector)]);}
}
(async()=>{
 const doc={activeElement:null,createElement(tag){return new Node(tag,this);}},mount=new Node('main',doc);let state={projectId:'p',principalId:'author',taskId:'task-uuid',revision:'store:1',context,capabilities:{authenticated:true,granted:['contribute']},pages:{},draft:null,phase:'ready',busy:false,pendingBody:null},posts=0;
 global.WorkspaceTaskSession={createSession(opts){return{state:()=>JSON.parse(JSON.stringify(state)),edit(kind,seed){state.draft={kind,fields:seed};opts.onChange(state);},change(key,value){state.draft.fields[key]=value;opts.onChange(state);},submit(){posts++;}};}};
 const renderer=tasks.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',taskId:'task-uuid',activePanel:'task-workflows'}),storage:{}});
 const all=n=>[n,...n.children.flatMap(all)];
 assert.ok(all(mount).some(n=>n.tagName==='button'&&n.textContent==='Subtasks'),'selected task exposes bounded subtask section');
 assert.ok(all(mount).some(n=>n.tagName==='button'&&n.textContent==='Knowledge'),'selected task exposes scoped finding section');
 const help=all(mount).find(n=>n['aria-label']==='Filter task actions and help');assert.ok(help);help.value='merge';help.oninput();
 assert.ok(!all(mount).some(n=>n.tagName==='button'&&n.textContent==='Record an attempt'),'help filter narrows visible actions');
 assert.equal(state.draft,null);assert.equal(posts,0,'filtering help never creates a draft or mutation');
 const clear=all(mount).find(n=>n['aria-label']==='Filter task actions and help');clear.value='';clear.oninput();

 assert.equal(all(mount).find(n=>n.tagName==='button'&&n.textContent==='Record independent validation').disabled,true,'known missing review capability disables independent validation');
 await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Declare task scope').onclick();
 assert.equal(Object.hasOwn(state.draft.fields,'decomposition_complete'),false,'opening a scope editor cannot silently declare incomplete scope');
 assert.equal(Object.hasOwn(state.draft.fields,'area_id'),false,'missing context area cannot silently clear existing scope');
 const retained=JSON.stringify(state.draft);let prevented=false;
 const researchLink=all(mount).find(n=>n.tagName==='a'&&n.textContent==='Open research for this task');
 assert.equal(typeof researchLink.onclick,'function','task context links guard unsaved navigation');
 researchLink.onclick({preventDefault(){prevented=true;}});
 assert.equal(prevented,true,'unsent task draft blocks full-page research navigation');
 assert.equal(JSON.stringify(state.draft),retained,'blocked link retains authored task fields');
 state.draft=null;state.pendingBody=JSON.stringify({operations:[{op:'record_attempt',task_id:'task-uuid'}]});prevented=false;
 researchLink.onclick({preventDefault(){prevented=true;}});
 assert.equal(prevented,true,'pending request also blocks navigation');
 state.pendingBody=null;prevented=false;researchLink.onclick({preventDefault(){prevented=true;}});
 assert.equal(prevented,false,'clean task links retain normal navigation');
 assert.equal(posts,0,'opening task forms never submits');
 delete global.WorkspaceTaskSession;console.log('task workflow DOM checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
assert.equal(tasks.canAct('record_validation',{principalId:'creator',capabilities:{authenticated:true,granted:['contribute','review']}},{...context,task:{...context.task,created_by:'creator'}}),false,'creator cannot independently validate');
assert.equal(tasks.canAct('record_validation',{principalId:'claimant',capabilities:{authenticated:true,granted:['contribute','review']}},{...context,progress:{claim:{principal_id:'claimant',state:'expired'}}}),false,'expired unreleased claim still prevents self-validation');
assert.equal(tasks.canAct('heartbeat_claim',{principalId:'other',capabilities:{authenticated:true,granted:['contribute']}},{...context,progress:{claim:{principal_id:'claimant',state:'live'}}}),false,'another contributor cannot renew a protected claim');
assert.equal(tasks.canAct('handoff_claim',{principalId:'claimant',capabilities:{authenticated:true,granted:['contribute']}},{...context,progress:{claim:{principal_id:'claimant',state:'live'}}}),false,'handoff requires manage_claims even for claimant');
assert.equal(tasks.canAct('complete_task',{capabilities:{authenticated:true,granted:['contribute']}},{...accepted,task:{...accepted.task,status:'review'}}),false,'completion requires review capability in authenticated transaction contract');
assert.equal(tasks.operation('record_blocker',context,{kind:'review',target_id:'',rationale:'Independent review needed'}).target_id,null,'targetless review blockers use canonical null');
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 global.WorkspaceTaskSession=require('../docs/workspace-task-session.js');
 const project='11111111-1111-4111-8111-111111111111',principal='22222222-2222-4222-8222-222222222222',task='33333333-3333-4333-8333-333333333333';
 const doc={activeElement:null,createElement(tag){return new Node(tag,this);}},mount=new Node('main',doc),stored=new Map(),calls=[];let revision=1;
 const r=tasks.create({mount,document:doc,getContext:()=>({projectId:project,principalId:principal,taskId:task,activePanel:'task-workflows'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api:async(path,options)=>{
  calls.push({path,options});if(options?.method==='POST'){const body=JSON.parse(options.body);revision++;return {project_id:project,revision:'store:'+revision,request_id:body.request_id,changed_records:[{type:'task',id:task,record_revision:revision}]};}
  if(path.includes('/capabilities'))return {project_id:project,revision:'store:'+revision,capabilities:{authenticated:true,granted:['contribute']}};
  if(path.includes('/context'))return {project_id:project,revision:'store:'+revision,context:{task:{id:task,project_id:project,title:'<script>source text</script>',record_revision:revision,structure_revision:1,status:'active'},acceptance_state:'pending',progress:{claim:{state:'unclaimed'}},validation:null}};
  return {project_id:project,revision:'store:'+revision,items:[],next_cursor:null};
 }});
 const all=n=>[n,...n.children.flatMap(all)];await r.load();
 await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Record an attempt').onclick();
 r.change('boundary_id','camera-comparison');r.change('outcome','failed');r.change('evidence',['Synthetic trace1']);r.change('rationale','Document the scoped mismatch');
 await all(mount).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
 assert.equal(r.state().phase,'ready');assert.equal(r.state().context.task.record_revision,2);assert.equal(r.state().draft,null);
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,1,'real task controller/renderer save emits exactly one mutation');assert.equal(stored.size,0);
 assert.ok(!all(mount).some(n=>n.tagName==='script'),'task source titles render literally');
 delete global.WorkspaceTaskSession;console.log('task controller/renderer integration checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
const moving={...context,task:{...context.task,parent_id:'old'},ancestors:[{id:'old'},{id:'root'}]};
const destination={task:{id:'new',project_id:'p'},ancestors:[{id:'root'}]};
assert.deepEqual(tasks.affectedParentIds(moving,destination),['old','root','new']);
assert.deepEqual(tasks.parentGuardMap('p','store:3',['old'],[{project_id:'p',revision:'store:3',context:{task:{id:'old',project_id:'p',structure_revision:7}}}]),{old:7});
assert.throws(()=>tasks.parentGuardMap('p','store:3',['old'],[{project_id:'p',revision:'store:4',context:{task:{id:'old',project_id:'p',structure_revision:7}}}]),/snapshot|revision/i);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));global.WorkspaceTaskSession=require('../docs/workspace-task-session.js');
 const project='11111111-1111-4111-8111-111111111111',principal='22222222-2222-4222-8222-222222222222',task='33333333-3333-4333-8333-333333333333',old='44444444-4444-4444-8444-444444444444',next='55555555-5555-4555-8555-555555555555';
 const doc={activeElement:null,createElement(tag){return new Node(tag,this);}},mount=new Node('main',doc),stored=new Map(),posts=[];let revision=1;
 const r=tasks.create({mount,document:doc,getContext:()=>({projectId:project,principalId:principal,taskId:task,activePanel:'task-workflows'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api:async(path,options)=>{
  if(options?.method==='POST'){const body=JSON.parse(options.body);posts.push(body);revision++;return {project_id:project,revision:'store:'+revision,request_id:body.request_id,changed_records:[{type:'task',id:task,record_revision:revision}]};}
  if(path.includes('/capabilities'))return {project_id:project,capabilities:{authenticated:true,granted:['contribute']}};
  if(path.includes('merge-preview'))return {project_id:project,revision:'store:'+revision,preview:{survivor_id:task,absorbed_id:'66666666-6666-4666-8666-666666666666',guard_digest:'merge-guard',guards:{[task]:{record_revision:revision}},field_conflicts:{title:{survivor:{present:true,value:'Keep'},absorbed:{present:true,value:'Incoming'}}},metadata_conflicts:{},problems:[],claim_conflicts:[],apply_available:true}};
  const id=path.includes(old)?old:path.includes(next)?next:task;
  return {project_id:project,revision:'store:'+revision,context:{task:{id,project_id:project,parent_id:id===task?(revision===1?old:next):null,record_revision:revision,structure_revision:id===old?7:id===next?8:1,status:'active'},ancestors:id===task?[{id:revision===1?old:next}]:[],progress:{claim:{state:'unclaimed'}}}};
 }});
 const all=n=>[n,...n.children.flatMap(all)];await r.load();await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Move task').onclick();r.change('parent_id',next);r.change('required_for_parent',false);r.change('rationale','Move synthetic scope after reviewing both parent guards');
 await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Read affected parent scopes').onclick();assert.deepEqual(r.state().draft.fields.expected_parents,{[old]:7,[next]:8});
 await all(mount).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});assert.equal(posts.length,1);assert.equal(posts[0].operations[0].required_for_parent,false);assert.deepEqual(posts[0].operations[0].expected_parents,{[old]:7,[next]:8});assert.equal(r.state().context.task.parent_id,next);
 await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Review task merge').onclick();r.change('absorbed_id','66666666-6666-4666-8666-666666666666');await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Read current merge preview').onclick();assert.equal(posts.length,1,'merge preview never submits');
 r.change('choices',{title:'absorbed'});r.change('source_references',['Synthetic merge comparison']);r.change('rationale','Reviewed explicit conflict choices');await all(mount).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});assert.equal(posts.length,2);assert.equal(posts[1].operations[0].op,'merge_tasks');assert.equal(posts[1].operations[0].choices.title,'absorbed');assert.equal(posts[1].operations[0].preview_guard,'merge-guard');delete global.WorkspaceTaskSession;

 console.log('task move parent-scope integration checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
const mergePreview={survivor_id:'task-uuid',absorbed_id:'absorbed',guard_digest:'guard',guards:{'task-uuid':{record_revision:4}},field_conflicts:{title:{survivor:{value:'Mine'},absorbed:{value:'Theirs'}}},metadata_conflicts:{},problems:[],apply_available:true};
assert.throws(()=>tasks.mergeDecision('merge_tasks',context,mergePreview,{choices:{},metadata_choices:{},source_references:['Synthetic comparison'],rationale:'Merge reviewed tasks'}),/choice|every|conflict/i);
const mergeOp=tasks.mergeDecision('merge_tasks',context,mergePreview,{choices:{title:'absorbed'},metadata_choices:{},source_references:['Synthetic comparison'],rationale:'Merge reviewed tasks'});
assert.equal(mergeOp.preview_guard,'guard');assert.equal(mergeOp.absorbed_id,'absorbed');assert.equal(mergeOp.choices.title,'absorbed');
assert.throws(()=>tasks.mergeDecision('reverse_merge',context,{survivor_id:'task-uuid',merge_id:'m',guard_digest:'g',can_reverse:false},{rationale:'Reverse'}),/intervening|reverse|compensation/i);
assert.throws(()=>tasks.mergeDecision('compensate_merge',context,{survivor_id:'task-uuid',merge_id:'m',guard_digest:'g',apply_available:true,actions:[{key:'action'}]},{choices:{},source_references:['Review'],rationale:'Preserve later work'}),/choice|every/i);
assert.throws(()=>tasks.mergeDecision('merge_tasks',context,mergePreview,{absorbed_id:'different',choices:{title:'absorbed'},metadata_choices:{},source_references:['Reviewed'],rationale:'Displayed target changed'}),/identity|target|preview/i,'changing the displayed incoming task cannot reuse an old preview');
assert.throws(()=>tasks.mergeDecision('reverse_merge',context,{survivor_id:'task-uuid',merge_id:'original-merge',guard_digest:'guard',can_reverse:true},{merge_id:'different-merge',rationale:'Displayed merge changed'}),/identity|target|preview/i);
assert.equal(tasks.canAct('record_attempt',{principalId:'author',capabilities:{authenticated:true,granted:['contribute']}},{...context,task:{...context.task,archived_at:'2026-11-15'}}),false,'archived tasks must be explicitly reopened before new work');
assert.equal(tasks.canAct('reopen_task',{principalId:'author',capabilities:{authenticated:true,granted:['contribute']}},{...context,task:{...context.task,archived_at:'2026-11-15'}}),true);
