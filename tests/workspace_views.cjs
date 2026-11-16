const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('docs/roadmap-dashboard.html', 'utf8');
assert.equal(Buffer.compare(Buffer.from(html,'utf8'),fs.readFileSync('docs/roadmap-dashboard.html')),0,
  'the declared UTF-8 dashboard does not silently replace mixed-encoding bytes');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
assert.doesNotThrow(() => new vm.Script(source), 'dashboard inline script parses');
const context = { module: { exports: {} }, globalThis: {}, URLSearchParams };
vm.runInNewContext(source, context);
const model = context.module.exports;
const plain = value => JSON.parse(JSON.stringify(value));
const editOperation={op:'revise_question',question_id:'q',expected_record_revision:1,
  text:'My wording',expected_evidence:'My evidence',criteria:{any_of:[{identifier:'x',allow_identifier_only:true}]},rationale:'Reviewed'};
const currentQuestion={id:'q',record_revision:2,text:'Concurrent wording',expected_evidence:'Current evidence',criteria:{}};
assert.equal(model.reconcileQuestionRevision(editOperation,currentQuestion,{}),null,
  'every edited question field requires an explicit conflict choice');
const revised=model.reconcileQuestionRevision(editOperation,currentQuestion,
  {text:'draft',expected_evidence:'latest',criteria:'latest'});
assert.equal(revised.expected_record_revision,2);
assert.equal(revised.text,'My wording');
assert.equal(revised.expected_evidence,'Current evidence');
assert.deepEqual(plain(revised.criteria),{});
assert.equal(model.reconcileQuestionRevision(editOperation,{...currentQuestion,id:'other'},
  {text:'draft',expected_evidence:'draft',criteria:'draft'}),null,'cannot reconcile against another question');
assert.match(html,/id="questionEditForm"/);
assert.match(html, /<button\b[^>]*\bid="discardDraft"/,
  'the runtime draft-discard handler has an actual control during startup');

assert.equal(typeof model.transactionPayload, 'function');
assert.equal(typeof model.resolveConflict, 'function');
assert.equal(typeof model.postTransaction, 'function');
const ops = [{ op: 'decide_hint', hint_id: 'H-1', state: 'dismissed', reason: 'Different build' }];
const payload = model.transactionPayload('P-1', 'principal-1', 'store:3', 'request-fixed', ops);
assert.deepEqual(plain(payload), {
  project_id: 'P-1', actor: { principal_id: 'principal-1' }, expected_revision: 'store:3',
  request_id: 'request-fixed', operations: ops,
});
assert.equal(model.resolveConflict({ body: payload }, 'store:4', 'request-rebased', false), null,
  'conflict cannot silently rebase without explicit acknowledgement');
const rebased = model.resolveConflict({ body: payload }, 'store:4', 'request-rebased', true);
assert.equal(rebased.expected_revision, 'store:4');
assert.equal(rebased.request_id, 'request-rebased', 'changed expected revision receives a new idempotency key');

const first = model.pageState();
const page1 = model.mergePage(first, { items: [{ id: 'O-1', value: 1 }], next_cursor: 'c2', revision: 'store:5' });
const page2 = model.mergePage(page1, { items: [{ id: 'O-1', value: 2 }, { id: 'O-2' }], next_cursor: null, revision: 'store:5' });
assert.equal(page2.items.length, 2, 'pages merge and update repeated records by identity');
assert.equal(page2.items[0].value, 2);
assert.equal(page2.cursor, null);

const differences = model.compareObservations(
  { identifier: 'speed', namespace: 'objc', scope_key: 'Player', value_json: '3', binary_build_id: 'A' },
  { identifier: 'speed', namespace: 'objc', scope_key: 'Player', value_json: '4', binary_build_id: 'B' },
);
assert.ok(differences.some(x => x.key === 'value_json' && !x.same));
assert.ok(differences.some(x => x.key === 'binary_build_id' && !x.same));

