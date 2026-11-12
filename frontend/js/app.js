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
const chatSearch = document.getElementById('chat-search');
const newChatButton = document.getElementById('new-chat-button');
const emptyState = document.getElementById('empty-state');
const messages   = document.getElementById('messages');
const chatScroll = document.getElementById('chat-scroll');
const userInput  = document.getElementById('user-input');
const sendBtn    = document.getElementById('send-btn');
const themeToggle = document.getElementById('theme-toggle');
const themeToggleIcon = document.getElementById('theme-toggle-icon');
const agentNavItems = document.querySelectorAll('.agent-option[data-agent]');
const providerPicker = document.getElementById('provider-picker');
const greeting = document.getElementById('greeting');
const welcomeAgentName = document.getElementById('welcome-agent-name');
const welcomeDescription = document.getElementById('welcome-description');
const chatHead = document.getElementById('chat-head');
const chatTitle = document.getElementById('chat-title');
const chatSub = document.getElementById('chat-sub');
const chatDeleteButton = document.getElementById('chat-delete');
const chatNewButton = document.getElementById('chat-new');
const chatBackButton = document.getElementById('chat-back');
const modelLabel = document.getElementById('model-label');
const modelPicker = document.getElementById('model-picker');
const accountName = document.getElementById('account-name');
const accountPlan = document.getElementById('account-plan');
const accountAvatars = document.querySelectorAll('.account-avatar');
const menuToggle = document.getElementById('menu-toggle');
const sidebarScrim = document.getElementById('sidebar-scrim');
const accountTrigger = document.getElementById('account-trigger');
const profileGate = document.getElementById('profile-gate');
const profileList = document.getElementById('profile-list');
const showCreateProfile = document.getElementById('show-create-profile');
const unlockForm = document.getElementById('profile-unlock-form');
const unlockName = document.getElementById('unlock-name');
const unlockPin = document.getElementById('unlock-pin');
const createForm = document.getElementById('profile-create-form');
const claimForm = document.getElementById('profile-claim-form');
const showClaimProfile = document.getElementById('show-claim-profile');
const googleSignIn = document.getElementById('google-sign-in');
const gateAccount = document.getElementById('gate-account');
const gateError = document.getElementById('gate-error');
const profileDialog = document.getElementById('profile-dialog');
const profileForm = document.getElementById('profile-form');
const profileFormStatus = document.getElementById('profile-form-status');
const consentForm = document.getElementById('consent-form');
const recoverForm = document.getElementById('profile-recover-form');
const recoveryCodeView = document.getElementById('recovery-code-view');
const recoverySaved = document.getElementById('recovery-saved');
const recoveryContinue = document.getElementById('recovery-continue');
const consentNotice = document.getElementById('consent-notice');
const gateCard = profileGate.querySelector('.gate-card');
const gateTitle = document.getElementById('gate-title');
const gateSub = document.getElementById('gate-sub');
const gateNote = document.getElementById('gate-note');
const dataZone = document.getElementById('data-zone');
const dataSummary = document.getElementById('data-summary');
const dataStatus = document.getElementById('data-status');
const settingsNotice = document.getElementById('settings-notice');
const planBrokers = document.getElementById('plan-brokers');
const planName = document.getElementById('plan-name');
const planUsage = document.getElementById('plan-usage');
const PROFILE_FIELDS = ['riskAppetite', 'ageBand', 'investmentHorizonYears', 'monthlyInvestmentInr', 'primaryGoal'];
// The sidebar lists this many recent chats; "See all" opens the full list.
const SIDEBAR_CHATS = 6;
let isConnected = false;
// True between a successful load of the signed-in, unlocked, consenting profile and the next loadChats().
let isSessionReady = false;
let currentUser = null;
let storageMode = 'device';
// The profile's plan and today's questions from /api/session (limit null: not capped).
let usage = null;
let privacyPolicyVersion = null;
let unlockTarget = null;
// True when the signed-in profile is locked after a restart (unlock with /api/session/unlock).
let isLockedSession = false;
// 'local': the shared-computer profile picker. 'google': each person signs in with Google, then their PIN unlocks.
let authMode = 'local';
// Google mode: the signed-in account's email, or a Google account waiting to create or claim its profile.
let sessionAccount = null;
// False when this copy has an admin page: backups are the operator's job, so the profile dialog hides them.
let backupsInApp = true;
const SIGN_IN_ERRORS = {
  cancelled: 'Google sign-in was cancelled.',
  expired: 'That sign-in took too long or started in another browser. Try again.',
  failed: 'Google sign-in could not be completed. Try again.',
};
// Set from ?signInError= when Google sends the browser back after a failed sign-in.
let signInError = (() => {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('signInError');
  if (!code) return '';
  params.delete('signInError');
  const query = params.toString();
  window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : '') + window.location.hash);
  return SIGN_IN_ERRORS[code] || SIGN_IN_ERRORS.failed;
})();
// Set from ?brokerError=<id> when a broker's own sign-in (Upstox, Dhan, Groww) did not complete.
(() => {
  const params = new URLSearchParams(window.location.search);
  const broker = params.get('brokerError');
  if (!broker) return;
  params.delete('brokerError');
  const query = params.toString();
  window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : '') + window.location.hash);
  window.addEventListener('load', () => {
    const message = broker === 'limit'
      ? 'The Free plan connects up to 2 brokers. Disconnect one, or see the plans at /pricing.'
      : `Could not connect ${broker.slice(0, 20)}. Open your profile and try Connect again.`;
    setTimeout(() => (typeof toast === 'function' ? toast(message) : alert(message)), 1500);
  });
})();
// Cloudflare Turnstile on sign-up: the site key from /api/session (null when off), the widget and its script.
let turnstileSiteKey = null;
let turnstileWidget = null;
let turnstileScript = null;
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
      : `${provider.label} · ${provider.setup || 'add API key in .env'}`;
    providerPicker.appendChild(option);
  }
  const configured = providers.filter((provider) => provider.configured);
  const savedProvider = configured.find((provider) => provider.id === selectedProvider);
  selectedProvider = savedProvider?.id || data.defaultProvider || 'openai';
  if (!configured.length) {
    providerPicker.innerHTML = '<option value="">Set OLLAMA_MODEL or an AI API key in .env</option>';
    providerPicker.disabled = true;
  } else {
    providerPicker.value = selectedProvider;
    providerPicker.disabled = false;
  }
  providerModels = Object.fromEntries(configured.map((provider) => [provider.id, provider.model || provider.label]));
  providerReady = true;
  localStorage.setItem('ca-mode-provider', selectedProvider);
  updateModelLabel();
}

// The composer shows the chosen model's short name ("ca-mode ▾"); the native select laid over it opens the list
// with the full "provider · model" names, and its title says the whole choice.
let providerModels = {};
function updateModelLabel() {
  const option = providerPicker.selectedOptions[0];
  const full = option?.textContent?.trim() || '';
  modelLabel.textContent = providerPicker.disabled ? 'No model' : (providerModels[providerPicker.value] || full || 'Model');
  modelPicker.title = full ? `AI model: ${full}` : 'AI model';
  modelPicker.classList.toggle('is-disabled', providerPicker.disabled);
}

providerPicker.addEventListener('change', () => {
  selectedProvider = providerPicker.value;
  localStorage.setItem('ca-mode-provider', selectedProvider);
  updateModelLabel();
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
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', selected === 'dark' ? '#070a08' : '#f4f7f5');
  localStorage.setItem('ca-mode-theme', selected);
}

// Dark is the default look; a new key so everyone sees it once, then their own choice sticks.
applyTheme(localStorage.getItem('ca-mode-theme') || 'dark');
themeToggle.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  // Plotly bakes colors into each chart, so redraw open charts for the new theme.
  document.querySelectorAll('.plotly-chart[data-plotly-ready]').forEach(graph => {
    const spec = chartSpecRegistry.get(graph.id);
    const view = graph.closest('.data-chart')?.dataset.chartView;
    if (!spec || !window.Plotly || view === 'table') return;
    // History charts take their line and shading colours from the theme too.
    if (spec.history || spec.page) window.Plotly.react(graph, plotlyTraces(spec, view), plotlyLayout(spec, view), plotlyConfig(spec));
    else window.Plotly.relayout(graph, plotlyLayout(spec, view));
  });
});

function updateGreeting() {
  const hour = new Date().getHours();
  const text = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
  greeting.textContent = text;
  document.querySelectorAll('.js-greeting').forEach((item) => { item.textContent = text; });
}
updateGreeting();

/* ── Phone navigation drawer (More in the bottom tab bar) ───────────── */
function setSidebarOpen(open) {
  const wasOpen = document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', open);
  sidebarScrim.hidden = !open;
  menuToggle.setAttribute('aria-expanded', String(open));
  if (open && !wasOpen) document.querySelector('#sidebar .nav-item')?.focus();
  else if (!open && wasOpen && document.getElementById('sidebar').contains(document.activeElement)) menuToggle.focus();
}
menuToggle.addEventListener('click', () => setSidebarOpen(!document.body.classList.contains('sidebar-open')));
sidebarScrim.addEventListener('click', () => setSidebarOpen(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setSidebarOpen(false);
  // Cmd+K (Ctrl+K elsewhere) jumps to the search field, unless a sign-in screen or dialog is open.
  if (event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey
    && !document.body.classList.contains('gate-open') && !document.querySelector('dialog[open]')) {
    event.preventDefault();
    chatSearch.focus();
    chatSearch.select();
  }
});
if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) document.getElementById('search-kbd').textContent = '⌘K';

/* ── Phone keyboard ───────────────────────────────────────────────────── */
// iOS shrinks only the visual viewport when the on-screen keyboard opens, which would leave the composer under
// the keyboard. While a text field is focused and the visible area is clearly shorter than the page, the app is
// sized to the visible area (--viewport-h) and the tab bar steps aside (body.keyboard-open).
if (window.visualViewport) {
  const viewport = window.visualViewport;
  let fullHeight = viewport.height;
  const fitKeyboard = () => {
    const typing = document.activeElement?.matches?.('textarea, input:not([type="checkbox"]):not([type="radio"]), select, [contenteditable="true"]');
    if (!typing) fullHeight = Math.max(viewport.height, document.documentElement.clientHeight);
    const open = Boolean(typing) && fullHeight - viewport.height > 120 && window.matchMedia('(max-width: 900px)').matches;
    const wasOpen = document.body.classList.contains('keyboard-open');
    document.body.classList.toggle('keyboard-open', open);
    // Where the browser already resized the page for the keyboard (Android), nothing needs sizing.
    const resize = open && document.documentElement.clientHeight - viewport.height > 1;
    document.documentElement.style.setProperty('--viewport-h', resize ? `${Math.round(viewport.height)}px` : '');
    if (resize) window.scrollTo(0, 0);
    if (open && !wasOpen && document.activeElement === userInput) requestAnimationFrame(scrollToBottom);
  };
  viewport.addEventListener('resize', fitKeyboard);
  document.addEventListener('focusin', () => setTimeout(fitKeyboard, 50));
  document.addEventListener('focusout', () => setTimeout(fitKeyboard, 50));
}

function agentName(agent = selectedAgent) {
  return agent === 'portfolio' ? 'Portfolio Analyzer' : 'Finance Agent';
}

function providerName() {
  return providerPicker.disabled ? '' : providerPicker.selectedOptions[0]?.textContent?.split(' · ')[0] || '';
}

