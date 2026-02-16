const test = require('node:test');
const assert = require('node:assert/strict');

const compactCardState = require('../../src/issue_orchestrator/static/js/compact_card_state.js');

test('computeCompactCardFingerprint is stable for identical card payload', () => {
    const card = {
        issue_number: 10,
        title: 'Fix stale UI',
        state_label: 'Queued',
        phase: 'Queued',
        phase_age: '2m',
        summary: 'Summary: Blocked',
        is_stale: false,
        stale_reason: '',
        issue_url: 'https://example.test/10',
        orchestrator_labels: ['agent:backend', 'blocked'],
    };
    const first = compactCardState.computeCompactCardFingerprint(card);
    const second = compactCardState.computeCompactCardFingerprint(card);
    assert.equal(first, second);
});

test('computeCompactCardFingerprint ignores volatile freshness age fields', () => {
    const baseline = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Fix stale UI',
        phase: 'Queued',
        phase_age: '2m',
        orchestrator_labels: ['agent:backend'],
        last_refreshed_age_seconds: 12,
    });
    const changedAge = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Fix stale UI',
        phase: 'Queued',
        phase_age: '2m',
        orchestrator_labels: ['agent:backend'],
        last_refreshed_age_seconds: 48,
    });
    assert.equal(baseline, changedAge);
});

test('computeCompactCardFingerprint changes when rendered fields change', () => {
    const baseline = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'A',
        phase: 'Queued',
        orchestrator_labels: ['agent:backend'],
    });
    const changed = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'B',
        phase: 'Queued',
        orchestrator_labels: ['agent:backend'],
    });
    assert.notEqual(baseline, changed);
});
