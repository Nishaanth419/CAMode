# Redesign batch 2: the shell — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed sidebar and top bar with the approved shell: a sidebar that opens (248px) and closes (64px), a command menu (Cmd/Ctrl+K), the restyled phone tab bar with a staggered More drawer, right-side slide-overs, View Transitions between pages and a one-column sign-in gate.

**Architecture:** `#sidebar` and `#topbar` are rebuilt in place in `frontend/index.html`, keeping every id, `data-*` hook and JS-read class; `#topbar` survives only as the phone header (below 900px). Testable logic (initial sidebar state, shortcuts, command results, stagger delays, view transitions) goes into pure helpers in `frontend/js/ui.js`, tested in `tests/js/ui.test.js`. The DOM wiring for the sidebar state, shortcuts and the command menu lives in a new `frontend/js/shell.js`, loaded after `pages.js` and before `rail.js`, so it can call `navigate()`, `startNewChat()`, `refreshSnapshot()`, `openGoalDialog()`, `openBrokers()` and `toggleTheme()`. Markup and CSS rules are pinned by assertions in `tests/js/design.test.js`.

**Tech Stack:** Vanilla CSS and JS, `node --test`, pytest, `<dialog>`, `@starting-style`, the View Transitions API (`document.startViewTransition`), Phosphor sprite `frontend/icons.svg`.

**Spec:** `docs/superpowers/specs/2026-11-20-frontend-redesign-design.md` ("The shell", "Motion", "Constraints", "Delivery" item 2). Mockups: https://claude.ai/artifact/6Yfq8NUdeJe2wH4vauAXuX (artboards Main, Command, Phone). Batch 1 plan for format: `docs/superpowers/plans/2026-11-20-redesign-batch-1-foundations.md`.

## Global Constraints

