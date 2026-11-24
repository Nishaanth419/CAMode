# Redesign batch 1: foundations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CA Mode's visual foundations (colour, type, radius, shadow, motion, states, icons, confirmations, chart theming) with the approved system, keeping every page's layout and behaviour.

**Architecture:** Token values in `frontend/css/app.css` are rewritten in place, keeping today's token *names* so the 2,700 lines of rules that use them change look without changing structure. A Node "design lint" test (`tests/js/design.test.js`) reads the CSS, HTML and JS as text and fails on anything outside the system; every task adds its assertions there first. Confirmations become one in-app dialog driven by copy in `ui.js`.

**Tech Stack:** Vanilla CSS and JS, `node --test`, pytest, Geist from Google Fonts, Phosphor icons (MIT) as an SVG sprite, Plotly 4.1.1 (unchanged).

**Spec:** `docs/superpowers/specs/2026-11-20-frontend-redesign-design.md` (approved; mockups at https://claude.ai/artifact/6Yfq8NUdeJe2wH4vauAXuX). Read the spec's "Design system" section before starting.

## Global Constraints

- Work on a branch from `main` in its own worktree; never touch the uncommitted chat-streaming edits on `fix/kite-mode-tools`.
- No framework, build step or new runtime dependency. No change to any API, figure or behaviour.
- Keep every `id`, `data-*` attribute and class that JS or tests read. Layout of every page stays as it is (the shell is batch 2).
- Dark surfaces only `#000000`, `#181818`, `#1F1F1F`, `#272727`, `#313131`. Accent `#C2A66B` (light `#8A6F35`). Gain `#5FB38A` / light `#2E7D57`; loss `#E07A6A` / light `#B5483A`; warning `#D9A54A` / light `#9A6A12`.
- Text dark `#FFFFFF` / `#A3A3A3` / `#8A8A8A`; light `#0A0A0A` / `#4A4A4A` / `#6B6B6B`. Light surfaces `#FFFFFF`, `#F6F6F5`, `#EFEFED`, `#E4E4E1` (line `#E4E4E1`).
- Font: Geist 400/500/600/700 only; Geist Mono 400/500 only for code and ids. Font sizes only 12, 14, 16, 18, 20, 24, 30, 36, 48, 60px.
- Motion: `--ease: cubic-bezier(.22, 1, .36, 1)`, `--dur-move: 500ms`, `--dur-fade: 300ms`; nothing under 200ms; never `ease`, `linear` or `ease-in-out` keywords in transitions.
- No `text-transform: uppercase`, no `font-style: italic`, no `font-weight` above 700, no background gradients (the only `gradient(` allowed is the net-worth text gradient added in batch 3).
- No inline handlers or inline styles in `index.html` (CSP). Bump every `?v=` in `index.html` and `CACHE_NAME` in `sw.js`.
- Copy: sentence case, buttons name their action ("Delete chat"), no exclamation marks.
- Every check passes at the end: `python -m compileall -q app tests && python -m pytest -q`, `node --test tests/js/*.test.js`, `for f in frontend/js/*.js; do node --check "$f"; done`.

## Review Focus

1. **Light theme** (`data-theme="light"` and system light): every token has a light value; text pairs pass AA. Test: Task 1 asserts both theme blocks define the same set of custom properties.
2. **A confirmation the person cancels** (Esc, Cancel, backdrop click): nothing is deleted and focus returns to the button that opened it. Test: Task 5 browser step presses Esc on "Delete chat" and checks the chat is still listed.
3. **Reduced motion:** with `prefers-reduced-motion: reduce` no element moves; infinite loaders stop. Test: Task 4 lint asserts the reduced-motion block exists and sets `animation-iteration-count: 1`.
4. **Installed PWA after update:** the new `icons.svg` and fonts load offline from the cache. Test: Task 6 asserts `/icons.svg` is in `APP_SHELL` (extend `tests/js/sw.test.js`).
5. **Charts in both themes:** Plotly reads colours and the font from tokens at draw time, so a theme toggle redraws in the right colours. Test: Task 7 browser step toggles theme on Home and screenshots the chart.

---

### Task 1: Design lint test, colour tokens and Geist

**Files:**
- Create: `tests/js/design.test.js`
- Modify: `frontend/css/app.css:1-150` (both token blocks), `frontend/index.html:6,30` (theme-color, font link)

**Interfaces:**
- Produces: `tests/js/design.test.js` with helpers `readCss()`, `readHtml()`, `readJs(name)` and `tokenBlock(css, selector) -> Map<name, value>`; later tasks append tests to this file.
- Token names kept; new tokens added: `--line` (= `--border`), `--gain`, `--loss`, `--warning`, `--ease`, `--dur-move`, `--dur-fade`, `--grain`.

- [ ] **Step 1: Write the failing tests**

```js
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
  assert.deepEqual([...tokenBlock(css, ':root[data-theme="dark"]').keys()].sort(),
    [...tokenBlock(css, ':root').keys()].filter((k) => !/-(w|h)$|^--radius|^--control|^--sans|^--serif|^--mono|^--ease|^--dur/.test(k)).sort());
});
```

- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL (Inter found, `--canvas` is `#070a08`).

- [ ] **Step 3: Rewrite both token blocks.** Values from Global Constraints. Mappings for the old names: `--green` → gain, `--red`/`--error` → loss, `--amber` → warning, `--blue` → `#8FA6C9` (light `#4F6E99`), `--violet` → `#C9A0A0` (light `#A0645F`), each `-soft` = the same colour at 12% alpha; `--accent-2`, `--accent-3`, `--mint` = accent; `--accent-glow` 25%, `--accent-wash` 10%, `--accent-wash2` 5% of the accent; `--seg-active` = surface-3; `--side-*`: bg canvas, text/soft/muted = text tokens, border = line, hover surface-2, active surface-2, active text = text, active icon = accent. Series 1–8 dark: `#C2A66B #E8E2D4 #8FA6C9 #9DB59A #C9A0A0 #8C8C8C #5A5A5A #3A3A3A`; light: `#8A6F35 #C9BC9C #4F6E99 #5E7D5B #A0645F #8C8C8C #B5B5B5 #DADADA`. `--sans` and `--serif`: `'Geist', ui-sans-serif, system-ui, sans-serif`; `--mono`: `'Geist Mono', ui-monospace, Menlo, monospace`. `--grain`: an inline SVG `feTurbulence` noise as a `data:` URI (CSP allows `img-src data:`), drawn by `body::before` at 3% opacity, `pointer-events: none`. Font link: `family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap`. `theme-color` meta `#000000`. `body` gets `font-variant-numeric: tabular-nums`; `h1, h2, h3 { text-wrap: balance }`, `p { text-wrap: pretty }`.

- [ ] **Step 4:** Run `node --test tests/js/design.test.js`. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "style(frontend): approved colour tokens and Geist"`

### Task 2: Type scale

**Files:** Modify `frontend/css/app.css`, `frontend/css/public.css`, `frontend/css/admin.css`; Test `tests/js/design.test.js`

- [ ] **Step 1: Failing test**

```js
test('every font size is on the scale', () => {
  const allowed = new Set([12, 14, 16, 18, 20, 24, 30, 36, 48, 60]);
  for (const file of ['app.css', 'public.css', 'admin.css']) {
    for (const [, n] of readCss(file).matchAll(/font-size:\s*([\d.]+)px/g)) assert.ok(allowed.has(Number(n)), `${file}: ${n}px`);
  }
});
test('no uppercase, italic or weights above bold', () => {
  const css = readCss();
  assert.doesNotMatch(css, /text-transform:\s*uppercase/);
  assert.doesNotMatch(css, /font-style:\s*italic/);
  assert.doesNotMatch(css, /font-weight:\s*(800|900)/);
});
```

- [ ] **Step 2:** Run it. Expected: FAIL listing sizes such as `11.5px`.
- [ ] **Step 3:** Snap each size to the nearest step below (13/13.5 → 12, 15 → 14, 17 → 16, 22 → 20, 28 → 24, 31/32 → 30, 40/44 → 36, 56 → 48); sizes under 12 become 12. Where a rule also sets `line-height`, use the step's pair (12/16, 14/20, 16/24, 18/28, 20/28, 24/32, 30/36, 36/40, 48 and 60 at 1). `.line-axis-label` uses `var(--sans)`. Remove the uppercase and italic rules (keep weight or colour for emphasis); 800 → 700; negative tracking `-0.02em` on 30px and up; remove positive tracking that only served uppercase.
- [ ] **Step 4:** Run tests. Expected: PASS. Open the demo in the browser at 1440px and 390px; no text overflows its box (fix any by wrapping, never by shrinking below 12px).
- [ ] **Step 5: Commit** `style(frontend): snap type to the scale`

### Task 3: Radius, shadows, gradients and cards

**Files:** Modify `frontend/css/app.css`; Test `tests/js/design.test.js`

- [ ] **Step 1: Failing test**

```js
test('radii come from the scale', () => {
  const ok = /^(0|2px|4px|6px|8px|12px|16px|50%|9999px|inherit|var\(--radius-[a-z]+\))$/;
  for (const [, v] of readCss().matchAll(/border-radius:\s*([^;]+);/g)) for (const part of v.trim().split(/\s+/)) assert.match(part, ok, v);
});
test('no background gradients and no generic shadows', () => {
  const css = readCss();
  assert.equal((css.match(/gradient\(/g) || []).length, 0);
  assert.doesNotMatch(css, /box-shadow:\s*0 1px 3px rgba\(0,\s*0,\s*0/);
});
```

- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Tokens: `--radius-sm 6px`, `--radius-md 8px`, `--radius-kpi 12px`, `--radius-lg 12px`, `--radius-xl 16px`. Literals: 5px → 4px, 9px/10px → 8px, `99px`/`999px` → `9999px` only on avatars and toggle tracks, otherwise 6px. Shadows: `--shadow-sm: none`; `--shadow-md` dark `0 16px 40px rgba(0,0,0,.5)`, light `0 16px 40px rgba(10,10,10,.08)`; `--shadow-lg` dark `0 24px 64px rgba(0,0,0,.7)`, light `0 24px 64px rgba(10,10,10,.12)`. Remove `box-shadow` from in-page panels, KPI cards and chat rows (elevation only on dialogs, menus, the composer, toasts). Remove borders from in-page panels where the surface colour already separates them from the canvas; keep borders on inputs, tables and dialogs. Replace the 7 gradients: `.rail-skeleton` shimmer becomes the same opacity pulse as `.skeleton-row`; decorative washes become flat `--surface`.
- [ ] **Step 4:** Run tests. Expected: PASS. Screenshot Home, Holdings and Goals in both themes for the PR.
- [ ] **Step 5: Commit** `style(frontend): radius scale, flat surfaces, no gradients`

### Task 4: Motion and interaction states

**Files:** Modify `frontend/css/app.css`; Test `tests/js/design.test.js`

- [ ] **Step 1: Failing test**

```js
test('transitions use the motion tokens', () => {
  for (const [, v] of readCss().matchAll(/transition:\s*([^;]+);/g)) {
    assert.doesNotMatch(v, /\b(ease|linear|ease-in-out|ease-in|ease-out)\b/, v);
    for (const [, ms] of v.matchAll(/(\d+)ms/g)) assert.ok(Number(ms) >= 200, v);
  }
});
test('reduced motion stops movement and loops', () => {
  const block = readCss().split('@media (prefers-reduced-motion: reduce)')[1] || '';
  assert.match(block, /animation-iteration-count:\s*1/);
  assert.match(block, /transition-property:\s*opacity/);
});
test('keyboard focus is always visible', () => assert.match(readCss(), /:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/));
```

- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Every `transition` uses `var(--dur-fade) var(--ease)` for colour/opacity/background and `var(--dur-move) var(--ease)` for transform; animate only `transform` and `opacity` (convert any `width`/`height`/`top`/`left` transition, except the `::details-content` disclosure). Keyframes keep their names; their timing functions become `var(--ease)`; `typing-bounce` and `pulse-dot` keep looping. Add: `button:active:not(:disabled), .button:active { transform: scale(.98) }`; hover changes background only; `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }` (remove any `outline: none` without a replacement). Replace the single reduced-motion rule with one block: `*, *::before, *::after { animation-duration: 200ms !important; animation-iteration-count: 1 !important; transition-duration: 200ms !important; transition-property: opacity, color, background-color, border-color !important; scroll-behavior: auto !important }`. `html { scroll-behavior: smooth }` outside it.
- [ ] **Step 4:** Run tests. Expected: PASS. In the browser emulate reduced motion and open the profile dialog and a chat: nothing slides.
- [ ] **Step 5: Commit** `style(frontend): motion tokens, press and focus states`

### Task 5: In-app confirmations

**Files:**
- Modify: `frontend/js/ui.js` (add `CONFIRMS`, `confirmCopy`), `frontend/js/app.js:123,419,1030,1069,1115,1169,1693`, `frontend/js/pages.js:2007`, `frontend/js/demo.js:130`, `frontend/index.html` (one `<dialog id="confirm-dialog">`), `frontend/css/app.css`
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Produces: `ui.CONFIRMS` — frozen map of `{title, body, confirmLabel, danger}` keyed `deleteStatement`, `privateMode`, `newRecoveryCode`, `clearHistory`, `disconnectIndmoney`, `deleteChat`, `deleteGoal`; `ui.confirmCopy(key, values = {}) -> {title, body, confirmLabel, cancelLabel: 'Cancel', danger}` (fills `{name}` placeholders, escapes nothing: text goes in via `textContent`); throws for an unknown key.
- Produces (app.js): `confirmAction(key, values) -> Promise<boolean>` opening `#confirm-dialog` with `showModal()`, resolving `true` only on the confirm button; Esc, Cancel and a backdrop click resolve `false`; focus returns to the element focused before opening.

- [ ] **Step 1: Failing tests**

```js
test('every confirmation names its action and keeps today\'s meaning', () => {
  assert.deepEqual(ui.confirmCopy('deleteChat'), { title: 'Delete this chat?', body: 'The chat and its answers are removed from this profile.', confirmLabel: 'Delete chat', cancelLabel: 'Cancel', danger: true });
  assert.equal(ui.confirmCopy('deleteGoal', { name: 'House' }).title, 'Delete the goal "House"?');
  assert.equal(ui.confirmCopy('deleteGoal', { name: 'House' }).body, 'Decisions linked to it are kept.');
  assert.equal(ui.confirmCopy('disconnectIndmoney').confirmLabel, 'Disconnect INDmoney');
  assert.equal(ui.confirmCopy('privateMode').confirmLabel, 'Switch to private mode');
  for (const key of Object.keys(ui.CONFIRMS)) assert.ok(!/[!]/.test(JSON.stringify(ui.CONFIRMS[key])), key);
  assert.throws(() => ui.confirmCopy('nope'));
});
```
and in `design.test.js`: no `window.confirm(`, `confirm(` or `alert(` in `app.js`, `pages.js`, `demo.js`.

- [ ] **Step 2:** Run both. Expected: FAIL.
- [ ] **Step 3:** Copy for the other keys, carried over from today's text: `deleteStatement` ("Delete this statement?", "Its holdings and mutual fund history are removed.", "Delete statement"), `privateMode` ("Switch to private mode?", "Private mode deletes the chats and portfolio snapshots saved for this profile.", "Switch to private mode"), `newRecoveryCode` ("Create a new recovery code?", "Your current recovery code will stop working.", "Create new code", danger false), `clearHistory` ("Delete chats and snapshots?", "Your profile and broker connections are kept.", "Delete history"), `disconnectIndmoney` ("Disconnect INDmoney?", "Saved portfolio snapshots for this profile are deleted. Your chats are kept.", "Disconnect INDmoney"). Implement `confirmAction`; make each caller's handler `async` where it is not and `if (!(await confirmAction(...))) return`. `app.js:123` falls back to `toast` only (no `alert`); `demo.js:130` uses `toast` when defined, else writes the message into the page's status line. The dialog uses `.danger-button` for danger confirms, `--shadow-lg`, and scales from .98 on open. `admin.js` keeps its `confirm()` until batch 4 (it does not load `ui.js`).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS. In the browser: delete a chat (confirm → gone), try again and press Esc (still listed, focus back on Delete chat).
- [ ] **Step 5: Commit** `feat(frontend): in-app confirmations instead of browser dialogs`

### Task 6: Phosphor icon sprite

**Files:**
- Create: `frontend/icons.svg`, `frontend/icons-LICENSE.txt` (Phosphor MIT licence text)
- Modify: `frontend/index.html` (the 45 inline `<svg>`), `frontend/js/pages.js` (2 inline `<svg>`), `frontend/sw.js:1-2`, `frontend/css/app.css` (`.icon` rule)
- Test: `tests/js/design.test.js`, `tests/js/sw.test.js`

**Interfaces:**
- Produces: `icons.svg` with `<symbol id="ph-<name>" viewBox="0 0 256 256">` per icon, Phosphor regular weight from `@phosphor-icons/core@2.1.1` (`https://unpkg.com/@phosphor-icons/core@2.1.1/assets/regular/<name>.svg`), `fill="currentColor"`. Markup: `<svg class="icon" aria-hidden="true"><use href="/icons.svg#ph-<name>"></use></svg>`; `.icon { width: 20px; height: 20px; flex-shrink: 0 }`.
- The brand mark (`.logo-mark`) stays inline; it is the logo, not an icon.

- [ ] **Step 1: Failing tests:** every `<use href="/icons.svg#ph-X">` in `index.html` and `pages.js` has a matching `<symbol id="ph-X">` in `icons.svg`; `index.html` has no inline `<svg` other than `.logo-mark` and `.model-caret`; `sw.test.js` asserts `APP_SHELL` contains `/icons.svg`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Map each inline icon to the nearest Phosphor name by what it shows (house, chart-pie-slice, list-bullets, chat-circle, pulse, chats, target, trend-up, calculator, chart-line, file-text, magnifying-glass, plus, x, sun, moon, gear, sign-out, trash, arrow-up, caret-down, sidebar-simple, check, warning, …); download only the names used; keep each icon's existing `aria-*` and wrapper classes. Add `/icons.svg` to `APP_SHELL`; bump `CACHE_NAME`.
- [ ] **Step 4:** Run tests. Expected: PASS. Browser: every icon renders in both themes, also offline after one load (DevTools offline, reload).
- [ ] **Step 5: Commit** `style(frontend): Phosphor icons as one sprite`

### Task 7: Chart theming

**Files:** Modify `frontend/js/app.js:2207-2370` (`renderDataChart`, `historyLayout`, `historyTraces`); Test `tests/js/design.test.js`

**Interfaces:**
- Produces (app.js): `chartTheme() -> {font, text, muted, line, surface, accent, gain, loss, series: string[8]}` read from `getComputedStyle(document.documentElement)` on every draw (so a theme toggle redraws correctly; today's theme toggle already re-renders charts — keep that path).

- [ ] **Step 1: Failing test:** `app.js` contains no `'Inter` and no hex colour literal inside `renderDataChart`/`historyLayout`/`historyTraces` (slice the source between those function names and assert `/#[0-9a-fA-F]{3,6}\b/` does not match).
- [ ] **Step 2:** Run. Expected: FAIL (`'Inter, sans-serif'`).
- [ ] **Step 3:** Implement `chartTheme()`; layouts use `font.family = theme.font`, gridlines `theme.line`, history line and area `theme.accent` (area fill at 8% alpha, no gradient), signed bars `gain`/`loss`, donut and bar series from `theme.series`. Hover labels on `surface` with `line` border.
- [ ] **Step 4:** Run tests. Expected: PASS. Browser: Home chart, Portfolio donut, a calculator chart and a chat chart in both themes.
- [ ] **Step 5: Commit** `style(frontend): charts take colours and font from the tokens`

### Task 8: Ship batch 1

**Files:** Modify `frontend/index.html` (`?v=` on every asset), `frontend/sw.js` (`CACHE_NAME`), `.claude/CLAUDE.md` (Frontend section: tokens, Geist, `icons.svg`, `confirmAction`/`CONFIRMS`, `design.test.js`, `chartTheme()`), `.claude/launch.json` only if the demo server is rebuilt.

- [ ] **Step 1:** Run every check from Global Constraints. Expected: all pass.
- [ ] **Step 2:** Start the app (`ca-mode` in `.claude/launch.json`, `?demo`) and screenshot Home, Holdings, Ask AI with an answer, Goals and the profile dialog at 1440px and 390px, dark and light. Compare against the mockups' colours and type; layout differences are expected (batches 2–4).
- [ ] **Step 3:** Commit the spec and this plan with the work (copy them from the main checkout's `docs/superpowers/` if the worktree lacks them), push, open a PR titled `Redesign batch 1: foundations` whose body lists the tasks, the screenshots and "Layout unchanged; shell is batch 2". No Claude attribution lines.
