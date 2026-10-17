/* ── Finance math and formatting ────────────────────────────────────────
   Pure functions shared by the pages and the right rail. In the browser
   they become globals; under Node they are exported for tests/js. */
(function (root) {
  'use strict';

  /* ── Formatting ───────────────────────────────────────────────────── */
  const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 });

  function money(value) {
    return Number.isFinite(value) ? inr.format(value) : '—';
  }

  // ₹12.40 L / ₹1.20 Cr for headline figures; exact rupees everywhere else.
  function moneyShort(value) {
    if (!Number.isFinite(value)) return '—';
    const abs = Math.abs(value);
    const sign = value < 0 ? '−' : '';
    if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`;
    if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)} L`;
    return sign + inr.format(abs);
  }

  function signedMoney(value) {
    if (!Number.isFinite(value)) return '—';
    return (value > 0 ? '+' : value < 0 ? '−' : '') + inr.format(Math.abs(value));
  }

  function signedShort(value) {
    if (!Number.isFinite(value)) return '—';
    return (value > 0 ? '+' : '') + moneyShort(value);
  }

  function percent(value, { signed = false } = {}) {
    if (!Number.isFinite(value)) return '—';
    const sign = signed && value > 0 ? '+' : value < 0 ? '−' : '';
    return `${sign}${Math.abs(value).toFixed(Math.abs(value) < 10 ? 2 : 1)}%`;
  }

  // "just now", "5m ago", "3h ago", "2d ago" for timestamps in milliseconds.
  function timeAgo(timestamp, now = Date.now()) {
    const minutes = Math.floor((now - Number(timestamp)) / 60000);
    if (!Number.isFinite(minutes)) return '';
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h ago`;
    return `${Math.floor(minutes / (60 * 24))}d ago`;
  }

  /* ── Growth ───────────────────────────────────────────────────────── */
  // Monthly compounding at a yearly rate; SIP instalments are invested at the start of each month.
  function sipValue(monthly, yearlyPct, months) {
    const rate = yearlyPct / 100 / 12;
    if (!rate) return monthly * months;
    return monthly * (((1 + rate) ** months - 1) / rate) * (1 + rate);
  }

  function grow(amount, yearlyPct, months) {
    return amount * (1 + yearlyPct / 100 / 12) ** months;
  }

  // The monthly SIP that, with ``saved`` already invested, reaches ``target`` in ``months``.
  function monthlyNeeded(target, saved, yearlyPct, months) {
    const gap = target - grow(saved, yearlyPct, months);
    if (gap <= 0) return 0;
    return months > 0 ? gap / sipValue(1, yearlyPct, months) : gap;
  }

  /* ── Goals ────────────────────────────────────────────────────────── */
  // Goal plans (gap, scenarios, status) are calculated on the server (app/goal_planning.py);
  // these helpers only format them. Amounts stay in each goal's own currency.
  const GOAL_CURRENCIES = ['INR', 'USD', 'EUR', 'GBP'];
  const currencyFormats = new Map();

  function currencyFormat(currency, compact) {
    const key = `${currency}:${compact ? 'short' : 'exact'}`;
    if (!currencyFormats.has(key)) {
      currencyFormats.set(key, new Intl.NumberFormat('en-US', {
        // Both digit limits are explicit: runtimes default minimumFractionDigits differently for currencies.
        style: 'currency', currency, minimumFractionDigits: 0, maximumFractionDigits: compact ? 1 : 0,
        ...(compact ? { notation: 'compact' } : {}),
      }));
    }
    return currencyFormats.get(key);
  }

  // An exact amount in ``currency``; rupees keep the Indian grouping.
  function amount(value, currency = 'INR') {
    if (!Number.isFinite(value)) return '—';
    return currency === 'INR' ? money(value) : currencyFormat(currency, false).format(value);
  }

  // A headline amount: ₹12.40 L for rupees, $45.2K for other currencies.
  function amountShort(value, currency = 'INR') {
    if (!Number.isFinite(value)) return '—';
    return currency === 'INR' ? moneyShort(value) : currencyFormat(currency, true).format(value);
  }

  // Sums of ``field`` per currency (rupees first), since amounts in different currencies are never added.
  function totalsByCurrency(goals, field) {
    const totals = new Map();
    for (const goal of goals || []) {
      // A draft can lack the amount or the currency; Number(null) would count it as 0.
      const raw = goal && goal[field];
      const value = Number(raw);
      if (!goal || !goal.currency || raw === null || raw === undefined || !Number.isFinite(value)) continue;
      totals.set(goal.currency, (totals.get(goal.currency) || 0) + value);
    }
    return [...totals.entries()].sort(([a], [b]) => (a === 'INR' ? -1 : b === 'INR' ? 1 : a.localeCompare(b)));
  }

  function formatTotals(totals, short = true) {
    if (!totals.length) return '—';
    return totals.map(([currency, value]) => (short ? amountShort : amount)(value, currency)).join(' + ');
  }

  const GOAL_STATUSES = {
    reached: ['Reached', 'is-connected'],
    on_track: ['On track', 'is-connected'],
    behind: ['Needs more', 'is-behind'],
    due_now: ['Due now', 'is-behind'],
    past_due: ['Past due', 'is-behind'],
    insufficient_data: ['Missing details', 'is-behind'],
  };

  // [label, pill class] for a plan status; unknown statuses show as missing details. "On track" that holds only under
  // the assumed return (it falls short with no growth) says so, so an assumption never reads as a certainty.
  function goalStatus(plan) {
    if (plan && plan.status === 'on_track' && plan.feasibility && plan.feasibility.status === 'depends_on_returns') {
      return ['On track if returns hold', 'is-behind'];
    }
    return GOAL_STATUSES[plan && plan.status] || GOAL_STATUSES.insufficient_data;
  }

  // A goal saved with only a name (status "draft"); old goals have no status and are active.
  // The amount a goal is measured against: the plan's inflation-adjusted target when it raised one, else the target
  // as entered. todayMoney is the entered target when the two differ (null otherwise).
  function headlineTarget(goal) {
    const plan = (goal && goal.plan) || {};
    const entered = goal && goal.targetAmount != null ? Number(goal.targetAmount) : null;
    if (plan.inflation && Number.isFinite(Number(plan.effectiveTarget))) {
      return { amount: Number(plan.effectiveTarget), todayMoney: entered, inflationPct: plan.inflation.inflationPct };
    }
    return { amount: entered, todayMoney: null, inflationPct: null };
  }

  function isDraft(goal) {
    return Boolean(goal) && goal.status === 'draft';
  }

  // Percent of a holding still unassigned after the goal shares and general wealth (negative when over 100).
  function allocationRemaining(goalPcts, generalPct) {
    const numbers = [...(goalPcts || []), generalPct].map((value) => Number(value));
    return 100 - numbers.reduce((sum, value) => sum + (Number.isFinite(value) && value > 0 ? value : 0), 0);
  }

  // Category labels grouped by the server's verdict, in the order shown to the user.
  function suitabilityGroups(suitability) {
    const groups = { allowed: [], caution: [], unsuitable: [] };
    for (const item of (suitability && suitability.categories) || []) {
      if (groups[item.verdict]) groups[item.verdict].push(item.label);
    }
    return groups;
  }

  // A tag and a sentence for one way to close a gap, formatting the server's numbers (nothing is calculated here).
  function alternativeLabel(item, currency = 'INR', dateLabel = (isoDate) => isoDate) {
    const tag = item.basis === 'zero_return' ? 'No growth' : `At your ${item.annualReturnPct}%`;
    let text = '';
    if (item.kind === 'increase_monthly') {
      text = `Raise the monthly contribution to ${amount(item.requiredMonthly, currency)}`;
    } else if (item.kind === 'extend_deadline') {
      text = `Move the date by ${item.extraMonths} months, to ${dateLabel(item.newTargetDate)}`;
    } else if (item.kind === 'reduce_target') {
      text = `Lower the target to ${amount(item.reachableTarget, currency)}${item.inTodayMoney ? " in today's money" : ''}`;
    } else if (item.kind === 'earmark_lump_sum') {
      text = `Earmark ${amount(item.lumpSum, currency)} more today`;
    }
    return { tag, text };
  }

  function goalIsOnTrack(goal) {
    const status = goal && goal.plan && goal.plan.status;
    return status === 'on_track' || status === 'reached';
  }

  // The scenario the goal is judged by: the user's assumption, else no growth.
  function baseScenario(plan) {
    const scenarios = (plan && plan.scenarios) || [];
    return scenarios.find((item) => item.name === plan.baseScenario) || scenarios[0] || null;
  }

  const GOAL_KINDS = [
    ['home', /\b(home|house|flat|apartment|property|down ?payment)\b/i],
    ['education', /\b(education|college|school|university|study|studies|child|kid|son|daughter)\b/i],
    ['financial_independence', /\b(financial(ly)? independen\w*|fire|early retire\w*)\b/i],
    ['retirement', /\b(retire|retirement|pension)\b/i],
    ['car', /\b(car|bike|vehicle|scooter)\b/i],
    ['travel', /\b(travel|trip|vacation|holiday|tour)\b/i],
    ['wedding', /\b(wedding|marriage)\b/i],
    ['emergency', /\b(emergency|safety|rainy)\b/i],
  ];

  // Picks an icon for a goal from its name; ``other`` when nothing matches.
  function goalKind(name) {
    const match = GOAL_KINDS.find(([, pattern]) => pattern.test(String(name || '')));
    return match ? match[0] : 'other';
  }

  // The kind the user chose, or one inferred from the name for goals saved without it.
  function goalIcon(goal) {
    const chosen = goal && goal.kind;
    if (chosen === 'other' || GOAL_KINDS.some(([kind]) => kind === chosen)) return chosen;
    return goalKind(goal && goal.name);
  }

  /* ── Net worth history ────────────────────────────────────────────── */
  function daysBetween(fromIso, toIso) {
    return Math.round((Date.parse(`${toIso}T00:00:00Z`) - Date.parse(`${fromIso}T00:00:00Z`)) / 86400000);
  }

  // A short label for how much history a change covers: 7D, 4M, 1Y.
  function spanLabel(days) {
    if (days >= 330) return '1Y';
    if (days >= 28) return `${Math.round(days / 30)}M`;
    return `${days}D`;
  }

  // Change from the first to the last point with a net worth; null with fewer than two points.
  function historyChange(points) {
    const valid = (points || []).filter((point) => Number.isFinite(point.networthInr) && point.date);
    if (valid.length < 2) return null;
    const first = valid[0];
    const last = valid[valid.length - 1];
    const days = daysBetween(first.date, last.date);
    if (days < 1) return null;
    const change = last.networthInr - first.networthInr;
    return {
      change,
      pct: first.networthInr ? change / Math.abs(first.networthInr) * 100 : NaN,
      days,
      label: spanLabel(days),
      from: first.date,
    };
  }

  /* ── Insights from the latest snapshot ────────────────────────────── */
  function holdingLabel(item) {
    return String(item.name || item.code || 'A holding');
  }

  function dayPct(item) {
    const base = (item.current_value_inr || 0) - item.one_day_change_inr;
    return base > 0 ? item.one_day_change_inr / base * 100 : NaN;
  }

  // Up to ``limit`` plain-language observations about the user's own holdings.
  // ``portfolio`` is the /api/portfolio response; nothing here is market news.
  function portfolioInsights(portfolio, limit = 4) {
    if (!portfolio || !portfolio.available) return [];
    const holdings = portfolio.holdings || [];
    const figures = portfolio.figures || {};
    const insights = [];
    const moved = holdings.filter((item) => Number.isFinite(item.one_day_change_inr) && item.one_day_change_inr !== 0);
    if (moved.length) {
      const change = moved.reduce((sum, item) => sum + item.one_day_change_inr, 0);
      const base = holdings.reduce((sum, item) => sum + (item.current_value_inr || 0), 0) - change;
      const up = moved.filter((item) => item.one_day_change_inr > 0).length;
      insights.push({
        kind: 'day',
        tone: change >= 0 ? 'up' : 'down',
        title: `Your holdings are ${change >= 0 ? 'up' : 'down'} ${money(Math.abs(change))} on the day`,
        detail: `${percent(base > 0 ? change / base * 100 : NaN, { signed: true })} · ${up} up, ${moved.length - up} down`,
        prompt: 'Which of my holdings moved the most on the last trading day, and why might that be?',
      });
      const gainer = moved.filter((item) => item.one_day_change_inr > 0).sort((a, b) => dayPct(b) - dayPct(a))[0];
      if (gainer) {
        insights.push({
          kind: 'gainer',
          tone: 'up',
          title: `${holdingLabel(gainer)} up ${percent(dayPct(gainer))}`,
          detail: `${signedMoney(gainer.one_day_change_inr)} on your holding`,
          prompt: `Tell me about my holding ${holdingLabel(gainer)}: its recent move, value and share of my portfolio.`,
        });
      }
      const loser = moved.filter((item) => item.one_day_change_inr < 0).sort((a, b) => dayPct(a) - dayPct(b))[0];
      if (loser) {
        insights.push({
          kind: 'loser',
          tone: 'down',
          title: `${holdingLabel(loser)} down ${percent(Math.abs(dayPct(loser)))}`,
          detail: `${signedMoney(loser.one_day_change_inr)} on your holding`,
          prompt: `Tell me about my holding ${holdingLabel(loser)}: its recent move, value and share of my portfolio.`,
        });
      }
    }
    const concentration = figures.concentration || {};
    if (concentration.largest_holding && Number.isFinite(concentration.largest_holding_pct)) {
      insights.push({
        kind: 'concentration',
        tone: concentration.largest_holding_pct >= 25 ? 'warn' : 'neutral',
        title: `${concentration.largest_holding} is ${percent(concentration.largest_holding_pct)} of your portfolio`,
        detail: Number.isFinite(concentration.top_5_pct) ? `Your top 5 holdings make up ${percent(concentration.top_5_pct)}` : 'Your largest single holding',
        prompt: 'Is my portfolio too concentrated? Look at my largest holdings.',
      });
    }
    const us = (figures.by_asset_type || []).find((row) => row.asset_type === 'US_STOCK' && row.current_value_inr > 0);
    if (us) {
      const usd = figures.usd && figures.usd.US_STOCK && figures.usd.US_STOCK.current_value_usd;
      insights.push({
        kind: 'global',
        tone: 'neutral',
        title: `US stocks are ${percent(us.share_of_portfolio_pct)} of your portfolio`,
        detail: Number.isFinite(usd) ? `$${new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(usd)} (${moneyShort(us.current_value_inr)})` : moneyShort(us.current_value_inr),
        prompt: 'How are my US stocks doing? Show their value in USD and INR.',
      });
    }
    return insights.slice(0, limit);
  }

  /* ── Income tax (resident individuals below 60) ───────────────────── */
  // Slabs for FY 2026-27 (AY 2027-28). Budget 2026 kept the FY 2025-26 rates.
  // Update this table, its label, and tests/js/finance.test.js after each budget.
  const TAX_RULES = {
    label: 'FY 2026-27',
    cessPct: 4,
    regimes: {
      new: {
        name: 'New regime',
        standardDeduction: 75000,
        allowsDeductions: false,
        slabs: [[400000, 0], [800000, 5], [1200000, 10], [1600000, 15], [2000000, 20], [2400000, 25], [Infinity, 30]],
        rebateLimit: 1200000,
        rebateMax: 60000,
        rebateMarginalRelief: true,
        surcharge: [[5000000, 10], [10000000, 15], [20000000, 25]],
      },
      old: {
        name: 'Old regime',
        standardDeduction: 50000,
        allowsDeductions: true,
        slabs: [[250000, 0], [500000, 5], [1000000, 20], [Infinity, 30]],
        rebateLimit: 500000,
        rebateMax: 12500,
        rebateMarginalRelief: false,
        surcharge: [[5000000, 10], [10000000, 15], [20000000, 25], [50000000, 37]],
      },
    },
  };

  function slabTax(income, slabs) {
    let tax = 0;
    let lower = 0;
    for (const [upper, ratePct] of slabs) {
      if (income <= lower) break;
      tax += (Math.min(income, upper) - lower) * ratePct / 100;
      lower = upper;
    }
    return tax;
  }

  // Tax plus surcharge, with marginal relief: crossing a surcharge threshold never
  // costs more than the income above it.
  function taxWithSurcharge(income, rules) {
    const base = slabTax(income, rules.slabs);
    const bands = rules.surcharge.filter(([threshold]) => income > threshold);
    if (!bands.length) return { tax: base, surcharge: 0 };
    const [threshold, ratePct] = bands[bands.length - 1];
    const previousPct = bands.length > 1 ? bands[bands.length - 2][1] : 0;
    const full = base * (1 + ratePct / 100);
    const capped = slabTax(threshold, rules.slabs) * (1 + previousPct / 100) + (income - threshold);
    const total = Math.min(full, capped);
    return { tax: base, surcharge: total - base };
  }

  function regimeTax(regime, { salary = 0, other = 0, deductions = 0 }) {
    const rules = TAX_RULES.regimes[regime];
    const standard = Math.min(Math.max(salary, 0), rules.standardDeduction);
    const allowed = rules.allowsDeductions ? Math.max(deductions, 0) : 0;
    const gross = Math.max(salary, 0) + Math.max(other, 0);
    const taxable = Math.max(0, gross - standard - allowed);
    const { tax, surcharge } = taxWithSurcharge(taxable, rules);
    let rebate = 0;
    if (taxable <= rules.rebateLimit) rebate = Math.min(tax, rules.rebateMax);
    else if (rules.rebateMarginalRelief) rebate = Math.max(0, tax - (taxable - rules.rebateLimit));
    const beforeCess = Math.max(0, tax - rebate) + surcharge;
    const cess = beforeCess * TAX_RULES.cessPct / 100;
    return {
      regime,
      name: rules.name,
      gross,
      standardDeduction: standard,
      deductions: allowed,
      taxable,
      slabTax: Math.round(tax),
      rebate: Math.round(rebate),
      surcharge: Math.round(surcharge),
      cess: Math.round(cess),
      total: Math.round(beforeCess + cess),
    };
  }

  // Both regimes for one income, and which costs less.
  function incomeTax(inputs) {
    const newRegime = regimeTax('new', inputs);
    const oldRegime = regimeTax('old', inputs);
    return {
      new: newRegime,
      old: oldRegime,
      better: newRegime.total <= oldRegime.total ? 'new' : 'old',
      saving: Math.abs(newRegime.total - oldRegime.total),
    };
  }

  const api = {
    inr, money, moneyShort, signedMoney, signedShort, percent, timeAgo,
    sipValue, grow, monthlyNeeded, goalKind, goalIcon,
    GOAL_CURRENCIES, amount, amountShort, totalsByCurrency, formatTotals, goalStatus, goalIsOnTrack, headlineTarget, isDraft, allocationRemaining, suitabilityGroups, alternativeLabel, baseScenario,
    spanLabel, historyChange, portfolioInsights,
    TAX_RULES, slabTax, regimeTax, incomeTax,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
