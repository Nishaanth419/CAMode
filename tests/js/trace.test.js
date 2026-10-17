// Unit tests for frontend/js/trace.js. Run with: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const trace = require('../../frontend/js/trace.js');

// Shaped like a saved run from GET /api/investigations/{id} (app/investigations.py).
function completedRecord(overrides = {}) {
  return {
    id: 'inv-1', kind: 'portfolio_change', status: 'completed', createdAt: 1_760_500_000_000, completedAt: 1_760_500_000_042,
    updatedAt: 1_760_500_000_042, saved: true,
    request: { baselineDate: null, trigger: 'agent', chatId: 'chat-1' },
    metadata: { engineVersion: 'portfolio_change.v1' },
    steps: [
      { step: 1, name: 'get_latest_snapshot', purpose: 'Load the newest saved snapshot', status: 'completed', durationMs: 3, summary: 'Snapshot of 2026-10-14.', evidence: ['snap:2026-10-14'] },
      { step: 2, name: 'get_previous_snapshot', purpose: 'Load the comparable earlier snapshot', status: 'completed', durationMs: 2, summary: 'Snapshot of 2026-10-13.' },
      { step: 3, name: 'validate_snapshot_consistency', purpose: 'Check scope, freshness, totals, denominators and currency', status: 'warning', durationMs: 1, summary: '5 of 6 checks passed; flagged: freshness.' },
    ],
    result: {
      title: 'Portfolio change investigation',
      summary: 'Net worth rose by ₹79,000.00 between 2026-10-13 and 2026-10-14.',
      comparison: {
        previous: { snapshotId: 'snap:2026-10-13', asOfDate: '2026-10-13', capturedAt: '2026-10-13T09:00+05:30', accountScope: 'all', currency: 'INR', source: 'indmoney' },
        current: { snapshotId: 'snap:2026-10-14', asOfDate: '2026-10-14', capturedAt: '2026-10-14T09:00+05:30', accountScope: 'all', currency: 'INR', source: 'indmoney' },
      },
      findings: [
        { id: 'net_worth', kind: 'observed', claim: 'Net worth rose by ₹79,000.00.', verified: true, changeInr: 79000, changePct: 9.75, percentageDenominator: 'net worth on 2026-10-13' },
        { id: 'market_effect', kind: 'estimate', claim: 'About +₹25,000.00 points to price movement.', verified: true, valueInr: 25000 },
      ],
      changes: {
        byAssetType: [{ assetType: 'MF', holdingsComparable: true, previousInr: 500000, currentInr: 554000, changeInr: 54000, changePct: 10.8, shareOfChangePct: 68.35 }],
        shareOfChangeDenominator: 'change in total current value (₹79,000.00)',
        topRising: [{ name: 'Parag Parikh Flexi Cap Fund', assetType: 'MF', status: 'held', changeInr: 54000, changePct: 10.8, priceEffectInr: 54000, quantityEffectInr: 0 }],
        topFalling: [],
      },
      allocation: { denominatorInr: 889000, denominatorBasis: 'total current value', byAssetType: [{ assetType: 'MF', valueInr: 554000, sharePct: 62.32 }] },
      checks: [{ check: 'snapshot_freshness', status: 'warning', summary: 'The latest snapshot is 40 hours old.', nextStep: 'Save a snapshot now.' }],
      warnings: ['The latest snapshot is 40 hours old.'],
      limitations: ['These are observed changes in your saved data.'],
      calculations: ['change = value in the later snapshot - value in the earlier snapshot'],
      nextActions: [{ label: 'Check allocation', prompt: 'Check my current allocation by asset type.' }],
    },
    ...overrides,
  };
}

const listItem = (id, status, extra = {}) => ({ id, kind: 'portfolio_change', status, createdAt: 1000, updatedAt: 1000, ...extra });

/* ── 1. Statuses ───────────────────────────────────────────────────── */

