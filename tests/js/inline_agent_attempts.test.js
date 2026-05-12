// Tests for the inline "Agent attempts on issue #N" expander
// (issue #6322).  Three layers, no DOM, no Playwright:
//
//   A. Pure render: ``renderInlineAgentAttemptsExpander(n)`` produces
//      the right markup (closed by default, carries the issue
//      number, has the lazy-fetch hook).
//
//   B. Render-from-payload: given a representative IssueDetailPayload,
//      the internal renderer produces an Attempt block per run with
//      the right outcome icons + cycle rows.
//
//   C. Lazy-fetch behavior: stub ``fetch`` to return the payload,
//      drive the toggle handler, assert the body got populated.
//      Re-toggling the same expander does NOT re-fetch (cache hit).

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function _makeContext() {
    const calls = { fetch: [] };
    const _fetchImpl = (url) => {
        calls.fetch.push(url);
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({}),
        });
    };
    const fetchSpy = (url) => _fetchImpl(url);
    const ctx = {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,  // never actually fires; cache eviction is incidental
        fetch: fetchSpy,
        document: {},
        navigator: {},
        // The module reads ``window.foo = bar`` to publish symbols.
        // Provide a real-enough stand-in.
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    };
    ctx.window = ctx;  // module assigns to ``window.foo`` — alias
    vm.createContext(ctx);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/inline_agent_attempts.js'),
        'utf8',
    );
    vm.runInContext(source, ctx, { filename: 'inline_agent_attempts.js' });
    return { ctx, calls };
}

// ── Layer A: pure render of the expander shell ───────────────────

test('expander shell: renders closed-by-default with issue number on it', () => {
    const { ctx } = _makeContext();
    const html = ctx.renderInlineAgentAttemptsExpander(4503);
    assert.ok(html.includes('agent-context-attempts-expander'), 'must have the expander class');
    assert.ok(html.includes('data-issue-number="4503"'), 'must carry issue number');
    assert.ok(html.includes('data-loaded=""'), 'must start unloaded');
    // <details> with no ``open`` attribute → closed by default.
    assert.ok(!/<details[^>]*\sopen[\s>]/.test(html), 'must be closed by default');
    // Title mentions the issue.
    assert.ok(html.includes('Attempts on issue #4503'));
    // Lazy-fetch hook is wired.
    assert.ok(html.includes('ontoggle="_handleAgentAttemptsToggle(this)"'));
    // Body is empty until first open (lazy fetch fills it).
    assert.ok(/<div class="agent-context-attempts-body"><\/div>/.test(html));
});

test('expander shell: returns empty string for invalid issue numbers', () => {
    const { ctx } = _makeContext();
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(0), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(-1), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander('not-a-number'), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(null), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(undefined), '');
});

// ── Layer C: lazy-fetch handler ──────────────────────────────────

function _fakeDetailsEl(issueNumber, body) {
    const detailsEl = {
        open: true,
        dataset: {
            issueNumber: String(issueNumber),
            loaded: '',
        },
        querySelector(sel) {
            if (sel === '.agent-context-attempts-body') return body;
            return null;
        },
    };
    return detailsEl;
}
function _fakeBody() {
    return {
        innerHTML: '',
    };
}

function _makeContextWithFetch(payload) {
    const calls = { fetch: [] };
    const fetchImpl = (url) => {
        calls.fetch.push(url);
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(payload),
        });
    };
    const ctx = {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,
        fetch: fetchImpl,
        document: {},
        navigator: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/inline_agent_attempts.js'),
        'utf8',
    );
    vm.runInContext(source, ctx, { filename: 'inline_agent_attempts.js' });
    return { ctx, calls };
}

test('toggle handler: lazy-fetches /api/issue-detail on first open and populates body', async () => {
    const payload = {
        runs: [
            {
                outcome: 'blocked',
                run_number: 1,
                cycles: [
                    { cycle_number: 1, outcome: 'failed', validation: { state: 'failed' } },
                    { cycle_number: 2, outcome: 'failed', cycle_label: 'Cycle 2 (rework)', validation: { state: 'failed' } },
                ],
            },
        ],
    };
    const { ctx, calls } = _makeContextWithFetch(payload);
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(4503, body);
    ctx._handleAgentAttemptsToggle(detailsEl);
    // Loading state replaces the empty body synchronously.
    assert.ok(body.innerHTML.includes('Loading agent attempts'));
    // Fetch went to the right URL.
    assert.deepStrictEqual(calls.fetch, ['/api/issue-detail/4503?view=ops']);
    // Once the fetch resolves, the body is populated with the
    // Attempt + Cycle rendering.  Drain the microtask queue.
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(body.innerHTML.includes('Attempt 1'),
        `expected 'Attempt 1' in body, got: ${body.innerHTML.slice(0, 300)}`);
    assert.ok(body.innerHTML.includes('Cycle 1'));
    assert.ok(body.innerHTML.includes('Cycle 2 (rework)'));
    // Each cycle shows its outcome + validation state.
    assert.ok(body.innerHTML.includes('outcome: failed'));
    assert.ok(body.innerHTML.includes('validation: failed'));
    // Marked loaded so a re-toggle skips the fetch.
    assert.strictEqual(detailsEl.dataset.loaded, '1');
});

