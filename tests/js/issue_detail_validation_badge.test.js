// JS-vm test for the per-cycle validation badge render + dispatch path
// (issue #6310 AC-2 / reviewer Blocker 3 on PR #6312).
//
// Loads the shared ``lifecycle_commands.js`` dispatcher and just the
// badge-relevant helpers from ``issue_detail_drawer.js`` into a node:vm
// context, then exercises the four ``CycleValidationBadge`` states
// (``passed`` / ``failed`` / ``not_validated`` / ``pending``) end-to-end:
//
// 1. ``_renderCycleValidationBadge`` produces the expected HTML for each
//    state, including the typed-Command payload for passed/failed.
// 2. Simulating a click on the rendered button (via
//    ``runLifecycleCommandFromButton``) dispatches the correct
//    handler with the correct args.
//
// Without this test the drawer badge can render the wrong shape (or call
// the wrong handler) and only Playwright would catch it.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadBadgeContext(overrides = {}) {
    const calls = [];
    const toasts = [];
    const baseStubs = {
        console,
        calls,
        toasts,
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (snake) => String(snake || '')
            .split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' '),
        // Dispatch targets the validation badge cares about:
        openValidationFailure: (issueNumber, runDir, target) =>
            calls.push(['open_validation_details', issueNumber, runDir, target]),
        // Other dispatch targets stubbed so unrelated kinds wouldn't
        // accidentally route into the validation handler.
        openIssueTimeline: (issueNumber, _ref, opts) =>
            calls.push(['open_issue_timeline', issueNumber, opts || {}]),
        openAgentLogAction: (issueNumber, runDir, label, target, opts) =>
            calls.push(['open_session_recording', issueNumber, runDir, label, target, opts || {}]),
        openReviewTranscript: (issueNumber, runDir, opts, target) =>
            calls.push(['open_review_transcript', issueNumber, runDir, opts || {}, target]),
        openPath: (p) => calls.push(['open_completion_record', String(p)]),
        showToast: (message, severity) => toasts.push([String(message), severity]),
    };
    const context = { ...baseStubs, ...overrides };
    vm.createContext(context);

    // Shared command renderer/dispatcher loads first — same as in the
    // production asset manifest (``view_models/dashboard_assets.py``).
    const commandsSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    vm.runInContext(commandsSource, context, { filename: 'lifecycle_commands.js' });

    // Load only the badge helper from issue_detail_drawer.js by extracting
    // it from source — the rest of the drawer module depends on browser
    // globals (issueDetailDrawer, document, etc.) that we don't want to
    // stub here.  The helper is small enough to slice out by regex.
    const drawerSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js'),
        'utf8',
    );
    const badgeMatch = drawerSource.match(
        /function _renderCycleValidationBadge[\s\S]+?\n\}\n/,
    );
    if (!badgeMatch) {
        throw new Error('_renderCycleValidationBadge not found in issue_detail_drawer.js');
    }
    vm.runInContext(badgeMatch[0], context, { filename: 'issue_detail_drawer.js (badge slice)' });
    return { context, calls, toasts };
}

function _extractPayload(html) {
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    if (!match) return null;
    return JSON.parse(
        match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')
    );
}

test('badge passed state renders an inline-expansion button (Phase B)', () => {
    // Phase B (issue #6310 follow-up): the badge no longer carries a
    // typed-Command payload that opens a modal.  It's an in-drawer
    // button that routes through the hierarchical host capability
    // registry to expand the cycle's validation event row inline.  The Command
    // pipeline is still used elsewhere (e.g. timeline event actions);
    // it's just no longer the badge's path.
    const { context } = loadBadgeContext();
    const badge = {
        state: 'passed',
        command: {
            kind: 'open_validation_details',
            label: 'Validation Details',
            issue_number: 4124,
            run_dir: '/tmp/run-1',
        },
    };
    const html = context._renderCycleValidationBadge(badge, 4124);
    assert.match(html, /journey-cycle-validation-badge is-passed/);
    assert.match(html, /✓ Validated/);
    assert.match(html, /runHierarchicalTimelineHostCapability\('handleCycleValidationBadgeClick', this\)/);
    assert.match(html, /data-validation-state="passed"/);
    assert.match(html, /data-issue-number="4124"/);
    // Modal Command payload is gone from the badge HTML.
    assert.doesNotMatch(html, /data-lifecycle-command/);
});

