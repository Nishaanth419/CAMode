/* CA Mode: pure helpers for the chat and the profile dialog (loaded before app.js, exported for Node tests). */
(function (root) {
  'use strict';

  // Shown instead of raw server or browser error text (an exception message, a proxy's HTML page).
  const NETWORK_ERROR = 'Could not reach CA Mode. Check your connection and try again.';
  const GENERIC_ERROR = 'Something went wrong. Try again in a moment.';
  const STATUS_ERRORS = {
    400: 'CA Mode could not use that request. Check what you entered and try again.',
    401: 'Your session ended. Sign in again to continue.',
    403: 'You do not have access to that. Sign in again or unlock your history.',
    404: 'That could not be found. It may have been deleted.',
    408: 'The request took too long. Try again.',
    413: 'That is too large to send.',
    429: 'Too many requests right now. Wait a moment and try again.',
  };

  // An error body CA Mode wrote itself: JSON with a plain `error` sentence meant for the user.
  function apiError(body) {
    return body && typeof body === 'object' && typeof body.error === 'string' && body.error.trim() ? body.error.trim() : '';
  }

  // The message to show for a failed request: CA Mode's own error sentence, else a plain one for the status.
  function plainError(status, body) {
    const own = apiError(body);
    if (own) return own;
    if (!status) return NETWORK_ERROR;
    if (STATUS_ERRORS[status]) return STATUS_ERRORS[status];
    if (status >= 500) return 'CA Mode could not finish that. Try again in a moment.';
    return GENERIC_ERROR;
  }

  // The text to show for anything a request threw. CA Mode's own code throws plain `Error`s with sentences written
  // for the user; a failed fetch is a TypeError ("Failed to fetch", "Load failed"), and anything else (a JSON
  // SyntaxError, a bug) is internal text the user should not see.
  function errorText(error) {
    if (!error) return GENERIC_ERROR;
    if (error.name === 'TypeError' || error.name === 'AbortError') return NETWORK_ERROR;
    if (error.name === 'Error' && typeof error.message === 'string' && error.message.trim()) return error.message;
    return GENERIC_ERROR;
  }

  const TIMEOUT_STATUSES = new Set([408, 502, 503, 504, 520, 521, 522, 523, 524]);

  // A chat request that died before CA Mode answered: the connection dropped (status 0), or a proxy in front
  // (Cloudflare, Caddy) gave up waiting and sent its own page. The server may still finish and save the answer.
  function lostChatResponse(status, body) {
    if (!status) return true;
    return TIMEOUT_STATUSES.has(status) && !apiError(body);
  }

  // The saved answer to `question` in `chat` (GET /api/chats/{id}) when the chat changed after `sinceUpdatedAt`
  // (server time, from the chat list) and ends with that question and its answer; otherwise null.
  function savedAnswerFor(chat, question, sinceUpdatedAt) {
    if (!chat || !Array.isArray(chat.messages) || chat.messages.length < 2) return null;
    if (Number.isFinite(sinceUpdatedAt) && !(Number(chat.updatedAt) > sinceUpdatedAt)) return null;
    const [asked, answer] = chat.messages.slice(-2);
    if (!asked || asked.role !== 'user' || !answer || answer.role !== 'assistant') return null;
    if (String(asked.content || '').trim() !== String(question || '').slice(0, 12000).trim()) return null;
    return answer;
  }

  // Enter that finishes an IME composition (Japanese, Chinese, Korean, Indic keyboards) must not send.
  function isComposingEnter(event) {
    return Boolean(event && (event.isComposing || event.keyCode === 229));
  }

  function parseChartSpec(json) {
    try {
      // Normalize HTML-encoded spaces/quotes that occasionally appear in model output.
      const normalized = String(json)
        .replace(/&#x20;|&#32;/gi, ' ')
        .replace(/&quot;/gi, '"')
        .replace(/&amp;/gi, '&');
      const spec = JSON.parse(normalized);
      return spec && (
        (['bar', 'donut'].includes(spec.type) && Array.isArray(spec.data)) ||
        (spec.type === 'line' && Array.isArray(spec.series)) ||
        (spec.type === 'candlestick' && (Array.isArray(spec.candles) || Array.isArray(spec.ohlc)))
      ) ? spec : null;
    } catch {
      return null;
    }
  }

  // Only fenced ```chart blocks are drawn: the server grounds those in tool data (charts.ground_tool_charts,
  // charts.ground_candlestick_charts), so JSON written in bold or inline stays text and never draws figures
  // the model made up. The pattern is the server's `_CHART_BLOCK`, so a block it skips is never drawn.
  // Each block becomes a \x00CHART<n>\x00 placeholder; `charts[n]` is null when unreadable.
  function extractChartBlocks(text) {
    const charts = [];
    const rest = String(text).replace(/```chart[ \t]*\n([\s\S]*?)```/gi, (_, json) => {
      const idx = charts.push(parseChartSpec(json)) - 1;
      return `\x00CHART${idx}\x00`;
    });
    return { text: rest, charts };
  }

  // Run `action` with `button` disabled, so a second click cannot repeat it, and show any failure in `statusEl`
  // (plain text, see errorText). Returns the action's result, or undefined when it failed or was already running.
  async function withPending(button, statusEl, action) {
    if (button && button.disabled) return undefined;
    if (button) {
      button.disabled = true;
      button.setAttribute?.('aria-busy', 'true');
    }
    try {
      return await action();
    } catch (error) {
      if (statusEl) statusEl.textContent = errorText(error);
      return undefined;
    } finally {
      if (button) {
        button.disabled = false;
        button.removeAttribute?.('aria-busy');
      }
    }
  }

  // Parse a server-sent event stream pushed in arbitrary chunks. Calls onEvent(name, data) per complete event with
  // JSON data; comments and non-JSON events are skipped. end() is true when an unfinished event was dropped.
  function createSseParser(onEvent) {
    let buffer = '';
    let heldCr = false; // a trailing CR may be the first half of CRLF, so it waits for the next chunk
    function dispatch(block) {
      let name = 'message';
      const data = [];
      for (const line of block.split('\n')) {
        if (!line || line.startsWith(':')) continue;
        const at = line.indexOf(':');
        const field = at < 0 ? line : line.slice(0, at);
        let value = at < 0 ? '' : line.slice(at + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'event') name = value;
        else if (field === 'data') data.push(value);
      }
      if (!data.length) return;
      let parsed;
      try { parsed = JSON.parse(data.join('\n')); } catch { return; }
      onEvent(name, parsed);
    }
    return {
      push(text) {
        let chunk = (heldCr ? '\r' : '') + String(text);
        heldCr = chunk.endsWith('\r');
        if (heldCr) chunk = chunk.slice(0, -1);
        buffer += chunk.replace(/\r\n?/g, '\n');
        let at;
        while ((at = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, at);
          buffer = buffer.slice(at + 2);
          dispatch(block);
        }
      },
      end() {
        const partial = buffer.split('\n').some((line) => line.trim() !== '' && !line.startsWith(':'));
        buffer = '';
        heldCr = false;
        return partial;
      },
    };
  }

  // Read a fetch response body (a ReadableStream of bytes) as server-sent events, calling onEvent(name, data) for
  // each. Resolves true when the stream ended inside an unfinished event; rejects when a read fails.
  async function readEventStream(body, onEvent) {
    const parser = createSseParser(onEvent);
    const decoder = new TextDecoder();
    const reader = body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    return parser.end();
  }

  // Copy of every in-app confirmation (app.js confirmAction() shows it). Text only: it goes in with textContent.
  const CONFIRMS = Object.freeze({
    deleteStatement: Object.freeze({ title: 'Delete this statement?', body: 'Its holdings are removed from CA Mode.', confirmLabel: 'Delete statement', danger: true }),
    privateMode: Object.freeze({ title: 'Switch to private mode?', body: 'Private mode deletes the chats and portfolio snapshots saved for this profile.', confirmLabel: 'Switch to private mode', danger: true }),
    newRecoveryCode: Object.freeze({ title: 'Create a new recovery code?', body: 'Your current recovery code will stop working.', confirmLabel: 'Create new code', danger: false }),
    clearHistory: Object.freeze({ title: 'Delete chats and snapshots?', body: 'Your profile and broker connections are kept.', confirmLabel: 'Delete history', danger: true }),
    disconnectIndmoney: Object.freeze({ title: 'Disconnect INDmoney?', body: 'Saved portfolio snapshots for this profile are deleted. Your chats are kept.', confirmLabel: 'Disconnect INDmoney', danger: true }),
    deleteChat: Object.freeze({ title: 'Delete this chat?', body: 'The chat and its answers are removed from this profile.', confirmLabel: 'Delete chat', danger: true }),
    deleteGoal: Object.freeze({ title: 'Delete the goal "{name}"?', body: 'Decisions linked to it are kept.', confirmLabel: 'Delete goal', danger: true }),
    deleteDecision: Object.freeze({ title: 'Delete this decision?', body: 'It is removed from what the assistant can use.', confirmLabel: 'Delete decision', danger: true }),
    clearContext: Object.freeze({ title: 'Delete saved goals, decisions and profile details?', body: 'Chats, snapshots and statements are kept. This cannot be undone.', confirmLabel: 'Delete saved details', danger: true }),
  });

  function confirmCopy(key, values = {}) {
    const entry = CONFIRMS[key];
    if (!entry) throw new Error(`Unknown confirmation: ${key}`);
    const fill = (text) => text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
    return { title: fill(entry.title), body: fill(entry.body), confirmLabel: entry.confirmLabel, cancelLabel: 'Cancel', danger: entry.danger };
  }

  // The desktop sidebar's open or closed choice, kept per browser. Storage can be missing or throw (private
  // windows, blocked site data): reads then give null and writes report false, and the app works without it.
  const SIDEBAR_KEY = 'ca-mode-sidebar';
  const SIDEBAR_STATES = ['open', 'closed'];

  function readSidebarState(storage) {
    try {
      const value = storage ? storage.getItem(SIDEBAR_KEY) : null;
      return SIDEBAR_STATES.includes(value) ? value : null;
    } catch (error) {
      return null;
    }
  }

  function saveSidebarState(storage, state) {
    try {
      storage.setItem(SIDEBAR_KEY, state);
      return true;
    } catch (error) {
      return false;
    }
  }

  // Up to 1280px the sidebar starts closed whatever was saved; wider, it follows the saved choice (open by default).
  function initialSidebarState({ stored, width }) {
    if (width <= 1280) return 'closed';
    return stored === 'closed' ? 'closed' : 'open';
  }

  // Cmd (Ctrl elsewhere) + K opens the command menu and + B toggles the sidebar, never over a sign-in screen or dialog.
  function shortcutFor(event, { gateOpen, dialogOpen }) {
    if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return null;
    if (gateOpen || dialogOpen) return null;
    const key = String(event.key || '').toLowerCase();
    if (key === 'k') return 'command';
    if (key === 'b') return 'sidebar';
    return null;
  }

  function shortcutLabel(platform) {
    return /Mac|iPhone|iPad/.test(platform || '') ? '⌘K' : 'Ctrl K';
  }

  // What the command menu lists for a query. A blank query browses pages, recent chats and actions; anything typed
  // can always be asked of either agent, then pages, chats and actions whose label holds every word of it.
  function commandResults(query, { pages = [], chats = [], actions = [] } = {}) {
    const text = String(query || '').trim();
    const pageItems = pages.map((page) => ({ id: `page-${page.route}`, kind: 'page', label: page.label, hint: '', value: page.route }));
    const chatItems = chats.map((chat) => ({ id: `chat-${chat.id}`, kind: 'chat', label: chat.title || 'New chat', hint: '', value: String(chat.id) }));
    const actionItems = actions.map((action) => ({ id: `action-${action.id}`, kind: 'action', label: action.label, hint: '', value: action.id }));
    if (!text) {
      return [
        { title: 'Pages', items: pageItems },
        { title: 'Recent chats', items: chatItems.slice(0, 5) },
        { title: 'Actions', items: actionItems },
      ].filter((group) => group.items.length);
    }
    const words = text.toLowerCase().split(/\s+/);
    const matches = (item) => words.every((word) => item.label.toLowerCase().includes(word));
    return [
      { title: 'Ask', items: [
        { id: 'ask-portfolio', kind: 'ask', label: `Ask Portfolio Analyzer: "${text}"`, hint: 'Enter', value: text, agent: 'portfolio' },
        { id: 'ask-finance', kind: 'ask', label: `Ask Finance Agent: "${text}"`, hint: '', value: text, agent: 'finance' },
      ] },
      { title: 'Pages', items: pageItems.filter(matches) },
      { title: 'Chats', items: chatItems.filter(matches).slice(0, 6) },
      { title: 'Actions', items: actionItems.filter(matches) },
    ].filter((group) => group.items.length);
  }

  // Commands that need the conversation free: they would start, switch or end it, or open the profile panel
  // (which refuses while an answer streams). They wait until the current answer has finished.
  const COMMANDS_AFTER_ANSWER = ['new-chat', 'connect-broker', 'sign-out'];
  function commandWaitsForAnswer(item) {
    return item.kind === 'ask' || item.kind === 'chat' || (item.kind === 'action' && COMMANDS_AFTER_ANSWER.includes(item.value));
  }

  // The next active option for the arrow keys, wrapping at both ends; -1 when there is nothing to move through.
  function moveActive(index, delta, count) {
    if (!count) return -1;
    return (((index + delta) % count) + count) % count;
  }

  // Start delays (ms) for the drawer's items: one step apart, none later than max.
  function staggerDelays(count, step = 60, max = 360) {
    return Array.from({ length: count }, (_, i) => Math.min(i * step, max));
  }

  // Runs a DOM update inside a view transition when the browser has one. The update always runs, and an error
  // from it rejects; a transition that is skipped or aborted never does.
  async function withViewTransition(doc, update) {
    if (typeof doc?.startViewTransition !== 'function') {
      await update();
      return;
    }
    let transition;
    try {
      transition = doc.startViewTransition(update);
    } catch {
      await update();
      return;
    }
    transition.finished?.catch(() => {});
    await transition.updateCallbackDone;
  }

  const api = {
    withViewTransition, CONFIRMS, confirmCopy, createSseParser, readEventStream, NETWORK_ERROR, GENERIC_ERROR, plainError, errorText, lostChatResponse, savedAnswerFor,
    isComposingEnter, parseChartSpec, extractChartBlocks, withPending,
    SIDEBAR_KEY, readSidebarState, saveSidebarState, initialSidebarState, shortcutFor, shortcutLabel,
    commandResults, moveActive, commandWaitsForAnswer, staggerDelays,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
