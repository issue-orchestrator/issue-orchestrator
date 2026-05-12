// Phase-0 plugin: issue-orchestrator agent context.
//
// Registered under ``io.agent-context``.  Activated when a JUnit test
// case carries an entry like:
//
//   case.extras = [{
//     namespace: "io.agent-context",
//     payload: {
//       issue_number: 4503,
//       issue_title: "fixture: cohort split",
//       final_state: "blocked",
//       run_url: "/api/dashboard/issue/4503?focus=timeline",
//       summary: "agent retried 2x then blocked on validation",
//     }
//   }]
//
// In Phase A we render a slim summary (one-line context + a deep link
// to the issue's drawer).  Phase C extends this to embed a thin journey
// summary when richer payload is provided (cycles, validation events).
// In all cases the renderer is the only thing in the codebase that
// knows about agent-context as a domain concept — the canonical viewer
// stays generic.
//
// See ``docs/journeys/validation-viewer-redesign.md`` for the why.

(function () {
    if (typeof registerValidationPlugin !== 'function') {
        // The viewer module didn't load (e.g. dashboard chunk order
        // broke).  Bail silently rather than throw a console error
        // that suggests the plugin is at fault.
        return;
    }

    registerValidationPlugin('io.agent-context', function renderAgentContext(payload) {
        if (!payload || typeof payload !== 'object') return '';
        const issueNumber = Number(payload.issue_number);
        if (!Number.isInteger(issueNumber) || issueNumber <= 0) return '';
        const title = typeof payload.issue_title === 'string' ? payload.issue_title : '';
        const finalState = typeof payload.final_state === 'string' ? payload.final_state : '';
        const summary = typeof payload.summary === 'string' ? payload.summary : '';

        // Status chip — use the canonical viewer's chip styling.  Map
        // common orchestrator final states to outcome colors.
        const stateClass = finalState === 'completed'
            ? 'cvv-chip-passed'
            : finalState === 'blocked' || finalState === 'failed'
            ? 'cvv-chip-failed'
            : finalState === 'errored'
            ? 'cvv-chip-error'
            : '';
        const stateChip = finalState
            ? `<span class="cvv-chip ${stateClass}">${escapeHtml(finalState)}</span>`
            : '';

        // Open-issue-drawer affordance routes through the shared typed-
        // Command pipeline (``lifecycle_commands.js`` →
        // ``runE2ELifecycleCommand`` → ``openIssueTimeline``) so the
        // click actually opens the drawer.  The Phase-C reviewer
        // (PR #6319 Blocker 1) flagged the prior plain ``<a href>``
        // approach as broken: there's no ``/api/dashboard/issue/N``
        // route, and the drawer-open contract has always been the
        // typed command.  Only render the button when the shared
        // command renderer is loaded — pure-JUnit consumers without
        // the lifecycle bundle still get a useful (text-only) plugin
        // block, just without the button.
        let actionsHtml = '';
        if (typeof _renderLifecycleCommandButton === 'function') {
            const cmd = {
                kind: 'open_issue_timeline',
                issue_number: issueNumber,
                scope_kind: 'dashboard',
                label: 'Open issue drawer ↗',
            };
            actionsHtml = `<div class="agent-context-actions">${_renderLifecycleCommandButton(cmd, 'Open issue drawer ↗', 'btn')}</div>`;
        }

        let html = '<div class="cvv-plugin agent-context">';
        html += '<div class="cvv-plugin-header">';
        html += `<span class="cvv-plugin-tag">↳ Linked issue · driven by orchestrator</span>`;
        html += '</div>';
        html += '<div class="cvv-plugin-body">';
        html += `<div class="agent-context-row"><strong>Issue:</strong> <code>#${issueNumber}</code>`;
        if (title) html += ` — ${escapeHtml(title)}`;
        html += '</div>';
        if (finalState) {
            html += `<div class="agent-context-row"><strong>Final state:</strong> ${stateChip}</div>`;
        }
        if (summary) {
            html += `<div class="agent-context-row"><strong>Summary:</strong> ${escapeHtml(summary)}</div>`;
        }
        html += actionsHtml;
        html += '</div></div>';
        return html;
    });
})();
