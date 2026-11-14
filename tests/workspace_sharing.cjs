const assert=require('node:assert/strict');
const sharing=require('../docs/workspace-sharing.js');
const plain=x=>JSON.parse(JSON.stringify(x));
function fixture(){
 let project='p',failure=null,posts=[];
 const page={project_id:'p',revision:'store:1',review_digest:'digest1',limit:20,cursor:null,next_cursor:'next',items:[{source_event_id:'e',task_id:'task',source_sha256:'a'.repeat(64),proposals:{redacted:{details_json:'{"text":"safe <script>literal</script>"}'},omitted:{details_json:'{"notice":"withheld"}'}},current_choice:null}]};
 const api=async(path,options)=>{
  if(path==='/v1/transactions'){
   posts.push(options.body);if(failure){const error=failure;failure=null;throw error}
   const body=JSON.parse(options.body);return{request_id:body.request_id,revision:'store:2',changed_records:[{type:'journal_sharing_review',id:'receipt'}]};
  }
  if(path.includes('/capabilities'))return{capabilities:{authenticated:true,local_principal_id:'actor',granted:['contribute','review']}};
  return plain(page);
 };
 let n=0;const session=sharing.createSession({api,getContext:()=>({projectId:project}),requestId:()=>`request-${++n}`,onChange:()=>{},onDirty:()=>{}});
 return{session,page,posts,api,setProject:value=>project=value,fail:error=>failure=error};
}
(async()=>{
 const f=fixture();await f.session.load();
 assert.deepEqual(plain(f.session.state().choices),{},'no modes selected by default');
 await assert.rejects(f.session.save(),/Choose/);assert.equal(f.posts.length,0);
 f.session.choose('e','redacted');
 await assert.rejects(f.session.save(),/acknowledge/i);
 f.session.acknowledge(true);f.fail(Object.assign(Error('lost reply'),{status:503}));
 await f.session.save();assert.equal(f.session.state().phase,'uncertain');
 const frozen=f.session.state().pendingBody;
 assert.equal(f.session.discard(),false);await f.session.load();assert.equal(f.session.state().phase,'uncertain');
 assert.throws(()=>f.session.choose('e','omitted'),/pending|locked/i);
 f.setProject('different');await assert.rejects(f.session.save(),/project/i);assert.equal(f.posts.length,1);
 f.setProject('p');await f.session.save();assert.equal(f.session.state().phase,'saved');
 assert.equal(f.posts[0],f.posts[1]);assert.equal(f.posts[1],frozen,'retry body is byte-for-byte frozen');
 const body=JSON.parse(frozen);assert.equal(body.expected_revision,'store:1');assert.equal(body.operations[0].review_digest,'digest1');
 const stale=fixture();await stale.session.load();stale.session.choose('e','omitted');stale.session.acknowledge(true);
 stale.fail(Object.assign(Error('changed'),{status:409}));await stale.session.save();
 assert.equal(stale.session.state().phase,'stale');assert.equal(stale.session.state().choices.e,'omitted');
 await assert.rejects(stale.session.save(),/fresh review/i);assert.equal(stale.posts.length,1);
 await stale.session.load(null,{discardChoices:true});assert.deepEqual(plain(stale.session.state().choices),{});
 const denied=fixture();const api=async(path,opts)=>path.includes('/capabilities')?{capabilities:{authenticated:true,local_principal_id:'actor',granted:['contribute']}}:denied.api(path,opts);
 const readOnly=sharing.createSession({api,getContext:()=>({projectId:'p'}),onChange:()=>{},onDirty:()=>{}});await readOnly.load();
 assert.equal(readOnly.state().canReview,false);assert.throws(()=>readOnly.choose('e','redacted'),/review/i);
 let actualProject='p',resolveP,resolveQ;
 const delayed=sharing.createSession({getContext:()=>({projectId:actualProject}),api:async path=>path.includes('/capabilities')?{capabilities:{local_principal_id:'actor'}}:new Promise(resolve=>{if(path.includes('project_id=p'))resolveP=resolve;else resolveQ=resolve}),onChange:()=>{},onDirty:()=>{}});
 const first=delayed.load();actualProject='q';const second=delayed.load();
 resolveQ({...plain(f.page),project_id:'q'});await second;resolveP(plain(f.page));await first;
 assert.equal(delayed.state().projectId,'q');assert.equal(delayed.state().page.project_id,'q','late response cannot repaint another project');
 let malformed=true;const uncertain=fixture();
 const broken=sharing.createSession({getContext:()=>({projectId:'p'}),requestId:()=> 'frozen-invalid-reply',api:async(path,opts)=>path==='/v1/transactions'&&malformed?{}:uncertain.api(path,opts),onChange:()=>{},onDirty:()=>{}});
 await broken.load();broken.choose('e','redacted');broken.acknowledge(true);await broken.save();assert.equal(broken.state().phase,'uncertain','200 without matching receipt remains uncertain');
 malformed=false;await broken.save();assert.equal(broken.state().phase,'saved');
 let activePanel='sharing',otherDraft=false,resolveHidden;
 const hidden=sharing.createSession({getContext:()=>({projectId:'p',activePanel}),onDirty:value=>{otherDraft=value},onChange:()=>{},api:async path=>path.includes('/capabilities')?{capabilities:{local_principal_id:'actor'}}:new Promise(resolve=>{resolveHidden=resolve})});
 const hiddenLoad=hidden.load();activePanel='questions';otherDraft=true;resolveHidden(plain(f.page));await hiddenLoad;
 assert.equal(otherDraft,true,'a late sharing load cannot clear a draft in another panel');
 await assert.rejects(hidden.save(),/Return to sharing/);
 assert.match(sharing.reloadLabel({phase:'ready',acknowledged:false,choices:{e:'redacted'}}),/discard current choices/);
 const values=new Map(),storage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
 const durable=fixture();let durableFailure=true;
 const durableApi=async(path,opts)=>{if(path==='/v1/transactions'&&durableFailure)throw Object.assign(Error('lost'),{status:503});return durable.api(path,opts)};
 const options={api:durableApi,getContext:()=>({projectId:'p'}),storage,onChange:()=>{},onDirty:()=>{},requestId:()=> 'persistent-request'};
 const before=sharing.createSession(options);await before.load();before.choose('e','redacted');before.acknowledge(true);await before.save();
 const savedBody=before.state().pendingBody;const after=sharing.createSession(options);assert.equal(after.state().pendingBody,savedBody);assert.equal(after.state().phase,'uncertain');
 await after.load();assert.equal(durable.posts.length,0,'recovery must not automatically submit');
 const deniedApi=sharing.createSession({...options,api:async()=>{throw Object.assign(Error('revoked'),{status:403})}});await deniedApi.save();assert.equal(deniedApi.state().pendingBody,savedBody,'access failure cannot prove a previous unknown request did not commit');assert.equal(deniedApi.discard(),false);
 durableFailure=false;await after.save();assert.equal(after.state().phase,'saved');assert.equal(values.size,0);assert.equal(durable.posts[0],savedBody);
 console.log('journal sharing controller checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
