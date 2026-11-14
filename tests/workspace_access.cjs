'use strict';
const assert = require('node:assert/strict');
const WorkspaceAccess = require('../docs/workspace-access.js');

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
    values
  };
}

function fakeDocument() {
  const elements = new Map();
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.value = ''; }
    setAttribute(key, value) { this[key] = value; }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.children.push(item); if (item.id) elements.set(item.id, item); return item; }
    replaceChildren(...items) { this.children = items; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    insertBefore(item) { return this.appendChild(item); }
  }
  const header = new Element('header');
  return {
    header,
    querySelector: selector => selector === 'header' ? header : null,
    getElementById: id => elements.get(id) || null,
    createElement: tag => new Element(tag),
  };
}

async function main() {
  assert.equal(WorkspaceAccess.validToken('A'.repeat(32)), true);
  assert.equal(WorkspaceAccess.validToken('A'.repeat(256)), true);
  assert.equal(WorkspaceAccess.validToken('A'.repeat(31)), false);
  assert.equal(WorkspaceAccess.validToken('A'.repeat(257)), false);
  assert.equal(WorkspaceAccess.validToken('not+a/token' + 'A'.repeat(32)), false);

  const location = { href: 'https://workspace.example/dashboard', origin: 'https://workspace.example' };
  const store = storage({ [WorkspaceAccess.STORAGE_KEY]: 'Q'.repeat(40) });
  const access = WorkspaceAccess.createAccess({ location, storage: store, fetch: async () => {}, document: null });
  const api = access.options('/v1/projects', { headers: { Accept: 'application/json' } });
  assert.equal(api.headers.get('authorization'), 'Bearer ' + 'Q'.repeat(40));
  assert.equal(api.redirect, 'error');
  assert.equal(api.headers.get('accept'), 'application/json');

  for (const url of ['https://attacker.example/v1/projects', '/elsewhere', '/api/projects', 'http://workspace.example/v1/projects', 'https://u:p@workspace.example/v1/projects']) {
    const wrapped = access.options(url, { headers: { Authorization: 'Bearer caller-secret' } });
    assert.equal(wrapped.headers.has('authorization'), false, `authorization leaked for ${url}`);
  }
  assert.equal(WorkspaceAccess.allowedService('http://127.0.0.1/v1/projects', { href: 'http://127.0.0.1/', origin: 'http://127.0.0.1' }), true);
  assert.equal(WorkspaceAccess.allowedService('http://remote.example/v1/projects', { href: 'http://remote.example/', origin: 'http://remote.example' }), false);
  assert.equal(WorkspaceAccess.allowedService('https://remote.example/v1/projects', { href: 'https://remote.example/', origin: 'https://remote.example' }), true);

  const noStorage = WorkspaceAccess.createAccess({ location, storage: { getItem() { throw Error('blocked'); } }, fetch: async () => {}, document: null });
  assert.equal(noStorage.options('/v1/projects', { headers: { Authorization: 'Bearer old' } }).headers.has('authorization'), false);
  assert.equal(noStorage.getToken(), null);

  const doc = fakeDocument();
  const uiStore = storage();
  let reloads = 0;
  const uiLocation = { ...location, reload() { reloads++; } };
  const calls = [];
  const ui = WorkspaceAccess.createAccess({
    location: uiLocation, storage: uiStore, document: doc,
    fetch: async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => url === '/v1/service-info'
        ? { api_version: 'research-workspace/v1', shared_mode: true } : { items: [] } };
    }
  });
  await ui.mount();
  const panel = doc.getElementById('workspaceAccess');
  assert.ok(panel);
  const form = panel.children[0];
  const [label, signIn, signOut, reload, status] = form.children;
  const input = label.children[0];
  assert.equal(reload.textContent, 'Reload workspace');
  assert.equal(signOut.textContent, 'Sign out and reload');
  input.value = 'R'.repeat(40);
  await form.listeners.submit({ preventDefault() {} });
  assert.equal(uiStore.getItem(WorkspaceAccess.STORAGE_KEY), 'R'.repeat(40));
  assert.equal(calls[1].url, '/v1/projects');
  assert.equal(calls[1].opts.headers.Authorization, 'Bearer ' + 'R'.repeat(40));
  assert.equal(calls[1].opts.redirect, 'error');
  assert.match(status.textContent, /Signed in/);
  reload.listeners.click();
  assert.equal(reloads, 1);
  signOut.listeners.click();
  assert.equal(uiStore.getItem(WorkspaceAccess.STORAGE_KEY), null);
  assert.equal(reloads, 2);

  const localDoc = fakeDocument();
  await WorkspaceAccess.createAccess({ location, storage: uiStore, document: localDoc,
    fetch: async () => ({ ok: true, json: async () => ({ api_version: 'research-workspace/v1', shared_mode: false }) })
  }).mount();
  assert.deepEqual(localDoc.getElementById('workspaceAccess').children, []);
  console.log('workspace access checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
