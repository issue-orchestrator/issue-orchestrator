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
        // Fallback humanizer used by ``_renderLifecycleCommandButton`` only
        // when no label is supplied; the dialog renderer always supplies one,
        // so this is a defensive stub that never fires in these tests.
        _humanizeSnakeCase: (s) => String(s || '')
            .split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' '),
        formatTimestamp: (value, fallback = '') => {
            if (!value || value === '-') return fallback;
            return `local:${String(value).slice(0, 10)}`;
        },
    };
    vm.createContext(context);
    // ``validation_viewer.js`` defines ``renderCanonicalValidationViewer``
    // which ``session_dialogs.js`` delegates the body of the validation
    // dialog to (issue #6310 follow-up).  Load it first so the symbol
    // is in scope when session_dialogs evaluates.
    const viewerSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/validation_viewer.js'),
        'utf8',
    );
    vm.runInContext(viewerSource, context, { filename: 'validation_viewer.js' });
    // ``lifecycle_commands.js`` owns the shared typed-Command renderer
    // (``_renderLifecycleCommandButton``) + dispatcher that the dialog
    // action buttons route through (issue #6327).  Loaded before
    // ``session_dialogs.js`` so the symbol is in scope, mirroring the
    // production bundle order in ``dashboard_assets.py``.
    const lifecycleSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    vm.runInContext(lifecycleSource, context, { filename: 'lifecycle_commands.js' });
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

test('passed run: body delegates to the canonical viewer (no Failed Tests subsection)', () => {
    // Per issue #6310 follow-up Phase A: the body is the canonical
    // viewer (``cvv-root``).  Failed-Tests subsection is gone; the
    // legacy "All tests passed." placeholder is gone too.  Passed
    // tests, if any, are browseable via the viewer's browse-by-file
    // expander.
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    assert.match(html, /cvv-root/);
    assert.doesNotMatch(html, /<div class="diag-section-title">Failed Tests/);
    assert.doesNotMatch(html, /All tests passed\./);
});

