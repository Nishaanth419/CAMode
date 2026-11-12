/* ── Right rail (Ask AI only) ───────────────────────────────────────────
   "This answer used": the latest answer's sources read off its trace (trace.js answerSources(), so nothing is
   guessed), then the net worth from the latest snapshot with a link to Holdings, then recent chats. Loaded after
   finance.js, trace.js, app.js and pages.js, and reuses their data loaders, so the rail and the pages share one
   request per resource. */

const railSources = document.getElementById('rail-sources');
const railPortfolio = document.getElementById('rail-portfolio');
const railChats = document.getElementById('rail-chats');
const RAIL_CHATS = 4;

function railMessage(text, action = '') {
  return `<p class="rail-copy">${escHtml(text)}</p>${action}`;
}

function railSkeleton(lines = 3) {
  return `<div class="rail-skeleton" aria-hidden="true">${'<span></span>'.repeat(lines)}</div>`;
}

function railError(error) {
  return `<p class="rail-copy rail-error" role="alert">${escHtml(error.message || 'Could not load this.')}</p>`;
}

/* ── This answer used ───────────────────────────────────────────────── */
function sourceRow(title, note, tone) {
  return `<li class="source-row is-${tone}"><span class="source-dot" aria-hidden="true"></span><span class="source-text"><strong>${escHtml(title)}</strong>${note ? `<small>${escHtml(note)}</small>` : ''}</span></li>`;
}

const SOURCE_NOTES = {
  local: 'Saved in CA Mode; no live INDmoney call',
  live: 'Read-only call to your INDmoney account',
};

function renderRailSources() {
  const trace = typeof latestAnswerTrace === 'undefined' ? null : latestAnswerTrace;
  if (!trace) {
    railSources.innerHTML = railMessage('After an answer, the data it read and the checks on its figures show here.');
    return;
  }
  const sources = answerSources(trace);
  const rows = [];
  if (sources.snapshot) rows.push(sourceRow('Saved snapshot', snapshotLabel(sources.snapshot), 'good'));
  for (const tool of sources.tools.slice(0, 5)) {
    const calls = tool.calls > 1 ? ` · ${tool.calls} calls` : '';
    const failed = tool.failed ? ` · ${tool.failed} did not complete` : '';
    rows.push(sourceRow(tool.title, `${tool.name}${calls}${failed} · ${SOURCE_NOTES[tool.source]}`, tool.failed ? 'warn' : 'info'));
  }
  if (sources.tools.length > 5) rows.push(sourceRow(`${sources.tools.length - 5} more tools`, 'Listed in the answer\'s steps', 'info'));
  if (sources.context.length) rows.push(sourceRow('Your saved details', sources.context.join(' · '), 'info'));
  if (sources.numbers) rows.push(sourceRow(sources.numbers.label, sources.numbers.note, sources.numbers.status === 'passed' ? 'good' : 'warn'));
  if (sources.omitted) rows.push(sourceRow(`${sources.omitted} steps not kept`, 'Long answers keep only their first and last steps', 'muted'));
  railSources.innerHTML = rows.length
    ? `<ul class="source-list">${rows.join('')}</ul>`
    : railMessage(trace.length ? 'This answer read none of your data.' : 'No steps were recorded for this answer.');
}

/* ── Portfolio summary ──────────────────────────────────────────────── */
// One line when a connected broker's login has ended or its holdings are old (brokerAttention is set in app.js).
function attentionLine() {
  const found = typeof brokerAttention === 'undefined' ? [] : brokerAttention;
  if (!found.length) return '';
  const first = brokerBannerText(found[0]);
  return `<small class="rail-note is-warn">${escHtml(first)}${found.length > 1 ? ` (+${found.length - 1} more)` : ''}</small>`;
}

