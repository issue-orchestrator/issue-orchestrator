// Behavioral tests for the E2E run-view JS module.
//
// These run in node:test with the source loaded into a `vm` context. The
// stubs below stand in for browser globals (DOM nodes, window-scoped helpers
// that live in other source files in the runtime bundle). Coverage goal: one
// test per clickable surface plus per-shape rendering, so handler regressions
// surface here in <100ms instead of in Playwright (~7s/test) or — worst case —
// in production.
const test = require('node:test');
// Non-strict assert: vm.runInContext creates objects with prototypes from a
// different realm, so deepStrictEqual rejects them even when shape-equal.
// Loose deepEqual still catches all the mismatches we care about (extra/missing
// keys, wrong values) while tolerating cross-realm prototypes.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadE2ERunView(overrides = {}) {
    const calls = [];
    const toasts = [];
    const opens = [];

    // PRE-LOAD stubs: free variables that e2e_run_view.js calls but doesn't
    // define (they live in other files in the runtime bundle).
    const preLoadStubs = {
        console,
        calls,
        toasts,
        opens,
        REPO_ROOT: '/tmp/repo',
        CONFIG_NAME: 'default.yaml',
        window: {
            dashboardData: {
                agents: ['default-agent'],
                githubOwner: 'owner',
                githubRepo: 'repo',
            },
            openPath: (p) => opens.push(['window.openPath', String(p)]),
            open: (url) => opens.push(['window.open', String(url)]),
        },
        document: { getElementById: () => null },
        navigator: {},
        confirm: () => true,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        quarantineSingleTest: async (nodeid) => calls.push(['quarantine_test', nodeid]),
        openIssueTimeline: (issueNumber, _ref, opts) => calls.push(['open_issue_timeline', issueNumber, opts || {}]),
        openAgentLogAction: (issueNumber, runDir, label, target, opts) =>
            calls.push(['open_session_recording', issueNumber, runDir, label, target, opts || {}]),
        openReviewTranscript: (issueNumber, runDir, opts, target) =>
            calls.push(['open_review_transcript', issueNumber, runDir, opts || {}, target]),
        openValidationFailure: (issueNumber, runDir, target) =>
            calls.push(['open_validation_details', issueNumber, runDir, target]),
        openPath: (p) => calls.push(['open_completion_record', String(p)]),
        showToast: (message, severity) => toasts.push([String(message), severity]),
    };
    const context = { ...preLoadStubs, ...overrides };
    vm.createContext(context);
    const sharedSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/test_results_panel.js'),
        'utf8',
    );
    vm.runInContext(sharedSource, context, { filename: 'test_results_panel.js' });
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_run_view.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'e2e_run_view.js' });

    // POST-LOAD stubs: e2e_run_view.js DEFINES these as top-level functions,
    // so script execution overwrites anything we put in the context. Re-apply
    // after load so dispatch tests can observe their callsites.
    const postLoadStubs = {
        copyTestErrorFromRun: (nodeid) => calls.push(['copy_test_error', nodeid]),
        closeE2EIssue: (issueNumber, nodeid) => calls.push(['close_issue', issueNumber, nodeid]),
        showCreateIssueDropdown: (_button, nodeid) => calls.push(['create_issue_dropdown', nodeid]),
        createSingleIssueWithAgent: (nodeid, agent) => calls.push(['create_issue_with_agent', nodeid, agent]),
        showUnifiedRunView: () => {},
    };
    Object.assign(context, postLoadStubs, overrides);
    return context;
}

// ── DOM stubbing helpers ──────────────────────────────────────────────────
//
// We intentionally hand-roll minimal nodes instead of pulling in jsdom — the
// project's JS testing surface is stdlib-only on purpose (no node_modules).
// Each helper exposes only the surface the function-under-test touches.

