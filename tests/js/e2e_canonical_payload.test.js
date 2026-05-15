// JS-vm tests for the E2E run-data → canonical-viewer payload translator
// (Phase C of issue #6310 follow-up).
//
// The translator is the seam where the orchestrator's per-category
// shape (untriaged / has_issue / flaky / fixed / passed / quarantined /
// skipped) flattens onto JUnit-canonical outcomes (passed / failed /
// skipped).  Linked-issue context survives via per-case
// ``extras: [{namespace: 'io.agent-context', payload: ...}]``.
//
// These tests cover the translator + the flakiness-chip helper.  Both
// are pure functions; no DOM, no fetch, no globals.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadModule() {
    const context = { console };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_canonical_payload.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'e2e_canonical_payload.js' });
    return context;
}

// ── e2eRunToCanonicalPayload ───────────────────────────────────────────────

test('translator: empty run yields a passed payload with no cases', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({});
    assert.strictEqual(out.status, 'passed');
    assert.deepEqual(out.junit_cases, []);
    assert.deepEqual(out.failed_tests, []);
    assert.deepEqual(out.stdout_excerpt, []);
    assert.deepEqual(out.stderr_excerpt, []);
    assert.deepEqual(out.action_sections, []);
});

test('translator: pure-pass run produces only passed cases and status=passed', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            passed: [
                { nodeid: 'tests/test_a.py::test_one', suite_name: 'tests/test_a.py', outcome: 'passed', duration_seconds: 0.01 },
                { nodeid: 'tests/test_b.py::test_two', suite_name: 'tests/test_b.py', outcome: 'passed', duration_seconds: 0.02 },
            ],
        },
    });
    assert.strictEqual(out.status, 'passed');
    assert.strictEqual(out.junit_cases.length, 2);
    assert.strictEqual(out.failed_tests.length, 0);
    for (const c of out.junit_cases) {
        assert.strictEqual(c.outcome, 'passed');
        assert.deepEqual(c.extras, []);
        assert.strictEqual(c.failure_details, null);
    }
});

test('translator: JUnit-sourced cases carry lazy captured-output URLs', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        run: { id: 88 },
        results_by_category: {
            passed: [{
                nodeid: 'tests/e2e/test_chatty.py::test_output',
                suite_name: 'tests/e2e/test_chatty.py',
                result_source: 'junit_xml',
                outcome: 'passed',
                captured_output: {
                    stdout_available: true,
                    stderr_available: false,
                },
            }],
        },
    });
    assert.strictEqual(
        out.junit_cases[0].captured_output_url,
        '/api/e2e-run/88/test-output?nodeid=tests%2Fe2e%2Ftest_chatty.py%3A%3Atest_output',
    );
    assert.deepEqual(out.junit_cases[0].captured_output, {
        stdout_available: true,
        stderr_available: false,
    });
    assert.strictEqual(out.junit_cases[0].system_out, null);
    assert.strictEqual(out.junit_cases[0].system_err, null);
});

test('translator: cases without captured-output availability do not claim lazy captured output', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        run: { id: 88 },
        results_by_category: {
            passed: [{
                nodeid: 'tests/e2e/test_quiet.py::test_output',
                result_source: 'junit_xml',
                outcome: 'passed',
            }],
        },
    });
    assert.strictEqual(out.junit_cases[0].captured_output_url, undefined);
    assert.strictEqual(out.junit_cases[0].captured_output, undefined);
});

test('translator: unavailable captured-output metadata survives without lazy URL', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        run: { id: 88 },
        results_by_category: {
            skipped: [{
                nodeid: 'tests/e2e/test_quiet.py::test_skipped',
                result_source: 'junit_xml',
                outcome: 'skipped',
                captured_output: {
                    stdout_available: false,
                    stderr_available: false,
                },
            }],
        },
    });
    assert.strictEqual(out.junit_cases[0].captured_output_url, undefined);
    assert.deepEqual(out.junit_cases[0].captured_output, {
        stdout_available: false,
        stderr_available: false,
    });
});