- Keep every `id`, `data-*` attribute and JS-read class (`#sidebar`, `#chat-list`, `#new-chat-button`, `#account-trigger`, `#account-name`, `#account-plan`, `.account-avatar`, `#sidebar-scrim`, `#menu-toggle`, `#tabbar`, `.tab`, `.nav-item`, `[data-route]`, `#topbar-account`, `#chat-search`, `#status-badge`, `#status-dot`, `#status-text`, `#theme-toggle`, `#theme-toggle-icon`, `#chat-new`, `#profile-dialog`, `#goal-dialog`, `#allocation-dialog`, `#confirm-dialog`, every `#profile-gate` form id). New structure wraps existing hooks.
- Sidebar: 248px open, 64px closed; the column animates `grid-template-columns` over `var(--dur-move)` `var(--ease)` (the one layout animation); labels fade over `var(--dur-fade)`. Remembered in `localStorage` key `ca-mode-sidebar` (`"open"` / `"closed"`), every access in try/catch, open by default. Between 900px and 1280px it starts closed. Ctrl/Cmd+B toggles.
- Sidebar order, grouped by spacing only, no group labels: Home, Portfolio, Holdings · Ask AI, Activity, All chats · Goals, Returns & tax, Calculators · Markets, Reports. (The spec writes "Planning"; the label stays "Calculators" because that is the page's own title, `ROUTE_META.planning`.) Active item: `--surface-2` background, `--text` label, accent icon. Background canvas, `--line` border on the right.
- Toggle button: panel icon, `aria-expanded`, label and `title` "Close sidebar" / "Open sidebar". Search button text "Search or ask" with `⌘K` on Apple platforms, `Ctrl K` elsewhere.
- Status pill and theme toggle move into the profile panel; theme is also a command.
- Command menu: pages, chats (same data `renderAllChats()` filters: `chats`, by title), ask with either agent, actions (New chat, Refresh snapshot, Add a goal, Connect a broker, Switch to light/dark mode, Sign out). Arrow keys, Enter, Esc; listbox. Panel 640px wide, radius 16px, scales from 0.98, dimmed backdrop.
- Phones (< 900px): sidebar hidden; tab bar (Home, Holdings, Ask, Goals, More) restyled per the Phone mockup; More opens the full open sidebar as a drawer from the left, items 60ms apart in reading order, last delay at most 360ms.
- Slide-overs: `#profile-dialog`, `#goal-dialog`, `#allocation-dialog` are right-side panels (`<dialog>` kept), full screen below 640px. `#confirm-dialog` stays a small centred dialog.
- Motion: `--ease`, `--dur-move` (500ms), `--dur-fade` (300ms) only; nothing under 200ms; only `transform` and `opacity` animate, except the sidebar width. Every new keyframe with `transform` is redefined as a fade in the reduced-motion block (the lint enforces it).
- CSP: no inline handlers or `style=""` in `index.html`; JS may set custom properties with `element.style.setProperty()`.
- Icons only from the sprite: `<svg class="icon" aria-hidden="true"><use href="/icons.svg?v=1#ph-NAME"></use></svg>`; a new symbol is copied from Phosphor core 2.1.1 regular and the `?v=` on every `icons.svg` href is bumped to 2.
- Copy: sentence case, buttons name their action, no exclamation marks.
- Every check passes at the end: `python -m compileall -q app tests && python -m pytest -q`, `node --test tests/js/*.test.js`, `for f in frontend/js/*.js; do node --check "$f"; done`, `python -m scripts.eval_tasks`, `python -m scripts.eval_workflows`.

## Review Focus

1. **Typing in a field:** Ctrl/Cmd+B or K pressed while the composer, a search box or a dialog field has focus. Expected: K always opens the command menu (unless the gate or another dialog is open); B toggles the sidebar only when no dialog is open, and neither shortcut fires while the sign-in gate is shown. Test: Task 1 `shortcutFor` cases with `gateOpen` / `dialogOpen`.
2. **Storage blocked** (private window, Safari with storage off): `localStorage` throws. Expected: the sidebar opens, toggling still works, nothing throws. Test: Task 1 `readSidebarState` / `saveSidebarState` with a throwing storage.
3. **Window resized across 900px and 1280px** with the drawer open or the sidebar closed. Expected: above 900px the drawer class is cleared and the remembered state applies; below 900px the drawer always shows the full open sidebar. Test: Task 1 design lint asserts `body.sidebar-closed` rules sit outside the `max-width: 900px` block and the phone block resets the closed width; Task 4 browser step.
4. **Command menu with no chats, private mode or a query matching nothing.** Expected: the Ask group still offers both agents for any non-blank query; with a blank query, pages and actions show; nothing throws on an empty `chats` list. Test: Task 2 `commandResults` cases.
5. **Reduced motion:** sidebar, slide-overs, command menu, drawer stagger and page transitions become 200ms fades or nothing; the sidebar width snaps. Test: Task 3 and Task 5 lint assertions on the reduced-motion block.

---

### Task 1: Sidebar that opens and closes, phone header, shortcuts

**Files:**
- Modify: `frontend/index.html` (the `#sidebar` aside, `#topbar` header, the All chats page head, `#profile-dialog` head, script tags), `frontend/css/app.css` (App shell, Sidebar, Main column sections, the `max-width: 900px` block), `frontend/js/ui.js`, `frontend/js/app.js:13-45,182-236,980-984`, `frontend/js/pages.js:337-341` (if needed), `frontend/sw.js`
- Create: `frontend/js/shell.js`
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`, `tests/js/sw.test.js`

**Interfaces:**
- Produces in `ui.js` (exported on `api`):
  - `SIDEBAR_KEY = 'ca-mode-sidebar'`
  - `readSidebarState(storage) -> 'open' | 'closed' | null` (null for a missing, unknown or throwing read)
  - `saveSidebarState(storage, state) -> boolean` (false when it throws)
  - `initialSidebarState({ stored, width }) -> 'open' | 'closed'`: `width <= 1280` → `'closed'`; else `stored === 'closed' ? 'closed' : 'open'`. The state is saved only when `width > 1280` (shell.js), so a medium window never overwrites the desktop choice.
  - `shortcutFor(event, { gateOpen, dialogOpen }) -> 'command' | 'sidebar' | null`: Meta or Ctrl, no Alt or Shift; `k` → `'command'` unless `gateOpen` or `dialogOpen`; `b` → `'sidebar'` unless `gateOpen` or `dialogOpen`; anything else null. Key compared case-insensitively.
  - `shortcutLabel(platform) -> '⌘K' | 'Ctrl K'` (`/Mac|iPhone|iPad/`).
- Produces in `app.js`: `toggleTheme()` (the body of today's `themeToggle` click handler, which now calls it).
- Produces in `shell.js`: `setSidebarCollapsed(collapsed, { save = true } = {})` (toggles `body.sidebar-closed`, updates `#sidebar-toggle` `aria-expanded`, `aria-label`, `title`; saves through `saveSidebarState` only when `innerWidth > 1280` and `save`), and the single `keydown` listener for shortcuts (replacing the Cmd+K block in `app.js`; Escape still calls `setSidebarOpen(false)`). Task 2 adds `openCommandMenu()` here; until then `'command'` focuses `#command-open`.

- [ ] **Step 1: Write the failing tests**

`tests/js/ui.test.js`:
```js
test('the sidebar remembers its state and survives blocked storage', () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(ui.readSidebarState(storage), null);
  assert.equal(ui.saveSidebarState(storage, 'closed'), true);
  assert.equal(ui.readSidebarState(storage), 'closed');
  store.set(ui.SIDEBAR_KEY, 'sideways');
  assert.equal(ui.readSidebarState(storage), null);
  const blocked = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.equal(ui.readSidebarState(blocked), null);
  assert.equal(ui.saveSidebarState(blocked, 'open'), false);
});
test('the sidebar starts open on wide screens and closed up to 1280px', () => {
  assert.equal(ui.initialSidebarState({ stored: null, width: 1440 }), 'open');
  assert.equal(ui.initialSidebarState({ stored: 'closed', width: 1440 }), 'closed');
  assert.equal(ui.initialSidebarState({ stored: 'open', width: 1100 }), 'closed');
  assert.equal(ui.initialSidebarState({ stored: null, width: 1280 }), 'closed');
});
test('Cmd/Ctrl+K opens the command menu and Cmd/Ctrl+B toggles the sidebar', () => {
  const key = (k, mods = {}) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods });
  const free = { gateOpen: false, dialogOpen: false };
  assert.equal(ui.shortcutFor(key('k', { metaKey: true }), free), 'command');
  assert.equal(ui.shortcutFor(key('K', { ctrlKey: true }), free), 'command');
  assert.equal(ui.shortcutFor(key('b', { ctrlKey: true }), free), 'sidebar');
  assert.equal(ui.shortcutFor(key('k', { metaKey: true, shiftKey: true }), free), null);
  assert.equal(ui.shortcutFor(key('k'), free), null);
  assert.equal(ui.shortcutFor(key('k', { metaKey: true }), { gateOpen: true, dialogOpen: false }), null);
  assert.equal(ui.shortcutFor(key('b', { metaKey: true }), { gateOpen: false, dialogOpen: true }), null);
  assert.equal(ui.shortcutLabel('MacIntel'), '⌘K');
  assert.equal(ui.shortcutLabel('Win32'), 'Ctrl K');
});
```

`tests/js/design.test.js`:
```js
test('the sidebar opens at 248px and closes to 64px, animating only its column', () => {
  const css = readCss();
  assert.match(css, /--sidebar-w:\s*248px/);
  assert.match(css, /body\.sidebar-closed\s*\{[^}]*--sidebar-w:\s*64px/);
  assert.match(css, /#app\s*\{[^}]*transition:\s*grid-template-columns var\(--dur-move\) var\(--ease\)/);
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
  assert.match(phone.slice(0, phone.indexOf('\n}\n')), /body\.sidebar-closed #sidebar/);
});
```

`tests/js/sw.test.js`: extend the existing `APP_SHELL` assertion so it includes `'/js/shell.js'`.

Also in `design.test.js`: add `'shell.js'` to the file lists of "no browser dialogs in the app scripts" and "every sprite icon referenced by the app exists in icons.svg". That icon test fails on unused symbols too, so a symbol whose last use goes (e.g. `ph-gear` with `.account-gear`, `ph-moon`/`ph-sun` stay used by `applyTheme`) is removed from `icons.svg`.

- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL (`readSidebarState` not a function, `sidebar-toggle` missing, `/js/shell.js` not cached).

- [ ] **Step 3: Implement the ui.js helpers** with the signatures in Interfaces.

- [ ] **Step 4: Rebuild `#sidebar` in `index.html`** (Main mockup): head row (the existing `.brand` link, then `<button id="sidebar-toggle" class="icon-button sidebar-toggle" aria-expanded="true" aria-controls="sidebar" aria-label="Close sidebar" title="Close sidebar">` with a panel icon: add Phosphor `ph-sidebar-simple` to `icons.svg`); `<button id="command-open" class="command-open" aria-haspopup="dialog" aria-keyshortcuts="Meta+K Control+K">` with the magnifying glass, a `<span class="nav-label">Search or ask</span>` and `<kbd id="search-kbd" class="search-kbd nav-label">Ctrl K</kbd>` (shell.js sets it from `shortcutLabel`); `.main-nav` with four `.nav-group` divs (no labels, no `role="group"`) in the Global Constraints order; each `.nav-item` keeps its icon and wraps its text in `<span class="nav-label">`; All chats is a `.nav-item` with `ph-chats` (add the symbol). `.sidebar-section-head` keeps "Recent chats" (as `.nav-label`) and `#new-chat-button`; the "See all" link goes (All chats is in the nav). `.sidebar-account` keeps `#account-trigger`; drop `.account-gear`.
- Closed (`body.sidebar-closed`, outside any media query): `.nav-label`, `.sidebar-section-head`, `#chat-list` and `.account-copy` fade to `opacity: 0` and stop taking space (`width: 0; overflow: hidden` for labels; `display: none` for the recent chats after the fade is acceptable), the brand text hides, the head centres the toggle. Each `.nav-item .nav-label` becomes a tooltip on `:hover` and `:focus-visible` (absolutely positioned to the right, `--surface-3` background, `--radius-sm`, opacity transition), so the name is visible without `title`.
- `#app` `transition: grid-template-columns var(--dur-move) var(--ease)`; in the reduced-motion block `#app { transition: none; }`.

- [ ] **Step 5: Turn `#topbar` into the phone header.** Keep `<header id="topbar">` with `.topbar-brand`, a new `<button id="command-open-phone" class="icon-button" aria-label="Search or ask" aria-haspopup="dialog">` (magnifying glass) and `#topbar-account`; `#topbar { display: none; }` above 900px, shown in the 900px block (Phone mockup: 56px tall, 44px buttons). Move `#status-badge` and `#theme-toggle` into `#profile-dialog` as an "Appearance and status" `.dialog-section` right after `.dialog-head` (theme button with a visible label "Switch to light mode" / "Switch to dark mode": `applyTheme()` already writes the label; also set the text of a `<span id="theme-toggle-label">`). Move `#chat-search` into the All chats page head as `<label class="search-box">` before the New chat button (placeholder and aria-label unchanged); delete `.topbar-spacer` and its rules.

- [ ] **Step 6: Create `frontend/js/shell.js`** with `setSidebarCollapsed()` and the keydown listener from Interfaces; on load apply `initialSidebarState({ stored: readSidebarState(localStorage-or-null), width: innerWidth })` with `{ save: false }`; `#sidebar-toggle` click toggles. In `app.js` move the theme click handler body into `toggleTheme()` and remove the Cmd+K block and the `search-kbd` line. Add `<script src="/js/shell.js?v=1" defer></script>` after `pages.js` and `'/js/shell.js'` to `APP_SHELL`. Bump every `?v=` in `index.html` and `CACHE_NAME` in `sw.js` (`ca-mode-shell-v71`), and the `icons.svg?v=` to 2 everywhere it is referenced (HTML and JS).

- [ ] **Step 7:** Run `node --test tests/js/*.test.js && for f in frontend/js/*.js; do node --check "$f"; done`. Expected: PASS.

- [ ] **Step 8: Commit** `git commit -m "feat(frontend): sidebar that opens and closes, phone header and shortcuts"`

### Task 2: Command menu

**Files:**
- Modify: `frontend/index.html` (a `<dialog id="command-dialog">` before `#confirm-dialog`), `frontend/css/app.css` (new "Command menu" section), `frontend/js/ui.js`, `frontend/js/shell.js`
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Consumes: `shortcutFor()` (Task 1); globals `navigate(route)`, `ROUTE_META` (pages.js), `chats`, `selectChat(id)`, `startNewChat()`, `runPrompt(prompt, agent)`, `refreshSnapshot()`, `openBrokers()`, `toggleTheme()`, `openGoalDialog()` (pages.js), `loadChats()`, `currentUser`.
- Produces in `ui.js`:
  - `commandResults(query, { pages, chats, actions }) -> Array<{ title: string, items: Array<{ id: string, kind: 'ask' | 'page' | 'chat' | 'action', label: string, hint: string, value: string, agent?: 'portfolio' | 'finance' }> }>`. `pages`: `[{ route, label }]`; `chats`: the app's chat objects (`id`, `title`, `updatedAt`); `actions`: `[{ id, label }]`. Blank query: groups "Pages" (all), "Recent chats" (first 5, title or "New chat"), "Actions" (all). Non-blank (trimmed): "Ask" with `Ask Portfolio Analyzer: "<q>"` (`agent: 'portfolio'`, hint `Enter` on the first) and `Ask Finance Agent: "<q>"`, then "Pages", "Chats" (up to 6), "Actions" whose label contains every word of the query, case-insensitively; empty groups are dropped. Item ids are unique strings (`ask-portfolio`, `page-home`, `chat-<id>`, `action-<id>`).
  - `moveActive(index, delta, count) -> number` (wraps; `count === 0` → `-1`).
- Produces in `shell.js`: `openCommandMenu()`, `closeCommandMenu()`.

- [ ] **Step 1: Write the failing tests**

`tests/js/ui.test.js`:
```js
const PAGES = [{ route: 'home', label: 'Home' }, { route: 'holdings', label: 'Holdings' }];
const ACTIONS = [{ id: 'new-chat', label: 'New chat' }, { id: 'theme', label: 'Switch to light mode' }];
const CHATS = [{ id: 'c1', title: 'Am I too concentrated in banks?' }, { id: 'c2', title: '' }];
test('a blank command query lists pages, recent chats and actions', () => {
  const groups = ui.commandResults('  ', { pages: PAGES, chats: CHATS, actions: ACTIONS });
  assert.deepEqual(groups.map((g) => g.title), ['Pages', 'Recent chats', 'Actions']);
  assert.equal(groups[1].items[1].label, 'New chat');
});
test('a command query offers both agents first, then matches', () => {
  const groups = ui.commandResults('Bank', { pages: PAGES, chats: CHATS, actions: ACTIONS });
  assert.deepEqual(groups.map((g) => g.title), ['Ask', 'Chats']);
  assert.equal(groups[0].items[0].label, 'Ask Portfolio Analyzer: "Bank"');
  assert.equal(groups[0].items[0].agent, 'portfolio');
  assert.equal(groups[0].items[1].agent, 'finance');
  assert.equal(groups[1].items[0].id, 'chat-c1');
});
test('a query matching nothing still offers to ask, and no chats is fine', () => {
  const groups = ui.commandResults('zzz', { pages: PAGES, chats: [], actions: ACTIONS });
  assert.deepEqual(groups.map((g) => g.title), ['Ask']);
  assert.deepEqual(ui.commandResults('light mode', { pages: [], chats: [], actions: ACTIONS })[1].items.map((i) => i.id), ['action-theme']);
});
test('arrow keys wrap through the results', () => {
  assert.equal(ui.moveActive(0, -1, 4), 3);
  assert.equal(ui.moveActive(3, 1, 4), 0);
  assert.equal(ui.moveActive(-1, 1, 0), -1);
});
```

`tests/js/design.test.js`:
```js
test('the command menu is a dialog with a combobox driving a listbox', () => {
  const html = readHtml();
  const menu = html.slice(html.indexOf('<dialog id="command-dialog"'), html.indexOf('</dialog>', html.indexOf('<dialog id="command-dialog"')));
  assert.match(menu, /aria-label="Command menu"/);
  assert.match(menu, /id="command-input"[^>]*role="combobox"[^>]*aria-controls="command-list"/);
  assert.match(menu, /id="command-list"[^>]*role="listbox"/);
  assert.match(readCss(), /\.command-dialog\s*\{[^}]*width:\s*min\(640px/);
});
```

- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.

- [ ] **Step 3: Implement `commandResults()` and `moveActive()`** in `ui.js`.

- [ ] **Step 4: Add the dialog** (Command mockup): `<dialog id="command-dialog" class="command-dialog" aria-label="Command menu">` with a search row (`ph-magnifying-glass`, visually hidden label "Search or ask", `<input id="command-input" type="text" role="combobox" aria-expanded="true" aria-autocomplete="list" aria-controls="command-list" autocomplete="off">`, `<kbd>Esc</kbd>`), `<div id="command-list" role="listbox" aria-label="Results">`, and a footer "↑ ↓ to move · Enter to open · Esc to close". CSS: top-aligned at 12vh, radius 16px, `--surface` background, `--line` border, shadow token for floating chrome; groups as `role="group"` with a 12px muted title; options 40px, `aria-selected="true"` on `--surface-3` with an accent icon tile. Opening: `@starting-style` from `opacity: 0; transform: scale(.98)` over `var(--dur-fade) var(--ease)`; `::backdrop` `rgba(0,0,0,.6)` fading in. Reduced motion: no transform.

- [ ] **Step 5: Wire it in `shell.js`.** `openCommandMenu()` clears the input, renders `commandResults()` with pages from `ROUTE_META` (every route except `not-found`; `chat` labelled "Ask AI"), `chats`, and the actions in Global Constraints (theme label from the current theme; Refresh snapshot only when INDmoney is connected, as the page buttons decide), sets the first option active, `showModal()`s. Input `input` re-renders; ArrowUp/Down call `moveActive` and set `aria-activedescendant` and scroll the option into view; Enter runs the active item; click runs the clicked item; Esc is the dialog's own. Running: close first, then `page` → `navigate(route)`; `chat` → `navigate('chat')` then `selectChat(id)`; `ask` → `runPrompt(query, agent)`; actions as named (`new-chat` → `navigate('chat')` + `startNewChat()`; `add-goal` → `navigate('goals')` + `openGoalDialog()`; `connect-broker` → `openBrokers()`; `sign-out` → `POST /api/session/logout` then `loadChats()`). Errors go to `showError`. `#command-open` and `#command-open-phone` open it; shortcut `'command'` opens it. Option rows built with `textContent` (titles are user text).

- [ ] **Step 6:** Run `node --test tests/js/*.test.js && node --check frontend/js/shell.js`. Expected: PASS.

- [ ] **Step 7: Commit** `git commit -m "feat(frontend): command menu"`

### Task 3: Slide-overs

**Files:**
- Modify: `frontend/css/app.css` (`.profile-dialog`, `.goal-dialog`, `.confirm-dialog`, reduced-motion block), `frontend/index.html` only if the dialogs need a scroll wrapper
- Test: `tests/js/design.test.js`

**Interfaces:**
- Consumes: nothing new; `#profile-dialog`, `#goal-dialog`, `#allocation-dialog` keep their markup and JS.

- [ ] **Step 1: Write the failing test**

```js
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
```

- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.

- [ ] **Step 3: Restyle.** `.profile-dialog`: `width: min(480px, 100%)` (goal and allocation `min(560px, 100%)`), `height: 100dvh`, `max-height: 100dvh`, `margin: 0 0 0 auto`, `border-radius: 16px 0 0 16px`, border only on the left (`--line`), `overflow-y: auto`, sticky `.dialog-head` on `--surface`; open/close with `transition: transform var(--dur-move) var(--ease), opacity var(--dur-fade) var(--ease), overlay var(--dur-move) allow-discrete, display var(--dur-move) allow-discrete` from `translateX(100%)` (`@starting-style` for opening, the closed state for closing); `::backdrop` fades the same way. Below 640px: `width: 100%`, radius 0. `.dialog-actions` stick to the bottom of the panel. `#confirm-dialog` keeps centred, scaling from .98. Reduced motion: `transform: none` on these dialogs (opacity only).

- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "style(frontend): profile, goal and allocation editors as slide-overs"`

### Task 4: Phone tab bar and the staggered More drawer

**Files:**
- Modify: `frontend/css/app.css` (`#tabbar` and the `max-width: 900px` block), `frontend/js/ui.js`, `frontend/js/app.js:215-225` (`setSidebarOpen`)
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Produces in `ui.js`: `staggerDelays(count, step = 60, max = 360) -> number[]` (milliseconds, `min(i * step, max)`).
- `setSidebarOpen(open)` sets `--stagger: <ms>ms` with `style.setProperty` on each drawer item, in reading order: `.brand`, `#command-open`, every `.nav-item`, `.sidebar-section-head`, every `#chat-list .chat-row`, `.sidebar-account`.

- [ ] **Step 1: Write the failing tests**

```js
test('drawer items arrive 60ms apart, the last within 360ms', () => {
  assert.deepEqual(ui.staggerDelays(4), [0, 60, 120, 180]);
  assert.equal(ui.staggerDelays(20).at(-1), 360);
  assert.deepEqual(ui.staggerDelays(0), []);
});
```
`tests/js/design.test.js`:
```js
test('the phone tab bar marks the current tab with an accent bar and the drawer staggers its items', () => {
  const css = readCss();
  assert.match(css, /\.tab\[aria-current="page"\][^{]*::before\s*\{[^}]*background:\s*var\(--accent\)/);
  assert.match(css, /body\.sidebar-open #sidebar [^{]*\{[^}]*transition-delay:\s*var\(--stagger/);
  const reduce = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert.match(reduce, /#sidebar[^{]*\{[^}]*transform:\s*none/);
});
```

- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.

- [ ] **Step 3: Implement** `staggerDelays()` and the `setSidebarOpen()` change. Tab bar (Phone mockup): 5 columns, translucent `--surface` with `backdrop-filter: blur(16px)` (needs a `--tabbar-bg` token in both themes: dark `rgba(24,24,24,.86)`, light `rgba(246,246,245,.86)`), `--line` top border, 12px/500 labels, `--text-muted` idle and `--text` current, a 20×3px accent bar above the current tab's icon (`::before`), `padding-bottom: max(8px, env(safe-area-inset-bottom))`, 44px minimum targets; `#menu-toggle.is-active` gets the same bar. Drawer: the sidebar at 280px (`min(280px, 86vw)`), always the open layout (`body.sidebar-closed` has no effect below 900px), items start `opacity: 0; transform: translateX(-8px)` and transition to none with `transition-delay: var(--stagger, 0ms)` when `body.sidebar-open`; reduced motion: `transform: none` and no delay.

- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "style(frontend): phone tab bar and staggered More drawer"`

### Task 5: Page changes with View Transitions

**Files:**
- Modify: `frontend/js/ui.js`, `frontend/js/pages.js:266-298` (`renderRoute`), `frontend/css/app.css` (view transition rules, reduced-motion block)
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Produces in `ui.js`: `withViewTransition(doc, update) -> Promise<void>`: runs `update()` inside `doc.startViewTransition(update)` and resolves when `updateCallbackDone` settles; without `startViewTransition` runs `update()` directly; never rejects because of the transition itself (an error from `update` still rejects).
- `renderRoute()` wraps the DOM swap (hiding/showing `#chat-scroll`/`#page-view`, toggling `.page`) in `withViewTransition(document, swap)` only when the route changed and this is not the first render; the rest (`renderPage`, events, scroll reset) runs after the swap as today.

- [ ] **Step 1: Write the failing tests**

```js
test('page changes use a view transition when the browser has one', async () => {
  const calls = [];
  const doc = { startViewTransition(cb) { calls.push('vt'); const done = Promise.resolve().then(cb); return { updateCallbackDone: done, finished: done }; } };
  await ui.withViewTransition(doc, () => calls.push('swap'));
  assert.deepEqual(calls, ['vt', 'swap']);
  const plain = [];
  await ui.withViewTransition({}, () => plain.push('swap'));
  assert.deepEqual(plain, ['swap']);
  await assert.rejects(ui.withViewTransition({}, () => { throw new Error('boom'); }), /boom/);
});
```
`tests/js/design.test.js`:
```js
test('the page body cross-fades and rises 8px; the sidebar never moves', () => {
  const css = readCss();
  assert.match(css, /#page-view,\s*#chat-scroll\s*\{[^}]*view-transition-name:\s*page-body/);
  assert.match(css, /::view-transition-new\(page-body\)\s*\{[^}]*animation:[^;]*var\(--dur-fade\) var\(--ease\)/);
  assert.match(css, /::view-transition-old\(root\),\s*::view-transition-new\(root\)\s*\{[^}]*animation:\s*none/);
});
```

- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.

- [ ] **Step 3: Implement** `withViewTransition()`, the `renderRoute()` change, and the CSS: `@keyframes page-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }`, `::view-transition-old(page-body)` fades out over `var(--dur-fade)`, root animations off; in the reduced-motion block `@keyframes page-in` is opacity only and durations 200ms.

- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(frontend): view transitions between pages"`

### Task 6: Sign-in gate as one centred column

**Files:**
- Modify: `frontend/css/app.css` (`.profile-gate`, `.gate-card`, `.gate-logo`), `frontend/index.html` only for the brand line under the mark
- Test: `tests/js/design.test.js`

- [ ] **Step 1: Write the failing test**

```js
test('the sign-in gate is one column on the canvas, not a card', () => {
  const css = readCss();
  const card = /\.gate-card\s*\{([^}]*)\}/.exec(css)[1];
  assert.doesNotMatch(card, /border:\s*1px|box-shadow|background:\s*var\(--surface\)/);
  assert.match(card, /width:\s*min\(400px, 100%\)/);
  assert.match(/\.profile-gate\s*\{([^}]*)\}/.exec(css)[1], /background:\s*var\(--canvas\)/);
});
```

- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.

- [ ] **Step 3: Restyle:** `.gate-card` transparent, no border or shadow, `width: min(400px, 100%)`, padding 0, centred text for the brand mark (44px accent tile), "CA Mode" wordmark under it (16px/600), title 24px; forms left-aligned full width with 44px controls (unchanged); the consent step widens to `min(560px, 100%)` as today; remove the dark-only shadow override.

- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "style(frontend): sign-in gate as one centred column"`

### Task 7: Browser verification, docs, screenshots

**Files:**
- Modify: `.claude/CLAUDE.md` (Frontend: `index.html`, `app.js`, new `shell.js` and `ui.js` bullets), `frontend/index.html` / `frontend/sw.js` if a further `?v=` bump is needed
- Create: `docs/superpowers/screenshots/redesign-batch-2/*.png`

- [ ] **Step 1:** Run every check in Global Constraints. Expected: all pass.
- [ ] **Step 2:** In the built-in browser on `/?demo` (server `ca-mode` from `.claude/launch.json`, restarted after `index.html` edits): at 1440px dark and light, sidebar open and closed (Ctrl+B, reload keeps the state), tooltips on the closed sidebar, command menu (type "bank", arrows, Enter asks; Esc closes and focus returns), profile panel with status and theme; at 1100px the sidebar starts closed; at 390px the phone header, tab bar, More drawer stagger, a full-screen slide-over; keyboard only from the toggle to the profile row; `prefers-reduced-motion` emulated: page changes fade, nothing slides.
- [ ] **Step 3:** Save screenshots: `desktop-open-dark.png`, `desktop-closed-light.png`, `command-menu.png`, `profile-slide-over.png`, `tablet-1100.png`, `phone-home.png`, `phone-drawer.png`, `sign-in.png`.
- [ ] **Step 4:** Update `.claude/CLAUDE.md` Frontend notes (shell structure, `shell.js`, the `ui.js` helpers, `#chat-search` on All chats, status and theme in the profile panel).
- [ ] **Step 5: Commit** `git commit -m "docs: batch 2 shell notes and screenshots"`