// The line above an answer: agent, model and, when the trace shows one was read, the snapshot's date. The model
// comes from the answer's own trace (a saved chat may have used another one), else the current choice.
function answerMeta(trace = []) {
  const sources = answerSources(trace);
  const snapshot = snapshotLabel(sources.snapshot);
  return [agentName(), sources.provider || providerName(), snapshot ? `from the ${snapshot} snapshot` : ''].filter(Boolean).join(' · ');
}

function setAgent(agent) {
  selectedAgent = agent === 'portfolio' ? 'portfolio' : 'finance';
  const isPortfolio = selectedAgent === 'portfolio';
  agentNavItems.forEach((item) => item.setAttribute('aria-pressed', String(item.dataset.agent === selectedAgent)));
  welcomeAgentName.textContent = agentName();
  welcomeDescription.textContent = isPortfolio
    ? 'Ask about your holdings, allocations, and account activity at the brokers you connected.'
    : 'Ask basic finance questions and get clear explanations in plain language.';
  userInput.placeholder = isPortfolio ? 'Ask about your portfolio…' : 'Ask anything about your finances…';
  localStorage.setItem('ca-mode-agent', selectedAgent);
  renderChatHead();
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
  addMessage('assistant', '<span style="color:var(--error)">' + escHtml(errorText(error)) + '</span>');
}

/* ── Status helpers ─────────────────────────────────────────────────── */
function setStatus(state, label) {
  statusDot.className = 'status-dot ' + state;
  statusText.textContent = label;
}

function setBusy(busy) {
  isStreaming = busy;
  sendBtn.disabled = busy;
  document.querySelectorAll('button[data-prompt]').forEach((button) => { button.disabled = busy; });
  agentNavItems.forEach((item) => { item.disabled = busy; });
}

function renderAccount() {
  accountName.textContent = currentUser?.displayName || 'Guest';
  document.getElementById('switch-profile').textContent = authMode === 'google' ? 'Sign out' : 'Switch profile';
  const initial = (currentUser?.displayName || '?').trim().charAt(0).toUpperCase();
  accountAvatars.forEach((avatar) => { avatar.textContent = initial; });
  planName.textContent = `${usage?.planName || 'Free'} plan`;
  planUsage.textContent = usageText();
  planBrokers.textContent = usage && usage.brokersConnected != null ? `Brokers: ${usage.brokersConnected} of ${usage.brokerLimit == null ? 'any number' : usage.brokerLimit}` : '';
  accountPlan.textContent = [
    usage?.planName,
    isConnected ? 'INDmoney connected' : '',
    storageMode === 'private' ? 'Private' : '',
  ].filter(Boolean).join(' · ');
  accountPlan.title = accountPlan.textContent;
  accountAvatars.forEach((avatar) => avatar.classList.toggle('is-connected', isConnected));
}

function usageText() {
  if (!usage) return '';
  if (usage.credits) return creditsText(usage.credits);
  if (usage.limit == null) return 'Unlimited questions';
  const left = Math.max(0, usage.remaining);
  return `${left} of ${usage.limit} question${usage.limit === 1 ? '' : 's'} left today`;
}

