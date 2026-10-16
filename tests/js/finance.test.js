// Unit tests for frontend/js/finance.js. Run with: node --test tests/js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const finance = require('../../frontend/js/finance.js');

const close = (actual, expected, tolerance = 1) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);

test('formatting uses Indian grouping, lakh and crore', () => {
  assert.equal(finance.money(1372800), '₹13,72,800');
  assert.equal(finance.moneyShort(1372800), '₹13.73 L');
  assert.equal(finance.moneyShort(23400000), '₹2.34 Cr');
  assert.equal(finance.moneyShort(-250000), '−₹2.50 L');
  assert.equal(finance.moneyShort(950), '₹950');
  assert.equal(finance.signedMoney(-1800), '−₹1,800');
  assert.equal(finance.signedShort(140000), '+₹1.40 L');
  assert.equal(finance.percent(12.345, { signed: true }), '+12.3%');
  assert.equal(finance.percent(-0.874), '−0.87%');
  assert.equal(finance.money(NaN), '—');
});

test('timeAgo rounds down to minutes, hours and days', () => {
  const now = Date.UTC(2026, 9, 15, 12);
  assert.equal(finance.timeAgo(now - 20_000, now), 'just now');
  assert.equal(finance.timeAgo(now - 5 * 60_000, now), '5m ago');
  assert.equal(finance.timeAgo(now - 3 * 3_600_000, now), '3h ago');
  assert.equal(finance.timeAgo(now - 50 * 3_600_000, now), '2d ago');
});

test('SIP and lumpsum growth match the standard formulas', () => {
  // ₹10,000 a month for 10 years at 12%: ≈ ₹23.23 lakh (start-of-month instalments).
  close(finance.sipValue(10000, 12, 120), 2323391);
  assert.equal(finance.sipValue(1000, 0, 12), 12000);
  close(finance.grow(100000, 12, 12), 112683);
});

test('monthlyNeeded closes the gap and is zero when savings already suffice', () => {
  const monthly = finance.monthlyNeeded(3984620, 0, 11, 96);
  close(finance.sipValue(monthly, 11, 96), 3984620);
  assert.equal(finance.monthlyNeeded(100000, 200000, 8, 24), 0);
  assert.equal(finance.monthlyNeeded(50000, 0, 10, 0), 50000);
});

test('goal amounts keep their own currency', () => {
  assert.equal(finance.amount(2500000, 'INR'), '₹25,00,000');
  assert.equal(finance.amount(80000, 'USD'), '$80,000');
  assert.equal(finance.amountShort(2500000, 'INR'), '₹25.00 L');
  assert.equal(finance.amountShort(45200, 'USD'), '$45.2K');
  assert.equal(finance.amountShort(1500, 'EUR'), '€1.5K');
  assert.equal(finance.amount(NaN, 'GBP'), '—');
});

test('goal totals are kept per currency and never added across currencies', () => {
  const goals = [
    { currency: 'USD', targetAmount: 50000 }, { currency: 'INR', targetAmount: 2000000 },
    { currency: 'INR', targetAmount: 500000 }, { currency: 'USD', targetAmount: 30000 }, { targetAmount: 7 },
  ];
  assert.deepEqual(finance.totalsByCurrency(goals, 'targetAmount'), [['INR', 2500000], ['USD', 80000]]);
  assert.equal(finance.formatTotals(finance.totalsByCurrency(goals, 'targetAmount')), '₹25.00 L + $80K');
  assert.equal(finance.formatTotals([]), '—');
});

