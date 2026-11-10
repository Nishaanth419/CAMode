// Tests for frontend/sw.js, run in a fake service worker scope. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../frontend/sw.js'), 'utf8');
const ORIGIN = 'https://ca.example.test';

// A tiny Cache Storage: entries keyed by URL, matched with or without the query like the real one.
function fakeCaches() {
  const stores = new Map();
  const keyOf = (input) => new URL(typeof input === 'string' ? input : input.url, ORIGIN);
  const open = async (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      async addAll(urls) { for (const url of urls) store.set(keyOf(url).href, `cached ${keyOf(url).pathname}`); },
      async put(input, response) { store.set(keyOf(input).href, response.body); },
    };
  };
  const match = async (input, options = {}) => {
    const wanted = keyOf(input);
    for (const store of stores.values()) {
      for (const [href, body] of store) {
        const url = new URL(href);
        const same = options.ignoreSearch ? url.origin + url.pathname === wanted.origin + wanted.pathname : href === wanted.href;
        if (same) return { body, ok: true };
      }
    }
    return undefined;
  };
  return { stores, open, match, keys: async () => [...stores.keys()], delete: async (name) => stores.delete(name) };
}

function loadWorker({ online }) {
  const listeners = {};
  const caches = fakeCaches();
  const fetched = [];
  const context = {
    URL, Promise, Set,
    caches,
    Response: { error: () => ({ error: true }) },
    fetch: async (request) => {
      fetched.push(request.url);
      if (!online) throw new TypeError('Failed to fetch');
      return { ok: true, body: `network ${request.url}`, clone() { return { ...this }; } };
    },
    self: {
      location: { origin: ORIGIN },
      addEventListener: (type, handler) => { listeners[type] = handler; },
      skipWaiting() {}, clients: { claim() {} },
    },
  };
  vm.runInNewContext(SOURCE, context);
  const dispatch = async (type, extra = {}) => {
    let waited;
    let responded;
    listeners[type]({ ...extra, waitUntil: (promise) => { waited = promise; }, respondWith: (promise) => { responded = promise; } });
    await waited;
    return responded === undefined ? undefined : await responded;
  };
  const request = (pathAndQuery, mode = 'no-cors') => ({ url: ORIGIN + pathAndQuery, method: 'GET', mode });
  return { caches, fetched, dispatch, request };
}

test('the cache name is bumped from the version that never matched versioned assets', () => {
  const version = Number(/ca-mode-shell-v(\d+)/.exec(SOURCE)[1]);
  assert.ok(version >= 54);
});

test('offline, versioned shell assets are served from the cache', async () => {
  const worker = loadWorker({ online: false });
  await worker.dispatch('install');
  const script = await worker.dispatch('fetch', { request: worker.request('/js/app.js?v=43') });
  assert.equal(script.body, 'cached /js/app.js');
  const css = await worker.dispatch('fetch', { request: worker.request('/css/app.css?v=49') });
  assert.equal(css.body, 'cached /css/app.css');
  const page = await worker.dispatch('fetch', { request: worker.request('/', 'navigate') });
  assert.equal(page.body, 'cached /');
});

test('online, shell assets come from the network and refresh the cached copy', async () => {
  const worker = loadWorker({ online: true });
  await worker.dispatch('install');
  const script = await worker.dispatch('fetch', { request: worker.request('/js/ui.js?v=2') });
  assert.equal(script.body, `network ${ORIGIN}/js/ui.js?v=2`);
  await new Promise((resolve) => setImmediate(resolve));
  const [store] = worker.caches.stores.values();
  assert.equal(store.get(`${ORIGIN}/js/ui.js`), `network ${ORIGIN}/js/ui.js?v=2`);
});

test('API, auth and other files are never answered from the cache', async () => {
  const worker = loadWorker({ online: false });
  await worker.dispatch('install');
  assert.equal(await worker.dispatch('fetch', { request: worker.request('/api/session') }), undefined);
  assert.equal(await worker.dispatch('fetch', { request: worker.request('/auth/indmoney/connect') }), undefined);
  assert.equal(await worker.dispatch('fetch', { request: worker.request('/brokers/zerodha.png') }), undefined);
  assert.equal(await worker.dispatch('fetch', { request: { url: 'https://cdn.plot.ly/plotly.js', method: 'GET', mode: 'no-cors' } }), undefined);
});
