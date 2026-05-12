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

test('render: two-row variant produces .cvv-headline box + COLLAPSED traceback row', () => {
    // Predictable-collapse rule: the traceback row defaults closed
    // regardless of failure count.  The headline above shows the
    // 1-line summary; the user clicks ``traceback ▸`` to drill in.
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

    // Traceback row must NOT carry the ``open`` attribute or
    // aria-expanded="true".  Find the row's opening tag and assert
    // on its attributes.
    const tracebackTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">traceback<\/span>)/);
    assert.ok(tracebackTag, 'traceback <details> tag not found');
    assert.doesNotMatch(tracebackTag[0], /\bopen\b/,
        'traceback row must NOT carry the open attribute');
    assert.match(tracebackTag[0], /aria-expanded="false"/);
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

// ── Skip reason (content-checked) ─────────────────────────────────────────

test('skip-reason: a skipped test surfaces its failure_details inline when the row opens', () => {
    // The JUnit parser maps ``<skipped message="reason"/>`` into
    // ``failure_details`` on the JUnitCase.  The viewer surfaces it
    // verbatim in a muted block so users can read it without
    // leaving the dashboard.  This test asserts the *exact text*
    // appears in the rendered HTML (content, not just structure).
    const ctx = loadViewer();
    const reason = "Skipped: not implemented on macOS — see PR #5500 for the cross-platform shim";
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{
            case_id: 'a',
            display_name: 'test_macos_specific',
            suite_name: 'tests/integration/test_platform.py',
            outcome: 'skipped',
            failure_details: reason,
            extras: [],
        }],
    });
    // The skip-reason block uses its dedicated class.
    assert.match(html, /<div class="cvv-skip-reason">/);
    // The exact reason text appears in the rendered DOM.  ``&#39;``
    // is the entity for ``'`` (the escapeHtml output for the reason
    // would contain it if the reason had an apostrophe — this one
    // doesn't, so the text passes through unchanged).
    assert.match(html, /Skipped: not implemented on macOS — see PR #5500 for the cross-platform shim/);
});

test('skip-reason: a skipped test with no failure_details shows the "no skip reason" placeholder', () => {
    // If the JUnit XML wrote ``<skipped/>`` with no message body,
    // ``failure_details`` ends up empty.  The viewer still has to
    // render something — a muted placeholder so the absent reason
    // is itself visible information (not a layout-collapse).
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{
            case_id: 'a',
            display_name: 'test_silently_skipped',
            outcome: 'skipped',
            failure_details: '',
            extras: [],
        }],
    });
    assert.match(html, /No skip reason was recorded for this test/);
    // No empty skip-reason block (no signal to show).
    assert.doesNotMatch(html, /<div class="cvv-skip-reason"><\/div>/);
});

test('skip-reason: passing tests do NOT render a skip-reason block (failure_details ignored)', () => {
    // A passing test should never carry a skip-reason block even
    // if its ``failure_details`` is accidentally set — the field
    // only has meaning for skips in the viewer's render path.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{
            case_id: 'a',
            display_name: 'test_works',
            outcome: 'passed',
            failure_details: 'should not appear',
            extras: [],
        }],
    });
    assert.doesNotMatch(html, /cvv-skip-reason/);
    assert.doesNotMatch(html, /should not appear/);
});

test('skip-reason: HTML in the reason is escaped, not interpreted', () => {
    // A defense-in-depth check: skip-reason text comes from JUnit
    // XML (which is decoded by the parser already), but the viewer
    // must HTML-escape on its way into the DOM so a hostile
    // adopter who plants ``<script>`` in a JUnit ``<skipped
    // message="...">`` does not get a script execution out of it.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{
            case_id: 'a',
            display_name: 'test_hostile',
            outcome: 'skipped',
            failure_details: '<script>alert("xss")</script>',
            extras: [],
        }],
    });
    assert.doesNotMatch(html, /<script>alert/);
    assert.match(html, /&lt;script&gt;alert/);
});
