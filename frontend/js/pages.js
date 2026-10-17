/* ── Pages behind the sidebar ───────────────────────────────────────────
   Home, Portfolio Overview, Investments, Planning Tools, Market Insights,
   Goals, Reports, Activity and All chats. Loaded after app.js and uses its state
   (currentUser, isConnected, storageMode, chats) and helpers. */

const ROUTES = ['home', 'chat', 'portfolio', 'investments', 'planning', 'markets', 'goals', 'reports', 'activity', 'chats'];
const pageView = document.getElementById('page-view');
const inputArea = document.getElementById('input-area');
let currentRoute = 'home';
let currentSub = '';

const ASSET_TYPE_LABELS = {
  MF: 'Mutual funds', IND_STOCK: 'Indian stocks', US_STOCK: 'US stocks', EPF: 'EPF', PPF: 'PPF', NPS: 'NPS',
  FD: 'Fixed deposits', BONDS: 'Bonds', GOLD: 'Gold', CRYPTO: 'Crypto', REAL_ESTATE: 'Real estate', CASH: 'Cash',
};

function assetTypeLabel(type) {
  if (!type) return 'Other';
  return ASSET_TYPE_LABELS[type] || String(type).toLowerCase().replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/* ── Formatting ─────────────────────────────────────────────────────── */
const plainNumber = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });

function toneOf(value) {
  return !Number.isFinite(value) || value === 0 ? '' : value > 0 ? 'is-up' : 'is-down';
}

function formatDate(isoDate) {
  if (!isoDate) return '';
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });
}

/* ── Data loading ───────────────────────────────────────────────────── */
// Promises keyed by request ('portfolio', 'history:30', 'goals') so pages opened together share one request.
const pageCache = new Map();

// Goals carry the value of assigned holdings, so a new snapshot makes them stale too.
function clearPortfolioCache() {
  pageCache.clear();
}

// One session reload at a time, however many requests found the session locked.
let sessionReload = null;
function reloadSession() {
  sessionReload ||= loadChats().finally(() => { sessionReload = null; });
  return sessionReload;
}

// The first field error FastAPI reported, without Pydantic's "Value error, " prefix.
function validationMessage(detail) {
  const message = String(detail[0]?.msg || '').replace(/^Value error, /, '');
  return message ? `Check the values and try again: ${message}` : 'Check the values and try again.';
}

async function fetchJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error('Could not reach CA Mode. Check that it is still running, then try again.');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (data.profileRequired || data.unlockRequired || data.consentRequired) reloadSession();
    throw new Error(data.error || (Array.isArray(data.detail) ? validationMessage(data.detail) : `HTTP ${response.status}`));
  }
  return data;
}

function cached(key, load) {
  if (!pageCache.has(key)) {
    // Failures are not cached, so the next visit tries again.
    pageCache.set(key, load().catch((error) => {
      pageCache.delete(key);
      throw error;
    }));
  }
  return pageCache.get(key);
}

const getPortfolio = () => cached('portfolio', () => fetchJson('/api/portfolio'));
const getHistory = (days) => cached(`history:${days}`, () => fetchJson(`/api/snapshots?days=${days}`));
const getGoalsData = () => cached('goals', () => fetchJson('/api/goals'));
const getGoals = () => getGoalsData().then((data) => data.goals || []);
const getAllocations = () => cached('allocations', () => fetchJson('/api/goals/allocations'));

function sessionReady() {
  return isSessionReady && Boolean(currentUser);
}

// What the portfolio pages can show for this profile.
function portfolioAccess() {
  if (!isConnected) return 'disconnected';
  if (storageMode === 'private') return 'private';
  return 'ok';
}

/* ── Shared building blocks ─────────────────────────────────────────── */
function statTile(label, value, sub = '', tone = '') {
  return `<div class="stat-tile"><span class="stat-label">${escHtml(label)}</span><strong class="stat-value ${tone}">${escHtml(value)}</strong>${sub ? `<small class="stat-sub ${tone}">${escHtml(sub)}</small>` : ''}</div>`;
}

function panel(title, body, { action = '', className = '' } = {}) {
  return `<section class="panel ${className}"><div class="rail-head"><h2>${escHtml(title)}</h2>${action}</div>${body}</section>`;
}

function notice(title, body, actionHtml = '') {
  return `<section class="panel empty-panel"><h2>${escHtml(title)}</h2><p>${escHtml(body)}</p>${actionHtml}</section>`;
}

const CONNECT_BUTTON = '<button class="rail-button inline-button" type="button" data-connect>Connect INDmoney</button>';
const REFRESH_BUTTON = '<button class="rail-button inline-button" type="button" data-refresh-snapshot>Save a snapshot now</button>';

function accessNotice(access) {
  if (access === 'disconnected') {
    return notice('Connect INDmoney', 'Link your INDmoney account to see your net worth, holdings and how they change each day. Access is read-only.', CONNECT_BUTTON);
  }
  return notice('Private mode', 'Private mode saves no portfolio snapshots, so there is nothing to chart here. Ask Portfolio Analyzer for live figures instead, or turn on saving from your profile.',
    '<button class="rail-button inline-button" type="button" data-agent="portfolio" data-prompt="Give me a snapshot of my current net worth.">Ask Portfolio Analyzer</button>');
}

function noSnapshotNotice() {
  return notice('No snapshot yet', 'CA Mode saves one portfolio snapshot a day. The first one is on its way; you can also save one now.', REFRESH_BUTTON);
}

function loadingBlock() {
  return '<div class="page-loading" role="status">Loading…</div>';
}

function errorBlock(error) {
  return `<div class="page-error" role="alert">${escHtml(error.message || 'Something went wrong.')}</div>`;
}

function promptChips(prompts) {
  return `<div class="follow-ups page-chips">${prompts.map(([label, prompt, agent = 'portfolio']) =>
    `<button class="chip" type="button" data-agent="${agent}" data-prompt="${escHtml(prompt)}">${escHtml(label)}</button>`).join('')}</div>`;
}

// Line charts draw at most 100 points per series, so thin long histories evenly, keeping the newest.
function thin(points, limit = 100) {
  if (points.length <= limit) return points;
  const step = (points.length - 1) / (limit - 1);
  return Array.from({ length: limit }, (_, index) => points[Math.round(index * step)]);
}

function netWorthSeries(points) {
  return [{ name: 'Net worth', data: thin(points).map((point) => ({ label: point.date, value: point.networthInr })) }];
}

function chartHtml(spec) {
  return renderDataChart(spec) || '<p class="page-muted">Not enough data to chart yet.</p>';
}

// Replaces a container's HTML, cleaning up and then drawing any Plotly charts.
function setHtml(container, html) {
  destroyPlotlyCharts(container);
  container.innerHTML = html;
  renderPlotlyCharts(container);
}

let toastTimer = null;
function toast(message) {
  let box = document.getElementById('toast');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast';
    box.className = 'toast';
    box.setAttribute('role', 'alert');
    document.body.appendChild(box);
  }
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 5000);
}

