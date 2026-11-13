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
  // Reaching the target only under the assumed return is not shown as a plain "On track".
  assert.deepEqual(finance.goalStatus({ status: 'on_track', feasibility: { status: 'depends_on_returns' } }), ['On track if returns hold', 'is-behind']);
  assert.deepEqual(finance.goalStatus({ status: 'on_track', feasibility: { status: 'covered_without_growth' } }), ['On track', 'is-connected']);
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

test('historySeries uses the combined value only when broker history exists', () => {
  const plain = { history: [{ date: '2026-11-01', networthInr: 10 }, { date: '2026-11-02', networthInr: null }] };
  const same = finance.historySeries(plain);
  assert.deepEqual(same.points, [{ date: '2026-11-01', networthInr: 10 }]);
  assert.equal(same.includesBrokers, false);
  assert.equal(same.note, '');
  const withBrokers = finance.historySeries({
    includesBrokers: true,
    history: [
      { date: '2026-11-01', networthInr: 10, combinedInr: 12, combined: { partial: false } },
      { date: '2026-11-02', networthInr: null, combinedInr: 7, combined: { partial: true } },
      { date: '2026-11-03', networthInr: 11, combinedInr: null, combined: { partial: true } },
    ],
  });
  assert.deepEqual(withBrokers.points.map((point) => point.networthInr), [12, 7]);  // null is dropped, never zero
  assert.equal(withBrokers.partial, true);
  assert.match(withBrokers.note, /brokers/);
  assert.deepEqual(finance.historySeries(null).points, []);
});

test('spanPhrase reads a change span in words', () => {
  assert.equal(finance.spanPhrase(120), 'over the last 4 months');
  assert.equal(finance.spanPhrase(30), 'over the last month');
  assert.equal(finance.spanPhrase(12), 'over the last 12 days');
  assert.equal(finance.spanPhrase(1), 'over the last day');
  assert.equal(finance.spanPhrase(365), 'over the last year');
  assert.equal(finance.spanPhrase(0), '');
  assert.equal(finance.spanPhrase(NaN), '');
});

test('niceTicks covers the data with round steps and never invents a range', () => {
  assert.deepEqual(finance.niceTicks(1543288, 1605331), [1540000, 1560000, 1580000, 1600000, 1620000]);
  assert.deepEqual(finance.niceTicks(-5000, 12000), [-5000, 0, 5000, 10000, 15000]);
  assert.deepEqual(finance.niceTicks(0.1, 0.37), [0.1, 0.2, 0.3, 0.4]);  // no floating-point noise
  const flat = finance.niceTicks(250000, 250000);
  assert.ok(flat[0] < 250000 && flat[flat.length - 1] > 250000);  // a flat line still gets an axis around it
  assert.deepEqual(finance.niceTicks(NaN, 10), []);
  assert.deepEqual(finance.niceTicks(5, Infinity), []);
});

test('goalTone separates on track, on track only if returns hold, and behind', () => {
  assert.equal(finance.goalTone({ status: 'on_track' }), 'good');
  assert.equal(finance.goalTone({ status: 'reached' }), 'good');
  assert.equal(finance.goalTone({ status: 'on_track', feasibility: { status: 'depends_on_returns' } }), 'conditional');
  assert.equal(finance.goalTone({ status: 'behind' }), 'behind');
  assert.equal(finance.goalTone({ status: 'insufficient_data' }), 'behind');
  assert.equal(finance.goalTone(null), 'behind');
});

test('allocationShares sorts asset types by value and leaves out missing or empty ones', () => {
  const shares = finance.allocationShares([
    { asset_type: 'EPF', current_value_inr: 250000 },
    { asset_type: 'MF', current_value_inr: 750000 },
    { asset_type: 'GOLD', current_value_inr: 0 },
    { asset_type: 'FD', current_value_inr: null },
  ]);
  assert.deepEqual(shares.map((row) => row.assetType), ['MF', 'EPF']);
  assert.deepEqual(shares.map((row) => row.pct), [75, 25]);
  assert.equal(shares[0].value, 750000);
  assert.deepEqual(finance.allocationShares([]), []);
  assert.deepEqual(finance.allocationShares(null), []);
});

