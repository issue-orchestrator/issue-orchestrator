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
        show_stale_badge: false,
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
        show_stale_badge: false,
        last_refreshed_age_seconds: 12,
    });
    const changedAge = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Fix stale UI',
        phase: 'Queued',
        phase_age: '2m',
        orchestrator_labels: ['agent:backend'],
        show_stale_badge: false,
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
        show_stale_badge: false,
    });
    const changed = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'B',
        phase: 'Queued',
        orchestrator_labels: ['agent:backend'],
        show_stale_badge: false,
    });
    assert.notEqual(baseline, changed);
});

test('computeCompactCardFingerprint changes when issue label gains a logical key', () => {
    // Without the label in the fingerprint, switching from "#4057" to
    // "M9-009 · #4057" would skip the rebuild and leave a stale label.
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 4057,
        title: 'Surface circuit breaker',
        issue_label: '#4057',
        show_stale_badge: false,
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 4057,
        title: 'Surface circuit breaker',
        issue_key: 'M9-009',
        issue_label: 'M9-009 · #4057',
        show_stale_badge: false,
    });
    assert.notEqual(before, after);
});

test('computeCompactCardFingerprint ignores phase_age tick changes', () => {
    // Including phase_age (a relative time string that ticks every few
    // seconds) in the fingerprint forces every running card's DOM node
    // to be replaced on every refresh, which the user perceives as a
    // flash. The phase-age text is synced in place by renderCompactCards
    // when the rest of the fingerprint matches.
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Running task',
        phase: 'Coding',
        phase_age: '2s',
        state_label: 'running',
        show_stale_badge: false,
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Running task',
        phase: 'Coding',
        phase_age: '12s',
        state_label: 'running',
        show_stale_badge: false,
    });
    assert.equal(before, after);
});

test('computeCompactCardFingerprint changes when the stack signal changes', () => {
    // The server encodes the chip's rendered chain context (predecessor and
    // successor refs) into stack_signal. A base moving from "before #30" to
    // "before #31" — same gates, same successor count — changes the signal and
    // must re-fingerprint the card, or the reused DOM node keeps stale chain
    // text in the chip title.
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Stacked slice',
        show_stale_badge: false,
        stack_signal: 'stack::#20',
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Stacked slice',
        show_stale_badge: false,
        stack_signal: 'stack::#21',
    });
    assert.notEqual(before, after);
});

test('computeCompactCardFingerprint changes when run_dir changes', () => {
    // A running card whose run directory advanced (e.g. a rework-<issue> slot
    // reused by a new run) must re-fingerprint. Without run_dir in the
    // fingerprint, renderCompactCards reuses the DOM node and the launch-prompt
    // action reads a stale data-run-dir.
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 202,
        title: 'Rework run',
        phase: 'Coding',
        state_label: 'running',
        show_stale_badge: false,
        run_dir: '/runs/rework-202/run-a',
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 202,
        title: 'Rework run',
        phase: 'Coding',
        state_label: 'running',
        show_stale_badge: false,
        run_dir: '/runs/rework-202/run-b',
    });
    assert.notEqual(before, after);
});

test('computeCompactCardFingerprint still ignores phase_age when run_dir is held constant', () => {
    // run_dir and phase_age volatility rules are independent: a run_dir change
    // re-fingerprints (previous test), but a phase_age tick with an unchanged
    // run_dir must still be a no-op so the node is reused.
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 202,
        title: 'Rework run',
        phase: 'Coding',
        phase_age: '2s',
        state_label: 'running',
        show_stale_badge: false,
        run_dir: '/runs/rework-202/run-a',
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 202,
        title: 'Rework run',
        phase: 'Coding',
        phase_age: '12s',
        state_label: 'running',
        show_stale_badge: false,
        run_dir: '/runs/rework-202/run-a',
    });
    assert.equal(before, after);
});

test('computeCompactCardFingerprint changes when stale badge visibility changes', () => {
    const before = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Terminal task',
        phase: 'Done',
        is_stale: true,
        show_stale_badge: false,
    });
    const after = compactCardState.computeCompactCardFingerprint({
        issue_number: 10,
        title: 'Terminal task',
        phase: 'Done',
        is_stale: true,
        show_stale_badge: true,
    });
    assert.notEqual(before, after);
});
