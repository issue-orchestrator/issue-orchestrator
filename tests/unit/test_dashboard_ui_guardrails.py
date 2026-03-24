from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard.js"
DASHBOARD_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "dashboard.html"
UI_ACTION_CONTRACT_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "ui_action_contract.js"
DASHBOARD_CSS = ROOT / "src" / "issue_orchestrator" / "static" / "css" / "dashboard.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"Function '{name}' not found"
    brace = source.find("{", start)
    assert brace != -1, f"Function '{name}' body start not found"
    depth = 0
    i = brace
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace : i + 1]
        i += 1
    raise AssertionError(f"Function '{name}' body end not found")


def test_unblock_paths_use_unblock_api() -> None:
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    assert "/api/unblock-retry" in contract_js
    assert "buildUnblockRequest" in contract_js
    assert "issues" in contract_js
    assert "/api/bulk-retry" in contract_js
    assert "buildBulkRetryRequest" in contract_js
    assert "/api/bulk-cancel-queued" in contract_js
    assert "buildBulkCancelQueuedRequest" in contract_js


def test_blocked_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkUnblock\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkResetRetry\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkResetRetryFromScratch\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkMarkViewed\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkClearViewed\(\)"\s+disabled', html)


def test_completed_and_awaiting_merge_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkRetryAwaitingMerge\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkRetryCompleted\(\)"\s+disabled', html)
    # awaiting-merge column also has reset & retry buttons
    # Find the awaiting-merge section and verify buttons exist there
    am_start = html.find("column.id == 'awaiting-merge'")
    am_end = html.find("column.id == 'completed'", am_start)
    am_section = html[am_start:am_end]
    assert "bulkResetRetry()" in am_section
    assert "bulkResetRetryFromScratch()" in am_section


def test_issue_detail_uses_timeline_label_not_journey() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert '<h3 class="issue-detail-section-title">Timeline</h3>' in html
    assert '<h3 class="issue-detail-section-title">Journey</h3>' not in html


def test_issue_detail_template_includes_retry_publish_button() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailRetryPublishBtn"' in html


def test_issue_detail_template_includes_validation_failure_section() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailValidation"' in html
    assert 'id="issueDetailValidationBtn"' in html


def test_dashboard_loads_ui_state_helpers_before_dashboard_js() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    idx_issue_row = html.find('/static/js/issue_row_state.js')
    idx_expanded = html.find('/static/js/expanded_column_state.js')
    idx_compact = html.find('/static/js/compact_card_state.js')
    idx_action_contract = html.find('/static/js/ui_action_contract.js')
    idx_xterm_css = html.find('/static/vendor/xterm/xterm.css')
    idx_xterm_js = html.find('/static/vendor/xterm/xterm.js')
    idx_xterm_fit = html.find('/static/vendor/xterm/addon-fit.js')
    idx_dashboard = html.find('/static/js/dashboard.js')
    assert idx_issue_row != -1
    assert idx_expanded != -1
    assert idx_compact != -1
    assert idx_action_contract != -1
    assert idx_xterm_css != -1
    assert idx_xterm_js != -1
    assert idx_xterm_fit != -1
    assert idx_dashboard != -1
    assert idx_issue_row < idx_dashboard
    assert idx_expanded < idx_dashboard
    assert idx_compact < idx_dashboard
    assert idx_action_contract < idx_dashboard
    assert idx_xterm_css < idx_dashboard
    assert idx_xterm_js < idx_dashboard
    assert idx_xterm_fit < idx_dashboard


def test_compact_cards_use_fingerprint_delta_path() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderCompactCards")
    assert "computeCompactCardFingerprint" in body
    assert "dataset.cardFingerprint" in body
    assert "card.card_id" in body
    assert "dataset.cardId" in body


def test_expanded_cards_render_label_badges() -> None:
    js = _read(DASHBOARD_JS)
    marker = "async function loadExpandedColumn"
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 6000]
    assert "orchestrator_labels" in snippet
    assert "badge-orch" in snippet
    assert "card-badges" in snippet
    assert "resetRetrySingle" in snippet
    assert "retryExpandedSingle" in snippet


def test_session_replay_seek_reuses_terminal_for_forward_progress() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "replaySessionToIndex")
    assert "if (!sessionReplayState.terminal)" in body
    assert "if (clampedIndex < sessionReplayState.playbackIndex)" in body
    assert "for (let index = sessionReplayState.playbackIndex; index < clampedIndex; index += 1)" in body