// A paid plan's credits: "2,840 of 3,000 credits left · renews 14 Nov", plus any top-up credits.
function creditsText(credits) {
  const number = (value) => Number(value).toLocaleString('en-IN');
  const renews = new Date(`${credits.renewsOn}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  const topUp = credits.topUp > 0 ? ` + ${number(credits.topUp)} top-up` : '';
  return `${number(credits.monthlyRemaining)} of ${number(credits.monthly)} credits left${topUp} · renews ${renews}`;
}

// Credits left after an answer (X-Credits-Remaining, monthly plus top-up) or a refusal (0).
// Monthly credits are used before top-ups, so the top-up part only shrinks once the month's are gone.
function updateCreditsLeft(total) {
  const credits = usage?.credits;
  if (!credits || !Number.isFinite(total)) return;
  const left = Math.max(0, total);
  credits.topUp = Math.min(credits.topUp, left);
  credits.monthlyRemaining = left - credits.topUp;
  credits.used = credits.monthly - credits.monthlyRemaining;
  credits.remaining = left;
  renderAccount();
}

// Today's remaining questions after an answer (X-Questions-Remaining) or a daily-limit refusal (0).
function updateQuestionsLeft(remaining) {
  if (!usage || usage.limit == null || !Number.isFinite(remaining)) return;
  usage.remaining = Math.max(0, remaining);
  usage.used = usage.limit - usage.remaining;
  renderAccount();
}

/* ── Daily portfolio snapshots ──────────────────────────────────────── */
function formatCaptured(timestamp) {
  return new Date(Number(timestamp)).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// Saves a new snapshot from INDmoney, then tells the pages and the rail to reload their figures.
async function refreshSnapshot() {
  const response = await fetch('/api/snapshots/refresh', { method: 'POST' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not refresh the snapshot.');
  document.dispatchEvent(new CustomEvent('ca:snapshot'));
}

/* ── CAS statements (holdings at every broker) ──────────────────────── */
const casCard = document.getElementById('cas-card');
const casForm = document.getElementById('cas-form');
const casPill = document.getElementById('cas-pill');
const casCopy = document.getElementById('cas-copy');
const casStatus = document.getElementById('cas-status');
const casAsk = document.getElementById('cas-ask');
const CAS_DEFAULT_COPY = casCopy.textContent;

function renderCasStatements(statements) {
  const list = document.getElementById('cas-statements');
  list.innerHTML = '';
  if (!statements.length) return;
  // A list rather than a table, so each statement reads as one row on a phone too.
  const items = document.createElement('ul');
  items.className = 'cas-list';
  items.setAttribute('aria-label', 'Saved statements');
  for (const statement of statements) {
    const item = document.createElement('li');
    const count = Number(statement.holdings) || 0;
    item.innerHTML = `<span class="cas-item-main"><span class="cas-item-title">${escHtml(statement.source)} · ${escHtml(statement.periodTo)}</span>
      <small>${count} ${count === 1 ? 'holding' : 'holdings'} · ${escHtml(money(statement.valueInr))}</small></span>`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'table-action';
    remove.textContent = 'Delete';
    remove.setAttribute('aria-label', `Delete the ${statement.source} statement to ${statement.periodTo}`);
    remove.addEventListener('click', async () => {
      if (!window.confirm('Delete this saved statement?')) return;
      const response = await fetch('/api/cas/' + encodeURIComponent(statement.id), { method: 'DELETE' });
      if (response.ok) await loadCasStatus();
    });
    item.appendChild(remove);
    items.appendChild(item);
  }
  list.appendChild(items);
}

function renderCas(latest) {
  casPill.textContent = latest ? `As of ${latest.periodTo}` : 'No statement';
  casPill.classList.toggle('is-connected', Boolean(latest));
  casAsk.hidden = !latest;
  if (latest) {
    const brokers = latest.brokers.length;
    casCopy.textContent = `${money(latest.valueInr)} in ${latest.holdings} holdings across ${brokers} account${brokers === 1 ? '' : 's'} (${latest.source} statement). Upload a newer one each month.`;
  } else {
    casCopy.textContent = CAS_DEFAULT_COPY;
  }
}

async function loadCasStatus() {
  casCard.hidden = !currentUser;
  if (!currentUser) return;
  if (storageMode === 'private') {
    casForm.hidden = true;
    casAsk.hidden = true;
    casCopy.textContent = 'Private mode saves no history, so CAS statements cannot be uploaded.';
    return;
  }
  casForm.hidden = false;
  try {
    const response = await fetch('/api/cas');
    if (response.ok) {
      const statements = (await response.json()).statements || [];
      renderCas(statements[0]);
      renderCasStatements(statements);
    }
  } catch { /* The card keeps its default text. */ }
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(new Error('Could not read that file.'));
    reader.readAsDataURL(file);
  });
}

casForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = document.getElementById('cas-file').files[0];
  const password = document.getElementById('cas-password');
  if (!file) return;
  if (file.size > 15 * 1024 * 1024) {
    casStatus.classList.add('is-error');
    casStatus.textContent = 'That file is larger than 15 MB; a CAS is usually much smaller.';
    return;
  }
  const submit = casForm.querySelector('[type="submit"]');
  submit.disabled = true;
  casStatus.classList.remove('is-error');
  casStatus.textContent = 'Reading your statement…';
  try {
    const response = await fetch('/api/cas', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password.value, pdfBase64: await readAsBase64(file) }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Could not read that statement.');
    casForm.reset();
    await loadCasStatus();
    casStatus.textContent = data.ledger?.available
      ? 'Saved. The PDF and its password were not kept. Your mutual fund returns are on the Returns & tax page.'
      : `Saved. The PDF and its password were not kept. ${data.ledger?.hint || ''}`.trim();
  } catch (error) {
    casStatus.classList.add('is-error');
    casStatus.textContent = errorText(error);
  } finally {
    password.value = '';
    submit.disabled = false;
  }
});

casAsk.addEventListener('click', () => {
  runPrompt('Using my latest CAS statement, list my holdings grouped by broker with the value at each.', 'portfolio').catch(showError);
});

/* ── Load this signed-in user's conversations ───────────────────────── */
async function loadChats() {
  // Pages and the rail drop the previous profile's figures until this load finishes.
  isSessionReady = false;
  document.dispatchEvent(new CustomEvent('ca:session-ended'));
  try {
    const sessionResponse = await fetch('/api/session');
    if (!sessionResponse.ok) throw new Error('Could not initialize your local session.');
    const session = await sessionResponse.json();
    currentUser = session.user;
    isConnected = Boolean(session.connected);
    storageMode = session.storageMode || 'device';
    usage = session.usage || null;
    authMode = session.authMode === 'google' ? 'google' : 'local';
    sessionAccount = session.account || null;
    backupsInApp = session.backupsInApp !== false;
    turnstileSiteKey = session.turnstileSiteKey || null;
    renderAccount();
    if (!currentUser) {
      chats = [];
      activeChatId = null;
      resetConversationView();
      renderChats();
      if (authMode === 'google') {
        showAccountGate(sessionAccount);
        setStatus('online', 'Sign in');
      } else {
        showProfileGate(session.profiles || []);
        setStatus('online', 'Choose a profile');
      }
      return;
    }
    if (session.locked) {
      chats = [];
      activeChatId = null;
      resetConversationView();
      renderChats();
      showLockedGate();
      setStatus('online', 'Locked');
      return;
    }
    if (session.consentRequired) {
      chats = [];
      activeChatId = null;
      resetConversationView();
      renderChats();
      await showConsentGate();
      setStatus('online', 'Review privacy notice');
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
    setStatus('online', statusLabel());
    loadCasStatus();
    isSessionReady = true;
    document.dispatchEvent(new CustomEvent('ca:session'));
  } catch (err) {
    chatList.innerHTML = '<div class="tools-error">Could not load chats<br><small>' + escHtml(errorText(err)) + '</small></div>';
    setStatus('error', 'Connection failed');
  }
}

/* ── Profile picker ─────────────────────────────────────────────────── */
function showGateError(message) {
  gateError.textContent = message || '';
  gateError.hidden = !message;
}

const GATE_TEXT = {
  default: ["Who's using CA Mode?", 'Each person gets their own chats, broker connections, and daily portfolio snapshots.'],
  google: ['Welcome to CA Mode', 'Sign in with your Google account. Your PIN then unlocks your own chats, broker connections and portfolio history.'],
  'account-create': ['Create your profile', 'Choose a name and a PIN. The PIN encrypts your saved history, and you enter it each time you sign in.'],
  claim: ['Claim your profile', 'Enter the name and PIN of the profile you used on the shared computer. From now on it signs in with this Google account.'],
  locked: ['Welcome back', 'Enter your PIN to unlock your saved chats and portfolio history.'],
  recover: ['Reset your PIN', 'Use the recovery code you saved when you created this profile.'],
  'recovery-code': ['Save your recovery code', "If you forget your PIN, this code is the only way back into your saved history. Nobody can reset it for you."],
  consent: ['Before you start', 'Here is what CA Mode keeps, where, and what it sends elsewhere. Choose how much history to keep.'],
};

const GATE_NOTES = {
  local: "Your PIN encrypts your saved history, so choose one other people on this computer can't guess. It is not a substitute for a device password.",
  google: 'Your PIN encrypts your saved history, so choose one nobody else can guess. Signing in with Google alone does not unlock it, and nobody can reset it for you.',
};

function showGateView(view) {
  const isConsent = view === 'consent';
  const isGoogle = authMode === 'google';
  profileList.hidden = view !== 'list';
  showCreateProfile.hidden = view !== 'list';
  googleSignIn.hidden = view !== 'google';
  unlockForm.hidden = view !== 'unlock';
  createForm.hidden = view !== 'create';
  claimForm.hidden = view !== 'claim';
  showClaimProfile.hidden = !(isGoogle && view === 'create' && sessionAccount?.canClaim);
  recoverForm.hidden = view !== 'recover';
  recoveryCodeView.hidden = view !== 'recovery-code';
  consentForm.hidden = !isConsent;
  gateNote.hidden = !['list', 'unlock', 'create', 'claim'].includes(view);
  gateNote.textContent = GATE_NOTES[authMode];
  const email = isGoogle && ['create', 'claim', 'unlock'].includes(view) ? sessionAccount?.email : '';
  gateAccount.hidden = !email;
  gateAccount.textContent = email ? `Signed in with Google as ${email}` : '';
  document.getElementById('create-title').hidden = isGoogle;
  gateCard.classList.toggle('is-consent', isConsent);
  const viewText = isGoogle && view === 'create' ? 'account-create' : view;
  const textKey = view === 'unlock' && isLockedSession ? 'locked' : (GATE_TEXT[viewText] ? viewText : 'default');
  const [title, sub] = GATE_TEXT[textKey];
  const name = currentUser?.displayName;
  gateTitle.textContent = name && ['locked', 'consent'].includes(textKey) ? `${title}, ${name}` : title;
  gateSub.textContent = sub;
  showGateError('');
  if (view === 'create') showTurnstile();
  const focusTargets = {
    consent: consentNotice, unlock: unlockPin, create: document.getElementById('create-name'),
    google: document.getElementById('google-sign-in-button'), claim: document.getElementById('claim-name'),
    recover: document.getElementById('recover-code'), 'recovery-code': recoverySaved,
  };
  (focusTargets[view] || profileList.querySelector('button'))?.focus();
}

function openGate() {
  profileGate.hidden = false;
  document.body.classList.add('gate-open');
}

function showLockedGate() {
  isLockedSession = true;
  unlockTarget = currentUser;
  unlockName.textContent = currentUser.displayName;
  unlockForm.reset();
  unlockForm.querySelector('[data-gate-back]').textContent = authMode === 'google' ? 'Sign out' : 'Switch profile';
  openGate();
  showGateView('unlock');
}

function showRecoveryCode(code) {
  document.getElementById('recovery-code-value').textContent = code;
  recoverySaved.checked = false;
  recoveryContinue.disabled = true;
  openGate();
  showGateView('recovery-code');
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'Copied';
  } catch {
    button.textContent = 'Select and copy';
  }
  setTimeout(() => { button.textContent = 'Copy'; }, 1600);
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
  isLockedSession = false;
  unlockForm.querySelector('[data-gate-back]').textContent = 'Back';
  openGate();
  showGateView(profiles.length ? 'list' : 'create');
  createForm.querySelector('[data-gate-back]').hidden = !profiles.length;
}

// Google mode, not signed in to a profile: the Google button, or create/claim for an account that has none yet.
function showAccountGate(account) {
  isLockedSession = false;
  const back = createForm.querySelector('[data-gate-back]');
  back.hidden = false;
  back.textContent = 'Use another account';
  openGate();
  if (account?.pending) {
    const nameInput = document.getElementById('create-name');
    if (!nameInput.value) nameInput.value = account.name || '';
    showGateView('create');
  } else {
    showGateView('google');
  }
  if (signInError) {
    showGateError(signInError);
    signInError = '';
  }
}

function hideProfileGate() {
  profileGate.hidden = true;
  document.body.classList.remove('gate-open');
  unlockForm.reset();
  createForm.reset();
  claimForm.reset();
  recoverForm.reset();
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
    // A rate limit (rateLimited) carries its own message; otherwise a 429 is the PIN lockout.
    if (response.status === 429 && !data.rateLimited) {
      const minutes = Math.ceil(Number(data.retryAfterSeconds || response.headers.get('Retry-After') || 300) / 60);
      throw new Error(`Too many incorrect attempts. Try again in ${minutes} minute${minutes === 1 ? '' : 's'}.`);
    }
    if (!response.ok) {
      // 404/405 here usually means the server is older than this page: restart it.
      throw new Error(data.error || `Could not sign in (HTTP ${response.status}). If you just updated CA Mode, restart the server.`);
    }
    if (data.recoveryCode) {
      currentUser = data.user || currentUser;
      showRecoveryCode(data.recoveryCode);
      return;
    }
    await loadChats();
  } catch (error) {
    showGateError(errorText(error));
  } finally {
    submit.disabled = false;
  }
}

showCreateProfile.addEventListener('click', () => showGateView('create'));
profileGate.querySelectorAll('[data-gate-back]').forEach(button => button.addEventListener('click', async () => {
  // Google mode has no list to go back to: going back signs out (and forgets an account waiting for a profile).
  if (!isLockedSession && authMode !== 'google') {
    showGateView('list');
    return;
  }
  await fetch('/api/session/logout', { method: 'POST' });
  await loadChats();
}));
unlockForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (isLockedSession) submitGate('/api/session/unlock', { pin: unlockPin.value }, unlockForm);
  else if (unlockTarget) submitGate('/api/session/login', { userId: unlockTarget.id, pin: unlockPin.value }, unlockForm);
});
document.getElementById('show-recover').addEventListener('click', () => {
  if (!unlockTarget) return;
  recoverForm.reset();
  document.getElementById('recover-name').textContent = unlockTarget.displayName;
  showGateView('recover');
});
recoverForm.querySelector('[data-recover-back]').addEventListener('click', () => showGateView('unlock'));
recoverForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const newPin = document.getElementById('recover-pin').value;
  if (newPin !== document.getElementById('recover-pin-confirm').value) {
    showGateError('The two PINs do not match.');
    return;
  }
  submitGate('/api/session/recover', {
    userId: unlockTarget.id, recoveryCode: document.getElementById('recover-code').value, newPin,
  }, recoverForm);
});
recoverySaved.addEventListener('change', () => { recoveryContinue.disabled = !recoverySaved.checked; });
document.getElementById('copy-recovery-code').addEventListener('click', (event) => {
  copyText(document.getElementById('recovery-code-value').textContent, event.currentTarget);
});
recoveryContinue.addEventListener('click', async () => {
  document.getElementById('recovery-code-value').textContent = '';
  await loadChats();
});
document.getElementById('google-sign-in-button').addEventListener('click', (event) => {
  event.currentTarget.disabled = true;
  window.location.assign('/auth/google/start');
});
showClaimProfile.addEventListener('click', () => {
  claimForm.reset();
  showGateView('claim');
});
claimForm.querySelector('[data-claim-back]').addEventListener('click', () => showGateView('create'));
claimForm.addEventListener('submit', (event) => {
  event.preventDefault();
  submitGate('/api/account/claim', {
    displayName: document.getElementById('claim-name').value, pin: document.getElementById('claim-pin').value,
  }, claimForm);
});
createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const pin = document.getElementById('create-pin').value;
  if (pin !== document.getElementById('create-pin-confirm').value) {
    showGateError('The two PINs do not match.');
    return;
  }
  const body = { displayName: document.getElementById('create-name').value, pin };
  if (turnstileSiteKey) {
    body.turnstileToken = turnstileToken();
    if (!body.turnstileToken) {
      showGateError('Complete the check below, then create your profile.');
      return;
    }
  }
  await submitGate('/api/users', body, createForm);
  resetTurnstile(); // Each Turnstile response can be used once.
});

/* ── Cloudflare Turnstile on sign-up (only when the server sends a site key) ── */
function loadTurnstile() {
  turnstileScript ||= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.onload = () => resolve(window.turnstile);
    script.onerror = () => {
      turnstileScript = null;
      script.remove();
      reject(new Error('Could not load the sign-up check. Check your connection and try again.'));
    };
    document.head.appendChild(script);
  });
  return turnstileScript;
}

async function showTurnstile() {
  const slot = document.getElementById('create-turnstile');
  slot.hidden = !turnstileSiteKey;
  if (!turnstileSiteKey || turnstileWidget !== null) return;
  try {
    const turnstile = await loadTurnstile();
    turnstileWidget = turnstile.render(slot, { sitekey: turnstileSiteKey, action: 'sign_up' });
  } catch (error) {
    showGateError(errorText(error));
  }
}

function turnstileToken() {
  return turnstileWidget === null ? null : (window.turnstile.getResponse(turnstileWidget) || null);
}

function resetTurnstile() {
  if (turnstileWidget !== null) window.turnstile.reset(turnstileWidget);
}

/* ── Privacy notice and storage choices ─────────────────────────────── */
function renderNotice(container, notice) {
  container.innerHTML = '';
  for (const item of notice || []) {
    const section = document.createElement('section');
    const title = document.createElement('h3');
    title.textContent = item.title;
    const body = document.createElement('p');
    body.textContent = item.body;
    section.append(title, body);
    container.appendChild(section);
  }
}

function setStorageChoice(form, name, mode, retentionDays) {
  const selected = form.querySelector(`input[name="${name}"][value="${mode === 'private' ? 'private' : 'device'}"]`);
  if (selected) selected.checked = true;
  const retention = form.querySelector('[data-retention-field] select');
  if (retention && retentionDays) retention.value = String(retentionDays);
  syncRetentionField(form, name);
}

function readStorageChoice(form, name) {
  return {
    storageMode: form.querySelector(`input[name="${name}"]:checked`)?.value || 'device',
    snapshotRetentionDays: Number(form.querySelector('[data-retention-field] select').value),
  };
}

function syncRetentionField(form, name) {
  form.querySelector('[data-retention-field]').hidden = readStorageChoice(form, name).storageMode === 'private';
}

async function loadPrivacy() {
  const response = await fetch('/api/privacy');
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not load the privacy notice.');
  privacyPolicyVersion = data.policyVersion;
  return data;
}

async function savePrivacyChoice(choice) {
  const response = await fetch('/api/privacy/consent', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ policyVersion: privacyPolicyVersion, ...choice }),
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 409) {
    throw new Error('The privacy notice changed while this page was open. Read the updated notice and try again.');
  }
  if (!response.ok) throw new Error(data.error || 'Could not save your data settings.');
  return data;
}

async function showConsentGate() {
  profileGate.hidden = false;
  document.body.classList.add('gate-open');
  showGateView('consent');
  consentForm.reset();
  try {
    const privacy = await loadPrivacy();
    renderNotice(consentNotice, privacy.notice);
    setStorageChoice(consentForm, 'consent-storage', privacy.storageMode, privacy.snapshotRetentionDays);
  } catch (error) {
    showGateError(errorText(error));
  }
}

consentForm.querySelectorAll('input[name="consent-storage"]').forEach(input => input.addEventListener('change', () => syncRetentionField(consentForm, 'consent-storage')));
consentForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = consentForm.querySelector('[type="submit"]');
  submit.disabled = true;
  showGateError('');
  try {
    await savePrivacyChoice(readStorageChoice(consentForm, 'consent-storage'));
    await loadChats();
  } catch (error) {
    showGateError(errorText(error));
    if (error.message.startsWith('The privacy notice changed')) await showConsentGate();
  } finally {
    submit.disabled = false;
  }
});
document.getElementById('consent-sign-out').addEventListener('click', async () => {
  await fetch('/api/session/logout', { method: 'POST' });
  await loadChats();
});

/* ── Profile settings dialog ────────────────────────────────────────── */
async function openProfileDialog() {
  if (!currentUser || isStreaming) return;
  profileFormStatus.textContent = '';
  document.getElementById('delete-pin').value = '';
  document.getElementById('export-pin').value = '';
  dataStatus.textContent = '';
  document.getElementById('settings-recovery-value').textContent = '';
  document.getElementById('settings-recovery').hidden = true;
  profileForm.querySelector('.danger-zone').open = false;
  dataZone.open = false;
  const [response, privacy] = await Promise.all([fetch('/api/profile'), loadPrivacy()]);
  brokersStatus.textContent = '';
  loadBrokers();
  document.querySelector('.backup-row').hidden = !backupsInApp;
  if (backupsInApp) loadBackupStatus();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not load your profile.');
  renderDataZone(privacy);
  document.getElementById('profile-dialog-title').textContent = data.displayName;
  for (const field of PROFILE_FIELDS) {
    document.getElementById(`field-${field}`).value = data.profile?.[field] ?? '';
  }
  profileDialog.showModal();
}

[accountTrigger, document.getElementById('topbar-account')].forEach((trigger) => trigger.addEventListener('click', () => {
  setSidebarOpen(false);
  openProfileDialog().catch(showError);
}));
profileDialog.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => profileDialog.close()));

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  // Only the Save button submits; Enter in a field submits through it too.
  const button = profileForm.querySelector('.dialog-save');
  profileFormStatus.textContent = '';
  const body = {};
  for (const field of PROFILE_FIELDS) {
    const input = document.getElementById(`field-${field}`);
    const value = input.value.trim();
    if (value === '') continue;
    body[field] = input.type === 'number' ? Number(value) : value;
  }
  await withPending(button, profileFormStatus, async () => {
    const response = await fetch('/api/profile', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(Array.isArray(data.detail) ? 'Check the highlighted values and try again.' : plainError(response.status, data));
    }
    profileFormStatus.textContent = 'Saved.';
    setTimeout(() => profileDialog.close(), 600);
  });
});

function renderDataZone(privacy) {
  const accepted = privacy.acceptedAt ? ` You agreed to the current notice on ${new Date(privacy.acceptedAt).toLocaleDateString()}.` : '';
  dataSummary.textContent = (privacy.storageMode === 'private'
    ? 'Private mode: chats stay in memory until you sign out, and no portfolio snapshots are saved.'
    : `Saving history: chats for 30 days and portfolio snapshots for ${privacy.snapshotRetentionDays} days.`) + accepted;
  renderNotice(settingsNotice, privacy.notice);
  setStorageChoice(profileForm, 'settings-storage', privacy.storageMode, privacy.snapshotRetentionDays);
}

profileForm.querySelectorAll('input[name="settings-storage"]').forEach(input => input.addEventListener('change', () => syncRetentionField(profileForm, 'settings-storage')));

document.getElementById('save-data-settings').addEventListener('click', async (event) => {
  const choice = readStorageChoice(profileForm, 'settings-storage');
  if (choice.storageMode === 'private' && storageMode !== 'private'
    && !window.confirm('Private mode deletes the chats and portfolio snapshots saved for this profile. Continue?')) return;
  dataStatus.textContent = '';
  await withPending(event.currentTarget, dataStatus, async () => {
    renderDataZone(await savePrivacyChoice(choice));
    dataStatus.textContent = 'Data settings saved.';
    await loadChats();
  });
});

document.getElementById('export-data').addEventListener('click', async (event) => {
  const pin = document.getElementById('export-pin').value;
  if (!pin) {
    dataStatus.textContent = 'Enter your PIN to export your data.';
    return;
  }
  dataStatus.textContent = '';
  await withPending(event.currentTarget, dataStatus, async () => {
    const response = await fetch('/api/users/me/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(plainError(response.status, data));
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    link.download = `ca-mode-export-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    // Revoking straight away can cancel the download in some browsers.
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    document.getElementById('export-pin').value = '';
    dataStatus.textContent = 'Export downloaded. It contains your chats and portfolio data, so keep it somewhere safe.';
  });
});

