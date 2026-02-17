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


def test_blocked_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkUnblock\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkResetRetry\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkMarkViewed\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkClearViewed\(\)"\s+disabled', html)


def test_completed_and_awaiting_merge_bulk_buttons_default_disabled_in_template() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert re.search(r'onclick="bulkRetryAwaitingMerge\(\)"\s+disabled', html)
    assert re.search(r'onclick="bulkRetryCompleted\(\)"\s+disabled', html)


def test_issue_detail_uses_timeline_label_not_journey() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    assert '<h3 class="issue-detail-section-title">Timeline</h3>' in html
    assert '<h3 class="issue-detail-section-title">Journey</h3>' not in html


def test_dashboard_loads_ui_state_helpers_before_dashboard_js() -> None:
    html = _read(DASHBOARD_TEMPLATE)
    idx_issue_row = html.find('/static/js/issue_row_state.js')
    idx_expanded = html.find('/static/js/expanded_column_state.js')
    idx_compact = html.find('/static/js/compact_card_state.js')
    idx_action_contract = html.find('/static/js/ui_action_contract.js')
    idx_dashboard = html.find('/static/js/dashboard.js')
    assert idx_issue_row != -1
    assert idx_expanded != -1
    assert idx_compact != -1
    assert idx_action_contract != -1
    assert idx_dashboard != -1
    assert idx_issue_row < idx_dashboard
    assert idx_expanded < idx_dashboard
    assert idx_compact < idx_dashboard
    assert idx_action_contract < idx_dashboard


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


def test_unblock_handlers_use_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    for fn in ("unblockSingle", "bulkUnblock", "unblockFromDrawer", "unblockSelectedIssues"):
        body = _function_body(js, fn)
        assert "uiActionContract.buildUnblockRequest" in body
        assert "/api/unblock-retry" not in body


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


def test_single_reset_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "resetRetrySingle")
    assert "uiActionContract.buildResetRetryRequest" in body
    assert "/api/reset-retry" not in body


def test_deprioritize_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "bulkDeprioritize")
    assert "uiActionContract.buildBulkDeprioritizeRequest" in body
    assert "/api/bulk-deprioritize" not in body


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


def test_retry_handler_uses_ui_action_contract() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "retryIssue")
    assert "uiActionContract.buildIssueRetryRequest" in body
    assert "/api/issues/" not in body


def test_requeue_paths_use_optimistic_requeue_helper() -> None:
    js = _read(DASHBOARD_JS)
    for fn in (
        "unblockSingle",
        "bulkUnblock",
        "bulkResetRetry",
        "resetRetrySingle",
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
    assert "const otherRetryStatuses = new Set(['failed', 'completed', 'timed-out', 'awaiting-merge']);" in body
    assert "menuUnblock.style.display = '';" in body
    assert "menuResetRetry.style.display = '';" in body
    assert "menuRetry.style.display = '';" in body
    assert "setMenuVisible(menuLog, !isCompactCardMenu && !isBlockedHistory);" in body
    assert "setMenuVisible(menuAgentLog, !isCompactCardMenu && !isBlockedHistory);" in body


def test_compact_menu_infers_column_id_from_parent_column() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "openCompactCardActionsMenu")
    assert "button?.closest('.kanban-column')?.dataset?.column" in body
    assert "columnId: String(columnId || '')" in body


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
    assert "fetch(`/api/issue-detail/${issueNumber}`)" in body


def test_session_diagnostics_actions_use_primary_plus_more_menu() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "renderGroupedDialogActions")
    assert "primaryTypes" in body
    assert "More ▾" in body
    assert "diag-more-menu" in body
    assert "Issue-Scoped Orchestrator Log" in js
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


def test_journey_action_delegate_handles_more_items_and_closes_menus() -> None:
    js = _read(DASHBOARD_JS)
    body = _function_body(js, "_renderJourneyRuns")
    assert ".timeline-action-btn, .timeline-more-item" in body
    assert "closeTimelineEventMenus(ownerMenu)" in body
    assert "closeTimelineEventMenus();" in body


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
