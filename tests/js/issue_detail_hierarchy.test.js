// JS-vm tests for the dashboard issue-detail timeline hierarchy.
//
// The dashboard timeline should use the same native disclosure-row idiom
// as the E2E run list: run and cycle nodes are <details>/<summary>
// containers, while validation evidence remains the canonical viewer body
// mounted under validation events.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

function _escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function loadRenderSlice(overrides = {}) {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js'),
        'utf8',
    );
    const lifecycleSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    const slice = [
        _extractFunction(lifecycleSource, 'function _renderLifecycleCommandAttr'),
        _extractFunction(source, 'function _journeyDisclosureCommandAttr'),
        _extractFunction(source, 'function _renderJourneyRuns'),
    ].join('\n');
    const listeners = [];
    const context = {
        console,
        journeyFilter: 'all',
        timelineView: 'user',
        issueDetailData: { issue_number: 4503, summary: {} },
        escapeHtml: _escapeHtml,
        escapeAttr: _escapeHtml,
        filterRuns: (runs) => runs,
        formatJourneyHeaderTimestamp: (_timestamp, label) => label || '13:05',
        formatJourneyStepTimestamp: (_timestamp, label) => label || '13:05',
        _readOutcomeBadge: (outcome) => ({
            label: outcome && outcome.label ? outcome.label : 'In progress',
            toneClass: outcome && outcome.tone === 'failed' ? 'outcome-failed' : '',
        }),
        _renderCycleValidationBadge: () => '',
        _filterStepActions: (step) => Array.isArray(step && step.actions) ? step.actions : [],
        _isValidationStep: () => false,
        _findCycleRunDir: () => '',
        renderTimelineEventActions: () => '',
        handleTimelineEventActionsClick: () => {},
        listeners,
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (_renderJourneyRuns slice)' });
    return context;
}

function loadCommandSlice(overrides = {}) {
    const lifecycleSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    const calls = [];
    const slice = [
        _extractFunction(lifecycleSource, 'function _lifecycleCommandFromElement'),
        _extractFunction(lifecycleSource, 'function runLifecycleCommandFromToggle'),
        _extractFunction(lifecycleSource, 'function runE2ELifecycleCommandFromToggle'),
        _extractFunction(lifecycleSource, 'function runE2ELifecycleCommand('),
    ].join('\n');
    const context = {
        console,
        calls,
        showToast: (message, severity) => calls.push(['toast', String(message), severity]),
        syncJourneyDisclosureState: (triggerEl) => calls.push(['sync', triggerEl && triggerEl.id]),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (timeline command slice)' });
    return context;
}

function renderJourney(payload, overrides = {}) {
    const ctx = loadRenderSlice(overrides);
    const container = {
        innerHTML: '',
        dataset: {},
        addEventListener: (type, handler) => ctx.listeners.push({ type, handler }),
    };
    ctx.issueDetailData.summary = payload.summary || {};
    ctx._renderJourneyRuns(container, payload.runs || []);
    return { html: container.innerHTML, listeners: ctx.listeners };
}

test('issue detail timeline renders runs and cycles as native disclosure nodes', () => {
    const { html, listeners } = renderJourney({
        runs: [
            {
                run_number: 1,
                run_label: 'Run 1',
                expanded: false,
                outcome: { label: 'Blocked', tone: 'failed' },
                cycles: [
                    {
                        cycle_in_run: 1,
                        cycle_label: 'Cycle 1',
                        expanded: false,
                        outcome: { label: 'Validation failed', tone: 'failed' },
                        phase_groups: [],
                    },
                ],
            },
        ],
        summary: {},
    });

    assert.match(html, /<details class="journey-run unified-timeline-node" id="journey-run-0"/);
    assert.match(html, /<summary class="journey-cycle-header unified-timeline-summary">/);
    assert.match(html, /<details class="journey-cycle unified-timeline-node" id="journey-cycle-0-0"/);
    assert.match(html, /data-lifecycle-command="/);
    assert.match(html, /&quot;kind&quot;:&quot;sync_journey_disclosure&quot;/);
    assert.match(html, /ontoggle="runLifecycleCommandFromToggle\(this\)"/);
    assert.match(html, /<div class="journey-cycle-body collapsed" id="journey-run-0-body">/);
    assert.match(html, /<div class="journey-cycle-body collapsed" id="journey-cycle-0-0-body">/);
    assert.doesNotMatch(html, /onclick="toggleJourneyCycle/);
    assert.doesNotMatch(html, /data-timeline-command/);
    assert.doesNotMatch(html, /runJourneyTimelineCommandFromToggle/);
    assert.strictEqual(listeners.length, 1);
    assert.strictEqual(listeners[0].type, 'click');
});

test('cycle artifact affordance is a keyboard-reachable button', () => {
    const { html } = renderJourney({
        runs: [
            {
                run_number: 1,
                run_label: 'Run 1',
                expanded: true,
                outcome: { label: 'In progress', tone: 'in_progress' },
                cycles: [
                    {
                        cycle_in_run: 1,
                        cycle_label: 'Cycle 1',
                        expanded: true,
                        outcome: { label: 'Completed', tone: 'passed' },
                        artifacts: { has_review_feedback: true },
                        phase_groups: [],
                    },
                ],
            },
        ],
        summary: {},
    });

    assert.match(html, /<button type="button" class="journey-cycle-artifacts-btn"/);
    assert.match(html, /aria-label="Open artifacts for Cycle 1"/);
    assert.doesNotMatch(html, /<span class="journey-cycle-artifacts-btn"/);
});

test('timeline command: toggle dispatch syncs the disclosure UI', () => {
    const ctx = loadCommandSlice();
    const disclosure = {
        id: 'journey-cycle-0-0',
        open: false,
        dataset: {
            lifecycleCommand: JSON.stringify({
                kind: 'sync_journey_disclosure',
                target_id: 'journey-cycle-0-0',
            }),
        },
    };

    ctx.runLifecycleCommandFromToggle(disclosure);

    assert.deepEqual(ctx.calls, [['sync', 'journey-cycle-0-0']]);
});

test('timeline command: malformed command fails before touching UI', () => {
    const ctx = loadCommandSlice();
    ctx.runLifecycleCommandFromToggle({
        id: 'journey-run-0',
        dataset: { lifecycleCommand: '{not json' },
    });

    assert.strictEqual(ctx.calls.length, 1);
    assert.strictEqual(ctx.calls[0][0], 'toast');
    assert.match(ctx.calls[0][1], /Failed to decode lifecycle command/);
    assert.strictEqual(ctx.calls[0][2], 'error');
});

test('timeline command: unsupported kind does not sync UI', () => {
    const ctx = loadCommandSlice();
    ctx.runE2ELifecycleCommand({ kind: 'unknown_timeline_kind' }, { id: 'journey-run-0' });

    assert.deepEqual(ctx.calls, [[
        'toast',
        'Unsupported lifecycle command: unknown_timeline_kind',
        'warning',
    ]]);
});

test('timeline command: target mismatch does not sync UI', () => {
    const ctx = loadCommandSlice();
    ctx.runE2ELifecycleCommand(
        { kind: 'sync_journey_disclosure', target_id: 'journey-cycle-0-1' },
        { id: 'journey-cycle-0-0' },
    );

    assert.deepEqual(ctx.calls, [[
        'toast',
        'Timeline command target mismatch',
        'error',
    ]]);
});
