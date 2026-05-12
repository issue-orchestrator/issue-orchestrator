// Tests for the inline "Agent attempts on issue #N" expander
// (issue #6322 follow-up).  Four layers, no DOM, no Playwright:
//
//   A. Pure render: ``renderInlineAgentAttemptsExpander(n)`` produces
//      the right markup — closed by default, typed
//      ``data-lifecycle-command`` JSON on the ``<details>``, and an
//      ``ontoggle`` that dispatches through the shared lifecycle
//      pipeline.  No bespoke per-element handler.
//
//   B. Loader: ``loadInlineAgentAttempts(issue, detailsEl)`` is the
//      typed-Command handler.  Validate the lazy-fetch URL, the
//      cache, the loaded marker, and the error path.
//
//   C. Dispatcher: the toggle helper +
//      ``runE2ELifecycleCommandFromToggle`` route the
//      ``open_inline_agent_attempts`` Command from the rendered
//      attribute to ``loadInlineAgentAttempts`` with the trigger
//      element forwarded — same single-owner pipeline as every other
//      typed Command in the dashboard.
//
//   D. Plugin integration: the ``io.agent-context`` plugin embeds the
//      expander when ``renderInlineAgentAttemptsExpander`` is
//      defined, and degrades to summary-only otherwise.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _baseStubs() {
    return {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,
        document: {},
        navigator: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (s) => String(s || ''),
        showToast: () => {},
    };
}

function _loadInlineModule(extra = {}) {
    const ctx = { ..._baseStubs(), ...extra };
    ctx.window = ctx;  // module assigns to ``window.foo`` — alias
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'inline_agent_attempts.js'), 'utf8'),
        ctx,
        { filename: 'inline_agent_attempts.js' },
    );
    return ctx;
}

function _loadInlinePlusDispatcher(extra = {}) {
    const ctx = _loadInlineModule(extra);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'lifecycle_commands.js'), 'utf8'),
        ctx,
        { filename: 'lifecycle_commands.js' },
    );
    return ctx;
}

function _fakeBody() {
    return { innerHTML: '' };
}

function _fakeDetailsEl(issueNumber, body, opts = {}) {
    const command = {
        kind: 'open_inline_agent_attempts',
        label: 'Open Inline Agent Attempts',
        issue_number: Number(issueNumber),
    };
    const detailsEl = {
        open: opts.open ?? true,
        dataset: {
            issueNumber: String(issueNumber),
            loaded: opts.loaded ?? '',
            lifecycleCommand: JSON.stringify(command),
        },
        querySelector(sel) {
            if (sel === '.agent-context-attempts-body') return body;
            return null;
        },
    };
    return detailsEl;
}

// ── Layer A: pure render shape ───────────────────────────────────

test('expander shell: typed-Command JSON sits on the <details> with the right shape', () => {
    const ctx = _loadInlineModule();
    const html = ctx.renderInlineAgentAttemptsExpander(4503);
    assert.ok(html.includes('agent-context-attempts-expander'), 'must have the expander class');
    assert.ok(html.includes('data-issue-number="4503"'), 'must carry issue number');
    assert.ok(html.includes('data-loaded=""'), 'must start unloaded');
    // <details> with no ``open`` attribute → closed by default.
    assert.ok(!/<details[^>]*\sopen[\s>]/.test(html), 'must be closed by default');
    // Title mentions the issue.
    assert.ok(html.includes('Attempts on issue #4503'));
    // Body is empty until first open (lazy fetch fills it).
    assert.ok(/<div class="agent-context-attempts-body"><\/div>/.test(html));
    // Typed-Command pipeline: ontoggle dispatches through the shared
    // owner, NOT a bespoke per-element handler.
    assert.ok(
        html.includes('ontoggle="runE2ELifecycleCommandFromToggle(this)"'),
        'expander must dispatch via the shared lifecycle Command pipeline',
    );
    assert.ok(
        !html.includes('_handleAgentAttemptsToggle'),
        'legacy bespoke toggle handler must not survive in the rendered HTML',
    );
    // Decode + assert the typed-Command payload.
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    assert.ok(match, `expected data-lifecycle-command in: ${html.slice(0, 200)}…`);
    const cmd = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
    assert.deepStrictEqual(cmd, {
        kind: 'open_inline_agent_attempts',
        label: 'Open Inline Agent Attempts',
        issue_number: 4503,
    });
});

test('expander shell: returns empty string for invalid issue numbers', () => {
    const ctx = _loadInlineModule();
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(0), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(-1), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander('not-a-number'), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(null), '');
    assert.strictEqual(ctx.renderInlineAgentAttemptsExpander(undefined), '');
});

