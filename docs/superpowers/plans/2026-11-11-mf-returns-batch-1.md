# Mutual fund returns, batch 1 (ledger, lots, XIRR, AMFI NAVs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a sealed mutual fund transaction ledger from detailed CAMS/KFintech CAS uploads and show true returns (FIFO lots, XIRR) per fund and for the portfolio, valued at AMFI's daily NAV, in the app (`#/returns`), the API (`GET /api/returns`) and the Portfolio Analyzer (`get_mf_returns`).

**Architecture:** `cas.parse_statement()` adds a `ledger` (per-scheme transactions, folio numbers replaced by a salted HMAC) to the statement it already seals. `mf_ledger.build()` merges stored statements (newest wins per folio and ISIN) and assigns each scheme a status. `mf_math` (pure `Decimal`) turns transactions into lots and cash flows and solves XIRR; `amfi` keeps a plain table of today's NAVs downloaded by the scheduler; `mf_returns` joins the three into the figures the route, page and tool show.

**Tech Stack:** Python 3.12, FastAPI, SQLite (`connect_db()`), `Decimal`, `httpx`, `casparser` 1.4.1 models in tests; vanilla JS (`finance.js` helpers, `pages.js` route), `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-11-mf-returns-and-tax-design.md` (sections 1, 2 and the batch-1 parts of 5 and 6).

## Global Constraints

- Money, units and NAVs are `Decimal` end to end; JSON carries them as exact strings in stored data and as numbers rounded to 2 dp (units 3 dp, NAV 4 dp, percentages 2 dp, all `ROUND_HALF_UP`) only in API/tool output.
- Missing is never zero: an unknown figure is `None`/`null` with a `reason`; every excluded scheme carries a reason.
- The folio number is never stored: `folioKey` = HMAC-SHA256(salt, folio number) hex.
- Never kept from a statement: PAN, KYC fields, nominees, advisor/ARN, investor name, email, address, mobile, DP/client IDs, transaction `description`, `balance`, `dividend_rate`, `gift_folio`, the PDF and its password.
- Statuses and reasons exactly: `ok`, `incomplete_history` ("History before <period start> is missing; upload a since-inception CAS."), `unreconciled` (units do not add up, tolerance 0.001 units), `unsupported` (reason names the transaction type: `GIFT_IN`, `GIFT_OUT`, `SEGREGATION`, `MISC`, `UNKNOWN` with non-zero units), `nav_stale` (NAV older than `fund_data.stale_days()`).
- Upload hint copy: "For returns and tax, upload a detailed CAS from CAMS or KFintech covering since inception."
- XIRR: Newton from 0.10, bisection fallback on [-0.9999, 10]; under 365 days from first flow to valuation date gives `xirr: None`, reason `under_one_year`, plus `absoluteReturnPct`.
- `MF_DATA_ENABLED` (default true; `tests/conftest.py` sets `"false"`), `AMFI_NAV_URL` (default `https://www.amfiindia.com/spages/NAVAll.txt`); requests carry the generic user agent `CA-Mode/1.0` and nothing about any user. No network in tests.
- Reads need a profile and an unlocked session (`history_gate(user)`), no consent.
- Figures are information: no "you should", "sell", "buy" wording in UI or tool payloads.
- Tool payloads under 5,500 characters.
- Conventional Commits; no Claude co-author trailer or "Generated with" line.

## Deviation from the spec (needs your OK)

The spec puts the per-profile folio salt in `users.profile` meta. `users.update_profile()` replaces the whole stored profile and `DELETE /api/context` clears it, so the salt would be lost and the same folio would get a different key on the next upload (breaking newest-wins merging). This plan stores it in a new sealed column `users.folio_salt` (added via `_ADDED_USER_COLUMNS`), never exported, deleted with the profile. Everything else follows the spec.

Two additions the spec implies but does not list: each ledger scheme also keeps `valuation: {date, nav, value}` (needed for the statement-NAV fallback), and the per-scheme output carries `amfiCategory` (batches 2 and 3 key on it).

## Review Focus

