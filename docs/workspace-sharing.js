/* Explicit journal disclosure review. Original notes never enter the save payload. */
(function(root){
'use strict';
const copy=value=>JSON.parse(JSON.stringify(value));
const RECOVERY_KEY='workbench.journal-sharing.pending.v1';
function recoverBody(storage){
 const raw=storage?.getItem(RECOVERY_KEY);if(!raw)return null;
 const body=JSON.parse(raw),op=body.operations?.[0];
 if(typeof raw!=='string'||raw.length>100000||!body||Object.keys(body).sort().join(',')!=='actor,expected_revision,operations,project_id,request_id'||!Array.isArray(body.operations)||body.operations.length!==1||!op||Object.keys(op).sort().join(',')!=='choices,cursor,limit,op,review_digest'||op.op!=='review_journal_sharing'||!op.choices||typeof op.choices!=='object'||Array.isArray(op.choices)||!Object.keys(op.choices).length||Object.keys(op.choices).length>100||!Object.values(op.choices).every(mode=>['redacted','omitted'].includes(mode))||!Number.isInteger(op.limit)||op.limit<1||op.limit>100||!(op.cursor===null||typeof op.cursor==='string')||typeof op.review_digest!=='string'||typeof body.project_id!=='string'||typeof body.request_id!=='string'||typeof body.expected_revision!=='string'||!body.actor||Object.keys(body.actor).join(',')!=='principal_id'||typeof body.actor.principal_id!=='string')throw Error('The browser contains an invalid pending sharing request. Keep it for recovery; no request was sent.');
 return raw;
}
function reloadLabel(state){if(state.phase==='stale'||state.phase==='rejected')return 'Load fresh review · discard choices';if(state.acknowledged||Object.keys(state.choices).length)return 'Reload review · discard current choices';return 'Reload sharing review';}
function createSession({api,getContext,onChange=()=>{},onDirty=()=>{},requestId=()=>root.crypto.randomUUID(),storage=null,requirePersistence=false}){
 let projectId=null,page=null,principal=null,canReview=false,phase='idle',error='',choices=new Map(),ack=false,pendingBody=null,sequence=0;
 try{pendingBody=recoverBody(storage);if(pendingBody){projectId=JSON.parse(pendingBody).project_id;phase='uncertain';error='A pending sharing request was recovered. Retry it to confirm the original outcome.';}}catch(failure){phase='recovery-error';error=failure.message;}
 const state=()=>({projectId,page:page?copy(page):null,canReview,phase,error,choices:Object.fromEntries(choices),acknowledged:ack,pendingBody});
 const emit=()=>{const context=getContext();if(projectId!==context.projectId)return;if(context.activePanel===undefined||context.activePanel==='sharing')onDirty(choices.size>0||ack||pendingBody!==null||phase==='submitting');onChange(state())};
 const locked=()=>pendingBody!==null||phase==='submitting';
 async function load(cursor=null,{discardChoices=false}={}){
  if(phase==='recovery-error'){emit();throw Error(error);}
  if(locked()){if(projectId===getContext().projectId){emit();return}throw Error('A pending save must be confirmed in its original project before loading another review.');}
  if((choices.size||ack)&&!discardChoices)throw Error('Save or discard the current review choices first.');
  const mine=++sequence,project=getContext().projectId;
  if(!project)throw Error('Choose a project first.');
  projectId=project;choices=new Map();ack=false;page=null;error='';phase='loading';emit();
  try{
   const [review,caps]=await Promise.all([api('/v1/journal-sharing-review?'+new URLSearchParams({project_id:project,limit:'20',...(cursor?{cursor}:{})})),api('/v1/projects/'+encodeURIComponent(project)+'/capabilities')]);
   if(mine!==sequence||getContext().projectId!==project)return;
   if(review.project_id!==project||typeof review.revision!=='string'||typeof review.review_digest!=='string'||!Array.isArray(review.items))throw Error('The review response did not match this project.');
   page=copy(review);principal=caps.capabilities?.local_principal_id;
   canReview=Boolean(principal)&&(!caps.capabilities?.authenticated||['contribute','review'].every(role=>caps.capabilities.granted?.includes(role)));
   phase='ready';emit();
  }catch(failure){if(mine!==sequence||getContext().projectId!==project)return;phase=failure.status===409?'stale':'load-error';error=failure.message;emit()}
 }
 function choose(identity,mode){
  if(locked())throw Error('The pending request is locked.');
  if(phase!=='ready')throw Error('Load a fresh review before choosing.');
  if(!canReview)throw Error('Current project review permission is required.');
  const item=page.items.find(item=>item.source_event_id===identity);
  if(!item||mode&&!Object.hasOwn(item.proposals||{},mode))throw Error('Choose an offered public projection.');
  if(mode)choices.set(identity,mode);else choices.delete(identity);emit();
 }
 function acknowledge(value){if(locked())throw Error('The pending request is locked.');ack=value===true;emit()}
 async function save(){
  if(getContext().projectId!==projectId)throw Error('Return to the original project before saving this review.');
  if(getContext().activePanel!==undefined&&getContext().activePanel!=='sharing')throw Error('Return to sharing review before saving.');
  if(phase==='submitting')throw Error('A save is already pending.');
  if(!pendingBody){
   if(phase!=='ready')throw Error('Load a fresh review before saving.');
   if(!canReview)throw Error('Current project review permission is required.');
   if(!page.items.length||choices.size!==page.items.length||page.items.some(item=>!Object.hasOwn(item.proposals||{},choices.get(item.source_event_id))))throw Error('Choose a public copy or omission for every note on this page.');
   if(!ack)throw Error('Please acknowledge that published copies remain in shared history.');
   const prepared=JSON.stringify({project_id:projectId,actor:{principal_id:principal},expected_revision:page.revision,request_id:requestId(),operations:[{op:'review_journal_sharing',review_digest:page.review_digest,limit:page.limit,cursor:page.cursor,choices:Object.fromEntries(choices)}]});
   if(requirePersistence&&!storage)throw Error('Browser session storage is required to preserve the sharing request before sending.');
   if(storage)storage.setItem(RECOVERY_KEY,prepared);
   pendingBody=prepared;
  }
  const wasUncertain=phase==='uncertain';
  phase='submitting';error='';emit();
  try{
   const expected=JSON.parse(pendingBody),result=await api('/v1/transactions',{method:'POST',headers:{'Content-Type':'application/json'},body:pendingBody});
   if(result.request_id!==expected.request_id||typeof result.revision!=='string'||!result.changed_records?.some(row=>row.type==='journal_sharing_review'))throw Error('The save response did not confirm this request.');
   let cleanupWarning='';try{if(storage)storage.removeItem(RECOVERY_KEY)}catch{cleanupWarning='Saved, but the browser recovery entry could not be cleared. A later exact replay is safe.'}
   pendingBody=null;choices=new Map();ack=false;phase='saved';error=cleanupWarning;emit();return result;
  }catch(failure){
   if(!wasUncertain&&failure.status>=400&&failure.status<500){try{if(storage)storage.removeItem(RECOVERY_KEY)}catch{}pendingBody=null;phase=failure.status===409?'stale':'rejected';error=failure.status===409?'The project changed. Load a fresh review and choose again.':'These choices were not saved: '+failure.message;}
   else{phase='uncertain';error=wasUncertain&&failure.status>=400&&failure.status<500?'The original outcome is still unconfirmed. Restore access or resolve the server error, then retry this exact request.':'Save outcome is uncertain. Retry sends the exact same request and choices.';}
   emit();return null;
  }
 }
 function discard(){if(locked()||phase==='recovery-error')return false;++sequence;choices=new Map();ack=false;page=null;phase='idle';error='';emit();return true}
 return{state,load,choose,acknowledge,save,discard};
}
function create({api,notice,getContext,onDirty}){
 const container=document.querySelector('#sharingReview');
 let originalTicket=0,focusKey=null;
 const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);return node};
 const run=fn=>async()=>{try{await fn()}catch(error){notice(error.message,'error')}};
 let storage=null;try{storage=root.sessionStorage}catch{}
 const session=createSession({api,getContext,onDirty,onChange:render,storage,requirePersistence:true});
 async function original(item,target,cursor=null){
  const ticket=++originalTicket,project=getContext().projectId,digest=session.state().page?.review_digest;
  target.replaceChildren(el('p','Loading original task history…'));
  try{
   const result=await api('/v1/tasks/'+encodeURIComponent(item.task_id)+'/history?'+new URLSearchParams({project_id:project,limit:'100',...(cursor?{cursor}:{})}));
   if(ticket!==originalTicket||getContext().projectId!==project||session.state().page?.review_digest!==digest)return;
   const note=result.items?.find(note=>note.event_id===item.source_event_id&&!note.disclosure);
   target.replaceChildren();
   if(note){target.append(el('h4','Original local note'),el('p','This remains unchanged. Only your chosen public copy or omission is shared.'),el('pre',JSON.stringify(note.details,null,2)));}
   else if(result.next_cursor){const more=el('button','Search next history page');more.type='button';more.onclick=run(()=>original(item,target,result.next_cursor));target.append(el('p','The original is outside this history page.'),more);}
   else target.append(el('p','The original note was not found in the available task history. Reload the review before saving.'));
  }catch(error){if(ticket===originalTicket&&getContext().projectId===project)target.replaceChildren(el('p','Original history could not be loaded: '+error.message))}
 }
 function render(state){
  focusKey=document.activeElement?.getAttribute('data-sharing-focus')||focusKey;
  ++originalTicket;container.replaceChildren();
  const locked=['submitting','uncertain'].includes(state.phase),busy=state.phase==='loading';
  const reload=el('button',reloadLabel(state));reload.type='button';reload.setAttribute('data-sharing-focus','reload');reload.disabled=locked||busy;
  reload.onclick=run(()=>session.load(null,{discardChoices:true}));container.append(reload);
  if(state.error){const message=el('p',state.error);message.setAttribute('role','alert');container.append(message)}
  if(busy){container.append(el('p','Loading safe public proposals…'));return}
  if(state.phase==='saved')container.append(el('p','Sharing choices saved. Reload the review to continue with current pages.'));
  if(!state.page){if(state.pendingBody){const request=JSON.parse(state.pendingBody);container.append(el('p','Recovered '+Object.keys(request.operations[0].choices).length+' reviewed sharing choices. No new request has been sent.'));const retry=el('button','Retry exact same sharing request');retry.type='button';retry.disabled=state.phase==='submitting';retry.onclick=run(()=>session.save());container.append(retry)}return;}
  if(!state.canReview)container.append(el('p','You can inspect the proposed public copies. A project reviewer must approve sharing.'));
  container.append(el('p','Reviewed '+state.page.inspected+' journal entries on this page. '+state.page.items.length+' contain local information.'));
  if(!state.page.items.length)container.append(el('p','No journal notes on this page need a local-path disclosure. Other project records may still need separate review.'));
  const cards=el('div');cards.className='cards';
  for(const item of state.page.items){
   const box=el('article');box.className='card';const witness=Object.values(item.proposals||{})[0];box.append(el('h3',witness?.kind?'Journal '+witness.kind:'Journal note'));const task=el('p','Task: '+item.task_id);task.className='muted';box.append(task);const provenance=el('details');provenance.append(el('summary','Original source provenance'),el('p','Original source hash: '+item.source_sha256));box.append(provenance);
   if(item.current_choice)box.append(el('p','Current local sharing choice: '+item.current_choice));
   const originalBox=el('div'),read=el('button','Read original note');read.type='button';read.setAttribute('data-sharing-focus','original:'+item.source_event_id);read.disabled=locked;read.onclick=run(()=>original(item,originalBox));box.append(read,originalBox);
   const label=el('label','Public version to share');label.className='field';const select=el('select');select.setAttribute('aria-label','Public version for note '+item.source_event_id);select.setAttribute('data-sharing-focus','choice:'+item.source_event_id);
   const blank=el('option','Choose a public version');blank.value='';select.append(blank);
   for(const mode of ['redacted','omitted'])if(Object.hasOwn(item.proposals||{},mode)){const option=el('option',mode==='redacted'?'Redacted copy':'Omission notice');option.value=mode;select.append(option)}
   select.value=state.choices[item.source_event_id]||'';select.disabled=locked||state.phase!=='ready'||!state.canReview||item.unsupported;
   select.onchange=()=>{try{session.choose(item.source_event_id,select.value)}catch(error){notice(error.message,'error')}};label.append(select);box.append(label);
   if(item.unsupported)box.append(el('p','This note has unsupported identity or attribution data. It remains blocked for separate review.'));
   for(const [mode,proposal]of Object.entries(item.proposals||{})){const section=el('section');section.append(el('h4',mode==='redacted'?'Proposed redacted copy':'Proposed omission notice'));let details;try{details=JSON.stringify(JSON.parse(proposal.details_json),null,2)}catch{details='Public details are unavailable.'}try{for(const [key,value] of Object.entries(JSON.parse(proposal.details_json))){section.append(el('strong',key[0].toUpperCase()+key.slice(1)));if(typeof value==='string')section.append(el('p',value));else section.append(el('pre',JSON.stringify(value,null,2)))}}catch{section.append(el('p',details))}box.append(section)}
   cards.append(box);
  }
  container.append(cards);
  if(state.page.items.length){
   const acknowledgment=el('label');acknowledgment.className='sharing-ack';const check=el('input');check.type='checkbox';check.setAttribute('data-sharing-focus','ack');check.checked=state.acknowledged;check.disabled=locked||state.phase!=='ready'||!state.canReview;check.onchange=()=>session.acknowledge(check.checked);
   acknowledgment.append(check,el('span','I reviewed these public copies. Once published, they remain in shared history; originals stay unchanged locally.'));container.append(acknowledgment);
   const save=el('button',state.phase==='uncertain'?'Retry exact same sharing request':state.phase==='submitting'?'Saving…':'Save reviewed sharing choices');save.type='button';save.setAttribute('data-sharing-focus','save');save.disabled=state.phase!=='uncertain'&&(state.phase!=='ready'||!state.canReview||!state.acknowledged||state.page.items.some(item=>!state.choices[item.source_event_id]));
   save.onclick=run(async()=>{const result=await session.save();if(result)notice('Sharing choices saved. Reload the review to continue.','ok')});container.append(save);
  }
  if(state.page.next_cursor){const next=el('button','Next journal page');next.type='button';next.setAttribute('data-sharing-focus','next');next.disabled=locked||state.phase!=='ready'||state.acknowledged||Object.keys(state.choices).length>0;next.onclick=run(()=>session.load(state.page.next_cursor));container.append(next)}
  if(state.phase==='saved')focusKey='reload';
  const focus=Array.from(container.querySelectorAll('[data-sharing-focus]')).find(node=>node.getAttribute('data-sharing-focus')===focusKey);
  if(focus&&!focus.disabled)focus.focus();
 }
 return{load:()=>session.load(),discard:()=>session.discard(),state:()=>session.state()};
}
const exported={createSession,create,reloadLabel,RECOVERY_KEY};root.WorkspaceSharing=exported;if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
