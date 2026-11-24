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

// The custom properties of `selector` inside the `@media (media) { ... }` block, found by brace matching.
function mediaTokenBlock(css, media, selector) {
  const start = css.indexOf(`@media (${media})`);
  assert.ok(start >= 0, `no @media (${media}) block`);
  const open = css.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) return tokenBlock(css.slice(open + 1, i), selector);
  }
  throw new Error(`unterminated @media (${media}) block`);
}

// The declarations of the first rule whose selector list is exactly `selector` (any nesting depth).
function ruleBody(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`(?:^|[}{;])[ \\t]*${escaped}\\s*\\{([^}]*)\\}`, 'm').exec(css);
  assert.ok(match, `no rule for ${selector}`);
  return match[1];
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

test('the command menu is a dialog with a combobox driving a listbox', () => {
  const html = readHtml();
  const menu = html.slice(html.indexOf('<dialog id="command-dialog"'), html.indexOf('</dialog>', html.indexOf('<dialog id="command-dialog"')));
  assert.match(menu, /aria-label="Command menu"/);
  assert.match(menu, /id="command-input"[^>]*role="combobox"[^>]*aria-controls="command-list"/);
  assert.match(menu, /id="command-list"[^>]*role="listbox"/);
  assert.match(readCss(), /\.command-dialog\s*\{[^}]*width:\s*min\(640px/);
});

test('radii come from the scale', () => {
  const ok = /^(0|2px|4px|6px|8px|12px|16px|50%|9999px|inherit|var\(--radius-[a-z]+\))$/;
  for (const name of ['app.css', 'public.css', 'admin.css']) {
    for (const [, v] of readCss(name).matchAll(/border-radius:\s*([^;]+);/g)) for (const part of v.trim().split(/\s+/)) assert.match(part, ok, `${name}: ${v}`);
  }
});

test('no background gradients and no generic shadows', () => {
  for (const name of ['app.css', 'public.css', 'admin.css']) {
    const css = readCss(name);
    const gradients = (css.match(/gradient\(/g) || []).length;
    if (name === 'app.css') {
      // Exactly one: the Home hero figure's text gradient.
      assert.equal(gradients, 1, name);
      assert.match(ruleBody(css, '#home-networth-value'), /linear-gradient\(/);
    } else {
      assert.equal(gradients, 0, name);
    }
    assert.doesNotMatch(css, /box-shadow:\s*0 1px 3px rgba\(0,\s*0,\s*0/, name);
    assert.doesNotMatch(css, /--shadow-sm:\s*0/, name);
  }
});

test('transitions use the motion tokens', () => {
  const css = ['app.css', 'public.css', 'admin.css'].map((name) => readCss(name)).join('\n');
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
  for (const name of ['app.js', 'pages.js', 'demo.js', 'shell.js', 'admin.js']) {
    assert.doesNotMatch(readJs(name), /window\.(confirm|prompt|alert)\(|(^|[^\w.])(confirm|prompt)\(|\balert\(/m, name);
  }
});

test('admin page: dialog, flat figures, tables and buttons on the tokens', () => {
  const css = readCss('admin.css');
  const dialog = ruleBody(css, '.admin-dialog');
  assert.match(dialog, /border-radius:\s*16px/);
  assert.match(css, /\.admin-dialog::backdrop\s*\{/);
  assert.doesNotMatch(ruleBody(css, '.admin-tile'), /(^|[;\s])border:/);
  const tiles = ruleBody(css, '.admin-tiles');
  assert.match(tiles, /background:\s*var\(--surface\)/);
  assert.match(tiles, /border-radius:\s*12px/);
  const th = ruleBody(css, '.admin-table th');
  assert.match(th, /font-size:\s*12px/);
  assert.match(th, /font-weight:\s*500/);
  for (const m of css.matchAll(/([^{}]*:hover[^{}]*)\{([^}]*)\}/g)) {
    assert.doesNotMatch(m[2], /border-color:\s*var\(--accent\)/, m[1].trim());
  }
  assert.match(ruleBody(css, '.admin-danger'), /color:\s*var\(--loss-text\)/);
  for (const name of ['--danger', '--warn', '--muted-bg', '--border']) {
    assert.doesNotMatch(css, new RegExp(`var\\(${name}\\)`), `admin.css reads ${name}`);
  }
});

test('every sprite icon referenced by the app exists in icons.svg', () => {
  const sprite = fs.readFileSync(path.join(FRONTEND, 'icons.svg'), 'utf8');
  const symbols = new Set([...sprite.matchAll(/<symbol id="(ph-[\w-]+)" viewBox="0 0 256 256"/g)].map((m) => m[1]));
  assert.ok(symbols.size > 0);
  const sources = [readHtml(), readJs('pages.js'), readJs('app.js'), readJs('shell.js')];
  const used = new Set();
  for (const source of sources) {
    for (const m of source.matchAll(/\/icons\.svg\?v=\d+#(ph-[\w-]+)/g)) used.add(m[1]);
  }
  assert.ok(used.size > 0);
  for (const id of used) assert.ok(symbols.has(id), `missing symbol ${id}`);
  for (const id of symbols) assert.ok(used.has(id), `unused symbol ${id}`);
  assert.match(sprite, /Phosphor Icons.*MIT.*icons-LICENSE\.txt/s);
  assert.ok(fs.existsSync(path.join(FRONTEND, 'icons-LICENSE.txt')));
});

test('index.html keeps inline svg only for the brand marks, the Google mark and the model caret', () => {
  const html = readHtml();
  const inline = [...html.matchAll(/<svg\b([^>]*)>/g)].map((m) => m[1]);
  for (const attrs of inline) {
    const allowed = /viewBox="0 0 28 28"/.test(attrs)
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

test('chart code takes every colour and the font from chartTheme(), with no hex literals', () => {
  const src = readJs('app.js');
  assert.doesNotMatch(src, /'Inter[,']/);
  assert.match(src, /function chartTheme\(\)/);
  const start = src.indexOf('function renderDataChart');
  const end = src.indexOf('function renderPlotlyCharts');
  assert.ok(start > 0 && end > start);
  const region = src.slice(start, end);
  assert.doesNotMatch(region, /#[0-9a-fA-F]{3,6}\b/);
  const outside = region.replace(/function chartTheme\(\) \{[\s\S]*?\n\}\n/, '');
  assert.doesNotMatch(outside, /getPropertyValue|getComputedStyle/);
  assert.match(src, /getComputedStyle\(document\.documentElement\)/);
  assert.doesNotMatch(src, /setAttribute\('content', selected === 'dark' \? '#/);
});

// The body of the first `@media (prefers-reduced-motion: reduce)` block, found by brace matching.
function reducedMotionBlock(css) {
  const start = css.indexOf('@media (prefers-reduced-motion: reduce)');
  assert.ok(start >= 0);
  let depth = 0;
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) return css.slice(css.indexOf('{', start) + 1, i);
  }
  throw new Error('unterminated reduced-motion block');
}
function keyframes(css) {
  const found = new Map();
  for (const m of css.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    let depth = 1;
    let i = m.index + m[0].length;
    for (; i < css.length && depth > 0; i++) depth += css[i] === '{' ? 1 : css[i] === '}' ? -1 : 0;
    found.set(m[1], css.slice(m.index, i));
  }
  return found;
}

test('reduced motion keeps keyframes to opacity and drops the press', () => {
  const css = readCss();
  const block = reducedMotionBlock(css);
  const inside = keyframes(block);
  const outside = keyframes(css.replace(block, ''));
  for (const [name, body] of inside) assert.doesNotMatch(body, /transform/, name);
  for (const [name, body] of outside) {
    if (/transform/.test(body)) assert.ok(inside.has(name), `${name} moves and is not redefined for reduced motion`);
  }
  assert.match(block, /button:active:not\(:disabled\), \.button:active \{ transform: none; \}/);
});

function channel(v) { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }
function rgb(hex) { const n = parseInt(hex.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
function luminance([r, g, b]) { return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b); }
function ratio(a, b) { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); }
function over(fg, alpha, bg) { return fg.map((v, i) => Math.round(v * alpha + bg[i] * (1 - alpha))); }

const CONTRAST_ROWS = [];
for (const [theme, selector] of [['light', ':root'], ['dark', ':root[data-theme="dark"]']]) {
  test(`text tokens reach 4.5:1 in the ${theme} theme`, () => {
    const t = tokenBlock(readCss(), selector);
    const hex = (name) => { const v = t.get(name); assert.match(v, /^#[0-9A-Fa-f]{6}$/, name); return rgb(v); };
    const surfaces = { surface: hex('--surface'), 'surface-2': hex('--surface-2') };
    const texts = ['--text', '--text-soft', '--text-muted', '--accent-text', '--gain-text', '--loss-text', '--warning-text', '--violet-text', '--blue-text'];
    const check = (label, fg, bg) => {
      const r = ratio(fg, bg);
      CONTRAST_ROWS.push(`${theme} ${label} ${r.toFixed(2)}`);
      assert.ok(r >= 4.5, `${theme} ${label} is ${r.toFixed(2)}`);
    };
    for (const name of texts) {
      for (const [sname, bg] of Object.entries(surfaces)) check(`${name} on ${sname}`, hex(name), bg);
    }
    const tints = { '--accent-text': ['--accent', 0.1], '--gain-text': ['--gain', 0.12], '--loss-text': ['--loss', 0.12],
      '--warning-text': ['--warning', 0.12], '--violet-text': ['--violet', 0.12], '--blue-text': ['--blue', 0.12] };
    for (const [name, [base, alpha]] of Object.entries(tints)) {
      const baseHex = t.get(base);
      assert.match(baseHex, /^#[0-9A-Fa-f]{6}$/, base);
      check(`${name} on its tint`, hex(name), over(rgb(baseHex), alpha, surfaces.surface));
    }
    check('accent-ink on accent', hex('--accent-ink'), hex('--accent'));
    check('accent-ink on accent-3 (hover)', hex('--accent-ink'), hex('--accent-3'));
  });
}
test.after(() => { if (process.env.SHOW_CONTRAST) console.log(CONTRAST_ROWS.join('\n')); });

test('the sidebar opens at 248px and closes to 64px, animating only its column', () => {
  const css = readCss();
  assert.match(css, /--sidebar-w:\s*248px/);
  assert.match(css, /html\.sidebar-closed\s*\{[^}]*--sidebar-w:\s*64px/);
  assert.match(css, /#app\s*\{[^}]*transition:\s*grid-template-columns var\(--dur-move\) var\(--ease\)/);
  assert.match(css, /body:not\(\.shell-ready\) #app\s*\{\s*transition:\s*none;?\s*\}/);
  assert.match(readJs('shell.js'), /classList\.add\('shell-ready'\)/);
});
test('the sidebar keeps its hooks and drops the group labels and the top bar search', () => {
  const html = readHtml();
  for (const id of ['sidebar', 'sidebar-toggle', 'command-open', 'chat-list', 'new-chat-button', 'account-trigger', 'sidebar-scrim', 'topbar', 'topbar-account', 'chat-search', 'status-badge', 'theme-toggle', 'menu-toggle']) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.doesNotMatch(html, /nav-group-label/);
  assert.match(html, /id="sidebar-toggle"[^>]*aria-expanded="true"[^>]*aria-label="Close sidebar"/);
  const sidebar = html.slice(html.indexOf('<aside id="sidebar"'), html.indexOf('</aside>'));
  const order = [...sidebar.matchAll(/data-route="([\w-]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, ['home', 'portfolio', 'holdings', 'chat', 'activity', 'chats', 'goals', 'returns', 'planning', 'markets', 'reports']);
  const profile = html.slice(html.indexOf('<dialog id="profile-dialog"'), html.indexOf('</dialog>', html.indexOf('<dialog id="profile-dialog"')));
  assert.match(profile, /id="status-badge"/);
  assert.match(profile, /id="theme-toggle"/);
  const allChats = html.slice(html.indexOf('data-page="chats"'), html.indexOf('data-page="not-found"'));
  assert.match(allChats, /id="chat-search"/);
});
test('closed-sidebar rules are desktop only; the phone drawer always shows the open sidebar', () => {
  const css = readCss();
  const phone = css.slice(css.indexOf('@media (max-width: 900px)'));
  assert.match(phone.slice(0, phone.indexOf('\n}\n')), /html\.sidebar-closed #sidebar/);
});
test('the closed sidebar keeps scrolling and names its items with native titles, not CSS tooltips', () => {
  const css = readCss();
  const closedRules = [...css.matchAll(/html\.sidebar-closed #sidebar[^{]*\{([^}]*)\}/g)];
  for (const [, body] of closedRules) assert.doesNotMatch(body, /overflow(-y)?:\s*visible/);
  assert.doesNotMatch(css, /html\.sidebar-closed #sidebar \.nav-item:hover \.nav-label/);
  assert.match(css, /#sidebar\s*\{[^}]*overflow-y:\s*auto/);
  const shell = readJs('shell.js');
  assert.match(shell, /classList\.toggle\('sidebar-closed', collapsed\);\n\s*setSidebarTitles\(collapsed\);/);
  assert.match(shell, /querySelectorAll\('#sidebar \.nav-item, #command-open, #account-trigger'\)/);
  assert.match(shell, /if \(item\.hasAttribute\('title'\)\) return;/);
  assert.match(shell, /item\.removeAttribute\('title'\)/);
});
test('the phone drawer fades its items out together when it closes', () => {
  const css = readCss();
  const phone = css.slice(css.indexOf('@media (max-width: 900px)'));
  const base = /#sidebar \.brand, #sidebar #command-open, #sidebar \.nav-item[^{]*\{([^}]*)\}/.exec(phone)[1];
  assert.match(base, /opacity:\s*0/);
  assert.match(base, /transition:\s*opacity var\(--dur-fade\) var\(--ease\)/);
  assert.doesNotMatch(base, /transition-delay/);
});
test('boot.js runs in <head> before paint and the closed state lives on the root element', () => {
  const html = readHtml();
  const head = html.slice(0, html.indexOf('</head>'));
  const tag = /<script src="\/js\/boot\.js\?v=\d+"([^>]*)><\/script>/.exec(head);
  assert.ok(tag, 'boot.js is not in <head>');
  assert.doesNotMatch(tag[1], /defer|async|type="module"/);
  assert.ok(head.indexOf('/css/app.css') < head.indexOf('/js/boot.js'));
  assert.doesNotMatch(readCss(), /body\.sidebar-closed/);
  assert.match(readJs('shell.js'), /document\.documentElement\.classList\.toggle\('sidebar-closed'/);
  assert.match(fs.readFileSync(path.join(FRONTEND, 'sw.js'), 'utf8'), /APP_SHELL = \[[^\]]*'\/js\/boot\.js'/);
});

test('profile, goal and allocation dialogs are right-side slide-overs', () => {
  const css = readCss();
  const rule = /\.profile-dialog\s*\{([^}]*)\}/.exec(css)[1];
  assert.match(rule, /margin:\s*0 0 0 auto/);
  assert.match(rule, /height:\s*100dvh/);
  assert.match(rule, /border-radius:\s*16px 0 0 16px/);
  assert.match(css, /@starting-style\s*\{[^{}]*\.profile-dialog\[open\]\s*\{[^}]*transform:\s*translateX\(100%\)/);
  const reduce = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert.match(reduce, /\.profile-dialog[^{]*\{[^}]*transform:\s*none/);
});

test('slide-over head and actions keep clear of the notch and home indicator', () => {
  const css = readCss();
  assert.match(css, /\.profile-dialog \.dialog-head\s*\{[^}]*padding:\s*max\(20px, env\(safe-area-inset-top\)\)/);
  assert.match(css, /\.profile-dialog \.dialog-actions\s*\{[^}]*max\(20px, env\(safe-area-inset-bottom\)\)/);
  assert.match(css, /\.profile-dialog \.dialog-head\s*\{[^}]*env\(safe-area-inset-right\)/);
});

test('the phone tab bar marks the current tab with an accent bar and the drawer staggers its items', () => {
  const css = readCss();
  assert.match(css, /\.tab\[aria-current="page"\][^{]*::before\s*\{[^}]*background:\s*var\(--accent\)/);
  assert.match(css, /body\.sidebar-open #sidebar [^{]*\{[^}]*transition-delay:\s*var\(--stagger/);
  const reduce = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert.match(reduce, /#sidebar[^{]*\{[^}]*transform:\s*none/);
});

test('the page body cross-fades and rises 8px; the sidebar never moves', () => {
  const css = readCss();
  assert.match(css, /#page-view,\s*#chat-scroll\s*\{[^}]*view-transition-name:\s*page-body/);
  assert.match(css, /::view-transition-new\(page-body\)\s*\{[^}]*animation:[^;]*var\(--dur-fade\) var\(--ease\)/);
  assert.match(css, /::view-transition-old\(root\),\s*::view-transition-new\(root\)\s*\{[^}]*animation:\s*none/);
});

test('the sign-in gate is one column on the canvas, not a card', () => {
  const css = readCss();
  const card = /\.gate-card\s*\{([^}]*)\}/.exec(css)[1];
  assert.doesNotMatch(card, /border:\s*1px|box-shadow|background:\s*var\(--surface\)/);
  assert.match(card, /width:\s*min\(400px, 100%\)/);
  assert.match(/\.profile-gate\s*\{([^}]*)\}/.exec(css)[1], /background:\s*var\(--canvas\)/);
});

test('hero tokens', () => {
  const css = readCss();
  const dark = tokenBlock(css, ':root[data-theme="dark"]');
  const light = tokenBlock(css, ':root');
  assert.equal(dark.get('--hero-from'), '#FFFFFF');
  assert.equal(dark.get('--hero-to'), '#9B9B9B');
  assert.equal(light.get('--hero-from'), '#000000');
  assert.equal(light.get('--hero-to'), '#666666');
});

test('the hero figure is 60px with the gradient, 36px on phones', () => {
  const css = readCss();
  const rule = ruleBody(css, '#home-networth-value');
  assert.match(rule, /font-size:\s*60px/);
  assert.match(rule, /line-height:\s*1;/);
  assert.match(rule, /font-weight:\s*600/);
  assert.match(rule, /letter-spacing:\s*-0?\.035em/);
  assert.match(rule, /linear-gradient\(90deg, var\(--hero-from\), var\(--hero-to\)\)/);
  assert.match(rule, /background-clip:\s*text/);
  assert.match(rule, /color:\s*transparent/);
  assert.match(rule, /overflow-wrap:\s*anywhere/);
  const phone = /@media \(max-width: 899px\)\s*\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(phone, 'no max-width: 899px media block');
  assert.match(ruleBody(phone[1], '#home-networth-value'), /font-size:\s*36px/);
  const forced = /@media \(forced-colors: active\)\s*\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(forced, 'no forced-colors block');
  const forcedRule = ruleBody(forced[1], '#home-networth-value');
  assert.match(forcedRule, /color:\s*CanvasText/);
  assert.match(forcedRule, /background:\s*none/);
  assert.match(ruleBody(css, '#home-networth-value:empty'), /display:\s*none/);
});

test('Home is lists and plain figures, not cards', () => {
  const html = readHtml();
  const home = html.slice(html.indexOf('data-page="home"'), html.indexOf('data-page="portfolio"'));
  assert.doesNotMatch(home, /class="panel[ "]/);
  assert.doesNotMatch(home, /home-card/);
  assert.doesNotMatch(home, /→/);
  assert.match(home, /ph-caret-right/);
  const sprite = fs.readFileSync(path.join(FRONTEND, 'icons.svg'), 'utf8');
  assert.match(sprite, /<symbol id="ph-caret-right"[^>]*fill="currentColor"/);
  const css = readCss();
  const strip = ruleBody(css, '.kpi-strip');
  assert.match(strip, /background:\s*var\(--surface\)/);
  assert.match(strip, /border-radius:\s*(12px|var\(--radius-lg\))/);
  assert.match(strip, /padding:\s*24px/);
  assert.doesNotMatch(strip, /border:/);
  const card = ruleBody(css, '.kpi-card');
  assert.doesNotMatch(card, /border:|background:|box-shadow:/);
  assert.match(ruleBody(css, '.kpi-value'), /font-size:\s*24px/);
  assert.match(ruleBody(css, '.kpi-label'), /font-size:\s*14px/);
  assert.match(ruleBody(css, '.home-composer'), /background:\s*var\(--surface\)/);
  assert.doesNotMatch(ruleBody(css, '.home-composer'), /border:/);
  assert.match(ruleBody(css, '.home-composer:focus-within'), /var\(--accent\)/);
});

test('pages.js Home and Holdings templates have no arrow glyphs', () => {
  const js = readJs('pages.js');
  for (const fn of ['kpiCard', 'allocationCard', 'topHoldingsList', 'insightList', 'renderHomeGoals', 'renderHome', 'sourceTabs', 'holdingsRows']) {
    const start = js.search(new RegExp(`(?:async )?function ${fn}\\(`));
    assert.ok(start >= 0, fn);
    const next = js.indexOf('\nfunction ', start + 10);
    const body = js.slice(start, next < 0 ? undefined : next);
    assert.doesNotMatch(body, /→/, fn);
  }
  assert.doesNotMatch(readHtml(), /icons\.svg\?v=2/);
});

test('Home list rows highlight on hover', () => {
  const css = readCss();
  for (const sel of ['.alloc-list li:hover', '.holding-list li:hover', '.change-list li:hover']) {
    assert.match(css, new RegExp(`${sel.replace(/[.:]/g, '\\$&')}[^{]*\\{[^}]*background:\\s*var\\(--surface-2\\)`), sel);
  }
});

test('the Home markup balances its divs and sections', () => {
  const html = readHtml();
  const home = html.slice(html.indexOf('data-page="home"'), html.indexOf('data-page="portfolio"'));
  const count = (text, re) => (text.match(re) || []).length;
  assert.equal(count(home, /<div[\s>]/g), count(home, /<\/div>/g), 'div');
  // The slice starts inside Home's opening <section> tag and ends after Portfolio's, so the counts match.
  assert.equal(count(home, /<section[\s>]/g), count(home, /<\/section>/g), 'section');
});

test('the page view region balances its divs and sections', () => {
  const html = readHtml();
  const start = html.indexOf('<div id="page-view"');
  // From #page-view to its own closing </div>, just before the </section> that ends the chat area.
  const end = html.lastIndexOf('</div>', html.indexOf('<!-- ── Right rail')) + '</div>'.length;
  const region = html.slice(start, end);
  const count = (re) => (region.match(re) || []).length;
  assert.equal(count(/<div[\s>]/g), count(/<\/div>/g), 'div');
  assert.equal(count(/<section[\s>]/g), count(/<\/section>/g), 'section');
});

test('the Home entrance animates the figure, the line, the area and the KPI row', () => {
  const css = readCss();
  const play = (sel) => ruleBody(css, `[data-entrance="play"] ${sel}`);
  assert.match(play('#home-networth-value'), /animation:[^;]*entrance-in[^;]*var\(--ease\)/);
  assert.match(play('#home-stats'), /animation:[^;]*entrance-in[^;]*var\(--ease\)[^;]*650ms/);
  assert.match(play('.history-line-draw'), /animation:[^;]*line-draw[^;]*900ms var\(--ease\) 150ms/);
  assert.match(play('.history-area-in'), /animation:[^;]*area-in[^;]*700ms var\(--ease\)/);
  const kf = keyframes(css.replace(reducedMotionBlock(css), ''));
  assert.match(kf.get('line-draw'), /stroke-dashoffset:\s*1[\s\S]*stroke-dashoffset:\s*0/);
  assert.match(kf.get('entrance-in'), /translateY\(8px\)/);
  // line-draw is the one keyframe allowed to animate something other than transform and opacity.
  for (const [name, body] of kf) {
    if (name === 'line-draw') continue;
    assert.doesNotMatch(body, /stroke-dashoffset|(?<![-\w])(width|height|top|left|margin|background)\s*:/, name);
  }
  const block = reducedMotionBlock(css);
  const inside = keyframes(block);
  for (const name of ['entrance-in', 'area-in']) {
    assert.ok(inside.has(name), name);
    assert.doesNotMatch(inside.get(name), /transform/, name);
  }
  assert.match(block, /\.history-line-draw\s*\{[^}]*animation:\s*none[^}]*stroke-dasharray:\s*none/);
});

test('renderHome claims the entrance once and never animates a figure in script', () => {
  const js = readJs('pages.js');
  const start = js.indexOf('async function renderHome()');
  const body = js.slice(start, js.indexOf('\nfunction homeKpis', start));
  assert.equal((body.match(/claimEntrance\(/g) || []).length, 1);
  assert.doesNotMatch(body, /setInterval|requestAnimationFrame/);
  assert.match(body, /removeAttribute\('data-entrance'\)/);
  // The empty, private and error branches each clear the attribute before returning.
  const branches = body.split("homePage.removeAttribute('data-entrance');").length - 1;
  assert.ok(branches >= 2, 'the not-connected/private and the no-snapshot/error branches clear data-entrance');
  assert.match(js, /addEventListener\('ca:route'[\s\S]{0,300}clearTimeout\(entranceTimer\)[\s\S]{0,120}removeAttribute\('data-entrance'\)/);
  assert.match(body, /drawHistoryLine/);
  assert.match(js, /\.scatterlayer \.js-line/);
  assert.match(js, /history-line-draw/);
  assert.match(js, /history-area-in/);
});

test('Home KPI placeholders stay hidden while loading, so nothing flashes before the entrance', () => {
  const css = readCss();
  assert.match(ruleBody(css, '#home-stats.is-loading'), /visibility:\s*hidden/);
  const js = readJs('pages.js');
  const start = js.indexOf('async function renderHome()');
  const body = js.slice(start, js.indexOf('\nfunction homeKpis', start));
  assert.match(body, /classList\.add\('is-loading'\)/);
  assert.match(body, /classList\.remove\('is-loading'\)/);
});

test('prompt row icon tiles use --surface-2, so they show on the light canvas', () => {
  assert.match(ruleBody(readCss(), '.prompt-row-icon'), /background:\s*var\(--surface-2\)/);
});

test('prompt row: the trailing caret rule does not reach the icon tile', () => {
  const css = readCss();
  assert.match(css, /\.prompt-row > \.icon:last-child\s*\{/);
  assert.doesNotMatch(css, /\.prompt-row \.icon:last-child/);
});

test('print: the hero figure is plain black text, not transparent clipped text', () => {
  const css = readCss();
  const prints = [...css.matchAll(/@media print\s*\{([\s\S]*?)\n\}/g)].map((m) => m[1]).join('\n');
  const rule = prints.match(/body #home-networth-value\s*\{([^}]*)\}/);
  assert.ok(rule, 'a print rule for the hero figure');
  assert.match(rule[1], /background:\s*none/);
  assert.match(rule[1], /color:\s*#000/);
});

test('hidden Home stats and focus rings do not rely on colour alone', () => {
  const css = readCss();
  assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  assert.match(ruleBody(css, '#home-stats.is-loading'), /visibility:\s*hidden/);
  assert.match(ruleBody(css, '.home-composer:focus-within'), /outline:\s*2px solid var\(--accent\)/);
});

test('follow-up chips fill backwards so a disabled chip keeps its opacity', () => {
  assert.match(ruleBody(readCss(), '.answer-follow-ups .chip'), /animation:[^;]*backwards/);
});

test('dead Home and tone rules are gone', () => {
  assert.doesNotMatch(readCss(), /\.figure-xl|\.tone-(green|blue|amber|violet)\b/);
});

test('Holdings: sliding tab underline, table on the surface, sticky header', () => {
  const css = readCss();
  assert.match(ruleBody(css, '.source-tabs'), /position:\s*relative/);
  const after = ruleBody(css.replace(reducedMotionBlock(css), ''), '.source-tabs::after');
  assert.match(after, /height:\s*2px/);
  assert.match(after, /background:\s*var\(--accent\)/);
  assert.match(after, /width:\s*100px/);
  assert.match(after, /transform:\s*translateX\(var\(--tab-x,\s*0(px)?\)\)\s*scaleX\(/);
  assert.match(after, /transition:\s*transform var\(--dur-move\) var\(--ease\)/);
  assert.doesNotMatch(ruleBody(css, '.source-tab[aria-pressed="true"]'), /border-bottom-color/);
  const panel = ruleBody(css, '.holdings-panel');
  assert.match(panel, /background:\s*var\(--surface\)/);
  assert.doesNotMatch(panel, /border\s*:/);
  assert.doesNotMatch(panel, /overflow:\s*hidden/);
  assert.match(panel, /overflow:\s*clip/);
  const th = ruleBody(css, '.holdings-table thead th');
  assert.match(th, /position:\s*sticky/);
  assert.match(th, /top:\s*calc\(-1 \* var\(--page-pad-top/);
  assert.match(ruleBody(css, '#page-view'), /--page-pad-top:\s*28px[^}]*padding:\s*var\(--page-pad-top\)/);
  assert.match(ruleBody(css, '.source-tabs.is-placing::after'), /transition:\s*none/);
  assert.match(th, /background:\s*var\(--surface(-2)?\)/);
  assert.match(ruleBody(css, '.holdings-table tbody tr:hover'), /var\(--surface-2\)/);
  // Scrolling sideways is only for pages under 900px (a container query); above it sticky needs no scroll ancestor.
  assert.doesNotMatch(css, /^\.holdings-scroll\s*\{/m);
  assert.match(css, /@container \(max-width: 900px\) \{\s*\.holdings-scroll \{ overflow-x: auto; \}/);
  assert.match(reducedMotionBlock(css), /\.source-tabs::after\s*\{[^}]*transition:\s*none/);
  assert.match(css, /\.holdings-table tbody tr\s*\{[^}]*grid-template-areas/);
  assert.doesNotMatch(readJs('pages.js'), /→/);
});

test('placeTabIndicator carries the old underline across the rebuild and reruns on resize and logo load', () => {
  const js = readJs('pages.js');
  assert.match(js, /function placeTabIndicator\(/);
  assert.match(js, /tabIndicator\(/);
  assert.match(js, /--tab-x/);
  assert.match(js, /--tab-w/);
  assert.match(js, /addEventListener\('resize'/);
  assert.match(js, /addEventListener\('load'/);
  assert.match(js, /document\.fonts\?\.ready/);
  assert.match(js, /function snapTabIndicator/);
  assert.match(js, /target\.w > 0/);
});

test('Ask AI: 720px column, answers as text on the canvas, questions as quiet blocks', () => {
  const css = readCss();
  assert.match(css, /#chat-area\s*\{\s*--chat-w:\s*720px;/);
  const body = ruleBody(css, '.message.assistant .message-body');
  assert.match(body, /max-width:\s*65ch/);
  assert.doesNotMatch(body, /background|border/);
  const lead = ruleBody(css, '.message.assistant .message-body > p:first-child');
  assert.match(lead, /font-size:\s*18px/);
  assert.match(lead, /line-height:\s*28px/);
  assert.match(lead, /font-weight:\s*500/);
  const question = ruleBody(css, '.message.user .message-body');
  assert.match(question, /background:\s*var\(--surface\)/);
  assert.match(question, /border-radius:\s*12px/);
  assert.match(question, /padding:\s*12px 16px/);
  assert.doesNotMatch(question, /border:|border-color/);
  assert.doesNotMatch(css, /:root\[data-theme="dark"\] \.message\.user \.message-body/);
});

test('Ask AI: a new answer rises, follow-up chips arrive staggered', () => {
  const css = readCss();
  assert.match(ruleBody(css, '.message'), /animation:\s*rise-in var\(--dur-move\) var\(--ease\)/);
  assert.match(/@keyframes rise-in\s*\{\s*from\s*\{[^}]*translateY\(8px\)/.exec(css)?.[0] || '', /translateY\(8px\)/);
  const chip = ruleBody(css, '.answer-follow-ups .chip');
  assert.match(chip, /animation:\s*rise-in var\(--dur-move\) var\(--ease\) backwards/);
  assert.match(chip, /var\(--chip-delay/);
  const js = readJs('app.js');
  const fn = /function placeFollowUps[\s\S]*?\n\}\n/.exec(js)[0];
  assert.match(fn, /staggerDelays/);
  assert.match(fn, /--chip-delay/);
});

test('Ask AI: assistant answers have no avatar tile and the empty state has no illustration', () => {
  const js = readJs('app.js');
  assert.doesNotMatch(js, /message-avatar/);
  assert.doesNotMatch(readCss(), /\.message-avatar|\.hero-art|\.coins|\.hero-tag/);
  const html = readHtml();
  assert.doesNotMatch(html, /class="hero-art"|class="coins"/);
  assert.doesNotMatch(html.slice(html.indexOf('id="empty-state"'), html.indexOf('id="messages"')), /→|tone-(green|blue|amber|violet)/);
  assert.match(html.slice(html.indexOf('id="empty-state"'), html.indexOf('id="messages"')), /ph-caret-right/);
});

test('Ask AI: the composer floats, the input area has no divider, the dots are 6px', () => {
  const css = readCss();
  const composer = ruleBody(css, '.composer');
  assert.match(composer, /border-radius:\s*16px/);
  assert.match(composer, /background:\s*var\(--surface\)/);
  assert.match(composer, /box-shadow:/);
  assert.doesNotMatch(ruleBody(css, '#input-area'), /border-top/);
  const dot = ruleBody(css, '.typing-indicator span');
  assert.match(dot, /width:\s*6px/);
  assert.match(dot, /height:\s*6px/);
  assert.match(dot, /var\(--text-muted\)/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)[\s\S]*@keyframes typing-bounce/);
  const head = ruleBody(css, '.chat-head-title');
  assert.match(head, /font-size:\s*24px/);
  assert.match(ruleBody(css, '.chat-head-sub'), /font-size:\s*14px/);
});

test('the Ask AI rail has no card borders or dividers', () => {
  const css = readCss();
  const html = readHtml();
  const js = readJs('rail.js');
  assert.doesNotMatch(css, /\.rail-section \+ \.rail-section\s*\{[^}]*border/);
  assert.match(ruleBody(css, '#right-rail'), /border-left:\s*0/);
  assert.match(ruleBody(css, '#right-rail'), /background:\s*transparent/);
  assert.doesNotMatch(css, /#right-rail\s*\{[^}]*border-left-color/);
  assert.match(ruleBody(css, '#right-rail'), /gap:\s*40px/);
  const card = ruleBody(css, '.rail-card');
  assert.match(card, /background:\s*var\(--surface\)/);
  assert.match(card, /border-radius:\s*12px/);
  assert.doesNotMatch(card, /border:|box-shadow/);
  const head = ruleBody(css, '.rail-head h2');
  assert.match(head, /font-size:\s*14px/);
  assert.match(head, /line-height:\s*20px/);
  assert.match(head, /font-weight:\s*600/);
  assert.match(ruleBody(css, '.rail-kicker'), /font-size:\s*14px[^}]*font-weight:\s*600|font-weight:\s*600[^}]*font-size:\s*14px/);
  assert.match(ruleBody(css, '.rail-chat:hover'), /background:\s*var\(--surface-2\)/);
  assert.match(ruleBody(css, '#right-rail .rail-figure'), /font-size:\s*24px[^}]*line-height:\s*32px[^}]*font-weight:\s*600/);
  assert.match(ruleBody(css, '.source-dot'), /background:\s*var\(--tone/);
  assert.match(ruleBody(css, '.source-row.is-good'), /--tone:\s*var\(--accent\)/);
  assert.doesNotMatch(js, /→/);
  const rail = html.slice(html.indexOf('<aside id="right-rail"'));
  assert.doesNotMatch(rail.slice(0, rail.indexOf('</aside>')), /→/);
  assert.match(rail, /class="rail-section rail-card"[^>]*aria-labelledby="rail-portfolio-title"/);
  assert.match(js, /rail-chat-day/);
});

// Every rule (selector list -> body) in `css`, flattened through at-rule blocks.
function rulesWithSelector(css, selector) {
  const found = [];
  for (const m of css.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    const selectors = m[1].replace(/\/\*[\s\S]*?\*\//g, '').split(',').map((s) => s.trim());
    if (selectors.includes(selector)) found.push(m[2]);
  }
  return found;
}

// The contents of each `@container (max-width: 640px)` block, braces matched.
function containerBlocks(css, query) {
  const blocks = [];
  let from = 0;
  for (;;) {
    const start = css.indexOf(query, from);
    if (start < 0) return blocks;
    const open = css.indexOf('{', start);
    let depth = 1;
    let i = open + 1;
    for (; depth > 0 && i < css.length; i += 1) {
      if (css[i] === '{') depth += 1;
      else if (css[i] === '}') depth -= 1;
    }
    blocks.push(css.slice(open + 1, i - 1));
    from = i;
  }
}

test('page kit: sections sit on the canvas with Home\'s heading size', () => {
  const css = readCss();
  const section = ruleBody(css, '.page-section');
  assert.match(section, /display:\s*grid/);
  assert.match(section, /gap:\s*16px/);
  assert.doesNotMatch(section, /background|border|padding/);
  const titles = rulesWithSelector(css, '.page-section .card-title');
  assert.ok(titles.some((body) => /font-size:\s*20px/.test(body) && /line-height:\s*28px/.test(body)), 'no 20px/28px rule for .page-section .card-title');
});

test('page kit: surface and table blocks have no border', () => {
  const css = readCss();
  for (const selector of ['.surface-block', '.table-surface']) {
    const body = ruleBody(css, selector);
    assert.match(body, /background:\s*var\(--surface\)/, selector);
    assert.match(body, /border-radius:\s*12px/, selector);
    assert.doesNotMatch(body, /border:/, selector);
  }
});

test('tables read at 14px with quiet headers and surface hover', () => {
  const css = readCss();
  assert.match(ruleBody(css, '.data-table th, .data-table td'), /padding:\s*12px;/);
  assert.match(ruleBody(css, '.data-table th, .data-table td'), /border-bottom:\s*1px solid var\(--line\)/);
  assert.match(ruleBody(css, '.data-table'), /font-size:\s*14px/);
  const th = ruleBody(css, '.data-table th');
  assert.match(th, /font-size:\s*12px/);
  assert.match(th, /font-weight:\s*500/);
  assert.match(ruleBody(css, '.data-table tbody tr:hover'), /background:\s*var\(--surface-2\)/);
  assert.doesNotMatch(css, /#activity-body \.data-table\s*\{[^}]*font-size:\s*12px/);
});

test('phone tables stay cards on the canvas', () => {
  const block = containerBlocks(readCss(), '@container (max-width: 640px)').find((b) => b.includes('.card-table tbody tr'));
  assert.ok(block, 'no 640px container block for card tables');
  const row = ruleBody(block, '.card-table tbody tr');
  assert.match(row, /background:\s*var\(--surface\)/);
  assert.match(row, /border-radius:\s*12px/);
  const surface = ruleBody(block, '.table-surface');
  assert.match(surface, /background:\s*none/);
  assert.match(surface, /padding:\s*0/);
});

test('segmented controls nest their radii', () => {
  const css = readCss();
  const seg = ruleBody(css, '.seg');
  assert.match(seg, /border-radius:\s*8px/);
  assert.match(seg, /padding:\s*2px/);
  assert.match(ruleBody(css, '.seg button'), /border-radius:\s*6px/);
});

test('the 404 section is a column on the canvas with one action', () => {
  const css = readCss();
  assert.doesNotMatch(ruleBody(css, '.not-found'), /border:/);
  const code = ruleBody(css, '.not-found-code');
  assert.match(code, /font-size:\s*60px/);
  assert.match(code, /line-height:\s*1;/);
  assert.match(code, /color:\s*var\(--text-muted\)/);
  const html = readHtml();
  const start = html.indexOf('data-page="not-found"');
  const section = html.slice(start, html.indexOf('</section>', start));
  const links = [...section.matchAll(/<a\b[^>]*>/g)];
  assert.equal(links.length, 1);
  assert.match(links[0][0], /class="[^"]*rail-button/);
  assert.doesNotMatch(section, /→/);
});

test('batch 3 markup never uses the page kit', () => {
  const html = readHtml();
  const slice = (from, to) => html.slice(html.indexOf(from), html.indexOf(to));
  assert.doesNotMatch(slice('data-page="home"', 'data-page="portfolio"'), /page-section/);
  assert.doesNotMatch(slice('data-page="holdings"', 'data-page="returns"'), /page-section/);
  const pages = readJs('pages.js');
  for (const name of ['renderHome', 'renderHoldings']) {
    const start = pages.indexOf(`function ${name}(`);
    assert.ok(start >= 0, name);
    const rest = pages.slice(start + 1);
    const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')].filter((i) => i >= 0);
    assert.doesNotMatch(rest.slice(0, Math.min(...ends)), /page-section/, name);
  }
});

// The source of `function name(` (or `async function name(`) up to the next top-level function.
function functionSource(js, name) {
  const start = js.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `no function ${name}`);
  const rest = js.slice(start + 1);
  const ends = [rest.indexOf('\nfunction '), rest.indexOf('\nasync function ')].filter((i) => i >= 0);
  return rest.slice(0, Math.min(...ends));
}

test('Portfolio, Reports and Markets sections are page sections', () => {
  const pages = readJs('pages.js');
  assert.match(functionSource(pages, 'pageSection'), /<section class="page-section/);
  assert.match(functionSource(pages, 'tableSurface'), /class="[^"]*table-surface/);
  for (const name of ['renderPortfolio', 'renderReports', 'renderMarkets', 'downloadsPanel']) {
    const source = functionSource(pages, name);
    assert.match(source, /pageSection\(|page-section/, name);
    assert.doesNotMatch(source, /class="panel[\s"]|\bpanel\(|reportCard\(|home-card/, name);
  }
  for (const name of ['renderPortfolio', 'renderReports', 'renderMarkets', 'moverTable', 'holdingsTable']) {
    const source = functionSource(pages, name);
    assert.doesNotMatch(source, /class="panel[\s"]|home-card/, name);
    for (const m of source.matchAll(/class="data-table/g)) {
      assert.match(source.slice(Math.max(0, m.index - 120), m.index), /table-surface|tableSurface\(/, `${name}: a data-table outside .table-surface`);
    }
  }
});

test('Portfolio: charts on the canvas, concentration as ruled rows', () => {
  const css = readCss();
  const chart = rulesWithSelector(css, '.page-section .data-chart');
  assert.ok(chart.some((body) => /background:\s*(transparent|none)/.test(body) && /padding:\s*0/.test(body)), 'charts in a page section keep a surface');
  const row = ruleBody(css, '#portfolio-body .fact-list div');
  assert.match(row, /font-size:\s*14px/);
  assert.match(row, /border-bottom:\s*1px solid var\(--line\)/);
  const block = containerBlocks(css, '@container (max-width: 640px)').find((b) => b.includes('.card-table tbody tr'));
  assert.match(ruleBody(block, '.table-surface:not(:has(> .card-table))'), /background:\s*var\(--surface\)/);
});

test('Markets: forms on the surface, live questions end in a caret', () => {
  const html = readHtml();
  const start = html.indexOf('data-page="markets"');
  const markets = html.slice(start, html.indexOf('data-page="goals"'));
  assert.doesNotMatch(markets, /→/);
  assert.doesNotMatch(markets, /class="panel[\s"]/);
  assert.equal(markets.match(/ph-caret-right/g)?.length, 4);
  for (const id of ['stock-chart-title', 'mf-category-title']) {
    assert.match(markets, new RegExp(`<section class="[^"]*surface-block[^"]*" aria-labelledby="${id}"`), id);
  }
  assert.match(markets, /<section class="[^"]*page-section[^"]*" aria-labelledby="markets-live-title"/);
  for (const m of markets.matchAll(/<span class="prompt-row-arrow"[^>]*>([\s\S]*?)<\/span>/g)) {
    assert.match(m[1], /^<svg class="icon" aria-hidden="true"><use href="\/icons\.svg\?v=\d+#ph-caret-right"><\/use><\/svg>$/);
  }
});

test('Reports: downloads are plain buttons', () => {
  const css = readCss();
  assert.ok(rulesWithSelector(css, '.download-actions').length, 'no .download-actions rule');
  for (const body of rulesWithSelector(css, '.downloads-card')) assert.doesNotMatch(body, /border/);
});

// Declarations of every rule naming `selector`, as one string, so a border can be found wherever it is set.
const borderIn = (css, selector) => rulesWithSelector(css, selector).filter((body) => /(^|;)\s*border(-top|-left)?\s*:/.test(body));

test('Goals: cards are surfaces with no borders, drafts and behind goals marked without one', () => {
  const css = readCss();
  const card = ruleBody(css, '.goal-card');
  assert.match(card, /background:\s*var\(--surface\)/);
  assert.match(card, /border-radius:\s*12px/);
  assert.match(card, /padding:\s*24px/);
  assert.doesNotMatch(card, /(^|;)\s*border\s*:/);
  for (const m of css.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    if (/\.goal-card\.is-(behind|draft)/.test(m[1])) assert.doesNotMatch(m[2], /border/, m[1].trim());
  }
  assert.match(ruleBody(css, '.goal-ring-track'), /stroke-width:\s*6/);
  assert.match(ruleBody(css, '.goal-ring-fill'), /stroke-width:\s*6/);
  const label = ruleBody(css, '.goal-ring text');
  assert.match(label, /font-size:\s*14px/);
  assert.match(label, /font-weight:\s*600/);
  assert.match(ruleBody(css, '.goal-figures dd'), /font-size:\s*16px/);
});

test('Goals: the callout is not a left-border block', () => {
  const css = readCss();
  const callout = ruleBody(css, '.goal-callout');
  assert.doesNotMatch(callout, /border(-left)?\s*:/);
  assert.match(callout, /background:\s*var\(--surface\)/);
  assert.match(callout, /font-size:\s*14px/);
  const dot = ruleBody(css, '.goal-callout::before');
  assert.match(dot, /width:\s*8px/);
  assert.match(dot, /height:\s*8px/);
  assert.match(dot, /background:\s*var\(--warning\)/);
});

test('Goals: no inner borders', () => {
  const css = readCss();
  for (const selector of ['.goal-gap-options li', '.fund-item', '.verdict-chip', '.context-item', '.goal-section', '.goal-foot']) {
    assert.ok(rulesWithSelector(css, selector).length, `no rule for ${selector}`);
    assert.deepEqual(borderIn(css, selector), [], selector);
  }
  for (const [tone, soft, text] of [['allowed', 'green-soft', 'gain-text'], ['caution', 'amber-soft', 'warning-text'], ['unsuitable', 'red-soft', 'loss-text']]) {
    const body = ruleBody(css, `.verdict-chip.is-${tone}`);
    assert.match(body, new RegExp(`background:\\s*var\\(--${soft}\\)`), tone);
    assert.match(body, new RegExp(`color:\\s*var\\(--${text}\\)`), tone);
    assert.doesNotMatch(body, /border/, tone);
  }
  assert.match(ruleBody(css, '.goal-gap'), /background:\s*var\(--surface-2\)/);
  const option = ruleBody(css, '.goal-gap-options li');
  assert.match(option, /background:\s*var\(--surface-3\)/);
  assert.match(option, /border-radius:\s*8px/);
});

test('Goals: disclosures use a sprite caret and fade in', () => {
  const css = readCss();
  for (const m of css.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    if (m[1].includes('.goal-toggle')) assert.doesNotMatch(m[2], /content:\s*"▾"/, m[1].trim());
  }
  const card = functionSource(readJs('pages.js'), 'goalCard');
  const toggle = /<button class="goal-toggle"[^`]*?<\/button>/.exec(card);
  assert.ok(toggle, 'no goal-toggle button in goalCard');
  assert.match(toggle[0], /<svg class="icon" aria-hidden="true"><use href="\/icons\.svg\?v=\d+#ph-caret-down"><\/use><\/svg>/);
  assert.match(toggle[0], /data-goal-panel/);
  assert.match(toggle[0], /aria-expanded/);
  assert.match(card, /class="goal-panel"[^`]*hidden/);
  const open = ruleBody(css, '.goal-toggle[aria-expanded="true"] .icon');
  assert.match(open, /transform:\s*rotate\(180deg\)/);
  assert.match(open, /transition:\s*transform var\(--dur-move\) var\(--ease\)/);
  assert.doesNotMatch(ruleBody(css, '.goal-toggle'), /border\s*:\s*1px/);
  const panel = ruleBody(css, '.goal-panel:not([hidden])');
  const name = /animation:\s*([\w-]+)/.exec(panel)?.[1];
  assert.ok(name, 'the open panel has no animation');
  const reduced = keyframes(reducedMotionBlock(css));
  assert.ok(reduced.has(name), `${name} is not redefined for reduced motion`);
  assert.match(reduced.get(name), /opacity/);
  assert.doesNotMatch(reduced.get(name), /transform/);
});

test('Goals: reading text is 14px', () => {
  const css = readCss();
  assert.match(ruleBody(css, '.goal-panel'), /font-size:\s*14px/);
  assert.match(ruleBody(css, '.goal-checks'), /font-size:\s*14px/);
});

test('Goals: allocations and saved context are page sections', () => {
  const pages = readJs('pages.js');
  const investments = functionSource(pages, 'renderGoalInvestments');
  assert.match(investments, /goal-investments-body/);
  assert.match(investments, /pageSection\(|page-section/);
  assert.match(investments, /tableSurface\(|table-surface/);
  assert.match(investments, /alloc-table/);
  assert.doesNotMatch(investments, /\bpanel\(/);
  assert.match(functionSource(pages, 'renderSavedContext'), /saved-context-body/);
  const context = functionSource(pages, 'savedContextHtml');
  assert.match(context, /pageSection\(|page-section/);
  assert.doesNotMatch(context, /\bpanel\(/);
  const css = readCss();
  const item = ruleBody(css, '.context-item');
  assert.match(item, /background:\s*var\(--surface-2\)/);
  assert.match(item, /border-radius:\s*8px/);
});

const RETURNS_SOURCES = ['renderReturns', 'renderTax', 'benchmarkPanel', 'exemptionPanel', 'taxRowsTable', 'setOffBlock', 'leftOutBlock', 'planningCard', 'planningBlock'];
// One function's body only: up to its closing brace at the start of a line (renderTax is followed by listeners and CALCULATORS).
const ownSource = (js, name) => { const source = functionSource(js, name); return source.slice(0, source.indexOf('\n}\n') + 2); };

test('Returns: sections on the canvas, no strip nested in a surface', () => {
  const pages = readJs('pages.js');
  for (const name of ['renderReturns', 'renderTax']) {
    assert.match(functionSource(pages, name), /pageSection\(|page-section/, name);
  }
  for (const name of RETURNS_SOURCES) {
    const source = ownSource(pages, name);
    // Panels only through notice() (empty states); no report cards or surface blocks around a strip.
    assert.doesNotMatch(source, /class="panel[\s"]|\bpanel\(|reportCard\(|home-card|surface-block/, name);
    for (const m of source.matchAll(/class="data-table/g)) {
      assert.match(source.slice(Math.max(0, m.index - 120), m.index), /table-surface|tableSurface\(/, `${name}: a data-table outside .table-surface`);
    }
  }
  assert.match(readCss(), /#returns-body\s*\{[^}]*gap:\s*32px/);
});

test('Returns: the tab bar is an underline bar', () => {
  const pages = readJs('pages.js');
  const tabs = functionSource(pages, 'returnsTabs');
  assert.match(tabs, /class="returns-tabs"/);
  assert.match(tabs, /class="returns-tab"/);
  assert.match(tabs, /data-returns-tab/);
  assert.doesNotMatch(tabs, /\bseg\b/);
  const css = readCss();
  const tab = ruleBody(css, '.returns-tab');
  assert.match(tab, /font-size:\s*14px/);
  assert.match(tab, /border-bottom:\s*2px solid transparent/);
  assert.match(tab, /background:\s*transparent/);
  assert.doesNotMatch(tab, /transition|animation/);
  const selected = ruleBody(css, '.returns-tab[aria-selected="true"]');
  assert.match(selected, /border-bottom:\s*2px solid var\(--accent\)/);
  assert.doesNotMatch(selected, /background/);
  for (const m of css.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    if (m[1].includes('.returns-tab')) assert.doesNotMatch(m[2], /transform|animation/, m[1].trim());
  }
});

test('Tax: exemption meter and planning list are flat', () => {
  const css = readCss();
  const track = ruleBody(css, '#returns-body .exemption-meter');
  assert.match(track, /background:\s*var\(--surface-2\)/);
  assert.match(track, /border-radius:\s*9999px/);
  assert.match(track, /height:\s*12px/);
  assert.match(ruleBody(css, '#returns-body .exemption-meter span'), /background:\s*var\(--accent\)/);
  assert.match(functionSource(readJs('pages.js'), 'exemptionPanel'), /class="progress-bar exemption-meter"/);
  const item = ruleBody(css, '.planning-list li');
  assert.match(item, /background:\s*var\(--surface\)/);
  assert.match(item, /border-radius:\s*12px/);
  assert.deepEqual(borderIn(css, '.planning-list li'), []);
});

test('Calculators: inputs on a surface block, results on the canvas', () => {
  const pages = readJs('pages.js');
  const planning = functionSource(pages, 'renderPlanning');
  assert.match(planning, /class="surface-block[^"]*calc-inputs-card/);
  assert.doesNotMatch(planning, /class="panel[\s"]/);
  const output = functionSource(pages, 'renderCalcOutput');
  assert.doesNotMatch(output, /class="panel[\s"]|home-card/);
  assert.match(output, /pageSection\('Year by year'/);
  assert.match(output, /pageSection\('How it adds up', tableSurface\(/);
});

test('Calculators: fields read at 14px and sliders take the accent', () => {
  const css = readCss();
  const label = ruleBody(css, '.calc-field > span');
  assert.match(label, /font-size:\s*14px/);
  assert.match(label, /font-weight:\s*500/);
  assert.match(ruleBody(css, '.calc-field input[type="range"]'), /accent-color:\s*var\(--accent\)/);
  assert.match(readCss(), /#calc-body\s*\{[^}]*gap:\s*32px/);
});

// Visible border: any border declaration other than 0, none or transparent.
const visibleBorderIn = (css, selector) => borderIn(css, selector)
  .filter((body) => [...body.matchAll(/(?:^|;)\s*border(?:-top|-left)?\s*:\s*([^;]+)/g)].some((m) => !/^(0|none|0 none|[^;]*transparent)\s*$/.test(m[1].trim())));

test('Activity: rows and records are borderless surfaces', () => {
  const css = readCss();
  for (const selector of ['.activity-item', '.activity-skeleton', '#activity-body .evidence-card', '.run-card']) {
    const body = ruleBody(css, selector);
    assert.match(body, /border-radius:\s*12px/, selector);
    assert.deepEqual(visibleBorderIn(css, selector), [], selector);
  }
  for (const selector of ['.activity-item', '.activity-skeleton', '.run-card']) {
    assert.match(ruleBody(css, selector), /background:\s*var\(--surface\)/, selector);
  }
  for (const selector of ['#activity-body .evidence-card', '.run-card']) {
    assert.match(ruleBody(css, selector), /padding:\s*24px/, selector);
  }
  assert.match(ruleBody(css, '.activity-item:hover'), /background:\s*var\(--surface-2\)/);
  assert.match(ruleBody(css, '.skeleton-row span'), /animation:\s*skeleton-fade/);
  // Sections inside a record are separated by space, not rules; their summaries read at 16px.
  assert.deepEqual(visibleBorderIn(css, '#activity-body .evidence-section'), []);
  assert.match(ruleBody(css, '#activity-body .evidence-section'), /border-top:\s*0/);
  const summary = ruleBody(css, '#activity-body .evidence-section > summary');
  assert.match(summary, /font-size:\s*16px/);
  assert.match(summary, /line-height:\s*24px/);
  assert.match(summary, /font-weight:\s*600/);
  // The compact card under an answer keeps its own rules.
  assert.match(ruleBody(css, '.evidence-card'), /padding:\s*14px 16px/);
});

test('Activity: page-size reading text', () => {
  const css = readCss();
  for (const selector of ['#activity-body .evidence-summary', '.activity-outcome', '#activity-body .timeline-summary']) {
    const body = ruleBody(css, selector);
    assert.match(body, /font-size:\s*14px/, selector);
    assert.match(body, /line-height:\s*20px/, selector);
  }
  assert.match(ruleBody(css, '.activity-item .timeline-meta'), /font-size:\s*12px/);
  assert.doesNotMatch(css, /#activity-body \.timeline-summary\s*,[^{]*\{[^}]*font-size:\s*12px/);
});

test('Activity: lists are page sections', () => {
  const pages = readJs('pages.js');
  const list = functionSource(pages, 'drawActivityList');
  assert.match(list, /class="page-section activity-section"/);
  assert.match(readCss(), /#activity-body\s*\{[^}]*gap:\s*32px/);
  for (const name of ['drawActivityList', 'loadActivityDetail', 'drawActivityDetail', 'loadRunDetail', 'startInvestigation']) {
    assert.doesNotMatch(functionSource(pages, name), /class="panel[\s"]|home-card|→/, name);
  }
  assert.match(pages, /class="panel-link activity-back"/);
});

test('All chats: period groups are labelled lists on the canvas', () => {
  const pages = readJs('pages.js');
  const all = functionSource(pages, 'renderAllChats');
  assert.match(all, /'page-section chat-group-card'/);
  assert.doesNotMatch(all, /panel|home-card/);
  const css = readCss();
  for (const body of rulesWithSelector(css, '.chat-group-card')) assert.doesNotMatch(body, /background|padding/);
  const heading = ruleBody(css, '#all-chats .chat-group');
  assert.match(heading, /font-size:\s*14px/);
  assert.match(heading, /line-height:\s*20px/);
  assert.match(heading, /font-weight:\s*500/);
  assert.match(heading, /color:\s*var\(--text-soft\)/);
  assert.match(ruleBody(css, '#page-view .chat-select:hover'), /background:\s*var\(--surface-2\)/);
  assert.match(css, /#all-chats\s*\{[^}]*gap:\s*32px/);
});

const PUBLIC_TOKENS = ['--canvas', '--surface', '--surface-2', '--line', '--text', '--text-soft', '--text-muted',
  '--accent', '--accent-3', '--accent-text', '--accent-ink', '--loss-text', '--warning-text',
  '--red-soft', '--danger-border', '--shadow-lg'];

test('public pages carry the app\'s token values in both themes', () => {
  const app = readCss();
  const pub = readCss('public.css');
  const appLight = tokenBlock(app, ':root');
  const appDark = tokenBlock(app, ':root[data-theme="dark"]');
  const pubLight = tokenBlock(pub, ':root');
  const pubDark = mediaTokenBlock(pub, 'prefers-color-scheme: dark', ':root');
  for (const name of PUBLIC_TOKENS) {
    assert.ok(appLight.has(name), `app.css light has ${name}`);
    assert.equal(pubLight.get(name), appLight.get(name), `light ${name}`);
    assert.equal(pubDark.get(name), appDark.get(name), `dark ${name}`);
  }
});

test('public pages: Geist, flat sections, no shadows', () => {
  const css = readCss('public.css');
  const body = ruleBody(css, 'body');
  assert.match(body, /background:\s*var\(--canvas\)/);
  const family = /font(?:-family)?:[^;]*?('Geist'[^;]*);/.exec(body);
  assert.ok(family, 'body uses Geist');
  assert.doesNotMatch(body, /font(?:-family)?:[^;]*(Inter|-apple-system)[^;]*'Geist'/);
  assert.doesNotMatch(css, /Inter|-apple-system/);
  for (const sectionBody of rulesWithSelector(css, 'section')) {
    assert.doesNotMatch(sectionBody, /(^|[;\s])border:/);
    assert.doesNotMatch(sectionBody, /background/);
  }
  const plan = ruleBody(css, '.plan');
  assert.match(plan, /background:\s*var\(--surface\)/);
  assert.match(plan, /border-radius:\s*12px/);
  const featured = ruleBody(css, '.plan-featured');
  assert.doesNotMatch(featured, /box-shadow/);
  assert.match(featured, /border:\s*1px solid var\(--accent\)/);
  assert.doesNotMatch(css, /box-shadow/);
  // The price is a <p class="plan-price">: no rule that reaches a plan's paragraphs may reset its size.
  assert.match(ruleBody(css, '.plan-price'), /font-size:\s*30px/);
  for (const m of css.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    const selectors = m[1].replace(/\/\*[\s\S]*?\*\//g, '').split(',').map((sel) => sel.trim());
    for (const sel of selectors) {
      if (/\.plan\b(?!-)[^,]*\sp(?![\w-])/.test(sel) && /font-size|line-height|(^|[;\s])font:/.test(m[2])) {
        assert.match(sel, /:not\(\.plan-price\)/, `${sel} would resize the plan price`);
      }
    }
  }
  assert.match(css, /:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/);
  const code = ruleBody(css, '.public-code');
  assert.match(code, /font-size:\s*60px/);
  assert.match(code, /color:\s*var\(--text-muted\)/);
});

test('Activity records: no arrow glyphs and no browser disclosure triangles', () => {
  const css = readCss();
  assert.doesNotMatch(readJs('pages.js'), /← All activity/);
  assert.match(readJs('pages.js'), /#ph-caret-left"><\/use><\/svg>All activity/);
  assert.match(css, /\.evidence-section > summary::-webkit-details-marker, \.timeline-details > summary::-webkit-details-marker \{ display: none; \}/);
  assert.match(css, /\.evidence-section > summary, \.timeline-details > summary \{[^}]*list-style: none;/);
});

test('public page footers space their links apart', () => {
  assert.match(readCss('public.css'), /\.public-link \+ \.public-link \{ margin-left: 12px; \}/);
});
