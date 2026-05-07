const test = require('node:test');
const assert = require('node:assert/strict');

const browserAuth = require('../../src/issue_orchestrator/static/js/browser_auth.js');

function targetWithMeta(metaBySelector) {
    return {
        document: {
            querySelector: (selector) => {
                if (!Object.prototype.hasOwnProperty.call(metaBySelector, selector)) return null;
                return {
                    getAttribute: (name) => (name === 'content' ? metaBySelector[selector] : ''),
                };
            },
        },
    };
}

test('buildFetchOptions adds CSRF header to mutating requests', () => {
    const options = browserAuth.buildFetchOptions('/api/resume', { method: 'POST' }, 'csrf-token', Headers);

    assert.equal(options.method, 'POST');
    assert.equal(options.headers.get('X-CSRF-Token'), 'csrf-token');
});

test('buildFetchOptions preserves an explicit CSRF header', () => {
    const options = browserAuth.buildFetchOptions(
        '/api/resume',
        { method: 'POST', headers: { 'X-CSRF-Token': 'caller-token' } },
        'meta-token',
        Headers,
    );

    assert.equal(options.headers.get('X-CSRF-Token'), 'caller-token');
});

test('buildFetchOptions does not add CSRF header to safe requests', () => {
    const options = browserAuth.buildFetchOptions('/api/view-model', { method: 'GET' }, 'csrf-token', Headers);

    assert.equal(options.headers, undefined);
});

test('buildSseUrl appends encoded single-use SSE token', () => {
    assert.equal(
        browserAuth.buildSseUrl('/api/events?tab=flow', 'token/value'),
        '/api/events?tab=flow&sse_token=token%2Fvalue',
    );
});

test('isBrowserAuthRequired defaults closed when meta is missing', () => {
    assert.equal(browserAuth.isBrowserAuthRequired(targetWithMeta({})), true);
    assert.equal(
        browserAuth.isBrowserAuthRequired(
            targetWithMeta({ 'meta[name="io-browser-auth-required"]': '0' }),
        ),
        false,
    );
    assert.equal(
        browserAuth.isBrowserAuthRequired(
            targetWithMeta({ 'meta[name="io-browser-auth-required"]': '1' }),
        ),
        true,
    );
});

test('openAuthenticatedSseStream uses raw EventSource when auth is disabled', async () => {
    const opened = [];
    const target = {
        ...targetWithMeta({ 'meta[name="io-browser-auth-required"]': '0' }),
        EventSource: class {
            constructor(url) {
                this.url = url;
                opened.push(url);
            }
        },
        fetch: async () => {
            throw new Error('sse-token should not be requested when auth is disabled');
        },
    };

    const stream = await browserAuth.openAuthenticatedSseStream('/api/events', target);

    assert.equal(stream.url, '/api/events');
    assert.deepEqual(opened, ['/api/events']);
});

test('openAuthenticatedSseStream requests token when auth is enabled', async () => {
    const fetches = [];
    const target = {
        ...targetWithMeta({ 'meta[name="io-browser-auth-required"]': '1' }),
        EventSource: class {
            constructor(url) {
                this.url = url;
            }
        },
        fetch: async (url, opts) => {
            fetches.push([url, opts]);
            return {
                ok: true,
                json: async () => ({ sse_token: 'token/value' }),
            };
        },
    };

    const stream = await browserAuth.openAuthenticatedSseStream('/api/events', target);

    assert.equal(stream.url, '/api/events?sse_token=token%2Fvalue');
    assert.deepEqual(fetches, [['/api/sse-token', { cache: 'no-store' }]]);
});

function fakeDomTarget() {
    const elements = new Map();
    const body = makeNode('body');
    const doc = {
        body,
        createElement: (tag) => makeNode(tag),
        getElementById: (id) => elements.get(id) || null,
        _registerId(id, node) { elements.set(id, node); },
    };
    function makeNode(tag) {
        const node = {
            tagName: tag.toUpperCase(),
            children: [],
            attributes: {},
            style: { cssText: '' },
            listeners: {},
            id: '',
            type: '',
            textContent: '',
            setAttribute(name, value) { this.attributes[name] = String(value); },
            getAttribute(name) { return this.attributes[name] ?? null; },
            appendChild(child) { this.children.push(child); child._parent = this; return child; },
            addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
            focus() { /* no-op */ },
        };
        Object.defineProperty(node, 'id', {
            get() { return this._id || ''; },
            set(value) {
                this._id = value;
                if (value) doc._registerId(value, this);
            },
        });
        return node;
    }
    let assigned = null;
    return {
        target: {
            document: doc,
            location: {
                assign: (url) => { assigned = url; },
            },
        },
        getAssigned: () => assigned,
        findById: (id) => elements.get(id) || null,
    };
}

test('maybeShowAuthExpiredOverlay renders overlay only once for authenticated API 401s', () => {
    const { target, findById } = fakeDomTarget();

    assert.equal(
        browserAuth.maybeShowAuthExpiredOverlay({ status: 401, url: 'http://127.0.0.1/api/status' }, target),
        true,
    );
    assert.equal(
        browserAuth.maybeShowAuthExpiredOverlay({ status: 401, url: 'http://127.0.0.1/control/repos' }, target),
        false,
    );

    const overlay = findById('io-auth-expired-overlay');
    assert.ok(overlay, 'overlay should be inserted into the DOM');
    assert.equal(overlay.getAttribute('role'), 'alertdialog');
    assert.equal(overlay.getAttribute('aria-modal'), 'true');
});

test('maybeShowAuthExpiredOverlay ignores non-auth paths', () => {
    const { target, findById } = fakeDomTarget();

    assert.equal(
        browserAuth.maybeShowAuthExpiredOverlay({ status: 401, url: 'http://127.0.0.1/static/app.js' }, target),
        false,
    );
    assert.equal(findById('io-auth-expired-overlay'), null);
});

test('overlay sign-in button navigates to / on click', () => {
    const { target, getAssigned, findById } = fakeDomTarget();

    browserAuth.maybeShowAuthExpiredOverlay({ status: 401, url: 'http://127.0.0.1/api/state' }, target);

    const button = findById('io-auth-expired-overlay-signin');
    assert.ok(button, 'sign-in button should exist');
    assert.equal(button.tagName, 'BUTTON');
    assert.equal(typeof button.listeners.click?.[0], 'function');

    button.listeners.click[0]();
    assert.equal(getAssigned(), '/');
});

test('isAuthExpiredResponse only triggers on /api/ or /control/ 401s', () => {
    assert.equal(browserAuth.isAuthExpiredResponse({ status: 401, url: '/api/state' }), true);
    assert.equal(browserAuth.isAuthExpiredResponse({ status: 401, url: '/control/repos' }), true);
    assert.equal(browserAuth.isAuthExpiredResponse({ status: 401, url: '/static/css/x.css' }), false);
    assert.equal(browserAuth.isAuthExpiredResponse({ status: 200, url: '/api/state' }), false);
    assert.equal(browserAuth.isAuthExpiredResponse(null), false);
});