1. **Fully redeemed scheme** (`close == 0`): current value 0 is a real zero, realised gain counts, its flows stay in portfolio XIRR; it must not be reported as missing. Test in Task 4.
2. **Several purchases on one date** (SIP plus lump sum) with one or more stamp-duty rows: stamp duty is split across that date's purchases in proportion to amount; a stamp-duty row with no purchase that date is added to the scheme's `problems` as a note, not lost silently. Test in Task 2.
3. **Scheme without an ISIN** (`Scheme.isin is None`): merge key falls back to `amfi:<code>` then `name:<scheme>`; NAV falls back to the statement. Test in Task 3.
4. **AMFI NAV older than the statement NAV** (AMFI download failing for days): the newer of the two dates is used and `navSource` says which. Test in Task 4.
5. **Flows with no sign change or no root** (a fund worth 0 after one purchase, all flows on one day): `xirr` returns `None` with reason `no_sign_change` / `no_convergence`, never raises. Test in Task 2.

---

### Task 1: AMFI NAV table and daily download

**Files:**
- Create: `app/amfi.py`, `tests/test_amfi.py`
- Modify: `app/db.py` (schema: `amfi_navs`), `app/snapshots.py:425-460` (`run_scheduler`), `tests/conftest.py` (`MF_DATA_ENABLED=false`), `.env.example`

**Interfaces:**
- Produces:
  - `NavRow` (frozen dataclass): `isin: str, scheme_code: str, scheme_name: str, amfi_category: str | None, nav: Decimal, nav_date: date`
  - `parse_nav_file(text: str) -> list[NavRow]` (one row per ISIN; a line with both ISINs yields two rows)
  - `store_navs(rows: list[NavRow]) -> int` (`INSERT OR REPLACE` in one transaction; returns count)
  - `navs_for(isins: Collection[str]) -> dict[str, NavRow]`
  - `is_enabled() -> bool` (`MF_DATA_ENABLED`)
  - `async refresh_if_due(now: datetime, *, fetch: Callable[[], Awaitable[str]] | None = None) -> bool` (downloads once per local date in `SNAPSHOT_TIMEZONE`; a failed try waits `RETRY_SECONDS = 3600`; state in memory; returns True when it stored rows)
  - `reset_state() -> None` (tests)

- [ ] **Step 1: Write failing tests in `tests/test_amfi.py`** over a fixture string in the real layout (header line `Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date`, blank lines, headings `Open Ended Schemes(Equity Scheme - Large Cap Fund)` and `Open Ended Schemes(Debt Scheme - Liquid Fund)`, AMC name lines, data lines `120716;INF179K01VK6;-;HDFC Index Fund Nifty 50 Plan - Direct;250.1234;10-Nov-2026`):
  - `test_parse_reads_category_from_the_section_heading` → `amfi_category == "Equity Scheme - Large Cap Fund"`, AMC lines ignored.
  - `test_both_isin_columns_map_to_the_row` → payout and reinvestment ISINs both present with the same NAV; `-` skipped.
  - `test_na_and_non_numeric_navs_are_skipped` (`N.A.`, empty, `abc`).
  - `test_store_and_lookup` → `navs_for(["INF179K01VK6", "UNKNOWN"])` returns only the known one with `Decimal("250.1234")`, `date(2026, 11, 10)`.
  - `test_refresh_runs_once_a_day_and_retries_hourly` with a fake `fetch` (raises once, then succeeds): no second call within the hour after failure; one call after an hour; no call later the same day after success; a call next day.
  - `test_refresh_is_off_when_disabled` (conftest default) → `fetch` never called.