test('translator: untriaged failure → failed outcome, no extras', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'tests/test_a.py::test_breaks',
                suite_name: 'tests/test_a.py',
                outcome: 'failed',
                duration_seconds: 0.42,
                failure_summary: 'AssertionError: bad',
                longrepr: 'AssertionError: bad\n  File "test_a.py", line 7',
            }],
        },
    });
    assert.strictEqual(out.status, 'failed');
    assert.strictEqual(out.junit_cases.length, 1);
    const c = out.junit_cases[0];
    assert.strictEqual(c.outcome, 'failed');
    assert.strictEqual(c.case_id, 'tests/test_a.py::test_breaks');
    assert.strictEqual(c.suite_name, 'tests/test_a.py');
    assert.match(c.failure_details, /AssertionError: bad/);
    assert.match(c.failure_details, /File "test_a.py"/);
    assert.deepEqual(c.extras, []);
    assert.deepEqual(out.failed_tests, ['tests/test_a.py::test_breaks']);
});

test('translator: has_issue failure → io.agent-context extra carries the issue number', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            has_issue: [{
                nodeid: 'tests/test_a.py::test_linked',
                suite_name: 'tests/test_a.py',
                outcome: 'failed',
                duration_seconds: 30,
                failure_summary: 'TimeoutError: orchestrator did not publish within 30s',
                existing_issue: { number: 4503, title: 'orchestrator publish flake', state: 'open' },
            }],
        },
    });
    assert.strictEqual(out.status, 'failed');
    assert.strictEqual(out.junit_cases.length, 1);
    const c = out.junit_cases[0];
    assert.strictEqual(c.outcome, 'failed');
    assert.strictEqual(c.extras.length, 1);
    const extra = c.extras[0];
    assert.strictEqual(extra.namespace, 'io.agent-context');
    assert.strictEqual(extra.payload.issue_number, 4503);
    assert.strictEqual(extra.payload.issue_title, 'orchestrator publish flake');
    assert.strictEqual(extra.payload.final_state, 'open');
    assert.strictEqual(extra.payload.summary, 'TimeoutError: orchestrator did not publish within 30s');
    // The drawer-open affordance routes through the typed Command
    // dispatcher (``open_issue_timeline``), so the translator no
    // longer emits a ``run_url`` (PR #6319 Blocker 1).
    assert.strictEqual(extra.payload.run_url, undefined);
});

test('translator: flaky failure → still failed in canonical viewer (rendered as failed in *this* run)', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            flaky: [{
                nodeid: 'tests/test_a.py::test_flaky',
                outcome: 'failed',
                existing_issue: { number: 9001 },
            }],
        },
    });
    assert.strictEqual(out.status, 'failed');
    assert.strictEqual(out.junit_cases[0].outcome, 'failed');
    // Flaky tests carry an existing issue → plugin extra still populated.
    assert.strictEqual(out.junit_cases[0].extras[0].namespace, 'io.agent-context');
});

test('translator: fixed test → outcome=passed, but linked-issue extra survives so plugin can offer close-issue (future)', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            fixed: [{
                nodeid: 'tests/test_a.py::test_was_broken',
                outcome: 'passed',
                existing_issue: { number: 4488, state: 'open' },
            }],
        },
    });
    assert.strictEqual(out.status, 'passed');  // no failures this run
    assert.strictEqual(out.junit_cases[0].outcome, 'passed');
    assert.strictEqual(out.junit_cases[0].extras[0].namespace, 'io.agent-context');
});

test('translator: skipped + quarantined both map to outcome=skipped', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            skipped: [{ nodeid: 'tests/test_a.py::test_pending', outcome: 'skipped' }],
            quarantined: [{ nodeid: 'tests/test_a.py::test_held_out', is_quarantined: true }],
        },
    });
    assert.strictEqual(out.status, 'passed');  // no failures
    assert.strictEqual(out.junit_cases.length, 2);
    for (const c of out.junit_cases) assert.strictEqual(c.outcome, 'skipped');
});

