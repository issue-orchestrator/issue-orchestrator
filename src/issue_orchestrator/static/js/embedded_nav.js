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
    const themeResolution = typeof module === 'object' && module.exports
        ? require('./theme_resolution.js')
        : root.themeResolution;
    const api = factory(themeResolution);
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.embeddedNav = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (themeResolution) {
    if (!themeResolution) {
        throw new Error('themeResolution helper not loaded');
    }
    const EMBEDDED_CONTEXT_PARAMS = Object.freeze(['embedded', 'theme']);

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

    // Rewrite every server-rendered Settings anchor so it carries the embedded
    // context params, using the same rule as buildHref. Templates render these
    // links with a plain href="/settings" (so they work without JS and in
    // standalone mode); on load the dashboard hands the document to this owner
    // and the href is upgraded in place. This keeps the Dashboard → Settings
    // propagation rule owned here rather than duplicated at each link.
    function applySettingsLinks(root, search) {
        if (!root || typeof root.querySelectorAll !== 'function') {
            return;
        }
        const href = buildHref('/settings', search);
        for (const link of root.querySelectorAll('a[data-embedded-settings-link]')) {
            link.setAttribute('href', href);
        }
    }

    // Single resolver for the effective theme across Dashboard and Settings.
    // Precedence: explicit override (postMessage from CC) > ?theme= URL > stored
    // localStorage preference > 'system'. A 'system' raw value is resolved to
    // 'dark' or 'light' using the caller-supplied prefersDark flag.
    //
    // Kept pure (no DOM / localStorage / matchMedia access) so it can be
    // verified under Node's test runner alongside buildHref.
    function resolveEffectiveTheme(opts) {
        return themeResolution.resolveEffectiveTheme(opts);
    }

    return {
        EMBEDDED_CONTEXT_PARAMS,
        buildHref,
        applySettingsLinks,
        resolveEffectiveTheme,
    };
});
