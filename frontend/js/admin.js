/* Admin page (/admin): profiles, plans and usage from /api/admin/*. Never shows PIN-locked history. */
(() => {
  'use strict';

  const root = document.getElementById('admin');
  const SIGN_IN_ERRORS = {
    not_admin: 'That Google account is not on this copy’s admin list (ADMIN_EMAILS).',
    cancelled: 'Google sign-in was cancelled.',
    expired: 'The sign-in took too long. Try again.',
    failed: 'Google sign-in could not be verified. Try again.',
  };
  let state = { overview: null, filter: '', message: '' };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const dateFormat = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' });

  function when(ms) {
    if (!ms) return el('span', { class: 'admin-muted' }, 'Never');
    const minutes = Math.round((Date.now() - ms) / 60000);
    let relative;
    if (minutes < 1) relative = 'just now';
    else if (minutes < 60) relative = `${minutes} min ago`;
    else if (minutes < 48 * 60) relative = `${Math.round(minutes / 60)} h ago`;
    else relative = `${Math.round(minutes / 1440)} days ago`;
    return el('time', { datetime: new Date(ms).toISOString(), title: dateFormat.format(ms) }, relative);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      credentials: 'same-origin',
    });
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && body.adminRequired) {
      renderSignIn();
      throw new Error(body.error);
    }
    if (!response.ok) throw new Error(body.error || `Request failed (HTTP ${response.status}).`);
    return body;
  }

  function render(...nodes) {
    root.replaceChildren(...nodes.filter(Boolean));
  }

  function renderSignIn(error) {
    render(el('section', { class: 'admin-signin' },
      el('h2', {}, 'Sign in'),
      el('p', {}, 'Use a Google account listed in ADMIN_EMAILS. This signs you in to the admin page only, not to a profile.'),
      error ? el('p', { class: 'admin-error', role: 'alert' }, error) : null,
      el('a', { class: 'public-button', href: '/auth/admin/start' }, 'Sign in with Google'),
    ));
  }

  function tile(label, value, note) {
    return el('div', { class: 'admin-tile' },
      el('span', { class: 'admin-tile-label' }, label),
      el('strong', {}, String(value)),
      note ? el('span', { class: 'admin-muted' }, note) : null,
    );
  }

  function renderTotals(overview) {
    const { totals, plans, dailyLimit } = overview;
    const planNote = plans.map((plan) => `${totals.byPlan[plan.id] || 0} ${plan.name}`).join(' · ');
    return el('div', { class: 'admin-tiles' },
      tile('Profiles', totals.profiles, planNote),
      tile('Active in the last 7 days', totals.activeLastWeek),
      tile('Questions today', totals.questionsToday, dailyLimit ? `Free plan cap: ${dailyLimit} a day` : 'No daily cap set'),
      tile('INDmoney connected', totals.indmoneyConnected),
      tile('PIN locked now', totals.locked),
    );
  }

  async function act(label, request) {
    state.message = '';
    try {
      await request();
      state.message = label;
      await load();
    } catch (error) {
      state.message = error.message;
      renderOverview();
    }
  }

  function planSelect(profile, plans) {
    return el('select', {
      'aria-label': `Plan for ${profile.name}`,
      onchange: (event) => act(`${profile.name} is now on the ${event.target.selectedOptions[0].textContent} plan.`, () =>
        api(`/api/admin/profiles/${encodeURIComponent(profile.id)}/plan`, {
          method: 'PUT', body: JSON.stringify({ plan: event.target.value }),
        })),
    }, plans.map((plan) => el('option', { value: plan.id, selected: plan.id === profile.plan }, plan.name)));
  }

  function profileActions(profile) {
    const id = encodeURIComponent(profile.id);
    return el('div', { class: 'admin-actions' },
      profile.lockedUntil ? el('button', {
        type: 'button',
        onclick: () => act(`Cleared the PIN lock on ${profile.name}.`, () =>
          api(`/api/admin/profiles/${id}/clear-lockout`, { method: 'POST' })),
      }, 'Clear PIN lock') : null,
      el('button', {
        type: 'button',
        title: 'End every browser session signed in to this profile',
        onclick: () => {
          if (!window.confirm(`Sign ${profile.name} out on every device?`)) return;
          act(`Signed ${profile.name} out everywhere.`, () => api(`/api/admin/profiles/${id}/sign-out`, { method: 'POST' }));
        },
      }, 'Sign out'),
      el('button', {
        type: 'button',
        class: 'admin-danger',
        onclick: () => {
          const typed = window.prompt(
            `Delete ${profile.name} and everything it owns (chats, goals, snapshots, INDmoney connection)? ` +
            'This cannot be undone. Type the profile name to confirm.');
          if (typed === null) return;
          act(`Deleted ${profile.name}.`, () => api(`/api/admin/profiles/${id}`, {
            method: 'DELETE', body: JSON.stringify({ confirmName: typed }),
          }));
        },
      }, 'Delete'),
    );
  }

  function status(profile) {
    const items = [];
    items.push(el('span', { class: profile.consentCurrent ? 'admin-badge' : 'admin-badge admin-badge-warn' },
      profile.consentCurrent ? 'Notice accepted' : 'Notice not accepted'));
    if (profile.storageMode === 'private') items.push(el('span', { class: 'admin-badge' }, 'Private mode'));
    if (profile.lockedUntil) {
      items.push(el('span', { class: 'admin-badge admin-badge-warn', title: `Until ${dateFormat.format(profile.lockedUntil)}` }, 'PIN locked'));
    } else if (profile.failedPins) {
      items.push(el('span', { class: 'admin-badge' }, `${profile.failedPins} wrong PIN${profile.failedPins === 1 ? '' : 's'}`));
    }
    return el('div', { class: 'admin-badges' }, items);
  }

  function indmoney(profile) {
    if (!profile.indmoneyConnected) return el('span', { class: 'admin-muted' }, 'Not connected');
    return el('div', {},
      el('div', {}, 'Connected'),
      el('div', { class: profile.snapshotFailing ? 'admin-error' : 'admin-muted' },
        profile.snapshotFailing ? 'Last snapshot failed · ' : 'Snapshot ', when(profile.lastSnapshotAt)),
    );
  }

  function questions(profile) {
    if (profile.dailyLimit === null) return el('span', {}, `${profile.questionsToday}`, el('span', { class: 'admin-muted' }, ' · no cap'));
    return el('span', {}, `${profile.questionsToday} of ${profile.dailyLimit}`);
  }

  function matches(profile, filter) {
    if (!filter) return true;
    return [profile.name, profile.email, profile.planName, profile.id].some((value) => (value || '').toLowerCase().includes(filter));
  }

  function renderProfiles(overview) {
    const filter = state.filter.trim().toLowerCase();
    const rows = overview.profiles.filter((profile) => matches(profile, filter));
    const search = el('input', {
      type: 'search', placeholder: 'Search name, email or plan', value: state.filter, 'aria-label': 'Search profiles',
      oninput: (event) => {
        state.filter = event.target.value;
        const table = renderProfiles(overview);
        document.getElementById('admin-profiles').replaceWith(table);
      },
    });
    const body = rows.length ? rows.map((profile) => el('tr', {},
      el('td', {}, el('strong', {}, profile.name), el('div', { class: 'admin-muted' }, profile.email ?? 'Local profile (no Google account)')),
      el('td', {}, planSelect(profile, overview.plans)),
      el('td', {}, questions(profile)),
      el('td', {}, when(profile.lastSeenAt), el('div', { class: 'admin-muted' }, 'Joined ', when(profile.createdAt))),
      el('td', {}, indmoney(profile)),
      el('td', {}, status(profile)),
      el('td', {}, profileActions(profile)),
    )) : [el('tr', {}, el('td', { colspan: 7, class: 'admin-muted' }, overview.profiles.length ? 'No profile matches.' : 'No profiles yet.'))];
    const section = el('section', { id: 'admin-profiles' },
      el('div', { class: 'admin-section-head' }, el('h2', {}, `Profiles (${overview.profiles.length})`), search),
      el('div', { class: 'admin-table-wrap' }, el('table', { class: 'admin-table' },
        el('thead', {}, el('tr', {}, ['Profile', 'Plan', 'Questions today', 'Last seen', 'INDmoney', 'Status', ''].map((name) => el('th', { scope: 'col' }, name)))),
        el('tbody', {}, body),
      )),
    );
    if (document.activeElement?.type === 'search') {
      queueMicrotask(() => {
        const input = section.querySelector('input[type=search]');
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
    }
    return section;
  }

  function renderBackups(overview) {
    const { backups } = overview;
    return el('section', {},
      el('h2', {}, 'Backups'),
      el('p', {}, backups.enabled ? `Daily, keeping the newest ${backups.keep}. ` : 'Daily backups are off (BACKUP_ENABLED). ',
        `${backups.count} in ${backups.directory}. Latest: `, backups.latest ? dateFormat.format(new Date(backups.latest)) : 'none', '.'),
      el('button', {
        type: 'button',
        onclick: () => act('Backup created.', () => api('/api/admin/backups', { method: 'POST' })),
      }, 'Back up now'),
    );
  }

  function renderPageViews(overview) {
    const entries = Object.entries(overview.pageViews.byPage);
    return el('section', {},
      el('h2', {}, `Page views, last ${overview.pageViews.days} days`),
      entries.length
        ? el('ul', { class: 'admin-views' }, entries.map(([route, views]) => el('li', {}, el('span', {}, route), el('strong', {}, String(views)))))
        : el('p', { class: 'admin-muted' }, 'None counted yet.'),
    );
  }

  function renderOverview() {
    const { overview, admin } = state;
    render(
      el('div', { class: 'admin-bar' },
        el('span', { class: 'admin-muted' }, `Signed in as ${admin.email} · ${overview.authMode === 'google' ? 'Google sign-in' : 'Shared-computer profiles'}`),
        el('button', { type: 'button', onclick: signOut }, 'Sign out'),
      ),
      state.message ? el('p', { class: 'admin-message', role: 'status' }, state.message) : null,
      renderTotals(overview),
      renderProfiles(overview),
      el('div', { class: 'admin-columns' }, renderBackups(overview), renderPageViews(overview)),
    );
  }

  async function load() {
    state.overview = await api('/api/admin/overview');
    renderOverview();
  }

  async function signOut() {
    await fetch('/api/admin/sign-out', { method: 'POST', credentials: 'same-origin' });
    state = { overview: null, filter: '', message: '' };
    renderSignIn();
  }

  async function start() {
    const params = new URLSearchParams(window.location.search);
    const error = SIGN_IN_ERRORS[params.get('error')];
    if (params.has('error')) window.history.replaceState(null, '', window.location.pathname);
    try {
      const session = await api('/api/admin/session');
      if (!session.admin) {
        renderSignIn(error);
        return;
      }
      state.admin = session.admin;
      await load();
    } catch (failure) {
      if (!root.querySelector('.admin-signin')) render(el('p', { class: 'admin-error', role: 'alert' }, failure.message));
    }
  }

  start();
})();
