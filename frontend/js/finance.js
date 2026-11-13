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

  // One sentence on where a goal stands, built only from the server's plan: the projection under the goal's own
  // assumption (or with no growth when there is none), whether no growth still reaches the target, or the shortfall.
  function goalVerdict(plan, dateLabel = (isoDate) => isoDate) {
    if (!plan) return '';
    const cur = plan.currency;
    switch (plan.status) {
      case 'reached':
        return `The ${amountShort(plan.startingAmount ?? plan.savedAmount, cur)} counted so far already covers the target.`;
      case 'past_due':
      case 'due_now':
        return `${amount(plan.remainingGap, cur)} is still needed, and no monthly contributions are left before ${dateLabel(plan.targetDate)}.`;
      case 'insufficient_data':
        return (plan.warnings || [])[0] || 'Some details this goal needs are missing.';
      default:
        break;
    }
    const base = baseScenario(plan);
    if (!base) return (plan.feasibility && plan.feasibility.summary) || '';
    const assumed = base.basis !== 'zero_return';
    const projected = amountShort(base.projectedAmount, cur);
    const feasibility = plan.feasibility && plan.feasibility.status;
    if (feasibility === 'covered_without_growth') {
      return assumed
        ? `Reaches the target even with no growth. Projected ${projected} at ${base.annualReturnPct}% a year.`
        : `Reaches the target even with no growth, with ${projected} projected by ${dateLabel(plan.targetDate)}.`;
    }
    const where = assumed ? `Projected ${projected} at ${base.annualReturnPct}% a year` : `Projected ${projected} with no growth`;
    if (feasibility === 'depends_on_returns') return `${where}. With no growth it falls short, so it relies on returns.`;
    if (base.onTrack) return `${where}, ${amountShort(base.difference, cur)} more than the target.`;
    return `${where}, ${amountShort(-base.difference, cur)} short of the target.`;
  }

  const GAP_KINDS = ['increase_monthly', 'extend_deadline', 'reduce_target', 'earmark_lump_sum'];

  // A tile for one way to close a gap: the server's exact figure as the title and what it means as the note.
  function gapOption(item, currency = 'INR', dateLabel = (isoDate) => isoDate) {
    if (item.kind === 'increase_monthly') {
      return {
        title: `Save ${amount(item.requiredMonthly, currency)} a month`,
        note: item.additionalMonthly > 0 ? `${amount(item.additionalMonthly, currency)} more than now` : 'What you plan to save now',
      };
    }
    if (item.kind === 'extend_deadline') {
      return { title: `Move the date to ${dateLabel(item.newTargetDate)}`, note: `${item.extraMonths} more month${item.extraMonths === 1 ? '' : 's'}` };
    }
    if (item.kind === 'reduce_target') {
      return {
        title: `Lower the target to ${amount(item.reachableTarget, currency)}`,
        note: item.inTodayMoney ? "What the plan reaches, in today's money" : 'What the plan reaches',
      };
    }
    return { title: `Set aside ${amount(item.lumpSum, currency)} more today`, note: 'On top of what is saved and linked' };
  }

  // The "Ways to close the gap" panel for a goal that falls short (null otherwise): up to three options on the
  // first basis the server lists (no growth when it falls short there), and every other option it gave as a line.
  function gapOptions(plan, dateLabel = (isoDate) => isoDate) {
    const feasibility = plan && plan.feasibility;
    if (!feasibility || !['shortfall', 'no_time_left'].includes(feasibility.status)) return null;
    const items = plan.alternatives || [];
    if (!items.length) return null;
    const { basis, label } = items[0];
    const shown = GAP_KINDS.map((kind) => items.find((item) => item.basis === basis && item.kind === kind)).filter(Boolean).slice(0, 3);
    const gap = Number(basis === 'zero_return' ? feasibility.shortfallZeroReturn : feasibility.shortfallAssumed);
    const what = Number.isFinite(gap) && gap > 0 ? `the ${amount(gap, plan.currency)} gap` : 'the gap';
    return {
      title: `Ways to close ${what}${label ? ` (${label})` : ''}`,
      options: shown.map((item) => gapOption(item, plan.currency, dateLabel)),
      others: items.filter((item) => !shown.includes(item)).map((item) => alternativeLabel(item, plan.currency, dateLabel)),
    };
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

  // The points a history chart draws: with broker history, each point's combined value (a day whose combined
  // value could not be worked out is left out, never drawn as zero); otherwise INDmoney's net worth.
  function historySeries(data) {
    const history = (data && data.history) || [];
    const withBrokers = Boolean(data && data.includesBrokers);
    const points = withBrokers
      ? history.map((point) => ({ ...point, networthInr: point.combinedInr })).filter((point) => Number.isFinite(point.networthInr))
      : history.filter((point) => Number.isFinite(point.networthInr));
    return {
      points,
      includesBrokers: withBrokers,
      partial: withBrokers && history.some((point) => point.combined && point.combined.partial),
      note: withBrokers
        ? 'Includes holdings fetched directly from your brokers; INDmoney rows those brokers replace are left out. Days marked partial are incomplete.'
        : '',
    };
  }

  // "over the last 4 months": the span a history change covers, for the line beside the change.
  function spanPhrase(days) {
    if (!Number.isFinite(days) || days < 1) return '';
    if (days >= 330) return 'over the last year';
    if (days >= 28) {
      const months = Math.round(days / 30);
      return months === 1 ? 'over the last month' : `over the last ${months} months`;
    }
    return days === 1 ? 'over the last day' : `over the last ${days} days`;
  }

  // Round axis ticks (1, 2, 2.5 or 5 times a power of ten) that cover [min, max] in about ``count`` steps.
  // Empty when either end is missing, so an axis never starts from a made-up zero.
  function niceTicks(min, max, count = 4) {
    if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
    let low = Math.min(min, max);
    let high = Math.max(min, max);
    if (low === high) {
      const pad = Math.abs(low) * 0.01 || 1;
      low -= pad;
      high += pad;
    }
    const rough = (high - low) / Math.max(1, count);
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough);
    const first = Math.floor(low / step) * step;
    const last = Math.ceil(high / step) * step;
    const ticks = [];
    for (let index = 0; first + index * step <= last + step / 1e6; index += 1) ticks.push(Number((first + index * step).toPrecision(12)));
    return ticks;
  }

  // How a goal's status reads at a glance: "good" (reached or on track), "conditional" (on track only if the
  // assumed return holds) or "behind" (short, due, past due or missing details).
  function goalTone(plan) {
    const status = plan && plan.status;
    if (status === 'on_track' && plan.feasibility && plan.feasibility.status === 'depends_on_returns') return 'conditional';
    return status === 'on_track' || status === 'reached' ? 'good' : 'behind';
  }

  // Asset types with a value, largest first, each with its share of their total. Rows without a positive value are
  // left out rather than drawn as zero.
  function allocationShares(rows) {
    const shown = (rows || []).filter((row) => row && Number.isFinite(row.current_value_inr) && row.current_value_inr > 0)
      .sort((a, b) => b.current_value_inr - a.current_value_inr);
    const total = shown.reduce((sum, row) => sum + row.current_value_inr, 0);
    return shown.map((row) => ({ assetType: row.asset_type, value: row.current_value_inr, pct: row.current_value_inr / total * 100 }));
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

  // Fund figures from the server (app/fund_data.py): the value as given and, beside it, where it came from and how
  // current it is. Only a figure with a date and status "current" reads as current; anything else says old or undated.
  const FUND_PERCENT_FIELDS = ['expenseRatioPct', 'return1yPct', 'return3yPct', 'return5yPct'];

  function fundMetricValue(field, metric) {
    if (!metric || metric.value == null || metric.value === '') return '—';
    return FUND_PERCENT_FIELDS.includes(field) ? `${metric.value}%` : String(metric.value);
  }

  function fundMetricNote(metric, formatDate) {
    if (!metric) return '';
    const source = String(metric.source || '').startsWith('INDmoney') ? 'INDmoney' : (metric.source || 'source unknown');
    if (metric.asOf && metric.status === 'current') return `as of ${formatDate(metric.asOf)} · ${source}`;
    if (metric.asOf && metric.status === 'stale') return `old: as of ${formatDate(metric.asOf)} · ${source}`;
    const fetched = metric.retrievedAt ? ` · fetched ${formatDate(String(metric.retrievedAt).slice(0, 10))}` : '';
    return `no date given${fetched} · ${source}`;
  }

  // Holdings page tabs from GET /api/holdings/combined: "All" (when there is a combined total), then each source.
  function holdingsTabs(payload) {
    const sources = Array.isArray(payload?.sources) ? payload.sources : [];
    if (!sources.length) return [];
    // INDmoney first, like the Brokers list; the others keep the order they came in.
    const ordered = [...sources.filter((source) => source.id === 'indmoney'), ...sources.filter((source) => source.id !== 'indmoney')];
    const tabs = ordered.map((source) => ({
      id: source.id, label: source.name || source.id, available: Boolean(source.available), stale: Boolean(source.stale), asOf: source.asOf ?? null,
      logo: source.logo || `/brokers/${source.id}.png`,
    }));
    return payload.combined?.available ? [{ id: 'all', label: 'All', available: true, stale: false, asOf: null, logo: null }, ...tabs] : tabs;
  }

  /* ── Holdings table: tiles, filters and sorting ───────────────────── */
  // Two letters for a holding's tile: the first letters of its first two words, else the word's first two.
  function holdingInitials(name) {
    const words = String(name || '').split(/\s+/).filter((word) => /^[\p{L}\p{N}]/u.test(word));
    if (!words.length) return '?';
    const letters = words.length > 1 ? words[0][0] + words[1][0] : [...words[0]].slice(0, 2).join('');
    return letters.toUpperCase();
  }

  // Asset types in the order they first appear (rows come largest first), each with its count.
  function holdingTypeCounts(rows) {
    const counts = new Map();
    for (const row of rows || []) {
      if (row?.asset_type) counts.set(row.asset_type, (counts.get(row.asset_type) || 0) + 1);
    }
    return [...counts].map(([type, count]) => ({ type, count }));
  }

  // Largest first; a missing figure sorts last, never as zero.
  function byFigure(key) {
    return (a, b) => {
      const x = a[key];
      const y = b[key];
      if (Number.isFinite(x) !== Number.isFinite(y)) return Number.isFinite(x) ? -1 : 1;
      return Number.isFinite(x) ? y - x : 0;
    };
  }
  const HOLDING_SORTS = {
    value: byFigure('current_value_inr'),
    pnl: byFigure('pnl_inr'),
    pnlPct: byFigure('pnl_pct'),
    day: byFigure('one_day_change_inr'),
    name: (a, b) => String(a.name || a.code || '').localeCompare(String(b.name || b.code || '')),
  };

  // The rows a holdings table shows: one asset type (or ALL), matching the search in name, code or broker.
  function filterHoldings(rows, { type = 'ALL', query = '', sort = 'value' } = {}) {
    const needle = String(query).trim().toLowerCase();
    return (rows || [])
      .filter((row) => type === 'ALL' || row.asset_type === type)
      .filter((row) => !needle || [row.name, row.code, row.broker].some((value) => String(value || '').toLowerCase().includes(needle)))
      .sort(HOLDING_SORTS[sort] || HOLDING_SORTS.value);
  }

  // The status line for a broker in the profile dialog.
  function brokerStatusLabel(status, holdingsAt, now = Date.now()) {
    const saved = holdingsAt ? ` Showing holdings from ${timeAgo(holdingsAt, now)}.` : '';
    switch (status) {
      case 'connected': return 'Connected';
      case 'pending': return 'Waiting for you to log in. Open the login link, then come back.';
      case 'needs_login': return `Login expired (logins end daily around 6 AM, or after 12 hours).${saved}`;
      case 'unavailable': return `Could not reach the broker right now.${saved}`;
      default: return 'Not connected';
    }
  }

  /* ── Broker logos and the plan's broker limit ─────────────────────── */
  const LIMIT_NOTE = 'Free plan: up to 2 brokers. Disconnect one or see plans.';

  function monogram(name) {
    const letter = String(name || '').trim().charAt(0);
    return letter ? letter.toUpperCase() : '?';
  }

  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  }

  // The broker's own site icon on the left of its name. `alt` is empty because the name is written next to it; if the
  // image fails to load, app.js swaps it for the monogram tile (data-monogram) so no broken-image icon shows.
  function brokerLogoHtml(id, name, logo) {
    const src = typeof logo === 'string' && logo.startsWith('/brokers/') ? logo : `/brokers/${encodeURIComponent(id)}.png`;
    return `<img class="broker-logo" src="${escapeText(src)}" alt="" width="24" height="24" loading="lazy" data-monogram="${escapeText(monogram(name))}">`;
  }

  function monogramHtml(letter) {
    return `<span class="broker-logo broker-monogram" aria-hidden="true">${escapeText(letter)}</span>`;
  }

  // Whether Connect is disabled for a broker: not connected yet, a plan with a limit, and the limit already used.
  function brokerConnectBlocked(broker, limit, connected) {
    return broker?.status === 'disconnected' && Number.isFinite(limit) && Number.isFinite(connected) && connected >= limit;
  }

  /* ── Reconnect reminders ──────────────────────────────────────────── */
  const BROKER_STALE_MS = 24 * 60 * 60 * 1000;
  const BROKER_CHECK_MS = 60 * 60 * 1000;

  // Connected brokers whose login has ended, or whose holdings are over a day old (worked out from `now`, so a
  // page left open keeps counting). Brokers that are not connected, or are still waiting for a login, are skipped.
  function brokersNeedingAttention(brokers, now = Date.now()) {
    return (brokers || []).filter((broker) => broker.status === 'needs_login'
      || (broker.status === 'connected' && broker.auth !== 'indmoney_oauth' && Number.isFinite(broker.holdingsAt) && now - broker.holdingsAt > BROKER_STALE_MS));
  }

  function brokerBannerText(broker, now = Date.now()) {
    const from = Number.isFinite(broker.holdingsAt) ? ` Holdings shown are from ${timeAgo(broker.holdingsAt, now)}.` : ' Reconnect to see your holdings.';
    if (broker.status === 'needs_login') return `Your ${broker.name} login has ended.${from}`;
    return `Your ${broker.name} holdings were last fetched ${timeAgo(broker.holdingsAt, now)}. Refresh them by reconnecting.`;
  }

  // What the Reconnect button does: a login-tool broker asks the server for a link, an OAuth broker goes to its
  // connect route, Kotak (QR) opens the Brokers section of the profile dialog.
  function brokerReconnectAction(broker) {
    if (broker.auth === 'indmoney_oauth') return { type: 'link', href: '/auth/indmoney/connect' };
    if (broker.auth === 'oauth') return { type: 'link', href: `/auth/brokers/${encodeURIComponent(broker.broker)}/connect` };
    if (broker.auth === 'mcp_login_qr') return { type: 'dialog' };
    return { type: 'login-link' };
  }

  function localDay(now = Date.now()) {
    const date = new Date(now);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  // Dismissal lasts for the local day. `storage` may be missing or throw (private browsing): then nothing is remembered.
  function bannerDismissed(storage, brokerId, now = Date.now()) {
    try {
      return storage.getItem(`ca-broker-banner:${brokerId}`) === localDay(now);
    } catch {
      return false;
    }
  }

  function dismissBanner(storage, brokerId, now = Date.now()) {
    try {
      storage.setItem(`ca-broker-banner:${brokerId}`, localDay(now));
    } catch { /* not remembered */ }
  }

  // Brokers whose status is worth asking the broker for: connected, and not asked in the last hour.
  function brokersToCheck(brokers, checkedAt, now = Date.now()) {
    return (brokers || []).filter((broker) => broker.status === 'connected' && broker.auth !== 'indmoney_oauth'
      && !(Number.isFinite((checkedAt || {})[broker.broker]) && now - checkedAt[broker.broker] < BROKER_CHECK_MS))
      .map((broker) => broker.broker);
  }

  /* ── Mutual fund returns (figures come from /api/returns; nothing is computed here) ── */
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const RETURN_REASONS = {
    no_flows: 'No purchases or sales to measure.',
    no_sign_change: 'Returns cannot be worked out from these cash flows.',
    no_convergence: 'XIRR could not be worked out from these cash flows.',
    no_schemes: 'No fund could be included.',
  };
  const RETURN_STATUSES = {
    nav_stale: 'Old NAV', no_nav: 'No NAV', incomplete_history: 'History missing', unreconciled: 'Units don’t add up', unsupported: 'Not supported',
  };

  function fixedPct(value) {
    return `${value < 0 ? '−' : ''}${Math.abs(value).toFixed(2)}%`;
  }

  function shortDate(isoDate) {
    const [year, month, day] = String(isoDate || '').split('-').map(Number);
    return year && month && day ? `${day} ${MONTHS[month - 1]} ${year}` : '';
  }

  // The headline return of a fund or the portfolio: XIRR, the plain return under a year, or why there is none.
  // `text` is the whole phrase; `short` and `label` split it for a stat tile.
  function returnFigure(item) {
    if (Number.isFinite(item?.xirr)) {
      const short = fixedPct(item.xirr);
      return { text: `${short} a year (XIRR)`, short, label: 'a year (XIRR)', note: '' };
    }
    if (Number.isFinite(item?.absoluteReturnPct) && item.returnReason === 'under_one_year') {
      const short = fixedPct(item.absoluteReturnPct);
      return { text: `${short} (held under a year)`, short, label: 'held under a year', note: 'Not annualised: held for less than a year.' };
    }
    return { text: 'Not available', short: 'Not available', label: '', note: RETURN_REASONS[item?.returnReason] || item?.reason || '' };
  }

  function returnStatus(status) {
    if (status === 'ok') return { label: '', tone: '' };
    return { label: RETURN_STATUSES[status] || String(status || 'Unknown'), tone: 'is-warning' };
  }

  function navNote(item) {
    const date = shortDate(item?.navDate);
    if (!date) return 'No NAV available';
    return `${item.navSource === 'amfi' ? 'AMFI' : 'Statement'} NAV of ${date}`;
  }

  // A fund's or the portfolio's benchmark comparison (GET /api/returns `benchmark`): the benchmark's return on the
  // same cash flows, or why there is none.
  function benchmarkFigure(b) {
    if (b?.status === 'ok' && Number.isFinite(b.xirr)) return { short: fixedPct(b.xirr), label: `${b.label} (XIRR)`, note: '' };
    if (b?.status === 'ok' && Number.isFinite(b.absoluteReturnPct) && b.returnReason === 'under_one_year') {
      return { short: fixedPct(b.absoluteReturnPct), label: `${b.label}, held under a year`, note: '' };
    }
    return { short: 'Not compared', label: '', note: b?.reason || '' };
  }

  // The fund's value against what the same flows would be worth in the benchmark; never advice.
  function benchmarkDifference(diff, label) {
    if (!Number.isFinite(diff)) return '';
    if (diff === 0) return `Same as in ${label}`;
    return `${money(Math.abs(diff))} ${diff > 0 ? 'more' : 'less'} than in ${label}`;
  }

  function benchmarkNavNote(b) {
    const date = shortDate(b?.navDate);
    if (!date) return 'No benchmark NAV yet';
    return `${b.label}${/fund$/i.test(b.label || '') ? '' : ' index fund'} NAV of ${date}`;
  }

  // Mutual fund tax (GET /api/tax). The figures come from the server; these only word them.
  const TAX_SLAB_NOTE = 'Depends on your tax slab. Add it in your profile to include it in the estimate.';
  const TAX_CLASSES = {
    equity: 'Equity-oriented', debt: 'Debt (specified fund)', other: 'Other (gold, international, FoFs)', unclassified: 'Not classified',
  };

  function fyLabel(fy) {
    return fy ? `FY ${fy}` : '';
  }

  function taxClassLabel(taxClass) {
    return TAX_CLASSES[taxClass] || 'Not classified';
  }

  function termLabel(term) {
    return term === 'short' ? 'Short-term' : term === 'long' ? 'Long-term' : 'No rule';
  }

  function taxRate(rate) {
    if (rate === 'slab') return 'Your slab';
    return Number.isFinite(rate) ? `${rate}%` : '—';
  }

  // The equity long-term exemption: how much of the limit this statement's gains used.
  function exemptionShare(exemption) {
    if (!exemption || !Number.isFinite(exemption.limit) || !exemption.limit) return { usedPct: null, used: null, left: null, text: '' };
    const usedPct = Math.min(100, Math.max(0, Math.round((exemption.used / exemption.limit) * 100)));
    return {
      usedPct, used: exemption.used, left: exemption.left,
      text: `${money(exemption.used)} of ${money(exemption.limit)} used; ${money(exemption.left)} left`,
    };
  }

  // The estimated tax for a stat tile: never ₹0 when the slab-taxed part is unknown.
  function taxEstimate(tax) {
    if (!tax) return { short: 'Not estimated', note: '' };
    if (tax.status === 'estimated' && Number.isFinite(tax.total)) return { short: money(tax.total), note: 'Estimate incl. 4% cess' };
    if (tax.status === 'partial' && Number.isFinite(tax.total)) return { short: `${money(tax.total)} + slab tax`, note: TAX_SLAB_NOTE };
    return { short: 'Not estimated', note: TAX_SLAB_NOTE };
  }

  const api = {
    inr, money, moneyShort, signedMoney, signedShort, percent, timeAgo,
    sipValue, grow, monthlyNeeded, goalKind, goalIcon,
    GOAL_CURRENCIES, amount, amountShort, totalsByCurrency, formatTotals, goalStatus, goalIsOnTrack, headlineTarget, isDraft, allocationRemaining, suitabilityGroups, alternativeLabel, baseScenario, goalVerdict, gapOption, gapOptions,
    spanLabel, spanPhrase, niceTicks, goalTone, allocationShares, historyChange, historySeries, portfolioInsights, fundMetricValue, fundMetricNote, holdingsTabs, holdingInitials, holdingTypeCounts, filterHoldings, brokerStatusLabel, monogram, brokerLogoHtml, monogramHtml, brokerConnectBlocked, LIMIT_NOTE,
    brokersNeedingAttention, brokerBannerText, brokerReconnectAction, bannerDismissed, dismissBanner, brokersToCheck, localDay,
    TAX_RULES, slabTax, regimeTax, incomeTax,
    returnFigure, returnStatus, navNote, shortDate, benchmarkFigure, benchmarkDifference, benchmarkNavNote,
    TAX_SLAB_NOTE, fyLabel, taxClassLabel, termLabel, taxRate, exemptionShare, taxEstimate,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
