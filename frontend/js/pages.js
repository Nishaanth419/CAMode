/* ── Pages behind the sidebar ───────────────────────────────────────────
   Home, Portfolio Overview, Investments, Planning Tools, Market Insights,
   Goals, Reports and All chats. Loaded after app.js and uses its state
   (currentUser, isConnected, storageMode, chats) and helpers. */

const ROUTES = ['home', 'chat', 'portfolio', 'investments', 'planning', 'markets', 'goals', 'reports', 'chats'];
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

function clearPortfolioCache() {
  for (const key of [...pageCache.keys()]) if (key !== 'goals') pageCache.delete(key);
}

// One session reload at a time, however many requests found the session locked.
let sessionReload = null;
function reloadSession() {
  sessionReload ||= loadChats().finally(() => { sessionReload = null; });
  return sessionReload;
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
    throw new Error(data.error || (Array.isArray(data.detail) ? 'Check the values and try again.' : `HTTP ${response.status}`));
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
const getGoals = () => cached('goals', () => fetchJson('/api/goals').then((data) => data.goals || []));

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
    markets: renderMarkets, goals: renderGoals, reports: renderReports, chats: renderAllChats,
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
  for (const id of ['home-stats', 'home-chats', 'portfolio-body', 'investments-body', 'markets-body', 'goals-body', 'reports-body', 'all-chats']) {
    setHtml(document.getElementById(id), '');
  }
  for (const id of ['portfolio-asof', 'investments-asof', 'home-name']) document.getElementById(id).textContent = '';
  goalForm.hidden = true;
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
  const onTrack = goals.filter((goal) => goalProjection(goal).onTrack).length;
  if (!isCurrent()) return;
  stats.innerHTML = worth + pnl
    + statTile('Goals on track', goals.length ? `${onTrack} of ${goals.length}` : 'None yet', goals.length ? 'At your expected returns' : 'Add one on the Goals page')
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
        goal: { targetInr: Math.round(future), targetYear: new Date().getFullYear() + years, savedInr: Math.round(saved), monthlyInr: Math.ceil(monthly / 100) * 100, expectedReturnPct: rate },
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
  navigate('goals');
  openGoalForm({ name: '', ...result.goal });
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
const MAX_GOALS = 10;
const goalForm = document.getElementById('goal-form');
let editingGoalId = null;

async function renderGoals() {
  const isCurrent = startRender('goals');
  const body = document.getElementById('goals-body');
  body.innerHTML = loadingBlock();
  let goals;
  try {
    goals = await getGoals();
  } catch (error) {
    if (isCurrent()) body.innerHTML = errorBlock(error);
    return;
  }
  if (!isCurrent()) return;
  document.getElementById('add-goal').hidden = goals.length >= MAX_GOALS;
  if (!goals.length) {
    body.innerHTML = notice('No goals yet', 'Add what you are saving for, such as a home, your child\'s education or retirement, to see whether your monthly investment gets you there on time.',
      '<button class="rail-button inline-button" type="button" data-add-goal>Add your first goal</button>');
    return;
  }
  const projections = goals.map((goal) => goalProjection(goal));
  const totalTarget = goals.reduce((sum, goal) => sum + goal.targetInr, 0);
  const totalSaved = goals.reduce((sum, goal) => sum + (goal.savedInr || 0), 0);
  const totalMonthly = goals.reduce((sum, goal) => sum + (goal.monthlyInr || 0), 0);
  const onTrack = projections.filter((p) => p.onTrack).length;
  body.innerHTML = `
    <div class="stat-grid">
      ${statTile('On track', `${onTrack} of ${goals.length}`, 'At your expected returns', onTrack === goals.length ? 'is-up' : '')}
      ${statTile('Total targets', moneyShort(totalTarget), money(totalTarget))}
      ${statTile('Saved so far', moneyShort(totalSaved), percent(totalTarget ? totalSaved / totalTarget * 100 : NaN) + ' of targets')}
      ${statTile('Investing monthly', moneyShort(totalMonthly), 'Across all goals')}
    </div>
    <div class="goal-grid">${goals.map((goal, index) => goalCard(goal, projections[index])).join('')}</div>
    ${promptChips([['Review my goals', 'Review my goals against my current portfolio: which are on track, which need more, and which of my holdings fit each goal\'s timeline.']])}`;
}

function goalCard(goal, p) {
  const status = p.onTrack
    ? '<span class="pill is-connected">On track</span>'
    : `<span class="pill is-behind">${p.months ? 'Needs more' : 'Due now'}</span>`;
  const advice = p.onTrack
    ? `Projected ${moneyShort(p.projected)} by Dec ${goal.targetYear}, ${moneyShort(p.projected - goal.targetInr)} more than the target.`
    : p.months
      ? `Projected ${moneyShort(p.projected)} by Dec ${goal.targetYear}. Investing about ${money(Math.ceil(p.needed / 100) * 100)} a month would close the gap.`
      : `${moneyShort(goal.targetInr - (goal.savedInr || 0))} is still needed.`;
  const ask = `Review my goal "${goal.name}": ${money(goal.targetInr)} by ${goal.targetYear}, ${money(goal.savedInr || 0)} saved, investing ${money(goal.monthlyInr || 0)} a month at an assumed ${goal.expectedReturnPct}% a year. Is the assumption reasonable, and which of my holdings suit this timeline?`;
  return `<article class="goal-card">
    <header><h3>${escHtml(goal.name)}</h3>${status}</header>
    <p class="goal-target"><strong>${moneyShort(goal.targetInr)}</strong> by ${goal.targetYear}</p>
    <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.progress.toFixed(0)}" aria-label="Saved so far"><span style="width:${p.progress.toFixed(1)}%"></span></div>
    <p class="goal-meta">${moneyShort(goal.savedInr || 0)} saved (${percent(p.progress)}) · ${money(goal.monthlyInr || 0)} a month · ${goal.expectedReturnPct}% a year</p>
    <p class="goal-advice">${escHtml(advice)}</p>
    <div class="goal-actions">
      <button class="table-action" type="button" data-edit-goal="${escHtml(goal.id)}">Edit</button>
      <button class="table-action" type="button" data-delete-goal="${escHtml(goal.id)}">Delete</button>
      <button class="table-action" type="button" data-agent="portfolio" data-prompt="${escHtml(ask)}">Ask AI</button>
    </div>
  </article>`;
}

function openGoalForm(goal = null) {
  editingGoalId = goal?.id || null;
  document.getElementById('goal-form-title').textContent = editingGoalId ? 'Edit goal' : 'New goal';
  document.getElementById('goal-name').value = goal?.name || '';
  document.getElementById('goal-target').value = goal?.targetInr ?? '';
  document.getElementById('goal-year').value = goal?.targetYear ?? new Date().getFullYear() + 5;
  document.getElementById('goal-saved').value = goal?.savedInr ?? 0;
  document.getElementById('goal-monthly').value = goal?.monthlyInr ?? 0;
  document.getElementById('goal-return').value = goal?.expectedReturnPct ?? 10;
  document.getElementById('goal-status').textContent = '';
  goalForm.hidden = false;
  goalForm.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  document.getElementById('goal-name').focus();
}

async function saveGoals(goals) {
  const data = await fetchJson('/api/goals', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goals }),
  });
  pageCache.set('goals', Promise.resolve(data.goals));
  document.dispatchEvent(new CustomEvent('ca:goals'));
  return data.goals;
}

