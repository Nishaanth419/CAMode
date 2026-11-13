# Mutual fund returns, batch 3 (tax position and sell-planning) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show, for the mutual funds in a detailed CAS, this financial year's realised capital gains by tax class and term, the equity long-term exemption used and left, an estimated tax, and (current year only) sell-planning figures, in `GET /api/tax`, a Tax tab on `#/returns` and a `get_mf_tax` tool.

**Architecture:** `app/mf_tax.py` is pure `Decimal`: dated rule rows (`MF_TAX_RULES`), the Cost Inflation Index, a tax class per fund, tax lots built on `mf_math.fifo()` (mergers carried over, 31 Jan 2018 grandfathering), `position()` and `planning()`. A thin loader (`tax_for()`) reads the ledger, AMFI rows and the profile's optional `taxSlabPct`; the route, tool and page only pass figures through.

**Tech Stack:** Python 3.12, FastAPI, `Decimal`, `casparser.analysis.utils.nav_search` (offline 31 Jan 2018 NAVs from `casparser_isin`); vanilla JS (`finance.js`, `pages.js`), `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-11-mf-returns-and-tax-design.md` (section 4, the batch-3 parts of 5 and 6, section 7). Earlier plans: `2026-11-11-mf-returns-batch-1.md`, `2026-11-13-mf-returns-batch-2.md`; build on the shipped code (`mf_ledger`, `mf_math`, `mf_returns`, `benchmarks`) where it differs from the spec.

## Global Constraints

- Everything in the batch-1 and batch-2 Global Constraints still holds: `Decimal` end to end; output rounding only (money 2 dp, units 3 dp, percent 2 dp, `ROUND_HALF_UP`); missing is never zero; every exclusion has a reason; no "you should", "sell", "buy" imperatives in UI copy, notes or tool payloads; tool payload under 5,500 characters; no network in tests; Conventional Commits, never a Claude co-author or "Generated with" line.
- Every gain row carries `ruleId`; every rule row carries `source` (citation) and `sections` (`{"st": ..., "lt": ...}` in the act in force on the sale date).
- Tax is always an estimate: 4% health and education cess, no surcharge, no rebate, no carried-forward losses from earlier years; slab-taxed gains are taxed only when `taxSlabPct` is set.
- Supported financial years: `MIN_FY = "2023-24"` to the current FY in `SNAPSHOT_TIMEZONE` (`snapshots.local_now()`). FY strings are `YYYY-YY` (`"2026-27"`).
- `EQUITY_LT_EXEMPTION_INR`: FY 2023-24 `100000`; FY 2024-25 onward `125000`, per financial year, in aggregate across rule rows of that year.
- Planning windows: `TURNING_LONG_TERM_DAYS = 90`; `ELSS_LOCK_MONTHS = 36`.
- Exact copy (constants in `mf_tax`):
  - `EXEMPTION_NOTE`: "Shares, equity ETFs or funds sold outside this statement also use this exemption."
  - `CARRY_FORWARD_NOTE`: "Unused losses can be carried forward 8 years if your return is filed on time."
  - `ESTIMATE_NOTE`: "An estimate: 4% cess included; surcharge, rebates and losses carried from earlier years are not."
  - `SLAB_NOTE`: "Depends on your tax slab. Add it in your profile to include it in the estimate."
  - `EXIT_LOAD_NOTE`: "Check the exit load before redeeming; the statement does not include it."
  - `DEADLINE_NOTE`: "Redeem by about 25 March so the sale is dated in this financial year."
  - `SET_OFF_NOTE`: "Losses are set off against slab-taxed gains first, then other short-term gains, then long-term gains; your return may use another order the law allows."
  - `FIGURES_NOTE`: "Figures and options for information, not advice or a recommendation to redeem or invest."
