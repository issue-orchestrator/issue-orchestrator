const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const dashboardDir = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function escapeAttr(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function loadTimeline(overrides = {}) {
    const calls = [];
    const blockedList = {
        innerHTML: '',
        querySelectorAll: () => [],
    };
    const context = {
        blockedIssuesData: [],
        blockedList,
        blockedSelectAll: { checked: false, disabled: false, indeterminate: false },
        blockedSelectAllLabel: { textContent: '' },
        blockedWarning: { style: {} },
        blockedWarningText: { textContent: '' },
        escapeHtml: escapeAttr,
        escapeAttr,
        updateBlockedSelection: () => calls.push(['updateBlockedSelection']),
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(dashboardDir, 'timeline.js'), 'utf8'),
        context,
    );
    return { context, calls, blockedList };
}

test('renderBlockedList binds Resume to recorded run_dir', () => {
    const { context, blockedList } = loadTimeline({
        blockedIssuesData: [{
            issue_number: 123,
            issue_url: 'https://github.test/owner/repo/issues/123',
            title: 'Blocked',
            blocking_label: 'blocked',
            needs_human: false,
            failure_reason: 'agent done',
            worktree_path: '/tmp/worktree',
            run_dir: '/tmp/run"quoted',
            has_completion: true,
        }],
    });

    context.renderBlockedList();

    assert.match(blockedList.innerHTML, /class="resume-btn"/);
    assert.match(blockedList.innerHTML, /data-run-dir="\/tmp\/run&quot;quoted"/);
    assert.match(
        blockedList.innerHTML,
        /onclick="resumeIssue\(123, this\.dataset\.runDir, event\)"/,
    );
});

test('renderBlockedList hides Resume without recorded run_dir', () => {
    const { context, blockedList } = loadTimeline({
        blockedIssuesData: [{
            issue_number: 123,
            issue_url: 'https://github.test/owner/repo/issues/123',
            title: 'Blocked',
            blocking_label: 'blocked',
            needs_human: false,
            worktree_path: '/tmp/worktree',
            run_dir: null,
            has_completion: true,
        }],
    });

    context.renderBlockedList();

    assert.doesNotMatch(blockedList.innerHTML, /class="resume-btn"/);
});

function loadDiagnosticsActions(overrides = {}) {
    const calls = [];
    const context = {
        blockedList: { querySelectorAll: () => [] },
        blockedSelectAll: {},
        blockedSelectAllLabel: {},
        blockedUnblockBtn: {},
        blockedResetBtn: {},
        blockedIssuesData: [],
        showToast: (message, severity) => calls.push(['toast', message, severity]),
        closeBlockedModal: () => calls.push(['closeBlockedModal']),
        setTimeout: () => calls.push(['setTimeout']),
        uiActionContract: {
            buildIssueResumeRequest: (issueNumber, runDir) => ({
                endpoint: `/api/issues/${issueNumber}/resume`,
                method: 'POST',
                body: { run_dir: String(runDir) },
            }),
        },
        fetch: async (endpoint, options) => {
            calls.push(['fetch', endpoint, options]);
            return { json: async () => ({ success: false, error: 'still blocked' }) };
        },
        console: { error: () => {} },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(dashboardDir, 'diagnostics_actions.js'), 'utf8'),
        context,
    );
    return { context, calls };
}

test('resumeIssue posts the recorded run_dir contract', async () => {
    const { context, calls } = loadDiagnosticsActions();
    const button = { textContent: 'Resume', disabled: false };

    await context.resumeIssue(
        123,
        '/tmp/run',
        { stopPropagation: () => calls.push(['stopPropagation']), target: button },
    );

    const fetchCall = calls.find((call) => call[0] === 'fetch');
    assert.ok(fetchCall, 'resumeIssue should issue a fetch');
    assert.equal(fetchCall[1], '/api/issues/123/resume');
    assert.deepEqual(JSON.parse(fetchCall[2].body), { run_dir: '/tmp/run' });
});

test('resumeIssue refuses missing run_dir before fetch', async () => {
    const { context, calls } = loadDiagnosticsActions();
    const button = { textContent: 'Resume', disabled: false };

    await context.resumeIssue(
        123,
        '',
        { stopPropagation: () => calls.push(['stopPropagation']), target: button },
    );

    assert.ok(!calls.some((call) => call[0] === 'fetch'));
    assert.deepEqual(calls.find((call) => call[0] === 'toast'), [
        'toast',
        'Cannot resume: missing recorded run directory',
        'error',
    ]);
});
