/* ── State ─────────────────────────────────────────────────────────── */
let chats = [];
let activeChatId = null;
let isStreaming = false;

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch((error) => {
    console.warn('The app shell could not be cached for offline use:', error);
  }));
}

/* ── DOM refs ──────────────────────────────────────────────────────── */
const statusDot  = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const chatList   = document.getElementById('chat-list');
const chatCount  = document.getElementById('chat-count');
const chatSearch = document.getElementById('chat-search');
const newChatButton = document.getElementById('new-chat-button');
const emptyState = document.getElementById('empty-state');
const messages   = document.getElementById('messages');
const chatScroll = document.getElementById('chat-scroll');
const userInput  = document.getElementById('user-input');
const sendBtn    = document.getElementById('send-btn');
const authButton = document.getElementById('auth-button');
const themeToggle = document.getElementById('theme-toggle');
const themeToggleIcon = document.getElementById('theme-toggle-icon');
const agentNavItems = document.querySelectorAll('.nav-item[data-agent]');
const providerPicker = document.getElementById('provider-picker');
const greeting = document.getElementById('greeting');
const welcomeAgentName = document.getElementById('welcome-agent-name');
const welcomeDescription = document.getElementById('welcome-description');
const followUps = document.getElementById('follow-ups');
const accountName = document.getElementById('account-name');
const accountPlan = document.getElementById('account-plan');
const accountAvatar = document.querySelector('.account-avatar');
const portfolioPill = document.getElementById('portfolio-pill');
const portfolioCopy = document.getElementById('portfolio-copy');
const portfolioCta = document.getElementById('portfolio-cta');
const menuToggle = document.getElementById('menu-toggle');
const sidebarScrim = document.getElementById('sidebar-scrim');
const promptButtons = document.querySelectorAll('[data-prompt]');
const accountTrigger = document.getElementById('account-trigger');
const portfolioFigure = document.getElementById('portfolio-figure');
const portfolioSpark = document.getElementById('portfolio-spark');
const sparkArea = document.getElementById('spark-area');
const sparkLine = document.getElementById('spark-line');
const snapshotRefresh = document.getElementById('snapshot-refresh');
const profileGate = document.getElementById('profile-gate');
const profileList = document.getElementById('profile-list');
const showCreateProfile = document.getElementById('show-create-profile');
const unlockForm = document.getElementById('profile-unlock-form');
const unlockName = document.getElementById('unlock-name');
const unlockPin = document.getElementById('unlock-pin');
const createForm = document.getElementById('profile-create-form');
const gateError = document.getElementById('gate-error');
const profileDialog = document.getElementById('profile-dialog');
const profileForm = document.getElementById('profile-form');
const profileFormStatus = document.getElementById('profile-form-status');
const PROFILE_FIELDS = ['riskAppetite', 'ageBand', 'investmentHorizonYears', 'monthlyInvestmentInr', 'primaryGoal'];
let isConnected = false;
let currentUser = null;
let unlockTarget = null;
let selectedAgent = localStorage.getItem('ca-mode-agent') || 'finance';
let selectedProvider = localStorage.getItem('ca-mode-provider') || 'openai';
let providerReady = false;

async function loadProviders() {
  const response = await fetch('/api/providers');
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not load AI providers.');
  const providers = data.providers || [];
  providerPicker.innerHTML = '';
  for (const provider of providers) {
    const option = document.createElement('option');
    option.value = provider.id;
    option.disabled = !provider.configured;
    option.textContent = provider.configured
      ? `${provider.label} · ${provider.model}`
      : `${provider.label} · add API key in .env`;
    providerPicker.appendChild(option);
  }
  const configured = providers.filter((provider) => provider.configured);
  const savedProvider = configured.find((provider) => provider.id === selectedProvider);
  selectedProvider = savedProvider?.id || data.defaultProvider || 'openai';
  if (!configured.length) {
    providerPicker.innerHTML = '<option value="">Add an AI API key in .env</option>';
    providerPicker.disabled = true;
  } else {
    providerPicker.value = selectedProvider;
    providerPicker.disabled = false;
  }
  providerReady = true;
  localStorage.setItem('ca-mode-provider', selectedProvider);
}

providerPicker.addEventListener('change', () => {
  selectedProvider = providerPicker.value;
  localStorage.setItem('ca-mode-provider', selectedProvider);
});

function applyTheme(theme) {
  const selected = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = selected;
  const nextLabel = selected === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
  themeToggle.setAttribute('aria-label', nextLabel);
  themeToggle.title = nextLabel;
  themeToggleIcon.innerHTML = selected === 'dark'
    ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>'
    : '<path d="M20.2 15.7A8.5 8.5 0 0 1 8.3 3.8 8.6 8.6 0 1 0 20.2 15.7Z"/>';
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', selected === 'dark' ? '#0d1613' : '#f4f7f5');
  localStorage.setItem('code-mode-theme', selected);
}

applyTheme(localStorage.getItem('code-mode-theme') || 'light');
themeToggle.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  // Plotly bakes colors into each chart, so redraw open charts for the new theme.
  document.querySelectorAll('.plotly-chart[data-plotly-ready]').forEach(graph => {
    const spec = chartSpecRegistry.get(graph.id);
    const view = graph.closest('.data-chart')?.dataset.chartView;
    if (spec && window.Plotly && view !== 'table') window.Plotly.relayout(graph, plotlyLayout(spec, view));
  });
});

function updateGreeting() {
  const hour = new Date().getHours();
  greeting.textContent = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
}
updateGreeting();

/* ── Mobile navigation drawer ───────────────────────────────────────── */
function setSidebarOpen(open) {
  document.body.classList.toggle('sidebar-open', open);
  sidebarScrim.hidden = !open;
  menuToggle.setAttribute('aria-expanded', String(open));
}
menuToggle.addEventListener('click', () => setSidebarOpen(!document.body.classList.contains('sidebar-open')));
sidebarScrim.addEventListener('click', () => setSidebarOpen(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setSidebarOpen(false);
});

function agentName(agent = selectedAgent) {
  return agent === 'portfolio' ? 'Portfolio Analyzer' : 'Finance Agent';
}

