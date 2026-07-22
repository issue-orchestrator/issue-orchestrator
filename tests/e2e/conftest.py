"""E2E test fixtures for live testing.

These fixtures create real GitHub issues and run the orchestrator.
"""

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Generator, AsyncGenerator

import pytest

from issue_orchestrator.infra.config import Config, AgentConfig
from issue_orchestrator.domain.issue_key import IssueKey, GitHubIssueKey
from issue_orchestrator.testing.asyncdsl import (
    OrchestratorWatcher,
    SSEEventStream,
    HTTPSnapshotProvider,
    HTTPReplayProvider,
    WatcherConfig,
)
from issue_orchestrator.testing.support.test_data import (
    create_issue,
    create_test_issues,
    cleanup_test_issues,
    cleanup_issues_by_label,
)
from ._stale_orchestrator_cleanup import kill_stale_e2e_orchestrators

# Import all helpers from fixture modules
from .fixtures import (
    # Core process/tracking
    find_free_port,
    trigger_refresh,
    register_inflight_issue,
    ensure_inflight_refresh,
    reset_inflight_tracker,
    get_control_api_port,
    OrchestratorProcess,
    E2E_LOG_DIR,
    keep_artifacts,
    keep_remote_artifacts,
    _github_adapter,
    get_issue_comments,
    wait_with_process_check,
    FATAL_ERROR_PATTERNS,
    inflight_create,
    inflight_update,
    inflight_close,
    # Timing
    E2ETimingStats,
    # Logging utils
    E2E_CURRENT_TEST,
    write_progress,
    snapshot_logs,
    # GitHub utils
    is_gh_authenticated,
    is_github_connection_error,
    is_claude_available,
    is_github_reachable,
    get_test_repo,
    env_token_name,
    # Cleanup
    DEFAULT_E2E_FILTER_LABEL,
    cleanup_local_worktrees,
    run_cleanup_step,
    verify_cleanup_items,
    cleanup_remote_branches,
    cleanup_prs,
    ensure_required_pr_labels,
    cleanup_e2e_labels,
    cleanup_issues,
    # GH Audit
    fetch_gh_audit_report,
    usage_units_from_report,
    calls_from_report,
    scope_usage,
    scope_calls,
    delta_counts,
    log_top_deltas,
    # Wait helpers
    wait_for_issue_seen,
    wait_for_session_started,
    wait_for_issue_label_snapshot,
    # Direct GitHub polling (more efficient than full refresh)
    poll_issue_label,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

E2E_TEST_LABEL_PREFIX = "io:e2e:"
E2E_RUN_LABEL_PREFIX = "io:e2e-run-"
E2E_LABEL_CLEANUP_PREFIXES = (E2E_RUN_LABEL_PREFIX, E2E_TEST_LABEL_PREFIX)

def e2e_label(logical: str) -> str:
    """Apply the e2e label prefix to a logical test label."""
    if logical.startswith(E2E_TEST_LABEL_PREFIX):
        return logical
    return f"{E2E_TEST_LABEL_PREFIX}{logical}"


# ---------------------------------------------------------------------------
# Pytest Configuration Hooks
# ---------------------------------------------------------------------------

def pytest_configure(config):
    """Configure pytest for e2e tests - fail fast by default."""
    if any("e2e" in str(arg) for arg in config.args):
        # Only set maxfail if not explicitly passed on the command line
        explicit_maxfail = any(
            arg.startswith("--maxfail") or arg.startswith("-x")
            for arg in config.invocation_params.args
        )
        if not explicit_maxfail:
            config.option.maxfail = 1
            logger.info("[E2E] Fail-fast enabled (maxfail=1)")


def pytest_sessionstart(session):
    write_progress("SESSION_START")


def pytest_sessionfinish(session, exitstatus):
    write_progress("SESSION_FINISH", extra=f"exitstatus={exitstatus}")
    if exitstatus != 0:
        snapshot_logs(f"exitstatus={exitstatus}")

    # Print timing summary
    global _timing_stats
    if _timing_stats and _timing_stats.test_timings:
        _timing_stats.print_summary()
    print(f"\n  [E2E] Log files directory: {E2E_LOG_DIR}", flush=True)
    log_files = sorted(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if log_files:
        print(f"  [E2E] Latest log: {log_files[0]}", flush=True)


def pytest_runtest_logstart(nodeid, location):
    try:
        E2E_CURRENT_TEST.write_text(nodeid)
    except OSError:
        pass
    write_progress("TEST_START", nodeid=nodeid)


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    status = "PASSED"
    if report.failed:
        status = "FAILED"
    elif report.skipped:
        status = "SKIPPED"
    write_progress("TEST_END", nodeid=report.nodeid, extra=f"status={status}")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test result on the node for access in fixtures."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# ---------------------------------------------------------------------------
# Global Timing State
# ---------------------------------------------------------------------------

_timing_stats: E2ETimingStats | None = None


# ---------------------------------------------------------------------------
# Skip Markers
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.skipif(
        not is_gh_authenticated(),
        reason="GitHub CLI not authenticated"
    ),
    pytest.mark.skipif(
        not is_github_reachable(get_test_repo()),
        reason="GitHub API not reachable"
    ),
    pytest.mark.skipif(
        not is_claude_available(),
        reason="Claude CLI not available"
    ),
]


# ---------------------------------------------------------------------------
# Session-Scoped Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def gh_audit_session() -> Generator[None, None, None]:
    """Enable gh audit for e2e runs with a per-session file prefix."""
    from issue_orchestrator import gh_audit

    run_id = str(int(time.time()))
    gh_audit.configure(
        enabled=True,
        include_events=True,
        audit_path=str(E2E_LOG_DIR / f"gh-audit-{{pid}}-{run_id}.json"),
    )
    gh_audit.reset_stats()
    yield


@pytest.fixture(scope="session", autouse=True)
def kill_stale_orchestrators():
    """Kill stale E2E-owned orchestrator processes before running e2e tests."""
    kill_stale_e2e_orchestrators()
    yield


@pytest.fixture(scope="session", autouse=True)
def e2e_reconciliation_at_session_start(e2e_worktree_base: Path):
    """Comprehensive e2e test reconciliation around the test session.

    Cleans up:
    - Default /tmp/e2e-worktrees directory
    - Current e2e test session's worktree directory
    - E2E PRs/branches/issues before and after the session
    """
    repo = get_test_repo()
    logger.info("=" * 60)
    logger.info("[E2E RECONCILIATION] Cleaning up artifacts from previous runs...")
    logger.info("[E2E RECONCILIATION] worktree_base=%s", e2e_worktree_base)
    logger.info("=" * 60)

    if keep_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping local cleanup (E2E_KEEP_ARTIFACTS=1)")
    else:
        # Clean up default locations (from non-isolated runs)
        cleanup_local_worktrees()
        # Clean up this session's isolated resources
        cleanup_local_worktrees(e2e_worktree_base)

    if keep_remote_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping remote cleanup (E2E_KEEP_REMOTE_ARTIFACTS=1)")
        prs_closed = branches_deleted = issues_closed = labels_deleted = 0
        ensure_required_pr_labels(repo)
    else:
        prs_closed = run_cleanup_step("PR cleanup", lambda: cleanup_prs(repo), timeout_s=120)
        branches_deleted = 0  # cleanup_prs already deletes branches for all PRs
        issues_closed = run_cleanup_step("Issue cleanup", lambda: cleanup_issues(repo), timeout_s=120)
        labels_deleted = run_cleanup_step("Label cleanup", lambda: cleanup_e2e_labels(repo, E2E_LABEL_CLEANUP_PREFIXES), timeout_s=60)
        ensure_required_pr_labels(repo)

    logger.info("=" * 60)
    logger.info("[E2E RECONCILIATION] Summary: PRs=%d, Branches=%d, Issues=%d, Labels=%d",
                prs_closed, branches_deleted, issues_closed, labels_deleted)
    logger.info("=" * 60)
    yield

    logger.info("=" * 60)
    logger.info("[E2E RECONCILIATION] Cleaning up artifacts from completed run...")
    logger.info("=" * 60)

    if keep_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping post-run local cleanup (E2E_KEEP_ARTIFACTS=1)")
    else:
        cleanup_local_worktrees(e2e_worktree_base)

    if keep_remote_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping post-run remote cleanup (E2E_KEEP_REMOTE_ARTIFACTS=1)")
        prs_closed = issues_closed = labels_deleted = 0
    else:
        prs_closed = run_cleanup_step("Post-run PR cleanup", lambda: cleanup_prs(repo), timeout_s=120)
        issues_closed = run_cleanup_step("Post-run issue cleanup", lambda: cleanup_issues(repo), timeout_s=120)
        labels_deleted = run_cleanup_step(
            "Post-run label cleanup",
            lambda: cleanup_e2e_labels(repo, E2E_LABEL_CLEANUP_PREFIXES),
            timeout_s=60,
        )

    logger.info("=" * 60)
    logger.info(
        "[E2E RECONCILIATION] Post-run summary: PRs=%d, Issues=%d, Labels=%d",
        prs_closed,
        issues_closed,
        labels_deleted,
    )
    logger.info("=" * 60)


@pytest.fixture(scope="session")
def e2e_timing_stats() -> E2ETimingStats:
    """Session-scoped timing statistics."""
    global _timing_stats
    _timing_stats = E2ETimingStats()
    return _timing_stats


@pytest.fixture(scope="session")
def repo_name() -> str:
    """Get the repo name for e2e tests."""
    return get_test_repo()


@pytest.fixture(scope="session")
def e2e_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="session")
def e2e_session_tmp(tmp_path_factory) -> Path:
    """Session-scoped temp directory for e2e tests."""
    return tmp_path_factory.mktemp("e2e")


