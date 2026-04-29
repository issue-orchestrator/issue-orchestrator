"""E2E test fixtures split from conftest.py for better organization."""

from .inflight_tracker import (
    find_free_port,
    control_api_headers,
    trigger_refresh,
    register_inflight_issue,
    ensure_inflight_refresh,
    reset_inflight_tracker,
    get_control_api_port,
)
from .orchestrator_process import (
    OrchestratorProcess,
    E2E_LOG_DIR,
    keep_artifacts,
    keep_remote_artifacts,
)
from .github_client import (
    _github_adapter,
    get_issue_comments,
    get_issue_labels,
    get_pr_uncached,
    poll_issue_label,
    POLL_BACKOFF_SECONDS,
)
from .assertions import (
    wait_with_process_check,
    FATAL_ERROR_PATTERNS,
)
from .data_factory import (
    inflight_create,
    inflight_update,
    inflight_close,
)
from .timing import (
    Phase,
    TestTiming,
    E2ETimingStats,
)
from .logging_utils import (
    E2E_PROGRESS_LOG,
    E2E_CURRENT_TEST,
    E2E_SNAPSHOT_LOG,
    write_progress,
    tail_lines,
    find_recent_worktrees,
    claude_project_dir_for,
    snapshot_logs,
)
from .github_utils import (
    is_gh_authenticated,
    GitHubRateLimitError,
    check_github_rate_limit,
    is_rate_limit_error,
    is_github_connection_error,
    is_claude_available,
    is_github_reachable,
    get_repo_from_git,
    get_test_repo,
    env_token_name,
)
from .cleanup import (
    DEFAULT_E2E_FILTER_LABEL,
    cleanup_local_worktrees,
    run_cleanup_step,
    verify_cleanup_items,
    cleanup_remote_branches,
    cleanup_prs,
    ensure_pr_label,
    ensure_required_pr_labels,
    cleanup_e2e_labels,
    cleanup_issues,
)
from .gh_audit import (
    fetch_gh_audit_report,
    usage_units_from_report,
    calls_from_report,
    scope_usage,
    scope_calls,
    delta_counts,
    log_top_deltas,
)
from .wait_helpers import (
    wait_for_issue_seen,
    wait_for_session_started,
    wait_for_session_completed,
    wait_for_issue_label_snapshot,
    wait_for_file_with_content,
)

__all__ = [
    # inflight_tracker
    "find_free_port",
    "control_api_headers",
    "trigger_refresh",
    "register_inflight_issue",
    "ensure_inflight_refresh",
    "reset_inflight_tracker",
    "get_control_api_port",
    # cleanup
    "verify_cleanup_items",
    # orchestrator_process
    "OrchestratorProcess",
    "E2E_LOG_DIR",
    "keep_artifacts",
    "keep_remote_artifacts",
    # github_client
    "_github_adapter",
    "get_issue_comments",
    "get_issue_labels",
    "get_pr_uncached",
    "poll_issue_label",
    "POLL_BACKOFF_SECONDS",
    # assertions
    "wait_with_process_check",
    "FATAL_ERROR_PATTERNS",
    # data_factory
    "inflight_create",
    "inflight_update",
    "inflight_close",
    # timing
    "Phase",
    "TestTiming",
    "E2ETimingStats",
    # logging_utils
    "E2E_PROGRESS_LOG",
    "E2E_CURRENT_TEST",
    "E2E_SNAPSHOT_LOG",
    "write_progress",
    "tail_lines",
    "find_recent_worktrees",
    "claude_project_dir_for",
    "snapshot_logs",
    # github_utils
    "is_gh_authenticated",
    "GitHubRateLimitError",
    "check_github_rate_limit",
    "is_rate_limit_error",
    "is_github_connection_error",
    "is_claude_available",
    "is_github_reachable",
    "get_repo_from_git",
    "get_test_repo",
    "env_token_name",
    # cleanup
    "DEFAULT_E2E_FILTER_LABEL",
    "cleanup_local_worktrees",
    "run_cleanup_step",
    "verify_cleanup_items",
    "cleanup_remote_branches",
    "cleanup_prs",
    "ensure_pr_label",
    "ensure_required_pr_labels",
    "cleanup_e2e_labels",
    "cleanup_issues",
    # gh_audit
    "fetch_gh_audit_report",
    "usage_units_from_report",
    "calls_from_report",
    "scope_usage",
    "scope_calls",
    "delta_counts",
    "log_top_deltas",
    # wait_helpers
    "wait_for_issue_seen",
    "wait_for_session_started",
    "wait_for_session_completed",
    "wait_for_issue_label_snapshot",
    "wait_for_file_with_content",
]
