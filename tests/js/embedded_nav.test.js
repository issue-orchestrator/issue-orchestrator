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
