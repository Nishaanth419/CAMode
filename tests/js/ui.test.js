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
