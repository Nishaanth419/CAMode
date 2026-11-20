// Unit tests for frontend/js/ui.js. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../frontend/js/ui.js');

test('Enter during an IME composition is not a send', () => {
  assert.equal(ui.isComposingEnter({ key: 'Enter', isComposing: true, keyCode: 13 }), true);
  // Safari reports the composition-ending Enter as keyCode 229 with isComposing already false.
  assert.equal(ui.isComposingEnter({ key: 'Enter', isComposing: false, keyCode: 229 }), true);
  assert.equal(ui.isComposingEnter({ key: 'Enter', isComposing: false, keyCode: 13 }), false);
  assert.equal(ui.isComposingEnter(undefined), false);
});

test('a failed request shows CA Mode\'s own sentence, else a plain one for the status', () => {
  assert.equal(ui.plainError(502, { error: 'The AI model returned an error. Try again in a moment.' }),
    'The AI model returned an error. Try again in a moment.');
  // A proxy's HTML page or an empty body never reaches the user.
  assert.equal(ui.plainError(524, {}), 'CA Mode could not finish that. Try again in a moment.');
  assert.equal(ui.plainError(500, null), 'CA Mode could not finish that. Try again in a moment.');
  assert.equal(ui.plainError(413, { error: '' }), 'That is too large to send.');
  assert.equal(ui.plainError(0, undefined), ui.NETWORK_ERROR);
  assert.equal(ui.plainError(418, { detail: 'teapot' }), ui.GENERIC_ERROR);
});

test('only plain Errors written by CA Mode are shown as is', () => {
  assert.equal(ui.errorText(new Error('Could not open this chat.')), 'Could not open this chat.');
  assert.equal(ui.errorText(new TypeError('Failed to fetch')), ui.NETWORK_ERROR);
  assert.equal(ui.errorText(Object.assign(new Error('aborted'), { name: 'AbortError' })), ui.NETWORK_ERROR);
  assert.equal(ui.errorText(new SyntaxError('Unexpected token < in JSON at position 0')), ui.GENERIC_ERROR);
  assert.equal(ui.errorText(new RangeError('Invalid time value')), ui.GENERIC_ERROR);
  assert.equal(ui.errorText(new Error('  ')), ui.GENERIC_ERROR);
  assert.equal(ui.errorText(undefined), ui.GENERIC_ERROR);
});

test('a dropped connection or a proxy timeout page counts as a lost chat response', () => {
  assert.equal(ui.lostChatResponse(0, {}), true);
  assert.equal(ui.lostChatResponse(524, {}), true); // Cloudflare: origin did not answer in 100 s
  assert.equal(ui.lostChatResponse(504, {}), true); // Caddy's own gateway timeout, no JSON body
  // CA Mode's own answers are final: its 504 deadline, a busy model, a failed answer.
  assert.equal(ui.lostChatResponse(504, { error: 'That took too long.', chatDeadline: true }), false);
  assert.equal(ui.lostChatResponse(503, { error: 'Busy', modelBusy: true }), false);
  assert.equal(ui.lostChatResponse(502, { error: 'The AI model returned an error.' }), false);
  assert.equal(ui.lostChatResponse(400, {}), false);
  assert.equal(ui.lostChatResponse(500, {}), false);
});

test('the saved answer is found only when the chat ends with this question, saved after the send', () => {
  const chat = {
    updatedAt: 2000,
    messages: [
      { role: 'user', content: 'Old question' }, { role: 'assistant', content: 'Old answer' },
      { role: 'user', content: 'How did my portfolio do?' }, { role: 'assistant', content: 'Up 2%.', runId: 'run-1' },
    ],
  };
  assert.deepEqual(ui.savedAnswerFor(chat, '  How did my portfolio do?\n', 1000), { role: 'assistant', content: 'Up 2%.', runId: 'run-1' });
  // Not saved since the send: the same question asked earlier is not this answer.
  assert.equal(ui.savedAnswerFor(chat, 'How did my portfolio do?', 2000), null);
  assert.equal(ui.savedAnswerFor(chat, 'Something else', 1000), null);
  // A new chat has no earlier time to compare with.
  assert.equal(ui.savedAnswerFor(chat, 'How did my portfolio do?', NaN)?.content, 'Up 2%.');
  assert.equal(ui.savedAnswerFor({ updatedAt: 3000, messages: [{ role: 'user', content: 'Hi' }] }, 'Hi', 1000), null);
  assert.equal(ui.savedAnswerFor(null, 'Hi', 1000), null);
  // The server stores at most 12,000 characters of a question.
  const long = 'x'.repeat(12500);
  assert.ok(ui.savedAnswerFor({ updatedAt: 5, messages: [{ role: 'user', content: long.slice(0, 12000) }, { role: 'assistant', content: 'ok' }] }, long, 1));
});

