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
