/* CA Mode: the app shell. The desktop sidebar that opens and closes, the command menu and the keyboard shortcuts.
   Loaded after pages.js; uses the globals of app.js and pages.js and the helpers from ui.js. */

const sidebarToggle = document.getElementById('sidebar-toggle');
const commandOpen = document.getElementById('command-open');
const commandOpenPhone = document.getElementById('command-open-phone');

// The wide-screen choice is the only one saved, so a medium window never overwrites it.
function sidebarStorage() {
  try {
    return window.localStorage;
  } catch (error) {
    return null;
  }
}

// Closed, the labels are hidden, so each item names itself in a native tooltip. A title the markup already has
// (the profile button's) is left alone; only the titles added here are removed again when the sidebar opens.
function setSidebarTitles(collapsed) {
  document.querySelectorAll('#sidebar .nav-item, #command-open, #account-trigger').forEach((item) => {
    if (collapsed) {
      if (item.hasAttribute('title')) return;
      item.title = item.querySelector('.nav-label')?.textContent.trim() || item.getAttribute('aria-label') || '';
      item.dataset.sidebarTitle = '';
    } else if ('sidebarTitle' in item.dataset) {
      item.removeAttribute('title');
      delete item.dataset.sidebarTitle;
    }
  });
}

function setSidebarCollapsed(collapsed, { save = true } = {}) {
  document.documentElement.classList.toggle('sidebar-closed', collapsed);
  setSidebarTitles(collapsed);
  const label = collapsed ? 'Open sidebar' : 'Close sidebar';
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
  sidebarToggle.setAttribute('aria-label', label);
  sidebarToggle.title = label;
  if (save && window.innerWidth > 1280) saveSidebarState(sidebarStorage(), collapsed ? 'closed' : 'open');
}

/* ── Command menu ───────────────────────────────────────────────────── */
const commandDialog = document.getElementById('command-dialog');
const commandInput = document.getElementById('command-input');
const commandList = document.getElementById('command-list');
let commandItems = [];
let commandActive = -1;
let commandOpener = null;

const COMMAND_ICONS = {
  ask: '/icons.svg?v=4#ph-chat-circle',
  chat: '/icons.svg?v=4#ph-chats',
  page: '/icons.svg?v=4#ph-arrow-right',
  'new-chat': '/icons.svg?v=4#ph-plus',
  'refresh-snapshot': '/icons.svg?v=4#ph-pulse',
  'add-goal': '/icons.svg?v=4#ph-target',
  'connect-broker': '/icons.svg?v=4#ph-stack',
  'sign-out': '/icons.svg?v=4#ph-arrow-right',
  light: '/icons.svg?v=4#ph-sun',
  dark: '/icons.svg?v=4#ph-moon',
};

// Every page except not-found, labelled with its own title (Ask AI for the chat).
function commandPages() {
  return Object.keys(ROUTE_META).filter((route) => route !== 'not-found').map((route) => ({ route, label: ROUTE_META[route][0] }));
}

function commandActions() {
  const dark = document.documentElement.dataset.theme === 'dark';
  return [
    { id: 'new-chat', label: 'New chat' },
    // Offered when the pages offer their own refresh button: INDmoney connected and snapshots kept.
    ...(portfolioAccess() === 'ok' ? [{ id: 'refresh-snapshot', label: 'Refresh snapshot' }] : []),
    { id: 'add-goal', label: 'Add a goal' },
    { id: 'connect-broker', label: 'Connect a broker' },
    { id: 'theme', label: dark ? 'Switch to light mode' : 'Switch to dark mode' },
    { id: 'sign-out', label: 'Sign out' },
  ];
}

function commandIcon(item) {
  if (item.kind === 'page') {
    return document.querySelector(`#sidebar .nav-item[data-route="${item.value}"] use`)?.getAttribute('href') || COMMAND_ICONS.page;
  }
  if (item.kind === 'action' && item.value === 'theme') return document.documentElement.dataset.theme === 'dark' ? COMMAND_ICONS.light : COMMAND_ICONS.dark;
  return COMMAND_ICONS[item.kind === 'action' ? item.value : item.kind] || COMMAND_ICONS.page;
}