test('translator: failed_tests list mirrors the failing node IDs across all failure categories', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{ nodeid: 'a::1', outcome: 'failed' }],
            has_issue: [{ nodeid: 'b::2', outcome: 'failed', existing_issue: { number: 1 } }],
            flaky: [{ nodeid: 'c::3', outcome: 'failed', existing_issue: { number: 2 } }],
            passed: [{ nodeid: 'd::4', outcome: 'passed' }],
        },
    });
    assert.deepEqual(out.failed_tests.sort(), ['a::1', 'b::2', 'c::3']);
});

test('translator: display_name falls back to label, display_name, then short node-id chunk', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            passed: [
                { nodeid: 'tests/test_a.py::test_one', label: 'pretty one' },
                { nodeid: 'tests/test_a.py::test_two', display_name: 'pretty two' },
                { nodeid: 'tests/test_a.py::test_three' },
            ],
        },
    });
    assert.strictEqual(out.junit_cases[0].display_name, 'pretty one');
    assert.strictEqual(out.junit_cases[1].display_name, 'pretty two');
    assert.strictEqual(out.junit_cases[2].display_name, 'test_three');
});

test('translator: longrepr beats failure_summary when both present', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'a::1',
                outcome: 'failed',
                failure_summary: 'short',
                longrepr: 'short\n  more',
            }],
        },
    });
    assert.strictEqual(out.junit_cases[0].failure_details, 'short\n  more');
});

test('translator: failure with only failure_summary (no longrepr) → single-line failure_details', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'a::1',
                outcome: 'failed',
                failure_summary: 'TimeoutError: oops',
            }],
        },
    });
    // No newline → triggers 3a inline-headline layout downstream.
    assert.strictEqual(out.junit_cases[0].failure_details, 'TimeoutError: oops');
    assert.ok(!out.junit_cases[0].failure_details.includes('\n'));
});

test('translator: linked issue with non-numeric .number is ignored (no plugin extra)', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            has_issue: [{
                nodeid: 'a::1', outcome: 'failed',
                existing_issue: { number: 'not-a-number' },
            }],
        },
    });
    assert.deepEqual(out.junit_cases[0].extras, []);
});

test('translator: malformed results_by_category degrades to empty payload', () => {
    const ctx = loadModule();
    assert.strictEqual(ctx.e2eRunToCanonicalPayload(null).junit_cases.length, 0);
    assert.strictEqual(ctx.e2eRunToCanonicalPayload({ results_by_category: 'oops' }).junit_cases.length, 0);
    assert.strictEqual(ctx.e2eRunToCanonicalPayload({ results_by_category: { passed: 'oops' } }).junit_cases.length, 0);
});

test('translator: run.log_excerpt populates stdout_excerpt (single merged channel)', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        run: {
            id: 7,
            log_excerpt: ['line 1', 'line 2', 'line 3'],
        },
        results_by_category: { passed: [] },
    });
    assert.deepEqual(out.stdout_excerpt, ['line 1', 'line 2', 'line 3']);
    assert.deepEqual(out.stderr_excerpt, []);
});

test('translator: missing or malformed log_excerpt yields empty arrays', () => {
    const ctx = loadModule();
    for (const run of [undefined, null, {}, { log_excerpt: null }, { log_excerpt: 'oops' }]) {
        const out = ctx.e2eRunToCanonicalPayload({ run, results_by_category: { passed: [] } });
        assert.deepEqual(out.stdout_excerpt, []);
        assert.deepEqual(out.stderr_excerpt, []);
    }
});

test('translator: log_excerpt drops non-string entries', () => {
    const ctx = loadModule();
    const out = ctx.e2eRunToCanonicalPayload({
        run: { id: 7, log_excerpt: ['ok', 42, null, 'also ok'] },
        results_by_category: { passed: [] },
    });
    assert.deepEqual(out.stdout_excerpt, ['ok', 'also ok']);
});

// ``flakinessChipForTest`` tests removed alongside the helper itself
// (PR #6319 Blocker 3) — it was added without a render path and
// therefore had no shipped behavior to verify.  Re-introduce both
// helper and tests when the rendering surface lands.