test('every backend status has its own label and tone', () => {
  const expected = {
    pending: ['Pending', 'pending'], running: ['Running', 'running'], verifying: ['Verifying', 'running'],
    completed: ['Completed', 'ok'], passed: ['Passed', 'ok'], warning: ['Warning', 'warning'],
    insufficient_data: ['Not enough history', 'warning'], failed: ['Failed', 'error'], cancelled: ['Cancelled', 'error'],
    skipped: ['Skipped', 'muted'], not_checked: ['Not checked', 'muted'],
  };
  for (const [status, [label, tone]] of Object.entries(expected)) {
    const info = trace.statusInfo(status);
    assert.equal(info.label, label, status);
    assert.equal(info.tone, tone, status);
    assert.match(trace.statusPill(status), new RegExp(`class="status-pill is-${tone}">${label}<`));
  }
});

test('failed, pending and skipped never look like success', () => {
  for (const status of ['failed', 'pending', 'skipped', 'cancelled', 'not_checked']) {
    assert.notEqual(trace.statusInfo(status).tone, 'ok', status);
  }
  assert.notEqual(trace.statusInfo('failed').tone, trace.statusInfo('pending').tone);
});

test('a step still running after its run ended shows as not finished', () => {
  assert.equal(trace.settledStatus('running', true), 'unfinished');
  assert.equal(trace.settledStatus('running', false), 'running');
  assert.equal(trace.statusInfo('unfinished').label, 'Did not finish');
  const html = trace.renderChatTrace([{ kind: 'tool', name: 'networth_snapshot', source: 'indmoney', status: 'running' }]);
  assert.match(html, /Did not finish/);
  assert.doesNotMatch(html, />Running</);
});

test('each run status renders on the evidence card', () => {
  for (const status of ['running', 'verifying', 'completed', 'insufficient_data', 'failed', 'cancelled']) {
    const html = trace.renderInvestigationCard(completedRecord({ status }));
    assert.match(html, new RegExp(`data-status="${status}"`));
    assert.match(html, new RegExp(trace.statusInfo(status).label));
  }
});

test('an unknown status is shown by name in a neutral tone', () => {
  assert.deepEqual(trace.statusInfo('needs_review'), { key: 'unknown', label: 'Needs review', tone: 'muted', icon: '?' });
  assert.equal(trace.statusInfo(undefined).label, 'Unknown');
});

/* ── 2. Ordering and association ───────────────────────────────────── */

test('investigation steps run in step order whatever order they arrive in', () => {
  const steps = [{ step: 3, name: 'c' }, { step: 1, name: 'a' }, { step: 2, name: 'b' }, null, { name: 'no-number' }];
  assert.deepEqual(trace.orderedSteps(steps).map((step) => step.name), ['a', 'b', 'c', 'no-number']);
  const html = trace.renderInvestigationTimeline({ status: 'completed', steps: [steps[0], steps[1], steps[2]] });
  assert.ok(html.indexOf('<code>a</code>') < html.indexOf('<code>b</code>'));
  assert.ok(html.indexOf('<code>b</code>') < html.indexOf('<code>c</code>'));
});

test('the chat trace keeps execution order and links each tool step to its own investigation', () => {
  const html = trace.renderChatTrace([
    { kind: 'route', status: 'completed', category: 'account_data', source: 'keywords', duration_ms: 1 },
    { kind: 'model', provider: 'Ollama', model: 'ca-mode', status: 'completed', duration_ms: 900 },
    { kind: 'tool', name: 'investigate_portfolio_change', source: 'local', status: 'completed', investigation_id: 'first' },
    { kind: 'tool', name: 'investigate_portfolio_change', source: 'local', status: 'completed', investigation_id: 'second' },
  ]);
  const order = ['Classify the request', 'Ask Ollama', 'href="#/activity/first"', 'href="#/activity/second"'].map((text) => html.indexOf(text));
  assert.ok(order.every((position) => position >= 0), order.join());
  assert.deepEqual([...order].sort((a, b) => a - b), order);
  assert.match(html, /How this answer was made · 4 steps/);
});

test('timeline rows carry durations and evidence references', () => {
  const html = trace.renderInvestigationTimeline(completedRecord());
  assert.match(html, /3 ms/);
  assert.match(html, /Evidence: <code>snap:2026-10-14<\/code>/);
  assert.match(html, /<code>get_latest_snapshot<\/code>/);
  assert.equal(trace.formatDuration(1500), '1.5 s');
  assert.equal(trace.formatDuration(125_000), '2 min 5 s');
  assert.equal(trace.formatDuration(undefined), '');
});