function assistantLabel() {
  return `${agentName()} · ${providerPicker.selectedOptions[0]?.textContent?.split(' · ')[0] || 'AI'}`;
}

function setAgent(agent) {
  selectedAgent = agent === 'portfolio' ? 'portfolio' : 'finance';
  const isPortfolio = selectedAgent === 'portfolio';
  agentNavItems.forEach((item) => item.setAttribute('aria-pressed', String(item.dataset.agent === selectedAgent)));
  welcomeAgentName.textContent = agentName();
  welcomeDescription.textContent = isPortfolio
    ? 'Ask about your INDmoney investments, holdings, allocations, and account activity.'
    : 'Ask basic finance questions and get clear explanations in plain language.';
  userInput.placeholder = isPortfolio ? 'Ask about your portfolio…' : 'Ask anything about your finances…';
  localStorage.setItem('ca-mode-agent', selectedAgent);
}

async function switchAgent(agent) {
  if (isStreaming) return;
  setAgent(agent);
  await startNewChat();
}

agentNavItems.forEach((item) => item.addEventListener('click', () => {
  setSidebarOpen(false);
  if (item.dataset.agent === selectedAgent) return;
  switchAgent(item.dataset.agent).catch(showError);
}));
setAgent(selectedAgent);

function showError(error) {
  addMessage('assistant', '<span style="color:var(--error)">' + escHtml(error.message) + '</span>');
}

/* ── Status helpers ─────────────────────────────────────────────────── */
function setStatus(state, label) {
  statusDot.className = 'status-dot ' + state;
  statusText.textContent = label;
}

function setBusy(busy) {
  isStreaming = busy;
  sendBtn.disabled = busy;
  promptButtons.forEach((button) => { button.disabled = busy; });
  agentNavItems.forEach((item) => { item.disabled = busy; });
}

function renderAccount() {
  authButton.textContent = isConnected ? 'Disconnect' : 'Connect';
  authButton.title = isConnected ? 'Disconnect your INDmoney account' : 'Connect your INDmoney account for portfolio analysis';
  accountName.textContent = currentUser?.displayName || 'Guest';
  accountPlan.textContent = isConnected ? 'Connected' : 'Not connected';
  accountPlan.title = accountPlan.textContent;
  accountAvatar.classList.toggle('is-connected', isConnected);
  portfolioPill.textContent = isConnected ? 'Connected' : 'Not connected';
  portfolioPill.classList.toggle('is-connected', isConnected);
  portfolioCopy.textContent = isConnected
    ? 'Your INDmoney account is linked. Ask the Portfolio Analyzer for a live snapshot of your net worth and holdings.'
    : 'Connect INDmoney to ask the Portfolio Analyzer about your net worth, holdings, and SIPs.';
  portfolioCta.textContent = isConnected ? 'Ask about my net worth' : 'Connect INDmoney';
  snapshotRefresh.hidden = !isConnected;
  if (!isConnected) renderSnapshot(null, []);
}

/* ── Daily portfolio snapshot card ──────────────────────────────────── */
const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 });

function formatCaptured(timestamp) {
  return new Date(Number(timestamp)).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function renderSnapshot(latest, history, schedule = '09:00') {
  const values = history.map(point => point.networthInr).filter(Number.isFinite);
  portfolioFigure.hidden = !Number.isFinite(latest?.networthInr);
  portfolioFigure.textContent = portfolioFigure.hidden ? '' : inr.format(latest.networthInr);
  // SVG elements have no `hidden` property, so toggle the attribute directly.
  portfolioSpark.toggleAttribute('hidden', values.length < 2);
  if (values.length >= 2) {
    const min = Math.min(...values);
    const range = Math.max(...values) - min || 1;
    const points = values.map((value, index) => [
      (index / (values.length - 1)) * 240,
      62 - ((value - min) / range) * 54,
    ].map(n => n.toFixed(1)).join(' '));
    sparkLine.setAttribute('d', `M${points.join(' L')}`);
    sparkArea.setAttribute('d', `M${points.join(' L')} V70 H0 Z`);
  }
  if (!isConnected) return;
  portfolioCopy.textContent = latest
    ? `Snapshot captured ${formatCaptured(latest.capturedAt)}. It refreshes every day at ${schedule.split(' ')[0]}.`
    : `Your first snapshot is being saved. After that it refreshes every day at ${schedule.split(' ')[0]}.`;
}

async function loadSnapshot() {
  if (!isConnected) return;
  try {
    const response = await fetch('/api/snapshots?days=30');
    if (!response.ok) return;
    const data = await response.json();
    renderSnapshot(data.latest, data.history || [], data.schedule);
  } catch { /* The card keeps its connected copy; chat still works. */ }
}

snapshotRefresh.addEventListener('click', async () => {
  snapshotRefresh.disabled = true;
  snapshotRefresh.textContent = 'Refreshing…';
  try {
    const response = await fetch('/api/snapshots/refresh', { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Could not refresh the snapshot.');
    await loadSnapshot();
  } catch (error) {
    portfolioCopy.textContent = error.message;
  } finally {
    snapshotRefresh.disabled = false;
    snapshotRefresh.textContent = 'Refresh snapshot now';
  }
});

/* ── Load this signed-in user's conversations ───────────────────────── */
async function loadChats() {
  try {
    const sessionResponse = await fetch('/api/session');
    if (!sessionResponse.ok) throw new Error('Could not initialize your local session.');
    const session = await sessionResponse.json();
    currentUser = session.user;
    isConnected = Boolean(session.connected);
    renderAccount();
    if (!currentUser) {
      chats = [];
      activeChatId = null;
      resetConversationView();
      renderChats();
      showProfileGate(session.profiles || []);
      setStatus('online', 'Choose a profile');
      return;
    }
    hideProfileGate();
    const response = await fetch('/api/chats');
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    chats = data.chats || [];
    renderChats();
    if (chats.length) await selectChat(chats[0].id);
    else await startNewChat();
    setStatus('online', isConnected ? 'INDmoney connected' : 'Finance mode');
    loadSnapshot();
  } catch (err) {
    chatList.innerHTML = '<div class="tools-error">Could not load chats<br><small>' + escHtml(err.message) + '</small></div>';
    setStatus('error', 'Connection failed');
  }
}

/* ── Profile picker ─────────────────────────────────────────────────── */
function showGateError(message) {
  gateError.textContent = message || '';
  gateError.hidden = !message;
}

function showGateView(view) {
  profileList.hidden = view !== 'list';
  showCreateProfile.hidden = view !== 'list';
  unlockForm.hidden = view !== 'unlock';
  createForm.hidden = view !== 'create';
  showGateError('');
  const focusTarget = view === 'unlock' ? unlockPin : view === 'create' ? document.getElementById('create-name') : profileList.querySelector('button');
  focusTarget?.focus();
}

function showProfileGate(profiles) {
  profileList.innerHTML = '';
  for (const profile of profiles) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'profile-choice';
    const initial = document.createElement('span');
    initial.className = 'profile-initial';
    initial.setAttribute('aria-hidden', 'true');
    initial.textContent = (profile.displayName || '?').trim().charAt(0).toUpperCase();
    const name = document.createElement('span');
    name.textContent = profile.displayName;
    button.append(initial, name);
    button.addEventListener('click', () => {
      unlockTarget = profile;
      unlockName.textContent = profile.displayName;
      unlockPin.value = '';
      showGateView('unlock');
    });
    profileList.appendChild(button);
  }
  profileGate.hidden = false;
  document.body.classList.add('gate-open');
  showGateView(profiles.length ? 'list' : 'create');
  createForm.querySelector('[data-gate-back]').hidden = !profiles.length;
}

function hideProfileGate() {
  profileGate.hidden = true;
  document.body.classList.remove('gate-open');
  unlockForm.reset();
  createForm.reset();
}

async function submitGate(url, body, form) {
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  showGateError('');
  try {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 429) {
      const minutes = Math.ceil(Number(data.retryAfterSeconds || response.headers.get('Retry-After') || 300) / 60);
      throw new Error(`Too many incorrect PINs. Try again in ${minutes} minute${minutes === 1 ? '' : 's'}.`);
    }
    if (!response.ok) {
      // 404/405 here usually means the server is older than this page: restart it.
      throw new Error(data.error || `Could not sign in (HTTP ${response.status}). If you just updated CA Mode, restart the server.`);
    }
    await loadChats();
  } catch (error) {
    showGateError(error.message);
  } finally {
    submit.disabled = false;
  }
}

showCreateProfile.addEventListener('click', () => showGateView('create'));
profileGate.querySelectorAll('[data-gate-back]').forEach(button => button.addEventListener('click', () => showGateView('list')));
unlockForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (unlockTarget) submitGate('/api/session/login', { userId: unlockTarget.id, pin: unlockPin.value }, unlockForm);
});
createForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const pin = document.getElementById('create-pin').value;
  if (pin !== document.getElementById('create-pin-confirm').value) {
    showGateError('The two PINs do not match.');
    return;
  }
  submitGate('/api/users', { displayName: document.getElementById('create-name').value, pin }, createForm);
});

