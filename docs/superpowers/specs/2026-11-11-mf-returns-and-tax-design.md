# Mutual fund returns and tax: design

Date: 2026-11-11. Status: approved in conversation, awaiting review of this written spec.

## Understanding

**Goal.** CA Mode shows what people own; it does not yet show what they have earned or what they owe in tax. This feature adds, for mutual funds:

1. True returns: XIRR per fund and for the whole mutual fund portfolio, compared with a benchmark on the user's own cash flows.
2. This financial year's tax position: realised short-term and long-term gains, the equity long-term exemption used and left, estimated tax.
3. Sell-planning before 31 March: gain harvesting, loss harvesting, lots turning long-term soon.
4. An export per financial year that the user or their CA can use to file.

**Decided with the user.**

- Version 1 covers mutual funds only, from a CAMS or KFintech *detailed* CAS. Stocks come later from broker tradebook uploads.
- All four outcomes above are in scope.
- Benchmarks are index-fund NAVs from public AMFI data, the same requests for every user.
- Slab-rate tax is estimated only when the user gives an optional slab (a sealed profile field); otherwise slab-taxed gains are shown as amounts.
- Approach A: our own `Decimal` engine with tax rules as dated data. `casparser` only parses and supplies 31 Jan 2018 NAVs (offline, `casparser_isin`).
- Delivered in four batches, one session and one PR each.

**Assumptions (correct these if wrong).**

- Users can get a since-inception detailed CAS from CAMS or KFintech; when they cannot, partial history is reported as such, never filled in.
- The hosted server may make outbound HTTPS requests to AMFI and mfapi.in.
- Figures are information, not advice: no "you should sell" wording anywhere (SEBI positioning).

**Success criteria.**

- For a fixture CAS, every per-scheme and portfolio figure matches a hand-computed value (XIRR matches a spreadsheet XIRR to 0.01 percentage points).
- Each tax rule boundary (dates, holding periods, grandfathering, specified funds) has a test.
- No figure is ever shown as zero when it is unknown; every exclusion carries a reason.
- The agent answers "what is my XIRR", "how much LTCG exemption is left", "which funds could I harvest" with figures the number check traces to the new tools.

**Not in scope.** Stocks, ETFs and bonds held in demat; exit loads (not in the CAS); surcharge and rebates in tax estimates; placing orders; auto-fetching the CAS from email (separate feature).

## 1. Ledger: what is kept from a statement

**Input.** A CAMS or KFintech statement with `cas_type == DETAILED`. NSDL/CDSL statements and summary CAMS/KFintech statements keep their current behaviour (holdings only). The upload response adds `ledger: {available: false, hint}` with the hint "For returns and tax, upload a detailed CAS from CAMS or KFintech covering since inception."

**Kept per scheme**, inside the existing sealed `cas_statements.data` JSON under a new `ledger` key:

| Field | Source | Note |
| --- | --- | --- |
| `isin`, `amfi` | `Scheme.isin`, `Scheme.amfi` | |
| `name`, `amc`, `rta` | `Scheme.scheme`, `Folio.amc`, `Scheme.rta` | |
| `folioKey` | HMAC-SHA256 of the folio number with a per-profile salt | the folio number itself is never stored |
| `open`, `close` | `Scheme.open`, `Scheme.close` | units, exact strings |
| `transactions[]` | `Scheme.transactions` | `date`, `type`, `amount`, `units`, `nav`; stamp duty and STT rows kept as their own typed rows |

The per-profile salt is random, generated once and stored sealed in `users.profile` meta (not exported), so the same folio maps to the same key across uploads of one profile and to different keys across profiles.

**Never kept** (unchanged rule): PAN, KYC fields, nominees, advisor/ARN, investor name, email, address, mobile, DP/client IDs, the PDF and its password.

**Building the ledger** (`mf_ledger.build(statements)`): over every stored detailed statement, the newest statement wins for each `(folioKey, isin)`. This merges a CAMS and a KFintech statement, and a newer over an older upload.

