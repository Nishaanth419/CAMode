/* ── Right rail ─────────────────────────────────────────────────────────
   Portfolio summary, goals, and insights from the latest snapshot. Loaded
   after finance.js, app.js and pages.js, and reuses their data loaders,
   so the rail and the pages share one request per resource. */

const railPortfolio = document.getElementById('rail-portfolio');
const railGoals = document.getElementById('rail-goals');
const railInsights = document.getElementById('rail-insights');
const RAIL_GOALS = 3;

const GOAL_ICONS = {
  home: '<path d="M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1Z"/>',
  education: '<path d="m3 9 9-4 9 4-9 4Z"/><path d="M7 11v4.5c0 1.4 2.2 2.5 5 2.5s5-1.1 5-2.5V11"/><path d="M21 9v5"/>',
  retirement: '<path d="M3.5 12a8.5 8.5 0 0 1 17 0Z"/><path d="M12 12v6.5a1.5 1.5 0 0 1-3 0"/><path d="M12 3.5V2"/>',
  car: '<path d="M5 16v-4l2-5h10l2 5v4Z"/><path d="M5 12h14"/><circle cx="8" cy="16.5" r="1.5"/><circle cx="16" cy="16.5" r="1.5"/>',
  travel: '<path d="M2.5 13.5 21 6l-4.5 13-4-5.5Z"/><path d="m12.5 13.5 3-3"/>',
  wedding: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z"/>',
  emergency: '<path d="M12 3.5 19 6v5.5c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6Z"/>',
  other: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/>',
};
const GOAL_TONES = ['green', 'blue', 'violet', 'amber', 'red'];

const INSIGHT_ICONS = {
  day: ['<path d="M5 19v-6M10 19V9M15 19v-5M20 19V6"/>', 'blue'],
  gainer: ['<path d="m4 16 6-6 4 4 6-7"/><path d="M15 7h5v5"/>', 'green'],
  loser: ['<path d="m4 8 6 6 4-4 6 7"/><path d="M15 17h5v-5"/>', 'red'],
  concentration: ['<circle cx="12" cy="12" r="8"/><path d="M12 4v8l6 5"/>', 'amber'],
  global: ['<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 3.5 5.5 3.5 8.5s-1 6-3.5 8.5c-2.5-2.5-3.5-5.5-3.5-8.5s1-6 3.5-8.5Z"/>', 'violet'],
};

function railMessage(text, action = '') {
  return `<p class="rail-copy">${escHtml(text)}</p>${action}`;
}

function railSkeleton(lines = 3) {
  return `<div class="rail-skeleton" aria-hidden="true">${'<span></span>'.repeat(lines)}</div>`;
}

function railError(error) {
  return `<p class="rail-copy rail-error" role="alert">${escHtml(error.message || 'Could not load this.')}</p>`;
}

function sparkline(points, tone) {
  const values = thin(points).map((point) => point.networthInr);
  if (values.length < 2) return '';
  const min = Math.min(...values);
  const range = Math.max(...values) - min || 1;
  const coords = values.map((value, index) => [
    (index / (values.length - 1)) * 240,
    64 - ((value - min) / range) * 56,
  ].map((n) => n.toFixed(1)).join(' '));
  return `<svg class="rail-spark ${tone}" viewBox="0 0 240 70" preserveAspectRatio="none" role="img" aria-label="Net worth trend">
    <defs><linearGradient id="rail-spark-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="currentColor" stop-opacity=".28"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="M${coords.join(' L')} V70 H0 Z" fill="url(#rail-spark-fill)"/>
    <path d="M${coords.join(' L')}" class="spark-line"/>
  </svg>`;
}

