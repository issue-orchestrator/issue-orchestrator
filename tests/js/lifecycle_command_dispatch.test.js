// Entry-point tests for the shared lifecycle-Command dispatcher
// (issue #6337).
//
// ``tests/js/ui_contract_json.test.js`` proves the shared reader
// classifies payloads correctly.  This file proves the lifecycle entry
// points actually REACH that reader, which is a separate failure mode:
// a guard in ``_lifecycleCommandFromElement`` can suppress a diagnostic
// the reader would otherwise have produced, and the reader's own tests
// cannot see that.
//
// The distinction under test is absent vs present-but-empty:
//
//   * absent ``data-lifecycle-command``  → the element carries no
//     Command.  Nothing to report; stay quiet.
//   * ``data-lifecycle-command=""``      → a render bug.  The control
//     is wired to dispatch but carries no payload, and without a
//     diagnostic it reads to the user as a dead button.
//
// Truthiness collapses those two ('' is falsy), which is why these
// tests dispatch through ``runLifecycleCommandFromButton`` /
// ``runLifecycleCommandFromToggle`` rather than calling the reader.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Real generated validators + fail-closed reader (issue #6337): the
// module under test validates its JSON payloads through this.
const {
    captureContractViolations,
    resetContractViolationReporter,
    uiContractJson,
} = require('./ui_contract_test_support.js');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

const VALID_COMMAND = {
    kind: 'open_issue_timeline',
    label: 'Open timeline',
    issue_number: 4242,
    scope_kind: 'dashboard',
};

// Loads lifecycle_commands.js alone, with every dispatch handler it can
// route to replaced by a spy.  ``calls`` records any handler that fired,
// so "no handler ran" is asserted against the whole dispatch surface
// rather than a single expected target.
function _loadDispatcher() {
    const calls = [];
    const record = (name) => (...args) => calls.push({ name, args });
    const ctx = vm.createContext({
        console,
        uiContractJson,
        escapeHtml: (v) => String(v == null ? '' : v),
        escapeAttr: (v) => String(v == null ? '' : v),
        _humanizeSnakeCase: (s) => String(s || ''),
        showToast: record('showToast'),
        openIssueTimeline: record('openIssueTimeline'),
        openAgentLogAction: record('openAgentLogAction'),
        openReviewArtifact: record('openReviewArtifact'),
        openValidationFailure: record('openValidationFailure'),
        openPath: record('openPath'),
        expandE2ERunRow: record('expandE2ERunRow'),
        loadE2ERunIntoRow: record('loadE2ERunIntoRow'),
        switchE2ETimelineView: record('switchE2ETimelineView'),
        createIssuesForUntriaged: record('createIssuesForUntriaged'),
        loadInlineAgentAttempts: record('loadInlineAgentAttempts'),
    });
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'lifecycle_commands.js'), 'utf8'),
        ctx,
        { filename: 'lifecycle_commands.js' },
    );
    ctx.calls = calls;
    return ctx;
}

// A ``<details>``-alike in the state a real open toggle arrives in, so
// the payload check is what decides the outcome — not ``open`` or
// ``dataset.loaded``.
function _toggleEl(dataset) {
    return { open: true, dataset: { loaded: '', ...dataset } };
}

test.afterEach(() => resetContractViolationReporter());

test('button: a valid Command dispatches to its handler', () => {
    // Sanity anchor for the two no-dispatch tests below: proves the
    // harness CAN dispatch, so "no handler ran" there is the payload
    // being rejected and not a mis-wired stub.
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromButton({ dataset: { lifecycleCommand: JSON.stringify(VALID_COMMAND) } });
    assert.deepStrictEqual(violations, [], 'a contract-valid payload must not report a violation');
    assert.deepStrictEqual(ctx.calls.map((c) => c.name), ['openIssueTimeline']);
    assert.strictEqual(ctx.calls[0].args[0], 4242, 'the typed issue_number must reach the handler');
});

test('button: an empty data-lifecycle-command reports one violation and dispatches nothing', () => {
    // The regression: '' is falsy, so a truthiness guard here returned
    // early and the control died silently. A wired button carrying no
    // payload is a render bug and must be diagnosable from the console.
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromButton({ dataset: { lifecycleCommand: '' } });
    assert.strictEqual(violations.length, 1, 'an empty payload must report exactly one violation');
    assert.strictEqual(violations[0].schemaName, 'LifecycleCommandPayload');
    assert.strictEqual(violations[0].source, 'data-lifecycle-command', 'must name the DOM boundary');
    assert.deepStrictEqual(ctx.calls, [], 'no handler may run for a rejected payload');
});

test('toggle: an empty data-lifecycle-command reports one violation and dispatches nothing', () => {
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromToggle(_toggleEl({ lifecycleCommand: '' }));
    assert.strictEqual(violations.length, 1, 'an empty payload must report exactly one violation');
    assert.strictEqual(violations[0].source, 'data-lifecycle-command');
    assert.deepStrictEqual(ctx.calls, [], 'no handler may run for a rejected payload');
});

test('button: an absent data-lifecycle-command stays quiet', () => {
    // The other side of the same boundary. Not every element a caller
    // hands the dispatcher carries a Command; that is not a violation,
    // and reporting it would bury the real ones in noise.
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromButton({ dataset: {} });
    assert.deepStrictEqual(violations, [], 'an absent attribute is not a contract violation');
    assert.deepStrictEqual(ctx.calls, []);
});

test('toggle: an absent data-lifecycle-command stays quiet', () => {
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromToggle(_toggleEl({}));
    assert.deepStrictEqual(violations, [], 'an absent attribute is not a contract violation');
    assert.deepStrictEqual(ctx.calls, []);
});

test('a malformed-but-present payload is rejected before the open/loaded toggle gates', () => {
    // Ordering guard: the payload check must not sit behind the
    // ``open``/``loaded`` short-circuits, or a render bug on a closed or
    // already-loaded row would report nothing and the diagnostic would
    // depend on which row the user happened to expand first.
    const violations = captureContractViolations();
    const ctx = _loadDispatcher();
    ctx.runLifecycleCommandFromToggle({ open: false, dataset: { lifecycleCommand: '' } });
    ctx.runLifecycleCommandFromToggle({ open: true, dataset: { lifecycleCommand: '', loaded: '1' } });
    assert.strictEqual(violations.length, 2, 'each present-but-empty payload reports, gates notwithstanding');
    assert.deepStrictEqual(ctx.calls, []);
});
