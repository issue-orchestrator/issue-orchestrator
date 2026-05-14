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

function _renderLifecycleCommandAttr(command) {
    if (!command || typeof command !== 'object') return '';
    return `data-lifecycle-command="${escapeAttr(JSON.stringify(command))}"`;
}

function _lifecycleCommandFromElement(element) {
    if (!element || !element.dataset) return null;
    const raw = element.dataset.lifecycleCommand || '';
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch (err) {
        showToast(`Failed to decode lifecycle command: ${err instanceof Error ? err.message : String(err)}`, 'error');
        return null;
    }
}

function runE2ELifecycleCommandFromButton(button) {
    const command = _lifecycleCommandFromElement(button);
    if (!command) return;
    runE2ELifecycleCommand(command, button);
}

// Element-aware toggle dispatcher: fired from a ``<details>`` element's
// inline ``ontoggle="runLifecycleCommandFromToggle(this)"`` so native
// disclosure rows route through the same typed-Command pipeline as click
// affordances.  Most toggle Commands are open-only lazy loaders:
// closed → no-op, and re-open after ``dataset.loaded === '1'`` → no-op.
// ``sync_journey_disclosure`` is the deliberate exception because its
// job is to mirror the native open/closed state into the journey row's
// legacy body/caret classes on both transitions.
function runLifecycleCommandFromToggle(detailsEl) {
    if (!detailsEl || !detailsEl.dataset) return;
    const command = _lifecycleCommandFromElement(detailsEl);
    if (!command) return;
    const kind = String(command.kind || '').trim();
    const runsWhenClosed = kind === 'sync_journey_disclosure';
    if (!runsWhenClosed && detailsEl.open !== true) return;
    if (!runsWhenClosed && detailsEl.dataset.loaded === '1') return;
    runE2ELifecycleCommand(command, detailsEl);
}

function runE2ELifecycleCommandFromToggle(detailsEl) {
    runLifecycleCommandFromToggle(detailsEl);
}

function runE2ELifecycleCommand(command, triggerEl = null) {
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
    if (kind === 'sync_journey_disclosure') {
        const targetId = String(command.target_id || '').trim();
        const triggerId = triggerEl && triggerEl.id ? String(triggerEl.id) : '';
        if (targetId && targetId !== triggerId) {
            showToast('Timeline command target mismatch', 'error');
            return;
        }
        if (typeof syncJourneyDisclosureState !== 'function') {
            showToast('Timeline disclosure sync is unavailable.', 'error');
            return;
        }
        syncJourneyDisclosureState(triggerEl);
        return;
    }
    // ``open_e2e_run`` is the typed "navigate the user to run #N"
    // Command emitted by chips, View buttons, and other affordances
    // anywhere on the dashboard.  Routes to the inline runs-list
    // driver ``expandE2ERunRow``, which opens (and scrolls to) the
    // matching row.  ``expand_run_details`` opens the row's nested
    // "Run details & artifacts" disclosure once it mounts.
    if (kind === 'open_e2e_run' && command.run_id) {
        const expandRunDetails = command.expand_run_details === true;
        if (typeof expandE2ERunRow !== 'function') {
            showToast('E2E runs list is not loaded.', 'warning');
            return;
        }
        expandE2ERunRow(command.run_id, { expandRunDetails });
        return;
    }
    // ``expand_e2e_run`` fires from the row's ``ontoggle`` the first
    // time it opens.  ``triggerEl`` is the ``<details>`` itself,
    // forwarded by ``runLifecycleCommandFromToggle``.
    if (kind === 'expand_e2e_run' && command.run_id) {
        if (typeof loadE2ERunIntoRow !== 'function') return;
        loadE2ERunIntoRow(command.run_id, triggerEl);
        return;
    }
    // ``switch_e2e_timeline_view`` and ``create_e2e_untriaged_issues``
    // are emitted by buttons inside an expanded row.  Both handlers
    // route through ``resolveRowCommandContext`` (single owner of
    // row-targeting policy) — the dispatcher just forwards the
    // typed payload + trigger element.
    if (kind === 'switch_e2e_timeline_view' && command.run_id && command.view) {
        if (typeof switchE2ETimelineView !== 'function') return;
        switchE2ETimelineView(command.run_id, command.view, triggerEl);
        return;
    }
    if (kind === 'create_e2e_untriaged_issues' && command.run_id) {
        if (typeof createIssuesForUntriaged !== 'function') return;
        createIssuesForUntriaged(command.run_id, triggerEl);
        return;
    }
    // Typed-Command entry point for the inline Attempts expander
    // (issue #6322 follow-up).  ``triggerEl`` is the ``<details>``
    // carrying ``data-issue-number`` and the per-expander body that
    // the loader populates.  Backed by ``OpenInlineAgentAttemptsCommand``
    // in ``view_models/lifecycle_semantics.py``.
    if (kind === 'open_inline_agent_attempts' && command.issue_number) {
        if (typeof loadInlineAgentAttempts !== 'function') return;
        loadInlineAgentAttempts(command.issue_number, triggerEl);
        return;
    }
    showToast(`Unsupported lifecycle command: ${kind}`, 'warning');
}