async function renderRailPortfolio(isCurrent) {
  const access = portfolioAccess();
  if (access === 'disconnected') {
    railPortfolio.innerHTML = railMessage('Connect INDmoney to track your net worth and how it changes every day. Access is read-only.',
      '<button class="rail-button" type="button" data-connect>Connect INDmoney</button>');
    return;
  }
  if (access === 'private') {
    railPortfolio.innerHTML = railMessage('Private mode saves no daily snapshots, so there is no net worth to show here. Portfolio Analyzer can still read INDmoney live.');
    return;
  }
  railPortfolio.innerHTML = railSkeleton(2);
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
  const netWorth = portfolio.figures.totals.net_worth_inr ?? portfolio.networthInr;
  const series = historySeries(history);
  const change = historyChange(series.points);
  const tone = change && change.change < 0 ? 'is-down' : 'is-up';
  const changeHtml = change
    ? `<p class="rail-change ${tone}" title="Change since ${escHtml(formatDate(change.from))}"><span aria-hidden="true">${change.change < 0 ? '▼' : '▲'}</span> ${escHtml(signedMoney(change.change))} · ${escHtml(percent(change.pct))} ${escHtml(spanPhrase(change.days))}</p>`
    : '';
  railPortfolio.innerHTML = `
    <p class="rail-figure">${escHtml(money(netWorth))}</p>
    ${changeHtml}
    <small class="rail-note">From your snapshot, updated ${escHtml(timeAgo(portfolio.capturedAt))}${series.includesBrokers ? '; the change includes your connected brokers' : ''}.</small>
    ${attentionLine()}`;
}

/* ── Recent chats ───────────────────────────────────────────────────── */
function renderRailChats() {
  const recent = (typeof chats === 'undefined' ? [] : chats).filter((chat) => chat.id !== activeChatId).slice(0, RAIL_CHATS);
  if (!recent.length) {
    railChats.innerHTML = railMessage('Your other conversations show here.');
    return;
  }
  railChats.innerHTML = `<ul class="rail-chat-list">${recent.map((chat) => `<li><button class="rail-chat" type="button" data-rail-chat="${escHtml(chat.id)}" title="${escHtml(chat.title || 'New chat')}">${escHtml(chat.title || 'New chat')}</button></li>`).join('')}</ul>`;
}

railChats.addEventListener('click', (event) => {
  const button = event.target.closest('[data-rail-chat]');
  if (button) selectChat(button.dataset.railChat).catch(showError);
});

/* ── Wiring ─────────────────────────────────────────────────────────── */
let railToken = 0;
// The rail shows only beside Ask AI, so it loads the portfolio only there; elsewhere a change marks it stale.
let railStale = true;
let railQueued = false;

function railShown() {
  return currentRoute === 'chat';
}

function renderRail() {
  railToken += 1;
  const token = railToken;
  const isCurrent = () => token === railToken;
  if (!sessionReady()) {
    clearRail();
    return;
  }
  if (!railShown()) {
    railStale = true;
    return;
  }
  railStale = false;
  renderRailSources();
  renderRailChats();
  renderRailPortfolio(isCurrent).catch((error) => console.warn('Rail portfolio failed to render:', error));
}

// Sign-in fires both ca:route and ca:session in one go; render once for both.
function queueRail() {
  if (railQueued) return;
  railQueued = true;
  queueMicrotask(() => {
    railQueued = false;
    renderRail();
  });
}

function clearRail() {
  railSources.innerHTML = '';
  railPortfolio.innerHTML = '';
  railChats.innerHTML = '';
}

document.addEventListener('ca:route', () => { if (railShown() && railStale) queueRail(); });
document.addEventListener('ca:session', () => {
  railStale = true;
  queueRail();
});
for (const name of ['ca:snapshot', 'ca:brokers']) {
  document.addEventListener(name, () => {
    if (!sessionReady()) return;
    railStale = true;
    queueRail();
  });
}
document.addEventListener('ca:session-ended', () => {
  railToken += 1;
  railStale = true;
  clearRail();
});
// The sources and chat list are cheap and already in memory, so they follow every answer and list change.
document.addEventListener('ca:answer', () => { if (sessionReady()) renderRailSources(); });
document.addEventListener('ca:chats', () => { if (sessionReady()) renderRailChats(); });