- `POLICY_VERSION` becomes `2026-11-13.2` (or the commit day's `.1`); pinned in `tests/test_privacy.py`, `test_brokers.py`, `test_broker_registry.py`, `test_mf_ledger.py`.

## Tax rules, verified

Read on 2026-11-13 from the **Finance (No. 2) Bill, 2024** as introduced (Bill No. 55 of 2024, indiabudget.gov.in) and its **Memorandum**, the **Finance Bill, 2026** (Bill No. 3 of 2026) rates for the Income-tax Act, 2025, and the Income Tax Department's CII chart (Notification 70/2025; FY 2024-25 = 363 by Notification 44/2024). The Bill text was enacted as the Finance (No. 2) Act, 2024 with these clauses; the enacted text itself was not re-read.

| `id` | `taxClasses` | Sold | Bought | LT after (months) | ST | LT | Exempt / FY | Indexation | Source (`source`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `equity_fy2023_24` | equity | 2023-04-01–2024-03-31 | any | 12 | 15% | 10% | 1,00,000 | no | ITA 1961 ss. 111A, 112A, 2(42A) proviso 1 |
| `equity_2024_before_jul23` | equity | 2024-04-01–2024-07-22 | any | 12 | 15% | 10% | 1,25,000 (aggregate FY) | no | ss. 111A(1)(i)(a), 112A(2)(i)(a) and proviso, substituted by Finance (No. 2) Act 2024 ss. 29, 31 |
| `equity_from_2024_07_23` | equity | 2024-07-23–2026-03-31 | any | 12 | 20% | 12.5% | 1,25,000 | no | ss. 111A(1)(i)(b), 112A(2)(i)(b), Finance (No. 2) Act 2024 ss. 29, 31 |
| `equity_ita2025` | equity | 2026-04-01– | any | 12 | 20% | 12.5% | 1,25,000 | no | Income-tax Act 2025 ss. 196, 198 (rates in Finance Act 2026) |
| `specified_debt` | debt | 2023-04-01–2026-03-31 | from 2023-04-01 | never | slab | – | – | – | ITA 1961 s. 50AA (Finance Act 2023); Explanation (ii) substituted by Finance (No. 2) Act 2024 s. 21(b) from AY 2026-27 |
| `specified_debt_ita2025` | debt | 2026-04-01– | from 2023-04-01 | never | slab | – | – | – | Income-tax Act 2025 s. 76 |
| `specified_other_old_definition` | other | 2023-04-01–2025-03-31 | from 2023-04-01 | never | slab | – | – | – | s. 50AA Explanation (ii) as inserted by Finance Act 2023 (not more than 35% in domestic equity) |
| `other_new_definition` | other | 2025-04-01–2026-03-31 | from 2023-04-01 | 24 | slab | 12.5% | – | no | s. 50AA Explanation (ii) substituted from AY 2026-27; ss. 2(42A), 112 as amended by Finance (No. 2) Act 2024 ss. 3(b), 30 |
| `other_new_definition_ita2025` | other | 2026-04-01– | from 2023-04-01 | 24 | slab | 12.5% | – | no | Income-tax Act 2025 ss. 76, 197 |
| `non_equity_indexed` | debt, other | 2023-04-01–2024-07-22 | to 2023-03-31 | 36 | slab | 20% | – | yes | s. 2(42A) (36 months for units other than equity-oriented); s. 112(1)(a) with s. 48 second proviso |
| `non_equity` | debt, other | 2024-07-23–2026-03-31 | to 2023-03-31 | 24 | slab | 12.5% | – | no | Finance (No. 2) Act 2024 ss. 3(b) [2(42A)], 30 [112] |
| `non_equity_ita2025` | debt, other | 2026-04-01– | to 2023-03-31 | 24 | slab | 12.5% | – | no | Income-tax Act 2025 s. 197 |

`sections` per row: 1961 rows `{"st": "111A", "lt": "112A"}` (equity), `{"st": "50AA", "lt": None}` (specified), `{"st": None, "lt": "112"}` (others; slab ST has no special section); 2025-Act rows `196` / `198`, `76`, `197`.

- **Long-term test**: held for more than N months, i.e. long-term when `sell_date > add_months(buy_date, N)` (`goal_planning.add_months`; 29 Feb clamps to 28 Feb). `long_term_on(buy, N) = add_months(buy, N) + 1 day`.
- **Grandfathering** (s. 55(2)(ac), units not listed: FMV = NAV on 31 Jan 2018): equity, long-term, bought on or before 2018-01-31: cost = max(actual cost, min(FMV, sale value)), FMV = `nav_search(isin) × units`. No 2018 NAV: actual cost, flag `no_2018_nav`.
- **Indexation** (`non_equity_indexed` only): indexed cost = cost × CII(sale FY) / CII(purchase FY); purchase before FY 2001-02 has no index in the 2001 series: actual cost, flag `no_index`. `COST_INFLATION_INDEX` = 2001-02 100, 02-03 105, 03-04 109, 04-05 113, 05-06 117, 06-07 122, 07-08 129, 08-09 137, 09-10 148, 10-11 167, 11-12 184, 12-13 200, 13-14 220, 14-15 240, 15-16 254, 16-17 264, 17-18 272, 18-19 280, 19-20 289, 20-21 301, 21-22 317, 22-23 331, 23-24 348, 24-25 363.
- **Mergers** (s. 47(xviii)/(xix), cost s. 49(2AD), holding period s. 2(42A) Explanation 1): `SWITCH_OUT_MERGER` is not a sale; the receiving scheme's units keep the old lots' buy dates and costs.
- **Losses** (ss. 70, 74): short-term losses against any gain, long-term losses only against long-term gains; unused losses listed with `CARRY_FORWARD_NOTE`.

### Could not confirm (for the user and their CA)

1. **Income-tax Act, 2025 s. 76** (specified funds): the department's page title names it, but its text could not be read (HTTP 403). One commentary reads the 2025 Act as not deeming units held over 24 months short-term; we keep "always short-term", as in s. 50AA.
2. **Finance Act, 2026** rates (ss. 196 20%, 197 12.5%, 198 12.5% above ₹1,25,000) are from the Bill as introduced; the enacted text was not checked.
3. **Holding-period day count** ("more than 12 months": long-term from the day after the 12-month anniversary) and **ELSS lock-in end** (redeemable from the day after the third anniversary): conventional readings, not stated in the Act.
4. **Set-off order and exemption order**: the law lets the return choose; we use `SET_OFF_NOTE`'s order, and in FY 2024-25 the ₹1,25,000 exemption absorbs 12.5% gains before 10% ones (lowest estimate).
5. **Fund classification by AMFI category**: a fund's actual equity/debt share decides the law's test; categories are a proxy (below). Balanced Advantage, Multi Asset, domestic FoF, solution-oriented and life-cycle funds are `equity` only when a redemption paid STT, else `unclassified`.

## Deviations from the spec (need your OK)

1. **Specified-fund source and the old definition.** The 65%-debt definition comes from the Finance (No. 2) Act 2024 (s. 21(b), from AY 2026-27, so sales from 1 Apr 2025), not a Finance Act 2025. Under the earlier 35%-domestic-equity definition, gold, international and other FoFs bought from 1 Apr 2023 were specified funds too: rows `specified_other_old_definition` / `other_new_definition` cover that; the spec table had no row for them.
2. **Exemption per financial year.** FY 2024-25's ₹1,25,000 applies to the whole year, across sales on both sides of 23 Jul 2024 (proviso to s. 112A(2)(i)); the spec table said ₹1,00,000 before 23 Jul.
3. **Conservative Hybrid is `debt`** (75–90% debt, so a specified fund under both definitions), not `other`.
4. **No AMFI category but STT on a redemption → `equity`** (spec: always `unclassified`).
5. **Years before FY 2023-24 are not supported** (422 "Tax figures start from FY 2023-24."): no rules before s. 50AA are encoded.
6. **Mergers carried over** (batch-1 hand-over): a merger is not a realised gain; a merger with no matching other side is flagged.
7. **`taxSlabPct` control lives in the profile dialog** ("About you"), because `PUT /api/profile` replaces every field; the Tax tab links there. It is not added to the prompt's preference labels (only the tool uses it).

## Review Focus

1. **One redemption spanning grandfathered and later lots, and partial lots**: each lot is its own gain row with its own cost basis and term; a lot split by a sale keeps the exact remaining units and cost. Test in Task 2.
2. **FY 2024-25 sales on both sides of 23 Jul 2024**: one ₹1,25,000 exemption for the year, absorbed by the 12.5% gains first, rates 10% / 12.5% / 15% / 20% by sale date. Test in Task 3.
3. **A redemption whose purchases are not in the statement** (`incomplete_history`, `unreconciled`, `unsupported`): the scheme is excluded with its reason, never taxed on zero cost, and the position says gains are incomplete. Test in Task 2.
4. **Only debt funds and no slab**: `tax.status == "partial"`, the slab-taxed amount with `SLAB_NOTE`, never a tax of ₹0. Test in Task 3.
5. **Planning with a missing or stale NAV**: a fund with no NAV is left out of planning with a reason; a stale NAV is used and its date shown. Test in Task 4.

---

### Task 1: Rules, financial years and tax class

**Files:**
- Create: `app/mf_tax.py`, `tests/test_mf_tax.py`
- Modify: `app/benchmarks.py` (rename `_sub_category` → `sub_category`, keep a module alias if anything else uses it)

**Interfaces:**
- Produces:
  - `TaxRule` (frozen dataclass): `id, tax_classes: tuple[str, ...], sold_from: date, sold_to: date | None, bought_from: date | None, bought_to: date | None, long_term_after_months: int | None, st_rate: Decimal | Literal["slab"], lt_rate: Decimal | None, lt_exemption_inr: Decimal | None, indexation: bool, sections: dict[str, str | None], act: str, source: str`; `MF_TAX_RULES: tuple[TaxRule, ...]` (the table above, in order).
  - `rule_for(tax_class: str, bought: date, sold: date) -> TaxRule | None`, `is_long_term(rule, bought, sold) -> bool`, `long_term_on(rule, bought) -> date | None`.
  - `COST_INFLATION_INDEX: dict[str, int]`, `indexed_cost(cost, bought, sold) -> tuple[Decimal, str | None]` (flag `no_index`).
  - `fy_of(day) -> str`, `parse_fy(text: str, today: date) -> str` (raises `ValueError` with the 422 message: malformed "Use a financial year like 2026-27.", future "That financial year has not started.", too early "Tax figures start from FY 2023-24."), `fy_bounds(fy) -> tuple[date, date]`, `fy_options(today) -> list[str]` (newest first), `exemption_for(fy) -> Decimal`.
  - `tax_class(amfi_category: str | None, scheme_name: str, transactions: Sequence[Txn]) -> tuple[str, str]` → (`equity` | `debt` | `other` | `unclassified`, reason).

- [ ] **Step 1: Write failing tests:**
  - `test_rule_boundaries` (parametrised `(class, bought, sold) -> rule id`): equity sold 2024-07-22 → `equity_2024_before_jul23`, 2024-07-23 → `equity_from_2024_07_23`, 2024-03-31 → `equity_fy2023_24`, 2026-03-31 / 2026-04-01 split into the 1961 and 2025 rows; debt bought 2023-03-31 vs 2023-04-01 (→ `non_equity_*` vs `specified_debt`); other bought 2023-04-01 sold 2025-03-31 → `specified_other_old_definition`, sold 2025-04-01 → `other_new_definition`; debt bought 2023-03-31 sold 2024-07-22 → `non_equity_indexed`, sold 2024-07-23 → `non_equity`; sold 2023-03-31 → `None`; `unclassified` → `None`.
  - `test_long_term_boundaries`: equity bought 2024-01-15, sold 2025-01-15 → short, 2025-01-16 → long; non-equity 24 months (bought 2022-08-01: sold 2024-08-01 short, 2024-08-02 long) and 36 months under `non_equity_indexed` (bought 2020-07-10: sold 2023-07-10 short, 2023-07-11 long); bought 2024-02-29 → `long_term_on == date(2025, 3, 1)`; specified rows → never long-term, `long_term_on is None`.
  - `test_every_rule_is_cited`: every row has `source`, `act` ("Income-tax Act, 1961" before 2026-04-01, "Income-tax Act, 2025" after), no two rows overlap for the same class and dates (walk every day 2023-04-01..2027-03-31 for each class and both purchase sides: at most one match; equity/debt/other always one).
  - `test_indexed_cost`: cost 100000 bought 2015-06-01 sold 2024-07-01 → `Decimal("100000") * 363 / 254`; bought 2000-05-01 → actual cost with `no_index`.
  - `test_financial_years`: `fy_of(date(2027, 3, 31)) == "2026-27"`, `fy_of(date(2026, 4, 1)) == "2026-27"`; `parse_fy` accepts `"2025-26"`, rejects `"2025-27"`, `"2025"`, `"FY25"`, future `"2027-28"` (today 2026-11-13) and `"2022-23"` with the exact messages; `fy_options(date(2026, 11, 13)) == ["2026-27", "2025-26", "2024-25", "2023-24"]`; `exemption_for("2023-24") == 100000`, `("2024-25") == 125000`.
  - `test_tax_class` (parametrised over live AMFI spellings): `Equity Scheme - Large Cap Fund`, `Equity Schemes - Thematic Fund`, `ELSS`, `Equity Schemes - ELSS- Tax Saver Fund`, `Hybrid Scheme - Aggressive Hybrid Fund`, `Hybrid Schemes - Arbitrage Fund`, `Hybrid Scheme - Equity Savings`, `Index Funds - Equity Funds`, `Exchange Traded Funds (ETFs) - Equity ETF` → equity; `Debt Scheme - Liquid Fund`, `Income/Debt Oriented Schemes - Gilt Fund`, `Index Funds - Debt Funds`, `Income`, `Gilt`, `Hybrid Scheme - Conservative Hybrid Fund`, `Exchange Traded Funds (ETFs) - Debt ETF` → debt; `Other Scheme - FoF Overseas`, `Overseas Fund of Funds - Fund of Funds investing overseas`, `Other Scheme - Gold ETF`, `Exchange Traded Funds (ETFs) - Silver ETF`, `Other Scheme - FoF Domestic` named "... Gold Savings Fund" → other; `Other Scheme - Index Funds` named "... Nifty SDL Apr 2027 Index Fund" → debt, "... Nifty 50 Index Fund" → equity; `Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage`, `Hybrid Schemes - Multi Asset Allocation Fund`, `Other Scheme - FoF Domestic`, `Solution Oriented Scheme - Retirement Fund`, `Life Cycle Funds - ...`, `Growth` → equity with an `STT_TAX` row on a redemption date, else unclassified; `None` → equity with STT, else unclassified. Each reason non-empty (e.g. "Classified as equity-oriented because its redemptions paid STT.", "Balanced advantage, multi-asset and fund-of-funds schemes can be taxed either way; no redemption paid STT, so tax is not estimated.").
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_tax.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** Category: major group = text before `" - "` (lower-case), sub-category = `benchmarks.sub_category()`. Equity: major group starts `equity`, `elss`, or sub in {aggressive hybrid, arbitrage, equity savings}, or `index funds - equity funds`, `equity etf`. Debt: major group starts `debt` or `income`, or `gilt`, `index funds - debt funds`, `debt etf`, sub `conservative hybrid`, fixed term plan. Other: `fof overseas`, `fund of funds investing overseas`, gold/silver ETF, `etfs investing overseas`, and FoF Domestic whose name has `gold` or `silver`. `Other Scheme - Index Funds`: name has any of `sdl`, `g-sec`, `gsec`, `gilt`, `bond`, `crisil ibx`, `psu`, `debt`, `t-bill`, `money market`, `liquid`, `target maturity` → debt; `gold`, `silver` → other; else equity. STT test: any `STT_TAX` row on a date with a `SELL_TYPES` row. Rules as a tuple literal with `Decimal` rates.
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_tax.py tests/test_benchmarks.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(mf_tax): dated capital gains rules and fund tax classes`

### Task 2: Tax lots, mergers and grandfathering

**Files:**
- Modify: `app/mf_math.py` (lot and disposal tags, carried lots), `app/mf_tax.py`, `app/mf_returns.py` (make `_nav` public as `nav_for`), `tests/test_mf_tax.py`, `tests/test_mf_math.py`

**Interfaces:**
- Consumes: Task 1; `mf_ledger.LedgerScheme`; `amfi.NavRow`.
- Produces:
  - `mf_math.Origin(isin: str | None, ratio: Decimal)` (frozen): the scheme a merged lot came from and its units per unit of the receiving scheme. `Lot` gains `buy_type: str = ""`, `origin: Origin | None = None`; `Disposal` gains `sell_type: str = ""`, `buy_type: str = ""`, `origin: Origin | None = None`.
  - `mf_math.fifo(transactions, *, carried: dict[date, list[Lot]] | None = None)`: a `SWITCH_IN_MERGER` on a date with carried lots adds those lots (units and their split of the transaction's units in proportion, costs and buy dates as given) instead of one lot at the merger amount. Existing behaviour unchanged when `carried` is None.
  - `mf_tax.Gain` (frozen): `scheme, isin, folio_key, tax_class, rule_id, term ("short" | "long"), bought, sold, units, sale_value, cost, tax_cost, fmv_2018: Decimal | None, gain, flags: tuple[str, ...]`.
  - `mf_tax.SchemeLots` : `scheme: LedgerScheme, tax_class, class_reason, status, reason, open_lots: list[Lot], gains: list[Gain], flags`.
  - `tax_lots(schemes: Sequence[LedgerScheme], navs: dict[str, NavRow], *, fmv: Callable[[str], Decimal | None] = nav_2018) -> list[SchemeLots]`; `nav_2018(isin) -> Decimal | None` (wraps `nav_search`, cached per process).
  - `mf_returns.nav_for(scheme, navs) -> tuple[Decimal | None, date | None, str | None]` (was `_nav`).

- [ ] **Step 1: Write failing tests** (fixtures from `tests/mf_fixtures.py` builders or direct `LedgerScheme`/`Txn`; hand-computed values in comments; `fmv` passed as a dict lookup):
  - `test_fifo_tags_lots_and_disposals` (`test_mf_math.py`): buy/sell types recorded; existing fifo tests still pass.
  - `test_grandfathering_cases` (equity, bought 2017-06-01 at cost 100/unit, 10 units, sold 2025-01-10): FMV 150/unit, sale 200 → tax cost 1500, gain 500; FMV 250, sale 200 → tax cost min(2500, 2000)=2000, gain 0; sale 80 < cost 100 → tax cost 1000, loss −200 (max(cost, min(FMV, sale))=1000); bought 2018-01-31 grandfathered, 2018-02-01 not; no 2018 NAV → actual cost and flag `no_2018_nav`; a short-term or non-equity sale never grandfathered.
  - `test_one_sale_spans_lots`: lots 2017-06-01 (10 u) and 2024-12-01 (10 u), one redemption of 15 u on 2025-06-02: two gain rows, first long-term grandfathered, second 5 u short-term `equity_from_2024_07_23`, remaining lot 5 u at half its cost.
  - `test_merger_is_carried_over`: scheme A (folio F) bought 2016-05-01, `SWITCH_OUT_MERGER` 2018-05-18 of all units; scheme B (folio F) `SWITCH_IN_MERGER` the same day with half the units: no gain row for A; B's lot has buy date 2016-05-01, A's cost, `origin.isin == A`, `ratio == 2`; a later sale of B is long-term and grandfathered with `nav_2018(A) × units × 2`.
  - `test_unmatched_merger_is_flagged`: merger-in with no merger-out on that date in the folio → lot at the merger date and amount, flag `merged_in_without_source` ("Merged in from a scheme not in this statement: the holding period and cost may start earlier."); merger-out with no merger-in → no gain, flag `merged_out_without_target`.
  - `test_excluded_schemes`: `incomplete_history`, `unreconciled`, `unsupported` and a FIFO/closing mismatch → `status` kept, `reason` given, `gains == []`; never a gain at zero cost.
  - `test_indexation_applies_only_to_its_rule`: debt bought 2019-05-01 sold 2023-06-01 → `tax_cost = cost × 348 / 289`; the same sold 2024-08-01 → `tax_cost == cost`.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_tax.py tests/test_mf_math.py -q` → FAIL.
- [ ] **Step 3: Implement.** First pass: `fifo()` for every eligible scheme (ledger `ok` and FIFO units match closing within `UNIT_TOLERANCE`), collecting `SWITCH_OUT_MERGER` disposals by `(folio_key, date)`. Then re-run `fifo(carried=...)` for schemes with a `SWITCH_IN_MERGER` on a date with exactly one merger-out scheme in the folio (lots built from that scheme's merger disposals: `Lot(d.buy_date, units, d.cost, d.buy_type, Origin(isin_A, ratio))`, where ratio = A units / B units and an `origin` already present is composed by multiplying ratios), repeating until no scheme changes (at most `len(schemes)` passes; chains A→B→C resolve). Gains skip `SWITCH_OUT_MERGER` disposals. Per disposal: `rule_for(class, buy, sell)` (None → flag `no_rule`, gain still shown under `unclassified`); term; grandfathering for equity long-term lots bought ≤ 2018-01-31 using `origin.isin` and `units × ratio` when merged; indexation for `rule.indexation`. STT is not deductible (s. 48), so `sale_value` is the redemption amount.
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_tax.py tests/test_mf_math.py tests/test_mf_returns.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(mf_tax): tax lots with merger carry-over and 2018 grandfathering`

### Task 3: This year's position

**Files:**
- Modify: `app/mf_tax.py`, `tests/test_mf_tax.py`

**Interfaces:**
- Consumes: `tax_lots()`, `exemption_for()`, `fy_bounds()`.
- Produces: `position(lots: Sequence[SchemeLots], fy: str, slab_pct: int | None) -> dict`:
  ```
  {fy, from, to, act, rows: [{taxClass, term, ruleId, rate ("slab" | "20.00"), section, gains, losses, net}],
   setOff: [{loss: "short"|"long", against: ruleId, amount}],
   exemption: {limit, used, left, note} | null (no equity gains or losses this FY → still shown with used 0),
   tax: {status: "estimated"|"partial"|"none", items: [{ruleId, taxable, rate, tax}], slabPct, slabTaxable, beforeCess, cess, total, notes},
   carryForward: {short, long, note} | null,
   unclassified: {gain, schemes: [{name, reason}]} | null,
   schemes: [{name, isin, taxClass, classReason, status, reason, shortTerm, longTerm, flags}],
   excluded: [{name, status, reason}], notes}
  ```
  Money as 2-dp floats; `total` null when nothing could be estimated.

- [ ] **Step 1: Write failing tests:**
  - `test_position_splits_by_class_and_term` (FY 2025-26, equity ST 30,000, equity LT 2,00,000, debt specified 10,000): rows per rule; exemption `{limit 125000, used 125000, left 0}` with `EXEMPTION_NOTE`; with `slab_pct=30`: tax = 30000×20% + 75000×12.5% + 10000×30% = 6000 + 9375 + 3000 = 18375, cess 735, total 19110, status `estimated`; with `slab_pct=None`: `slabTaxable 10000`, status `partial`, `total` 15990 (fixed-rate part only) and `SLAB_NOTE` in notes.
  - `test_fy_2024_25_exemption_is_one_for_the_year`: equity LT 80,000 sold 2024-06-10 (10%) and 1,00,000 sold 2024-09-10 (12.5%): exemption absorbs the 12.5% gains first → taxable 55,000 at 10% = 5,500; limit 125000 used 125000.
  - `test_losses_set_off_in_order`: ST loss 50,000 against debt slab gain 20,000 then equity ST 10,000 then non-equity LT, then equity LT; LT loss only against LT gains; leftover listed in `carryForward` with `CARRY_FORWARD_NOTE`; `setOff` lists each step.
  - `test_only_debt_without_slab_is_never_zero`: status `partial`, `total is None`, `slabTaxable` the gains.
  - `test_unclassified_gains_shown_not_taxed`: in `unclassified` with the scheme's reason, absent from rows' tax and set-off.
  - `test_excluded_schemes_are_listed` and a note "Gains from funds left out are not included." when any are excluded.
  - `test_other_years_gains_are_ignored`: a sale on 2026-04-01 is not in FY 2025-26.
  - `test_act_label`: FY 2025-26 → "Income-tax Act, 1961"; FY 2026-27 → "Income-tax Act, 2025" and the 2025-Act sections on rows.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_tax.py -k position -q` → FAIL.
- [ ] **Step 3: Implement.** Net per rule; set-off order: ST losses → slab ST rows → equity ST rows (higher rate first) → non-equity LT rows → equity LT rows; LT losses → non-equity LT → equity LT (higher rate first); then exemption on remaining equity LT, higher rate first; tax per row at `rate` (slab rows at `slab_pct` when set); cess 4% on the sum. `ESTIMATE_NOTE`, `SET_OFF_NOTE`, `FIGURES_NOTE` always in notes.
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_tax.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(mf_tax): realised gains, exemption and estimated tax for a financial year`

### Task 4: Sell-planning

**Files:**
- Modify: `app/mf_tax.py`, `tests/test_mf_tax.py`

**Interfaces:**
- Consumes: `tax_lots()`, `position()`, `mf_returns.nav_for()`, `fund_data.stale_days()`.
- Produces: `planning(lots: Sequence[SchemeLots], navs: dict[str, NavRow], today: date, position: dict, slab_pct: int | None) -> dict`:
  ```
  {asOf, exemptionLeft, notes: [DEADLINE_NOTE, EXIT_LOAD_NOTE, FIGURES_NOTE, "These options each use the same exemption left; together they cannot use more than it."],
   gainHarvest: [{name, isin, units, value, gain, tax: 0, nav, navDate, navSource, stoppedBy: "short_term"|"exemption"|"all_units"}],
   lossHarvest: [{name, isin, taxClass, units, value, loss, shortTermLoss, longTermLoss, couldOffset, nav, navDate}],
   turningLongTerm: [{name, isin, taxClass, units, unitsBefore, longTermOn, gain, taxNow, taxAfter, extraTax, extraTaxNote, nav, navDate}],
   elssLocked: [{name, units, unlocksOn}], left: [{name, reason}]}
  ```
- [ ] **Step 1: Write failing tests** (today 2027-01-15, FY 2026-27, NAVs given):
  - `test_gain_harvest_stops_before_first_short_term_lot`: lots LT (gain 40,000), LT (gain 50,000), ST: option takes both LT lots, `stoppedBy "short_term"`, gain 90,000, tax 0; with exemption left 60,000: the whole first lot plus the units of the second whose gain is 20,000 (`units = lot.units × 20000 / lot gain`, 3 dp down), `stoppedBy "exemption"`; an equity fund whose oldest lot is short-term offers nothing; non-equity funds never offered.
  - `test_loss_harvest_takes_the_most_loss_in_fifo_order`: lots −5,000, −3,000, +10,000 → units of the first two, loss 8,000 split ST/LT; `couldOffset` = min(loss, gains this year it may be set against by term); a fund whose cumulative result never goes below 0 is absent.
  - `test_turning_long_term_in_90_days`: an equity lot turning long-term on 2027-03-20 with gain 10,000 (and older lots before it: `unitsBefore`): `taxNow = 2000` (20%), `taxAfter = 1250` (12.5%), `extraTax 750`, `extraTaxNote` "If the gain also fits within the exemption, the saving is larger."; a lot turning on 2027-04-20 (95 days) absent; an `other` lot with slab unknown → `taxNow None`, `extraTax None`, note `SLAB_NOTE`; specified debt never listed.
  - `test_elss_units_within_three_years_never_offered`: ELSS lots bought 2024-02-01 (locked until 2027-02-01) and 2023-12-01 (free): gain/loss harvesting stops at the first locked lot; `elssLocked` lists it with `unlocksOn 2027-02-02`.
  - `test_planning_without_nav`: no NAV → in `left` with "No NAV is available, so redemption figures cannot be worked out."; stale NAV used with its date.
  - `test_planning_wording`: no string anywhere in the result matches `/\b(you should|sell|buy|recommend)\b/i`.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_tax.py -k planning -q` → FAIL.
- [ ] **Step 3: Implement.** Per scheme, walk `open_lots` oldest first with value = units × NAV and tax cost as in Task 2 (grandfathering, rule for selling today). Harvest only from schemes with `tax_class == "equity"`; loss harvesting for any class with rules; ELSS = sub-category `elss`; a lot is locked while `today <= add_months(buy, 36)`.
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_tax.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(mf_tax): gain and loss harvesting and lots turning long-term`

### Task 5: Tax slab field and privacy notice

**Files:**
- Modify: `app/users.py` (`ProfileFields`), `app/privacy.py`, `tests/test_privacy.py`, `tests/test_api.py` (profile tests), the three other version pins

- [ ] **Step 1: Write failing tests:** `PUT /api/profile` with `{"taxSlabPct": 20}` stores and returns it (sealed; `GET` returns it); `7`, `"20"`, `35`, `-5` → 422; `null` clears it; export carries it under the profile fields; `test_notice_names_the_tax_slab`: the profile-details sentence contains "optional income-tax slab"; pins become the new version.
- [ ] **Step 2: Run** `python -m pytest tests/test_privacy.py tests/test_api.py -k "profile or notice" -q` → FAIL.
- [ ] **Step 3: Implement.** `taxSlabPct: Literal[0, 5, 10, 15, 20, 25, 30] | None = None` (strict ints: `Field(strict=True)`). Notice: "the optional profile details (including an optional income-tax slab, used only to estimate tax on mutual fund gains)". `POLICY_VERSION` with a history comment line ("the optional tax slab profile field").
- [ ] **Step 4: Run** `python -m pytest tests/test_privacy.py tests/test_api.py tests/test_brokers.py tests/test_broker_registry.py tests/test_mf_ledger.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(profile): optional income-tax slab for tax estimates`

### Task 6: `GET /api/tax`

**Files:**
- Modify: `app/mf_tax.py` (`tax_from`, `tax_for`), `app/main.py`, `tests/test_mf_tax.py` (API section)

**Interfaces:**
- Produces: `tax_from(schemes, navs, today, fy, slab_pct, *, fmv=nav_2018) -> dict` = `{available: True, fy, fys: fy_options(today), current: bool, position, planning (current FY only, else absent), navDate, statementPeriod, notes}` or `{available: False, hint: mf_ledger.HINT}`; `tax_for(user_id, keys, today, fy: str | None, slab_pct) -> dict` (raises `LockedError`, `ValueError` from `parse_fy`).
- Route `GET /api/tax?fy=`: profile + `history_gate` (no consent, like `/api/returns`); `slab_pct` from `users.profile_fields(user, user["keys"]).get("taxSlabPct")`; `ValueError` → `422 {"error": <message>, "fys": [...]}`; computed in `asyncio.to_thread`.

- [ ] **Step 1: Write failing tests:** no profile → 401; locked → 403 `unlockRequired`; no CAS → `available: false` with the hint; with a stored detailed CAS (existing `test_mf_returns` helpers) → `position` and `planning`, `current: true`; `?fy=2025-26` → no `planning`; `?fy=2027-28`, `?fy=26-27`, `?fy=2022-23` → 422 with the messages; profile B never sees A's figures; slab set → `tax.status "estimated"`; private mode (no CAS stored) → `available: false`; today 2027-03-31 vs 2027-04-01 (monkeypatched `snapshots.local_now`) switches the default FY.
- [ ] **Step 2: Run** `python -m pytest tests/test_mf_tax.py -k api -q` → FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `python -m pytest tests/test_mf_tax.py tests/test_mf_returns.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(api): GET /api/tax with this year's position and planning`

### Task 7: `get_mf_tax` tool, prompt rules and evals

**Files:**
- Modify: `app/snapshots.py` (`mf_tax_tool`, `mf_tax_payload`, `portfolio_context`; `mf_returns_tool` gains an optional `compute`), `app/agent.py` (`MF_TAX_RULES`, appended when `get_mf_tax` is offered), `app/tasks.py` (`ACCOUNT_LOCAL_TOOLS`), `scripts/eval_workflows.py` (`build_context` reads a case's `"mf": "sample"`), `evals/financial_workflows.jsonl`, `evals/request_routing.jsonl`
- Create: `tests/fixtures/mf_tax_sample.py` (ledger, NAV rows and 2018 NAVs for today 2026-10-28 with hand-computed figures)
- Test: `tests/test_mf_tax.py` (tool section), `tests/test_eval_workflows.py` (picks up new cases)

**Interfaces:**
- Produces: `snapshots.mf_tax_tool(compute: Callable[[str | None], dict]) -> LocalTool` (`compute(fy)` raises `ValueError` for a bad FY); `mf_tax_payload(figures, include_planning: bool) -> str` (< `MF_RETURNS_MAX_CHARS`: drops per-scheme rows, then planning options beyond the largest 5 per list, with `omitted` counts); input schema `{fy: string pattern ^\d{4}-\d{2}$, include_planning: boolean}`, `additionalProperties: false`. `portfolio_context` adds it beside `get_mf_returns` (same `has_ledger` check), reading the slab once per request.
- `MF_TAX_RULES`: "get_mf_tax gives the user's mutual fund capital gains and an estimated tax for a financial year, computed in code from their detailed CAS. Quote its figures as given and never recompute them; always call the tax an estimate and name the rule ids or sections it gives; say which funds are left out or unclassified and why; when the slab is not set, say slab-taxed gains depend on the slab. Planning figures are options for information: never tell the user to sell, buy or redeem, and mention the exit load and the 25 March note when giving them."

- [ ] **Step 1: Write failing tests:** tool schema; payload for the fixture under 5,500 chars, also with 60 schemes; bad `fy` → `{"error": ..., "fys": [...]}`; `tasks.is_account_tool("get_mf_tax")`; `get_mf_tax` offered only with a ledger (Portfolio Analyzer) and never to the Finance Agent; the agent quoting `exemption.left` and `tax.total` gets no number warning (scripted provider, as in `test_mf_returns`).
- [ ] **Step 2: Add eval cases** to `evals/financial_workflows.jsonl`, each scripted (calls + answer, `expect`: `task account_data`, `verdict met`, `numbers passed`, `tools`) and `live.grade`:
  - `mf_tax_exemption_left`: "How much of my LTCG exemption is left this year?" → mentions the fixture's `exemption.left`; says `/estimate|exemption/`.
  - `mf_tax_estimate_without_slab`: "How much tax will I pay on my mutual fund gains this year?" → says `/estimate/`, `/slab/`; never `/you should/`.
  - `mf_tax_harvest_options`: "Which of my funds could I harvest gains from before March?" → mentions the gain-harvest gain; says `/exit load/`; never `/\byou should\b|\bI recommend\b/`.
  - `mf_xirr`: "What is my mutual fund XIRR?" → `get_mf_returns` on the same fixture; mentions the portfolio XIRR.
  Routing cases in `evals/request_routing.jsonl`: "How much LTCG exemption do I have left this year?", "Which of my mutual funds could I harvest losses from?", "What is my mutual fund XIRR?" → `account_data`; "What is the LTCG tax rate on equity mutual funds?", "How does tax-loss harvesting work?" → `general_finance`.
- [ ] **Step 3: Run** `python -m pytest tests/test_mf_tax.py tests/test_eval_workflows.py tests/test_tasks.py tests/test_agent.py -q` → FAIL until implemented, then implement and re-run → PASS.
- [ ] **Step 4: Run** `python -m scripts.eval_workflows` and `python -m scripts.eval_tasks` → all scripted cases pass; `python -m scripts.eval_router` (keyword baseline) and record the new counts. When Ollama is reachable: `python -m scripts.eval_workflows --provider ollama --runs 3 --case mf_tax_exemption_left --case mf_tax_estimate_without_slab --case mf_tax_harvest_options --case mf_xirr` and `python -m scripts.eval_router --provider ollama`; report results either way.
- [ ] **Step 5: Commit** `feat(agent): get_mf_tax tool for Portfolio Analyzer` and `test(evals): mutual fund tax and XIRR cases`

### Task 8: Tax tab and slab control

**Files:**
- Modify: `frontend/js/finance.js`, `frontend/js/pages.js` (`renderReturns` tabs, `renderTax`), `frontend/js/app.js` (`PROFILE_FIELDS`), `frontend/index.html` (slab `<select>` in About you, `?v=` busters), `frontend/css/app.css` (exemption bar if needed), `frontend/sw.js` (`CACHE_NAME` +1), `tests/js/finance.test.js`

**Interfaces:**
- Consumes: `GET /api/tax` (Task 6).
- Produces (`finance.js`, exported): `fyLabel("2026-27") === "FY 2026-27"`; `taxClassLabel(c)` (`equity` "Equity-oriented", `debt` "Debt (specified fund)", `other` "Other (gold, international, FoFs)", `unclassified` "Not classified"); `termLabel("short"|"long")` "Short-term"/"Long-term"; `exemptionShare(ex) -> {usedPct, used, left, text}` (`text` "₹75,000 of ₹1,25,000 used; ₹50,000 left"; `usedPct` clamped 0–100; null exemption → `{usedPct: null, text: ''}`); `taxEstimate(tax) -> {short, note}` (estimated → `money(total)`, note "Estimate incl. 4% cess"; partial → `money(total)` + " + slab tax", note `SLAB_NOTE`; none / null total → "Not estimated").
- Tabs: `#/returns` (Returns) and `#/returns/tax` (Tax), parsed like `#/holdings/<source>`; page view route stays `returns`. Tax tab: FY `.filter-chips` from `fys`; KPI strip (Realised short-term, Realised long-term, Exemption left, Estimated tax); exemption bar (`role="meter"`, `aria-valuenow`) with `EXEMPTION_NOTE`; "Gains by rule" `.card-table` (class, term, rule id, section, rate, gains, losses, net); set-off and carry-forward lines; unclassified and excluded funds with reasons; planning cards (current FY only): Gain harvesting, Loss harvesting, Turning long-term soon, ELSS units still locked, each row with its NAV date and source, notes at the end; when `slabPct` is null a line "Add your tax slab" with a button opening the profile dialog (`openProfileDialog()`).
- Profile dialog: "Tax slab" `<select id="field-taxSlabPct">` (Not set, 0%, 5%, 10%, 15%, 20%, 25%, 30%) with the hint "Optional. Used only to estimate tax on mutual fund gains."; `PROFILE_FIELDS` sends it as a number or null.

- [ ] **Step 1: Write failing tests** in `tests/js/finance.test.js` for every helper string above.
- [ ] **Step 2: Run** `node --test tests/js/finance.test.js` → FAIL.
- [ ] **Step 3: Implement**, bump `?v=` for changed files and `CACHE_NAME`.
- [ ] **Step 4: Run** `node --test tests/js/*.test.js && for f in frontend/js/*.js; do node --check "$f"; done` → PASS. Start the app on a temp `APP_DATA_DIR` with a stored detailed CAS fixture, open `#/returns/tax` in the browser pane at desktop and 375px: tabs switch, FY chips reload, the bar and tables read correctly, the slab saved in the profile dialog changes the estimate.
- [ ] **Step 5: Commit** `feat(frontend): Tax tab on Returns with planning cards and slab setting`

### Task 9: Docs, full verification, PR

**Files:**
- Modify: `.claude/CLAUDE.md` (module entry `app/mf_tax.py`; `mf_math` tags/carried lots; `GET /api/tax`; `get_mf_tax` and `MF_TAX_RULES`; `ProfileFields.taxSlabPct`; POLICY_VERSION history; Returns page Tax tab; `test_mf_tax.py`; routing case count), this plan (tick boxes)

- [ ] **Step 1: Update `.claude/CLAUDE.md`** in its terse style.
- [ ] **Step 2: Full gate:** `python -m compileall -q app tests && python -m pytest -q`; `for f in frontend/js/*.js; do node --check "$f"; done`; `node --test tests/js/*.test.js`; `python -m scripts.eval_tasks`; `python -m scripts.eval_workflows` → all green (the known local-only failure `test_broker_registry.py::test_free_plan_refuses_a_third_broker_on_every_connect_route` was fixed on main in 9eeb714; if anything else fails, fix or report it).
- [ ] **Step 3: Commit** `docs: describe mutual fund tax position and planning`; push; open a PR to `main` (summary, the verified rule table, the "could not confirm" list, deviations, test plan); bind it; leave a batch-4 hand-over note in the shared notes folder (interfaces for `GET /api/tax/export`: `tax_lots()` gain rows with `fmv_2018`, `sections`, rule ids).
