// Blocked-item custody rendering (#7331): payload -> markup.
//
// The custody owner decides the state server-side (the producer half is
// tests/unit/test_blocked_custody_projection.py). These pin how the browser
// renders that payload: the state as words, the "why" disclosure, the charter
// decision, both row forms, in-place age sync, and the column summary.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS = path.join(__dirname, '../../src/issue_orchestrator/static/js');

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function loadModule() {
    const context = {
        compactCardState: { computeCompactCardFingerprint: () => 'fingerprint' },
        cssEscape: (value) => String(value),
        document: {},
        escapeAttr: escapeHtml,
        escapeHtml,
        formatDashboardTimestamps: () => {},
        localStorage: { getItem: () => null, setItem: () => {} },
        window: { dashboardData: {}, location: { href: 'http://example.test/' } },
    };
    vm.createContext(context);
    for (const file of ['dashboard/blocked_custody.js', 'dashboard/kanban_columns.js']) {
        vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), context);
    }
    return context;
}

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
        charter: null,
        ...overrides,
    };
}

function card(overrides = {}) {
    return {
        issue_number: 42,
        issue_label: '#42',
        title: 'Stuck',
        state_label: 'blocked',
        phase: 'Blocked',
        status: 'blocked',
        is_stale: false,
        show_stale_badge: false,
        orchestrator_labels: ['blocked-failed'],
        custody: custody(),
        ...overrides,
    };
}

test('no custody markup outside the Blocked lane', () => {
    const { renderCustodyHtml } = loadModule();
    assert.strictEqual(renderCustodyHtml({ custody: null }), '');
    assert.strictEqual(renderCustodyHtml({}), '');
});

test('the state is written as words; the icon is decorative', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card());

    assert.match(html, /<span class="custody-label">Waiting on you<\/span>/);
    assert.match(html, /<span class="custody-icon" aria-hidden="true">/);
    assert.match(html, /class="card-custody custody--you" data-custody-state="waiting_on_you"/);
    assert.match(html, /<span class="custody-age"> · 2h<\/span>/);
});

test('why lives in a native disclosure with its reason, owner, clock and limit', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card());

    assert.match(html, /<details class="custody-why"><summary>Why this state\?<\/summary>/);
    assert.match(html, /<p class="custody-reason">Proposal #700 \(kill hung session\) awaits your approval\.<\/p>/);
    assert.match(html, /Owner: you\. In this state since <span data-dashboard-timestamp="2026-09-27T10:00:00\+00:00"/);
    assert.match(html, /\(proposal filed\)\. Counts as stale after 1d\./);
    assert.doesNotMatch(html, /aria-expanded/, 'native <details> reports its own state');
});

test('an undated state says so instead of inventing a time', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card({ custody: custody({ since: '', since_basis: '', age_label: 'age unknown' }) }));

    assert.match(html, /No fact dates when it entered this state\./);
    assert.match(html, / · age unknown/);
});

test('an item needing attention says so in words', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card({
        custody: custody({
            state: 'unowned', label: 'Unowned', owner: 'nobody', tone: 'attention',
            needs_attention: true, attention_text: 'Needs attention: nobody owns it.',
            stale_after_label: '',
        }),
    }));

    assert.match(html, /<span class="custody-attention">Needs attention<\/span>/);
    assert.match(html, /<p class="custody-attention-text">Needs attention: nobody owns it\.<\/p>/);
    assert.doesNotMatch(html, /Counts as stale after/);
});

test('the charter decision that put it there is spelled out', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card({ custody: custody({ charter: charter() }) }));

    assert.match(html, /<dl class="custody-charter">/);
    assert.match(html, /<dt>Charter role<\/dt><dd>flow<\/dd>/);
    assert.match(html, /<dt>Outcome<\/dt><dd>Proposed, awaiting approval \(awaiting approval\)<\/dd>/);
    assert.match(html, /depth restructure, authority execute; ceiling propose from <code>tech_lead\.authority\.kill_hung_session<\/code>/);
    assert.match(html, /<dt>Proposal<\/dt><dd>#700<\/dd>/);

    const disabled = renderCustodyHtml(card({ custody: custody({ charter: charter({ role_enabled: false, proposal_issue_number: 0 }) }) }));
    assert.match(disabled, /, role disabled;/);
    assert.doesNotMatch(disabled, /<dt>Proposal<\/dt>/);
});