function _stubElement(overrides = {}) {
    const el = {
        children: [],
        attrs: {},
        dataset: {},
        textContent: '',
        innerHTML: '',
        outerHTML: '',
        hidden: false,
        style: {},
        appendChild(child) { this.children.push(child); return child; },
        setAttribute(name, value) {
            this.attrs[name] = value;
            if (name === 'hidden') this.hidden = true;
        },
        removeAttribute(name) {
            delete this.attrs[name];
            if (name === 'hidden') this.hidden = false;
        },
        hasAttribute(name) {
            return name === 'hidden' ? this.hidden
                : Object.prototype.hasOwnProperty.call(this.attrs, name);
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        closest() { return null; },
        classList: {
            _set: new Set(),
            add(c) { this._set.add(c); },
            remove(c) { this._set.delete(c); },
            toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
            contains(c) { return this._set.has(c); },
        },
        ...overrides,
    };
    return el;
}

// Build the row + main + caret + expand triple that toggleTestRowExpand walks.
function _makeRowFor(toggle) {
    const caret = _stubElement();
    const expand = _stubElement();
    expand.hidden = !toggle.startOpen;  // start closed for collapsed rows
    const main = _stubElement();
    main.querySelector = (sel) => sel === '.trr-caret' ? caret : null;
    const row = _stubElement();
    row.dataset = { expandable: '1' };
    row.querySelector = (sel) => sel === '.trr-expand' ? expand : null;
    main.closest = (sel) => sel === '.trr-row' ? row : null;
    return { row, main, expand, caret };
}

// ── Click dispatch: row actions ──────────────────────────────────────────

test('row action: copy_test_error dispatches with nodeid', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ERowActionFromButton({
        dataset: { e2eAction: 'copy_test_error', nodeid: 'tests/e2e/test_smoke.py::test_checkout' },
    });
    assert.deepEqual(ctx.calls, [['copy_test_error', 'tests/e2e/test_smoke.py::test_checkout']]);
});

test('row action: copy_test_error fails fast when nodeid is missing', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.runE2ERowActionFromButton({ dataset: { e2eAction: 'copy_test_error' } }),
        /Copy-error action missing nodeid/,
    );
});

test('row action: create_issue_dropdown dispatches with nodeid', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ERowActionFromButton({
        dataset: { e2eAction: 'create_issue_dropdown', nodeid: 'pkg::test_x' },
    });
    assert.deepEqual(ctx.calls, [['create_issue_dropdown', 'pkg::test_x']]);
});

test('row action: create_issue_dropdown fails fast when nodeid is missing', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.runE2ERowActionFromButton({ dataset: { e2eAction: 'create_issue_dropdown' } }),
        /Create-issue action missing nodeid/,
    );
});

test('row action: quarantine_test dispatches with nodeid', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ERowActionFromButton({
        dataset: { e2eAction: 'quarantine_test', nodeid: 'pkg::test_y' },
    });
    assert.deepEqual(ctx.calls, [['quarantine_test', 'pkg::test_y']]);
});

test('row action: close_issue dispatches with parsed integer issue number', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ERowActionFromButton({
        dataset: { e2eAction: 'close_issue', issueNumber: '4321', nodeid: 'pkg::test_z' },
    });
    assert.deepEqual(ctx.calls, [['close_issue', 4321, 'pkg::test_z']]);
});

test('row action: close_issue fails fast when issue number is non-integer', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.runE2ERowActionFromButton({
            dataset: { e2eAction: 'close_issue', issueNumber: 'NaN', nodeid: 'pkg::test_z' },
        }),
        /Close-issue action missing issue number or nodeid/,
    );
});

test('row action: create_issue_with_agent dispatches with both ids', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ERowActionFromButton({
        dataset: { e2eAction: 'create_issue_with_agent', nodeid: 'pkg::test_a', agent: 'codex' },
    });
    assert.deepEqual(ctx.calls, [['create_issue_with_agent', 'pkg::test_a', 'codex']]);
});

test('row action: create_issue_with_agent fails fast when agent is missing', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.runE2ERowActionFromButton({
            dataset: { e2eAction: 'create_issue_with_agent', nodeid: 'pkg::test_a' },
        }),
        /Create-issue-with-agent action missing nodeid or agent/,
    );
});

test('row action: unknown action surfaces a typed error', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.runE2ERowActionFromButton({ dataset: { e2eAction: 'mystery', nodeid: 'pkg::x' } }),
        /Unknown E2E row action: mystery/,
    );
});

// ── Click dispatch: artifact buttons ─────────────────────────────────────

test('artifact button: opens the recorded path through window.openPath', () => {
    const ctx = loadE2ERunView();
    ctx.openE2EArtifactFromButton({ dataset: { artifactPath: '/tmp/run/junit.xml' } });
    assert.deepEqual(ctx.opens, [['window.openPath', '/tmp/run/junit.xml']]);
});

