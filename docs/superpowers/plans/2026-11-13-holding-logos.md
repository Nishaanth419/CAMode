# Holding Logos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every holding row shows its company or fund-house logo (letter tile when none), fetched and cached on the server automatically after snapshots and broker fetches.

**Architecture:** A new shared module `app/logos.py` owns logo keys, the `logos` table, the on-disk cache and two sources (INDmoney `get_mf_funds_details` `image_url` for mutual funds, logo.dev by ticker for stocks). Snapshot capture and live broker fetches schedule a background fill of missing logos; holdings payloads gain a `logo` path only for cached files, served by `GET /api/logos/{key}`. The frontend reuses the broker logo / monogram fallback.

**Tech Stack:** FastAPI, SQLite (`app/db.py` `_SCHEMA`), httpx, vanilla JS (`finance.js`, `pages.js`), pytest, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-13-holding-logos-design.md`

## Global Constraints

- CSP stays `img-src 'self' data: blob:`; the browser never loads a logo from another host.
- A lookup sends only a ticker (logo.dev) or fund ids (INDmoney, with the profile's own token during its own capture); never a profile id, amount, units or a person's holding list.
- A logo failure never raises out of, slows or changes the result of `capture_snapshot()`, `fetch_holdings()` or any route.
- Downloads: https only, at most `MAX_LOGO_BYTES = 200_000`, `LOGO_TIMEOUT_SECONDS = 10`, PNG/JPEG/WebP by magic bytes only (no SVG), at most `LOGO_CONCURRENCY = 4` at once.
- Retry: `missing` after `MISSING_RETRY_MS = 30 days`, `failed` after `FAILED_RETRY_MS = 1 day`.
- Env: `LOGO_DEV_TOKEN` (unset: no stock lookups), `LOGOS_ENABLED` (default true). `conftest.py` sets `LOGOS_ENABLED=false` and deletes `LOGO_DEV_TOKEN`.
- Logos are not linked to a profile: not exported, not cleared with history, not deleted with a profile.
- Keys come from asset type + code only, never from names.
- Frontend asset changes bump `?v=` in `index.html` and `CACHE_NAME` in `sw.js`.
- Commit messages follow Conventional Commits, no Claude attribution.

## Review Focus

1. A code with path characters or odd case (`../x`, `infy`, `BRK.B`, spaces): the key must be normalised, and the file name must stay inside the logos directory. Test in Task 1.
2. A logo.dev or INDmoney response that is HTML, SVG, empty, a redirect to http or bigger than the cap: stored as `failed`, never written to disk. Test in Task 1 (`download`) and Task 2.
3. The same missing key requested by two profiles' captures at once: fetched once (in-process lock per key). Test in Task 2.
4. A deleted or truncated cache file whose row says `found`: no `logo` on the holding and the route 404s, and the next fill fetches it again. Test in Task 3.
5. ISIN-only stock rows (Zerodha `investment_code` = ISIN) and US rows: ISINs get no key (letter tile), `US_STOCK` maps to `us:`. Test in Task 1.

---

### Task 1: Logo keys, storage and safe download

**Files:**
- Create: `app/logos.py`
- Modify: `app/db.py` (`_SCHEMA`: `logos` table)
- Test: `tests/test_logos.py`

**Interfaces:**
- Produces:
  - `logo_key(asset_type: str | None, code: str | None) -> str | None`: `MF` with a code of digits → `mf:<code>`; `IND_STOCK`/`STOCK` (via `portfolio_math.canonical_asset_type`) with a symbol matching `^[A-Z0-9&.-]{1,20}$` after `.strip().upper()` and not ISIN-shaped (`brokers.ISIN_RE`, or a local copy of the same pattern to avoid an import cycle) → `in:<SYMBOL>`; `US_STOCK` → `us:<TICKER>`; else None.
  - `logos_dir() -> Path`: `db.DATA_DIR / "logos"` (created on demand).
  - `file_name(key: str) -> str`: `key.replace(":", "-")` plus extension; keys are already restricted to `[a-z]{2}:[A-Z0-9&.-]+`, `&` and `.` replaced so no `..` survives.
  - `status_of(key: str) -> dict | None`, `record(key: str, status: Literal["found","missing","failed"], file: str | None, now_ms: int) -> None`, `is_due(key: str, now_ms: int) -> bool` (no row, or past the retry window for its status; `found` is never due unless its file is gone).
  - `cached_path(key: str) -> Path | None`: the file when the row is `found` and the file exists and is non-empty.
  - `async download(url: str, client: httpx.AsyncClient) -> tuple[bytes, str] | None`: (content, extension) or None for non-https, non-200, over the cap (stream and stop), or not PNG/JPEG/WebP magic bytes. Follows redirects only to https.
  - `with_logos(holdings: list[dict]) -> list[dict]`: adds `"logo": "/api/logos/" + urllib.parse.quote(key, safe=":")` (so `in:M&M` becomes `in:M%26M`) to each holding whose key has a `cached_path`; returns the same list. One DB read for all keys.
  - `enabled() -> bool` (`LOGOS_ENABLED`).
- Schema: `CREATE TABLE IF NOT EXISTS logos (key TEXT PRIMARY KEY, status TEXT NOT NULL, file TEXT, checked_at INTEGER NOT NULL)` with a comment that it is shared and holds no profile data.

- [ ] **Step 1: Write the failing tests** in `tests/test_logos.py`:
  - `test_logo_key_cases`: `("MF","120503")→"mf:120503"`, `("STOCK"," infy ")→"in:INFY"`, `("IND_STOCK","M&M")→"in:M&M"`, `("US_STOCK","brk.b")→"us:BRK.B"`, `("IND_STOCK","INE009A01021")→None`, `("MF","INF179K01UT0")→None`, `("IND_STOCK","../x")→None`, `(None,"INFY")→None`.
  - `test_file_name_stays_inside_dir`: for every key above that is not None, `(logos_dir() / file_name(key)).resolve().parent == logos_dir().resolve()` and `".." not in file_name(key)`.
  - `test_download_accepts_png_and_rejects_others` (httpx `MockTransport`): PNG bytes → `(bytes, "png")`; SVG text, HTML, empty body, 404, `http://` URL, a redirect to `http://`, 200_001 bytes → None.
  - `test_retry_windows`: `missing` not due at +29 days, due at +30; `failed` due after 1 day; `found` with its file deleted is due.
  - `test_with_logos_only_cached`: two holdings, one key `found` with a file, one `missing` → only the first gets `logo == "/api/logos/in:INFY"`.
