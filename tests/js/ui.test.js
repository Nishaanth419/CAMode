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