test('artifact button: fails fast when data-artifact-path is missing', () => {
    const ctx = loadE2ERunView();
    assert.throws(
        () => ctx.openE2EArtifactFromButton({ dataset: {} }),
        /Artifact button missing data-artifact-path/,
    );
});

// ── Click dispatch: lifecycle commands ───────────────────────────────────

test('lifecycle: open_issue_timeline routes through openIssueTimeline with e2e scope', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({
        kind: 'open_issue_timeline',
        issue_number: 5723,
        scope_kind: 'e2e_run',
        e2e_run_id: 99,
    });
    assert.deepEqual(ctx.calls, [['open_issue_timeline', 5723, { e2eRunId: 99 }]]);
});

test('lifecycle: open_session_recording forwards round + role context', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({
        kind: 'open_session_recording',
        issue_number: 7,
        run_dir: '/runs/issue-7-cycle-1',
        round_index: 1,
        session_role: 'coder',
        label: 'Coder Session',
    });
    assert.deepEqual(ctx.calls, [
        ['open_session_recording', 7, '/runs/issue-7-cycle-1', 'Coder Session', 'toast', { round_index: 1, session_role: 'coder' }],
    ]);
});

test('lifecycle: open_review_transcript forwards transcript role', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({
        kind: 'open_review_transcript',
        issue_number: 8,
        run_dir: '/runs/issue-8',
        round_index: 2,
        transcript_role: 'reviewer',
    });
    assert.deepEqual(ctx.calls, [
        ['open_review_transcript', 8, '/runs/issue-8', { round_index: 2, transcript_role: 'reviewer' }, 'toast'],
    ]);
});

test('lifecycle: open_validation_details routes through openValidationFailure', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({
        kind: 'open_validation_details',
        issue_number: 9,
        run_dir: '/runs/issue-9',
    });
    assert.deepEqual(ctx.calls, [['open_validation_details', 9, '/runs/issue-9', 'toast']]);
});

test('lifecycle: open_completion_record routes through openPath', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({ kind: 'open_completion_record', path: '/runs/issue-9/completion.json' });
    assert.deepEqual(ctx.calls, [['open_completion_record', '/runs/issue-9/completion.json']]);
});

test('lifecycle: unsupported kind warns through showToast (no thrown error)', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommand({ kind: 'open_unicorn', issue_number: 1 });
    assert.deepEqual(ctx.calls, []);
    assert.deepEqual(ctx.toasts, [['Unsupported lifecycle command: open_unicorn', 'warning']]);
});

test('lifecycle button: malformed JSON surfaces an error toast', () => {
    const ctx = loadE2ERunView();
    ctx.runE2ELifecycleCommandFromButton({ dataset: { lifecycleCommand: '{not-valid-json' } });
    assert.equal(ctx.toasts.length, 1);
    assert.match(ctx.toasts[0][0], /Failed to decode lifecycle command/);
    assert.equal(ctx.toasts[0][1], 'error');
});

// ── Click dispatch: filter chips ─────────────────────────────────────────

test('filter chip: hides rows whose filter group does not match, activates clicked chip', () => {
    const ctx = loadE2ERunView();
    const failedRow = _stubElement();
    failedRow.dataset = { filterGroup: 'failed' };
    const passedRow = _stubElement();
    passedRow.dataset = { filterGroup: 'passed' };
    const list = _stubElement();
    list.querySelectorAll = (sel) => sel === '.trr-row' ? [failedRow, passedRow] : [];
    const failedChip = _stubElement();
    const passedChip = _stubElement();
    const panel = _stubElement();
    panel.querySelector = (sel) => sel === '.test-results-list' ? list : null;
    panel.querySelectorAll = (sel) => sel === '.trf-chip' ? [failedChip, passedChip] : [];
    failedChip.closest = (sel) => sel === '.test-results-panel' ? panel : null;

    ctx.filterTestResults('failed', failedChip);

    assert.equal(failedRow.style.display, '', 'failed row stays visible under failed filter');
    assert.equal(passedRow.style.display, 'none', 'passed row is hidden under failed filter');
    assert.equal(failedChip.classList.contains('active'), true, 'clicked chip becomes active');
    assert.equal(passedChip.classList.contains('active'), false, 'other chip loses active');
    assert.equal(failedChip.attrs['aria-selected'], 'true');
    assert.equal(passedChip.attrs['aria-selected'], 'false');
});