document.getElementById('new-recovery-code').addEventListener('click', async (event) => {
  const pin = document.getElementById('export-pin').value;
  if (!pin) {
    dataStatus.textContent = 'Enter your PIN to create a new recovery code.';
    return;
  }
  if (!window.confirm('Create a new recovery code? Your current recovery code will stop working.')) return;
  dataStatus.textContent = '';
  await withPending(event.currentTarget, dataStatus, async () => {
    const response = await fetch('/api/users/me/recovery-code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(plainError(response.status, data));
    document.getElementById('export-pin').value = '';
    document.getElementById('settings-recovery-value').textContent = data.recoveryCode;
    document.getElementById('settings-recovery').hidden = false;
    dataStatus.textContent = 'Save this code somewhere safe now. It will not be shown again, and your old code no longer works.';
  });
});
document.getElementById('copy-settings-recovery').addEventListener('click', (event) => {
  copyText(document.getElementById('settings-recovery-value').textContent, event.currentTarget);
});

function renderBackupStatus(status) {
  const where = status.directory || 'the backup folder';
  const last = status.latest
    ? `Last backup ${new Date(status.latest).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}`
    : 'No backups yet';
  const schedule = status.enabled ? 'A backup is made every day' : 'Daily backups are off';
  document.getElementById('backup-status').textContent = `${last}. ${schedule}; the last ${status.keep} are kept in ${where}.`;
}

async function loadBackupStatus() {
  try {
    const response = await fetch('/api/backups');
    if (response.ok) renderBackupStatus(await response.json());
  } catch { /* The rest of the dialog still works. */ }
}

document.getElementById('backup-now').addEventListener('click', async (event) => {
  dataStatus.textContent = '';
  await withPending(event.currentTarget, dataStatus, async () => {
    const response = await fetch('/api/backups', { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(plainError(response.status, data));
    renderBackupStatus(data);
    dataStatus.textContent = `Backed up to ${data.file}.`;
  });
});

document.getElementById('clear-history').addEventListener('click', async (event) => {
  if (!window.confirm('Delete all chats and portfolio snapshots for this profile? Your profile and broker connections are kept.')) return;
  dataStatus.textContent = '';
  await withPending(event.currentTarget, dataStatus, async () => {
    const response = await fetch('/api/users/me/history', { method: 'DELETE' });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (data.unlockRequired) {
        profileDialog.close();
        await loadChats();
        return;
      }
      throw new Error(plainError(response.status, data.error ? data : { error: 'Could not clear your history.' }));
    }
    profileDialog.close();
    await loadChats();
  });
});

document.getElementById('switch-profile').addEventListener('click', async (event) => {
  profileFormStatus.textContent = '';
  await withPending(event.currentTarget, profileFormStatus, async () => {
    await fetch('/api/session/logout', { method: 'POST' });
    profileDialog.close();
    await loadChats();
  });
});

document.getElementById('delete-profile').addEventListener('click', async (event) => {
  const deleteStatus = document.getElementById('delete-status');
  const pin = document.getElementById('delete-pin').value;
  if (!pin) {
    deleteStatus.textContent = 'Enter your PIN to delete this profile.';
    return;
  }
  deleteStatus.textContent = '';
  await withPending(event.currentTarget, deleteStatus, async () => {
    const response = await fetch('/api/users/me', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(plainError(response.status, data));
    profileDialog.close();
    await loadChats();
  });
});

// Opens the profile dialog at its Brokers list, where every broker (INDmoney included) is connected.
async function openBrokers() {
  await openProfileDialog();
  brokersSection.scrollIntoView?.({ block: 'start' });
}

// INDmoney keeps its own sign-in and disconnect routes; the list treats it like any other broker.
async function disconnectIndmoney() {
  if (!window.confirm('Disconnect INDmoney? Saved portfolio snapshots for this profile will be deleted; your chats are kept.')) return false;
  const response = await fetch('/auth/indmoney/disconnect', { method: 'POST' });
  if (!response.ok) throw new Error(plainError(response.status, await response.json().catch(() => ({}))));
  await loadChats();
  return true;
}

/* ── Brokers (profile dialog) ───────────────────────────────────────── */
// A broker login happens on the broker's own site: the server returns its link, the user opens it, and we poll
// the status until it reads connected. The link is shown as an anchor because a tab opened after an await is blocked.
const brokersSection = document.getElementById('brokers-section');
const brokersList = document.getElementById('brokers-list');
const brokersStatus = document.getElementById('brokers-status');
const BROKER_POLL_MS = 3000;
const BROKER_MAX_POLLS = 100;
let brokerPollTimer = null;
let lastBrokers = [];
let brokerLimit = null;
let brokersConnected = 0;
const brokerLoginLinks = {};
let pendingBrokers = [];

function stopBrokerPolling() {
  clearTimeout(brokerPollTimer);
  brokerPollTimer = null;
}

function brokerButtons(broker) {
  const id = escHtml(broker.broker);
  const link = brokerLoginLinks[broker.broker];
  const button = (action, label) => `<button class="gate-secondary" type="button" data-broker-action="${action}" data-broker="${id}">${label}</button>`;
  const oauthLink = (label) => `<a class="gate-secondary" href="/auth/brokers/${encodeURIComponent(broker.broker)}/connect">${label}</a>`;
  if (broker.auth === 'indmoney_oauth') {
    const link = (label) => `<a class="gate-secondary" href="/auth/indmoney/connect">${label}</a>`;
    if (broker.status === 'connected') return button('disconnect', 'Disconnect');
    if (broker.status === 'needs_login') return link('Reconnect') + button('disconnect', 'Disconnect');
    return link('Connect');
  }
  if (broker.auth === 'oauth') {
    if (broker.status === 'connected') return button('disconnect', 'Disconnect');
    if (broker.status === 'needs_login' || broker.status === 'unavailable') return oauthLink('Reconnect') + button('disconnect', 'Disconnect');
    return oauthLink('Connect');
  }
  if (broker.auth === 'mcp_login_qr') {
    const ucc = `<input class="broker-ucc" type="text" maxlength="5" autocomplete="off" spellcheck="false" placeholder="Client code" aria-label="${escHtml(broker.name)} client code (UCC)" data-broker-ucc="${id}">`;
    if (broker.status === 'pending') {
      return (link ? `<a class="gate-secondary" href="${escHtml(link)}" target="_blank" rel="noopener noreferrer">Open QR login</a>` : '')
        + button('verify', "I've scanned the QR") + button('disconnect', 'Cancel');
    }
    if (broker.status === 'connected') return button('disconnect', 'Disconnect');
    return ucc + button('connect', broker.status === 'disconnected' ? 'Connect' : 'Reconnect')
      + (broker.status === 'disconnected' ? '' : button('disconnect', 'Disconnect'));
  }
  if (broker.status === 'pending') {
    return (link ? `<a class="gate-secondary" href="${escHtml(link)}" target="_blank" rel="noopener noreferrer">Open ${escHtml(broker.name)} login</a>` : button('connect', 'Get login link'))
      + button('disconnect', 'Cancel');
  }
  if (broker.status === 'connected') return button('disconnect', 'Disconnect');
  if (broker.status === 'needs_login' || broker.status === 'unavailable') return button('connect', 'Reconnect') + button('disconnect', 'Disconnect');
  return button('connect', 'Connect');
}

const BROKER_TOOL_ACCESS = {
  read: ['Read-only', 'CA Mode and the assistant can use this.'],
  not_enabled: ['Not enabled', 'Not reviewed yet, so CA Mode never calls it.'],
  write: ['Blocked', 'Changes your account (orders, watchlists or alerts), so CA Mode never calls it.'],
};
const brokerToolsOpen = new Set();
const brokerToolsHtml = {};

function brokerToolsPanel(id) {
  return `<div class="broker-tools" id="broker-tools-${escHtml(id)}">${brokerToolsHtml[id] || '<p class="dialog-sub">Loading the tools…</p>'}</div>`;
}

function renderBrokerTools(data) {
  const tools = data.tools || [];
  if (data.needsConnection) return `<p class="dialog-sub">Connect ${escHtml(data.name)} to see the tools its server offers.</p>`;
  if (!tools.length) return `<p class="dialog-sub">${escHtml(data.name)}'s server lists no tools.</p>`;
  const usable = tools.filter((tool) => tool.access === 'read').length;
  return `<p class="dialog-sub broker-head">${brokerLogoHtml(data.broker, data.name, null)}<span>${tools.length} tools on ${escHtml(data.name)}'s MCP server; CA Mode uses the ${usable} read-only ones.</span></p>
    <ul class="broker-tool-list">${tools.map((tool) => {
      const [label, why] = BROKER_TOOL_ACCESS[tool.access] || BROKER_TOOL_ACCESS.not_enabled;
      return `<li><span class="broker-tool-head"><code>${escHtml(tool.name)}</code><span class="pill broker-tool-${escHtml(tool.access)}" title="${escHtml(why)}">${label}</span></span>${tool.description ? `<small>${escHtml(tool.description)}</small>` : ''}</li>`;
    }).join('')}</ul>`;
}

async function loadBrokerTools(id) {
  try {
    const response = await fetch(`/api/brokers/${encodeURIComponent(id)}/tools`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Could not read the tool list.');
    brokerToolsHtml[id] = renderBrokerTools(data);
  } catch (error) {
    delete brokerToolsHtml[id];
    const panel = document.getElementById(`broker-tools-${id}`);
    if (panel) panel.innerHTML = `<p class="dialog-sub">${escHtml(errorText(error))}</p>`;
    return;
  }
  const panel = document.getElementById(`broker-tools-${id}`);
  if (panel) panel.innerHTML = brokerToolsHtml[id];
}

function renderBrokers(brokers) {
  brokersSection.hidden = !brokers.length;
  const blocked = (broker) => brokerConnectBlocked(broker, brokerLimit, brokersConnected);
  brokersList.innerHTML = brokers.map((broker) => {
    const open = brokerToolsOpen.has(broker.broker);
    const actions = blocked(broker)
      ? `<button class="gate-secondary" type="button" disabled title="${escHtml(LIMIT_NOTE)}">Connect</button>`
      : brokerButtons(broker);
    return `<div class="broker-item"><div class="connection-row">
      <span class="broker-head">${brokerLogoHtml(broker.broker, broker.name, broker.logo)}<span><button class="broker-name" type="button" data-broker-tools="${escHtml(broker.broker)}" aria-expanded="${open}" title="Show the tools ${escHtml(broker.name)}'s server exposes"><strong>${escHtml(broker.name)}</strong><span aria-hidden="true">${open ? '▾' : '▸'}</span></button><small>${escHtml(brokerStatusLabel(broker.status, broker.holdingsAt))}</small></span></span>
      <span class="broker-actions">${actions}</span>
    </div>${open ? brokerToolsPanel(broker.broker) : ''}</div>`;
  }).join('');
  const limitLine = document.getElementById('brokers-limit');
  const atLimit = brokers.some(blocked);
  limitLine.hidden = !atLimit;
  limitLine.innerHTML = atLimit ? `${escHtml(LIMIT_NOTE.replace(' or see plans.', ' or '))}<a href="/pricing" target="_blank" rel="noopener">see plans</a>.` : '';
  pendingBrokers = brokers.filter((broker) => broker.status === 'pending' && broker.auth !== 'mcp_login_qr').map((broker) => broker.broker);
  if (pendingBrokers.length) pollBrokers(0);
}

async function loadBrokers() {
  try {
    const response = await fetch('/api/brokers');
    if (!response.ok) {
      brokersSection.hidden = true;  // Not offered here (for example in the demo).
      return;
    }
    const body = await response.json();
    lastBrokers = body.brokers || [];
    brokerLimit = body.limit ?? null;
    brokersConnected = body.connected ?? 0;
    renderBrokers(lastBrokers);
  } catch {
    brokersSection.hidden = true;
  }
}

function pollBrokers(count) {
  stopBrokerPolling();
  if (count >= BROKER_MAX_POLLS) return;
  brokerPollTimer = setTimeout(async () => {
    if (!profileDialog.open || document.hidden) return;
    let settled = true;
    for (const id of pendingBrokers) {
      try {
        const response = await fetch(`/api/brokers/${encodeURIComponent(id)}/status`);
        const status = response.ok ? (await response.json()).status : 'pending';
        if (status === 'pending') settled = false;
        else if (status === 'connected') {
          delete brokerLoginLinks[id];
          // Fetch and save the first holdings, then let the pages show them.
          fetch(`/api/brokers/${encodeURIComponent(id)}/holdings?refresh=1`).finally(() => document.dispatchEvent(new CustomEvent('ca:holdings')));
        }
      } catch { settled = false; }
    }
    if (settled) await loadBrokers();
    else pollBrokers(count + 1);
  }, BROKER_POLL_MS);
}

brokersList.addEventListener('click', async (event) => {
  const toggle = event.target.closest('[data-broker-tools]');
  if (toggle) {
    const id = toggle.dataset.brokerTools;
    if (brokerToolsOpen.has(id)) brokerToolsOpen.delete(id);
    else brokerToolsOpen.add(id);
    renderBrokers(lastBrokers);
    if (brokerToolsOpen.has(id) && !brokerToolsHtml[id]) loadBrokerTools(id);
    return;
  }
  const button = event.target.closest('[data-broker-action]');
  if (!button) return;
  const id = button.dataset.broker;
  brokersStatus.textContent = '';
  button.disabled = true;
  try {
    if (button.dataset.brokerAction === 'disconnect') {
      if (id === 'indmoney') {
        if (!(await disconnectIndmoney())) return;
      } else {
        const response = await fetch(`/api/brokers/${encodeURIComponent(id)}/disconnect`, { method: 'POST' });
        if (!response.ok) throw new Error(plainError(response.status, await response.json().catch(() => ({}))));
      }
      delete brokerLoginLinks[id];
      document.dispatchEvent(new CustomEvent('ca:holdings'));
    } else {
      const verifying = button.dataset.brokerAction === 'verify';
      const input = brokersList.querySelector(`[data-broker-ucc="${CSS.escape(id)}"]`);
      const response = await fetch(`/api/brokers/${encodeURIComponent(id)}/${verifying ? 'verify' : 'connect'}`, {
        method: 'POST',
        ...(input ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ucc: input.value.trim() }) } : {}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(plainError(response.status, data.error ? data : { error: 'Could not start the login.' }));
      if (data.loginUrl) brokerLoginLinks[id] = data.loginUrl;
      if (verifying) {
        delete brokerLoginLinks[id];
        fetch(`/api/brokers/${encodeURIComponent(id)}/holdings?refresh=1`).finally(() => document.dispatchEvent(new CustomEvent('ca:holdings')));
      }
    }
    await loadBrokers();
  } catch (error) {
    brokersStatus.textContent = errorText(error);
  } finally {
    button.disabled = false;
  }
});

// A logo that fails to load becomes a monogram tile (error events do not bubble, so listen while capturing).
document.addEventListener('error', (event) => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement) || !image.classList.contains('broker-logo')) return;
  image.replaceWith(Object.assign(document.createElement('span'), { className: 'broker-logo broker-monogram', textContent: image.dataset.monogram || '?', ariaHidden: 'true' }));
}, true);

profileDialog.addEventListener('close', stopBrokerPolling);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopBrokerPolling();
  else if (profileDialog.open && pendingBrokers.length) pollBrokers(0);
});

