// JS-vm tests for the per-issue drawer's inline validation expansion
// (issue #6310 follow-up, Phase B).
//
// The drawer's validation event row is now expandable in place: clicking
// the row triggers a fetch to /api/dialog/validation-failure/N, renders
// the canonical viewer below the step, and caches the result so
// subsequent toggles just show/hide.  The badge in the cycle header is
// a shortcut into the same expansion (no more modal).

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeFakeBody() {
    return {
        innerHTML: '',
        classList: {
            _classes: new Set(['collapsed']),
            contains(c) { return this._classes.has(c); },
            toggle(c) {
                if (this._classes.has(c)) this._classes.delete(c);
                else this._classes.add(c);
                return this._classes.has(c);
            },
        },
        dataset: { loaded: '0', runDir: '' },
        // Phase D (issue #6310 follow-up): after mounting the viewer
        // HTML, the drawer calls ``body.querySelector('.cvv-root')`` so
        // it can enhance the canonical viewer with ARIA tree
        // accessibility.  These tests don't exercise the live DOM, so
        // returning null is sufficient — the enhancer call is a no-op
        // when the root isn't present.
        querySelector(_selector) { return null; },
    };
}

function makeFakeStep(bodyEl) {
    return {
        querySelector(selector) {
            if (selector === ':scope > .journey-step-row > .journey-step-caret') {
                return { textContent: '▸' };
            }
            return null;
        },
        scrollIntoView() {},
    };
}

// Match a single top-level function definition in the source.  We
// rely on the trailing ``}\n`` at column 0 as the function boundary,
// which matches the project's house style for the drawer module.
function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

// Extract a top-level ``const NAME = <expression>;`` statement.  Works
// for multi-line expressions (Set literals, object literals, etc.) as
// long as they close with ``\n]);\n`` or ``\n});\n`` or ``;\n`` — the
// helper looks for the next semicolon at column 0 after the ``const``.
function _extractValueDeclaration(source, name) {
    const pattern = new RegExp(`^const\\s+${name}\\s*=`, 'm');
    const m = source.match(pattern);
    if (!m) throw new Error(`const not found: ${name}`);
    const start = m.index;
    // Find the next line that ends with ``;`` (closing the declaration).
    // Scan forward, line by line.
    let cursor = start;
    while (cursor < source.length) {
        const lineEnd = source.indexOf('\n', cursor);
        if (lineEnd < 0) throw new Error(`unterminated const for ${name}`);
        const line = source.slice(cursor, lineEnd);
        if (line.trimEnd().endsWith(';')) {
            return source.slice(start, lineEnd + 1);
        }
        cursor = lineEnd + 1;
    }
    throw new Error(`could not find end of const ${name}`);
}

function loadDrawerInIsolation() {
    // Extract just the helpers we exercise so the test doesn't depend
    // on the rest of the drawer module (which carries deep transitive
    // imports — global ``issueDetailDrawer``, ``journeyFilter``, etc.).
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js'),
        'utf8',
    );
    const slice = [
        _extractFunction(source, 'async function toggleValidationEventInline'),
        _extractFunction(source, 'function _handleCycleValidationBadgeClick'),
    ].join('\n');

    const fetchCalls = [];
    const fetchResponses = [];
    const viewerCalls = [];
    const context = {
        console,
        URLSearchParams,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        // Spy on the viewer call so tests can assert what was passed in
        // the ``options`` arg (e.g. the action-section renderer).  Real
        // viewer output is irrelevant to these tests — the stub returns
        // a sentinel div and stashes the args via the outer closure.
        renderCanonicalValidationViewer: (data, options) => {
            viewerCalls.push({ data, options: options || {} });
            return `<div data-cvv-mock>cases=${(data.junit_cases || []).length}</div>`;
        },
        // The drawer now passes
        // ``renderActionSections: renderValidationFailureActionSections``
        // into the viewer so the inline body keeps the Validation
        // artifacts footer (reviewer Blocker 1 on PR #6315).  A
        // sentinel stub lets us prove the same function was forwarded.
        renderValidationFailureActionSections: (sections) =>
            `<div data-test-renderer-invoked="1" data-sections="${sections.length}"></div>`,
        document: {
            _byId: {},
            getElementById(id) { return this._byId[id] || null; },
        },
        fetch: async (url) => {
            fetchCalls.push(url);
            const next = fetchResponses.shift();
            return next || { ok: true, json: async () => ({ junit_cases: [] }) };
        },
        // Stubs for helpers _handleCycleValidationBadgeClick reaches into.
        // These tests don't exercise the badge handler — included only
        // so the slice evaluates cleanly.
        toggleJourneyCycle: () => {},
        issueDetailData: { issue_number: 4242 },
        fetchCalls,
        fetchResponses,
        viewerCalls,
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (slice)' });
    return context;
}