def test_session_replay_bootstraps_from_recorded_geometry() -> None:
    js = _read(DASHBOARD_JS)
    init_body = _function_body(js, "initializeSessionReplay")
    create_body = _function_body(js, "createSessionReplayTerminal")
    fit_body = _function_body(js, "fitSessionReplayTerminal")
    assert "resolveSessionReplayInitialGeometry" in init_body
    assert "sessionReplayState.initialGeometry" in create_body
    assert "terminalOptions.rows = sessionReplayState.initialGeometry.rows" in create_body
    assert "terminalOptions.cols = sessionReplayState.initialGeometry.cols" in create_body
    assert "if (sessionReplayState.initialGeometry) return;" in fit_body


def test_session_replay_resize_event_does_not_fit_over_recorded_geometry() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "applyTerminalRecordingEvent")
    assert "sessionReplayState.initialGeometry = { rows: event.rows, cols: event.cols }" in body
    assert "sessionReplayState.terminal.resize(event.cols, event.rows);" in body
    assert "fitSessionReplayTerminal();" not in body


def test_unblock_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    for fn in ("unblockSingle", "bulkUnblock", "unblockFromDrawer", "unblockSelectedIssues"):
        body = _function_body(js, fn)
        assert "uiActionContract.buildUnblockRequest" in body
        assert "/api/unblock-retry" not in body


def test_retry_publish_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "retryPublishFromDrawer")
    assert "uiActionContract.buildRetryPublishRequest" in body
    assert "/api/issues/" not in body


def test_render_issue_detail_toggles_retry_publish_button_from_actions() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderIssueDetail")
    assert "issueDetailRetryPublishBtn" in body
    assert "action.id === 'retry_publish'" in body


def test_render_issue_detail_renders_validation_failure_callout() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderIssueDetailValidation")
    assert "summary.run_diagnostic" in body
    assert "action.id === 'open_validation_failure'" in body
    assert "openValidationFailure" in body


def test_open_validation_failure_uses_dedicated_dialog_endpoint() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openValidationFailure")
    assert "/api/dialog/validation-failure/" in body


def test_timeline_prioritizes_validation_details_for_validation_failures() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineEventActions")
    assert "'open_validation_failure'" in body


def test_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "resetSelectedIssues")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "/api/reset-retry" not in body


def test_bulk_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkResetRetry")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "/api/reset-retry" not in body


def test_bulk_reset_from_scratch_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkResetRetryFromScratch")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "fromScratch: true" in body
    assert "/api/reset-retry" not in body


def test_single_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    marker = "async function performResetRetry("
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "uiActionContract.buildResetRetryRequest" in snippet
    assert "/api/reset-retry" not in snippet


def test_deprioritize_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkDeprioritize")
    assert "uiActionContract.buildBulkDeprioritizeRequest" in body
    assert "/api/bulk-deprioritize" not in body


def test_cancel_queued_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    for fn in ("cancelQueuedSingle", "bulkCancelQueued"):
        body = _function_body(js, fn)
        assert "uiActionContract.buildBulkCancelQueuedRequest" in body
        assert "/api/bulk-cancel-queued" not in body


def test_bulk_retry_completed_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkRetryCompleted")
    assert "uiActionContract.buildBulkRetryRequest" in body
    assert "/api/bulk-retry" not in body


def test_bulk_retry_awaiting_merge_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkRetryAwaitingMerge")
    assert "uiActionContract.buildUnblockRequest" in body
    assert "/api/unblock-retry" not in body


def test_bulk_reset_retry_reads_from_blocked_and_awaiting_merge() -> None:
    """Both bulk reset handlers must collect issues from blocked AND awaiting-merge."""
    js = _read(DASHBOARD_JS)
    for fn_name in ("bulkResetRetry", "bulkResetRetryFromScratch"):
        body = _function_body(js, fn_name)
        assert "getSelectedIssueNumbers('blocked')" in body, f"{fn_name} missing blocked selection"
        assert "getSelectedIssueNumbers('awaiting-merge')" in body, f"{fn_name} missing awaiting-merge selection"
        assert "'awaiting-merge'" in body, f"{fn_name} missing awaiting-merge in optimistic requeue"


def test_retry_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "retryIssue")
    assert "uiActionContract.buildIssueRetryRequest" in body
    assert "/api/issues/" not in body


def test_reveal_worktree_menu_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    assert "uiActionContract.buildRevealWorktreeRequest" in js
    assert "/api/finder/" not in js


def test_open_log_file_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openLogFile")
    assert "uiActionContract.buildHostOpenPathRequest" in body
    assert "/api/host/open-path" not in body


