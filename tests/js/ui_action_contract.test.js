const test = require('node:test');
const assert = require('node:assert/strict');

const uiActionContract = require('../../src/issue_orchestrator/static/js/ui_action_contract.js');

test('buildUnblockRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildUnblockRequest([4057, '12', 0, 'x']);
    assert.equal(req.endpoint, '/api/unblock-retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issues: [4057, 12] });
});

test('normalizeIssueNumbers rejects invalid issue numbers', () => {
    assert.deepEqual(
        uiActionContract.normalizeIssueNumbers([1, -1, 0, '3', 'x', null, 4.5]),
        [1, 3],
    );
});

test('buildResetRetryRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildResetRetryRequest([10, '11']);
    assert.equal(req.endpoint, '/api/reset-retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issues: [10, 11], from_scratch: false });
});

test('buildBulkDeprioritizeRequest returns canonical endpoint and payload', () => {
    const req = uiActionContract.buildBulkDeprioritizeRequest([99, 'x', 100]);
    assert.equal(req.endpoint, '/api/bulk-deprioritize');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, { issue_numbers: [99, 100] });
});

test('buildIssueRetryRequest returns issue-specific endpoint', () => {
    const req = uiActionContract.buildIssueRetryRequest('4057');
    assert.equal(req.endpoint, '/api/issues/4057/retry');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, {});
});

test('buildRetryPublishRequest returns issue-specific endpoint', () => {
    const req = uiActionContract.buildRetryPublishRequest('4057');
    assert.equal(req.endpoint, '/api/issues/4057/retry-publish');
    assert.equal(req.method, 'POST');
    assert.deepEqual(req.body, {});
});

test('buildTerminalRecordingRequest returns canonical endpoint and query params', () => {
    const req = uiActionContract.buildTerminalRecordingRequest(4057, '/tmp/run', { offset: 10, limit: 0 });
    assert.equal(
        req.endpoint,
        '/api/session/terminal-recording/4057?run_dir=%2Ftmp%2Frun&offset=10&limit=0',
    );
    assert.equal(req.method, 'GET');
});
