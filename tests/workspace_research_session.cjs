'use strict';
const assert = require('node:assert/strict');
const { createSession, RECOVERY } = require('../docs/workspace-research-session.js');

const uuid = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
function harness(overrides={}) {
  const calls=[], stored=new Map(); let context={projectId:uuid(1),principalId:uuid(2),activePanel:'research'};
  const defaultStorage={getItem:k=>stored.has(k)?stored.get(k):null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};const storage=overrides.storage||defaultStorage;if(overrides.initialRecovery!==undefined)stored.set(RECOVERY,overrides.initialRecovery);
  const api=async (path,options={})=>{calls.push({path,options});let result=overrides.api?await overrides.api(path,options,calls):({api_version:'v1',project_id:context.projectId,revision:'store:1',coverage:{},items:[],next_cursor:null});if(path.includes('/capabilities')&&result?.capabilities===undefined)result={...result,capabilities:{}};if(path.startsWith('/v1/findings?')&&result?.items===undefined)result={...result,items:[],next_cursor:null};return result;};
  const session=createSession({api,getContext:()=>context,storage,requestId:()=>uuid(8)});
  return {session,calls,stored,setContext:v=>context={...context,...v},storage};
}
const receipt=(body,changed_records=[])=>({api_version:'v1',project_id:body.project_id,revision:'store:2',request_id:body.request_id,changed_records});
const createFinding=()=>({op:'create_finding',id:uuid(11),claim:'Observed behavior',scope:{},classification:'observation',uncertainty:'',evidence:[]});

