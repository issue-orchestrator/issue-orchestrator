(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.issueRowState = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function computeIssueRowFingerprint(row) {
        const issueNumber = row?.issue_number ?? '';
        const html = row?.html ?? '';
        return `${issueNumber}|${html}`;
    }

    return {
        computeIssueRowFingerprint,
    };
});
