// Design lint for the frontend: tokens, fonts and house rules. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const FRONTEND = path.join(__dirname, '..', '..', 'frontend');
const readCss = (name = 'app.css') => fs.readFileSync(path.join(FRONTEND, 'css', name), 'utf8');
const readHtml = () => fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
const readJs = (name) => fs.readFileSync(path.join(FRONTEND, 'js', name), 'utf8');

// The custom properties of the rule whose selector is exactly `selector`, as name -> trimmed value.
function tokenBlock(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, 'm').exec(css);
  assert.ok(match, `no block for ${selector}`);
  const tokens = new Map();
  for (const m of match[1].matchAll(/(--[\w-]+)\s*:\s*((?:[^;"]|"[^"]*")+);/g)) {
    tokens.set(m[1], m[2].trim());
  }
  return tokens;
}

test('only Geist and Geist Mono are loaded', () => {
  const html = readHtml();
  assert.match(html, /family=Geist:wght@400;500;600;700/);
  assert.doesNotMatch(html, /Inter|Fraunces|JetBrains/);
  assert.doesNotMatch(readCss(), /'Inter'|Fraunces|JetBrains Mono/);
});

test('dark tokens use only the approved surfaces and accent', () => {
  const dark = tokenBlock(readCss(), ':root[data-theme="dark"]');
  assert.equal(dark.get('--canvas'), '#000000');
  assert.equal(dark.get('--surface'), '#181818');
  assert.equal(dark.get('--surface-2'), '#1F1F1F');
  assert.equal(dark.get('--surface-3'), '#272727');
  assert.equal(dark.get('--border'), '#313131');
  assert.equal(dark.get('--accent'), '#C2A66B');
  assert.equal(dark.get('--text-muted'), '#8A8A8A');
});

test('light and dark define the same tokens', () => {
  const css = readCss();
  assert.deepEqual(
    [...tokenBlock(css, ':root[data-theme="dark"]').keys()].sort(),
    [...tokenBlock(css, ':root').keys()]
      .filter((k) => !/-(w|h)$|^--radius|^--control|^--sans|^--serif|^--mono|^--ease|^--dur/.test(k))
      .sort(),
  );
});

test('light tokens follow the spec and --line equals --border', () => {
  const css = readCss();
  const light = tokenBlock(css, ':root');
  assert.equal(light.get('--canvas'), '#FFFFFF');
  assert.equal(light.get('--surface'), '#F6F6F5');
  assert.equal(light.get('--surface-2'), '#EFEFED');
  assert.equal(light.get('--border'), '#E4E4E1');
  assert.equal(light.get('--accent'), '#8A6F35');
  assert.equal(light.get('--text-muted'), '#6B6B6B');
  for (const block of [light, tokenBlock(css, ':root[data-theme="dark"]')]) {
    assert.equal(block.get('--line'), block.get('--border'));
  }
});

module.exports = { readCss, readHtml, readJs, tokenBlock };
