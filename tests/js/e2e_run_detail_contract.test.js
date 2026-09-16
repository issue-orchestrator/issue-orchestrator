// JS-vm tests for the single owner of "fetch one E2E run's detail
// payload" — ``_fetchE2ERunDetail`` in ``e2e_run_view.js`` (issue #6337).
//
// Both mount paths come through this helper: the runs-list row loader
// (``e2e_runs_list.js → loadE2ERunIntoRow``) and the timeline view
// switcher (``switchE2ETimelineView``).  Before #6337 each did its own
// ``response.json().catch(() => ({}))``, so a malformed success body
// became ``{}`` and rendered as an empty run panel — a payload bug that
// looked like "this run had no results".
//
// What's covered here:
//   - the request URL (each caller's run id + view reach the endpoint)
//   - a contract-valid body is returned unchanged
//   - a contract-violating body fails closed: throws, reports exactly
//     one diagnostic, and never returns a payload to render
//   - HTTP failures surface the server's message; error bodies are
//     deliberately outside the contract (the 200 is the only schema).

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
    captureContractViolations,
    resetContractViolationReporter,
    uiContractJson,
} = require('./ui_contract_test_support.js');

const E2E_RUN_VIEW_JS = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/e2e_run_view.js',
);

// A minimal payload that satisfies every required field of the
// generated ``E2ERunDetailPayload`` contract. Deliberately a static
// literal rather than something derived from the schema at test time:
// if the contract gains a required field, this fixture must be updated
// by hand, which is the signal that the browser boundary changed.
function _validRunDetail() {
    return {
        run: {
            id: 88,
            orchestrator_id: 'orch-1',
            started_at: '2026-05-12T10:00:00Z',
            finished_at: '2026-05-12T10:05:00Z',
            status: 'passed',
            exit_code: 0,
            duration_seconds: 300.0,
            pytest_args: [],
            command: ['pytest', 'tests/e2e'],
            runner_kind: 'pytest',
            commit_sha: 'abc1234',
            branch: 'main',
            log_path: '/tmp/run.log',
            log_excerpt: [],
            artifacts_dir: '/tmp/e2e-artifacts/run-88',
            total_tests: 0,
            current_test: '',
        },
        results_summary: {
            untriaged: 0, has_issue: 0, flaky: 0, fixed: 0,
            passed: 0, quarantined: 0, skipped: 0, total: 0,
        },
        results_by_category: {
            untriaged: [], has_issue: [], flaky: [], fixed: [],
            passed: [], quarantined: [], skipped: [],
        },
        artifacts: [],
        reports: [],
        artifact_diagnostic: { state: 'collected', collected_count: 0, configured_glob_count: 0 },
        issue_number: 12,
        title: 'A run',
        issue_url: 'https://example.invalid/issues/12',
        phase_toc: [],
        cycles: [],
        events: [],
        summary: { status: 'passed', last_event: '', event_count: 0 },
        actions: [],
        status_explanation: '',
        attempts: [],
        timeline_steps: [],
        attempt_count: 0,
        previous_runs: [],
        previous_runs_count: 0,
        raw_events_count: 0,
        blocked_detail: { reason: '', labels: [], rework_info: '', event_summary: '' },
        issue_affordances: [],
        lifecycle: {
            kind: 'dashboard',
            subject: { kind: 'dashboard', id: 'd', label: 'Dashboard' },
            current: {
                kind: 'dashboard_current',
                subject: { kind: 'dashboard', id: 'd', label: 'Dashboard' },
                issue_lifecycles: [],
                diagnostics: [],
            },
        },
    };
}

function _response(body, { ok = true, status = 200 } = {}) {
    return {
        ok,
        status,
        url: '/api/e2e-run-detail/88',
        text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    };
}

function _loadRunView(fetchImpl) {
    const fetchCalls = [];
    const context = {
        console,
        URLSearchParams,
        window: { dashboardData: { agents: [] } },
        uiContractJson,
        escapeHtml: (value) => String(value == null ? '' : value),
        escapeAttr: (value) => String(value == null ? '' : value),
        formatTimestamp: () => '',
        showToast: () => {},
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        fetch: async (url) => {
            fetchCalls.push(url);
            return fetchImpl(url);
        },
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(E2E_RUN_VIEW_JS, 'utf8'), context, {
        filename: 'e2e_run_view.js',
    });
    return { context, fetchCalls };
}

test.afterEach(() => resetContractViolationReporter());

test('run detail fetch targets the run + view the caller asked for', async () => {
    const { context, fetchCalls } = _loadRunView(async () => _response(_validRunDetail()));

    await context._fetchE2ERunDetail(88, 'user');

    assert.deepStrictEqual(fetchCalls, ['/api/e2e-run-detail/88?view=user']);
});

test('a contract-valid body is returned to the caller', async () => {
    const { context } = _loadRunView(async () => _response(_validRunDetail()));

    const payload = await context._fetchE2ERunDetail(88, 'user');

    assert.strictEqual(payload.run.id, 88);
    assert.strictEqual(payload.issue_number, 12);
});

test('a contract-violating body fails closed instead of reaching the renderer', async () => {
    // ``run.id`` as a string is exactly the silent-coercion class the
    // generated contract exists to reject: the old ``typeof payload
    // === 'object'`` check waved it through.
    const malformed = _validRunDetail();
    malformed.run.id = '88';
    const violations = captureContractViolations();
    const { context } = _loadRunView(async () => _response(malformed));

    await assert.rejects(
        () => context._fetchE2ERunDetail(88, 'user'),
        /E2ERunDetailPayload contract/,
    );
    assert.strictEqual(violations.length, 1, 'exactly one diagnostic per rejection');
    assert.strictEqual(violations[0].schemaName, 'E2ERunDetailPayload');
});

test('a body missing a required field fails closed rather than rendering a partial run', async () => {
    const malformed = _validRunDetail();
    delete malformed.results_summary;
    captureContractViolations();
    const { context } = _loadRunView(async () => _response(malformed));

    await assert.rejects(
        () => context._fetchE2ERunDetail(88, 'user'),
        /E2ERunDetailPayload contract/,
    );
});

test('a forbidden extra field fails closed', async () => {
    const malformed = _validRunDetail();
    malformed.unexpected_field = 'nope';
    captureContractViolations();
    const { context } = _loadRunView(async () => _response(malformed));

    await assert.rejects(
        () => context._fetchE2ERunDetail(88, 'user'),
        /E2ERunDetailPayload contract/,
    );
});

test('an empty body no longer degrades to an empty run panel', async () => {
    // The pre-#6337 ``.catch(() => ({}))`` turned this into ``{}`` and
    // rendered a run with no results.
    captureContractViolations();
    const { context } = _loadRunView(async () => _response('', { ok: true }));

    await assert.rejects(() => context._fetchE2ERunDetail(88, 'user'));
});

test('an HTTP failure surfaces the server-provided message', async () => {
    const { context } = _loadRunView(async () =>
        _response({ error: 'run 88 is gone' }, { ok: false, status: 404 }));

    await assert.rejects(
        () => context._fetchE2ERunDetail(88, 'user'),
        /run 88 is gone/,
    );
});

test('an HTTP failure with a non-JSON body falls back to the status', async () => {
    const { context } = _loadRunView(async () =>
        _response('<html>502 Bad Gateway</html>', { ok: false, status: 502 }));

    await assert.rejects(() => context._fetchE2ERunDetail(88, 'user'), /HTTP 502/);
});