@pytest.fixture(scope="session")
def e2e_run_id() -> str:
    """Unique identifier for this e2e test run.

    Used to isolate e2e test resources (worktrees) from
    a running orchestrator instance.
    """
    return f"e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def e2e_worktree_base(e2e_run_id: str) -> Path:
    """Worktree base directory for this e2e test run.

    Isolated from the default '/tmp/e2e-worktrees' to avoid conflicts.
    """
    base = Path(f"/tmp/{e2e_run_id}-worktrees")
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture(scope="session")
def filter_label() -> str:
    """Configurable filter label for parallel test runs."""
    return os.environ.get("E2E_FILTER", DEFAULT_E2E_FILTER_LABEL)


@pytest.fixture(scope="session")
def e2e_ui_mode() -> str:
    """Get the UI mode for e2e tests.

    Configurable via E2E_UI_MODE environment variable.
    Defaults to 'web' (subprocess backend).
    """
    return os.environ.get("E2E_UI_MODE", "web")


@pytest.fixture(scope="session")
def e2e_terminal_adapter() -> str | None:
    """Optional terminal adapter override for e2e tests.

    Set E2E_TERMINAL_ADAPTER=subprocess to run with the subprocess backend.
    """
    value = os.environ.get("E2E_TERMINAL_ADAPTER")
    return value if value else None