- [ ] **Step 2:** Run `python -m pytest tests/test_logos.py -q`; expected: fails (module missing).
- [ ] **Step 3:** Implement the interfaces above and the schema.
- [ ] **Step 4:** Run `python -m pytest tests/test_logos.py -q`; expected: pass.
- [ ] **Step 5:** Commit `feat(logos): logo keys, cache table and safe download`.

### Task 2: Sources and automatic fill

**Files:**
- Modify: `app/logos.py`, `app/snapshots.py` (`capture_snapshot`), `app/brokers.py` (`fetch_holdings`), `tests/conftest.py`, `.env.example`
- Test: `tests/test_logos.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `async fill_fund_logos(fund_ids: list[str], call: Callable[[str, dict], Awaitable[str]], *, now_ms: int | None = None) -> int`: due `mf:` keys only, batches of 10 joined by commas as `{"fund_ids": "a,b"}` to `call("get_mf_funds_details", ...)`; reads `data[].fund_id` and `data[].data.fund_detail.image_url`; a fund with no `image_url` is `missing`, a failed download `failed`; a failing call marks that batch `failed`. Returns how many were saved.
  - `async fill_stock_logos(keys: list[str], *, now_ms: int | None = None) -> int`: only when `LOGO_DEV_TOKEN` is set; due `in:`/`us:` keys; URL `LOGO_DEV_URL` (module constant) built per key. The builder confirms logo.dev's ticker form for NSE symbols (e.g. a `.NS` suffix) from its docs and records it in a comment. 404 → `missing`.
  - `schedule_fill(holdings: list[dict], *, fund_call=None) -> None`: when `enabled()`, starts one background `asyncio` task (kept in a module set so it is not garbage-collected) that runs both fills; every exception is caught and logged as `logo_fill_failed` with `error=type(exc).__name__`. A per-key in-process `asyncio.Lock` stops two fills fetching the same key.
- Hooks:
  - `snapshots.capture_snapshot`: after `store_snapshot`, build holdings with `portfolio_math.all_holdings(results)` and call `logos.schedule_fill(holdings, fund_call=...)`, where `fund_call` is a closure that calls `call_mcp_tool` with this capture's token and session on a fresh `httpx.AsyncClient` (the capture's client is closed by then), and only if `get_mf_funds_details` is in `available`.
  - `brokers.fetch_holdings`: in the live-success branch, `logos.schedule_fill(payload["holdings"])` (stocks only; no INDmoney call).
- `conftest.py`: `monkeypatch.setenv("LOGOS_ENABLED", "false")`, `monkeypatch.delenv("LOGO_DEV_TOKEN", raising=False)`.
- `.env.example`: commented `LOGO_DEV_TOKEN=` and `LOGOS_ENABLED=true` with one line each.

- [ ] **Step 1: Write the failing tests:**
  - `test_fill_fund_logos_batches_and_records`: 12 fund ids, a fake `call` returning `tests/fixtures/indmoney_funds.details()`-shaped JSON whose `image_url` points at a MockTransport PNG → 2 calls (10 + 2), 12 `found` rows; a fund missing from the reply → `missing`.
  - `test_fill_fund_logos_skips_fresh_rows`: a `found` key is not requested again.
  - `test_fill_stock_logos_needs_token`: no token → 0 requests; with token, 200 PNG → `found`, 404 → `missing`, SVG → `failed`, and the request URL contains the ticker and token only.
  - `test_same_key_fetched_once`: two `fill_stock_logos(["in:INFY"])` gathered → one request.
  - `test_capture_survives_logo_failure`: `LOGOS_ENABLED=true`, `fill_*` patched to raise → `capture_snapshot` returns the stored snapshot as before; with the fill patched to record calls, it receives the MF fund ids from the fixture snapshot.
  - `test_broker_fetch_schedules_fill`: a live Kite fetch (existing fake in `tests/test_brokers.py`) calls `schedule_fill` with its holdings and returns an unchanged payload.
- [ ] **Step 2:** Run `python -m pytest tests/test_logos.py -q`; expected: the new tests fail.
- [ ] **Step 3:** Implement the interfaces and hooks.
- [ ] **Step 4:** Run `python -m pytest tests/test_logos.py tests/test_snapshots.py tests/test_brokers.py -q`; expected: pass.
- [ ] **Step 5:** Commit `feat(logos): fetch fund and stock logos after snapshots and broker fetches`.

### Task 3: Serving logos and `logo` on holdings

**Files:**
- Modify: `app/main.py`, `app/snapshots.py` (`portfolio_figures`), `app/brokers.py` (`rows_view`)
- Test: `tests/test_logos.py`

**Interfaces:**
- Consumes: `logos.with_logos`, `logos.cached_path`.
- Produces: `GET /api/logos/{key}`: profile required (`profile_required()` otherwise); a key not matching `logo_key`'s output shape or with no `cached_path` → 404 JSON; else `FileResponse` with the type from the extension and `Cache-Control: private, max-age=604800`. Declared before any catch-all. `snapshots.portfolio_figures()` and `brokers.rows_view()` wrap `holdings` with `logos.with_logos()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_logo_route`: signed out → 401 `profileRequired`; signed in, cached PNG → 200 `image/png` with the cache header; unknown key, `in:..%2Fx` → 404.
  - `test_portfolio_and_broker_holdings_carry_logo`: with a cached `in:INFY`, `/api/portfolio` holdings for INFY have `logo`, others none; `/api/holdings/combined` INDmoney source the same.
  - `test_deleted_file_drops_logo`: row `found`, file removed → no `logo`, route 404, `is_due` True.
- [ ] **Step 2:** Run the tests; expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest -q`; expected: whole suite passes.
- [ ] **Step 5:** Commit `feat(logos): serve cached logos and add them to holdings`.

