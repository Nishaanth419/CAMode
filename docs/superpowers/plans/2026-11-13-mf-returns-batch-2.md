# Mutual fund returns, batch 2 (benchmarks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compare each mutual fund, and the whole mutual fund portfolio, with a benchmark index fund on the user's own cash flows (benchmark XIRR and the rupee difference), in `GET /api/returns?benchmark=`, the `#/returns` page and the `get_mf_returns` tool.

**Architecture:** `app/benchmarks.py` holds the five benchmark funds and the category mapping as data, keeps their NAV history in a plain `benchmark_navs` table (seeded from mfapi.in, topped up daily from the AMFI file batch 1 already stores) and is run by the scheduler. `mf_math.replay()` (pure `Decimal`) buys and sells benchmark units with the user's flows. `mf_returns` adds a `benchmark` block per scheme (its category's default) and for the portfolio (the chosen benchmark); the route, tool and page only pass the choice through and display.

**Tech Stack:** Python 3.12, FastAPI, SQLite (`connect_db()`), `Decimal`, `httpx`; vanilla JS (`finance.js` helpers, `pages.js`), `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-11-mf-returns-and-tax-design.md` (section 3, the batch-2 parts of 5 and 6, section 7). Batch 1 plan: `docs/superpowers/plans/2026-11-11-mf-returns-batch-1.md`; build on the shipped code where they differ.

## Global Constraints

- Everything from the batch-1 plan's Global Constraints still holds (Decimal end to end, 2 dp money / 4 dp NAV / 2 dp percent `ROUND_HALF_UP` only in output, missing is never zero, every exclusion has a reason, no "you should / sell / buy" wording, tool payload under 5,500 characters, no network in tests, Conventional Commits with no Claude co-author or "Generated with" line).
- Benchmark funds, verified on 2026-11-13 against the live AMFI `NAVAll.txt` (NAV date 12-Nov-2026) and `https://api.mfapi.in/mf/<code>`:

  | id | label | scheme code | ISIN | AMFI scheme name | mfapi history from |
  | --- | --- | --- | --- | --- | --- |
  | `nifty50` | Nifty 50 | `120716` | `INF789F01XA0` | UTI Nifty 50 Index Fund - Direct Plan - Growth | 2013-01-02 |
  | `nifty500` | Nifty 500 | `147625` | `INF247L01957` | Motilal Oswal Nifty 500 Index Fund - Direct Plan - Growth | 2019-09-11 |
  | `midcap150` | Nifty Midcap 150 | `147622` | `INF247L01916` | Motilal Oswal Nifty Midcap 150 Index Fund - Direct Plan - Growth | 2019-09-11 |
  | `smallcap250` | Nifty Smallcap 250 | `147623` | `INF247L01932` | Motilal Oswal Nifty Smallcap 250 Index Fund - Direct Plan - Growth | 2019-09-11 |
  | `liquid` | Liquid fund | `119800` | `INF200K01UT4` | SBI LIQUID FUND - Direct Plan - Growth | 2013-01-01 |

  Each is the direct-growth fund with the longest history on its index in the live file.
- `DEFAULT_BENCHMARK = "nifty50"`. `MAX_NAV_GAP_DAYS = 5`. `RESEED_AFTER_DAYS = 7`.
- `MFAPI_URL` (default `https://api.mfapi.in`), gated by `MF_DATA_ENABLED` like AMFI; user agent `amfi.USER_AGENT` (`CA-Mode/1.0`); requests name only the five fixed scheme codes.
- Page note, exact copy (`benchmarks.INDEX_NOTE`): "Benchmarks use a direct-growth index fund's NAV: it includes reinvested dividends and is net of a small expense ratio, so it approximates the total-return index."
- `POLICY_VERSION` becomes `2026-11-13.1`.

## Deviations from the spec (need your OK)