function downloadCsv(filename, rows) {
  const cell = (value) => {
    const text = value === null || value === undefined ? '' : String(value);
    // Leading =, +, - or @ would run as a formula in spreadsheet apps.
    const safe = /^[=+\-@]/.test(text) && !/^-?\d/.test(text) ? `'${text}` : text;
    return /[",\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
  };
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([rows.map((row) => row.map(cell).join(',')).join('\n')], { type: 'text/csv' }));
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// Each page render gets a token so a slow response never overwrites a newer render.
const renderTokens = {};
function startRender(route) {
  renderTokens[route] = (renderTokens[route] || 0) + 1;
  const token = renderTokens[route];
  return () => renderTokens[route] === token;
}

/* ── Routing ────────────────────────────────────────────────────────── */
function parseHash() {
  const [route = '', sub = ''] = location.hash.replace(/^#\/?/, '').split('/');
  return { route: ROUTES.includes(route) ? route : 'home', sub: decodeURIComponent(sub) };
}

function navigate(route, sub = '') {
  const hash = route === 'home' ? '#/' : `#/${route}${sub ? `/${encodeURIComponent(sub)}` : ''}`;
  if (location.hash !== hash) history.pushState(null, '', hash);
  renderRoute();
}

function renderRoute() {
  const { route, sub } = parseHash();
  const isChat = route === 'chat';
  const changed = route !== currentRoute;
  currentRoute = route;
  currentSub = sub;
  chatScroll.hidden = !isChat;
  inputArea.hidden = !isChat;
  pageView.hidden = isChat;
  pageView.querySelectorAll('.page').forEach((page) => { page.hidden = page.dataset.page !== route; });
  document.querySelectorAll('#sidebar [data-route]').forEach((link) => {
    if (link.dataset.route === route) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  setSidebarOpen(false);
  if (isChat) {
    scrollToBottom();
    return;
  }
  if (changed) pageView.scrollTop = 0;
  renderPage(route).catch((error) => console.warn('Page failed to render:', error));
}

async function renderPage(route) {
  if (!sessionReady()) return;
  const renderers = {
    home: renderHome, portfolio: renderPortfolio, investments: renderInvestments, planning: renderPlanning,
    markets: renderMarkets, goals: renderGoals, reports: renderReports, activity: renderActivity, chats: renderAllChats,
  };
  await renderers[route]?.();
}

window.addEventListener('hashchange', renderRoute);
document.addEventListener('ca:session', () => {
  pageCache.clear();
  renderRoute();
});
// Another profile may sign in next, so drop this one's figures from the page.
document.addEventListener('ca:session-ended', () => {
  pageCache.clear();
  latestHoldings = [];
  reportPoints = [];
  resetActivity();
  for (const id of ['home-stats', 'home-chats', 'portfolio-body', 'investments-body', 'markets-body', 'goals-body', 'saved-context-body', 'reports-body', 'activity-body', 'all-chats']) {
    setHtml(document.getElementById(id), '');
  }
  for (const id of ['portfolio-asof', 'investments-asof', 'home-name']) document.getElementById(id).textContent = '';
  if (goalDialog.open) goalDialog.close();
});
document.addEventListener('ca:snapshot', () => {
  clearPortfolioCache();
  if (currentRoute !== 'chat') renderPage(currentRoute);
});
document.addEventListener('ca:chats', () => {
  if (!sessionReady()) return;
  if (currentRoute === 'chats') renderAllChats();
  if (currentRoute === 'home') renderHomeChats();
});

chatSearch.addEventListener('input', () => {
  if (currentRoute !== 'chats') navigate('chats');
  else renderAllChats();
});

document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-connect], [data-go], [data-refresh-snapshot], .js-snapshot-refresh');
  if (!target || target.disabled) return;
  if (target.matches('[data-connect]')) {
    if (!isConnected) toggleConnection();
    return;
  }
  if (target.matches('[data-go]')) {
    navigate(target.dataset.go);
    if (target.hasAttribute('data-new-chat')) startNewChat().catch(showError);
    return;
  }
  const label = target.textContent;
  target.disabled = true;
  target.textContent = 'Saving snapshot…';
  try {
    await refreshSnapshot();
  } catch (error) {
    toast(error.message);
  } finally {
    target.disabled = false;
    target.textContent = label;
  }
});

/* ── Home ───────────────────────────────────────────────────────────── */
async function renderHome() {
  const isCurrent = startRender('home');
  document.getElementById('home-name').textContent = currentUser ? `, ${currentUser.displayName}` : '';
  updateGreeting();
  renderHomeChats();
  const stats = document.getElementById('home-stats');
  const goalsPromise = getGoals().catch(() => []);
  const access = portfolioAccess();
  let worth = statTile('Net worth', isConnected ? 'Live only' : 'Not connected', isConnected ? 'Private mode saves no snapshots' : 'Connect INDmoney to track it');
  let pnl = statTile('Invested P&L', '—', 'Needs a portfolio snapshot');
  if (access === 'ok') {
    stats.innerHTML = loadingBlock();
    const [portfolio, trend] = await Promise.all([getPortfolio().catch(() => null), getHistory(30).catch(() => null)]);
    if (portfolio?.available) {
      const values = (trend?.history || []).map((point) => point.networthInr).filter(Number.isFinite);
      const change = values.length > 1 ? values[values.length - 1] - values[0] : NaN;
      const changePct = values.length > 1 && values[0] ? change / values[0] * 100 : NaN;
      worth = statTile('Net worth', moneyShort(portfolio.figures.totals.net_worth_inr ?? portfolio.networthInr),
        Number.isFinite(change) ? `${signedMoney(change)} (${percent(changePct, { signed: true })}) in ${values.length} days` : `As of ${formatDate(portfolio.asOfDate)}`, toneOf(change));
      const p = portfolio.figures.pnl;
      pnl = statTile('Invested P&L', signedShort(p.pnl_inr), `${percent(p.pnl_pct, { signed: true })} on ${p.holdings_counted} holdings`, toneOf(p.pnl_inr));
    } else {
      worth = statTile('Net worth', '—', 'First snapshot on its way');
    }
  }
  const goals = await goalsPromise;
  const onTrack = goals.filter(goalIsOnTrack).length;
  if (!isCurrent()) return;
  stats.innerHTML = worth + pnl
    + statTile('Goals on track', goals.length ? `${onTrack} of ${goals.length}` : 'None yet', goals.length ? 'Under each goal\'s own assumption' : 'Add one on the Goals page')
    + statTile('Saved chats', String(chats.length), storageMode === 'private' ? 'Private: cleared at sign-out' : 'Kept for 30 days');
}

function renderHomeChats() {
  const list = document.getElementById('home-chats');
  list.innerHTML = '';
  // Untitled chats have no messages yet, so there is nothing to continue.
  const recent = chats.filter((chat) => chat.title && chat.title !== 'New chat').slice(0, 4);
  if (!recent.length) {
    list.innerHTML = '<p class="page-muted">No conversations yet. Ask anything to get started.</p>';
    return;
  }
  recent.forEach((chat) => list.appendChild(chatRow(chat, { showAgent: true })));
}

/* ── Portfolio Overview ─────────────────────────────────────────────── */
let trendDays = 90;

async function renderPortfolio() {
  const isCurrent = startRender('portfolio');
  const body = document.getElementById('portfolio-body');
  const asOf = document.getElementById('portfolio-asof');
  const refresh = document.querySelector('[data-page="portfolio"] .js-snapshot-refresh');
  const access = portfolioAccess();
  refresh.hidden = access !== 'ok';
  asOf.textContent = '';
  if (access !== 'ok') {
    setHtml(body, accessNotice(access));
    return;
  }
  setHtml(body, loadingBlock());
  let data;
  try {
    data = await getPortfolio();
  } catch (error) {
    if (isCurrent()) setHtml(body, errorBlock(error));
    return;
  }
  if (!isCurrent()) return;
  if (!data.available) {
    setHtml(body, noSnapshotNotice());
    return;
  }
  asOf.textContent = `From your snapshot of ${formatCaptured(data.capturedAt)}.`;
  const f = data.figures;
  const t = f.totals;
  const p = f.pnl;
  const stats = statTile('Net worth', moneyShort(t.net_worth_inr ?? data.networthInr), money(t.net_worth_inr ?? data.networthInr))
    + statTile('Current value', moneyShort(t.current_value_inr), 'All investments')
    + statTile('Invested', moneyShort(t.invested_inr), 'As reported by INDmoney')
    + statTile('P&L', signedShort(p.pnl_inr), `${percent(p.pnl_pct, { signed: true })} on ${p.holdings_counted} holdings with a cost`, toneOf(p.pnl_inr))
    + statTile('Liabilities', moneyShort(t.liabilities_inr), 'Loans and card dues');

  const allocation = (f.by_asset_type || []).filter((row) => row.current_value_inr > 0)
    .map((row) => ({ label: assetTypeLabel(row.asset_type), value: row.current_value_inr }));
  const classes = (f.by_asset_class || []).map((row) =>
    `<tr><td>${escHtml(row.assetclass_l2 || 'Other')}</td><td>${money(row.current_value_inr)}</td><td>${percent(row.share_pct)}</td></tr>`).join('');
  const caps = (f.by_market_cap_direct_stocks || []).map((row) =>
    `<tr><td>${escHtml(row.market_cap || 'Other')}</td><td>${money(row.current_value_inr)}</td><td>${percent(row.share_pct)}</td></tr>`).join('');
  const c = f.concentration || {};
  const concentration = `<dl class="fact-list">
      <div><dt>Holdings</dt><dd>${Number(c.holdings_count) || 0}</dd></div>
      <div><dt>Largest holding</dt><dd>${escHtml(c.largest_holding || '—')} ${Number.isFinite(c.largest_holding_pct) ? `(${percent(c.largest_holding_pct)})` : ''}</dd></div>
      <div><dt>Top 5 holdings</dt><dd>${percent(c.top_5_pct)} of the portfolio</dd></div>
      <div><dt>Top 10 holdings</dt><dd>${percent(c.top_10_pct)} of the portfolio</dd></div>
    </dl>`;
  const notes = [...(f.data_notes || []), ...Object.keys(data.unavailable || {}).map((name) => `${name} could not be fetched for this snapshot.`)];

  setHtml(body, `
    <div class="stat-grid">${stats}</div>
    ${panel('Net worth trend', '<div id="trend-chart"></div>', { action: trendControls() })}
    <div class="page-columns">
      ${panel('Allocation by asset type', chartHtml({ type: 'donut', title: 'Allocation by asset type', unit: '₹', data: allocation }))}
      <div class="page-stack">
        ${panel('Concentration', concentration)}
        ${classes ? panel('Asset classes', `<table class="data-table compact"><thead><tr><th>Class</th><th>Value</th><th>Share</th></tr></thead><tbody>${classes}</tbody></table>`) : ''}
        ${caps ? panel('Direct stocks by market cap', `<table class="data-table compact"><thead><tr><th>Market cap</th><th>Value</th><th>Share</th></tr></thead><tbody>${caps}</tbody></table>`) : ''}
      </div>
    </div>
    ${panel('Top holdings', holdingsTable(f.top_holdings || [], { compact: true }), { action: '<a class="panel-link" href="#/investments">All investments</a>' })}
    ${notes.length ? panel('About these numbers', `<ul class="note-list">${notes.map((note) => `<li>${escHtml(note)}</li>`).join('')}</ul>`) : ''}
    ${promptChips([
      ['Explain my allocation', 'Explain my current asset allocation and what stands out.'],
      ['Concentration risk', 'Is my portfolio too concentrated? Look at my largest holdings and sectors.'],
      ['Rebalancing ideas', 'Based on my risk appetite and goals, how far is my allocation from a balanced mix? Explain the trade-offs, without recommending specific products.'],
    ])}`);
  renderTrend();
}

function trendControls() {
  return `<div class="seg seg-small" role="group" aria-label="Trend period">${[[30, '1M'], [90, '3M'], [180, '6M'], [365, '1Y']].map(([days, label]) =>
    `<button type="button" data-trend-days="${days}" aria-pressed="${days === trendDays}">${label}</button>`).join('')}</div>`;
}

async function renderTrend() {
  const container = document.getElementById('trend-chart');
  if (!container) return;
  let data;
  try {
    data = await getHistory(trendDays);
  } catch (error) {
    setHtml(container, errorBlock(error));
    return;
  }
  const points = (data.history || []).filter((point) => Number.isFinite(point.networthInr));
  if (points.length < 2) {
    setHtml(container, '<p class="page-muted">The trend appears once there are two days of snapshots.</p>');
    return;
  }
  setHtml(container, chartHtml({
    type: 'line', title: `Net worth, last ${trendDays} days`, unit: '₹',
    series: netWorthSeries(points),
  }));
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-trend-days]');
  if (!button) return;
  trendDays = Number(button.dataset.trendDays);
  button.parentElement.querySelectorAll('button').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
  renderTrend();
});