(async () => {
  const calls = [];
  let responseStatus = 409;
  const fakeFetch = async (url, options) => {
    calls.push({ url, options, body: JSON.parse(options.body) });
    return { status: responseStatus, ok: responseStatus < 400, json: async () => ({ latest_revision: 'store:4' }) };
  };
  const staleResponse = await model.postTransaction(fakeFetch, payload);
  assert.equal(staleResponse.status, 409, 'fake fetch exercises a revision conflict');
  assert.equal(calls[0].url, '/v1/transactions');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].body.request_id, 'request-fixed', 'fake fetch observes exact retry identity');
  assert.equal(calls[0].body.expected_revision, 'store:3');
  const reconciled = model.resolveConflict({ body: payload }, 'store:4', 'request-after-ack', true);
  responseStatus = 200;
  await model.postTransaction(fakeFetch, reconciled);
  assert.equal(calls[1].body.expected_revision, 'store:4');
  assert.equal(calls[1].body.request_id, 'request-after-ack');
  assert.deepEqual(calls[1].body.operations, calls[0].body.operations);
  const retries = [];
  const uncertainFetch = async (url, options) => { retries.push(JSON.parse(options.body)); if (retries.length === 1) throw Error('connection lost'); return { status: 200 }; };
  await assert.rejects(model.postTransaction(uncertainFetch, payload), /connection lost/);
  await model.postTransaction(uncertainFetch, payload);
  assert.deepEqual(retries[0], retries[1], 'uncertain retries reuse the identical request ID and body');
  const access=require('../docs/workspace-access.js').createAccess({location:{href:'https://workbench.example/',origin:'https://workbench.example'},storage:{getItem:()=> 'x'.repeat(64)}});
  await model.postTransaction(fakeFetch,payload,access.options);
  assert.equal(calls.at(-1).options.headers.get('Authorization'),'Bearer '+'x'.repeat(64),'initial and retry writes use session authentication');
  assert.equal(calls.at(-1).options.redirect,'error');
  assert.match(html,/postTransaction\(fetch,payload,root\.WorkspaceAccess\.options\)/);
  assert.match(html,/postTransaction\(fetch,pendingRetry,root\.WorkspaceAccess\.options\)/);
  assert.match(html, /textContent=esc\(/, 'untrusted API strings are assigned as text, not HTML');
  for (const endpoint of ['/v1/projects', '/v1/observations', '/v1/findings', '/v1/questions', '/v1/hints', '/v1/inbox', '/v1/documents', '/v1/discussions', '/v1/proposals', '/v1/feedback'])
    assert.ok(html.includes(endpoint) || html.includes(`'${endpoint.slice(4)}'`), `panel/API resource is wired: ${endpoint}`);
  assert.match(html, /name="identifier"/);
  assert.match(html, /name="namespace"/);
  assert.match(html, /name="scope_key"/);
  console.log('research workspace view checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
assert.match(html,/data-panel="research"/,'research authoring is reachable from dashboard navigation');
assert.match(html,/id="researchWorkspace"/);
assert.ok(html.indexOf('/assets/workspace-research-session.js')<html.indexOf('/assets/workspace-research.js'),'controller loads before renderer');
assert.match(html,/\['research',researchPanel.state\(\)\]/,'pending research recovery participates in startup routing');
assert.match(html,/Draft finding from observation/,'observations can enter finding authoring');
assert.match(html,/Open question research/,'question and hint cards can enter exact research context');
assert.match(html,/async function enterResearch/);
const taskId='11111111-1111-4111-8111-111111111111';
assert.deepEqual(plain(model.researchTarget('?panel=research&project_id=p&task_id='+taskId,[{id:'p'}])),{projectId:'p',taskId});
assert.equal(model.researchTarget('?panel=research&project_id=foreign&task_id='+taskId,[{id:'p'}]),null,'task links cannot switch into unavailable project scope');
assert.equal(model.researchTarget('?panel=research&project_id=p&task_id=alias',[{id:'p'}]),null,'task aliases are never substituted for stable task UUIDs');
assert.deepEqual(plain(model.workspaceTarget('?panel=task-workflows&project_id=p&task_id='+taskId,[{id:'p'}])),{panel:'task-workflows',projectId:'p',taskId});
assert.equal(model.workspaceTarget('?panel=unknown&project_id=p&task_id='+taskId,[{id:'p'}]),null);
assert.match(html,/id="taskWorkspace"/,'selected-task workflows have a real dashboard mount');
assert.match(html,/\['task-workflows',taskPanel.state\(\)\]/,'durable task recovery is routed at startup');
assert.equal(model.matchesTaskTarget({project_id:'p',redirect:{from:taskId,to:'survivor'},context:{task:{id:'survivor',project_id:'p'}}},'p',taskId,'task-workflows'),true,'task navigation may display a validated redirect without silently changing selected identity');
assert.equal(model.matchesTaskTarget({project_id:'p',redirect:{from:'different',to:'survivor'},context:{task:{id:'survivor',project_id:'p'}}},'p',taskId,'task-workflows'),false);
assert.deepEqual(plain(model.workspaceTarget('?panel=research&project_id=p&task_id='+taskId+'&entity_type=finding&entity_id=task-finding',[{id:'p'}])),{panel:'research',projectId:'p',taskId,entity:{type:'finding',id:'task-finding'}},'task knowledge links open the exact finding record');
assert.equal(model.workspaceTarget('?panel=research&project_id=p&task_id='+taskId+'&entity_type=unknown&entity_id=x',[{id:'p'}]),null);

assert.equal(model.workspaceTarget('?panel=research&project_id=p&task_id='+taskId+'&entity_type=source&entity_id=version',[{id:'p'}]).entity.type,'source');

const exactComparison=model.compareObservations({identifier:'same',source_sha256:'a'.repeat(64),locator_key:{line:1},value:1},{identifier:'same',sha256:'b'.repeat(64),locator_key:{line:20},value:2});
assert.ok(exactComparison.some(x=>x.key==='source_sha256'&&!x.same),'compare exact source bytes');
assert.ok(exactComparison.some(x=>x.key==='locator_key'&&!x.same),'compare exact observation locations');
