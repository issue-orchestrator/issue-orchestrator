(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.dashboardBoot = api;
        if (root.document) {
            api.applyInitialDocumentState({
                documentElement: root.document.documentElement,
                search: root.location ? root.location.search : '',
                storedTheme: api.readStoredTheme(root.localStorage),
                prefersDark: api.prefersDark(root.matchMedia),
            });
        }
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const VALID_THEME_VALUES = new Set(['light', 'dark', 'system']);

    function normalizedTheme(value) {
        return VALID_THEME_VALUES.has(value) ? value : null;
    }

    function readStoredTheme(storage) {
        try {
            return storage ? storage.getItem('theme') : null;
        } catch (_error) {
            return null;
        }
    }

    function prefersDark(matchMediaFn) {
        try {
            return Boolean(
                matchMediaFn
                && matchMediaFn('(prefers-color-scheme: dark)').matches
            );
        } catch (_error) {
            return false;
        }
    }

    function resolveEffectiveTheme(opts) {
        const { override, search, storedTheme, prefersDark: darkPreferred } = opts || {};
        const urlTheme = new URLSearchParams(search || '').get('theme');
        const rawTheme = (
            normalizedTheme(override)
            || normalizedTheme(urlTheme)
            || normalizedTheme(storedTheme)
            || 'system'
        );
        if (rawTheme === 'system') {
            return darkPreferred ? 'dark' : 'light';
        }
        return rawTheme;
    }

    function resolveInitialDocumentState(opts) {
        const search = opts?.search || '';
        const params = new URLSearchParams(search);
        return {
            embedded: params.get('embedded') === '1',
            theme: resolveEffectiveTheme(opts),
        };
    }

    function applyInitialDocumentState(opts) {
        const documentElement = opts?.documentElement;
        if (!documentElement) {
            return resolveInitialDocumentState(opts);
        }
        const state = resolveInitialDocumentState(opts);
        documentElement.setAttribute('data-booting', 'true');
        documentElement.setAttribute('data-theme', state.theme);
        if (state.embedded) {
            documentElement.setAttribute('data-embedded', 'true');
        } else {
            documentElement.removeAttribute('data-embedded');
        }
        return state;
    }

    return {
        applyInitialDocumentState,
        prefersDark,
        readStoredTheme,
        resolveEffectiveTheme,
        resolveInitialDocumentState,
    };
});