document.getElementById('add-goal').addEventListener('click', () => openGoalForm());
document.getElementById('goal-cancel').addEventListener('click', () => { goalForm.hidden = true; });

goalForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = document.getElementById('goal-status');
  const goals = await getGoals().catch(() => []);
  const goal = {
    id: editingGoalId || `g${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    name: document.getElementById('goal-name').value.trim(),
    targetInr: Math.round(Number(document.getElementById('goal-target').value)),
    targetYear: Math.round(Number(document.getElementById('goal-year').value)),
    savedInr: Math.round(Number(document.getElementById('goal-saved').value) || 0),
    monthlyInr: Math.round(Number(document.getElementById('goal-monthly').value) || 0),
    expectedReturnPct: Number(document.getElementById('goal-return').value) || 0,
  };
  const next = editingGoalId ? goals.map((item) => (item.id === editingGoalId ? goal : item)) : [...goals, goal];
  const submit = goalForm.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await saveGoals(next);
    goalForm.hidden = true;
    renderGoals();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.addEventListener('click', async (event) => {
  const add = event.target.closest('[data-add-goal]');
  const edit = event.target.closest('[data-edit-goal]');
  const remove = event.target.closest('[data-delete-goal]');
  if (add) {
    if (currentRoute !== 'goals') navigate('goals');
    openGoalForm();
  }
  if (!edit && !remove) return;
  const goals = await getGoals().catch(() => []);
  if (edit) openGoalForm(goals.find((goal) => goal.id === edit.dataset.editGoal));
  if (remove) {
    const goal = goals.find((item) => item.id === remove.dataset.deleteGoal);
    if (!goal || !window.confirm(`Delete the goal "${goal.name}"?`)) return;
    try {
      await saveGoals(goals.filter((item) => item.id !== goal.id));
      renderGoals();
    } catch (error) {
      toast(error.message);
    }
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
