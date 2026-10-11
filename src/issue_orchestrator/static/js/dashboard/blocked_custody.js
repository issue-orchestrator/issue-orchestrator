// Blocked-item custody on the board (#7331).
//
// Renders the server-derived ``item.custody`` (BlockedItemCustodyPayload) on
// blocked cards and rows, and the Blocked column's ``custody_summary``
// (BlockedCustodySummaryPayload) under its heading. The custody owner
// (control/blocked_item_custody.py) decides every state, reason, clock and
// staleness; this file only turns that payload into markup, so a card, a row
// and the header can never disagree.
//
// Accessibility:
// - the state is always written as words; the tone class only styles it, and
//   the icon is decorative (aria-hidden);
// - "Why this state?" is a native <details>/<summary> disclosure, so it is
//   keyboard reachable, named, and reports expanded/collapsed itself;
// - the column summary is a polite live region, announced when it changes.

const CUSTODY_ICONS = {
    unowned: '!',
    queued_for_tech_lead: '⏳',
    investigating: '🔎',
    waiting_on_you: '✋',
    being_fixed: '🛠',
    waiting_on_world: '🌐',
    held: '⏸',
    verify: '✓',
};

function custodyAgeText(custody) {
    return ` · ${custody.age_label}`;
}

function renderCustodyCharterHtml(charter) {
    const proposal = charter.proposal_issue_number > 0
        ? `<dt>Proposal</dt><dd>#${charter.proposal_issue_number} <button type="button" class="custody-open-tech-lead" data-tech-lead-proposal="${charter.proposal_issue_number}">Open in the Tech lead page</button></dd>`
        : '';
    const lifecycle = charter.lifecycle_label
        ? ` (${escapeHtml(charter.lifecycle_label)})`
        : '';
    const enabled = charter.role_enabled ? '' : ', role disabled';
    return '<dl class="custody-charter">'
        + `<dt>Charter role</dt><dd>${escapeHtml(charter.role)}</dd>`
        + `<dt>Action</dt><dd>${escapeHtml(charter.action)} (needs depth ${escapeHtml(charter.required_depth)})</dd>`
        + `<dt>Outcome</dt><dd>${escapeHtml(charter.outcome_label)}${lifecycle}</dd>`
        + `<dt>Dials</dt><dd>depth ${escapeHtml(charter.role_depth)}, authority ${escapeHtml(charter.role_authority)}${enabled}; ceiling ${escapeHtml(charter.action_ceiling)} from <code>${escapeHtml(charter.ceiling_source)}</code></dd>`
        + `<dt>Charter reason</dt><dd>${escapeHtml(charter.reason)}</dd>`
        + proposal
        + '</dl>';
}

// One blocked item's custody line plus its "why" disclosure. Empty for an
// item outside the Blocked lane (no custody on the payload).
function renderCustodyHtml(item) {
    const custody = item && item.custody;
    if (!custody) return '';
    const icon = CUSTODY_ICONS[custody.state] || '';
    const attention = custody.needs_attention
        ? '<span class="custody-attention">Needs attention</span>'
        : '';
    const since = custody.since
        ? `In this state since <span data-dashboard-timestamp="${escapeAttr(custody.since)}" data-dashboard-timestamp-fallback="${escapeAttr(custody.since)}">${escapeHtml(custody.since)}</span> (${escapeHtml(custody.since_basis)}).`
        : 'No fact dates when it entered this state.';
    const limit = custody.stale_after_label
        ? ` Counts as stale after ${escapeHtml(custody.stale_after_label)}.`
        : '';
    const attentionText = custody.attention_text
        ? `<p class="custody-attention-text">${escapeHtml(custody.attention_text)}</p>`
        : '';
    const charter = custody.charter ? renderCustodyCharterHtml(custody.charter) : '';
    return `<div class="card-custody custody--${escapeAttr(custody.tone)}" data-custody-state="${escapeAttr(custody.state)}">`
        + '<div class="card-line custody-line">'
        + `<span class="custody-icon" aria-hidden="true">${icon}</span>`
        + `<span class="custody-label">${escapeHtml(custody.label)}</span>`
        + `<span class="custody-age">${escapeHtml(custodyAgeText(custody))}</span>`
        + attention
        + '</div>'
        + '<details class="custody-why">'
        + '<summary>Why this state?</summary>'
        + `<p class="custody-reason">${escapeHtml(custody.reason)}</p>`
        + `<p class="custody-meta">Owner: ${escapeHtml(custody.owner)}. ${since}${limit}</p>`
        + attentionText
        + charter
        + '</details>'
        + '</div>';
}