def test_session_replay_uses_terminal_recording_endpoint_and_emulator() -> None:
    js = _read(DASHBOARD_JS)
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    open_body = _function_body(js, "openAgentLog")
    refresh_body = _function_body(js, "refreshAgentLog")
    assert "uiActionContract.buildTerminalRecordingRequest" in open_body
    assert "uiActionContract.buildTerminalRecordingRequest" in refresh_body
    assert "/api/log/local/" not in open_body
    assert "/api/log/local/" not in refresh_body
    assert "round_index" in open_body
    assert "session_role" in open_body
    assert "round_index" in contract_js
    assert "session_role" in contract_js


def test_review_transcript_uses_dedicated_endpoint_not_terminal_replay() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewTranscript")
    assert "/api/session/review-transcript/" in body
    assert "/api/session/terminal-recording/" not in body
    assert "Review Transcript #" in body
    assert "round_index" in body
    assert "transcript_role" in body


def test_timeline_prefers_session_recording_before_review_transcript() -> None:
    js = _read(DASHBOARD_JS)
    timeline_body = _function_body(js, "renderTimelineEventActions")
    short_label_body = _function_body(js, "_timelineActionShortLabel")
    assert timeline_body.index("'open_agent_log'") < timeline_body.index("'open_review_transcript'")
    assert "Session Recording" in short_label_body
    assert "Review Transcript" in short_label_body


def test_session_replay_terminal_wrap_allows_scroll_for_fixed_geometry() -> None:
    css = _read(DASHBOARD_CSS)
    assert ".session-replay-terminal {" in css
    assert "overflow: auto;" in css


def test_host_action_contract_exposes_host_neutral_builders() -> None:
    contract_js = _read(UI_ACTION_CONTRACT_JS)
    assert "buildRevealWorktreeRequest" in contract_js
    assert "REVEAL_WORKTREE" in contract_js
    assert "ENDPOINTS.REVEAL_WORKTREE" in contract_js
    assert "buildHostOpenPathRequest" in contract_js
    assert "HOST_OPEN_PATH" in contract_js


def test_requeue_paths_use_optimistic_requeue_helper() -> None:
    js = _read(DASHBOARD_JS)
    for fn in (
        "unblockSingle",
        "bulkUnblock",
        "bulkResetRetry",
        "bulkResetRetryFromScratch",
        "retryExpandedSingle",
        "bulkRetryAwaitingMerge",
        "bulkRetryCompleted",
        "retryIssue",
        "unblockFromDrawer",
        "unblockSelectedIssues",
        "resetSelectedIssues",
    ):
        body = _function_body(js, fn)
        assert "applyOptimisticRequeue(" in body
        assert "location.reload()" not in body
    marker = "async function performResetRetry("
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "applyOptimisticRequeue(" in snippet
    assert "location.reload()" not in snippet


def test_menu_retry_handler_uses_contract_and_refresh_not_reload() -> None:
    js = _read(DASHBOARD_JS)
    marker = "menuRetry?.addEventListener('click'"
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 1400]
    assert "uiActionContract.buildIssueRetryRequest" in snippet
    assert "applyOptimisticRequeue(" in snippet
    assert "await refreshViewModel()" in snippet
    assert "location.reload()" not in snippet


def test_context_menu_orchestrator_log_avoids_legacy_agent_log_endpoint() -> None:
    js = _read(DASHBOARD_JS)
    marker = "menuLog?.addEventListener('click'"
    start = js.find(marker)
    assert start != -1
    snippet = js[start : start + 700]
    assert "openFilteredOrchestratorLog(" in snippet
    assert "/api/log/" not in snippet


def test_timeline_more_menu_renders_inline_list_not_absolute_popover() -> None:
    css = _read(DASHBOARD_CSS)
    assert ".timeline-more-items {" in css
    block = css.split(".timeline-more-items {", 1)[1].split("}", 1)[0]
    assert "position: static;" in block
    assert "overflow: auto;" in block


def test_session_diagnostics_tracks_timeout_and_session_settings_action() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openSessionManifest")
    assert "currentDiagnosticsRunDir" in body
    assert "'timeout'" in body


def test_context_menu_retry_statuses_include_awaiting_merge() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showContextMenu")
    assert "normalizedStatus = statusLower.replace(/_/g, '-')" in body
    assert "effectiveHistoryStatus = (isCompactCardMenu && columnId) ? columnId : normalizedStatus" in body
    assert "'awaiting-merge'" in body


