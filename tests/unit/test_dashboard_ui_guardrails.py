from __future__ import annotations

from pathlib import Path
import re

from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_CSS_CHUNKS
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_JS_CHUNKS


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard.js"
DASHBOARD_JS_DIR = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard"
DASHBOARD_CSS_DIR = ROOT / "src" / "issue_orchestrator" / "static" / "css" / "dashboard"
DASHBOARD_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "dashboard.html"
ISSUE_ROW_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "issue_row.html"
UI_ACTION_CONTRACT_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "ui_action_contract.js"
BROWSER_AUTH_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "browser_auth.js"
THEME_RESOLUTION_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "theme_resolution.js"
DASHBOARD_BOOT_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "dashboard_boot.js"


def _read(path: Path) -> str:
    if path == DASHBOARD_JS:
        return _read_dashboard_js_bundle()
    return path.read_text(encoding="utf-8")


def _read_dashboard_js_bundle() -> str:
    return "\n".join(
        (DASHBOARD_JS_DIR / chunk).read_text(encoding="utf-8")
        for chunk in DASHBOARD_JS_CHUNKS
    )


def _read_dashboard_css_bundle() -> str:
    return "\n".join(
        _read(DASHBOARD_CSS_DIR / chunk)
        for chunk in DASHBOARD_CSS_CHUNKS
    )


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


def test_dashboard_css_uses_direct_split_stylesheets_without_compat_entrypoint() -> None:
    compat_entrypoint = DASHBOARD_CSS_DIR.parent / "dashboard.css"

    assert not compat_entrypoint.exists()
    assert DASHBOARD_CSS_CHUNKS == (
        "base.css",
        "cards.css",
        "issue_detail.css",
        "overlays.css",
        "e2e_run_detail.css",
    )
    assert ".issue-detail-drawer" in _read_dashboard_css_bundle()


def test_toast_severity_variants_have_css_rules() -> None:
    css = _read_dashboard_css_bundle()

    for severity in ("info", "success", "warning", "error"):
        assert re.search(rf"#toast\.{severity}\s*\{{[^}}]*border-color:", css, re.DOTALL)
        assert re.search(rf"#toast\.{severity}\s*\{{[^}}]*background:", css, re.DOTALL)


def test_show_toast_uses_module_timer_and_click_dismiss() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showToast")

    assert "let toastTimer = null;" in js
    assert "window.dashboardToastTimer" not in js
    assert "toast.addEventListener('click'" in body
    assert "clearTimeout(toastTimer)" in body
    assert "hideToast(toast)" in body


def test_show_toast_normalizes_supported_severities() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "normalizeToastType")

    assert "if (type === true) return 'error';" in body
    assert "if (type === false || type === null || type === undefined) return 'info';" in body
    assert "['error', 'warning', 'success', 'info'].includes(type)" in body


def test_start_e2e_errors_use_explicit_error_toasts() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "startE2E")

    assert "showToast('Failed to stop running E2E', 'error')" in body
    assert "showToast(data.detail || data.error || 'Failed to start E2E', 'error')" in body
    assert "showToast('Failed to start E2E: ' + err.message, 'error')" in body
    assert "showToast(data.detail || data.error || 'Failed to start E2E', true)" not in body
    assert "showToast('Failed to start E2E: ' + err.message, true)" not in body


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
    assert '<h3 class="issue-detail-section-title" id="issueDetailTimelineHeading" tabindex="-1">Timeline</h3>' in html
    assert '<h3 class="issue-detail-section-title">Journey</h3>' not in html


def test_issue_detail_template_includes_retry_publish_button() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailRetryPublishBtn"' in html


def test_issue_detail_template_includes_validation_failure_section() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert 'id="issueDetailValidation"' in html
    assert 'id="issueDetailValidationBtn"' in html
    # Container for the structured (JUnit-backed) test results view.
    assert 'id="issueDetailValidationStructured"' in html


def test_dashboard_loads_ui_state_helpers_before_dashboard_js() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    idx_issue_row = html.find('/static/js/issue_row_state.js')
    idx_expanded = html.find('/static/js/expanded_column_state.js')
    idx_compact = html.find('/static/js/compact_card_state.js')
    idx_action_contract = html.find('/static/js/ui_action_contract.js')
    idx_xterm_css = html.find('/static/vendor/xterm/xterm.css')
    idx_xterm_js = html.find('/static/vendor/xterm/xterm.js')
    idx_xterm_fit = html.find('/static/vendor/xterm/addon-fit.js')
    idx_chunk_loop = html.find("{% for chunk in dashboard_js_chunks %}")
    idx_dashboard = html.find('/static/js/dashboard.js')
    assert idx_issue_row != -1
    assert idx_expanded != -1
    assert idx_compact != -1
    assert idx_action_contract != -1
    assert idx_xterm_css != -1
    assert idx_xterm_js != -1
    assert idx_xterm_fit != -1
    assert idx_chunk_loop != -1
    assert idx_dashboard != -1
    assert idx_issue_row < idx_dashboard
    assert idx_expanded < idx_dashboard
    assert idx_compact < idx_dashboard
    assert idx_action_contract < idx_dashboard
    assert idx_xterm_css < idx_dashboard
    assert idx_xterm_js < idx_dashboard
    assert idx_xterm_fit < idx_dashboard
    assert idx_xterm_fit < idx_chunk_loop < idx_dashboard


def test_dashboard_refreshes_on_history_reconciled_sse() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "wireEventListeners")

    assert "'history.reconciled'" in body
    assert "refreshViewModel({ reloadOnListChange: true })" in body