const HOUR = 3600 * 1000;

test('brokersNeedingAttention flags ended logins and day-old holdings only for connected brokers', () => {
  const now = Date.UTC(2026, 10, 6, 12);
  const brokers = [
    { broker: 'zerodha', name: 'Zerodha', status: 'needs_login', holdingsAt: now - 2 * HOUR },
    { broker: 'fyers', name: 'FYERS', status: 'connected', holdingsAt: now - 25 * HOUR },
    { broker: 'dhan', name: 'Dhan', status: 'connected', holdingsAt: now - 23 * HOUR },
    { broker: 'groww', name: 'Groww', status: 'connected', holdingsAt: null },
    { broker: 'kotak', name: 'Kotak Neo', status: 'pending', holdingsAt: null },
    { broker: 'upstox', name: 'Upstox', status: 'disconnected', holdingsAt: null },
    { broker: 'x', name: 'X', status: 'unavailable', holdingsAt: now - 90 * HOUR },
  ];
  assert.deepEqual(finance.brokersNeedingAttention(brokers, now).map((item) => item.broker), ['zerodha', 'fyers']);
  assert.deepEqual(finance.brokersNeedingAttention(null, now), []);
});

test('brokerBannerText says when the holdings are from', () => {
  const now = Date.UTC(2026, 10, 6, 12);
  const ended = finance.brokerBannerText({ name: 'Zerodha', status: 'needs_login', holdingsAt: now - 3 * HOUR }, now);
  assert.match(ended, /^Your Zerodha login has ended\. Holdings shown are from /);
  assert.match(finance.brokerBannerText({ name: 'Zerodha', status: 'needs_login', holdingsAt: null }, now), /Reconnect to see your holdings/);
  assert.match(finance.brokerBannerText({ name: 'FYERS', status: 'connected', holdingsAt: now - 30 * HOUR }, now), /^Your FYERS holdings were last fetched /);
});

test('brokerReconnectAction depends on how the broker signs in', () => {
  assert.deepEqual(finance.brokerReconnectAction({ broker: 'upstox', auth: 'oauth' }), { type: 'link', href: '/auth/brokers/upstox/connect' });
  assert.deepEqual(finance.brokerReconnectAction({ broker: 'kotak', auth: 'mcp_login_qr' }), { type: 'dialog' });
  assert.deepEqual(finance.brokerReconnectAction({ broker: 'zerodha', auth: 'mcp_login_tool' }), { type: 'login-link' });
});

test('banner dismissal lasts the local day and survives broken storage', () => {
  const store = new Map();
  const storage = { getItem: (key) => (store.has(key) ? store.get(key) : null), setItem: (key, value) => store.set(key, value) };
  const morning = new Date(2026, 10, 6, 9).getTime();
  const night = new Date(2026, 10, 6, 23).getTime();
  const tomorrow = new Date(2026, 10, 7, 9).getTime();
  assert.equal(finance.bannerDismissed(storage, 'zerodha', morning), false);
  finance.dismissBanner(storage, 'zerodha', morning);
  assert.equal(finance.bannerDismissed(storage, 'zerodha', night), true);
  assert.equal(finance.bannerDismissed(storage, 'fyers', night), false);
  assert.equal(finance.bannerDismissed(storage, 'zerodha', tomorrow), false);
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.doesNotThrow(() => finance.dismissBanner(broken, 'zerodha', morning));
  assert.equal(finance.bannerDismissed(broken, 'zerodha', morning), false);
  assert.doesNotThrow(() => finance.dismissBanner(null, 'zerodha', morning));
  assert.equal(finance.bannerDismissed(null, 'zerodha', morning), false);
});

