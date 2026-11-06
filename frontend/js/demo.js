/* ── Demo mode ────────────────────────────────────────────────────────
   Open the app with ?demo to try it on a made-up investor without signing in.
   The API is answered in this browser from /demo/data.json (recorded from the
   real server by scripts/build_demo_data.py), so nothing reaches a server and
   nothing is saved. New chats live only in this tab and get a fixed reply.
   Loaded before the other scripts so their first requests are already answered here. */
(() => {
  const FLAG = 'ca-mode-demo';
  const store = (() => { try { return window.sessionStorage; } catch { return null; } })();
  const params = new URLSearchParams(window.location.search);
  if (params.has('demo')) {
    store?.setItem(FLAG, '1');
    params.delete('demo');
    const query = params.toString();
    window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : '') + window.location.hash);
  } else if (store?.getItem(FLAG) !== '1') {
    return;
  }

  const NOT_SAVED = "This is a demo with a made-up portfolio, so changes aren't saved. Sign in to CA Mode to use your own.";
  const REPLY = [
    'This demo shows answers recorded earlier on a made-up portfolio, so it does not answer new questions.',
    '',
    'Open the saved chats in the sidebar to see full answers and the trace behind each one, or sign in to CA Mode with Google to ask your own questions.',
  ].join('\n');

  const realFetch = window.fetch.bind(window);
  const recorded = realFetch('/demo/data.json').then((response) => response.json());
  // Chats changed in the demo (new ones, and recorded ones with a follow-up) and recorded ones deleted:
  // kept in memory only, so a reload starts fresh.
  const changed = new Map();
  const removed = new Set();
  const summary = ({ messages, ...chat }) => chat;

  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  function exitDemo() {
    store?.removeItem(FLAG);
    window.location.assign('/');
  }

  // A recorded GET; trend ranges fall back to the nearest longer one, list limits to any recorded limit.
  function replay(data, path, search) {
    const exact = data[`GET ${path}${search}`];
    if (exact) return json(exact.body, exact.status);
    const query = new URLSearchParams(search);
    if (path === '/api/snapshots') {
      const wanted = Number(query.get('days')) || 365;
      const ranges = Object.keys(data).filter((key) => key.startsWith('GET /api/snapshots?days='))
        .map((key) => Number(key.split('=')[1])).sort((a, b) => a - b);
      const days = ranges.find((range) => range >= wanted) || ranges[ranges.length - 1];
      if (days) return json(data[`GET /api/snapshots?days=${days}`].body);
    }
    const sameRoute = Object.keys(data).find((key) => key.startsWith(`GET ${path}?`));
    if (sameRoute) return json(data[sameRoute].body, data[sameRoute].status);
    return json({ error: 'Not available in the demo.' }, 404);
  }

  async function answer(url, init) {
    const method = (init?.method || 'GET').toUpperCase();
    const path = url.pathname;
    const data = await recorded;
    const body = (() => { try { return JSON.parse(init?.body || '{}'); } catch { return {}; } })();
    const chatId = path.startsWith('/api/chats/') ? decodeURIComponent(path.slice('/api/chats/'.length)) : null;

    if (method === 'GET') {
      if (path === '/api/chats') {
        const saved = (data['GET /api/chats']?.body.chats || []).filter((chat) => !removed.has(chat.id));
        const all = [...changed.values()].map(summary).concat(saved.filter((chat) => !changed.has(chat.id)));
        return json({ chats: all.sort((a, b) => b.updatedAt - a.updatedAt) });
      }
      if (chatId && changed.has(chatId)) return json(changed.get(chatId));
      if (chatId && removed.has(chatId)) return json({ error: 'Chat not found in this account.' }, 404);
      return replay(data, path, url.search);
    }
    if (path === '/api/chats' && method === 'POST') {
      const chat = { id: `demo-${Date.now()}`, title: 'New chat', agentType: body.agentType || 'finance', updatedAt: Date.now(), messages: [] };
      changed.set(chat.id, chat);
      return json(chat, 201);
    }
    if (chatId && method === 'DELETE') {
      changed.delete(chatId);
      removed.add(chatId);
      return json({ ok: true });
    }
    if (path === '/api/chat' && method === 'POST') {
      const recordedChat = data[`GET /api/chats/${body.chatId}`]?.body;
      const chat = changed.get(body.chatId) || (recordedChat && structuredClone(recordedChat));
      if (chat) {
        if (chat.title === 'New chat') chat.title = String(body.message || '').slice(0, 60);
        chat.messages.push({ role: 'user', content: body.message }, { role: 'assistant', content: REPLY, trace: [] });
        chat.updatedAt = Date.now();
        changed.set(chat.id, chat);
      }
      return new Response(REPLY, { headers: { 'Content-Type': 'text/plain; charset=utf-8', 'X-Agent-Trace': '[]' } });
    }
    if (path === '/api/page-views') return json({ ok: true });
    if (path === '/api/session/logout') {
      exitDemo();
      return json({ ok: true });
    }
    return json({ error: NOT_SAVED }, 403);
  }

  window.fetch = (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, window.location.href);
    if (url.origin !== window.location.origin || !/^\/(api|auth)\//.test(url.pathname)) return realFetch(input, init);
    return answer(url, init);
  };

  document.addEventListener('DOMContentLoaded', () => {
    document.documentElement.classList.add('is-demo');
    const bar = document.createElement('div');
    bar.className = 'demo-bar';
    bar.setAttribute('role', 'note');
    bar.innerHTML = '<span><strong>Demo</strong> · a made-up portfolio, nothing is saved</span>'
      + '<a href="/" target="_blank" rel="noopener">Sign in to use yours</a>'
      + '<button type="button">Exit demo</button>';
    bar.querySelector('button').addEventListener('click', exitDemo);
    document.body.prepend(bar);
  });

  // Connecting INDmoney and switching profile leave the page, so the demo answers them here instead.
  document.addEventListener('click', (event) => {
    const target = event.target.closest('#switch-profile, [data-connect]');
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (target.id === 'switch-profile') exitDemo();
    else window.alert(NOT_SAVED);
  }, true);
})();
