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
const authButton = document.getElementById('auth-button');
const themeToggle = document.getElementById('theme-toggle');
const themeToggleIcon = document.getElementById('theme-toggle-icon');
const agentNavItems = document.querySelectorAll('.agent-option[data-agent]');
const providerPicker = document.getElementById('provider-picker');
const greeting = document.getElementById('greeting');
const welcomeAgentName = document.getElementById('welcome-agent-name');
const welcomeDescription = document.getElementById('welcome-description');
const followUps = document.getElementById('follow-ups');
const accountName = document.getElementById('account-name');
const accountPlan = document.getElementById('account-plan');
const accountAvatar = document.querySelector('.account-avatar');
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
const connectionStatus = document.getElementById('connection-status');
const PROFILE_FIELDS = ['riskAppetite', 'ageBand', 'investmentHorizonYears', 'monthlyInvestmentInr', 'primaryGoal'];
// The sidebar lists this many recent chats; "See all" opens the full list.
const SIDEBAR_CHATS = 6;
let isConnected = false;
// True between a successful load of the signed-in, unlocked, consenting profile and the next loadChats().
let isSessionReady = false;
let currentUser = null;
let storageMode = 'device';
let privacyPolicyVersion = null;
let unlockTarget = null;
// True when the signed-in profile is locked after a restart (unlock with /api/session/unlock).
let isLockedSession = false;
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
  const text = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
  greeting.textContent = text;
  document.querySelectorAll('.js-greeting').forEach((item) => { item.textContent = text; });
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
  document.querySelectorAll('button[data-prompt]').forEach((button) => { button.disabled = busy; });
  agentNavItems.forEach((item) => { item.disabled = busy; });
}

function renderAccount() {
  authButton.textContent = isConnected ? 'Disconnect' : 'Connect';
  authButton.title = isConnected ? 'Disconnect your INDmoney account' : 'Connect your INDmoney account for portfolio analysis';
  connectionStatus.textContent = isConnected ? 'Connected. Portfolio Analyzer can read your holdings.' : 'Not connected';
  accountName.textContent = currentUser?.displayName || 'Guest';
  accountAvatar.textContent = (currentUser?.displayName || '?').trim().charAt(0).toUpperCase();
  accountPlan.textContent = [isConnected ? 'INDmoney connected' : 'INDmoney not connected', storageMode === 'private' ? 'Private' : '']
    .filter(Boolean).join(' · ');
  accountPlan.title = accountPlan.textContent;
  accountAvatar.classList.toggle('is-connected', isConnected);
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
  const table = document.createElement('table');
  table.className = 'data-table compact';
  table.innerHTML = '<thead><tr><th>Statement</th><th>Holdings</th><th>Value</th><th><span class="sr-only">Actions</span></th></tr></thead>';
  const body = document.createElement('tbody');
  for (const statement of statements) {
    const row = document.createElement('tr');
    row.innerHTML = `<td>${escHtml(statement.source)} · ${escHtml(statement.periodTo)}</td><td>${Number(statement.holdings) || 0}</td><td>${inr.format(statement.valueInr || 0)}</td><td></td>`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'table-action';
    remove.textContent = 'Delete';
    remove.addEventListener('click', async () => {
      if (!window.confirm('Delete this saved statement?')) return;
      const response = await fetch('/api/cas/' + encodeURIComponent(statement.id), { method: 'DELETE' });
      if (response.ok) await loadCasStatus();
    });
    row.lastElementChild.appendChild(remove);
    body.appendChild(row);
  }
  table.appendChild(body);
  list.appendChild(table);
}