function holdingsTable(holdings, { compact = false } = {}) {
  if (!holdings.length) return '<p class="page-muted">No holdings in this view.</p>';
  const rows = holdings.map((item) => {
    const where = [item.broker, assetTypeLabel(item.asset_type)].filter(Boolean).join(' · ');
    const ask = `Tell me about my holding ${item.name}: its value, P&L, share of my portfolio, and anything to keep an eye on.`;
    return `<tr>
      <td><span class="holding-name">${escHtml(item.name || item.code || 'Unnamed')}</span><small>${escHtml(where)}</small></td>
      <td>${money(item.current_value_inr)}${Number.isFinite(item.current_value_usd) ? `<small>$${plainNumber.format(item.current_value_usd)}</small>` : ''}</td>
      ${compact ? '' : `<td>${money(item.invested_inr)}</td>`}
      <td class="${toneOf(item.pnl_inr)}">${item.pnl_inr === null ? '<span class="page-muted" title="No invested amount reported (usually a linked broker)">n/a</span>' : `${signedMoney(item.pnl_inr)}<small>${percent(item.pnl_pct, { signed: true })}</small>`}</td>
      <td>${percent(item.share_of_portfolio_pct)}</td>
      ${compact ? '' : `<td class="${toneOf(item.one_day_change_inr)}">${Number.isFinite(item.one_day_change_inr) && item.one_day_change_inr !== 0 ? signedMoney(item.one_day_change_inr) : '—'}</td>
      <td><button class="table-action" type="button" data-agent="portfolio" data-prompt="${escHtml(ask)}">Ask</button></td>`}
    </tr>`;
  }).join('');
  return `<div class="table-scroll"><table class="data-table"><thead><tr>
      <th>Holding</th><th>Value</th>${compact ? '' : '<th>Invested</th>'}<th>P&amp;L</th><th>Share</th>${compact ? '' : '<th>1 day</th><th><span class="sr-only">Ask AI</span></th>'}
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

/* ── Investments ────────────────────────────────────────────────────── */
const holdingsView = { type: 'ALL', query: '', sort: 'value' };
let latestHoldings = [];

async function renderInvestments() {
  const isCurrent = startRender('investments');
  const body = document.getElementById('investments-body');
  const asOf = document.getElementById('investments-asof');
  const csv = document.getElementById('holdings-csv');
  csv.hidden = true;
  asOf.textContent = 'Every holding from your latest snapshot, plus holdings at other brokers from your CAS.';
  const access = portfolioAccess();
  if (access !== 'ok') {
    setHtml(body, accessNotice(access));
    return;
  }
  setHtml(body, loadingBlock());
  let data;
  try {
    data = await getPortfolio();
  } catch (error) {
    if (isCurrent()) setHtml(body, errorBlock(error));
    return;
  }
  if (!isCurrent()) return;
  if (!data.available) {
    setHtml(body, noSnapshotNotice());
    return;
  }
  latestHoldings = data.holdings || [];
  csv.hidden = !latestHoldings.length;
  asOf.textContent = `${latestHoldings.length} holdings from your snapshot of ${formatCaptured(data.capturedAt)}.`;
  const types = [...new Set(latestHoldings.map((item) => item.asset_type).filter(Boolean))];
  if (holdingsView.type !== 'ALL' && !types.includes(holdingsView.type)) holdingsView.type = 'ALL';
  const brokers = (data.figures.by_broker || []).map((row) =>
    `<tr><td>${escHtml(row.broker)}</td><td>${Number(row.holdings) || 0}</td><td>${money(row.current_value_inr)}</td><td>${percent(row.share_of_listed_holdings_pct)}</td></tr>`).join('');
  setHtml(body, `
    <section class="panel">
      <div class="table-controls">
        <div class="seg seg-small" role="group" aria-label="Filter by asset type">
          ${['ALL', ...types].map((type) => `<button type="button" data-holding-type="${escHtml(type)}" aria-pressed="${type === holdingsView.type}">${escHtml(type === 'ALL' ? 'All' : assetTypeLabel(type))}</button>`).join('')}
        </div>
        <label class="search-box table-search"><input id="holding-search" type="search" placeholder="Search holdings…" aria-label="Search holdings" value="${escHtml(holdingsView.query)}" autocomplete="off" /></label>
        <label class="field inline-field"><span>Sort by</span>
          <select id="holding-sort">
            ${[['value', 'Value'], ['pnl', 'P&L (₹)'], ['pnlPct', 'P&L (%)'], ['day', '1-day change'], ['name', 'Name']].map(([value, label]) =>
              `<option value="${value}" ${value === holdingsView.sort ? 'selected' : ''}>${label}</option>`).join('')}
          </select>
        </label>
      </div>
      <div id="holdings-table"></div>
    </section>
    ${brokers ? panel('By broker', `<table class="data-table compact"><thead><tr><th>Broker</th><th>Holdings</th><th>Value</th><th>Share</th></tr></thead><tbody>${brokers}</tbody></table>`) : ''}`);
  renderHoldingsTable();
}

function renderHoldingsTable() {
  const container = document.getElementById('holdings-table');
  if (!container) return;
  const query = holdingsView.query.trim().toLowerCase();
  const sorters = {
    value: (a, b) => (b.current_value_inr || 0) - (a.current_value_inr || 0),
    pnl: (a, b) => (b.pnl_inr ?? -Infinity) - (a.pnl_inr ?? -Infinity),
    pnlPct: (a, b) => (b.pnl_pct ?? -Infinity) - (a.pnl_pct ?? -Infinity),
    day: (a, b) => (b.one_day_change_inr || 0) - (a.one_day_change_inr || 0),
    name: (a, b) => String(a.name || '').localeCompare(String(b.name || '')),
  };
  const rows = latestHoldings
    .filter((item) => holdingsView.type === 'ALL' || item.asset_type === holdingsView.type)
    .filter((item) => !query || [item.name, item.code, item.broker].some((value) => String(value || '').toLowerCase().includes(query)))
    .sort(sorters[holdingsView.sort] || sorters.value);
  container.innerHTML = holdingsTable(rows);
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-holding-type]');
  if (!button) return;
  holdingsView.type = button.dataset.holdingType;
  button.parentElement.querySelectorAll('button').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
  renderHoldingsTable();
});
document.addEventListener('input', (event) => {
  if (event.target.id === 'holding-search') {
    holdingsView.query = event.target.value;
    renderHoldingsTable();
  }
});
document.addEventListener('change', (event) => {
  if (event.target.id === 'holding-sort') {
    holdingsView.sort = event.target.value;
    renderHoldingsTable();
  }
});

function holdingsCsvRows(holdings) {
  return [
    ['Holding', 'Code', 'Asset type', 'Broker', 'Units', 'Price (INR)', 'Value (INR)', 'Invested (INR)', 'P&L (INR)', 'P&L (%)', 'Share (%)', '1-day change (INR)', 'Value (USD)'],
    ...holdings.map((item) => [item.name, item.code, assetTypeLabel(item.asset_type), item.broker, item.units, item.unit_price_inr,
      item.current_value_inr, item.invested_inr, item.pnl_inr, item.pnl_pct, item.share_of_portfolio_pct, item.one_day_change_inr, item.current_value_usd]),
  ];
}

document.getElementById('holdings-csv').addEventListener('click', () => {
  downloadCsv(`ca-mode-holdings-${new Date().toISOString().slice(0, 10)}.csv`, holdingsCsvRows(latestHoldings));
});

/* ── Planning Tools ─────────────────────────────────────────────────── */
const CALCULATORS = {
  sip: {
    title: 'SIP calculator',
    fields: [
      ['amount', 'Monthly investment (₹)', 10000, 100, 10000000, 500],
      ['rate', 'Expected return (% a year)', 12, 0, 30, 0.5],
      ['years', 'Years', 10, 1, 50, 1],
    ],
    compute({ amount, rate, years }) {
      const months = years * 12;
      const value = sipValue(amount, rate, months);
      const invested = amount * months;
      return {
        results: [['Estimated value', moneyShort(value), money(value)], ['You invest', moneyShort(invested), money(invested)], ['Estimated gains', moneyShort(value - invested), percent(invested ? (value - invested) / invested * 100 : NaN, { signed: true })]],
        series: yearly(years, (y) => amount * 12 * y, (y) => sipValue(amount, rate, 12 * y)),
        prompt: `I plan a SIP of ${money(amount)} a month for ${years} years and assume ${rate}% a year. My calculator estimates ${money(value)}. Is that return assumption realistic for Indian equity or debt funds, and what risks should I keep in mind?`,
      };
    },
  },
  lumpsum: {
    title: 'Lumpsum calculator',
    fields: [
      ['amount', 'Amount invested (₹)', 100000, 1000, 1000000000, 1000],
      ['rate', 'Expected return (% a year)', 12, 0, 30, 0.5],
      ['years', 'Years', 10, 1, 50, 1],
    ],
    compute({ amount, rate, years }) {
      const value = grow(amount, rate, years * 12);
      return {
        results: [['Estimated value', moneyShort(value), money(value)], ['You invest', moneyShort(amount), money(amount)], ['Estimated gains', moneyShort(value - amount), percent((value - amount) / amount * 100, { signed: true })]],
        series: yearly(years, () => amount, (y) => grow(amount, rate, 12 * y)),
        prompt: `If I invest a lumpsum of ${money(amount)} for ${years} years at an assumed ${rate}% a year, my calculator estimates ${money(value)}. Should I invest it at once or spread it out with an STP? Explain the trade-offs.`,
      };
    },
  },
  goal: {
    title: 'Goal planner',
    fields: [
      ['target', "Goal cost in today's money (₹)", 2500000, 1000, 10000000000, 10000],
      ['years', 'Years to the goal', 8, 1, 50, 1],
      ['inflation', 'Inflation (% a year)', 6, 0, 15, 0.5],
      ['saved', 'Already saved for it (₹)', 0, 0, 10000000000, 10000],
      ['rate', 'Expected return (% a year)', 11, 0, 30, 0.5],
    ],
    compute({ target, years, inflation, saved, rate }) {
      const months = years * 12;
      const future = target * (1 + inflation / 100) ** years;
      const monthly = monthlyNeeded(future, saved, rate, months);
      return {
        results: [['Monthly SIP needed', moneyShort(monthly), `For ${years} years at ${rate}% a year`], ['Goal cost then', moneyShort(future), `${inflation}% inflation for ${years} years`], ['Savings grow to', moneyShort(grow(saved, rate, months)), money(grow(saved, rate, months))]],
        series: yearly(years, () => future, (y) => grow(saved, rate, 12 * y) + sipValue(monthly, rate, 12 * y), ['Goal cost', 'Projected savings']),
        prompt: `I want to reach a goal costing ${money(target)} today in ${years} years. With ${inflation}% inflation it will cost about ${money(future)}. I have ${money(saved)} saved and my calculator says I need a SIP of ${money(monthly)} a month at ${rate}% a year. How should I think about asset allocation for this goal as it gets closer?`,
        // Saved as the user's own assumption; the Goals page then shows no growth and scenarios around it.
        goal: { currency: 'INR', targetAmount: Math.round(future), targetDate: yearsFromToday(years), savedAmount: Math.round(saved), monthlyContribution: Math.ceil(monthly / 100) * 100, assumedReturnPct: rate },
      };
    },
  },
  tax: {
    title: `Income tax (${TAX_RULES.label})`,
    fields: [
      ['salary', 'Salary or pension a year (₹)', 1200000, 0, 1000000000, 10000],
      ['other', 'Other income a year (₹)', 0, 0, 1000000000, 10000],
      ['deductions', 'Old-regime deductions: 80C, 80D, HRA, home loan (₹)', 150000, 0, 100000000, 5000],
    ],
    compute(values) {
      const result = incomeTax(values);
      const gross = result.new.gross;
      const rows = [
        ['Gross income', 'gross'], ['Standard deduction', 'standardDeduction', true], ['Other deductions', 'deductions', true],
        ['Taxable income', 'taxable'], ['Tax on slabs', 'slabTax'], ['Rebate (section 87A)', 'rebate', true],
        ['Surcharge', 'surcharge'], [`Health and education cess (${TAX_RULES.cessPct}%)`, 'cess'],
      ].map(([label, key, minus]) => `<tr><td>${label}</td>${['new', 'old'].map((regime) =>
        `<td>${minus && result[regime][key] ? '−' : ''}${money(result[regime][key])}</td>`).join('')}</tr>`).join('');
      const effective = (regime) => percent(gross ? result[regime].total / gross * 100 : 0);
      const better = result[result.better].name;
      return {
        results: [
          ['New regime', moneyShort(result.new.total), `${money(result.new.total)} · ${effective('new')} of income`],
          ['Old regime', moneyShort(result.old.total), `${money(result.old.total)} · ${effective('old')} of income`],
          [result.saving ? `${better} saves` : 'Both regimes', result.saving ? moneyShort(result.saving) : 'Same tax', result.saving ? `${money(result.saving)} a year` : ''],
        ],
        table: `<div class="table-scroll"><table class="data-table"><thead><tr><th></th><th>New regime</th><th>Old regime</th></tr></thead><tbody>${rows}
          <tr class="total-row"><td>Total tax</td><td>${money(result.new.total)}</td><td>${money(result.old.total)}</td></tr></tbody></table></div>`,
        note: `For resident individuals below 60 in ${TAX_RULES.label}. All income is taxed at slab rates here: capital gains, senior-citizen limits and employer NPS contributions are not included. Check your final figure with a tax professional or the income tax portal.`,
        prompt: `My salary is ${money(values.salary)} a year with ${money(values.other)} of other income and ${money(values.deductions)} of old-regime deductions. A calculator estimates ${money(result.new.total)} tax under the new regime and ${money(result.old.total)} under the old regime for ${TAX_RULES.label}. What else should I consider when choosing a regime, and which deductions could I be missing?`,
      };
    },
  },
  emi: {
    title: 'Loan EMI calculator',
    fields: [
      ['principal', 'Loan amount (₹)', 3000000, 10000, 1000000000, 10000],
      ['rate', 'Interest rate (% a year)', 8.5, 0, 30, 0.05],
      ['years', 'Tenure (years)', 20, 1, 40, 1],
    ],
    compute({ principal, rate, years }) {
      const months = years * 12;
      const i = rate / 100 / 12;
      const emi = i ? principal * i * (1 + i) ** months / ((1 + i) ** months - 1) : principal / months;
      const balance = (m) => (i ? principal * (1 + i) ** m - emi * (((1 + i) ** m - 1) / i) : principal - emi * m);
      const total = emi * months;
      return {
        results: [['Monthly EMI', money(emi), `${months} instalments`], ['Total interest', moneyShort(total - principal), percent((total - principal) / principal * 100) + ' of the loan'], ['Total payment', moneyShort(total), money(total)]],
        series: yearly(years, (y) => Math.max(0, balance(12 * y)), (y) => emi * 12 * y, ['Outstanding loan', 'Paid so far']),
        prompt: `I am considering a loan of ${money(principal)} at ${rate}% for ${years} years; the EMI is about ${money(emi)} and total interest about ${money(total - principal)}. How do prepayments or a shorter tenure change the interest I pay, and what should I weigh against investing instead?`,
      };
    },
  },
};

function yearly(years, first, second, names = ['Invested', 'Estimated value']) {
  const step = Math.max(1, Math.ceil(years / 30));
  const points = [];
  for (let y = 0; y <= years; y += step) points.push(y);
  if (points[points.length - 1] !== years) points.push(years);
  return [first, second].map((fn, index) => ({
    name: names[index],
    data: points.map((y) => ({ label: String(new Date().getFullYear() + y), value: Math.round(fn(y)) })),
  }));
}

let activeCalc = 'sip';
const calcValues = {};
let calcTimer = null;

function renderPlanning() {
  if (CALCULATORS[currentSub]) activeCalc = currentSub;
  const calc = CALCULATORS[activeCalc];
  calcValues[activeCalc] ||= Object.fromEntries(calc.fields.map(([id, , value]) => [id, value]));
  document.querySelectorAll('#calc-tabs [data-calc]').forEach((tab) => {
    tab.setAttribute('aria-selected', String(tab.dataset.calc === activeCalc));
    tab.setAttribute('aria-pressed', String(tab.dataset.calc === activeCalc));
  });
  const inputs = calc.fields.map(([id, label, , min, max, step]) => `
    <label class="field calc-field"><span>${escHtml(label)}</span>
      <input type="number" data-calc-input="${id}" value="${calcValues[activeCalc][id]}" min="${min}" max="${max}" step="any" inputmode="decimal" />
      <input type="range" data-calc-input="${id}" value="${calcValues[activeCalc][id]}" min="${min}" max="${Math.min(max, rangeMax(id, max))}" step="${step}" aria-hidden="true" tabindex="-1" />
    </label>`).join('');
  setHtml(document.getElementById('calc-body'), `
    <div class="calc-layout">
      ${panel(calc.title, `<div class="calc-inputs">${inputs}</div>`)}
      <div class="page-stack" id="calc-output"></div>
    </div>`);
  renderCalcOutput();
}

// Sliders cover the usual range; the number box still accepts anything up to ``max``.
function rangeMax(id, max) {
  return { amount: activeCalc === 'sip' ? 200000 : 10000000, principal: 20000000, target: 50000000, saved: 10000000, salary: 5000000, other: 5000000, deductions: 500000 }[id] || max;
}

function renderCalcOutput() {
  const output = document.getElementById('calc-output');
  if (!output) return;
  const calc = CALCULATORS[activeCalc];
  const values = calcValues[activeCalc];
  const valid = calc.fields.every(([id, , , min, max]) => Number.isFinite(values[id]) && values[id] >= min && values[id] <= max);
  if (!valid) {
    setHtml(output, '<p class="page-error">Enter values within the allowed ranges.</p>');
    return;
  }
  const result = calc.compute(values);
  setHtml(output, `
    <div class="stat-grid stat-grid-3">${result.results.map(([label, value, sub]) => statTile(label, value, sub)).join('')}</div>
    ${result.series ? panel('Year by year', chartHtml({ type: 'line', title: calc.title, unit: '₹', series: result.series })) : ''}
    ${result.table ? panel('How it adds up', result.table) : ''}
    ${result.note ? `<p class="page-muted calc-note">${escHtml(result.note)}</p>` : ''}
    <div class="page-actions">
      <button class="gate-secondary" type="button" data-agent="finance" data-prompt="${escHtml(result.prompt)}">Ask Finance Agent about this</button>
      ${result.goal ? `<button class="rail-button page-cta" type="button" data-save-calc-goal>Save as a goal</button>` : ''}
    </div>`);
}

document.getElementById('calc-tabs').addEventListener('click', (event) => {
  const tab = event.target.closest('[data-calc]');
  if (tab) navigate('planning', tab.dataset.calc);
});

document.addEventListener('input', (event) => {
  const input = event.target.closest('[data-calc-input]');
  if (!input) return;
  const id = input.dataset.calcInput;
  calcValues[activeCalc][id] = input.value === '' ? NaN : Number(input.value);
  // Keep the number box and slider in step.
  input.closest('.calc-field').querySelectorAll('[data-calc-input]').forEach((other) => {
    if (other !== input) other.value = input.value;
  });
  clearTimeout(calcTimer);
  calcTimer = setTimeout(renderCalcOutput, 200);
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-save-calc-goal]')) return;
  const result = CALCULATORS.goal.compute(calcValues.goal);
  openGoalDialog({ name: '', ...result.goal });
});

/* ── Market Insights ────────────────────────────────────────────────── */
async function renderMarkets() {
  const isCurrent = startRender('markets');
  if (currentSub === 'funds') {
    // Quick Tools' "Compare Funds" lands on the fund form.
    requestAnimationFrame(() => {
      document.getElementById('mf-category-form').scrollIntoView({ block: 'center' });
      document.getElementById('mf-category').focus({ preventScroll: true });
    });
  }
  const body = document.getElementById('markets-body');
  const access = portfolioAccess();
  if (access === 'disconnected') {
    setHtml(body, accessNotice(access));
    return;
  }
  if (access === 'private') {
    setHtml(body, '');
    return;
  }
  setHtml(body, loadingBlock());
  let data;
  try {
    data = await getPortfolio();
  } catch (error) {
    if (isCurrent()) setHtml(body, errorBlock(error));
    return;
  }
  if (!isCurrent()) return;
  if (!data.available) {
    setHtml(body, noSnapshotNotice());
    return;
  }
  const holdings = data.holdings || [];
  const moved = holdings.filter((item) => Number.isFinite(item.one_day_change_inr) && item.one_day_change_inr !== 0);
  const dayChange = moved.reduce((sum, item) => sum + item.one_day_change_inr, 0);
  const previous = holdings.reduce((sum, item) => sum + (item.current_value_inr || 0), 0) - dayChange;
  const up = moved.filter((item) => item.one_day_change_inr > 0).sort((a, b) => b.one_day_change_inr - a.one_day_change_inr);
  const down = moved.filter((item) => item.one_day_change_inr < 0).sort((a, b) => a.one_day_change_inr - b.one_day_change_inr);
  const moverTable = (items) => items.length ? `<table class="data-table compact"><thead><tr><th>Holding</th><th>Value</th><th>Day change</th></tr></thead><tbody>${items.slice(0, 5).map((item) => {
    const base = (item.current_value_inr || 0) - item.one_day_change_inr;
    return `<tr><td>${escHtml(item.name || item.code)}</td><td>${money(item.current_value_inr)}</td><td class="${toneOf(item.one_day_change_inr)}">${signedMoney(item.one_day_change_inr)}<small>${percent(base ? item.one_day_change_inr / base * 100 : NaN, { signed: true })}</small></td></tr>`;
  }).join('')}</tbody></table>` : '<p class="page-muted">None.</p>';
  const caps = (data.figures.by_market_cap_direct_stocks || []).map((row) => ({ label: row.market_cap || 'Other', value: row.current_value_inr }));
  document.getElementById('markets-asof').textContent = `Day moves from your snapshot of ${formatCaptured(data.capturedAt)}. Live prices come from INDmoney through Portfolio Analyzer.`;
  setHtml(body, `
    <div class="stat-grid stat-grid-3">
      ${statTile('Day change', moved.length ? signedMoney(dayChange) : '—', moved.length ? percent(previous ? dayChange / previous * 100 : NaN, { signed: true }) : 'No day change reported', toneOf(dayChange))}
      ${statTile('Holdings up', String(up.length), `of ${holdings.length} holdings`, up.length ? 'is-up' : '')}
      ${statTile('Holdings down', String(down.length), `of ${holdings.length} holdings`, down.length ? 'is-down' : '')}
    </div>
    <div class="page-columns">
      ${panel('Top gainers', moverTable(up))}
      ${panel('Top losers', moverTable(down))}
    </div>
    ${caps.length ? panel('Your stocks by market cap', chartHtml({ type: 'donut', title: 'Direct stocks by market cap', unit: '₹', data: caps })) : ''}`);
}

document.getElementById('stock-chart-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = document.getElementById('stock-symbol');
  const symbol = input.value.trim().toUpperCase();
  if (!/^[A-Z0-9&.-]{1,20}$/.test(symbol)) {
    input.setCustomValidity('Use the NSE symbol, for example INFY or HDFCBANK.');
    input.reportValidity();
    return;
  }
  input.setCustomValidity('');
  const days = Number(document.getElementById('stock-days').value);
  runPrompt(`Show a candlestick chart of ${symbol} daily prices for the last ${days} days, with a one-line summary of the move.`, 'portfolio').catch(showError);
});
document.getElementById('stock-symbol').addEventListener('input', (event) => event.target.setCustomValidity(''));

document.getElementById('mf-category-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const category = document.getElementById('mf-category').value;
  runPrompt(`Compare the top ${category} mutual funds in a table: 1, 3 and 5 year returns, expense ratio and risk. Mark any I already hold.`, 'portfolio').catch(showError);
});

/* ── Goals ──────────────────────────────────────────────────────────── */
// Plans (gap, scenarios, status) come from the server with each goal; the page only formats them.
const MAX_GOALS = 10;
const goalDialog = document.getElementById('goal-dialog');
const goalForm = document.getElementById('goal-form');
const goalFields = {
  name: document.getElementById('goal-name'), kind: document.getElementById('goal-kind'),
  currency: document.getElementById('goal-currency'), date: document.getElementById('goal-date'),
  target: document.getElementById('goal-target'), saved: document.getElementById('goal-saved'),
  monthly: document.getElementById('goal-monthly'), rate: document.getElementById('goal-return'),
  notes: document.getElementById('goal-notes'), horizon: document.getElementById('goal-horizon'),
  flexibility: document.getElementById('goal-flex'), liquidityNeed: document.getElementById('goal-liquidity'),
  riskTolerance: document.getElementById('goal-risk'), lossCapacity: document.getElementById('goal-loss'),
  inflation: document.getElementById('goal-inflation'),
};
let editingGoalId = null;
// The type follows the goal's name until the user picks one.
let goalKindChosen = false;
// False while the date field still holds the dialog's pre-filled suggestion for a new goal.
let goalDateChosen = false;
let goalPreviewTimer = null;
let goalPreviewSeq = 0;

// Shared by the Goals page and the right rail; tones repeat after five goals.
const GOAL_ICONS = {
  home: '<path d="M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1Z"/>',
  education: '<path d="m3 9 9-4 9 4-9 4Z"/><path d="M7 11v4.5c0 1.4 2.2 2.5 5 2.5s5-1.1 5-2.5V11"/><path d="M21 9v5"/>',
  financial_independence: '<path d="M12 3.5v17"/><path d="M7 7.5h7a3 3 0 0 1 0 6H9.5a3 3 0 0 0 0 6H17"/>',
  retirement: '<path d="M3.5 12a8.5 8.5 0 0 1 17 0Z"/><path d="M12 12v6.5a1.5 1.5 0 0 1-3 0"/><path d="M12 3.5V2"/>',
  car: '<path d="M5 16v-4l2-5h10l2 5v4Z"/><path d="M5 12h14"/><circle cx="8" cy="16.5" r="1.5"/><circle cx="16" cy="16.5" r="1.5"/>',
  travel: '<path d="M2.5 13.5 21 6l-4.5 13-4-5.5Z"/><path d="m12.5 13.5 3-3"/>',
  wedding: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z"/>',
  emergency: '<path d="M12 3.5 19 6v5.5c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6Z"/>',
  other: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/>',
};
const GOAL_TONES = ['green', 'blue', 'violet', 'amber', 'red'];

function goalIconHtml(goal, tone, className = 'insight-icon') {
  return `<span class="${className} tone-${tone}" aria-hidden="true"><svg viewBox="0 0 24 24">${GOAL_ICONS[goalIcon(goal)]}</svg></span>`;
}

function isoDay(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function yearsFromToday(years) {
  const date = new Date();
  date.setFullYear(date.getFullYear() + years);
  return isoDay(date);
}

// One sentence on where the goal stands, from its plan.
function goalSummary(plan) {
  if (!plan) return '';
  const cur = plan.currency;
  const base = baseScenario(plan);
  const by = formatDate(plan.targetDate);
  switch (plan.status) {
    case 'reached':
      return `The ${amountShort(plan.startingAmount ?? plan.savedAmount, cur)} counted so far already covers the target.`;
    case 'past_due':
    case 'due_now':
      return `${amount(plan.remainingGap, cur)} is still needed, and no monthly contributions are left before ${by}.`;
    case 'insufficient_data':
      return (plan.warnings || [])[0] || 'Some details this goal needs are missing.';
    default:
      if (!base) return '';
      if (base.onTrack) return `Projected ${amountShort(base.projectedAmount, cur)} by ${by} at ${base.annualReturnPct}% a year, ${amountShort(base.difference, cur)} more than the target.`;
      return `Projected ${amountShort(base.projectedAmount, cur)} by ${by} at ${base.annualReturnPct}% a year, ${amountShort(-base.difference, cur)} short. `
        + (base.requiredMonthly != null ? `About ${amount(base.requiredMonthly, cur)} a month would close the gap under the same assumption.` : '');
  }
}

const RISK_NOTE = 'Illustrations under stated assumptions, not advice or promised returns. Market-linked investments can lose value.';

function scenarioDetails(plan) {
  if (!plan || !(plan.scenarios || []).length) return '';
  const cur = plan.currency;
  const rows = plan.scenarios.map((item) => `<tr>
      <td>${escHtml(item.label)}</td>
      <td>${amountShort(item.projectedAmount, cur)}</td>
      <td class="${item.difference >= 0 ? 'is-up' : 'is-down'}">${item.difference >= 0 ? '+' : '−'}${amountShort(Math.abs(item.difference), cur)}</td>
      <td>${item.requiredMonthly == null ? '—' : amount(item.requiredMonthly, cur)}</td>
    </tr>`).join('');
  const notes = [...(plan.warnings || []), ...(plan.assumptions || []), RISK_NOTE];
  const inflation = plan.inflation
    ? `<p class="goal-meta">Target ${amount(plan.inflation.targetInTodayMoney, cur)} in today's money becomes ${amount(plan.inflation.inflatedTarget, cur)} at ${plan.inflation.inflationPct}% a year. Projected amounts are in money of the target date and are compared with that raised target. Total planned contributions: ${amount(plan.totalFutureContributions, cur)}.</p>`
    : `<p class="goal-meta">Projected amounts are in money of the target date; inflation is not taken into account. Total planned contributions: ${amount(plan.totalFutureContributions, cur)}.</p>`;
  return `<details class="goal-scenarios"><summary>Scenarios and assumptions</summary>
    ${inflation}
    <div class="table-scroll"><table class="data-table compact"><thead><tr><th>Yearly return</th><th>Projected</th><th>vs target</th><th>Needed a month</th></tr></thead><tbody>${rows}</tbody></table></div>
    <ul class="goal-notes">${notes.map((note) => `<li>${escHtml(note)}</li>`).join('')}</ul>
  </details>`;
}