test('passed run: outcome chip is is-ok green, not is-warn red', () => {
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    // Operators eyeballing the chip row need an at-a-glance signal.
    assert.match(html, /diag-chip is-ok">passed<\/span>/);
    assert.doesNotMatch(html, /diag-chip is-warn">failed/);
});

test('passed run: stdout excerpt still surfaces for spot-checking the green run', () => {
    const { html } = renderValidationDialog(_PASSED_PAYLOAD, 4244);
    // The original requirement from #6274 (passing runs should let
    // users see the test tail) is preserved by the canonical viewer's
    // "Run stdout" expander — same content, new container.
    assert.match(html, /Run stdout/);
    assert.match(html, /142 passed in 41\.21s/);
});

test('failed run: title stays "Validation Failure #N"', () => {
    const { title } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.strictEqual(title, 'Validation Failure #4242');
});

test('failed run: surfaces failing test node-ids inside the canonical viewer', () => {
    // The canonical viewer renders triage cards per junit case.  When
    // ``junit_cases`` is empty (this payload), the viewer falls back to
    // the "Run stdout" excerpt which carries the FAILED line.  The
    // viewer also accepts the legacy ``failed_tests`` string list via
    // the header chip row.  Both surfaces are checked: chips + stdout.
    const { html } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.match(html, /2 failing tests/);
    assert.match(html, /tests\/unit\/test_one\.py::test_a/);
});

test('failed run: outcome chip is is-warn red', () => {
    const { html } = renderValidationDialog(_FAILED_PAYLOAD, 4242);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
});

test('summary timestamp rows render through the local formatter', () => {
    const payload = {
        ..._FAILED_PAYLOAD,
        summary_rows: [
            { label: 'Started At', value: '2026-05-05T00:00:00Z', value_kind: 'timestamp' },
            { label: 'Completed', value: '2026-05-05T00:00:30Z', value_kind: 'timestamp' },
            { label: 'Outcome', value: 'Failed' },
        ],
    };

    const { html } = renderValidationDialog(payload, 4242);

    assert.match(html, /local:2026-05-05/);
    assert.doesNotMatch(html, /2026-05-05T00:00:00Z/);
    assert.doesNotMatch(html, /2026-05-05T00:00:30Z/);
});

test('summary timestamp-like labels stay raw without typed value_kind', () => {
    const payload = {
        ..._FAILED_PAYLOAD,
        summary_rows: [
            { label: 'Started', value: '2026-05-05T00:00:00Z' },
            { label: 'Retention Expires', value: '2026-05-06T00:00:00Z' },
        ],
    };

    const { html } = renderValidationDialog(payload, 4242);

    assert.match(html, /2026-05-05T00:00:00Z/);
    assert.match(html, /2026-05-06T00:00:00Z/);
    assert.doesNotMatch(html, /local:2026-05-05/);
});

test('payload missing status field falls back to failed rendering (back-compat)', () => {
    // Defensive: an older server (or a regression in the contract layer)
    // could omit `status`. The renderer must default to the failure UX so
    // operators don't see a false-green dialog.
    const legacyPayload = { ..._FAILED_PAYLOAD };
    delete legacyPayload.status;
    const { html } = renderValidationDialog(legacyPayload, 4242);
    assert.match(html, /diag-chip is-warn">failed<\/span>/);
    // The canonical viewer wrapper marks failed status on the root
    // element so the styling cascades correctly.
    assert.match(html, /data-cvv-status="failed"/);
});

test('failed run with junit_cases: viewer renders per-case triage cards', () => {
    // When the parser populates ``junit_cases`` (the typed per-test
    // contract that's been available since #6309), the viewer surfaces
    // failing tests as triage cards with the headline + traceback split.
    // This is the path that becomes the dominant render once
    // ``junit_cases`` is consistently populated server-side.
    const payload = {
        ..._FAILED_PAYLOAD,
        junit_cases: [
            {
                case_id: 't1',
                display_name: 'test_a',
                suite_name: 'tests/unit/test_one.py',
                outcome: 'failed',
                duration_seconds: 0.012,
                failure_details: 'AssertionError: expected 1 to equal 2\n  File "test_one.py", line 7',
                system_out: 'before assert',
                system_err: null,
                extras: [],
            },
        ],
    };
    const { html } = renderValidationDialog(payload, 4242);
    assert.match(html, /cvv-triage-card/);
    assert.match(html, /AssertionError: expected 1 to equal 2/);
    assert.match(html, /test_a/);
});

// ── Affordance-glyph convention (issue #6322 / PR #6325 / #6327) ──────────
//
// Every dialog command kind maps to exactly one glyph (or to none, for local
// actions).  The convention is:
//   ``↗`` — external viewer (opens an OS app, file, or page outside
//           the current scroll context)
//   ``⧉`` — modal viewer (opens ``#modalOverlay`` on this page)
//   (none) — local action (does the thing in place, no UI change)
//
// Issue #6327 re-keyed the glyph off the typed ``command.kind`` (the backend
// emits the typed command; the JS no longer translates ``action.type``).

test('affordance: every dialog command kind maps to the right glyph', () => {
    const ctx = loadSessionDialogs();
    // External viewers: handler opens an OS app or external page.
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'open_path' }), ' ↗');
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'open_orchestrator_log' }), ' ↗');
    // Modal viewers: handler opens ``#modalOverlay``.
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'open_session_recording' }), ' ⧉');
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'open_session_diagnostics' }), ' ⧉');
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'view_claude_log' }), ' ⧉');
    // Local actions: no glyph.
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'copy_session_recording' }), '');
    // Unknown kinds default to no glyph (intentional — better a missing
    // affordance than a wrong one for kinds we don't know how to characterize).
    assert.strictEqual(ctx._affordanceGlyphForCommand({ kind: 'made_up_action' }), '');
    assert.strictEqual(ctx._affordanceGlyphForCommand({}), '');
    assert.strictEqual(ctx._affordanceGlyphForCommand(null), '');
});