- [ ] **Step 2: Run** `python -m pytest tests/test_amfi.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** Table in `app/db.py` schema: `CREATE TABLE IF NOT EXISTS amfi_navs (isin TEXT PRIMARY KEY, scheme_code TEXT NOT NULL, scheme_name TEXT NOT NULL, amfi_category TEXT, nav TEXT NOT NULL, nav_date TEXT NOT NULL)` (NAV as exact text, date ISO). Heading rule: a line without `;` matching `^(Open|Close|Interval)[^()]*\((.+)\)\s*$` sets the category; other lines without `;` are AMC names. Date parsed with `%d-%b-%Y`. Default fetch: `httpx.AsyncClient(timeout=30, headers={"User-Agent": "CA-Mode/1.0"})`, parse in `asyncio.to_thread`. Hook in `run_scheduler()` as its own `try/except` block logging `amfi_refresh_failed`, like `broker_history`. Add `MF_DATA_ENABLED` and `AMFI_NAV_URL` to `.env.example`.
- [ ] **Step 4: Run** `python -m pytest tests/test_amfi.py tests/test_snapshots.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(amfi): download daily NAVs from AMFI into a plain table`

### Task 2: Lots and XIRR (`mf_math`)

**Files:**
- Create: `app/mf_math.py`, `tests/test_mf_math.py`

**Interfaces:**
- Produces:
  - `Txn` (frozen dataclass): `date: date, type: str, amount: Decimal | None, units: Decimal | None, nav: Decimal | None` (casparser `TransactionType` names; amounts/units signed as casparser gives them: purchases positive, redemptions and switch-outs negative)
  - `Lot`: `buy_date, units, cost` · `Disposal`: `buy_date, sell_date, units, cost, sale_value, stt` · `FifoResult`: `open_lots: list[Lot], disposals: list[Disposal], cashflows: list[tuple[date, Decimal]], problems: list[str]`
  - `fifo(transactions: Sequence[Txn]) -> FifoResult` (cash flows from the investor's side: outflow negative)
  - `XirrResult`: `rate: Decimal | None, reason: str | None` (`no_sign_change`, `no_convergence`, `no_flows`)
  - `xirr(cashflows: Sequence[tuple[date, Decimal]]) -> XirrResult` (365-day year, flows on the same date summed)
  - `absolute_return_pct(cashflows) -> Decimal | None` = (inflows − outflows) / outflows × 100; `None` when outflows are 0
- Rules `fifo` must follow (spec section 2): buys = `PURCHASE`, `PURCHASE_SIP`, `SWITCH_IN`, `SWITCH_IN_MERGER`, `DIVIDEND_REINVEST`, and `REVERSAL` with positive units; sells = `REDEMPTION`, `SWITCH_OUT`, `SWITCH_OUT_MERGER`. Lot cost = amount + that date's `STAMP_DUTY_TAX` split across that date's buys by amount. Sale cash flow = |amount| − that date's `STT_TAX`; STT is shared across that date's disposals by units. `DIVIDEND_PAYOUT` is an inflow, not a disposal. `TDS_TAX` is kept out of cash flows (a tax credit). `REVERSAL` with negative units removes the newest lot with the same units and adds an inflow of |amount|; no such lot is a problem. Selling more than held by over 0.001 units is a problem, the shortfall is not booked and no lot goes negative.

- [ ] **Step 1: Write failing tests** (expected values hand-computed in comments):
  - `test_fifo_with_stamp_duty`: buy 2024-01-10 100 u for 10000 + stamp 0.50; SIP 2024-06-10 40 u for 5000 + stamp 0.25; redeem 2025-03-10 −120 u for −13200, STT 0.13. Disposals: (100 u, cost 10000.50, sale 11000, stt 0.13×100/120), (20 u, cost 2500.125, sale 2200); open lot 20 u cost 2500.125 dated 2024-06-10; realised gain 699.375; cash flows −10000.50, −5000.25, +13199.87.
  - `test_same_day_purchases_share_the_stamp_duty` (Review Focus 2): two buys of 3000 and 1000 on one day, stamp 0.20 → costs 3000.15 and 1000.05; a lone stamp row on a date without a buy → one entry in `problems`.
  - `test_switch_in_and_out_are_lots_and_disposals`, `test_dividend_reinvest_adds_a_lot`, `test_dividend_payout_is_cash_not_a_disposal`, `test_negative_reversal_cancels_the_matching_lot`.
  - `test_overselling_is_a_problem_not_a_negative_lot`.
  - `test_xirr_matches_the_spreadsheet_example`: flows −10000 (2008-01-01), 2750 (2008-03-01), 4250 (2008-10-30), 3250 (2009-02-15), 2750 (2009-04-01) → `abs(rate − Decimal("0.373362535")) < Decimal("0.0001")` (Excel's documented XIRR, 37.34%).
  - `test_xirr_two_flows_closed_form`: −10000 (2024-01-01), +12100 (2026-01-01) → 0.0998565877 within 0.0001 (= 1.21^(365/731) − 1).
  - `test_xirr_negative_return` (−10000, +8000 two years later → −0.1056 within 0.0001), `test_xirr_without_sign_change` and `test_xirr_no_flows` (Review Focus 5), `test_xirr_falls_back_to_bisection` (monkeypatch the Newton step count to 0; same answer as the spreadsheet example).
  - `test_absolute_return` (−10000, +500 dividend, +10200 → 7.00).
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_math.py -q` → FAIL.
- [ ] **Step 3: Implement** in `app/mf_math.py` (no I/O; `Decimal` with `localcontext(prec=34)`; fractional powers via `Decimal.__pow__`; Newton stops at |NPV| < 1e-9 × total outflow or 100 iterations; bisection 300 iterations needs NPV signs to differ at the bounds, else `no_convergence`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mf): FIFO lots and XIRR in Decimal`

### Task 3: Ledger from detailed CAS (`mf_ledger`, `cas`)

**Files:**
- Create: `app/mf_ledger.py`, `tests/test_mf_ledger.py`
- Modify: `app/cas.py` (`parse_statement`), `app/db.py` (`_ADDED_USER_COLUMNS`: `folio_salt TEXT`, and `_USERS_COLUMNS`), `app/main.py:1242-1256` (`upload_cas`), `tests/test_cas.py`

**Interfaces:**
- Consumes: `mf_math.Txn` (Task 2).
- Produces:
  - `folio_salt(user_id: str, keys: ProfileKeys) -> bytes` (creates 32 random bytes sealed in `users.folio_salt` inside `BEGIN IMMEDIATE` if missing; raises `sealing.LockedError` when locked)
  - `folio_key_for(salt: bytes) -> Callable[[str], str]`
  - `extract(data: CASData, folio_key: Callable[[str], str]) -> dict | None` → `{"schemes": [{isin, amfi, name, amc, rta, folioKey, open, close, valuation: {date, nav, value}, transactions: [{date, type, amount, units, nav}]}]}` (numbers as exact strings, dates ISO); `None` unless `file_type` is CAMS/KFINTECH and `cas_type == "DETAILED"`
  - `LedgerScheme` (dataclass): the stored fields plus `transactions: list[Txn]`, `period_from: date`, `period_to: date`, `status: str`, `reason: str | None`
  - `build(statements: list[dict]) -> list[LedgerScheme]` (stored statements as `cas.all_statements()` returns them; key `(folioKey, isin or "amfi:"+amfi or "name:"+name)`; newest by `period_to`, then `uploadedAt`, wins; status from the Global Constraints order: `unsupported`, then `unreconciled`, then `incomplete_history`, else `ok`)
  - `ledger_for(user_id: str, keys: ProfileKeys) -> list[LedgerScheme]`, `has_ledger(user_id, keys) -> bool` (False when locked)
  - `cas.parse_statement(pdf, password, *, folio_key: Callable[[str], str] | None = None)`; adds `"ledger"` to the statement only when `folio_key` is given and `extract()` returns one.
  - Upload response gains `ledger: {"available": bool, "hint": str | None}`.

- [ ] **Step 1: Write failing tests** with fixtures built from casparser models (`Scheme`, `TransactionData`, `Folio`, `CASData`, as `tests/test_cas.py` does; folio `"12345/67"`, `PAN`, `KYC`, `advisor="ARN-1234"`, `nominees=["Nominee Name"]`):
  - `test_extract_keeps_transactions_and_nothing_personal` (no PERSONAL value, folio number, ARN, nominee or description text in `json.dumps(ledger)`).
  - `test_summary_and_demat_statements_have_no_ledger` (cas_type `SUMMARY`; NSDL).
  - `test_folio_keys_are_stable_per_profile_and_differ_across_profiles` (two signed-up profiles; salt sealed in the column).
  - `test_newest_statement_wins_per_folio_and_isin` (older statement with 2 transactions, newer with 3 → 3; a scheme only in the older one is kept).
  - `test_scheme_without_isin_merges_by_amfi_code` (Review Focus 3).
  - statuses: `test_open_units_mark_incomplete_history` (reason names the period start), `test_units_that_do_not_add_up_are_unreconciled`, `test_gift_transactions_are_unsupported` (reason names `GIFT_IN`), `test_clean_scheme_is_ok`.
  - in `tests/test_cas.py`: `test_upload_reports_ledger_availability` (detailed CAMS → `{"available": True, "hint": None}`; NSDL → `available: False` and the hint copy) and the stored statement contains `ledger`.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_ledger.py tests/test_cas.py -q` → FAIL.