/* ── Profile settings dialog ────────────────────────────────────────── */
async function openProfileDialog() {
  if (!currentUser || isStreaming) return;
  profileFormStatus.textContent = '';
  document.getElementById('delete-pin').value = '';
  profileForm.querySelector('.danger-zone').open = false;
  const response = await fetch('/api/profile');
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not load your profile.');
  document.getElementById('profile-dialog-title').textContent = data.displayName;
  for (const field of PROFILE_FIELDS) {
    document.getElementById(`field-${field}`).value = data.profile?.[field] ?? '';
  }
  profileDialog.showModal();
}

accountTrigger.addEventListener('click', () => {
  setSidebarOpen(false);
  openProfileDialog().catch(showError);
});
profileDialog.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => profileDialog.close()));

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const body = {};
  for (const field of PROFILE_FIELDS) {
    const input = document.getElementById(`field-${field}`);
    const value = input.value.trim();
    if (value === '') continue;
    body[field] = input.type === 'number' ? Number(value) : value;
  }
  const response = await fetch('/api/profile', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (response.ok) {
    profileFormStatus.textContent = 'Saved.';
    setTimeout(() => profileDialog.close(), 600);
  } else {
    const data = await response.json().catch(() => ({}));
    profileFormStatus.textContent = Array.isArray(data.detail) ? 'Check the highlighted values and try again.' : (data.error || 'Could not save your profile.');
  }
});

document.getElementById('switch-profile').addEventListener('click', async () => {
  profileDialog.close();
  await fetch('/api/session/logout', { method: 'POST' });
  await loadChats();
});