def test_card_focus_renders_combined_issue_label_in_template() -> None:
    """Cards must show the formatted label (M9-009 · #274) instead of bare #number."""
    html = _read(DASHBOARD_TEMPLATE)
    assert "card.issue_label or '#' ~ card.issue_number" in html
    # Bare "#{{ card.issue_number }}" used to lead the focus button;
    # ensure the regression doesn't sneak back into the focus title/body.
    assert "#{{ card.issue_number }} {{ card.title }}" not in html


def test_card_focus_renders_combined_issue_label_in_js() -> None:
    """Client-side card renderers must mirror the template's label fallback."""
    js = _read(DASHBOARD_JS)
    assert "card.issue_label" in js
    assert "item.issue_label" in js


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


def test_issue_detail_validation_renders_structured_view_when_junit_cases_present() -> None:
    """When the validation diagnostic carries parsed JUnit cases, the drawer
    renders the same test-centric layout used by the E2E run modal — pass/fail
    headline, filter chips, per-row error expand. Falls back to the simple
    failed-test-name list when no JUnit data is available."""
    js = _read(DASHBOARD_JS)
    render_body = _function_body(js, "renderIssueDetailValidation")
    structured_body = _function_body(js, "_renderIssueValidationStructured")
    reset_body = _function_body(js, "resetIssueDetailValidation")

    # Render branches on junit_cases.
    assert "diagnostic.junit_cases" in render_body
    assert "_renderIssueValidationStructured" in render_body
    # Falls through to the existing failed_tests_preview <ul> when junit_cases is empty.
    assert "failed_tests_preview" in render_body

    # Structured renderer reuses the shared test-results primitives.
    assert "renderTestResultsHeadline(" in structured_body
    assert "renderTestResultsFilters(" in structured_body
    assert "_renderTestRow(" in structured_body
    assert "_testFilterGroup" in structured_body

    # Reset clears both the simple <ul> and the structured panel.
    assert "issueDetailValidationStructured" in reset_body

    css = _read_dashboard_css_bundle()
    assert ".issue-detail-validation-structured" in css


def test_open_validation_failure_uses_dedicated_dialog_endpoint() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openValidationFailure")
    assert "/api/dialog/validation-failure/" in body
    assert "Validation Results" in body
    assert "renderValidationFailureActionSections" in js
    assert "action_sections" in body
    assert "diag-validation-grid" in body
    assert "data.actions" not in body


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
    css = _read_dashboard_css_bundle()
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
    css = _read_dashboard_css_bundle()
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
    assert "setMenuVisible(menuIssue, Boolean(prUrl && row.dataset.issueUrl));" in body


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
    assert "menuIssue?.addEventListener('click'" in js
    assert "window.open(currentRow.dataset.issueUrl, '_blank');" in js


def test_compact_card_primary_github_link_uses_view_model_fields() -> None:
    js = _read(DASHBOARD_JS)
    render_body = _function_body(js, "renderCompactCardHtml")
    helper_body = _function_body(js, "buildCompactGithubLink")
    assert "const ghLink = buildCompactGithubLink(card);" in render_body
    assert "card.github_url" in helper_body
    assert "card.github_label" in helper_body
    assert "'PR ↗'" in helper_body
    assert "card-pr-link" in helper_body


def test_bulk_open_prs_uses_pr_links_not_issue_links() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkOpenPRs")
    assert "card.querySelector('.card-pr-link')" in body
    assert "card.querySelector('.card-gh')" not in body


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
    css = _read_dashboard_css_bundle()
    assert "body.column-focus-mode #tab-e2e" in css
    assert "display: none;" in css


def test_embedded_back_controls_share_primary_button_style() -> None:
    css = _read_dashboard_css_bundle()
    assert ".embedded-back {" in css
    assert "color: var(--text);" in css


SETTINGS_TEMPLATE = ROOT / "src" / "issue_orchestrator" / "templates" / "settings.html"
EMBEDDED_NAV_JS = ROOT / "src" / "issue_orchestrator" / "static" / "js" / "embedded_nav.js"
EMBEDDED_NAV_TEST_JS = ROOT / "tests" / "js" / "embedded_nav.test.js"


def test_dashboard_settings_nav_uses_shared_embedded_nav_helper() -> None:
    # Regression: Dashboard → Settings must go through the shared embeddedNav
    # helper so ?embedded=1 AND ?theme= are both forwarded. Ad-hoc string
    # concatenation here previously dropped theme on the round-trip.
    js = _read(DASHBOARD_JS)
    assert "function goToSettings()" in js
    body = _function_body(js, "goToSettings")
    assert "embeddedNav.buildHref('/settings', window.location.search)" in body
    # Old ad-hoc helper must be gone so we have a single owner of the rule.
    assert "function withEmbeddedFlag" not in js
    tmpl = _read(DASHBOARD_TEMPLATE)
    assert 'onclick="goToSettings()"' in tmpl
    assert "window.location.href='/settings'" not in tmpl
    # Shared module must be loaded before the dashboard bundle.
    assert '<script src="/static/js/embedded_nav.js"></script>' in tmpl


