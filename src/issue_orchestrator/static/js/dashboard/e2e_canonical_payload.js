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
// ``e2eRunToCanonicalPayload(runData) -> canonicalPayload`` is a pure
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
    const runId = _runIdFromRunData(data);

    const junitCases = [];
    const failedTests = [];

    // Failed categories (untriaged, has_issue, flaky) — each becomes a
    // JUnit case with outcome=failed.  Flaky/fixed retain their original
    // outcome category but render as failed in the canonical view if the
    // test failed in this run.
    for (const category of ['untriaged', 'has_issue', 'flaky']) {
        for (const test of categories[category] || []) {
            const junitCase = _testToJunitCase(test, 'failed', runId);
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
        junitCases.push(_testToJunitCase(test, 'passed', runId));
    }
    // ``passed`` — pure passes, no orchestrator context.
    for (const test of categories.passed || []) {
        junitCases.push(_testToJunitCase(test, 'passed', runId));
    }
    for (const test of categories.skipped || []) {
        junitCases.push(_testToJunitCase(test, 'skipped', runId));
    }
    // ``quarantined`` is an orthogonal marker, not a skipped outcome.
    // Preserve the result's actual outcome so the canonical viewer can
    // render quarantine separately without inflating skipped counts.
    for (const test of categories.quarantined || []) {
        junitCases.push(_testToJunitCase(
            Object.assign({}, test, { is_quarantined: true }),
            _outcomeFromTest(test, 'failed'),
            runId,
        ));
    }

    const failureCount = failedTests.length;
    const status = failureCount === 0 ? 'passed' : 'failed';

    return {
        status,
        junit_cases: junitCases,
        failed_tests: failedTests,
        // The orchestrator merges the worker subprocess's stdout and
        // stderr at capture time (e2e_runner.py sets
        // ``stderr=subprocess.STDOUT``), so there is only one
        // run-level channel: the worker's stdout. Surface its tail in
        // ``stdout_excerpt`` and leave ``stderr_excerpt`` empty so the
        // canonical viewer renders a single "Run stdout" disclosure
        // footer instead of an empty / misleading "Run stderr" row.
        stdout_excerpt: _runLogExcerptFromRunData(data),
        stderr_excerpt: [],
        // ``action_sections`` is the "Validation artifacts" footer in
        // the modal.  E2E artifacts are surfaced via the run-details
        // disclosure, not the canonical viewer's footer.
        action_sections: [],
    };
}

function _runLogExcerptFromRunData(data) {
    const excerpt = data && data.run && data.run.log_excerpt;
    if (!Array.isArray(excerpt)) return [];
    return excerpt.filter((line) => typeof line === 'string');
}

// ─── internals (exported so JS-vm tests can exercise them directly) ────────

function _runIdFromRunData(data) {
    const runId = Number(data && data.run && data.run.id);
    return Number.isInteger(runId) && runId > 0 ? runId : null;
}

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

function _testToJunitCase(test, outcome, runId) {
    const safe = (test && typeof test === 'object') ? test : {};
    const nodeId = String(safe.nodeid || '').trim();
    const displayName = String(safe.label || safe.display_name || _shortNameFromNodeId(nodeId) || nodeId);
    const suiteName = safe.suite_name ? String(safe.suite_name) : null;
    const duration = (typeof safe.duration_seconds === 'number' && Number.isFinite(safe.duration_seconds))
        ? safe.duration_seconds
        : null;
    // ``failure_details`` carries two different things depending on
    // outcome:
    //   - failed → traceback / one-liner (rendered as the failure
    //              headline/body)
    //   - skipped → JUnit ``<skipped message="..."/>`` reason text
    //              (rendered inline by ``_renderPassedTestRow``)
    // The JUnit parser stores both on ``failure_details``; preserve
    // the raw value for skipped tests so the viewer can surface the
    // skip reason.  Passed / errored tests have nothing to put here.
    let failureDetails = null;
    if (outcome === 'failed') {
        failureDetails = _failureDetailsFromTest(safe);
    } else if (outcome === 'skipped') {
        const raw = String(safe.failure_details || safe.skip_reason || '').trim();
        failureDetails = raw || null;
    }

    const junitCase = {
        case_id: nodeId || displayName,
        display_name: displayName,
        duration_seconds: duration,
        extras: _extrasForTest(safe, outcome),
        failure_details: failureDetails,
        is_quarantined: safe.is_quarantined === true,
        outcome,
        suite_name: suiteName,
        system_err: _optionalCapturedText(safe.system_err),
        system_out: _optionalCapturedText(safe.system_out),
    };
    const capturedOutputUrl = _capturedOutputUrlForTest(safe, runId);
    const capturedOutput = _capturedOutputAvailabilityForTest(safe);
    const hasCapturedOutputMetadata = _hasCapturedOutputAvailabilityMetadata(safe);
    if (hasCapturedOutputMetadata) {
        junitCase.captured_output = capturedOutput;
    }
    if (capturedOutputUrl && (capturedOutput.stdout_available || capturedOutput.stderr_available)) {
        junitCase.captured_output_url = capturedOutputUrl;
    }
    return junitCase;
}

function _outcomeFromTest(test, fallback) {
    const raw = String(test && test.outcome || '').trim().toLowerCase();
    if (raw === 'passed' || raw === 'failed' || raw === 'error' || raw === 'skipped') {
        return raw;
    }
    return fallback;
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
    //   {issue_number, issue_title, final_state, summary}
    // and degrades gracefully on missing fields.  The drawer-open
    // affordance routes through the shared lifecycle Command
    // dispatcher (``open_issue_timeline``), so the plugin only needs
    // ``issue_number`` — no URL field required.
    const issueNumber = Number(linkedIssue.number);
    const payload = { issue_number: issueNumber };
    if (linkedIssue.title) payload.issue_title = String(linkedIssue.title);
    if (linkedIssue.state) payload.final_state = String(linkedIssue.state);
    if (test && test.failure_summary) payload.summary = String(test.failure_summary);
    return payload;
}

function _shortNameFromNodeId(nodeId) {
    if (!nodeId) return '';
    const parts = String(nodeId).split('::');
    return parts.length > 0 ? parts[parts.length - 1] : '';
}

function _optionalCapturedText(value) {
    return typeof value === 'string' ? value : null;
}

function _capturedOutputUrlForTest(test, runId) {
    if (!Number.isInteger(runId) || runId <= 0) return null;
    const nodeId = String(test && (test.nodeid || test.case_id) || '').trim();
    if (!nodeId) return null;
    return `/api/e2e-run/${runId}/test-output?nodeid=${encodeURIComponent(nodeId)}`;
}

function _capturedOutputAvailabilityForTest(test) {
    const capturedOutput = test && test.captured_output && typeof test.captured_output === 'object'
        ? test.captured_output
        : {};
    return {
        stdout_available: capturedOutput.stdout_available === true,
        stderr_available: capturedOutput.stderr_available === true,
    };
}

function _hasCapturedOutputAvailabilityMetadata(test) {
    return !!(
        test
        && test.captured_output
        && typeof test.captured_output === 'object'
    );
}

// (Phase C originally introduced a ``flakinessChipForTest`` helper here
// that derived a "flaky · N/M" / "new failure" / "regression · N" chip
// from the test's prior-run history.  Reviewer Blocker 3 on PR #6319
// flagged it as dead — the helper was tested but the chip was never
// rendered.  Removed pending a real render path; the data is still on
// ``test.history`` and the chip semantics can be re-introduced when
// the rendering surface lands.  Tracked alongside the validation
// viewer redesign as a follow-up.)