test('goal status comes from the server plan', () => {
  assert.deepEqual(finance.goalStatus({ status: 'on_track' }), ['On track', 'is-connected']);
  assert.deepEqual(finance.goalStatus({ status: 'past_due' }), ['Past due', 'is-behind']);
  assert.deepEqual(finance.goalStatus(undefined), ['Missing details', 'is-behind']);
  assert.equal(finance.goalIsOnTrack({ plan: { status: 'reached' } }), true);
  assert.equal(finance.goalIsOnTrack({ plan: { status: 'behind' } }), false);
  assert.equal(finance.goalIsOnTrack({}), false);
  const plan = { baseScenario: 'assumed', scenarios: [{ name: 'no_growth' }, { name: 'assumed' }] };
  assert.equal(finance.baseScenario(plan).name, 'assumed');
  assert.equal(finance.baseScenario({ scenarios: [{ name: 'no_growth' }] }).name, 'no_growth');
  assert.equal(finance.baseScenario({ scenarios: [] }), null);
});

test('goalKind picks an icon from the goal name', () => {
  assert.equal(finance.goalKind('Buy a home'), 'home');
  assert.equal(finance.goalKind("Child's education"), 'education');
  assert.equal(finance.goalKind('Retirement corpus'), 'retirement');
  assert.equal(finance.goalKind('New car'), 'car');
  assert.equal(finance.goalKind('Europe trip'), 'travel');
  assert.equal(finance.goalKind('Emergency fund'), 'emergency');
  assert.equal(finance.goalKind('Something else'), 'other');
  assert.equal(finance.goalKind(undefined), 'other');
});

test('goalIcon prefers the chosen kind and falls back to the name', () => {
  assert.equal(finance.goalIcon({ name: 'Buy a home', kind: 'travel' }), 'travel');
  assert.equal(finance.goalIcon({ name: 'Buy a home', kind: 'other' }), 'other');
  assert.equal(finance.goalIcon({ name: 'Buy a home' }), 'home');
  assert.equal(finance.goalIcon({ name: 'Buy a home', kind: 'yacht' }), 'home');
  assert.equal(finance.goalIcon(null), 'other');
});

test('historyChange measures first to last point and labels the span', () => {
  const points = [
    { date: '2026-06-17', networthInr: 1000000 },
    { date: '2026-08-01', networthInr: null },
    { date: '2026-10-15', networthInr: 1124000 },
  ];
  const change = finance.historyChange(points);
  assert.equal(change.change, 124000);
  close(change.pct, 12.4, 0.001);
  assert.equal(change.days, 120);
  assert.equal(change.label, '4M');
  assert.equal(finance.historyChange([{ date: '2026-10-15', networthInr: 5 }]), null);
  assert.equal(finance.spanLabel(365), '1Y');
  assert.equal(finance.spanLabel(7), '7D');
});

function portfolio(overrides = {}) {
  return {
    available: true,
    holdings: [
      { name: 'Infosys', current_value_inr: 180000, one_day_change_inr: 2300 },
      { name: 'TCS', current_value_inr: 190000, one_day_change_inr: -1800 },
      { name: 'HDFC Bank', current_value_inr: 130000, one_day_change_inr: 950 },
      { name: 'Index fund', current_value_inr: 125000, one_day_change_inr: 0 },
    ],
    figures: {
      concentration: { largest_holding: 'TCS', largest_holding_pct: 30.4, top_5_pct: 100 },
      by_asset_type: [{ asset_type: 'US_STOCK', current_value_inr: 100000, share_of_portfolio_pct: 8 }],
      usd: { US_STOCK: { current_value_usd: 1200 } },
    },
    ...overrides,
  };
}

test('portfolioInsights describes the day move, movers and concentration', () => {
  const insights = finance.portfolioInsights(portfolio());
  assert.deepEqual(insights.map((item) => item.kind), ['day', 'gainer', 'loser', 'concentration']);
  assert.equal(insights[0].title, 'Your holdings are up ₹1,450 on the day');
  assert.equal(insights[0].detail, '+0.23% · 2 up, 1 down');
  assert.equal(insights[1].title, 'Infosys up 1.29%');
  assert.equal(insights[2].title, 'TCS down 0.94%');
  assert.equal(insights[3].tone, 'warn');
});