test('affordance: rendered button label carries the right trailing glyph for every command', () => {
    // End-to-end through the renderer: ``_renderDialogActionButton`` appends
    // the glyph to the command's label.  The action is ``{ command, group }``
    // — exactly what the backend view models emit.
    const ctx = loadSessionDialogs();
    const cases = [
        // [command, expectedSuffix]
        [{ kind: 'open_path', label: 'TheLabel', path: '/tmp/some.log' }, ' ↗'],
        [{ kind: 'open_orchestrator_log', label: 'TheLabel', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' }, ' ↗'],
        [{ kind: 'open_session_recording', label: 'TheLabel', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' }, ' ⧉'],
        [{ kind: 'open_session_diagnostics', label: 'TheLabel', issue_number: 42, run_dir: '/run/x' }, ' ⧉'],
        [{ kind: 'view_claude_log', label: 'TheLabel', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' }, ' ⧉'],
        [{ kind: 'copy_session_recording', label: 'TheLabel', issue_number: 42, run_dir: '/run/x' }, ''],
    ];
    for (const [command, suffix] of cases) {
        const html = ctx._renderDialogActionButton({ command, group: 'session_evidence' }, null, 'diag-btn');
        assert.notStrictEqual(html, '', `expected non-empty HTML for kind=${command.kind}`);
        // Strip the dispatch wrapper to just look at the visible button text.
        const visibleLabel = html.match(/>([^<]+)<\/button>/);
        assert.ok(visibleLabel, `could not find visible label in: ${html.slice(0, 200)}…`);
        const expected = `TheLabel${suffix}`;
        assert.strictEqual(visibleLabel[1], expected,
            `kind=${command.kind}: expected button label ${JSON.stringify(expected)}, got ${JSON.stringify(visibleLabel[1])}`);
    }
});

test('affordance: rendered button carries the backend command routed through the shared dispatcher', () => {
    // Issue #6327: dialog action buttons carry the typed
    // ``data-lifecycle-command`` the backend produced and route through the
    // single shared ``runLifecycleCommandFromButton`` owner — no per-action
    // inline ``onclick`` handler name leaks into the markup.
    const ctx = loadSessionDialogs();
    const legacyHandlers = [
        'openPath(', 'openValidationFailure(', 'openReviewTranscript(',
        'openReviewFeedback(', 'openSessionManifest(', 'openAgentLogAction(',
        'viewClaudeLog(', 'copyAgentLogAction(', 'openFilteredOrchestratorLog(',
    ];
    const commands = [
        { kind: 'open_path', label: 'L', path: '/p' },
        { kind: 'open_orchestrator_log', label: 'L', issue_number: 1, run_dir: '/run/x', error_surface: 'inline' },
        { kind: 'open_session_recording', label: 'L', issue_number: 1, run_dir: '/run/x', error_surface: 'inline' },
        { kind: 'open_session_diagnostics', label: 'L', issue_number: 1, run_dir: '/run/x' },
        { kind: 'view_claude_log', label: 'L', issue_number: 1, run_dir: '/run/x', error_surface: 'inline' },
        { kind: 'copy_session_recording', label: 'L', issue_number: 1, run_dir: '/run/x' },
    ];
    for (const command of commands) {
        const html = ctx._renderDialogActionButton({ command, group: 'session_evidence' }, null, 'diag-btn');
        assert.ok(html.includes('runLifecycleCommandFromButton(this)'),
            `kind=${command.kind}: expected shared dispatcher onclick, got: ${html.slice(0, 240)}…`);
        for (const legacy of legacyHandlers) {
            assert.ok(!html.includes(legacy),
                `kind=${command.kind}: legacy inline handler ${legacy} must not appear in the migrated markup`);
        }
        // The rendered button carries the backend command verbatim.
        const match = html.match(/data-lifecycle-command="([^"]+)"/);
        assert.ok(match, `kind=${command.kind}: expected a data-lifecycle-command payload`);
        const decoded = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
        assert.deepStrictEqual(decoded, command,
            `kind=${command.kind}: rendered command must equal the backend command`);
    }
});
