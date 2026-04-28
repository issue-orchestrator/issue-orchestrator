const test = require('node:test');
const assert = require('node:assert/strict');

const embeddedNav = require('../../src/issue_orchestrator/static/js/embedded_nav.js');

test('EMBEDDED_CONTEXT_PARAMS covers embedded and theme', () => {
    assert.deepEqual(
        [...embeddedNav.EMBEDDED_CONTEXT_PARAMS],
        ['embedded', 'theme'],
    );
});

test('buildHref preserves both embedded and theme on Dashboard → Settings', () => {
    assert.equal(
        embeddedNav.buildHref('/settings', '?embedded=1&theme=dark'),
        '/settings?embedded=1&theme=dark',
    );
});

test('buildHref preserves both embedded and theme on Settings → Dashboard', () => {
    assert.equal(
        embeddedNav.buildHref('/', '?embedded=1&theme=light'),
        '/?embedded=1&theme=light',
    );
});

test('buildHref keeps embedded when no theme is present', () => {
    assert.equal(
        embeddedNav.buildHref('/', '?embedded=1'),
        '/?embedded=1',
    );
});

test('buildHref preserves theme even without embedded', () => {
    // Not a common real-world URL, but the rule is "carry context params
    // that exist", not "require embedded".
    assert.equal(
        embeddedNav.buildHref('/', '?theme=dark'),
        '/?theme=dark',
    );
});

test('buildHref returns bare base path when no context params exist', () => {
    assert.equal(embeddedNav.buildHref('/', ''), '/');
    assert.equal(embeddedNav.buildHref('/settings', '?tab=e2e'), '/settings');
});

test('buildHref drops dashboard-internal params that are not embedded context', () => {
    assert.equal(
        embeddedNav.buildHref('/settings', '?embedded=1&theme=dark&tab=e2e&page=3'),
        '/settings?embedded=1&theme=dark',
    );
});

test('buildHref tolerates a search string without the leading ?', () => {
    assert.equal(
        embeddedNav.buildHref('/', 'embedded=1&theme=dark'),
        '/?embedded=1&theme=dark',
    );
});

test('buildHref drops empty-valued context params', () => {
    // An empty theme should not produce a dangling theme= on the target URL.
    assert.equal(
        embeddedNav.buildHref('/', '?embedded=1&theme='),
        '/?embedded=1',
    );
});

test('buildHref is a pure transformation (no mutation of inputs)', () => {
    const search = '?embedded=1&theme=dark&tab=e2e';
    embeddedNav.buildHref('/settings', search);
    embeddedNav.buildHref('/settings', search);
    assert.equal(search, '?embedded=1&theme=dark&tab=e2e');
});

// resolveEffectiveTheme — shared precedence across Dashboard + Settings.

test('resolveEffectiveTheme prefers explicit override over URL and storage', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            override: 'light',
            search: '?theme=dark',
            storedTheme: 'dark',
            prefersDark: true,
        }),
        'light',
    );
});

test('resolveEffectiveTheme prefers URL theme over stored/system', () => {
    // This is the behavior Settings was missing: CC passes ?theme=dark but
    // local storage says 'light' → URL must win for embedded consistency.
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '?embedded=1&theme=dark',
            storedTheme: 'light',
            prefersDark: false,
        }),
        'dark',
    );
});

test('resolveEffectiveTheme uses stored theme when URL has no theme', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '?embedded=1',
            storedTheme: 'dark',
            prefersDark: false,
        }),
        'dark',
    );
});

test('resolveEffectiveTheme resolves "system" to dark when prefersDark', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '',
            storedTheme: 'system',
            prefersDark: true,
        }),
        'dark',
    );
});

test('resolveEffectiveTheme resolves "system" to light when not prefersDark', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '',
            storedTheme: 'system',
            prefersDark: false,
        }),
        'light',
    );
});

test('resolveEffectiveTheme falls back to system when nothing is set', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '',
            storedTheme: null,
            prefersDark: true,
        }),
        'dark',
    );
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '',
            storedTheme: null,
            prefersDark: false,
        }),
        'light',
    );
});

test('resolveEffectiveTheme propagates explicit "system" through matchMedia', () => {
    // An explicit URL theme of 'system' should still honor prefersDark,
    // not short-circuit back to stored.
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            search: '?theme=system',
            storedTheme: 'dark',
            prefersDark: false,
        }),
        'light',
    );
});

test('resolveEffectiveTheme ignores invalid theme values', () => {
    assert.equal(
        embeddedNav.resolveEffectiveTheme({
            override: 'sepia',
            search: '?theme=solarized',
            storedTheme: 'light',
            prefersDark: true,
        }),
        'light',
    );
});

test('resolveEffectiveTheme is robust to missing opts', () => {
    // No opts at all: treat as fully unspecified → system → depends on prefersDark
    // (defaults to falsy prefersDark → light).
    assert.equal(embeddedNav.resolveEffectiveTheme(), 'light');
    assert.equal(embeddedNav.resolveEffectiveTheme({}), 'light');
});