test('portfolioInsights falls back to allocation when nothing moved', () => {
  const quiet = portfolio({ holdings: [{ name: 'Index fund', current_value_inr: 125000, one_day_change_inr: 0 }] });
  const insights = finance.portfolioInsights(quiet);
  assert.deepEqual(insights.map((item) => item.kind), ['concentration', 'global']);
  assert.equal(insights[1].detail, '$1,200 (₹1.00 L)');
  assert.deepEqual(finance.portfolioInsights({ available: false }), []);
  assert.deepEqual(finance.portfolioInsights(null), []);
});

const tax = (regime, inputs) => finance.regimeTax(regime, inputs);

test('new regime: no tax up to ₹12 lakh taxable after the standard deduction', () => {
  const result = tax('new', { salary: 1275000 });
  assert.equal(result.taxable, 1200000);
  assert.equal(result.slabTax, 60000);
  assert.equal(result.rebate, 60000);
  assert.equal(result.total, 0);
});

test('new regime: marginal relief just above ₹12 lakh', () => {
  // Slab tax ₹61,500 is capped at the ₹10,000 earned above ₹12 lakh, plus 4% cess.
  assert.equal(tax('new', { salary: 1285000 }).total, 10400);
});

test('new regime: slabs and cess at ₹16 lakh taxable', () => {
  const result = tax('new', { salary: 1675000 });
  assert.equal(result.slabTax, 120000);
  assert.equal(result.cess, 4800);
  assert.equal(result.total, 124800);
});

test('new regime ignores old-regime deductions; other income gets no standard deduction', () => {
  assert.equal(tax('new', { salary: 1675000, deductions: 150000 }).total, 124800);
  assert.equal(tax('new', { other: 1600000 }).taxable, 1600000);
  assert.equal(tax('new', { salary: 40000 }).standardDeduction, 40000);
});

test('old regime: deductions, slabs and the ₹5 lakh rebate', () => {
  const result = tax('old', { salary: 1050000, deductions: 150000 });
  assert.equal(result.taxable, 850000);
  assert.equal(result.total, 85800);
  assert.equal(tax('old', { salary: 550000 }).total, 0);
});

test('surcharge applies above ₹50 lakh with marginal relief at the threshold', () => {
  const sixty = tax('new', { other: 6000000 });
  assert.equal(sixty.slabTax, 1380000);
  assert.equal(sixty.surcharge, 138000);
  assert.equal(sixty.total, 1578720);
  // ₹10,000 above ₹50 lakh: tax plus surcharge is capped at ₹10,80,000 + ₹10,000.
  const justOver = tax('new', { other: 5010000 });
  assert.equal(justOver.slabTax + justOver.surcharge, 1090000);
  assert.equal(justOver.total, 1133600);
});

test('incomeTax compares both regimes', () => {
  const result = finance.incomeTax({ salary: 1500000, deductions: 200000 });
  assert.equal(result.better, 'new');
  assert.equal(result.saving, Math.abs(result.new.total - result.old.total));
  assert.equal(finance.TAX_RULES.label, 'FY 2026-27');
});

test('financial independence has its own goal kind and drafts are recognised', () => {
  assert.equal(finance.goalKind('Financial independence by 50'), 'financial_independence');
  assert.equal(finance.goalKind('Early retirement fund'), 'financial_independence');
  assert.equal(finance.goalKind('Retirement corpus'), 'retirement');
  assert.equal(finance.goalIcon({ name: 'x', kind: 'financial_independence' }), 'financial_independence');
  assert.equal(finance.isDraft({ status: 'draft' }), true);
  assert.equal(finance.isDraft({ status: 'active' }), false);
  assert.equal(finance.isDraft({}), false);
  assert.equal(finance.isDraft(null), false);
});

