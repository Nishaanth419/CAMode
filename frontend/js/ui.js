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
        buffer += String(text).replace(/\r\n?/g, '\n');
        let at;
        while ((at = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, at);
          buffer = buffer.slice(at + 2);
          dispatch(block);
        }
      },
      end() {
        const partial = buffer.trim() !== '';
        buffer = '';
        return partial;
      },
    };
  }

  const api = {
    createSseParser, NETWORK_ERROR, GENERIC_ERROR, plainError, errorText, lostChatResponse, savedAnswerFor,
    isComposingEnter, parseChartSpec, extractChartBlocks, withPending,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
