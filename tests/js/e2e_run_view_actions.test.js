const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadE2ERunView(overrides = {}) {
    const calls = [];
    const context = {
        console,
        calls,
        window: {
            dashboardData: {
                agents: [],
                githubOwner: 'owner',
                githubRepo: 'repo',
            },
        },
        document: {},
        navigator: {},
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        closeE2EIssue: async (issueNumber, nodeid) => calls.push(['close_issue', issueNumber, nodeid]),
        showCreateIssueDropdown: (button, nodeid) => calls.push(['create_issue_dropdown', button, nodeid]),
        quarantineSingleTest: async (nodeid) => calls.push(['quarantine_test', nodeid]),
        createSingleIssueWithAgent: async (nodeid, agent) => calls.push(['create_issue_with_agent', nodeid, agent]),
        copyTestErrorFromRun: (nodeid) => calls.push(['copy_test_error', nodeid]),
        ...overrides,
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_run_view.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'e2e_run_view.js' });
    context.copyTestErrorFromRun = (nodeid) => calls.push(['copy_test_error', nodeid]);
    return context;
}

test('copy error row action dispatches through the command contract with nodeid', () => {
    const context = loadE2ERunView();

    context.runE2ERowActionFromButton({
        dataset: {
            e2eAction: 'copy_test_error',
            nodeid: 'tests/e2e/test_smoke.py::test_checkout',
        },
    });

    assert.deepEqual(context.calls, [
        ['copy_test_error', 'tests/e2e/test_smoke.py::test_checkout'],
    ]);
});

test('copy error row action fails fast when nodeid is missing', () => {
    const context = loadE2ERunView();

    assert.throws(
        () => context.runE2ERowActionFromButton({ dataset: { e2eAction: 'copy_test_error' } }),
        /Copy-error action missing nodeid/,
    );
    assert.deepEqual(context.calls, []);
});