test('only fenced chart blocks are drawn; bold or inline chart JSON stays text', () => {
  const fenced = 'Allocation:\n```chart\n{"type": "donut", "data": [{"label": "Equity", "value": 60}]}\n```\nDone.';
  const { text, charts } = ui.extractChartBlocks(fenced);
  assert.equal(text, 'Allocation:\n\x00CHART0\x00\nDone.');
  assert.equal(charts.length, 1);
  assert.equal(charts[0].type, 'donut');

  const bold = 'Here: **{"type": "bar", "data": [{"label": "A", "value": 9999}]}**';
  assert.deepEqual(ui.extractChartBlocks(bold), { text: bold, charts: [] });
  const inline = 'Here: `{"type": "bar", "data": [{"label": "A", "value": 9999}]}`';
  assert.deepEqual(ui.extractChartBlocks(inline), { text: inline, charts: [] });
  // The fence must match the server's pattern (```chart, spaces or tabs, newline) or it is not drawn.
  const crlf = '```chart\r\n{"type": "bar", "data": []}\n```';
  assert.deepEqual(ui.extractChartBlocks(crlf).charts, []);

  // An unreadable fenced block keeps its place and draws as the "could not draw" note.
  assert.deepEqual(ui.extractChartBlocks('```chart\nnot json\n```').charts, [null]);
});

function fakeButton() {
  const attributes = {};
  return {
    disabled: false, attributes,
    setAttribute(name, value) { attributes[name] = value; },
    removeAttribute(name) { delete attributes[name]; },
  };
}

test('a dialog action disables its button while it runs and ignores a second click', async () => {
  const button = fakeButton();
  const status = { textContent: '' };
  let release;
  let runs = 0;
  const first = ui.withPending(button, status, () => { runs += 1; return new Promise((resolve) => { release = resolve; }); });
  assert.equal(button.disabled, true);
  assert.equal(button.attributes['aria-busy'], 'true');
  assert.equal(await ui.withPending(button, status, async () => { runs += 1; }), undefined);
  release('done');
  assert.equal(await first, 'done');
  assert.equal(runs, 1);
  assert.equal(button.disabled, false);
  assert.equal(button.attributes['aria-busy'], undefined);
  assert.equal(status.textContent, '');
});

test('a failed dialog action shows a plain message and frees its button', async () => {
  const button = fakeButton();
  const status = { textContent: '' };
  assert.equal(await ui.withPending(button, status, async () => { throw new Error('Incorrect PIN.'); }), undefined);
  assert.equal(status.textContent, 'Incorrect PIN.');
  assert.equal(button.disabled, false);
  await ui.withPending(button, status, async () => { throw new TypeError('Failed to fetch'); });
  assert.equal(status.textContent, ui.NETWORK_ERROR);
  await ui.withPending(button, status, async () => { JSON.parse('<html>'); });
  assert.equal(status.textContent, ui.GENERIC_ERROR);
});

test('SSE parser joins chunks split mid-event', () => {
  const seen = [];
  const parser = ui.createSseParser((name, data) => seen.push([name, data]));
  parser.push('event: progress\nda');
  assert.equal(seen.length, 0);
  parser.push('ta: {"seq":0,"kind":"start"}\n\n');
  assert.deepEqual(seen, [['progress', { seq: 0, kind: 'start' }]]);
  assert.equal(parser.end(), false);
});

test('SSE parser handles CRLF, keepalive comments and bad JSON', () => {
  const seen = [];
  const parser = ui.createSseParser((name, data) => seen.push([name, data]));
  parser.push(': keepalive\n\nevent: x\ndata: {\n\n');
  assert.equal(seen.length, 0);
  parser.push('event: a\r\ndata: {"n":1}\r\n\r\n');
  assert.deepEqual(seen, [['a', { n: 1 }]]);
});

test('SSE parser joins multi-line data', () => {
  const seen = [];
  const parser = ui.createSseParser((name, data) => seen.push([name, data]));
  parser.push('event: answer\ndata: {"text":\ndata: "hi"}\n\n');
  assert.deepEqual(seen, [['answer', { text: 'hi' }]]);
});

test('SSE parser end() reports a trailing partial event', () => {
  const parser = ui.createSseParser(() => {});
  parser.push('event: answer\ndata: {"te');
  assert.equal(parser.end(), true);
  assert.equal(parser.end(), false);
});

test('SSE parser handles CRLF split across chunks and cut comments', () => {
  const seen = [];
  const parser = ui.createSseParser((name, data) => seen.push([name, data]));
  parser.push('event: a\r');
  parser.push('\ndata: {"n":1}\r\n\r');
  parser.push('\n');
  assert.deepEqual(seen, [['a', { n: 1 }]]);
  parser.push(': keepal');
  assert.equal(parser.end(), false);
});

