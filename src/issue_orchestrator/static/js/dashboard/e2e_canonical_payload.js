// Translator: E2E run-detail payload → canonical-validation-viewer payload.
//
// Phase C of the validation viewer redesign (issue #6310 follow-up).  The
// canonical viewer (used today by the validation modal and the per-issue
// drawer's inline expansion) wants a flat JUnit-shaped payload:
// ``{junit_cases: [{case_id, display_name, outcome, ...}], failed_tests:
// [nodeid, ...], stdout_excerpt, stderr_excerpt, action_sections, status}``.
//
// The E2E run-detail endpoint returns a different shape:
// ``{results_by_category: {untriaged, has_issue, flaky, fixed, passed,
// quarantined, skipped}, lifecycle, issue_affordances, run, artifacts, ...}``.
//
// ``e2eRunToCanonicalPayload(runData) → canonicalPayload`` is a pure
// function that converts the former to the latter.  It is the single seam
// where orchestrator-workflow categories (untriaged / has_issue / flaky /
// fixed / quarantined) collapse onto JUnit-canonical outcomes (passed /
// failed / skipped).  Per-test orchestrator context (linked issue,
// final state) is preserved by populating
// ``junit_cases[i].extras: [{namespace: 'io.agent-context', payload: ...}]``
// — picked up at render time by the registered plugin.
//
// The function is intentionally pure (no DOM, no fetch, no globals) so
// it can be JS-vm-tested without a browser.  It is loaded as a plain
// <script> in the dashboard bundle; the symbol
// ``e2eRunToCanonicalPayload`` is reachable from e2e_run_view.js at call
// time the same way every other dashboard helper resolves.

// ─── public entry point ────────────────────────────────────────────────────

function e2eRunToCanonicalPayload(runData) {
    const data = (runData && typeof runData === 'object') ? runData : {};
    const categories = _categoriesFromRunData(data);

    const junitCases = [];
    const failedTests = [];

    // Failed categories (untriaged, has_issue, flaky) — each becomes a
    // JUnit case with outcome=failed.  Flaky/fixed retain their original
    // outcome category but render as failed in the canonical view if the
    // test failed in this run.
    for (const category of ['untriaged', 'has_issue', 'flaky']) {
        for (const test of categories[category] || []) {
            const junitCase = _testToJunitCase(test, 'failed');
            junitCases.push(junitCase);
            const nodeId = String(test && test.nodeid || '').trim();
            if (nodeId) failedTests.push(nodeId);
        }
    }
    // ``fixed`` category: the test passed THIS run but was previously
    // failing (linked issue can now be closed).  Render as passed in the
    // canonical viewer — the "previously failing" story is in the
    // ``existing_issue`` linkage carried via ``extras``.
    for (const test of categories.fixed || []) {
        junitCases.push(_testToJunitCase(test, 'passed'));
    }
    // ``passed`` — pure passes, no orchestrator context.
    for (const test of categories.passed || []) {
        junitCases.push(_testToJunitCase(test, 'passed'));
    }
    // ``skipped`` and ``quarantined`` both map to outcome=skipped.
    // Quarantined tests get a small marker in ``extras`` so the future
    // plugin (or the canonical viewer's badge logic) can label them —
    // but for Phase C scope we just preserve the data path.  Actual
    // quarantine UI is deferred per issue #6318.
    for (const test of categories.skipped || []) {
        junitCases.push(_testToJunitCase(test, 'skipped'));
    }
    for (const test of categories.quarantined || []) {
        junitCases.push(_testToJunitCase(test, 'skipped'));
    }

    const failureCount = failedTests.length;
    const status = failureCount === 0 ? 'passed' : 'failed';

    return {
        status,
        junit_cases: junitCases,
        failed_tests: failedTests,
        // E2E payloads don't carry run-level stdout/stderr the way the
        // validation modal does, so leave these empty.  The canonical
        // viewer renders no "Run stdout/stderr" footer when empty.
        stdout_excerpt: [],
        stderr_excerpt: [],
        // ``action_sections`` is the "Validation artifacts" footer in
        // the modal.  E2E artifacts are surfaced via the run-details
        // disclosure, not the canonical viewer's footer.
        action_sections: [],
    };
}

// ─── internals (exported so JS-vm tests can exercise them directly) ────────

function _categoriesFromRunData(data) {
    const payload = (data && data.results_by_category && typeof data.results_by_category === 'object')
        ? data.results_by_category
        : {};
    return {
        untriaged: Array.isArray(payload.untriaged) ? payload.untriaged : [],
        has_issue: Array.isArray(payload.has_issue) ? payload.has_issue : [],
        flaky: Array.isArray(payload.flaky) ? payload.flaky : [],
        fixed: Array.isArray(payload.fixed) ? payload.fixed : [],
        passed: Array.isArray(payload.passed) ? payload.passed : [],
        quarantined: Array.isArray(payload.quarantined) ? payload.quarantined : [],
        skipped: Array.isArray(payload.skipped) ? payload.skipped : [],
    };
}

