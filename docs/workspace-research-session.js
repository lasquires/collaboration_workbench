'use strict';
(function(root){
  const RECOVERY = 'workbench.research.pending.v1';
  const MAX_BYTES = 2 * 1024 * 1024;
  const RESOURCES = new Set([
    'findings', 'questions', 'artifact-versions', 'finding-revisions',
    'evidence-links', 'evidence-changes', 'research-review-notices', 'answers',
    'answer-reviews', 'question-resolutions', 'answer-support', 'source-derivations',
    'observations', 'negative-searches', 'index-runs'
  ]);
  const SCHEMAS = {
    create_finding: ['id','claim','scope','classification','uncertainty','evidence'],
    revise_finding: ['finding_id','expected_record_revision','fields','rationale'],
    archive_finding: ['finding_id','expected_record_revision','rationale'],
    restore_finding: ['finding_id','expected_record_revision','rationale'],
    set_evidence_state: ['evidence_link_id','expected_state','state','reason','replacement_id'],
    create_answer: ['id','question_id','expected_question_revision','claim','evidence_link_ids','evidence'],
    review_answer: ['answer_id','expected_question_revision','outcome','rationale','supersedes'],
    resolve_question: ['question_id','expected_record_revision','answer_id','rationale'],
    set_question_state: ['question_id','expected_record_revision','state','rationale'],
    record_negative_search: ['id','artifact_version_id','query','method','searched_range','coverage','outcome']
  };
  const PAGE_FILTERS = {
    findings: [], questions: [], 'artifact-versions': [],
    'finding-revisions': ['subject_id'], 'evidence-links': ['subject_type','subject_id'],
    'evidence-changes': ['subject_id'],
    'research-review-notices': ['subject_type','subject_id'], answers: ['subject_id'],
    'answer-reviews': ['subject_id'], 'question-resolutions': ['subject_id'],
    'answer-support': ['subject_id'], 'source-derivations': ['subject_id'],
    observations: ['subject_id'], 'negative-searches': ['subject_id'], 'index-runs': []
  };
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const utf8Bytes = value => new TextEncoder().encode(value).length;
  const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  const nonempty = value => typeof value === 'string' && value.length > 0;
  const validId = value => nonempty(value) && value.length <= 200;
  const positiveInt = value => Number.isSafeInteger(value) && value > 0;
  const uniqueStrings = (items, limit=100) => Array.isArray(items) && items.length <= limit &&
    items.every(validId) && new Set(items).size === items.length;

  function validateSupport(items) {
    if (!Array.isArray(items) || items.length > 100) return false;
    return items.every(item => {
      if (!isObject(item) || Object.keys(item).sort().join(',') !==
          'artifact_version_id,limitations,locator,method') return false;
      const locatorOk = (typeof item.locator === 'string' && item.locator.length > 0 && item.locator.length <= 10000) ||
        (isObject(item.locator) && utf8Bytes(JSON.stringify(item.locator)) <= 10000);
      return validId(item.artifact_version_id) && locatorOk &&
        typeof item.method === 'string' && item.method.length > 0 && item.method.length <= 4000 &&
        typeof item.limitations === 'string' && item.limitations.length > 0 && item.limitations.length <= 4000;
    });
  }

  function validateOperation(operation) {
    if (!isObject(operation) || !SCHEMAS[operation.op]) throw Error('Unsupported research operation.');
    if (Object.keys(operation).some(key => key !== 'op' && !SCHEMAS[operation.op].includes(key))) {
      throw Error('Operation contains unsupported fields.');
    }
    const required = {
      create_finding: ['id','claim','scope','classification','uncertainty','evidence'],
      revise_finding: ['finding_id','expected_record_revision','fields','rationale'],
      archive_finding: ['finding_id','expected_record_revision','rationale'],
      restore_finding: ['finding_id','expected_record_revision','rationale'],
      set_evidence_state: ['evidence_link_id','expected_state','state','reason'],
      create_answer: ['id','question_id','expected_question_revision','claim','evidence_link_ids'],
      review_answer: ['answer_id','expected_question_revision','outcome','rationale'],
      resolve_question: ['question_id','expected_record_revision','answer_id','rationale'],
      set_question_state: ['question_id','expected_record_revision','state','rationale'],
      record_negative_search: ['id','artifact_version_id','query','method','searched_range','coverage','outcome']
    }[operation.op];
    if (required.some(key => !(key in operation))) throw Error('Operation is missing required guards or fields.');
    const text = (value, max=20000) => typeof value === 'string' && value.length > 0 && value.length <= max;
    if (operation.op === 'create_finding' && (!validId(operation.id) || !text(operation.claim) ||
        !isObject(operation.scope) || !text(operation.classification,120) ||
        typeof operation.uncertainty !== 'string' || !validateSupport(operation.evidence))) {
      throw Error('Malformed finding creation.');
    }
    if (operation.op === 'revise_finding') {
      const fields = operation.fields;
      if (!validId(operation.finding_id) || !positiveInt(operation.expected_record_revision) ||
          !isObject(fields) || !Object.keys(fields).length ||
          Object.keys(fields).some(key => !['claim','scope','classification','uncertainty','evidence'].includes(key)) ||
          ('claim' in fields && !text(fields.claim)) || ('scope' in fields && !isObject(fields.scope)) ||
          ('classification' in fields && !text(fields.classification,120)) ||
          ('uncertainty' in fields && typeof fields.uncertainty !== 'string') ||
          ('evidence' in fields && !validateSupport(fields.evidence)) || !text(operation.rationale,4000)) {
        throw Error('Malformed finding revision.');
      }
    }
    if (['archive_finding','restore_finding'].includes(operation.op) &&
        (!validId(operation.finding_id) || !positiveInt(operation.expected_record_revision) || !text(operation.rationale,4000))) {
      throw Error('Malformed finding state change.');
    }
    if (operation.op === 'set_evidence_state' &&
        (!validId(operation.evidence_link_id) || operation.expected_state !== 'available' ||
         !['retracted','superseded'].includes(operation.state) || !text(operation.reason,4000) ||
         (operation.state === 'retracted' && 'replacement_id' in operation) ||
         (operation.state === 'superseded' && (!validId(operation.replacement_id) || operation.replacement_id === operation.evidence_link_id)))) {
      throw Error('Invalid evidence state transition.');
    }
    if (operation.op === 'create_answer') {
      const evidence = operation.evidence ?? [];
      if (!validId(operation.id) || !validId(operation.question_id) || !positiveInt(operation.expected_question_revision) ||
          !text(operation.claim) || !uniqueStrings(operation.evidence_link_ids) || !validateSupport(evidence) ||
          operation.evidence_link_ids.length + evidence.length > 100 ||
          (!operation.evidence_link_ids.length && !evidence.length)) throw Error('Malformed answer proposal or pinned source identities.');
    }
    if (operation.op === 'review_answer' &&
        (!validId(operation.answer_id) || !positiveInt(operation.expected_question_revision) ||
         !['accepted','rejected'].includes(operation.outcome) || !text(operation.rationale,4000) ||
         !uniqueStrings(operation.supersedes ?? []))) throw Error('Invalid independent review operation.');
    if (operation.op === 'resolve_question' &&
        (!validId(operation.question_id) || !positiveInt(operation.expected_record_revision) ||
         !validId(operation.answer_id) || !text(operation.rationale,4000))) throw Error('Malformed question resolution.');
    if (operation.op === 'set_question_state' &&
        (!validId(operation.question_id) || !positiveInt(operation.expected_record_revision) ||
         !['open','investigating','withdrawn'].includes(operation.state) || !text(operation.rationale,4000))) {
      throw Error('Invalid question state.');
    }
    if (operation.op === 'record_negative_search' &&
        (!validId(operation.id) || !validId(operation.artifact_version_id) || !text(operation.query) ||
         !text(operation.method) || !text(operation.searched_range) || !text(operation.outcome) ||
         !isObject(operation.coverage) || typeof operation.coverage.complete!=='boolean')) {
      throw Error('Malformed negative search record.');
    }
    return clone(operation);
  }

  function targetIds(operation) {
    const keys = {
      create_finding: ['id'], revise_finding: ['finding_id'], archive_finding: ['finding_id'],
      restore_finding: ['finding_id'], set_evidence_state: ['evidence_link_id'],
      create_answer: ['id','question_id'], review_answer: ['answer_id'],
      resolve_question: ['question_id'], set_question_state: ['question_id'],
      record_negative_search: ['id','artifact_version_id']
    }[operation.op] || [];
    return Object.fromEntries(keys.filter(key => operation[key] !== undefined).map(key => [key,operation[key]]));
  }

  function recoverySelection(operation) {
    if (operation.finding_id) return {type:'finding',id:operation.finding_id,context:null};
    if (operation.op==='review_answer') return {type:'answer',id:operation.answer_id,context:null};
    if (operation.op==='set_evidence_state') return {type:'evidence',id:operation.evidence_link_id,context:null};
    if (operation.question_id) return {type:'question',id:operation.question_id,context:null};
    if (operation.op==='record_negative_search') return {type:'source',id:operation.artifact_version_id,context:null};
    return null;
  }
  function validateContext(result, project, type, id) {
    if (!result || result.project_id !== project || typeof result.revision !== 'string' || !result.revision ||
        !result.context || result.context.entity_type !== type || !isObject(result.context.entity) ||
        result.context.entity.id !== id || result.context.entity.project_id !== project) {
      throw Error('Entity context did not match the requested project and record.');
    }
    if (type === 'answer' && (!isObject(result.context.question) ||
        result.context.question.project_id !== project ||
        result.context.question.id !== result.context.entity.question_id)) {
      throw Error('Answer context question did not match the answer project and identity.');
    }
    if (type === 'source') {
      const entity=result.context.entity;
      if (typeof entity.sha256!=='string' || !/^[0-9a-f]{64}$/i.test(entity.sha256) ||
          (entity.binary_build_id!==null && entity.binary_build_id!==undefined && !validId(entity.binary_build_id)) ||
          !['available','missing','unavailable'].includes(entity.availability) || typeof entity.local_bytes_available!=='boolean' ||
          !validId(entity.artifact_id) || typeof entity.relative_path!=='string' || typeof entity.kind!=='string' || !isObject(result.context.related)) {
        throw Error('Source context did not include the immutable artifact version identity.');
      }
      const required=['source-derivations','observations','negative-searches'];
      for (const resource of required) {
        const descriptor=Object.values(result.context.related).find(value=>value?.resource===resource);
        if (!isObject(descriptor) || !isObject(descriptor.params) ||
            descriptor.params.project_id!==project || descriptor.params.subject_id!==id) throw Error(`Source related descriptor for '${resource}' did not match the selected version.`);
      }
    }
    return result;
  }

  function createSession({api,getContext,onChange=()=>{},onDirty=()=>{},storage=null,requestId=()=>root.crypto.randomUUID()}) {
    if (typeof api !== 'function' || typeof getContext !== 'function') throw Error('Research API and context are required.');
    let projectId=null, principalId=null, revision=null, capabilities=null, selection=null, pages={};
    let draft=null, phase='idle', error='', busy=false, pendingBody=null, sequence=0, mutation=false;
    let recoveryBroken=false, recoveryChecked=false, recoveryRaw=null;
    let readRevision=null, freshContextRevision=null, freshContextKey=null;
    const contextNow=()=>{const value=getContext()||{};return {projectId:value.projectId??null,principalId:value.principalId??null,activePanel:value.activePanel};};
    const state=()=>clone({projectId,principalId,revision,capabilities,selection,pages,draft,phase,error,busy,pendingBody,recoveryRaw});
    function emit() {
      const context=contextNow();
      const matches=context.projectId===projectId && context.principalId===principalId &&
        (context.activePanel===undefined || context.activePanel==='research');
      if (matches) onDirty(Boolean(draft)||pendingBody!==null||phase==='submitting'||phase==='uncertain');
      onChange(state());
    }
    const call=(path,options)=>api(path,options);
    const busyGuard=()=>{if (busy || mutation) throw Error('Another research action is already in flight.');};
    function scoped(context=contextNow()) {
      if (!context.projectId || !context.principalId) throw Error('Choose a project and principal first.');
      if (context.activePanel!==undefined && context.activePanel!=='research') throw Error('Return to the research panel before continuing.');
      if ((draft || pendingBody) && projectId && context.projectId!==projectId) throw Error('Resolve the draft or pending request in its original project first.');
      if ((draft || pendingBody) && principalId && context.principalId!==principalId) throw Error('Return with the original principal access before continuing.');
      return context;
    }
    function contextKey() { return `${projectId}|${principalId}|${selection?.type||'project'}|${selection?.id||''}`; }
    function adoptCleanContext(context) {
      if (draft || pendingBody) return;
      if (projectId===context.projectId && principalId===context.principalId) return;
      projectId=context.projectId; principalId=context.principalId; revision=null; capabilities=null;
      selection=null; pages={}; readRevision=null; freshContextRevision=null; freshContextKey=null;
      phase='idle'; error='';
    }
    function readRecoveryOnce() {
      if (recoveryChecked) return;
      recoveryChecked=true;
      if (!storage) return;
      let raw;
      try { raw=storage.getItem(RECOVERY); }
      catch (failure) { recoveryBroken=true; phase='recovery-error'; error=`Could not inspect pending research recovery: ${failure.message}`; return; }
      if (raw===null) return;
      recoveryRaw=raw;
      try {
        pendingBody=validatePending(raw);
        const body=JSON.parse(raw), operation=body.operations[0]; projectId=body.project_id; principalId=body.actor.principal_id;
        revision=body.expected_revision; selection=recoverySelection(operation);
        draft={kind:operation.op,fields:clone(operation),baseRevision:body.expected_revision,
          recordRevision:operation.expected_record_revision??operation.expected_question_revision??null,
          entity:selection?clone(selection):null,targetIds:targetIds(operation),version:0,needsReconcile:false};
        phase='uncertain';
        error='A pending research transaction was recovered. Reload only reads; retry confirms the original outcome.';
      } catch (failure) {
        recoveryBroken=true; phase='recovery-error'; error=failure.message;
      }
    }
    function validatePending(raw) {
      if (typeof raw!=='string' || utf8Bytes(raw)>MAX_BYTES) throw Error('Pending research body is malformed or exceeds 2 MiB; it was retained without sending.');
      let body;
      try { body=JSON.parse(raw); } catch { throw Error('Pending research body is malformed; it was retained without sending.'); }
      if (!isObject(body) || Object.keys(body).sort().join(',')!=='actor,expected_revision,operations,project_id,request_id' ||
          !validId(body.project_id) || !validId(body.request_id) || !validId(body.expected_revision) ||
          !isObject(body.actor) || Object.keys(body.actor).join(',')!=='principal_id' || !validId(body.actor.principal_id) ||
          !Array.isArray(body.operations) || body.operations.length!==1) {
        throw Error('Pending research body is invalid; it was retained without sending.');
      }
      validateOperation(body.operations[0]);
      return raw;
    }
    function checkPendingContext(context) {
      if (!pendingBody) return;
      const body=JSON.parse(pendingBody);
      if (body.project_id!==context.projectId || body.actor.principal_id!==context.principalId) {
        phase='uncertain'; error='Pending request requires its original project and principal access.';
        throw Error(error);
      }
    }
    function responseEnvelope(result, project) {
      if (!result || result.project_id!==project || typeof result.revision!=='string' || !result.revision) {
        throw Error('Response did not match the active project or lacked a revision.');
      }
      return result;
    }
    function sameFields(left,right) {
      const leftKeys=Object.keys(left).sort(),rightKeys=Object.keys(right).sort();
      return leftKeys.length===rightKeys.length && leftKeys.every((key,index)=>key===rightKeys[index] && left[key]===right[key]);
    }
    function pageFilters(resource, params, project) {
      if (!RESOURCES.has(resource)) throw Error('Unsupported research page resource.');
      if (!isObject(params)) throw Error('Invalid page descriptor.');
      if (params.project_id!==undefined && params.project_id!==project) throw Error('Page descriptor belongs to another project.');
      const allowed=PAGE_FILTERS[resource];
      if (Object.keys(params).some(key => key!=='project_id' && !allowed.includes(key))) throw Error('Unsupported page filter.');
      for (const key of allowed) if (params[key]!==undefined && !validId(params[key])) throw Error('Malformed page filter.');
      if (selection && Object.keys(params).some(key=>key!=='project_id')) {
        const descriptors=Object.values(selection.context.related||{}).filter(value=>value?.resource===resource);
        const normalized=Object.fromEntries(Object.entries(params).filter(([key])=>key!=='project_id'));
        const match=descriptors.some(value=>{
          const descriptorParams=value.params||{};
          if (descriptorParams.project_id!==project) return false;
          return sameFields(Object.fromEntries(Object.entries(descriptorParams).filter(([key])=>key!=='project_id')),normalized);
        });
        if (!match) throw Error('Page filters do not match a selected entity related descriptor.');
      }
      return Object.fromEntries(Object.entries(params).filter(([key])=>key!=='project_id'));
    }
    function validatePage(result, project, resource=null, filters={}) {
      responseEnvelope(result,project);
      if (!Array.isArray(result.items) || result.items.length>20 ||
          !(result.next_cursor===null || (typeof result.next_cursor==='string' && result.next_cursor.length>0 && result.next_cursor.length<=100000))) {
        throw Error('Malformed or oversized research page response.');
      }
      const recordFilter={
        'finding-revisions':['subject_id','finding_id'], 'evidence-links':['subject_id','subject_id'],
        'evidence-changes':['subject_id','evidence_link_id'], 'research-review-notices':['subject_id','subject_id'],
        answers:['subject_id','question_id'], 'answer-reviews':['subject_id','answer_id'],
        'question-resolutions':['subject_id','question_id'],
        observations:['subject_id','artifact_version_id'], 'negative-searches':['subject_id','artifact_version_id']
      }[resource];
      if (filters.subject_id!==undefined && ['source-derivations','observations','negative-searches'].includes(resource) && result.subject_id!==filters.subject_id) {
        throw Error('Research page envelope did not match its requested source subject.');
      }
      for (const item of result.items) {
        if (!isObject(item) || item.project_id!==project || !validId(item.id)) throw Error('Research page contained a foreign or malformed record.');
        if (recordFilter && filters.subject_id!==undefined && item[recordFilter[1]]!==filters.subject_id) throw Error('Research page contained a record outside its requested identity.');
        if (recordFilter && filters.subject_type!==undefined && item.subject_type!==filters.subject_type) throw Error('Research page contained a record outside its requested type.');
        if (resource==='source-derivations' && filters.subject_id!==undefined) {
          const parent=item.parent_version, child=item.child_version, projectOk=value=>!value || (isObject(value) && value.project_id===project && validId(value.id));
          if (!projectOk(parent) || !projectOk(child) || !(parent?.id===filters.subject_id || child?.id===filters.subject_id)) throw Error('Source derivation did not touch the requested version within this project.');
        }
        if (resource==='answer-support' && selection?.type==='answer') {
          const pinned=selection.context.entity.evidence;
          if (!Array.isArray(pinned) || !pinned.includes(item.id)) throw Error('Answer support page contained an unpinned source link.');
        }
      }
      return result;
    }
    async function readCapabilities(project) {
      const result=await call('/v1/projects/'+encodeURIComponent(project)+'/capabilities');
      if (!result || result.project_id!==project || (result.capabilities!==undefined && !isObject(result.capabilities))) {
        throw Error('Capabilities response did not match the active project.');
      }
      return clone(result.capabilities??null);
    }
    function rememberContext(result) {
      selection.context=clone(result.context); revision=result.revision; readRevision=result.revision;
      pages={context:{revision:result.revision,context:clone(result.context)}};
    }
    async function fetchEntityContext(type,id,context) {
      const segment={finding:'findings',question:'questions',answer:'answers',evidence:'evidence-links',source:'artifact-versions'}[type];
      const result=await call(`/v1/${segment}/${encodeURIComponent(id)}/context?project_id=${encodeURIComponent(context.projectId)}`);
      return validateContext(result,context.projectId,type,id);
    }

    async function load() {
      busyGuard(); readRecoveryOnce();
      if (recoveryBroken) { emit(); throw Error(error); }
      const context=scoped(); adoptCleanContext(context); checkPendingContext(context);
      const ticket=++sequence, identity={projectId:context.projectId,principalId:context.principalId,selectionId:selection?.id||null};
      busy=true; phase=pendingBody?'uncertain':'loading'; error=''; emit();
      try {
        if (selection) {
          const result=await fetchEntityContext(selection.type,selection.id,context);
          if (!isCurrent(ticket,identity)) return state();
          rememberContext(result);
        } else if (!pendingBody) {
          const result=validatePage(await call(`/v1/findings?project_id=${encodeURIComponent(context.projectId)}&limit=20`),context.projectId);
          if (!isCurrent(ticket,identity)) return state();
          revision=result.revision; readRevision=result.revision;
          pages.findings={revision:result.revision,params:{},cursor:null,items:clone(result.items),next_cursor:result.next_cursor??null};
        }
        if (!capabilities) capabilities=await readCapabilities(context.projectId);
        if (!isCurrent(ticket,identity)) return state();
        if (!pendingBody) phase='ready';
        return state();
      } catch (failure) {
        if (ticket===sequence) { phase=failure.status===409?'stale':'load-error'; error=failure.message; }
        throw failure;
      } finally { if (ticket===sequence) { busy=false; emit(); } }
    }
    function isCurrent(ticket,identity) {
      const context=contextNow();
      return ticket===sequence && context.projectId===identity.projectId && context.principalId===identity.principalId &&
        (context.activePanel===undefined || context.activePanel==='research') && (selection?.id||null)===identity.selectionId;
    }
    async function open(type,id) {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      const context=scoped(); if (draft || pendingBody) throw Error('Save, reconcile or discard the current draft before changing selection.');
      if (!['finding','question','answer','evidence','source'].includes(type) || !validId(id)) throw Error('Invalid research selection.');
      adoptCleanContext(context);
      const ticket=++sequence, identity={projectId:context.projectId,principalId:context.principalId,selectionId:selection?.id||null};
      busy=true; phase='loading'; error=''; emit();
      try {
        const result=await fetchEntityContext(type,id,context);
        if (!isCurrent(ticket,identity)) return state();
        const nextCapabilities=await readCapabilities(context.projectId);
        if (!isCurrent(ticket,identity)) return state();
        selection={type,id,context:clone(result.context)};
        capabilities=nextCapabilities;
        rememberContext(result); phase='ready'; return state();
      } catch (failure) {
        if (ticket===sequence) { phase=failure.status===409?'stale':'load-error'; error=failure.message; }
        throw failure;
      } finally { if (ticket===sequence) { busy=false; emit(); } }
    }
    async function readPage(resource,params={},cursor=null) {
      busyGuard(); const context=scoped();
      if (projectId!==context.projectId || principalId!==context.principalId) throw Error('Load or open this project with the current principal before reading pages.');
      const filters=pageFilters(resource,params,context.projectId);
      if (cursor!==null && (typeof cursor!=='string' || cursor.length===0 || cursor.length>100000)) throw Error('Invalid page cursor.');
      const ticket=++sequence, identity={projectId:context.projectId,principalId:context.principalId,selectionId:selection?.id||null};
      const query=new URLSearchParams({project_id:context.projectId,limit:'20'});
      for (const [key,value] of Object.entries(filters)) query.set(key,value);
      if (cursor!==null) query.set('cursor',cursor);
      busy=true; error=''; emit();
      try {
        const result=validatePage(await call(`/v1/${resource}?${query}`),context.projectId,resource,filters);
        if (!isCurrent(ticket,identity)) return null;
        if (Object.values(pages).some(page=>page?.revision!==result.revision)) pages={};
        pages[resource]={resource,params:clone(filters),cursor,revision:result.revision,items:clone(result.items),next_cursor:result.next_cursor};
        readRevision=result.revision; return clone(pages[resource]);
      } catch (failure) {
        if (ticket===sequence && contextNow().projectId===context.projectId && contextNow().principalId===context.principalId) {
          phase=failure.status===409?'stale':'read-error'; error=failure.message; emit();
        }
        throw failure;
      } finally { if (ticket===sequence) { busy=false; emit(); } }
    }
    async function inspectAnswer(id) {
      busyGuard(); readRecoveryOnce(); if(recoveryBroken)throw Error(error);
      const context=scoped();checkPendingContext(context);
      if(pendingBody)throw Error('Confirm the pending transaction before inspecting a replacement context.');
      if(projectId!==context.projectId||principalId!==context.principalId)throw Error('Load the current project and principal first.');
      if(!validId(id)||selection?.type!=='question')throw Error('Select the exact answer question first.');
      const ticket=++sequence, captured={projectId,principalId,selectionId:selection.id};
      busy=true;emit();
      try{
        const result=await fetchEntityContext('answer',id,context);
        if(!isCurrent(ticket,captured))return null;
        if(result.context.entity.question_id!==selection.id)throw Error('Answer belongs to another selected question.');
        return clone(result.context);
      }finally{if(ticket===sequence){busy=false;emit();}}
    }
    function edit(kind,seed={}) {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      const context=scoped(); if (pendingBody || draft) throw Error('Resolve or discard the existing draft first.');
      if (projectId!==context.projectId || principalId!==context.principalId) throw Error('Load or open the current project and principal before editing.');
      if (!SCHEMAS[kind]) throw Error('Unsupported research edit.');
      if (typeof revision!=='string') throw Error('Load a project or entity revision before editing.');
      const fields=clone(seed||{});
      if (!fields.finding_id && ['revise_finding','archive_finding','restore_finding'].includes(kind) && selection?.type==='finding') fields.finding_id=selection.id;
      if (!fields.question_id && ['create_answer','resolve_question','set_question_state'].includes(kind) && selection?.type==='question') fields.question_id=selection.id;
      if (!fields.answer_id && kind==='review_answer' && selection?.type==='answer') fields.answer_id=selection.id;
      if (kind==='record_negative_search') {
        if (selection?.type!=='source') throw Error('Select the exact immutable source version first.');
        if (!fields.id) fields.id=requestId();
        if (!fields.artifact_version_id) fields.artifact_version_id=selection.id;
        if (fields.artifact_version_id!==selection.id) throw Error('Negative search must target the selected source version.');
      }
      if (selection?.type==='source' && !['record_negative_search','create_finding'].includes(kind)) throw Error('Only a derivation finding or negative search can be authored from a source version.');
      if (selection?.type==='source' && kind==='create_finding') {
        const scope=fields.scope;
        if (!isObject(scope) || scope.artifact_version_id!==selection.id) throw Error('A source finding must retain the selected child version.');
        if (scope.derivation?.format==='workbench-derived-source/v1' &&
            scope.derivation.parent_version_id!=='' && !validId(scope.derivation.parent_version_id)) throw Error('Malformed source derivation metadata.');
      }
      if (['create_finding','create_answer'].includes(kind) && !fields.id) fields.id=requestId();
      draft={kind,fields,baseRevision:revision,recordRevision:fields.expected_record_revision??fields.expected_question_revision??null,
        entity:selection?clone(selection):null,targetIds:targetIds({...fields,op:kind}),version:0,needsReconcile:false};
      phase='editing'; error=''; emit(); return state();
    }
    function change(field,value) {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error); scoped();
      if (!draft || pendingBody) throw Error('No editable research draft.');
      if (typeof field!=='string' || !field || ['expected_revision','principal_id','project_id'].includes(field)) throw Error('That field cannot be changed in the editor.');
      if (draft.kind==='record_negative_search' && field==='artifact_version_id' && value!==draft.entity?.id) throw Error('Negative search source identity cannot change from the selected version.');
      if (draft.kind==='create_finding' && draft.entity?.type==='source' && field==='scope' &&
          (!isObject(value) || value.artifact_version_id!==draft.entity.id)) {
        throw Error('Source finding child identity cannot change from the selected source version.');
      }
      draft.fields[field]=clone(value); draft.version++; emit(); return state();
    }
    function ensureTargetIdentity(operation) {
      const submitted=targetIds(operation);
      for (const [key,value] of Object.entries(draft?.targetIds||{})) {
        if (submitted[key]!==value) throw Error('Operation target identity cannot change from the original draft.');
      }
      if (draft?.kind==='record_negative_search' && (operation.artifact_version_id!==draft.entity?.id || operation.id!==draft.fields.id)) throw Error('Negative search identity cannot change from the original source draft.');
      if (draft?.kind==='create_finding' && draft.entity?.type==='source' && operation.scope?.artifact_version_id!==draft.entity.id) throw Error('Source finding must remain bound to the selected child source version.');
    }
    function prepare(operation) {
      const context=scoped();
      if (recoveryBroken) throw Error(error);
      if (draft?.needsReconcile) throw Error('Refresh and explicitly reconcile this rejected draft before creating another transaction.');
      if (draft && operation.op!==draft.kind) throw Error('Operation does not match the current draft.');
      ensureTargetIdentity(operation);
      const safe=validateOperation(operation);
      const expected=draft?.baseRevision??revision;
      if (!validId(expected)) throw Error('A loaded base revision is required.');
      const body={project_id:context.projectId,actor:{principal_id:context.principalId},expected_revision:expected,request_id:requestId(),operations:[safe]};
      const serialized=JSON.stringify(body);
      if (utf8Bytes(serialized)>MAX_BYTES) throw Error('Research transaction exceeds the 2 MiB recovery limit.');
      return serialized;
    }
    function rejectionProof(failure,body) {
      const proof=failure?.data?.transaction_rejection;
      return isObject(proof) && Object.keys(proof).sort().join(',')==='expected_revision,principal_id,project_id,request_id,state' &&
        proof.project_id===body.project_id && proof.principal_id===body.actor.principal_id &&
        proof.request_id===body.request_id && proof.expected_revision===body.expected_revision && proof.state==='not_applied';
    }
    function clearPending() {
      try {
        storage.removeItem(RECOVERY);
        const remaining=storage.getItem(RECOVERY);
        if (remaining!==null) throw Error('Recovery entry remains in storage.');
        pendingBody=null; recoveryRaw=null; return true;
      } catch (failure) {
        error=`Could not clear the exact recovery entry: ${failure.message}`;
        return false;
      }
    }
    function expectedReceipt(body,result) {
      const operation=body.operations[0];
      const records=Array.isArray(result?.changed_records)?result.changed_records:[];
      if (operation.op==='review_answer') return records.some(row=>row?.type==='answer_review' && validId(row.id) &&
        row.answer_id===operation.answer_id && row.outcome===operation.outcome);
      if (operation.op==='set_evidence_state') return records.some(row=>row?.type==='evidence_change' && validId(row.id) &&
        row.evidence_link_id===operation.evidence_link_id && row.state===operation.state);
      if (operation.op==='record_negative_search') return records.some(row=>row?.type==='negative_search' && row.id===operation.id);
      const descriptor={
        create_finding:['finding',operation.id], revise_finding:['finding',operation.finding_id],
        archive_finding:['finding',operation.finding_id], restore_finding:['finding',operation.finding_id],
        create_answer:['answer',operation.id], resolve_question:['question',operation.question_id],
        set_question_state:['question',operation.question_id]
      }[operation.op];
      return Boolean(descriptor && records.some(row=>row?.type===descriptor[0] && row.id===descriptor[1]));
    }
    async function transmit(operation, isRetry=false) {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      const context=scoped(); checkPendingContext(context);
      if (pendingBody && !isRetry) throw Error('A frozen request exists. Retry that exact request before another submit.');
      if (context.projectId!==projectId || context.principalId!==principalId) throw Error('Return to the original project and principal before sending.');
      if (!pendingBody) {
        const serialized=prepare(operation || (draft ? {...draft.fields,op:draft.kind} : null));
        try { if (!storage) throw Error('Persistent browser storage is required before sending.'); storage.setItem(RECOVERY,serialized); }
        catch (failure) { throw Error('Could not persist the exact research transaction; nothing was sent. '+failure.message); }
        pendingBody=serialized; recoveryRaw=serialized;
      }
      const body=JSON.parse(pendingBody); mutation=true; busy=true; phase='submitting'; error=''; emit();
      try {
        const result=await call('/v1/transactions',{method:'POST',headers:{'Content-Type':'application/json'},body:pendingBody});
        const sameStore=typeof result?.revision==='string' && result.revision.startsWith(body.expected_revision.split(':')[0]+':');
        if (!result || (result.project_id!==undefined && result.project_id!==body.project_id) ||
            result.request_id!==body.request_id || !sameStore || !expectedReceipt(body,result)) {
          throw Error('Transaction acknowledgment did not match the frozen request.');
        }
        draft=null; revision=result.revision;
        if (clearPending()) phase='saved'; else phase='saved-cleanup-error';
        return result;
      } catch (failure) {
        if (rejectionProof(failure,body)) {
          if (draft) draft.needsReconcile=true; freshContextRevision=null; freshContextKey=null;
          if (clearPending()) { phase='rejected'; error=failure.message||'The service proved this transaction was not applied.'; }
          else phase='rejected-cleanup-error';
        } else { phase='uncertain'; error=failure.message||'Outcome unknown. Retry sends the exact frozen bytes.'; }
        throw failure;
      } finally { busy=false; mutation=false; emit(); }
    }
    async function submit(operation) {
      if (!isObject(operation)) throw Error('A complete reviewed operation is required.');
      if (draft && operation.op!==draft.kind) throw Error('Operation does not match the current draft.');
      if (['create_finding','create_answer','record_negative_search'].includes(operation.op) && !operation.id) operation={...operation,id:draft?.fields.id||requestId()};
      if (operation.op==='record_negative_search' && !operation.artifact_version_id) operation={...operation,artifact_version_id:draft?.entity?.id};
      return transmit(operation);
    }
    async function retry() {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      if (!pendingBody) throw Error('No pending research transaction to retry.');
      return transmit(undefined,true);
    }
    function discard() {
      if (busy || mutation || pendingBody || recoveryBroken) throw Error('A pending or malformed recovery record cannot be discarded here.');
      const context=contextNow();
      if (draft && (context.projectId!==projectId || context.principalId!==principalId)) throw Error('Return to the original project and principal before discarding this draft.');
      draft=null; phase=selection?'ready':'idle'; error=''; emit(); return true;
    }
    async function refreshContext() {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      const context=scoped(); checkPendingContext(context);
      if (context.projectId!==projectId || context.principalId!==principalId) throw Error('Load or open the current project and principal before refreshing context.');
      if (pendingBody) throw Error('Confirm the pending transaction before refreshing context.');
      const captured={projectId:context.projectId,principalId:context.principalId,selectionId:selection?.id||null,type:selection?.type||null};
      const ticket=++sequence; busy=true; emit();
      try {
        let result;
        if (selection) result=await fetchEntityContext(selection.type,selection.id,context);
        else result=responseEnvelope(await call(`/v1/findings?project_id=${encodeURIComponent(context.projectId)}&limit=20`),context.projectId);
        if (!isCurrent(ticket,captured)) return state();
        if (selection) rememberContext(result);
        else {
          validatePage(result,context.projectId);
          revision=result.revision; readRevision=result.revision;
          pages.findings={revision:result.revision,params:{},cursor:null,items:clone(result.items),next_cursor:result.next_cursor??null};
        }
        freshContextRevision=result.revision; freshContextKey=contextKey(); phase=draft?'editing':'ready'; return state();
      } catch (failure) {
        if (ticket===sequence) { phase=failure.status===409?'stale':'read-error'; error=failure.message; }
        throw failure;
      } finally { if (ticket===sequence) { busy=false; emit(); } }
    }
    async function reconcile(operation,acknowledged) {
      busyGuard(); readRecoveryOnce(); if (recoveryBroken) throw Error(error);
      if (acknowledged!==true || !draft || pendingBody) throw Error('Explicit reviewed reconciliation is required, with no uncertain transaction.');
      const context=scoped();
      if (typeof freshContextRevision!=='string' || freshContextKey!==contextKey()) throw Error('Refresh the selected context after rejection before reconciling.');
      if (!isObject(operation) || operation.op!==draft.kind) throw Error('Reconciled operation does not match this draft.');
      if (['create_finding','create_answer','record_negative_search'].includes(operation.op) && !operation.id) operation={...operation,id:draft.fields.id};
      if (operation.op==='record_negative_search' && !operation.artifact_version_id) operation={...operation,artifact_version_id:draft.entity?.id};
      ensureTargetIdentity(operation);
      const safe=validateOperation(operation);
      draft={...draft,fields:safe,baseRevision:freshContextRevision,recordRevision:safe.expected_record_revision??safe.expected_question_revision??draft.recordRevision,needsReconcile:false,version:draft.version+1};
      revision=freshContextRevision; phase='editing'; error=''; emit(); return state();
    }
    // Recovery inspection is constructor-time so the dashboard can guard navigation before load().
    readRecoveryOnce();
    return {state,load,open,readPage,inspectAnswer,edit,change,submit,retry,discard,refreshContext,reconcile};
  }
  const exported={createSession,RECOVERY};
  if (typeof module!=='undefined' && module.exports) module.exports=exported;
  root.WorkspaceResearchSession=exported;
})(typeof globalThis!=='undefined'?globalThis:this);