### Task 4: Logos in the holdings table

**Files:**
- Modify: `frontend/js/finance.js` (export `holdingLogoHtml`), `frontend/js/pages.js` (`holdingsTable`, ~line 688), `frontend/js/app.js` (the image-error swap at ~line 1382 also covers `.holding-logo`), `frontend/css/*.css` (the file holding `.broker-logo`), `frontend/index.html` (`?v=`), `frontend/sw.js` (`CACHE_NAME`)
- Test: `tests/js/finance.test.js`

**Interfaces:**
- Produces: `holdingLogoHtml(item) -> string`: when `item.logo` is a string starting with `/api/logos/`, `<img class="broker-logo holding-logo" src=… alt="" width="24" height="24" loading="lazy" data-monogram="X">`; else `monogramHtml(monogram(item.name || item.code))` with `holding-logo` added. Escaped like `brokerLogoHtml`.
- `holdingsTable()` puts it before `.holding-name` in the name cell, inline with the name (flex row), in both full and compact tables.

- [ ] **Step 1: Write the failing tests** in `tests/js/finance.test.js`: logo path → `<img` with that `src` and `data-monogram="I"` for "Infosys"; no logo → monogram span with "I"; `logo: "https://evil.example/x.png"` → monogram, no `<img`; a name with `<script>` is escaped.
- [ ] **Step 2:** Run `node --test tests/js/*.test.js`; expected: fail.
- [ ] **Step 3:** Implement, style (24px, rounded, `flex-shrink: 0`, gap matching `.broker-head`), bump cache-busters.
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `for file in frontend/js/*.js; do node --check "$file"; done`; expected: pass. Start the app (`preview_start`) and check `#/holdings` shows tiles/logos with no console errors.
- [ ] **Step 5:** Commit `feat(frontend): show logos beside holdings`.

