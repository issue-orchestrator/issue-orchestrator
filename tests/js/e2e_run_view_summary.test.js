// JS-vm tests for E2E run summary count projection.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadModule() {
    const context = {
        console,
        window: { dashboardData: { agents: [] } },
        escapeHtml: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        escapeAttr: (value) => String(value).replace(/"/g, '&quot;'),
        formatTimestamp: () => '',
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_run_view.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'e2e_run_view.js' });
    return context;
}

test('summary chips count quarantined separately from skipped and failing', () => {
    const ctx = loadModule();
    const html = ctx._renderRunSummaryChips(
        { run: { status: 'warning', command: ['pytest', 'tests/e2e'], duration_seconds: 12.3 } },
        {
            status: 'passed',
            failed_tests: [],
            junit_cases: [
                { case_id: 'p1', outcome: 'passed' },
                { case_id: 's1', outcome: 'skipped' },
                { case_id: 'q1', outcome: 'failed', is_quarantined: true },
                { case_id: 'q2', outcome: 'error', is_quarantined: true },
            ],
        },
    );

    assert.match(html, /e2e-run-chip-warning">warning/);
    assert.match(html, />2 quarantined</);
    assert.match(html, />1 skipped</);
    assert.match(html, />1 passing</);
    assert.doesNotMatch(html, />2 failing</);
    assert.doesNotMatch(html, />3 skipped</);
});