test('runs that stop early say the later steps were not run', () => {
  const failed = trace.renderInvestigationTimeline({ status: 'failed', steps: [{ step: 1, name: 'get_latest_snapshot', status: 'failed', error: 'Timed out.' }] });
  assert.match(failed, /later steps were not run/);
  assert.match(failed, /Timed out\./);
  const short = trace.renderInvestigationTimeline({ status: 'insufficient_data', steps: [{ step: 1, name: 'get_latest_snapshot', status: 'completed' }] });
  assert.match(short, /comparison steps were not run/);
  assert.match(trace.renderInvestigationTimeline({ status: 'running', steps: [] }), /Waiting for the first step/);
});

/* ── 3. Expanding details ──────────────────────────────────────────── */

test('step details are collapsible, open for failures and absent when there is nothing to show', () => {
  const ok = trace.renderChatTrace([{ kind: 'tool', name: 'networth_holdings', source: 'indmoney', status: 'completed', arguments: '{"asset_type":"MF"}', result_preview: '{"rows":[]}' }]);
  assert.match(ok, /<details class="timeline-details"><summary>Details<\/summary>/);
  assert.match(ok, /&quot;asset_type&quot;: &quot;MF&quot;/);
  const failed = trace.renderChatTrace([{ kind: 'tool', name: 'networth_holdings', source: 'indmoney', status: 'failed', arguments: '{"asset_type":"MF"}', error: 'HTTP 500' }]);
  assert.match(failed, /<details class="timeline-details" open>/);
  const plain = trace.renderChatTrace([{ kind: 'tool', name: 'get_portfolio_snapshot', source: 'local', status: 'completed', arguments: '{}' }]);
  assert.doesNotMatch(plain, /timeline-details/);
  // The whole trace starts collapsed under the answer.
  assert.match(ok, /^<details class="chat-trace"><summary>/);
});

/* ── 4. Missing fields and unknown kinds ───────────────────────────── */

test('missing optional fields and unknown step kinds render without errors', () => {
  assert.equal(trace.renderChatTrace(undefined), '');
  assert.equal(trace.renderChatTrace('nope'), '');
  const html = trace.renderChatTrace([{}, { kind: 'handoff', name: 'later', status: 'mystery' }, null]);
  assert.match(html, /Step: handoff/);
  assert.match(html, /Mystery/);
  assert.doesNotThrow(() => trace.renderInvestigationCard({}));
  assert.doesNotThrow(() => trace.renderInvestigationCard(null));
  const bare = trace.renderInvestigationCard({ id: 'x', status: 'failed', error: 'Timed out. No findings were produced.' });
  assert.match(bare, /No saved snapshot/);
  assert.match(bare, /Timed out\. No findings were produced\./);
  assert.doesNotMatch(bare, /undefined|NaN/);
  assert.equal(trace.investigationTitle({}), 'Portfolio change');
});

test('a long trace says how many steps were left out and counts them in the total', () => {
  const steps = [{ kind: 'route', category: 'account_data' }, { kind: 'model', attempt: 1 }, { kind: 'tool', name: 'networth_snapshot' },
    { kind: 'omitted', status: 'completed', count: 9 }, { kind: 'model', attempt: 9, status: 'completed' }];
  assert.equal(trace.chatStepView(steps[3]).title, '9 steps not shown');
  assert.equal(trace.chatStepView({ kind: 'omitted', count: 1 }).title, '1 step not shown');
  assert.equal(trace.chatStepView({ kind: 'omitted', count: 'x' }).title, '0 steps not shown');
  assert.match(trace.renderChatTrace(steps), /How this answer was made · 13 steps/);
});

