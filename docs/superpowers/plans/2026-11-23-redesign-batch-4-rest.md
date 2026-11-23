# Redesign batch 4: remaining pages, public and admin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every page batches 1 to 3 did not redo (Portfolio, Goals, Returns & tax, calculators, Markets, Reports, Activity, All chats, the 404 section), the public pages and the admin page the look Home, Holdings and Ask AI now have, and replace admin's two browser dialogs with an in-app one.

**Architecture:** One shared page kit in `frontend/css/app.css` (a `.page-section` on the canvas with a 20px heading like Home's sections, tables on a `--surface` block, forms on `--surface`, records such as goal cards and Activity rows stay `--surface` blocks without borders), then one task per page group that moves its templates (`frontend/index.html`, `frontend/js/pages.js`, `frontend/js/trace.js`) onto the kit, changing template strings and class names only. `public.css` gets its own copy of the token values (it is standalone and follows the system theme); `admin.js` gets a small dialog of its own because the admin page loads neither `ui.js` nor `app.js`. Every rule is pinned in `tests/js/design.test.js`; admin's pure parts in a new `tests/js/admin.test.js`; public markup in `tests/test_public_pages.py`.

**Tech Stack:** Vanilla CSS and JS, `node --test`, pytest, Plotly (CDN, unchanged), Phosphor sprite `frontend/icons.svg`.

**Spec:** `docs/superpowers/specs/2026-11-20-frontend-redesign-design.md` ("Design system", "Pages", "Delivery" item 4). Earlier plans for format and house rules: `docs/superpowers/plans/2026-11-20-redesign-batch-{1-foundations,2-shell,3-pages}.md`. Look reference: `docs/superpowers/screenshots/redesign-batch-3/home-*.jpg`, `holdings-*.jpg` (the mockups at https://claude.ai/artifact/6Yfq8NUdeJe2wH4vauAXuX cover only Home, Ask AI, Command and Phone; read them with the Artifact tool, never web-fetch).

## Global Constraints

- Keep every `id`, `data-*` attribute and class that JS or tests read (grep `pages.js`, `app.js`, `trace.js`, `rail.js`, `shell.js` and `tests/js/*.test.js` before renaming or dropping a class; add new classes beside old ones rather than replacing a read one). No change to any figure, API, route, calculation or behaviour; the goal and tax maths are untouched.
- Batch 3 pages are not restyled again: nothing in this batch may change how Home, Holdings, Ask AI, the rail, the shell or the dialogs look. New rules are scoped to `.page-section` or to the batch 4 page bodies (`#portfolio-body`, `#goals-body`, `#goal-investments-body`, `#saved-context-body`, `#returns-body`, `#calc-body`, `#markets-body`, `[data-page="markets"]`, `#reports-body`, `#activity-body`, `#all-chats`, `[data-page="not-found"]`). Exception: the global table rule `.data-table` (Task 1), which Home and Holdings do not use (`#holdings-table` is `.holdings-table`; check before changing).
- Values: dark surfaces only `#000000 #181818 #1F1F1F #272727 #313131`; accent `#C2A66B` (light `#8A6F35`); gain `#5FB38A`, loss `#E07A6A`, warning `#D9A54A`; muted text `#8A8A8A`. Coloured text uses the `-text` tokens (`--accent-text`, `--gain-text`, `--loss-text`, `--warning-text`). No new hex values in `app.css`.
- Type: Geist only, sizes 12/14/16/18/20/24/30/36/48/60px. Page sections: heading 20px/28px 600 `-0.01em` (Home's section heading rule; reuse its selector list rather than writing a second value). Reading text and tables 14px/20px; labels, notes and table headers 12px/16px. Figures `tabular-nums`. Sentence case, no `→` on links or rows (the two date-range `→` in `trace.js` lines ~580 and ~687 stay), no exclamation marks, no ALL-CAPS.
- Surfaces by background: no `border` on sections, cards, chips or rows except inputs, the table row rules (`1px solid var(--line)`), the outlined secondary/danger buttons batch 1 defined, empty-state dashed boxes, and the one featured pricing plan. No left-border callouts. Radii 6px chips/inputs, 8px buttons, 12px surfaces. Shadows only on floating chrome (the admin dialog counts).
- Motion only in answer to an action: disclosures, filters, saving. `--ease`, `--dur-move` 500ms, `--dur-fade` 300ms, nothing under 200ms; only `transform` and `opacity`; every new keyframe with `transform` is redefined as an opacity fade in the `@media (prefers-reduced-motion: reduce)` block (the lint enforces it).
- Icons only from the sprite (`<svg class="icon" aria-hidden="true"><use href="/icons.svg?v=N#ph-NAME"></use></svg>`); a new symbol is copied from Phosphor core 2.1.1 regular with `fill="currentColor"`, and every `icons.svg?v=` href in HTML and JS is bumped by one in the same commit.
- CSP: no inline handlers or `style=""` in `index.html` or the Python-built pages; JS may set custom properties and data-driven widths as today.
- Git: the worktree holds an untracked `.venv` symlink and `.env` for the dev server. Never `git add -A` or `git add .`; add explicit paths and check `git status --short` before each commit. No Claude co-author or "Generated with" lines.
- Each task's checks: `node --test tests/js/*.test.js` and `for f in frontend/js/*.js; do node --check "$f"; done` (plus `python -m pytest -q tests/test_public_pages.py tests/test_admin.py` for Tasks 7 and 8). The full list runs in Task 9: `python -m compileall -q app tests && python -m pytest -q`, `node --test tests/js/*.test.js`, `node --check` on every `frontend/js/*.js`, `python -m scripts.eval_tasks`, `python -m scripts.eval_workflows`.
- Bump every `?v=` in `index.html` and `CACHE_NAME` in `frontend/sw.js` once, in Task 9; the Python-built pages' busters (`public.css?v=`, `admin.css?v=`, `admin.js?v=` in `app/public_pages.py`) are bumped in the task that changes those files.

## Review Focus

1. **Phone tables after the panels go:** with sections on the canvas, the `card-table` rows below a 640px container (Markets movers, Reports tables, Returns by fund, tax "Gains by rule") and `.alloc-table` rows below 640px must still read as cards. Expected: each row a `--surface` block (radius 12px) on the canvas, the table's own surface wrapper transparent at that width. Test: Task 1 lint on the `@container (max-width: 640px)` rules.
2. **Goal disclosures:** one panel open per card, `aria-expanded` and `hidden` kept, the caret turns, the panel fades in on open and only fades under reduced motion. Test: Task 3 lint (template keeps `data-goal-panel`, `aria-expanded`, `hidden`; keyframe redefined in the reduced-motion block).
3. **Admin delete cancelled or mistyped:** Esc, Cancel and the backdrop send nothing; a typed name goes to the server exactly as typed (the server stays the check, no client gating, no trimming). Test: Task 8 `admin.test.js`.
4. **Public pages in both system themes and forced colours:** standalone `public.css` must carry the same values as `app.css` for both themes. Test: Task 7 token-parity lint.
5. **Batch 3 pages unchanged by shared rules:** `.data-table`, `.panel`, `.card-head`, `.seg` and `.empty-panel` are shared. Expected: Home and Holdings markup never gains `page-section`, and the Home/Holdings/Ask lints keep passing. Test: Task 1 lint that the Home and Holdings slices of `index.html` and the Home/Holdings templates contain no `page-section`; every task reruns the whole design suite; Task 9 browser glance at Home and Holdings.

---

### Task 1: Shared page kit and the 404 section

**Files:**
- Modify: `frontend/css/app.css` (sections "Pages behind the sidebar", "Tables", "Page not found", the `@container (max-width: 640px)` card-table block, "Calculators, Markets, ..." shared rules), `frontend/index.html` (`data-page="not-found"` only)
- Test: `tests/js/design.test.js`

**Interfaces:**
- Produces CSS classes later tasks use (no JS):
  - `.page-section`: a section on the canvas: `display: grid; gap: 16px; min-width: 0`, no background, padding, border or shadow. Its `.card-head` has `margin-bottom: 0`; its `.card-title` takes Home's section heading size (20px/28px 600 `-0.01em`).
  - `.surface-block`: `background: var(--surface); border-radius: 12px; padding: 24px`, no border (forms, chart groups that need grouping, records).
  - `.table-surface`: the wrapper for a table in a section: `--surface`, radius 12px, `padding: 4px 16px`, `overflow-x: auto` (use with the existing `.table-scroll`).
  - `.data-table` (global): cells 14px/20px, padding `12px`, headers 12px/16px weight 500 `--text-soft`, row rule `1px solid var(--line)`, last row none, row hover `var(--surface-2)` (not `--accent-wash2`), `td small` 12px `--text-muted`.
  - `.seg`: radius 8px, padding 2px, buttons radius 6px (the batch 1 nested-radius minor).
  - `.page-sub` 14px/20px `--text-soft` (was 12px); `.empty-panel h2` 16px/24px, `.empty-panel p` 14px/20px.

- [ ] **Step 1: Write the failing tests** in `design.test.js` (one `test()` each):
  - `page kit: sections sit on the canvas with Home's heading size`: `ruleBody(css, '.page-section')` has `display: grid`, `gap: 16px`, and no `background`, `border` or `padding`; a rule whose selector list contains `.page-section .card-title` sets `font-size: 20px` and `line-height: 28px`.
  - `page kit: surface and table blocks have no border`: `.surface-block` and `.table-surface` have `background: var(--surface)`, `border-radius: 12px`, no `border`.
  - `tables read at 14px with quiet headers and surface hover`: `.data-table th, .data-table td` padding `12px`, `.data-table` font-size `14px`; `.data-table th` `font-size: 12px` and weight `500`; `.data-table tbody tr:hover` `background: var(--surface-2)`; the row rule uses `var(--line)`; no rule in `app.css` sets `#activity-body .data-table { font-size: 12px }`.
  - `phone tables stay cards on the canvas`: inside the `@container (max-width: 640px)` block, `.card-table tbody tr` has `background: var(--surface)` and `border-radius: 12px`, and a rule sets `.table-surface` to `background: none` and `padding: 0`.
  - `segmented controls nest their radii`: `.seg` `border-radius: 8px` and `padding: 2px`; `.seg button` `border-radius: 6px`.
  - `the 404 section is a column on the canvas with one action`: `.not-found` has no `border`; `.not-found-code` is `60px`, `line-height: 1`, colour `var(--text-muted)`; the `data-page="not-found"` slice of `index.html` has exactly one `<a` (class contains `rail-button`) and no `→`.
  - `batch 3 markup never uses the page kit`: the Home slice (`data-page="home"` to `data-page="portfolio"`) and Holdings slice (`data-page="holdings"` to `data-page="returns"`) of `index.html`, and the source of `renderHome`, `renderHoldings` in `pages.js` (slice from `function renderHome` to the next `\nfunction ` / `\nasync function `), contain no `page-section`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL (classes missing, tables 12px, `.seg` radius).
- [ ] **Step 3: Implement** the rules above; drop the `#activity-body .data-table { font-size: 12px }` override; in the 404 section keep `.not-found`, `#not-found-title`, `.not-found-code`, `.not-found-action` (centred column, 64px top margin, no box, sub 14px, one primary button 40px high).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS (all batch 1 to 3 tests too).
- [ ] **Step 5: Commit** `git commit -m "style(frontend): shared page kit, quieter tables and the 404 section"`

### Task 2: Portfolio, Reports and Markets on the page kit

**Files:**
- Modify: `frontend/js/pages.js` (`renderPortfolio` and its helpers, `renderReports`, `renderMarkets`, `moverTable` and any shared section helper they call: templates and class names only), `frontend/index.html` (`data-page="markets"` section), `frontend/css/app.css` (Markets, Reports, Portfolio rules)
- Test: `tests/js/design.test.js`

**Interfaces:**
- Consumes: Task 1 classes. A section helper used by Home as well must not change its output for Home: add a parameter or a separate helper (`pageSection(title, body, { id, actions })` returning `<section class="page-section" ...>`), whichever keeps Home's markup byte-for-byte.

- [ ] **Step 1: Write the failing tests:**
  - `Portfolio, Reports and Markets sections are page sections`: the sources of `renderPortfolio`, `renderReports`, `renderMarkets` (and `moverTable`) contain `page-section` and no `class="panel` except the `.empty-panel` path (`notice()`); every `data-table` they emit sits inside `table-surface`.
  - `Markets: forms on the surface, live questions end in a caret`: the `data-page="markets"` slice of `index.html` has no `→`, no `class="panel`, `ph-caret-right` appears 4 times, the two forms' sections carry `surface-block`, and the "Live questions" section carries `page-section`.
  - `Reports: downloads are plain buttons`: `.download-actions` exists; `.downloads-card` (if still used) has no `border`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.** Portfolio: KPI strip unchanged (`.kpi-strip-5`), then "Net worth trend" as a page section with the chart on the canvas and its period `.seg` in the head, then a two-column row (`.page-columns`) of "Allocation by asset type" (donut and its Bars/Donut/Data buttons on the canvas) and "Concentration" (`.fact-list` rows 14px with `--line` row rules, no card). Reports: `.kpi-strip-3`, "Net worth over time" section, "Change by period" and "Month-end" sections with tables in `.table-surface`, Downloads section. Markets: KPI strip, "Top gainers" / "Top losers" sections side by side (tables in `.table-surface`), market-cap donut section, the two forms as `surface-block`s (24px padding, controls 40px), "Live questions" as a page section with the existing `.prompt-rows` (replace each `.prompt-row-arrow` text `→` with the `ph-caret-right` sprite icon Home uses, keeping the `.prompt-row-arrow` class).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `node --check frontend/js/pages.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Portfolio, Reports and Markets as sections on the canvas"`

### Task 3: Goals

**Files:**
- Modify: `frontend/js/pages.js` (`renderGoals`, `goalCard`, `goalRing`, `gapPanel`, `fundBlock` / `fundComparisonHtml`, the allocations table and saved-context renderers: templates only), `frontend/css/app.css` (the "Goals" block, "Goal planning", "Saved context"), `frontend/icons.svg` only if `ph-caret-down` is missing
- Test: `tests/js/design.test.js`

**Interfaces:**
- Consumes: Task 1 classes. Keeps every hook: `#add-goal`, `[data-goal-panel]`, `.goal-toggle` with `aria-expanded` and `aria-controls`, `.goal-panel[hidden]`, `[data-edit-goal]`, `[data-delete-goal]` (or whatever `pages.js` reads today), `data-prompt`, `.alloc-table`, `#goal-investments-body`, `#saved-context-body`.

- [ ] **Step 1: Write the failing tests:**
  - `Goals: cards are surfaces with no borders, drafts and behind goals marked without one`: `.goal-card` has `background: var(--surface)`, `border-radius: 12px`, no `border`; no rule matches `.goal-card.is-behind` or `.goal-card.is-draft` with `border`.
  - `Goals: the callout is not a left-border block`: `.goal-callout` has no `border-left`/`border`, has `background: var(--surface)`, 14px text and a `::before` 8px dot in `var(--warning)`.
  - `Goals: no inner borders`: `.goal-gap-options li`, `.fund-item`, `.verdict-chip`, `.context-item`, `.goal-section`, `.goal-foot` have no `border` / `border-top` (`.verdict-chip` tones use background `--green-soft` / `--amber-soft` / `--red-soft` with the `-text` colours).
  - `Goals: disclosures use a sprite caret and fade in`: no `content: "▾"` in a `.goal-toggle` rule; the `goalCard` source contains `ph-caret-down` inside the toggle and still contains `data-goal-panel`, `aria-expanded` and `hidden`; `.goal-toggle[aria-expanded="true"] .icon` rotates 180deg with `transition: transform var(--dur-move) var(--ease)`; `.goal-panel:not([hidden])` has `animation:` naming a keyframe that the reduced-motion block redefines with opacity only.
  - `Goals: reading text is 14px`: `.goal-panel` font-size `14px`; `.goal-checks` `14px`.
  - `Goals: allocations and saved context are page sections`: the sources rendering `#goal-investments-body` and `#saved-context-body` contain `page-section`; the allocations table sits in `table-surface`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.** Goal card: `--surface`, 24px padding, ring 72px with `stroke-width: 6` and 14px/600 label, name 18px/28px, status `.tone-pill` as today (the pill and ring carry "behind" and "draft"); figures 12px labels over 16px/600 values; `.goal-checks` 14px with the warning dot; gap panel `--surface-2` radius 12px, options `--surface-3` radius 8px; footer separated by 16px of space, no rule. Toggles: no border, `--text-soft`, hover `--surface-2`, expanded `--surface-2` with `--text`; the caret is `<svg class="icon">` `ph-caret-down` (12px) after the label. Panel: `--surface-2`, radius 12px, 16px padding, 14px text, fades in (`panel-in`: opacity 0 and `translateY(-4px)` to none, `var(--dur-fade) var(--ease)`). Inside panels nested tables and fund items sit on `--surface`. Allocations ("Your investments and goals") and "What the assistant can use" become page sections; context items `--surface-2` rows radius 8px; the decision form controls 40px. The phone `.alloc-table` rows below 640px become `--surface` blocks radius 12px with 12px 16px padding instead of row rules.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `node --check frontend/js/pages.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): Goals cards, disclosures and panels without borders"`

### Task 4: Returns & tax

**Files:**
- Modify: `frontend/js/pages.js` (`renderReturns`, `renderTax`, `returnsTabs` and their helpers: templates only), `frontend/css/app.css` (Returns and tax rules, the benchmark chips block near "Returns: the benchmark chips")
- Verification data is made in the scratchpad (Step 5), never committed
- Test: `tests/js/design.test.js`

**Interfaces:**
- Consumes: Task 1 classes. Keeps `data-returns-tab`, `data-returns-benchmark`, `data-tax-fy`, `data-open-profile`, `.filter-chip`, `.planning-list`, `.card-table`.

- [ ] **Step 1: Write the failing tests:**
  - `Returns: sections on the canvas, no strip nested in a surface`: the sources of `renderReturns` and `renderTax` contain `page-section`, emit their tables inside `table-surface`, and never put `kpi-strip` inside a `class="panel` or `surface-block` element (assert no `panel` class in those sources except via `notice()`).
  - `Returns: the tab bar is an underline bar`: the rule for the Returns/Tax tab buttons (selector containing `[data-returns-tab]` or the class `returnsTabs()` emits) has no `background` on the selected state; the selected tab has `border-bottom: 2px solid var(--accent)` and the others `border-bottom: 2px solid transparent` (14px text like Holdings' tabs); no slide animation.
  - `Tax: exemption meter and planning list are flat`: the exemption meter track is `--surface-2` radius 9999px with an `--accent` fill; `.planning-list li` (or its card class) has `--surface` background, radius 12px, no `border`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.** Returns: KPI strip, "Compared with a benchmark" section with chips above a plain figure row (no nested surface), "By fund" section with the `.card-table` in `.table-surface`, NAV notes 12px. Tax: FY chips, KPI strip, exemption meter (12px track), "Gains by rule" table, losses and carry-forward, excluded funds, planning cards, the "Add your tax slab" secondary button. Status chips keep their tones.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `node --check frontend/js/pages.js`. Expected: PASS.
- [ ] **Step 5: Verification data (not committed).** The demo answers `/api/returns` and `/api/tax` with "Not available in the demo". In the scratchpad write `returns_fixture.py` that runs `mf_returns.returns_from(...)` and `mf_tax.tax_from(...)` on `tests/fixtures/mf_tax_sample.py` (`SCHEMES`, `NAVS`, `TODAY`; see how `scripts/eval_workflows.py` feeds them) and prints both JSON bodies (`{available: true, ...}` in the route's shape, `main.py` `/api/returns` and `/api/tax`). In the browser, wrap `window.fetch` to answer those two paths (and `?fy=`, `?benchmark=`) from the printed JSON, then re-navigate to `#/returns` and `#/returns/tax`. Expected: both tabs render with the sample figures, dark and light, 1440 and 390px. Report in the task summary what was seen.
- [ ] **Step 6: Commit** `git commit -m "style(frontend): Returns and tax as sections, flat meter and planning cards"`

### Task 5: Calculators

**Files:**
- Modify: `frontend/js/pages.js` (`renderPlanning`, `renderCalcOutput`: templates only), `frontend/css/app.css` (Calculators block, `.calc-field`, range inputs)
- Test: `tests/js/design.test.js`

**Interfaces:**
- Consumes: Task 1 classes. Keeps `#calc-tabs`, `[data-calc]`, `role="tab"`, `#calc-body`, every input `id`/`name` the calculators read, `.kpi-strip-3`.

- [ ] **Step 1: Write the failing tests:**
  - `Calculators: inputs on a surface block, results on the canvas`: `renderPlanning`'s source puts the inputs in `surface-block` (not `class="panel`) and the "Year by year" chart and tax table in `page-section` (table inside `table-surface`).
  - `Calculators: fields read at 14px and sliders take the accent`: `.calc-field > span` 14px weight 500; a rule for `.calc-field input[type="range"]` sets `accent-color: var(--accent)`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.** Inputs card: `surface-block`, heading 18px/28px, each field a 14px label, number box (40px) and slider; results: `.kpi-strip-3`, chart section, tax table (`total-row` 600 weight, no rule under it), note 12px, actions (primary + secondary, 40px). Keep the `@container (max-width: 900px)` stacking.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(frontend): calculators with inputs on the surface and results on the canvas"`

### Task 6: Activity, run and investigation cards, All chats

**Files:**
- Modify: `frontend/js/pages.js` (`renderActivity`, `activityItem`, `runItem`, the detail view, `renderAllChats` / chat-group template), `frontend/js/trace.js` (`renderInvestigationCard`, `renderRunCard`, `renderRunList`: class names only, no wording or figure change), `frontend/css/app.css` (Activity block, evidence card, run card, All chats block, skeletons)
- Test: `tests/js/design.test.js`, `tests/js/trace.test.js` (only if a class name it asserts changes)

**Interfaces:**
- Consumes: Task 1 classes. Keeps `#activity-body`, `#activity-live`, `#activity-refresh`, `#activity-run`, the filter `.filter-chip`s, `.activity-item`, `.activity-title`, `.status-pill`, `.timeline-marker`, `.evidence-card`, `.run-card`, `#all-chats`, `.chat-group`, `.chat-select`, `[data-clear-chat-search]`, and everything `trace.test.js` asserts.

- [ ] **Step 1: Write the failing tests:**
  - `Activity: rows and records are borderless surfaces`: `.activity-item`, `.activity-skeleton`, `.evidence-card` (and `#activity-body .evidence-card`), `.run-card` have no `border` and use `background: var(--surface)` with radius 12px; `.activity-item:hover` is `var(--surface-2)`.
  - `Activity: page-size reading text`: `#activity-body .evidence-summary`, `.activity-outcome`, `#activity-body .timeline-summary` are `14px`; meta lines stay 12px.
  - `Activity: lists are page sections`: `renderActivity` source wraps the investigations and answers lists in `page-section`; the back link keeps `.activity-back` and has no `→`.
  - `All chats: period groups are labelled lists on the canvas`: `.chat-group-card` has no `background` and no `padding` (or the template no longer emits it and emits `page-section`); the period heading `.chat-group` is 14px/20px weight 500 `--text-soft`; chat rows hover `var(--surface-2)`.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement** per the tests. Skeleton rows keep their `skeleton-fade` loop (the reduced-motion block already stops loops). Evidence and run cards: 24px padding, `<details>` sections separated by space, not rules, summary 16px/24px 600.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `node --check` on `pages.js` and `trace.js`. Expected: PASS.
- [ ] **Step 5: Verification data (not committed).** The demo has answer runs but no investigation. Using a completed investigation record from `tests/js/trace.test.js` (copy it to a scratchpad JSON), wrap `window.fetch` in the browser to add it to `/api/investigations?limit=50` and answer `/api/investigations/<id>`, then open `#/activity` and `#/activity/<id>`. Expected: list row and evidence card render in both themes at 1440 and 390px.
- [ ] **Step 6: Commit** `git commit -m "style(frontend): Activity rows and records, All chats as labelled lists"`

### Task 7: Public pages

**Files:**
- Modify: `frontend/css/public.css`, `app/public_pages.py` (`_page()` head: theme-color metas, Geist link, `public.css?v=3`; markup classes in `privacy_page`, `terms_page`, `pricing_page`, `not_found_page` only if needed)
- Test: `tests/js/design.test.js`, `tests/test_public_pages.py`

**Interfaces:**
- Produces in `public.css`: a light `:root` block and a dark `:root` block inside `@media (prefers-color-scheme: dark)` with the names `--canvas`, `--surface`, `--surface-2`, `--line`, `--text`, `--text-soft`, `--text-muted`, `--accent`, `--accent-text`, `--accent-ink` (button text on the accent), `--loss-text`, `--warning-text`, all equal to `app.css`'s light (`:root`) and dark (`:root[data-theme="dark"]`) values. `admin.css` (Task 8) builds on these names.
- Produces in `design.test.js`: `mediaTokenBlock(css, media, selector) -> Map` (the custom properties of `selector` inside the `@media (...)` block whose condition is `media`).

- [ ] **Step 1: Write the failing tests.** `design.test.js`:
  - `public pages carry the app's token values in both themes`: for each name above, `tokenBlock(readCss('public.css'), ':root')` equals `tokenBlock(readCss(), ':root')`, and `mediaTokenBlock(readCss('public.css'), 'prefers-color-scheme: dark', ':root')` equals `tokenBlock(readCss(), ':root[data-theme="dark"]')`.
  - `public pages: Geist, flat sections, no shadows`: `public.css` `body` uses `var(--canvas)` and a `'Geist'` family and no `Inter`/`-apple-system` first; `section` rules have no `border` and no `background`; `.plan` has `background: var(--surface)`, radius 12px; `.plan-featured` has no `box-shadow` (its one allowed `border: 1px solid var(--accent)`); `:focus-visible` outline `2px solid var(--accent)`; `.public-code` 60px `--text-muted`.
  - Extend `transitions use the motion tokens` to read `public.css` and `admin.css` too.
  `tests/test_public_pages.py`:
  - `test_public_pages_load_geist_and_the_new_theme_colours`: for `/privacy`, `/terms`, `/pricing` and an unknown path, the HTML contains `family=Geist:wght@400;500;600;700`, `<meta name="theme-color" content="#FFFFFF" media="(prefers-color-scheme: light)"`, `content="#000000" media="(prefers-color-scheme: dark)"`, `/css/public.css?v=3`, and not `#0a1a15` or `#f4f7f5`.
- [ ] **Step 2:** Run `node --test tests/js/design.test.js` and `python -m pytest -q tests/test_public_pages.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Body 16px/26px on the canvas; brand row 18px/600 with the icon at radius 8px; `h1` 36px/40px 600 `-0.02em` (30px below 640px); `h2` 20px/28px 600; sections are text on the canvas separated by 32px, no cards; meta and lead 14px/16px `--text-soft`; primary button `--accent` background with `--accent-ink` text, 40px, radius 8px, `:active` scale(.98) (and none under reduced motion); links `--accent-text`. Pricing: plans as `--surface` blocks radius 12px 24px padding, price 30px/36px 600 `-0.02em`, featured plan with the one accent border. 404: the column like the app's not-found section. Add `<link rel="preconnect">` to `fonts.googleapis.com` / `fonts.gstatic.com` and the Geist stylesheet link (same URL as `index.html`), and `@media (forced-colors: active)` keeps buttons visible (`border: 1px solid ButtonText`).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `python -m pytest -q tests/test_public_pages.py`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "style(public): privacy, terms, pricing and 404 on the app's tokens and Geist"`

### Task 8: Admin page and its in-app confirmations

**Files:**
- Modify: `frontend/js/admin.js`, `frontend/css/admin.css`, `app/public_pages.py` (`admin.css?v=2`, `admin.js?v=4`)
- Create: `tests/js/admin.test.js`
- Test: `tests/js/design.test.js`, `tests/test_admin.py` (run only; extend if it pins the old busters)

**Interfaces:**
- Produces in `admin.js` (exported as `module.exports` when `module` exists; `start()` runs only when `typeof document !== 'undefined'`, mirroring `ui.js`):
  - `ADMIN_CONFIRMS = { signOut: { title, body, confirmLabel, danger }, deleteProfile: { title, body, confirmLabel, danger, field } }` with `{name}` placeholders. Copy: signOut title "Sign {name} out everywhere?", body "Every browser signed in to this profile is signed out. Their data stays.", label "Sign out everywhere", danger false. deleteProfile title "Delete {name}?", body "This deletes the profile and everything it owns: chats, goals, snapshots and the INDmoney connection. It cannot be undone.", label "Delete profile", danger true, field "Type the profile name to confirm".
  - `adminConfirmCopy(key, values) -> { title, body, confirmLabel, danger, field? }` (placeholders filled; unknown key throws).
  - `confirmResult(action, typed) -> null | { confirmed: true, typed?: string }`: `action` is `'confirm'` or anything else (`'cancel'`, `''` for Esc, `'backdrop'`); returns `null` unless `'confirm'`; `typed` is passed through unchanged (no trim, no comparison with the name).
  - `askAdmin(key, values) -> Promise<null | {confirmed, typed?}>`: builds one `<dialog class="admin-dialog">` (title, body, optional labelled `<input>`, Cancel and the confirm button named by `confirmLabel`, danger style when `danger`), `showModal()`, resolves through `confirmResult` on close, removes the dialog, returns focus to the button that opened it. Backdrop click and Esc cancel.
- `profileActions()` uses `askAdmin('signOut', {name})` and `askAdmin('deleteProfile', {name})`; on a result it calls the same `act(...)` as today with `confirmName: result.typed`.

- [ ] **Step 1: Write the failing tests.** `tests/js/admin.test.js`:
```js
const admin = require('../../frontend/js/admin.js');
test('admin confirmations name the action and the profile', () => {
  const copy = admin.adminConfirmCopy('deleteProfile', { name: 'Arjun' });
  assert.equal(copy.title, 'Delete Arjun?');
  assert.equal(copy.confirmLabel, 'Delete profile');
  assert.equal(copy.danger, true);
  assert.equal(copy.field, 'Type the profile name to confirm');
  assert.equal(admin.adminConfirmCopy('signOut', { name: 'Arjun' }).confirmLabel, 'Sign out everywhere');
  assert.throws(() => admin.adminConfirmCopy('nope', {}));
});
test('only the confirm button confirms, and the typed name is sent as typed', () => {
  assert.equal(admin.confirmResult('cancel', 'Arjun'), null);
  assert.equal(admin.confirmResult('', 'Arjun'), null);
  assert.equal(admin.confirmResult('backdrop'), null);
  assert.deepEqual(admin.confirmResult('confirm', ' arjun '), { confirmed: true, typed: ' arjun ' });
  assert.deepEqual(admin.confirmResult('confirm'), { confirmed: true });
});
```
  `design.test.js`: extend `no browser dialogs in the app scripts` to `admin.js` and to `prompt(`; `admin.css` has `.admin-dialog` with `border-radius: 16px` and a `::backdrop` rule; `.admin-tile` has no `border` (tiles read as the KPI strip: one `--surface` block of figures, `.admin-tiles` `background: var(--surface)`, radius 12px); `.admin-table th` 12px weight 500; buttons have no `border-color: var(--accent)` hover (hover shifts background only); `.admin-danger` uses `--loss-text`.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: FAIL (`require` runs the IIFE against no document; functions missing; `confirm(` and `prompt(` present).
- [ ] **Step 3: Implement** the exports, the dialog and the restyle (tables like `.data-table`, badges as 12px `--surface-2` chips radius 6px, warn badges `--warning-text`, sign-in block a centred column like the app's gate, message line `--surface` radius 8px). Bump the busters in `public_pages.py`.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js`, `node --check frontend/js/admin.js`, `python -m pytest -q tests/test_admin.py tests/test_public_pages.py`. Expected: PASS.
- [ ] **Step 5: Browser check (not committed).** Put `ADMIN_EMAILS=admin@example.test`, `GOOGLE_CLIENT_ID=dummy`, `GOOGLE_CLIENT_SECRET=dummy` in the worktree `.env`, restart `ca-mode`, open `/admin` (sign-in state). Then wrap `window.fetch` to answer `/api/admin/session` (`{admin: {email: ...}}`) and `/api/admin/overview` with a small made-up overview in the shape `admin.overview()` returns (read `app/admin.py`), and re-run the script by appending `<script src="/js/admin.js?x=1">`. Open both dialogs: Esc and Cancel send no request (`read_network_requests`), Delete sends `confirmName` exactly as typed. If this proves impractical, say so in the task summary; the PR must not claim it was browser-checked.
- [ ] **Step 6: Commit** `git commit -m "feat(admin): in-app confirmations and the redesign tokens"`

### Task 9: Cache-busters, docs, browser pass, screenshots, PR

**Files:**
- Modify: `frontend/index.html` (every `?v=`), `frontend/sw.js` (`CACHE_NAME`), `.claude/CLAUDE.md` (Frontend notes: the page kit classes, Goals/Returns/Activity/All chats/calculator/Markets/Reports looks, the 404 section, public.css token copy and parity lint, admin dialog and its exports, `admin.test.js`, the "Confirmations" note no longer excepting `admin.js`)
- Create: `docs/superpowers/screenshots/redesign-batch-4/*.jpg`

- [ ] **Step 1:** Bump every `?v=` in `index.html` and `CACHE_NAME` in `sw.js`; run every check in Global Constraints. Expected: all pass.
- [ ] **Step 2:** In the built-in browser on `/?demo` (`ca-mode` from `.claude/launch.json`, restarted after `index.html` edits) at 1440, 1100 and 390px, dark and light, and once with reduced motion emulated: Portfolio, Goals (open a disclosure), Returns and Tax (with the Task 4 data), a calculator, Markets, Reports, Activity list and an answer run (and the Task 6 investigation), All chats, an unknown hash (404 section), `/privacy`, `/pricing`, `/terms`, an unknown path, `/admin`. Glance at Home and Holdings for regressions. Fix anything wrong in a commit before screenshots.
- [ ] **Step 3:** Save screenshots (JPEG, as batches 1 to 3) under `docs/superpowers/screenshots/redesign-batch-4/`: at least `portfolio-dark-1440`, `goals-dark-1440`, `goals-light-390`, `returns-dark-1440`, `tax-light-1440`, `calculator-dark-1100`, `markets-dark-1440`, `reports-light-1440`, `activity-dark-1440`, `chats-dark-390`, `not-found-dark-1440`, `privacy-light-1440`, `pricing-dark-1440`, `admin-dark-1440`.
- [ ] **Step 4:** Update `.claude/CLAUDE.md`, commit docs and screenshots, push the branch and open the PR "Redesign batch 4: remaining pages, public and admin" (body: what changed per page, how Returns/Activity/admin were checked with injected data, what still looks old).
