/* ── Right rail ─────────────────────────────────────────────────────────
   Portfolio summary, goals, and insights from the latest snapshot. Loaded
   after finance.js, app.js and pages.js, and reuses their data loaders,
   so the rail and the pages share one request per resource. */

const railPortfolio = document.getElementById('rail-portfolio');
const railGoals = document.getElementById('rail-goals');
const railInsights = document.getElementById('rail-insights');
const RAIL_GOALS = 3;

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

// Exact rupees like the mock-up, falling back to lakh/crore when they would not fit a column.
function railAmount(value) {
  const exact = money(value);
  return exact.length > 10 ? moneyShort(value) : exact;
}

function sparkline(points, tone) {
  const values = thin(points, 60).map((point) => point.networthInr);
  if (values.length < 2) return '';
  const width = 280;
  const height = 110;
  const min = Math.min(...values);
  const range = Math.max(...values) - min || 1;
  const coords = values.map((value, index) => [
    (index / (values.length - 1)) * (width - 6),
    height - 8 - ((value - min) / range) * (height - 22),
  ]);
  const line = coords.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L');
  // Round dots that stay round when the chart stretches: zero-length strokes with round caps.
  const markerEvery = Math.max(1, Math.round(values.length / 7));
  const markers = coords.filter((_, index) => index > 0 && index < coords.length - 1 && index % markerEvery === 0)
    .map(([x, y]) => `M${x.toFixed(1)} ${y.toFixed(1)}h0`).join(' ');
  const [lastX, lastY] = coords[coords.length - 1];
  return `<svg class="rail-spark ${tone}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Net worth trend">
    <defs><linearGradient id="rail-spark-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="currentColor" stop-opacity=".32"/><stop offset="1" stop-color="currentColor" stop-opacity=".02"/>
    </linearGradient></defs>
    <path d="M${line} V${height} H0 Z" fill="url(#rail-spark-fill)"/>
    <path d="M${line}" class="spark-line"/>
    ${markers ? `<path d="${markers}" class="spark-dots"/>` : ''}
    <path d="M${lastX.toFixed(1)} ${lastY.toFixed(1)}h0" class="spark-dot-end"/>
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
    ? `<span class="rail-change ${tone}" title="Change since ${escHtml(formatDate(change.from))}"><span aria-hidden="true">${change.change < 0 ? '↓' : '↑'}</span> ${percent(change.pct, { signed: true })} <small>(${change.label})</small></span>`
    : '';
  const updated = `Net worth from your snapshot, updated ${timeAgo(portfolio.capturedAt)}`;
  const basis = `Over ${pnl.holdings_counted} holdings with a reported cost${pnl.holdings_excluded ? `; ${pnl.holdings_excluded} without one (usually linked brokers) are left out` : ''}.`;
  const returnsTone = pnl.pnl_inr < 0 ? 'is-down' : '';
  railPortfolio.innerHTML = `
    <div class="rail-figure-row">
      <strong class="rail-figure" title="${escHtml(updated)}">${money(netWorth)}</strong>
      ${changeHtml}
    </div>
    <span class="sr-only">${escHtml(updated)}.</span>
    ${sparkline(points, tone)}
    <dl class="rail-metrics" title="${escHtml(basis)}">
      <div><dt>Total Invested</dt><dd>${railAmount(pnl.invested_inr)}</dd></div>
      <div><dt>Total Returns</dt><dd class="${returnsTone}">${railAmount(pnl.pnl_inr)}</dd></div>
      <div><dt>Return</dt><dd class="${returnsTone}">${percent(pnl.pnl_pct)}</dd></div>
    </dl>`;
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
  document.querySelector('#right-rail a[href="#/goals"]').textContent = goals.length > RAIL_GOALS ? `See all (${goals.length})` : 'See all';
  if (!goals.length) {
    railGoals.innerHTML = railMessage('Set what you are saving for, like a home or your child\'s education, and track your progress here.',
      '<button class="rail-button" type="button" data-add-goal>Set a goal</button>');
    return;
  }
  // The nearest targets first; tones follow the user's own order so a goal keeps its colour.
  const shown = goals.map((goal, index) => ({ goal, index }))
    .sort((a, b) => a.goal.targetYear - b.goal.targetYear || a.index - b.index)
    .slice(0, RAIL_GOALS);
  const rows = shown.map(({ goal, index }) => {
    const projection = goalProjection(goal);
    const tone = GOAL_TONES[index % GOAL_TONES.length];
    const pct = Math.floor(projection.progress);
    const status = projection.onTrack ? 'on track' : 'needs more';
    return `<li><button class="rail-goal tone-${tone}" type="button" data-edit-goal="${escHtml(goal.id)}" title="${escHtml(`Edit ${goal.name} (${status}, target year ${goal.targetYear})`)}">
      ${goalIconHtml(goal, tone, 'rail-goal-icon')}
      <strong class="rail-goal-name">${escHtml(goal.name)}</strong>
      <span class="rail-goal-pct">${pct}%</span>
      <small class="rail-goal-target">Target: ${money(goal.targetInr)}</small>
      <span class="progress rail-progress" role="progressbar" aria-label="${escHtml(goal.name)} saved" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${projection.progress.toFixed(1)}%"></span></span>
    </button></li>`;
  }).join('');
  const add = goals.length < MAX_GOALS ? '<button class="rail-add" type="button" data-add-goal><span aria-hidden="true">+</span> Add a goal</button>' : '';
  railGoals.innerHTML = `<ul class="rail-list rail-goals">${rows}</ul>${add}`;
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