def test_compact_card_context_menu_action_mapping_is_column_consistent() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showContextMenu")
    assert "const isBlockedHistory = effectiveHistoryStatus === 'blocked' || effectiveHistoryStatus === 'needs-human';" in body
    assert "const resetRetryStatuses = new Set(['blocked', 'awaiting-merge']);" in body
    assert "const otherRetryStatuses = new Set(['failed', 'completed', 'timed-out']);" in body
    assert "menuUnblock.style.display = isBlockedHistory ? '' : 'none';" in body
    assert "menuResetRetry.style.display = '';" in body
    assert "menuResetRetryScratch.style.display = '';" in body
    assert "menuRetry.style.display = '';" in body
    assert "setMenuVisible(menuLog, !isCompactCardMenu && !isBlockedHistory);" in body
    assert "setMenuVisible(menuAgentLog, !isCompactCardMenu && !isBlockedHistory);" in body
    assert "setMenuVisible(menuPR, Boolean(prUrl || row.dataset.issueUrl));" in body
    assert "menuPR.textContent = prUrl ? 'Open PR ↗' : 'Open Issue ↗';" in body


def test_context_menu_includes_reset_retry_from_scratch_label() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert "Reset and Retry From Scratch" in html


def test_compact_menu_infers_column_id_from_parent_column() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openCompactCardActionsMenu")
    assert "button?.closest('.kanban-column')?.dataset?.column" in body
    assert "columnId: String(columnId || '')" in body


def test_overlay_positioning_uses_shared_clamp_helpers() -> None:
    js = _read(DASHBOARD_JS)
    assert "function clampPagePoint(" in js
    assert "function clampClientPoint(" in js
    context_body = _function_body(js, "showContextMenu")
    assert "const clamped = clampPagePoint(" in context_body
    confirm_body = _function_body(js, "showConfirm")
    assert "const clamped = clampClientPoint(" in confirm_body


def test_context_menu_open_action_prefers_pr_then_issue_url() -> None:
    js = _read(DASHBOARD_JS)
    assert "const targetUrl = currentRow.dataset.prUrl || currentRow.dataset.issueUrl;" in js
    assert "window.open(targetUrl, '_blank');" in js


def test_embedded_back_hidden_when_column_expanded() -> None:
    js = _read(DASHBOARD_JS)
    assert "function updateEmbeddedBackButtonVisibility()" in js
    assert "label.textContent = hasExpandedColumn ? 'Back to dashboard' : 'Back to repositories';" in js
    assert "embeddedBackLabel" in _read(DASHBOARD_TEMPLATE)
    body = _function_body(js, "toggleColumnExpand")
    assert "updateEmbeddedBackButtonVisibility();" in body
    assert "document.body.classList.toggle('column-focus-mode', !isExpanded);" in body


def test_update_bulk_bar_keeps_retry_columns_visible() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "updateBulkBar")
    assert "blocked', 'awaiting-merge', 'completed'" in body


def test_e2e_tab_hidden_in_column_focus_mode() -> None:
    css = _read(DASHBOARD_CSS)
    assert "body.column-focus-mode #tab-e2e" in css
    assert "display: none;" in css


def test_embedded_back_controls_share_primary_button_style() -> None:
    css = _read(DASHBOARD_CSS)
    assert ".embedded-back {" in css
    assert "color: var(--text);" in css


def test_journey_cycle_labels_use_run_local_numbering() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "const displayCycleNumber = c.cycle_in_run || c.cycle || (cycleIndex + 1);" in body


def test_journey_renders_phase_group_headers() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "c.phase_groups" in body
    assert "journey-phase-header" in body


def test_toggle_journey_cycle_targets_own_header_toggle() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleJourneyCycle")
    assert "const cycleNode = document.getElementById(cycleId);" in body
    assert ":scope > .journey-cycle-header .journey-cycle-toggle" in body


def test_review_feedback_modal_includes_review_comment_events_and_details() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "review.comment_added" in body
    assert "evt.detail" in body
    assert "Open review comment on GitHub" in body


def test_review_feedback_modal_resolves_requested_issue_detail() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "issueDetailData.issue_number === issueNumber" in body
    # Review feedback always fetches with ops view so review.comment_added
    # events (ops-only) are included regardless of the user's current view.
    assert "fetch(`/api/issue-detail/${issueNumber}?view=ops`)" in body