**Scheme status** (each with a human-readable `reason`):

- `incomplete_history`: `open > 0` (units held before the statement period, purchase dates unknown). Counted in value; returns and gains say "history before <period start> missing; upload a since-inception CAS". Never treated as zero cost.
- `unreconciled`: `open + sum(transaction units) != close` beyond 0.001 units. Excluded from figures.
- `unsupported`: contains `GIFT_IN`, `GIFT_OUT`, `SEGREGATION`, `MISC` or `UNKNOWN` transactions that affect units. Excluded, reason names the type.
- `ok` otherwise.

**Retention.** `KEEP_STATEMENTS` (12) still applies. History clearing, private mode (no CAS at all) and profile deletion already remove `cas_statements`. Export adds `ledger` per statement (folio keys included, folio numbers never existed).

## 2. Lots, XIRR and today's NAV

**`app/mf_math.py`**: pure `Decimal`, no I/O.

- `fifo(transactions) -> FifoResult(open_lots, disposals, problems)`.
  - Purchase, SIP purchase, switch-in (incl. merger) and dividend reinvestment add a lot: buy date, units, cost = amount + stamp duty paid on that date for that scheme.
  - Redemption and switch-out (incl. merger) consume the oldest lots first. Each disposal records buy date, sell date, units, cost share, sale value share, STT share.
  - Dividend payout is cash returned, not a disposal.
  - Selling more units than held is a `problem` for the scheme (status `unreconciled`), never a negative lot.
- `xirr(cashflows) -> XirrResult(rate | None, reason)`: Newton's method from 10%, falling back to bisection on [-99.99%, 1000%]; no convergence returns `None` with the reason.
- Holdings under 365 days (first cash flow to valuation date) report `absoluteReturnPct` and `xirr: None` with reason `under_one_year`.
- **Cash flows.** Purchases and switch-ins are outflows; redemptions, switch-outs and dividend payouts are inflows; the terminal inflow is open units x current NAV on the NAV date. At portfolio level all schemes' flows are pooled; a switch between two schemes in the ledger cancels out.

**`app/amfi.py`**: today's NAV.

- The scheduler downloads AMFI's `NAVAll.txt` (`AMFI_NAV_URL`) once a day, retrying at most hourly on failure.
- Parsed into a plain table `amfi_navs(isin PRIMARY KEY, scheme_code, scheme_name, amfi_category, nav, nav_date)`. Both the growth and the reinvestment ISIN of a row map to it. `N.A.` and non-numeric NAVs are skipped.
- The section headings (for example `Open Ended Schemes(Equity Scheme - Large Cap Fund)`) give `amfi_category`.
- Public data, nothing personal: not sealed, not exported, not deleted with a profile.
- Fallback when AMFI is unreachable or the ISIN is absent: the statement's own valuation NAV and date. A NAV older than `FUND_STALE_DAYS` makes the scheme `nav_stale` (still counted, labelled).

**Outputs** (`mf_returns.scheme_returns()`, `portfolio_returns()`):

- Per scheme: `invested` (net of redemptions at cost), `currentValue`, `unrealisedGain`, `realisedGain`, `xirr` or `absoluteReturnPct`, `heldSince`, `navDate`, `navSource` (`amfi` / `statement`), `status`, `reason`.
- Portfolio: the same over `ok` and `nav_stale` schemes, plus `coverage` (share of mutual fund value included, with the excluded schemes and reasons). Missing is never zero.

## 3. Benchmark comparison

**`app/benchmarks.py`.**

- `BENCHMARKS`: data rows `{id, label, schemeCode, note}` for `nifty50`, `nifty500`, `midcap150`, `smallcap250` (direct-growth index funds) and `liquid` (a direct-growth liquid fund as the cash proxy). Scheme codes are verified against the live AMFI file in batch 2, never written from memory.
- The page states that an index fund's NAV includes reinvested dividends and is net of a small expense ratio, so it approximates the total-return index.
- History: seeded once per benchmark from `MFAPI_URL` (`/mf/<scheme code>`, full NAV history), then appended daily from the AMFI file already downloaded. Both requests name only the fixed benchmark codes, so they reveal nothing about any user.
- Storage: plain table `benchmark_navs(benchmark_id, nav_date, nav, PRIMARY KEY(benchmark_id, nav_date))`.

