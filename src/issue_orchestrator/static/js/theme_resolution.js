(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.themeResolution = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const VALID_THEME_VALUES = new Set(['light', 'dark', 'system']);

    function normalizedTheme(value) {
        return VALID_THEME_VALUES.has(value) ? value : null;
    }

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
        VALID_THEME_VALUES,
        normalizedTheme,
        resolveEffectiveTheme,
    };
});