/* ── Reconnect reminders ───────────────────────────────────────────────
   A banner on Home and Holdings (and a line in the rail) when a connected broker's login has ended or its holdings
   are over a day old. On load each connected broker's status is asked for at most once an hour, so an ended login
   is noticed. Dismissal lasts for the local day (localStorage, with a fallback when it is not available). */
const BROKER_CHECKED_KEY = 'ca-broker-status-checked';
let brokerAttention = [];
const brokerChecksMemory = {};
const dismissedBanners = new Set();
const bannerLinks = {};

function safeStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readBrokerChecks() {
  let stored = {};
  try {
    stored = JSON.parse(safeStorage().getItem(BROKER_CHECKED_KEY) || '{}') || {};
  } catch { /* none remembered */ }
  return { ...stored, ...brokerChecksMemory };
}

function writeBrokerChecks(checked) {
  Object.assign(brokerChecksMemory, checked);
  try {
    safeStorage().setItem(BROKER_CHECKED_KEY, JSON.stringify(checked));
  } catch { /* the in-memory copy still limits this page */ }
}

async function fetchBrokerList() {
  try {
    const response = await fetch('/api/brokers');
    if (!response.ok) return null;  // not offered here (for example in the demo)
    return (await response.json()).brokers || [];
  } catch {
    return null;
  }
}

function renderBrokerBanners() {
  const now = Date.now();
  const html = brokerAttention
    .filter((broker) => !dismissedBanners.has(broker.broker) && !bannerDismissed(safeStorage(), broker.broker, now))
    .map((broker) => {
      const id = escHtml(broker.broker);
      const link = bannerLinks[broker.broker];
      const action = link
        ? `<a class="gate-secondary" href="${escHtml(link)}" target="_blank" rel="noopener noreferrer">Open ${escHtml(broker.name)} login</a>`
        : `<button class="gate-secondary" type="button" data-banner-reconnect="${id}">Reconnect</button>`;
      return `<div class="broker-banner" role="status"><span class="broker-head">${brokerLogoHtml(broker.broker, broker.name, broker.logo)}<span>${escHtml(brokerBannerText(broker, now))}</span></span>
        <span class="broker-banner-actions">${action}<button class="broker-banner-dismiss" type="button" data-banner-dismiss="${id}" aria-label="Dismiss for today" title="Dismiss for today">×</button></span></div>`;
    }).join('');
  document.querySelectorAll('[data-broker-banners]').forEach((element) => { element.innerHTML = html; });
}

async function refreshBrokerAttention({ check = false } = {}) {
  let list = await fetchBrokerList();
  if (!list) {
    brokerAttention = [];
    renderBrokerBanners();
    return;
  }
  if (check) {
    const now = Date.now();
    const checked = readBrokerChecks();
    const ids = brokersToCheck(list, checked, now);
    if (ids.length) {
      ids.forEach((id) => { checked[id] = now; });
      writeBrokerChecks(checked);
      await Promise.all(ids.map((id) => fetch(`/api/brokers/${encodeURIComponent(id)}/status`).catch(() => null)));
      list = (await fetchBrokerList()) || list;
    }
  }
  brokerAttention = brokersNeedingAttention(list);
  renderBrokerBanners();
  document.dispatchEvent(new CustomEvent('ca:brokers'));
}