test('headlineTarget uses the plan\'s inflation-adjusted target and keeps the entered one as today\'s money', () => {
  const inflated = { targetAmount: 2000000, plan: { effectiveTarget: 2247200, inflation: { inflationPct: 6 } } };
  assert.deepEqual(finance.headlineTarget(inflated), { amount: 2247200, todayMoney: 2000000, inflationPct: 6 });
  assert.deepEqual(finance.headlineTarget({ targetAmount: 500000, plan: { effectiveTarget: 500000, inflation: null } }),
    { amount: 500000, todayMoney: null, inflationPct: null });
  assert.deepEqual(finance.headlineTarget({ targetAmount: null, status: 'draft' }), { amount: null, todayMoney: null, inflationPct: null });
  assert.deepEqual(finance.headlineTarget(null), { amount: null, todayMoney: null, inflationPct: null });
});

test('goal totals skip drafts without a currency or an amount instead of counting them as zero', () => {
  const goals = [
    { currency: 'INR', targetAmount: 100000, status: 'active' },
    { currency: null, targetAmount: null, status: 'draft' },
    { currency: 'INR', targetAmount: null, status: 'draft' },
  ];
  assert.deepEqual(finance.totalsByCurrency(goals, 'targetAmount'), [['INR', 100000]]);
  assert.deepEqual(finance.totalsByCurrency([goals[2]], 'targetAmount'), []);
});

test('allocationRemaining is what is left of 100 and goes negative when over-assigned', () => {
  assert.equal(finance.allocationRemaining([60, 40], 0), 0);
  assert.equal(finance.allocationRemaining([30], 20), 50);
  assert.equal(finance.allocationRemaining([], 0), 100);
  assert.equal(finance.allocationRemaining([70, 40], 10), -20);
  assert.equal(finance.allocationRemaining(['25', ''], '5'), 70);
  assert.equal(finance.allocationRemaining([NaN, -5], undefined), 100);
});

test('suitabilityGroups lists category labels by verdict', () => {
  const groups = finance.suitabilityGroups({ categories: [
    { label: 'Liquid', verdict: 'allowed' }, { label: 'Hybrid', verdict: 'caution' },
    { label: 'Equity', verdict: 'unsuitable' }, { label: 'Debt', verdict: 'allowed' }, { label: 'Odd', verdict: 'other' },
  ] });
  assert.deepEqual(groups, { allowed: ['Liquid', 'Debt'], caution: ['Hybrid'], unsuitable: ['Equity'] });
  assert.deepEqual(finance.suitabilityGroups(null), { allowed: [], caution: [], unsuitable: [] });
});

test('alternativeLabel tags each alternative with its basis and formats the server numbers', () => {
  assert.deepEqual(
    finance.alternativeLabel({ kind: 'increase_monthly', basis: 'zero_return', requiredMonthly: 32308, annualReturnPct: 0 }, 'INR'),
    { tag: 'No growth', text: 'Raise the monthly contribution to ₹32,308' },
  );
  assert.deepEqual(
    finance.alternativeLabel({ kind: 'extend_deadline', basis: 'assumed_return', annualReturnPct: 9, extraMonths: 14, newTargetDate: '2036-02-29' }),
    { tag: 'At your 9%', text: 'Move the date by 14 months, to 2036-02-29' },
  );
  assert.equal(
    finance.alternativeLabel({ kind: 'extend_deadline', basis: 'zero_return', extraMonths: 3, newTargetDate: '2031-01-16' }, 'INR', (iso) => `[${iso}]`).text,
    'Move the date by 3 months, to [2031-01-16]',
  );
  assert.equal(
    finance.alternativeLabel({ kind: 'reduce_target', basis: 'zero_return', reachableTarget: 1500, inTodayMoney: true }, 'USD').text,
    "Lower the target to $1,500 in today's money",
  );
  assert.equal(
    finance.alternativeLabel({ kind: 'earmark_lump_sum', basis: 'zero_return', lumpSum: 250000 }, 'INR').text,
    'Earmark ₹2,50,000 more today',
  );
});