test('toggle handler: second open does NOT re-fetch (loaded marker honored)', async () => {
    const payload = { runs: [{ outcome: 'completed', run_number: 1, cycles: [] }] };
    const { ctx, calls } = _makeContextWithFetch(payload);
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(42, body);
    ctx._handleAgentAttemptsToggle(detailsEl);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.strictEqual(calls.fetch.length, 1);
    // Now simulate close + reopen.
    detailsEl.open = true;  // (in a real <details> this would only fire on transitions)
    ctx._handleAgentAttemptsToggle(detailsEl);
    assert.strictEqual(calls.fetch.length, 1,
        'second open must not re-fetch — loaded marker must short-circuit');
});

test('toggle handler: cache shared across expanders for the same issue', async () => {
    const payload = { runs: [{ outcome: 'completed', run_number: 1, cycles: [] }] };
    const { ctx, calls } = _makeContextWithFetch(payload);
    const body1 = _fakeBody();
    const body2 = _fakeBody();
    const expander1 = _fakeDetailsEl(99, body1);
    const expander2 = _fakeDetailsEl(99, body2);
    ctx._handleAgentAttemptsToggle(expander1);
    ctx._handleAgentAttemptsToggle(expander2);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    // Two expanders, same issue → one fetch.
    assert.strictEqual(calls.fetch.length, 1);
    // Both bodies got populated from the cached fetch.
    assert.ok(body1.innerHTML.includes('Attempt 1'));
    assert.ok(body2.innerHTML.includes('Attempt 1'));
});

test('toggle handler: fetch failure renders an error message and clears loaded', async () => {
    const calls = { fetch: [] };
    const ctx = {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,
        fetch: (url) => {
            calls.fetch.push(url);
            return Promise.resolve({
                ok: false,
                status: 500,
                json: () => Promise.resolve({}),
            });
        },
        document: {},
        navigator: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/inline_agent_attempts.js'),
        'utf8',
    );
    vm.runInContext(source, ctx, { filename: 'inline_agent_attempts.js' });

    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(123, body);
    ctx._handleAgentAttemptsToggle(detailsEl);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(body.innerHTML.includes('Failed to load'),
        `error path must surface a failure message, got: ${body.innerHTML}`);
    // Cleared so the user can retry by closing and reopening.
    assert.strictEqual(detailsEl.dataset.loaded, '');
});

test('toggle handler: closed details is a no-op (no fetch)', () => {
    const { ctx, calls } = _makeContextWithFetch({ runs: [] });
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(7, body);
    detailsEl.open = false;
    ctx._handleAgentAttemptsToggle(detailsEl);
    assert.deepStrictEqual(calls.fetch, [], 'closed details must NOT trigger a fetch');
});

// ── Plugin integration: agent-context plugin uses the expander ──

test('plugin integration: agent-context plugin renders the inline expander when wired', () => {
    // Load BOTH inline_agent_attempts and the agent-context plugin
    // into the same vm so the symbol-resolution check inside the
    // plugin sees the expander helper.
    const captured = { plugin: null };
    const ctx = {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
        document: {},
        navigator: {},
        registerValidationPlugin: (name, fn) => { captured.plugin = fn; },
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/inline_agent_attempts.js'),
            'utf8',
        ),
        ctx,
        { filename: 'inline_agent_attempts.js' },
    );
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/plugins/agent_context.js'),
            'utf8',
        ),
        ctx,
        { filename: 'agent_context.js' },
    );

    // Invoke the registered plugin with a representative payload.
    const renderedHtml = captured.plugin({
        issue_number: 4503,
        issue_title: 'cohort split',
        final_state: 'blocked',
        summary: 'agent retried 2x then blocked',
    });
    assert.ok(renderedHtml.includes('agent-context'), 'must render the plugin block');
    assert.ok(renderedHtml.includes('Final state'));
    // The legacy Open-issue-drawer button is gone.
    assert.ok(!renderedHtml.includes('Open issue drawer'),
        'legacy "Open issue drawer" button must be replaced by the inline expander');
    // The new inline expander is present.
    assert.ok(renderedHtml.includes('Attempts on issue #4503'),
        'inline Attempts expander must be present');
    assert.ok(renderedHtml.includes('agent-context-attempts-expander'));
});

test('plugin integration: agent-context plugin degrades gracefully when expander not loaded', () => {
    // Simulate a tixmeup-style consumer: the agent_context plugin
    // loads but the inline_agent_attempts.js module did NOT.  The
    // plugin should still render the summary fields, just without
    // the expander.
    const captured = { plugin: null };
    const ctx = {
        console,
        URLSearchParams,
        document: {},
        navigator: {},
        registerValidationPlugin: (name, fn) => { captured.plugin = fn; },
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    // Deliberately do NOT load inline_agent_attempts.js.
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/plugins/agent_context.js'),
            'utf8',
        ),
        ctx,
        { filename: 'agent_context.js' },
    );

    const renderedHtml = captured.plugin({
        issue_number: 4503,
        issue_title: 'cohort split',
        final_state: 'blocked',
    });
    // Summary fields still render.
    assert.ok(renderedHtml.includes('#4503'));
    assert.ok(renderedHtml.includes('blocked'));
    // Expander is NOT present.
    assert.ok(!renderedHtml.includes('agent-context-attempts-expander'),
        'expander must be absent when inline_agent_attempts.js did not load');
    // And we did NOT silently revert to the legacy button either.
    assert.ok(!renderedHtml.includes('Open issue drawer'));
});