test('filter chip: short-circuits when click happens outside a panel', () => {
    const ctx = loadE2ERunView();
    const orphanChip = _stubElement();  // closest('.test-results-panel') returns null
    // Should not throw — silent no-op.
    ctx.filterTestResults('failed', orphanChip);
});

function _makeFailedRowWithPlaceholder({ runId, nodeid, hidden }) {
    const status = _stubElement();
    const placeholder = _stubElement();
    placeholder.dataset = { needsFetch: '1', runId: String(runId), nodeid };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stubElement();
    // Failed rows render expanded by default (no hidden attribute).
    expand.querySelector = (sel) => sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;
    const row = _stubElement();
    row.dataset = { filterGroup: 'failed', expandable: '1' };
    if (hidden) row.style.display = 'none';
    row.querySelector = (sel) => sel === '.trr-expand' ? expand : null;
    expand.closest = (sel) => sel === '.trr-row' ? row : null;
    return { row, expand, placeholder };
}

test('auto-load: skips placeholders inside rows hidden by the initial filter', () => {
    const fetchCalls = [];
    const ctx = loadE2ERunView({
        fetch: (url) => { fetchCalls.push(url); return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); },
    });
    const visible = _makeFailedRowWithPlaceholder({ runId: 1, nodeid: 'pkg::shown', hidden: false });
    const hiddenByFilter = _makeFailedRowWithPlaceholder({ runId: 1, nodeid: 'pkg::offscreen', hidden: true });
    const root = _stubElement();
    root.querySelectorAll = (sel) => sel === '.trr-expand:not([hidden])' ? [visible.expand, hiddenByFilter.expand] : [];

    ctx._autoLoadVisibleCapturedOutput(root);

    // Only the visible row's placeholder triggers a fetch — the filter-hidden
    // one waits until the user reveals it.
    assert.deepEqual(fetchCalls, ['/api/e2e-run/1/test-output?nodeid=pkg%3A%3Ashown']);
    assert.equal(visible.placeholder.dataset.needsFetch, '0');
    assert.equal(hiddenByFilter.placeholder.dataset.needsFetch, '1');
});

test('filter chip click: revives auto-fetch for newly visible rows', () => {
    const fetchCalls = [];
    const ctx = loadE2ERunView({
        fetch: (url) => { fetchCalls.push(url); return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); },
    });
    // Two failed rows: one already-fetched (mimics the initially-visible row),
    // one previously hidden (filter-hidden ⇒ skipped at initial render).
    const alreadyFetched = _makeFailedRowWithPlaceholder({ runId: 5, nodeid: 'pkg::a', hidden: false });
    alreadyFetched.placeholder.dataset.needsFetch = '0';
    alreadyFetched.expand.querySelector = () => null;  // no placeholder needs fetch
    const newlyRevealed = _makeFailedRowWithPlaceholder({ runId: 5, nodeid: 'pkg::b', hidden: true });

    const list = _stubElement();
    list.querySelectorAll = (sel) => sel === '.trr-row' ? [alreadyFetched.row, newlyRevealed.row] : [];
    const chip = _stubElement();
    const panel = _stubElement();
    panel.querySelector = (sel) => sel === '.test-results-list' ? list : null;
    panel.querySelectorAll = (sel) => sel === '.trf-chip' ? [chip] : sel === '.trr-expand:not([hidden])' ? [alreadyFetched.expand, newlyRevealed.expand] : [];
    chip.closest = (sel) => sel === '.test-results-panel' ? panel : null;

    ctx.filterTestResults('all', chip);

    // After 'all' filter: both rows are visible; only the previously-hidden
    // row's placeholder still needs a fetch.
    assert.deepEqual(fetchCalls, ['/api/e2e-run/5/test-output?nodeid=pkg%3A%3Ab']);
});

// ── Click dispatch: row toggle (expand / collapse) ───────────────────────