test('brokersToCheck asks each connected broker at most once an hour', () => {
  const now = Date.UTC(2026, 10, 6, 12);
  const brokers = [
    { broker: 'zerodha', status: 'connected' }, { broker: 'fyers', status: 'connected' },
    { broker: 'dhan', status: 'needs_login' }, { broker: 'groww', status: 'disconnected' },
  ];
  assert.deepEqual(finance.brokersToCheck(brokers, {}, now), ['zerodha', 'fyers']);
  assert.deepEqual(finance.brokersToCheck(brokers, { zerodha: now - 10 * 60 * 1000 }, now), ['fyers']);
  assert.deepEqual(finance.brokersToCheck(brokers, { zerodha: now - 61 * 60 * 1000 }, now), ['zerodha', 'fyers']);
  assert.deepEqual(finance.brokersToCheck(undefined, undefined, now), []);
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

test('fund figures carry their source and date, and only dated fresh figures read as current', () => {
  const day = (iso) => `[${iso}]`;
  const current = { value: '14.20', asOf: '2026-10-15', status: 'current', source: 'INDmoney get_mf_by_category' };
  assert.equal(finance.fundMetricValue('return1yPct', current), '14.20%');
  assert.equal(finance.fundMetricNote(current, day), 'as of [2026-10-15] · INDmoney');
  assert.equal(finance.fundMetricNote({ ...current, status: 'stale' }, day), 'old: as of [2026-10-15] · INDmoney');
  const undated = { value: '0.62', asOf: null, status: 'undated', source: 'INDmoney get_mf_by_category', retrievedAt: '2026-10-16T04:00:00Z' };
  assert.equal(finance.fundMetricNote(undated, day), 'no date given · fetched [2026-10-16] · INDmoney');
  // "current" without a date is never shown as current.
  assert.equal(finance.fundMetricNote({ ...current, asOf: null }, day), 'no date given · INDmoney');
  assert.equal(finance.fundMetricValue('benchmark', { value: 'NIFTY 500 TRI' }), 'NIFTY 500 TRI');
  assert.equal(finance.fundMetricValue('return5yPct', undefined), '—');
  assert.equal(finance.fundMetricNote(undefined, day), '');
});

test('holdingsTabs lists All first only when there is a combined total', () => {
  const sources = [
    { id: 'indmoney', name: 'INDmoney', available: true, stale: false, asOf: 1 },
    { id: 'zerodha', name: 'Zerodha', available: true, stale: true, asOf: 2 },
  ];
  const tabs = finance.holdingsTabs({ sources, combined: { available: true } });
  assert.deepEqual(tabs.map((tab) => tab.id), ['all', 'indmoney', 'zerodha']);
  assert.equal(tabs[2].stale, true);
  assert.deepEqual(finance.holdingsTabs({ sources, combined: { available: false } }).map((tab) => tab.id), ['indmoney', 'zerodha']);
  assert.deepEqual(finance.holdingsTabs({ sources: [], combined: { available: false } }), []);
  assert.deepEqual(finance.holdingsTabs(null), []);
});

test('holdingInitials takes two letters from the first two words, else from the one word', () => {
  assert.equal(finance.holdingInitials('Parag Parikh Flexi Cap Fund'), 'PP');
  assert.equal(finance.holdingInitials('HDFC Index Fund Nifty 50'), 'HI');
  assert.equal(finance.holdingInitials('Apple'), 'AP');
  assert.equal(finance.holdingInitials('  infosys  '), 'IN');
  assert.equal(finance.holdingInitials('(G) Axis Liquid'), 'AL');
  assert.equal(finance.holdingInitials(''), '?');
  assert.equal(finance.holdingInitials(null), '?');
});

test('holdingTypeCounts keeps the order types first appear in', () => {
  const rows = [{ asset_type: 'MF' }, { asset_type: 'IND_STOCK' }, { asset_type: 'MF' }, { asset_type: null }, { asset_type: 'US_STOCK' }];
  assert.deepEqual(finance.holdingTypeCounts(rows), [{ type: 'MF', count: 2 }, { type: 'IND_STOCK', count: 1 }, { type: 'US_STOCK', count: 1 }]);
  assert.deepEqual(finance.holdingTypeCounts(undefined), []);
});

test('filterHoldings filters by type and search, and sorts missing figures last', () => {
  const rows = [
    { name: 'Infosys', code: 'INFY', asset_type: 'IND_STOCK', broker: 'INDmoney', current_value_inr: 90000, pnl_inr: 9000, pnl_pct: 10.9, one_day_change_inr: 232 },
    { name: 'HDFC Bank', code: 'HDFCBANK', asset_type: 'IND_STOCK', broker: 'Zerodha', current_value_inr: 155000, pnl_inr: null, pnl_pct: null, one_day_change_inr: -44 },
    { name: 'Axis Liquid Fund', code: '120', asset_type: 'MF', broker: null, current_value_inr: 146000, pnl_inr: 3000, pnl_pct: 2.1, one_day_change_inr: null },
  ];
  const names = (list) => list.map((row) => row.name);
  assert.deepEqual(names(finance.filterHoldings(rows)), ['HDFC Bank', 'Axis Liquid Fund', 'Infosys']);
  assert.deepEqual(names(finance.filterHoldings(rows, { type: 'MF' })), ['Axis Liquid Fund']);
  assert.deepEqual(names(finance.filterHoldings(rows, { query: 'zerodha' })), ['HDFC Bank']);
  assert.deepEqual(names(finance.filterHoldings(rows, { query: ' infy ' })), ['Infosys']);
  assert.deepEqual(names(finance.filterHoldings(rows, { sort: 'pnl' })), ['Infosys', 'Axis Liquid Fund', 'HDFC Bank']);
  assert.deepEqual(names(finance.filterHoldings(rows, { sort: 'day' })), ['Infosys', 'HDFC Bank', 'Axis Liquid Fund']);
  assert.deepEqual(names(finance.filterHoldings(rows, { sort: 'name' })), ['Axis Liquid Fund', 'HDFC Bank', 'Infosys']);
  // The caller's list is left in its order.
  assert.deepEqual(names(rows), ['Infosys', 'HDFC Bank', 'Axis Liquid Fund']);
});

test('brokerStatusLabel never shows an expired login as current', () => {
  const now = Date.UTC(2026, 10, 6, 12);
  assert.equal(finance.brokerStatusLabel('connected', null, now), 'Connected');
  assert.match(finance.brokerStatusLabel('pending', null, now), /Waiting for you to log in/);
  assert.match(finance.brokerStatusLabel('needs_login', now - 3 * 3600000, now), /Login expired.*Showing holdings from 3h ago/);
  assert.match(finance.brokerStatusLabel('needs_login', null, now), /^Login expired/);
  assert.match(finance.brokerStatusLabel('unavailable', null, now), /Could not reach/);
  assert.equal(finance.brokerStatusLabel('disconnected', null, now), 'Not connected');
});

test('monogram, logo markup and the connect limit', () => {
  const f = require('../../frontend/js/finance.js');
  assert.equal(f.monogram('upstox'), 'U');
  assert.equal(f.monogram('  '), '?');
  const html = f.brokerLogoHtml('zerodha', 'Zerodha', '/brokers/zerodha.png');
  assert.match(html, /src="\/brokers\/zerodha\.png"/);
  assert.match(html, /alt=""/);
  assert.match(html, /loading="lazy"/);
  assert.match(html, /data-monogram="Z"/);
  assert.match(f.brokerLogoHtml('x"><b>', 'X', 'https://evil.example/a.png'), /src="\/brokers\/x%22%3E%3Cb%3E\.png"/);
  assert.match(f.monogramHtml('<'), /&lt;/);
  assert.equal(f.brokerConnectBlocked({ status: 'disconnected' }, 2, 2), true);
  assert.equal(f.brokerConnectBlocked({ status: 'disconnected' }, 2, 1), false);
  assert.equal(f.brokerConnectBlocked({ status: 'connected' }, 2, 3), false);
  assert.equal(f.brokerConnectBlocked({ status: 'disconnected' }, null, 9), false);
  assert.match(f.LIMIT_NOTE, /up to 2 brokers/);
});

test('INDmoney comes first in the tabs and is reconnected through its own route', () => {
  const f = require('../../frontend/js/finance.js');
  const tabs = f.holdingsTabs({ sources: [{ id: 'zerodha', name: 'Zerodha', available: true }, { id: 'indmoney', name: 'INDmoney', available: true, logo: '/brokers/indmoney.png' }], combined: { available: true } });
  assert.deepEqual(tabs.map((tab) => tab.id), ['all', 'indmoney', 'zerodha']);
  assert.equal(tabs[0].logo, null);
  assert.equal(tabs[2].logo, '/brokers/zerodha.png');
  assert.deepEqual(f.brokerReconnectAction({ broker: 'indmoney', auth: 'indmoney_oauth' }), { type: 'link', href: '/auth/indmoney/connect' });
  const now = 10 * 24 * 3600 * 1000;
  const stale = { broker: 'indmoney', auth: 'indmoney_oauth', status: 'connected', holdingsAt: 1 };
  assert.deepEqual(f.brokersNeedingAttention([stale, { broker: 'indmoney', auth: 'indmoney_oauth', status: 'needs_login' }], now).map((b) => b.status), ['needs_login']);
  assert.deepEqual(f.brokersToCheck([stale, { broker: 'zerodha', status: 'connected' }], {}, now), ['zerodha']);
});

test('mutual fund returns: XIRR, short holdings and missing figures', () => {
  assert.deepEqual(finance.returnFigure({ xirr: 12.34, absoluteReturnPct: null, returnReason: null }),
    { text: '12.34% a year (XIRR)', short: '12.34%', label: 'a year (XIRR)', note: '' });
  assert.deepEqual(finance.returnFigure({ xirr: -3.5, returnReason: null }),
    { text: '−3.50% a year (XIRR)', short: '−3.50%', label: 'a year (XIRR)', note: '' });
  assert.deepEqual(finance.returnFigure({ xirr: null, absoluteReturnPct: 5.1, returnReason: 'under_one_year' }),
    { text: '5.10% (held under a year)', short: '5.10%', label: 'held under a year', note: 'Not annualised: held for less than a year.' });
  const excluded = finance.returnFigure({ xirr: null, absoluteReturnPct: null, returnReason: 'unreconciled', reason: 'Units do not add up.' });
  assert.equal(excluded.text, 'Not available');
  assert.equal(excluded.short, 'Not available');
  assert.equal(excluded.note, 'Units do not add up.');
  assert.equal(finance.returnFigure({ xirr: null, returnReason: 'no_convergence' }).note, 'XIRR could not be worked out from these cash flows.');
  // Unknown is never shown as zero.
  assert.equal(finance.returnFigure({ xirr: null, absoluteReturnPct: null }).text, 'Not available');
  assert.equal(finance.money(null), '—');
});

test('mutual fund returns: status chips and NAV notes', () => {
  assert.deepEqual(finance.returnStatus('ok'), { label: '', tone: '' });
  assert.deepEqual(finance.returnStatus('nav_stale'), { label: 'Old NAV', tone: 'is-warning' });
  assert.equal(finance.returnStatus('incomplete_history').label, 'History missing');
  assert.equal(finance.returnStatus('unreconciled').label, 'Units don’t add up');
  assert.equal(finance.returnStatus('unsupported').label, 'Not supported');
  assert.equal(finance.returnStatus('no_nav').label, 'No NAV');
  assert.equal(finance.navNote({ navSource: 'amfi', navDate: '2026-11-10' }), 'AMFI NAV of 10 Nov 2026');
  assert.equal(finance.navNote({ navSource: 'statement', navDate: '2026-09-30' }), 'Statement NAV of 30 Sep 2026');
  assert.equal(finance.navNote({ navSource: null, navDate: null }), 'No NAV available');
});

test('mutual fund returns: benchmark comparison', () => {
  assert.deepEqual(finance.benchmarkFigure({ label: 'Nifty 50', status: 'ok', xirr: 11.2, absoluteReturnPct: null }),
    { short: '11.20%', label: 'Nifty 50 (XIRR)', note: '' });
  assert.deepEqual(finance.benchmarkFigure({ label: 'Nifty 50', status: 'ok', xirr: null, absoluteReturnPct: -2, returnReason: 'under_one_year' }),
    { short: '−2.00%', label: 'Nifty 50, held under a year', note: '' });
  const hybrid = 'Hybrid funds mix asset classes, so no single index fits.';
  assert.deepEqual(finance.benchmarkFigure({ status: 'none', reason: hybrid }), { short: 'Not compared', label: '', note: hybrid });
  assert.deepEqual(finance.benchmarkFigure(null), { short: 'Not compared', label: '', note: '' });
  assert.equal(finance.benchmarkDifference(123456, 'Nifty 50'), '₹1,23,456 more than in Nifty 50');
  assert.equal(finance.benchmarkDifference(-12345, 'Nifty 50'), '₹12,345 less than in Nifty 50');
  assert.equal(finance.benchmarkDifference(0, 'Nifty 50'), 'Same as in Nifty 50');
  assert.equal(finance.benchmarkDifference(null, 'Nifty 50'), '');
  assert.equal(finance.benchmarkNavNote({ label: 'Nifty 50', navDate: '2026-11-12' }), 'Nifty 50 index fund NAV of 12 Nov 2026');
  assert.equal(finance.benchmarkNavNote({ label: 'Liquid fund', navDate: '2026-11-12' }), 'Liquid fund NAV of 12 Nov 2026');
  assert.equal(finance.benchmarkNavNote({ label: 'Nifty 50', navDate: null }), 'No benchmark NAV yet');
});

/* Goal cards: verdict sentence and ways to close the gap (plan shapes as the server returns them) */
const scenario = (name, basis, annualReturnPct, projectedAmount, difference) => ({ name, basis, annualReturnPct, projectedAmount, difference, onTrack: difference >= 0 });

test('goalVerdict reads only the plan: projection, no-growth cover, reliance on returns or the shortfall', () => {
  const covered = {
    status: 'on_track', currency: 'INR', targetDate: '2027-11-01', baseScenario: 'assumed',
    scenarios: [scenario('no_growth', 'zero_return', 0, 376549, 76549), scenario('assumed', 'assumed_return', 6.5, 396508, 96508)],
    feasibility: { status: 'covered_without_growth' },
  };
  assert.equal(finance.goalVerdict(covered), 'Reaches the target even with no growth. Projected ₹3.97 L at 6.5% a year.');
  const relies = {
    status: 'on_track', currency: 'INR', baseScenario: 'assumed',
    scenarios: [scenario('no_growth', 'zero_return', 0, 1343006, -156994), scenario('assumed', 'assumed_return', 11, 1962432, 462432)],
    feasibility: { status: 'depends_on_returns' },
  };
  assert.equal(finance.goalVerdict(relies), 'Projected ₹19.62 L at 11% a year. With no growth it falls short, so it relies on returns.');
  const behind = {
    status: 'behind', currency: 'INR', baseScenario: 'assumed',
    scenarios: [scenario('no_growth', 'zero_return', 0, 244000, -156000), scenario('assumed', 'assumed_return', 7, 266037, -133963)],
    feasibility: { status: 'shortfall' },
  };
  assert.equal(finance.goalVerdict(behind), 'Projected ₹2.66 L at 7% a year, ₹1.34 L short of the target.');
});

test('goalVerdict never invents a return and keeps the missing-details wording', () => {
  const noAssumption = {
    status: 'behind', currency: 'INR', baseScenario: 'no_growth',
    scenarios: [scenario('no_growth', 'zero_return', 0, 244000, -156000)], feasibility: { status: 'shortfall' },
  };
  assert.equal(finance.goalVerdict(noAssumption), 'Projected ₹2.44 L with no growth, ₹1.56 L short of the target.');
  const coveredNoAssumption = { ...noAssumption, status: 'on_track', targetDate: '2028-10-31', scenarios: [scenario('no_growth', 'zero_return', 0, 410000, 10000)], feasibility: { status: 'covered_without_growth' } };
  assert.equal(finance.goalVerdict(coveredNoAssumption, () => 'Oct 2028'), 'Reaches the target even with no growth, with ₹4.10 L projected by Oct 2028.');
  assert.equal(finance.goalVerdict({ status: 'insufficient_data', warnings: ['Cannot plan this goal without: target date.'] }), 'Cannot plan this goal without: target date.');
  assert.equal(finance.goalVerdict({ status: 'insufficient_data' }), 'Some details this goal needs are missing.');
  assert.equal(finance.goalVerdict({ status: 'reached', currency: 'USD', startingAmount: 12000 }), 'The $12K counted so far already covers the target.');
  assert.equal(finance.goalVerdict(null), '');
});

test('gapOptions shows up to three options on the first basis with the exact figures, and keeps the rest', () => {
  const alt = (kind, basis, extra) => ({ kind, basis, label: basis === 'zero_return' ? 'with no growth' : 'if your 7% a year assumption held', annualReturnPct: basis === 'zero_return' ? 0 : 7, ...extra });
  const plan = {
    status: 'behind', currency: 'INR',
    feasibility: { status: 'shortfall', shortfallZeroReturn: 156000, shortfallAssumed: 133964 },
    alternatives: [
      alt('increase_monthly', 'zero_return', { requiredMonthly: 14783, additionalMonthly: 6783 }),
      alt('extend_deadline', 'zero_return', { extraMonths: 20, newTargetDate: '2030-06-30' }),
      alt('reduce_target', 'zero_return', { reachableTarget: 244000, inTodayMoney: false }),
      alt('earmark_lump_sum', 'zero_return', { lumpSum: 156000 }),
      alt('increase_monthly', 'assumed_return', { requiredMonthly: 13428, additionalMonthly: 5428 }),
    ],
  };
  const gap = finance.gapOptions(plan, () => 'Jun 2030');
  assert.equal(gap.title, 'Ways to close the ₹1,56,000 gap (with no growth)');
  assert.deepEqual(gap.options, [
    { title: 'Save ₹14,783 a month', note: '₹6,783 more than now' },
    { title: 'Move the date to Jun 2030', note: '20 more months' },
    { title: 'Lower the target to ₹2,44,000', note: 'What the plan reaches' },
  ]);
  assert.deepEqual(gap.others.map((item) => item.tag), ['No growth', 'At your 7%']);
  assert.equal(gap.others[0].text, 'Earmark ₹1,56,000 more today');
  // Only a goal that falls short gets the panel; on track (even only if returns hold) does not.
  assert.equal(finance.gapOptions({ ...plan, feasibility: { status: 'depends_on_returns' } }), null);
  assert.equal(finance.gapOptions({ ...plan, alternatives: [] }), null);
  assert.equal(finance.gapOptions(null), null);
  // With no months left only the date and a lump sum remain.
  const late = finance.gapOptions({ currency: 'INR', feasibility: { status: 'no_time_left', shortfallZeroReturn: 5000 }, alternatives: [alt('earmark_lump_sum', 'zero_return', { lumpSum: 5000 })] });
  assert.deepEqual(late.options, [{ title: 'Set aside ₹5,000 more today', note: 'On top of what is saved and linked' }]);
  assert.deepEqual(late.others, []);
});

test('holdingLogoHtml shows a cached logo or a monogram and escapes names', () => {
  const f = require('../../frontend/js/finance.js');
  const logo = f.holdingLogoHtml({ name: 'Infosys', logo: '/api/logos/in:M%26M' });
  assert.match(logo, /<img class="broker-logo holding-logo" src="\/api\/logos\/in:M%26M"/);
  assert.match(logo, /alt=""/);
  assert.match(logo, /data-monogram="I"/);
  const none = f.holdingLogoHtml({ name: 'Infosys' });
  assert.doesNotMatch(none, /<img/);
  assert.match(none, /holding-logo/);
  assert.match(none, />I</);
  assert.doesNotMatch(f.holdingLogoHtml({ name: 'Infosys', logo: 'https://evil.example/x.png' }), /<img/);
  assert.match(f.holdingLogoHtml({ code: 'abc' }), />A</);
  const xss = f.holdingLogoHtml({ name: '<script>', logo: '/api/logos/in:X' });
  assert.doesNotMatch(xss, /<script>/);
});
