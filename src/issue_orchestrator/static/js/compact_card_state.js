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
        // phase_age is intentionally excluded — see compute_compact_card_fingerprint
        // in view_models/dashboard_flow.py for the rationale.
        const cardId = card?.card_id ?? '';
        const issueNumber = card?.issue_number ?? '';
        const issueKey = card?.issue_key ?? '';
        const issueLabel = card?.issue_label ?? '';
        const title = card?.title ?? '';
        const stateLabel = card?.state_label ?? '';
        const phase = card?.phase ?? '';
        const summary = card?.summary ?? '';
        const stale = Boolean(card?.is_stale);
        const staleReason = card?.stale_reason ?? '';
        const issueUrl = card?.issue_url ?? '';
        const prUrl = card?.pr_url ?? '';
        const githubUrl = card?.github_url ?? '';
        const githubLabel = card?.github_label ?? '';
        const githubTitle = card?.github_title ?? '';
        const githubAriaLabel = card?.github_aria_label ?? '';
        const labels = normalizeLabels(card?.orchestrator_labels).join(',');
        return [
            cardId,
            issueNumber,
            issueKey,
            issueLabel,
            title,
            stateLabel,
            phase,
            summary,
            stale,
            staleReason,
            issueUrl,
            prUrl,
            githubUrl,
            githubLabel,
            githubTitle,
            githubAriaLabel,
            labels,
        ].join('|');
    }

    return {
        computeCompactCardFingerprint,
    };
});