**Default benchmark by `amfi_category`** (data table `CATEGORY_BENCHMARK`):

| Category | Benchmark |
| --- | --- |
| Large Cap, Flexi Cap, ELSS, Focused, Value, Contra, Dividend Yield, Index funds on Nifty 50/Sensex | `nifty50` |
| Multi Cap, Large & Mid Cap | `nifty500` |
| Mid Cap | `midcap150` |
| Small Cap | `smallcap250` |
| Liquid, Overnight, Money Market | `liquid` |
| Hybrid, other debt, sectoral/thematic, international, gold, other FoFs | none, with reason |

**Replay** (`mf_math.replay(cashflows, navs)`): each dated rupee flow buys or sells benchmark units at that date's NAV (the previous available NAV up to 5 days earlier; beyond that `unavailable`). Terminal value is the remaining units x the latest NAV. Returns the benchmark XIRR on the user's own flows and `differenceInr` (user value minus benchmark value). If a redemption would take the benchmark position below zero, the result is `not_comparable` with the reason; units never go negative.

**Portfolio level**: mutual fund XIRR vs `nifty50` by default; `?benchmark=` picks any `BENCHMARKS` id.

## 4. Tax position and sell-planning

**`app/mf_tax.py`.** Rules live in `MF_TAX_RULES`, a list of dated rows. Every gain carries the `ruleId` that produced it. Each row has: `id`, `taxClass`, `soldFrom`, `soldTo`, `boughtFrom`, `boughtTo`, `longTermAfterMonths` (or `None`: always short-term), `stRate` (number or `"slab"`), `ltRate`, `ltExemptionInr` (per financial year, equity only), `indexation`, `source`.

Rules to encode (each verified against the Finance Act text in batch 3 and cited in `source`):

| Class | Sale date | Bought | Long-term after | Short-term | Long-term |
| --- | --- | --- | --- | --- | --- |
| Equity-oriented | before 23 Jul 2024 | any | 12 months | 15% | 10% above Rs 1,00,000 |
| Equity-oriented | on/after 23 Jul 2024 | any | 12 months | 20% | 12.5% above Rs 1,25,000 |
| Specified fund (debt) | any | on/after 1 Apr 2023 | never | slab | n/a |
| Other | before 23 Jul 2024 | before 1 Apr 2023 | 36 months | slab | 20% with indexation |
| Other | on/after 23 Jul 2024 | before 1 Apr 2023, or not a specified fund | 24 months | slab | 12.5%, no indexation |

- **Grandfathering** for equity-oriented units bought on or before 31 Jan 2018: cost = max(actual cost, min(NAV on 31 Jan 2018 x units, sale value)). The 2018 NAV comes from `casparser.analysis.utils.nav_search` (offline). No 2018 NAV available: the gain is computed on actual cost and flagged.
- **Specified fund definition**: before 1 Apr 2025, a fund with at most 35% in domestic equity; from 1 Apr 2025, a fund with more than 65% in debt and money market instruments (or a fund of funds investing 65%+ in such funds). Encoded as two dated rows.
- **The Income-tax Act, 2025** applies from 1 Apr 2026 (FY 2026-27). Rates are carried over; section and schedule names change. Rules keep their effective dates; labels refer to the act in force on the sale date.

**Tax class** (`mf_tax.tax_class(amfi_category, transactions)`):

- `Equity Scheme - *`, ELSS, Arbitrage, Equity Savings, Aggressive Hybrid, and index funds/ETFs on equity indices: `equity`.
- Debt Scheme categories: `specified_debt` (subject to the dated definition and purchase date).
- Balanced Advantage / Dynamic Asset Allocation, Multi Asset, domestic FoF: `equity` if any redemption in the ledger carries STT (only equity-oriented funds charge it), else `unclassified`.
- International, gold, other FoFs, Conservative Hybrid: `other`.
- No category: `unclassified`. Gains are shown; no tax is estimated for them.

