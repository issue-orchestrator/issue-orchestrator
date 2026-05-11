"""Static asset manifests for dashboard template rendering."""

from __future__ import annotations

DASHBOARD_JS_CHUNKS: tuple[str, ...] = (
    "core.js",
    "session_replay.js",
    "session_dialogs.js",
    "controls_refresh.js",
    "kanban_columns.js",
    "issue_metadata.js",
    "issue_menus.js",
    "issue_detail_modals.js",
    "issue_detail_drawer.js",
    "timeline.js",
    "diagnostics_actions.js",
    "shell_actions.js",
    "e2e_runtime.js",
    "e2e_triage.js",
    "test_results_panel.js",
    "e2e_run_view.js",
)

DASHBOARD_CSS_CHUNKS: tuple[str, ...] = (
    "base.css",
    "cards.css",
    "issue_detail.css",
    "overlays.css",
    "e2e_run_detail.css",
)