async function pollBannerLogin(id, count = 0) {
  if (count >= BROKER_MAX_POLLS) return;
  await new Promise((resolve) => setTimeout(resolve, BROKER_POLL_MS));
  if (document.hidden) return;
  try {
    const response = await fetch(`/api/brokers/${encodeURIComponent(id)}/status`);
    const status = response.ok ? (await response.json()).status : 'pending';
    if (status === 'connected') {
      delete bannerLinks[id];
      await fetch(`/api/brokers/${encodeURIComponent(id)}/holdings?refresh=1`).catch(() => null);
      document.dispatchEvent(new CustomEvent('ca:holdings'));
      await refreshBrokerAttention();
      return;
    }
  } catch { /* keep waiting */ }
  await pollBannerLogin(id, count + 1);
}

document.addEventListener('click', async (event) => {
  const dismiss = event.target.closest('[data-banner-dismiss]');
  if (dismiss) {
    dismissedBanners.add(dismiss.dataset.bannerDismiss);
    dismissBanner(safeStorage(), dismiss.dataset.bannerDismiss);
    renderBrokerBanners();
    return;
  }
  const reconnect = event.target.closest('[data-banner-reconnect]');
  if (!reconnect) return;
  const broker = brokerAttention.find((item) => item.broker === reconnect.dataset.bannerReconnect);
  if (!broker) return;
  const action = brokerReconnectAction(broker);
  if (action.type === 'link') {
    window.location.href = action.href;
  } else if (action.type === 'dialog') {
    openProfileDialog();
  } else {
    reconnect.disabled = true;
    try {
      const response = await fetch(`/api/brokers/${encodeURIComponent(broker.broker)}/connect`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(plainError(response.status, data.error ? data : { error: 'Could not start the login.' }));
      if (data.loginUrl) bannerLinks[broker.broker] = data.loginUrl;
      renderBrokerBanners();
      pollBannerLogin(broker.broker);
    } catch (error) {
      reconnect.disabled = false;
      reconnect.textContent = 'Try again';
      reconnect.title = errorText(error);
    }
  }
});

document.addEventListener('ca:session', () => { refreshBrokerAttention({ check: true }); });
document.addEventListener('ca:holdings', () => { refreshBrokerAttention(); });
document.addEventListener('ca:session-ended', () => {
  brokerAttention = [];
  renderBrokerBanners();
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

function chatRow(chat, { showAgent = false } = {}) {
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
  meta.textContent = [showAgent ? agentName(chat.agentType) : '', relativeDay(chat.updatedAt)].filter(Boolean).join(' · ');
  select.append(title, meta);
  select.addEventListener('click', () => {
    setSidebarOpen(false);
    navigate('chat');
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
  return row;
}

// The header above the conversation: its title, agent and last message day, with Delete and New chat once there
// is something to delete or leave. Chats keep no start date, so the day shown is the latest message's.
function renderChatHead() {
  const chat = activeChatId ? chats.find((item) => item.id === activeChatId) : null;
  const title = chat?.title || 'New chat';
  chatTitle.textContent = title;
  chatTitle.title = title;
  const day = chat ? relativeDay(chat.updatedAt) : '';
  const when = day ? `last message ${/^(Today|Yesterday)$/.test(day) ? day.toLowerCase() : day}` : '';
  chatSub.textContent = [agentName(chat?.agentType || selectedAgent), when].filter(Boolean).join(' · ');
  chatDeleteButton.hidden = !chat;
  chatDeleteButton.setAttribute('aria-label', chat ? `Delete chat: ${title}` : 'Delete chat');
  chatNewButton.hidden = !chat && !messages.classList.contains('has-messages');
}

function renderChats() {
  chatList.innerHTML = '';
  renderChatHead();
  document.dispatchEvent(new CustomEvent('ca:chats'));
  if (!chats.length) {
    const empty = document.createElement('div');
    empty.className = 'chat-empty';
    empty.textContent = 'No conversations yet.';
    chatList.appendChild(empty);
    return;
  }
  for (const chat of chats.slice(0, SIDEBAR_CHATS)) chatList.appendChild(chatRow(chat));
}

async function refreshChats() {
  const response = await fetch('/api/chats');
  if (!response.ok) throw new Error('Could not refresh the chat list.');
  chats = (await response.json()).chats || [];
  renderChats();
  setStatus('online', statusLabel());
}

function statusLabel() {
  const label = isConnected ? 'INDmoney connected' : 'Finance mode';
  return storageMode === 'private' ? `${label} · Private` : label;
}

function resetConversationView() {
  destroyPlotlyCharts(messages);
  messages.innerHTML = '';
  messages.classList.remove('has-messages');
  emptyState.hidden = false;
  chatScroll.scrollTop = 0;
  setLatestAnswer(null);
  renderChatHead();
}

// A new conversation stays a draft (no activeChatId) until its first message is
// sent, so New Chat and agent switches change no state a send could race with,
// and never leave an empty chat behind (creating one can evict the oldest).
async function startNewChat() {
  if (isStreaming) return;
  activeChatId = null;
  resetConversationView();
  renderChats();
  userInput.focus();
}

async function createChat() {
  const response = await fetch('/api/chats', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agentType: selectedAgent }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not create a new chat.');
  chats.unshift(data);
  activeChatId = data.id;
  renderChats();
  return data.id;
}

// A chat created for a message that then failed holds nothing; drop it so
// failed sends do not fill the chat list.
async function discardEmptyChat(chatId) {
  try {
    const response = await fetch('/api/chats/' + encodeURIComponent(chatId));
    if (!response.ok || ((await response.json()).messages || []).length) return;
    await fetch('/api/chats/' + encodeURIComponent(chatId), { method: 'DELETE' });
    chats = chats.filter((chat) => chat.id !== chatId);
    if (activeChatId === chatId) activeChatId = null;
    renderChats();
  } catch { /* An empty chat left behind is harmless and expires with the others. */ }
}

async function selectChat(chatId) {
  if (isStreaming) return;
  // Hold sends until the chat has loaded, so a message cannot go to the
  // previous chat and then be wiped from the view.
  setBusy(true);
  let data;
  try {
    const response = await fetch('/api/chats/' + encodeURIComponent(chatId));
    data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Could not open this chat.');
  } finally {
    setBusy(false);
  }
  activeChatId = chatId;
  setAgent(data.agentType || 'portfolio');
  resetConversationView();
  for (const message of data.messages || []) {
    if (message.role === 'user') addMessage('user', escHtml(message.content).replace(/\n/g, '<br>'));
    else if (message.role === 'assistant') addMessage('assistant', renderMarkdown(message.content), message.trace || [], message.runId, { answer: true });
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
  navigate('chat');
  startNewChat().catch(showError);
});
chatNewButton.addEventListener('click', () => startNewChat().catch(showError));
chatDeleteButton.addEventListener('click', () => {
  if (activeChatId) deleteChat(activeChatId).catch(showError);
});

// Back (phone header) returns to the page Ask AI was opened from, else Home.
let chatReturnHash = '#/';
document.addEventListener('ca:route', (event) => {
  if (event.detail?.route !== 'chat') chatReturnHash = location.hash || '#/';
});
chatBackButton.addEventListener('click', () => {
  if (location.hash !== chatReturnHash) location.hash = chatReturnHash;
  else navigate('home');
});

/* ── Chat ────────────────────────────────────────────────────────────── */
function showMessages() {
  emptyState.hidden = true;
  messages.classList.add('has-messages');
  chatNewButton.hidden = false;
}

function scrollToBottom() {
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

// Suggestions under the latest answer (the data-prompt buttons send them; setBusy disables them while answering).
const FOLLOW_UPS = [
  ['Show more detail', 'Can you explain that in more detail?'],
  ['Explain in simple terms', 'Explain that in simple terms.'],
  ['Key takeaways', 'Summarize the key takeaways as a short list.'],
  ['Show as a chart', 'Show that as a chart.'],
];

function placeFollowUps(messageDiv) {
  messages.querySelectorAll('.answer-follow-ups').forEach((item) => item.remove());
  if (!messageDiv) return;
  const holder = document.createElement('div');
  holder.className = 'follow-ups answer-follow-ups';
  holder.setAttribute('role', 'group');
  holder.setAttribute('aria-label', 'Follow-up suggestions');
  holder.innerHTML = FOLLOW_UPS.map(([label, prompt]) =>
    `<button class="chip" type="button" data-prompt="${escHtml(prompt)}"${isStreaming ? ' disabled' : ''}>${escHtml(label)}</button>`).join('');
  messageDiv.querySelector('.message-main').appendChild(holder);
}

// The latest answer's trace feeds the rail's "This answer used" (rail.js listens for ca:answer).
let latestAnswerTrace = null;
function setLatestAnswer(trace) {
  latestAnswerTrace = Array.isArray(trace) ? trace : null;
  document.dispatchEvent(new CustomEvent('ca:answer', { detail: { trace: latestAnswerTrace } }));
}

const ASSISTANT_AVATAR = '<span class="message-avatar assistant" aria-hidden="true">₹</span>';

// A question is a bubble on the right; an answer has the mark, a meta line, the body, its trace and, for the
// latest answer, follow-up suggestions. ``answer`` marks a real answer (not a notice such as a limit message).
function addMessage(role, content, trace = [], runId = null, { answer = false } = {}) {
  showMessages();
  const div = document.createElement('div');
  div.className = `message ${role}`;
  if (role === 'user') {
    div.innerHTML = `<span class="sr-only">You:</span><div class="message-body">${content}</div>`;
  } else {
    div.innerHTML = `${ASSISTANT_AVATAR}
      <div class="message-main">
        <p class="message-meta">${escHtml(answerMeta(trace))}</p>
        <div class="message-body">${content}</div>
        ${renderChatTrace(trace, { runId })}
      </div>`;
  }
  // A new question or notice ends the previous answer's suggestions.
  messages.querySelectorAll('.answer-follow-ups').forEach((item) => item.remove());
  messages.appendChild(div);
  renderPlotlyCharts(div);
  if (role === 'assistant') {
    if (answer || (Array.isArray(trace) && trace.length)) setLatestAnswer(trace);
    if (answer) placeFollowUps(div);
    attachInvestigationCard(div, trace).catch(() => { /* The answer and trace still show. */ });
  }
  scrollToBottom();
  return div;
}

function parseTraceHeader(value) {
  try {
    const trace = JSON.parse(value || '[]');
    return Array.isArray(trace) ? trace : [];
  } catch {
    return [];
  }
}

/* ── Investigation evidence card ─────────────────────────────────────── */
// Figures come from the saved investigation by id, never from the answer text (rendering lives in trace.js).

async function attachInvestigationCard(messageDiv, trace) {
  const id = [...(Array.isArray(trace) ? trace : [])].reverse().find((step) => step.investigation_id)?.investigation_id;
  if (!id || messageDiv.querySelector('.evidence-card')) return;
  const response = await fetch('/api/investigations/' + encodeURIComponent(id));
  if (!response.ok) return;
  const holder = document.createElement('div');
  holder.innerHTML = renderInvestigationCard(await response.json(), { chart: renderDataChart });
  const card = holder.firstElementChild;
  const traceDetails = messageDiv.querySelector('.chat-trace');
  if (traceDetails) traceDetails.before(card); else messageDiv.querySelector('.message-main')?.appendChild(card);
  renderPlotlyCharts(card);
}

function addTypingIndicator() {
  showMessages();
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-indicator';
  div.innerHTML = `${ASSISTANT_AVATAR}
    <div class="message-main">
      <p class="message-meta">${escHtml(answerMeta())}</p>
      <div class="message-body">
        <div class="typing-indicator" role="status" aria-label="Answering"><span></span><span></span><span></span></div>
      </div>
    </div>`;
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
  let createdChatId = null;

  try {
    if (!activeChatId) createdChatId = await createChat();
    const chatId = activeChatId;
    const sinceUpdatedAt = Number(chats.find((chat) => chat.id === chatId)?.updatedAt);
    let res = null;
    try {
      res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, chatId, agentType: selectedAgent, aiProvider: selectedProvider }),
      });
    } catch { /* The connection dropped: the server may still save the answer. */ }

    if (!res || !res.ok) {
      const err = res ? await res.json().catch(() => ({})) : {};
      if (lostChatResponse(res ? res.status : 0, err)) {
        // A proxy timed out or the connection dropped, but the server keeps answering and saves the answer
        // with the chat: look for it there before calling this a failure.
        setStatus('loading', 'Checking for the answer…');
        const saved = await waitForSavedAnswer(chatId, text, sinceUpdatedAt);
        if (saved) {
          removeTypingIndicator();
          addMessage('assistant', renderMarkdown(saved.content), saved.trace || [], saved.runId, { answer: true });
          createdChatId = null;
          await refreshChats().catch(() => {});
          return;
        }
        throw new Error(res ? 'The connection timed out before the answer arrived. If it was saved, it appears when you reopen this chat.'
          : NETWORK_ERROR);
      }
      const trace = parseTraceHeader(res.headers.get('X-Agent-Trace'));
      if (err.profileRequired || err.unlockRequired || err.consentRequired) {
        removeTypingIndicator();
        await loadChats();
        return;
      }
      if (res.status === 429 && (err.dailyLimitReached || err.rateLimited)) {
        // A limit, not a failure: say so plainly ("You've reached today's limit… resets at midnight IST").
        removeTypingIndicator();
        const plansLink = err.pricingUrl
          ? ` <a href="${escHtml(err.pricingUrl)}" target="_blank" rel="noopener">See paid plans</a>`
          : '';
        addMessage('assistant', `<p>${escHtml(err.error || 'Too many questions for now. Try again later.')}${plansLink}</p>`);
        if (err.dailyLimitReached) updateQuestionsLeft(0);
        setStatus('online', err.dailyLimitReached ? 'Daily limit reached' : 'Too many requests');
        if (createdChatId) await discardEmptyChat(createdChatId);
        return;
      }
      if ((res.status === 503 && err.modelBusy) || (res.status === 429 && err.chatInFlight) || (res.status === 504 && err.chatDeadline)) {
        // The shared model is busy, this profile's previous answer is still running, or the answer ran out of
        // time: nothing is wrong with the question, so say so plainly and when to try again.
        removeTypingIndicator();
        const wait = Number(err.retryAfterSeconds);
        const fallback = err.chatDeadline ? 'That took too long to answer. Try again, or ask a shorter question.'
          : 'The assistant is busy right now. Try again in a moment.';
        const retry = !err.chatDeadline && wait > 0 && !/\d+ seconds/.test(err.error || '') ? ` Try again in about ${wait} seconds.` : '';
        addMessage('assistant', `<p>${escHtml(err.error || fallback)}${escHtml(retry)}</p>`);
        setStatus('online', err.modelBusy ? 'Assistant busy' : err.chatInFlight ? 'Still answering' : 'Took too long');
        if (createdChatId) await discardEmptyChat(createdChatId);
        return;
      }
      if (res.status === 402 && err.creditsExhausted) {
        // Credits used up: say when they renew and where to top up, like the daily limit.
        removeTypingIndicator();
        const plansLink = err.pricingUrl
          ? ` <a href="${escHtml(err.pricingUrl)}" target="_blank" rel="noopener">How to top up</a>`
          : '';
        addMessage('assistant', `<p>${escHtml(err.error || "You've used this month's credits.")}${plansLink}</p>`);
        updateCreditsLeft(0);
        setStatus('online', 'Credits used up');
        if (createdChatId) await discardEmptyChat(createdChatId);
        return;
      }
      if (err.loginUrl) {
        const connectLink = `<br><a href="${escHtml(err.loginUrl)}">Connect INDmoney</a>`;
        throw Object.assign(new Error(err.error || 'Connect your INDmoney account.'), { connectLink, trace });
      }
      throw Object.assign(new Error(plainError(res.status, err)), { trace, runId: err.runId });
    }

    const trace = parseTraceHeader(res.headers.get('X-Agent-Trace'));
    const questionsLeft = res.headers.get('X-Questions-Remaining');
    if (questionsLeft !== null) updateQuestionsLeft(Number(questionsLeft));
    const creditsLeft = res.headers.get('X-Credits-Remaining');
    if (creditsLeft !== null) updateCreditsLeft(Number(creditsLeft));
    removeTypingIndicator();
    const msgDiv = addMessage('assistant', '', trace, res.headers.get('X-Agent-Run'), { answer: true });
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
          if (currentTrace) currentTrace.outerHTML = renderChatTrace(savedAssistant.trace, { runId: savedAssistant.runId });
          msgDiv.querySelector('.message-meta').textContent = answerMeta(savedAssistant.trace);
          if (msgDiv === messages.querySelector('.message.assistant:last-child')) setLatestAnswer(savedAssistant.trace);
        }
      }
    } catch { /* The compact trace from the response header is still visible. */ }
  } catch (err) {
    removeTypingIndicator();
    addMessage('assistant',
      `<span style="color:var(--error)">⚠ ${escHtml(errorText(err))}</span>${err.connectLink || ''}`,
      err.trace || [], err.runId,
    );
    setStatus('error', 'Request failed');
    if (createdChatId) await discardEmptyChat(createdChatId);
  } finally {
    setBusy(false);
    scrollToBottom();
  }
}

