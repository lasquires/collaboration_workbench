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
  assert.match(html, /textContent=esc\(/, 'untrusted API strings are assigned as text, not HTML');
  for (const endpoint of ['/v1/projects', '/v1/observations', '/v1/findings', '/v1/questions', '/v1/hints', '/v1/inbox', '/v1/documents', '/v1/discussions', '/v1/proposals', '/v1/feedback'])
    assert.ok(html.includes(endpoint) || html.includes(`'${endpoint.slice(4)}'`), `panel/API resource is wired: ${endpoint}`);
  assert.match(html, /name="identifier"/);
  assert.match(html, /name="namespace"/);
  assert.match(html, /name="scope_key"/);
  console.log('research workspace view checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