- [ ] **Step 3: Implement.** `upload_cas` gets the salt with `mf_ledger.folio_salt(user["id"], user["keys"])` before parsing and passes `folio_key=mf_ledger.folio_key_for(salt)`.
- [ ] **Step 4: Run** → PASS, plus `python -m pytest tests/test_users.py tests/test_privacy.py -q` (column migration).
- [ ] **Step 5: Commit** `feat(cas): keep a sealed mutual fund ledger from detailed CAS`

### Task 4: Returns figures and `GET /api/returns`

**Files:**
- Create: `app/mf_returns.py`, `tests/test_mf_returns.py`
- Modify: `app/main.py` (route next to the CAS routes)

**Interfaces:**
- Consumes: `mf_ledger.ledger_for`, `mf_math.fifo/xirr/absolute_return_pct`, `amfi.navs_for`, `fund_data.stale_days()`.
- Produces:
  - `scheme_returns(scheme: LedgerScheme, navs: dict[str, NavRow], today: date) -> dict` with `name, isin, amc, amfiCategory, folioKey, status, reason, units, nav, navDate, navSource ("amfi" | "statement"), currentValue, invested, unrealisedGain, realisedGain, xirr, absoluteReturnPct, returnReason, heldSince, problems`. `currentValue` = close × NAV for every status. `invested`/gains/returns are `None` (with `returnReason`) for `incomplete_history`, `unreconciled`, `unsupported`. NAV: AMFI row unless the statement's valuation date is newer; then `nav_stale` if older than `stale_days()` (applies only to otherwise `ok` schemes).
  - `portfolio_returns(schemes: list[dict], flows: dict[key, list[tuple[date, Decimal]]], today) -> dict`: `totalValue` (all schemes), `includedValue`, `invested`, `unrealisedGain`, `realisedGain`, `xirr`/`absoluteReturnPct`/`returnReason` over pooled flows of `ok` + `nav_stale` schemes, `coverage: {pct, excluded: [{name, status, reason}]}`.
  - `returns_for(user_id: str, keys: ProfileKeys, today: date) -> dict`: `{available: True, schemes, portfolio, coverage, navDate, statementPeriod}` or `{available: False, hint}`.
  - Route `GET /api/returns` → `history_gate(user)` (no consent), `asyncio.to_thread(returns_for, ...)` with today in `SNAPSHOT_TIMEZONE`.

