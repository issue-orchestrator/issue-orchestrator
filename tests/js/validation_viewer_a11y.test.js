// JS-vm tests for canonical validation viewer accessibility
// (issue #6310 follow-up, Phase D).
//
// The viewer's accessibility story is two-stage:
//
//   1. Render-time: bake ARIA roles into the HTML so a screen reader
//      sees a real tree the moment the DOM is mounted.
//      ``role="tree"`` on .cvv-root, ``role="treeitem"`` on every
//      .cvv-row, ``role="group"`` on every .cvv-row-body, with
//      ``aria-expanded`` reflecting the <details> open state.
//
//   2. Post-mount enhancer (live DOM): aria-level / aria-setsize /
//      aria-posinset + roving tabindex + delegated keyboard nav.
//
// These JS-vm tests cover the render-time invariants on raw HTML
// strings.  The post-mount enhancer needs a real DOM (focus, keyboard
// dispatch, toggle events) and is covered by Playwright tests in
// tests/e2e_web/test_validation_viewer_a11y.py.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadViewer() {
    const baseStubs = {
        console,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    };
    const context = { ...baseStubs };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/validation_viewer.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'validation_viewer.js' });
    return context;
}

function _samplePayload() {
    return {
        status: 'failed',
        junit_cases: [
            {
                case_id: 'a',
                display_name: 'test_alpha',
                suite_name: 'tests/test_a.py',
                outcome: 'failed',
                failure_details: 'AssertionError\n  at frame 1',
                system_out: 'before',
                system_err: 'spooky',
                extras: [],
            },
            {
                case_id: 'b',
                display_name: 'test_beta',
                suite_name: 'tests/test_b.py',
                outcome: 'passed',
                duration_seconds: 0.003,
                extras: [],
            },
            {
                case_id: 'c',
                display_name: 'test_gamma',
                suite_name: 'tests/test_c.py',
                outcome: 'skipped',
                extras: [],
            },
        ],
        stdout_excerpt: ['line a'],
        stderr_excerpt: ['line b'],
    };
}

// ── Render-time ARIA roles ──────────────────────────────────────────────────

test('a11y: viewer root has role="tree" with aria-orientation=vertical and a label', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    assert.match(html, /<div class="cvv-root"[^>]*role="tree"/);
    assert.match(html, /aria-orientation="vertical"/);
    assert.match(html, /aria-label="Validation results"/);
});

test('a11y: every cvv-row has role="treeitem" with aria-expanded set', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    const detailsOpens = html.match(/<details[^>]*class="[^"]*cvv-row[^"]*"[^>]*>/g) || [];
    assert.ok(detailsOpens.length >= 5, `expected several cvv-row treeitems, got ${detailsOpens.length}`);
    for (const tag of detailsOpens) {
        assert.match(tag, /role="treeitem"/, `treeitem role missing on: ${tag}`);
        assert.match(tag, /aria-expanded="(true|false)"/, `aria-expanded missing on: ${tag}`);
    }
});

test('a11y: auto-open rows render with aria-expanded="true" matching the open attribute', () => {
    const ctx = loadViewer();
    // Multi-line failure_details so the traceback row renders (the
    // headline goes to a non-collapsible block; the rest of the body
    // is the auto-open traceback row).
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'x', display_name: 'fixture error', outcome: 'error',
            failure_details: 'TypeError\n  File "x.py", line 1',
            system_err: 'fixture stderr',
            extras: [],
        }],
    });
    // Traceback row auto-opens for failed/errored cases when there's a
    // body to show.
    const tracebackTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">traceback<\/span>)/);
    assert.ok(tracebackTag, 'traceback <details> tag not found');
    assert.match(tracebackTag[0], /aria-expanded="true"/);
    assert.match(tracebackTag[0], /\bopen\b/);

    // Stderr row auto-opens when outcome is error AND stderr is present.
    const stderrTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">stderr<\/span>)/);
    assert.ok(stderrTag, 'stderr <details> tag not found');
    assert.match(stderrTag[0], /aria-expanded="true"/);
    assert.match(stderrTag[0], /\bopen\b/);
});

test('a11y: collapsed rows render with aria-expanded="false" and no open attribute', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    // Pull out the Run stdout / Run stderr <details> opening tags and
    // assert they carry aria-expanded="false" + don't have the ``open``
    // boolean attribute.
    const runStdoutTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">Run stdout<\/span>)/);
    assert.ok(runStdoutTag, `Run stdout <details> tag not found in: ${html.slice(0, 200)}…`);
    assert.match(runStdoutTag[0], /aria-expanded="false"/);
    assert.doesNotMatch(runStdoutTag[0], /\bopen\b/);

    const runStderrTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">Run stderr<\/span>)/);
    assert.ok(runStderrTag, 'Run stderr <details> tag not found');
    assert.match(runStderrTag[0], /aria-expanded="false"/);
    assert.doesNotMatch(runStderrTag[0], /\bopen\b/);
});

test('a11y: every cvv-row-body has role="group"', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    const bodies = html.match(/<div class="cvv-row-body"[^>]*>/g) || [];
    assert.ok(bodies.length >= 1, 'expected at least one row body');
    for (const body of bodies) {
        assert.match(body, /role="group"/, `role=group missing on: ${body}`);
    }
});

test('a11y: browse-by-file expander aria-expanded reflects the open attribute', () => {
    // When there are no failures, the browse expander auto-opens — its
    // aria-expanded must read "true".  When there ARE failures, it
    // stays closed and aria-expanded="false".
    const ctx = loadViewer();
    const passedOnly = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{ case_id: 'a', display_name: 'a', outcome: 'passed', suite_name: 'tests/test_a.py', extras: [] }],
    });
    assert.match(passedOnly, /class="cvv-row cvv-row-browse"[^>]*aria-expanded="true"[^>]* open/);

    const mixed = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [
            { case_id: 'a', display_name: 'a', outcome: 'failed', failure_details: 'x', extras: [] },
            { case_id: 'b', display_name: 'b', outcome: 'passed', extras: [] },
        ],
    });
    assert.match(mixed, /class="cvv-row cvv-row-browse"[^>]*aria-expanded="false"/);
});

test('a11y: enhancer is exported as a symbol on the viewer module', () => {
    // Hosts (modal, drawer, E2E view) call
    // ``enhanceCanonicalValidationViewerAccessibility(root)`` after
    // mounting the HTML.  Missing the export would silently disable
    // keyboard nav.
    const ctx = loadViewer();
    assert.strictEqual(
        typeof ctx.enhanceCanonicalValidationViewerAccessibility,
        'function',
        'enhancer must be available on the viewer module',
    );
});
