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
  for (const name of ['app.js', 'pages.js', 'demo.js', 'shell.js']) {
    assert.doesNotMatch(readJs(name), /window\.confirm\(|(^|[^\w.])confirm\(|\balert\(/m, name);
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
  assert.match(body, /drawHistoryLine/);
  assert.match(js, /\.scatterlayer \.js-line/);
  assert.match(js, /history-line-draw/);
  assert.match(js, /history-area-in/);
});
