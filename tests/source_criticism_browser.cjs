'use strict';
// Real DOM + authenticated production HTTP. Optional dev dependency: Playwright.
// WORKBENCH_BROWSER_CHANNEL=msedge uses an installed Edge instead of bundled Chromium.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {resolve} = require('node:path');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');
const py = process.env.WORKBENCH_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const child = spawn(py, ['-B', '-X', 'utf8', 'tests/source_http_fixture.py'], {
  cwd: resolve(__dirname, '..'), stdio: ['pipe', 'pipe', 'inherit']
});
const lines = createInterface({input: child.stdout});
const config = new Promise((ok, fail) => {
  const timer = setTimeout(() => fail(Error('Source fixture startup timed out')), 15000);
  lines.once('line', line => {clearTimeout(timer); try {ok(JSON.parse(line));} catch(e) {fail(e);}});
  child.once('error', fail);
  child.once('exit', code => {clearTimeout(timer); fail(Error('Source fixture exited: ' + code));});
});
let browser;
async function main() {
  const c = await config;
  browser = await chromium.launch({headless: true, ...(process.env.WORKBENCH_BROWSER_CHANNEL ? {channel: process.env.WORKBENCH_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  // Avoid printing secrets even on assertion failure.
  await context.addInitScript(token => sessionStorage.setItem('research-workspace.session-token.v1', token), c.token);
  page.on('dialog', dialog => dialog.accept());
  const research = page.locator('#researchWorkspace');
  const detail = research.getByRole('region', {name: 'Selected research context'});
  const editor = research.getByRole('region', {name: 'Research authoring'});
  const records = research.getByRole('region', {name: 'Research records'});
  const click = label => research.getByRole('button', {name: label, exact: true}).click();
  const field = (label, value) => editor.getByLabel(label, {exact: true}).fill(value);
  const waitText = (locator, text) => locator.getByText(text, {exact: true}).first().waitFor();
  async function api(path, body) {
    const r = await context.request.fetch(c.base + path, {method: body ? 'POST' : 'GET',
      headers: {Authorization: 'Bearer ' + c.token}, ...(body ? {data: body} : {})});
    return {status: r.status(), data: await r.json()};
  }
  const get = async path => {const r = await api(path); assert.equal(r.status, 200); return r.data;};
  const query = '?project_id=' + c.project;
  const sourceUrl = id => c.base + '/?project_id=' + c.project + '&task_id=' + c.task + '&panel=research&entity_type=source&entity_id=' + id;
  async function openSource(id) {
    await page.goto(sourceUrl(id));
    await waitText(detail, id);
  }
  const bodies = [];
  let loseNext = false;
  await page.route('**/v1/transactions', async route => {
    bodies.push(route.request().postData());
    const response = await route.fetch();
    if (loseNext) {
      loseNext = false;
      await route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"Synthetic lost response after commit"}'});
    } else await route.fulfill({response});
  });

  // Conflict comparison and observation -> exact source navigation use the shipped dashboard.
  await page.goto(c.base);
  await page.locator('#identity').filter({hasText:'Authenticated'}).waitFor();
  await page.getByRole('button', {name:'Evidence', exact:true}).click();
  await page.locator('#observations input').nth(1).waitFor();
  await page.locator('#observations input').nth(0).check();
  await page.locator('#observations input').nth(1).check();
  const comparison = await page.locator('#compare').innerText();
  for (const text of ['build-a', 'build-b', 'source_sha256', 'locator_key', 'chooses no winner', '2', '3']) assert.ok(comparison.includes(text), text);
  const observations = (await get('/v1/observations' + query)).items;
  for (const row of observations) assert.ok(comparison.includes(row.source_sha256), 'Comparison pins both hashes');
  await page.locator('#observations article').first().getByRole('button', {name: 'Open exact source'}).click();
  await detail.getByText('Source version', {exact: true}).waitFor();
  assert.equal(bodies.length, 0, 'Navigation and comparison never submit');

  async function derive(childName, claim, run='run-build-a') {
    await openSource(c.versions[childName]);
    await click('Record derivation assertion');
    await field('Claim', claim);
    await field('Uncertainty', 'Still unexplained: conflicting build B; no runtime proof.');
    await click('Browse parent source versions');
    const parent = records.locator('article').filter({hasText: c.versions['build-a']});
    await parent.getByRole('button', {name: 'Use as parent source'}).click();
    for (let i=1; i<=2; i++) {
      assert.equal(await editor.getByLabel('Observation method ' + i, {exact:true}).inputValue(), '', 'Methods remain authored');
      await field('Source locator ' + i, 'line 1 <literal>');
      await field('Observation method ' + i, 'Read pinned synthetic source');
      await field('Source limitations ' + i, 'Synthetic fixture only; not independent evidence');
    }
    await click('Browse published origin runs');
    await records.locator('article').filter({hasText: run}).getByRole('button', {name: 'Use recorded origin run'}).click();
    const before = bodies.length;
    // Collection browsing must preserve the authored claim and exact child binding.
    assert.equal(await editor.getByLabel('Claim', {exact:true}).inputValue(), claim);
    await click('Save reviewed action');
    if (run === 'run-build-b') {
      await page.locator('#notice').filter({hasText: /parent|input/i}).waitFor();
      assert.equal(bodies.length, before+1);
      assert.equal(await editor.getByLabel('Claim', {exact:true}).inputValue(), claim);
      await click('Discard draft');
      return null;
    }
    await waitText(detail, claim);
    const op = JSON.parse(bodies.at(-1)).operations[0];
    assert.equal(op.scope.artifact_version_id, c.versions[childName]);
    assert.equal(op.scope.derivation.parent_version_id, c.versions['build-a']);
    assert.equal(op.scope.derivation.index_run_id, run);
    assert.equal(op.evidence.length, 2);
    await waitText(detail, 'Still unexplained: conflicting build B; no runtime proof.');
    return op.id;
  }
  await derive('report-a', 'Rejected wrong-input run', 'run-build-b');
  const first = await derive('report-a', 'Report <one> derives from build A');
  const second = await derive('report-b', 'Report two shares the same recorded run');
  assert.equal((await get('/v1/findings' + query)).items.length, 2, 'Wrong-input rejection creates no finding');
  await openSource(c.versions['build-a']);
  await click('Read derivations');
  await waitText(records, 'Shared recorded origin within this page: run-build-a (2 assertions)');
  assert.equal(await records.getByText('Current assertion', {exact:true}).count(), 2);
  assert.ok((await records.innerText()).includes('matching hashes alone establish neither dependence nor independence'));
  await records.locator('article').filter({hasText: 'Report <one>'}).getByRole('button', {name:'Open assertion finding'}).click();
  await waitText(detail, 'Report <one> derives from build A');
  await click('Revise finding');
  await field('Claim', 'Report one interpretation narrowed');
  await field('Rationale', 'Preserve the earlier interpretation in history');
  await click('Save reviewed action');
  await waitText(detail, 'Report one interpretation narrowed');
  await click('Archive finding');
  await field('Rationale', 'Retain historical source assertion');
  await click('Save reviewed action');
  await research.getByRole('button', {name:'Restore finding', exact:true}).waitFor();
  const f = (await get('/v1/findings/' + first + '/context' + query)).context.entity;
  const links = (await get('/v1/evidence-links' + query + '&subject_id=' + f.evidence_origin_id)).items;
  const rev = (await get('/v1/artifact-versions/' + c.versions['build-a'] + '/context' + query)).revision;
  const retract = await api('/v1/transactions', {project_id:c.project, actor:{principal_id:c.principal}, expected_revision:rev,
    request_id:require('node:crypto').randomUUID(), operations:[{op:'set_evidence_state', evidence_link_id:links[0].id, expected_state:'available', state:'retracted', reason:'Synthetic source limitation'}]});
  assert.equal(retract.status,200);
  await openSource(c.versions['build-a']); await click('Read derivations');
  await waitText(records, 'Historical assertion'); await waitText(records, 'Archived finding');
  assert.ok((await records.innerText()).includes('Retracted citations: 1'));

  // Literal authored search, draft navigation, phone layout and lost-response retry.
  await openSource(c.versions['build-b']); await click('Record scoped search');
  const literal = 'missing <member> & "quoted"';
  await field('Search query', literal); await field('Search method', 'Literal scan');
  await field('Searched range', 'Line 1 only');
  await editor.getByLabel('Search coverage', {exact:true}).selectOption('partial');
  await field('Search outcome', 'Not found in line 1; other ranges remain unknown.');
  await click('artifact versions');
  assert.equal(await editor.getByLabel('Search query', {exact:true}).inputValue(), literal);
  await page.getByRole('button', {name:'Evidence', exact:true}).click();
  assert.ok(await research.isVisible(), 'Unsaved source draft guards panel navigation');
  await page.setViewportSize({width:390,height:844});
  const widths = await page.evaluate(() => ({viewport:innerWidth, body:document.body.scrollWidth, document:document.documentElement.scrollWidth,
    fields:[...document.querySelectorAll('#researchWorkspace textarea, #researchWorkspace select')].map(n=>n.getBoundingClientRect().width)}));
  assert.ok(widths.document <= widths.viewport && widths.body <= widths.viewport, JSON.stringify(widths));
  assert.ok(widths.fields.every(n=>n>150 && n<=390), JSON.stringify(widths));
  loseNext = true;
  await click('Save reviewed action');
  await research.getByRole('button', {name:'Retry exact request',exact:true}).waitFor();
  const frozen = bodies.at(-1), count = bodies.length;
  await page.reload();
  await research.getByRole('button', {name:'Retry exact request',exact:true}).waitFor();
  assert.equal(bodies.length,count,'Reload never submits a recovered request');
  await click('Retry exact request');
  await editor.locator('form').waitFor({state:'detached'});
  assert.equal(bodies.at(-1),frozen,'Exact retry retains byte-identical body and request ID');
  await click('Read negative_searches');
  await waitText(records,literal);
  const searches = await get('/v1/negative-searches' + query + '&subject_id=' + c.versions['build-b']);
  assert.equal(searches.items.length,1); assert.equal(searches.items[0].query,literal); assert.equal(searches.items[0].coverage.complete,false);
  assert.equal((await get('/v1/negative-searches' + query + '&subject_id=' + c.versions['build-a'])).items.length,0);

  // Entry points from the pinned catalog, scoped finding and its citation.
  await click('artifact versions');
  await records.locator('article').filter({hasText:c.versions['report-b']}).getByRole('button',{name:'Open source',exact:true}).click();
  await waitText(detail,c.versions['report-b']);
  await click('Read derivations');
  await records.locator('article').filter({hasText:'Report two shares'}).getByRole('button',{name:'Open assertion finding'}).click();
  await waitText(detail,'Report two shares the same recorded run');
  await detail.getByRole('button',{name:'Open cited source',exact:true}).click();
  await waitText(detail,c.versions['report-b']);
  await click('Read derivations');
  await records.locator('article').filter({hasText:'Report two shares'}).getByRole('button',{name:'Open assertion finding'}).click();
  await click('Read evidence');
  const citation = records.locator('article').filter({hasText:c.versions['build-a']});
  await citation.getByRole('button',{name:'Open cited source',exact:true}).click();
  await waitText(detail,c.versions['build-a']);
  await click('Read observations');
  await waitText(records,'speed');
  await records.getByRole('button',{name:'Open cited source',exact:true}).click();
  await waitText(detail,c.versions['build-a']);

  // A proven stale rejection preserves fields but requires a fresh exact-source read
  // and explicit adoption; a catalog refresh alone cannot authorize resubmission.
  await click('Record scoped search');
  await field('Search query','stale draft <literal>');
  await field('Search method','Literal scan'); await field('Searched range','Line 1');
  await editor.getByLabel('Search coverage',{exact:true}).selectOption('partial');
  await field('Search outcome','Not found within this range only');
  const latest = await get('/v1/artifact-versions/' + c.versions['build-a'] + '/context' + query);
  const concurrent = await api('/v1/transactions',{project_id:c.project,actor:{principal_id:c.principal},expected_revision:latest.revision,
    request_id:require('node:crypto').randomUUID(),operations:[{op:'record_negative_search',id:'concurrent-search',artifact_version_id:c.versions['build-b'],
      query:'Other search',method:'Synthetic scan',searched_range:'Line 1',coverage:{complete:false},outcome:'Unknown outside this range'}]});
  assert.equal(concurrent.status,200);
  await click('Save reviewed action');
  await research.getByRole('button',{name:'Read latest context',exact:true}).waitFor();
  assert.equal(await editor.getByLabel('Search query',{exact:true}).inputValue(),'stale draft <literal>');
  assert.equal(await editor.getByRole('button',{name:'Save reviewed action'}).isDisabled(),true);
  const staleBody=JSON.parse(bodies.at(-1)), staleCount=bodies.length;
  await click('Read latest context');
  assert.equal(bodies.length,staleCount,'Fresh context read never submits');
  await click('Adopt explicitly reviewed choices');
  assert.equal(bodies.length,staleCount,'Explicit adoption does not itself publish');
  await click('Save reviewed action');
  await editor.locator('form').waitFor({state:'detached'});
  const reconciled=JSON.parse(bodies.at(-1));
  assert.notEqual(reconciled.request_id,staleBody.request_id);
  assert.deepEqual(reconciled.operations,staleBody.operations,'Reconciliation retains exact source, search ID and authored operation');
  assert.equal((await get('/v1/negative-searches'+query+'&subject_id='+c.versions['build-a'])).items.length,1);

  // Onboard two previously unconfigured repositories through the actual UI.
  // Owner-only setup remains separate from the contributor research session.
  const ownerContext = await browser.newContext({viewport:{width:1440,height:1000}});
  await ownerContext.addInitScript(token=>sessionStorage.setItem('research-workspace.session-token.v1',token),c.owner_token);
  const ownerPage = await ownerContext.newPage();
  ownerPage.on('pageerror',e=>errors.push(e.message));
  await ownerPage.goto(c.base);
  await ownerPage.locator('#identity').filter({hasText:'Authenticated'}).waitFor();
  const projectIds=[];
  for(const [index,root] of c.repositories.entries()) {
    if(index) await ownerPage.getByRole('button',{name:'Repositories',exact:true}).click();
    await ownerPage.getByLabel('Repository root path').fill(root);
    await ownerPage.getByLabel('Project name, optional').fill(index?'Beta project':'Alpha project');
    const previewResponse=ownerPage.waitForResponse(r=>r.url().endsWith('/v1/repository-setup'));
    await ownerPage.getByRole('button',{name:'Preview repository connection',exact:true}).click();
    const previewResult=await previewResponse;
    assert.equal(previewResult.status(),200,'Repository preview: '+(await previewResult.json()).error);
    const applyResponse=ownerPage.waitForResponse(r=>r.url().endsWith('/v1/repository-setup'));
    await ownerPage.getByRole('button',{name:'Apply repository connection',exact:true}).click();
    const applyResult=await applyResponse;
    assert.equal(applyResult.status(),200,'Repository apply: '+(await applyResult.json()).error);
    await ownerPage.locator('#notice').filter({hasText:'Repository connected.'}).waitFor();
    projectIds.push(await ownerPage.locator('#projectSelect').inputValue());
    const refreshResponse=ownerPage.waitForResponse(r=>r.url().endsWith('/v1/repository-refresh'));
    await ownerPage.getByRole('button',{name:'Check Git changes now',exact:true}).click();
    const refreshed=await refreshResponse;
    assert.equal(refreshed.status(),200,'Source refresh: '+(await refreshed.json()).error);
    const sourceState=await ownerContext.request.get(c.base+'/v1/repositories?project_id='+projectIds[index],{headers:{Authorization:'Bearer '+c.owner_token}});
    const repositoryState=(await sourceState.json()).items[0];
    assert.ok(['current','partial'].includes(repositoryState.evidence_state),'Pinned evidence: '+repositoryState.evidence_state+' '+repositoryState.evidence_error);
    await ownerPage.getByRole('button',{name:'Evidence',exact:true}).click();
    await ownerPage.locator('#observations').getByText(new RegExp(index?'beta_only':'alpha_only')).first().waitFor();
    assert.ok(!(await ownerPage.locator('#observations').innerText()).includes(index?'alpha_only':'beta_only'),'Project evidence stays isolated');
    const fs=require('node:fs');
    const manifest=fs.readFileSync(resolve(root,'.research-workspace/project.json'),'utf8');
    assert.equal(JSON.parse(manifest).project_id,projectIds[index]);
    assert.ok(!manifest.includes(root),'Portable manifest excludes local root');
    assert.ok(fs.existsSync(resolve(root,'.research-workspace/AGENTS.md')));
    assert.ok(fs.existsSync(resolve(root,'.research-workspace/schema.json')));
    assert.ok(!fs.readdirSync(root).some(name=>name.endsWith('.sqlite3')),'Authority DB remains outside source project');
  }
  assert.notEqual(projectIds[0],projectIds[1]);
  await ownerPage.locator('#projectSelect').selectOption(projectIds[0]);
  await ownerPage.locator('#observations').getByText(/alpha_only/).first().waitFor();
  assert.ok(!(await ownerPage.locator('#observations').innerText()).includes('beta_only'));
  // A contributor cannot turn local repository paths into setup authority.
  assert.equal((await api('/v1/repository-setup',{root:c.repositories[0],apply:false})).status,403);
  await ownerContext.close();
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('authenticated browser acceptance passed: conflicts, all exact-source entry points, authored derivations, shared run, history/archive/withdrawal, bounded search, mobile, draft guard, lost-response exact retry, explicit stale reconciliation, two-repository onboarding and project isolation');
}
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  child.stdin.end('\n'); lines.close();
});
