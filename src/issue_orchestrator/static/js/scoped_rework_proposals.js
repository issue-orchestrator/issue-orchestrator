/* Typed approval adapter; consent and execution policy live in the proposal owner. */
(function () {
    'use strict';
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const endpoint = window.uiActionContract.ENDPOINTS.TECH_LEAD_REWORK_PROPOSALS;
    function renderProposal(item) {
        const command = decision => escape(JSON.stringify({proposal_issue_number: item.proposal_issue_number, decision}));
        return `<li class="rework-proposal"><h3>PR #${escape(item.pr_number)} → issue #${escape(item.issue_number)}</h3>
            <p><strong>${escape(item.status.replaceAll('_', ' '))}</strong> — ${escape(item.detail)}</p>
            <p>${escape(item.feedback)}</p><p>${escape(item.mutations)}</p>
            <dl><dt>Repository</dt><dd>${escape(item.repository)}</dd><dt>Expected head</dt><dd><code>${escape(item.expected_head)}</code></dd>
            <dt>Evidence</dt><dd><code>${escape(item.evidence_identity)}</code></dd></dl>
            <details><summary>Review report</summary><pre>${escape(item.report)}</pre></details>
            ${item.forward_issue_number ? `<p>Forward fix: #${escape(item.forward_issue_number)}</p>` : ''}
            <div class="rework-proposal-actions">
            <button type="button" data-rework-command="${command('approve')}" ${item.can_approve ? '' : 'disabled'} aria-label="Approve rework for PR ${escape(item.pr_number)}">Approve rework</button>
            <button type="button" data-rework-command="${command('decline')}" ${item.can_decline ? '' : 'disabled'} aria-label="Decline rework for PR ${escape(item.pr_number)}">Decline</button></div></li>`;
    }
    async function dispatch(command, send = fetch) {
        const request = window.uiActionContract.buildTechLeadProposalRequest(command);
        const response = await send(request.endpoint, {method: request.method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request.body)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Proposal command failed');
        return result;
    }
    window.scopedReworkProposals = {renderProposal, dispatch};
    const panel = document.getElementById('reworkProposalsPanel');
    if (!panel) return;
    const list = document.getElementById('reworkProposalList');
    const status = document.getElementById('reworkProposalStatus');
    async function refresh() {
        try {
            const response = await fetch(endpoint);
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || 'Could not read proposals');
            list.innerHTML = result.proposals.map(renderProposal).join('');
            status.textContent = result.proposals.length ? `${result.proposals.length} proposal dispositions` : 'No scoped rework proposals.';
        } catch (error) { status.textContent = error.message; }
    }
    panel.addEventListener('toggle', () => { if (panel.open) refresh(); });
    document.getElementById('refreshReworkProposals').addEventListener('click', refresh);
    list.addEventListener('click', async event => {
        const button = event.target.closest('[data-rework-command]');
        if (!button || button.disabled) return;
        button.disabled = true;
        try {
            const result = await dispatch(JSON.parse(button.dataset.reworkCommand));
            await refresh();
            status.textContent = result.detail;
            document.getElementById('reworkProposalsSummary').focus();
        } catch (error) { status.textContent = error.message; button.disabled = false; }
    });
})();
