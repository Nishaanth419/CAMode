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
  // Months from ``now`` to the end of the goal's target year.
  function monthsUntil(targetYear, now = new Date()) {
    return Math.max(0, (targetYear - now.getFullYear()) * 12 + (11 - now.getMonth()));
  }

  function goalProjection(goal, now = new Date()) {
    const months = monthsUntil(goal.targetYear, now);
    const rate = goal.expectedReturnPct ?? 10;
    const saved = goal.savedInr || 0;
    const projected = grow(saved, rate, months) + sipValue(goal.monthlyInr || 0, rate, months);
    return {
      months,
      projected,
      onTrack: projected >= goal.targetInr,
      needed: monthlyNeeded(goal.targetInr, saved, rate, months),
      progress: goal.targetInr > 0 ? Math.min(100, saved / goal.targetInr * 100) : 0,
    };
  }

  const GOAL_KINDS = [
    ['home', /\b(home|house|flat|apartment|property|down ?payment)\b/i],
    ['education', /\b(education|college|school|university|study|studies|child|kid|son|daughter)\b/i],
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
    sipValue, grow, monthlyNeeded, monthsUntil, goalProjection, goalKind, goalIcon,
    spanLabel, historyChange, portfolioInsights,
    TAX_RULES, slabTax, regimeTax, incomeTax,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
