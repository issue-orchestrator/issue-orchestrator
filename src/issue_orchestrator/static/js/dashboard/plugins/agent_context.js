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
//       summary: "agent retried 2x then blocked on validation",
//     }
//   }]
//
// The linked-issue drill-in is the inline ``▸ Attempts on issue #N``
// expander defined in ``inline_agent_attempts.js`` (issue #6322):
// rather than teleporting to a per-issue drawer, the user expands the
// agent's Attempts → Cycles tree inline beneath the failure summary.
// Pure-JUnit consumers without that module (tixmeup et al.) see the
// summary fields and nothing else — the expander silently drops in.
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

        // Inline "Agent attempts" expander (issue #6322): instead of
        // teleporting to the per-issue drawer, the user expands the
        // attempts inline.  Lazy-fetches the issue-detail payload
        // and renders Attempts → Cycles in place.  No fallback
        // ``↗`` to open as a fresh root — the user said they don't
        // want it; if it becomes painful, file a follow-up.
        //
        // Pure-JUnit consumers without the inline-attempts bundle
        // (e.g. tixmeup) see the summary fields and nothing else —
        // the expander silently drops in.
        let attemptsHtml = '';
        if (typeof renderInlineAgentAttemptsExpander === 'function') {
            attemptsHtml = `<div class="agent-context-actions">${renderInlineAgentAttemptsExpander(issueNumber)}</div>`;
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
        html += attemptsHtml;
        html += '</div></div>';
        return html;
    });
})();
