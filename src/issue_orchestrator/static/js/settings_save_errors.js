(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.settingsSaveErrors = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function normalizedText(value) {
        return typeof value === 'string' ? value.trim() : '';
    }

    function formatErrorDetail(item) {
        if (!item || typeof item !== 'object') return null;

        const name = normalizedText(item.name);
        const detail = normalizedText(item.detail);
        if (!name && !detail) return null;
        if (!name) return detail;
        if (!detail) return name;
        return `${name}: ${detail}`;
    }

    function formatSaveErrorMessage(result, fallbackMessage) {
        const fallback = normalizedText(fallbackMessage) || 'Failed to save settings';
        if (!result || typeof result !== 'object') return fallback;

        const summary = normalizedText(result.error) || fallback;
        const details = Array.isArray(result.errors)
            ? result.errors.map(formatErrorDetail).filter(Boolean)
            : [];

        if (details.length === 0) return summary;
        return `${summary}:\n- ${details.join('\n- ')}`;
    }

    return {
        formatErrorDetail,
        formatSaveErrorMessage,
    };
});