document.getElementById('delete-profile').addEventListener('click', async () => {
  const pin = document.getElementById('delete-pin').value;
  if (!pin) {
    profileFormStatus.textContent = 'Enter your PIN to delete this profile.';
    return;
  }
  const response = await fetch('/api/users/me', {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    profileFormStatus.textContent = data.error || 'Could not delete this profile.';
    return;
  }
  profileDialog.close();
  await loadChats();
});

async function toggleConnection() {
  if (!isConnected) {
    window.location.assign('/auth/indmoney/connect');
    return;
  }
  if (!window.confirm('Disconnect INDmoney? Saved portfolio snapshots for this profile will be deleted; your chats are kept.')) return;
  authButton.disabled = true;
  try {
    await fetch('/auth/indmoney/disconnect', { method: 'POST' });
    await loadChats();
  } finally {
    authButton.disabled = false;
  }
}

authButton.addEventListener('click', toggleConnection);
portfolioCta.addEventListener('click', () => {
  if (!isConnected) {
    toggleConnection();
    return;
  }
  runPrompt('Give me a snapshot of my current net worth.', 'portfolio').catch(showError);
});

function relativeDay(timestamp) {
  if (!Number.isFinite(Number(timestamp))) return '';
  const startOfDay = (value) => new Date(value).setHours(0, 0, 0, 0);
  const days = Math.round((startOfDay(Date.now()) - startOfDay(Number(timestamp))) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return new Date(Number(timestamp)).toLocaleDateString([], { day: 'numeric', month: 'short' });
}

function renderChats() {
  chatList.innerHTML = '';
  const query = chatSearch.value.trim().toLowerCase();
  const visible = query ? chats.filter((chat) => (chat.title || 'New chat').toLowerCase().includes(query)) : chats;
  chatCount.textContent = chats.length ? String(chats.length) : '';
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'chat-empty';
    empty.textContent = query ? 'No chats match your search.' : 'No conversations yet.';
    chatList.appendChild(empty);
    return;
  }
  for (const chat of visible) {
    const row = document.createElement('div');
    row.className = 'chat-row';
    const select = document.createElement('button');
    select.type = 'button';
    select.className = 'chat-select' + (chat.id === activeChatId ? ' active' : '');
    select.title = chat.title || 'New chat';
    const title = document.createElement('span');
    title.className = 'chat-title';
    title.textContent = chat.title || 'New chat';
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = [agentName(chat.agentType), relativeDay(chat.updatedAt)].filter(Boolean).join(' · ');
    select.append(title, meta);
    select.addEventListener('click', () => {
      setSidebarOpen(false);
      selectChat(chat.id).catch(showError);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-delete';
    remove.textContent = '×';
    remove.title = 'Delete chat';
    remove.setAttribute('aria-label', 'Delete ' + (chat.title || 'chat'));
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteChat(chat.id).catch(showError);
    });
    row.append(select, remove);
    chatList.appendChild(row);
  }
}

chatSearch.addEventListener('input', renderChats);

async function refreshChats() {
  const response = await fetch('/api/chats');
  if (!response.ok) throw new Error('Could not refresh the chat list.');
  chats = (await response.json()).chats || [];
  renderChats();
  setStatus('online', isConnected ? 'INDmoney connected' : 'Finance mode');
}

function resetConversationView() {
  destroyPlotlyCharts(messages);
  messages.innerHTML = '';
  messages.classList.remove('has-messages');
  emptyState.hidden = false;
  followUps.hidden = true;
  chatScroll.scrollTop = 0;
}

async function startNewChat() {
  if (isStreaming) return;
  const response = await fetch('/api/chats', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agentType: selectedAgent }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not create a new chat.');
  chats.unshift(data);
  activeChatId = data.id;
  resetConversationView();
  renderChats();
  userInput.focus();
}

async function selectChat(chatId) {
  if (isStreaming) return;
  const response = await fetch('/api/chats/' + encodeURIComponent(chatId));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not open this chat.');
  activeChatId = chatId;
  setAgent(data.agentType || 'portfolio');
  resetConversationView();
  for (const message of data.messages || []) {
    if (message.role === 'user') addMessage('user', escHtml(message.content).replace(/\n/g, '<br>'));
    else if (message.role === 'assistant') addMessage('assistant', renderMarkdown(message.content), message.trace || []);
  }
  renderChats();
}

async function deleteChat(chatId) {
  if (isStreaming) return;
  if (!window.confirm('Delete this chat?')) return;
  const response = await fetch('/api/chats/' + encodeURIComponent(chatId), { method: 'DELETE' });
  if (!response.ok) throw new Error('Could not delete this chat.');
  chats = chats.filter((chat) => chat.id !== chatId);
  if (activeChatId === chatId) {
    activeChatId = null;
    if (chats.length) await selectChat(chats[0].id);
    else await startNewChat();
  } else renderChats();
}

newChatButton.addEventListener('click', () => {
  setSidebarOpen(false);
  startNewChat().catch(showError);
});

/* ── Chat ────────────────────────────────────────────────────────────── */
function showMessages() {
  emptyState.hidden = true;
  messages.classList.add('has-messages');
  followUps.hidden = false;
}

function scrollToBottom() {
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

function timeStr() {
  return new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
}

const ASSISTANT_AVATAR = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="8" width="14" height="11" rx="4"/><path d="M12 4v4M9.5 13h0M14.5 13h0"/></svg>';

function messageShell(role, label, time = '') {
  const avatar = role === 'user' ? 'You' : ASSISTANT_AVATAR;
  return `
    <div class="message-header">
      <div class="message-avatar ${role}">${avatar}</div>
      <div class="message-meta">
        <div class="message-role">${escHtml(label)}</div>
        ${time ? `<div class="message-time">${time}</div>` : ''}
      </div>
    </div>`;
}

function addMessage(role, content, trace = []) {
  showMessages();
  const div = document.createElement('div');
  div.className = `message ${role}`;
  const label = role === 'user' ? 'You' : assistantLabel();
  div.innerHTML = `
    ${messageShell(role, label, timeStr())}
    <div class="message-body">${content}</div>
    ${role === 'assistant' ? renderTraceDropdown(trace) : ''}
  `;
  messages.appendChild(div);
  renderPlotlyCharts(div);
  scrollToBottom();
  return div;
}

function renderTraceDropdown(trace) {
  if (!Array.isArray(trace) || !trace.length) return '';
  const steps = trace.map((step, index) => {
    const isTool = step.kind === 'tool';
    const title = isTool
      ? `INDmoney · ${escHtml(step.name || 'tool')}`
      : `${escHtml(step.provider || 'AI')} · ${escHtml(step.model || 'model request')}`;
    const status = escHtml(step.status || 'sent');
    let details = '';
    if (isTool && step.arguments) {
      let formatted = step.arguments;
      try { formatted = JSON.stringify(JSON.parse(step.arguments), null, 2); } catch { /* Keep the short raw preview. */ }
      details += `<div class="trace-detail-label">Arguments</div><pre class="trace-code">${escHtml(formatted)}</pre>`;
    }
    if (isTool && step.result_preview) {
      let result = step.result_preview;
      try { result = JSON.stringify(JSON.parse(result), null, 2); } catch { /* Keep the text preview. */ }
      details += `<details class="trace-result"><summary>Returned data preview</summary><pre class="trace-code">${escHtml(result)}</pre></details>`;
    }
    if (step.error) details += `<div class="trace-error">${escHtml(step.error)}</div>`;
    const duration = Number.isFinite(Number(step.duration_ms)) ? ` · ${Number(step.duration_ms)} ms` : '';
    const attempt = !isTool && Number(step.attempt) > 1 ? ` · attempt ${Number(step.attempt)}` : '';
    return `<li class="trace-step"><div class="trace-step-heading"><span class="trace-step-number">${index + 1}</span><strong>${title}</strong><span class="trace-status ${status === 'failed' ? 'is-error' : ''}">${status}${duration}${attempt}</span></div>${details}</li>`;
  }).join('');
  return `<details class="chat-trace"><summary>Trace · ${trace.length} step${trace.length === 1 ? '' : 's'}</summary><ol class="trace-list">${steps}</ol></details>`;
}

function parseTraceHeader(value) {
  try {
    const trace = JSON.parse(value || '[]');
    return Array.isArray(trace) ? trace : [];
  } catch {
    return [];
  }
}

function addTypingIndicator() {
  showMessages();
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-indicator';
  div.innerHTML = `
    ${messageShell('assistant', assistantLabel())}
    <div class="message-body">
      <div class="typing-indicator"><span></span><span></span><span></span></div>
    </div>
  `;
  messages.appendChild(div);
  scrollToBottom();
  return div;
}

function removeTypingIndicator() {
  const el = document.getElementById('typing-indicator');
  if (el) el.remove();
}

/* ── Streaming response handler ──────────────────────────────────────── */
async function sendMessage(text) {
  if (!text.trim() || isStreaming) return;
  if (!providerReady || providerPicker.disabled) {
    addMessage('assistant', '<span style="color:var(--error)">Configure at least one AI API key in .env, then restart CA Mode.</span>');
    return;
  }
  setBusy(true);
  setStatus('loading', 'Thinking…');

  addMessage('user', escHtml(text).replace(/\n/g, '<br>'));
  userInput.value = '';
  autoResize();

  const typingDiv = addTypingIndicator();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, chatId: activeChatId, agentType: selectedAgent, aiProvider: selectedProvider }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      const trace = parseTraceHeader(res.headers.get('X-Agent-Trace'));
      if (err.profileRequired) {
        removeTypingIndicator();
        await loadChats();
        return;
      }
      if (err.loginUrl) {
        const connectLink = `<br><a href="${escHtml(err.loginUrl)}">Connect INDmoney</a>`;
        throw Object.assign(new Error(err.error || 'Connect your INDmoney account.'), { connectLink, trace });
      }
      throw Object.assign(new Error([err.error, err.details].filter(Boolean).join(': ') || `HTTP ${res.status}`), { trace });
    }

    const trace = parseTraceHeader(res.headers.get('X-Agent-Trace'));
    removeTypingIndicator();
    const msgDiv = addMessage('assistant', '', trace);
    const bodyDiv = msgDiv.querySelector('.message-body');
    bodyDiv.innerHTML = '';

    // Stream and parse the response
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let rawText = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      rawText += chunk;
      // Render the accumulated text as it comes in
      destroyPlotlyCharts(bodyDiv);
      bodyDiv.innerHTML = renderMarkdown(rawText.trim());
      scrollToBottom();
    }

    if (rawText.trim() === '') {
      bodyDiv.innerHTML = '<em>The model returned no text. Check the local server logs for details.</em>';
    }
    renderPlotlyCharts(bodyDiv);

    await refreshChats();
    try {
      const savedResponse = await fetch('/api/chats/' + encodeURIComponent(activeChatId));
      if (savedResponse.ok) {
        const savedChat = await savedResponse.json();
        const savedAssistant = [...(savedChat.messages || [])].reverse().find(item => item.role === 'assistant');
        if (savedAssistant?.trace?.length) {
          const currentTrace = msgDiv.querySelector('.chat-trace');
          if (currentTrace) currentTrace.outerHTML = renderTraceDropdown(savedAssistant.trace);
        }
      }
    } catch { /* The compact trace from the response header is still visible. */ }
  } catch (err) {
    removeTypingIndicator();
    addMessage('assistant',
      `<span style="color:var(--error)">⚠ Error: ${escHtml(err.message)}</span>${err.connectLink || ''}`,
      err.trace || [],
    );
    setStatus('error', 'Request failed');
  } finally {
    setBusy(false);
    scrollToBottom();
  }
}

