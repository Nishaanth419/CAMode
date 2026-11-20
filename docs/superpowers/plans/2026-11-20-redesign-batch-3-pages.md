# Redesign batch 3: Home, Holdings, Ask AI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the three most-used pages their approved layouts: Home with the gradient net-worth hero and the one-time entrance, Holdings with a sticky-header table and a sliding tab underline, and Ask AI with a 720px text-on-canvas answer column, a floating composer and a borderless rail.

**Architecture:** Markup in `frontend/index.html` is restructured in place (every `id`, `data-*` hook and JS-read class kept); render functions in `frontend/js/pages.js`, `frontend/js/app.js` and `frontend/js/rail.js` change only their template strings and class names, never a figure or an API call. Testable logic (entrance claim, tab-indicator geometry) is pure and lives in `frontend/js/ui.js` beside the batch 2 helpers, tested in `tests/js/ui.test.js`; every CSS and markup rule is pinned in `tests/js/design.test.js`. All colours come from the existing tokens; the only new tokens are the hero gradient pair.

**Tech Stack:** Vanilla CSS and JS, `node --test`, pytest, Plotly (CDN, unchanged), Phosphor sprite `frontend/icons.svg`.

**Spec:** `docs/superpowers/specs/2026-11-20-frontend-redesign-design.md` ("Pages", "Motion", "Design system", "Delivery" item 3). Mockups: https://claude.ai/artifact/6Yfq8NUdeJe2wH4vauAXuX (artboards `project/Main.dc.html` Home, `project/AskAI.dc.html` Ask AI, `project/Phone.dc.html`). Earlier plans for format and house rules: `docs/superpowers/plans/2026-11-20-redesign-batch-1-foundations.md`, `...batch-2-shell.md`.

## Global Constraints

