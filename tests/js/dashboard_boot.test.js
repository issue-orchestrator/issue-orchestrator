const test = require('node:test');
const assert = require('node:assert/strict');

const dashboardBoot = require('../../src/issue_orchestrator/static/js/dashboard_boot.js');
const themeResolution = require('../../src/issue_orchestrator/static/js/theme_resolution.js');

test('dashboard boot delegates theme resolution to the shared helper', () => {
    assert.equal(dashboardBoot.resolveEffectiveTheme, themeResolution.resolveEffectiveTheme);
});

test('resolveInitialDocumentState resolves theme and embedded mode from first-paint inputs', () => {
    assert.deepEqual(
        dashboardBoot.resolveInitialDocumentState({
            search: '?embedded=1&theme=light',
            storedTheme: 'dark',
            prefersDark: true,
        }),
        { embedded: true, theme: 'light' },
    );
});

test('resolveInitialDocumentState uses stored theme before system preference', () => {
    assert.deepEqual(
        dashboardBoot.resolveInitialDocumentState({
            search: '',
            storedTheme: 'light',
            prefersDark: true,
        }),
        { embedded: false, theme: 'light' },
    );
});

test('resolveInitialDocumentState falls back to system for invalid theme values', () => {
    assert.deepEqual(
        dashboardBoot.resolveInitialDocumentState({
            search: '?theme=sepia',
            storedTheme: 'solarized',
            prefersDark: false,
        }),
        { embedded: false, theme: 'light' },
    );
});

test('applyInitialDocumentState writes all pre-paint document attributes', () => {
    const attributes = new Map();
    const documentElement = {
        setAttribute(name, value) {
            attributes.set(name, value);
        },
        removeAttribute(name) {
            attributes.delete(name);
        },
    };

    const state = dashboardBoot.applyInitialDocumentState({
        documentElement,
        search: '?embedded=1&theme=dark',
        storedTheme: 'light',
        prefersDark: false,
    });

    assert.deepEqual(state, { embedded: true, theme: 'dark' });
    assert.equal(attributes.get('data-booting'), 'true');
    assert.equal(attributes.get('data-theme'), 'dark');
    assert.equal(attributes.get('data-embedded'), 'true');
});

test('readStoredTheme returns null when localStorage is unavailable', () => {
    assert.equal(
        dashboardBoot.readStoredTheme({
            getItem() {
                throw new Error('blocked');
            },
        }),
        null,
    );
});

test('getLocalStorage returns null when window property access is blocked', () => {
    const root = {};
    Object.defineProperty(root, 'localStorage', {
        get() {
            throw new Error('blocked');
        },
    });

    assert.equal(dashboardBoot.getLocalStorage(root), null);
});

test('clearBootingWhenStable removes data-booting after two animation frames', () => {
    const removed = [];
    const frameCallbacks = [];
    const root = {
        document: {
            documentElement: {
                removeAttribute(name) {
                    removed.push(name);
                },
            },
        },
        requestAnimationFrame(callback) {
            frameCallbacks.push(callback);
        },
    };

    dashboardBoot.clearBootingWhenStable(root);
    assert.deepEqual(removed, []);
    frameCallbacks.shift()();
    assert.deepEqual(removed, []);
    frameCallbacks.shift()();
    assert.deepEqual(removed, ['data-booting']);
});

test('installBootCleanup schedules a setTimeout fallback (no load handler)', () => {
    // The boot path now relies on core.js (dashboard.js) clearing
    // `data-booting` *after* the first refreshViewModel resolves; clearing
    // on the `load` event would re-enable CSS transitions mid-render and
    // re-introduce the dashboard-open flash. This test guards against
    // re-introducing a load-event clear and verifies a setTimeout is
    // scheduled as a safety fallback in case core.js never runs.
    let timeoutHandler = null;
    let timeoutMs = null;
    let loadHandlerAttached = false;
    const removed = [];
    let frameCallbacks = [];
    const root = {
        document: {
            readyState: 'loading',
            documentElement: {
                removeAttribute(name) {
                    removed.push(name);
                },
            },
        },
        addEventListener() {
            loadHandlerAttached = true;
        },
        setTimeout(handler, ms) {
            timeoutHandler = handler;
            timeoutMs = ms;
        },
        requestAnimationFrame(callback) {
            frameCallbacks.push(callback);
        },
    };

    dashboardBoot.installBootCleanup(root);
    assert.equal(loadHandlerAttached, false, 'must not attach a load handler');
    assert.equal(typeof timeoutHandler, 'function');
    assert.ok(timeoutMs >= 5000, `fallback timeout must be >=5s, was ${timeoutMs}ms`);
    assert.deepEqual(removed, []);

    // When the fallback fires it should clear via clearBootingWhenStable
    // (i.e. two RAFs before removing the attribute).
    timeoutHandler();
    assert.deepEqual(removed, []);
    frameCallbacks.shift()();
    assert.deepEqual(removed, []);
    frameCallbacks.shift()();
    assert.deepEqual(removed, ['data-booting']);
});
