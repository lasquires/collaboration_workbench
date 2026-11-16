'use strict';
(function (root) {
  const RECOVERY = 'workbench.task.pending.v1';
  const MAX_BYTES = 2 * 1024 * 1024;
  const TASK_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const RESOURCES = new Set(['attempts',
  'validations',
  'blockers',
  'claims']);
  const SCHEMAS = {
    set_task_scope: ['area_id',
    'decomposition_complete'],
    move_task: ['parent_id',
    'required_for_parent',
    'expected_parents'],
    attach_child: ['parent_id',
    'required_for_parent',
    'expected_parents'],
    detach_child: ['expected_parents'],
    archive_task: ['expected_parents'],
    reopen_task: [],
    claim_task: ['reserved_paths',
    'lease_seconds'],
    heartbeat_claim: ['lease_seconds'],
    handoff_claim: ['to_principal_id',
    'session_id',
    'lease_seconds'],
    release_claim: [],
    record_attempt: ['boundary_id',
    'outcome',
    'evidence'],
    resolve_attempt: ['attempt_id'],
    record_validation: ['outcome',
    'inputs',
    'outputs',
    'evidence'],
    complete_task: [],
    record_blocker: ['kind',
    'target_id'],
    resolve_blocker: ['blocker_id'],
    merge_tasks: ['absorbed_id',
    'preview_guard',
    'choices',
    'metadata_choices',
    'source_references'],
    reverse_merge: ['merge_id',
    'preview_guard'],
    compensate_merge: ['merge_id',
    'preview_guard',
    'choices',
    'source_references']
  };
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  const validText = (value,
  max=4000) => typeof value === 'string' && value.trim().length > 0 && [...value].length <= max;
  const validId = value => typeof value === 'string' && value.length > 0 && value.length <= 200;
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const bytes = value => new TextEncoder().encode(value).length;
  function validateOperation(operation,
  selectedTask) {
    if (!isObject(operation) || !Object.hasOwn(SCHEMAS,
    operation.op)) throw Error('Unsupported task operation.');
    const allowed = new Set(['op',
    'task_id',
    'expected_record_revision',
    'rationale',
    ...SCHEMAS[operation.op]]);
    if (Object.keys(operation).some(key => !allowed.has(key))) throw Error('Task operation contains unsupported fields.');
    if (operation.task_id !== selectedTask || !TASK_ID.test(operation.task_id || '')) throw Error('Operation target must match the selected stable task identity.');
    if (!positive(operation.expected_record_revision)) throw Error('Current task record revision is required.');
    if (!validText(operation.rationale)) throw Error('A rationale is required for every task operation.');
    const keys = Object.keys(operation).filter(key => !['op',
    'task_id',
    'expected_record_revision',
    'rationale'].includes(key));
    const has = key => Object.hasOwn(operation,
    key);
    const parentGuards = () => {
      if (!isObject(operation.expected_parents) || Object.keys(operation.expected_parents).some(key => !TASK_ID.test(key) || !positive(operation.expected_parents[key]))) throw Error('Current parent structure revisions are required.');
    }
    ;
    switch (operation.op) {
      case 'set_task_scope':
      if (!keys.length || (has('area_id') && operation.area_id !== null && !validId(operation.area_id)) ||
      (has('decomposition_complete') && typeof operation.decomposition_complete !== 'boolean')) throw Error('Malformed task scope update.');
      break;
      case 'move_task': case 'attach_child':
      if (!validId(operation.parent_id) || (has('required_for_parent') && typeof operation.required_for_parent !== 'boolean')) throw Error('A destination task is required.');
      parentGuards();
      break;
      case 'detach_child': case 'archive_task': parentGuards();
      break;
      case 'claim_task':
      if (has('lease_seconds') && (!Number.isSafeInteger(operation.lease_seconds) || operation.lease_seconds < 60 || operation.lease_seconds > 86400)) throw Error('Claim duration must be 60–86400 seconds.');
      if (has('reserved_paths') && (!Array.isArray(operation.reserved_paths) || operation.reserved_paths.length > 500)) throw Error('Invalid claim reservations.');
      break;
      case 'heartbeat_claim':
      if (!Number.isSafeInteger(operation.lease_seconds) || operation.lease_seconds < 60 || operation.lease_seconds > 86400) throw Error('Claim duration must be 60–86400 seconds.');
      break;
      case 'handoff_claim':
      if (!validId(operation.to_principal_id) || (has('session_id') && !validId(operation.session_id)) ||
      (has('lease_seconds') && (!Number.isSafeInteger(operation.lease_seconds) || operation.lease_seconds < 60 || operation.lease_seconds > 86400))) throw Error('A project member and valid claim duration are required.');
      break;
      case 'record_attempt':
      if (!validText(operation.boundary_id,
      200) || !['failed',
      'passed',
      'blocked',
      'inconclusive'].includes(operation.outcome) || !Array.isArray(operation.evidence) || !operation.evidence.length) throw Error('Attempt needs a boundary, outcome, and evidence.');
      break;
      case 'resolve_attempt': if (!validId(operation.attempt_id)) throw Error('Attempt identity is required.');
      break;
      case 'record_validation':
      if (!['passed',
      'failed'].includes(operation.outcome) || !isObject(operation.inputs) || !isObject(operation.outputs) || !Array.isArray(operation.evidence) || !operation.evidence.length) throw Error('Validation needs an outcome, inputs, outputs, and pinned evidence.');
      break;
      case 'record_blocker':
      if (!['question',
      'prerequisite',
      'review'].includes(operation.kind) ||
      (operation.kind !== 'review' && !validId(operation.target_id)) || (has('target_id') && operation.target_id !== null && !validId(operation.target_id))) throw Error('Blocker kind and target do not match.');
      break;
      case 'resolve_blocker': if (!validId(operation.blocker_id)) throw Error('Blocker identity is required.');
      break;
      case 'merge_tasks':
      if (!validId(operation.absorbed_id) || !validText(operation.preview_guard,
      128) || !isObject(operation.choices) || !isObject(operation.metadata_choices) || !validSources(operation.source_references)) throw Error('Merge needs its fresh preview and explicit choices.');
      break;
      case 'reverse_merge': if (!validId(operation.merge_id) || !validText(operation.preview_guard,
      128)) throw Error('Reversal needs its fresh preview.');
      break;
      case 'compensate_merge':
      if (!validId(operation.merge_id) || !validText(operation.preview_guard,
      128) || !isObject(operation.choices) || !validSources(operation.source_references)) throw Error('Compensation needs its fresh preview and explicit choices.');
      break;
    }
    return clone(operation);
  }
  function validSources(value) {
    return Array.isArray(value) && value.length > 0 && value.length <= 100 && value.every(item => validText(item));
  }
  function validateContext(result, project, task) {
    const entity=result?.context?.task, redirect=result?.redirect;
    const redirected=isObject(redirect)&&redirect.from===task&&redirect.to===entity?.id&&redirect.to!==task&&TASK_ID.test(redirect.to||'');
    if (!isObject(result)||result.project_id!==project||typeof result.revision!=='string'||!result.revision||
        !isObject(entity)||entity.project_id!==project||!positive(entity.record_revision)||!positive(entity.structure_revision)||
        (entity.id!==task&&!redirected)||(redirect!==undefined&&redirect!==null&&!redirected)) {
      throw Error('Task context did not match the selected project and task.');
    }
    return result;
  }
  function createSession({
    api,
    getContext,
    onChange=()=>{
    }
    ,
    onDirty=()=>{
    }
    ,
    storage=null,
    requestId=()=>root.crypto.randomUUID() }
  ) {
    if (typeof api !== 'function' || typeof getContext !== 'function') throw Error('Task API and context are required.');
    let projectId=null,
    principalId=null,
    taskId=null,
    selection=null,
    capabilities=null,
    revision=null,
    pages={
    }
    ;
    let draft=null,
    pendingBody=null,
    recoveryRaw=null,
    recoveryBroken=false,
    recoveryChecked=false;
    let phase='idle',
    error='',
    busy=false,
    mutation=false,
    sequence=0,
    freshRevision=null,
    freshKey=null;
    const now=()=>{
      const value=getContext()||{
      }
      ;
      return{
        projectId:value.projectId??null,
        principalId:value.principalId??null,
        taskId:value.taskId??null,
        activePanel:value.activePanel}
      ;
    }
    ;
    const state=()=>clone({
      projectId,
      principalId,
      taskId,
      context:selection?.context||null,
      selection,
      capabilities,
      revision,
      pages,
      draft,
      pendingBody,
      recoveryRaw,
      phase,
      error,
      busy}
    );
    function emit(){
      const c=now(),
      same=c.projectId===projectId&&c.principalId===principalId&&c.taskId===taskId&&(c.activePanel===undefined||c.activePanel==='task-workflows');
      if(same)onDirty(Boolean(draft)||pendingBody!==null||phase==='submitting'||phase==='uncertain');
      onChange(state());
    }
    const call=(path,
    options)=>api(path,
    options);
    function busyGuard(){
      if(busy||mutation)throw Error('Another task action is in flight.');
    }
    function scoped(){
      const c=now();
      if(!c.projectId||!c.principalId||!c.taskId)throw Error('Choose a project, principal, and task first.');
      if(c.activePanel!==undefined&&c.activePanel!=='task-workflows')throw Error('Return to the task workflow panel before continuing.');
      if((draft||pendingBody)&&((projectId&&c.projectId!==projectId)||(principalId&&c.principalId!==principalId)||(taskId&&c.taskId!==taskId)))throw Error('Return to the original project, principal, and task before resolving this draft.');
      return c;
    }
    function key(){
      return `${projectId}|${principalId}|${taskId}`;
    }
    function isCurrent(ticket,
    c){
      const n=now();
      return ticket===sequence&&n.projectId===c.projectId&&n.principalId===c.principalId&&n.taskId===c.taskId&&(n.activePanel===undefined||n.activePanel==='task-workflows');
    }
    function readRecovery(){
      if(recoveryChecked)return;
      recoveryChecked=true;
      if(!storage)return;
      let raw;
      try{
        raw=storage.getItem(RECOVERY);
      }
      catch(e){
        recoveryBroken=true;
        phase='recovery-error';
        error=`Could not inspect task recovery: ${e.message}`;
        return;
      }
      if(raw===null)return;
      recoveryRaw=raw;
      try{
        pendingBody=validatePending(raw);
        const body=JSON.parse(raw),
        op=body.operations[0];
        projectId=body.project_id;
        principalId=body.actor.principal_id;
        taskId=op.task_id;
        revision=body.expected_revision;
        draft={
          kind:op.op,
          fields:clone(op),
          baseRevision:body.expected_revision,
          recordRevision:op.expected_record_revision,
          targetId:op.task_id,
          needsReconcile:false}
        ;
        selection={
          id:taskId,
          context:null,
          redirect:null}
        ;
        phase='uncertain';
        error='A task transaction was recovered. Reads are safe; retry resends the original bytes.';
      }
      catch(e){
        recoveryBroken=true;
        phase='recovery-error';
        error=e.message;
      }
    }
    function validatePending(raw){
      if(typeof raw!=='string'||bytes(raw)>MAX_BYTES)throw Error('Pending task transaction is malformed or exceeds 2 MiB; it was retained.');
      let body;
      try{
        body=JSON.parse(raw);
      }
      catch{
        throw Error('Pending task transaction is malformed; it was retained.');
      }
      if(!isObject(body)||Object.keys(body).sort().join(',')!=='actor,expected_revision,operations,project_id,request_id'||!validId(body.project_id)||!validId(body.request_id)||!validId(body.expected_revision)||!isObject(body.actor)||Object.keys(body.actor).sort().join(',')!=='principal_id'||!validId(body.actor.principal_id)||!Array.isArray(body.operations)||body.operations.length!==1)throw Error('Pending task transaction has invalid scope or shape; it was retained.');
      const op=body.operations[0];
      if(!TASK_ID.test(op?.task_id||''))throw Error('Pending task target is invalid; it was retained.');
      validateOperation(op,
      op.task_id);
      return raw;
    }
    function envelope(result,
    c){
      if(!isObject(result)||result.project_id!==c.projectId||typeof result.revision!=='string'||!result.revision)throw Error('Task response did not match the active project and snapshot.');
      return result;
    }
    async function readCaps(c){
      const result=await call(`/v1/projects/${encodeURIComponent(c.projectId)}/capabilities`);
      if(!isObject(result)||result.project_id!==c.projectId||!isObject(result.capabilities))throw Error('Capabilities response did not match the active project.');
      return clone(result.capabilities);
    }
    function remember(result) {
      const redirect=result.redirect??null;
      selection={id:taskId,context:redirect?null:clone(result.context),redirect:clone(redirect)};
      revision=result.revision;
      pages=redirect?{}:{context:{revision:result.revision,context:clone(result.context)}};
    }
    async function getTaskContext(c){
      return validateContext(await call(`/v1/tasks/${encodeURIComponent(c.taskId)}/context?project_id=${encodeURIComponent(c.projectId)}`),
      c.projectId,
      c.taskId);
    }
    async function load(){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      const c=scoped();
      if(pendingBody&&(c.projectId!==projectId||c.principalId!==principalId||c.taskId!==taskId))throw Error('Pending task transaction requires its original project, principal, and task.');
      if(!pendingBody&&(projectId!==c.projectId||principalId!==c.principalId||taskId!==c.taskId)){
        projectId=c.projectId;
        principalId=c.principalId;
        taskId=c.taskId;
        selection=null;
        pages={
        }
        ;
        revision=null;
        capabilities=null;
        freshRevision=null;
        freshKey=null;
      }
      const ticket=++sequence;
      busy=true;
      phase=pendingBody?'uncertain':'loading';
      error='';
      emit();
      try{
        const result=await getTaskContext(c);
        if(!isCurrent(ticket,
        c))return state();
        remember(result);
        const caps=await readCaps(c);
        if(!isCurrent(ticket,
        c))return state();
        capabilities=caps;
        if(!pendingBody)phase=selection.redirect?'redirect':draft?'editing':'ready';
        return state();
      }
      catch(e){
        if(isCurrent(ticket,
        c)){
          phase=e.status===409?'stale':'load-error';
          error=e.message;
        }
        throw e;
      }
      finally{
        if(ticket===sequence){
          busy=false;
          emit();
        }
      }
    }
    async function readPage(resource,
    params={
    }
    ,
    cursor=null){
      busyGuard();
      const c=scoped();
      if(!RESOURCES.has(resource))throw Error('Unsupported task collection.');
      if(params===null)params={
      }
      ;
      if(!isObject(params)||Object.keys(params).some(key=>key!=='task_id')||(params.task_id!==undefined&&params.task_id!==c.taskId))throw Error('Task collection filter must match the selected task.');
      if(projectId!==c.projectId||principalId!==c.principalId||taskId!==c.taskId||!selection?.context)throw Error('Load the selected task before reading its collections.');
      if(cursor!==null&&(typeof cursor!=='string'||!cursor||cursor.length>100000))throw Error('Invalid task collection cursor.');
      const ticket=++sequence,
      rev=revision;
      const query=new URLSearchParams({
        project_id:c.projectId,
        task_id:c.taskId,
        limit:'20',
        order:'desc'}
      );
      if(resource==='blockers')query.set('state',
      'open');
      if(cursor)query.set('cursor',
      cursor);
      busy=true;
      emit();
      try{
        const result=envelope(await call(`/v1/${resource}?${query}`),
        c);
        if(!isCurrent(ticket,
        c))return null;
        if(result.revision!==rev||!Array.isArray(result.items)||result.items.length>20||!(result.next_cursor===null||typeof result.next_cursor==='string'))throw Error('Task collection did not match its snapshot or page bound.');
        for(const item of result.items){
          if(!isObject(item)||item.project_id!==c.projectId||!TASK_ID.test(item.task_id||''))throw Error('Task collection returned a row outside the selected project.');
        }
        pages[resource]={
          resource,
          taskId:c.taskId,
          revision:result.revision,
          cursor,
          items:clone(result.items),
          next_cursor:result.next_cursor}
        ;
        return clone(pages[resource]);
      }
      catch(e){
        if(isCurrent(ticket,
        c)){
          phase=e.status===409?'stale':'read-error';
          error=e.message;
        }
        throw e;
      }
      finally{
        if(ticket===sequence){
          busy=false;
          emit();
        }
      }
    }
    function edit(kind,
    seed={
    }
    ){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      const c=scoped();
      if(pendingBody||draft)throw Error('Resolve the existing task draft first.');
      if(projectId!==c.projectId||principalId!==c.principalId||taskId!==c.taskId||!selection?.context)throw Error('Load the exact selected task before editing.');
      if(!Object.hasOwn(SCHEMAS,
      kind))throw Error('Unsupported task operation.');
      if(seed.expected_record_revision!==undefined&&seed.expected_record_revision!==selection.context.task.record_revision)throw Error('Read and explicitly reconcile the changed task revision.');
      if(seed.task_id!==undefined&&seed.task_id!==taskId)throw Error('Task target must match the selected task.');
      const fields={
        ...clone(seed),
        op:kind,
        task_id:taskId,
        expected_record_revision:selection.context.task.record_revision}
      ;
      draft={
        kind,
        fields,
        baseRevision:revision,
        recordRevision:fields.expected_record_revision,
        targetId:taskId,
        needsReconcile:false}
      ;
      phase='editing';
      error='';
      emit();
      return state();
    }
    function change(field,
    value){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      scoped();
      if(!draft||pendingBody)throw Error('No editable task draft.');
      if(['task_id',
      'expected_revision',
      'expected_record_revision',
      'project_id'].includes(field))throw Error('Task identity and transaction guards cannot be edited directly.');
      draft.fields[field]=clone(value);
      emit();
      return state();
    }
    function rejectionProof(failure,
    body){
      const proof=failure?.data?.transaction_rejection;
      return isObject(proof)&&Object.keys(proof).sort().join(',')==='expected_revision,principal_id,project_id,request_id,state'&&proof.project_id===body.project_id&&proof.principal_id===body.actor.principal_id&&proof.request_id===body.request_id&&proof.expected_revision===body.expected_revision&&proof.state==='not_applied';
    }
    function clearPending(){
      try{
        storage.removeItem(RECOVERY);
        if(storage.getItem(RECOVERY)!==null)throw Error('Recovery entry remains.');
        pendingBody=null;
        recoveryRaw=null;
        return true;
      }
      catch(e){
        error=`Could not clear the exact task recovery entry: ${e.message}`;
        return false;
      }
    }
    function hasReceipt(body,
    result){
      const op=body.operations[0];
      return Array.isArray(result?.changed_records)&&result.changed_records.some(row=>row?.type==='task'&&row.id===op.task_id&&positive(row.record_revision));
    }
    async function transmit(operation,
    retry=false){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      const c=scoped();
      if(pendingBody&&!retry)throw Error('Retry the exact frozen task request before another write.');
      if(projectId!==c.projectId||principalId!==c.principalId||taskId!==c.taskId)throw Error('Return to the original task context before sending.');
      if(!pendingBody){
        const safe=validateOperation(operation||draft?.fields,
        taskId);
        if(draft?.needsReconcile)throw Error('Refresh and explicitly reconcile the rejected task draft first.');
        const raw=JSON.stringify({
          project_id:c.projectId,
          actor:{
            principal_id:c.principalId}
          ,
          expected_revision:draft?.baseRevision??revision,
          request_id:requestId(),
          operations:[safe]}
        );
        if(bytes(raw)>MAX_BYTES)throw Error('Task transaction exceeds the 2 MiB recovery limit.');
        try{
          if(!storage)throw Error('Persistent storage is required before sending.');
          storage.setItem(RECOVERY,
          raw);
        }
        catch(e){
          throw Error(`Could not persist the exact task request; nothing was sent. ${e.message}`);
        }
        pendingBody=raw;
        recoveryRaw=raw;
      }
      const body=JSON.parse(pendingBody);
      mutation=true;
      busy=true;
      phase='submitting';
      error='';
      emit();
      try{
        const result=await call('/v1/transactions',
        {
          method:'POST',
          headers:{
            'Content-Type':'application/json'}
          ,
          body:pendingBody}
        );
        const sameStore=typeof result?.revision==='string'&&result.revision.startsWith(body.expected_revision.split(':',
        1)[0]+':');
        if(!result||(result.project_id!==undefined&&result.project_id!==body.project_id)||result.request_id!==body.request_id||!sameStore||!hasReceipt(body,
        result))throw Error('Task transaction acknowledgment did not match its frozen target.');
        draft=null;
        revision=result.revision;
        if(clearPending())phase='saved';
        else phase='saved-cleanup-error';
        return result;
      }
      catch(e){
        if(rejectionProof(e,
        body)){
          if(draft)draft.needsReconcile=true;
          freshRevision=null;
          freshKey=null;
          if(clearPending()){
            phase='rejected';
            error=e.message||'The service proved the task transaction was not applied.';
          }
          else phase='rejected-cleanup-error';
        }
        else{
          phase='uncertain';
          error=e.message||'Task write outcome is uncertain; retry sends the exact frozen bytes.';
        }
        throw e;
      }
      finally{
        busy=false;
        mutation=false;
        emit();
      }
    }
    async function submit(operation){
      const safe=validateOperation(operation||draft?.fields,
      taskId);
      if(draft&&safe.op!==draft.kind)throw Error('Operation does not match the task draft.');
      if(draft&&safe.expected_record_revision!==draft.recordRevision)throw Error('Submitted task revision must match the captured draft guard until explicit reconciliation.');
      return transmit(safe);
    }
    async function retry(){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      if(!pendingBody)throw Error('No pending task transaction to retry.');
      return transmit(undefined,
      true);
    }
    function discard(){
      if(busy||mutation||pendingBody||recoveryBroken)throw Error('A pending or malformed task recovery record cannot be discarded here.');
      const c=now();
      if(draft&&(c.projectId!==projectId||c.principalId!==principalId||c.taskId!==taskId))throw Error('Return to the original task context before discarding.');
      draft=null;
      phase='ready';
      error='';
      emit();
      return true;
    }
    async function refreshContext(){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      const c=scoped();
      if(pendingBody)throw Error('Confirm the pending task transaction before refreshing.');
      if(c.projectId!==projectId||c.principalId!==principalId||c.taskId!==taskId)throw Error('Load the selected task before refreshing context.');
      const ticket=++sequence;
      busy=true;
      emit();
      try{
        const result=await getTaskContext(c);
        if(!isCurrent(ticket,
        c))return state();
        remember(result);
        freshRevision=selection.redirect?null:result.revision;
        freshKey=selection.redirect?null:key();
        phase=selection.redirect?'redirect':draft?'editing':'ready';
        return state();
      }
      catch(e){
        if(ticket===sequence){
          phase=e.status===409?'stale':'read-error';
          error=e.message;
        }
        throw e;
      }
      finally{
        if(ticket===sequence){
          busy=false;
          emit();
        }
      }
    }
    async function reconcile(operation,
    acknowledged){
      busyGuard();
      readRecovery();
      if(recoveryBroken)throw Error(error);
      if(acknowledged!==true||!draft||pendingBody||!draft.needsReconcile)throw Error('Explicit reviewed task reconciliation is required.');
      const c=scoped();
      if(freshRevision===null||freshKey!==key())throw Error('Refresh the selected task context after rejection before reconciling.');
      const safe=validateOperation(operation,
      taskId);
      if(safe.op!==draft.kind)throw Error('Reconciled operation does not match the draft.');
      if(safe.expected_record_revision!==selection.context.task.record_revision)throw Error('Reconciled task record guard must match the freshly read task.');
      draft={
        ...draft,
        fields:safe,
        baseRevision:freshRevision,
        recordRevision:safe.expected_record_revision,
        needsReconcile:false}
      ;
      revision=freshRevision;
      phase='editing';
      error='';
      emit();
      return state();
    }
    readRecovery();
    return{
      state,
      load,
      readPage,
      edit,
      change,
      submit,
      retry,
      discard,
      refreshContext,
      reconcile}
    ;
  }
  const exported={
    createSession,
    RECOVERY,
    validateOperation};
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
  root.WorkspaceTaskSession=exported;
}
)(typeof globalThis!=='undefined'?globalThis:this);