test('retries, checks, tokens and tool error kinds have their own rows', () => {
  const retry = trace.chatStepView({ kind: 'retry', status: 'completed', reason: 'goal_calculator', detail: 'goal figures written without the calculator' });
  assert.equal(retry.title, 'Ask the model again');
  assert.match(retry.purpose, /calculator/);
  assert.equal(retry.summary, 'goal figures written without the calculator');
  const check = trace.chatStepView({ kind: 'check', check: 'numbers', status: 'warning', detail: '₹4.5 lakh' });
  assert.equal(check.title, 'Check the figures');
  assert.equal(check.status, 'warning');
  assert.equal(trace.chatStepView({ kind: 'check', check: 'task_contract', status: 'failed' }).title, 'Check the answer is complete');
  assert.equal(trace.chatStepView({ kind: 'check', check: 'new_rule', status: 'passed' }).title, 'Check: New rule');
  assert.equal(trace.chatStepView({ kind: 'model', attempt: 2, tokens: 12345 }).summary, 'Attempt 2 · 12,345 tokens');
  assert.equal(trace.chatStepView({ kind: 'model', attempt: 1, tokens: 'x' }).summary, '');
  const tool = trace.chatStepView({ kind: 'tool', name: 'networth_snapshot', status: 'failed', error_kind: 'unavailable', error: '503' });
  assert.equal(tool.summary, 'The data was unavailable');
  assert.equal(tool.error, '503');
  const html = trace.renderChatTrace([{ kind: 'retry', reason: '<b>x</b>', detail: '<img src=x>' }]);
  assert.doesNotMatch(html, /<img|<b>/);
});

/* ── Answer runs ───────────────────────────────────────────────────── */

function savedRun(overrides = {}) {
  return {
    id: 'run-1', createdAt: 1_760_500_000_000, chatId: 'chat-1', agentType: 'finance', question: 'How much a month for ₹50 lakh?',
    status: 'answered', task: 'goal_calculation', verdict: { task: 'goal_calculation', status: 'met', checks: [
      { check: 'answered', outcome: 'passed' }, { check: 'required_tool', outcome: 'passed', detail: 'calculate_goal_plan' },
      { check: 'numbers', outcome: 'warning' }] },
    summary: { modelCalls: 2, toolCalls: 2, failedToolCalls: 1, retries: 1, promptTokens: 3000, completionTokens: 400, durationMs: 15200, termination: 'answered' },
    events: [
      { seq: 1, atMs: 0, kind: 'route', category: 'general_finance', source: 'model', model: 'ca-mode', durationMs: 1200 },
      { seq: 2, atMs: 1, kind: 'contract', task: 'goal_calculation', requiredTools: ['calculate_goal_plan'] },
      { seq: 3, atMs: 2, kind: 'model_call', provider: 'Ollama', model: 'ca-mode', attempt: 1 },
      { seq: 4, atMs: 3000, kind: 'model_result', attempt: 1, status: 'completed', promptTokens: 1000, completionTokens: 100, durationMs: 2998 },
      { seq: 5, atMs: 3001, kind: 'tool_call', name: 'calculate_goal_plan', source: 'local', arguments: '{"currency":"₹"}' },
      { seq: 6, atMs: 3002, kind: 'tool_result', name: 'calculate_goal_plan', errorKind: 'rejected', error: 'currency: Input should be INR', durationMs: 1 },
      { seq: 7, atMs: 3003, kind: 'tool_call', name: 'calculate_goal_plan', source: 'local', arguments: '{"currency":"INR"}' },
      { seq: 8, atMs: 3004, kind: 'tool_result', name: 'calculate_goal_plan', resultPreview: '{"plan":{}}', durationMs: 1 },
      { seq: 9, atMs: 3005, kind: 'model_call', provider: 'Ollama', model: 'ca-mode', attempt: 2 },
      { seq: 10, atMs: 9000, kind: 'retry', reason: 'goal_calculator' },
      { seq: 11, atMs: 15000, kind: 'validation', check: 'numbers', outcome: 'warning', detail: '₹4.5 lakh' },
      { seq: 12, atMs: 15100, kind: 'answer', chars: 420 },
      { seq: 13, atMs: 15200, kind: 'termination', reason: 'answered' },
    ],
    ...overrides,
  };
}