/* ── Portfolio card ─────────────────────────────────────────────────── */
async function renderRailPortfolio(isCurrent) {
  const access = portfolioAccess();
  if (access === 'disconnected') {
    railPortfolio.innerHTML = railMessage('Connect INDmoney to track your net worth, returns and how they change every day. Access is read-only.',
      '<button class="rail-button" type="button" data-connect>Connect INDmoney</button>');
    return;
  }
  if (access === 'private') {
    railPortfolio.innerHTML = railMessage('Private mode saves no daily snapshots, so there is no trend to show. Portfolio Analyzer can still read INDmoney live.',
      '<button class="rail-button" type="button" data-agent="portfolio" data-prompt="Give me a snapshot of my current net worth.">Ask about my net worth</button>');
    return;
  }
  railPortfolio.innerHTML = railSkeleton(4);
  let portfolio;
  let history;
  try {
    [portfolio, history] = await Promise.all([getPortfolio(), getHistory(365).catch(() => null)]);
  } catch (error) {
    if (isCurrent()) railPortfolio.innerHTML = railError(error);
    return;
  }
  if (!isCurrent()) return;
  if (!portfolio.available) {
    railPortfolio.innerHTML = railMessage('Your first daily snapshot is on its way. You can also save one now.',
      '<button class="rail-button" type="button" data-refresh-snapshot>Save a snapshot now</button>');
    return;
  }
  const totals = portfolio.figures.totals;
  const pnl = portfolio.figures.pnl;
  const netWorth = totals.net_worth_inr ?? portfolio.networthInr;
  const points = (history?.history || []).filter((point) => Number.isFinite(point.networthInr));
  const change = historyChange(points);
  const tone = change && change.change < 0 ? 'is-down' : 'is-up';
  const changeHtml = change
    ? `<span class="rail-change ${tone}" title="Change since ${escHtml(formatDate(change.from))}">${change.change < 0 ? '↓' : '↑'} ${percent(change.pct, { signed: true })} <small>(${change.label})</small></span>`
    : '';
  const basis = `Over ${pnl.holdings_counted} holdings with a reported cost${pnl.holdings_excluded ? `; ${pnl.holdings_excluded} without one (usually linked brokers) are left out` : ''}.`;
  const schedule = history?.schedule ? ` · refreshes daily at ${escHtml(history.schedule.split(' ')[0])}` : '';
  railPortfolio.innerHTML = `
    <div class="rail-figure-row">
      <strong class="rail-figure" title="Net worth">${money(netWorth)}</strong>
      ${changeHtml}
    </div>
    ${sparkline(points, tone)}
    <dl class="rail-metrics" title="${escHtml(basis)}">
      <div><dt>Invested</dt><dd>${moneyShort(pnl.invested_inr)}</dd></div>
      <div><dt>Returns</dt><dd class="${toneOf(pnl.pnl_inr)}">${signedShort(pnl.pnl_inr)}</dd></div>
      <div><dt>Return</dt><dd class="${toneOf(pnl.pnl_pct)}">${percent(pnl.pnl_pct, { signed: true })}</dd></div>
    </dl>
    <p class="rail-foot">Updated ${escHtml(timeAgo(portfolio.capturedAt))}${schedule} · <button class="link-button" type="button" data-refresh-snapshot>Refresh</button></p>`;
}

/* ── Goals card ─────────────────────────────────────────────────────── */
async function renderRailGoals(isCurrent) {
  railGoals.innerHTML = railSkeleton(3);
  let goals;
  try {
    goals = await getGoals();
  } catch (error) {
    if (isCurrent()) railGoals.innerHTML = railError(error);
    return;
  }
  if (!isCurrent()) return;
  if (!goals.length) {
    railGoals.innerHTML = railMessage('Add what you are saving for, like a home or your child\'s education, to track your progress here.',
      '<button class="rail-button" type="button" data-add-goal>Add a goal</button>');
    return;
  }
  railGoals.innerHTML = `<ul class="rail-list">${goals.slice(0, RAIL_GOALS).map((goal, index) => {
    const projection = goalProjection(goal);
    const tone = GOAL_TONES[index % GOAL_TONES.length];
    const status = projection.onTrack ? 'On track' : 'Needs more';
    return `<li><a class="rail-row tone-${tone}" href="#/goals" title="${escHtml(`${goal.name}: ${status}`)}">
      <span class="insight-icon tone-${tone}" aria-hidden="true"><svg viewBox="0 0 24 24">${GOAL_ICONS[goalKind(goal.name)]}</svg></span>
      <span class="rail-row-body">
        <span class="rail-row-head"><strong>${escHtml(goal.name)}</strong><span class="rail-pct">${Math.floor(projection.progress)}%</span></span>
        <small>Target: ${money(goal.targetInr)} · ${goal.targetYear}</small>
        <span class="progress rail-progress" role="progressbar" aria-label="${escHtml(goal.name)} saved" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.floor(projection.progress)}"><span style="width:${projection.progress.toFixed(1)}%"></span></span>
      </span>
    </a></li>`;
  }).join('')}</ul>`;
}

