// Shared owner for typed-Command rendering and dispatch (issue #6310).
//
// The dashboard exposes typed ``TimelineCommand`` payloads (from
// ``view_models/lifecycle_semantics.py``) as buttons that route through a
// single dispatcher.  Both the E2E run modal and the issue-detail drawer
// render command buttons and need this dispatcher, so the module is loaded
// before either consumer in ``view_models/dashboard_assets.py``.
//
// Per-Command-kind dispatch handlers (``openIssueTimeline``,
// ``openAgentLogAction``, ``openReviewTranscript``, ``openValidationFailure``,
// ``openPath``) live in other modules and are invoked here at click time —
// not import time — so load order between this file and those handlers
// only needs to settle before the user clicks a Command button.

function _renderLifecycleCommandButton(command, fallbackLabel = null, cssClass = 'issue-action-btn') {
    if (!command || typeof command !== 'object') return '';
    const payload = escapeAttr(JSON.stringify(command));
    const label = fallbackLabel || command.label || _humanizeSnakeCase(command.kind || 'Action');
    return `<button class="${cssClass}" data-lifecycle-command="${payload}" onclick="runE2ELifecycleCommandFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function runE2ELifecycleCommandFromButton(button) {
    if (!button || !button.dataset) return;
    const raw = button.dataset.lifecycleCommand || '';
    if (!raw) return;
    try {
        runE2ELifecycleCommand(JSON.parse(raw));
    } catch (err) {
        showToast(`Failed to decode lifecycle command: ${err instanceof Error ? err.message : String(err)}`, 'error');
    }
}

function runE2ELifecycleCommand(command) {
    if (!command || typeof command !== 'object') return;
    const kind = String(command.kind || '').trim();
    if (!kind) return;
    if (kind === 'open_issue_timeline' && command.issue_number) {
        const opts = command.scope_kind === 'e2e_run' && command.e2e_run_id
            ? { e2eRunId: command.e2e_run_id }
            : {};
        openIssueTimeline(command.issue_number, null, opts);
        return;
    }
    if (kind === 'open_session_recording' && command.issue_number && command.run_dir) {
        const label = command.label ? String(command.label) : 'Session Recording';
        openAgentLogAction(command.issue_number, command.run_dir, label, 'toast', {
            round_index: command.round_index || null,
            session_role: command.session_role || null,
        });
        return;
    }
    if (kind === 'open_review_transcript' && command.issue_number && command.run_dir) {
        openReviewTranscript(command.issue_number, command.run_dir, {
            round_index: command.round_index || null,
            transcript_role: command.transcript_role || null,
        }, 'toast');
        return;
    }
    if (kind === 'open_validation_details' && command.issue_number) {
        openValidationFailure(command.issue_number, command.run_dir || null, 'toast');
        return;
    }
    if (kind === 'open_completion_record' && command.path) {
        openPath(command.path);
        return;
    }
    // Typed-Command entry point for the E2E run view (issue #6322).
    // The legacy inline ``onclick="showUnifiedRunView(id)"`` carried
    // the same intent but bypassed the typed-Command pipeline, which
    // means cheap-integration tests couldn't extract or spy on the
    // navigation.  Routing through here makes the chip + issue-row
    // affordances first-class commands.
    if (kind === 'open_e2e_run' && command.run_id) {
        const opts = command.options && typeof command.options === 'object' ? command.options : {};
        showUnifiedRunView(command.run_id, opts);
        return;
    }
    showToast(`Unsupported lifecycle command: ${kind}`, 'warning');
}