// ── Layer B: loader (the typed-Command handler) ──────────────────

function _ctxWithFetch(payload, ok = true, status = 200) {
    const calls = { fetch: [] };
    return {
        calls,
        ctx: _loadInlineModule({
            fetch: (url) => {
                calls.fetch.push(url);
                return Promise.resolve({
                    ok,
                    status,
                    json: () => Promise.resolve(payload),
                });
            },
        }),
    };
}

test('loader: lazy-fetches /api/issue-detail on first call and populates body', async () => {
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
    const { ctx, calls } = _ctxWithFetch(payload);
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(4503, body);
    ctx.loadInlineAgentAttempts(4503, detailsEl);
    assert.ok(body.innerHTML.includes('Loading agent attempts'));
    assert.deepStrictEqual(calls.fetch, ['/api/issue-detail/4503?view=ops']);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(body.innerHTML.includes('Attempt 1'),
        `expected 'Attempt 1' in body, got: ${body.innerHTML.slice(0, 300)}`);
    assert.ok(body.innerHTML.includes('Cycle 1'));
    assert.ok(body.innerHTML.includes('Cycle 2 (rework)'));
    assert.ok(body.innerHTML.includes('outcome: failed'));
    assert.ok(body.innerHTML.includes('validation: failed'));
    assert.strictEqual(detailsEl.dataset.loaded, '1');
});

test('loader: cache shared across calls for the same issue', async () => {
    const payload = { runs: [{ outcome: 'completed', run_number: 1, cycles: [] }] };
    const { ctx, calls } = _ctxWithFetch(payload);
    const body1 = _fakeBody();
    const body2 = _fakeBody();
    ctx.loadInlineAgentAttempts(99, _fakeDetailsEl(99, body1));
    ctx.loadInlineAgentAttempts(99, _fakeDetailsEl(99, body2));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.strictEqual(calls.fetch.length, 1);
    assert.ok(body1.innerHTML.includes('Attempt 1'));
    assert.ok(body2.innerHTML.includes('Attempt 1'));
});

test('loader: fetch failure renders an error message and clears loaded', async () => {
    const { ctx } = _ctxWithFetch({}, false, 500);
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(123, body);
    ctx.loadInlineAgentAttempts(123, detailsEl);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(body.innerHTML.includes('Failed to load'),
        `error path must surface a failure message, got: ${body.innerHTML}`);
    assert.strictEqual(detailsEl.dataset.loaded, '');
});

test('loader: invalid issue number is a defensive no-op', () => {
    const { ctx, calls } = _ctxWithFetch({});
    ctx.loadInlineAgentAttempts(0, _fakeDetailsEl(0, _fakeBody()));
    ctx.loadInlineAgentAttempts(-1, _fakeDetailsEl(-1, _fakeBody()));
    ctx.loadInlineAgentAttempts('not-a-number', _fakeDetailsEl(1, _fakeBody()));
    ctx.loadInlineAgentAttempts(123, null);  // no element → no-op
    assert.deepStrictEqual(calls.fetch, []);
});

// ── Layer C: typed-Command dispatch through the shared owner ─────

test('dispatcher: runE2ELifecycleCommandFromToggle reads typed JSON, dispatches to loadInlineAgentAttempts', async () => {
    const calls = { fetch: [], load: [] };
    const ctx = _loadInlinePlusDispatcher({
        fetch: (url) => {
            calls.fetch.push(url);
            return Promise.resolve({
                ok: true,
                status: 200,
                json: () => Promise.resolve({ runs: [] }),
            });
        },
    });
    const body = _fakeBody();
    const detailsEl = _fakeDetailsEl(7777, body);
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    // Single-owner contract: the dispatcher routed the typed
    // ``open_inline_agent_attempts`` Command to the loader, which
    // hit the lazy-fetch URL for issue 7777.
    assert.deepStrictEqual(calls.fetch, ['/api/issue-detail/7777?view=ops']);
    assert.strictEqual(detailsEl.dataset.loaded, '1');
});

test('dispatcher: closed details is a no-op (no fetch)', () => {
    const calls = { fetch: [] };
    const ctx = _loadInlinePlusDispatcher({
        fetch: (url) => {
            calls.fetch.push(url);
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        },
    });
    const detailsEl = _fakeDetailsEl(7, _fakeBody(), { open: false });
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    assert.deepStrictEqual(calls.fetch, [], 'closed details must NOT trigger a fetch');
});