function alternativesList(plan) {
  const items = plan.alternatives || [];
  if (!items.length) return '';
  return `<div class="goal-section"><h4>Ways to close the gap</h4><ul class="goal-alts">${items.map((item) => {
    const { tag, text } = alternativeLabel(item, plan.currency, formatDate);
    return `<li><span class="basis-tag ${item.basis === 'zero_return' ? 'is-certain' : ''}">${escHtml(tag)}</span> ${escHtml(text)}</li>`;
  }).join('')}</ul></div>`;
}

const VERDICT_LABELS = { allowed: 'Allowed', caution: 'Caution', unsuitable: 'Unsuitable' };

function suitabilityBlock(suitability) {
  if (!suitability) return '';
  const groups = suitabilityGroups(suitability);
  const chips = Object.entries(groups).filter(([, labels]) => labels.length).map(([verdict, labels]) =>
    `<div class="chip-row"><span class="chip-row-label">${VERDICT_LABELS[verdict]}</span>${labels.map((label) => `<span class="verdict-chip is-${verdict}">${escHtml(label)}</span>`).join('')}</div>`).join('');
  const reasons = suitability.categories.map((item) => `<li><strong>${escHtml(item.label)}</strong> (${VERDICT_LABELS[item.verdict].toLowerCase()}): ${escHtml(item.examples)}<ul>${item.reasons.map((reason) => `<li>${escHtml(reason)}</li>`).join('')}</ul></li>`).join('');
  const info = suitability.enoughInformation ? ''
    : `<p class="goal-meta is-warn">Not enough information: cautious values were assumed for ${escHtml(suitability.assumedLabels.join(', '))}. Add details to see a fuller picture.</p>`;
  return `<div class="goal-section"><h4>Kinds of investment that fit the timeline and risk</h4>${chips}${info}
    <details class="goal-scenarios"><summary>Why</summary><ul class="goal-notes">${reasons}</ul><p class="goal-meta">${escHtml(suitability.note)}</p></details></div>`;
}