- Keep every `id`, `data-*` attribute and JS-read class. Home: `#home-name`, `.js-greeting`, `.js-goal-agent`, `#home-ask`, `#home-ask-input`, `#home-ask-agent`, `#home-stats`, `#home-networth-value`, `#home-networth-change`, `#home-performance`, `#home-range`, `[data-home-range]`, `#home-allocation`, `#home-holdings`, `#home-goals`, `#home-insights`, `#home-chats`, `[data-broker-banners]`, `.js-snapshot-refresh`, `[data-go]`, `[data-new-chat]`, `[data-add-goal]`, `[data-edit-goal]`, `.ask-why`, `.change-pill`. Holdings: `#holdings-body`, `#holdings-asof`, `#holdings-csv`, `#holdings-table`, `#holding-search`, `#holding-sort`, `[data-holdings-tab]`, `[data-holding-type]`, `[data-broker-refresh]`, `#cas-card` and every id inside it, `.source-tab`, `.filter-chip`, `.table-action`. Ask AI: `#chat-area`, `#chat-head`, `#chat-scroll`, `#messages`, `#empty-state`, `#input-area`, `#input-wrap`, `#user-input`, `#provider-picker`, `#model-label`, `#chat-delete`, `#chat-new`, `.agent-option`, `.message`, `.message-body`, `.message-meta`, `.chip`, `.follow-ups`, `.answer-follow-ups`, `.typing-indicator`, `.typing-progress`, and everything `rail.js` selects.
- No change to any figure, API, route, or behaviour. No count-up or interpolation on money: a figure is written once, final.
- Hero figure `#home-networth-value`: 60px/1 at 900px and wider, 36px/40px below 900px (Phone mockup), weight 600, letter-spacing `-0.035em`, text gradient `linear-gradient(90deg, var(--hero-from), var(--hero-to))` with `-webkit-background-clip: text; background-clip: text; color: transparent`. Tokens: dark `--hero-from: #FFFFFF`, `--hero-to: #9B9B9B`; light `--hero-from: #000000`, `--hero-to: #666666`. This is the only `gradient(` in `app.css`. `@media (forced-colors: active)` returns it to `CanvasText`.
- Entrance (Home, first Home render of a browser session only, ~900ms): figure fades in (500ms, rises 8px), history line draws left to right (900ms, 150ms delay), chart area fades in at 700ms, KPI row fades in at 650ms. Easing `var(--ease)`. Reduced motion: every piece is a 200ms plain fade, the line does not draw.
- Home page structure per the Main mockup: no `.panel` card borders; KPI row is one `--surface` block (radius 12px, padding 24px) of four plain figures (label 14px `--text-soft`, figure 24px/32px 600 `-0.01em`, note 12px `--text-muted`), 4 across, 2 across below 900px; allocation is one 12px stacked bar plus a list with hover rows; largest holdings and "What changed today" are lists with `--surface-2` row hover, not cards; section headings 20px/28px 600 `-0.01em`; links are accent text with no `→` and no trailing arrow glyph (`aria-hidden` arrows removed from Home and Holdings markup and templates); the suggested-question rows end in a `ph-caret-right` sprite icon.
- Holdings per spec: table on `--surface` (no border), sticky header, `--surface-2` row hover, source tabs with a 2px accent underline that slides between tabs over `var(--dur-move) var(--ease)` (transform only); the phone card rows (below 640px) stay.
- Ask AI per the AskAI mockup: column `--chat-w: 720px`; assistant answers are text on the canvas (no bubble, no avatar tile), lead paragraph 18px/28px 500 for the first paragraph, body 16px/26px, capped at `65ch`; user questions are a right-aligned `--surface` block (radius 12px, padding 12px 16px, max 80%); a new answer rises 8px and fades (`var(--dur-move)`); follow-up chips arrive 60ms apart (`ui.staggerDelays`, last delay at most 360ms); the composer floats at the bottom (surface, `--line` border, 16px radius, floating-chrome shadow); the rail's three sections have no card borders or dividers (the portfolio figure sits on `--surface`, radius 12px, as in the mockup). The typing indicator keeps its markup and is restyled; chat-streaming progress lines are not designed here.
- Motion rules from batch 1 hold: `--ease`, `--dur-move` 500ms, `--dur-fade` 300ms, nothing under 200ms; only `transform` and `opacity` animate except the named `line-draw` keyframe (stroke-dashoffset) and the sidebar width; every keyframe with `transform` is redefined in the reduced-motion block; no inline handlers or `style=""` in `index.html`; JS may set custom properties and the data-driven `width` of bars as today.
- Icons only from the sprite; a new symbol (`ph-caret-right`) is copied from Phosphor core 2.1.1 regular with `fill="currentColor"` and every `icons.svg?v=` href is bumped by one. Sentence case, no exclamation marks.
- Bump every `?v=` in `index.html` and `CACHE_NAME` in `frontend/sw.js` once, in Task 6.
- Every check passes at the end: `python -m compileall -q app tests && python -m pytest -q`, `node --test tests/js/*.test.js`, `for f in frontend/js/*.js; do node --check "$f"; done`, `python -m scripts.eval_tasks`, `python -m scripts.eval_workflows`.

## Review Focus

1. **Entrance replays by accident:** the period buttons, "Refresh snapshot", a session reload and returning to Home re-run `renderHome()`. Expected: the entrance plays once per browser session (first Home render), never again, and money figures are never animated. Test: Task 2 `claimEntrance` cases (storage present, throwing, second call) and a lint that `renderHome` calls it exactly once per render and never sets an interval or `requestAnimationFrame` loop on a figure.
2. **Empty, private and error Home states:** disconnected, private, no snapshot and a failed load show no hero figure and no KPI row. Expected: no empty gradient box, no stale entrance classes. Test: Task 1 lint `#home-networth-value:empty { display: none }` and `#home-stats[hidden]` stays hidden; Task 2 asserts `renderHome` removes `data-entrance` on the empty branches.
3. **Huge figures on phones:** a crore-sized figure at 60px overflows 390px. Expected: 36px below 900px and `overflow-wrap: anywhere`. Test: Task 1 lint on the media rule.
4. **Gradient text in forced-colors and print:** transparent text disappears. Expected: `CanvasText` fallback. Test: Task 1 lint.
5. **Sliding underline on a scrolled tab bar or after resize:** indicator geometry must use offsets inside the scrolling bar, not viewport rects, and be recomputed on resize and when logos load. Expected: the underline sits under the selected tab at every width, with no slide when only one tab exists. Test: Task 3 `tabIndicator` cases (scrolled container, single tab, zero width) and Task 6 browser step at 390px.

