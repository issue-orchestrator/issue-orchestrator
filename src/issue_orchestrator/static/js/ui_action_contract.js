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
        HOST_OPEN_PATH: '/api/host/open-path',
        REVEAL_WORKTREE: (issueNumber) => `/api/host/reveal-worktree/${issueNumber}`,
        TERMINAL_RECORDING: (issueNumber) => `/api/session/terminal-recording/${issueNumber}`,
        RETRY_PUBLISH: (issueNumber) => `/api/issues/${issueNumber}/retry-publish`,
        CLOSE_ISSUE: (issueNumber) => `/api/issues/${issueNumber}/close`,
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

    function buildHostOpenPathRequest(path) {
        return {
            endpoint: ENDPOINTS.HOST_OPEN_PATH,
            method: 'POST',
            body: { path: String(path || '') },
        };
    }

    function buildRetryPublishRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for retry-publish action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.RETRY_PUBLISH(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    function buildCloseIssueRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for close-issue action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.CLOSE_ISSUE(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    function buildRevealWorktreeRequest(issueNumber) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for reveal-worktree action: ${issueNumber}`);
        }
        return {
            endpoint: ENDPOINTS.REVEAL_WORKTREE(normalized[0]),
            method: 'POST',
            body: {},
        };
    }

    function buildTerminalRecordingRequest(issueNumber, runDir, options = {}) {
        const normalized = normalizeIssueNumbers([issueNumber]);
        if (normalized.length !== 1) {
            throw new Error(`Invalid issue number for terminal recording action: ${issueNumber}`);
        }
        if (!runDir) {
            throw new Error('runDir is required for terminal recording action');
        }
        const params = new URLSearchParams();
        params.set('run_dir', String(runDir));
        if (options.offset !== undefined) {
            params.set('offset', String(options.offset));
        }
        if (options.limit !== undefined) {
            params.set('limit', String(options.limit));
        }
        const roundIndex = Number(options.round_index);
        if (Number.isInteger(roundIndex) && roundIndex > 0) {
            params.set('round_index', String(roundIndex));
        }
        const sessionRole = typeof options.session_role === 'string'
            ? options.session_role.trim()
            : '';
        if (sessionRole) {
            params.set('session_role', sessionRole);
        }
        const sinceHash = typeof options.since_hash === 'string' ? options.since_hash : '';
        if (sinceHash) {
            params.set('since_hash', sinceHash);
        }
        return {
            endpoint: `${ENDPOINTS.TERMINAL_RECORDING(normalized[0])}?${params.toString()}`,
            method: 'GET',
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
        buildRetryPublishRequest,
        buildCloseIssueRequest,
        buildHostOpenPathRequest,
        buildRevealWorktreeRequest,
        buildTerminalRecordingRequest,
    };
});
