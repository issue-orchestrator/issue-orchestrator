// Behavioral tests for E2E runtime dashboard actions.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadE2ERuntime(overrides = {}) {
    const calls = [];
    const toasts = [];
    const context = {
        console,
        calls,
        toasts,
        URLSearchParams,
        setInterval: () => 1,
        clearInterval: () => {},
        setTimeout: () => 1,
        alert: () => {},
        confirm: () => true,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        showToast: (message, severity) => toasts.push([String(message), severity]),
        showUnifiedRunView: (runId) => calls.push(['show_unified_run_view', runId]),
        document: {
            addEventListener: () => {},
            getElementById: () => null,
        },
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: null,
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_runtime.js'),
            'utf8',
        ),
        context,
        { filename: 'e2e_runtime.js' },
    );
    Object.assign(context, overrides);
    return context;
}

function makeActionEvent(dataset) {
    const event = {
        prevented: false,
        stopped: false,
        target: {
            closest: (selector) => selector === '[data-action]' ? { dataset } : null,
        },
        preventDefault() { this.prevented = true; },
        stopPropagation() { this.stopped = true; },
    };
    return event;
}

test('latest run results action opens formatted unified run view', () => {
    const ctx = loadE2ERuntime({
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: { id: 42, status: 'passed' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });

    ctx.showLatestE2ERunResults();

    assert.deepEqual(ctx.calls, [['show_unified_run_view', 42]]);
    assert.deepEqual(ctx.toasts, []);
});

test('delegated latest results click uses live latest-run state', () => {
    const ctx = loadE2ERuntime({
        window: {
            location: { search: '' },
            dashboardData: {
                repoRoot: '/tmp/repo',
                configName: 'default.yaml',
                e2eRunning: false,
                e2eLastRun: { id: 43, status: 'passed' },
                e2eNeedsAttention: false,
                e2eFailedTests: [],
            },
        },
    });
    const event = makeActionEvent({ action: 'show-latest-e2e-run-results' });

    ctx.handleE2ERuntimeActionClick(event);

    assert.equal(event.prevented, true);
    assert.equal(event.stopped, true);
    assert.deepEqual(ctx.calls, [['show_unified_run_view', 43]]);
});

test('delegated run-history results click uses the rendered run id', () => {
    const ctx = loadE2ERuntime();
    const event = makeActionEvent({ action: 'show-e2e-run-results', runId: '88' });

    ctx.handleE2ERuntimeActionClick(event);

    assert.equal(event.prevented, true);
    assert.equal(event.stopped, true);
    assert.deepEqual(ctx.calls, [['show_unified_run_view', 88]]);
});

test('latest run results action shows a toast when no run exists', () => {
    const ctx = loadE2ERuntime();

    ctx.showLatestE2ERunResults();

    assert.deepEqual(ctx.calls, []);
    assert.deepEqual(ctx.toasts, [['No E2E run data available', true]]);
});