test('toggle: collapses an open expand and updates caret + aria-expanded', () => {
    const ctx = loadE2ERunView();
    const { main, expand, caret } = _makeRowFor({ startOpen: true });

    ctx.toggleTestRowExpand(main);

    assert.equal(expand.hidden, true);
    assert.equal(main.attrs['aria-expanded'], 'false');
    assert.equal(caret.textContent, '▸');
});

test('toggle: expands a closed expand, updates caret, and triggers lazy fetch', () => {
    const fetchCalls = [];
    const ctx = loadE2ERunView({
        fetch: (url) => { fetchCalls.push(url); return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); },
    });
    const { main, expand, caret } = _makeRowFor({ startOpen: false });
    // Wire a placeholder so we can confirm the toggle reaches into lazy-load.
    const placeholder = _stubElement();
    placeholder.dataset = { needsFetch: '1', runId: '11', nodeid: 'pkg::test_a' };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? _stubElement() : null;
    expand.querySelector = (sel) => sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx.toggleTestRowExpand(main);

    assert.equal(expand.hidden, false);
    assert.equal(main.attrs['aria-expanded'], 'true');
    assert.equal(caret.textContent, '▾');
    assert.deepEqual(fetchCalls, ['/api/e2e-run/11/test-output?nodeid=pkg%3A%3Atest_a']);
});

test('toggle: ignores non-expandable rows', () => {
    const ctx = loadE2ERunView();
    const main = _stubElement();
    const row = _stubElement();
    row.dataset = { expandable: '0' };  // not expandable
    main.closest = () => row;
    // Should be a silent no-op; absence of an expand element shouldn't throw.
    ctx.toggleTestRowExpand(main);
});

// ── Click dispatch: lazy-load captured output ────────────────────────────

test('lazy-load: GETs /api/e2e-run/{id}/test-output with the placeholder nodeid', async () => {
    const fetchCalls = [];
    let resolveFetch;
    const ctx = loadE2ERunView({
        fetch: (url) => {
            fetchCalls.push(url);
            return new Promise((resolve) => { resolveFetch = resolve; });
        },
    });
    const status = _stubElement();
    const placeholder = _stubElement();
    placeholder.dataset = { needsFetch: '1', runId: '42', nodeid: 'tests/e2e/test_smoke.py::test_x' };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stubElement();
    expand.querySelector = (sel) => sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx._maybeLoadCapturedOutput(expand);

    assert.equal(placeholder.dataset.needsFetch, '0');
    assert.deepEqual(fetchCalls, ['/api/e2e-run/42/test-output?nodeid=tests%2Fe2e%2Ftest_smoke.py%3A%3Atest_x']);

    resolveFetch({ ok: true, status: 200, json: async () => ({ system_out: 'hello world', system_err: null }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.match(status.outerHTML, /stdout/);
    assert.match(status.outerHTML, /hello world/);
});

test('lazy-load: short-circuits when there is no placeholder needing fetch', () => {
    const fetchCalls = [];
    const ctx = loadE2ERunView({
        fetch: (url) => { fetchCalls.push(url); return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); },
    });
    const expand = _stubElement();
    ctx._maybeLoadCapturedOutput(expand);
    assert.deepEqual(fetchCalls, []);
});

test('lazy-load: 404 renders an empty note rather than an error', async () => {
    let resolveFetch;
    const ctx = loadE2ERunView({ fetch: () => new Promise((resolve) => { resolveFetch = resolve; }) });
    const status = _stubElement();
    const placeholder = _stubElement();
    placeholder.dataset = { needsFetch: '1', runId: '7', nodeid: 'pkg::test_quiet' };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stubElement();
    expand.querySelector = (sel) => sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx._maybeLoadCapturedOutput(expand);
    resolveFetch({ ok: false, status: 404, json: async () => ({}) });
    await new Promise(resolve => setImmediate(resolve));

    assert.match(status.outerHTML, /No captured output recorded/);
    assert.doesNotMatch(status.outerHTML, /Failed to load/);
});

test('lazy-load: network error renders a load-failed note', async () => {
    const ctx = loadE2ERunView({ fetch: () => Promise.reject(new Error('econnrefused')) });
    const status = _stubElement();
    const placeholder = _stubElement();
    placeholder.dataset = { needsFetch: '1', runId: '7', nodeid: 'pkg::test_loud' };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stubElement();
    expand.querySelector = (sel) => sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx._maybeLoadCapturedOutput(expand);
    await new Promise(resolve => setImmediate(resolve));

    assert.match(status.outerHTML, /Failed to load captured output/);
    assert.match(status.outerHTML, /econnrefused/);
});

// ── Render shape: rows ───────────────────────────────────────────────────

function _testFixture(overrides = {}) {
    return {
        nodeid: 'pkg::test_a',
        label: 'test_a',
        display_name: 'test_a',
        suite_name: 'pkg',
        outcome: 'passed',
        result_source: 'junit_xml',
        duration_seconds: 1.23,
        history: [],
        flip_rate_percent: 0,
        is_likely_flaky: false,
        category: 'passed',
        result_category: 'passed',
        ...overrides,
    };
}

test('render: failed row is marked expandable, defaults open, has a failure-details block', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(
        _testFixture({ outcome: 'failed', longrepr: 'AssertionError: expected 1 got 2', category: 'untriaged', result_category: 'untriaged' }),
        null,
        'all',
        { runId: 99 },
    );
    assert.match(html, /data-expandable="1"/);
    assert.match(html, /class="trr-caret" aria-hidden="true">▾</);  // open
    assert.match(html, /aria-expanded="true"/);
    assert.match(html, /<div class="trr-expand" >/);  // no hidden attribute
    assert.match(html, /Failure details/);
    assert.match(html, /AssertionError: expected 1 got 2/);
    assert.match(html, /trr-captured-output/);  // captured-output placeholder present
});

test('render: passed JUnit row is expandable and renders a captured-output placeholder', () => {
    // A passing test can still emit useful debug output (setup logs, prints).
    // The UI exposes that via the same lazy-fetch placeholder used for failed
    // rows, so users have a path to drill in regardless of outcome.
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(_testFixture(), null, 'all', { runId: 99 });
    assert.match(html, /data-expandable="1"/);
    assert.match(html, /class="trr-caret" aria-hidden="true">▸</);  // collapsed by default
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /<div class="trr-expand" hidden>/);
    assert.match(html, /trr-captured-output/);
    assert.match(html, /data-needs-fetch="1"/);
});

