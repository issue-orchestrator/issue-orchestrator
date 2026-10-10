// Control Center pause/resume surface the engine's failure cause (#8222).
//
// The Control Center answers a failed engine command with
// ``{"error": "passthrough_failed", "detail": <why>}``. Before #8222 the
// toast dropped the body and said only "Failed to pause repository engine".
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const uiContractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');

function loadControlCenter(fetchImpl) {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const toasts = [];
    const requests = [];
    const context = {
        URL,
        console,
        localStorage: { getItem: () => null, setItem() {} },
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
            uiContractJson,
        },
        document: { addEventListener() {} },
        fetch: async (url, init) => {
            requests.push({ url, init });
            return fetchImpl(url, init);
        },
        setInterval() {},
        setTimeout() {},
        clearTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.showToast = (message, kind) => toasts.push({ message, kind });
    context.loadRepos = async () => {};
    return { context, toasts, requests };
}

function failure(status, body) {
    return { ok: false, status, text: async () => JSON.stringify(body) };
}

for (const [verb, fn] of [['pause', 'pauseRepo'], ['resume', 'resumeRepo']]) {
    test(`${fn} toasts the engine's failure detail, not a bare failure`, async () => {
        const detail = `Engine did not answer ${verb} at http://127.0.0.1:8081 within 120s (ReadTimeout).`;
        const { context, toasts, requests } = loadControlCenter(
            async () => failure(504, { error: 'passthrough_failed', failure: 'no_answer', detail }),
        );

        await context[fn]('/repo');

        assert.strictEqual(requests[0].url, `/control/orchestrator/${verb}`);
        assert.deepStrictEqual(JSON.parse(requests[0].init.body), { repo_root: '/repo' });
        assert.strictEqual(toasts.length, 1);
        assert.strictEqual(toasts[0].kind, 'error');
        assert.strictEqual(toasts[0].message, `Failed to ${verb} repository engine: ${detail}`);
    });

    test(`${fn} still names the HTTP status when the body carries no cause`, async () => {
        const { context, toasts } = loadControlCenter(
            async () => ({ ok: false, status: 502, text: async () => '<html>bad gateway</html>' }),
        );

        await context[fn]('/repo');

        assert.strictEqual(toasts[0].message, `Failed to ${verb} repository engine: HTTP 502`);
    });
}

// ── the toast that carries the cause (#8222 review r1 F5) ─────────────

function fakeElement(tag) {
    const element = {
        tagName: tag.toUpperCase(),
        children: [],
        attributes: {},
        classList: {
            values: new Set(),
            add(...names) { names.forEach((name) => this.values.add(name)); },
            contains(name) { return this.values.has(name); },
        },
        style: {},
        listeners: {},
        removed: false,
        set className(value) { value.split(/\s+/).filter(Boolean).forEach((name) => this.classList.add(name)); },
        appendChild(child) { this.children.push(child); return child; },
        prepend(child) { this.children.unshift(child); return child; },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        addEventListener(type, handler) { this.listeners[type] = handler; },
        remove() { this.removed = true; },
    };
    return element;
}

function loadToast() {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const container = fakeElement('div');
    const timers = [];
    const context = {
        URL,
        console,
        localStorage: { getItem: () => null, setItem() {} },
        window: { addEventListener() {}, matchMedia: () => ({ addEventListener() {}, matches: false }), setTimeout() {} },
        document: {
            addEventListener() {},
            createElement: fakeElement,
            getElementById: (id) => (id === 'toastContainer' ? container : null),
        },
        fetch: async () => ({ ok: true }),
        setInterval() {},
        setTimeout: (fn, ms) => timers.push({ fn, ms }),
        clearTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    return { context, container, timers };
}

test('an error toast stays until dismissed and its cause is keyboard-reachable', () => {
    const { context, container, timers } = loadToast();
    const cause = 'Failed to pause repository engine: ' + 'x'.repeat(2000);

    context.showToast(cause, 'error');

    const toast = container.children[0];
    const [message, close] = toast.children;
    assert.strictEqual(message.textContent, cause);
    assert.ok(message.classList.contains('toast-message'));
    assert.strictEqual(message.tabIndex, 0, 'a long cause must be scrollable by keyboard');
    assert.strictEqual(close.tagName, 'BUTTON');
    assert.strictEqual(close.attributes['aria-label'], 'Dismiss notification');
    // Nothing is scheduled to remove it: run every timer and it is still there.
    timers.splice(0).forEach(({ fn }) => fn());
    assert.strictEqual(toast.removed, false);

    close.listeners.click();
    timers.splice(0).forEach(({ fn }) => fn());
    assert.strictEqual(toast.removed, true);
});

test('a success toast still dismisses itself', () => {
    const { context, container, timers } = loadToast();

    context.showToast('Repository engine paused', 'success');

    const toast = container.children[0];
    assert.strictEqual(toast.children.length, 1, 'no close button on a transient toast');
    while (timers.length) timers.shift().fn();
    assert.strictEqual(toast.removed, true);
});

test('the newest toast lands first so it is in view atop the stack', () => {
    const { context, container } = loadToast();

    context.showToast('first failure', 'error');
    context.showToast('second failure', 'error');

    assert.deepStrictEqual(
        container.children.map((toast) => toast.children[0].textContent),
        ['second failure', 'first failure'],
    );
});

test('the toast message is height-bounded and scrolls', () => {
    const css = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/css/control_center.css'),
        'utf8',
    );
    const rule = css.match(/\.toast-message\s*\{([^}]*)\}/);
    assert.ok(rule, '.toast-message rule exists');
    assert.match(rule[1], /max-height:\s*min\(40vh,\s*240px\)/);
    assert.match(rule[1], /overflow-y:\s*auto/);
    assert.match(css, /\.toast-close:focus-visible/);
    const stack = css.match(/\.toast-container\s*\{([^}]*)\}/);
    assert.ok(stack, '.toast-container rule exists');
    assert.match(stack[1], /max-height:\s*calc\(100vh - 24px\)/);
    assert.match(stack[1], /overflow-y:\s*auto/);
});