def test_review_feedback_modal_includes_exchange_round_events() -> None:
    """Review exchange round feedback must be rendered in the feedback modal."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "review_exchange.round_completed" in body
    assert "reviewer_response_text" in body
    assert "Review exchange rounds" in body


def test_session_diagnostics_actions_use_primary_plus_visible_secondary_actions() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderGroupedDialogActions")
    assert "primaryTypes" in body
    assert "diag-secondary-actions" in body
    assert "Artifacts & Logs ▾" not in body
    assert "Issue-Scoped Orchestrator Log" in js
    assert "Copy Session Recording" in js
    assert "openSessionManifest(action.issue_number, action.run_dir || null)" in js


def test_diagnostics_action_errors_render_inline_in_modal() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openSessionManifest")
    assert "diagActionMessage" in body
    assert "showToast(data.error" not in body
    assert "reportActionError(" in js
    assert "'inline'" in js


def test_timeline_event_actions_use_primary_plus_more_menu() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineEventActions")
    assert "primaryTypes" in body
    assert "timeline-more-menu" in body
    assert "More ▾" in body
    assert "_timelineActionShortLabel" in js


def test_timeline_modal_delegate_handles_more_items() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    assert ".timeline-action-btn, .timeline-more-item" in body


def test_journey_action_delegate_handles_more_items_and_closes_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert ".timeline-action-btn, .timeline-more-item" in body
    assert "closeTimelineEventMenus(ownerMenu)" in body
    assert "closeTimelineEventMenus();" in body


def test_review_feedback_modal_can_filter_to_specific_timeline_entry() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openReviewFeedback")
    assert "context = null" in js
    assert "_matchesReviewFeedbackContext" in js
    assert "_matchesReviewFeedbackContext(e, context)" in body


def test_journey_renders_local_timestamps_from_raw_event_times() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "formatJourneyHeaderTimestamp(run.timestamp" in body
    assert "formatJourneyHeaderTimestamp(c.timestamp" in body
    assert "formatJourneyStepTimestamp(s.timestamp" in body


def test_journey_layout_uses_content_column_for_actions_and_detail() -> None:
    css = _read(DASHBOARD_CSS)
    assert ".journey-main" in css
    assert ".journey-summary-row" in css
    assert "grid-template-columns: minmax(72px, max-content) minmax(0, 1fr)" in css


def test_toggle_journey_cycle_closes_open_timeline_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "toggleJourneyCycle")
    assert "closeTimelineEventMenus();" in body


def test_journey_empty_state_uses_diagnostic_when_timeline_missing() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "timelineDiagnostic" in body
    assert "expected_history_missing" in body
    assert "Timeline data missing" in body
    assert "Expected timeline store" in body


def test_journey_empty_state_falls_back_to_no_activity_when_no_diagnostic() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert "No activity recorded." in body


def test_expanded_cards_list_uses_start_aligned_grid() -> None:
    """Cards in expanded view should not stretch to fill the container."""
    css = _read(DASHBOARD_CSS)
    # Find the .expanded-cards-list rule
    start = css.find(".expanded-cards-list {")
    assert start != -1, ".expanded-cards-list rule not found in dashboard.css"
    end = css.find("}", start)
    rule = css[start : end + 1]
    assert "align-content: start" in rule
    assert "grid-auto-rows: max-content" in rule


def test_expanded_column_state_handles_running_column() -> None:
    """expanded_column_state.js must map 'running' to active_items."""
    js = Path(
        ROOT / "src" / "issue_orchestrator" / "static" / "js" / "expanded_column_state.js"
    ).read_text(encoding="utf-8")
    assert "columnId === 'running'" in js
    assert "active_items" in js


def test_provider_outage_banner_element_in_template() -> None:
    """Dashboard template must include the provider outage banner element."""
    html = _read(DASHBOARD_TEMPLATE)
    assert "providerOutageBanner" in html
    assert "provider-outage-banner" in html
    assert "provider_circuits" in html


def test_provider_outage_banner_css_exists() -> None:
    """CSS must define .provider-outage-banner styles."""
    css = _read(DASHBOARD_CSS)
    assert ".provider-outage-banner" in css


def test_provider_outage_sse_handlers_in_js() -> None:
    """dashboard.js must handle provider.outage_entered and provider.outage_exited SSE events."""
    js = _read(DASHBOARD_JS)
    assert "provider.outage_entered" in js
    assert "provider.outage_exited" in js
    assert "updateProviderOutageBanner" in js


def test_provider_circuits_initialized_from_dashboard_data() -> None:
    """dashboard.js must seed openProviderCircuits from window.dashboardData.providerCircuits."""
    js = _read(DASHBOARD_JS)
    assert "openProviderCircuits" in js
    assert "providerCircuits" in js
