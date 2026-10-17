/* ── Agent trace, investigation evidence and activity ───────────────────
   Pure helpers that turn the backend's chat trace steps and saved
   investigation records into accessible HTML, and keep the activity list in
   step with the server. Nothing here computes a financial figure: amounts
   and percentages are only formatted. In the browser they become globals
   (loaded after finance.js); under Node they are exported for tests/js. */
(function (root) {
  'use strict';

  const fin = typeof module === 'object' && module.exports ? require('./finance.js') : root;

  function esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

  /* ── Statuses ─────────────────────────────────────────────────────── */
  // Every status the backend sends for runs, steps and checks. Anything else shows its own name in a neutral tone.
  const STATUSES = {
    pending: { label: 'Pending', tone: 'pending', icon: '○' },
    running: { label: 'Running', tone: 'running', icon: '●' },
    verifying: { label: 'Verifying', tone: 'running', icon: '●' },
    completed: { label: 'Completed', tone: 'ok', icon: '✓' },
    passed: { label: 'Passed', tone: 'ok', icon: '✓' },
    warning: { label: 'Warning', tone: 'warning', icon: '!' },
    insufficient_data: { label: 'Not enough history', tone: 'warning', icon: '!' },
    failed: { label: 'Failed', tone: 'error', icon: '✕' },
    cancelled: { label: 'Cancelled', tone: 'error', icon: '✕' },
    skipped: { label: 'Skipped', tone: 'muted', icon: '–' },
    not_checked: { label: 'Not checked', tone: 'muted', icon: '–' },
    // A step still "running" after its run ended never reported back.
    unfinished: { label: 'Did not finish', tone: 'error', icon: '✕' },
  };
  const ACTIVE_STATUSES = new Set(['pending', 'running', 'verifying']);
  const TERMINAL_STATUSES = new Set(['completed', 'insufficient_data', 'failed', 'cancelled']);

  function statusInfo(status) {
    const key = String(status ?? '').trim().toLowerCase();
    if (Object.prototype.hasOwnProperty.call(STATUSES, key)) return { key, ...STATUSES[key] };
    const label = key ? key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()) : 'Unknown';
    return { key: 'unknown', label, tone: 'muted', icon: '?' };
  }

  function settledStatus(status, runFinished) {
    return runFinished && ACTIVE_STATUSES.has(String(status ?? '').toLowerCase()) ? 'unfinished' : status;
  }

  function statusPill(status) {
    const info = statusInfo(status);
    return `<span class="status-pill is-${info.tone}">${esc(info.label)}</span>`;
  }

  function formatDuration(ms) {
    if (!isNumber(ms) || ms < 0) return '';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
    const minutes = Math.floor(ms / 60_000);
    return `${minutes} min ${Math.round((ms % 60_000) / 1000)} s`;
  }

  function formatIsoDate(isoDate) {
    if (typeof isoDate !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(isoDate)) return '';
    const when = new Date(`${isoDate}T00:00:00Z`);
    return Number.isNaN(when.getTime()) ? '' : when.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  function formatDateTime(value) {
    const when = new Date(value);
    return Number.isNaN(when.getTime()) ? '' : when.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
  }

  /* ── Redaction ────────────────────────────────────────────────────── */
  // The backend masks trace text before saving it; this repeats the masking for chats saved before it did.
  const SENSITIVE_KEY_WORDS = ['token', 'secret', 'password', 'passwd', 'pin', 'pan', 'otp', 'cookie', 'authorization',
    'api_key', 'apikey', 'private_key', 'secret_key', 'session_id', 'client_secret'];
  const JSON_PAIR = /"([A-Za-z0-9_-]{1,60})"(\s*:\s*)("(?:[^"\\]|\\.)*(?:"|$)|[^,}\]\s]+)/g;

  function isSensitiveKey(key) {
    const words = key.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/-/g, '_');
    return SENSITIVE_KEY_WORDS.some((word) => words === word || words.endsWith(`_${word}`));
  }

  function redactText(text) {
    return String(text ?? '')
      .replace(JSON_PAIR, (match, key, colon) => (isSensitiveKey(key) ? `"${key}"${colon}"[redacted]"` : match))
      .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]')
      .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[redacted]')
      .replace(/\b[A-Z]{5}[0-9]{4}[A-Z]\b/g, '[redacted PAN]');
  }

  function prettyJson(text) {
    try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
  }

  /* ── Chat trace steps ─────────────────────────────────────────────── */
  const ROUTE_LABELS = {
    account_data: 'Needs your account data',
    general_finance: 'General finance',
    code_request: 'Code request, declined',
    off_topic: 'Not about finance, declined',
  };

  const LOCAL_TOOLS = {
    get_portfolio_snapshot: ['Read your saved snapshot', 'Figures computed in code from the latest daily snapshot'],
    get_networth_history: ['Read net worth history', 'Daily net worth from saved snapshots'],
    investigate_portfolio_change: ['Investigate the portfolio change', 'Compare two saved snapshots and check the data'],
    get_cas_holdings: ['Read CAS statement holdings', 'Holdings from your latest uploaded statement'],
  };

  // Why the model was asked again (agent.py "retry" events).
  const RETRY_REASONS = {
    no_account_tool: 'It answered an account question without reading your account data',
    goal_calculator: 'It worked out goal figures itself instead of using the calculator',
    invalid_tool_call: 'The AI provider rejected a malformed tool call',
    rerun_with_live_tools: 'Run again with the live INDmoney tools',
  };

  // Checks on the answer (agent.py "validation" events).
  const CHECKS = {
    numbers: ['Check the figures', 'Every amount and percentage should match a number a tool returned'],
    task_contract: ['Check the answer is complete', 'The request type decides which data it needs before it counts as answered'],
    code_output: ['Check for code', 'Answers never contain programming code'],
    length: ['Check the answer length', 'Whether the model stopped at its length limit'],
  };

  // How a tool call failed (app/tool_results.py).
  const TOOL_ERROR_KINDS = {
    invalid_arguments: 'Not called: the arguments did not fit the tool',
    rejected: 'The tool refused the request',
    unavailable: 'The data was unavailable',
    auth: 'INDmoney asked to sign in again',
    failed: 'The tool failed',
  };

  // What a tool result says about itself, when it is JSON with a status or summary (e.g. an investigation).
  function resultSummary(preview) {
    try {
      const parsed = JSON.parse(preview);
      if (!parsed || typeof parsed !== 'object') return '';
      if (typeof parsed.error === 'string' && parsed.error) return parsed.error;
      if (typeof parsed.summary === 'string' && parsed.summary) return parsed.summary;
      if (parsed.available === false) return 'No saved data was available.';
    } catch { /* A cut-off preview is not JSON; it still shows in the details. */ }
    return '';
  }

  // One backend trace step as a timeline row; unknown kinds keep their name and status.
  function chatStepView(step, runFinished = true) {
    const item = step && typeof step === 'object' ? step : {};
    const status = settledStatus(item.status || 'completed', runFinished);
    const view = { title: '', tool: '', purpose: '', status, durationMs: item.duration_ms, summary: '', error: '', details: [], links: [] };
    if (item.kind === 'route') {
      view.title = 'Classify the request';
      view.tool = item.source === 'model' ? `AI check · ${item.model || 'model'}` : 'keyword rules';
      view.purpose = 'Decide whether it needs your account data, is general finance, or is declined';
      view.summary = ROUTE_LABELS[item.category] || (item.category ? String(item.category) : '');
    } else if (item.kind === 'context') {
      const items = Array.isArray(item.items) ? item.items.map((entry) => redactText(String(entry))) : [];
      view.title = 'Use your saved goals and details';
      view.tool = 'saved context';
      view.purpose = 'Only what you entered that bears on this question, with figures calculated in code';
      view.summary = items.length ? items.join(' · ') : '';
    } else if (item.kind === 'model') {
      view.title = `Ask ${item.provider || 'the AI model'}`;
      view.tool = item.model || '';
      view.purpose = 'Write the answer or choose which data to read';
      const parts = [];
      if (Number(item.attempt) > 1) parts.push(`Attempt ${Number(item.attempt)}`);
      if (Number.isFinite(Number(item.tokens)) && Number(item.tokens) > 0) parts.push(`${Number(item.tokens).toLocaleString('en-IN')} tokens`);
      view.summary = parts.join(' · ');
    } else if (item.kind === 'tool') {
      const known = item.source === 'local' ? LOCAL_TOOLS[item.name] : null;
      view.title = known ? known[0] : item.source === 'local' ? 'Read saved data' : 'Fetch live INDmoney data';
      view.tool = item.name || 'tool';
      view.purpose = known ? known[1] : item.source === 'local' ? 'Saved on this device' : 'Read-only call to your INDmoney account';
      const args = typeof item.arguments === 'string' ? item.arguments.trim() : '';
      if (args && args !== '{}') view.details.push({ label: 'Arguments', code: prettyJson(redactText(args)) });
      if (typeof item.result_preview === 'string' && item.result_preview) {
        view.summary = redactText(resultSummary(item.result_preview));
        view.details.push({ label: 'Output preview (first 1,000 characters)', code: prettyJson(redactText(item.result_preview)) });
      }
      if (item.investigation_id) view.links.push({ label: 'Open investigation', href: `#/activity/${encodeURIComponent(item.investigation_id)}` });
      if (item.error_kind && TOOL_ERROR_KINDS[item.error_kind]) view.summary = TOOL_ERROR_KINDS[item.error_kind];
    } else if (item.kind === 'retry') {
      view.title = 'Ask the model again';
      view.tool = item.reason ? String(item.reason) : '';
      view.purpose = RETRY_REASONS[item.reason] || 'The first reply could not be used';
      view.summary = item.detail ? String(item.detail) : '';
    } else if (item.kind === 'check') {
      const known = CHECKS[item.check];
      view.title = known ? known[0] : `Check: ${humanize(item.check)}`;
      view.tool = item.check ? String(item.check) : '';
      view.purpose = known ? known[1] : '';
      view.summary = item.detail ? String(item.detail) : '';
    } else if (item.kind === 'omitted') {
      // The server keeps a long run's first and last steps and counts the ones in between (agent.bounded_trace).
      const count = omittedCount(item);
      view.title = `${count} step${count === 1 ? '' : 's'} not shown`;
      view.purpose = 'Long answers keep their first and last steps';
    } else {
      view.title = item.kind ? `Step: ${String(item.kind)}` : 'Step';
      view.tool = item.name || '';
    }
    if (item.error) view.error = redactText(item.error);
    return view;
  }

  /* ── Investigation steps ──────────────────────────────────────────── */
  function humanize(name) {
    const text = String(name || '').replace(/_/g, ' ').trim();
    return text ? text.replace(/^\w/, (c) => c.toUpperCase()) : 'Step';
  }

  // Steps in execution order (their step number, then the order received).
  function orderedSteps(steps) {
    return (Array.isArray(steps) ? steps : [])
      .filter((step) => step && typeof step === 'object')
      .map((step, index) => ({ step, index }))
      .sort((a, b) => (Number(a.step.step) || a.index + 1) - (Number(b.step.step) || b.index + 1) || a.index - b.index)
      .map(({ step }) => step);
  }

  function investigationStepView(step, runFinished) {
    return {
      title: step.purpose || humanize(step.name),
      tool: step.name || '',
      purpose: '',
      status: settledStatus(step.status, runFinished),
      durationMs: step.durationMs,
      summary: step.summary || '',
      error: step.error ? redactText(step.error) : '',
      evidence: Array.isArray(step.evidence) ? step.evidence : [],
      details: [],
      links: [],
    };
  }

  /* ── Timeline ─────────────────────────────────────────────────────── */
  function timelineStep(view, index) {
    const info = statusInfo(view.status);
    const duration = formatDuration(view.durationMs);
    const evidence = (view.evidence || []).length
      ? `<p class="timeline-evidence">Evidence: ${view.evidence.map((id) => `<code>${esc(id)}</code>`).join(' ')}</p>` : '';
    const details = view.details.length
      ? `<details class="timeline-details"${info.tone === 'error' ? ' open' : ''}><summary>Details</summary>${view.details.map((detail) =>
        `<div class="trace-detail-label">${esc(detail.label)}</div><pre class="trace-code">${esc(detail.code)}</pre>`).join('')}</details>` : '';
    const links = view.links.map((link) => `<a class="timeline-link" href="${esc(link.href)}">${esc(link.label)}</a>`).join('');
    return `<li class="timeline-step is-${info.tone}" data-status="${esc(info.key)}">
      <span class="timeline-marker" aria-hidden="true">${info.icon}</span>
      <div class="timeline-body">
        <div class="timeline-head"><span class="timeline-index">${index + 1}.</span><strong>${esc(view.title)}</strong>${statusPill(view.status)}${duration ? `<span class="timeline-time">${esc(duration)}</span>` : ''}</div>
        ${view.tool || view.purpose ? `<p class="timeline-meta">${view.tool ? `<code>${esc(view.tool)}</code>` : ''}${view.tool && view.purpose ? ' · ' : ''}${esc(view.purpose)}</p>` : ''}
        ${view.summary ? `<p class="timeline-summary">${esc(view.summary)}</p>` : ''}
        ${view.error ? `<p class="timeline-error">${esc(view.error)}</p>` : ''}
        ${evidence}${details}${links ? `<div class="timeline-links">${links}</div>` : ''}
      </div>
    </li>`;
  }

  function renderTimeline(views, { label = 'Steps', note = '' } = {}) {
    if (!views.length) return '';
    return `<ol class="timeline" aria-label="${esc(label)}">${views.map(timelineStep).join('')}</ol>${note ? `<p class="timeline-note">${esc(note)}</p>` : ''}`;
  }

  function countBy(views, tone) {
    return views.filter((view) => statusInfo(view.status).tone === tone).length;
  }

  // The disclosure under each assistant answer. The trace arrives after the answer, so every step has ended.
  function omittedCount(step) {
    return Math.max(0, Math.floor(Number(step.count)) || 0);
  }

  function renderChatTrace(trace) {
    if (!Array.isArray(trace) || !trace.length) return '';
    const views = trace.map((step) => chatStepView(step, true));
    const failed = countBy(views, 'error');
    const extra = failed ? ` · ${failed} did not complete` : '';
    const total = trace.reduce((sum, step) => sum + (step && step.kind === 'omitted' ? omittedCount(step) : 1), 0);
    return `<details class="chat-trace"><summary>How this answer was made · ${total} step${total === 1 ? '' : 's'}${extra}</summary>${renderTimeline(views, { label: 'Answer steps' })}</details>`;
  }

  function stoppedNote(record) {
    if (record.status === 'insufficient_data') return 'Stopped early: there is not enough saved history, so the comparison steps were not run.';
    if (record.status === 'failed' || record.status === 'cancelled') return 'The run stopped here; later steps were not run.';
    return '';
  }

  function renderInvestigationTimeline(record) {
    const runFinished = TERMINAL_STATUSES.has(record.status);
    const views = orderedSteps(record.steps).map((step) => investigationStepView(step, runFinished));
    if (!views.length) {
      return ACTIVE_STATUSES.has(record.status) ? '<p class="timeline-note">Waiting for the first step…</p>' : '';
    }
    return renderTimeline(views, { label: 'Investigation steps', note: stoppedNote(record) });
  }

  /* ── Investigation evidence ───────────────────────────────────────── */
  function snapshotWhen(snapshot) {
    if (!snapshot) return '—';
    return formatDateTime(snapshot.capturedAt) || formatIsoDate(snapshot.asOfDate) || '—';
  }

  function changeCell(value, pct) {
    if (!isNumber(value)) return '<td>—</td>';
    const tone = value > 0 ? 'is-up' : value < 0 ? 'is-down' : '';
    return `<td class="${tone}">${esc(fin.signedMoney(value))}${isNumber(pct) ? `<small>${esc(fin.percent(pct, { signed: true }))}</small>` : ''}</td>`;
  }

  function findingBadge(finding) {
    if (finding.kind === 'estimate') return '<span class="status-pill is-warning">Estimate</span>';
    return finding.verified ? '<span class="status-pill is-ok">Verified</span>' : '<span class="status-pill is-warning">Unverified</span>';
  }

  function section(title, body, { open = false } = {}) {
    return body ? `<details class="evidence-section"${open ? ' open' : ''}><summary>${esc(title)}</summary>${body}</details>` : '';
  }

  function table(head, rows) {
    return `<div class="markdown-table-wrap"><table class="data-table compact"><thead><tr>${head.map((cell) => `<th>${esc(cell)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  }

  function renderFindings(findings) {
    if (!findings.length) return '';
    return table(['Finding', 'Change', 'Evidence'], findings.map((finding) => `<tr><td>${esc(finding.claim)}${finding.percentageDenominator
      ? `<small>% of ${esc(finding.percentageDenominator)}</small>` : ''}</td>${isNumber(finding.changeInr) ? changeCell(finding.changeInr, finding.changePct)
      : `<td>${isNumber(finding.valueInr) ? esc(fin.signedMoney(finding.valueInr)) : '—'}</td>`}<td>${findingBadge(finding)}</td></tr>`));
  }

  function renderAssetTypes(changes) {
    const rows = changes?.byAssetType || [];
    if (!rows.length) return '';
    const body = table(['Asset type', 'Before', 'After', 'Change', 'Share of change'], rows.map((row) => `<tr><td>${esc(row.assetType)}${row.holdingsComparable === false
      ? '<small>Holdings not compared</small>' : ''}</td><td>${esc(fin.money(row.previousInr))}</td><td>${esc(fin.money(row.currentInr))}</td>${changeCell(row.changeInr, row.changePct)}<td>${isNumber(row.shareOfChangePct) ? esc(fin.percent(row.shareOfChangePct, { signed: true })) : '—'}</td></tr>`));
    const basis = changes.shareOfChangeDenominator ? `<p class="evidence-note">Share of change is of the ${esc(changes.shareOfChangeDenominator)}.</p>` : '';
    return section(`Change by asset type · ${rows.length}`, body + basis);
  }

  function renderAllocation(allocation, chart) {
    const rows = (allocation?.byAssetType || []).filter((row) => isNumber(row.valueInr));
    if (!rows.length) return '';
    const spec = { type: 'donut', title: 'Allocation by asset type', unit: '₹', data: rows.map((row) => ({ label: row.assetType, value: row.valueInr })) };
    const drawn = typeof chart === 'function' ? chart(spec) : '';
    const list = table(['Asset type', 'Value', 'Share'], rows.map((row) => `<tr><td>${esc(row.assetType)}</td><td>${esc(fin.money(row.valueInr))}</td><td>${isNumber(row.sharePct) ? esc(fin.percent(row.sharePct)) : '—'}</td></tr>`));
    const basis = `<p class="evidence-note">Shares of ${esc(fin.money(allocation.denominatorInr))}${allocation.denominatorBasis ? ` (${esc(String(allocation.denominatorBasis).replace(/_/g, ' '))})` : ''} in the later snapshot.</p>`;
    return section('Allocation in the later snapshot', (drawn || list) + basis);
  }

  function renderMovers(changes) {
    const movers = [...(changes?.topRising || []), ...(changes?.topFalling || [])];
    if (!movers.length) return '';
    return section(`Largest movers · ${movers.length}`, table(['Holding', 'Change', 'From price', 'From units'], movers.map((item) => `<tr><td>${esc(item.name)}<small>${esc(item.assetType)}${item.broker
      ? ` · ${esc(item.broker)}` : ''}${item.status && item.status !== 'held' ? ` · ${esc(item.status)}` : ''}</small></td>${changeCell(item.changeInr, item.changePct)}<td>${isNumber(item.priceEffectInr)
      ? esc(fin.signedMoney(item.priceEffectInr)) : '—'}</td><td>${isNumber(item.quantityEffectInr) ? esc(fin.signedMoney(item.quantityEffectInr)) : '—'}</td></tr>`)));
  }

  function renderChecks(checks) {
    if (!checks.length) return '';
    const passed = checks.filter((check) => check.status === 'passed').length;
    const items = checks.map((check) => `<li class="check-row"><div class="timeline-head"><strong>${esc(humanize(check.check))}</strong>${statusPill(check.status)}</div><p class="evidence-note">${esc(check.summary)}${check.nextStep ? ` ${esc(check.nextStep)}` : ''}</p></li>`).join('');
    return section(`Data checks · ${passed} of ${checks.length} passed`, `<ul class="check-list">${items}</ul>`);
  }

  function renderSources(comparison) {
    const refs = [comparison.previous, comparison.current].filter(Boolean);
    if (!refs.length) return '';
    const rows = refs.map((ref, index) => `<tr><td>${refs.length > 1 && index === 0 ? 'Earlier' : 'Later'}<small><code>${esc(ref.snapshotId)}</code></small></td><td>${esc(formatIsoDate(ref.asOfDate) || ref.asOfDate || '—')}</td><td>${esc(snapshotWhen(ref))}</td><td>${esc(ref.accountScope || '—')}</td><td>${esc(ref.currency || '—')}</td></tr>`);
    return section('Sources', table(['Snapshot', 'As of', 'Captured', 'Scope', 'Currency'], rows));
  }

  function renderList(title, items, className = '') {
    if (!Array.isArray(items) || !items.length) return '';
    return `<div class="evidence-list ${className}"><strong>${esc(title)}</strong><ul>${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div>`;
  }

  function sourceLine(comparison) {
    if (comparison.previous) {
      return `Saved INDmoney snapshots · ${esc(snapshotWhen(comparison.previous))} → ${esc(snapshotWhen(comparison.current))} · ${esc(comparison.current?.currency || 'INR')}`;
    }
    return comparison.current ? `Latest saved snapshot ${esc(snapshotWhen(comparison.current))}` : 'No saved snapshot';
  }

  // The saved run's evidence. ``chart`` draws a chart spec (the app's Plotly renderer); without it tables are shown.
  function renderInvestigationCard(record, { chart = null, linkToActivity = true, openSteps = false } = {}) {
    const run = record && typeof record === 'object' ? record : {};
    const result = run.result || {};
    const comparison = result.comparison || {};
    const actions = (Array.isArray(result.nextActions) ? result.nextActions : []).map((action) =>
      `<button class="chip" type="button" data-agent="portfolio" data-prompt="${esc(action.prompt)}">${esc(action.label)}</button>`).join('');
    const open = linkToActivity && run.id && run.saved !== false
      ? `<a class="panel-link" href="#/activity/${encodeURIComponent(run.id)}">Open in Activity</a>` : '';
    const calculations = Array.isArray(result.calculations) && result.calculations.length
      ? `<ul class="evidence-calc">${result.calculations.map((line) => `<li>${esc(line)}</li>`).join('')}</ul>` : '';
    const engine = run.metadata?.engineVersion ? `<p class="evidence-note">Engine <code>${esc(run.metadata.engineVersion)}</code></p>` : '';
    const steps = renderInvestigationTimeline(run);
    const stepCount = Array.isArray(run.steps) ? run.steps.length : 0;
    return `<section class="evidence-card" aria-label="Investigation evidence" data-investigation-id="${esc(run.id || '')}" data-status="${esc(run.status || '')}">
      <header class="evidence-head"><strong>${esc(result.title || 'Portfolio change investigation')}</strong>${statusPill(run.status)}${open}</header>
      <p class="evidence-note">${sourceLine(comparison)}</p>
      ${result.summary ? `<p class="evidence-summary">${esc(result.summary)}</p>` : ''}
      ${run.error ? `<p class="evidence-error" role="note">${esc(run.error)}</p>` : ''}
      ${run.saveError ? `<p class="evidence-note">${esc(run.saveError)}</p>` : ''}
      ${renderList('Data warnings', result.warnings, 'is-warning')}
      ${renderFindings(Array.isArray(result.findings) ? result.findings : [])}
      ${renderAssetTypes(result.changes)}
      ${renderAllocation(result.allocation, chart)}
      ${renderMovers(result.changes)}
      ${renderChecks(Array.isArray(result.checks) ? result.checks : [])}
      ${section('How this was calculated', calculations ? calculations + engine : '')}
      ${renderSources(comparison)}
      ${section(`Steps · ${stepCount}`, steps, { open: openSteps })}
      ${renderList('Limitations', result.limitations)}
      ${actions ? `<div class="follow-ups evidence-actions">${actions}</div>` : ''}
    </section>`;
  }

  /* ── Activity ─────────────────────────────────────────────────────── */
  // Filters over the backend's statuses. Nothing in CA Mode waits for approval, so there is no such filter.
  const ACTIVITY_FILTERS = [
    { id: 'all', label: 'All', statuses: null },
    { id: 'running', label: 'Running', statuses: ['running', 'verifying'] },
    { id: 'completed', label: 'Completed', statuses: ['completed', 'insufficient_data'] },
    { id: 'failed', label: 'Failed', statuses: ['failed', 'cancelled'] },
  ];

  function filterInvestigations(items, filterId) {
    const filter = ACTIVITY_FILTERS.find((item) => item.id === filterId) || ACTIVITY_FILTERS[0];
    return filter.statuses ? items.filter((item) => filter.statuses.includes(item.status)) : items.slice();
  }

  function filterCounts(items) {
    return Object.fromEntries(ACTIVITY_FILTERS.map((filter) => [filter.id, filterInvestigations(items, filter.id).length]));
  }

  function isActive(item) {
    return Boolean(item) && ACTIVE_STATUSES.has(item.status);
  }

  // Which copy of the same run to keep: the later update wins, and a finished run is never replaced by
  // an older or undated copy that still says it is running (a late response from an earlier request).
  function newerRun(current, incoming) {
    if (!current) return incoming;
    if (!incoming) return current;
    const a = Number(current.updatedAt);
    const b = Number(incoming.updatedAt);
    if (Number.isFinite(a) && Number.isFinite(b) && a !== b) return b > a ? incoming : current;
    if (TERMINAL_STATUSES.has(current.status) && !TERMINAL_STATUSES.has(incoming.status)) return current;
    return incoming;
  }

  // Merge a fresh list from the server into the one on screen. The server's list decides which runs
  // exist (deleted or pruned runs drop out); for each run the newer copy is kept. Newest first.
  function reconcileInvestigations(current, incoming) {
    const known = new Map((Array.isArray(current) ? current : []).map((item) => [item.id, item]));
    const merged = new Map();
    for (const item of Array.isArray(incoming) ? incoming : []) {
      if (!item || typeof item.id !== 'string') continue;
      merged.set(item.id, newerRun(newerRun(known.get(item.id), merged.get(item.id)), item));
    }
    return [...merged.values()].sort((a, b) => (Number(b.createdAt) || 0) - (Number(a.createdAt) || 0) || String(a.id).localeCompare(String(b.id)));
  }

  // Responses to overlapping requests can arrive out of order; only the latest request's answer is applied.
  function createSequencer() {
    let issued = 0;
    let applied = 0;
    return {
      next() { issued += 1; return issued; },
      accept(ticket) {
        if (ticket <= applied || ticket > issued) return false;
        applied = ticket;
        return true;
      },
    };
  }

  // Poll while a run is in progress, backing off; the server marks a run interrupted after about a minute.
  function pollDelay(attempt) {
    return [2000, 3000, 5000, 8000][Math.min(Math.max(0, attempt), 3)];
  }

  function investigationTitle(item) {
    const previous = formatIsoDate(item?.period?.previous);
    const current = formatIsoDate(item?.period?.current);
    if (previous && current) return `Portfolio change, ${previous} → ${current}`;
    const baseline = formatIsoDate(item?.baselineDate);
    if (baseline) return `Portfolio change since ${baseline}`;
    return 'Portfolio change';
  }

  // Runs only read saved snapshots, so a failed or cancelled one can be started again safely (never automatically).
  function canRetry(item) {
    return Boolean(item) && (item.status === 'failed' || item.status === 'cancelled');
  }

  function outcomeText(item) {
    if (isActive(item)) return 'In progress.';
    return item.error || item.summary || '';
  }

  function activityItem(item, { now = Date.now(), chatIds = null } = {}) {
    const info = statusInfo(item.status);
    const id = encodeURIComponent(item.id);
    const started = isNumber(item.createdAt) ? `Started ${fin.timeAgo(item.createdAt, now)}` : '';
    const updated = isActive(item) && isNumber(item.updatedAt) ? `updated ${fin.timeAgo(item.updatedAt, now)}` : '';
    const duration = formatDuration(item.durationMs);
    const meta = [
      started, updated, duration ? `took ${duration}` : '',
      item.trigger === 'agent' ? 'From a chat' : item.trigger === 'api' ? 'Run from Activity' : '',
      item.warningCount ? `${item.warningCount} warning${item.warningCount === 1 ? '' : 's'}` : '',
    ].filter(Boolean).join(' · ');
    const hasChat = item.chatId && (!chatIds || chatIds.has(item.chatId));
    const actions = [
      `<a class="gate-secondary small-button" href="#/activity/${id}">Open</a>`,
      hasChat ? `<button class="gate-secondary small-button" type="button" data-open-chat="${esc(item.chatId)}">Open chat</button>` : '',
      canRetry(item) ? `<button class="gate-secondary small-button" type="button" data-retry-investigation="${esc(item.id)}">Run again</button>` : '',
    ].join('');
    const absolute = isNumber(item.createdAt) ? formatDateTime(item.createdAt) : '';
    return `<li class="activity-item is-${info.tone}" data-investigation-id="${esc(item.id)}" data-status="${esc(info.key)}">
      <span class="timeline-marker" aria-hidden="true">${info.icon}</span>
      <div class="activity-body">
        <div class="timeline-head"><a class="activity-title" href="#/activity/${id}">${esc(investigationTitle(item))}</a>${statusPill(item.status)}</div>
        ${outcomeText(item) ? `<p class="activity-outcome">${esc(outcomeText(item))}</p>` : ''}
        ${meta ? `<p class="timeline-meta"${absolute ? ` title="${esc(absolute)}"` : ''}>${esc(meta)}</p>` : ''}
        <div class="activity-actions">${actions}</div>
      </div>
    </li>`;
  }

  function activityFilters(items, selected) {
    const counts = filterCounts(items);
    return `<div class="seg seg-small" role="group" aria-label="Filter investigations">${ACTIVITY_FILTERS.map((filter) =>
      `<button type="button" data-activity-filter="${filter.id}" aria-pressed="${filter.id === selected}">${esc(filter.label)} <span class="seg-count">${counts[filter.id]}</span></button>`).join('')}</div>`;
  }

  const EMPTY_FILTER_TEXT = {
    running: 'No investigation is running.',
    completed: 'No completed investigations.',
    failed: 'No failed or cancelled investigations.',
  };

  function renderActivityList(items, { filter = 'all', now = Date.now(), chatIds = null } = {}) {
    if (!items.length) return '';
    const visible = filterInvestigations(items, filter);
    const list = visible.length
      ? `<ol class="activity-list">${visible.map((item) => activityItem(item, { now, chatIds })).join('')}</ol>`
      : `<p class="page-muted activity-empty">${esc(EMPTY_FILTER_TEXT[filter] || 'Nothing matches this filter.')}</p>`;
    return `${activityFilters(items, filter)}${list}`;
  }

  function activitySkeleton(rows = 3) {
    return `<div class="activity-skeleton" role="status" aria-label="Loading investigations">${'<div class="skeleton-row"><span></span><span></span></div>'.repeat(rows)}</div>`;
  }

  const api = {
    STATUSES, ACTIVITY_FILTERS, statusInfo, settledStatus, statusPill, formatDuration, formatIsoDate,
    redactText, chatStepView, orderedSteps, investigationStepView, renderTimeline, renderChatTrace,
    renderInvestigationTimeline, renderInvestigationCard,
    filterInvestigations, filterCounts, isActive, newerRun, reconcileInvestigations, createSequencer, pollDelay,
    investigationTitle, canRetry, activityItem, renderActivityList, activitySkeleton,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
