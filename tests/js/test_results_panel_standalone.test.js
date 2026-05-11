// Standalone smoke test for the shared test-results renderer.
//
// Proves that `test_results_panel.js` is genuinely framework-agnostic: a
// consumer can load it WITHOUT `e2e_run_view.js`, render rows, and not hit
// `ReferenceError`. This pins the boundary the PR #6307 review asked for —
// E2E-specific behavior (row actions, lifecycle cluster, captured-output
// endpoint) must travel through `opts`, not through cross-file globals.
//
// The "validation modal" consumer (a future PR) will look exactly like
// this: load the shared file, provide minimal opts, render rows.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadStandalone(overrides = {}) {
    // Only the absolute-minimum globals the renderer touches at evaluation
    // time. NO _e2eRowActionButton, NO _lifecycleSessionCommand, NO
    // _renderLifecycleCommandButton, NO unifiedRunData. If the renderer
    // reaches for any of those, the test fails with ReferenceError — which
    // is the regression we're guarding against.
    const stubs = {
        console,
        window: {},
        document: { getElementById: () => null },
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    };
    const context = { ...stubs, ...overrides };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/test_results_panel.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'test_results_panel.js' });
    return context;
}

function _validationTestCase(overrides = {}) {
    return {
        nodeid: 'tests/unit/test_circuits.py::test_open_breaker',
        case_id: 'tests/unit/test_circuits.py::test_open_breaker',
        label: 'test_open_breaker',
        display_name: 'test_open_breaker',
        suite_name: 'tests/unit/test_circuits.py',
        outcome: 'failed',
        retry_outcome: null,
        duration_seconds: 0.4,
        longrepr: 'AssertionError: expected breaker open',
        failure_summary: 'AssertionError: expected breaker open',
        history: [],
        existing_issue: null,
        category: 'failed',
        result_category: 'failed',
        flip_rate: 0.0,
        flip_rate_percent: 0.0,
        is_likely_flaky: false,
        is_quarantined: false,
        result_source: 'junit_xml',
        updated_at: '',
        ...overrides,
    };
}

test('standalone: shared module loads without any e2e_run_view.js globals', () => {
    // The act of running this test without throwing already proves load-time
    // freedom from E2E coupling — the module body has no top-level reference
    // to E2E globals. Asserting a known export is a sanity belt.
    const ctx = loadStandalone();
    assert.equal(typeof ctx._renderTestRow, 'function');
    assert.equal(typeof ctx.renderTestResultsHeadline, 'function');
    assert.equal(typeof ctx.renderTestResultsFilters, 'function');
    assert.equal(typeof ctx.filterTestResults, 'function');
    assert.equal(typeof ctx.toggleTestRowExpand, 'function');
});

test('standalone: renders a failed row with NO opts (validation-modal default shape)', () => {
    // Validation modal will likely pass `{ capturedOutputUrl: () => '...inline...' }`
    // or omit opts entirely. Either way the renderer must not reach for E2E
    // helpers — and the row must still show the failure-details block.
    const ctx = loadStandalone();
    const html = ctx._renderTestRow(_validationTestCase(), null, 'all');
    assert.match(html, /trr-row/);
    assert.match(html, /test_open_breaker/);
    assert.match(html, /trr-error/);
    assert.match(html, /expected breaker open/);
    // No E2E lifecycle block — opts.renderLifecycleBlock not provided.
    assert.doesNotMatch(html, /trr-lifecycle/);
    // No E2E row-action buttons — opts.renderRowActions not provided.
    assert.doesNotMatch(html, /data-e2e-action/);
    // No captured-output placeholder — opts.capturedOutputUrl not provided.
    assert.doesNotMatch(html, /trr-captured-output/);
});