test('render: passed row without a runId is not expandable (nothing to fetch)', () => {
    // Without runId we can't construct the test-output URL, so the placeholder
    // and caret are suppressed — clicking would dead-end.
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(_testFixture(), null, 'all', {});
    assert.match(html, /data-expandable="0"/);
    assert.doesNotMatch(html, /trr-captured-output/);
});

test('render: passed row with non-junit source is not expandable', () => {
    // Captured output is parsed from JUnit XML on disk. A row sourced from
    // anything else has nothing for the endpoint to look up.
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(
        _testFixture({ result_source: 'external_report' }),
        null,
        'all',
        { runId: 99 },
    );
    assert.match(html, /data-expandable="0"/);
    assert.doesNotMatch(html, /trr-captured-output/);
});

test('render: passed row with history shows the recent-runs cluster alongside the captured-output placeholder', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(
        _testFixture({
            history: [
                { outcome: 'passed' }, { outcome: 'failed' }, { outcome: 'passed' },
            ],
            flip_rate_percent: 50,
        }),
        null,
        'all',
        { runId: 99 },
    );
    assert.match(html, /class="test-history-label">Recent</);
    assert.match(html, /50% flake/);
    assert.match(html, /trr-captured-output/);
});

test('render: result_source=junit_xml suppresses the per-row provenance tag', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(_testFixture({ result_source: 'junit_xml' }), null, 'all', { runId: 99 });
    assert.doesNotMatch(html, /class="test-source"/);
});

test('render: unusual result_source (external_report) renders the per-row provenance tag', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRow(_testFixture({ result_source: 'external_report' }), null, 'all', { runId: 99 });
    assert.match(html, /class="test-source">External Report/);
});

test('render: history cluster glyphs map outcomes pass→✓, fail→✗, skip→○', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderHistoryCluster(_testFixture({
        history: [{ outcome: 'passed' }, { outcome: 'failed' }, { outcome: 'skipped' }],
        flip_rate_percent: 67,
    }));
    // History is rendered newest-first via reverse(); fixture above is oldest-first
    // so the rendered order is: skip, fail, pass.
    assert.match(html, /<span class="hist-icon skip">○<\/span><span class="hist-icon fail">✗<\/span><span class="hist-icon pass">✓<\/span>/);
    assert.match(html, /· 67% flake/);
});