// After a lost chat response, re-read the chat until its saved answer shows up or the server's own answer
// deadline (CHAT_DEADLINE_SECONDS, 240 by default) has surely passed.
const LOST_ANSWER_POLL_MS = 10000;
const LOST_ANSWER_WAIT_MS = 150000;

async function waitForSavedAnswer(chatId, question, sinceUpdatedAt) {
  const giveUpAt = Date.now() + LOST_ANSWER_WAIT_MS;
  for (;;) {
    try {
      const response = await fetch('/api/chats/' + encodeURIComponent(chatId));
      if (response.status >= 400 && response.status < 500) return null;
      if (response.ok) {
        const saved = savedAnswerFor(await response.json(), question, sinceUpdatedAt);
        if (saved) return saved;
      }
    } catch { /* Still offline: try again below. */ }
    if (Date.now() + LOST_ANSWER_POLL_MS > giveUpAt) return null;
    await new Promise((resolve) => setTimeout(resolve, LOST_ANSWER_POLL_MS));
  }
}

/* ── Markdown renderer (minimal) ─────────────────────────────────────── */
function renderMarkdown(text) {
  // Escape helper
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  // Extract chart blocks first so the agent can return data-driven visuals. Only fenced ```chart blocks
  // count: the server grounds those in tool data, while bold or inline JSON stays text.
  const extracted = extractChartBlocks(text);
  const chartBlocks = extracted.charts;
  text = extracted.text;

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

  // Mark list items, quote lines and rules before emphasis runs, so a "* " bullet
  // is not read as the start of italics. ">" is already escaped here.
  text = text
    .replace(/^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, '\x00HR\x00')
    .replace(/^[ \t]*[-*+][ \t]+(?=\S)/gm, '\x00UL\x00')
    .replace(/^[ \t]*\d{1,3}[.)][ \t]+(?=\S)/gm, '\x00OL\x00')
    .replace(/^[ \t]*&gt;[ \t]?/gm, '\x00QUOTE\x00');

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

  // Consecutive marked lines become one list or quote (each rule stands alone),
  // set apart as its own paragraph so it is restored outside any <p>.
  const listBlocks = [];
  const blockLines = text.split('\n');
  const groupedLines = [];
  for (let i = 0; i < blockLines.length;) {
    const kind = blockLines[i].match(/^\x00(UL|OL|QUOTE|HR)\x00/)?.[1];
    if (!kind) {
      groupedLines.push(blockLines[i++]);
      continue;
    }
    const marker = `\x00${kind}\x00`;
    const items = [];
    if (kind === 'HR') i++;
    else while (i < blockLines.length && blockLines[i].startsWith(marker)) items.push(blockLines[i++].slice(marker.length).trimEnd());
    const tag = kind.toLowerCase();
    const idx = listBlocks.push(kind === 'HR' ? '<hr>'
      : kind === 'QUOTE' ? `<blockquote>${items.join('<br>')}</blockquote>`
      : `<${tag}>${items.map(item => `<li>${item}</li>`).join('')}</${tag}>`) - 1;
    groupedLines.push(`@@CA_LIST_${idx}@@`);
  }
  text = groupedLines.join('\n')
    .replace(/\n*(@@CA_LIST_\d+@@)\n*/g, '\n\n$1\n\n')
    .replace(/^\n+|\n+$/g, '');

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
  text = text.replace(/<p(?: style="margin-top:12px")?>\s*@@CA_LIST_(\d+)@@\s*<\/p>/g, (_, idx) => listBlocks[idx]);
  text = text.replace(/@@CA_LIST_(\d+)@@/g, (_, idx) => listBlocks[idx]);

  // A chart block that cannot be drawn says so instead of leaving a blank space.
  text = text.replace(/\x00CHART(\d+)\x00/g, (_, idx) => renderDataChart(chartBlocks[idx])
    || '<p class="chart-missing" role="note">This chart could not be drawn because its data was incomplete.</p>');

  // Detect result JSON blocks (wrapped in triple backtick or plain)
  // The agent may send result sections prefixed with "Result:" or "Output:"
  text = text.replace(/<p>(<strong>)?(?:Result|Output|Execution Result):?(<\/strong>)?\s*<\/p>/gi,
    '<div class="exec-result-header">⬡ Execution Result</div>');

  return text;
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

// Candles as objects, or as the compact [label, open, high, low, close] rows the server writes.
function chartCandles(spec) {
  const rows = Array.isArray(spec?.candles) ? spec.candles
    : Array.isArray(spec?.ohlc) ? spec.ohlc.map(row => (Array.isArray(row)
      ? { label: row[0], open: row[1], high: row[2], low: row[3], close: row[4] } : null))
    : [];
  return rows.slice(-300).map(item => ({
    label: cleanChartText(item?.label, 50),
    open: Number(item?.open), high: Number(item?.high),
    low: Number(item?.low), close: Number(item?.close),
  })).filter(item => item.label && [item.open, item.high, item.low, item.close].every(Number.isFinite)
    && item.high >= Math.max(item.open, item.close, item.low)
    && item.low <= Math.min(item.open, item.close, item.high));
}

