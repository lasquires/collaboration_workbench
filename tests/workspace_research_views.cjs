const assert=require('node:assert/strict');
const views=require('../docs/workspace-research.js');
const question={id:'q',project_id:'p',record_revision:3,state:'open',recorded_state:'open'};
const finding={id:'f',project_id:'p',record_revision:4,scope:{identifier:'speed',namespace:'class',scope_key:'Ball',custom_scope:{build:'native-a'}},claim:'Original claim',classification:'source-supported',uncertainty:'Not runtime proof'};
const support={artifact_version_id:'v',locator:{line:7},method:'Read pinned fixture',limitations:'Synthetic, not native'};
const fields={id:'new-f',claim:'Authored claim',classification:'unclassified',uncertainty:'Unknown runtime behavior',scope:{...finding.scope},supportMode:'keep',evidence:[{...support,id:'withdrawn',state:'retracted'}],rationale:'Clarify the wording'};
const editorial=views.buildFindingOperation('revise_finding',fields,finding);
assert.equal(editorial.expected_record_revision,4);assert.equal(editorial.finding_id,'f');assert.ok(!Object.hasOwn(editorial.fields,'evidence'),'ordinary edits never recreate withdrawn support');assert.deepEqual(editorial.fields.scope.custom_scope,{build:'native-a'});
assert.throws(()=>views.buildFindingOperation('revise_finding',{...fields,supportMode:'replace'},finding),/withdrawn|available|retracted/i,'replacement cannot silently revive a withdrawn link');
const created=views.buildFindingOperation('create_finding',{...fields,evidence:[support]});assert.equal(created.id,'new-f');assert.deepEqual(created.evidence,[support]);assert.equal(created.classification,'unclassified');
assert.deepEqual(finding.scope.custom_scope,{build:'native-a'},'builders never mutate prior scope');
const seed=views.findingSeed({id:'observation',occurrence_id:'occurrence',artifact_version_id:'v',identifier:'speed',namespace:'class',scope_key:'Ball',locator_key:{line:7},provenance:{method:'Indexed source',limitations:'Partial index'}},'task-uuid');
assert.equal(views.findingSeed({id:'observation-only',candidate_type:'observation'}).scope.occurrence_id,undefined,'observation IDs must not be invented as occurrence identities');
assert.equal(seed.scope.task_id,'task-uuid');assert.equal(seed.scope.occurrence_id,'occurrence');assert.equal(seed.classification,'unclassified');assert.deepEqual(seed.evidence[0].locator,{line:7});
const answer=views.buildAnswerOperation({id:'a',claim:'A proposed answer',evidence_link_ids:['link'],evidence:[]},question);assert.equal(answer.expected_question_revision,3);assert.equal(answer.question_id,'q');
assert.throws(()=>views.buildAnswerOperation({id:'a',claim:'No support',evidence_link_ids:[],evidence:[]},question),/support|evidence|pinned/i);
const accepted={id:'a',question_id:'q',created_by:'author',review_state:'accepted',review_concerns:[]};
assert.throws(()=>views.buildReviewOperation({outcome:'accepted',rationale:'My own claim'},accepted,question,'author',[]),/independent|author|own/i);
const review=views.buildReviewOperation({outcome:'rejected',rationale:'Independent disagreement',supersedes:['mine']},accepted,question,'reviewer',[{id:'mine',answer_id:'a',reviewer_id:'reviewer'}]);assert.equal(review.expected_question_revision,3);assert.deepEqual(review.supersedes,['mine']);
assert.throws(()=>views.buildReviewOperation({outcome:'accepted',rationale:'Cannot erase another review',supersedes:['theirs']},accepted,question,'reviewer',[{id:'theirs',answer_id:'a',reviewer_id:'other'}]),/own|review/i);
assert.throws(()=>views.buildResolutionOperation({...accepted,review_state:'disputed'},question,'Select answer'),/accepted|review/i);
assert.throws(()=>views.buildResolutionOperation({...accepted,question_id:'other'},question,'Wrong question'),/question/i);
assert.throws(()=>views.buildResolutionOperation(accepted,{...question,state:'resolved'},'Replace progress'),/reopen|open|resolved/i);
assert.equal(views.buildResolutionOperation(accepted,question,'Reviewed selection').answer_id,'a');
const reopen=views.buildQuestionStateOperation({state:'investigating',rationale:'Record effective reopened state'},{...question,state:'investigating',recorded_state:'resolved'});assert.equal(reopen.state,'investigating');
const link={id:'e',project_id:'p',state:'available'},replacement={id:'r',project_id:'p',state:'available'};
assert.throws(()=>views.buildEvidenceOperation({state:'superseded',reason:'Replace witness'},link,{...replacement,project_id:'other'}),/project|scope/i);
assert.throws(()=>views.buildEvidenceOperation({state:'retracted',reason:'Withdraw'}, {...link,state:'retracted'}),/available|withdrawn|retracted/i);
assert.equal(views.buildEvidenceOperation({state:'superseded',reason:'Reviewed replacement'},link,replacement).replacement_id,'r');
assert.equal(views.buildFindingStateOperation('archive_finding',finding,'No longer active').expected_record_revision,4);
console.log('research operation builder checks passed');
const draftState={principalId:'reviewer',selection:{context:{entity_type:'finding',entity:finding}},draft:{kind:'revise_finding',fields:{finding_id:'f',expected_record_revision:2,fields:{claim:'My draft',scope:finding.scope,classification:'unclassified',uncertainty:'Still unknown'},rationale:'Explain',_supportMode:'keep'}}};
assert.equal(views.operationForDraft(draftState).expected_record_revision,2,'a new read cannot silently advance a draft guard');
assert.throws(()=>views.operationForDraft(draftState,{latest:true}),/choice|choose/i,'reconciliation needs explicit field choices');
const reconciled=views.operationForDraft(draftState,{latest:true,choices:{claim:'draft',scope:'latest',classification:'draft',uncertainty:'latest',evidence:'latest'}});
assert.equal(reconciled.expected_record_revision,4);assert.equal(reconciled.fields.claim,'My draft');assert.equal(reconciled.fields.uncertainty,finding.uncertainty);assert.ok(!Object.hasOwn(reconciled.fields,'evidence'));
assert.throws(()=>views.buildFindingOperation('create_finding',{...fields,evidence:[{...support,_locatorInvalid:true}]}),/locator/i);
assert.equal(typeof views.create,'function','research renderer must expose its dashboard entry point');
assert.throws(()=>views.operationForDraft({principalId:'r',selection:{context:{entity_type:'finding',entity:{id:'q',project_id:'p',record_revision:3}}},draft:{kind:'set_question_state',fields:{question_id:'q',expected_record_revision:3,state:'investigating',rationale:'Continue'}}}),/type|context/i,'same ID in a different entity table must not authorize a question edit');
class Element {
 constructor(tag,doc){this.tagName=tag;this.doc=doc;this.children=[];this.dataset={};this.textContent='';this.value='';}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 setAttribute(key,value){this[key]=value;}
 setCustomValidity(value){this.validationMessage=value;}
 reportValidity(){return !this.querySelectorAll('[data-research-field]').some(n=>n.validationMessage);}
 focus(){this.doc.activeElement=this;}
 setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
 querySelectorAll(selector){const result=[];const walk=n=>{for(const c of n.children){if(selector==='[data-research-field]'&&c.dataset.researchField||selector==='[data-editor-error]'&&Object.hasOwn(c.dataset,'editorError'))result.push(c);walk(c);}};walk(this);return result;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}};
 const mount=new Element('main',doc),stored=new Map(),calls=[];
 const renderer=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api:async(path,options)=>{calls.push({path,options});return {project_id:'p',revision:'store:1',items:[{id:'f',project_id:'p',claim:'<img src=x onerror=alert(1)>'}],next_cursor:null,capabilities:{granted:['contribute']}};}});
 await renderer.load();
 const flatten=n=>[n,...n.children.flatMap(flatten)];
 assert.ok(flatten(mount).some(n=>n.textContent==='<img src=x onerror=alert(1)>'),'source text remains literal textContent');
 assert.ok(!flatten(mount).some(n=>n.tagName==='img'),'source markup cannot create elements');
 await flatten(mount).find(n=>n.tagName==='button'&&n.textContent==='New finding').onclick();
 assert.equal(renderer.state().draft.kind,'create_finding');
 const claim=mount.querySelectorAll('[data-research-field]').find(n=>n.dataset.researchField==='Claim');
 claim.value='A human-authored claim';doc.activeElement=claim;claim.selectionStart=5;claim.selectionEnd=5;claim.oninput();
 assert.equal(renderer.state().draft.fields.claim,'A human-authored claim');
 assert.equal(doc.activeElement.value,'A human-authored claim');assert.equal(doc.activeElement.selectionStart,5,'rerender preserves caret');
 const scopeInput=mount.querySelectorAll('[data-research-field]').find(n=>n.dataset.researchField==='Scope');
 scopeInput.value='{unfinished';doc.activeElement=scopeInput;scopeInput.oninput();
 const claimAgain=mount.querySelectorAll('[data-research-field]').find(n=>n.dataset.researchField==='Claim');
 claimAgain.value='Another authored edit';doc.activeElement=claimAgain;claimAgain.oninput();
 const preservedScope=mount.querySelectorAll('[data-research-field]').find(n=>n.dataset.researchField==='Scope');
 assert.equal(preservedScope.value,'{unfinished','invalid authored JSON is not dropped when another field rerenders');
 assert.ok(preservedScope.validationMessage,'invalid source/scope input keeps submission blocked');
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0,'browsing and drafting never auto-submit');
 await flatten(mount).find(n=>n.tagName==='button'&&n.textContent==='artifact versions').onclick();
 const addSource=flatten(mount).find(n=>n.tagName==='button'&&n.textContent==='Use pinned version');
 assert.ok(addSource,'version browser offers explicit support selection while preserving draft');
 await addSource.onclick();
 assert.equal(renderer.state().draft.fields.evidence[0].artifact_version_id,'f');
 assert.equal(renderer.state().draft.fields.evidence[0].method,'','catalog selection cannot invent an observation method');
 assert.equal(renderer.state().draft.fields.claim,'Another authored edit','catalog selection preserves authored text');
 await flatten(mount).find(n=>n.tagName==='button'&&n.textContent==='Discard draft').onclick();
 assert.equal(renderer.state().draft,null,'local discard clears unsaved authored draft without a mutation');
 assert.ok(!flatten(mount).some(n=>n.tagName==='form'),'local discard removes the authoring form');
 console.log('research renderer DOM checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
assert.equal(views.canResearch({capabilities:{authenticated:true,granted:['contribute']}},'review'),false,'known contributor-only access cannot start independent review');
assert.equal(views.canResearch({capabilities:{authenticated:true,granted:['contribute','review']}},'review'),true);
(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc),stored=new Map(),calls=[];let saved=null;
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api:async(path,options)=>{
  calls.push({path,options});if(options?.method==='POST'){const body=JSON.parse(options.body);saved=body.operations[0];return {project_id:'p',revision:'store:2',request_id:body.request_id,changed_records:[{type:'finding',id:saved.id}]};}
  if(path.includes('/context'))return {project_id:'p',revision:'store:2',context:{entity_type:'finding',entity:{...saved,id:saved.id,project_id:'p',record_revision:1},related:{}}};
  return {project_id:'p',revision:'store:1',items:[],next_cursor:null,capabilities:{authenticated:true,granted:['contribute']}};
 }});
 const all=n=>[n,...n.children.flatMap(all)];await r.load();
 await all(mount).find(n=>n.tagName==='button'&&n.textContent==='New finding').onclick();
 r.change('claim','Saved claim remains scoped');
 const form=all(mount).find(n=>n.tagName==='form');
 await form.onsubmit({preventDefault(){}});
 assert.equal(r.state().phase,'ready','confirmed save refreshes the exact current record');
 assert.equal(r.state().selection?.id,saved.id);assert.equal(r.state().selection.context.entity.claim,'Saved claim remains scoped');
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,1,'post-save refresh never creates another mutation');
 assert.equal(stored.size,0,'only confirmed receipt clears durable pending request');
 console.log('research confirmed-save context checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc),stored=new Map();let cleanupFails=true;const bodies=[];
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem(key){if(cleanupFails)throw Error('cleanup unavailable');stored.delete(key);}},api:async(path,options)=>{
  if(options?.method==='POST'){bodies.push(options.body);const body=JSON.parse(options.body);return {project_id:'p',revision:'store:2',request_id:body.request_id,changed_records:[{type:'finding',id:body.operations[0].id}]};}
  return {project_id:'p',revision:'store:1',items:[],next_cursor:null,capabilities:{authenticated:true,granted:['contribute']}};
 }});
 const all=n=>[n,...n.children.flatMap(all)];await r.load();await all(mount).find(n=>n.tagName==='button'&&n.textContent==='New finding').onclick();r.change('claim','Receipt saved; recovery cleanup blocked');await all(mount).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});
 assert.equal(r.state().phase,'saved-cleanup-error');assert.equal(r.state().draft,null);assert.ok(r.state().pendingBody);
 assert.ok(all(mount).some(n=>n.tagName==='button'&&n.textContent==='Retry exact request'),'confirmed receipt with failed cleanup must retain an accessible exact retry control even after draft cleared');
 cleanupFails=false;await all(mount).find(n=>n.tagName==='button'&&n.textContent==='Retry exact request').onclick();assert.equal(r.state().pendingBody,null);assert.equal(stored.size,0);assert.equal(bodies.length,2);assert.equal(bodies[1],bodies[0],'cleanup recovery retries exactly the original frozen bytes');
 console.log('research cleanup recovery control checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc),calls=[];
 const entities={finding:{...finding,claim:'<img src=x onerror=alert(1)>',review_concerns:['Conflicting witness remains unresolved']},evidence:{id:'e',project_id:'p',state:'available',subject_type:'finding_revision',subject_id:'revision',artifact_version_id:'v',source_sha256:'a'.repeat(64),binary_build_id:'build-A',locator:{line:7},method:'Read pinned fixture',limitations:'Synthetic, not native'}};
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:{getItem:()=>null,setItem(){},removeItem(){}},api:async(path,options)=>{
  calls.push({path,options});
  if(path.includes('/context')){const type=path.includes('evidence-links')?'evidence':path.includes('/questions/')?'question':path.includes('/answers/')?'answer':'finding';return {project_id:'p',revision:'store:1',context:{entity_type:type,entity:JSON.parse(JSON.stringify(entities[type])),...(type==='answer'?{question:entities.question}:{}),related:{}}};}
  return {project_id:'p',revision:'store:1',items:[],next_cursor:null,capabilities:{authenticated:true,granted:['contribute']}};
 }});
 entities.question={...question,state:'investigating',recorded_state:'resolved',resolution:{answer_id:'a',active:0},resolution_concerns:['Support was withdrawn']};
 entities.answer={...accepted,id:'a',project_id:'p',acceptance_scope:'Independent review is bounded; it is not forensic proof'};
 entities.finding.scope={...entities.finding.scope,occurrence_id:'exact-occurrence',area_id:'exact-area'};
 const all=n=>[n,...n.children.flatMap(all)];await r.load();await r.open('finding','f');
 assert.ok(all(mount).some(n=>n.textContent==='What we think'),'selected finding exposes its authored claim as readable content');
 assert.ok(all(mount).some(n=>n.textContent==='Still unexplained'),'selected finding gives uncertainty its own visible label');
 assert.ok(all(mount).some(n=>n.textContent==='Not runtime proof'),'uncertainty is visible outside a raw JSON dump');
 for(const value of ['exact-occurrence','exact-area'])assert.ok(all(mount).some(n=>n.textContent===value),'known scope identities are readable without opening raw provenance');
 assert.ok(all(mount).some(n=>n.textContent==='Conflicting witness remains unresolved'),'review concerns remain visible');
 const raw=all(mount).find(n=>n.tagName==='details'&&n.children.some(c=>c.tagName==='summary'&&c.textContent==='Full record and provenance'));
 assert.ok(raw,'complete original record remains available in a collapsed detail');
 assert.ok(all(raw).some(n=>n.tagName==='pre'&&n.textContent.includes('custom_scope')),'unknown authored scope remains lossless');
 assert.ok(!all(mount).some(n=>n.tagName==='img'),'readable research fields still render source markup literally');
 await r.open('evidence','e');
 for(const text of ['Source citation','Source version','Build','Source SHA-256','Locator','Observation method','Source limitations','Read pinned fixture','Synthetic, not native'])assert.ok(all(mount).some(n=>n.textContent===text),`readable source context exposes ${text}`);
 delete entities.evidence.source_sha256;entities.evidence.binary_build_id=null;await r.open('evidence','e');
 assert.ok(all(mount).some(n=>n.textContent==='Not included in this read'),'a source field omitted by the projection is not claimed missing from storage');
 assert.ok(all(mount).some(n=>n.textContent==='Not recorded'),'an explicit null source field remains distinct from omitted metadata');
 await r.open('question','q');
 assert.ok(all(mount).some(n=>n.textContent==='Support was withdrawn'),'effective reopened-state concerns stay visible');
 assert.ok(all(mount).some(n=>n.textContent==='Previously selected answer'),'inactive historical selection is not presented as current acceptance');
 await r.open('answer','a');
 assert.ok(all(mount).some(n=>n.textContent==='Independent review is bounded; it is not forensic proof'),'accepted review keeps its explicit forensic boundary visible');
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0,'readable context never changes research state');
 console.log('readable research context checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

assert.equal(typeof views.derivationSeed,'function','source history authors derivations using ordinary finding records');
const derived=views.derivationSeed({id:'child',project_id:'p'},'task');
assert.deepEqual(derived.scope,{artifact_version_id:'child',task_id:'task',derivation:{format:'workbench-derived-source/v1',parent_version_id:''}});
assert.equal(derived.evidence[0].method,'','a source catalog selection never invents how derivation was established');
assert.equal(derived.claim,'','derivation assertion remains explicitly authored');
assert.throws(()=>views.buildFindingOperation('create_finding',{...derived,evidence:[support]}),/parent|derivation/i,'typed source assertions need an exact parent');
assert.equal(typeof views.buildNegativeSearchOperation,'function');
const searchFields={id:'negative',artifact_version_id:'child',query:'missingSymbol',method:'Exact text scan',searched_range:'Lines 1-20',coverage:{complete:false,excluded:['Other files']},outcome:'Not found in this bounded search'};
const search=views.buildNegativeSearchOperation(searchFields,{id:'child',project_id:'p'});
assert.deepEqual(search,{op:'record_negative_search',...searchFields});
assert.throws(()=>views.buildNegativeSearchOperation({...searchFields,artifact_version_id:'other'},{id:'child',project_id:'p'}),/source|version|target/i);
assert.throws(()=>views.buildNegativeSearchOperation({...searchFields,coverage:{complete:'false'}},{id:'child',project_id:'p'}),/coverage/i);
assert.equal(views.operationForDraft({projectId:'p',selection:{context:{entity_type:'source',entity:{id:'child',project_id:'p'}}},draft:{kind:'record_negative_search',fields:searchFields}}).artifact_version_id,'child');
assert.throws(()=>views.operationForDraft({projectId:'p',selection:{context:{entity_type:'source',entity:{id:'child',project_id:'foreign'}}},draft:{kind:'record_negative_search',fields:searchFields}}),/project|target/i);
console.log('source criticism operation builder checks passed');

(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc),calls=[],stored=new Map();
 const source={id:'search-source',project_id:'p',sha256:'a'.repeat(64),binary_build_id:null,artifact_id:'artifact',relative_path:'src/exact.js',kind:'file',availability:'available',local_bytes_available:true};
 const context={entity_type:'source',entity:source,related:Object.fromEntries(['source-derivations','observations','negative-searches'].map(resource=>[resource,{resource,params:{project_id:'p',subject_id:source.id}}]))};
 let saved;
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:{getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},api:async(path,options)=>{
  calls.push({path,options});if(options?.method==='POST'){const body=JSON.parse(options.body);saved=body.operations[0];return {project_id:'p',revision:'store:2',request_id:body.request_id,changed_records:[{type:'negative_search',id:saved.id}]};}
  if(path.includes('/context'))return {project_id:'p',revision:saved?'store:2':'store:1',context};
  return {project_id:'p',revision:'store:1',items:[],next_cursor:null};
 }});
 const all=n=>[n,...n.children.flatMap(all)],click=async label=>{const b=all(mount).find(n=>n.tagName==='button'&&n.textContent===label);assert.ok(b,'Missing control: '+label);assert.equal(b.disabled,false);await b.onclick();};
 await r.open('source',source.id);await click('Record scoped search');
 assert.equal(r.state().draft.fields.artifact_version_id,source.id);
 assert.deepEqual(r.state().draft.fields.coverage,{},'coverage is never assumed complete or partial');
 assert.ok(all(mount).some(n=>n.textContent.includes('not proof of absence')));
 const input=(label,value)=>{const n=mount.querySelectorAll('[data-research-field]').find(n=>n.dataset.researchField===label);assert.ok(n,label);n.value=value;n.oninput();};
 input('Search query','<script>needle</script>');input('Search method','Literal search');input('Searched range','lines 1–40 only');input('Search outcome','No matching text; generated code not searched');
 let form=all(mount).find(n=>n.tagName==='form');await form.onsubmit({preventDefault(){}});
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0,'explicit coverage required before save');
 input('Search coverage','partial');
 assert.equal(r.state().draft.fields.coverage.complete,false);
 assert.equal(r.state().draft.fields.query,'<script>needle</script>','rerender retains authored text literally');
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0,'choosing coverage does not submit');
 form=all(mount).find(n=>n.tagName==='form');await form.onsubmit({preventDefault(){}});
 assert.equal(saved.artifact_version_id,source.id);assert.deepEqual(saved.coverage,{complete:false});assert.equal(saved.searched_range,'lines 1–40 only');assert.ok(saved.id);
 assert.ok(!Object.hasOwn(saved,'rationale'),'no unrelated operation fields');assert.equal(r.state().draft,null);
 console.log('scoped search renderer checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc),calls=[];
 const child={id:'child',project_id:'p',sha256:'b'.repeat(64),binary_build_id:null,artifact_id:'artifact',relative_path:'report.js',kind:'file',availability:'available',local_bytes_available:true};
 const context={entity_type:'source',entity:child,related:Object.fromEntries(['source-derivations','observations','negative-searches'].map(resource=>[resource,{resource,params:{project_id:'p',subject_id:child.id}}]))};
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:null,api:async(path,options)=>{
  calls.push({path,options});if(path.includes('/context'))return {project_id:'p',revision:'store:1',context};
  return {project_id:'p',revision:'store:1',items:path.includes('index-runs')?[{id:'published-run',project_id:'p',state:'published'},{id:'draft-run',project_id:'p',state:'draft'}]:[{...child},{id:'parent',project_id:'p',sha256:'c'.repeat(64)}],next_cursor:null};
 }});
 const all=n=>[n,...n.children.flatMap(all)],click=async label=>{const b=all(mount).find(n=>n.tagName==='button'&&n.textContent===label);assert.ok(b,'Missing control: '+label);assert.equal(b.disabled,false);await b.onclick();};
 await r.load();await click('artifact versions');await click('Open source');assert.equal(r.state().selection.id,'child');
 await click('Record derivation assertion');assert.equal(r.state().draft.fields.scope.artifact_version_id,'child');
 await click('Browse parent source versions');
 const parentButtons=all(mount).filter(n=>n.tagName==='button'&&n.textContent==='Use as parent source');assert.equal(parentButtons.length,1,'child cannot be its own parent');await parentButtons[0].onclick();
 assert.equal(r.state().draft.fields.scope.derivation.parent_version_id,'parent');
 assert.deepEqual(r.state().draft.fields.evidence.map(x=>x.artifact_version_id),['child','parent']);
 assert.ok(r.state().draft.fields.evidence.every(x=>x.method===''&&x.limitations===''&&x.locator===''),'parent selection cannot invent evidence');
 await click('Browse published origin runs');assert.equal(all(mount).filter(n=>n.textContent==='Use recorded origin run').length,1,'draft runs cannot be selected');await click('Use recorded origin run');
 assert.equal(r.state().draft.fields.scope.derivation.index_run_id,'published-run');await click('Clear origin run');assert.ok(!Object.hasOwn(r.state().draft.fields.scope.derivation,'index_run_id'));
 assert.equal(calls.filter(c=>c.options?.method==='POST').length,0,'source browsing and parent/run selection never write');
 console.log('source navigation and derivation controls passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

(async()=>{
 const doc={activeElement:null,createElement(tag){return new Element(tag,this);}},mount=new Element('main',doc);
 const version=id=>({id,project_id:'p',sha256:'d'.repeat(64),binary_build_id:null,artifact_id:'artifact',relative_path:id,kind:'file',availability:'available',local_bytes_available:true});
 const related=Object.fromEntries(['source-derivations','observations','negative-searches'].map(resource=>[resource,{resource,params:{project_id:'p',subject_id:'child'}}]));
 const r=views.create({mount,document:doc,getContext:()=>({projectId:'p',principalId:'author',activePanel:'research'}),storage:null,api:async(path)=>{
  if(path.includes('/context')){const id=path.includes('/parent/')?'parent':'child';return {project_id:'p',revision:'store:1',context:{entity_type:'source',entity:version(id),related:Object.fromEntries(Object.entries(related).map(([k,v])=>[k,{...v,params:{...v.params,subject_id:id}}]))}};}
  const derivation={id:'revision',project_id:'p',finding_id:'finding',claim:'Authored relation',uncertainty:'Direction unresolved',is_current:false,archived:true,parent_version:version('parent'),child_version:version('child'),origin_run:{id:'shared-run',project_id:'p'},support_counts:{available:1,retracted:1,superseded:0}};
  const observation={id:'observation',project_id:'p',candidate_type:'observation',artifact_version_id:'child',identifier:'speed',namespace:'source',scope_key:'Ball',value:0,locator_key:{line:1},provenance:{method:'Read pinned fixture',limitations:'No runtime measurement'}};
  return {project_id:'p',revision:'store:1',subject_id:'child',items:path.includes('source-derivations')?[derivation,{...derivation,id:'revision2',finding_id:'finding2',is_current:true,archived:false}]:path.includes('/observations?')?[observation]:[{id:'search',project_id:'p',artifact_version_id:'child',query:'needle',method:'literal scan',searched_range:'lines 1–40',coverage:{complete:false},outcome:'No match in that range'}],next_cursor:null};
 }});
 const all=n=>[n,...n.children.flatMap(all)],click=async label=>{const b=all(mount).find(n=>n.textContent===label&&n.tagName==='button');assert.ok(b,label);await b.onclick();};
 await r.open('source','child');assert.ok(all(mount).some(n=>n.textContent==='d'.repeat(64)),'source hash readable');
 await click('Read source-derivations');for(const value of ['Historical assertion','Archived finding','Retracted citations: 1','Recorded origin run: shared-run','Shared recorded origin within this page: shared-run (2 assertions)','Open parent source','Open derived source','Open assertion finding'])assert.ok(all(mount).some(n=>n.textContent===value),value);
 await click('Read negative-searches');for(const value of ['Search query','needle','Searched range','lines 1–40','Partial coverage','No match in that range'])assert.ok(all(mount).some(n=>n.textContent===value),value);
 assert.ok(!all(mount).some(n=>n.textContent==='Recorded review outcome'),'search outcome must not imply independent review');
 await click('Read observations');
 const readable=all(mount).filter(n=>n.tagName!=='pre').map(n=>n.textContent);
 for(const value of ['Identifier','speed','Namespace','source','Scope','Ball','Observed value','0','Read pinned fixture','No runtime measurement'])assert.ok(readable.includes(value),`scoped observation displays ${value} outside collapsed provenance`);
 await click('Read source-derivations');await click('Open parent source');assert.equal(r.state().selection.id,'parent');assert.ok(!all(mount).some(n=>n.textContent==='Historical assertion'),'navigation does not present the previous source collection as new-source history');
 console.log('source history and scoped search presentation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