### Task 5: Privacy notice and docs

**Files:**
- Modify: `app/privacy.py` (`NOTICE`, `POLICY_VERSION`, history comment), `.claude/CLAUDE.md`, `README.md` (env var), `docs/hosting.md` ("Upgrading an existing server": add `LOGO_DEV_TOKEN` to `deploy/.env`)
- Test: `tests/test_logos.py`

**Interfaces:**
- `POLICY_VERSION = "2026-11-13.2"` with comment `# 2026-11-13.2: holding logos (tickers sent to logo.dev, fund ids to INDmoney, shared logo files).`
- New `NOTICE` section titled `"Logos"` stating: the server looks up each stock's ticker at logo.dev and each mutual fund's id at INDmoney (with your INDmoney connection), never with your name, amounts or other holdings; logo images are kept on the server and shared by every profile; they are public images, not part of your data, so export and deletion do not include them; with no logo.dev key set, stocks show a letter instead.

- [ ] **Step 1: Write the failing test** `test_notice_mentions_logos`: a `NOTICE` section titled "Logos" whose body contains `"logo.dev"`, `"ticker"` and `"INDmoney"`.
- [ ] **Step 2:** Run it; expected: fail.
- [ ] **Step 3:** Update the notice and version; add `app/logos.py` to CLAUDE.md's module list, `LOGO_DEV_TOKEN` / `LOGOS_ENABLED` to its env list and `test_logos.py` to its test coverage; record what INDmoney's `image_url` turned out to be (fund house or scheme).
- [ ] **Step 4:** Run `python -m compileall -q app tests && python -m pytest -q && node --test tests/js/*.test.js`; expected: all pass.
- [ ] **Step 5:** Commit `docs(privacy): add the logos notice section`.