function renderCas(latest) {
  casPill.textContent = latest ? `As of ${latest.periodTo}` : 'No statement';
  casPill.classList.toggle('is-connected', Boolean(latest));
  casAsk.hidden = !latest;
  if (latest) {
    const brokers = latest.brokers.length;
    casCopy.textContent = `${inr.format(latest.valueInr)} in ${latest.holdings} holdings across ${brokers} account${brokers === 1 ? '' : 's'} (${latest.source} statement). Upload a newer one each month.`;
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
    casStatus.textContent = 'Saved. The PDF and its password were not kept.';
  } catch (error) {
    casStatus.classList.add('is-error');
    casStatus.textContent = error.message;
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
    chatList.innerHTML = '<div class="tools-error">Could not load chats<br><small>' + escHtml(err.message) + '</small></div>';
    setStatus('error', 'Connection failed');
  }
}

/* ── Profile picker ─────────────────────────────────────────────────── */
function showGateError(message) {
  gateError.textContent = message || '';
  gateError.hidden = !message;
}

const GATE_TEXT = {
  default: ["Who's using CA Mode?", 'Each person gets their own chats, INDmoney connection, and daily portfolio snapshots.'],
  locked: ['Welcome back', 'Enter your PIN to unlock your saved chats and portfolio history.'],
  recover: ['Reset your PIN', 'Use the recovery code you saved when you created this profile.'],
  'recovery-code': ['Save your recovery code', "If you forget your PIN, this code is the only way back into your saved history. Nobody can reset it for you."],
  consent: ['Before you start', 'Here is what CA Mode keeps on this computer and what it sends elsewhere. Choose how much history to keep.'],
};

