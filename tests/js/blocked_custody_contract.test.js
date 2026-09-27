// Blocked-item custody payloads (#7331) against the REAL generated validators.
//
// The custody owner derives each blocked item's state server-side; the browser
// only renders it. These cases pin the contract the browser reads that answer
// through: every state word it may receive, and the malformed shapes that must
// be rejected before any handler sees them.

const test = require('node:test');
const assert = require('node:assert');

const validators = require('../../src/issue_orchestrator/static/js/ui-contracts.validators.js');

function charter(overrides = {}) {
    return {
        decision_id: 'decision:run-1:A1',
        role: 'flow',
        action: 'kill hung session',
        required_depth: 'workaround',
        role_enabled: true,
        role_depth: 'restructure',
        role_authority: 'execute',
        action_ceiling: 'propose',
        ceiling_source: 'tech_lead.authority.kill_hung_session',
        outcome: 'proposed',
        outcome_label: 'Proposed, awaiting approval',
        lifecycle_label: 'awaiting approval',
        reason: 'flow may act, but kill_hung_session is set to propose',
        decided_at: '2026-09-27T11:00:00+00:00',
        proposal_issue_number: 700,
        ...overrides,
    };
}

function custody(overrides = {}) {
    return {
        state: 'waiting_on_you',
        label: 'Waiting on you',
        owner: 'you',
        reason: 'Proposal #700 (kill hung session) awaits your approval.',
        tone: 'you',
        since: '2026-09-27T10:00:00+00:00',
        since_basis: 'proposal filed',
        age_label: '2h',
        stale: false,
        stale_after_label: '1d',
        needs_attention: false,
        attention_text: '',
        charter: charter(),
        ...overrides,
    };
}

function summary(overrides = {}) {
    return {
        total: 2,
        needs_attention: 1,
        unowned: 1,
        stale: 0,
        headline: '1 of 2 blocked items need attention (1 unowned).',
        by_state: [
            { state: 'unowned', label: 'Unowned', count: 1 },
            { state: 'waiting_on_you', label: 'Waiting on you', count: 1 },
        ],
        ...overrides,
    };
}

function assertRejects(schemaName, value) {
    const result = validators.validate(schemaName, value);
    assert.strictEqual(result.ok, false, `expected rejection of ${JSON.stringify(value)}`);
    assert.strictEqual(result.value, null);
}

test('every custody state the owner can decide is accepted', () => {
    const states = [
        'unowned', 'queued_for_tech_lead', 'investigating', 'waiting_on_you',
        'being_fixed', 'waiting_on_world', 'held', 'verify',
    ];
    for (const state of states) {
        const result = validators.validate('BlockedItemCustodyPayload', custody({ state, charter: null }));
        assert.strictEqual(result.ok, true, `${state}: ${result.errors.join(' | ')}`);
    }
});

test('a custody payload with its charter decision is accepted', () => {
    assert.strictEqual(validators.validate('BlockedItemCustodyPayload', custody()).ok, true);
});

test('malformed custody payloads are rejected before any handler sees them', () => {
    assertRejects('BlockedItemCustodyPayload', custody({ state: 'limbo' }));
    assertRejects('BlockedItemCustodyPayload', custody({ reason: '' }));
    assertRejects('BlockedItemCustodyPayload', custody({ tone: 'red' }));
    assertRejects('BlockedItemCustodyPayload', custody({ stale: 'yes' }));
    assertRejects('BlockedItemCustodyPayload', custody({ extra: 1 }));
    const missing = custody();
    delete missing.needs_attention;
    assertRejects('BlockedItemCustodyPayload', missing);
    assertRejects('BlockedItemCustodyPayload', custody({ charter: charter({ outcome: 'maybe' }) }));
    assertRejects('BlockedItemCustodyPayload', custody({ charter: charter({ role: 'janitor' }) }));
});

test('the Blocked column summary is accepted, and a bad one rejected', () => {
    assert.strictEqual(validators.validate('BlockedCustodySummaryPayload', summary()).ok, true);
    assertRejects('BlockedCustodySummaryPayload', summary({ needs_attention: -1 }));
    assertRejects('BlockedCustodySummaryPayload', summary({ by_state: [{ state: 'unowned', label: 'Unowned', count: 0 }] }));
    assertRejects('BlockedCustodySummaryPayload', summary({ headline: '' }));
});

test('a blocked card and its column carry custody through the dashboard contract', () => {
    const card = { issue_number: 7, title: 'Blocked', show_stale_badge: false, custody: custody(), custody_signal: 'waiting_on_you||r|' };
    const column = { id: 'blocked', title: 'Blocked', count: 1, hidden_count: 0, items: [card], custody_summary: summary() };

    assert.strictEqual(validators.validate('FlowColumnPayload', column).ok, true);
    assertRejects('FlowColumnPayload', { ...column, custody_summary: summary({ total: 'two' }) });
    assertRejects('IssueItemPayload', { ...card, custody: custody({ state: 'limbo' }) });
});
