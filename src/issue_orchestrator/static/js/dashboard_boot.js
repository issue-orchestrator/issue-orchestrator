(function (root, factory) {
    const themeResolution = typeof module === 'object' && module.exports
        ? require('./theme_resolution.js')
        : root.themeResolution;
    const api = factory(themeResolution);
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.dashboardBoot = api;
        if (root.document) {
            api.applyInitialDocumentState({
                documentElement: root.document.documentElement,
                search: root.location ? root.location.search : '',
                storedTheme: api.readStoredTheme(api.getLocalStorage(root)),
                prefersDark: api.prefersDark(api.getMatchMedia(root)),
            });
            api.installBootCleanup(root);
        }
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (themeResolution) {
    if (!themeResolution) {
        throw new Error('themeResolution helper not loaded');
    }

    function readStoredTheme(storage) {
        try {
            return storage ? storage.getItem('theme') : null;
        } catch (_error) {
            return null;
        }
    }

    function getLocalStorage(root) {
        try {
            return root?.localStorage || null;
        } catch (_error) {
            return null;
        }
    }

    function getMatchMedia(root) {
        try {
            return root?.matchMedia || null;
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

    function resolveInitialDocumentState(opts) {
        const search = opts?.search || '';
        const params = new URLSearchParams(search);
        return {
            embedded: params.get('embedded') === '1',
            theme: themeResolution.resolveEffectiveTheme(opts),
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

    function clearBootingWhenStable(root) {
        const documentElement = root?.document?.documentElement;
        if (!documentElement) return;
        const finish = () => documentElement.removeAttribute('data-booting');
        if (typeof root.requestAnimationFrame === 'function') {
            root.requestAnimationFrame(() => root.requestAnimationFrame(finish));
        } else {
            finish();
        }
    }

    function installBootCleanup(root) {
        if (!root?.document) return;
        // Safety net only: if the dashboard boot path (core.js's
        // DOMContentLoaded handler that awaits the first refresh) never
        // runs to completion — for instance because a script failed to
        // load — clear `data-booting` after this fallback so the UI is
        // not permanently locked into the no-transitions state. Under
        // normal boot, core.js calls `clearBootingWhenStable` itself
        // after the first `refreshViewModel` resolves; if that races
        // with this fallback, both calls are idempotent.
        const FALLBACK_MS = 8000;
        try {
            (root.setTimeout || setTimeout)(() => clearBootingWhenStable(root), FALLBACK_MS);
        } catch (_error) {
            clearBootingWhenStable(root);
        }
    }

    return {
        applyInitialDocumentState,
        clearBootingWhenStable,
        getLocalStorage,
        getMatchMedia,
        installBootCleanup,
        prefersDark,
        readStoredTheme,
        resolveEffectiveTheme: themeResolution.resolveEffectiveTheme,
        resolveInitialDocumentState,
    };
});