@pytest.fixture(scope="session")
def e2e_session_config(
    e2e_project_root: Path,
    e2e_worktree_base: Path,
    repo_name: str,
    e2e_ui_mode: str,
    e2e_terminal_adapter: str | None,
) -> Config:
    """Session-scoped config for single orchestrator."""
    config = Config()
    config.repo = repo_name
    config.repo_root = e2e_project_root
    config.worktree_base = e2e_worktree_base
    config.ui_mode = e2e_ui_mode
    config.terminal_adapter = e2e_terminal_adapter
    config.max_concurrent_sessions = 4
    config.filtering.label = "io-e2e-test-data"
    config.github_token_env = env_token_name()
    config.queue_refresh_seconds = 600
    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    config.control_api_port = int(env_port) if env_port else find_free_port()
    # Auto-select web port to avoid conflict with running orchestrator (default 8080)
    env_web_port = os.environ.get("E2E_WEB_PORT")
    config.web_port = int(env_web_port) if env_web_port else find_free_port()
    config.e2e_pr_labels = ["io-e2e-test-data"]
    config.code_review_agent = "agent:script-review"
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.max_rework_cycles = 2
    config.tech_lead_review_agent = None
    config.tech_lead_review_threshold = 2
    # Disable review exchange for e2e tests — running real claude review sessions
    # is too slow and causes test timeouts.  Tests that need review behavior should
    # use dedicated review-flow tests with script agents.
    config.review_exchange_mode = "via-draft-pr"
    config.gh_audit_enabled = True
    config.gh_audit_events = True
    config.gh_audit_file = str(E2E_LOG_DIR / "gh-audit-{pid}.json")

    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "tests" / "e2e" / "fixtures" / "scripts" / "complete-immediately.sh",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
        "agent:script-completes": AgentConfig(
            prompt_path=e2e_project_root / "tests" / "e2e" / "fixtures" / "scripts" / "complete-immediately.sh",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
        "agent:script-review": AgentConfig(
            prompt_path=e2e_project_root / "tests" / "e2e" / "fixtures" / "scripts" / "review-decider.sh",
            timeout_minutes=3,
            model="sonnet",
            command="PR_NUMBER={pr_number} bash {prompt}",
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
        "agent:tech-lead-investigator": AgentConfig(
            prompt_path=e2e_project_root / "tests" / "e2e" / "fixtures" / "scripts" / "complete-immediately.sh",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
    }

    config.session_timeout_minutes = 3
    from tests.e2e.flows import review_timeout_from_config
    os.environ["E2E_REVIEW_TIMEOUT_S"] = str(review_timeout_from_config(config))

    config.validation.quick.cmd = "true"
    config.validation.quick.timeout_seconds = 30
    config.validation.publish.cmd = "true"
    config.validation.publish.timeout_seconds = 30
    if keep_artifacts():
        config.cleanup.with_tech_lead.close_ai_session_tabs = False
        config.cleanup.with_tech_lead.remove_worktrees = False
        config.cleanup.without_tech_lead.close_ai_session_tabs = False
        config.cleanup.without_tech_lead.remove_worktrees = False
    os.environ["E2E_CONTROL_API_PORT"] = str(config.control_api_port)
    os.environ["E2E_WEB_PORT"] = str(config.web_port)

    # Live by default: only use dry-run when explicitly requested.
    dry_run_env = os.environ.get("E2E_DRY_RUN_PUSH")
    if dry_run_env in ("1", "true", "True"):
        os.environ["E2E_DRY_RUN_PUSH"] = "1"
        print("[E2E] Running in DRY RUN mode (E2E_DRY_RUN_PUSH=1)")
    else:
        os.environ["E2E_DRY_RUN_PUSH"] = "false"
        print("[E2E] Running with REAL PR creation (E2E_DRY_RUN_PUSH=false)")

    return config


@pytest.fixture(scope="session")
def e2e_issues(repo_name: str) -> Generator[dict[str, int], None, None]:
    """Create all e2e test issues once at session start."""
    cleanup_test_issues(repo_name)

    issues = {
        "simple_task": create_issue(
            repo_name,
            "[E2E] Simple task",
            ["agent:e2e-test", "io-e2e-test-data"],
            body="A simple task for basic e2e testing.",
        ),
        "will_block": create_issue(
            repo_name,
            "[E2E] Task that blocks",
            ["agent:e2e-test", "io-e2e-test-data"],
            body="This task should end up blocked.",
        ),
    }

    print(f"\n[E2E SETUP] Created {len(issues)} test issues: {issues}")
    yield issues
    print(f"\n[E2E TEARDOWN] Cleaning up test issues...")
    cleanup_test_issues(repo_name)


@pytest.fixture(scope="session")
def e2e_orchestrator(
    e2e_session_config: Config,
    e2e_project_root: Path,
    filter_label: str,
) -> Generator["OrchestratorProcess", None, None]:
    """Single orchestrator instance for all e2e tests."""
    proc = OrchestratorProcess(e2e_session_config, e2e_project_root)
    max_issues = int(os.environ.get("E2E_MAX_ISSUES", "50"))
    proc.start(max_issues=max_issues, extra_args=["--label", filter_label])

    # Wait for orchestrator to be ready with retry logic
    max_retries = 10
    retry_delay = 1.0
    for attempt in range(max_retries):
        if not proc.is_running():
            # Process died - get logs for diagnostics
            stdout, stderr = proc.stop()
            log_contents = ""
            # noqa: SLF001 - E2E test infrastructure needs log access for diagnostics
            if proc._orchestrator_log_file and proc._orchestrator_log_file.exists():  # noqa: SLF001
                log_contents = f"\nLog file ({proc._orchestrator_log_file}):\n{proc._orchestrator_log_file.read_text()[-2000:]}"  # noqa: SLF001
            raise RuntimeError(
                f"Orchestrator process exited unexpectedly.\n"
                f"stdout: {stdout}\nstderr: {stderr}{log_contents}"
            )

        if proc._check_api_running():  # noqa: SLF001
            print(f"\n[E2E] Orchestrator API ready (pid={proc.process.pid}, attempt {attempt + 1})")
            break

        if attempt < max_retries - 1:
            print(f"  [E2E] Waiting for API to be ready (attempt {attempt + 1}/{max_retries})...")
            time.sleep(retry_delay)
    else:
        # API never became ready
        stdout, stderr = proc.stop()
        log_contents = ""
        # noqa: SLF001 - E2E test infrastructure needs log access for diagnostics
        if proc._orchestrator_log_file and proc._orchestrator_log_file.exists():  # noqa: SLF001
            log_contents = f"\nLog file ({proc._orchestrator_log_file}):\n{proc._orchestrator_log_file.read_text()[-2000:]}"  # noqa: SLF001
        raise RuntimeError(
            f"Orchestrator API did not become ready after {max_retries} attempts.\n"
            f"stdout: {stdout}\nstderr: {stderr}{log_contents}"
        )

    yield proc
    print(f"\n[E2E TEARDOWN] Stopping orchestrator...")
    proc.stop()


# ---------------------------------------------------------------------------
# Test-Scoped Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def track_test_timing(request, e2e_timing_stats):
    """Automatically track timing for each e2e test."""
    test_name = request.node.name
    e2e_timing_stats.start_test(test_name)
    print(f"\n⏱️  [{test_name}] Started at {time.strftime('%H:%M:%S')}")

    yield

    status = "passed"
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        status = "failed"

    timing = e2e_timing_stats.end_test(status)
    if timing:
        print(f"⏱️  [{test_name}] Completed in {timing.duration:.1f}s [{status}]")
        if timing.phases:
            timing.print_phases(indent="    ")


@pytest.fixture(autouse=True)
def e2e_inflight_refresh_guard() -> None:
    """Reset refresh tracking per test so inflight issues don't leak."""
    reset_inflight_tracker()
    yield


@pytest.fixture(autouse=True)
def e2e_gh_activity_guard(request) -> None:
    """Guard GH API activity within configured limits."""
    marker = request.node.get_closest_marker("gh_activity_limit")
    if marker is None:
        if request.node.get_closest_marker("e2e") is not None:
            pytest.fail("Missing gh_activity_limit marker on e2e test")
        yield
        return

    if marker.kwargs:
        unexpected = set(marker.kwargs.keys()) - {"test_gh_activity_limit", "system_gh_activity_limit"}
        if unexpected:
            pytest.fail(
                "gh_activity_limit only accepts test_gh_activity_limit and system_gh_activity_limit, "
                f"got: {sorted(unexpected)}"
            )
    missing = {"test_gh_activity_limit", "system_gh_activity_limit"} - set(marker.kwargs.keys())
    if missing:
        pytest.fail(f"gh_activity_limit requires {sorted(missing)} to be specified")

    port = get_control_api_port()
    before = fetch_gh_audit_report(port)
    yield
    after = fetch_gh_audit_report(port)
    if not before or not after:
        return

    delta_usage = usage_units_from_report(after) - usage_units_from_report(before)
    delta_calls = calls_from_report(after) - calls_from_report(before)
    delta_startup_usage = scope_usage(after, "startup") - scope_usage(before, "startup")
    delta_periodic_usage = scope_usage(after, "periodic") - scope_usage(before, "periodic")
    charged_usage = delta_usage - delta_startup_usage - delta_periodic_usage

    max_test_usage = marker.kwargs.get("test_gh_activity_limit")
    max_system_usage = marker.kwargs.get("system_gh_activity_limit")
    system_usage = delta_startup_usage + delta_periodic_usage

    logger.info("[E2E] GH activity usage: test=%d system=%d total=%d", charged_usage, system_usage, delta_usage)
    log_top_deltas("by_issue", delta_counts(before, after, "by_issue"))
    log_top_deltas("by_caller", delta_counts(before, after, "by_caller"))
    log_top_deltas("by_command", delta_counts(before, after, "by_command"))

    if max_test_usage is not None and charged_usage > int(max_test_usage):
        pytest.fail(f"Test GH activity exceeded limit: {charged_usage} > {max_test_usage}")
    if max_system_usage is not None and system_usage > int(max_system_usage):
        pytest.fail(f"System GH activity exceeded limit: {system_usage} > {max_system_usage}")


@pytest.fixture
async def orchestrator_watcher(
    e2e_orchestrator: "OrchestratorProcess",
) -> AsyncGenerator[OrchestratorWatcher, None]:
    """Async watcher wired to the control API SSE stream."""
    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    port = int(env_port) if env_port is not None else Config().control_api_port
    if port <= 0:
        pytest.skip("Control API disabled; async watcher unavailable")

    # Both watcher paths (this fixture and ``create_watcher_for_port``
    # in flows.py) route through ``build_watcher_clients`` so the
    # auth-wiring contract has one owner; see
    # ``tests/e2e/_watcher_auth.py``.
    from tests.e2e._watcher_auth import build_watcher_clients
    stream, snapshot_provider, replay_provider = build_watcher_clients(port)
    await stream.start()
    watcher = await OrchestratorWatcher.create(
        event_stream=stream,
        snapshot_provider=snapshot_provider,
        replay_provider=replay_provider,
        config=WatcherConfig(),
    )
    try:
        yield watcher
    finally:
        await watcher.close()
        await stream.close()


@pytest.fixture
def test_label(request) -> str:
    """Generate unique label from test name for isolation."""
    return request.node.name


@pytest.fixture
def test_issue_factory(repo_name: str, test_label: str, filter_label: str):
    """Factory for creating test-scoped issues."""
    cleanup_issues_by_label(repo_name, e2e_label(test_label))

    def create(title: str, extra_labels: list[str] | None = None) -> IssueKey:
        labels = [filter_label, "agent:e2e-test", e2e_label(test_label)]
        if extra_labels:
            labels.extend(extra_labels)
        try:
            issue_num = create_issue(repo_name, title, labels)
        except RuntimeError as exc:
            if is_github_connection_error(str(exc)):
                pytest.skip("GitHub API not reachable for live e2e tests")
            raise
        issue = GitHubIssueKey(repo=repo_name, external_id=str(issue_num))
        register_inflight_issue(issue)
        return issue

    return create


@pytest.fixture
def e2e_flow(repo_name: str, orchestrator_watcher, filter_label: str):
    """E2EFlow fixture with automatic cleanup on teardown.

    This ensures issues created during the test are cleaned up even if
    the test fails or times out.
    """
    from tests.e2e.flows import E2EFlow

    flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)
    yield flow
    # Cleanup runs even if test fails
    flow.cleanup_created_issues()


@pytest.fixture
def e2e_config(e2e_project_root: Path, tmp_path: Path, repo_name: str, e2e_ui_mode: str) -> Config:
    """Create e2e test config with e2e-test agent."""
    config = Config()
    config.repo = repo_name
    config.repo_root = e2e_project_root
    config.worktree_base = tmp_path / "worktrees"
    config.ui_mode = e2e_ui_mode
    config.terminal_adapter = os.environ.get("E2E_TERMINAL_ADAPTER")
    config.max_concurrent_sessions = 1
    config.filtering.label = "io-e2e-test-data"
    config.github_token_env = env_token_name()

    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "tests" / "e2e" / "fixtures" / "scripts" / "complete-immediately.sh",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            ai_system="claude-code",
        )
    }

    config.session_timeout_minutes = 3
    from tests.e2e.flows import review_timeout_from_config
    os.environ["E2E_REVIEW_TIMEOUT_S"] = str(review_timeout_from_config(config))

    config.validation.quick.cmd = "true"
    config.validation.quick.timeout_seconds = 30
    config.validation.publish.cmd = "true"
    config.validation.publish.timeout_seconds = 30
    if keep_artifacts():
        config.cleanup.with_tech_lead.close_ai_session_tabs = False
        config.cleanup.with_tech_lead.remove_worktrees = False
        config.cleanup.without_tech_lead.close_ai_session_tabs = False
        config.cleanup.without_tech_lead.remove_worktrees = False

    return config