- [ ] **Step 1: Write failing tests:** a ledger from the Task 2 FIFO example plus AMFI NAV 130 on a fresh date → `currentValue` 2600, `invested` 2500.13, `unrealisedGain` 99.88, `realisedGain` 699.38, `xirr` equal to `mf_math.xirr` of the flows plus 2600 on the NAV date; `test_fully_redeemed_scheme_counts` (Review Focus 1); `test_short_holding_reports_absolute_return`; `test_statement_nav_used_when_newer_or_missing` and `test_old_nav_is_stale_but_counted` (Review Focus 4); `test_excluded_schemes_have_reasons_and_coverage`; `test_switch_between_two_schemes_cancels_in_portfolio_flows`; API: `test_returns_requires_profile_and_unlock`, `test_returns_without_ledger_gives_hint`, `test_returns_are_per_profile`, `test_returns_need_no_consent`.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_returns.py -q` → FAIL.
- [ ] **Step 3: Implement** (rounding only in the output dict).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mf): returns per fund and portfolio with GET /api/returns`

### Task 5: Agent tool `get_mf_returns`

**Files:**
- Modify: `app/snapshots.py` (`mf_returns_tool()` beside `cas_tool()`, offered in `portfolio_context()` when `mf_ledger.has_ledger()`), `app/tasks.py` (`ACCOUNT_LOCAL_TOOLS`), `app/agent.py` (`MF_RETURNS_RULES`, appended when `"get_mf_returns" in local_tools`), `frontend/js/trace.js:110` (label), `tests/test_mf_returns.py`

**Interfaces:**
- Consumes: `mf_returns.returns_for`.
- Produces: `LocalTool(name="get_mf_returns", input_schema={"type": "object", "properties": {"scheme": {"type": "string"}}, "additionalProperties": False})`. With `scheme` (case-insensitive name substring or ISIN) one scheme in full; else portfolio, coverage and a compact row per scheme; if over 5,500 chars, the largest schemes by value plus `omittedSchemes` count.
- `MF_RETURNS_RULES`: "get_mf_returns gives mutual fund returns computed in code from the user's detailed CAS. Quote its figures as given and never recompute them; give each figure's NAV date and source; say which funds are left out and why, and the coverage. These are information, not a recommendation to buy or sell."

- [ ] **Step 1: Write failing tests:** tool offered only with a ledger (and not when locked); payload under 5,500 chars with 60 schemes; `scheme` filter; `tasks.is_account_tool("get_mf_returns", ...)` is True; a scripted Portfolio Analyzer run (as in `tests/test_agent_graph.py`) quoting the XIRR passes the number check (no `numbers` warning) and the prompt contains `MF_RETURNS_RULES`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `python -m pytest tests/test_mf_returns.py tests/test_tasks.py tests/test_agent_graph.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(agent): get_mf_returns tool for Portfolio Analyzer`

