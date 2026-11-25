// Tests for the answer-stream helpers in frontend/js/ui.js. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../frontend/js/ui.js');

test('parser returns events completed in a chunk and keeps the rest', () => {
  const parser = ui.createSseParser();
  assert.deepEqual(parser.push('event: delta\ndata: {"text":"Hel"}\n\nevent: delta\ndata: {"te'), [
    { event: 'delta', data: { text: 'Hel' } },
  ]);
  assert.deepEqual(parser.push('xt":"lo"}\n\n'), [{ event: 'delta', data: { text: 'lo' } }]);
});

test('parser handles CRLF, even split between the CR and the LF', () => {
  const parser = ui.createSseParser();
  const out = [...parser.push('event: reset\r\ndata: {}\r'), ...parser.push('\n\r\nevent: answer\r\ndata: {"text":"x"}\r\n\r\n')];
  assert.deepEqual(out, [{ event: 'reset', data: {} }, { event: 'answer', data: { text: 'x' } }]);
});

test('parser joins multi-line data, skips comments and bad JSON', () => {
  const parser = ui.createSseParser();
  const out = parser.push(': open\n\n: keepalive\n\nevent: answer\ndata: {"text":\ndata: "a"}\n\nevent: delta\ndata: not json\n\n');
  assert.deepEqual(out, [{ event: 'answer', data: { text: 'a' } }]);
});

test('flush returns an event the stream ended in the middle of', () => {
  const parser = ui.createSseParser();
  assert.deepEqual(parser.push('event: answer\ndata: {"text":"end"}'), []);
  assert.deepEqual(parser.flush(), [{ event: 'answer', data: { text: 'end' } }]);
  assert.deepEqual(parser.flush(), []);
});

test('draft text hides finished and unfinished fenced blocks', () => {
  assert.equal(ui.draftAnswerText('Plain words so far'), 'Plain words so far');
  assert.equal(ui.draftAnswerText('Before\n```chart\n{"a":1}\n```\nAfter'),
    'Before\n\n*Chart or table appears when the answer is ready*\n\nAfter');
  assert.equal(ui.draftAnswerText('Before\n```python\nprint(1)'), 'Before');
  assert.equal(ui.draftAnswerText('One\n```a\nx\n```\nTwo\n```b\ny\n```'),
    'One\n\n*Chart or table appears when the answer is ready*\n\nTwo\n\n*Chart or table appears when the answer is ready*');
  assert.equal(ui.draftAnswerText(''), '');
});
