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

    function isAuthExpiredResponse(response) {
        if (!response || response.status !== 401) return false;
        const url = typeof response.url === 'string' ? response.url : '';
        return url.includes('/api/') || url.includes('/control/');
    }

    const OVERLAY_ID = 'io-auth-expired-overlay';

    function showAuthExpiredOverlay(target = root) {
        const doc = target && target.document;
        if (!doc || typeof doc.createElement !== 'function' || !doc.body) {
            return false;
        }
        if (doc.getElementById && doc.getElementById(OVERLAY_ID)) {
            return false;
        }
        const overlay = doc.createElement('div');
        overlay.id = OVERLAY_ID;
        overlay.setAttribute('role', 'alertdialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-labelledby', `${OVERLAY_ID}-title`);
        // Inline styles so the overlay renders even before CSS loads or
        // when the dashboard markup is in a partially-rendered state.
        overlay.style.cssText = [
            'position:fixed', 'inset:0', 'z-index:2147483647',
            'background:rgba(15,20,25,0.92)',
            'display:flex', 'align-items:center', 'justify-content:center',
            'font-family:sans-serif', 'color:#e6e6e6',
        ].join(';');
        const card = doc.createElement('div');
        card.style.cssText = [
            'background:#1c2330', 'padding:32px', 'border-radius:8px',
            'min-width:320px', 'max-width:420px',
            'box-shadow:0 4px 16px rgba(0,0,0,0.4)',
            'text-align:left',
        ].join(';');
        const title = doc.createElement('h1');
        title.id = `${OVERLAY_ID}-title`;
        title.textContent = 'Session expired';
        title.style.cssText = 'margin:0 0 12px;font-size:20px';
        const body = doc.createElement('p');
        body.textContent = 'Your sign-in is no longer valid. Sign in again to continue.';
        body.style.cssText = 'margin:0 0 20px;color:#9aa5b1;font-size:13px;line-height:1.4';
        const button = doc.createElement('button');
        button.type = 'button';
        button.id = `${OVERLAY_ID}-signin`;
        button.textContent = 'Sign in';
        button.style.cssText = [
            'width:100%', 'padding:10px', 'border:0', 'border-radius:4px',
            'background:#3b82f6', 'color:#fff', 'font-weight:600',
            'cursor:pointer', 'font-size:14px',
        ].join(';');
        button.addEventListener('click', () => {
            if (target.location && typeof target.location.assign === 'function') {
                target.location.assign('/');
            }
        });
        card.appendChild(title);
        card.appendChild(body);
        card.appendChild(button);
        overlay.appendChild(card);
        doc.body.appendChild(overlay);
        if (typeof button.focus === 'function') {
            try { button.focus(); } catch (_e) { /* focus is best-effort */ }
        }
        return true;
    }

    function maybeShowAuthExpiredOverlay(response, target = root) {
        if (!isAuthExpiredResponse(response)) return false;
        if (target.__ioBrowserAuthReloadTriggered) return false;
        target.__ioBrowserAuthReloadTriggered = true;
        return showAuthExpiredOverlay(target);
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
                maybeShowAuthExpiredOverlay(response, target);
            }
            return response;
        };
    }

    const api = {
        buildFetchOptions,
        buildSseUrl,
        getCsrfToken,
        installAuthenticatedFetch,
        isAuthExpiredResponse,
        isBrowserAuthRequired,
        maybeShowAuthExpiredOverlay,
        openAuthenticatedSseStream,
        resolveRequestMethod,
        showAuthExpiredOverlay,
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