test('a saved run becomes the same step rows as the answer trace, every step kept', () => {
  const steps = trace.runSteps(savedRun().events);
  assert.deepEqual(steps.map((step) => step.kind), ['route', 'contract', 'model', 'tool', 'tool', 'model', 'retry', 'check', 'termination']);
  assert.equal(steps[2].tokens, 1100);
  assert.equal(steps[3].status, 'failed');
  assert.equal(steps[3].error_kind, 'rejected');
  assert.equal(steps[4].status, 'completed');
  // A model call with no result never reported back.
  assert.equal(trace.chatStepView(steps[5], true).status, 'unfinished');
  assert.deepEqual(trace.runSteps([null, 'x', { kind: 'tool_result', name: 'nope' }, { kind: 'model_result', attempt: 9 }]), []);
});

test('the run card shows the question, verdict checks, counts and every step', () => {
  const html = trace.renderRunCard(savedRun());
  for (const part of ['How much a month for ₹50 lakh?', 'Goal calculation', 'Complete', 'Checks on this answer', 'Check the figures',
    'Steps · 9', '2 model calls', '2 tool calls (1 failed)', '1 retry', '3,400 tokens', 'took 15', 'Decide what the answer needs',
    'Needs one of: calculate_goal_plan', 'The tool refused the request', 'Finished']) {
    assert.ok(html.includes(part), part);
  }
  assert.match(trace.renderRunCard(savedRun({ status: 'failed', error: 'Bearer abc.def broke', verdict: null })), /Bearer \[redacted\] broke/);
  assert.equal(trace.renderRunCard(null), '');
});

test('the run list links each answer to its record and its chat', () => {
  const html = trace.renderRunList([savedRun(), savedRun({ id: 'run-2', chatId: 'gone', question: '', task: 'account_data', status: 'auth_failed' })],
    { now: 1_760_500_060_000, chatIds: new Set(['chat-1']) });
  assert.match(html, /href="#\/activity\/answer-run-1"/);
  assert.equal((html.match(/data-open-chat="chat-1"/g) || []).length, 1);
  assert.match(html, /Account data/);
  assert.equal(trace.runStatus({ status: 'auth_failed' }), 'failed');
  assert.equal(trace.runStatus({ status: 'refused' }), 'skipped');
  assert.match(trace.renderRunList([]), /No saved answers yet/);
  assert.doesNotMatch(trace.renderRunList([savedRun({ question: '<script>x</script>' })]), /<script>/);
});

test('goal tools have their own titles instead of "Read saved data"', () => {
  assert.equal(trace.chatStepView({ kind: 'tool', name: 'calculate_goal_plan', source: 'local' }).title, 'Calculate the goal');
  assert.match(trace.chatStepView({ kind: 'tool', name: 'calculate_goal_plan', source: 'local' }).purpose, /nothing is saved/);
  assert.equal(trace.chatStepView({ kind: 'tool', name: 'get_goal_fund_comparison', source: 'local' }).title, 'Read fund figures for a goal');
});

test('an answer trace links to its saved run only when there is one', () => {
  const steps = [{ kind: 'route', category: 'general_finance' }];
  assert.match(trace.renderChatTrace(steps, { runId: 'abc 1' }), /href="#\/activity\/answer-abc%201"/);
  assert.doesNotMatch(trace.renderChatTrace(steps), /Open the full record/);
});

/* ── Structured answer ─────────────────────────────────────────────── */

test('the evidence card separates findings, warnings, sources, steps and actions with real ids', () => {
  const html = trace.renderInvestigationCard(completedRecord());
  for (const part of ['Data warnings', 'Change by asset type · 1', 'Allocation in the later snapshot', 'Largest movers · 1',
    'Data checks · 0 of 1 passed', 'How this was calculated', 'Sources', 'Steps · 3', 'Limitations']) {
    assert.ok(html.includes(part), part);
  }
  assert.match(html, /<span class="status-pill is-warning">Estimate<\/span>/);
  assert.match(html, /<span class="status-pill is-ok">Verified<\/span>/);
  assert.match(html, /href="#\/activity\/inv-1">Open in Activity/);
  assert.match(html, /data-prompt="Check my current allocation by asset type\."/);
  assert.match(html, /<code>snap:2026-10-13<\/code>/);
  // Figures keep their currency and sign; shares say what they are a share of.
  assert.match(html, /\+₹79,000/);
  assert.match(html, /\+9\.75%/);
  assert.match(html, /Share of change is of the change in total current value/);
});

