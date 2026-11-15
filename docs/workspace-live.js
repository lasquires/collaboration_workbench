(function(root){
'use strict';
const RECOVERY='workbench.live.pending.v1', CLAIM_RECOVERY='workbench.live.claim-submit.v1';
const copy=x=>JSON.parse(JSON.stringify(x));
function createSession({api,getContext,onChange=()=>{},onDirty=()=>{},storage=null,requestId=()=>root.crypto.randomUUID()}){
 let projectId=null,page=null,config=null,capabilities=null,phase='idle',error='',choices={},ack=false,pendingBody=null,pendingUncertain=false,claimReview=null,claimCurrent=null,claimConfirmation=null,claimBody=null,claimUncertain=false,sequence=0,requestTicket=0,mutation=0,valueTicket=0,draft={},draftRevision=0,recoveryInvalid=false;
 let safeStorage=storage; try{if(!storage&&root.sessionStorage)safeStorage=root.sessionStorage}catch{}
 function recover(key,claim=false){try{const raw=safeStorage?.getItem(key);if(!raw)return null;const x=JSON.parse(raw),body=x?.body;if(typeof raw!=='string'||raw.length>12000||!x||Object.keys(x).sort().join(',')!=='action,body,project_id'||typeof x.project_id!=='string'||!x.project_id||!['choices','prepare','submit','claim-submit'].includes(x.action)||(claim?x.action!=='claim-submit':x.action==='claim-submit')||!body||typeof body!=='object'||Array.isArray(body)||body.project_id!==x.project_id)throw Error();const allowed={choices:['project_id','review_id','offset','expected_draft_revision','request_id','choices'],prepare:['project_id','review_id','expected_draft_revision','acknowledged'],submit:['project_id','review_id','command_id'],'claim-submit':['project_id','review_id']},fields=allowed[x.action];if(Object.keys(body).sort().join(',')!==fields.slice().sort().join(',')||typeof body.review_id!=='string'||!body.review_id)throw Error();if(x.action==='choices'&&(typeof body.request_id!=='string'||!body.request_id||!Number.isInteger(body.offset)||body.offset<0||!Number.isInteger(body.expected_draft_revision)||body.expected_draft_revision<0||!body.choices||typeof body.choices!=='object'||Array.isArray(body.choices)||!Object.keys(body.choices).length||Object.keys(body.choices).length>20||Object.keys(body.choices).some(k=>!k||k.length>500)||Object.values(body.choices).some(v=>!['','local','remote'].includes(v))))throw Error();if(x.action==='prepare'&&(!Number.isInteger(body.expected_draft_revision)||body.expected_draft_revision<0||body.acknowledged!==true))throw Error();if(x.action==='submit'&&(typeof body.command_id!=='string'||! /^[a-f0-9]{64}$/.test(body.command_id)))throw Error();return raw}catch{recoveryInvalid=true;phase='recovery-error';error='A saved live action is malformed. Keep it for review; no request was sent.';return null}}
 pendingBody=recover(RECOVERY);claimBody=recover(CLAIM_RECOVERY,true);pendingUncertain=Boolean(pendingBody);claimUncertain=Boolean(claimBody);if(!pendingBody&&claimBody)projectId=JSON.parse(claimBody).project_id;
 if(pendingBody&&!recoveryInvalid){projectId=JSON.parse(pendingBody).project_id;phase='uncertain';error='A live action was recovered. Retry only after reviewing its exact action.'}
 if(pendingBody&&claimBody&&JSON.parse(pendingBody).project_id!==JSON.parse(claimBody).project_id){recoveryInvalid=true;phase='recovery-error';error='Saved recovery actions belong to different projects. Preserve both for explicit recovery review; nothing was sent.'}
 const state=()=>({draft:copy(draft),busy:Boolean(mutation),projectId,page:page?copy(page):null,config:config?copy(config):null,capabilities:capabilities?copy(capabilities):null,phase,error,choices:copy(choices),acknowledged:ack,pendingBody,claimReview:claimReview?copy(claimReview):null,claimCurrent:claimCurrent?copy(claimCurrent):null,claimConfirmation:claimConfirmation?copy(claimConfirmation):null,claimBody});
 const post=(path,body)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const emit=(render=true)=>{const c=getContext();if(c.projectId===projectId){if(c.activePanel===undefined||c.activePanel==='live')onDirty(Boolean(pendingBody||claimBody||page&&((page.phase==='reviewing'&&page.selected_count>0)||['prepared','pending'].includes(page.phase))||Object.keys(choices).length||ack||mutation||Object.keys(draft).length));if(render)onChange(state())}};
 const requireCurrent=()=>{if(recoveryInvalid)throw Error('Saved recovery state requires explicit review before sending actions.');const c=getContext(),origin=pendingBody?JSON.parse(pendingBody).project_id:claimBody?JSON.parse(claimBody).project_id:projectId;if(c.projectId!==origin)throw Error('Return to the original project to continue this saved action.');if(c.activePanel&&c.activePanel!=='live')throw Error('Return to Live controls to continue this action.')};
 function resetProject(p){if(projectId===p)return;projectId=p;page=null;config=null;capabilities=null;claimReview=null;claimCurrent=null;claimConfirmation=null;choices={};ack=false;draft={};draftRevision++;phase='idle';error='';++valueTicket;}
 function setDraft(key,value){requireCurrent();if(!['enabled','url','tokenFile','taskId','op','rationale','lease','principal','reservedPaths'].includes(key))throw Error('Unknown form field.');draft[key]=value;draftRevision++;emit(false)}
 function guarded(fn){return async(...args)=>{if(mutation)throw Error('A live action is already in progress.');mutation=1;emit();try{return await fn(...args)}finally{mutation=0;emit()}}}
 function persist(key,body){if(!safeStorage)throw Error('Private browser recovery storage is unavailable.');safeStorage.setItem(key,JSON.stringify(body))}
 async function status(){const c=getContext(),p=c.projectId,panel=c.activePanel,ticket=++requestTicket;if(!p)throw Error('Choose a project first.');const [cfg,caps]=await Promise.all([api('/v1/live-status?project_id='+encodeURIComponent(p)),api('/v1/projects/'+encodeURIComponent(p)+'/capabilities')]);const now=getContext();if(ticket!==requestTicket||now.projectId!==p||now.activePanel!==panel||panel&&panel!=='live')return;if(!pendingBody&&!claimBody)resetProject(p);config=cfg;capabilities=caps.capabilities||{};emit();return state()}
 async function configure(enabled,url,tokenFile){requireCurrent();++requestTicket;if(!capabilities?.host_owner&&getContext().hostOwner!==true)throw Error('A host owner must configure live sync.');if(pendingBody&&(url||tokenFile||enabled===false))throw Error('Resolve the pending action before changing or disabling its host.');const formRevision=draftRevision;const c=getContext(),p=projectId,ticket=++sequence;if(c.activePanel&&c.activePanel!=='live')throw Error('Return to Live controls to configure sync.');const body={project_id:p,enabled:Boolean(enabled)};if(url)body.url=url;if(tokenFile)body.token_file=tokenFile;const result=await post('/v1/live-configure',body);if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;config=result;if(draftRevision===formRevision){for(const key of ['enabled','url','tokenFile'])delete draft[key];draftRevision++}emit();return result}
 async function load(kind='join',offset=0){if(recoveryInvalid||mutation)return state();const c=getContext(),p=c.projectId,panel=c.activePanel;if(p===projectId&&Object.keys(choices).length)throw Error('Save or discard unsaved conflict choices before loading another page.');if(!p)throw Error('Choose a project first.');if(!['join','host','local'].includes(kind)||!Number.isInteger(offset)||offset<0)throw Error('Unknown review kind or invalid offset.');if((pendingBody||claimBody)&&p!==(pendingBody?JSON.parse(pendingBody).project_id:JSON.parse(claimBody).project_id))return state();const mine=++sequence;if(!pendingBody&&!claimBody)resetProject(p);if(!pendingBody)phase='loading';error='';emit();try{await status();if(mine!==sequence)return;const result=await api('/v1/live-ui-state?'+new URLSearchParams({project_id:p,offset:String(offset)}));if(mine!==sequence||getContext().projectId!==p||getContext().activePanel!==panel||panel&&panel!=='live')return;page=result.project_id===p?result:null;if(page?.phase==='idle')page=null;if(!pendingBody){choices={};ack=false;phase=page?.phase||'ready'}emit();return state()}catch(e){if(mine===sequence){if(!pendingBody)phase='error';error=e.message;emit()}throw e}}
 async function preview(kind){requireCurrent();if(pendingBody)throw Error('Resolve the saved action first.');if(!capabilities?.host_owner&&getContext().hostOwner!==true)throw Error('A host owner must authorize live reviews.');if(page&&['reviewing','prepared','pending'].includes(page.phase))throw Error('Discard or finish the current review before replacing it.');const c=getContext(),p=projectId,ticket=++sequence;const result=await post('/v1/live-ui-preview',{project_id:p,kind});if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;page=result;choices={};ack=false;phase=result.phase;emit();return state()}
 function choose(key,mode){requireCurrent();if(mutation)throw Error('A live action is already in progress.');if(pendingBody)throw Error('Resolve the saved action before editing choices.');if(!page||page.phase!=='reviewing')throw Error('Load a review first.');const item=page.items.find(x=>x.key===key);if(!item)throw Error('Unknown conflict row.');if(!['','local','remote'].includes(mode))throw Error('Choose local or remote explicitly.');if(item.remote_allowed===false&&mode==='remote')throw Error('This immutable history value cannot be replaced; keep the local value.');choices[key]=mode;emit()}
 async function postAction(action,path,body){requireCurrent();if(pendingBody)throw Error('Resolve the existing saved action before creating another.');const frozen={project_id:projectId,action,body};persist(RECOVERY,frozen);pendingBody=JSON.stringify(frozen);pendingUncertain=false;phase='submitting';emit();return retry(true)}
 async function saveChoices(){requireCurrent();if(!page||page.phase!=='reviewing')throw Error('Load a review first.');const pageChoices={};for(const item of page.items)pageChoices[item.key]=Object.hasOwn(choices,item.key)?choices[item.key]:(item.choice||'');const body={project_id:projectId,review_id:page.review_id,offset:page.offset,expected_draft_revision:page.draft_revision,request_id:requestId(),choices:pageChoices};return postAction('choices','/v1/live-ui-choices',body)}
 async function prepare(){requireCurrent();if(!page||page.phase!=='reviewing')throw Error('Load a review first.');if(Object.keys(choices).length||page.items.some(x=>!x.choice))throw Error('Save explicit page choices and clear unsaved edits before preparing.');if(!ack)throw Error('Explicit acknowledgment is required.');return postAction('prepare','/v1/live-ui-prepare',{project_id:projectId,review_id:page.review_id,expected_draft_revision:page.draft_revision,acknowledged:true})}
 async function submit(){requireCurrent();if(!page||!['prepared','pending'].includes(page.phase)||!page.command_id)throw Error('Prepare a reviewed command first.');return postAction('submit','/v1/live-ui-submit',{project_id:projectId,review_id:page.review_id,command_id:page.command_id})}
 function validPage(result,saved){return Boolean(result&&result.project_id===saved.project_id&&result.review_id===saved.body.review_id&&Number.isInteger(result.draft_revision)&&result.draft_revision>=0)}
 function clearPending(){pendingBody=null;pendingUncertain=false;safeStorage?.removeItem(RECOVERY)}
 async function retry(firstAttempt=false){if(!pendingBody)throw Error('There is no pending action to retry.');requireCurrent();const saved=JSON.parse(pendingBody),paths={choices:'/v1/live-ui-choices',prepare:'/v1/live-ui-prepare',submit:'/v1/live-ui-submit'};const c=getContext(),p=saved.project_id,ticket=++sequence;phase='submitting';emit();try{const result=await post(paths[saved.action],saved.body);if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;if(!validPage(result,saved)){pendingUncertain=true;phase='uncertain';error='The reply did not confirm the saved action. Retry the exact request.';emit();return state()}
   if(saved.action==='choices'&&result.choice_ack_request_id!==saved.body.request_id){pendingUncertain=true;phase='uncertain';error='The choices receipt did not match this exact request.';emit();return state()}
   if(saved.action==='prepare'&&(!['prepared','pending','acknowledged','rejected'].includes(result.phase)||typeof result.command_id!=='string'||! /^[a-f0-9]{64}$/.test(result.command_id))){pendingUncertain=true;phase='uncertain';error='The preparation reply did not contain the expected command handle.';emit();return state()}
   if(saved.action==='submit'){
    if(result.command_id!==saved.body.command_id){pendingUncertain=true;phase='uncertain';error='The response command handle did not match the saved action.';emit();return state()}
    const rejected=result.phase==='rejected';const receipt=result.result?.reviewed_command;
    const acknowledged=result.phase==='acknowledged'&&receipt?.id===saved.body.command_id&&receipt?.kind===result.kind&&receipt?.state==='acknowledged';
    if(!rejected&&!acknowledged){pendingUncertain=true;page=result;phase='uncertain';error='The server has not confirmed this reviewed command. Retry the same saved handle.';emit();return state()}
   }
   page=result;phase=result.phase;clearPending();choices={};ack=false;emit();return state()
  }catch(e){const definite=Number.isInteger(e.status)&&e.status>=400&&e.status<500;if(saved.action!=='submit'&&firstAttempt&&definite&&!pendingUncertain){clearPending();phase='rejected';error='The server rejected this action before accepting it. Reload the review before trying again.'}else{pendingUncertain=true;phase='uncertain';error='The action outcome is uncertain. Retry sends the exact saved request.'}emit();throw e}}
 function claimReviewId(){return claimReview?.review?.review_id||claimReview?.review_id||null}
 function normalizeClaimReview(raw){if(raw?.scope==='host'&&Object.hasOwn(raw,'review'))return raw;return{scope:'host',state:'prepared',review:raw,result:null,acknowledgment_is_current_lease:false}}
 async function claimPreview(operation){requireCurrent();if(claimBody)throw Error('Resolve the saved host claim submission first.');const c=getContext(),p=projectId,ticket=++sequence;const r=await post('/v1/live-claims-preview',{project_id:p,operation});if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;claimReview=normalizeClaimReview(r);emit();return state()}
 async function claimRead(task_id,cursor){requireCurrent();claimCurrent=null;emit();const c=getContext(),p=projectId,ticket=++sequence;const r=await post('/v1/live-claims-read',{project_id:p,task_id,...(cursor?{cursor}:{})});if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return null;claimCurrent={...r,read_at:new Date().toISOString(),task_id};emit();return r}
 async function claimStatus(){requireCurrent();const c=getContext(),p=projectId,ticket=++sequence;const r=await post('/v1/live-claims-review',{project_id:p});if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;claimReview=normalizeClaimReview(r);emit();return state()}
 function typedClaimReceipt(result,saved){const receipt=result?.command_receipt,id=saved.body.review_id;return Boolean(result?.scope==='host'&&result.acknowledgment_is_current_lease===false&&result.review_id===id&&receipt?.review_id===id&&receipt.request_id==='host-claim-'+id&&receipt.phase===result.state&&typeof receipt.remote_actor==='string'&&typeof receipt.remote_store==='string'&&typeof receipt.url==='string'&&typeof receipt.recorded_at==='string'&&typeof receipt.revision==='string'&&receipt.revision.split(':')[0]===receipt.remote_store)}
 function validClaimReceipt(result,saved){const reviewId=saved.body.review_id,review=claimReview?.review;return result?.scope==='host'&&result?.state==='acknowledged'&&result.acknowledgment_is_current_lease===false&&result.result?.request_id==='host-claim-'+reviewId&&(typedClaimReceipt(result,saved)||review?.review_id===reviewId&&review?.scope==='host'&&review.request?.request_id==='host-claim-'+reviewId)}
 async function claimSubmit(){requireCurrent();const review=claimReview?.review,reviewId=claimReviewId();if(!reviewId||claimReview?.scope!=='host'||review?.scope!=='host'||claimReview.state!=='prepared'||review.request?.request_id!=='host-claim-'+reviewId)throw Error('Load a valid prepared host claim review first.');const frozen={project_id:projectId,action:'claim-submit',body:{project_id:projectId,review_id:reviewId}};persist(CLAIM_RECOVERY,frozen);claimBody=JSON.stringify(frozen);claimUncertain=false;emit();return claimSend(true)}
 async function claimSend(firstAttempt=false){
  if(!claimBody)throw Error('There is no saved claim submission to retry.');requireCurrent();if(!claimReview?.review)await claimStatus();
  const saved=JSON.parse(claimBody),p=saved.project_id,ticket=++sequence;
  try{
   const result=await post('/v1/live-claims-submit',saved.body);
   if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return;
   const rejected=result?.state==='rejected'&&result.not_applied===true&&typedClaimReceipt(result,saved);
   if(!rejected&&!validClaimReceipt(result,saved)){claimUncertain=true;error='The lease reply did not confirm the exact host request. Retry the saved handle.';emit();return result}
   if(claimReview?.review?.review_id===saved.body.review_id)claimReview={...claimReview,state:result.state,result:result.result,acknowledgment_is_current_lease:false};
   if(result.command_receipt)claimConfirmation=copy(result);
   claimBody=null;claimUncertain=false;error='';safeStorage?.removeItem(CLAIM_RECOVERY);emit();return result;
  }catch(e){claimUncertain=true;emit();throw e}
 }
 async function claimRetry(){if(!claimReview?.review)await claimStatus();return claimSend(false)}
 async function readValue(key,side,offset=0){
  requireCurrent();
  if(!page?.review_id||!['local','remote','base','field','entity'].includes(side)||!Number.isInteger(offset)||offset<0)throw Error('Select a reviewed value and a valid slice offset.');
  const row=page.items.find(item=>item.key===key);if(!row)throw Error('Select a conflict on the current page.');
  const p=projectId,rid=page.review_id,panel=getContext().activePanel,ticket=++valueTicket,expected=row[side];
  const value=await api('/v1/live-ui-value?'+new URLSearchParams({project_id:p,review_id:rid,key,side,offset:String(offset)}));
  if(getContext().projectId!==p||getContext().activePanel!==panel||panel&&panel!=='live'||ticket!==valueTicket||page?.review_id!==rid)return null;
  const count=typeof value?.text==='string'?Array.from(value.text).length:-1;
  if(value?.project_id!==p||value.review_id!==rid||value.key!==key||value.side!==side||value.offset!==offset||count<0||count>8192||!Number.isInteger(value.length)||value.length<offset+count||! /^[a-f0-9]{64}$/.test(value.digest)||
     (value.next_offset!==null&&(!Number.isInteger(value.next_offset)||value.next_offset!==offset+count||value.next_offset>=value.length||count!==8192))||
     (value.next_offset===null&&offset+count!==value.length)||
     (expected&&(value.digest!==expected.digest||value.length!==expected.length)))throw Error('Value response did not match the selected review slice.');
  return value;
 }
 async function discard(){if(pendingBody||claimBody||phase==='recovery-error'||page&&page.phase==='pending')return false;if(!page){++sequence;choices={};ack=false;draft={};draftRevision++;claimReview=null;claimCurrent=null;claimConfirmation=null;phase='idle';error='';emit();return true}const c=getContext(),p=projectId,rid=page.review_id,ticket=++sequence;try{const result=await post('/v1/live-ui-discard',{project_id:p,review_id:rid});if(getContext().projectId!==p||getContext().activePanel&&getContext().activePanel!=='live'||ticket!==sequence)return false;if(result?.project_id!==p||result.phase!=='idle')return false;page=null;choices={};ack=false;draft={};draftRevision++;claimReview=null;claimCurrent=null;claimConfirmation=null;phase='idle';error='';emit();return true}catch{return false}}
 function acknowledge(value){if(pendingBody||mutation)return;ack=value===true;emit()}
 return{state,load,status,setDraft,configure:guarded(configure),preview:guarded(preview),choose,saveChoices:guarded(saveChoices),prepare:guarded(prepare),submit:guarded(submit),retry:guarded(retry),readValue,discard:guarded(discard),acknowledge,claimPreview:guarded(claimPreview),claimRead:guarded(claimRead),claimStatus:guarded(claimStatus),claimSubmit:guarded(claimSubmit),claimRetry:guarded(claimRetry)};
}
function create({api,notice=()=>{},getContext,onDirty=()=>{}}){
 const container=document.querySelector('#liveWorkspace');if(!container)throw Error('Missing #liveWorkspace.');
 let storage=null;try{storage=root.sessionStorage}catch{}
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);return n};
 let session;
 const button=(label,fn,disabled=false)=>{const b=el('button',label);b.type='button';b.disabled=disabled;b.onclick=()=>Promise.resolve(fn()).catch(e=>notice(e.message,'error'));return b};
 const field=(label,input)=>{const box=el('label');box.append(el('span',label),input);return box};
 function input(s,key,label,type='text'){
  const n=el(type==='textarea'?'textarea':'input');if(type!=='textarea')n.type=type;
  n.value=s.draft[key]??'';n.dataset.focus='live-'+key;n.oninput=()=>session.setDraft(key,n.value);return n;
 }
 async function valueSlice(key,side,target,offset=0){const slice=await session.readValue(key,side,offset);if(!slice||!target.isConnected)return;target.replaceChildren(el('pre',slice.text));if(Number.isInteger(slice.next_offset))target.append(button('Read next '+side+' slice',()=>valueSlice(key,side,target,slice.next_offset)))}
 function render(){
  const focused=document.activeElement?.dataset?.focus;
  const s=session.state(),disabled=s.busy||Boolean(s.pendingBody||s.claimBody),owner=Boolean(s.capabilities?.host_owner||getContext().hostOwner);
  container.replaceChildren();container.append(el('h2','Live collaboration'));
  container.append(el('p','Connect this project to a shared host, review changes, and coordinate task leases. Git synchronization remains available when the host is offline.'));
  if(s.error){const error=el('p',s.error);error.setAttribute('role','alert');container.append(error)}
  const connection=el('section');connection.className='card';connection.append(el('h3','Connection'));
  const cfg=s.config||{};connection.append(el('p','Transport: '+(cfg.preferred_transport||'local')+' · State: '+(cfg.state||'not configured')+(cfg.pending?' · queued work remains':'')));
  if(cfg.error)connection.append(el('p','Connection report: '+cfg.error));
  connection.append(button('Refresh connection status',()=>session.status(),s.busy));
  const enabled=el('input');enabled.type='checkbox';enabled.checked=s.draft.enabled??Boolean(cfg.enabled);enabled.disabled=!owner||disabled;enabled.dataset.focus='live-enabled';enabled.onchange=()=>session.setDraft('enabled',enabled.checked);
  connection.append(field('Enable live synchronization',enabled));
  const host=input(s,'url','Host URL','url');if(!Object.hasOwn(s.draft,'url'))host.value=cfg.url||'';host.disabled=!owner||disabled;
  const credential=input(s,'tokenFile','Private credential file');credential.disabled=!owner||disabled;
  const connectionFields=el('div');connectionFields.className='live-form-grid';connectionFields.append(field('Host URL',host),field('Private credential file path on this computer',credential));connection.append(connectionFields);
  connection.append(el('p','Leave the credential path blank to reuse the saved credential. Credential contents stay on this computer.'));
  connection.append(button('Save connection',()=>session.configure(enabled.checked,host.value,credential.value),!owner||disabled));
  if(owner&&(s.pendingBody||s.claimBody)&&cfg.enabled===false)connection.append(button('Resume existing host connection',()=>session.configure(true),s.busy));
  if(!owner)connection.append(el('p','A host owner must configure the connection and authorize live reviews.'));
  container.append(connection);
  const review=el('section');review.className='card';review.append(el('h3','Review project changes'));
  const kind=el('select');kind.setAttribute('aria-label','Review direction');kind.dataset.focus='live-kind';
  for(const [value,label]of [['join','Join a host'],['host','Resolve queued changes at the host'],['local','Resolve incoming changes in this app']]){const option=el('option',label);option.value=value;kind.append(option)}
  review.append(kind,button('Start review',()=>session.preview(kind.value),!owner||disabled));
  if(s.page){
   const page=s.page;review.append(el('h4',page.kind+' · '+page.phase));
   if(page.historical_only)review.append(el('p','Historical command confirmation recorded '+page.recorded_at+'. Current host access, synchronization and leases were not checked.'));
   review.append(el('p','Saved choices: '+page.selected_count+' of '+page.total_conflicts+' conflicts.'));
   if(page.host_url)review.append(el('p','Reviewed host: '+page.host_url));
   if(page.summary){const details=el('details');details.append(el('summary','Change summary'),el('pre',JSON.stringify(page.summary,null,2)));review.append(details)}
   if(page.error)review.append(el('p',page.error));
   const fieldContainer=['acknowledged','rejected'].includes(page.phase)?el('details'):el('div');if(fieldContainer.tagName==='DETAILS')fieldContainer.append(el('summary','Reviewed conflict choices'));review.append(fieldContainer);
   for(const item of page.items||[]){
    const article=el('article');article.className='card';article.append(el('h4',item.table+' · '+item.field+' · '+item.entity_key));
    if(item.label_truncated?.field||item.label_truncated?.entity_key){article.append(el('p','Long row labels are abbreviated.'));for(const [side,key,label]of [['field','field','field label'],['entity','entity_key','entity label']])if(item.label_truncated[key]){const target=el('div');article.append(button('Read full '+label,()=>valueSlice(item.key,side,target),s.busy),target)}}
    for(const side of ['local','remote','base']){
     const value=item[side]||{},label=side==='base'?'Common base':side==='local'?(page.kind==='host'?'HOST current value':'THIS APP current value'):(page.kind==='host'?'THIS APP queued value':'HOST value');
     article.append(el('strong',label),el('pre',value.present?value.preview:'(absent)'));
     if(value.truncated){const target=el('div');article.append(el('p','Preview truncated · '+value.length+' characters'),button('Read full '+label,()=>valueSlice(item.key,side,target),s.busy),target)}
    }
    const select=el('select');select.dataset.focus='choice-'+item.key;select.setAttribute('aria-label','Resolution for '+item.table+' '+item.field);
    for(const [value,label]of [['','Choose explicitly'],['local',page.kind==='host'?'Keep HOST current value':'Keep THIS APP current value'],['remote',page.kind==='host'?'Use THIS APP queued value':'Use HOST value']]){const option=el('option',label);option.value=value;option.disabled=value==='remote'&&item.remote_allowed===false;select.append(option)}
    select.value=Object.hasOwn(s.choices,item.key)?s.choices[item.key]:item.choice||'';select.disabled=disabled||page.phase!=='reviewing';select.onchange=()=>session.choose(item.key,select.value);article.append(select);
    if(item.remote_allowed===false)article.append(el('p','Immutable history must keep the local value.'));fieldContainer.append(article);
   }
   if(page.phase==='reviewing'){
    review.append(button('Save page choices',()=>session.saveChoices(),disabled));
    const unsaved=Object.keys(s.choices).length>0;
    if(page.offset>0)review.append(button('Previous conflicts',()=>session.load(page.kind,Math.max(0,page.offset-20)),disabled||unsaved));
    if(page.next_offset!==null)review.append(button('Next conflicts',()=>session.load(page.kind,page.next_offset),disabled||unsaved));
    const check=el('input');check.type='checkbox';check.checked=s.acknowledged;check.disabled=disabled;check.dataset.focus='live-ack';check.onchange=()=>session.acknowledge(check.checked);
    review.append(field('I reviewed the saved choices. This exchanges project records with the configured host in both directions.',check),button('Prepare reviewed command',()=>session.prepare(),disabled));
   }
   if(['prepared','pending'].includes(page.phase)&&!s.pendingBody)review.append(button(page.phase==='pending'?'Confirm existing command outcome':'Submit reviewed command',()=>session.submit(),disabled));
   if(page.phase==='acknowledged')review.append(el('p','This command was acknowledged. Check connection status separately for remaining queued work.'));
  }
  if(s.pendingBody)review.append(button('Retry exact saved action',()=>session.retry(),s.busy));
  review.append(button('Discard review and local form drafts',()=>session.discard(),disabled||s.page?.phase==='pending'));container.append(review);
  const claims=el('section');claims.className='card';claims.append(el('h3','Host task leases'),el('p','Lease actions are explicit. A saved acknowledgment is historical; read the host to check current ownership and expiry. Local task claims are separate.'));
  const task=input(s,'taskId','Host task ID'),rationale=input(s,'rationale','Rationale'),lease=input(s,'lease','Lease seconds','number'),principal=input(s,'principal','Handoff principal'),paths=input(s,'reservedPaths','Reserved paths','textarea');lease.min='1';lease.max='86400';
  const op=el('select');op.dataset.focus='live-op';for(const name of ['claim_task','heartbeat_claim','release_claim','handoff_claim']){const option=el('option',name.replaceAll('_',' '));option.value=name;op.append(option)}op.value=s.draft.op||'claim_task';op.onchange=()=>session.setDraft('op',op.value);
  const leaseFields=el('div');leaseFields.className='live-form-grid';leaseFields.append(field('Host task ID',task),field('Lease action',op),field('Rationale',rationale),field('Lease duration in seconds',lease),field('Handoff principal ID (handoff only)',principal),field('Reserved relative paths, one per line (new claim only)',paths));claims.append(leaseFields);
  claims.append(button('Preview lease action',()=>{const operation={op:op.value,task_id:task.value,rationale:rationale.value};if(['claim_task','heartbeat_claim','handoff_claim'].includes(op.value)&&lease.value)operation.lease_seconds=Number(lease.value);if(op.value==='handoff_claim')operation.to_principal_id=principal.value;if(op.value==='claim_task'&&paths.value.trim())operation.reserved_paths=paths.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);return session.claimPreview(operation)},disabled||!owner),button('Read current host leases',()=>session.claimRead(task.value),disabled),button('Read saved lease review',()=>session.claimStatus(),disabled));
  if(s.claimReview){const saved=s.claimReview,operation=saved.review?.request?.operations?.[0]||{};claims.append(el('h4','Saved lease review · '+saved.state));const details=el('dl');for(const [label,value]of [['Action',operation.op?.replaceAll('_',' ')],['Task',operation.task_id],['Rationale',operation.rationale],['Duration in seconds',operation.lease_seconds],['Reserved paths',operation.reserved_paths?.join(', ')],['Handoff principal',operation.to_principal_id]])if(value!==undefined)details.append(el('dt',label),el('dd',value));claims.append(details);if(saved.state==='prepared'&&!s.claimBody)claims.append(button('Submit this saved lease action',()=>session.claimSubmit(),disabled))}
  if(s.claimConfirmation){const receipt=s.claimConfirmation.command_receipt;claims.append(el('p','Historical lease command '+receipt.review_id+' · '+receipt.phase+' · recorded '+receipt.recorded_at+' · host '+receipt.url+'. This confirms that command only; current access and lease ownership were not checked.'))}
  if(s.claimBody)claims.append(button('Retry exact saved lease submission',()=>session.claimRetry(),s.busy));
  if(s.claimCurrent){const current=s.claimCurrent;claims.append(el('h4','Host lease read'),el('p','Read at '+(current.read_at||'unknown time')+'. This display does not automatically renew or refresh leases.'));for(const row of current.items||[]){const expired=row.expires_at&&Date.parse(row.expires_at)<=Date.now();claims.append(el('p',(row.task_id||'Task')+' · '+(row.principal_id||'Owner unknown')+' · '+(row.released_at?'released':expired?'expired':row.expires_at?'expires '+row.expires_at:'expiry unknown')))}if(!(current.items||[]).length)claims.append(el('p','No host lease rows returned.'));if(current.next_cursor)claims.append(button('Next host lease page',()=>session.claimRead(current.task_id||task.value,current.next_cursor),disabled));}
  container.append(claims);
  if(focused){const next=Array.from(container.querySelectorAll('[data-focus]')).find(n=>n.dataset.focus===focused);if(next)next.focus()}
 }
 session=createSession({api,getContext,onDirty,onChange:render,storage});render();return{load:session.load,discard:session.discard,state:session.state};
}
const exported={createSession,create,RECOVERY,CLAIM_RECOVERY};root.WorkspaceLive=exported;if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