**This year's position** (`mf_tax.position(ledger, fy, slab_pct)`):

- Realised gains for the financial year, split by class and short/long term.
- Equity long-term exemption used and left, with the note "Shares, equity ETFs or funds sold outside this statement also use this exemption."
- Estimated tax: fixed rates for equity; `taxSlabPct` x slab-taxed gains when the profile field is set, otherwise those gains as amounts with "depends on your tax slab". Plus 4% cess. No surcharge or rebate, stated in a note. Always labelled an estimate.
- Losses: set off as the law allows (short-term losses against any gain, long-term losses only against long-term gains); unused losses listed with "can be carried forward 8 years if your return is filed on time".

**Sell-planning** (current financial year only, `mf_tax.planning(ledger, today, navs)`). Every option is computed in FIFO order, since a redemption always takes the oldest units.

- **Gain harvesting**, per equity scheme: the most units whose redemption realises long-term gain within the exemption left, stopping before the first short-term lot. Shows units, approximate value at today's NAV, gain, tax (Rs 0).
- **Loss harvesting**: units with an unrealised loss when redeemed in FIFO order (stopping where the cumulative result turns into a gain), the loss, and what it could offset against this year's realised gains.
- **Turning long-term soon**: lots that cross their long-term date in the next 90 days with an unrealised gain, the date, and the extra tax a redemption today would cost compared with after that date.
- **Exclusions**: ELSS units within 3 years of purchase are never offered. Exit loads are not in the CAS: every option carries "check the exit load before redeeming".
- **Deadline note**: "Redeem by about 25 March so the sale is dated in this financial year."
- **Wording**: options and figures only. No "you should", "sell", "buy" imperatives in the UI or tool payloads.

**Profile field**: `ProfileFields.taxSlabPct`, one of 0, 5, 10, 15, 20, 25, 30, optional, sealed like the other profile fields.

## 5. Surfaces

**API** (profile, unlocked session; reads need no consent, as for `GET /api/cas`):

- `GET /api/returns?benchmark=` returns `{available, schemes, portfolio, benchmark, coverage, navDate}`; `{available: false, hint}` without a detailed CAS.
- `GET /api/tax?fy=` (default: current financial year in `SNAPSHOT_TIMEZONE`) returns `{fy, position, planning?, notes}`; `planning` only for the current year. 422 for a malformed or future `fy`.
- `GET /api/tax/export?fy=` returns `text/csv`. Section 1: equity long-term gains in Schedule 112A-style columns (ISIN, name, units, sale date, sale value, cost, 31 Jan 2018 value, gain). Section 2: every other gain with class, term and rule id. Header lines: "For you or your CA to check. Estimates; verify before filing." and a note that from FY 2026-27 the form schedule names follow the Income-tax Act, 2025.
- `PUT /api/profile` accepts `taxSlabPct`.

**Frontend.**

- New route `#/returns` ("Returns & tax") with two tabs:
  - **Returns**: portfolio XIRR vs benchmark (selector), per-fund table, status chips with reasons for excluded funds.
  - **Tax**: this year's position, an exemption used/left bar, planning cards, an export button per financial year.
- Every figure shows its date and source (AMFI NAV date or statement date). Formatting in `finance.js`; no maths in the browser beyond display.
- `#/investments` CAS upload links to `#/returns` and shows the detailed-CAS hint when `ledger.available` is false.
- Updates: `ROUTE_META`, `page_views.Route` literal (`"returns"`), nav entry, `?v=` cache-busters, `CACHE_NAME` in `sw.js`.

**Agent** (Portfolio Analyzer only, offered when the profile has a ledger):

