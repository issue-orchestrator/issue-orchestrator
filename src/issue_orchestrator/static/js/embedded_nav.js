// Shared helper for preserving Control Center embedded context across
// same-window navigations within the dashboard (Dashboard ↔ Settings).
//
// The Control Center loads the dashboard iframe with ?embedded=1 plus theme
// (see control_center.js buildDashboardUrlFromBase). Dashboard and Settings
// both read these from the URL on load. When either page navigates to the
// other, the context params must be forwarded or the round-trip drops them
// (no "Back to repositories" button, wrong theme flash on cross-origin
// embeds where localStorage is not shared).
//
// This module is the single owner of the propagation rule. It is consumed
// by the dashboard JS bundle (via window.embeddedNav) and by the inline
// script in settings.html (via the same global). It is also loadable under
// Node's test runner via require() so URL transformations are verified as
// real behavior, not template strings.
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.embeddedNav = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const EMBEDDED_CONTEXT_PARAMS = Object.freeze(['embedded', 'theme']);
    const VALID_THEME_VALUES = new Set(['light', 'dark', 'system']);

    function normalizedTheme(value) {
        return VALID_THEME_VALUES.has(value) ? value : null;
    }

    function buildHref(basePath, search) {
        const source = new URLSearchParams(search || '');
        const preserved = new URLSearchParams();
        for (const key of EMBEDDED_CONTEXT_PARAMS) {
            const value = source.get(key);
            if (value !== null && value !== '') {
                preserved.set(key, value);
            }
        }
        const query = preserved.toString();
        return query ? basePath + '?' + query : basePath;
    }

    // Single resolver for the effective theme across Dashboard and Settings.
    // Precedence: explicit override (postMessage from CC) > ?theme= URL > stored
    // localStorage preference > 'system'. A 'system' raw value is resolved to
    // 'dark' or 'light' using the caller-supplied prefersDark flag.
    //
    // Kept pure (no DOM / localStorage / matchMedia access) so it can be
    // verified under Node's test runner alongside buildHref.
    function resolveEffectiveTheme(opts) {
        const { override, search, storedTheme, prefersDark } = opts || {};
        const urlTheme = new URLSearchParams(search || '').get('theme');
        const raw = (
            normalizedTheme(override)
            || normalizedTheme(urlTheme)
            || normalizedTheme(storedTheme)
            || 'system'
        );
        if (raw === 'system') {
            return prefersDark ? 'dark' : 'light';
        }
        return raw;
    }

    return {
        EMBEDDED_CONTEXT_PARAMS,
        buildHref,
        resolveEffectiveTheme,
    };
});
