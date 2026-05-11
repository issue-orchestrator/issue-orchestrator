// JS-vm tests for the canonical validation viewer (issue #6310 follow-up,
// Phase A).  Exercises:
//
//   1. ``registerValidationPlugin`` registry semantics — register,
//      lookup, unknown namespace silently skips, misbehaving renderer
//      doesn't crash the viewer.
//   2. ``renderCanonicalValidationViewer`` output for the four canonical
//      cases: passed run, failed run with triage cards, mixed
//      pass/fail, empty.
//   3. Browse-by-file expansion of non-failed cases, with per-test
//      stdout rendering.
//   4. Plugin extras: when a case carries a registered namespace, the
//      plugin renderer's HTML appears inside the test's expansion.
//
// All tests run in a node:vm context with the viewer source loaded,
// matching the existing ``tests/js/e2e_run_view_actions.test.js``
// pattern.  No DOM library — we assert on rendered HTML strings.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadViewer(overrides = {}) {
    // Stubs for the shared dashboard primitives the viewer calls into.
    const baseStubs = {
        console,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        // Minimal stub: the viewer calls renderValidationFailureActionSections
        // when ``data.action_sections`` is populated.  We don't exercise
        // action_sections in these tests, so a no-op stub is fine.
        renderValidationFailureActionSections: (sections) =>
            `<div data-test-action-sections-count="${sections.length}"></div>`,
    };
    const context = { ...baseStubs, ...overrides };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/validation_viewer.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'validation_viewer.js' });
    return context;
}

function loadViewerWithAgentPlugin(overrides = {}) {
    const ctx = loadViewer(overrides);
    const pluginSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/plugins/agent_context.js'),
        'utf8',
    );
    vm.runInContext(pluginSource, ctx, { filename: 'plugins/agent_context.js' });
    return ctx;
}

// ── Registry tests ────────────────────────────────────────────────────────

test('registry: registerValidationPlugin rejects empty namespace', () => {
    const ctx = loadViewer();
    assert.throws(() => ctx.registerValidationPlugin('', () => ''), /namespace/);
    assert.throws(() => ctx.registerValidationPlugin(null, () => ''), /namespace/);
});

test('registry: registerValidationPlugin rejects non-function renderer', () => {
    const ctx = loadViewer();
    assert.throws(() => ctx.registerValidationPlugin('test.foo', null), /renderer/);
    assert.throws(() => ctx.registerValidationPlugin('test.foo', 'not a function'), /renderer/);
});

test('registry: register + getValidationPlugin round-trips', () => {
    const ctx = loadViewer();
    const renderer = (payload) => `[rendered ${payload.x}]`;
    ctx.registerValidationPlugin('test.echo', renderer);
    assert.strictEqual(ctx.getValidationPlugin('test.echo'), renderer);
    assert.strictEqual(ctx.getValidationPlugin('test.missing'), null);
});

test('registry: renderPluginExtras dispatches by namespace', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.echo', (p) => `<div data-x="${p.x}">echo</div>`);
    const html = ctx.renderPluginExtras({
        extras: [{ namespace: 'test.echo', payload: { x: 7 } }],
    });
    assert.match(html, /data-x="7"/);
});

test('registry: unknown namespace is silently skipped', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.echo', () => '<div>echo</div>');
    const html = ctx.renderPluginExtras({
        extras: [
            { namespace: 'test.unknown', payload: {} },
            { namespace: 'test.echo', payload: {} },
        ],
    });
    assert.match(html, /echo/);
    assert.doesNotMatch(html, /unknown/);
});

test('registry: misbehaving plugin renders an inline error, not a crash', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.crash', () => { throw new Error('boom'); });
    const html = ctx.renderPluginExtras({
        extras: [{ namespace: 'test.crash', payload: {} }],
    });
    assert.match(html, /Plugin <code>test\.crash<\/code> failed to render: boom/);
});

test('registry: extras with non-array shape is treated as empty', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx.renderPluginExtras({}), '');
    assert.strictEqual(ctx.renderPluginExtras({ extras: null }), '');
    assert.strictEqual(ctx.renderPluginExtras({ extras: 'not an array' }), '');
});

// ── Canonical viewer tests ────────────────────────────────────────────────

test('viewer: passed run renders the browse-by-file expander and no triage cards', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 'a', display_name: 'test a', outcome: 'passed', duration_seconds: 0.003, suite_name: 'tests/test_a.py', extras: [] },
            { case_id: 'b', display_name: 'test b', outcome: 'passed', duration_seconds: 0.004, suite_name: 'tests/test_b.py', extras: [] },
        ],
    });
    assert.doesNotMatch(html, /cvv-triage-card/);
    assert.match(html, /cvv-row-browse/);
    assert.match(html, /2 passed/);
    // Each file rendered as its own expander
    assert.match(html, /test_a\.py/);
    assert.match(html, /test_b\.py/);
});