function streamOf(chunks, { failAfter = null } = {}) {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream({
    pull(controller) {
      if (failAfter !== null && index === failAfter) { controller.error(new TypeError('network')); return; }
      if (index >= chunks.length) { controller.close(); return; }
      const chunk = chunks[index++];
      controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
    },
  });
}

test('readEventStream decodes chunks, including a character split across them', async () => {
  const seen = [];
  const bytes = new TextEncoder().encode('event: answer\ndata: {"text":"₹ 5"}\n\n');
  const cut = bytes.indexOf(0xe2) + 1; // inside the three bytes of ₹
  const partial = await ui.readEventStream(streamOf([': keepalive\n\nevent: progress\ndata: {"seq":0,"kind":"start"}\n\n', bytes.slice(0, cut), bytes.slice(cut)]),
    (name, data) => seen.push([name, data]));
  assert.equal(partial, false);
  assert.deepEqual(seen, [['progress', { seq: 0, kind: 'start' }], ['answer', { text: '₹ 5' }]]);
});

test('readEventStream reports a stream cut inside an event', async () => {
  const seen = [];
  const partial = await ui.readEventStream(streamOf(['event: progress\ndata: {"seq":0}\n\nevent: answer\ndata: {"te']),
    (name) => seen.push(name));
  assert.equal(partial, true);
  assert.deepEqual(seen, ['progress']);
});

test('readEventStream rejects when the read fails', async () => {
  await assert.rejects(ui.readEventStream(streamOf(['event: progress\ndata: {}\n\n'], { failAfter: 1 }), () => {}));
});

test('every confirmation names its action and keeps today\'s meaning', () => {
  assert.deepEqual(ui.confirmCopy('deleteChat'), { title: 'Delete this chat?', body: 'The chat and its answers are removed from this profile.', confirmLabel: 'Delete chat', cancelLabel: 'Cancel', danger: true });
  assert.equal(ui.confirmCopy('deleteGoal', { name: 'House' }).title, 'Delete the goal "House"?');
  assert.equal(ui.confirmCopy('deleteGoal', { name: 'House' }).body, 'Decisions linked to it are kept.');
  assert.equal(ui.confirmCopy('disconnectIndmoney').confirmLabel, 'Disconnect INDmoney');
  assert.equal(ui.confirmCopy('privateMode').confirmLabel, 'Switch to private mode');
  assert.equal(ui.confirmCopy('deleteDecision').confirmLabel, 'Delete decision');
  assert.equal(ui.confirmCopy('clearContext').confirmLabel, 'Delete saved details');
  assert.equal(ui.confirmCopy('newRecoveryCode').danger, false);
  for (const key of Object.keys(ui.CONFIRMS)) assert.ok(!/[!]/.test(JSON.stringify(ui.CONFIRMS[key])), key);
  assert.throws(() => ui.confirmCopy('nope'));
});

test('the sidebar remembers its state and survives blocked storage', () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(ui.readSidebarState(storage), null);
  assert.equal(ui.saveSidebarState(storage, 'closed'), true);
  assert.equal(ui.readSidebarState(storage), 'closed');
  store.set(ui.SIDEBAR_KEY, 'sideways');
  assert.equal(ui.readSidebarState(storage), null);
  const blocked = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.equal(ui.readSidebarState(blocked), null);
  assert.equal(ui.saveSidebarState(blocked, 'open'), false);
});
test('the sidebar starts open on wide screens and closed up to 1280px', () => {
  assert.equal(ui.initialSidebarState({ stored: null, width: 1440 }), 'open');
  assert.equal(ui.initialSidebarState({ stored: 'closed', width: 1440 }), 'closed');
  assert.equal(ui.initialSidebarState({ stored: 'open', width: 1100 }), 'closed');
  assert.equal(ui.initialSidebarState({ stored: null, width: 1280 }), 'closed');
});
test('Cmd/Ctrl+K opens the command menu and Cmd/Ctrl+B toggles the sidebar', () => {
  const key = (k, mods = {}) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods });
  const free = { gateOpen: false, dialogOpen: false };
  assert.equal(ui.shortcutFor(key('k', { metaKey: true }), free), 'command');
  assert.equal(ui.shortcutFor(key('K', { ctrlKey: true }), free), 'command');
  assert.equal(ui.shortcutFor(key('b', { ctrlKey: true }), free), 'sidebar');
  assert.equal(ui.shortcutFor(key('k', { metaKey: true, shiftKey: true }), free), null);
  assert.equal(ui.shortcutFor(key('k'), free), null);
  assert.equal(ui.shortcutFor(key('k', { metaKey: true }), { gateOpen: true, dialogOpen: false }), null);
  assert.equal(ui.shortcutFor(key('b', { metaKey: true }), { gateOpen: false, dialogOpen: true }), null);
  assert.equal(ui.shortcutLabel('MacIntel'), '⌘K');
  assert.equal(ui.shortcutLabel('Win32'), 'Ctrl K');
});
