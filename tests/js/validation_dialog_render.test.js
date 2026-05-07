// Pure data → DOM mapping tests for renderValidationDialog().
//
// The "command pattern" in two layers:
//   1. Backend command produces a payload — covered by Python integration
//      tests (test_validation_failure_dialog_endpoint.py) that hit the real
//      FastAPI route and assert the payload shape.
//   2. Payload → DOM mapping — covered HERE: hand-roll a payload, call
//      renderValidationDialog() directly, assert the rendered HTML.
//
// No fetch stub, no openModal capture, no DOM emulation — just a pure
// function call. Any future change to the passed-vs-failed render branch
// is caught at the layer where the bug actually shows up: the markup.
//
// Mirrors the stdlib-only vm pattern from `tests/js/e2e_run_view_actions.test.js`.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadSessionDialogs() {
    const context = {
        console,
        URLSearchParams,
        // Browser globals stubbed minimally — session_dialogs.js touches them
        // only at module-load time, not in the render path.
        window: { dashboardData: { startupComplete: true } },
        document: {
            getElementById: () => ({ innerHTML: '' }),
            addEventListener: () => {},
        },
        // Render helpers from sibling source files in the runtime bundle.
        // The render path reaches into these — they're real (escapeHtml) or
        // identity-leaning stubs (escapeAttr) that do not affect the
        // structural assertions below.
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/session_dialogs.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'session_dialogs.js' });
    return context;
}

const _ctx = loadSessionDialogs();
const renderValidationDialog = _ctx.renderValidationDialog;

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

test('passed run: title flips to "Validation Passed #N"', () => {
    const { title } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    assert.strictEqual(title, 'Validation Passed #4244');
});

test('passed run: renders Tests section, not Failed Tests', () => {
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    // Section heading flips so the modal doesn't read like a failure.
    assert.match(html, /<div class="diag-section-title">Tests<\/div>/);
    assert.doesNotMatch(html, /<div class="diag-section-title">Failed Tests/);
    // And shows the all-passed empty-state copy rather than the failure list.
    assert.match(html, /All tests passed\./);
});

test('passed run: outcome chip is is-ok green, not is-warn red', () => {
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    // Operators eyeballing the chip row need an at-a-glance signal.
    assert.match(html, /diag-chip is-ok">passed<\/span>/);
    assert.doesNotMatch(html, /diag-chip is-warn">failed/);
});

test('passed run: stdout excerpt section still renders for spot-checking the green run', () => {
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    // The whole point of #6274: passing runs should let users SEE the test
    // tail ("142 passed in 41.21s"). Regression here defeats the feature.
    assert.match(html, /Validation Output Excerpt/);
    assert.match(html, /142 passed in 41\.21s/);
});

test('failed run: title stays "Validation Failure #N"', () => {
    const { title } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.strictEqual(title, 'Validation Failure #4242');
});

test('failed run: renders Failed Tests section with each failing nodeid', () => {
    const { html } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.match(html, /<div class="diag-section-title">Failed Tests \(2\)<\/div>/);
    assert.match(html, /tests\/unit\/test_one\.py::test_a/);
    assert.match(html, /tests\/unit\/test_two\.py::test_b/);
});

test('failed run: outcome chip is is-warn red', () => {
    const { html } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
});

test('payload missing status field falls back to failed rendering (back-compat)', () => {
    // Defensive: an older server (or a regression in the contract layer)
    // could omit `status`. The renderer must default to the failure UX so
    // operators don't see a false-green dialog.
    const legacyPayload = { ..._FAILED_PAYLOAD };
    delete legacyPayload.status;
    const { html } = renderValidationDialog(legacyPayload, 4242);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
    assert.match(html, /Failed Tests/);
});

test('runDir falls back to first action-section run_dir when caller passes null', () => {
    // openValidationFailure passes runDir=null when the timeline action
    // didn't carry one; the renderer must extract a run_dir from the
    // payload's action sections so the modal's "Open ..." buttons work.
    const payload = {
        ..._FAILED_PAYLOAD,
        action_sections: [{
            title: 'Validation Artifacts',
            actions: [{ type: 'open_path', label: 'Open Validation Record', path: '/tmp/r1/validation-record.json', group: 'validation_artifacts', run_dir: '/run/r1' }],
        }],
    };
    const { runDir } = renderValidationDialog(payload, 4242, null);
    assert.strictEqual(runDir, '/run/r1');
});

test('runDir from caller takes precedence over payload', () => {
    // When the timeline action carried a run_dir, that's the authoritative
    // scope (the dialog might be opened on a different run than the latest).
    const payload = {
        ..._FAILED_PAYLOAD,
        action_sections: [{
            title: 'Validation Artifacts',
            actions: [{ type: 'open_path', label: 'X', path: '/y', group: 'validation_artifacts', run_dir: '/run/payload-suggests' }],
        }],
    };
    const { runDir } = renderValidationDialog(payload, 4242, '/run/caller-knows-best');
    assert.strictEqual(runDir, '/run/caller-knows-best');
});