test('badge failed state renders an inline-expansion button (Phase B)', () => {
    const { context } = loadBadgeContext();
    const badge = {
        state: 'failed',
        command: {
            kind: 'open_validation_details',
            label: 'Validation Details',
            issue_number: 4124,
            run_dir: '/tmp/run-2',
        },
    };
    const html = context._renderCycleValidationBadge(badge, 4124);
    assert.match(html, /journey-cycle-validation-badge is-failed/);
    assert.match(html, /✗ Failed/);
    assert.match(html, /runHierarchicalTimelineHostCapability\('handleCycleValidationBadgeClick', this\)/);
    assert.match(html, /data-validation-state="failed"/);
    assert.match(html, /data-issue-number="4124"/);
    assert.doesNotMatch(html, /data-lifecycle-command/);
});

test('badge not_validated state renders a non-clickable span with no command', () => {
    const { context } = loadBadgeContext();
    const html = context._renderCycleValidationBadge(
        { state: 'not_validated', command: null },
        4124,
    );
    assert.match(html, /<span /);
    assert.match(html, /is-not-validated/);
    assert.match(html, /⚠ Not validated/);
    assert.doesNotMatch(html, /data-lifecycle-command/);
    assert.doesNotMatch(html, /<button/);
});

test('badge pending state renders nothing', () => {
    const { context } = loadBadgeContext();
    const html = context._renderCycleValidationBadge(
        { state: 'pending', command: null },
        4124,
    );
    assert.strictEqual(html, '');
});

test('badge passed/failed state renders even without a command payload (Phase B)', () => {
    // The badge no longer needs the typed Command — the inline
    // expander locates the run_dir from the cycle's data attributes.
    // Server-side ``CycleValidationBadge`` validators still require the
    // command for passed/failed states (so the contract stays typed for
    // any OTHER consumer of the data), but the badge HTML path doesn't
    // require it.
    const { context } = loadBadgeContext();
    const html = context._renderCycleValidationBadge({ state: 'passed' }, 1);
    assert.match(html, /journey-cycle-validation-badge is-passed/);
});

test('badge dispatch covers every supported lifecycle command kind', () => {
    // Sanity check that the shared dispatcher routes each Command kind
    // it claims to handle.  Catches missing handler-table entries.
    const { context, calls } = loadBadgeContext();
    const cases = [
        [
            { kind: 'open_issue_timeline', issue_number: 7, scope_kind: 'e2e_run', e2e_run_id: 88 },
            ['open_issue_timeline', 7, { e2eRunId: 88 }],
        ],
        [
            { kind: 'open_issue_timeline', issue_number: 7, scope_kind: 'dashboard' },
            ['open_issue_timeline', 7, {}],
        ],
        [
            {
                kind: 'open_session_recording', issue_number: 7, run_dir: '/r',
                label: 'Session Recording', round_index: 2, session_role: 'coder',
            },
            ['open_session_recording', 7, '/r', 'Session Recording', 'toast', {
                round_index: 2, session_role: 'coder',
            }],
        ],
        [
            {
                kind: 'open_review_transcript', issue_number: 7, run_dir: '/r',
                round_index: 1, transcript_role: 'reviewer',
            },
            ['open_review_transcript', 7, '/r', {
                round_index: 1, transcript_role: 'reviewer',
            }, 'toast'],
        ],
        [
            { kind: 'open_validation_details', issue_number: 7, run_dir: '/r' },
            ['open_validation_details', 7, '/r', 'toast'],
        ],
        [
            { kind: 'open_completion_record', path: '/cr.json' },
            ['open_completion_record', '/cr.json'],
        ],
    ];
    for (const [command, expected] of cases) {
        context.runLifecycleCommand(command);
    }
    assert.deepEqual(
        calls,
        cases.map(([_command, expected]) => expected),
    );
});

test('badge dispatch toasts a warning for an unknown command kind', () => {
    const { context, calls, toasts } = loadBadgeContext();
    context.runLifecycleCommand({ kind: 'totally_unknown_kind', issue_number: 1 });
    assert.deepEqual(calls, []);
    assert.strictEqual(toasts.length, 1);
    assert.match(toasts[0][0], /Unsupported lifecycle command: totally_unknown_kind/);
    assert.strictEqual(toasts[0][1], 'warning');
});