test('viewer: failed run renders one triage card per failed/errored test', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [
            {
                case_id: 'a', display_name: 'broken assertion', outcome: 'failed',
                duration_seconds: 0.012, suite_name: 'tests/test_a.py',
                failure_details: 'AssertionError: expected x to equal y\n  at tests/test_a.py:42',
                extras: [],
            },
            {
                case_id: 'b', display_name: 'fixture exploded', outcome: 'error',
                duration_seconds: 0.001, suite_name: 'tests/test_b.py',
                failure_details: "TypeError: cannot read 'init' of undefined",
                extras: [],
            },
            { case_id: 'c', display_name: 'still works', outcome: 'passed', duration_seconds: 0.003, suite_name: 'tests/test_c.py', extras: [] },
        ],
    });
    // Two triage cards
    const triageCount = (html.match(/cvv-triage-card/g) || []).length;
    assert.ok(triageCount >= 2, `expected at least 2 triage cards, got ${triageCount}`);
    // Headline content for the failed test
    assert.match(html, /AssertionError: expected x to equal y/);
    // Headline content for the errored test
    assert.match(html, /TypeError: cannot read/);
    // Failed gets is-failed class; errored gets is-error
    assert.match(html, /cvv-headline is-failed/);
    assert.match(html, /cvv-headline is-error/);
    // Passed cases still browseable via browse expander
    assert.match(html, /cvv-row-browse/);
    assert.match(html, /1 passed/);
});

test('viewer: triage cards render stdout and stderr expanders', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'with output', outcome: 'failed',
            duration_seconds: 0.01, suite_name: 'tests/test_a.py',
            failure_details: 'AssertionError: nope',
            system_out: 'before assertion',
            system_err: 'WARNING: spooky',
            extras: [],
        }],
    });
    assert.match(html, /before assertion/);
    assert.match(html, /WARNING: spooky/);
});

test('viewer: errored case auto-opens its stderr expander', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'fixture error', outcome: 'error',
            failure_details: 'TypeError',
            system_err: 'fixture failure stderr',
            extras: [],
        }],
    });
    // The viewer marks the stderr row open=true when outcome is error
    // and stderr is present.  We match the row opener with the stderr
    // pre-block immediately following.
    assert.match(html, /<details class="cvv-row" open><summary><span class="cvv-caret">▸<\/span><span class="cvv-title">stderr<\/span>/);
});

test('viewer: empty payload renders cleanly with no triage and no browse', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({ junit_cases: [], status: 'passed' });
    assert.doesNotMatch(html, /cvv-triage-card/);
    assert.doesNotMatch(html, /cvv-row-browse/);
});

test('viewer: skipped tests appear in browse with skipped chip', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 'a', display_name: 'experimental', outcome: 'skipped', suite_name: 'tests/test_a.py', extras: [] },
        ],
    });
    assert.match(html, /1 skipped/);
    assert.match(html, /cvv-ico-skipped/);
});

test('viewer: legacy stdout_excerpt and stderr_excerpt appear in collapsed run-output expanders', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [],
        stdout_excerpt: ['line 1', 'line 2'],
        stderr_excerpt: ['err line'],
    });
    assert.match(html, /Run stdout/);
    assert.match(html, /Run stderr/);
    assert.match(html, /line 1\nline 2/);
});

test('viewer: action_sections render under a Validation artifacts expander', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [],
        action_sections: [{ title: 'Validation Artifacts', actions: [{ type: 'open_path', label: 'Open Record' }] }],
    });
    assert.match(html, /Validation artifacts/);
    assert.match(html, /data-test-action-sections-count="1"/);
});

// ── Plugin integration ────────────────────────────────────────────────────

test('plugin: agent-context plugin renders when case carries the namespace', () => {
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'e2e drove issue', outcome: 'failed',
            failure_details: 'AssertionError: not completed',
            extras: [{
                namespace: 'io.agent-context',
                payload: {
                    issue_number: 4503,
                    issue_title: 'fixture cohort split',
                    final_state: 'blocked',
                    summary: 'agent retried 2x then blocked on validation',
                    run_url: '/api/dashboard/issue/4503?focus=timeline',
                },
            }],
        }],
    });
    assert.match(html, /Linked issue · driven by orchestrator/);
    assert.match(html, /#4503/);
    assert.match(html, /fixture cohort split/);
    assert.match(html, /blocked/);
    assert.match(html, /Open issue drawer/);
});

test('plugin: agent-context plugin renders nothing when case lacks the namespace', () => {
    // Generic JUnit consumers don't populate ``extras``.  The plugin is
    // loaded but never invoked.  This is the tixmeup-style scenario.
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'generic failure', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [],
        }],
    });
    assert.doesNotMatch(html, /agent-context/);
    assert.doesNotMatch(html, /Linked issue/);
});

test('plugin: agent-context rejects malformed payload (no issue_number)', () => {
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'malformed extras', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [{ namespace: 'io.agent-context', payload: { /* missing issue_number */ } }],
        }],
    });
    assert.doesNotMatch(html, /Linked issue/);
});