test('inline toggle: lazy-loads dialog data on first expand', async () => {
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    body.dataset.runDir = '/tmp/run-1';
    const step = makeFakeStep(body);
    ctx.document._byId['step-1'] = step;
    ctx.document._byId['step-1-body'] = body;
    ctx.fetchResponses.push({
        ok: true,
        json: async () => ({ status: 'passed', junit_cases: [{ case_id: 'a' }] }),
    });

    await ctx.toggleValidationEventInline('step-1', 4242, '/tmp/run-1');

    assert.strictEqual(ctx.fetchCalls.length, 1);
    assert.match(ctx.fetchCalls[0], /\/api\/dialog\/validation-failure\/4242\?run_dir=%2Ftmp%2Frun-1/);
    assert.strictEqual(body.dataset.loaded, '1');
    assert.match(body.innerHTML, /data-cvv-mock/);
    assert.match(body.innerHTML, /cases=1/);
});

test('inline toggle: second click toggles visibility without re-fetching', async () => {
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    const step = makeFakeStep(body);
    ctx.document._byId['step-2'] = step;
    ctx.document._byId['step-2-body'] = body;
    ctx.fetchResponses.push({
        ok: true,
        json: async () => ({ status: 'passed', junit_cases: [] }),
    });

    await ctx.toggleValidationEventInline('step-2', 4242, '/tmp/r');
    assert.strictEqual(ctx.fetchCalls.length, 1);
    // Collapse — no second fetch
    await ctx.toggleValidationEventInline('step-2', 4242, '/tmp/r');
    assert.strictEqual(ctx.fetchCalls.length, 1);
    // Expand again — still no second fetch
    await ctx.toggleValidationEventInline('step-2', 4242, '/tmp/r');
    assert.strictEqual(ctx.fetchCalls.length, 1);
});

test('inline toggle: HTTP error surfaces inline, does not throw', async () => {
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    const step = makeFakeStep(body);
    ctx.document._byId['step-3'] = step;
    ctx.document._byId['step-3-body'] = body;
    ctx.fetchResponses.push({
        ok: false,
        status: 503,
        json: async () => ({ error: 'Validation record not found' }),
    });

    await ctx.toggleValidationEventInline('step-3', 4242, '/tmp/r');

    assert.match(body.innerHTML, /Validation record not found/);
    assert.strictEqual(body.dataset.loaded, 'error');
});

test('inline toggle: network error surfaces inline, does not throw', async () => {
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    const step = makeFakeStep(body);
    ctx.document._byId['step-4'] = step;
    ctx.document._byId['step-4-body'] = body;
    ctx.fetch = async () => { throw new Error('connection refused'); };

    await ctx.toggleValidationEventInline('step-4', 4242, '/tmp/r');

    assert.match(body.innerHTML, /connection refused/);
    assert.strictEqual(body.dataset.loaded, 'error');
});

test('inline toggle: runDir omitted when caller passes none and body has none cached', async () => {
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    body.dataset.runDir = '';
    const step = makeFakeStep(body);
    ctx.document._byId['step-5'] = step;
    ctx.document._byId['step-5-body'] = body;
    ctx.fetchResponses.push({
        ok: true,
        json: async () => ({ junit_cases: [] }),
    });

    await ctx.toggleValidationEventInline('step-5', 4242, '');

    // No run_dir query string when neither caller nor body knows one.
    assert.match(ctx.fetchCalls[0], /\/api\/dialog\/validation-failure\/4242$/);
});

