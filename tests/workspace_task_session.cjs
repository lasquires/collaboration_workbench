const assert=require('node:assert/strict');
const {createSession,RECOVERY}=require('../docs/workspace-task-session.js');

const project='11111111-1111-4111-8111-111111111111';
const principal='22222222-2222-4222-8222-222222222222';
const task='33333333-3333-4333-8333-333333333333';
const uuid=n=>`${String(n).padStart(8,'0')}-0000-4000-8000-${String(n).padStart(12,'0')}`;
const receipt=(body,revision='store:2')=>({request_id:body.request_id,revision,changed_records:[{type:'task',id:body.operations[0].task_id,record_revision:2}]});
function harness({api,raw=null,context={projectId:project,principalId:principal,taskId:task,activePanel:'task-workflows'}}={}){
  const data=new Map();if(raw!==null)data.set(RECOVERY,raw);const calls=[];
  const storage={getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};
  const session=createSession({api:async(path,options={})=>{calls.push({path,options});return api?api(path,options,calls):defaultResponse(path);},getContext:()=>context,storage,requestId:()=>uuid(90)});
  return{session,calls,data,storage,context};
}
function defaultResponse(path){
  if(path.includes('/capabilities'))return{project_id:project,revision:'store:1',capabilities:{granted:['contribute','review','manage_claims']}};
  if(path.includes('/context'))return{project_id:project,revision:'store:1',context:{task:{id:task,project_id:project,record_revision:1,structure_revision:1,status:'active'},redirect:null}};
  return{project_id:project,revision:'store:1',items:[],next_cursor:null};
}
function op(fields={}){return{op:'record_attempt',task_id:task,expected_record_revision:1,rationale:'Record a bounded fixture result',boundary_id:'fixture-boundary',outcome:'failed',evidence:[{note:'Synthetic'}],...fields};}