@pytest.fixture
def test_issues(repo_name: str) -> Generator[list[int], None, None]:
    """Create test issues, yield issue numbers, then cleanup."""
    cleanup_test_issues(repo_name)
    issue_numbers = create_test_issues(repo_name, ["agent:e2e-test"])
    yield issue_numbers
    cleanup_test_issues(repo_name)


@pytest.fixture
def single_test_issue(repo_name: str) -> Generator[dict, None, None]:
    """Create a single test issue and return its details."""
    cleanup_test_issues(repo_name)
    title = "[E2E-TEST] Automated test issue 0"
    issue_number = create_issue(
        repo=repo_name,
        title=title,
        labels=["agent:e2e-test", "io-e2e-test-data"],
        body="This is automated test issue 0 for e2e testing.\n\nExpected: Agent completes quickly.",
    )
    issue_data = {
        "number": issue_number,
        "url": f"https://github.com/{repo_name}/issues/{issue_number}",
        "title": title,
    }
    yield issue_data
    try:
        _github_adapter(repo_name).add_comment(issue_number, "Closed by e2e test cleanup.")
        _github_adapter(repo_name).update_issue_state(issue_number, "closed")
    except Exception:
        pass


@pytest.fixture
def concurrent_test_run(repo_name: str, request) -> Generator[dict, None, None]:
    """Create multiple issues with a unique label for concurrent processing."""
    import uuid
    count = getattr(request, "param", 3)

    run_id = str(uuid.uuid4())[:8]
    run_label = f"{E2E_RUN_LABEL_PREFIX}{run_id}"

    issues = []
    for i in range(count):
        title = f"[E2E-TEST] Concurrent test issue {i}"
        issue_number = create_issue(
            repo=repo_name,
            title=title,
            labels=["agent:e2e-test", run_label],
            body=f"Test issue {i} for concurrent e2e run {run_id}.\n\nExpected: Agent completes quickly.",
        )
        issues.append({
            "number": issue_number,
            "url": f"https://github.com/{repo_name}/issues/{issue_number}",
            "title": title,
        })
        print(f"Created test issue #{issue_number} with label {run_label}")

    yield {"label": run_label, "issues": issues, "run_id": run_id}

    for issue in issues:
        try:
            _github_adapter(repo_name).add_comment(issue["number"], "Closed by e2e test cleanup.")
            _github_adapter(repo_name).update_issue_state(issue["number"], "closed")
        except Exception:
            pass

    try:
        _github_adapter(repo_name).delete_label(run_label)
    except Exception:
        pass


@pytest.fixture
def orchestrator_process(
    e2e_config: Config,
    e2e_project_root: Path,
) -> Generator[OrchestratorProcess, None, None]:
    """Create orchestrator process wrapper."""
    proc = OrchestratorProcess(e2e_config, e2e_project_root)
    yield proc
    if proc.is_running():
        proc.stop()