test('action filter: validation steps drop the modal-opening open_validation_failure action (Blocker 2 on PR #6315)', () => {
    // Reviewer Blocker 2 on PR #6315: the per-issue drawer's
    // validation-step rendering used to forward the raw event actions
    // straight into ``renderTimelineEventActions``.  Among those is
    // ``open_validation_failure`` which the timeline action dispatcher
    // routes to ``openValidationFailure(..., 'inline')`` — and that
    // opens the validation modal.  Phase B's contract is "no modal on
    // validation rows".  ``_filterStepActions`` enforces it.
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js'),
        'utf8',
    );
    const slice = [
        _extractValueDeclaration(source, '_VALIDATION_EVENT_NAMES'),
        _extractFunction(source, 'function _isValidationStep'),
        _extractFunction(source, 'function _filterStepActions'),
    ].join('\n');
    const context = { console };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (filter slice)' });

    // Each canonical validation event name + a non-validation event.
    const validationEvents = [
        'validation.passed',
        'validation.failed',
        'session.validation_passed',
        'session.validation_failed',
    ];
    for (const eventName of validationEvents) {
        const step = {
            event: eventName,
            actions: [
                { type: 'open_validation_failure', issue_number: 42 },  // must be filtered
                { type: 'open_agent_log', issue_number: 42 },           // must survive
                { type: 'open_review_transcript', issue_number: 42 },   // must survive
            ],
        };
        const filtered = context._filterStepActions(step);
        assert.strictEqual(
            filtered.find((a) => a.type === 'open_validation_failure'),
            undefined,
            `open_validation_failure must be filtered for ${eventName}`,
        );
        assert.ok(
            filtered.find((a) => a.type === 'open_agent_log'),
            `open_agent_log must survive on ${eventName}`,
        );
        assert.ok(
            filtered.find((a) => a.type === 'open_review_transcript'),
            `open_review_transcript must survive on ${eventName}`,
        );
    }

    // Non-validation step: actions pass through unchanged.
    const nonValidationStep = {
        event: 'session.completed',
        actions: [
            { type: 'open_validation_failure', issue_number: 42 },  // not filtered here
            { type: 'open_agent_log', issue_number: 42 },
        ],
    };
    const passthrough = context._filterStepActions(nonValidationStep);
    assert.strictEqual(passthrough.length, 2);
});

test('inline toggle: passes renderActionSections so the artifacts footer survives (Blocker 1 on PR #6315)', async () => {
    // Reviewer Blocker 1 on PR #6315: ``toggleValidationEventInline``
    // previously called ``renderCanonicalValidationViewer(data)`` with
    // no options, which silently dropped the artifacts footer (the
    // viewer only renders ``action_sections`` when the caller passes a
    // renderer — explicit dependency boundary established in Phase A).
    // The inline path now passes
    // ``renderValidationFailureActionSections`` from session_dialogs.js
    // so the Validation artifacts footer is rendered identically in
    // the inline drawer mount and the modal.
    const ctx = loadDrawerInIsolation();
    const body = makeFakeBody();
    const step = makeFakeStep(body);
    ctx.document._byId['step-6'] = step;
    ctx.document._byId['step-6-body'] = body;
    ctx.fetchResponses.push({
        ok: true,
        json: async () => ({
            status: 'failed',
            junit_cases: [],
            action_sections: [{ title: 'Validation Artifacts', actions: [{ type: 'open_path', label: 'Open Record' }] }],
        }),
    });

    await ctx.toggleValidationEventInline('step-6', 4242, '/tmp/run-art');

    assert.strictEqual(ctx.viewerCalls.length, 1, 'viewer should have been called once');
    const call = ctx.viewerCalls[0];
    assert.ok(Array.isArray(call.data.action_sections), 'action_sections should round-trip');
    assert.strictEqual(call.data.action_sections.length, 1);
    // The explicit dependency must be passed; the test guards against
    // a regression to options-less viewer invocation.
    assert.strictEqual(
        typeof call.options.renderActionSections,
        'function',
        'options.renderActionSections must be a function',
    );
    // And it must be the *same* function the modal uses — the simplest
    // way to assert "the same renderer" is to invoke it and check it
    // produces the sentinel HTML the stub returns.
    const sectionHtml = call.options.renderActionSections(call.data.action_sections);
    assert.match(sectionHtml, /data-test-renderer-invoked="1"/);
    assert.match(sectionHtml, /data-sections="1"/);
});