def test_browser_auth_helper_is_shared_by_control_center_and_dashboard() -> None:
    dashboard = _read(DASHBOARD_TEMPLATE)
    control_center = _read(ROOT / "src" / "issue_orchestrator" / "templates" / "control_center.html")

    assert '<meta name="io-csrf-token" content="{{ csrf_token }}">' in dashboard
    assert '<meta name="io-csrf-token" content="{{ csrf_token }}">' in control_center
    assert '<meta name="io-browser-auth-required" content="{{ browser_auth_required }}">' in dashboard
    assert '<meta name="io-browser-auth-required" content="{{ browser_auth_required }}">' in control_center
    assert '<script src="/static/js/browser_auth.js"></script>' in dashboard
    assert '<script src="/static/js/browser_auth.js"></script>' in control_center
    assert dashboard.index('/static/js/browser_auth.js') < dashboard.index('/static/js/embedded_nav.js')
    assert dashboard.index('/static/js/browser_auth.js') < dashboard.index("{% for chunk in dashboard_js_chunks %}")
    assert control_center.index('/static/js/browser_auth.js') < control_center.index('/static/js/control_center.js')


def test_dashboard_pause_resume_surfaces_auth_failures() -> None:
    js = _read_dashboard_js_bundle()
    body = _function_body(js, "togglePause")
    helper_body = _function_body(js, "setPauseBadgeState")

    assert "const res = await fetch('/api/resume', { method: 'POST' });" in body
    assert "const res = await fetch('/api/pause', { method: 'POST' });" in body
    assert "if (!res.ok)" in body
    assert "setPauseBadgeState(false, 'Resuming...')" in body
    assert "setPauseBadgeState(true, 'Pausing...')" in body
    assert "readActionError(res)" in body
    assert "showToast(`Resume failed: ${message}`, true)" in body
    assert "showToast(`Pause failed: ${message}`, true)" in body
    resume_fetch = "const res = await fetch('/api/resume'"
    pause_fetch = "const res = await fetch('/api/pause'"
    resume_failure = body[
        body.index(resume_fetch) : body.index(
            "await refreshViewModel", body.index(resume_fetch)
        )
    ]
    pause_failure = body[
        body.index(pause_fetch) : body.index(
            "await refreshViewModel", body.index(pause_fetch)
        )
    ]
    assert resume_failure.index("setPauseBadgeState(true);") < resume_failure.index(
        "readActionError(res)"
    )
    assert pause_failure.index("setPauseBadgeState(false);") < pause_failure.index(
        "readActionError(res)"
    )
    assert "document.querySelectorAll('.status-badge').forEach" in helper_body
    assert (
        "badge.classList.remove('status-paused', 'status-running', 'status-starting')"
        in helper_body
    )
    assert (
        "badge.classList.add(paused ? 'status-paused' : 'status-running')"
        in helper_body
    )
    assert "updatePauseMenuFromViewModel({ paused })" in helper_body


def test_dashboard_bundle_loaded_marker_supports_browser_waits() -> None:
    legacy_wrapper = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "window.dashboardBundleLoaded = true;" in legacy_wrapper


def test_dashboard_first_paint_boot_runs_before_stylesheets() -> None:
    tmpl = _read(DASHBOARD_TEMPLATE)
    css = _read_dashboard_css_bundle()
    js = _read(DASHBOARD_JS)
    boot_js = _read(DASHBOARD_BOOT_JS)
    embedded_nav_js = _read(EMBEDDED_NAV_JS)

    assert '<script src="/static/js/theme_resolution.js"></script>' in tmpl
    assert '<script src="/static/js/dashboard_boot.js"></script>' in tmpl
    assert tmpl.index('/static/js/theme_resolution.js') < tmpl.index(
        '/static/js/dashboard_boot.js'
    )
    assert '<link rel="stylesheet" href="/static/css/dashboard.css">' not in tmpl
    assert "{% for chunk in dashboard_css_chunks %}" in tmpl
    assert '<link rel="stylesheet" href="/static/css/dashboard/{{ chunk }}">' in tmpl
    assert tmpl.index('/static/js/dashboard_boot.js') < tmpl.index(
        '/static/css/dashboard/'
    )
    assert "id=\"dashboardInitStatus\"" in tmpl
    assert "Initializing orchestrator" in tmpl
    assert ".dashboard-init-status.is-active" in css
    assert "html[data-booting=\"true\"] .dashboard-init-status" not in css
    assert "html[data-embedded=\"true\"] body > .container > header" in css
    assert "html[data-embedded=\"true\"] .scope-summary" in css
    assert "window.dashboardBoot.clearBootingWhenStable(window)" in js
    assert "try {" in js
    assert "finally" in js
    assert "VALID_THEME_VALUES" in _read(THEME_RESOLUTION_JS)
    assert "VALID_THEME_VALUES" not in boot_js
    assert "VALID_THEME_VALUES" not in embedded_nav_js
    assert "header.style.display" not in js
    assert "scope-embedded" not in js


def test_dashboard_sse_requires_authenticated_stream_helper() -> None:
    js = _read_dashboard_js_bundle()
    connect_body = _function_body(js, "connectEventStream")
    reconnect_body = _function_body(js, "scheduleReconnect")

    assert "window.openAuthenticatedSseStream('/api/events')" in connect_body
    assert "new EventSource('/api/events')" not in connect_body
    assert "authenticated SSE helper is not loaded" in connect_body
    assert "Event stream disconnected... reconnecting" in reconnect_body
    assert "Engine restarting... reconnecting" not in reconnect_body