test('allocation uses the app chart renderer when given and a table otherwise', () => {
  let seen = null;
  const drawn = trace.renderInvestigationCard(completedRecord(), { chart: (spec) => { seen = spec; return '<figure>chart</figure>'; } });
  assert.deepEqual(seen.data, [{ label: 'MF', value: 554000 }]);
  assert.match(drawn, /<figure>chart<\/figure>/);
  assert.match(trace.renderInvestigationCard(completedRecord()), /62\.3%/);
});

test('an unsaved run offers no link to Activity', () => {
  assert.doesNotMatch(trace.renderInvestigationCard(completedRecord({ saved: false })), /Open in Activity/);
  assert.doesNotMatch(trace.renderInvestigationCard(completedRecord(), { linkToActivity: false }), /Open in Activity/);
});

/* ── 5. Loading and empty states ───────────────────────────────────── */

test('loading and empty states are announced and specific', () => {
  assert.match(trace.activitySkeleton(), /role="status"/);
  assert.equal(trace.renderActivityList([]), '');
  const html = trace.renderActivityList([listItem('a', 'completed')], { filter: 'failed' });
  assert.match(html, /No failed or cancelled investigations\./);
  assert.match(html, /aria-pressed="true">Failed <span class="seg-count">0<\/span>/);
});

/* ── 6. Filters and reopening ──────────────────────────────────────── */

test('filters group the backend statuses and count from the list itself', () => {
  const items = [listItem('a', 'running'), listItem('b', 'verifying'), listItem('c', 'completed'),
    listItem('d', 'insufficient_data'), listItem('e', 'failed'), listItem('f', 'cancelled')];
  assert.deepEqual(trace.filterCounts(items), { all: 6, running: 2, completed: 2, failed: 2 });
  assert.deepEqual(trace.filterInvestigations(items, 'failed').map((item) => item.id), ['e', 'f']);
  assert.deepEqual(trace.filterInvestigations(items, 'bogus').length, 6);
  assert.ok(!trace.ACTIVITY_FILTERS.some((filter) => /approval/i.test(filter.label)), 'no approval workflow exists');
});

test('activity rows reopen the run and its chat, and offer a retry only when safe', () => {
  const now = 1000 + 3 * 3_600_000;
  const done = trace.activityItem(listItem('run 1', 'completed', {
    chatId: 'chat-1', durationMs: 42, trigger: 'agent', warningCount: 1, summary: 'Net worth rose.',
    period: { previous: '2026-10-01', current: '2026-10-14' },
  }), { now, chatIds: new Set(['chat-1']) });
  assert.match(done, /href="#\/activity\/run%201">Portfolio change, 1 Oct 2026 → 14 Oct 2026/);
  assert.match(done, /data-open-chat="chat-1"/);
  assert.match(done, /Started 3h ago · took 42 ms · From a chat · 1 warning/);
  assert.doesNotMatch(done, /data-retry-investigation/);
  // A chat that has since been deleted or expired is not offered.
  assert.doesNotMatch(trace.activityItem(listItem('x', 'completed', { chatId: 'gone' }), { chatIds: new Set() }), /data-open-chat/);
  const failed = trace.activityItem(listItem('y', 'failed', { error: 'Timed out. No findings were produced.' }));
  assert.match(failed, /data-retry-investigation="y"/);
  assert.match(failed, /Timed out\./);
  assert.ok(!trace.canRetry({ status: 'running' }) && !trace.canRetry({ status: 'completed' }) && trace.canRetry({ status: 'cancelled' }));
  assert.match(trace.activityItem(listItem('z', 'running')), /In progress\./);
});

/* ── 7. Duplicate and out-of-order updates ─────────────────────────── */

test('duplicate runs collapse to the newest copy', () => {
  const merged = trace.reconcileInvestigations([], [
    listItem('a', 'running', { updatedAt: 5 }), listItem('a', 'completed', { updatedAt: 9 }), listItem('a', 'running', { updatedAt: 7 }),
  ]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].status, 'completed');
});

