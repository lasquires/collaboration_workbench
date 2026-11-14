/* Session-only bearer access for the scoped Research Workspace API. */
(function (root) {
  'use strict';
  const STORAGE_KEY = 'research-workspace.session-token.v1';
  const API_PATH = /^\/v1(?:\/|$)/;
  const TOKEN = /^[A-Za-z0-9_-]{32,256}$/;

  function allowedService(url, location) {
    try {
      const parsed = new URL(url, location.href);
      return parsed.protocol === 'https:' ||
        (parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]', '::1'].includes(parsed.hostname));
    } catch (_) { return false; }
  }
  function apiUrl(url, location) {
    try {
      const parsed = new URL(url, location.href);
      return parsed.origin === location.origin && !parsed.username && !parsed.password && API_PATH.test(parsed.pathname);
    } catch (_) { return false; }
  }
  function validToken(token) { return typeof token === 'string' && TOKEN.test(token); }

  function createAccess(env) {
    const location = env.location;
    const storage = env.storage;
    const fetcher = env.fetch;
    function getToken() {
      try { const value = storage && storage.getItem(STORAGE_KEY); return validToken(value) ? value : null; }
      catch (_) { return null; }
    }
    function options(url, options = {}) {
      const result = Object.assign({}, options);
      const headers = new Headers(options.headers || {});
      // Never carry a caller-supplied bearer to an unsafe or unrelated target.
      if (!allowedService(url, location) || !apiUrl(url, location)) {
        headers.delete('Authorization');
        result.headers = headers;
        return result;
      }
      const token = getToken();
      if (token) headers.set('Authorization', 'Bearer ' + token);
      else headers.delete('Authorization');
      result.headers = headers;
      if (token) result.redirect = 'error';
      return result;
    }
    async function mount() {
      if (!env.document || !fetcher) return;
      let info;
      try {
        if (!allowedService('/v1/service-info', location)) return;
        const response = await fetcher('/v1/service-info', { headers: { Accept: 'application/json' } });
        if (!response.ok) return;
        info = await response.json();
      } catch (_) { return; }
      if (!info || info.api_version !== 'research-workspace/v1') return;
      const shared = info.shared_mode === true;
      let host = env.document.querySelector('header') || env.document.querySelector('main') || env.document.body;
      if (!host) return;
      let panel = env.document.getElementById('workspaceAccess');
      if (!panel) {
        panel = env.document.createElement('section');
        panel.id = 'workspaceAccess';
        panel.setAttribute('aria-label', 'Workspace access');
        if (host.firstChild) host.insertBefore(panel, host.firstChild); else host.appendChild(panel);
      }
      panel.replaceChildren();
      if (!shared) return;
      const form = env.document.createElement('form');
      const label = env.document.createElement('label');
      label.textContent = 'Workspace bearer token';
      const input = env.document.createElement('input');
      input.type = 'password'; input.autocomplete = 'current-password'; input.required = true;
      input.minLength = 32; input.maxLength = 256; input.pattern = '[A-Za-z0-9_-]{32,256}';
      label.appendChild(input);
      const signIn = env.document.createElement('button'); signIn.type = 'submit'; signIn.textContent = 'Sign in';
      const status = env.document.createElement('span'); status.setAttribute('role', 'status');
      const signOut = env.document.createElement('button'); signOut.type = 'button'; signOut.textContent = 'Sign out and reload';
      const reload = env.document.createElement('button'); reload.type = 'button'; reload.textContent = 'Reload workspace';
      reload.addEventListener('click', () => env.location.reload());
      const signedIn = !!getToken();
      status.textContent = signedIn ? 'Session token is stored in this tab.' : 'Sign in to access this shared workspace.';
      form.append(label, signIn, signOut, reload, status);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        const token = input.value;
        if (!validToken(token)) { status.textContent = 'Token must be 32 to 256 base64url characters.'; return; }
        signIn.disabled = true;
        try {
          const response = await fetcher('/v1/projects', { headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' }, redirect: 'error' });
          if (!response.ok) { status.textContent = 'Sign in was not accepted.'; return; }
          await response.json();
          try { storage.setItem(STORAGE_KEY, token); }
          catch (_) { status.textContent = 'Session storage is unavailable; token was not saved.'; return; }
          input.value = '';
          status.textContent = 'Signed in. Reload workspace to refresh its data.';
        } catch (_) { status.textContent = 'Sign in could not be confirmed.'; }
        finally { signIn.disabled = false; }
      });
      signOut.addEventListener('click', () => {
        try { if (storage) storage.removeItem(STORAGE_KEY); } catch (_) { /* storage may be blocked */ }
        input.value = '';
        status.textContent = 'Signed out. Reload workspace to refresh its data.';
        env.location.reload();
      });
      panel.appendChild(form);
    }
    return { options, mount, getToken };
  }

  const api = {
    STORAGE_KEY,
    validToken,
    allowedService,
    apiUrl,
    createAccess,
    options(url, options) {
      const location = root.location || { href: 'http://localhost/', origin: 'http://localhost' };
      let storage = null;
      try { storage = root.sessionStorage; } catch (_) { /* unavailable */ }
      return createAccess({ location, storage, fetch: root.fetch, document: root.document }).options(url, options);
    },
    mount() {
      let storage = null;
      try { storage = root.sessionStorage; } catch (_) { /* unavailable */ }
      return createAccess({ location: root.location, storage, fetch: root.fetch, document: root.document }).mount();
    }
  };
  root.WorkspaceAccess = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