// Rows are built with textContent: chat titles and the query are the user's own text.
function commandOption(item, index) {
  const option = document.createElement('div');
  option.className = 'command-option';
  option.id = `command-option-${index}`;
  option.setAttribute('role', 'option');
  option.setAttribute('aria-selected', 'false');
  option.dataset.index = String(index);
  const tile = document.createElement('span');
  tile.className = 'command-tile';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', commandIcon(item));
  svg.appendChild(use);
  tile.appendChild(svg);
  const label = document.createElement('span');
  label.className = 'command-label';
  label.textContent = item.label;
  option.append(tile, label);
  if (item.hint) {
    const hint = document.createElement('kbd');
    hint.setAttribute('aria-hidden', 'true');
    hint.textContent = item.hint;
    option.appendChild(hint);
  }
  return option;
}

function renderCommandResults() {
  const groups = commandResults(commandInput.value, { pages: commandPages(), chats, actions: commandActions() });
  commandItems = groups.flatMap((group) => group.items);
  let index = 0;
  commandList.replaceChildren(...groups.map((group, groupIndex) => {
    const element = document.createElement('div');
    element.className = 'command-group';
    element.setAttribute('role', 'group');
    const title = document.createElement('p');
    title.className = 'command-group-title';
    title.id = `command-group-${groupIndex}`;
    title.textContent = group.title;
    element.setAttribute('aria-labelledby', title.id);
    element.appendChild(title);
    for (const item of group.items) element.appendChild(commandOption(item, index++));
    return element;
  }));
  setCommandActive(commandItems.length ? 0 : -1);
}

function setCommandActive(index) {
  commandActive = index;
  commandList.querySelectorAll('[role="option"]').forEach((option) => {
    option.setAttribute('aria-selected', String(Number(option.dataset.index) === index));
  });
  const active = index >= 0 ? document.getElementById(`command-option-${index}`) : null;
  if (active) {
    commandInput.setAttribute('aria-activedescendant', active.id);
    active.scrollIntoView({ block: 'nearest' });
  } else {
    commandInput.removeAttribute('aria-activedescendant');
  }
}

function openCommandMenu(opener = document.activeElement) {
  if (commandDialog.open || document.body.classList.contains('gate-open')) return;
  commandOpener = opener;
  commandInput.value = '';
  renderCommandResults();
  commandDialog.showModal();
  commandInput.focus();
}

// Focus goes back to whatever opened the menu; an action run afterwards may still move it (the composer, a dialog).
function restoreCommandFocus() {
  const opener = commandOpener;
  commandOpener = null;
  if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
}

function closeCommandMenu() {
  if (!commandDialog.open) return;
  commandDialog.close();
  restoreCommandFocus();
}

// Errors show where the person is looking: in the conversation on Ask AI, else as a toast.
function reportCommandError(error) {
  if (currentRoute === 'chat') showError(error);
  else toast(errorText(error));
}

// Whether moving to `route` hides the element that opened the menu (it sits on the page or chat being left).
function openerHiddenBy(opener, route) {
  if (!opener || typeof opener.closest !== 'function' || route === currentRoute) return false;
  return Boolean(opener.closest('#page-view, #chat-head, #chat-scroll, #input-area'));
}

// After a page change, focus starts at the new page's heading, or in the composer on Ask AI.
function focusRouteStart(route) {
  if (route !== currentRoute) return;
  if (route === 'chat') {
    userInput.focus();
    return;
  }
  const heading = pageView.querySelector('.page:not([hidden]) h1');
  if (!heading) return;
  if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
  heading.focus();
}

