// JS-vm tests for the issue-orchestrator lifecycle renderer owned by
// the ``io.agent-context`` plugin.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _loadPlugin(extra = {}) {
    const ctx = {
        window: {},
        globalThis: {},
        console,
        URLSearchParams,
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
        _renderLifecycleCommandAttr: (command) =>
            `data-lifecycle-command="${String(command.kind || '')}"`,
        ...extra,
    };
    ctx.window = ctx;
    ctx.globalThis = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'hierarchical_timeline.js'), 'utf8'),
        ctx,
        { filename: 'hierarchical_timeline.js' },
    );
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'plugins/agent_context.js'), 'utf8'),
        ctx,
        { filename: 'plugins/agent_context.js' },
    );
    return ctx;
}

test('plugin lifecycle renderer renders the shared run/cycle/event tree with action menus', () => {
    const ctx = _loadPlugin();
    ctx.registerHierarchicalTimelineHostCapability('renderEventActions', () => (actions) => {
        const buttons = (Array.isArray(actions) ? actions : [])
            .map((action) => `<button class="timeline-action-btn" data-action="${action.type}">${action.type}</button>`)
            .join('');
        return buttons ? `<div class="timeline-event-actions">${buttons}</div>` : '';
    });

    const html = ctx.renderIssueLifecycleTimeline([
        {
            run_number: 1,
            run_label: 'Run 1',
            outcome: { label: 'Failed', tone: 'failed' },
            expanded: true,
            cycles: [{
                cycle_number: 1,
                cycle_label: 'Cycle 1',
                outcome: { label: 'Validation failed', tone: 'failed' },
                expanded: true,
                phase_groups: [{
                    key: 'coding',
                    label: 'Coding',
                    steps: [{
                        event: 'session.started',
                        narrative: 'Coding session started',
                        actions: [
                            { type: 'open_agent_log', label: 'Coding Session Recording' },
                            { type: 'open_review_transcript', label: 'Review Transcript' },
                        ],
                    }],
                }],
            }],
        },
    ], { baseId: 'shared' });

    assert.match(html, /<details class="journey-run unified-timeline-node" id="shared-run-0" open>/);
    assert.match(html, /<details class="journey-cycle unified-timeline-node" id="shared-cycle-0-0" open>/);
    assert.match(html, /Coding session started/);
    assert.match(html, /timeline-event-actions/);
    assert.match(html, /open_agent_log/);
    assert.match(html, /open_review_transcript/);
});

test('plugin lifecycle renderer gives validation events an inline canonical-JUnit host and filters modal action', () => {
    const ctx = _loadPlugin();
    ctx.registerHierarchicalTimelineHostCapability('renderEventActions', () => (actions) => {
        const buttons = (Array.isArray(actions) ? actions : [])
            .map((action) => `<button class="timeline-action-btn" data-action="${action.type}">${action.type}</button>`)
            .join('');
        return buttons ? `<div class="timeline-event-actions">${buttons}</div>` : '';
    });

    const html = ctx.renderIssueLifecycleTimeline([
        {
            run_number: 1,
            outcome: { label: 'Failed', tone: 'failed' },
            expanded: true,
            cycles: [{
                cycle_number: 1,
                outcome: { label: 'Validation failed', tone: 'failed' },
                expanded: true,
                phase_groups: [{
                    key: 'coding',
                    label: 'Coding',
                    steps: [{
                        event: 'validation.failed',
                        narrative: 'Validation failed',
                        run_dir: '/tmp/run',
                        actions: [
                            { type: 'open_validation_failure', label: 'Validation Details' },
                            { type: 'open_agent_log', label: 'Coding Session Recording' },
                        ],
                    }],
                }],
            }],
        },
    ], { baseId: 'shared', issueNumber: 4503 });

    assert.match(html, /journey-step-validation/);
    assert.match(html, /<button type="button" class="journey-step-inline-toggle"/);
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /aria-controls="shared-cycle-0-0-step-0-body"/);
    assert.match(html, /journey-step-validation-body collapsed/);
    assert.match(html, /role="region"/);
    assert.match(html, /aria-hidden="true"/);
    assert.match(html, /data-run-dir="\/tmp\/run"/);
    assert.match(html, /toggleValidationEventInline\(&quot;shared-cycle-0-0-step-0&quot;, 4503, &quot;\/tmp\/run&quot;\)/);
    assert.match(html, /open_agent_log/);
    assert.doesNotMatch(html, /open_validation_failure/);
});

test('plugin lifecycle renderer marks an in-round progress step with an accessible live badge', () => {
    const ctx = _loadPlugin();

    const html = ctx.renderIssueLifecycleTimeline([
        {
            run_number: 1,
            outcome: { label: 'In progress', tone: 'in_progress' },
            expanded: true,
            cycles: [{
                cycle_number: 1,
                outcome: { label: 'Review in progress', tone: 'in_progress' },
                expanded: true,
                phase_groups: [{
                    key: 'review',
                    label: 'Review',
                    steps: [{
                        event: 'review_exchange.role_prompted',
                        status: 'completed',
                        narrative: 'Coder running (round 1)',
                        in_round_progress: true,
                    }],
                }],
            }],
        },
    ], { baseId: 'shared' });

    assert.match(html, /journey-step-in-progress/);
    // The badge carries text ("In progress") and role="status" — status is not
    // signalled by colour alone (accessibility requirement, issue #6428).
    assert.match(html, /class="journey-progress-badge" role="status"/);
    assert.match(html, /journey-progress-dot/);
    assert.match(html, /In progress/);
    assert.match(html, /Coder running \(round 1\)/);
});

test('plugin lifecycle renderer omits the live badge for ordinary completed steps', () => {
    const ctx = _loadPlugin();

    const html = ctx.renderIssueLifecycleTimeline([
        {
            run_number: 1,
            outcome: { label: 'Passed', tone: 'passed' },
            expanded: true,
            cycles: [{
                cycle_number: 1,
                outcome: { label: 'Reviewed', tone: 'passed' },
                expanded: true,
                phase_groups: [{
                    key: 'review',
                    label: 'Review',
                    steps: [{
                        event: 'review.approved',
                        status: 'completed',
                        narrative: 'Reviewer approved',
                    }],
                }],
            }],
        },
    ], { baseId: 'shared' });

    assert.doesNotMatch(html, /journey-step-in-progress/);
    assert.doesNotMatch(html, /journey-progress-badge/);
});

test('plugin lifecycle renderer uses host timestamp capabilities instead of dashboard globals', () => {
    const ctx = _loadPlugin();
    ctx.registerHierarchicalTimelineHostCapabilities({
        formatHeaderTimestamp: () => (_timestamp, fallback) => `header:${fallback}`,
        formatStepTimestamp: () => (_timestamp, fallback) => `step:${fallback}`,
    });

    const html = ctx.renderIssueLifecycleTimeline([
        {
            run_label: 'Run',
            outcome: { label: 'Passed', tone: 'passed' },
            timestamp: 'raw-run',
            time_label: 'run-label',
            expanded: true,
            cycles: [{
                cycle_label: 'Cycle',
                outcome: { label: 'Passed', tone: 'passed' },
                timestamp: 'raw-cycle',
                time_label: 'cycle-label',
                expanded: true,
                steps: [{
                    event: 'session.completed',
                    narrative: 'done',
                    timestamp: 'raw-step',
                    time_label: 'step-label',
                }],
            }],
        },
    ], { baseId: 'shared' });

    assert.match(html, /header:run-label/);
    assert.match(html, /header:cycle-label/);
    assert.match(html, /step:step-label/);
});
