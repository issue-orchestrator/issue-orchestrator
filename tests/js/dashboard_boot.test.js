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

test('installBootCleanup installs a load fallback while document is loading', () => {
    let loadHandler = null;
    const removed = [];
    const root = {
        document: {
            readyState: 'loading',
            documentElement: {
                removeAttribute(name) {
                    removed.push(name);
                },
            },
        },
        addEventListener(type, handler, options) {
            assert.equal(type, 'load');
            assert.deepEqual(options, { once: true });
            loadHandler = handler;
        },
    };

    dashboardBoot.installBootCleanup(root);
    assert.equal(typeof loadHandler, 'function');
    assert.deepEqual(removed, []);
    loadHandler();
    assert.deepEqual(removed, ['data-booting']);
});