test('payload text is escaped, never interpreted', () => {
    const { renderCustodyHtml } = loadModule();
    const html = renderCustodyHtml(card({ custody: custody({ reason: '<img src=x onerror=alert(1)>' }) }));

    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('both row forms carry the same custody block', () => {
    const { renderCompactCardHtml, renderExpandedCardHtml, renderCustodyHtml } = loadModule();
    const item = card();
    const block = renderCustodyHtml(item);

    assert.ok(renderCompactCardHtml(item).includes(block), 'compact card');
    assert.ok(renderExpandedCardHtml(item, 'blocked', false).includes(block), 'expanded row');
});

test('a reused card only has its ticking age refreshed', () => {
    const { syncCustodyAge } = loadModule();
    const age = { textContent: ' · 2h' };
    const node = { querySelector: (selector) => (selector === '.custody-age' ? age : null) };

    syncCustodyAge(node, card({ custody: custody({ age_label: '3h' }) }));
    assert.strictEqual(age.textContent, ' · 3h');
    syncCustodyAge(node, card({ custody: null }));
    assert.strictEqual(age.textContent, ' · 3h');
});

function summary(overrides = {}) {
    return {
        total: 3,
        needs_attention: 2,
        unowned: 1,
        stale: 1,
        headline: '2 of 3 blocked items need attention (1 unowned, 1 stale).',
        by_state: [
            { state: 'unowned', label: 'Unowned', count: 1 },
            { state: 'held', label: 'Held', count: 2 },
        ],
        ...overrides,
    };
}

test('the column summary states the attention number in words', () => {
    const { renderBlockedCustodySummaryHtml } = loadModule();

    const html = renderBlockedCustodySummaryHtml(summary());
    assert.match(html, /<span class="custody-summary-count needs-attention">2 need attention<\/span>/);
    assert.match(html, /<li>Unowned: 1<\/li><li>Held: 2<\/li>/);
    assert.match(renderBlockedCustodySummaryHtml(summary({ needs_attention: 1 })), />1 needs attention</);
    assert.match(
        renderBlockedCustodySummaryHtml(summary({ needs_attention: 0, by_state: [], headline: 'Nothing is blocked.' })),
        /<span class="custody-summary-count is-under-control">Under control<\/span><span class="custody-summary-headline">Nothing is blocked\.<\/span>$/,
    );
});

test('the live summary is rewritten only when it changes', () => {
    const { syncBlockedCustodySummary } = loadModule();
    let writes = 0;
    const target = {
        dataset: {},
        set innerHTML(value) { writes += 1; this._html = value; },
        get innerHTML() { return this._html; },
    };
    const col = { querySelector: (selector) => (selector === '.blocked-custody-summary' ? target : null) };

    syncBlockedCustodySummary(col, { custody_summary: summary() });
    syncBlockedCustodySummary(col, { custody_summary: summary() });
    assert.strictEqual(writes, 1, 'an unchanged summary must not re-announce');
    syncBlockedCustodySummary(col, { custody_summary: summary({ needs_attention: 1 }) });
    assert.strictEqual(writes, 2);
    syncBlockedCustodySummary(col, { id: 'queued' });
    assert.strictEqual(writes, 2, 'a column without a summary leaves it alone');
});

test('the expanded list rebuilds when custody changes but not when only its age ticks', () => {
    const state = require(path.join(JS, 'expanded_column_state.js'));
    const base = { issue_number: 42, custody_signal: 'held||r|', custody: custody({ age_label: '2h' }) };
    const fp = (item) => state.computeExpandedItemsFingerprint([item], { columnId: 'blocked' });

    assert.strictEqual(fp(base), fp({ ...base, custody: custody({ age_label: '9h' }) }));
    assert.notStrictEqual(fp(base), fp({ ...base, custody_signal: 'verify||r|' }));
});

test('an unchanged expanded list still refreshes each row\'s age', () => {
    const { syncExpandedCustodyAges } = loadModule();
    const ages = { 42: { textContent: ' · 2h' }, 43: { textContent: ' · 1h' } };
    const list = {
        querySelector: (selector) => {
            const match = selector.match(/data-issue="(\d+)"/);
            const age = match && ages[match[1]];
            return age ? { querySelector: () => age } : null;
        },
    };

    syncExpandedCustodyAges(list, [
        card({ custody: custody({ age_label: '3h' }) }),
        { issue_number: 43, custody: custody({ age_label: '4h' }) },
        { issue_number: 44, custody: custody() },
    ]);

    assert.strictEqual(ages[42].textContent, ' · 3h');
    assert.strictEqual(ages[43].textContent, ' · 4h');
});
