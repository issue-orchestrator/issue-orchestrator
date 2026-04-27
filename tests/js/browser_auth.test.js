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

test('maybeReloadOnAuthExpiry reloads only once for authenticated API 401s', () => {
    let reloads = 0;
    const target = {
        location: {
            reload: () => {
                reloads += 1;
            },
        },
    };

    assert.equal(browserAuth.maybeReloadOnAuthExpiry({ status: 401, url: 'http://127.0.0.1/api/status' }, target), true);
    assert.equal(browserAuth.maybeReloadOnAuthExpiry({ status: 401, url: 'http://127.0.0.1/api/status' }, target), false);
    assert.equal(reloads, 1);
});

test('maybeReloadOnAuthExpiry ignores non-auth paths', () => {
    let reloads = 0;
    const target = {
        location: {
            reload: () => {
                reloads += 1;
            },
        },
    };

    assert.equal(browserAuth.maybeReloadOnAuthExpiry({ status: 401, url: 'http://127.0.0.1/static/app.js' }, target), false);
    assert.equal(reloads, 0);
});