function showGateView(view) {
  const isConsent = view === 'consent';
  profileList.hidden = view !== 'list';
  showCreateProfile.hidden = view !== 'list';
  unlockForm.hidden = view !== 'unlock';
  createForm.hidden = view !== 'create';
  recoverForm.hidden = view !== 'recover';
  recoveryCodeView.hidden = view !== 'recovery-code';
  consentForm.hidden = !isConsent;
  gateNote.hidden = !['list', 'unlock', 'create'].includes(view);
  gateCard.classList.toggle('is-consent', isConsent);
  const textKey = view === 'unlock' && isLockedSession ? 'locked' : (GATE_TEXT[view] ? view : 'default');
  const [title, sub] = GATE_TEXT[textKey];
  const name = currentUser?.displayName;
  gateTitle.textContent = name && ['locked', 'consent'].includes(textKey) ? `${title}, ${name}` : title;
  gateSub.textContent = sub;
  showGateError('');
  const focusTargets = {
    consent: consentNotice, unlock: unlockPin, create: document.getElementById('create-name'),
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
  unlockForm.querySelector('[data-gate-back]').textContent = 'Switch profile';
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

function hideProfileGate() {
  profileGate.hidden = true;
  document.body.classList.remove('gate-open');
  unlockForm.reset();
  createForm.reset();
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
    if (response.status === 429) {
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
    showGateError(error.message);
  } finally {
    submit.disabled = false;
  }
}

showCreateProfile.addEventListener('click', () => showGateView('create'));
profileGate.querySelectorAll('[data-gate-back]').forEach(button => button.addEventListener('click', async () => {
  if (!isLockedSession) {
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
createForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const pin = document.getElementById('create-pin').value;
  if (pin !== document.getElementById('create-pin-confirm').value) {
    showGateError('The two PINs do not match.');
    return;
  }
  submitGate('/api/users', { displayName: document.getElementById('create-name').value, pin }, createForm);
});

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
    showGateError(error.message);
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
    showGateError(error.message);
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
  loadBackupStatus();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Could not load your profile.');
  renderDataZone(privacy);
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

function renderDataZone(privacy) {
  const accepted = privacy.acceptedAt ? ` You agreed to the current notice on ${new Date(privacy.acceptedAt).toLocaleDateString()}.` : '';
  dataSummary.textContent = (privacy.storageMode === 'private'
    ? 'Private mode: chats stay in memory until you sign out, and no portfolio snapshots are saved.'
    : `Saving on this computer: chats for 30 days and portfolio snapshots for ${privacy.snapshotRetentionDays} days.`) + accepted;
  renderNotice(settingsNotice, privacy.notice);
  setStorageChoice(profileForm, 'settings-storage', privacy.storageMode, privacy.snapshotRetentionDays);
}

profileForm.querySelectorAll('input[name="settings-storage"]').forEach(input => input.addEventListener('change', () => syncRetentionField(profileForm, 'settings-storage')));

document.getElementById('save-data-settings').addEventListener('click', async () => {
  const choice = readStorageChoice(profileForm, 'settings-storage');
  if (choice.storageMode === 'private' && storageMode !== 'private'
    && !window.confirm('Private mode deletes the chats and portfolio snapshots saved for this profile on this computer. Continue?')) return;
  try {
    renderDataZone(await savePrivacyChoice(choice));
    dataStatus.textContent = 'Data settings saved.';
    await loadChats();
  } catch (error) {
    dataStatus.textContent = error.message;
  }
});

document.getElementById('export-data').addEventListener('click', async () => {
  const pin = document.getElementById('export-pin').value;
  if (!pin) {
    dataStatus.textContent = 'Enter your PIN to export your data.';
    return;
  }
  const response = await fetch('/api/users/me/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    dataStatus.textContent = data.error || 'Could not export your data.';
    return;
  }
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  link.download = `ca-mode-export-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  // Revoking straight away can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  document.getElementById('export-pin').value = '';
  dataStatus.textContent = 'Export downloaded. It contains your chats and portfolio data, so keep it somewhere safe.';
});

document.getElementById('new-recovery-code').addEventListener('click', async () => {
  const pin = document.getElementById('export-pin').value;
  if (!pin) {
    dataStatus.textContent = 'Enter your PIN to create a new recovery code.';
    return;
  }
  if (!window.confirm('Create a new recovery code? Your current recovery code will stop working.')) return;
  const response = await fetch('/api/users/me/recovery-code', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    dataStatus.textContent = data.error || 'Could not create a new recovery code.';
    return;
  }
  document.getElementById('export-pin').value = '';
  document.getElementById('settings-recovery-value').textContent = data.recoveryCode;
  document.getElementById('settings-recovery').hidden = false;
  dataStatus.textContent = 'Save this code somewhere safe now. It will not be shown again, and your old code no longer works.';
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
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch('/api/backups', { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Could not back up.');
    renderBackupStatus(data);
    dataStatus.textContent = `Backed up to ${data.file}.`;
  } catch (error) {
    dataStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

document.getElementById('clear-history').addEventListener('click', async () => {
  if (!window.confirm('Delete all chats and portfolio snapshots for this profile? Your profile and INDmoney connection are kept.')) return;
  const response = await fetch('/api/users/me/history', { method: 'DELETE' });
  if (!response.ok) {
    dataStatus.textContent = 'Could not clear your history.';
    return;
  }
  profileDialog.close();
  await loadChats();
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

function renderChats() {
  chatList.innerHTML = '';
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
  navigate('chat');
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
    ${role === 'assistant' ? renderChatTrace(trace) : ''}
  `;
  messages.appendChild(div);
  renderPlotlyCharts(div);
  if (role === 'assistant') attachInvestigationCard(div, trace).catch(() => { /* The answer and trace still show. */ });
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
  if (traceDetails) traceDetails.before(card); else messageDiv.appendChild(card);
  renderPlotlyCharts(card);
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
      if (err.profileRequired || err.unlockRequired || err.consentRequired) {
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
          if (currentTrace) currentTrace.outerHTML = renderChatTrace(savedAssistant.trace);
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
    // Markers on long daily series turn into a solid band, so draw only the line.
    type: 'scatter', mode: item.points.length > 40 ? 'lines' : 'lines+markers', name: item.name,
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
  providerPicker.innerHTML = `<option value="">${escHtml(error.message)}</option>`;
  providerPicker.disabled = true;
  providerReady = true;
});
loadChats();
userInput.focus();
