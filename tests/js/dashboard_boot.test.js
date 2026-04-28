const test = require('node:test');
const assert = require('node:assert/strict');

const dashboardBoot = require('../../src/issue_orchestrator/static/js/dashboard_boot.js');
const embeddedNav = require('../../src/issue_orchestrator/static/js/embedded_nav.js');

test('resolveEffectiveTheme matches embedded nav for valid dashboard theme inputs', () => {
    const cases = [
        { search: '?theme=light', storedTheme: 'dark', prefersDark: true },
        { search: '?theme=dark', storedTheme: 'light', prefersDark: false },
        { search: '', storedTheme: 'system', prefersDark: true },
        { search: '', storedTheme: 'system', prefersDark: false },
        { override: 'light', search: '?theme=dark', storedTheme: 'dark', prefersDark: true },
    ];
    for (const opts of cases) {
        assert.equal(
            dashboardBoot.resolveEffectiveTheme(opts),
            embeddedNav.resolveEffectiveTheme(opts),
        );
    }
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
