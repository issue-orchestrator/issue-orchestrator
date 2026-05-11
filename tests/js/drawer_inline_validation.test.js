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
    const context = {
        console,
        URLSearchParams,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        renderCanonicalValidationViewer: (data) => `<div data-cvv-mock>cases=${(data.junit_cases || []).length}</div>`,
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