// A reused card node keeps its open disclosure; only the ticking age is
// refreshed in place (the fingerprint leaves the age out on purpose).
function syncCustodyAge(node, item) {
    const custody = item && item.custody;
    if (!custody) return;
    const ageEl = node.querySelector('.custody-age');
    if (!ageEl) return;
    const desired = custodyAgeText(custody);
    if (ageEl.textContent !== desired) ageEl.textContent = desired;
}

// Every expanded row whose list was NOT rebuilt still refreshes its age.
function syncExpandedCustodyAges(list, items) {
    for (const item of items) {
        const node = list.querySelector(`.expanded-card[data-issue="${cssEscape(String(item.issue_number))}"]`);
        if (node) syncCustodyAge(node, item);
    }
}

// A rebuild must not close a "Why this state?" the operator opened, nor drop
// keyboard focus from its summary. One owner for every surface that replaces
// custody markup (compact cards, the expanded list, list rows): capture by
// issue before the replacement, restore after it.
function captureCustodyDisclosures(root) {
    const open = new Set();
    let focused = null;
    if (!root) return { open, focused };
    for (const details of root.querySelectorAll('details.custody-why[open]')) {
        const owner = details.closest('[data-issue]');
        if (owner) open.add(owner.dataset.issue);
    }
    const active = document.activeElement;
    if (active && root.contains(active)) {
        const details = active.closest('details.custody-why');
        const owner = details && details.closest('[data-issue]');
        if (owner) focused = owner.dataset.issue;
    }
    return { open, focused };
}

function restoreCustodyDisclosures(root, state) {
    if (!root) return;
    for (const issue of state.open) {
        const details = root.querySelector(`[data-issue="${cssEscape(issue)}"] details.custody-why`);
        if (details && !details.open) details.open = true;
    }
    if (state.focused === null) return;
    // Only when the replacement actually took focus away: never steal it.
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const summary = root.querySelector(
        `[data-issue="${cssEscape(state.focused)}"] details.custody-why > summary`,
    );
    if (summary) summary.focus();
}

// The Blocked column's "is it under control?" line. The count is text, and
// the headline spells out how it was reached.
function renderBlockedCustodySummaryHtml(summary) {
    const underControl = summary.needs_attention === 0;
    const count = underControl
        ? 'Under control'
        : `${summary.needs_attention} need${summary.needs_attention === 1 ? 's' : ''} attention`;
    const states = summary.by_state
        .map((entry) => `<li>${escapeHtml(entry.label)}: ${entry.count}</li>`)
        .join('');
    return `<span class="custody-summary-count${underControl ? ' is-under-control' : ' needs-attention'}">${escapeHtml(count)}</span>`
        + `<span class="custody-summary-headline">${escapeHtml(summary.headline)}</span>`
        + (states ? `<ul class="custody-summary-states" aria-label="Blocked items by custody">${states}</ul>` : '');
}

// What makes one summary different from another. The server writes the same
// key on first paint (templates/_blocked_custody.html), so the first refresh
// does not rewrite -- and re-announce -- an unchanged line.
function blockedCustodySummaryKey(summary) {
    const states = summary.by_state.map((entry) => `${entry.state}:${entry.count}`).join(',');
    return `${summary.total}|${summary.needs_attention}|${summary.unowned}|${summary.stale}|${summary.headline}|${states}`;
}

function syncBlockedCustodySummary(colEl, column) {
    const target = colEl.querySelector('.blocked-custody-summary');
    const summary = column && column.custody_summary;
    if (!target || !summary) return;
    // Rewrite only on a real change: this is a live region, and re-setting
    // identical markup every refresh would re-announce it.
    const key = blockedCustodySummaryKey(summary);
    if (target.dataset.custodyKey === key) return;
    target.dataset.custodyKey = key;
    target.innerHTML = renderBlockedCustodySummaryHtml(summary);
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        renderCustodyHtml,
        blockedCustodySummaryKey,
        captureCustodyDisclosures,
        restoreCustodyDisclosures,
        syncExpandedCustodyAges,
        renderBlockedCustodySummaryHtml,
        syncBlockedCustodySummary,
        syncCustodyAge,
    };
}
