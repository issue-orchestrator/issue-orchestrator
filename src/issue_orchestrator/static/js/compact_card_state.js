(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.compactCardState = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function normalizeLabels(labels) {
        if (!Array.isArray(labels)) return [];
        return labels.map(String);
    }

    function computeCompactCardFingerprint(card) {
        const issueNumber = card?.issue_number ?? '';
        const title = card?.title ?? '';
        const stateLabel = card?.state_label ?? '';
        const phase = card?.phase ?? '';
        const phaseAge = card?.phase_age ?? '';
        const summary = card?.summary ?? '';
        const stale = Boolean(card?.is_stale);
        const staleReason = card?.stale_reason ?? '';
        const issueUrl = card?.issue_url ?? '';
        const labels = normalizeLabels(card?.orchestrator_labels).join(',');
        return [
            issueNumber,
            title,
            stateLabel,
            phase,
            phaseAge,
            summary,
            stale,
            staleReason,
            issueUrl,
            labels,
        ].join('|');
    }

    return {
        computeCompactCardFingerprint,
    };
});
