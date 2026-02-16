const test = require('node:test');
const assert = require('node:assert/strict');

const issueRowState = require('../../src/issue_orchestrator/static/js/issue_row_state.js');

test('computeIssueRowFingerprint is stable for identical row payload', () => {
    const row = { issue_number: 123, html: '<div class="issue-row-group" data-issue="123"></div>' };
    const first = issueRowState.computeIssueRowFingerprint(row);
    const second = issueRowState.computeIssueRowFingerprint(row);
    assert.equal(first, second);
});

test('computeIssueRowFingerprint changes when html changes', () => {
    const baseline = issueRowState.computeIssueRowFingerprint({
        issue_number: 123,
        html: '<div>old</div>',
    });
    const updated = issueRowState.computeIssueRowFingerprint({
        issue_number: 123,
        html: '<div>new</div>',
    });
    assert.notEqual(baseline, updated);
});

test('computeIssueRowFingerprint includes issue identity', () => {
    const a = issueRowState.computeIssueRowFingerprint({ issue_number: 1, html: '<div>x</div>' });
    const b = issueRowState.computeIssueRowFingerprint({ issue_number: 2, html: '<div>x</div>' });
    assert.notEqual(a, b);
});
