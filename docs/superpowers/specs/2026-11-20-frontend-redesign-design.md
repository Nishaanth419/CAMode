# Frontend redesign: reskin and new app shell — design

Date: 2026-11-20. Status: approved; mockups approved 2026-11-20.

## Why

The app works, but it looks generated: Inter plus two more font families, 23 font sizes, about 15 radii, 40 generic
shadows, an acid-green-on-near-black dark theme, bordered rounded cards around every section, browser `confirm()`
boxes and almost no deliberate motion. Nishaanth wants it to read as a high-class product with the best experience
for the people using it (team release, then public), not as one more AI dashboard.

## Briefs, and which one wins

1. **elayadesign `redesign-existing-projects`** (github.com/elayadesign/redesign-skill), chosen by Nishaanth. Its
   *Design values* are the source of truth for fonts, dark surfaces, spacing, radius, icons, motion and type scale.
   Its audit decides what gets fixed. Nothing outside those values is invented without approval.
2. **Anthropic `frontend-design`** (anthropics/skills). Used for identity and restraint: one memorable element,
   motion only where it shows a change, no template chrome.

The builder does not need to fetch or re-run either skill: the audit was run on 2026-11-20 and its findings are the
"Why" section above (fonts, sizes, radii, shadows, gradients, uppercase/italic, `ease`/`linear` timings, one
reduced-motion rule, 9 `confirm()` and 1 `alert()`, a gradient shimmer, the sidebar and card kit). The values the
skill fixes are restated in "Design system" below; if they ever disagree with the skill, this document wins.

Where they disagree (settled, Nishaanth may overrule):

| Topic | elayadesign | frontend-design | Decision |
|---|---|---|---|
| Scroll reveals | every section rises in | reads as AI-made | One orchestrated entrance per page load; no per-section reveals on scroll |
| Hero text gradient | allowed on the hero heading | big number + gradient is the default | Only on the net-worth figure on Home |
| Mono for data | Geist Mono allowed | mono labels are a tell | Geist with `tabular-nums` for figures; Geist Mono only for code and ids |

## Goals

- One visual system across every screen: sign-in, Home, Holdings, Ask AI, Goals, Returns & tax, calculators,
  Markets, Reports, Activity, All chats, dialogs, public pages (`/privacy`, `/terms`, `/pricing`, 404) and `/admin`.
- A new shell: a sidebar that opens and closes, a command menu (Cmd/Ctrl+K) and slide-over panels instead of the fixed sidebar, top bar and
  centre dialogs.
- Motion that answers actions: opening, expanding, saving, deleting, an answer arriving, a page changing.
- Every state designed: loading (layout-shaped skeletons), empty (one action), error (what failed and what to do).

## Non-goals

- No framework, build step, Tailwind or new runtime dependency. Vanilla CSS and JS, as today.
- No change to any API, figure, calculation, privacy rule or agent behaviour.
- No new features. Copy changes only where the audit flags them.

## Anti-goals (a failure even if it ships)

- A figure shown wrong, rounded differently or animated through wrong values (no count-up on money).
- A regression in keyboard use, screen readers, reduced motion, phone layouts or the installed PWA.
- Breaking a JS hook: every `id`, `data-*` attribute and class that JS or tests read keeps working.
- Looking like any of the generated defaults: purple/blue gradients, acid accent on black, card-kit grids, ALL-CAPS
  eyebrows, mono labels, `→` on every link.

## Approaches considered

- **A. Reskin only.** Tokens, type, surfaces, states and motion on today's layout. Lowest risk; keeps the left-sidebar
  dashboard the audit flags as generic.
- **B. Reskin plus new shell (chosen by Nishaanth).** A, plus a new sidebar (top bar at first, changed to an open/close sidebar at the mockup review), command menu, slide-overs and custom
  confirmations. The shell is what people see all the time, so it carries most of the "high class" impression.
- **C. Rewrite in a framework.** Ruled out: the skill forbids migrating stacks and it would put 11k lines of tested
  behaviour at risk.

## Design system

### Colour

Dark is the default theme (as today). Dark surfaces come only from the elayadesign palette:

| Token | Value | Use |
|---|---|---|
| `--canvas` | `#000000` | page background |
| `--surface` | `#181818` | grouped content (replaces most cards: background only, no border, no shadow) |
| `--surface-2` | `#1F1F1F` | inputs, table headers, hovered rows |
| `--surface-3` | `#272727` | pressed, selected |
| `--line` | `#313131` | the few borders kept (inputs, tables, floating chrome) |

