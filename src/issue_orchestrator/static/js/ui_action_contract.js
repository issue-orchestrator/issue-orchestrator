(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.uiActionContract = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const ENDPOINTS = {
        UNBLOCK_RETRY: '/api/unblock-retry',
        RESET_RETRY: '/api/reset-retry',
        BULK_RETRY: '/api/bulk-retry',
        BULK_DEPRIORITIZE: '/api/bulk-deprioritize',
        BULK_CANCEL_QUEUED: '/api/bulk-cancel-queued',
    };

    function normalizeIssueNumbers(issueNumbers) {
        if (!Array.isArray(issueNumbers)) return [];
        return issueNumbers
            .map((value) => Number(value))
            .filter((value) => Number.isInteger(value) && value > 0);
    }

    function buildUnblockRequest(issueNumbers) {
        const issues = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.UNBLOCK_RETRY,
            method: 'POST',
            body: { issues },
        };
    }

    function buildResetRetryRequest(issueNumbers, options = {}) {
        const issues = normalizeIssueNumbers(issueNumbers);
        const fromScratch = Boolean(options.fromScratch);
        return {
            endpoint: ENDPOINTS.RESET_RETRY,
            method: 'POST',
            body: { issues, from_scratch: fromScratch },
        };
    }

    function buildBulkDeprioritizeRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_DEPRIORITIZE,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildBulkCancelQueuedRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_CANCEL_QUEUED,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildBulkRetryRequest(issueNumbers) {
        const issue_numbers = normalizeIssueNumbers(issueNumbers);
        return {
            endpoint: ENDPOINTS.BULK_RETRY,
            method: 'POST',
            body: { issue_numbers },
        };
    }

    function buildIssueRetryRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        return {
            endpoint: `/api/issues/${normalized[0] || 0}/retry`,
            method: 'POST',
            body: {},
        };
    }

    return {
        ENDPOINTS,
        normalizeIssueNumbers,
        buildUnblockRequest,
        buildResetRetryRequest,
        buildBulkRetryRequest,
        buildBulkDeprioritizeRequest,
        buildBulkCancelQueuedRequest,
        buildIssueRetryRequest,
    };
});
