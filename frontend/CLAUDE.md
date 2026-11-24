# frontend/CLAUDE.md

Rules for editing `frontend/` (static HTML/CSS/JS, installable PWA, no build step). Page-by-page detail: `docs/architecture/frontend.md`.

- Bump the `?v=` cache-busters in `index.html` and `CACHE_NAME` in `sw.js` whenever an asset changes, or browsers and the service worker keep the old files.
- Script load order matters: `finance.js` first, then `trace.js`, `ui.js`, `app.js`, `pages.js` (uses `app.js` globals), `rail.js` last. `finance.js`, `trace.js` and `ui.js` are pure helpers exported for Node tests (`tests/js/`); keep DOM access out of them.
- The Content-Security-Policy blocks inline handlers: use `data-*` attributes and delegated listeners (`data-prompt`, `data-set-chart-view`, ...), never `onclick=`.
- The browser formats figures and never computes ones the server provides (plans, returns, tax, history come from the API). Missing is shown as "—" or a note, never 0.
- Update `TAX_RULES`, its label and `tests/js/finance.test.js` after each budget; goal maths in `finance.js` must match `app/goal_planning.py`.
- Icons are generated: edit `icon.svg` / `assets/brand/*.svg`, run `python -m scripts.build_icons` (macOS `sips`), commit the outputs.
- Tables over three columns become cards below 640px (`.card-table` needs a container page body); history charts go through `historyChartHtml()` / `renderDataChart()`.
- Check with `for f in frontend/js/*.js; do node --check "$f"; done` and `node --test tests/js/*.test.js`.
- Design rules are lint-checked by `tests/js/design.test.js`: only Geist fonts, colours as tokens defined with the same names in the light and dark `:root` blocks (no new hex values, `-text` tokens for text on a surface), font sizes and radii on the scale, no gradients or generic shadows, transitions on `--ease`/`--dur-*`, a reduced-motion block, `:focus-visible`, no `window.confirm`/`alert` (use `confirmAction()` with a key in `ui.js` `CONFIRMS`), icons from `icons.svg` (bump its `?v=` when the sprite changes). Token and icon detail: `docs/architecture/frontend.md`.
