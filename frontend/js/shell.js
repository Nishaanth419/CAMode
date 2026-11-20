/* CA Mode: the app shell. The desktop sidebar that opens and closes, and the keyboard shortcuts.
   Loaded after pages.js; uses setSidebarOpen() from app.js and the helpers from ui.js. */

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

function setSidebarCollapsed(collapsed, { save = true } = {}) {
  document.body.classList.toggle('sidebar-closed', collapsed);
  const label = collapsed ? 'Open sidebar' : 'Close sidebar';
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
  sidebarToggle.setAttribute('aria-label', label);
  sidebarToggle.title = label;
  if (save && window.innerWidth > 1280) saveSidebarState(sidebarStorage(), collapsed ? 'closed' : 'open');
}

// Task 2 puts the command menu here; until then the search button takes focus.
function openCommand() {
  commandOpen.focus();
}

setSidebarCollapsed(
  initialSidebarState({ stored: readSidebarState(sidebarStorage()), width: window.innerWidth }) === 'closed',
  { save: false },
);
// Turn the column animation on only after the starting state has painted, so a load never slides the sidebar shut.
requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add('shell-ready')));
sidebarToggle.addEventListener('click', () => setSidebarCollapsed(!document.body.classList.contains('sidebar-closed')));
commandOpen.addEventListener('click', openCommand);
commandOpenPhone.addEventListener('click', openCommand);
document.getElementById('search-kbd').textContent = shortcutLabel(navigator.platform || navigator.userAgent);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setSidebarOpen(false);
  const shortcut = shortcutFor(event, {
    gateOpen: document.body.classList.contains('gate-open'),
    dialogOpen: Boolean(document.querySelector('dialog[open]')),
  });
  if (!shortcut) return;
  event.preventDefault();
  if (shortcut === 'command') openCommand();
  else setSidebarCollapsed(!document.body.classList.contains('sidebar-closed'));
});