---

### Task 1: Hero figure gradient and the Home layout

**Files:**
- Modify: `frontend/index.html` (Home section, lines of `data-page="home"`), `frontend/css/app.css` (token blocks, Home, KPI, allocation, holding-list, change-list, goal-progress, mini-chat sections), `frontend/js/pages.js` (`kpiCard`, `allocationCard`, `topHoldingsList`, `insightList`, `renderHomeGoals`, `renderHome` templates only), `frontend/icons.svg`
- Test: `tests/js/design.test.js`

**Interfaces:**
- Produces: tokens `--hero-from`, `--hero-to` in both theme blocks; markup contract for later tasks: the Home section keeps `.home-hero`, `.home-networth` (now unbordered, a plain `<section>`), `#home-stats.kpi-strip`; `kpiCard(label, value, note, tone)` keeps its signature and class names (`.kpi-card`, `.kpi-label`, `.kpi-value`, `.kpi-note`) but renders as plain figures; `.kpi-strip` is one `--surface` block wherever it is used (Home, Holdings, Portfolio, Returns, Reports share it; their layouts do not change).

- [ ] **Step 1: Write the failing tests** in `tests/js/design.test.js`:
  - Update "no background gradients and no generic shadows": `app.css` has exactly one `gradient(`, inside the rule whose selector is `#home-networth-value`, and `admin.css`/`public.css` have none.
  - `hero tokens`: `tokenBlock` dark `--hero-from` `#FFFFFF`, `--hero-to` `#9B9B9B`; light `#000000`, `#666666`.
  - `the hero figure is 60px with the gradient, 36px on phones`: the `#home-networth-value` rule has `font-size: 60px`, `line-height: 1`, `letter-spacing: -.035em` (accept `-0.035em`), `linear-gradient(90deg, var(--hero-from), var(--hero-to))`, `background-clip: text`, `color: transparent`; a `@media (max-width: 899px)` (or the existing 900px block) sets `font-size: 36px`; a `forced-colors: active` rule sets `color: CanvasText` and `background: none`; `#home-networth-value:empty` is `display: none`.
  - `Home is lists and plain figures, not cards`: the Home slice of `index.html` (from `data-page="home"` to `data-page="portfolio"`) contains no `class="panel`, no `→`, no `<span aria-hidden="true">→</span>`, and contains `ph-caret-right` in the prompt rows; `.kpi-strip` rule has `background: var(--surface)`, `border-radius: 12px` (or `var(--radius-lg)` if that token is 12px), `padding: 24px` and no `border`; `.kpi-card` has no `border`, `background` or `box-shadow`.
  - `pages.js` Home/Holdings templates contain no `→` (`topHoldingsList`, `insightList`, `renderHomeGoals`, `sourceTabs`, `holdingsRows`).
  - Allocation and holdings rows: `.alloc-list li:hover, .holding-list li:hover, .change-list li:hover` background `var(--surface-2)`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL (two gradients forbidden today, tokens missing, panels present).
- [ ] **Step 3: Implement** the tokens and the hero rule; in the Home markup remove the `.panel` / `.home-card` classes, drop the `→` spans, replace `.prompt-row-arrow` text with `<svg class="icon" aria-hidden="true"><use href="/icons.svg?v=3#ph-caret-right"></use></svg>`, keep the order: hero (figure block + period group, then chart), `#home-stats`, a two-column row (allocation | ask), a two-column row (largest holdings | What changed today), Goals and Continue a chat as plain sections. Headings use the 20px section style, links `.panel-link` accent without arrows. Restyle per Global Constraints; the `.home-composer` becomes a `--surface` block with no border and `:focus-within` accent ring. Add `ph-caret-right` to `icons.svg` and bump the `icons.svg?v=` href everywhere by one (`HTML` and `JS`).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `for f in frontend/js/*.js; do node --check "$f"; done`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Home hero figure, plain KPI row and list sections"`

