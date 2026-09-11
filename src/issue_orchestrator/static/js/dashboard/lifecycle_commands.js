// Shared owner for typed-Command rendering and dispatch (issue #6310).
//
// The dashboard exposes typed ``TimelineCommand`` payloads (from
// ``view_models/lifecycle_semantics.py``) as buttons and native
// disclosure rows that route through a single dispatcher.  Both the E2E
// surfaces and the issue-detail drawer render commands and need this
// dispatcher, so the module is loaded before those consumers in
// ``view_models/dashboard_assets.py``.
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
    return `<button class="${cssClass}" data-lifecycle-command="${payload}" onclick="runLifecycleCommandFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
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

function runLifecycleCommandFromButton(button) {
    const command = _lifecycleCommandFromElement(button);
    if (!command) return;
    runLifecycleCommand(command, button);
}

// Element-aware toggle dispatcher: fired from a ``<details>`` element's
// inline ``ontoggle="runLifecycleCommandFromToggle(this)"`` so native
// disclosure rows route through the same typed-Command pipeline as click
// affordances.  Most toggle Commands are open-only lazy loaders:
// closed → no-op, and re-open after ``dataset.loaded === '1'`` → no-op.
function runLifecycleCommandFromToggle(detailsEl) {
    if (!detailsEl || !detailsEl.dataset) return;
    const command = _lifecycleCommandFromElement(detailsEl);
    if (!command) return;
    if (detailsEl.open !== true) return;
    if (detailsEl.dataset.loaded === '1') return;
    runLifecycleCommand(command, detailsEl);
}

// Single owner for the dialog-only action-button Command family (issue #6327).
//
// The validation / diagnostics dialogs render their action buttons through the
// same ``data-lifecycle-command`` pipeline as every other affordance.  The
// backend dialog view models (``view_models/dialog_commands.py``) emit these
// typed ``DialogActionCommand`` payloads directly, so ``session_dialogs.js``
// renders the provided command without reconstructing one from a loose
// ``action.type`` dict.  Kinds that already have a canonical timeline command
// (``open_session_recording``, ``open_review_feedback``) are handled by the
// branch chain above and reused here rather than duplicated; this table owns
// only the dialog-specific kinds with no timeline counterpart.
//
// Each entry wires a Command ``kind`` to its handler and returns ``true`` on
// dispatch (``false`` when a required field is absent — an unreachable path
// from real buttons, which the backend omits entirely when their run context
// is missing).  Handlers are referenced lazily at click time, so load order
// between this module and the handler modules only needs to settle before the
// user clicks.  ``error_surface`` (default ``toast``) lets the dialog report a
// failed fetch inside the open dialog instead of via a page toast.
const _DIALOG_ACTION_COMMAND_DISPATCH = {
    open_path: (command) => {
        if (!command.path) return false;
        openPath(command.path);
        return true;
    },
    copy_session_recording: (command) => {
        if (!command.issue_number || !command.run_dir) return false;
        copyAgentLogAction(command.issue_number, command.run_dir);
        return true;
    },
    view_claude_log: (command) => {
        if (!command.issue_number || !command.run_dir) return false;
        viewClaudeLog(command.issue_number, command.run_dir, command.error_surface || 'toast');
        return true;
    },
    open_orchestrator_log: (command) => {
        if (!command.issue_number) return false;
        openFilteredOrchestratorLog(command.issue_number, command.run_dir || null, command.error_surface || 'toast');
        return true;
    },
    open_session_diagnostics: (command) => {
        if (!command.issue_number) return false;
        openSessionManifest(command.issue_number, command.run_dir || null);
        return true;
    },
};

function runLifecycleCommand(command, triggerEl = null) {
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
        // ``error_surface`` lets the emitter pick where load failures show:
        // timeline chips leave it unset (default ``toast``); dialog action
        // buttons set ``inline`` so the error renders inside the open dialog.
        openAgentLogAction(command.issue_number, command.run_dir, label, command.error_surface || 'toast', {
            round_index: command.round_index || null,
            session_role: command.session_role || null,
        });
        return;
    }
    if (kind === 'open_review_transcript' && command.issue_number && command.run_dir) {
        // ``error_surface`` lets a caller pick where load failures show:
        // chips/timeline use the default ``toast``; dialog action buttons
        // pass ``inline`` so the error renders inside the open modal.
        // ``?? null`` (not ``|| null``) preserves a legitimate round 0.
        openReviewTranscript(command.issue_number, command.run_dir, {
            round_index: command.round_index ?? null,
            transcript_role: command.transcript_role || null,
        }, command.error_surface || 'toast');
        return;
    }
    if (
        kind === 'open_review_artifact'
        && command.issue_number
        && command.run_dir
        && command.artifact_path
        && command.artifact_type
    ) {
        openReviewArtifact(
            command.issue_number,
            command.run_dir,
            command.artifact_path,
            command.artifact_type,
            command.render_mode || null,
        );
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
    // ``open_review_feedback`` is a canonical ``TimelineCommand`` (emitted by
    // review/changes-requested cycles); it lives here with the other typed
    // command branches rather than the dialog-action table.
    if (kind === 'open_review_feedback' && command.issue_number) {
        openReviewFeedback(command.issue_number);
        return;
    }
    // ``open_e2e_run`` is the typed "navigate the user to run #N"
    // Command emitted by chips, View buttons, and other affordances
    // anywhere on the dashboard.  Routes to the inline runs-list
    // driver ``expandE2ERunRow``, which opens (and scrolls to) the
    // matching row.  ``expand_run_details`` opens the row's nested
    // Diagnostics row once it mounts.
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
    // Dialog action-button Commands (issue #6327) route through a single
    // decision table (``_DIALOG_ACTION_COMMAND_DISPATCH``) rather than a
    // chain of per-kind branches — one owner for the whole dialog-command
    // family.  The handler returns ``true`` once it dispatches; a known kind
    // whose required fields are missing returns ``false`` and falls through
    // to the unsupported-command signal, matching the branch-chain kinds.
    const dialogHandler = _DIALOG_ACTION_COMMAND_DISPATCH[kind];
    if (dialogHandler && dialogHandler(command)) return;
    showToast(`Unsupported lifecycle command: ${kind}`, 'warning');
}