### Task 6: Privacy notice and export

**Files:**
- Modify: `app/privacy.py` (`POLICY_VERSION = "2026-11-11.1"`, `NOTICE`), `tests/test_mf_ledger.py` or `tests/test_privacy.py`

- [ ] **Step 1: Write failing tests:** notice mentions the transaction history from a detailed CAS, that folio numbers are not stored, and the daily AMFI download sending nothing about the user; export (`POST /api/users/me/export`) has `casStatements[0]["ledger"]["schemes"][0]["folioKey"]` and no folio number; history clearing, private mode and profile deletion leave no ledger (`mf_ledger.has_ledger` False) and deletion removes `folio_salt` with the row.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (extend the existing CAS sentences in `NOTICE`; add one sentence on AMFI to the section on what the app fetches). **Step 4: Run** `python -m pytest tests/test_privacy.py tests/test_mf_ledger.py -q` → PASS (existing tests reading `POLICY_VERSION` stay green).
- [ ] **Step 5: Commit** `docs(privacy): describe the fund ledger and AMFI download`

### Task 7: `#/returns` page and CAS hint

**Files:**
- Modify: `frontend/js/finance.js`, `tests/js/finance.test.js`, `frontend/js/pages.js` (`ROUTES`, `ROUTE_META.returns = ['Returns & tax', 'True returns (XIRR) on your mutual funds from a detailed CAS.']`, `renderReturns()`, ids in the clear list), `frontend/index.html` (nav item after Investments, `<section class="page" data-page="returns">` with Returns tab only, `?v=` bumps), `frontend/js/app.js` (CAS upload status: hint when `ledger.available` is false, link to `#/returns` when true; a `#/returns` link in the CAS card), `frontend/sw.js` (`CACHE_NAME` `ca-mode-shell-v55`), `app/page_views.py` (`"returns"` in `Route`), `tests/test_page_views.py` if it lists routes

**Interfaces:**
- Produces (`finance.js`, exported for Node):
  - `returnFigure(item) -> {text, note}`: XIRR "12.34% a year (XIRR)"; under a year "5.10% (held under a year)"; else "Not available" with the reason text.
  - `returnStatus(status) -> {label, tone}`: `ok` none, `nav_stale` "Old NAV", `incomplete_history` "History missing", `unreconciled` "Units don't add up", `unsupported` "Not supported".
  - `navNote(item) -> string`: "AMFI NAV of 10 Nov 2026" / "Statement NAV of 30 Sep 2026".

- [ ] **Step 1: Write failing Node tests** for the three helpers, including `null` figures never rendered as 0.
- [ ] **Step 2: Run** `node --test tests/js/finance.test.js` → FAIL. **Step 3: Implement** helpers, page (portfolio XIRR tile with coverage, per-fund table, chips with reasons, date and source beside each figure; `{available:false}` shows the hint and a link to `#/investments`), CAS hint, nav, route literal, cache names.
- [ ] **Step 4: Run** `for f in frontend/js/*.js; do node --check "$f"; done && node --test tests/js/*.test.js && python -m pytest tests/test_page_views.py tests/test_public_pages.py -q` → PASS. Then open `#/returns` in the browser pane against a local server with a temp `APP_DATA_DIR` and a stubbed statement to check the page renders (no console errors).
- [ ] **Step 5: Commit** `feat(pages): Returns & tax page with fund XIRR`

### Task 8: Docs, full checks, PR

**Files:**
- Modify: `.claude/CLAUDE.md` (module entries for `amfi.py`, `mf_math.py`, `mf_ledger.py`, `mf_returns.py`; `amfi_navs` in the tables list; `folio_salt`; `GET /api/returns`; `#/returns`; `MF_DATA_ENABLED`, `AMFI_NAV_URL`; POLICY_VERSION history; test list)

- [ ] **Step 1:** Update `.claude/CLAUDE.md`.
- [ ] **Step 2: Run all checks:** `python -m compileall -q app tests && python -m pytest -q`, `for f in frontend/js/*.js; do node --check "$f"; done`, `node --test tests/js/*.test.js`, `python -m scripts.eval_tasks`, `python -m scripts.eval_workflows` → all green.
- [ ] **Step 3: Commit** `docs: describe the mutual fund returns modules`, push, open a PR to `main` (description notes it includes the spec commit a33a465 as its base and lists the folio-salt deviation).