test('standalone: capturedOutputUrl opt drives the placeholder URL (no /api/e2e-run/ assumption)', () => {
    // Validation modal will plug in an issue-cycle endpoint. Verify the
    // renderer bakes that URL into data-url verbatim — no hardcoded
    // /api/e2e-run/ in the placeholder.
    const ctx = loadStandalone();
    const test_ = _validationTestCase({ outcome: 'passed', result_category: 'passed' });
    const html = ctx._renderTestRow(test_, null, 'all', {
        capturedOutputUrl: (t) => `/api/issue/408/validation/test-output?nodeid=${encodeURIComponent(t.nodeid)}`,
    });
    assert.match(html, /trr-captured-output/);
    assert.match(html, /data-url="\/api\/issue\/408\/validation\/test-output\?nodeid=tests/);
    // Crucially, no /api/e2e-run/ baked in.
    assert.doesNotMatch(html, /\/api\/e2e-run\//);
});

test('standalone: renderRowActions opt is the only path to row-action buttons', () => {
    // The validation modal might want zero actions (just an info display) —
    // and that should be the safe default. A consumer that wants actions
    // provides them as a string-returning callback. The shared module does
    // not synthesize any action buttons on its own.
    const ctx = loadStandalone();
    const test_ = _validationTestCase();

    const htmlWithout = ctx._renderTestRow(test_, null, 'all');
    assert.doesNotMatch(htmlWithout, /test-actions/);

    const htmlWith = ctx._renderTestRow(test_, null, 'all', {
        renderRowActions: () => '<button class="custom-action">Open Source</button>',
    });
    assert.match(htmlWith, /custom-action/);
});

test('standalone: renderLifecycleBlock opt is the only path to the lifecycle cluster', () => {
    // Even with `test.existing_issue` set (a shape that historically would
    // have triggered the E2E lifecycle block automatically), the shared
    // module must not invent a lifecycle block. The consumer either opts in
    // by providing renderLifecycleBlock, or gets nothing.
    const ctx = loadStandalone();
    const test_ = _validationTestCase({ existing_issue: { number: 7, status: 'open' } });
    const lifecycle = { issue_number: 7, title: 'Tracked', cycles: [{ cycle_number: 1, outcome: 'completed' }] };

    const htmlWithout = ctx._renderTestRow(test_, lifecycle, 'all');
    assert.doesNotMatch(htmlWithout, /trr-lifecycle/);

    const htmlWith = ctx._renderTestRow(test_, lifecycle, 'all', {
        renderLifecycleBlock: (t, lc) => `<div class="trr-lifecycle">tracked by #${lc.issue_number}</div>`,
    });
    assert.match(htmlWith, /trr-lifecycle.*tracked by #7/);
});

test('standalone: lazy-load reads data-url, never reconstructs /api/e2e-run/', async () => {
    // The fetcher must stay endpoint-agnostic. The placeholder's data-url
    // attribute is the contract.
    const fetchCalls = [];
    let resolveFetch;
    const ctx = loadStandalone({
        fetch: (url) => {
            fetchCalls.push(url);
            return new Promise((resolve) => { resolveFetch = resolve; });
        },
    });

    // Hand-build a minimal placeholder/expand pair using a private stub.
    function _stub() {
        return {
            attrs: {}, dataset: {}, children: [], style: {}, hidden: false,
            innerHTML: '', outerHTML: '', textContent: '',
            setAttribute(k, v) { this.attrs[k] = v; },
            removeAttribute(k) { delete this.attrs[k]; },
            hasAttribute(k) { return k in this.attrs; },
            classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
            querySelector: () => null,
            querySelectorAll: () => [],
        };
    }
    const status = _stub();
    const placeholder = _stub();
    placeholder.dataset = {
        needsFetch: '1',
        url: '/api/issue/408/validation/test-output?nodeid=pkg%3A%3Atest_a',
        nodeid: 'pkg::test_a',
    };
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stub();
    expand.querySelector = (sel) =>
        sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx._maybeLoadCapturedOutput(expand);

    assert.deepEqual(fetchCalls, ['/api/issue/408/validation/test-output?nodeid=pkg%3A%3Atest_a']);
    assert.equal(placeholder.dataset.needsFetch, '0');
    resolveFetch({ ok: true, status: 200, json: async () => ({ system_out: '' }) });
});

test('standalone: missing data-url surfaces a clear error (no silent hang)', () => {
    // If a consumer wires capturedOutputUrl wrong (returns ''), the renderer
    // already omits the placeholder. But if a placeholder somehow exists
    // without data-url (e.g. legacy markup), the fetcher must fail loudly.
    const ctx = loadStandalone();
    function _stub() {
        return {
            attrs: {}, dataset: {}, children: [], style: {}, hidden: false,
            innerHTML: '', outerHTML: '', textContent: '',
            setAttribute(k, v) { this.attrs[k] = v; },
            removeAttribute(k) { delete this.attrs[k]; },
            hasAttribute(k) { return k in this.attrs; },
            classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
            querySelector: () => null,
            querySelectorAll: () => [],
        };
    }
    const status = _stub();
    const placeholder = _stub();
    placeholder.dataset = { needsFetch: '1', nodeid: 'pkg::test' }; // no url
    placeholder.querySelector = (sel) => sel === '.trr-captured-status' ? status : null;
    const expand = _stub();
    expand.querySelector = (sel) =>
        sel === '.trr-captured-output[data-needs-fetch="1"]' ? placeholder : null;

    ctx._maybeLoadCapturedOutput(expand);
    assert.match(status.outerHTML, /Cannot load captured output: missing url/);
});