test('dispatcher: re-toggle after load (dataset.loaded="1") short-circuits, no re-fetch', () => {
    const calls = { fetch: [] };
    const ctx = _loadInlinePlusDispatcher({
        fetch: (url) => {
            calls.fetch.push(url);
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        },
    });
    const detailsEl = _fakeDetailsEl(42, _fakeBody(), { loaded: '1' });
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    assert.deepStrictEqual(calls.fetch, []);
});

test('dispatcher: round-trip — render → extract Command → toggle → fetch', async () => {
    // The strongest "Command pattern is wired right" check: render
    // the expander, pull the typed Command out of the rendered
    // ``data-lifecycle-command`` attribute, simulate the
    // ``<details>`` toggle through the SAME dispatcher used in
    // production, and observe the lazy-fetch URL on the spy.
    const calls = { fetch: [] };
    const ctx = _loadInlinePlusDispatcher({
        fetch: (url) => {
            calls.fetch.push(url);
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ runs: [] }) });
        },
    });
    const html = ctx.renderInlineAgentAttemptsExpander(8888);
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    assert.ok(match);
    const cmdJson = match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const body = _fakeBody();
    const detailsEl = {
        open: true,
        dataset: { issueNumber: '8888', loaded: '', lifecycleCommand: cmdJson },
        querySelector: (sel) => (sel === '.agent-context-attempts-body' ? body : null),
    };
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    assert.deepStrictEqual(calls.fetch, ['/api/issue-detail/8888?view=ops']);
});

test('dispatcher: unknown kind falls through to a warning toast (no crash)', () => {
    const toasts = [];
    const ctx = _loadInlinePlusDispatcher({
        fetch: () => Promise.reject(new Error('should not be reached')),
        showToast: (msg, severity) => toasts.push([msg, severity]),
    });
    ctx.runE2ELifecycleCommand({ kind: 'not_a_real_command' });
    assert.strictEqual(toasts.length, 1);
    assert.match(toasts[0][0], /Unsupported lifecycle command/);
    assert.strictEqual(toasts[0][1], 'warning');
});

// ── Layer D: agent-context plugin integration ────────────────────

test('plugin: agent-context plugin renders the typed expander when wired', () => {
    const captured = { plugin: null };
    const ctx = {
        ..._baseStubs(),
        registerValidationPlugin: (name, fn) => { captured.plugin = fn; },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'inline_agent_attempts.js'), 'utf8'),
        ctx,
        { filename: 'inline_agent_attempts.js' },
    );
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'plugins/agent_context.js'), 'utf8'),
        ctx,
        { filename: 'agent_context.js' },
    );
    const renderedHtml = captured.plugin({
        issue_number: 4503,
        issue_title: 'cohort split',
        final_state: 'blocked',
        summary: 'agent retried 2x then blocked',
    });
    assert.ok(renderedHtml.includes('agent-context'), 'must render the plugin block');
    assert.ok(renderedHtml.includes('Final state'));
    assert.ok(!renderedHtml.includes('Open issue drawer'),
        'legacy "Open issue drawer" button must be gone');
    assert.ok(renderedHtml.includes('Attempts on issue #4503'),
        'inline Attempts expander must be present');
    assert.ok(renderedHtml.includes('agent-context-attempts-expander'));
    // Typed Command is on the expander.
    const match = renderedHtml.match(/data-lifecycle-command="([^"]+)"/);
    assert.ok(match, 'expander must carry data-lifecycle-command');
    const cmd = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
    assert.strictEqual(cmd.kind, 'open_inline_agent_attempts');
    assert.strictEqual(cmd.issue_number, 4503);
});

test('plugin: agent-context plugin degrades gracefully when expander not loaded', () => {
    const captured = { plugin: null };
    const ctx = {
        ..._baseStubs(),
        registerValidationPlugin: (name, fn) => { captured.plugin = fn; },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    // Deliberately do NOT load inline_agent_attempts.js.
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'plugins/agent_context.js'), 'utf8'),
        ctx,
        { filename: 'agent_context.js' },
    );
    const renderedHtml = captured.plugin({
        issue_number: 4503,
        issue_title: 'cohort split',
        final_state: 'blocked',
    });
    assert.ok(renderedHtml.includes('#4503'));
    assert.ok(renderedHtml.includes('blocked'));
    assert.ok(!renderedHtml.includes('agent-context-attempts-expander'),
        'expander must be absent when inline_agent_attempts.js did not load');
    assert.ok(!renderedHtml.includes('Open issue drawer'),
        'legacy affordance must not silently come back');
});