async function runCommand(item) {
  const blocked = isStreaming && commandWaitsForAnswer(item);
  // Focus going back to an opener the page change is about to hide would fall to <body>.
  const route = item.kind === 'page' ? item.value : item.kind === 'chat' ? 'chat' : null;
  const refocus = !blocked && route !== null && openerHiddenBy(commandOpener, route);
  if (refocus) commandOpener = null;
  closeCommandMenu();
  if (blocked) {
    toast('An answer is still coming. Try again when it finishes.');
    return;
  }
  try {
    if (item.kind === 'page') {
      navigate(item.value);
      if (refocus) routeSwapped.then(() => focusRouteStart(route));
    } else if (item.kind === 'chat') {
      navigate('chat');
      if (refocus) routeSwapped.then(() => focusRouteStart(route));
      await selectChat(item.value);
    } else if (item.kind === 'ask') await runPrompt(item.value, item.agent);
    else if (item.value === 'new-chat') {
      navigate('chat');
      await startNewChat();
    } else if (item.value === 'refresh-snapshot') {
      toast('Saving snapshot…');
      await refreshSnapshot();
      toast('Snapshot saved.');
    } else if (item.value === 'add-goal') {
      navigate('goals');
      openGoalDialog();
    } else if (item.value === 'connect-broker') await openBrokers();
    else if (item.value === 'theme') toggleTheme();
    else if (item.value === 'sign-out') {
      await fetch('/api/session/logout', { method: 'POST' });
      await loadChats();
    }
  } catch (error) {
    reportCommandError(error);
  }
}

commandInput.addEventListener('input', renderCommandResults);
commandInput.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    setCommandActive(moveActive(commandActive, event.key === 'ArrowDown' ? 1 : -1, commandItems.length));
  } else if (event.key === 'Enter' && !isComposingEnter(event)) {
    event.preventDefault();
    if (commandItems[commandActive]) runCommand(commandItems[commandActive]);
  }
});
commandList.addEventListener('click', (event) => {
  const option = event.target.closest('[role="option"]');
  if (option) runCommand(commandItems[Number(option.dataset.index)]);
});
commandList.addEventListener('pointermove', (event) => {
  const option = event.target.closest('[role="option"]');
  if (option && Number(option.dataset.index) !== commandActive) setCommandActive(Number(option.dataset.index));
});
// The panel fills the dialog box, so a click on the dialog itself is a click on the backdrop.
commandDialog.addEventListener('click', (event) => {
  if (event.target === commandDialog) closeCommandMenu();
});
// Esc closes the dialog natively. A close queued by an earlier close() can arrive after a re-open, so check it is shut.
commandDialog.addEventListener('close', () => {
  if (!commandDialog.open) restoreCommandFocus();
});

setSidebarCollapsed(
  initialSidebarState({ stored: readSidebarState(sidebarStorage()), width: window.innerWidth }) === 'closed',
  { save: false },
);
// Turn the column animation on only after the starting state has painted, so a load never slides the sidebar shut.
requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add('shell-ready')));
sidebarToggle.addEventListener('click', () => setSidebarCollapsed(!document.documentElement.classList.contains('sidebar-closed')));
commandOpen.addEventListener('click', (event) => openCommandMenu(event.currentTarget));
commandOpenPhone.addEventListener('click', (event) => openCommandMenu(event.currentTarget));
document.getElementById('search-kbd').textContent = shortcutLabel(navigator.platform || navigator.userAgent);

document.addEventListener('keydown', (event) => {
  // Escape over an open dialog (a slide-over opened from the drawer) closes only that dialog.
  if (event.key === 'Escape' && !document.querySelector('dialog[open]')) setSidebarOpen(false);
  const shortcut = shortcutFor(event, {
    gateOpen: document.body.classList.contains('gate-open'),
    dialogOpen: Boolean(document.querySelector('dialog[open]')),
    commandOpen: commandDialog.open,
  });
  if (!shortcut) return;
  event.preventDefault();
  if (shortcut === 'command') openCommandMenu();
  else if (shortcut === 'close-command') closeCommandMenu();
  else setSidebarCollapsed(!document.documentElement.classList.contains('sidebar-closed'));
});
