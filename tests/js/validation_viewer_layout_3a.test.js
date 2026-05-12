// JS-vm tests for the Phase C / 3a failure-card layout selector and
// the built-in Copy-error icon.
//
// Both pieces live in validation_viewer.js.  They are pure (selector)
// and DOM-only (copy handler), no plugin coupling.
//
// Layout selector contract:
//   _failureCardLayoutForCase(testCase) →
//     { variant: 'inline' | 'two-row' | 'none', headlineMessage, tracebackBody }
//
//     * 'inline'   — failure_details has a headline, no traceback body.
//                    Renderer puts the headline next to the test name.
//     * 'two-row'  — failure_details has a headline AND a body.
//                    Renderer keeps the red-headline box + traceback row.
//     * 'none'     — failure_details is empty/blank.  No headline anywhere.
//
// Copy-error icon contract:
//   Every triage card with non-empty failure_details renders a
//   ``.cvv-copy-icon`` button whose ``data-cvv-copy-text`` carries the
//   full failure text.  Click handler writes to clipboard.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadViewer(overrides = {}) {
    const baseStubs = {
        console,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
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

// ── layout selector ───────────────────────────────────────────────────────

test('layout: single-line failure_details → inline variant', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({
        failure_details: 'TimeoutError: orchestrator did not publish within 30s',
    });
    assert.strictEqual(out.variant, 'inline');
    assert.match(out.headlineMessage, /TimeoutError/);
    assert.strictEqual(out.tracebackBody, '');
});

test('layout: multi-line failure_details → two-row variant', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({
        failure_details: 'AssertionError: bad\n  File "x.py", line 7\n  in test_foo',
    });
    assert.strictEqual(out.variant, 'two-row');
    assert.strictEqual(out.headlineMessage, 'AssertionError: bad');
    assert.match(out.tracebackBody, /File "x.py"/);
});

test('layout: empty failure_details → variant=none', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({ failure_details: '' });
    assert.strictEqual(out.variant, 'none');
    assert.strictEqual(out.headlineMessage, '');
    assert.strictEqual(out.tracebackBody, '');
});

test('layout: missing failure_details → variant=none', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({});
    assert.strictEqual(out.variant, 'none');
});

test('layout: failure_details with only blank lines → variant=none', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({ failure_details: '\n\n   \n' });
    assert.strictEqual(out.variant, 'none');
});

test('layout: trailing newline after headline → still inline (no body)', () => {
    const ctx = loadViewer();
    const out = ctx._failureCardLayoutForCase({
        failure_details: 'TimeoutError\n',
    });
    assert.strictEqual(out.variant, 'inline');
    assert.strictEqual(out.headlineMessage, 'TimeoutError');
    assert.strictEqual(out.tracebackBody, '');
});

// ── rendered output ───────────────────────────────────────────────────────

test('render: inline variant produces .cvv-inline-headline and no .cvv-headline box', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 't',
            display_name: 'test_thing',
            outcome: 'failed',
            suite_name: 'tests/test_x.py',
            failure_details: 'TimeoutError: 30s',
            extras: [],
        }],
    });
    assert.match(html, /cvv-layout-inline/);
    assert.match(html, /cvv-inline-headline/);
    assert.match(html, /TimeoutError: 30s/);
    assert.doesNotMatch(html, /class="cvv-headline /);
    // No traceback row when no body.
    assert.doesNotMatch(html, /<span class="cvv-title">traceback<\/span>/);
});

test('render: two-row variant produces .cvv-headline box + auto-open traceback row', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 't',
            display_name: 'test_thing',
            outcome: 'failed',
            suite_name: 'tests/test_x.py',
            failure_details: 'AssertionError: bad\n  more detail\n  even more',
            extras: [],
        }],
    });
    assert.match(html, /cvv-layout-two-row/);
    assert.match(html, /<div class="cvv-headline is-failed">/);
    assert.match(html, /<span class="cvv-title">traceback<\/span>/);
    assert.doesNotMatch(html, /class="cvv-inline-headline/);
});

test('render: none variant produces no headline and no traceback row', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 't',
            display_name: 'test_thing',
            outcome: 'failed',
            suite_name: 'tests/test_x.py',
            failure_details: '',
            extras: [],
        }],
    });
    assert.match(html, /cvv-layout-none/);
    assert.doesNotMatch(html, /cvv-inline-headline/);
    assert.doesNotMatch(html, /class="cvv-headline /);
    assert.doesNotMatch(html, /<span class="cvv-title">traceback<\/span>/);
});

// ── Copy-error icon ───────────────────────────────────────────────────────

test('copy-error: present on every failed card with non-empty failure_details', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [
            { case_id: 'a', outcome: 'failed', failure_details: 'oops', extras: [] },
            { case_id: 'b', outcome: 'failed', failure_details: 'multi\nline', extras: [] },
        ],
    });
    const matches = html.match(/cvv-copy-icon/g) || [];
    assert.strictEqual(matches.length, 2, `expected 2 copy icons, got ${matches.length}`);
});

test('copy-error: data-cvv-copy-text carries the failure body', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a',
            outcome: 'failed',
            failure_details: 'TimeoutError: oops',
            extras: [],
        }],
    });
    assert.match(html, /data-cvv-copy-text="TimeoutError: oops"/);
});

test('copy-error: omitted when there is no failure_details (nothing to copy)', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', outcome: 'failed', failure_details: '', extras: [],
        }],
    });
    assert.doesNotMatch(html, /cvv-copy-icon/);
});

test('copy-error: clipboard write happens when handler runs', () => {
    // Provide a fake clipboard stub via a shimmed navigator on the
    // module's global scope, then invoke the click handler the way the
    // rendered HTML would.
    let captured = null;
    const fakeNav = {
        clipboard: {
            writeText: (text) => { captured = text; return Promise.resolve(); },
        },
    };
    const ctx = loadViewer({ navigator: fakeNav });
    // Build a fake button element exposing the .getAttribute + .textContent surface.
    const button = {
        attrs: { 'data-cvv-copy-text': 'TimeoutError: oops' },
        textContent: '⎘',
        getAttribute(name) { return this.attrs[name] || ''; },
    };
    // setTimeout fires the visual ack — stub it so the test doesn't leak timers.
    ctx.setTimeout = () => 0;
    ctx._cvvCopyErrorFromButton(button);
    assert.strictEqual(captured, 'TimeoutError: oops');
    assert.strictEqual(button.textContent, '✓');
});

test('copy-error: handler is a no-op when the button is missing the data attribute', () => {
    let called = false;
    const fakeNav = { clipboard: { writeText: () => { called = true; return Promise.resolve(); } } };
    const ctx = loadViewer({ navigator: fakeNav });
    ctx.setTimeout = () => 0;
    ctx._cvvCopyErrorFromButton({
        getAttribute: () => '',
    });
    assert.strictEqual(called, false);
});

test('copy-error: handler degrades silently when navigator.clipboard is absent', () => {
    const ctx = loadViewer({ navigator: {} });
    ctx.setTimeout = () => 0;
    // Should not throw.
    ctx._cvvCopyErrorFromButton({
        attrs: { 'data-cvv-copy-text': 'oops' },
        textContent: '⎘',
        getAttribute(name) { return this.attrs[name] || ''; },
    });
});