Confirmed at the mockup review on 2026-11-20 (not in the skill's values; mockups: https://claude.ai/artifact/6Yfq8NUdeJe2wH4vauAXuX):

- **Accent** (one only, saturation under 80%): muted brass `#C2A66B` for primary actions, focus rings, the active
  nav item and chart highlight. Chosen for a private-banking register rather than a trading-app green.
- **Meaning colours** (not accents, used only for figures): gain `#5FB38A`, loss `#E07A6A`, warning `#D9A54A`.
- **Light theme:** white `#FFFFFF` canvas, `#F6F6F5` surface, `#EFEFED` surface-2, `#E4E4E1` line, text `#0A0A0A`;
  same accent darkened to `#8A6F35` for contrast. Every pair checked to WCAG AA.
- Text on dark: `#FFFFFF` primary, `#A3A3A3` secondary, `#8A8A8A` muted (5.1:1 on `#181818`; `#7A7A7A` was 4.1:1 and failed AA).
- Shadows only on floating chrome, tinted to the canvas, one light source from above.
- Subtle grain overlay on the canvas (CSS-generated SVG noise, 3% opacity) instead of any gradient.

### Type

- **Geist** for everything (Google Fonts, weights 400, 500, 600, 700). Geist Mono only for code and run ids.
  Inter, Fraunces and JetBrains Mono are removed.
- Tailwind type scale only (12/16, 14/20, 16/24, 18/28, 20/28, 24/32, 30/36, 36/40, 48/1, 60/1). Every current
  size snaps to the nearest step below. Body 16px in chat answers, 14px in tables and controls.
- `font-variant-numeric: tabular-nums` on every figure, table and KPI.
- Negative tracking on 30px and up; sentence case everywhere; the 8 `uppercase` rules and 3 italics go.
- `text-wrap: balance` on headings, `pretty` on paragraphs; answers capped at about 65 characters per line.

### Space, radius, icons

- Spacing scale: 0, 2, 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96px. Main buttons 8px × 12px, `text-base` semibold.
- Radius tokens from Tailwind steps: 6px (inputs, chips), 8px (buttons), 12px (surfaces), 16px (slide-overs, command
  menu), 9999px only for the avatar and toggle tracks. Nested shapes use outer minus gap.
- **Phosphor** icons (regular, one stroke weight), copied as inline SVG into one sprite (`frontend/icons.svg`,
  MIT licence noted), replacing the 45 hand-drawn SVGs.

### Motion

- Tokens: `--ease: cubic-bezier(.22, 1, .36, 1)`, `--dur-move: 500ms`, `--dur-fade: 300ms`; nothing under 200ms.
  Only `transform` and `opacity` animate.
- **Page change:** the View Transitions API (`document.startViewTransition`) cross-fades the page body and slides the
  new one up 8px; browsers without it get a plain swap. The sidebar never moves.
- **Page entrance (the one orchestrated moment):** on Home, the net-worth figure fades in, then its history line draws
  left to right once, then the KPI row fades in. About 900ms in all, only on the first load of a session.
- **Answers:** a new answer rises 8px and fades; follow-up chips arrive 60ms apart.
- **Slide-overs and command menu:** slide from the right / scale from 0.98 with a dimmed backdrop; close reverses.
- **Press:** `scale(.98)` on buttons; hover shifts background only.
- **Disclosures** (goal panels, data zone): height via `::details-content` / `interpolate-size` where supported.
- `prefers-reduced-motion`: every movement becomes a 200ms fade; the line does not draw.

## The shell

Changed at the mockup review (2026-11-20, Nishaanth): a left sidebar that opens and closes replaces the floating
top bar.

```
 open (248px)                      closed (64px)
 ┌────────────────┬──────────      ┌────┬──────────────────
 │ ◆ CA Mode   [▯]│                │[▯] │
 │ [⌕ Search  ⌘K] │   page         │[⌕] │   page content,
 │ ⌂ Home         │   content      │ ⌂  │   max 1120px,
 │ ◔ Portfolio    │                │ ◔  │   left-aligned
 │ ≡ Holdings     │                │ ≡  │   headers
 │   …            │                │ …  │
 │ Recent chats   │                │    │
 │ (A) Arjun      │                │(A) │
 └────────────────┴──────────      └────┴──────────────────
```

- **Sidebar** replaces `#sidebar` and `#topbar` in place (the `#sidebar` id and its hooks are kept): the brand and the
  open/close button (panel icon, `aria-expanded`, label "Open sidebar" / "Close sidebar"); a "Search or ask ⌘K"
  button that opens the command menu; every route as an icon plus label, grouped by spacing only, no group labels
  (Home, Portfolio, Holdings · Ask AI, Activity, All chats · Goals, Returns & tax, Planning · Markets, Reports);
  Recent chats (newest 6, open only); the profile row at the bottom (avatar, name, plan) opening the profile panel.
  Background is the canvas with a `--line` border on the right. The active item has the `--surface-2` background,
  `--text` label and an accent icon.
- **Open and closed:** 248px open, 64px closed (icons only, the name as `title` and a tooltip on hover/focus). The
  page column widens or narrows over 500ms on the base curve, labels fade over 300ms; the one layout animation in
  the app. The state is remembered per browser in `localStorage` (try/catch, open by default) and toggles with the
  button or Ctrl/Cmd+B. Between 900px and 1280px it starts closed so Ask AI keeps room for its rail.
- **Status and theme:** the status pill and theme toggle move into the profile panel (theme also as a command).
- **Command menu** (⌘K / Ctrl+K, replaces the topbar search): go to any page, search chats (same data the search uses
  today), ask a question with either agent, and actions (refresh snapshot, add a goal, connect a broker, toggle theme,
  sign out). Arrow keys, Enter, Esc; `role="dialog"` with a listbox.
- **New chat** is a button on the Ask AI header, a command, and the `+` beside Recent chats.
- **Phones (< 900px):** the sidebar is hidden and the bottom tab bar stays (Home, Holdings, Ask, Goals, More),
  restyled; More opens the full open sidebar as a drawer from the left with its items arriving 60ms apart in
  reading order (under 400ms).
- **Slide-overs:** the profile dialog, goal editor and allocation editor become right-side panels (`<dialog>` kept, so
  focus trapping and Esc work as today). Full screen on phones.
- **Confirmations:** the 9 `window.confirm` calls become `confirmAction({title, body, confirmLabel, danger})` in
  `ui.js` (tested in `tests/js/ui.test.js`), a small dialog with the action named on the button ("Delete chat").
  The `window.alert` in `demo.js` becomes a toast.
- **Sign-in gate:** one centred column on the canvas, brand mark, the form; no card.

## Pages (what changes beyond the system)

- **Home:** the net-worth figure is the hero (60px, the gradient, the drawn line); KPI row as plain figures on the
  surface with no card borders; allocation as one stacked bar; holdings and insights as lists, not cards.
- **Holdings:** table on the surface with sticky header, row hover, tabs with the sliding underline; phone cards kept.
- **Ask AI:** answer column 720px, answers as text on the canvas (no bubble), questions as quiet surface blocks; the
  composer floats at the bottom; the rail keeps its three sections without card borders.
- **Goals, Returns & tax, calculators, Markets, Reports, Activity, All chats:** same treatment: surfaces by
  background, KPI rows as figures, tables restyled, `card-table` phone layout kept.
- **Charts:** Plotly layouts take colours and Geist from CSS tokens (read once from `getComputedStyle`), accent for
  the primary series, gain/loss for signed bars.
- **Public pages and admin:** `public.css` and `admin.css` take the same tokens and font.

## Constraints

- Keep every `id`, `data-*` hook and JS-read class; new structure wraps existing hooks.
- Content-Security-Policy: no inline handlers or styles added; fonts stay on Google Fonts, Plotly on its CDN.
- Bump `?v=` cache-busters and `CACHE_NAME` in `sw.js` each batch; add `icons.svg` to `APP_SHELL`.
- All existing checks pass every batch: `pytest`, `node --test tests/js/*.test.js`, `node --check`, both evals.
- The uncommitted chat-streaming edits on `fix/kite-mode-tools` are not touched. The redesign lands first (Nishaanth's
  call); the streaming work then rebases and styles its progress lines with the new tokens and motion. Batch 3 keeps
  today's `$$$` typing indicator, restyled, and does not design the progress state.

## Delivery

Before code (a gate, passed on 2026-11-20): Claude Design mockups of Home and Ask AI, dark and
light, for Nishaanth to approve. The accent, light theme, meaning colours and the net-worth gradient (dark `#FFFFFF`
→ `#9B9B9B`, light `#000000` → `#666666`, the skill's values) are confirmed or changed there and written back into
this document. All colours live as tokens in one `:root` block so a later change is one edit. The demo server's
env file is gone; rebuilding it, or using `?demo` on the normal server, is the builder's call. Then four batches, each its own session, branch from `main` and PR, verified in the browser
on the demo server (`ca-mode-demo` in `.claude/launch.json`) at desktop and phone widths, both themes:

1. **Foundations:** tokens, Geist, type scale, radius, surfaces, focus and press states, motion tokens, Phosphor
   sprite, `confirmAction()` and the toast, chart theming. Pages look different but keep their layout.
2. **Shell:** sidebar (open/close, remembered), command menu, phone tab bar and the More drawer, slide-overs, View Transitions, sign-in.
3. **Home, Holdings, Ask AI** (with the rail), including the Home entrance.
4. **Everything else:** Portfolio, Goals, Returns & tax, calculators, Markets, Reports, Activity, All chats, public pages, admin.

## Left to the builder

Exact selector structure, how the sprite is referenced, the command menu's ranking of results, skeleton shapes per
page, and copy edits the audit flags, all within the values above.