1. **Valuation date.** The spec values the benchmark at "the latest NAV". This plan values it on the same date as the user's figures (the scheme's or portfolio's NAV date), at the latest benchmark NAV on or before it within 5 days, so the two values compare like for like. Same rule as every flow.
2. **Category strings.** The live AMFI file has two spellings per category (`Equity Scheme - Large Cap Fund` and `Equity Schemes - Large Cap Fund`, `ELSS`, `Equity Schemes - ELSS- Tax Saver Fund`, ...) and index funds sit under `Other Scheme - Index Funds` / `Index Funds - Equity Funds` whatever their index. The mapping therefore keys on the normalised sub-category, and index funds are mapped by the index in their scheme name (Nifty 50, Sensex, Nifty 500, Midcap 150, Smallcap 250; any other index, including variants such as Next 50, Equal Weight or Momentum, gets no comparison with a reason).
3. **Stale history.** The spec seeds once. If AMFI downloads fail for more than `RESEED_AFTER_DAYS` (7), the gap would make every later flow `unavailable` for good, so a benchmark whose latest stored NAV is older than that is fetched from mfapi.in again (same request, hourly retry).

## Review Focus

1. **Benchmark history starting after the user's first flow** (Nifty 500 / Midcap 150 / Smallcap 250 start on 2019-09-11): the comparison is `unavailable` with "The <label> fund's NAV history starts on 11 Sep 2019, after this fund's first investment on <date>.", never a partial replay. Test in Task 2.
2. **Fully redeemed scheme** (value 0, no NAV date): replay ends at the last flow's date; a winner whose redemptions exceed the benchmark position is `not_comparable`, a loser shows a negative `differenceInr`. Test in Task 3.
3. **Benchmark not downloaded yet** (fresh install, mfapi.in unreachable): every comparison is `unavailable` with "Benchmark NAV history has not been downloaded yet."; the rest of `/api/returns` is unchanged and nothing 500s. Test in Task 3.
4. **The model passing an unknown benchmark** (`"sensex"`): the tool returns an error payload listing the valid ids, never figures for the default. Test in Task 3.
5. **Two flows on one date / same-day buy and redemption**: flows are summed per date before replay (as `xirr` does), so a same-day switch never trips the negative-position rule. Test in Task 2.

---

### Task 1: Benchmark data, category mapping and NAV history

**Files:**
- Create: `app/benchmarks.py`, `tests/test_benchmarks.py`
- Modify: `app/db.py` (schema: `benchmark_navs`), `app/snapshots.py` (`run_scheduler`, after the AMFI block), `.env.example`

**Interfaces:**
- Consumes: `amfi.is_enabled()`, `amfi.USER_AGENT`, the `amfi_navs` table (batch 1).
- Produces:
  - `Benchmark` (frozen dataclass): `id: str, label: str, scheme_code: str, isin: str, scheme_name: str`
  - `BENCHMARKS: tuple[Benchmark, ...]` (table above, in that order), `BENCHMARK_IDS: tuple[str, ...]`, `DEFAULT_BENCHMARK`, `INDEX_NOTE`, `get(benchmark_id: str) -> Benchmark | None`
  - `CATEGORY_BENCHMARK: dict[str, str]` (normalised sub-category → id) and `benchmark_for(amfi_category: str | None, scheme_name: str) -> tuple[str | None, str | None]` (id, or `None` with a reason)
  - `parse_mfapi(payload: dict, scheme_code: str) -> list[tuple[date, Decimal]]`
  - `store_navs(benchmark_id: str, rows: Iterable[tuple[date, Decimal]]) -> int`, `nav_history(benchmark_id: str) -> list[tuple[date, Decimal]]` (ascending), `latest_nav_date(benchmark_id: str) -> date | None`
  - `async seed_if_needed(now: datetime, *, fetch: Callable[[str], Awaitable[dict]] | None = None) -> list[str]` (ids stored)
  - `append_from_amfi() -> int`
  - `async refresh(now: datetime, *, fetch=None) -> None` (seed, then append in a thread; no-op unless `amfi.is_enabled()`), `reset_state() -> None`