// ``history`` draws a line chart as a page's net-worth history (see historyLayout); ``compact`` leaves out the axis
// titles for a small card and ``area`` shades under the line. ``xTitle`` / ``yTitle`` name the axes of a page line
// chart whose labels are not dates (calculators: years), drawn as categories. ``page`` draws a bar or donut on a page:
// no toolbar or zoom, and rupees in Indian format on hover.
function renderDataChart(spec, { history = false, compact = false, area = false, height = 0, xTitle = '', yTitle = '', page = false } = {}) {
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
    ? { type: 'line', title: cleanChartText(spec.title), unit: cleanChartText(spec.unit, 16), series, ...(history ? { history, compact, area, height, xTitle, yTitle } : {}) }
    : { type: spec.type, title: cleanChartText(spec.title), unit: cleanChartText(spec.unit, 16), data: rows, ...(page ? { page } : {}) };
  chartSpecRegistry.set(id, safeSpec);
  const title = escHtml(safeSpec.title || 'Portfolio data');
  const initialView = isLine ? 'line' : isCandlestick ? 'candlestick' : spec.type === 'donut' && canPie ? 'pie' : 'bar';
  const controls = isLine ? '' : isCandlestick
    ? `<div class="chart-view-controls" role="group" aria-label="Choose chart view"><button type="button" class="chart-view-button is-active" aria-pressed="true" data-set-chart-view="candlestick">Candles</button><button type="button" class="chart-view-button" aria-pressed="false" data-set-chart-view="table">Data</button></div>`
    : `<div class="chart-view-controls" role="group" aria-label="Choose chart view"><button type="button" class="chart-view-button ${initialView === 'bar' ? 'is-active' : ''}" aria-pressed="${initialView === 'bar'}" data-set-chart-view="bar">Bars</button>${canPie ? `<button type="button" class="chart-view-button ${initialView === 'pie' ? 'is-active' : ''}" aria-pressed="${initialView === 'pie'}" data-set-chart-view="pie">Donut</button>` : ''}<button type="button" class="chart-view-button" aria-pressed="false" data-set-chart-view="table">Data</button></div>`;
  const table = renderChartTable(safeSpec);
  const hint = safeSpec.history || safeSpec.page ? '' : '<p class="chart-hint">Hover for values. Use the Plotly toolbar to zoom, pan, reset, or export the chart.</p>';
  return `<figure class="data-chart${isLine ? ' line-chart' : ' category-chart'}${safeSpec.history ? ' history-chart' : ''}" data-chart-id="${id}" data-chart-view="${initialView}"><div class="chart-heading"><figcaption class="data-chart-title">${title}</figcaption>${controls}</div><div class="plotly-chart" id="${id}" role="img" aria-label="Interactive chart: ${title}"></div><div class="chart-data-panel" hidden>${table}</div>${hint}</figure>`;
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
    colorway: document.documentElement.dataset.theme === 'dark'
      ? ['#8fdc4e', '#f2d64b', '#5ec8f2', '#c9a7ff', '#ff9f6b', '#5fe0b5', '#f27aa6', '#9aa59d']
      : ['#27835e', '#5d88b5', '#d09b4f', '#a56d9a', '#d16f62', '#4b9bb1', '#8aa657', '#8973af'],
    legend: { orientation: 'h', y: -0.22, font: { color: muted } },
    xaxis: { gridcolor: border, zerolinecolor: border, automargin: true },
    yaxis: { gridcolor: border, zerolinecolor: border, automargin: true, title: { text: unit.trim() } },
    hoverlabel: { bgcolor: surface, bordercolor: border, font: { color: text } },
  };
  if (spec.history) return historyLayout(spec, layout, muted);
  if (spec.page) {
    // On a page the card's own heading names the chart, and the card shows through behind it.
    Object.assign(layout, { title: { text: '' }, paper_bgcolor: 'rgba(0, 0, 0, 0)', plot_bgcolor: 'rgba(0, 0, 0, 0)', height: 320 });
    layout.xaxis = { ...layout.xaxis, fixedrange: true };
    layout.yaxis = { ...layout.yaxis, fixedrange: true };
  }
  if (view === 'pie') {
    layout.margin = { l: 24, r: 24, t: spec.page ? 16 : 56, b: 24 };
    layout.showlegend = true;
  } else if (view === 'bar') {
    layout.barmode = 'group';
    layout.yaxis = { ...layout.yaxis, autorange: 'reversed' };
    if (spec.page) {
      layout.margin = { l: 64, r: 24, t: 16, b: 48 };
      layout.yaxis.title = { text: '' };
      layout.xaxis = { ...layout.xaxis, title: { text: spec.unit ? `Value (${spec.unit})` : 'Value', font: { size: 11, color: muted } } };
    }
  } else if (spec.type === 'line') {
    const slider = spec.series[0]?.points.length > 8;
    layout.hovermode = 'x unified';
    layout.xaxis = { ...layout.xaxis, rangeslider: { visible: slider } };
    // Below the axis the legend would sit on top of the range slider.
    if (slider) layout.legend = { ...layout.legend, x: 1, xanchor: 'right', y: 1.02, yanchor: 'bottom' };
  } else if (spec.type === 'candlestick') {
    layout.hovermode = 'x unified';
    layout.xaxis = { ...layout.xaxis, rangeslider: { visible: spec.candles.length > 8 } };
  }
  return layout;
}

// A chart value in hover text: rupees in Indian grouping, anything else as a plain number.
function chartValueText(value, unit) {
  return unit === '₹' ? money(value) : `${new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(value)}${unit ? ` ${unit}` : ''}`;
}

// The y-axis ticks of a history chart: round values covering every point, labelled ₹15.40 L / ₹1.20 Cr.
function historyTicks(spec) {
  const values = spec.series.flatMap(item => item.points.map(point => point.value));
  return niceTicks(Math.min(...values), Math.max(...values), 4);
}

// Net worth over time on the pages: no range slider or mode bar (and no zoom, which they would be needed to undo),
// labelled axes, and hover that shows the exact amount in Indian format.
function historyLayout(spec, layout, muted) {
  const ticks = historyTicks(spec);
  const label = (value) => (spec.unit === '₹' ? moneyShort(value) : chartValueText(value, spec.unit));
  const axisTitle = (text) => (spec.compact ? { text: '' } : { text, font: { size: 11, color: muted }, standoff: 10 });
  const name = spec.series.length === 1 ? spec.series[0].name : 'Value';
  // Calculator charts label their points with years, not dates.
  const dates = !spec.xTitle;
  return {
    ...layout,
    title: { text: '' },
    // The card behind shows through, so the chart matches it in both themes.
    paper_bgcolor: 'rgba(0, 0, 0, 0)', plot_bgcolor: 'rgba(0, 0, 0, 0)',
    height: spec.height || 320,
    margin: { l: 8, r: 12, t: 12, b: 8 },
    hovermode: 'x unified',
    showlegend: spec.series.length > 1,
    legend: { orientation: 'h', x: 1, xanchor: 'right', y: 1.02, yanchor: 'bottom', font: { color: muted } },
    xaxis: {
      // Level labels, fewer of them on a phone, rather than Plotly turning crowded dates on their side.
      ...layout.xaxis, type: dates ? 'date' : 'category', fixedrange: true, showgrid: false, nticks: window.innerWidth < 640 ? 4 : 6, tickangle: 0,
      ...(dates ? { hoverformat: '%d %b %Y' } : {}), tickfont: { color: muted, size: 11 }, title: axisTitle(spec.xTitle || 'Date'),
    },
    yaxis: {
      ...layout.yaxis, fixedrange: true, zeroline: false, tickmode: 'array', tickvals: ticks, ticktext: ticks.map(label),
      range: ticks.length ? [ticks[0], ticks[ticks.length - 1]] : undefined,
      tickfont: { color: muted, size: 11 }, title: axisTitle(spec.yTitle || `${name}${spec.unit ? ` (${spec.unit})` : ''}`),
    },
  };
}

function historyTraces(spec) {
  const style = getComputedStyle(document.body);
  const accent = style.getPropertyValue('--accent').trim() || '#8fdc4e';
  const glow = style.getPropertyValue('--accent-glow').trim() || 'rgba(143, 220, 78, .25)';
  const ticks = historyTicks(spec);
  return spec.series.map((item, index) => ({
    type: 'scatter', mode: item.points.length > 40 ? 'lines' : 'lines+markers', name: item.name,
    x: item.points.map(point => point.label), y: item.points.map(point => point.value),
    customdata: item.points.map(point => chartValueText(point.value, spec.unit)),
    hovertemplate: spec.series.length > 1 ? '%{fullData.name}: %{customdata}<extra></extra>' : '%{customdata}<extra></extra>',
    line: { width: 2.5, ...(index === 0 ? { color: accent } : {}) }, marker: { size: 6 },
    ...(spec.area && index === 0 && ticks.length ? {
      fill: 'tozeroy', fillcolor: glow,
      fillgradient: { type: 'vertical', start: ticks[ticks.length - 1], stop: ticks[0], colorscale: [[0, glow], [1, 'rgba(0, 0, 0, 0)']] },
    } : {}),
  }));
}

// Page charts stay still and drop the toolbar; answer charts keep zoom and the toolbar.
function plotlyConfig(spec) {
  return spec.history || spec.page
    ? { responsive: true, displayModeBar: false, scrollZoom: false, displaylogo: false }
    : { responsive: true, scrollZoom: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
}

function plotlyTraces(spec, view) {
  if (spec.history) return historyTraces(spec);
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
    // Markers on long daily series turn into a solid band, so draw only the line.
    type: 'scatter', mode: item.points.length > 40 ? 'lines' : 'lines+markers', name: item.name,
    x: item.points.map(point => point.label), y: item.points.map(point => point.value),
    hovertemplate: '%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>',
    line: { width: 2.5 }, marker: { size: 7 },
  }));
  // A page chart shows rupees in Indian grouping on hover (Plotly's own format groups in thousands).
  const pageValues = spec.page ? { customdata: spec.data.map(row => chartValueText(row.value, spec.unit)) } : {};
  if (view === 'pie') return [{
    type: 'pie', hole: 0.52, labels: spec.data.map(row => row.label),
    values: spec.data.map(row => row.value), sort: false,
    textinfo: 'label+percent', textposition: 'outside', automargin: true,
    ...(spec.page ? { customdata: pageValues.customdata.map(text => [text]) } : {}),
    hovertemplate: spec.page ? '%{label}<br>%{customdata[0]} (%{percent})<extra></extra>' : '%{label}<br>%{value:,.2f} (%{percent})<extra></extra>',
  }];
  return [{
    type: 'bar', orientation: 'h',
    x: spec.data.map(row => row.value), y: spec.data.map(row => row.label),
    name: spec.title || 'Value', ...pageValues,
    hovertemplate: spec.page ? '%{y}<br>%{customdata}<extra></extra>' : '%{y}<br>%{x:,.2f}<extra></extra>',
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
    window.Plotly.newPlot(graph, plotlyTraces(spec, view), plotlyLayout(spec, view), plotlyConfig(spec));
  }
}

function destroyPlotlyCharts(container) {
  container.querySelectorAll?.('.plotly-chart').forEach(graph => {
    if (graph.dataset.plotlyReady && window.Plotly) window.Plotly.purge(graph);
    chartSpecRegistry.delete(graph.id);
  });
}

// The view buttons under a chart (Bars, Donut, Candles, Data). Delegated: the Content-Security-Policy blocks inline handlers.
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-set-chart-view]');
  if (button) setChartView(button, button.dataset.setChartView);
});

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
    window.Plotly.react(graph, plotlyTraces(spec, view), plotlyLayout(spec, view), plotlyConfig(spec));
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
  if (e.key === 'Enter' && !e.shiftKey && !isComposingEnter(e)) {
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
  setSidebarOpen(false);
  navigate('chat');
  if (agent && agent !== selectedAgent) await switchAgent(agent);
  await sendMessage(prompt);
}

// Prompt buttons are also rendered by the pages, so listen once on the document.
document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-prompt]');
  if (button && !button.disabled) runPrompt(button.dataset.prompt, button.dataset.agent).catch(showError);
});

/* ── Init ────────────────────────────────────────────────────────────── */
loadProviders().catch((error) => {
  providerPicker.innerHTML = `<option value="">${escHtml(errorText(error))}</option>`;
  providerPicker.disabled = true;
  providerReady = true;
});
loadChats();
userInput.focus();