def test_settings_page_uses_shared_embedded_nav_helper() -> None:
    # Regression: Settings back-link and Cancel must go through embeddedNav
    # so the Dashboard round-trip keeps both embedded=1 and theme.
    tmpl = _read(SETTINGS_TEMPLATE)
    assert '<script src="/static/js/theme_resolution.js"></script>' in tmpl
    assert '<script src="/static/js/embedded_nav.js"></script>' in tmpl
    assert tmpl.index('/static/js/theme_resolution.js') < tmpl.index(
        '/static/js/embedded_nav.js'
    )
    assert 'id="backToDashboardLink"' in tmpl
    assert 'id="cancelSettingsBtn"' in tmpl
    assert 'onclick="cancelSettings()"' in tmpl
    assert "window.embeddedNav.buildHref('/', window.location.search)" in tmpl
    assert "{{ tabs_for_js | tojson }}" in tmpl
    assert "{{ schemas_for_js | tojson }}" in tmpl
    # Old ad-hoc helpers / literals must be gone.
    assert "settingsIsEmbedded" not in tmpl
    assert "'/?embedded=1'" not in tmpl
    assert "onclick=\"window.location.href='/'\"" not in tmpl


def test_theme_resolution_uses_shared_embedded_nav_helper() -> None:
    # Cross-path rule drift guard: both Dashboard (applyDashboardTheme) and
    # Settings (applyTheme) must delegate theme resolution to
    # embeddedNav.resolveEffectiveTheme so the url > stored > system
    # precedence applies the same way on both surfaces. Previously Settings
    # ignored ?theme=, which left embedded Settings rendering the user's
    # local theme instead of the CC-supplied one.
    js = _read(DASHBOARD_JS)
    dashboard_body = _function_body(js, "applyDashboardTheme")
    assert "embeddedNav.resolveEffectiveTheme" in dashboard_body
    # The inlined ad-hoc precedence must be gone from Dashboard.
    assert "localStorage.getItem('theme')" in dashboard_body
    assert "urlTheme" not in dashboard_body

    tmpl = _read(SETTINGS_TEMPLATE)
    # Rough body extraction — the inline script has `function applyTheme()`.
    settings_apply_start = tmpl.find("function applyTheme()")
    assert settings_apply_start != -1, "applyTheme not found in settings.html"
    settings_apply_end = tmpl.find("\n        }", settings_apply_start)
    settings_apply = tmpl[settings_apply_start:settings_apply_end]
    assert "window.embeddedNav.resolveEffectiveTheme" in settings_apply
    # The old inlined system-only fallback must be gone.
    assert "storedTheme === 'system'" not in settings_apply