- [ ] **Step 1: Write failing tests in `tests/test_benchmarks.py`:**
  - `test_benchmarks_are_the_verified_funds` → ids `("nifty50", "nifty500", "midcap150", "smallcap250", "liquid")`, codes `("120716", "147625", "147622", "147623", "119800")`, ISINs as in the table, every `scheme_name` ends `Direct Plan - Growth`, `get("x") is None`.
  - `test_category_mapping` (parametrised) over `(category, name) -> id`:
    `("Equity Scheme - Large Cap Fund", ...) -> "nifty50"`, `("Equity Schemes - Flexi Cap Fund", ...) -> "nifty50"`, `("ELSS", ...)`, `("Equity Schemes - ELSS- Tax Saver Fund", ...)`, Focused, Value, Contra, Dividend Yield → `nifty50`; `Equity Schemes - Multi Cap Fund`, `Equity Scheme - Large & Mid Cap Fund` → `nifty500`; `Equity Scheme - Mid Cap Fund` → `midcap150`; `Equity Schemes - Small Cap Fund` → `smallcap250`; `Debt Scheme - Liquid Fund`, `Income/Debt Oriented Schemes - Overnight Fund`, `Debt Scheme - Money Market Fund` → `liquid`; `("Other Scheme - Index Funds", "UTI Nifty 50 Index Fund - Direct Plan - Growth") -> "nifty50"`, `"NAVI ELSS TAX SAVER NIFTY50 INDEX FUND"` → `nifty50`, `"... S&P BSE Sensex Index Fund ..."` → `nifty50`, `"Axis Nifty 500 Index Fund"` → `nifty500`, `"DSP Nifty Smallcap 250 Index Fund"` → `smallcap250`, `"Kotak Nifty Midcap 150 Index Fund"` → `midcap150`.
  - `test_categories_without_a_benchmark_say_why` → `None` with a non-empty reason for `Hybrid Scheme - Aggressive Hybrid Fund`, `Debt Scheme - Gilt Fund`, `Equity Scheme - Sectoral/ Thematic`, `Other Scheme - FoF Overseas`, `Other Scheme - Gold ETF`, `Other Scheme - FoF Domestic`, `None` category ("The fund's AMFI category is not known."), and index funds `"Nifty Next 50 Index Fund"`, `"Nifty 50 Equal Weight Index Fund"`, `"Nifty Midcap 150 Momentum 50 Index Fund"`, `"Nifty Bank Index Fund"` (reason "This index fund tracks an index that has no benchmark here.").
  - `test_parse_mfapi` over `{"status": "SUCCESS", "meta": {"scheme_code": 120716}, "data": [{"date": "12-11-2026", "nav": "178.25610"}, {"date": "bad", "nav": "1"}, {"date": "11-11-2026", "nav": "0"}, {"date": "10-11-2026", "nav": "N.A."}, {"date": "02-01-2013", "nav": "37.40400"}]}` → `[(date(2013, 1, 2), Decimal("37.40400")), (date(2026, 11, 12), Decimal("178.25610"))]`; a payload whose `meta.scheme_code` differs, or whose `status` is not `SUCCESS`, raises `ValueError`.
  - `test_store_and_read_history` → re-storing a date replaces its NAV; `nav_history` ascending; `latest_nav_date`.
  - `test_seed_fetches_only_missing_or_stale_benchmarks` with a fake `fetch(code)` recording codes: first run fetches all five; with `nifty50` holding a NAV dated `now - 3 days` and `liquid` one dated `now - 8 days`, a later run fetches the other four including `119800` but not `120716`.
  - `test_seed_failure_waits_an_hour_per_benchmark` → a fetch raising for `147625` only: the other four are stored; no retry of `147625` within 59 minutes, one at 60.
  - `test_seed_is_off_when_disabled` (conftest default) → `fetch` never called.
  - `test_append_from_amfi_adds_todays_nav_to_seeded_benchmarks` → with `amfi.store_navs([NavRow("INF789F01XA0", "120716", ..., Decimal("178.2561"), date(2026, 11, 12)), NavRow("INF247L01957", "147625", ...)])` and only `nifty50` seeded: returns 1, `nifty50` gains the 2026-11-12 row, `nifty500` stays empty (seeding must not be blocked by one appended row).
  - `test_scheduler_refreshes_benchmarks` → monkeypatch `benchmarks.refresh` to record calls; one `run_scheduler` tick (follow the existing scheduler tests' stop-event pattern) calls it once with the clock's time; an exception in it is logged and the tick continues.
- [ ] **Step 2: Run** `python -m pytest tests/test_benchmarks.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.**
  - Table: `CREATE TABLE IF NOT EXISTS benchmark_navs (benchmark_id TEXT NOT NULL, nav_date TEXT NOT NULL, nav TEXT NOT NULL, PRIMARY KEY (benchmark_id, nav_date))` with a comment like `amfi_navs` (public data, nothing personal).
  - Normalised sub-category: text after the first `" - "` (whole string when none), lower-case, a trailing `" fund"` removed, `"elss- tax saver"` → `"elss"`. `CATEGORY_BENCHMARK` lists `large cap, flexi cap, elss, focused, value, contra, dividend yield → nifty50; multi cap, large & mid cap → nifty500; mid cap → midcap150; small cap → smallcap250; liquid, overnight, money market → liquid`. Exact match, never substring (`large & mid cap` must not hit `mid cap`).
  - Index funds: category contains `index fund`. Scheme name lower-cased with `nifty50` → `nifty 50`, `mid cap` → `midcap`, `small cap` → `smallcap`; any of `next`, `equal`, `momentum`, `quality`, `value`, `alpha`, `low vol` → no benchmark; else first match of `nifty 500`, `midcap 150`, `smallcap 250`, `nifty 50`, `sensex` (in that order).
  - Reasons when unmapped, chosen by the category's first word group: hybrid "Hybrid funds mix asset classes, so no single index fits."; debt / income "Only liquid, overnight and money market debt funds have a benchmark here (a liquid fund)."; sectoral or thematic "Sectoral and thematic funds have no broad index benchmark here."; FoF, overseas, gold, silver, ETF "Funds of funds, international and gold funds have no benchmark here."; anything else "No benchmark is set for the AMFI category <category>.".
  - Default fetch: `httpx.AsyncClient(timeout=30, headers={"User-Agent": amfi.USER_AGENT})`, `GET {MFAPI_URL.rstrip('/')}/mf/{code}`, no redirects followed, `raise_for_status()`, `.json()`. Parse and store in `asyncio.to_thread`. Retry state in memory per benchmark id (`failed_at`), `RETRY_SECONDS = amfi.RETRY_SECONDS`. Due = no rows or `latest_nav_date < now.date() - RESEED_AFTER_DAYS`. Log `benchmark_seeded` (id, rows) / `benchmark_seed_failed` (id, error type name).
  - `append_from_amfi()`: `SELECT nav, nav_date FROM amfi_navs WHERE isin = ?` per seeded benchmark (by ISIN, which is unique in `amfi_navs`); `INSERT OR REPLACE`.
  - Scheduler: own `try/except` after the AMFI block, logging `benchmark_refresh_tick_failed`. `.env.example`: `# MFAPI_URL=https://api.mfapi.in` under the AMFI lines, comment that only the five benchmark codes are requested.
- [ ] **Step 4: Run** `python -m pytest tests/test_benchmarks.py tests/test_snapshots.py tests/test_amfi.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(benchmarks): keep index-fund NAV history for return benchmarks`

### Task 2: Replaying the user's flows in a benchmark (`mf_math.replay`)

**Files:**
- Modify: `app/mf_math.py`, `tests/test_benchmarks.py`

**Interfaces:**
- Produces:
  - `MAX_NAV_GAP_DAYS = 5`
  - `nav_on_or_before(navs: Sequence[tuple[date, Decimal]], day: date, max_gap_days: int = MAX_NAV_GAP_DAYS) -> tuple[date, Decimal] | None` (`bisect` on the ascending dates)
  - `Replay` (frozen dataclass): `status: str` (`ok` / `unavailable` / `not_comparable`), `reason: str | None`, `units: Decimal | None`, `value: Decimal | None`, `nav: Decimal | None`, `nav_date: date | None`, `flows: list[tuple[date, Decimal]]` (the user's flows plus the benchmark's terminal value; empty unless ok), `difference: Decimal | None` (user value minus benchmark value)
  - `replay(cashflows: Sequence[tuple[date, Decimal]], navs: Sequence[tuple[date, Decimal]], end: date, value: Decimal, *, label: str = "benchmark") -> Replay`

- [ ] **Step 1: Write failing tests** (NAVs `2024-01-01: 100`, `2024-01-29: 125`, `2024-03-01: 120`, `2025-03-03: 150`):
  - `test_replay_on_sip_flows` → flows `(2024-01-01, -1000)`, `(2024-02-01, -1000)` (NAV from 29 Jan, 3 days back), `(2024-03-01, -1200)`, end `2025-03-03`, value `4500`: units `28`, value `4200`, nav `150`, `nav_date 2025-03-03`, difference `300`, `flows == cashflows + [(2025-03-03, 4200)]`, status `ok`.
  - `test_replay_uses_a_nav_up_to_five_days_back` → a flow on `2024-02-03` (5 days after 29 Jan) is ok; on `2024-02-04` (6 days) gives `unavailable` with reason `"No benchmark NAV within 5 days before 4 Feb 2024."`.
  - `test_replay_before_benchmark_history_is_unavailable` → a flow on `2023-12-01` gives `unavailable`, reason `"The Nifty 500 fund's NAV history starts on 1 Jan 2024, after this fund's first investment on 1 Dec 2023."` (`label="Nifty 500"`).
  - `test_replay_end_needs_a_recent_nav` → end `2025-03-20` (17 days after the last NAV) gives `unavailable` with the 5-day reason for 20 Mar 2025.
  - `test_redemption_larger_than_the_benchmark_position_is_not_comparable` → `(2024-01-01, -1000)`, `(2024-03-01, +1500)` (12.5 units against 10 held): status `not_comparable`, reason `"Redemptions on 1 Mar 2024 are more than the same investments would be worth in the benchmark, so the comparison does not hold."`, `units is None`, `value is None`.
  - `test_same_day_flows_are_summed_first` → `(2024-03-01, -1200)` and `(2024-03-01, +200)` on an empty position: net `-1000`, ok, units `1000/120`.
  - `test_replay_without_flows` → `unavailable`, reason `"No cash flows to compare."`.
- [ ] **Step 2: Run** `python -m pytest tests/test_benchmarks.py -k replay -q` → FAIL.
- [ ] **Step 3: Implement** in `mf_math.py` under `localcontext(prec=PRECISION)`: sum flows per date, walk ascending dates; for each, NAV by `nav_on_or_before` (a day before the first NAV gives the history-start reason, any other miss the 5-day reason); `units -= amount / nav` (outflows are negative so they add units); `units < 0` → `not_comparable`. Terminal NAV the same way at `end`. Dates in reasons as `%-d %b %Y` (write a small `_day(d)` helper, since `%-d` is not portable: `f"{d.day} {d:%b %Y}"`).
- [ ] **Step 4: Run** `python -m pytest tests/test_benchmarks.py tests/test_mf_math.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(mf_math): replay a fund's cash flows in a benchmark`

### Task 3: Benchmarks in returns, the API and the tool

**Files:**
- Modify: `app/mf_returns.py`, `app/main.py` (`GET /api/returns`), `app/snapshots.py` (`mf_returns_payload`, `mf_returns_tool`), `app/agent.py` (`MF_RETURNS_RULES`), `tests/test_mf_returns.py`

**Interfaces:**
- Consumes: `benchmarks.get()`, `benchmark_for()`, `nav_history()`, `INDEX_NOTE`, `BENCHMARKS`, `DEFAULT_BENCHMARK` (Task 1); `mf_math.replay()` (Task 2).
- Produces:
  - `returns_from(schemes, navs, today, *, benchmark: str = DEFAULT_BENCHMARK, benchmark_navs: dict[str, list[tuple[date, Decimal]]] | None = None) -> dict` and `returns_for(user_id, keys, today, benchmark: str = DEFAULT_BENCHMARK) -> dict` (loads `nav_history` for the chosen id and every scheme's default id). An unknown id raises `ValueError` (the route and tool check first).
  - Per scheme and on `portfolio`, a `benchmark` object: `{id, label, status, reason, xirr, absoluteReturnPct, returnReason, value, differenceInr, navDate}`; `id`/`label` `null` with `status: "none"` and the mapping reason when the category has none; `status: "excluded"`, reason `"Left out of the totals, so not compared."` for schemes not in `INCLUDED`; `status: "unavailable"`, reason `"Benchmark NAV history has not been downloaded yet."` when its history is empty.
  - Top level `benchmark: {id, label, schemeName, schemeCode, navDate, note: INDEX_NOTE, options: [{id, label}]}` for the chosen one (`navDate` = its latest stored NAV, `null` when none).
  - `mf_returns_payload(figures, scheme=None)` rows gain `benchmark: {id, xirr, absoluteReturnPct, differenceInr, status, reason}`; the base gains the top-level `benchmark` without `options`.
  - Tool input schema gains `benchmark: {"type": "string", "enum": list(BENCHMARK_IDS)}`.

- [ ] **Step 1: Write failing tests in `tests/test_mf_returns.py`** (helper `bench_navs(id, {date: nav})` building the `benchmark_navs` dict; reuse the file's `ledger()`/`nav()` helpers; the existing `scheme()` fixture's category is `Other Scheme - Index Funds` named "HDFC Index Fund Nifty 50 Plan - Direct", so its default is `nifty50`):
  - `test_scheme_is_compared_with_its_category_benchmark` → `benchmark.id == "nifty50"`, status `ok`, `value` / `differenceInr` equal to `mf_math.replay(fifo cashflows, history, date(2026, 11, 10), Decimal("2600"))` rounded to 2 dp, `xirr` equal to `pct(xirr(replay.flows).rate)`, `navDate` the replay's NAV date.
  - `test_portfolio_uses_the_chosen_benchmark` → default `nifty50`; `benchmark="midcap150"` changes `portfolio.benchmark.id` and the top-level `benchmark.label` to "Nifty Midcap 150" while the scheme row keeps `nifty50`; `options` lists all five.
  - `test_unknown_or_unseeded_benchmarks` → empty history gives `unavailable` with the not-downloaded reason, other figures unchanged; `returns_from(..., benchmark="sensex")` raises `ValueError`.
  - `test_excluded_and_unmapped_schemes_say_why` → an `unreconciled` scheme gets `excluded`; a scheme whose AMFI category is `Hybrid Scheme - Aggressive Hybrid Fund` gets `status "none"`, `id None`, the hybrid reason.
  - `test_fully_redeemed_scheme_ends_at_its_last_flow` → close 0, no NAV: replay `end` is the last flow date, value 0; a loser shows a negative `differenceInr`.
  - `test_returns_route_takes_a_benchmark` → `GET /api/returns?benchmark=liquid` → `body["benchmark"]["id"] == "liquid"`; `?benchmark=sensex` → 422 `{"error": "Unknown benchmark.", "benchmarks": [the five ids]}`; no parameter → `nifty50`.
  - `test_tool_takes_a_benchmark` → schema enum is the five ids; payload has the top-level `benchmark` and per-row `benchmark`; `{"benchmark": "sensex"}` → `{"error": "Unknown benchmark 'sensex'.", "benchmarks": [...]}`, no figures.
  - Extend `test_tool_payload_stays_small_with_many_schemes` and `..._many_excluded_funds` to assert `< snapshots.MF_RETURNS_MAX_CHARS` with benchmark blocks present.
  - `test_the_agent_quotes_the_tool_figures` (existing): add a quoted benchmark `differenceInr` to the scripted answer and assert no number warning.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_returns.py -q` → FAIL.
- [ ] **Step 3: Implement.**
  - `_Computed` gains `cashflows` (the scheme's flows without the terminal value) and `end` (NAV date, or the last flow date when the value is 0 with no NAV). The per-scheme and portfolio benchmark blocks call one helper `_compare(flows, end, value, benchmark_id, history) -> dict` that runs `replay` with `label=benchmark.label` and applies the existing `_returns(replay.flows, end)` for XIRR / under-one-year, so benchmark and fund returns use the same rule. Portfolio: pooled `cashflows` of included schemes, `end` as today's portfolio `end`, value `includedValue`; no included schemes → `status "unavailable"`, reason `"No funds are included in the totals."`.
  - Route: `benchmark: str = DEFAULT_BENCHMARK` query parameter; not in `BENCHMARK_IDS` → `JSONResponse({"error": "Unknown benchmark.", "benchmarks": list(BENCHMARK_IDS)}, status_code=422)`.
  - Tool description adds: "Each fund is compared with the index fund for its category (benchmark: XIRR on the same cash flows and differenceInr, the fund's value minus what the benchmark would be worth); the portfolio with the benchmark you pass (default nifty50)."
  - `MF_RETURNS_RULES` appends: "Compare with a benchmark only as the tool gives it: name the benchmark index fund and its NAV date, quote its XIRR and differenceInr as given, and say why a fund is not compared when it is not. A benchmark comparison is not a recommendation."
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_returns.py tests/test_agent.py tests/test_tasks.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(returns): compare funds and the portfolio with a benchmark`

### Task 4: Privacy notice

**Files:**
- Modify: `app/privacy.py`, `tests/test_privacy.py`, `tests/test_brokers.py`, `tests/test_broker_registry.py`, `tests/test_mf_ledger.py`

- [ ] **Step 1: Write the failing test** `test_notice_names_mfapi` in `tests/test_privacy.py`: the "Public fund data" body contains `"mfapi.in"` and `"sends nothing about you"`; the four version pins become `"2026-11-13.1"`.
- [ ] **Step 2: Run** `python -m pytest tests/test_privacy.py -q` → FAIL.
- [ ] **Step 3: Implement.** `POLICY_VERSION = "2026-11-13.1"` with a comment line ("the benchmark NAV history from mfapi.in"). Append to the "Public fund data" body: "To compare your funds with an index, it also downloads the public NAV history of five index and liquid funds from mfapi.in and adds each day's NAV from the AMFI file. Those requests name only those five funds and send nothing about you or your holdings."
- [ ] **Step 4: Run** `python -m pytest tests/test_privacy.py tests/test_brokers.py tests/test_broker_registry.py tests/test_mf_ledger.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(privacy): say benchmark NAV history comes from mfapi.in`

### Task 5: Returns page benchmark selector and column

**Files:**
- Modify: `frontend/js/finance.js`, `frontend/js/pages.js` (`renderReturns`, `returnsSchemeRow`, session-ended reset), `frontend/index.html` (`?v=` for `finance.js` and `pages.js`, and `css` if touched), `frontend/sw.js` (`CACHE_NAME` → `ca-mode-shell-v62`), `tests/js/finance.test.js`

**Interfaces:**
- Consumes: the `benchmark` objects from Task 3.
- Produces (exported from `finance.js`):
  - `benchmarkFigure(b) -> { short, label, note }`: ok with `xirr` → `short` = `fixedPct(xirr)`, `label` = `` `${b.label} (XIRR)` ``; ok under a year → `fixedPct(absoluteReturnPct)`, label `` `${b.label}, held under a year` ``; otherwise `short` "Not compared", `label` '', `note` = `b.reason`.
  - `benchmarkDifference(diff, label) -> string`: `> 0` → "₹1,23,456 more than in Nifty 50"; `< 0` → "₹12,345 less than in Nifty 50"; `0` → "Same as in Nifty 50"; not finite → ''. (Indian grouping via `money()`, no sign in the amount.)
  - `benchmarkNavNote(b) -> string`: "Nifty 50 index fund NAV of 12 Nov 2026", or "No benchmark NAV yet".

- [ ] **Step 1: Write failing tests in `tests/js/finance.test.js`** for the three helpers with the exact strings above, including `benchmarkFigure({ status: 'none', reason: 'Hybrid funds mix asset classes, so no single index fits.' })` → `{ short: 'Not compared', label: '', note: 'Hybrid funds ...' }` and `benchmarkDifference(null, 'Nifty 50') === ''`.
- [ ] **Step 2: Run** `node --test tests/js/finance.test.js` → FAIL.
- [ ] **Step 3: Implement.**
  - `finance.js`: the three helpers, added to `api`.
  - `pages.js`: module state `returnsBenchmark = 'nifty50'` (reset in the `ca:session-ended` handler); fetch `` `/api/returns?benchmark=${encodeURIComponent(returnsBenchmark)}` ``. Under the KPI strip a `reportCard('Compared with a benchmark', ...)` containing a `.filter-chips` group (`role="group"`, `aria-label="Benchmark"`) of `data.benchmark.options` as `.filter-chip` buttons with `data-returns-benchmark` and `aria-pressed`, then a `.kpi-strip-3`: "Your mutual funds" (`returnFigure(p)`), the benchmark (`benchmarkFigure(p.benchmark)` with `benchmarkNavNote`), "Difference" (`benchmarkDifference(p.benchmark.differenceInr, p.benchmark.label)`, `toneOf`), and below it `data.benchmark.schemeName` and `data.benchmark.note`. A delegated `click` listener on `#returns-body` for `[data-returns-benchmark]` sets the state and calls `renderReturns()`.
  - Table: a "vs benchmark" column after Return: `benchmarkFigure(item.benchmark)` short with the label, then `benchmarkDifference(...)` or the reason as `.stat-sub` lines; `data-label="vs benchmark"` so the card layout under 640px keeps working.
  - Bump `finance.js` and `pages.js` `?v=` and `CACHE_NAME`.
- [ ] **Step 4: Run** `node --test tests/js/*.test.js && for f in frontend/js/*.js; do node --check "$f"; done` → PASS. Then start the app on a temp `APP_DATA_DIR` with a stored ledger and seeded benchmark rows (or use the running dev copy), open `#/returns` in the browser pane at desktop and 375px, check the chips switch the portfolio comparison and the table collapses into cards.
- [ ] **Step 5: Commit** `feat(frontend): benchmark selector and comparison on Returns`

### Task 6: Docs and full verification

**Files:**
- Modify: `.claude/CLAUDE.md` (module entry for `app/benchmarks.py`; `benchmark_navs` in the `db.py` table list; `mf_math` gains `replay()`; `mf_returns` / `GET /api/returns` take `benchmark`; `get_mf_returns` argument; `MFAPI_URL` in Environment Variables; POLICY_VERSION history line; `test_benchmarks.py` in Testing Strategy; the Returns page line), `docs/superpowers/plans/2026-11-13-mf-returns-batch-2.md` (tick boxes)

- [ ] **Step 1: Update `.claude/CLAUDE.md`** with the entries above, matching the file's terse style.
- [ ] **Step 2: Run the full gate:** `python -m compileall -q app tests && python -m pytest -q`, `for f in frontend/js/*.js; do node --check "$f"; done`, `node --test tests/js/*.test.js`, `python -m scripts.eval_tasks`, `python -m scripts.eval_workflows` → all green.
- [ ] **Step 3: Commit** `docs: describe return benchmarks`, push the branch, open a PR to `main` (summary, the verified fund table, the three deviations, test plan), then leave a hand-over note for batch 3 in the shared notes folder.