function followUpsBlock(goal) {
  const items = goal.followUps || [];
  if (!items.length) return '';
  return `<div class="goal-section"><h4>Still to answer</h4><ul class="goal-notes">${items.map((item) => `<li>${escHtml(item.question)}${item.required ? ' <em>(needed)</em>' : ''}</li>`).join('')}</ul>
    <button class="table-action" type="button" data-edit-goal="${escHtml(goal.id)}">Add details</button></div>`;
}

function mappingBlock(goal) {
  const mapping = goal.mapping;
  if (!mapping || !mapping.available || !(mapping.holdings.length || mapping.missing.length)) return '';
  const rows = mapping.holdings.map((item) => `<li>${escHtml(item.name)} · ${item.pct}% = ${amount(item.valueInr, 'INR')}
    <span class="verdict-chip is-${item.review.verdict}" title="${escHtml(item.review.reason)}">${escHtml(item.review.verdict.replace('_', ' '))}</span></li>`).join('');
  const missing = mapping.missing.map((item) => `<li>${escHtml(item.name)} · ${item.pct}%: value unknown. ${escHtml(item.reason)}</li>`).join('');
  const notes = [];
  if (mapping.concentration?.warning) notes.push(`${mapping.concentration.name} is ${mapping.concentration.sharePct}% of what is assigned to this goal.`);
  if (mapping.overlap?.status === 'not_assessable') notes.push(`Overlap between holdings: ${mapping.overlap.reason}`);
  return `<div class="goal-section"><h4>Assigned holdings (snapshot ${escHtml(formatDate(mapping.snapshotDate))}${mapping.stale ? ', old' : ''})</h4>
    <ul class="goal-notes">${rows}${missing}</ul>${notes.map((note) => `<p class="goal-meta">${escHtml(note)}</p>`).join('')}</div>`;
}

// A total tile: the first currency as the value and any others beside it, never added together.
function totalTile(label, totals, sub) {
  const [first, ...rest] = totals;
  if (!first) return statTile(label, '—', sub);
  return statTile(label, amountShort(first[1], first[0]), rest.length ? `+ ${formatTotals(rest)}, kept separate` : sub);
}

function goalStats(goals) {
  const active = goals.filter((goal) => !isDraft(goal));
  const onTrack = active.filter(goalIsOnTrack).length;
  const drafts = goals.length - active.length;
  return `<div class="stat-grid">
    ${statTile('On track', `${onTrack} of ${active.length}`, drafts ? `${drafts} draft${drafts === 1 ? '' : 's'} not counted` : 'Under each goal\'s own assumption', onTrack === active.length ? 'is-up' : '')}
    ${totalTile('Total targets', totalsByCurrency(goals.map((goal) => ({ currency: goal.currency, target: headlineTarget(goal).amount })), 'target'), 'Needed by each goal\'s date')}
    ${totalTile('Earmarked so far', totalsByCurrency(goals, 'savedAmount'), 'Amounts you entered')}
    ${totalTile('Contributing monthly', totalsByCurrency(goals, 'monthlyContribution'), 'Planned, across goals')}
  </div>`;
}

async function renderGoals() {
  const isCurrent = startRender('goals');
  const body = document.getElementById('goals-body');
  body.innerHTML = loadingBlock();
  let data;
  try {
    data = await getGoalsData();
  } catch (error) {
    if (isCurrent()) body.innerHTML = errorBlock(error) + '<button class="gate-secondary inline-button" type="button" data-retry-goals>Try again</button>';
    return;
  }
  if (!isCurrent()) return;
  const goals = data.goals || [];
  document.getElementById('add-goal').hidden = goals.length >= MAX_GOALS;
  renderSavedContext(goals);
  renderGoalInvestments(goals, isCurrent);
  if (!goals.length) {
    body.innerHTML = notice('No goals yet', 'Add what you are saving for, such as a home, your child\'s education or retirement, to see how far your planned contributions get you by the date you choose.',
      '<button class="rail-button inline-button" type="button" data-add-goal>Add your first goal</button>');
    return;
  }
  const fundNote = data.fundData && !data.fundData.available ? `<p class="goal-meta">No fund shortlist: ${escHtml(data.fundData.reason)}</p>` : '';
  const summary = data.portfolio?.available && (data.portfolio.underfunded || []).length
    ? `<p class="goal-meta is-warn">Short of target after assigned holdings: ${escHtml(data.portfolio.underfunded.join(', '))}.</p>` : '';
  body.innerHTML = `${goalStats(goals)}${summary}
    <div class="goal-grid">${goals.map((goal, index) => goalCard(goal, GOAL_TONES[index % GOAL_TONES.length])).join('')}</div>
    ${fundNote}
    ${promptChips([['Review my goals', 'Review my goals: which are on track, which need more, and which kinds of investment fit each goal\'s timeline and risk?']])}`;
}

