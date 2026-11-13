# Holding logos

## Why
Holdings (Indian stocks, US stocks, mutual funds) are easier to scan with each company's or fund house's logo. Logos must keep themselves up to date: a new stock or fund in the demat gets its logo without anyone adding it by hand.

## Goals
- Each holding row (Holdings page table, broker tabs, Home/rail lists that use `holdingsTable()`) shows a logo, or a letter tile when there is none.
- New holdings get a logo after the next snapshot or broker holdings fetch, with no manual step.

## Non-goals / anti-goals
- No logo is ever loaded by the browser from another site (CSP stays `img-src 'self'`).
- A logo lookup never sends a profile id, an amount, units or a list of one person's holdings to anyone.
- A logo failure never breaks or delays a snapshot, a holdings fetch or a page.

## Approaches
1. **Ticker lookup at a logo service (chosen).** Mutual funds use INDmoney's own `image_url`; stocks use logo.dev by ticker. Fully automatic; the service sees only a ticker from the server. Needs a free key and a privacy notice line.
2. Logos bundled with the app (~NIFTY 500 + top US). Nothing goes outside, but new or small listings stay letter tiles until someone adds them. Fails "updates by itself".
3. Bundled first, service for the rest. More moving parts for little privacy gain over 1.

## Design
- **`app/logos.py`**, shared across profiles (logos are public, not personal data):
  - Keys: `mf:<fund id>` (the MF row's `investment_code`), `in:<NSE symbol>`, `us:<ticker>`; derived from asset type + code (`portfolio_math.canonical_asset_type()`), never from names.
  - Files in `<APP_DATA_DIR>/logos/<safe key>.<ext>`; a `logos(key, status, file, checked_at)` table (`found` / `missing` / `failed`). `missing` is retried after 30 days, `failed` after a day.
  - Download rules: https only, size cap (~200 KB), content checked by magic bytes as PNG/JPEG/WebP (no SVG), short timeout, a few at a time.
- **Sources**
  - Mutual funds: during a profile's snapshot capture, the MF fund ids with no logo row go to INDmoney `get_mf_funds_details` (batched `fund_ids`, the profile's token, the same MCP session) and each `image_url` is downloaded.
  - Stocks: `https://img.logo.dev/ticker/<ticker>?token=LOGO_DEV_TOKEN` (exact form for NSE symbols to be verified by the builder against logo.dev's docs). With no token set, stocks get letter tiles.
- **Triggers**: after a successful `snapshots.capture_snapshot()` and a successful live `brokers.fetch_holdings()`, a background task fetches missing logos; it never blocks or fails them.
- **Serving**: `GET /api/logos/{key}` serves only cached files (no outbound fetch from a request), profile required, long `Cache-Control`. `/api/portfolio` holdings and broker holdings rows gain `logo` (the path) only when a file exists.
- **Frontend**: `holdingsTable()` shows a 24px logo before the name, reusing the broker logo / `monogram()` letter-tile fallback (failed image → letter tile). Bump `?v=` and `sw.js` `CACHE_NAME`.
- **Config**: `LOGO_DEV_TOKEN` (unset: no stock lookups), `LOGOS_ENABLED` (true). `conftest.py` turns lookups off.
- **Privacy**: a notice section ("Logos": tickers sent to logo.dev from the server, fund ids to INDmoney, files kept on the server for everyone) and a `POLICY_VERSION` bump. Logos are not in the export, history clearing or profile deletion (not linked to a profile), like page views.
- **Tests**: key derivation, download checks (size, type, https), retry windows, MF batch from a fake INDmoney, logo.dev from a fake server, triggers never failing the capture/fetch, the route, `logo` on holdings, notice wording; a Node test for the logo/letter-tile HTML.

## Left to the builder
- logo.dev's exact ticker format for NSE/BSE symbols, and whether `investment_code` for broker rows (ISIN) needs mapping to a symbol first; ISIN-only rows without a symbol get letter tiles.
- Whether INDmoney's `image_url` is a fund-house or a per-scheme image (either is fine; record what it is in CLAUDE.md).
