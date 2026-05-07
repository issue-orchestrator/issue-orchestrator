// Behavioral tests for openValidationFailure() — the dashboard JS path that
// renders both passed and failed validation runs in a single modal.
//
// Why this exists: PR #6274 added a `status: "passed"` rendering branch in
// session_dialogs.js but the original PR shipped only backend/contract tests
// (Pydantic payload, route, dialog-builder). A regression in the actual
// browser HTML rendering would have slipped past CI. These VM tests stub the
// dialog endpoint and assert the rendered modal title + section structure
// for both outcomes, so any future change to the passed-vs-failed branch is
// caught at the layer where the bug actually shows up — the DOM.
//
// Mirrors the stdlib-only vm pattern from `tests/js/e2e_run_view_actions.test.js`.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadSessionDialogs(payload) {
    // Capture every openModal(title, html) call so tests can inspect the
    // rendered HTML directly. We don't run a DOM — innerHTML is just a string.
    const modals = [];
    const toasts = [];

    const context = {
        console,
        URLSearchParams,
        // Module-level state declared without `var/let/const` in the source
        // gets implicitly attached to the vm context; pre-seed so the load
        // step doesn't throw on `currentDiagnosticsRunDir = ...`.
        currentDiagnosticsRunDir: null,
        // Browser-global stubs: minimal surface — only what session_dialogs.js
        // actually touches in the openValidationFailure() path.
        window: {
            dashboardData: { startupComplete: true },
        },
        document: {
            getElementById: () => ({ innerHTML: '' }),
            addEventListener: () => {},
        },
        fetch: async (url) => {
            // The endpoint path is exercised by the Python integration test
            // (test_validation_failure_dialog_endpoint.py); here we stub it so
            // the JS branch under test is the dialog renderer, not the network.
            modals.push(['fetch', String(url)]);
            return {
                ok: true,
                status: 200,
                json: async () => payload,
            };
        },
        // External JS helpers from other files in the runtime bundle.
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        openModal: (title, html) => modals.push(['openModal', String(title), String(html)]),
        showToast: (message, severity) => toasts.push([String(message), severity]),
        // Other functions in session_dialogs.js that openValidationFailure
        // happens to call indirectly. Any of these can stay as the real impl
        // because they're defined in the same source file we load below.
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/session_dialogs.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'session_dialogs.js' });
    return { context, modals, toasts };
}

const _PASSED_PAYLOAD = {
    title: 'Validation Passed #4244',
    status: 'passed',
    reason: 'Validation passed',
    suite: 'publish_gate',
    command: 'make validate',
    exit_code: 0,
    started_at: '2026-05-07T12:00:00Z',
    ended_at: '2026-05-07T12:04:30Z',
    failed_tests: [],
    stdout_excerpt: ['============= 142 passed in 41.21s ============='],
    stderr_excerpt: [],
    junit_cases: [],
    summary_rows: [
        { label: 'Outcome', value: 'Passed' },
        { label: 'Reason', value: 'Validation passed' },
        { label: 'Command', value: 'make validate' },
        { label: 'Exit Code', value: '0' },
        { label: 'Failing Tests', value: '0' },
    ],
    action_sections: [],
};

const _FAILED_PAYLOAD = {
    title: 'Validation Failure #4242',
    status: 'failed',
    reason: '2 unit tests failed',
    suite: 'publish_gate',
    command: 'make validate',
    exit_code: 2,
    started_at: '2026-05-05T00:00:00Z',
    ended_at: '2026-05-05T00:00:30Z',
    failed_tests: ['tests/unit/test_one.py::test_a', 'tests/unit/test_two.py::test_b'],
    stdout_excerpt: ['FAILED tests/unit/test_one.py::test_a'],
    stderr_excerpt: ['make: *** [validate] Error 2'],
    junit_cases: [],
    summary_rows: [
        { label: 'Outcome', value: 'Failed' },
        { label: 'Reason', value: '2 unit tests failed' },
        { label: 'Failing Tests', value: '2' },
    ],
    action_sections: [],
};

async function _renderModal(payload) {
    const { context, modals } = loadSessionDialogs(payload);
    await context.openValidationFailure(payload.title.match(/#(\d+)/)[1], '/run/r1', 'modal');
    const modalCall = modals.find(([kind]) => kind === 'openModal');
    assert.ok(modalCall, 'openModal must be invoked exactly once');
    return { title: modalCall[1], html: modalCall[2] };
}

test('passed run: title flips to "Validation Passed #N"', async () => {
    const { title } = await _renderModal(_PASSED_PAYLOAD);
    assert.strictEqual(title, 'Validation Passed #4244');
});

test('passed run: renders Tests section, not Failed Tests', async () => {
    const { html } = await _renderModal(_PASSED_PAYLOAD);
    // Section heading flips so the modal doesn't read like a failure.
    assert.match(html, /<div class="diag-section-title">Tests<\/div>/);
    assert.doesNotMatch(html, /<div class="diag-section-title">Failed Tests/);
    // And shows the all-passed empty-state copy rather than the failure list.
    assert.match(html, /All tests passed\./);
});

test('passed run: outcome chip is is-ok green, not is-warn red', async () => {
    const { html } = await _renderModal(_PASSED_PAYLOAD);
    // Operators eyeballing the chip row need an at-a-glance signal.
    assert.match(html, /diag-chip is-ok">passed<\/span>/);
    assert.doesNotMatch(html, /diag-chip is-warn">failed/);
});

test('passed run: stdout excerpt section still renders for spot-checking the green run', async () => {
    const { html } = await _renderModal(_PASSED_PAYLOAD);
    // The whole point of #6274: passing runs should let users SEE the test
    // tail ("142 passed in 41.21s"). Regression here defeats the feature.
    assert.match(html, /Validation Output Excerpt/);
    assert.match(html, /142 passed in 41\.21s/);
});

test('failed run: title stays "Validation Failure #N"', async () => {
    const { title } = await _renderModal(_FAILED_PAYLOAD);
    assert.strictEqual(title, 'Validation Failure #4242');
});

test('failed run: renders Failed Tests section with each failing nodeid', async () => {
    const { html } = await _renderModal(_FAILED_PAYLOAD);
    assert.match(html, /<div class="diag-section-title">Failed Tests \(2\)<\/div>/);
    assert.match(html, /tests\/unit\/test_one\.py::test_a/);
    assert.match(html, /tests\/unit\/test_two\.py::test_b/);
});

test('failed run: outcome chip is is-warn red', async () => {
    const { html } = await _renderModal(_FAILED_PAYLOAD);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
});

test('payload missing status field falls back to failed rendering (back-compat)', async () => {
    // Defensive: an older server (or a regression in the contract layer)
    // could omit `status`. The renderer must default to the failure UX so
    // operators don't see a false-green dialog.
    const legacyPayload = { ..._FAILED_PAYLOAD };
    delete legacyPayload.status;
    const { html } = await _renderModal(legacyPayload);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
    assert.match(html, /Failed Tests/);
});