### Task 2: Home entrance

**Files:**
- Modify: `frontend/js/ui.js`, `frontend/js/pages.js` (`renderHome`), `frontend/css/app.css` (entrance rules, reduced-motion block)
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Produces in `ui.js` (exported on `api`): `ENTRANCE_KEY = 'ca-mode-entrance'` and `claimEntrance(storage) -> boolean`: true the first time in a browser session (it writes `'1'` under `ENTRANCE_KEY` in the given `sessionStorage`-like object), false afterwards; a missing or throwing storage still returns true once per page load and false after (module-level fallback flag). Every access in try/catch.
- Produces in `pages.js`: `renderHome()` sets `data-entrance="play"` on `[data-page="home"]` when `ui.claimEntrance(sessionStorage-or-null)` is true and the portfolio loaded (not on the empty, private, error or no-snapshot branches, which remove the attribute), removes it after 1600ms, and after Plotly draws marks the history line for the draw animation (`drawHistoryLine(chartEl)`: add class `history-line-draw` to the first path under `.scatterlayer .js-line` and set its `pathLength` attribute to `1`, the area fill path gets `history-area-in`).

- [ ] **Step 1: Write the failing tests.** `ui.test.js`:
```js
test('the Home entrance plays once per browser session', () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
  assert.equal(ui.claimEntrance(storage), true);
  assert.equal(ui.claimEntrance(storage), false);
  assert.equal(ui.claimEntrance({ getItem: () => null, setItem: () => {} }), true); // another session
});
test('a blocked storage still plays the entrance once', () => {
  const blocked = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  const first = ui.claimEntrance(blocked);
  assert.equal(ui.claimEntrance(blocked), false);
  assert.equal(typeof first, 'boolean');
});
```
  (If the fallback flag was already spent by an earlier test in the file, the blocked case may return false first; the second assertion is the contract. Reset by loading `ui.js` through a fresh `require` cache entry if the implementer prefers a strict `true`.) `design.test.js`: `[data-entrance="play"] #home-networth-value` and `[data-entrance="play"] #home-stats` use `animation` with `var(--ease)`; `@keyframes line-draw` animates `stroke-dashoffset` from `1` to `0` over `900ms` with a `150ms` delay in `.history-line-draw`; `.history-area-in` fades at `700ms`; the KPI fade delay is `650ms`; the reduced-motion block redefines these as `200ms` opacity fades and sets `.history-line-draw { animation: none; stroke-dasharray: none }`; `pages.js` `renderHome` calls `claimEntrance` once and no `setInterval`, no `requestAnimationFrame`, and no number tween (`toLocaleString` is not used to animate: assert `renderHome`'s source has no `requestAnimationFrame`).
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** `claimEntrance` in `ui.js`, the `renderHome` wiring (the line draws only after `chartHtml` has produced the SVG: find how `chartHtml`/`setHtml` triggers Plotly and hook the post-render callback it already offers, else the `plotly_afterplot` event on the container, once), and the CSS: `@keyframes entrance-in` (opacity 0 + translateY(8px) → none, `var(--dur-move)`), `line-draw`, `area-in`; none apply unless `data-entrance="play"` is set, so later renders never replay.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(frontend): one-time Home entrance with the history line drawing"`

### Task 3: Holdings table and sliding tabs

**Files:**
- Modify: `frontend/js/ui.js`, `frontend/js/pages.js` (`sourceTabs`, `renderHoldings`, `showActiveSourceTab`, `holdingsRows`, `brokerSplit`, `holdingsKpis` only if markup needs), `frontend/css/app.css` (source tabs, holdings panel and table, broker split, notes)
- Test: `tests/js/ui.test.js`, `tests/js/design.test.js`

**Interfaces:**
- Produces in `ui.js`: `tabIndicator(active, bar) -> { x: number, w: number }` where `active` and `bar` are `{ offsetLeft, offsetWidth }`-shaped objects: `x = active.offsetLeft - bar.offsetLeft`, `w = active.offsetWidth` (never negative; a missing `active` gives `{ x: 0, w: 0 }`). Used by `pages.js` to set `--tab-x` (px) and `--tab-w` (unitless scale base: `w / 100`) on `.source-tabs`.
- Produces in `pages.js`: `placeTabIndicator(body, { from })`: sets `--tab-x`/`--tab-w` for the active `.source-tab`; `from` is the previous tab's `{x, w}` kept in a module variable so the new DOM starts where the old underline was and transitions to the new place on the next frame (`renderHoldings` rebuilds the body on every tab click). It reruns on `resize` and after the tab logos load (`img` `load` events inside `.source-tabs`).

- [ ] **Step 1: Write the failing tests.** `ui.test.js`:
```js
test('the tab underline follows offsets inside the scrolling bar', () => {
  assert.deepEqual(ui.tabIndicator({ offsetLeft: 180, offsetWidth: 96 }, { offsetLeft: 20, offsetWidth: 600 }), { x: 160, w: 96 });
  assert.deepEqual(ui.tabIndicator({ offsetLeft: 20, offsetWidth: 80 }, { offsetLeft: 20, offsetWidth: 80 }), { x: 0, w: 80 });
  assert.deepEqual(ui.tabIndicator(null, { offsetLeft: 0, offsetWidth: 10 }), { x: 0, w: 0 });
});
```
  `design.test.js`: `.source-tabs` rule has `position: relative`; `.source-tabs::after` has `height: 2px`, `background: var(--accent)`, `transform: translateX(var(--tab-x, 0)) scaleX(...)` (width `100px` base) and `transition: transform var(--dur-move) var(--ease)`; `.source-tab[aria-pressed="true"]` no longer sets `border-bottom-color`; `.holdings-panel` has `background: var(--surface)` and no `border`; `.holdings-table thead th` has `position: sticky` and `background: var(--surface-2)` (or `--surface`); `.holdings-table tbody tr:hover` is `var(--surface-2)`; the reduced-motion block sets `.source-tabs::after { transition: none; }`; the `.holdings-panel` rule does not set `overflow: hidden` (use `overflow: clip` so sticky headers work); the below-640px card-row rules still exist (`grid-template-areas` inside `.holdings-table` rules); `pages.js` has no `→`.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** the helper, the placement and the CSS. The table sits on the surface with 14px cells, header 12px `--text-soft` weight 500; header sticky at `top: 0` on desktop (under 640px the header row is already hidden by the card layout). Keep `holdings-scroll` horizontal scrolling only below 900px (`overflow-x: auto`); above it `overflow: visible` so sticky works. The underline respects `prefers-reduced-motion` (snaps). The By-broker section uses the Home allocation list style without a card.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Holdings table on the surface with sliding tab underline"`

### Task 4: Ask AI column, answers, questions, composer

**Files:**
- Modify: `frontend/index.html` (`#empty-state`, remove `.hero-art` illustration markup if no script reads it: grep first), `frontend/css/app.css` (chat area, messages, follow-ups, composer, typing indicator, empty state), `frontend/js/app.js` (`placeFollowUps()` / the code that builds follow-up chips: set each chip's delay with `ui.staggerDelays`)
- Test: `tests/js/design.test.js`, `tests/js/ui.test.js` (only if a helper changes)

**Interfaces:**
- Consumes: `ui.staggerDelays(count) -> number[]` from batch 2 (60ms apart, at most 360ms). Chips get `style.setProperty('--chip-delay', `${ms}ms`)`.
- Produces: CSS contract `.message.assistant` (no avatar tile: `.message-avatar.assistant` hidden or removed from the template in `app.js` `addMessage`, whichever keeps `.message` hooks), `.answer-follow-ups .chip` animation `rise-in var(--dur-move) var(--ease) both` delayed by `var(--chip-delay, 0ms)`.

- [ ] **Step 1: Write the failing tests** in `design.test.js`: `#chat-area` defines `--chat-w: 720px`; `.message.assistant .message-body` has `max-width: 65ch`, no `background`, no `border`; `.message.assistant .message-body > p:first-child` is `18px`/`28px` weight 500; `.message.user .message-body` has `background: var(--surface)`, `border-radius: 12px`, `padding: 12px 16px` and no border; the `.message` rule animates with `rise-in` over `var(--dur-move)` and `rise-in` keyframes start at `translateY(8px)`; `.answer-follow-ups .chip` uses `var(--chip-delay` and `rise-in`; `app.js` source contains `staggerDelays` within the follow-up placement function; the composer (`.composer`) has `border-radius: 16px`, `background: var(--surface)`, a `box-shadow` (floating chrome allowed), and `#input-area` has no `border-top`; `.typing-indicator span` is 6px with `--text-muted` and the reduced-motion block still covers its keyframe; `index.html` has no `class="hero-art"` and no `coins` illustration, and `design.test.js`'s known-leftovers wording is updated if `.coins` goes.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** per Global Constraints. The chat head keeps title, agent and last-message day (24px title, 14px `--text-muted` meta), Delete chat (outlined) and New chat (`--surface-3`) per the mockup. Empty state: heading, one sentence and the four suggested questions as quiet `--surface` rows (the existing `.feature-card` buttons restyled flat, no tone colours, caret icon instead of `→`). The composer stays above the phone tab bar and keyboard rules from batch 2 (do not alter `--viewport-h` or `body.keyboard-open` behaviour).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `for f in frontend/js/*.js; do node --check "$f"; done`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Ask AI answers as text, quiet question blocks, floating composer"`

### Task 5: The Ask AI rail without card borders

**Files:**
- Modify: `frontend/css/app.css` (rail sections), `frontend/js/rail.js` (template classes only)
- Test: `tests/js/design.test.js`

- [ ] **Step 1: Write the failing tests:** `.rail-section + .rail-section` has no `border-top`; `.rail-section` gap 40px between sections (or `margin-top`/`row-gap` equal to `40px`); `.rail-card` has `background: var(--surface)`, `border-radius: 12px`, no `border` and no `box-shadow`; section headings (`.rail-head h2`) are 14px/20px 600; `.rail-chat:hover` is `var(--surface-2)`; `rail.js` has no `→`; the source dots keep the accent for the used sources.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** the rail restyle: "This answer used" a plain list with an accent square marker, "Your portfolio" figure 24px/32px 600 on a `--surface` block with the change line and an accent "Open holdings" link (no arrow), "Recent chats" as rows with the title and the day.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Ask AI rail sections without card borders"`

### Task 6: Browser verification, cache-busters, docs, screenshots, PR

**Files:**
- Modify: `frontend/index.html` (`?v=` bump), `frontend/sw.js` (`CACHE_NAME`), `.claude/CLAUDE.md` (Frontend notes for Home, Holdings, Ask AI and rail, the hero token pair, the entrance, `claimEntrance`, `tabIndicator`, the single allowed gradient, the lint rule, the global `.kpi-strip` look)
- Create: `docs/superpowers/screenshots/redesign-batch-3/*.png` (or `.jpg` like batches 1 and 2)

- [ ] **Step 1:** Bump every `?v=` in `index.html` and `CACHE_NAME` in `sw.js`; run every check in Global Constraints. Expected: all pass.
- [ ] **Step 2:** In the built-in browser on `/?demo` (`ca-mode` from `.claude/launch.json`, restarted after `index.html` edits) at 1440, 1100 and 390px, dark and light: Home (entrance on a fresh session via a cleared `sessionStorage`, then no replay on period change or navigation), Holdings (sticky header while scrolling, underline slides between tabs, 390px card rows), Ask AI with an answer and follow-up chips, the rail at 1440px, the 1100px layout without a rail; emulate reduced motion (no draw, plain fades). Compare with the mockups; fix differences.
- [ ] **Step 3:** Save screenshots: `home-dark-1440`, `home-light-1440`, `home-dark-390`, `holdings-dark-1440`, `holdings-light-390`, `ask-dark-1440`, `ask-light-1440`, `ask-dark-1100`.
- [ ] **Step 4:** Update `.claude/CLAUDE.md` as listed, then run the checks once more.
- [ ] **Step 5: Commit** `git commit -m "docs: batch 3 pages notes and screenshots"`, push the branch and open the PR titled "Redesign batch 3: Home, Holdings, Ask AI".
