const test = require('node:test');
const assert = require('node:assert/strict');

const themeResolution = require('../../src/issue_orchestrator/static/js/theme_resolution.js');

test('resolveEffectiveTheme prefers override over URL and storage', () => {
    assert.equal(
        themeResolution.resolveEffectiveTheme({
            override: 'light',
            search: '?theme=dark',
            storedTheme: 'dark',
            prefersDark: true,
        }),
        'light',
    );
});

test('resolveEffectiveTheme resolves system from media preference', () => {
    assert.equal(
        themeResolution.resolveEffectiveTheme({
            search: '?theme=system',
            storedTheme: 'dark',
            prefersDark: false,
        }),
        'light',
    );
});

test('resolveEffectiveTheme ignores invalid values and falls back', () => {
    assert.equal(
        themeResolution.resolveEffectiveTheme({
            override: 'sepia',
            search: '?theme=solarized',
            storedTheme: 'light',
            prefersDark: true,
        }),
        'light',
    );
});