/* ── Markdown renderer (minimal) ─────────────────────────────────────── */
function renderMarkdown(text) {
  // Escape helper
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  // Extract chart blocks first so the agent can return data-driven visuals.
  const chartBlocks = [];
  text = text.replace(/```chart\s*\n([\s\S]*?)```/gi, (_, json) => {
    const idx = chartBlocks.length;
    chartBlocks.push(parseChartSpec(json));
    return `\x00CHART${idx}\x00`;
  });

  // Some model responses wrap the same JSON in bold Markdown instead of a
  // chart fence. Recognize that shape too and render it as a chart.
  text = text.replace(/\*\*\s*(\{[\s\S]*?\})\s*\*\*/g, (whole, json) => {
    const spec = parseChartSpec(json);
    if (!spec) return whole;
    const idx = chartBlocks.length;
    chartBlocks.push(spec);
    return `\x00CHART${idx}\x00`;
  });

  // Extract and replace code blocks
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || 'js', code: code.trimEnd() });
    return `\x00CODE${idx}\x00`;
  });

  // Render pipe tables even if a model omits the Markdown separator row.
  const tableBlocks = [];
  const lines = text.replace(/\\([|#])/g, '$1').split('\n');
  const renderedLines = [];
  const isTableRow = line => line.trim().startsWith('|') && (line.match(/\|/g) || []).length >= 2;
  const tableCells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
  const isTableDivider = line => tableCells(line).every(cell => /^:?-{3,}:?$/.test(cell));
  const inlineTableCell = value => esc(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>');
  const makeTable = rows => {
    const hasDivider = rows.length > 1 && isTableDivider(rows[1]);
    const header = hasDivider ? rows[0] : null;
    const bodyRows = rows.filter((_, index) => !(hasDivider && index <= 1));
    const headHtml = header
      ? `<thead><tr>${tableCells(header).map(cell => `<th scope="col">${inlineTableCell(cell)}</th>`).join('')}</tr></thead>`
      : '';
    const bodyHtml = bodyRows.map(row => `<tr>${tableCells(row).map(cell => `<td>${inlineTableCell(cell)}</td>`).join('')}</tr>`).join('');
    return `<div class="markdown-table-wrap"><table class="markdown-table">${headHtml}<tbody>${bodyHtml}</tbody></table></div>`;
  };
  for (let i = 0; i < lines.length;) {
    if (!isTableRow(lines[i])) {
      renderedLines.push(lines[i++]);
      continue;
    }
    const rows = [];
    while (i < lines.length && isTableRow(lines[i])) rows.push(lines[i++]);
    const idx = tableBlocks.push(makeTable(rows)) - 1;
    renderedLines.push('', `@@CA_TABLE_${idx}@@`, '');
  }
  text = renderedLines.join('\n');

  // Process inline elements
  text = esc(text);

  // Inline code
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold/italic
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Links: model output is untrusted, so only allow web/mail URLs and keep
  // quotes from breaking out of the href attribute.
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, url) => {
    const href = url.replace(/&amp;/g, '&');
    if (!/^(?:https?:\/\/|mailto:)/i.test(href)) return label;
    return `<a href="${escHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  const headingBlocks = [];
  text = text.replace(/^(#{1,6})\s+(.+)$/gm, (_, marks, content) => {
    const level = marks.length;
    const idx = headingBlocks.push(`<h${level}>${content}</h${level}>`) - 1;
    return `@@CA_HEADING_${idx}@@`;
  });

  // Line breaks & paragraphs
  text = text.replace(/\n\n/g, '</p><p style="margin-top:12px">');
  text = text.replace(/\n/g, '<br>');
  text = '<p>' + text + '</p>';

  // Restore code blocks with syntax highlighting
  text = text.replace(/\x00CODE(\d+)\x00/g, (_, idx) => {
    const { lang, code } = codeBlocks[idx];
    const highlighted = highlightCode(code);
    const btnId = 'copy-' + Math.random().toString(36).slice(2);
    return `
      <div class="code-block">
        <div class="code-block-header">
          <span class="lang-badge">${esc(lang || 'code')}</span>
          Code snippet
          <button class="copy-btn" id="${btnId}" onclick="copyCode(this, ${escHtml(JSON.stringify(code))})">Copy</button>
        </div>
        <pre>${highlighted}</pre>
      </div>`;
  });

  text = text.replace(/<p(?: style="margin-top:12px")?>\s*@@CA_TABLE_(\d+)@@\s*<\/p>/g, (_, idx) => tableBlocks[idx]);
  text = text.replace(/<p(?: style="margin-top:12px")?>\s*@@CA_HEADING_(\d+)@@\s*<\/p>/g, (_, idx) => headingBlocks[idx]);
  text = text.replace(/@@CA_TABLE_(\d+)@@/g, (_, idx) => tableBlocks[idx]);
  text = text.replace(/@@CA_HEADING_(\d+)@@/g, (_, idx) => headingBlocks[idx]);

  text = text.replace(/\x00CHART(\d+)\x00/g, (_, idx) => renderDataChart(chartBlocks[idx]));

  // Detect result JSON blocks (wrapped in triple backtick or plain)
  // The agent may send result sections prefixed with "Result:" or "Output:"
  text = text.replace(/<p>(<strong>)?(?:Result|Output|Execution Result):?(<\/strong>)?\s*<\/p>/gi,
    '<div class="exec-result-header">⬡ Execution Result</div>');

  return text;
}

function parseChartSpec(json) {
  try {
    // Normalize HTML-encoded spaces/quotes that occasionally appear in model output.
    const normalized = String(json)
      .replace(/&#x20;|&#32;/gi, ' ')
      .replace(/&quot;/gi, '"')
      .replace(/&amp;/gi, '&');
    const spec = JSON.parse(normalized);
    return spec && (
      (['bar', 'donut'].includes(spec.type) && Array.isArray(spec.data)) ||
      (spec.type === 'line' && Array.isArray(spec.series)) ||
      (spec.type === 'candlestick' && Array.isArray(spec.candles))
    ) ? spec : null;
  } catch {
    return null;
  }
}

const chartSpecRegistry = new Map();

function cleanChartText(value, maxLength = 120) {
  return String(value ?? '').replace(/[<>]/g, '').slice(0, maxLength);
}

function chartRows(spec) {
  return (Array.isArray(spec?.data) ? spec.data : []).slice(0, 24).map(item => ({
    label: cleanChartText(item?.label, 80), value: Number(item?.value),
  })).filter(item => item.label && Number.isFinite(item.value));
}

function chartSeries(spec) {
  return (Array.isArray(spec?.series) ? spec.series : []).slice(0, 8).map(item => ({
    name: cleanChartText(item?.name, 60),
    points: (Array.isArray(item?.data) ? item.data : []).slice(0, 100).map(point => ({
      label: cleanChartText(point?.label, 50), value: Number(point?.value),
    })).filter(point => point.label && Number.isFinite(point.value)),
  })).filter(item => item.name && item.points.length > 1);
}

function chartCandles(spec) {
  return (Array.isArray(spec?.candles) ? spec.candles : []).slice(0, 300).map(item => ({
    label: cleanChartText(item?.label, 50),
    open: Number(item?.open), high: Number(item?.high),
    low: Number(item?.low), close: Number(item?.close),
  })).filter(item => item.label && [item.open, item.high, item.low, item.close].every(Number.isFinite)
    && item.high >= Math.max(item.open, item.close, item.low)
    && item.low <= Math.min(item.open, item.close, item.high));
}

function renderDataChart(spec) {
  if (!spec || !['bar', 'donut', 'line', 'candlestick'].includes(spec.type)) return '';
  const isLine = spec.type === 'line';
  const isCandlestick = spec.type === 'candlestick';
  const rows = isLine || isCandlestick ? [] : chartRows(spec);
  const series = isLine ? chartSeries(spec) : [];
  const candles = isCandlestick ? chartCandles(spec) : [];
  if (isLine ? !series.length : isCandlestick ? !candles.length : !rows.length) return '';
  const canPie = !isLine && rows.every(row => row.value >= 0) && rows.some(row => row.value > 0);
  const id = `plotly-${Math.random().toString(36).slice(2)}`;
  const safeSpec = isCandlestick
    ? { type: 'candlestick', title: cleanChartText(spec.title), unit: cleanChartText(spec.unit, 16), candles }
    : isLine
    ? { type: 'line', title: cleanChartText(spec.title), unit: cleanChartText(spec.unit, 16), series }
    : { type: spec.type, title: cleanChartText(spec.title), unit: cleanChartText(spec.unit, 16), data: rows };
  chartSpecRegistry.set(id, safeSpec);
  const title = escHtml(safeSpec.title || 'Portfolio data');
  const initialView = isLine ? 'line' : isCandlestick ? 'candlestick' : spec.type === 'donut' && canPie ? 'pie' : 'bar';
  const controls = isLine ? '' : isCandlestick
    ? `<div class="chart-view-controls" role="group" aria-label="Choose chart view"><button type="button" class="chart-view-button is-active" aria-pressed="true" onclick="setChartView(this, 'candlestick')">Candles</button><button type="button" class="chart-view-button" aria-pressed="false" onclick="setChartView(this, 'table')">Data</button></div>`
    : `<div class="chart-view-controls" role="group" aria-label="Choose chart view"><button type="button" class="chart-view-button ${initialView === 'bar' ? 'is-active' : ''}" aria-pressed="${initialView === 'bar'}" onclick="setChartView(this, 'bar')">Bars</button>${canPie ? `<button type="button" class="chart-view-button ${initialView === 'pie' ? 'is-active' : ''}" aria-pressed="${initialView === 'pie'}" onclick="setChartView(this, 'pie')">Donut</button>` : ''}<button type="button" class="chart-view-button" aria-pressed="false" onclick="setChartView(this, 'table')">Data</button></div>`;
  const table = renderChartTable(safeSpec);
  return `<figure class="data-chart${isLine ? ' line-chart' : ' category-chart'}" data-chart-id="${id}" data-chart-view="${initialView}"><div class="chart-heading"><figcaption class="data-chart-title">${title}</figcaption>${controls}</div><div class="plotly-chart" id="${id}" role="img" aria-label="Interactive chart: ${title}"></div><div class="chart-data-panel" hidden>${table}</div><p class="chart-hint">Hover for values. Use the Plotly toolbar to zoom, pan, reset, or export the chart.</p></figure>`;
}

function renderChartTable(spec) {
  const esc = escHtml;
  if (spec.type === 'candlestick') {
    const rows = spec.candles.map(item => `<tr><td>${esc(item.label)}</td><td>${item.open}</td><td>${item.high}</td><td>${item.low}</td><td>${item.close}</td></tr>`).join('');
    return `<div class="chart-data-scroll"><table class="chart-data-table"><thead><tr><th>Period</th><th>Open</th><th>High</th><th>Low</th><th>Close</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  if (spec.type === 'line') {
    const rows = spec.series[0]?.points.map((point, index) => {
      const values = spec.series.map(item => item.points[index]?.value);
      return `<tr><td>${esc(point.label)}</td>${values.map(value => `<td>${Number.isFinite(value) ? new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(value) : '—'}</td>`).join('')}</tr>`;
    }).join('') || '';
    return `<div class="chart-data-scroll"><table class="chart-data-table"><thead><tr><th>Period</th>${spec.series.map(item => `<th>${esc(item.name)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  const total = spec.data.every(row => row.value >= 0) ? spec.data.reduce((sum, row) => sum + row.value, 0) : 0;
  const rows = spec.data.map(row => `<tr><td>${esc(row.label)}</td><td>${new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(row.value)}</td><td>${total ? `${(row.value / total * 100).toFixed(1)}%` : '—'}</td></tr>`).join('');
  return `<div class="chart-data-scroll"><table class="chart-data-table"><thead><tr><th>Category</th><th>Value</th><th>Share</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function plotlyLayout(spec, view) {
  const style = getComputedStyle(document.body);
  const text = style.getPropertyValue('--text').trim() || '#e5e7eb';
  const muted = style.getPropertyValue('--text-soft').trim() || '#9ca3af';
  const border = style.getPropertyValue('--border').trim() || '#374151';
  const surface = style.getPropertyValue('--surface').trim() || '#111827';
  const unit = spec.unit ? ` (${spec.unit})` : '';
  const layout = {
    title: { text: cleanChartText(spec.title || 'Portfolio data'), x: 0.02, xanchor: 'left', font: { size: 15 } },
    autosize: true, height: 360, margin: { l: 64, r: 24, t: 56, b: 58 },
    paper_bgcolor: surface, plot_bgcolor: surface,
    font: { family: 'Inter, sans-serif', color: text, size: 12 },
    colorway: ['#27835e', '#5d88b5', '#d09b4f', '#a56d9a', '#d16f62', '#4b9bb1', '#8aa657', '#8973af'],
    legend: { orientation: 'h', y: -0.22, font: { color: muted } },
    xaxis: { gridcolor: border, zerolinecolor: border, automargin: true },
    yaxis: { gridcolor: border, zerolinecolor: border, automargin: true, title: { text: unit.trim() } },
    hoverlabel: { bgcolor: surface, bordercolor: border, font: { color: text } },
  };
  if (view === 'pie') {
    layout.margin = { l: 24, r: 24, t: 56, b: 24 };
    layout.showlegend = true;
  } else if (view === 'bar') {
    layout.barmode = 'group';
    layout.yaxis = { ...layout.yaxis, autorange: 'reversed' };
  } else if (spec.type === 'line') {
    layout.hovermode = 'x unified';
    layout.xaxis = { ...layout.xaxis, rangeslider: { visible: spec.series[0]?.points.length > 8 } };
  } else if (spec.type === 'candlestick') {
    layout.hovermode = 'x unified';
    layout.xaxis = { ...layout.xaxis, rangeslider: { visible: spec.candles.length > 8 } };
  }
  return layout;
}

function plotlyTraces(spec, view) {
  if (spec.type === 'candlestick') return [{
    type: 'candlestick',
    x: spec.candles.map(item => item.label),
    open: spec.candles.map(item => item.open),
    high: spec.candles.map(item => item.high),
    low: spec.candles.map(item => item.low),
    close: spec.candles.map(item => item.close),
    increasing: { line: { color: '#27835e' } },
    decreasing: { line: { color: '#d16f62' } },
    hoverlabel: { namelength: -1 },
  }];
  if (spec.type === 'line') return spec.series.map(item => ({
    type: 'scatter', mode: 'lines+markers', name: item.name,
    x: item.points.map(point => point.label), y: item.points.map(point => point.value),
    hovertemplate: '%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>',
    line: { width: 2.5 }, marker: { size: 7 },
  }));
  if (view === 'pie') return [{
    type: 'pie', hole: 0.52, labels: spec.data.map(row => row.label),
    values: spec.data.map(row => row.value), sort: false,
    textinfo: 'label+percent', textposition: 'outside', automargin: true,
    hovertemplate: '%{label}<br>%{value:,.2f} (%{percent})<extra></extra>',
  }];
  return [{
    type: 'bar', orientation: 'h',
    x: spec.data.map(row => row.value), y: spec.data.map(row => row.label),
    name: spec.title || 'Value',
    hovertemplate: '%{y}<br>%{x:,.2f}<extra></extra>',
  }];
}

function renderPlotlyCharts(container) {
  const graphs = container.matches?.('.plotly-chart') ? [container] : container.querySelectorAll?.('.plotly-chart');
  if (!graphs) return;
  for (const graph of graphs) {
    const spec = chartSpecRegistry.get(graph.id);
    if (!spec || graph.dataset.plotlyReady) continue;
    if (!window.Plotly) {
      graph.textContent = 'Interactive charts could not load. Check your connection and refresh.';
      graph.classList.add('plotly-chart-error');
      continue;
    }
    graph.dataset.plotlyReady = 'true';
    const view = graph.closest('.data-chart')?.dataset.chartView || (spec.type === 'line' ? 'line' : 'bar');
    window.Plotly.newPlot(graph, plotlyTraces(spec, view), plotlyLayout(spec, view), {
      responsive: true, scrollZoom: true, displaylogo: false,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    });
  }
}

function destroyPlotlyCharts(container) {
  container.querySelectorAll?.('.plotly-chart').forEach(graph => {
    if (graph.dataset.plotlyReady && window.Plotly) window.Plotly.purge(graph);
    chartSpecRegistry.delete(graph.id);
  });
}

function setChartView(button, view) {
  const chart = button.closest('.category-chart');
  const graph = chart?.querySelector('.plotly-chart');
  const panel = chart?.querySelector('.chart-data-panel');
  const spec = graph && chartSpecRegistry.get(graph.id);
  if (!chart || !graph || !panel || !spec) return;
  chart.querySelectorAll('.chart-view-button').forEach(item => {
    const active = item === button;
    item.classList.toggle('is-active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  const showData = view === 'table';
  graph.hidden = showData;
  panel.hidden = !showData;
  chart.dataset.chartView = view;
  if (!showData && window.Plotly) {
    window.Plotly.react(graph, plotlyTraces(spec, view), plotlyLayout(spec, view), {
      responsive: true, scrollZoom: true, displaylogo: false,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    });
  }
}

/* ── Minimal syntax highlighter ──────────────────────────────────────── */
function highlightCode(code) {
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let s = esc(code);

  // Strings
  s = s.replace(/(&#39;[^&#]*&#39;|&quot;[^&]*&quot;|`[^`]*`)/g,
    '<span class="syntax-string">$1</span>');

  // Keywords
  const kw = ['const','let','var','async','await','function','return','if','else',
    'for','while','try','catch','throw','new','import','export','from','class',
    'extends','typeof','instanceof','null','undefined','true','false','this'];
  kw.forEach(k => {
    s = s.replace(new RegExp(`\\b(${k})\\b`, 'g'),
      '<span class="syntax-keyword">$1</span>');
  });

  // Comments
  s = s.replace(/(\/\/[^\n]*)/g, '<span class="syntax-comment">$1</span>');

  // Numbers
  s = s.replace(/\b(\d+\.?\d*)\b/g, '<span class="syntax-number">$1</span>');

  // Function calls
  s = s.replace(/\b([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(/g,
    '<span class="syntax-call">$1</span>(');

  // Properties after dot
  s = s.replace(/\.([a-zA-Z_$][a-zA-Z0-9_$]*)/g,
    '.<span class="syntax-property">$1</span>');

  return s;
}

/* ── Copy code ───────────────────────────────────────────────────────── */
function copyCode(btn, code) {
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '✓ Copied';
    setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
  });
}

/* ── Escape HTML ────────────────────────────────────────────────────── */
function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Textarea auto-resize ────────────────────────────────────────────── */
function autoResize() {
  userInput.style.height = 'auto';
  userInput.style.height = Math.min(userInput.scrollHeight, 160) + 'px';
}
userInput.addEventListener('input', autoResize);

/* ── Keyboard handler ────────────────────────────────────────────────── */
userInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage(userInput.value);
  }
});

/* ── Send button ─────────────────────────────────────────────────────── */
sendBtn.addEventListener('click', () => sendMessage(userInput.value));

/* ── Suggested prompts (feature cards, rail, quick tools, follow-ups) ── */
async function runPrompt(prompt, agent) {
  if (isStreaming) return;
  // Agents keep separate conversations, so a prompt for the other agent
  // opens a fresh chat with that agent before sending.
  if (agent && agent !== selectedAgent) await switchAgent(agent);
  setSidebarOpen(false);
  await sendMessage(prompt);
}

promptButtons.forEach(btn => {
  btn.addEventListener('click', () => runPrompt(btn.dataset.prompt, btn.dataset.agent).catch(showError));
});

/* ── Init ────────────────────────────────────────────────────────────── */
loadProviders().catch((error) => {
  providerPicker.innerHTML = `<option value="">${escHtml(error.message)}</option>`;
  providerPicker.disabled = true;
  providerReady = true;
});
loadChats();
userInput.focus();