test('render: history cluster omits the flake annotation at 0%', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderHistoryCluster(_testFixture({
        history: [{ outcome: 'passed' }, { outcome: 'passed' }],
        flip_rate_percent: 0,
    }));
    assert.match(html, /class="test-history-label">Recent</);
    assert.doesNotMatch(html, /flake/);
});

test('render: empty history renders nothing (history cluster is opt-in)', () => {
    const ctx = loadE2ERunView();
    assert.equal(ctx._renderHistoryCluster(_testFixture({ history: [] })), '');
});

// ── Render shape: pills ──────────────────────────────────────────────────

test('render headline: exposes structural count attributes', () => {
    const ctx = loadE2ERunView();
    const html = ctx.renderTestResultsHeadline([
        _testFixture({ outcome: 'passed' }),
        _testFixture({ outcome: 'failed', category: 'untriaged', result_category: 'untriaged' }),
    ]);

    assert.match(html, /data-total-count="2"/);
    assert.match(html, /data-passed-count="1"/);
    assert.match(html, /data-failed-count="1"/);
    assert.match(html, /data-action-needed-count="1"/);
});

test('render pills: failed + untriaged shows Failed primary + Action needed (no Flaky pill)', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestResultPills(_testFixture({
        outcome: 'failed', category: 'untriaged', result_category: 'untriaged',
    }));
    assert.match(html, /test-result-pill primary failed">Failed/);
    assert.match(html, /test-result-pill action-needed">Action needed/);
    // The Flaky peer pill was removed — flakiness lives in the history cluster now.
    assert.doesNotMatch(html, /test-result-pill flaky/);
});

test('render pills: historically flaky WITH history shows no flaky-note (cluster covers it)', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestResultPills(_testFixture({
        is_likely_flaky: true,
        history: [{ outcome: 'passed' }, { outcome: 'failed' }],
    }));
    assert.doesNotMatch(html, /test-result-flaky-note/);
});

test('render pills: historically flaky WITHOUT history shows the flaky-note fallback', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestResultPills(_testFixture({
        is_likely_flaky: true,
        history: [],
    }));
    assert.match(html, /class="test-result-flaky-note"/);
});

test('render pills: tracked failure shows Failed + Tracked', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestResultPills(_testFixture({
        outcome: 'failed',
        category: 'has_issue',
        result_category: 'has_issue',
        existing_issue: { number: 1, status: 'open', resolution: null },
    }));
    assert.match(html, /test-result-pill primary failed">Failed/);
    assert.match(html, /test-result-pill tracked">Tracked/);
});

// ── Render shape: expand block gating ────────────────────────────────────

test('render expand: failed row WITHOUT runId omits the captured-output placeholder', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRowExpand(
        _testFixture({ outcome: 'failed', category: 'untriaged', result_category: 'untriaged', longrepr: 'boom' }),
        null,
        {},  // no runId
    );
    assert.match(html, /Failure details/);
    assert.doesNotMatch(html, /trr-captured-output/);
});

test('render expand: passed row with junit source AND runId returns a captured-output placeholder', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRowExpand(
        _testFixture({ outcome: 'passed', result_source: 'junit_xml' }),
        null,
        { runId: 99 },
    );
    assert.match(html, /trr-captured-output/);
    assert.match(html, /data-needs-fetch="1"/);
    assert.match(html, /Captured output/);
});

test('render expand: passed row without runId returns empty (no fetch target)', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRowExpand(
        _testFixture({ outcome: 'passed', result_source: 'junit_xml' }),
        null,
        {},
    );
    assert.equal(html, '');
});

test('render expand: failed row with non-junit source omits captured-output (nothing to fetch)', () => {
    const ctx = loadE2ERunView();
    const html = ctx._renderTestRowExpand(
        _testFixture({
            outcome: 'failed', category: 'untriaged', result_category: 'untriaged',
            result_source: 'external_report', longrepr: 'boom',
        }),
        null,
        { runId: 99 },
    );
    assert.match(html, /Failure details/);
    assert.doesNotMatch(html, /trr-captured-output/);
});