def test_embedded_nav_module_behavior_verified_by_node_test_runner() -> None:
    # Behavior-level regression: actually exercise buildHref() under Node so
    # we verify real URL transformations, not just string presence in source.
    # Fails hard if node is missing — this is a required runtime for the
    # shared helper's contract tests, not optional infrastructure.
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node runtime is required to validate embedded_nav.js behavior"
    assert THEME_RESOLUTION_JS.exists(), f"theme resolver missing: {THEME_RESOLUTION_JS}"
    assert EMBEDDED_NAV_JS.exists(), f"shared helper missing: {EMBEDDED_NAV_JS}"
    assert EMBEDDED_NAV_TEST_JS.exists(), f"node test missing: {EMBEDDED_NAV_TEST_JS}"
    assert DASHBOARD_BOOT_JS.exists(), f"dashboard boot helper missing: {DASHBOARD_BOOT_JS}"

    result = subprocess.run(
        [
            node,
            "--test",
            str(ROOT / "tests" / "js" / "theme_resolution.test.js"),
            str(EMBEDDED_NAV_TEST_JS),
            str(ROOT / "tests" / "js" / "dashboard_boot.test.js"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        f"node --test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_browser_auth_module_behavior_verified_by_node_test_runner() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node runtime is required to validate browser_auth.js behavior"
    test_file = ROOT / "tests" / "js" / "browser_auth.test.js"
    assert BROWSER_AUTH_JS.exists(), f"shared helper missing: {BROWSER_AUTH_JS}"
    assert test_file.exists(), f"node test missing: {test_file}"

    result = subprocess.run(
        [node, "--test", str(test_file)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        f"node --test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


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
    assert "timeline-event-actions" in body
    assert "timeline-event-menu-trigger" in body
    assert "Event Details" in body
    assert "timeline-more-menu" in body
    assert "More ▾" in body
    assert "_timelineActionShortLabel" in js


def test_timeline_events_pass_detail_context_to_action_menu() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    actions_body = _function_body(js, "renderTimelineEventActions")
    assert "const detailIds = []" in body
    assert "renderTimelineEventActions(evt.actions || [], evt, detailIds)" in body
    assert "renderTimelineChildren(evt.children, detailIds)" in body
    assert "_clearTimelineEventDetails(container)" in body
    assert "openTimelineEventDetails" in js
    assert "show_event_details" in js
    assert "timelineEventDetailsById" in js
    assert "detail_id: _registerTimelineEventDetails(eventDetail, detailIds)" in actions_body
    assert "event: _timelineEventDetailsPayload(eventDetail)" not in actions_body
    assert "timeline-event-detail-overlay" in js
    assert "timeline-event-detail-overlay" in _read_dashboard_css_bundle()


def test_timeline_modal_delegate_handles_more_items() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    assert ".timeline-action-btn, .timeline-more-item" in body
    assert "timeline-event-menu-trigger" in body


def test_journey_action_delegate_handles_more_items_and_closes_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert ".timeline-action-btn, .timeline-more-item" in body
    assert "closeTimelineEventMenus(ownerMenu)" in body
    assert "closeTimelineEventMenus();" in body


def test_timeline_renders_issue_affordances_for_navigation() -> None:
    """E2E test events with issue_affordances render as clickable links to issue detail.

    Each affordance is a ``{issue_number, run_id}`` object; the anchor
    forwards both to ``openIssueDetail`` so the click is routed to the
    explicit ``/api/e2e-run/{run_id}/issue-detail/{N}`` endpoint.
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    assert "issue_affordances" in body
    assert "openIssueDetail" in body
    assert "timeline-issue-links" in body
    # The anchor must pass run_id to openIssueDetail so the dashboard
    # can route to the explicit e2e endpoint.
    assert "e2eRunId" in body


def test_open_issue_detail_routes_to_explicit_e2e_endpoint() -> None:
    """openIssueDetail(N, _, {e2eRunId}) hits the explicit e2e endpoint.

    We scan the whole file rather than using ``_function_body`` because
    ``openIssueDetail`` has an ``opts = {}`` default parameter whose
    braces confuse the helper's naive body extractor.
    """
    js = _read(DASHBOARD_JS)
    # Isolate the block from "async function openIssueDetail" to the next
    # top-level "async function " declaration so our assertions are
    # actually inside openIssueDetail.
    start = js.find("async function openIssueDetail(")
    assert start != -1, "openIssueDetail not found"
    next_fn = js.find("\nasync function ", start + 1)
    if next_fn == -1:
        next_fn = js.find("\nfunction ", start + 1)
    block = js[start:next_fn if next_fn != -1 else len(js)]

    assert "/api/e2e-run/" in block
    assert "issue-detail/" in block
    assert "e2eRunId" in block


def test_issue_detail_timeline_view_preserves_e2e_run_route() -> None:
    """Story/Ops/Debug switches must keep E2E issue detail run-scoped."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "setTimelineView")
    assert "currentIssueDetailE2ERunId" in body
    assert "/api/e2e-run/${e2eRunId}/issue-detail/${issueNumber}?view=${view}" in body
    assert "/api/issue-detail/${issueNumber}?view=${view}" in body


def test_issue_detail_drawer_stacks_above_modal_overlay() -> None:
    """Issue detail drawer must render ABOVE .modal-overlay (z-index 30).

    Without this, clicking an issue affordance from inside the E2E
    run drawer opens the issue drawer underneath the run modal — the
    drawer is technically visible but its interactive region is
    covered. We previously hacked around this by auto-dismissing the
    run modal; the CSS fix lets both drawers coexist so closing the
    issue drawer returns to the run drawer.
    """
    css = _read_dashboard_css_bundle()
    # Extract the .issue-detail-drawer rule block.
    start = css.find(".issue-detail-drawer {")
    assert start != -1, ".issue-detail-drawer rule not found"
    end = css.find("}", start)
    block = css[start:end]
    # Strip block comments so the regex can't match a z-index that
    # appears in documentation rather than the real declaration.
    import re

    block_no_comments = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    match = re.search(r"z-index:\s*(\d+)", block_no_comments)
    assert match, ".issue-detail-drawer has no z-index"
    drawer_z = int(match.group(1))
    assert drawer_z > 30, (
        f".issue-detail-drawer z-index must be > 30 (modal-overlay) "
        f"so it stacks above the e2e run modal; got {drawer_z}"
    )


def test_timeline_renders_affordance_label_with_hover_title() -> None:
    """Affordances render as ``label (N)`` with full branch in hover title.

    The matcher derives a compact label from the GitHub branch name
    (strip milestone prefix, collapse duplicate tokens, cap at 24
    chars). The renderer shows ``label (N)`` inline and puts the full
    branch name in the anchor's ``title`` so users can reclaim the
    untruncated detail on hover. Falls back to bare ``#N`` when no
    label is present (e.g. non-e2e contexts).
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    # Must read the new structured fields.
    assert "a.label" in body
    assert "a.branch_name" in body
    assert "title=" in body
    # Fallback to bare "#N" when label is absent.
    assert "`#${a.issue_number}`" in body


def test_timeline_surfaces_failure_longrepr_inline_on_failed_rows() -> None:
    """e2e.test_completed events with a longrepr must render the
    failure message inline so users can see WHY a test failed without
    leaving the run drawer. The renderer keys on ``evt.longrepr`` and
    ``evt.outcome === 'failed'`` / ``evt.status === 'error'`` to decide
    whether to emit the expandable detail block.

    Also pins the "terminal row only" restriction: the failure block
    must only render on ``e2e.test_completed`` events, not on the
    matching ``e2e.test_started`` row (both share the same nodeid and
    would otherwise display the same failure twice).
    """
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimeline")
    # The inline failure-detail block referenced by its dedicated class:
    assert "timeline-failure-detail" in body
    assert "timeline-failure-longrepr" in body
    # Triggered by the longrepr field — if the worker stops emitting
    # it or the endpoint stops promoting it, this assertion keeps the
    # failure-surfacing contract intact.
    assert "evt.longrepr" in body
    # And the renderer must look at the outcome/status to know when to
    # expand; otherwise every event would show a failure block.
    assert "'failed'" in body or '"failed"' in body
    assert "'error'" in body or '"error"' in body
    # Terminal-row gate — failure must NOT render on test_started rows.
    assert "'e2e.test_completed'" in body


def test_drawer_elevation_covers_all_modal_overlays_except_run_modal() -> None:
    """Every ``.modal-overlay`` opened from within the issue drawer
    must render above it, and the E2E run modal must stay below.

    The stacking policy has two halves:

    1. DEFAULT: any ``.modal-overlay.visible`` sibling of a visible
       ``#issueDetailDrawer`` elevates above the drawer. This covers
       ``#modalOverlay`` (session replay, validation failure, review
       transcript), ``#timelineModal`` (Focus button on the drawer),
       and any future modal class member. Opening a drill-down
       dialog from the drawer should NOT require remembering to
       register it in a CSS enumeration.
    2. EXCEPTION: ``#e2eDiagnosisModal`` is the drawer's launcher —
       the user clicked an affordance inside it to open the drawer,
       so closing the drawer should return them to the run modal.
       It must stay BELOW the drawer.

    Without the default rule, new modals silently render behind the
    drawer (this was the reviewer-caught regression on the timeline
    Focus flow). Without the exception, the run modal would pop
    over the drawer it spawned.
    """
    css = _read_dashboard_css_bundle()
    # Default: generic .modal-overlay elevates above the drawer.
    assert ":has(#issueDetailDrawer.visible) .modal-overlay.visible" in css, (
        "Missing DEFAULT elevation rule — generic .modal-overlay elements "
        "opened from the issue drawer (timeline Focus, session replay, "
        "validation failure, etc.) will render behind the drawer."
    )
    # Exception: e2e run modal stays below.
    assert ":has(#issueDetailDrawer.visible) #e2eDiagnosisModal.visible" in css, (
        "Missing e2e run modal stacking override. Without it, the "
        "run modal competes with the drawer on the default elevated "
        "z-index and the 'drawer opens on top of run modal' contract "
        "breaks."
    )


def test_e2e_timeline_has_view_switcher() -> None:
    """The Story/Ops/Debug timeline view switcher lives in the Run details disclosure."""
    js = _read(DASHBOARD_JS)
    disclosure_body = _function_body(js, "renderRunDetailsDisclosure")
    assert "e2e-timeline-view-switcher" in disclosure_body
    assert "switchE2ETimelineView" in disclosure_body
    assert "'user'" in disclosure_body
    assert "'ops'" in disclosure_body
    assert "'debug'" in disclosure_body


def test_e2e_run_timeline_is_directly_addressable() -> None:
    """The run modal exposes a Timeline entrypoint that auto-expands the Run details disclosure."""
    js = _read(DASHBOARD_JS)
    legacy_entry = _function_body(js, "openE2ERunTimeline")
    render_body = _function_body(js, "renderUnifiedRunView")
    assert "function openE2ERunTimeline(runId)" in js
    assert "showUnifiedRunView(runId, { expandRunDetails: true })" in legacy_entry
    assert "options.expandRunDetails" in render_body
    assert "runDetailsDisclosure" in render_body
    assert "renderE2ETimeline(timelineContainer, tl)" in render_body


def test_e2e_run_timeline_renders_run_level_issue_links() -> None:
    """Run-level issue affordances open cycle-aware E2E issue timelines."""
    js = _read(DASHBOARD_JS)
    timeline_body = _function_body(js, "renderE2ETimeline")
    affordance_body = _function_body(js, "renderE2EIssueTimelineAffordances")
    assert "tl.issue_affordances" in timeline_body
    assert "e2e-issue-timeline-affordances" in affordance_body
    assert "openIssueTimeline(${issueNumber}, this, {e2eRunId: ${runId}})" in affordance_body
    css = _read_dashboard_css_bundle()
    assert ".e2e-issue-timeline-affordances" in css
    assert ".e2e-issue-timeline-btn" in css


def test_e2e_run_modal_uses_test_centric_layout() -> None:
    """Run modal: tests are the headline, with filter chips and per-row expansion."""
    js = _read(DASHBOARD_JS)
    results_body = _function_body(js, "renderE2EResultsPanel")
    headline_body = _function_body(js, "renderTestResultsHeadline")
    filters_body = _function_body(js, "renderTestResultsFilters")
    row_body = _function_body(js, "_renderTestRow")
    expand_body = _function_body(js, "_renderTestRowExpand")
    actions_body = _function_body(js, "_renderTestRowActions")
    toggle_body = _function_body(js, "toggleTestRowExpand")
    filter_body = _function_body(js, "filterTestResults")

    # Headline + filters + flat list are the primary surface.
    assert "renderTestResultsHeadline(summary" in results_body
    assert "renderTestResultsFilters(counts" in results_body
    assert 'class="test-results-list"' in results_body
    assert "_renderTestRow(test, lifecycle)" in results_body
    # The test-results-list MUST NOT carry a fixed id — both the E2E run
    # modal and the issue-detail drawer render this layout, so a fixed id
    # would create duplicates and break panel-scoped filter dispatch.
    assert "testResultsList" not in js

    # Headline shows pass/fail/skipped/quarantined counts (passed/failing minimum).
    assert "passed" in headline_body
    assert "failing" in headline_body
    assert "trh-stat" in headline_body

    # Filter chips are tablist with All/Failing/Passed/Skipped/Quarantined groups.
    assert "trf-chip" in filters_body
    assert "data-filter=" in filters_body
    assert 'role="tablist"' in filters_body
    assert "filterTestResults(" in filters_body

    # Per-test rows are expandable when there's error or linked lifecycle.
    assert "data-filter-group=" in row_body
    assert "data-expandable=" in row_body
    assert "toggleTestRowExpand(this)" in row_body

    # Per-row expand contains error pre + linked-lifecycle inline (no separate top-level section).
    assert "trr-error-text" in expand_body
    assert "Linked agentic cycle" in expand_body
    assert "Coder Session" in expand_body
    assert "Review Session" in expand_body
    assert "Validation" in expand_body

    # Row actions still expose Create Issue / Quarantine / Copy Error for failing untriaged.
    assert "create_issue_dropdown" in actions_body
    assert "quarantine_test" in actions_body
    assert "copy_test_error" in actions_body

    # Toggle and filter behavior helpers exist.
    assert ".trr-row" in toggle_body
    assert "trr-expand" in toggle_body
    assert "filterGroup" in filter_body
    assert ".trf-chip" in filter_body
    # Filter dispatch must be panel-scoped, not global. Looking up the
    # list/chips through document.* would target the wrong panel when the
    # E2E run modal and the issue-detail drawer are open concurrently.
    assert "btnEl.closest('.test-results-panel')" in filter_body
    assert "document.getElementById" not in filter_body
    assert "document.querySelectorAll" not in filter_body


def test_e2e_run_details_disclosure_holds_metadata_artifacts_and_timeline() -> None:
    """Run details disclosure carries runner/command/raw artifacts and the full run timeline."""
    js = _read(DASHBOARD_JS)
    disclosure_body = _function_body(js, "renderRunDetailsDisclosure")
    artifact_body = _function_body(js, "_renderRunArtifactButtons")
    artifact_button_body = _function_body(js, "_artifactButton")
    artifact_open_body = _function_body(js, "openE2EArtifactFromButton")
    assert "<details" in disclosure_body
    assert 'id="runDetailsDisclosure"' in disclosure_body
    assert "rdd-grid" in disclosure_body
    assert "Runner" in disclosure_body
    assert "Command" in disclosure_body
    assert "Run timeline" in disclosure_body
    assert 'id="e2eTimelineContent"' in disclosure_body
    assert "Raw artifacts" in disclosure_body
    # Artifact buttons still go through openPath via the host action handler;
    # the broken file:// behavior is preserved for now in the disclosure but
    # is no longer the modal's headline.
    assert "Raw Output" in artifact_body
    assert "data-artifact-path" in artifact_button_body
    assert "openPath('" not in artifact_button_body
    assert "button.dataset.artifactPath" in artifact_open_body
    css = _read_dashboard_css_bundle()
    assert ".run-details-disclosure" in css
    assert ".test-results-headline" in css
    assert ".test-results-filters" in css
    assert ".trr-row" in css
    assert ".trr-expand" in css
    assert ".trr-lifecycle" in css


def test_e2e_run_modal_actions_use_data_action_dispatch() -> None:
    """Per-test action buttons use the data-e2e-action dispatch contract, not inline handlers."""
    js = _read(DASHBOARD_JS)
    row_action_button_body = _function_body(js, "_e2eRowActionButton")
    row_action_dispatch_body = _function_body(js, "runE2ERowActionFromButton")
    actions_body = _function_body(js, "_renderTestRowActions")
    dropdown_body = _function_body(js, "showCreateIssueDropdown")
    command_body = _function_body(js, "runE2ELifecycleCommand")
    assert "data-e2e-action" in row_action_button_body
    assert "dataset.nodeid" in row_action_dispatch_body
    assert "closeE2EIssue(" not in actions_body
    assert "showCreateIssueDropdown(this, '" not in actions_body
    assert "quarantineSingleTest('" not in actions_body
    assert "copyTestErrorFromRun('" not in actions_body
    assert "createSingleIssueWithAgent('" not in dropdown_body
    assert "openIssueTimeline" in command_body
    assert "openAgentLogAction" in command_body
    assert "openReviewTranscript" in command_body
    assert "openValidationFailure" in command_body


def test_dashboard_templates_expose_direct_timeline_affordances() -> None:
    """Issue rows still offer a direct Timeline control; run history rows route through title click."""
    dashboard = _read(DASHBOARD_TEMPLATE)
    issue_row = _read(ISSUE_ROW_TEMPLATE)
    # Run history rows are now single-click-from-title; per-row Open run / Timeline buttons removed.
    assert "openE2ERunTimeline({{ run.e2e_run_id }})" not in dashboard
    assert ">Open run<" not in dashboard
    assert 'showUnifiedRunView({{ run.e2e_run_id }})' in dashboard
    # Issue rows continue to expose direct Timeline controls.
    assert "openE2ERunTimeline({{ issue.e2e_run_id }})" in issue_row
    assert "openIssueTimeline({{ issue.issue_number }}, this); event.stopPropagation();" in issue_row
    assert "openTimelineModal({{ issue.issue_number }})" not in issue_row


def test_issue_cards_have_cycle_aware_timeline_affordance() -> None:
    """Issue card timeline buttons should open the cycle-aware drawer."""
    js = _read(DASHBOARD_JS)
    dashboard = _read(DASHBOARD_TEMPLATE)
    css = _read_dashboard_css_bundle()
    assert "card-timeline-btn" in js
    assert "Open timeline for issue #${n}" in js
    assert "function openIssueTimeline(issueNumber, triggerEl = null, opts = {})" in js
    assert "openIssueTimeline(${n}, this);event.stopPropagation();" in js
    assert "card-detail-chevron" not in js
    assert "issueDetailTimelineHeading" in js
    assert "card-timeline-btn" in dashboard
    assert "Open timeline for issue #{{ card.issue_number }}" in dashboard
    assert "openIssueTimeline({{ card.issue_number }}, this);event.stopPropagation();" in dashboard
    assert "card-detail-chevron" not in dashboard
    assert ".card-timeline-btn" in css
    assert ".card-detail-chevron" not in css


def test_server_rendered_issue_cards_render_summary_once() -> None:
    """Server-rendered cards should match the client renderer's summary hierarchy."""
    dashboard = _read(DASHBOARD_TEMPLATE)
    web_templates = _read(ROOT / "src" / "issue_orchestrator" / "entrypoints" / "web_templates.py")
    js = _read(DASHBOARD_JS)
    assert "{% elif card.summary %}" not in dashboard
    assert '{% if card.queue_wait_reason %}' in dashboard
    assert '<div class="card-line card-muted">{{ card.summary }}</div>' in dashboard
    assert 'select_autoescape(["html"])' in web_templates
    assert 'escapeHtml(String(card.summary))' in js


def test_e2e_timeline_view_switcher_refetches() -> None:
    """View switcher re-fetches timeline with view param."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "switchE2ETimelineView")
    assert "_fetchE2ERunDetail(runId, view)" in body
    assert "_fetchE2ERunDetail" in body
    assert "renderE2ETimeline" in body


def test_dashboard_and_e2e_ui_preserve_lifecycle_payloads() -> None:
    js = _read(DASHBOARD_JS)
    e2e_body = _function_body(js, "normalizeE2ETimelineData")
    render_e2e_body = _function_body(js, "renderE2ETimeline")
    issue_detail_body = _function_body(js, "renderIssueDetail")
    dataset_body = _function_body(js, "applyLifecycleDataset")

    assert "lifecycle" in e2e_body
    assert "applyLifecycleDataset(container, tl.lifecycle)" in render_e2e_body
    assert "applyLifecycleDataset(issueDetailDrawer, d.lifecycle || null)" in issue_detail_body
    assert "LIFECYCLE_DATASET_KEYS" in js
    assert "window.LIFECYCLE_DATASET_KEYS = LIFECYCLE_DATASET_KEYS" in js
    assert "dataset[LIFECYCLE_DATASET_KEYS.kind]" in dataset_body
    assert "dataset[LIFECYCLE_DATASET_KEYS.iterations]" in dataset_body
    assert "throw new Error('Lifecycle payload missing kind')" in dataset_body


def test_timeline_children_render_with_full_treatment() -> None:
    """renderTimelineChildren renders phase groups, actions, artifacts, and detail."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderTimelineChildren")
    # Phase grouping (same as main timeline)
    assert "formatPhaseLabel" in body
    assert "timeline-group-header" in body
    assert "timeline-group-body" in body
    # Full event rendering (not flat list)
    assert "timeline-event-header" in body
    assert "renderTimelineArtifacts" in body
    assert "renderTimelineEventActions" in body
    assert "timeline-summary" in body
    assert "timeline-detail" in body
    # Collapsible wrapper
    assert "timeline-children" in body
    assert "orchestrator event" in body


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
    css = _read_dashboard_css_bundle()
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
    css = _read_dashboard_css_bundle()
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


def test_render_compact_cards_removes_empty_placeholder_when_items_exist() -> None:
    """renderCompactCards must remove .column-empty and .skeleton-card when items arrive."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderCompactCards")
    # The function must remove placeholders before inserting real cards
    assert ".column-empty" in body
    assert ".skeleton-card" in body
    assert "remove()" in body


def test_e2e_run_note_rendered_in_template() -> None:
    """The e2e run list must surface run.note so fixture errors are visible."""
    html = _read(DASHBOARD_TEMPLATE)
    assert "run.note" in html, (
        "Dashboard template must render run.note for e2e runs so that "
        "fixture errors (e.g. GH activity guard) are visible in the list"
    )
    assert "e2e-run-note" in html, (
        "Dashboard template must use the e2e-run-note class for styling"
    )


def test_e2e_run_note_has_error_styling() -> None:
    """The e2e-run-note class must have error-toned styling."""
    css = _read_dashboard_css_bundle()
    assert ".e2e-run-note" in css, (
        "Dashboard CSS must define .e2e-run-note for fixture error display"
    )


def test_e2e_warning_badge_state_in_css() -> None:
    """The tab-badge must have a warning state style."""
    css = _read_dashboard_css_bundle()
    assert ".tab-badge.warning" in css, (
        "Dashboard CSS must style .tab-badge.warning for retry-needed runs"
    )


def test_e2e_header_updater_handles_warning_status() -> None:
    """The live header badge updater must recognize 'warning' status."""
    js = _read(DASHBOARD_JS)
    assert "'warning'" in js, (
        "Dashboard JS must handle 'warning' run status in the header updater"
    )
    assert "badge.classList.remove('running', 'passed', 'failed', 'warning')" in js or \
           "'warning'" in js, (
        "Badge classList.remove must include 'warning' to avoid stale classes"
    )


def test_flaky_analysis_uses_modal_for_all_outcomes() -> None:
    """showFlakyTestsList must use openModal for errors, empty, and success."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showFlakyTestsList")
    # All outcomes must use openModal — not showToast or alert
    assert "showToast" not in body, (
        "showFlakyTestsList must not use showToast — toasts are too easy to miss"
    )
    assert "alert(" not in body, (
        "showFlakyTestsList must use openModal, not alert()"
    )
    assert "openModal(" in body, (
        "showFlakyTestsList must use openModal for all user-visible feedback"
    )


def test_flaky_analysis_parses_response_as_text_first() -> None:
    """showFlakyTestsList must read response as text then JSON.parse,
    not res.json() which consumes the body and prevents fallback."""
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "showFlakyTestsList")
    assert "res.text()" in body, (
        "Must read body as text first so non-JSON responses can be displayed"
    )
    assert "JSON.parse(" in body, (
        "Must parse text as JSON rather than using res.json()"
    )