test('an older response never overwrites a newer status', () => {
  const shown = [listItem('a', 'completed', { updatedAt: 9 })];
  assert.equal(trace.reconcileInvestigations(shown, [listItem('a', 'running', { updatedAt: 5 })])[0].status, 'completed');
  // Without timestamps a finished run still wins over one that says it is running.
  assert.equal(trace.newerRun({ id: 'a', status: 'failed' }, { id: 'a', status: 'running' }).status, 'failed');
  assert.equal(trace.newerRun({ id: 'a', status: 'running', updatedAt: 1 }, { id: 'a', status: 'completed', updatedAt: 2 }).status, 'completed');
});

test('responses to earlier requests are dropped once a later one is applied', () => {
  const requests = trace.createSequencer();
  const first = requests.next();
  const second = requests.next();
  assert.equal(requests.accept(second), true);
  assert.equal(requests.accept(first), false);
  assert.equal(requests.accept(second), false, 'the same response is applied once');
  assert.equal(requests.accept(99), false, 'unknown tickets are ignored');
});

/* ── 8. Reconciling after a refresh ────────────────────────────────── */

test('a refresh adopts the server list: new runs appear, finished runs update, deleted runs go', () => {
  const before = [listItem('a', 'running', { createdAt: 1, updatedAt: 1 }), listItem('gone', 'completed', { createdAt: 2 })];
  const after = trace.reconcileInvestigations(before, [
    listItem('a', 'failed', { createdAt: 1, updatedAt: 60_000, error: 'This investigation was interrupted before it finished.' }),
    listItem('new', 'completed', { createdAt: 3, updatedAt: 3 }),
  ]);
  assert.deepEqual(after.map((item) => [item.id, item.status]), [['new', 'completed'], ['a', 'failed']]);
  assert.equal(trace.isActive(after[1]), false);
  assert.deepEqual([0, 1, 2, 3, 9].map(trace.pollDelay), [2000, 3000, 5000, 8000, 8000]);
});

/* ── 9. Sensitive data ─────────────────────────────────────────────── */

test('credentials and identifiers never reach the trace on screen', () => {
  const html = trace.renderChatTrace([{
    kind: 'tool', name: 'networth_snapshot', source: 'indmoney', status: 'failed',
    arguments: '{"accessToken":"tok-123","asset_type":"MF"}',
    result_preview: '{"pan":"ABCDE1234F","client_secret":"cs-9","networth":4200000,"refresh_token":"cut-mid',
    error: '401 Unauthorized: Bearer eyJhbGc.eyJzdWI.c2ln',
  }]);
  for (const secret of ['tok-123', 'ABCDE1234F', 'cs-9', 'cut-mid', 'eyJhbGc']) assert.ok(!html.includes(secret), secret);
  assert.match(html, /&quot;asset_type&quot;: &quot;MF&quot;/);
  assert.match(html, /4200000/);
  assert.equal(trace.redactText('{"max_tokens":50,"company":"Acme"}'), '{"max_tokens":50,"company":"Acme"}');
});

test('text from the model and tools is escaped, never run as HTML', () => {
  const html = trace.renderChatTrace([{ kind: 'tool', name: '<img src=x onerror=alert(1)>', source: 'indmoney', status: 'completed', result_preview: '{"summary":"<script>x</script>"}' }]);
  assert.doesNotMatch(html, /<img|<script/);
  const card = trace.renderInvestigationCard(completedRecord({ result: { ...completedRecord().result, nextActions: [{ label: 'x', prompt: '" onclick="alert(1)' }] } }));
  assert.doesNotMatch(card, /" onclick="/);
});

test('the saved-context step names what was shared, escaped and redacted', () => {
  const html = trace.renderChatTrace([
    { kind: 'route', status: 'completed', category: 'general_finance', source: 'keywords' },
    { kind: 'context', status: 'completed', items: ['Profile preferences', 'Goal: <b>Home</b>', 'Decision: my PAN is ABCDE1234F'] },
  ]);
  assert.match(html, /Use your saved goals and details/);
  assert.match(html, /Profile preferences · Goal: &lt;b&gt;Home&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>Home<\/b>/);
  assert.doesNotMatch(html, /ABCDE1234F/);
  assert.match(html, /2 steps/);
});