/* ── Insights card ──────────────────────────────────────────────────── */
async function renderRailInsights(isCurrent) {
  const access = portfolioAccess();
  if (access === 'disconnected') {
    railInsights.innerHTML = railMessage('Connect INDmoney to see how your holdings move each day.');
    return;
  }
  if (access === 'private') {
    railInsights.innerHTML = railMessage('Private mode keeps no snapshots to compare. Market Insights can still fetch live prices.');
    return;
  }
  railInsights.innerHTML = railSkeleton(4);
  let portfolio;
  try {
    portfolio = await getPortfolio();
  } catch (error) {
    if (isCurrent()) railInsights.innerHTML = railError(error);
    return;
  }
  if (!isCurrent()) return;
  if (!portfolio.available) {
    railInsights.innerHTML = railMessage('Insights about your holdings appear after your first snapshot.');
    return;
  }
  const insights = portfolioInsights(portfolio, 4);
  if (!insights.length) {
    railInsights.innerHTML = railMessage('Nothing stood out in your latest snapshot.');
    return;
  }
  const when = timeAgo(portfolio.capturedAt);
  railInsights.innerHTML = `<ul class="rail-list">${insights.map((insight) => {
    const [icon, color] = INSIGHT_ICONS[insight.kind] || INSIGHT_ICONS.day;
    const tone = insight.kind === 'day' ? (insight.tone === 'down' ? 'red' : 'green') : color;
    return `<li><button class="rail-row" type="button" data-agent="portfolio" data-prompt="${escHtml(insight.prompt)}">
      <span class="insight-icon tone-${tone}" aria-hidden="true"><svg viewBox="0 0 24 24">${icon}</svg></span>
      <span class="rail-row-body">
        <span class="rail-row-head"><strong>${escHtml(insight.title)}</strong><span class="rail-when">${escHtml(when)}</span></span>
        <small>${escHtml(insight.detail)}</small>
      </span>
    </button></li>`;
  }).join('')}</ul>
  <p class="rail-foot">From your latest snapshot, not market news. Select one to ask about it.</p>`;
}

/* ── Wiring ─────────────────────────────────────────────────────────── */
let railToken = 0;

function renderRail() {
  railToken += 1;
  const token = railToken;
  const isCurrent = () => token === railToken;
  if (!sessionReady()) {
    clearRail();
    return;
  }
  for (const render of [renderRailPortfolio, renderRailGoals, renderRailInsights]) {
    render(isCurrent).catch((error) => console.warn('Rail card failed to render:', error));
  }
}

function clearRail() {
  railPortfolio.innerHTML = '';
  railGoals.innerHTML = '';
  railInsights.innerHTML = '';
}

document.addEventListener('ca:session', renderRail);
document.addEventListener('ca:snapshot', renderRail);
document.addEventListener('ca:session-ended', () => {
  railToken += 1;
  clearRail();
});
document.addEventListener('ca:goals', () => {
  if (!sessionReady()) return;
  const token = railToken;
  renderRailGoals(() => token === railToken).catch((error) => console.warn('Rail goals failed to render:', error));
});