function _testToJunitCase(test, outcome) {
    const safe = (test && typeof test === 'object') ? test : {};
    const nodeId = String(safe.nodeid || '').trim();
    const displayName = String(safe.label || safe.display_name || _shortNameFromNodeId(nodeId) || nodeId);
    const suiteName = safe.suite_name ? String(safe.suite_name) : null;
    const duration = (typeof safe.duration_seconds === 'number' && Number.isFinite(safe.duration_seconds))
        ? safe.duration_seconds
        : null;
    const failureDetails = (outcome === 'failed')
        ? _failureDetailsFromTest(safe)
        : null;

    return {
        case_id: nodeId || displayName,
        display_name: displayName,
        duration_seconds: duration,
        extras: _extrasForTest(safe, outcome),
        failure_details: failureDetails,
        outcome,
        suite_name: suiteName,
        system_err: null,
        system_out: null,
    };
}

function _failureDetailsFromTest(test) {
    // ``longrepr`` is the full traceback when available; ``failure_summary``
    // is the short one-liner.  The canonical viewer's
    // ``_splitFailureDetails`` splits on the first newline, so a
    // multi-line ``longrepr`` produces a headline + traceback body and a
    // single-line ``failure_summary`` produces headline only — which the
    // 3a layout selector renders inline.
    const longrepr = String(test.longrepr || '').trim();
    if (longrepr) return longrepr;
    return String(test.failure_summary || '').trim() || null;
}

function _extrasForTest(test, outcome) {
    const extras = [];

    // Linked-issue plugin entry: only for tests whose backend already
    // populated ``existing_issue.number``.  This is what makes the
    // io.agent-context plugin render under that test row in the
    // canonical viewer.  Untriaged failures and pure passes have no
    // linked issue and therefore no plugin block.
    const linkedIssue = test && test.existing_issue;
    if (linkedIssue && Number.isFinite(Number(linkedIssue.number))) {
        extras.push({
            namespace: 'io.agent-context',
            payload: _agentContextPayload(test, linkedIssue),
        });
    }

    return extras;
}

function _agentContextPayload(test, linkedIssue) {
    // The Phase-A plugin renderer reads
    //   {issue_number, issue_title, final_state, summary, run_url}
    // and degrades gracefully on missing fields.  We populate what the
    // E2E run-detail endpoint actually carries; the rest is omitted.
    const issueNumber = Number(linkedIssue.number);
    const payload = { issue_number: issueNumber };

    if (linkedIssue.title) payload.issue_title = String(linkedIssue.title);
    if (linkedIssue.state) payload.final_state = String(linkedIssue.state);
    if (test && test.failure_summary) payload.summary = String(test.failure_summary);
    // run_url points the "Open issue drawer" button at the per-issue
    // route.  The dashboard's drawer opens directly from this URL.
    payload.run_url = `/api/dashboard/issue/${issueNumber}`;
    return payload;
}

function _shortNameFromNodeId(nodeId) {
    if (!nodeId) return '';
    const parts = String(nodeId).split('::');
    return parts.length > 0 ? parts[parts.length - 1] : '';
}

// ─── flaky-history chip (Phase C: surface failing frequency at scan time) ──
//
// Pure helper that derives the small chip we render next to the
// failed-test summary line.  Three flavors:
//   * `flaky · N/M`     — the test failed in N of the last M runs (M ≥ 2).
//   * `new failure`     — failed for the first time (no prior history or
//                          all prior history is passing).
//   * `regression · N`  — failed every one of the last N runs (N ≥ 2).
// Returns null when the test isn't a failure or when there's no
// historical signal worth showing.
//
// Lives in this module so the chip semantics are JS-vm-tested without
// touching the renderer.  The renderer just asks for the chip and
// places it in the meta row.

function flakinessChipForTest(test) {
    if (!test || typeof test !== 'object') return null;
    const outcome = String(test.retry_outcome || test.outcome || '').toLowerCase();
    if (outcome !== 'failed' && outcome !== 'error') return null;
    const history = Array.isArray(test.history) ? test.history : [];
    if (history.length === 0) {
        return { kind: 'new', label: 'new failure', title: 'first time this test has failed' };
    }
    const recent = history.slice(0, 5);
    const fails = recent.filter((h) => {
        const o = String(h && (h.outcome || h.retry_outcome) || '').toLowerCase();
        return o === 'failed' || o === 'error';
    }).length;
    if (recent.length >= 2 && fails === recent.length) {
        return {
            kind: 'regression',
            label: `regression · ${fails}`,
            title: `failed every one of the last ${fails} runs`,
        };
    }
    if (fails >= 1 && fails < recent.length) {
        return {
            kind: 'flaky',
            label: `flaky · ${fails}/${recent.length}`,
            title: `failed in ${fails} of the last ${recent.length} runs`,
        };
    }
    // failing this run, but all of the last N were green — first failure
    // in the window.  Same UX as "new failure".
    return { kind: 'new', label: 'new failure', title: 'first time this test has failed in the recent window' };
}
