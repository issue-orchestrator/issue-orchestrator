// Shared browser-session helper for Control Center and repository dashboards.
// Authenticated browser requests use a session cookie plus X-CSRF-Token for
// mutating fetches, and EventSource uses a short-lived query-string token.
(function(root) {
    'use strict';

    const CSRF_META_SELECTOR = 'meta[name="io-csrf-token"]';
    const AUTH_REQUIRED_META_SELECTOR = 'meta[name="io-browser-auth-required"]';
    const CSRF_HEADER = 'X-CSRF-Token';
    const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

    function getMetaContent(selector, target = root) {
        const doc = target.document;
        if (!doc || typeof doc.querySelector !== 'function') return null;
        const meta = doc.querySelector(selector);
        return meta ? (meta.getAttribute('content') || '') : null;
    }

    function getCsrfToken(target = root) {
        return getMetaContent(CSRF_META_SELECTOR, target) || '';
    }

    function isBrowserAuthRequired(target = root) {
        const value = getMetaContent(AUTH_REQUIRED_META_SELECTOR, target);
        if (value === null) return true;
        return value === '1';
    }

    function resolveRequestMethod(input, init = {}) {
        const requestMethod = typeof input !== 'string' && input ? input.method : null;
        return ((init && init.method) || requestMethod || 'GET').toUpperCase();
    }

    function buildFetchOptions(input, init = {}, csrfToken = '', HeadersCtor = root.Headers) {
        const method = resolveRequestMethod(input, init);
        const options = init || {};
        if (!csrfToken || SAFE_METHODS.has(method)) {
            return options;
        }
        if (typeof HeadersCtor !== 'function') {
            throw new Error('Headers constructor is unavailable');
        }
        const sourceHeaders = options.headers
            || (typeof input !== 'string' && input ? input.headers : undefined)
            || {};
        const headers = new HeadersCtor(sourceHeaders);
        if (!headers.has(CSRF_HEADER)) {
            headers.set(CSRF_HEADER, csrfToken);
        }
        return { ...options, headers };
    }

    function shouldReloadOnAuthExpiry(response) {
        if (!response || response.status !== 401) return false;
        const url = typeof response.url === 'string' ? response.url : '';
        return url.includes('/api/') || url.includes('/control/');
    }

    function maybeReloadOnAuthExpiry(response, target = root) {
        if (!shouldReloadOnAuthExpiry(response)) return false;
        if (target.__ioBrowserAuthReloadTriggered) return false;
        target.__ioBrowserAuthReloadTriggered = true;
        if (target.location && typeof target.location.reload === 'function') {
            target.location.reload();
        }
        return true;
    }

    function buildSseUrl(path, token) {
        const joiner = path.includes('?') ? '&' : '?';
        return `${path}${joiner}sse_token=${encodeURIComponent(token)}`;
    }

    async function openAuthenticatedSseStream(path, target = root) {
        const EventSourceCtor = target.EventSource;
        if (typeof EventSourceCtor !== 'function') {
            throw new Error('EventSource constructor is unavailable');
        }
        if (!isBrowserAuthRequired(target)) {
            return new EventSourceCtor(path);
        }
        const resp = await target.fetch('/api/sse-token', { cache: 'no-store' });
        if (!resp.ok) {
            throw new Error(`sse-token request failed (${resp.status})`);
        }
        const { sse_token } = await resp.json();
        return new EventSourceCtor(buildSseUrl(path, sse_token));
    }

    function installAuthenticatedFetch(target = root) {
        if (target.__ioBrowserAuthFetchInstalled) return;
        if (typeof target.fetch !== 'function') return;
        target.__ioBrowserAuthFetchInstalled = true;
        const originalFetch = target.fetch.bind(target);
        target.fetch = async (input, init = {}) => {
            const csrfToken = getCsrfToken(target);
            const options = buildFetchOptions(input, init, csrfToken, target.Headers);
            const response = await originalFetch(input, options);
            if (isBrowserAuthRequired(target)) {
                maybeReloadOnAuthExpiry(response, target);
            }
            return response;
        };
    }

    const api = {
        buildFetchOptions,
        buildSseUrl,
        getCsrfToken,
        installAuthenticatedFetch,
        isBrowserAuthRequired,
        maybeReloadOnAuthExpiry,
        openAuthenticatedSseStream,
        resolveRequestMethod,
        shouldReloadOnAuthExpiry,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }

    root.ioBrowserAuth = api;
    if (root.document) {
        installAuthenticatedFetch(root);
        root.openAuthenticatedSseStream = (path) => openAuthenticatedSseStream(path, root);
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