function goalCard(goal, tone) {
  const plan = goal.plan || {};
  const cur = goal.currency;
  const draft = isDraft(goal);
  const [label, pill] = draft ? ['Draft', 'is-behind'] : goalStatus(plan);
  const progress = Number.isFinite(plan.progressPct) ? plan.progressPct : 0;
  const assumption = goal.assumedReturnPct == null ? 'no return assumed' : `${goal.assumedReturnPct}% a year assumed`;
  const ask = `Explain the plan for my goal "${goal.name}": is it feasible, what are my options, and which kinds of investment suit its timeline and risk?`;
  const mapped = goal.mapping?.counted && plan.mappedAmount
    ? ` · + ${amountShort(plan.mappedAmount, cur)} from ${goal.mapping.holdings.length} assigned holding${goal.mapping.holdings.length === 1 ? '' : 's'} (snapshot ${escHtml(formatDate(goal.mapping.snapshotDate))})` : '';
  const headline = headlineTarget(goal);
  const target = headline.amount != null && goal.targetDate
    ? `<strong>${amountShort(headline.amount, cur)}</strong> by ${escHtml(formatDate(goal.targetDate))}${goal.targetDateFrom === 'horizon' && goal.horizonYears ? ` (set as ${goal.horizonYears} years)` : ''}${cur !== 'INR' ? ` · ${escHtml(cur)}` : ''}`
    : '<strong>Target not set yet</strong>';
  const feasibility = plan.feasibility && plan.feasibility.status !== 'insufficient_data' ? `<p class="goal-advice">${escHtml(plan.feasibility.summary)}</p>` : '';
  return `<article class="goal-card ${draft ? 'is-draft' : ''}">
    <header>${goalIconHtml(goal, tone, 'insight-icon goal-card-icon')}<h3>${escHtml(goal.name)}</h3><span class="pill ${pill}">${escHtml(label)}</span></header>
    <p class="goal-target">${target}</p>
    ${headline.todayMoney != null ? `<p class="goal-meta">${amountShort(headline.todayMoney, cur)} in today's money, raised by ${escHtml(headline.inflationPct)}% a year of inflation</p>` : ''}
    <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.toFixed(0)}" aria-label="Counted so far"><span style="width:${progress.toFixed(1)}%"></span></div>
    <p class="goal-meta">${amountShort(goal.savedAmount, cur)} earmarked${mapped} (${percent(progress)}) · ${amount(goal.monthlyContribution, cur)} a month · ${escHtml(assumption)}</p>
    <p class="goal-advice">${escHtml(goalSummary(plan))}</p>
    ${feasibility}
    ${alternativesList(plan)}
    ${suitabilityBlock(goal.suitability)}
    ${mappingBlock(goal)}
    ${followUpsBlock(goal)}
    ${goal.notes ? `<p class="goal-meta goal-note">${escHtml(goal.notes)}</p>` : ''}
    ${scenarioDetails(plan)}
    <p class="goal-meta">Saved ${escHtml(timeAgo(goal.updatedAt))} by you${goal.migratedFrom ? ' · moved from your earlier goal list (target date set to 31 Dec)' : ''}</p>
    <div class="goal-actions">
      <button class="table-action" type="button" data-edit-goal="${escHtml(goal.id)}">Edit</button>
      <button class="table-action" type="button" data-delete-goal="${escHtml(goal.id)}">Delete</button>
      <button class="table-action" type="button" data-agent="portfolio" data-prompt="${escHtml(ask)}">Ask AI</button>
    </div>
  </article>`;
}

/* Your investments and goals */
async function renderGoalInvestments(goals, isCurrent) {
  const host = document.getElementById('goal-investments-body');
  if (!goals.length) { host.innerHTML = ''; return; }
  host.innerHTML = loadingBlock();
  let data;
  try {
    data = await getAllocations();
  } catch (error) {
    if (isCurrent()) host.innerHTML = errorBlock(error);
    return;
  }
  if (!isCurrent()) return;
  if (!data.available) {
    host.innerHTML = notice('Your investments and goals', data.reason || 'Holdings are not available.');
    return;
  }
  const rows = data.holdings.map((item) => {
    const chips = item.goals.map((goal) => `<span class="verdict-chip">${escHtml(goal.goalName)} ${goal.pct}%</span>`).join(' ');
    const general = item.generalWealthPct ? `<span class="verdict-chip">General ${item.generalWealthPct}%</span>` : '';
    return `<tr>
      <td>${escHtml(item.name)}<br><small class="goal-meta">${escHtml(assetTypeLabel(item.assetType))}${item.broker ? ` · ${escHtml(item.broker)}` : ''}</small></td>
      <td>${amount(item.currentValueInr, 'INR')}</td>
      <td class="alloc-goals">${chips || '<span class="goal-meta">None</span>'} ${general}</td>
      <td>${item.unassignedPct}%</td>
      <td><button class="table-action" type="button" data-assign-holding="${escHtml(item.key)}">Assign</button></td>
    </tr>`;
  }).join('');
  const missing = data.missing.length
    ? `<p class="goal-meta is-warn">Assigned but not in the latest snapshot (value unknown, not counted): ${data.missing.map((item) => escHtml(item.name)).join(', ')}.</p>` : '';
  const unassigned = data.holdings.filter((item) => item.unassignedPct > 0);
  host.innerHTML = panel('Your investments and goals', `
    <p class="goal-meta">Assign what you already hold to a goal. Only INR holdings count toward an INR goal, and a holding is never counted twice. This is bookkeeping: nothing is bought or sold. Snapshot ${escHtml(formatDate(data.asOfDate))}${data.stale ? ' (old)' : ''}.</p>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>Holding</th><th>Value</th><th>Assigned to</th><th>Unassigned</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="goal-meta">${unassigned.length} of ${data.holdings.length} holdings have some of their value unassigned.</p>${missing}`);
}

/* Goal editor */
function numberOrNull(input) {
  return input.value.trim() === '' ? null : Number(input.value);
}

function textOrNull(input) {
  return input.value || null;
}

function whenMode() {
  return goalForm.querySelector('input[name="goal-when"]:checked').value;
}

// The date field or the years field is shown, never both; only the shown one is required.
function applyWhenMode() {
  const horizon = whenMode() === 'horizon';
  goalFields.date.hidden = horizon;
  goalFields.horizon.hidden = !horizon;
  goalFields.date.required = !horizon;
  goalFields.horizon.required = horizon;
}

function readGoalForm(status = 'active') {
  const horizon = whenMode() === 'horizon';
  const target = numberOrNull(goalFields.target);
  const years = numberOrNull(goalFields.horizon);
  return {
    name: goalFields.name.value.trim(),
    kind: goalFields.kind.value,
    status,
    currency: goalFields.currency.value,
    // A draft keeps no date the user did not set; an active goal saves the date shown, which the form requires.
    targetDate: horizon || (status === 'draft' && !goalDateChosen) ? null : (goalFields.date.value || null),
    horizonYears: horizon && years != null ? Math.round(years) : null,
    targetAmount: target == null ? null : Math.round(target),
    savedAmount: Math.round(Number(goalFields.saved.value) || 0),
    monthlyContribution: Math.round(Number(goalFields.monthly.value) || 0),
    assumedReturnPct: numberOrNull(goalFields.rate),
    inflationPct: numberOrNull(goalFields.inflation),
    flexibility: textOrNull(goalFields.flexibility),
    liquidityNeed: textOrNull(goalFields.liquidityNeed),
    riskTolerance: textOrNull(goalFields.riskTolerance),
    lossCapacity: textOrNull(goalFields.lossCapacity),
    notes: goalFields.notes.value.trim() || null,
  };
}

function planInputsValid() {
  return [goalFields.date, goalFields.horizon, goalFields.target, goalFields.saved, goalFields.monthly, goalFields.rate, goalFields.inflation]
    .every((input) => input.hidden || input.checkValidity());
}

function setGoalPreview(text, tone = '') {
  const preview = document.getElementById('goal-preview');
  preview.className = `goal-preview ${tone}`;
  preview.textContent = text;
}

// The preview asks the server, so it shows exactly what the saved goal will show.
async function renderGoalPreview() {
  const seq = ++goalPreviewSeq;
  const draft = readGoalForm();
  const hasWhen = Boolean(draft.targetDate || draft.horizonYears);
  if (!hasWhen || !(draft.targetAmount > 0) || !planInputsValid()) {
    setGoalPreview(draft.targetAmount > 0 || hasWhen ? 'Fill in valid amounts and a date to see the projection.' : '');
    return;
  }
  setGoalPreview('Calculating…', 'is-loading');
  const { name, kind, notes, status, ...inputs } = draft;
  for (const key of Object.keys(inputs)) if (inputs[key] === null) delete inputs[key];
  // An edited goal's assigned holdings count toward the preview, as they do on the saved goal.
  if (editingGoalId) inputs.goalId = editingGoalId;
  try {
    const plan = await fetchJson('/api/goals/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(inputs),
    });
    if (seq !== goalPreviewSeq) return;
    const groups = suitabilityGroups(plan.suitability);
    const lines = [goalSummary(plan), plan.feasibility?.summary];
    if (plan.mappedAmount) lines.push(`Includes ${amount(plan.mappedAmount, 'INR')} from holdings assigned to this goal.`);
    // Warnings about the return assumption itself belong next to the projection that uses it.
    lines.push(...(plan.warnings || []).filter((warning) => /high return|Equity is unsuitable/.test(warning)));
    if (groups.unsuitable.length) lines.push(`Unsuitable for this timeline and risk: ${groups.unsuitable.join(', ')}.`);
    if (plan.suitability && !plan.suitability.enoughInformation) lines.push('Add the flexibility and risk answers for a fuller suitability check.');
    if (plan.followUps?.length) lines.push(`Still open: ${plan.followUps.map((item) => item.question).join(' ')}`);
    setGoalPreview(lines.filter(Boolean).join(' '), goalStatus(plan)[1] === 'is-connected' ? 'is-up' : 'is-behind');
  } catch (error) {
    if (seq === goalPreviewSeq) setGoalPreview(`Could not calculate the projection: ${error.message}`, 'is-behind');
  }
}

function scheduleGoalPreview() {
  clearTimeout(goalPreviewTimer);
  goalPreviewTimer = setTimeout(renderGoalPreview, 300);
}

function openGoalDialog(goal = null) {
  if (!sessionReady()) return;
  editingGoalId = goal?.id || null;
  const today = isoDay(new Date());
  document.getElementById('goal-dialog-title').textContent = editingGoalId ? (isDraft(goal) ? 'Finish draft goal' : 'Edit goal') : 'New goal';
  goalFields.name.value = goal?.name || '';
  goalFields.kind.value = goalIcon(goal || {});
  goalKindChosen = Boolean(goal?.kind);
  goalDateChosen = Boolean(goal?.targetDate);
  goalFields.currency.value = goal?.currency || 'INR';
  // A saved goal opens with its date, even when it was entered as a number of years.
  goalForm.querySelector('input[name="goal-when"][value="date"]').checked = true;
  goalFields.horizon.value = '';
  applyWhenMode();
  // Saved goals may already be past due; new ones start today at the earliest.
  goalFields.date.min = goal?.targetDate && goal.targetDate < today ? goal.targetDate : today;
  goalFields.date.value = goal ? (goal.targetDate || '') : yearsFromToday(5);
  goalFields.target.value = goal?.targetAmount ?? '';
  goalFields.saved.value = goal?.savedAmount ?? 0;
  goalFields.monthly.value = goal?.monthlyContribution ?? 0;
  goalFields.rate.value = goal?.assumedReturnPct ?? '';
  goalFields.inflation.value = goal?.inflationPct ?? '';
  for (const field of ['flexibility', 'liquidityNeed', 'riskTolerance', 'lossCapacity']) goalFields[field].value = goal?.[field] || '';
  goalForm.querySelector('.goal-risk-inputs').open = ['flexibility', 'liquidityNeed', 'riskTolerance', 'lossCapacity', 'inflationPct']
    .some((field) => goal?.[field] != null);
  goalFields.notes.value = goal?.notes || '';
  document.getElementById('goal-status').textContent = '';
  document.getElementById('goal-delete').hidden = !editingGoalId;
  setGoalPreview('');
  renderGoalPreview();
  if (!goalDialog.open) goalDialog.showModal();
  (goal?.name ? goalFields.target : goalFields.name).focus();
}

function goalsChanged() {
  pageCache.delete('goals');
  pageCache.delete('allocations');
  document.dispatchEvent(new CustomEvent('ca:goals'));
}

async function deleteGoal(goal) {
  if (!goal || !window.confirm(`Delete the goal "${goal.name}"? Decisions linked to it are kept.`)) return false;
  await fetchJson(`/api/goals/${encodeURIComponent(goal.id)}`, { method: 'DELETE' });
  goalsChanged();
  toast('Goal deleted.');
  return true;
}

document.addEventListener('ca:goals', () => {
  if (currentRoute === 'goals' && sessionReady()) renderGoals();
});

document.getElementById('add-goal').addEventListener('click', () => openGoalDialog());
goalDialog.querySelectorAll('[data-close-goal]').forEach((button) => button.addEventListener('click', () => goalDialog.close()));
goalDialog.addEventListener('close', () => { clearTimeout(goalPreviewTimer); goalPreviewSeq += 1; });
goalForm.addEventListener('change', (event) => {
  if (event.target === goalFields.date) goalDateChosen = true;
  if (event.target.name === 'goal-when') {
    applyWhenMode();
    scheduleGoalPreview();
  }
});
goalForm.addEventListener('input', (event) => {
  if (event.target === goalFields.kind) goalKindChosen = true;
  if (event.target === goalFields.date) goalDateChosen = true;
  if (event.target === goalFields.name && !goalKindChosen) goalFields.kind.value = goalKind(goalFields.name.value);
  document.getElementById('goal-status').textContent = '';
  if (event.target !== goalFields.name && event.target !== goalFields.notes) scheduleGoalPreview();
});

goalForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  // "Save as draft" skips the required-field check; only a name is needed.
  const status = event.submitter?.value === 'draft' ? 'draft' : 'active';
  const message = document.getElementById('goal-status');
  if (status === 'draft' ? !goalFields.name.value.trim() : !goalForm.reportValidity()) {
    if (status === 'draft') {
      message.textContent = 'Give the goal a name.';
      goalFields.name.focus();
    }
    return;
  }
  const submits = [...goalForm.querySelectorAll('[type="submit"]')];
  const labels = submits.map((button) => button.textContent);
  submits.forEach((button) => { button.disabled = true; });
  if (event.submitter) event.submitter.textContent = 'Saving…';
  try {
    const url = editingGoalId ? `/api/goals/${encodeURIComponent(editingGoalId)}` : '/api/goals';
    await fetchJson(url, {
      method: editingGoalId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(readGoalForm(status)),
    });
    goalDialog.close();
    goalsChanged();
    toast(status === 'draft' ? 'Draft saved.' : editingGoalId ? 'Goal updated.' : 'Goal added.');
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submits.forEach((button, index) => { button.disabled = false; button.textContent = labels[index]; });
  }
});

document.getElementById('goal-delete').addEventListener('click', async () => {
  try {
    const goal = (await getGoals()).find((item) => item.id === editingGoalId);
    if (await deleteGoal(goal)) goalDialog.close();
  } catch (error) {
    document.getElementById('goal-status').textContent = error.message;
  }
});

document.addEventListener('click', async (event) => {
  const add = event.target.closest('[data-add-goal]');
  const edit = event.target.closest('[data-edit-goal]');
  const remove = event.target.closest('[data-delete-goal]');
  if (event.target.closest('[data-retry-goals]')) renderGoals();
  if (add) openGoalDialog();
  if (!edit && !remove) return;
  try {
    const goals = await getGoals();
    if (edit) openGoalDialog(goals.find((goal) => goal.id === edit.dataset.editGoal));
    if (remove) await deleteGoal(goals.find((item) => item.id === remove.dataset.deleteGoal));
  } catch (error) {
    toast(error.message);
  }
});

/* Assign a holding to goals */
const allocationDialog = document.getElementById('allocation-dialog');
const allocationForm = document.getElementById('allocation-form');
let allocationHolding = null;

function allocationPcts() {
  const goals = [...allocationForm.querySelectorAll('[data-alloc-goal]')].map((input) => Number(input.value));
  return { goals, general: Number(allocationForm.querySelector('#alloc-general').value) };
}

function updateAllocationRemaining() {
  const { goals, general } = allocationPcts();
  const left = allocationRemaining(goals, general);
  const text = document.getElementById('allocation-remaining');
  text.textContent = left < 0 ? `${-left}% over. A holding can be assigned at most 100% in total.` : `${left}% of this holding stays unassigned.`;
  text.className = `goal-preview ${left < 0 ? 'is-behind' : ''}`;
}

async function openAllocationDialog(key) {
  const [data, goals] = await Promise.all([getAllocations(), getGoals()]);
  const holding = data.holdings.find((item) => item.key === key);
  if (!holding) return;
  allocationHolding = holding;
  document.getElementById('allocation-title').textContent = `Assign ${holding.name}`;
  document.getElementById('allocation-sub').textContent = `Worth ${amount(holding.currentValueInr, 'INR')} in your latest snapshot. Give each goal a share in whole percent; a share is valued from the snapshot and counted once.`;
  const current = Object.fromEntries(holding.goals.map((item) => [item.goalId, item.pct]));
  document.getElementById('allocation-fields').innerHTML = goals.map((goal) => `<label class="field"><span>${escHtml(goal.name)}${goal.currency && goal.currency !== 'INR' ? ` (${escHtml(goal.currency)}, not counted)` : ''} %</span>
      <input type="number" min="0" max="100" step="1" inputmode="numeric" data-alloc-goal="${escHtml(goal.id)}" value="${current[goal.id] || ''}" /></label>`).join('')
    + `<label class="field"><span>General wealth (no goal) %</span><input id="alloc-general" type="number" min="0" max="100" step="1" inputmode="numeric" value="${holding.generalWealthPct || ''}" /></label>`;
  document.getElementById('allocation-status').textContent = '';
  updateAllocationRemaining();
  if (!allocationDialog.open) allocationDialog.showModal();
}

allocationForm.addEventListener('input', () => {
  document.getElementById('allocation-status').textContent = '';
  updateAllocationRemaining();
});
allocationDialog.querySelectorAll('[data-close-allocation]').forEach((button) => button.addEventListener('click', () => allocationDialog.close()));
allocationForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = document.getElementById('allocation-status');
  const submit = allocationForm.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const goals = [...allocationForm.querySelectorAll('[data-alloc-goal]')]
      .map((input) => ({ goalId: input.dataset.allocGoal, pct: Math.round(Number(input.value) || 0) })).filter((item) => item.pct > 0);
    await fetchJson('/api/goals/allocations', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ holdingKey: allocationHolding.key, goals, generalWealthPct: Math.round(allocationPcts().general || 0) }),
    });
    allocationDialog.close();
    goalsChanged();
    toast('Assignment saved.');
  } catch (error) {
    // The server is authoritative: its message (for example over 100%) is shown as is.
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.addEventListener('click', (event) => {
  const assign = event.target.closest('[data-assign-holding]');
  if (assign) openAllocationDialog(assign.dataset.assignHolding).catch((error) => toast(error.message));
});

/* Saved context: what the assistant may use, and the decisions the user recorded */
const PREFERENCE_LABELS = {
  riskAppetite: 'Risk appetite', investmentHorizonYears: 'Investment horizon (years)', primaryGoal: 'Primary goal',
  monthlyInvestmentInr: 'Monthly investment budget (₹)', ageBand: 'Age band',
};

function decisionRow(decision, goalNames) {
  const when = decision.decidedOn ? formatDate(decision.decidedOn) : `recorded ${timeAgo(decision.createdAt)}`;
  const goal = decision.goalId ? ` · ${goalNames[decision.goalId] ? `for “${escHtml(goalNames[decision.goalId])}”` : 'for a deleted goal'}` : '';
  return `<li class="context-item"><div><p>${escHtml(decision.text)}</p><small>${escHtml(when)}${goal}</small></div>
    <button class="table-action" type="button" data-delete-decision="${escHtml(decision.id)}">Delete</button></li>`;
}

function observedLines(observed) {
  const lines = [];
  const snapshot = observed?.latestSnapshot;
  lines.push(snapshot ? `Latest portfolio snapshot from INDmoney: ${formatDate(snapshot.asOfDate)}.` : 'No portfolio snapshot saved yet.');
  const cas = observed?.casStatements;
  if (cas?.count) lines.push(`${cas.count} CAS statement${cas.count === 1 ? '' : 's'}; the latest covers up to ${formatDate(cas.latest.periodTo)}.`);
  return lines;
}

function savedContextHtml(data, goals) {
  const confirmed = data.confirmed || {};
  const fields = confirmed.preferences?.fields || {};
  const preferences = Object.entries(PREFERENCE_LABELS).filter(([key]) => fields[key] != null && fields[key] !== '')
    .map(([key, label]) => `<li><span>${escHtml(label)}</span><strong>${escHtml(String(fields[key]))}</strong></li>`).join('');
  const goalNames = Object.fromEntries(goals.map((goal) => [goal.id, goal.name]));
  const decisions = (confirmed.decisions || []).slice().reverse();
  const goalOptions = goals.map((goal) => `<option value="${escHtml(goal.id)}">${escHtml(goal.name)}</option>`).join('');
  const hasAnything = preferences || decisions.length || goals.length;
  return panel('What the assistant can use', `
    <p class="page-muted">Only what you entered is saved, and the assistant sees an item only when your question is about it. CA Mode never saves details the assistant infers from your chats, and goal projections are recalculated each time, never stored.</p>
    <div class="context-grid">
      <section aria-labelledby="context-profile-title">
        <h3 id="context-profile-title" class="context-title">Profile details <small>${confirmed.preferences?.updatedAt ? `saved ${escHtml(timeAgo(confirmed.preferences.updatedAt))}` : 'entered by you'}</small></h3>
        ${preferences ? `<ul class="context-facts">${preferences}</ul>` : '<p class="page-muted">None saved.</p>'}
        <button class="table-action" type="button" data-open-profile>Edit profile details</button>
      </section>
      <section aria-labelledby="context-decisions-title">
        <h3 id="context-decisions-title" class="context-title">Decisions you recorded <small>${decisions.length} of ${data.maxDecisions || 50}</small></h3>
        ${decisions.length ? `<ul class="context-list">${decisions.map((item) => decisionRow(item, goalNames)).join('')}</ul>` : '<p class="page-muted">None yet. Record a decision you have made, such as keeping six months of expenses in a liquid fund, so the assistant can take it into account.</p>'}
        <form id="decision-form" class="decision-form" novalidate>
          <label class="field field-wide"><span>Decision</span><textarea id="decision-text" maxlength="500" rows="2" required placeholder="In your own words"></textarea></label>
          <label class="field"><span>Decided on (optional)</span><input id="decision-date" type="date" min="2000-01-01" max="${isoDay(new Date())}" /></label>
          ${goalOptions ? `<label class="field"><span>Goal (optional)</span><select id="decision-goal"><option value="">None</option>${goalOptions}</select></label>` : ''}
          <p id="decision-status" class="dialog-status is-error" role="alert"></p>
          <button class="rail-button inline-button" type="submit">Record decision</button>
        </form>
      </section>
      <section aria-labelledby="context-observed-title">
        <h3 id="context-observed-title" class="context-title">Observed data <small>not entered by you</small></h3>
        <ul class="goal-notes">${observedLines(data.observed).map((line) => `<li>${escHtml(line)}</li>`).join('')}</ul>
      </section>
    </div>
    ${hasAnything ? '<button class="gate-secondary danger-text" type="button" data-clear-context>Remove all goals, decisions and profile details</button>' : ''}`,
  { className: 'saved-context' });
}

async function renderSavedContext(goals) {
  const isCurrent = startRender('saved-context');
  const body = document.getElementById('saved-context-body');
  // Keep the current panel while refreshing after a change, so the page does not jump.
  if (!body.querySelector('.saved-context')) body.innerHTML = loadingBlock();
  try {
    const [data, decisions] = await Promise.all([fetchJson('/api/context'), fetchJson('/api/decisions')]);
    if (isCurrent()) body.innerHTML = savedContextHtml({ ...data, maxDecisions: decisions.maxDecisions }, goals);
  } catch (error) {
    if (isCurrent()) body.innerHTML = errorBlock(error);
  }
}

document.addEventListener('submit', async (event) => {
  if (event.target.id !== 'decision-form') return;
  event.preventDefault();
  const form = event.target;
  const status = document.getElementById('decision-status');
  if (!form.reportValidity()) return;
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  status.textContent = '';
  try {
    await fetchJson('/api/decisions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: document.getElementById('decision-text').value.trim(),
        decidedOn: document.getElementById('decision-date').value || null,
        goalId: document.getElementById('decision-goal')?.value || null,
      }),
    });
    toast('Decision recorded.');
    renderSavedContext(await getGoals());
  } catch (error) {
    status.textContent = error.message;
    submit.disabled = false;
  }
});

document.addEventListener('click', async (event) => {
  const removeDecision = event.target.closest('[data-delete-decision]');
  const clearAll = event.target.closest('[data-clear-context]');
  if (event.target.closest('[data-open-profile]')) openProfileDialog();
  if (!removeDecision && !clearAll) return;
  try {
    if (removeDecision) {
      if (!window.confirm('Delete this decision?')) return;
      await fetchJson(`/api/decisions/${encodeURIComponent(removeDecision.dataset.deleteDecision)}`, { method: 'DELETE' });
      toast('Decision deleted.');
      renderSavedContext(await getGoals());
    } else {
      if (!window.confirm('Remove every saved goal, recorded decision and profile detail? Chats, snapshots and statements are kept. This cannot be undone.')) return;
      await fetchJson('/api/context', { method: 'DELETE' });
      toast('Saved goals, decisions and profile details removed.');
      goalsChanged();
    }
  } catch (error) {
    toast(error.message);
  }
});

/* ── Reports ────────────────────────────────────────────────────────── */
let reportPoints = [];

async function renderReports() {
  const isCurrent = startRender('reports');
  const body = document.getElementById('reports-body');
  const access = portfolioAccess();
  if (access !== 'ok') {
    setHtml(body, accessNotice(access) + downloadsPanel(false));
    return;
  }
  setHtml(body, loadingBlock());
  let data;
  try {
    data = await getHistory(365);
  } catch (error) {
    if (isCurrent()) setHtml(body, errorBlock(error));
    return;
  }
  if (!isCurrent()) return;
  reportPoints = (data.history || []).filter((point) => Number.isFinite(point.networthInr));
  if (!reportPoints.length) {
    setHtml(body, noSnapshotNotice() + downloadsPanel(false));
    return;
  }
  const latest = reportPoints[reportPoints.length - 1];
  const periods = [[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 3 months'], [180, 'Last 6 months'], [365, 'Last 12 months']].map(([days, label]) => {
    const cutoff = new Date(`${latest.date}T00:00:00`);
    cutoff.setDate(cutoff.getDate() - days);
    const base = reportPoints.find((point) => new Date(`${point.date}T00:00:00`) >= cutoff);
    // A period only counts when the history reaches back close to its start.
    const lateDays = base ? (new Date(`${base.date}T00:00:00`) - cutoff) / 86_400_000 : Infinity;
    if (!base || base.date === latest.date || lateDays > Math.max(3, days * 0.1)) return `<tr><td>${label}</td><td colspan="4" class="page-muted">Not enough history yet</td></tr>`;
    const change = latest.networthInr - base.networthInr;
    return `<tr><td>${label}</td><td>${formatDate(base.date)}<small>${money(base.networthInr)}</small></td><td>${money(latest.networthInr)}</td><td class="${toneOf(change)}">${signedMoney(change)}</td><td class="${toneOf(change)}">${percent(base.networthInr ? change / base.networthInr * 100 : NaN, { signed: true })}</td></tr>`;
  }).join('');
  const values = reportPoints.map((point) => point.networthInr);
  const high = reportPoints[values.indexOf(Math.max(...values))];
  const low = reportPoints[values.indexOf(Math.min(...values))];
  const months = new Map();
  for (const point of reportPoints) months.set(point.date.slice(0, 7), point);
  let previous = null;
  const monthRows = [...months.entries()].map(([month, point]) => {
    const change = previous ? point.networthInr - previous.networthInr : NaN;
    const pct = previous?.networthInr ? change / previous.networthInr * 100 : NaN;
    previous = point;
    const label = new Date(`${month}-01T00:00:00`).toLocaleDateString([], { month: 'long', year: 'numeric' });
    return `<tr><td>${label}</td><td>${money(point.networthInr)}<small>on ${formatDate(point.date)}</small></td><td class="${toneOf(change)}">${Number.isFinite(change) ? signedMoney(change) : '—'}</td><td class="${toneOf(change)}">${percent(pct, { signed: true })}</td></tr>`;
  }).reverse().join('');
  setHtml(body, `
    <div class="stat-grid stat-grid-3">
      ${statTile('Latest net worth', moneyShort(latest.networthInr), formatDate(latest.date))}
      ${statTile('12-month high', moneyShort(high.networthInr), formatDate(high.date))}
      ${statTile('12-month low', moneyShort(low.networthInr), formatDate(low.date))}
    </div>
    ${panel('Net worth over time', reportPoints.length > 1 ? chartHtml({ type: 'line', title: 'Net worth, last 12 months', unit: '₹', series: netWorthSeries(reportPoints) }) : '<p class="page-muted">The chart appears once there are two days of snapshots.</p>')}
    ${panel('Change by period', `<div class="table-scroll"><table class="data-table"><thead><tr><th>Period</th><th>From</th><th>To</th><th>Change</th><th>%</th></tr></thead><tbody>${periods}</tbody></table></div>`)}
    ${panel('Month-end net worth', `<div class="table-scroll"><table class="data-table"><thead><tr><th>Month</th><th>Net worth</th><th>Change</th><th>%</th></tr></thead><tbody>${monthRows}</tbody></table></div>`)}
    ${downloadsPanel(true)}`);
}

function downloadsPanel(hasSnapshots) {
  return panel('Downloads', `<div class="page-actions wrap">
      ${hasSnapshots ? '<button class="gate-secondary" type="button" data-download="history">Net worth history (CSV)</button><button class="gate-secondary" type="button" data-download="holdings">Holdings (CSV)</button>' : ''}
      <button class="gate-secondary" type="button" data-download="export">Export all my data</button>
    </div>
    <p class="page-muted">CSV files open in Excel or Google Sheets. The full export (JSON) includes chats, snapshots, goals and CAS holdings and asks for your PIN.</p>`);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-download]');
  if (!button) return;
  const today = new Date().toISOString().slice(0, 10);
  if (button.dataset.download === 'history') {
    downloadCsv(`ca-mode-net-worth-${today}.csv`, [['Date', 'Net worth (INR)'], ...reportPoints.map((point) => [point.date, point.networthInr])]);
  } else if (button.dataset.download === 'holdings') {
    try {
      const data = await getPortfolio();
      downloadCsv(`ca-mode-holdings-${today}.csv`, holdingsCsvRows(data.holdings || []));
    } catch (error) {
      toast(error.message);
    }
  } else {
    await openProfileDialog().catch(showError);
    dataZone.open = true;
    document.getElementById('export-pin').focus();
  }
});

document.getElementById('print-report').addEventListener('click', () => window.print());

/* ── Activity ───────────────────────────────────────────────────────── */
// Saved portfolio investigations. The server is the source of truth: the list is fetched on every
// visit, when the tab comes back into view or the network returns, and polled only while a run is
// in progress. Responses that arrive out of order are dropped, and a finished run is never replaced
// by an older copy (see reconcileInvestigations in trace.js).
const ACTIVITY_LIMIT = 50;
const activityBody = document.getElementById('activity-body');
const activityLive = document.getElementById('activity-live');
const activityState = {
  items: [], filter: 'all', loaded: false, record: null,
  listRequests: createSequencer(), detailRequests: createSequencer(), pollTimer: null, pollAttempt: 0,
};

function resetActivity() {
  stopActivityPolling();
  Object.assign(activityState, { items: [], filter: 'all', loaded: false, record: null, pollAttempt: 0 });
  activityLive.textContent = '';
}

function stopActivityPolling() {
  clearTimeout(activityState.pollTimer);
  activityState.pollTimer = null;
}

function scheduleActivityPoll(load) {
  stopActivityPolling();
  activityState.pollTimer = setTimeout(() => {
    activityState.pollTimer = null;
    // A hidden tab or another page stops polling; coming back reloads from the server.
    if (currentRoute === 'activity' && !document.hidden && sessionReady()) load();
  }, pollDelay(activityState.pollAttempt++));
}

function onActivityList() {
  return currentRoute === 'activity' && !currentSub;
}

async function renderActivity() {
  stopActivityPolling();
  activityState.pollAttempt = 0;
  if (currentSub) await loadActivityDetail(currentSub);
  else await loadActivityList();
}

async function loadActivityList({ quiet = false } = {}) {
  const ticket = activityState.listRequests.next();
  // Show what is already known straight away (e.g. coming back from a run), then refresh it.
  if (!quiet) {
    if (activityState.loaded) drawActivityList();
    else setHtml(activityBody, activitySkeleton());
  }
  let data;
  try {
    data = await fetchJson(`/api/investigations?limit=${ACTIVITY_LIMIT}`);
  } catch (error) {
    if (!activityState.listRequests.accept(ticket) || !onActivityList()) return;
    if (activityState.loaded) {
      // Keep the list on screen and say it may be out of date.
      activityLive.textContent = `Could not refresh: ${error.message}`;
      drawActivityList(error);
    } else {
      setHtml(activityBody, errorBlock(error) + '<button class="gate-secondary inline-button" type="button" data-activity-reload>Try again</button>');
    }
    return;
  }
  if (!activityState.listRequests.accept(ticket) || !onActivityList()) return;
  const before = new Map(activityState.items.map((item) => [item.id, item.status]));
  activityState.items = reconcileInvestigations(activityState.items, data.investigations || []);
  activityState.loaded = true;
  const finished = activityState.items.filter((item) => isActive({ status: before.get(item.id) }) && !isActive(item));
  if (finished.length) activityLive.textContent = finished.map((item) => `${investigationTitle(item)}: ${statusInfo(item.status).label}.`).join(' ');
  drawActivityList();
  if (activityState.items.some(isActive)) scheduleActivityPoll(() => loadActivityList({ quiet: true }));
}

// What had focus inside the list, so a redraw (filter change, poll) does not drop keyboard users.
function activityFocus() {
  const active = document.activeElement;
  if (!active || !activityBody.contains(active)) return null;
  return {
    filter: active.dataset?.activityFilter,
    item: active.closest('[data-investigation-id]')?.dataset.investigationId,
    text: active.textContent.trim(),
  };
}

function restoreActivityFocus(focus) {
  if (!focus) return;
  let target = null;
  if (focus.filter) target = activityBody.querySelector(`[data-activity-filter="${focus.filter}"]`);
  else if (focus.item) {
    const row = [...activityBody.querySelectorAll('[data-investigation-id]')].find((element) => element.dataset.investigationId === focus.item);
    target = row && [...row.querySelectorAll('a, button')].find((element) => element.textContent.trim() === focus.text);
  }
  target?.focus();
}

function drawActivityList(refreshError = null) {
  const focus = activityFocus();
  const items = activityState.items;
  const stale = refreshError ? `<div class="page-error" role="alert">Could not refresh: ${escHtml(refreshError.message)} The list below may be out of date.</div>` : '';
  if (!items.length) {
    setHtml(activityBody, stale + (storageMode === 'private'
      ? notice('Private mode', 'Private mode keeps no snapshots or investigation history. Turn on saving from your profile to keep investigations here.')
      : notice('No investigations yet', 'An investigation compares two saved daily snapshots, checks the data and saves the evidence. Ask Portfolio Analyzer why your portfolio changed, or investigate the latest change now.')));
    return;
  }
  const chatIds = new Set(chats.map((chat) => chat.id));
  setHtml(activityBody, `${stale}<section class="panel activity-panel">${renderActivityList(items, { filter: activityState.filter, chatIds })}</section>`);
  restoreActivityFocus(focus);
}

async function loadActivityDetail(id) {
  const ticket = activityState.detailRequests.next();
  const back = '<a class="panel-link activity-back" href="#/activity">← All activity</a>';
  const shown = activityState.record?.id === id ? activityState.record : null;
  if (!shown) setHtml(activityBody, back + loadingBlock());
  let record;
  try {
    record = await fetchJson('/api/investigations/' + encodeURIComponent(id));
  } catch (error) {
    if (!activityState.detailRequests.accept(ticket) || currentSub !== id) return;
    setHtml(activityBody, back + errorBlock(error) + (shown ? renderInvestigationCard(shown, { chart: renderDataChart, linkToActivity: false, openSteps: true }) : ''));
    return;
  }
  if (!activityState.detailRequests.accept(ticket) || currentRoute !== 'activity' || currentSub !== id) return;
  record = newerRun(shown, record);
  if (isActive(shown) && !isActive(record)) activityLive.textContent = `Investigation ${statusInfo(record.status).label.toLowerCase()}.`;
  activityState.record = record;
  drawActivityDetail(record, back);
  if (isActive(record)) scheduleActivityPoll(() => loadActivityDetail(id));
}

function drawActivityDetail(record, back = '<a class="panel-link activity-back" href="#/activity">← All activity</a>') {
  const chatId = record.request?.chatId;
  const actions = [
    chatId && chats.some((chat) => chat.id === chatId)
      ? `<button class="gate-secondary small-button" type="button" data-open-chat="${escHtml(chatId)}">Open chat</button>` : '',
    canRetry(record) ? `<button class="gate-secondary small-button" type="button" data-retry-investigation="${escHtml(record.id)}">Run again</button>` : '',
  ].join('');
  const started = Number.isFinite(record.createdAt) ? new Date(record.createdAt).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '';
  const took = Number.isFinite(record.completedAt) && !isActive(record) ? formatDuration(record.completedAt - record.createdAt) : '';
  const meta = [started && `Started ${started}`, took && `took ${took}`, record.request?.trigger === 'agent' ? 'from a chat' : ''].filter(Boolean).join(' · ');
  setHtml(activityBody, `<div class="activity-detail-head">${back}${meta ? `<span class="page-muted">${escHtml(meta)}</span>` : ''}${actions ? `<div class="activity-actions">${actions}</div>` : ''}</div>
    ${renderInvestigationCard(record, { chart: renderDataChart, linkToActivity: false, openSteps: true })}`);
}

// Starts a run only when the user asks. Runs read saved snapshots and never write to INDmoney.
async function startInvestigation(button, baselineDate = null) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Investigating…';
  try {
    const record = await fetchJson('/api/investigations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(baselineDate ? { baselineDate } : {}),
    });
    if (record.saved) {
      activityState.record = record;
      navigate('activity', record.id);
    } else {
      // Private mode, or the result could not be saved: show it once, here.
      setHtml(activityBody, `<a class="panel-link activity-back" href="#/activity">← All activity</a>${renderInvestigationCard(record, { chart: renderDataChart, linkToActivity: false, openSteps: true })}`);
    }
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

document.getElementById('activity-run').addEventListener('click', (event) => startInvestigation(event.currentTarget));
document.getElementById('activity-refresh').addEventListener('click', () => {
  if (sessionReady()) renderActivity();
});

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-activity-filter], [data-activity-reload], [data-open-chat], [data-retry-investigation]');
  if (!target || target.disabled) return;
  if (target.matches('[data-activity-filter]')) {
    activityState.filter = target.dataset.activityFilter;
    drawActivityList();
  } else if (target.matches('[data-activity-reload]')) {
    renderActivity();
  } else if (target.matches('[data-open-chat]')) {
    navigate('chat');
    selectChat(target.dataset.openChat).catch((error) => toast(error.message));
  } else {
    const id = target.dataset.retryInvestigation;
    const run = activityState.record?.id === id ? activityState.record : activityState.items.find((item) => item.id === id);
    if (!run || !canRetry(run)) return;
    startInvestigation(target, run.request?.baselineDate ?? run.baselineDate ?? null);
  }
});

// Coming back to the tab or the network: reconcile with what the server saved meanwhile.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentRoute === 'activity' && sessionReady()) renderActivity();
});
window.addEventListener('online', () => {
  if (currentRoute === 'activity' && sessionReady()) renderActivity();
});

/* ── All chats ──────────────────────────────────────────────────────── */
function renderAllChats() {
  const container = document.getElementById('all-chats');
  const query = chatSearch.value.trim().toLowerCase();
  const visible = query ? chats.filter((chat) => (chat.title || 'New chat').toLowerCase().includes(query)) : chats;
  document.getElementById('chats-summary').textContent = `${chats.length} conversation${chats.length === 1 ? '' : 's'}. `
    + (storageMode === 'private' ? 'Private mode: they end when you sign out.' : 'Each is kept for 30 days after its last message.');
  container.innerHTML = '';
  if (!visible.length) {
    container.innerHTML = `<p class="page-muted">${query ? 'No chats match your search.' : 'No conversations yet.'}</p>`;
    return;
  }
  const groups = new Map();
  for (const chat of visible) {
    const day = relativeDay(chat.updatedAt);
    const group = day === 'Today' || day === 'Yesterday' ? day : day.endsWith('days ago') ? 'Earlier this week' : 'Older';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(chat);
  }
  for (const [label, items] of groups) {
    const heading = document.createElement('h2');
    heading.className = 'chat-group';
    heading.textContent = label;
    container.appendChild(heading);
    items.forEach((chat) => container.appendChild(chatRow(chat, { showAgent: true })));
  }
}

renderRoute();
