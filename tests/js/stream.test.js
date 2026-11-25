// Tests for the draft-answer helper in frontend/js/ui.js. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../frontend/js/ui.js');

test('draft text hides finished and unfinished fenced blocks', () => {
  assert.equal(ui.draftAnswerText('Plain words so far'), 'Plain words so far');
  assert.equal(ui.draftAnswerText('Before\n```chart\n{"a":1}\n```\nAfter'),
    'Before\n\n*Chart or table appears when the answer is ready*\n\nAfter');
  assert.equal(ui.draftAnswerText('Before\n```python\nprint(1)'), 'Before');
  assert.equal(ui.draftAnswerText('One\n```a\nx\n```\nTwo\n```b\ny\n```'),
    'One\n\n*Chart or table appears when the answer is ready*\n\nTwo\n\n*Chart or table appears when the answer is ready*');
  assert.equal(ui.draftAnswerText(''), '');
});