async function main(){
  {
    let resolvePost;const h=harness({api:(path,options)=>options.method==='POST'?new Promise(resolve=>{resolvePost=()=>resolve(receipt(JSON.parse(options.body)))}):defaultResponse(path)});
    await h.session.load();h.session.edit('record_attempt',op());const request=h.session.submit(op());await new Promise(r=>setImmediate(r));
    const frozen=h.session.state().pendingBody;assert.ok(frozen);assert.equal(h.data.get(RECOVERY),frozen);resolvePost();await request;
    assert.equal(h.session.state().phase,'saved');assert.equal(h.session.state().pendingBody,null);
  }
  {
    let posts=0;const frozen=JSON.stringify({project_id:project,actor:{principal_id:principal},expected_revision:'store:1',request_id:uuid(10),operations:[op()]});
    const h=harness({raw:frozen,api:(path,options)=>options.method==='POST'?(posts++,receipt(JSON.parse(options.body))):defaultResponse(path)});
    assert.equal(h.session.state().pendingBody,frozen);await h.session.load();assert.equal(posts,0);await h.session.retry();assert.equal(posts,1);
  }
  {
    const h=harness({api:(path,options)=>options.method==='POST'?(()=>{const e=Error('stale');e.data={transaction_rejection:{project_id:project,principal_id:principal,request_id:uuid(90),expected_revision:'store:1',state:'not_applied'}};throw e;})():defaultResponse(path)});
    await h.session.load();h.session.edit('record_attempt',op());await assert.rejects(h.session.submit(op()));assert.equal(h.session.state().draft.needsReconcile,true);assert.equal(h.session.state().phase,'rejected');
  }
  {
    const h=harness({api:(path,options)=>options.method==='POST'?{request_id:'wrong',revision:'store:2',changed_records:[{type:'task',id:task}]}:defaultResponse(path)});
    await h.session.load();h.session.edit('record_attempt',op());await assert.rejects(h.session.submit(op()));assert.equal(h.session.state().phase,'uncertain');assert.ok(h.session.state().pendingBody);
  }
  {
    let posts=0;const h=harness({api:(path,options)=>{if(options.method==='POST'){posts++;throw Error('connection dropped after send');}return defaultResponse(path);}});
    await h.session.load();h.session.edit('record_attempt',op());await assert.rejects(h.session.submit(op()));const raw=h.session.state().pendingBody;assert.ok(raw);
    const restarted=harness({raw,api:(path,options)=>{if(options.method==='POST'){assert.equal(options.body,raw);return receipt(JSON.parse(options.body));}return defaultResponse(path);}});
    await restarted.session.load();assert.equal(restarted.calls.filter(call=>call.options.method==='POST').length,0);await restarted.session.retry();assert.equal(restarted.session.state().pendingBody,null);assert.equal(posts,1);
  }
  {
    const h=harness({api:(path,options)=>{if(options.method==='POST')throw Error('untrusted generic rejection');return defaultResponse(path);}});
    await h.session.load();h.session.edit('record_attempt',op());await assert.rejects(h.session.submit(op()));assert.equal(h.session.state().phase,'uncertain');assert.ok(h.session.state().pendingBody);assert.equal(h.data.has(RECOVERY),true);
  }
  {
    const h=harness({api:(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body)):defaultResponse(path)});h.storage.removeItem=()=>{throw Error('cleanup denied');};
    await h.session.load();h.session.edit('record_attempt',op());await h.session.submit(op());assert.equal(h.session.state().phase,'saved-cleanup-error');assert.ok(h.session.state().pendingBody);assert.ok(h.data.has(RECOVERY));
  }
  {
    const storage={getItem(){return null},setItem(){throw Error('quota')},removeItem(){}};const h=harness();h.session.state();
    // Replace storage before constructing a separate session to prove persistence precedes POST.
    let posts=0;const blocked=createSession({api:async(path,options={})=>{if(options.method==='POST')posts++;return defaultResponse(path)},getContext:()=>h.context,storage,requestId:()=>uuid(90)});
    await blocked.load();blocked.edit('record_attempt',op());await assert.rejects(blocked.submit(op()),/persist/i);assert.equal(posts,0);
  }
  {
    const h=harness({raw:'{broken'});assert.equal(h.session.state().phase,'recovery-error');await assert.rejects(h.session.load());assert.equal(h.data.get(RECOVERY),'{broken');assert.equal(h.calls.length,0);
  }
  {
    let finish;const context={projectId:project,principalId:principal,taskId:task,activePanel:'task-workflows'};
    const h=harness({context,api:path=>path.includes('/context')?new Promise(resolve=>{finish=()=>resolve(defaultResponse(path))}):defaultResponse(path)});
    const loading=h.session.load();await new Promise(r=>setImmediate(r));context.activePanel='research';finish();await loading;assert.equal(h.session.state().selection,null);assert.equal(h.session.state().revision,null);
  }
  {
    const h=harness({api:defaultResponse});await h.session.load();h.session.edit('record_attempt',op());
    await h.session.readPage('attempts',null);assert.equal(h.session.state().draft.baseRevision,'store:1');
    await assert.rejects(h.session.submit(op({task_id:uuid(4)})),/target|task/i);
  }
  {
    let grants=['contribute','review'];
    const h=harness({api:(path,options,calls)=>path.includes('/capabilities')?{project_id:project,capabilities:{granted:[...grants]}}:defaultResponse(path)});
    await h.session.load();h.session.edit('record_attempt',op());h.session.change('rationale','Keep this draft across capability refresh');
    const before=h.session.state().draft;
    grants=[];await h.session.load();
    assert.deepEqual(h.session.state().capabilities.granted,[]);
    assert.equal(h.session.state().draft.baseRevision,before.baseRevision);
    assert.deepEqual(h.session.state().draft.fields,before.fields);
    assert.equal(h.session.state().phase,'editing');
  }
  {
    const h=harness({api:path=>path.startsWith('/v1/blockers?')?{project_id:project,revision:'store:1',items:[{id:uuid(7),project_id:project,task_id:task}],next_cursor:null}:defaultResponse(path)});
    await h.session.load();const page=await h.session.readPage('blockers',{task_id:task});assert.equal(page.items.length,1);const filters=new URL(h.calls.at(-1).path,'http://fixture').searchParams;assert.equal(filters.get('task_id'),task);assert.equal(filters.get('limit'),'20');assert.match(h.calls.at(-1).path,/state=open/);
    await assert.rejects(h.session.readPage('attempts',{task_id:uuid(8)}),/selected task/i);
  }
  {
    let reads=0;const h=harness({api:(path,options)=>{if(options.method==='POST'){const body=JSON.parse(options.body),e=Error('stale');e.data={transaction_rejection:{project_id:body.project_id,principal_id:body.actor.principal_id,request_id:body.request_id,expected_revision:body.expected_revision,state:'not_applied'}};throw e;}if(path.includes('/context')&&++reads>1)return{project_id:project,revision:'store:2',context:{task:{id:task,project_id:project,record_revision:2,structure_revision:1,status:'active'}}};return defaultResponse(path);}});
    await h.session.load();h.session.edit('record_attempt',op());h.session.change('rationale','Preserve the reviewed account');await assert.rejects(h.session.submit(op({rationale:'Preserve the reviewed account'})));
    await assert.rejects(h.session.reconcile(op({expected_record_revision:2,rationale:'Preserve the reviewed account'}),true),/refresh/i);await h.session.refreshContext();
    await h.session.reconcile(op({expected_record_revision:2,rationale:'Preserve the reviewed account'}),true);assert.equal(h.session.state().draft.fields.rationale,'Preserve the reviewed account');assert.equal(h.session.state().draft.baseRevision,'store:2');
  }
  {
    const h=harness({api:(path)=>path.includes('/context')?{project_id:project,revision:'store:1',context:{task:{id:uuid(6),project_id:project,record_revision:1,structure_revision:1}}}:defaultResponse(path)});
    await assert.rejects(h.session.load(),/context|task/i);assert.equal(h.session.state().selection,null);
  }
  {
    let posts=0;const h=harness({api:(path,options)=>{if(options.method==='POST'){posts++;const body=JSON.parse(options.body),e=Error('Proven stale rejection');e.data={transaction_rejection:{project_id:body.project_id,principal_id:body.actor.principal_id,request_id:body.request_id,expected_revision:body.expected_revision,state:'not_applied'}};throw e;}return defaultResponse(path);}});
    await h.session.load();h.session.edit('record_attempt',op());await assert.rejects(h.session.submit(op()));
    h.session.change('rationale','Keep this edit while reviewing the rejection');
    assert.equal(h.session.state().draft.needsReconcile,true,'editing retained text cannot bypass required explicit reconciliation');
    await assert.rejects(h.session.submit(op({rationale:'Keep this edit while reviewing the rejection'})),/reconcile/i);
    assert.equal(posts,1,'editing after rejection cannot send a replacement request before reread and acknowledgement');
  }
  {
    const survivor=uuid(44),h=harness({api:path=>path.includes('/context')?{project_id:project,revision:'store:3',redirect:{from:task,to:survivor},context:{task:{id:survivor,project_id:project,record_revision:3,structure_revision:2}}}:defaultResponse(path)});
    await h.session.load();assert.deepEqual(h.session.state().selection.redirect,{from:task,to:survivor});assert.equal(h.session.state().context,null,'merged task context cannot silently become the old target');
    assert.equal(h.session.state().taskId,task);assert.equal(h.session.state().phase,'redirect');assert.throws(()=>h.session.edit('record_attempt',op()),/selected task|merged|context/i);
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  }
  {
    const h=harness();await h.session.load();h.session.edit('record_attempt',op());
    await assert.rejects(h.session.submit(op({expected_record_revision:2})),/guard|revision|captured/i,'submitted task guard must remain the captured draft guard until explicit reconciliation');
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  }
  {
    const child=uuid(77),h=harness({api:path=>path.startsWith('/v1/task-children?')?{project_id:project,task_id:task,task_scope_ids:[task],revision:'store:1',items:[{id:child,project_id:project,parent_id:task}],next_cursor:null}:path.includes('/history?')?{project_id:project,revision:'store:1',items:[{event_id:uuid(78),task_id:task,kind:'observation',details:{text:'Pinned history'}}],next_cursor:null}:defaultResponse(path)});
    await h.session.load();h.session.edit('record_attempt',op());const before=h.session.state().draft;
    assert.equal((await h.session.readPage('task-children',{task_id:task})).items[0].id,child);
    assert.doesNotMatch(h.calls.at(-1).path,/order=/,'section API accepts only its explicit task/limit/cursor filters');
    assert.equal((await h.session.readPage('history',{task_id:task})).items[0].task_id,task);assert.match(h.calls.at(-1).path,new RegExp('/tasks/'+task+'/history'));
    assert.deepEqual(h.session.state().draft,before,'section navigation cannot change authored fields or base revision');
  }
  {
    const h=harness({api:path=>path.startsWith('/v1/task-findings?')?{project_id:project,task_id:task,task_scope_ids:[task],revision:'store:1',items:[{id:'foreign-scope-finding',project_id:project,scope:{task_id:uuid(88)}}],next_cursor:null}:defaultResponse(path)});await h.session.load();await assert.rejects(h.session.readPage('task-findings'),/scope|task/i);
  }
  console.log('workspace_task_session: protocol fixtures passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