- `get_mf_returns(scheme?: str, benchmark?: enum)`, `get_mf_tax(fy?: str, include_planning?: bool)`; read-only `LocalTool`s, payloads under 5,500 characters.
- Counted by `tasks.is_account_tool`; results go into `ResultStore` so the number check traces every quoted figure.
- `MF_RETURNS_TAX_RULES` prompt text: quote tool figures, never recompute; call tax an estimate; state exclusions and coverage; no buy/sell recommendations.
- Router: XIRR, capital gains and harvesting wording already route to `account_data` via keywords; add routing eval cases to confirm the classifier does too.
- The Finance Agent gets neither tool.

**Data jobs and settings.**

- `snapshots.run_scheduler()` calls `amfi.refresh_if_due()` (daily) and `benchmarks.seed_if_needed()` / `append_from_amfi()`.
- Env: `MF_DATA_ENABLED` (true; `conftest.py` sets false), `AMFI_NAV_URL` (default `https://www.amfiindia.com/spages/NAVAll.txt`), `MFAPI_URL` (default `https://api.mfapi.in`). Requests carry a generic user agent, no user data.

**Privacy.** Each batch that changes what is stored or shared bumps `POLICY_VERSION` and adds its own part of the notice (batch 1: ledger and AMFI; batch 2: mfapi.in; batch 3: slab field). Together `NOTICE` adds: mutual fund transaction history from a detailed CAS is stored sealed; folio numbers are not stored; the app downloads public NAV data from AMFI and mfapi.in without sending anything about the user; the optional tax slab field. Export gains `ledger`.

**Errors.** AMFI or mfapi.in unreachable: figures fall back as above and the page says which source and date were used; never a 500. A ledger problem in one scheme never hides the others.

## 6. Testing

Fixtures are built from `casparser`'s own models (as in `test_cas.py`) with hand-computed expected values in comments.

- `test_mf_ledger.py`: kept and dropped fields, folio keys stable per profile and different across profiles, newest-statement-wins merge, each status and reason.
- `test_mf_math.py`: FIFO with stamp duty, switches, dividend reinvest and payout, overselling; XIRR against spreadsheet values; no-convergence; under-one-year absolute return; portfolio pooling with a switch.
- `test_amfi.py`: parsing headings into categories, both ISIN columns, `N.A.`, stale NAVs, statement fallback.
- `test_benchmarks.py`: replay on SIP flows, missing dates, the 5-day rule, a position that would go negative, category mapping.
- `test_mf_tax.py`: each rule boundary (sale on 22 vs 23 Jul 2024; 12 months vs 12 months and a day; 24 and 36 months; bought 31 Mar vs 1 Apr 2023; the 2025 specified-fund definition), grandfathering (cost above FMV, FMV above sale, sale below cost), loss set-off, unclassified funds, harvesting stopping before short-term lots, ELSS lock-in, turning-long-term dates, slab missing vs given.
- API tests: gates (no profile, locked), profile isolation, private mode, export CSV, `taxSlabPct` validation, privacy export includes `ledger`.
- `tests/js/finance.test.js`: display helpers for the new page.
- Evals: new live cases in `evals/financial_workflows.jsonl` for returns, exemption left and harvesting questions; routing cases in `evals/request_routing.jsonl` (bump nothing unless `ROUTER_PROMPT` changes).

## 7. Batches

Each batch is its own session, branch and PR, cut from up-to-date `main`; trunk works after each.

1. **Ledger, lots, XIRR and AMFI NAVs**: `mf_ledger.py`, `mf_math.py` (fifo, xirr), `amfi.py`, `amfi_navs` table, CAS upload changes, `GET /api/returns` (without benchmark), Returns tab, `get_mf_returns`, `POLICY_VERSION` bump and notice, export `ledger`.
2. **Benchmarks**: `benchmarks.py`, `benchmark_navs`, replay, category mapping, benchmark selector, tool argument.
3. **Tax and planning**: `mf_tax.py`, `taxSlabPct`, `GET /api/tax`, Tax tab, `get_mf_tax`, prompt rules, evals.
4. **Export and docs**: `GET /api/tax/export`, README, `docs/features.md`, `docs/api.md`, `docs/privacy-and-data.md`, CLAUDE.md module entries.
