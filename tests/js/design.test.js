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

test('every font size is on the scale', () => {
  const allowed = new Set([12, 14, 16, 18, 20, 24, 30, 36, 48, 60]);
  for (const file of ['app.css', 'public.css', 'admin.css']) {
    const css = readCss(file);
    for (const [, n] of css.matchAll(/font-size:\s*([\d.]+)px/g)) assert.ok(allowed.has(Number(n)), `${file}: ${n}px`);
    for (const [, n] of css.matchAll(/[\s;{]font:\s*(?:[a-z-]+\s+)*([\d.]+)px/g)) assert.ok(allowed.has(Number(n)), `${file} font shorthand: ${n}px`);
    assert.doesNotMatch(css, /font-size:\s*[\d.]+rem/, `${file}: rem font size`);
  }
});

test('no uppercase, italic or weights above bold', () => {
  for (const file of ['app.css', 'public.css', 'admin.css']) {
    const css = readCss(file);
    assert.doesNotMatch(css, /text-transform:\s*uppercase/, file);
    assert.doesNotMatch(css, /font-style:\s*italic/, file);
    assert.doesNotMatch(css, /font-weight:\s*(800|900)/, file);
  }
});

module.exports = { readCss, readHtml, readJs, tokenBlock };

test('radii come from the scale', () => {
  const ok = /^(0|2px|4px|6px|8px|12px|16px|50%|9999px|inherit|var\(--radius-[a-z]+\))$/;
  for (const name of ['app.css', 'public.css', 'admin.css']) {
    for (const [, v] of readCss(name).matchAll(/border-radius:\s*([^;]+);/g)) for (const part of v.trim().split(/\s+/)) assert.match(part, ok, `${name}: ${v}`);
  }
});

test('no background gradients and no generic shadows', () => {
  for (const name of ['app.css', 'public.css', 'admin.css']) {
    const css = readCss(name);
    assert.equal((css.match(/gradient\(/g) || []).length, 0, name);
    assert.doesNotMatch(css, /box-shadow:\s*0 1px 3px rgba\(0,\s*0,\s*0/, name);
    assert.doesNotMatch(css, /--shadow-sm:\s*0/, name);
  }
});

test('transitions use the motion tokens', () => {
  const css = readCss();
  for (const [, v] of css.matchAll(/transition:\s*([^;]+);/g)) {
    assert.doesNotMatch(v, /(?<![-\w])(ease-in-out|ease-in|ease-out|ease|linear)(?![-\w])/, v);
    for (const [, ms] of v.matchAll(/(\d+)ms/g)) assert.ok(Number(ms) >= 200, v);
  }
  for (const [, v] of css.matchAll(/animation:\s*([^;]+);/g)) {
    assert.doesNotMatch(v, /(?<![-\w])(ease-in-out|ease-in|ease-out|ease|linear)(?![-\w])/, v);
  }
});
test('reduced motion stops movement and loops', () => {
  const block = readCss().split('@media (prefers-reduced-motion: reduce)')[1] || '';
  assert.match(block, /animation-iteration-count:\s*1/);
  assert.match(block, /transition-property:\s*opacity/);
});
test('keyboard focus is always visible', () => assert.match(readCss(), /:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/));

test('no browser dialogs in the app scripts', () => {
  for (const name of ['app.js', 'pages.js', 'demo.js']) {
    assert.doesNotMatch(readJs(name), /window\.confirm\(|(^|[^\w.])confirm\(|\balert\(/m, name);
  }
});

test('every sprite icon referenced by the app exists in icons.svg', () => {
  const sprite = fs.readFileSync(path.join(FRONTEND, 'icons.svg'), 'utf8');
  const symbols = new Set([...sprite.matchAll(/<symbol id="(ph-[\w-]+)" viewBox="0 0 256 256"/g)].map((m) => m[1]));
  assert.ok(symbols.size > 0);
  const sources = [readHtml(), readJs('pages.js'), readJs('app.js')];
  const used = new Set();
  for (const source of sources) {
    for (const m of source.matchAll(/\/icons\.svg#(ph-[\w-]+)/g)) used.add(m[1]);
  }
  assert.ok(used.size > 0);
  for (const id of used) assert.ok(symbols.has(id), `missing symbol ${id}`);
  for (const id of symbols) assert.ok(used.has(id), `unused symbol ${id}`);
  assert.match(sprite, /Phosphor Icons.*MIT.*icons-LICENSE\.txt/s);
  assert.ok(fs.existsSync(path.join(FRONTEND, 'icons-LICENSE.txt')));
});

test('index.html keeps inline svg only for the brand marks, the illustration, the Google mark and the model caret', () => {
  const html = readHtml();
  const inline = [...html.matchAll(/<svg\b([^>]*)>/g)].map((m) => m[1]);
  for (const attrs of inline) {
    const allowed = /viewBox="0 0 28 28"/.test(attrs) || /viewBox="0 0 260 170"/.test(attrs)
      || /class="google-mark"/.test(attrs) || /class="model-caret"/.test(attrs) || /class="icon"/.test(attrs);
    assert.ok(allowed, `unexpected inline svg: ${attrs}`);
  }
  assert.doesNotMatch(html, /<svg class="icon"[^>]*>(?!<use )/);
  assert.doesNotMatch(readJs('app.js'), /themeToggleIcon\.innerHTML/);
});

test('sprite icons are filled, whatever stroke rules the context sets', () => {
  const sprite = fs.readFileSync(path.join(FRONTEND, 'icons.svg'), 'utf8');
  for (const m of sprite.matchAll(/<symbol\b([^>]*)>/g)) {
    assert.match(m[1], /fill="currentColor"/);
    assert.match(m[1], /stroke="none"/);
  }
  assert.match(readCss(), /\.icon \{[^}]*width: 20px[^}]*height: 20px[^}]*flex-shrink: 0/);
});