(async()=>{
  assert.equal(RECOVERY,'workbench.research.pending.v1');
  {
    const h=harness({api:async path=>{
      if(path.includes('/capabilities'))return {project_id:uuid(1),revision:'store:4',capabilities:{}};
      if(path.includes('/answers/'))return {project_id:uuid(1),revision:'store:4',context:{entity_type:'answer',entity:{id:uuid(51),project_id:uuid(1),question_id:uuid(52),review_state:'accepted'},question:{id:uuid(52),project_id:uuid(1),record_revision:2},related:{}}};
      return {project_id:uuid(1),revision:'store:4',context:{entity_type:'question',entity:{id:uuid(52),project_id:uuid(1),record_revision:2,state:'open'},related:{}}};
    }});
    await h.session.open('question',uuid(52));
    const answer=await h.session.inspectAnswer(uuid(51));
    assert.equal(answer.entity.id,uuid(51));
    assert.equal(h.session.state().selection.id,uuid(52),'answer inspection does not replace question selection');
    h.session.edit('resolve_question',{question_id:uuid(52),expected_record_revision:2,answer_id:uuid(51),rationale:'Reviewed'});
    assert.equal((await h.session.inspectAnswer(uuid(51))).question.id,uuid(52),'recovered resolution can explicitly reload immutable answer context');
  }

  {
    const h=harness({api:async path=>{
      if(path.includes('/capabilities'))return {project_id:uuid(1),revision:'store:7',capabilities:{granted:['contribute']}};
      const id=path.includes(encodeURIComponent(uuid(41)))?uuid(41):uuid(42);
      return {project_id:uuid(1),revision:'store:7',context:{entity_type:'finding',entity:{id,project_id:uuid(1),record_revision:1},related:{}}};
    }});
    await h.session.open('finding',uuid(41));
    assert.equal(h.session.state().revision,'store:7','opening a record must install its revision before editing');
    assert.equal(h.session.state().phase,'ready');
    await h.session.open('finding',uuid(42));
    assert.equal(h.session.state().selection.id,uuid(42),'browsing may switch between existing selected records');
    h.session.edit('revise_finding',{finding_id:uuid(42),expected_record_revision:1,fields:{claim:'New wording'},rationale:'Clarify'});
    assert.equal(h.session.state().draft.baseRevision,'store:7');
  }

  {
    let rejectPost=true;
    const h=harness({api:async(path,options)=>{if(options.method==='POST'){if(rejectPost)throw Error('lost reply'); return receipt(JSON.parse(options.body),[{type:'finding',id:uuid(11)}]);} return {api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'project',entity:{},related:{}}};}});
    await h.session.load(); await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.submit(createFinding()));
    const frozen=h.stored.get(RECOVERY); assert.ok(frozen);
    const afterReload=harness({initialRecovery:frozen,api:async(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body),[{type:'finding',id:uuid(11)}]):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'project',entity:{},related:{}}}}); await afterReload.session.load();
    assert.equal(afterReload.calls.filter(c=>c.options.method==='POST').length,0,'reload must never auto-submit');
    assert.equal(afterReload.session.state().pendingBody,frozen);
    await afterReload.session.retry();
    assert.equal(afterReload.calls.find(c=>c.options.method==='POST').options.body,frozen,'retry reuses exact bytes');
    assert.equal(afterReload.stored.has(RECOVERY),false);
  }
  {
    const h=harness(); await h.session.load(); await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.submit({op:'resolve_question',question_id:uuid(3),expected_record_revision:1,answer_id:uuid(4),rationale:'x'}));
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0,'forbidden operation cannot reach API');
  }
  {
    const h=harness({api:async(path,options)=>{if(options.method==='POST'){const b=JSON.parse(options.body); const e=Error('validation');e.data={transaction_rejection:{project_id:b.project_id,principal_id:b.actor.principal_id,request_id:b.request_id,expected_revision:b.expected_revision,state:'not_applied'}};throw e;} return {api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'project',entity:{},related:{}}};}});
    await h.session.load();await h.session.edit('create_finding',createFinding());await assert.rejects(h.session.submit(createFinding()));
    assert.equal(h.stored.has(RECOVERY),false,'only exact rejection proof clears pending');
    assert.equal(h.session.state().phase,'rejected');
    assert.equal(h.session.state().draft.fields.claim,'Observed behavior');
    await assert.rejects(h.session.submit(createFinding()),/reconcile/,'proven rejection requires explicit reread and reconciliation');
  }
  {
    const h=harness(); await h.session.load(); await h.session.edit('create_finding',createFinding());
    const draft=h.session.state().draft; await h.session.readPage('findings',{},null);
    assert.equal(h.session.state().draft.baseRevision,draft.baseRevision,'reads do not move an authored base');
    h.session.change('claim','Preserved text'); await h.session.refreshContext();
    assert.equal(h.session.state().draft.fields.claim,'Preserved text');
    const op={...createFinding(),claim:'Reviewed field choice'}; await h.session.reconcile(op,true);
    assert.equal(h.session.state().draft.baseRevision,'store:1');
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  }
  {
    const h=harness({api:async(path,options)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[{id:'row',project_id:uuid(1)}],next_cursor:'c1'})});
    await h.session.load(); const page=await h.session.readPage('findings',{},'cursor');
    assert.equal(page.items.length,1); assert.equal(page.resource,'findings'); assert.equal(page.cursor,'cursor'); assert.equal(page.revision,'store:1'); assert.equal(page.next_cursor,'c1'); const req=h.calls.at(-1); assert.match(req.path,/findings/); assert.match(req.path,/limit=20/); assert.match(req.path,/cursor=cursor/);
  }
  {
    let release; const h=harness({api:async(path)=>path.includes('/capabilities')?{api_version:'v1',project_id:uuid(1),revision:'store:1',capabilities:{}}:new Promise(resolve=>{release=()=>resolve({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:'x'});})});
    const pending=h.session.load(); await new Promise(r=>setImmediate(r)); release(); await pending;
    const read=h.session.readPage('findings',{},null); await new Promise(r=>setImmediate(r)); h.setContext({projectId:uuid(9)}); release(); await read;
    assert.equal(h.session.state().projectId,uuid(1),'late response cannot change selected project identity');
  }
  {
    let h=harness({api:async(path,options)=>options.method==='POST'?{request_id:'wrong',revision:'store:2',changed_records:[{type:'finding'}]}:{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    await h.session.load(); await h.session.edit('create_finding',createFinding()); await assert.rejects(h.session.submit(createFinding()));
    assert.ok(h.stored.has(RECOVERY),'malformed acknowledgment retains exact body');
    assert.equal(h.session.state().phase,'uncertain');
  }
  {
    const h=harness(); h.storage.setItem=()=>{throw Error('quota')}; await h.session.load(); await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.submit(createFinding()),/persist/);
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0,'storage failure blocks POST');
  }
  {
    const h=harness(); await h.session.load(); await h.session.edit('create_finding',createFinding());
    const evidence=Array.from({length:100},(_,i)=>({artifact_version_id:uuid(100+i),locator:'é'.repeat(10000),method:'é'.repeat(4000),limitations:'é'.repeat(4000)}));
    await assert.rejects(h.session.submit({...createFinding(),evidence}),/2 MiB/);
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0,'oversize body blocks POST');
  }
  {
    let pageCalls=0;const h=harness({api:async(path,options)=>{if(path.includes('cursor=')){const e=Error('expired');e.status=409;throw e;}return {api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null};}});
    await h.session.load();await h.session.edit('create_finding',createFinding());h.session.change('claim','keep through 409');
    await assert.rejects(h.session.readPage('findings',{},'expired'));
    assert.equal(h.session.state().draft.fields.claim,'keep through 409','expired cursor preserves authored fields');
  }
  {
    const h=harness({api:async(path,options)=>path.includes('/capabilities')?{api_version:'v1',project_id:uuid(1),revision:'store:1',capabilities:{granted:['contribute']}}:path.includes('/context')?{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'answer',entity:{id:uuid(31),question_id:uuid(32),project_id:uuid(1)},question:{id:uuid(32),project_id:uuid(1)},related:{support:{resource:'answer-support',params:{project_id:uuid(1),subject_id:uuid(31)}},reviews:{resource:'answer-reviews',params:{project_id:uuid(1),subject_id:uuid(31)}}}}}:{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    await h.session.open('answer',uuid(31)); assert.equal(h.session.state().selection.id,uuid(31)); assert.deepEqual(h.session.state().capabilities.granted,['contribute']);
    const support=await h.session.readPage('answer-support',{subject_id:uuid(31)},null); assert.deepEqual(support.items,[]);
  }
  {
    const h=harness({initialRecovery:'{malformed'});
    await assert.rejects(h.session.load(),/malformed/); assert.equal(h.stored.get(RECOVERY),'{malformed','malformed recovery remains untouched');assert.equal(h.session.state().recoveryRaw,'{malformed','malformed raw body remains inspectable in controller state');
    assert.equal(h.calls.length,0,'malformed recovery fails closed before reads or writes');
  }
  {
    const h=harness(); await h.session.load(); await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.readPage('../../transactions',{},null),/Unsupported/);
    assert.equal(h.calls.filter(c=>c.path.includes('transactions')).length,0,'resource paths cannot escape whitelist');
  }
  {
    let finish;const h=harness({api:async(path,options)=>options.method==='POST'?new Promise(resolve=>{finish=()=>resolve(receipt(JSON.parse(options.body),[{type:'finding',id:uuid(11)}]));}):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null,capabilities:{}}});
    await h.session.load();await h.session.edit('create_finding',createFinding());const first=h.session.submit(createFinding());await new Promise(r=>setImmediate(r));
    await assert.rejects(h.session.submit(createFinding()),/in flight/,'second mutation is rejected while first is pending');finish();await first;
  }
  {
    const original=createFinding();const frozen=JSON.stringify({project_id:uuid(1),actor:{principal_id:uuid(2)},expected_revision:'store:1',request_id:uuid(88),operations:[original]});
    const h=harness({initialRecovery:frozen,api:async(path,options)=>{if(options.method==='POST'){const b=JSON.parse(options.body);const e=Error('rejected');e.data={transaction_rejection:{project_id:b.project_id,principal_id:b.actor.principal_id,request_id:b.request_id,expected_revision:b.expected_revision,state:'not_applied'}};throw e;}return {api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null,capabilities:{}};}});
    assert.equal(h.session.state().draft.fields.claim,original.claim,'recovery reconstructs the authored operation before load');await h.session.load();await assert.rejects(h.session.retry());
    assert.equal(h.session.state().draft.fields.claim,original.claim,'exact rejection after reload keeps authored data');assert.equal(h.session.state().draft.needsReconcile,true);
  }
  {
    const answerId=uuid(41),reviewId=uuid(42);
    const h=harness({api:async(path,options)=>options.method==='POST'?{request_id:JSON.parse(options.body).request_id,revision:'store:2',changed_records:[{type:'answer_review',id:reviewId,answer_id:answerId,outcome:'accepted'}]}:{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null,capabilities:{}}});
    await h.session.load();await h.session.edit('review_answer',{answer_id:answerId,expected_question_revision:1,outcome:'accepted',rationale:'independent',supersedes:[]});
    await h.session.submit({op:'review_answer',answer_id:answerId,expected_question_revision:1,outcome:'accepted',rationale:'independent',supersedes:[]});
    assert.equal(h.session.state().phase,'saved','answer review receipt must be recognized');
  }
  {
    const h=harness({initialRecovery:JSON.stringify({project_id:uuid(1),actor:{principal_id:uuid(2)},expected_revision:'store:1',request_id:uuid(8),operations:[createFinding()]})});
    assert.equal(h.session.state().pendingBody,h.stored.get(RECOVERY),'constructor discovers recovery before dashboard load');
    assert.equal(h.calls.length,0,'constructor recovery does not make requests');
  }
  {
    const h=harness({api:async(path)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null})});await h.session.load();
    const page=await h.session.readPage('answer-support',{project_id:uuid(1),subject_id:uuid(31)},null);assert.ok(page);
    await assert.rejects(h.session.readPage('findings',{project_id:uuid(9)},null),/project/);
    await assert.rejects(h.session.readPage('findings',{key:'x'},null),/filter/);
  }
  {
    const h=harness({api:async(path)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null})});await h.session.load();
    await assert.rejects(h.session.open('finding',uuid(51)),/match|project/);
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.reconcile({...createFinding(),id:uuid(12)},true),/refresh|reconcile/i);
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());
    await assert.rejects(h.session.submit({...createFinding(),id:uuid(99)}),/identity|target|draft/i);
  }
  {
    const h=harness({api:async(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body),[{type:'finding',id:uuid(11)}]):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});await h.session.load();await h.session.edit('create_finding',createFinding());
    await h.session.submit({...createFinding(),evidence:[{artifact_version_id:uuid(61),locator:'x'.repeat(500),method:'observe',limitations:'none stated'}]});
    assert.equal(h.session.state().phase,'saved','long valid source locator is not mistaken for a malformed object locator');
  }
  {
    const storage={getItem(){throw Error('blocked storage')}};
    const h=harness({storage});
    assert.equal(h.session.state().phase,'recovery-error');
    assert.throws(()=>h.session.edit('create_finding',createFinding()),/storage|recovery/i);
    await assert.rejects(h.session.submit(createFinding()),/storage|recovery/i);
    assert.equal(h.calls.length,0,'unreadable recovery blocks all writes');
  }
  {
    const h=harness({api:async(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body),[{type:'finding',id:uuid(11)}]):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    h.storage.removeItem=()=>{throw Error('denied')};await h.session.load();await h.session.edit('create_finding',createFinding());await h.session.submit(createFinding());
    assert.ok(h.session.state().pendingBody,'failed cleanup retains exact in-memory recovery');
    assert.ok(h.stored.has(RECOVERY),'failed cleanup blocks replacement body while storage remains');
    assert.equal(h.session.state().phase,'saved-cleanup-error');
  }
  {
    const h=harness({api:async(path)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:path.startsWith('/v1/findings?')?[]:Array.from({length:21},(_,i)=>({id:String(i),project_id:uuid(1)})),next_cursor:null})});
    await h.session.load();await assert.rejects(h.session.readPage('artifact-versions',{},null),/oversized/);
  }
  {
    const h=harness({api:async(path)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:path.startsWith('/v1/findings?')?[]:[{id:uuid(71),project_id:uuid(9)}],next_cursor:null})});
    await h.session.load();await assert.rejects(h.session.readPage('answers',{subject_id:uuid(71)},null),/foreign/);
  }
  {
    const h=harness();await h.session.load();await h.session.readPage('findings',{},null);await h.session.readPage('findings',{},'next');
    assert.deepEqual(Object.keys(h.session.state().pages),['findings'],'only the latest page per resource is cached');
  }
  {
    const h=harness({api:async(path)=>path.includes('/context')?{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'finding',entity:{id:uuid(51),project_id:uuid(9)},related:{}}}:{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    await assert.rejects(h.session.open('finding',uuid(51)),/context|project/i);
  }
  {
    const h=harness({api:async(path)=>path.includes('/context')?{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},context:{entity_type:'answer',entity:{id:uuid(81),question_id:uuid(82),project_id:uuid(1)},question:{id:uuid(82),project_id:uuid(9)},related:{}}}:{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null,capabilities:{}}});
    await assert.rejects(h.session.open('answer',uuid(81)),/question|context/i);
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());await h.session.readPage('findings',{},null);
    await assert.rejects(h.session.reconcile(createFinding(),true),/refresh/i,'catalog page is not a reconciliation context read');
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());await h.session.refreshContext();
    await assert.rejects(h.session.reconcile({...createFinding(),id:uuid(99)},true),/identity/i);
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());h.setContext({principalId:uuid(9)});
    assert.throws(()=>h.session.change('claim','wrong actor'),/principal/i);
  }
  {
    const rejectionApi=async(path,options)=>{if(options.method==='POST'){const b=JSON.parse(options.body);const e=Error('stale');e.status=409;e.data={transaction_rejection:{project_id:b.project_id,principal_id:b.actor.principal_id,request_id:b.request_id,expected_revision:b.expected_revision,state:'not_applied'}};throw e;}return {api_version:'v1',project_id:uuid(1),revision:'store:2',coverage:{},items:[],next_cursor:null,capabilities:{}};};
    const h=harness({api:rejectionApi});await h.session.load();await h.session.edit('create_finding',createFinding());await assert.rejects(h.session.submit(createFinding()));
    await h.session.readPage('answers',{},null);await assert.rejects(h.session.reconcile(createFinding(),true),/refresh/i);
    await h.session.refreshContext();await h.session.reconcile(createFinding(),true);
    assert.equal(h.session.state().draft.baseRevision,'store:2','only explicit fresh context advances base');
  }
  {
    const h=harness({api:async(path)=>{const project=path.includes(uuid(9))?uuid(9):uuid(1);return path.includes('/context')?{api_version:'v1',project_id:project,revision:'store:1',coverage:{},context:{entity_type:'question',entity:{id:uuid(91),project_id:project},related:{}}}:{api_version:'v1',project_id:project,revision:'store:1',coverage:{},items:[],next_cursor:null,capabilities:{}};}});
    await h.session.open('question',uuid(91));h.setContext({projectId:uuid(9)});await h.session.load();
    assert.equal(h.session.state().projectId,uuid(9));assert.equal(h.session.state().selection,null,'explicit clean load clears prior entity context before fetching new project');
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_finding',createFinding());
    h.setContext({activePanel:'other'});await assert.rejects(h.session.refreshContext(),/panel/i);
    assert.throws(()=>h.session.change('claim','wrong panel'),/panel/i);
  }
  {
    const h=harness();await h.session.load();await h.session.edit('create_answer',{question_id:uuid(101),id:uuid(102),expected_question_revision:1,claim:'answer',evidence_link_ids:[uuid(103)]});
    const op={op:'create_answer',id:uuid(102),question_id:uuid(101),expected_question_revision:1,claim:'answer',evidence_link_ids:Array.from({length:100},(_,i)=>uuid(200+i)),evidence:[{artifact_version_id:uuid(400),locator:'p.1',method:'inspect',limitations:'partial'}]};
    await assert.rejects(h.session.submit(op),/Malformed answer/);
  }
  {
    let permitRemoval=false;
    const h=harness({api:async(path,options)=>{if(options.method==='POST'){const b=JSON.parse(options.body);const e=Error('not applied');e.data={transaction_rejection:{project_id:b.project_id,principal_id:b.actor.principal_id,request_id:b.request_id,expected_revision:b.expected_revision,state:'not_applied'}};throw e;}return {api_version:'v1',project_id:uuid(1),revision:'store:2',coverage:{},items:[],next_cursor:null};}});
    const remove=h.storage.removeItem;h.storage.removeItem=key=>{if(!permitRemoval)throw Error('denied');remove(key);};
    await h.session.load();await h.session.edit('create_finding',createFinding());await assert.rejects(h.session.submit(createFinding()));
    const exact=h.session.state().pendingBody;assert.ok(exact);await assert.rejects(h.session.submit(createFinding()),/frozen|pending|resolve|draft/i);
    permitRemoval=true;await assert.rejects(h.session.retry());assert.equal(h.session.state().pendingBody,null);
    assert.equal(h.session.state().draft.needsReconcile,true,'proof cleanup preserves a rejected draft that still requires explicit reconciliation');
  }
  {
    const raw=JSON.stringify({project_id:uuid(1),actor:{principal_id:uuid(2)},expected_revision:'store:1',request_id:uuid(8),operations:[{...createFinding(),extra:'bad'}]});
    const h=harness({initialRecovery:raw});assert.equal(h.session.state().phase,'recovery-error');assert.equal(h.session.state().recoveryRaw,raw);
    await assert.rejects(h.session.retry());assert.equal(h.stored.get(RECOVERY),raw,'schema-invalid body is retained and never replayed');
  }
  {
    const linkId=uuid(501);
    const h=harness({api:async(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body),[{type:'evidence_change',id:uuid(502),evidence_link_id:linkId,state:'retracted'}]):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    await h.session.load();await h.session.edit('set_evidence_state',{evidence_link_id:linkId,expected_state:'available',state:'retracted',reason:'source invalid'});
    await h.session.submit({op:'set_evidence_state',evidence_link_id:linkId,expected_state:'available',state:'retracted',reason:'source invalid'});
    assert.equal(h.session.state().phase,'saved','evidence-change receipt confirms its link and state');
  }
  {
    const h=harness({api:async(path,options)=>options.method==='POST'?receipt(JSON.parse(options.body),[{type:'finding',id:JSON.parse(options.body).operations[0].id}]):{api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:[],next_cursor:null}});
    await h.session.load();await h.session.submit({...createFinding(),id:undefined});
    assert.equal(JSON.parse(h.calls.find(c=>c.options.method==='POST').options.body).operations[0].id,uuid(8),'creation identity is fixed before the request body is frozen');
  }
  {
    const requested=uuid(601);const h=harness({api:async(path)=>({api_version:'v1',project_id:uuid(1),revision:'store:1',coverage:{},items:path.startsWith('/v1/findings?')?[]:[{id:uuid(602),project_id:uuid(1),question_id:uuid(603)}],next_cursor:null})});
    await h.session.load();await assert.rejects(h.session.readPage('answers',{subject_id:requested},null),/identity/,'same-project rows still must match their requested record');
  }
  {
    const q=uuid(701),a=uuid(702),f=uuid(703),e=uuid(704);
    const cases=[
      [{op:'revise_finding',finding_id:f,expected_record_revision:1,fields:{claim:'Preserved authored text'},rationale:'Revision'},'finding',f],
      [{op:'create_answer',id:uuid(705),question_id:q,expected_question_revision:1,claim:'Pinned answer',evidence_link_ids:[e]},'question',q],
      [{op:'review_answer',answer_id:a,expected_question_revision:1,outcome:'accepted',rationale:'Independent review',supersedes:[]},'answer',a],
      [{op:'set_evidence_state',evidence_link_id:e,expected_state:'available',state:'retracted',reason:'Preserved withdrawal reason'},'evidence',e]
    ];
    for(const [op,type,id] of cases){
      const raw=JSON.stringify({project_id:uuid(1),actor:{principal_id:uuid(2)},expected_revision:'store:1',request_id:uuid(8),operations:[op]});
      const h=harness({initialRecovery:raw,api:async(path)=>path.includes('/capabilities')?{project_id:uuid(1),revision:'store:2',capabilities:{}}:{project_id:uuid(1),revision:'store:2',context:{entity_type:type,entity:{id,project_id:uuid(1),question_id:q},question:{id:q,project_id:uuid(1)},related:{}}}});
      assert.equal(h.session.state().selection?.type,type,'recover original existing target before dashboard load');
      assert.equal(h.session.state().selection?.id,id);await h.session.load();
      assert.equal(h.session.state().selection.context.entity.id,id);assert.equal(h.session.state().pendingBody,raw);
      assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0,'target context recovery never submits');
    }
  }
  console.log('workspace_research_session: all fixtures passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
