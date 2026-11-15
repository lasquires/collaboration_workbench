/* Human research operations preserve the backend's evidence and review boundaries. */
(function(root){
'use strict';
const copy=value=>JSON.parse(JSON.stringify(value));
function text(value,label,max=20000){if(typeof value!=='string'||!value.trim()||Array.from(value).length>max)throw Error(label+' is required and must fit its limit.');return value}
function identity(value,label='Record identity'){return text(value,label,200)}
function revision(row){if(!row||!Number.isSafeInteger(row.record_revision)||row.record_revision<1)throw Error('Load the current record revision first.');return row.record_revision}
function sourceSupport(rows){
 if(!Array.isArray(rows)||rows.length>100)throw Error('Choose at most 100 pinned source records.');
 return rows.map(row=>{
  if(!row||typeof row!=='object'||Array.isArray(row))throw Error('Invalid source support.');
  if(Object.hasOwn(row,'state')&&row.state!=='available')throw Error('Withdrawn support cannot silently become available again; add newly reviewed support.');
  if(row._locatorInvalid)throw Error('Correct the structured source locator before saving.');
  const locator=row.locator;
  if(!(typeof locator==='string'&&locator.trim()||locator&&typeof locator==='object'&&!Array.isArray(locator)&&Object.keys(locator).length)||Array.from(JSON.stringify(locator)).length>10000)throw Error('An exact source locator is required.');
  return {artifact_version_id:identity(row.artifact_version_id,'Pinned artifact version'),locator:copy(locator),method:text(row.method,'Observation method',4000),limitations:text(row.limitations,'Source limitations',4000)};
 });
}
function findingSeed(observation={},taskId=null){
 const scope={};for(const name of ['artifact_version_id','occurrence_id','identifier','namespace','scope_key'])if(typeof observation[name]==='string'&&observation[name])scope[name]=observation[name];
 if(taskId)scope.task_id=identity(taskId,'Selected task UUID');
 const evidence=observation.artifact_version_id?[{artifact_version_id:observation.artifact_version_id,locator:copy(observation.locator_key||observation.locator||''),method:observation.provenance?.method||'',limitations:observation.provenance?.limitations||''}]:[];
 return {claim:'',classification:'unclassified',uncertainty:'',scope,supportMode:'replace',evidence};
}
function buildFindingOperation(kind,fields,current=null){
 if(!['create_finding','revise_finding'].includes(kind))throw Error('Choose finding creation or revision.');
 const scope={...(current?.scope?copy(current.scope):{}),...(fields.scope?copy(fields.scope):{})};
 for(const key of ['artifact_version_id','occurrence_id','area_id','task_id','identifier','namespace','scope_key'])if(scope[key]==='')delete scope[key];
 const values={claim:text(fields.claim,'Finding claim'),scope,classification:text(fields.classification,'Authored classification',120),uncertainty:typeof fields.uncertainty==='string'?fields.uncertainty:''};
 if(kind==='create_finding')return {op:kind,...(fields.id?{id:identity(fields.id)}:{}),...values,evidence:sourceSupport(fields.evidence||[])};
 if(!current||current.archived_at)throw Error('Restore the archived finding before revising it.');
 if(fields.supportMode==='replace')values.evidence=sourceSupport(fields.evidence||[]);
 else if(fields.supportMode!=='keep')throw Error('Choose explicitly whether to keep or replace source support.');
 return {op:kind,finding_id:identity(current.id),expected_record_revision:revision(current),fields:values,rationale:text(fields.rationale,'Revision rationale',4000)};
}
function buildFindingStateOperation(kind,current,rationale){
 if(!['archive_finding','restore_finding'].includes(kind))throw Error('Invalid finding state action.');
 if(kind==='archive_finding'&&current?.archived_at)throw Error('Finding is already archived.');
 if(kind==='restore_finding'&&!current?.archived_at)throw Error('Finding is not archived.');
 return {op:kind,finding_id:identity(current?.id),expected_record_revision:revision(current),rationale:text(rationale,'Finding state rationale',4000)};
}
function openQuestion(question){if(!question||!['open','investigating'].includes(question.state))throw Error('Reopen the question before proposing or selecting an answer.');identity(question.id,'Question identity');return question}
function buildAnswerOperation(fields,question){
 openQuestion(question);const links=fields.evidence_link_ids||[],evidence=sourceSupport(fields.evidence||[]);
 if(!Array.isArray(links)||new Set(links).size!==links.length||links.some(id=>typeof id!=='string'||!id)||links.length+evidence.length<1||links.length+evidence.length>100)throw Error('Choose 1–100 distinct pinned evidence links or new source records.');
 return {op:'create_answer',...(fields.id?{id:identity(fields.id)}:{}),question_id:question.id,expected_question_revision:revision(question),claim:text(fields.claim,'Proposed answer'),evidence_link_ids:copy(links),evidence};
}
function buildReviewOperation(fields,answer,question,principalId,reviews=[]){
 if(!answer||answer.question_id!==question?.id)throw Error('Answer belongs to another question.');
 if(answer.created_by===principalId)throw Error('The author cannot independently review their own answer.');
 if(!['accepted','rejected'].includes(fields.outcome))throw Error('Choose an independent review outcome explicitly.');
 if(fields.outcome==='accepted'&&(answer.review_state==='stale'||(answer.review_concerns||[]).length||question.state==='withdrawn'))throw Error('Stale or withdrawn support cannot be accepted.');
 const supersedes=fields.supersedes||[];
 if(!Array.isArray(supersedes)||supersedes.length>100||new Set(supersedes).size!==supersedes.length||supersedes.some(id=>!reviews.some(row=>row.id===id&&row.answer_id===answer.id&&row.reviewer_id===principalId)))throw Error('Select only your own review records for this answer.');
 return {op:'review_answer',answer_id:identity(answer.id),expected_question_revision:revision(question),outcome:fields.outcome,rationale:text(fields.rationale,'Independent review rationale',4000),supersedes:copy(supersedes)};
}
function buildResolutionOperation(answer,question,rationale){
 openQuestion(question);
 if(answer?.question_id!==question.id)throw Error('Answer belongs to another question.');
 if(answer.review_state!=='accepted'||(answer.review_concerns||[]).length)throw Error('Resolution requires current independent accepted review.');
 return {op:'resolve_question',question_id:question.id,expected_record_revision:revision(question),answer_id:identity(answer.id),rationale:text(rationale,'Resolution rationale',4000)};
}
function buildQuestionStateOperation(fields,question){
 if(!['open','investigating','withdrawn'].includes(fields.state)||fields.state===(question.recorded_state||question.state))throw Error('Choose a changed open, investigating or withdrawn state.');
 return {op:'set_question_state',question_id:identity(question.id),expected_record_revision:revision(question),state:fields.state,rationale:text(fields.rationale,'Question state rationale',4000)};
}
function buildEvidenceOperation(fields,link,replacement=null){
 if(!link||link.state!=='available')throw Error('Only currently available support can be withdrawn or superseded.');
 if(!['retracted','superseded'].includes(fields.state))throw Error('Choose withdrawal or supersession explicitly.');
 const operation={op:'set_evidence_state',evidence_link_id:identity(link.id),expected_state:'available',state:fields.state,reason:text(fields.reason,'Evidence change reason',4000)};
 if(fields.state==='superseded'){
  if(!replacement||replacement.state!=='available'||replacement.id===link.id||replacement.project_id!==link.project_id)throw Error('Choose a distinct available replacement in this project scope.');
  operation.replacement_id=identity(replacement.id);
 }
 return operation;
}
function operationForDraft(state,{latest=false,choices={}}={}){
 const draft=state.draft, context=state.selection?.context;
 if(!draft)throw Error('Open an authored draft first.');
 const fields=copy(draft.fields), kind=draft.kind, entity=context?.entity;
 function target(key){
  const expectedType={finding_id:'finding',question_id:'question',answer_id:'answer',evidence_link_id:'evidence'}[key];
  if(!entity||context.entity_type!==expectedType||entity.id!==fields[key]||(state.projectId&&entity.project_id!==state.projectId))throw Error('Read the exact draft target type and project context first.');
  return copy(entity);
 }
 function guarded(row,key){
  if(!latest)row.record_revision=fields[key];
  return row;
 }
 function choose(key,authored,current){
  if(!['draft','latest'].includes(choices[key]))throw Error('Choose draft or latest explicitly for '+key+'.');
  return copy(choices[key]==='draft'?authored:current);
 }
 if(kind==='create_finding')return buildFindingOperation(kind,fields);
 if(kind==='revise_finding'){
  const current=guarded(target('finding_id'),'expected_record_revision');
  const values={...fields.fields,rationale:fields.rationale,
   supportMode:fields._supportMode||(Object.hasOwn(fields.fields,'evidence')?'replace':'keep')};
  if(latest){
   for(const key of ['claim','scope','classification','uncertainty'])values[key]=choose(key,values[key],entity[key]);
   if(!['draft','latest'].includes(choices.evidence))throw Error('Choose draft or latest explicitly for evidence.');
   if(choices.evidence==='latest'){values.supportMode='keep';delete values.evidence;}
  }
  return buildFindingOperation(kind,values,current);
 }
 if(['archive_finding','restore_finding'].includes(kind))return buildFindingStateOperation(kind,guarded(target('finding_id'),'expected_record_revision'),fields.rationale);
 if(kind==='set_evidence_state')return buildEvidenceOperation(fields,target('evidence_link_id'),fields._replacement);
 let question;
 if(['create_answer','set_question_state','resolve_question'].includes(kind))question=target('question_id');
 else if(kind==='review_answer'){
  target('answer_id');question=copy(context.question);
  if(!question||question.id!==entity.question_id)throw Error('Read the answer question context first.');
 }else throw Error('Unsupported research draft action.');
 question=guarded(question,['create_answer','review_answer'].includes(kind)?'expected_question_revision':'expected_record_revision');
 if(kind==='create_answer')return buildAnswerOperation(fields,question);
 if(kind==='review_answer')return buildReviewOperation(fields,entity,question,state.principalId,[...(fields._reviewRows||[]),...((state.pages?.['answer-reviews']?.params?.subject_id===entity.id)?state.pages['answer-reviews'].items:[])]);
 if(kind==='set_question_state')return buildQuestionStateOperation(fields,question);
 const answer=fields._answer;
 if(!answer||answer.id!==fields.answer_id)throw Error('Read the selected answer before resolving.');
 if(latest&&frozenAnswerRevisionMismatch())throw Error('Read the selected answer again in the latest question context before reconciling.');
 function frozenAnswerRevisionMismatch(){return fields._answerQuestionRevision!==question.record_revision;}
 return buildResolutionOperation(answer,question,fields.rationale);
}

function canResearch(state,capability){const caps=state.capabilities;return caps?.authenticated!==true||Array.isArray(caps.granted)&&caps.granted.includes(capability);}
function create({api,getContext,onDirty=()=>{},notice=()=>{},mount=null,storage,document:doc=root.document}={}){
 const host=mount||doc?.querySelector('#researchWorkspace');
 if(!host)throw Error('Research workspace mount is required.');
 const Session=root.WorkspaceResearchSession||(typeof require==='function'?require('./workspace-research-session.js'):null);
 let session,localError='',collection='findings',related=null;
 const choices={},invalidInputs=new Map();
 function node(tag,value,cls){const n=doc.createElement(tag);if(value!==undefined)n.textContent=String(value);if(cls)n.className=cls;return n;}
 function button(label,action,disabled=false){const b=node('button',label);b.type='button';b.disabled=disabled;b.onclick=()=>run(action);return b;}
 async function run(action){try{localError='';await action();}catch(e){localError=e.message;notice(e.message,'error');}render(session.state());}
 async function confirmSave(operation,retry=false){
  const result=retry?await session.retry():await session.submit(operation);
  if(session.state().pendingBody){notice('Saved outcome requires recovery-entry cleanup before continuing.','error');return;}
  notice('Research action confirmed saved.','ok');
  const changed=result.changed_records||[];
  const primary=changed.find(row=>['finding','answer','question'].includes(row.type));
  if(primary){related=null;await session.open(primary.type,primary.id);}
  else if(session.state().selection)await session.refreshContext();
 }

 function field(parent,label,value,save,{json=false,select=null,key=label}={}){
  const wrap=node('label',undefined,'field'),caption=node('span',label);let input;
  if(select){input=node('select');input.append(node('option','Choose explicitly…'));input.children[0].value='';for(const v of select){const o=node('option',v);o.value=v;input.append(o);}}
  else input=node('textarea');
  input.value=json?JSON.stringify(value??{},null,2):value??'';input.dataset.researchField=key;input.setAttribute('aria-label',label);
  if(invalidInputs.has(key)){const invalid=invalidInputs.get(key);input.value=invalid.value;input.setCustomValidity(invalid.error);}
  input.oninput=()=>{
   try{const parsed=json?JSON.parse(input.value):input.value;invalidInputs.delete(key);localError='';save(parsed);}
   catch(e){localError=label+': '+e.message;invalidInputs.set(key,{value:input.value,error:localError});}
   // JSON parse failures retain visible text and disable submission until corrected.
   input.setCustomValidity(invalidInputs.get(key)?.error||'');const status=host.querySelector('[data-editor-error]');if(status)status.textContent=localError;
  };
  wrap.append(caption,input);parent.append(wrap);return input;
 }
 function summary(parent,label,value){parent.append(node('h4',label),node('pre',typeof value==='string'?value:JSON.stringify(value,null,2),'research-record'));}
 async function browse(resource,params={},cursor=null){related=params.subject_id?{resource,params}:null;const page=await session.readPage(resource,params,cursor);const state=session.state();if(resource==='answer-reviews'&&state.draft?.kind==='review_answer'&&!state.pendingBody&&page){const ids=state.draft.fields.supersedes||[];const rows=[...(state.draft.fields._reviewRows||[]),...page.items].filter(row=>ids.includes(row.id)&&row.answer_id===state.draft.fields.answer_id&&row.reviewer_id===state.principalId);session.change('_reviewRows',Array.from(new Map(rows.map(row=>[row.id,row])).values()));}}
 function page(parent,resource,params={}){
  const p=session.state().pages[resource];if(!p)return;
  parent.append(node('p',`Showing ${p.items.length} records from one snapshot. ${p.next_cursor?'More records are available.':'End of this collection.'}`,'muted'));
  for(const row of p.items){const card=node('article',undefined,'card');card.append(node('strong',row.claim||row.text||row.reason||row.id));
   const type=resource==='findings'?'finding':resource==='questions'?'question':resource==='answers'?'answer':['evidence-links','answer-support'].includes(resource)?'evidence':null;
   if(resource==='answers'&&session.state().selection?.type==='question')card.append(button('Select reviewed answer for resolution',async()=>{const context=await session.inspectAnswer(row.id);if(!context)return;buildResolutionOperation(context.entity,context.question,'Check eligibility');start('resolve_question',{question_id:context.question.id,expected_record_revision:context.question.record_revision,answer_id:row.id,rationale:'',_answer:context.entity,_answerQuestionRevision:context.question.record_revision});},Boolean(session.state().draft||session.state().pendingBody)||row.review_state!=='accepted'));
   if(type)card.append(button('Open '+type,()=>session.open(type,row.id),Boolean(session.state().draft||session.state().pendingBody)));
   const state=session.state(),draft=state.draft,editable=draft&&!state.pendingBody&&!state.busy;
   if(resource==='artifact-versions'&&editable&&['create_finding','revise_finding','create_answer'].includes(draft.kind))card.append(button('Use pinned version',()=>{
    const latest=session.state().draft,revise=latest.kind==='revise_finding';
    const values=revise?latest.fields.fields:latest.fields,rows=values.evidence||[];
    if(rows.length+(latest.fields.evidence_link_ids?.length||0)>=100)throw Error('At most 100 source records may support this action.');
    const evidence=[...rows,{artifact_version_id:row.id,locator:'',method:'',limitations:''}];
    if(revise){session.change('_supportMode','replace');session.change('fields',{...values,evidence});}else session.change('evidence',evidence);
   }));
   if(['evidence-links','answer-support'].includes(resource)&&editable&&draft.kind==='create_answer'){
    const selected=(draft.fields.evidence_link_ids||[]).includes(row.id);
    card.append(button(selected?'Remove pinned link':'Use pinned link',()=>{
     const latest=session.state().draft.fields,ids=latest.evidence_link_ids||[];
     if(!selected&&ids.length+(latest.evidence?.length||0)>=100)throw Error('At most 100 source records may support this answer.');
     session.change('evidence_link_ids',selected?ids.filter(id=>id!==row.id):[...ids,row.id]);
    },!selected&&row.state!=='available'));
   }
   if(resource==='answer-reviews'&&editable&&draft.kind==='review_answer'&&row.answer_id===draft.fields.answer_id&&row.reviewer_id===state.principalId){
    const selected=(draft.fields.supersedes||[]).includes(row.id);
    card.append(button(selected?'Keep prior review':'Supersede my review',()=>{
     const latest=session.state().draft.fields,ids=latest.supersedes||[];
     if(!selected&&ids.length>=100)throw Error('At most 100 own reviews may be superseded.');
     session.change('_reviewRows',Array.from(new Map([...(latest._reviewRows||[]),row].map(item=>[item.id,item])).values()));
     session.change('supersedes',selected?ids.filter(id=>id!==row.id):[...ids,row.id]);
    }));
   }
   if(resource==='evidence-links'&&editable&&draft.kind==='set_evidence_state')card.append(button('Use as replacement support',()=>session.change('_replacement',copy(row)),row.state!=='available'||row.id===draft.fields.evidence_link_id));
   summary(card,'Recorded details',row);parent.append(card);
  }
  parent.append(button('Restart collection',()=>browse(resource,params)),button('Next 20',()=>browse(resource,params,p.next_cursor),!p.next_cursor));
 }
 function start(kind,seed){for(const k of Object.keys(choices))delete choices[k];session.edit(kind,seed);}
 function actions(parent,state){
  const c=state.selection?.context,e=c?.entity;if(!e)return;
  const locked=Boolean(state.draft||state.pendingBody||state.busy)||!canResearch(state,'contribute');
  if(c.entity_type==='finding'){
   parent.append(button('Revise finding',()=>start('revise_finding',{finding_id:e.id,expected_record_revision:e.record_revision,fields:{claim:e.claim,scope:copy(e.scope||{}),classification:e.classification,uncertainty:e.uncertainty||''},rationale:'',_supportMode:'keep'}),locked||Boolean(e.archived_at)));
   const kind=e.archived_at?'restore_finding':'archive_finding';parent.append(button(e.archived_at?'Restore finding':'Archive finding',()=>start(kind,{finding_id:e.id,expected_record_revision:e.record_revision,rationale:''}),locked));
  }
  if(c.entity_type==='question'){
   parent.append(button('Propose pinned answer',()=>start('create_answer',{question_id:e.id,expected_question_revision:e.record_revision,claim:'',evidence_link_ids:[],evidence:[]}),locked||!['open','investigating'].includes(e.state)));
   parent.append(button('Change question state',()=>start('set_question_state',{question_id:e.id,expected_record_revision:e.record_revision,state:'',rationale:''}),locked));
  }
  if(c.entity_type==='answer'){
   parent.append(button('Independent review',()=>start('review_answer',{answer_id:e.id,expected_question_revision:c.question.record_revision,outcome:'',rationale:'',supersedes:[],_reviewRows:[]}),locked||e.created_by===state.principalId||!canResearch(state,'review')));
   parent.append(button('Open question',()=>session.open('question',e.question_id),locked));
  }
  if(c.entity_type==='evidence')parent.append(button('Withdraw or supersede support',()=>start('set_evidence_state',{evidence_link_id:e.id,expected_state:'available',state:'',reason:''}),locked||e.state!=='available'));
  for(const [label,descriptor] of Object.entries(c.related||{}))parent.append(button('Read '+label,()=>browse(descriptor.resource,descriptor.params),state.busy));
 }
 function editor(parent,state){
  const d=state.draft||(state.pendingBody?{kind:JSON.parse(state.pendingBody).operations[0].op,fields:{}}:null);if(!d)return;const f=d.fields,kind=d.kind;
  const form=node('form',undefined,'card');form.append(node('h3',kind.replaceAll('_',' ')),node('p','Authored assertions and independent review remain distinct from verified forensic evidence.','muted'));
  if(state.pendingBody){summary(form,'Frozen request awaiting confirmation',JSON.parse(state.pendingBody));form.append(button('Retry exact request',()=>confirmSave(JSON.parse(state.pendingBody).operations[0],true),state.busy));parent.append(form);return;}
  const finding=kind==='revise_finding',values=finding?f.fields:f;
  function save(key,value){if(finding&&['claim','scope','classification','uncertainty','evidence'].includes(key))session.change('fields',{...session.state().draft.fields.fields,[key]:value});else session.change(key,value);}
  if(['create_finding','revise_finding','create_answer'].includes(kind))field(form,'Claim',values.claim,v=>save('claim',v));
  if(['create_finding','revise_finding'].includes(kind)){
   field(form,'Scope',values.scope,v=>save('scope',v),{json:true});field(form,'Classification',values.classification,v=>save('classification',v));field(form,'Uncertainty',values.uncertainty,v=>save('uncertainty',v));
   if(finding)field(form,'Source support',f._supportMode||(Object.hasOwn(values,'evidence')?'replace':'keep'),v=>session.change('_supportMode',v),{select:['keep','replace']});
  }
  if(['create_finding','revise_finding','create_answer'].includes(kind)&&(!finding||(f._supportMode||'keep')==='replace')){
   form.append(button('Browse pinned versions',()=>{collection='artifact-versions';return browse('artifact-versions');},state.busy));
   for(const [index,row] of (values.evidence||[]).entries()){
    const support=node('section',undefined,'card');support.append(node('h4','Pinned source '+(index+1)),node('p','Version '+row.artifact_version_id));
    function changeSupport(key,value){const current=session.state().draft.fields;const rows=copy((finding?current.fields:current).evidence||[]);rows[index]={...rows[index],[key]:value};save('evidence',rows);}
    field(support,'Source locator '+(index+1),row.locator,v=>changeSupport('locator',v),{json:typeof row.locator==='object',key:'source-locator-'+index});
    field(support,'Observation method '+(index+1),row.method,v=>changeSupport('method',v),{key:'source-method-'+index});
    field(support,'Source limitations '+(index+1),row.limitations,v=>changeSupport('limitations',v),{key:'source-limits-'+index});
    support.append(button('Remove source '+(index+1),()=>{if(invalidInputs.size)throw Error('Correct unfinished structured fields before removing a source.');save('evidence',(finding?session.state().draft.fields.fields:session.state().draft.fields).evidence.filter((_,i)=>i!==index));}));form.append(support);
   }
  }
  if(kind==='create_answer'){form.append(button('Browse evidence links',()=>{collection='evidence-links';return browse('evidence-links');},state.busy));summary(form,'Selected pinned link identities',f.evidence_link_ids||[]);for(const id of f.evidence_link_ids||[])form.append(button('Remove selected link '+id,()=>save('evidence_link_ids',session.state().draft.fields.evidence_link_ids.filter(value=>value!==id))));}
  if(kind==='review_answer'){
   field(form,'Independent outcome',f.outcome,v=>save('outcome',v),{select:['accepted','rejected']});
   summary(form,'Own reviews selected for supersession',f.supersedes||[]);const descriptor=state.selection?.context?.related?.reviews;if(descriptor)form.append(button('Browse my prior reviews',()=>browse(descriptor.resource,descriptor.params),state.busy));
  }
  if(kind==='set_question_state')field(form,'New question state',f.state,v=>save('state',v),{select:['open','investigating','withdrawn']});
  if(kind==='set_evidence_state'){
   field(form,'Support state',f.state,v=>save('state',v),{select:['retracted','superseded']});
   if(f.state==='superseded')field(form,'Reviewed replacement evidence record',f._replacement||{},v=>save('_replacement',v),{json:true});
   field(form,'Reason',f.reason,v=>save('reason',v));
  }else if(!['create_finding','create_answer'].includes(kind))field(form,'Rationale',f.rationale,v=>save('rationale',v));
  if(kind==='resolve_question'){summary(form,'Selected answer',f._answer||{id:f.answer_id,status:'Read current answer context before rebuilding this recovered draft.'});form.append(button('Read selected answer context',async()=>{const context=await session.inspectAnswer(f.answer_id);if(context){session.change('_answer',context.entity);session.change('_answerQuestionRevision',context.question.record_revision);}},state.busy));}
  const error=node('p',localError);error.dataset.editorError='';error.setAttribute('role','alert');form.append(error);
  if(d.needsReconcile){
   form.append(button('Read latest context',()=>session.refreshContext(),state.busy));
   if(finding)for(const key of ['claim','scope','classification','uncertainty','evidence'])field(form,'Reconcile '+key,choices[key]||'',v=>{choices[key]=v;},{select:['draft','latest'],key:'choice-'+key});
   form.append(button('Adopt explicitly reviewed choices',()=>{if(!form.reportValidity())throw Error('Correct unfinished structured fields before reconciling.');return session.reconcile(operationForDraft(session.state(),{latest:true,choices}),true);},state.busy));
  }
  const submit=node('button','Save reviewed action');submit.type='submit';submit.disabled=state.busy||d.needsReconcile||!canResearch(state,'contribute')||(kind==='review_answer'&&!canResearch(state,'review'));form.append(submit,button('Discard draft',()=>session.discard(),state.busy));
  form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;return run(()=>confirmSave(operationForDraft(session.state())));};parent.append(form);
 }
 function render(state){
  if(!state.draft)invalidInputs.clear();
  // Retain active editing text and selection across synchronous controller emissions.
  const active=doc.activeElement,key=active?.dataset?.researchField;
  const caret=key?{value:active.value,start:active.selectionStart,end:active.selectionEnd}:null;
  const box=node('div',undefined,'research-workspace');box.append(node('h2','Findings & answers'));if(getContext()?.taskId)box.append(node('p','Task context: '+getContext().taskId,'muted'));
  const status=node('p',localError||state.error||state.phase);status.setAttribute('role','status');box.append(status);
  const nav=node('div',undefined,'actions');
  for(const resource of ['findings','questions','artifact-versions','evidence-links'])nav.append(button(resource.replaceAll('-',' '),()=>{collection=resource;related=null;return browse(resource);},state.busy));
  nav.append(button('New finding',()=>start('create_finding',findingSeed({},getContext()?.taskId)),Boolean(state.draft||state.pendingBody||state.busy)||!canResearch(state,'contribute')));box.append(nav);
  const columns=node('div',undefined,'research-columns'),list=node('section'),detail=node('section'),edit=node('section');
  list.setAttribute('aria-label','Research records');detail.setAttribute('aria-label','Selected research context');edit.setAttribute('aria-label','Research authoring');
  page(list,related?.resource||collection,related?.params||{});
  if(state.selection?.context){summary(detail,'Current '+state.selection.type,state.selection.context.entity);if(state.selection.context.question)summary(detail,'Current question',state.selection.context.question);actions(detail,state);}
  else detail.append(node('p','Open a record to inspect its pinned evidence, history and current concerns.'));
  editor(edit,state);columns.append(list,detail,edit);box.append(columns);host.replaceChildren(box);
  if(key){const replacement=Array.from(host.querySelectorAll('[data-research-field]')).find(n=>n.dataset.researchField===key);if(replacement){replacement.value=caret.value;replacement.focus();if(typeof replacement.setSelectionRange==='function'&&caret.start!==null)replacement.setSelectionRange(caret.start,caret.end);}}
 }
 session=Session.createSession({api,getContext,onDirty,onChange:state=>{if(session)render(state);},storage:storage===undefined?root.localStorage:storage});
 render(session.state());
 return {...session,open:async(type,id)=>{related=null;await session.open(type,id);},seedFinding:(observation,taskId)=>start('create_finding',findingSeed(observation,taskId||getContext()?.taskId))};
}
const api={canResearch,create,operationForDraft,findingSeed,buildFindingOperation,buildFindingStateOperation,buildAnswerOperation,buildReviewOperation,buildResolutionOperation,buildQuestionStateOperation,buildEvidenceOperation};
root.WorkspaceResearch=api;if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:this);
