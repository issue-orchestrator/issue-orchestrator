"""E2E test fixtures for live testing.

These fixtures create real GitHub issues and run the orchestrator.
"""

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, AsyncGenerator, Callable, TypeVar

import pytest


# ---------------------------------------------------------------------------
# Timing Infrastructure
# ---------------------------------------------------------------------------

@dataclass
class TestTiming:
    """Track timing for a single test."""
    name: str
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start if self.end else time.time() - self.start


@dataclass
class E2ETimingStats:
    """Track timing for the entire e2e test session."""
    session_start: float = field(default_factory=time.time)
    test_timings: list[TestTiming] = field(default_factory=list)
    current_test: TestTiming | None = None

    def start_test(self, name: str) -> None:
        self.current_test = TestTiming(name=name, start=time.time())

    def end_test(self) -> TestTiming | None:
        if self.current_test:
            self.current_test.end = time.time()
            self.test_timings.append(self.current_test)
            result = self.current_test
            self.current_test = None
            return result
        return None

    @property
    def total_duration(self) -> float:
        return time.time() - self.session_start

    def print_summary(self) -> None:
        print("\n" + "=" * 70)
        print("E2E TEST TIMING SUMMARY")
        print("=" * 70)
        for t in self.test_timings:
            status = "✓" if t.duration < 120 else "⚠"  # Warn if > 2 min
            print(f"  {status} {t.name}: {t.duration:.1f}s")
        print("-" * 70)
        print(f"  TOTAL: {self.total_duration:.1f}s ({self.total_duration/60:.1f} min)")
        print(f"  Tests: {len(self.test_timings)}")
        if self.test_timings:
            avg = sum(t.duration for t in self.test_timings) / len(self.test_timings)
            print(f"  Average: {avg:.1f}s per test")
        print("=" * 70)


# Global timing tracker (session-scoped)
_timing_stats: E2ETimingStats | None = None


@pytest.fixture(scope="session")
def e2e_timing_stats() -> E2ETimingStats:
    """Session-scoped timing statistics."""
    global _timing_stats
    _timing_stats = E2ETimingStats()
    return _timing_stats


@pytest.fixture(autouse=True)
def track_test_timing(request, e2e_timing_stats):
    """Automatically track timing for each e2e test."""
    test_name = request.node.name
    e2e_timing_stats.start_test(test_name)
    print(f"\n⏱️  [{test_name}] Started at {time.strftime('%H:%M:%S')}")

    yield

    timing = e2e_timing_stats.end_test()
    if timing:
        print(f"⏱️  [{test_name}] Completed in {timing.duration:.1f}s")


def pytest_sessionfinish(session, exitstatus):
    """Print timing summary at end of test session."""
    global _timing_stats
    if _timing_stats and _timing_stats.test_timings:
        _timing_stats.print_summary()

from issue_orchestrator.config import Config, AgentConfig
from issue_orchestrator.domain.issue_key import IssueKey, GitHubIssueKey
from issue_orchestrator.test_data import (
    create_issue,
    create_test_issues,
    cleanup_test_issues,
    cleanup_issues_by_label,
    update_issue,
    close_issue,
)


T = TypeVar("T")


def wait_with_process_check(
    condition_fn: Callable[[], T | None],
    timeout: int,
    orchestrator: "OrchestratorProcess | None" = None,
    interval: int = 5,
    description: str = "condition",
) -> T | None:
    """Wait for a condition with optional orchestrator health checks.

    Args:
        condition_fn: Function that returns truthy value when condition is met, None otherwise
        timeout: Maximum time to wait in seconds
        orchestrator: If provided, fails fast if process crashes
        interval: Polling interval in seconds
        description: Description for error messages

    Returns:
        The truthy return value from condition_fn, or None on timeout

    Raises:
        RuntimeError: If orchestrator process crashes
    """
    start = time.time()
    while time.time() - start < timeout:
        # Fast failure detection: check if orchestrator crashed
        if orchestrator is not None and not orchestrator.is_running():
            stdout, stderr = orchestrator.stop()
            raise RuntimeError(
                f"Orchestrator process crashed while waiting for {description}.\n"
                f"stdout: {stdout[:1000] if stdout else '(empty)'}\n"
                f"stderr: {stderr[:1000] if stderr else '(empty)'}"
            )

        result = condition_fn()
        if result:
            return result
        time.sleep(interval)
    return None


def is_gh_authenticated() -> bool:
    """Check if gh CLI is authenticated."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit is exceeded."""
    pass


def check_github_rate_limit() -> dict:
    """Check GitHub API rate limit status.

    Returns:
        Dict with 'remaining', 'limit', 'reset_at' keys

    Raises:
        GitHubRateLimitError: If rate limit is exceeded
    """
    result = subprocess.run(
        ["gh", "api", "rate_limit"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # If we can't check rate limit, assume it's ok
        return {"remaining": -1, "limit": -1, "reset_at": "unknown"}

    try:
        data = json.loads(result.stdout)
        core = data.get("resources", {}).get("core", {})
        remaining = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_timestamp = core.get("reset", 0)

        # Convert unix timestamp to readable time
        import datetime
        reset_at = datetime.datetime.fromtimestamp(reset_timestamp).strftime("%H:%M:%S") if reset_timestamp else "unknown"

        if remaining == 0:
            raise GitHubRateLimitError(
                f"GitHub API rate limit EXCEEDED!\n"
                f"  Limit: {limit}\n"
                f"  Remaining: {remaining}\n"
                f"  Resets at: {reset_at}\n"
                f"  \n"
                f"  Wait for rate limit to reset or use a different token."
            )

        return {"remaining": remaining, "limit": limit, "reset_at": reset_at}
    except json.JSONDecodeError:
        return {"remaining": -1, "limit": -1, "reset_at": "unknown"}


def is_rate_limit_error(error_message: str) -> bool:
    """Check if an error message indicates a rate limit issue."""
    rate_limit_indicators = [
        "rate limit",
        "API rate limit",
        "rate_limit",
        "secondary rate limit",
        "abuse detection",
    ]
    error_lower = error_message.lower()
    return any(indicator.lower() in error_lower for indicator in rate_limit_indicators)


def is_claude_available() -> bool:
    """Check if claude CLI is available."""
    import shutil
    return shutil.which("claude") is not None


def get_repo_from_git() -> str:
    """Get repo owner/name from git remote."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent.parent,
    )
    if result.returncode != 0:
        return "test/repo"

    url = result.stdout.strip()
    # Parse git@github.com:owner/repo.git or https://github.com/owner/repo.git
    if url.startswith("git@"):
        # git@github.com:owner/repo.git
        parts = url.split(":")[-1]
    else:
        # https://github.com/owner/repo.git
        parts = "/".join(url.split("/")[-2:])

    return parts.replace(".git", "")


def get_test_repo() -> str:
    """Get the repo to use for e2e tests.

    Order of precedence:
    1. E2E_TEST_REPO environment variable (e.g., "myuser/my-test-repo")
    2. Current repo from git remote (for local development)

    For open-source contributors:
    - Fork the repo or create your own test repo
    - Set E2E_TEST_REPO=youruser/yourrepo
    - Run e2e tests against your repo
    """
    return os.environ.get("E2E_TEST_REPO", get_repo_from_git())


# Skip all e2e tests if prerequisites not met
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.skipif(
        not is_gh_authenticated(),
        reason="GitHub CLI not authenticated"
    ),
    pytest.mark.skipif(
        not is_claude_available(),
        reason="Claude CLI not available"
    ),
]


@pytest.fixture(scope="session", autouse=True)
def kill_stale_orchestrators():
    """Kill any stale orchestrator processes before running e2e tests."""
    import subprocess
    # Kill any orchestrator processes from previous interrupted runs
    subprocess.run(
        ["pkill", "-f", "issue-orchestrator.*start"],
        capture_output=True
    )
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_stale_prs_at_session_start():
    """Clean up stale PRs with review labels at session start.

    This runs once at the beginning of the e2e test session to ensure
    old PRs with needs-code-review or code-reviewed labels don't block
    new test runs by consuming orchestrator capacity.
    """
    repo = get_test_repo()
    labels_to_cleanup = ["test-data", "needs-code-review", "code-reviewed"]
    closed_prs = []

    print(f"\n[E2E SETUP] Checking for stale PRs in {repo}...")

    for label in labels_to_cleanup:
        result = subprocess.run(
            ["gh", "pr", "list",
             "--repo", repo,
             "--label", label,
             "--state", "open",
             "--json", "number,title,createdAt,headRefName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            for pr in prs:
                pr_num = pr["number"]
                if pr_num not in [p["number"] for p in closed_prs]:
                    print(f"[E2E SETUP] Closing stale PR #{pr_num}: {pr['title']}")
                    print(f"[E2E SETUP]   Branch: {pr['headRefName']}, Created: {pr['createdAt']}")
                    print(f"[E2E SETUP]   Matched label: {label}")
                    subprocess.run(
                        ["gh", "pr", "close", str(pr_num),
                         "--repo", repo,
                         "--delete-branch"],
                        capture_output=True
                    )
                    closed_prs.append(pr)

    if closed_prs:
        print(f"[E2E SETUP] Cleaned up {len(closed_prs)} stale PRs total")
    else:
        print("[E2E SETUP] No stale PRs found")

    yield


@pytest.fixture(scope="session")
def repo_name() -> str:
    """Get the repo name for e2e tests.

    Set E2E_TEST_REPO environment variable to use a different repo.
    Contributors should fork the repo or create their own test repo.
    """
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
def e2e_session_config(e2e_project_root: Path, e2e_session_tmp: Path, repo_name: str) -> Config:
    """Session-scoped config for single orchestrator."""
    from issue_orchestrator.config import ValidationConfig, ValidationGateConfig

    config = Config()
    config.repo = repo_name
    config.repo_root = e2e_project_root
    config.ui_mode = "tmux"
    config.max_concurrent_sessions = 4  # Allow more concurrency for saturation testing
    config.filter_label = "test-data"

    # Configure e2e-test agent
    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "prompts" / "e2e-test.md",
            worktree_base=e2e_session_tmp / "worktrees",
            timeout_minutes=3,
            model="sonnet",
        )
    }

    config.session_timeout_minutes = 3

    # Fast validation for e2e
    config.validation = ValidationConfig(
        agent_gate=ValidationGateConfig(cmd="make typecheck", timeout_seconds=120),
        publish_gate=ValidationGateConfig(cmd="make typecheck", timeout_seconds=120),
    )

    return config


@pytest.fixture(scope="session")
def e2e_issues(repo_name: str) -> Generator[dict[str, int], None, None]:
    """Create all e2e test issues once at session start.

    Returns a dict mapping scenario names to issue numbers.
    Tests reference issues by name for clarity.
    """
    # Clean up any stale issues first
    cleanup_test_issues(repo_name)

    issues = {
        "simple_task": create_issue(
            repo_name,
            "[E2E] Simple task",
            ["agent:e2e-test", "test-data"],
            body="A simple task for basic e2e testing.",
        ),
        "will_block": create_issue(
            repo_name,
            "[E2E] Task that blocks",
            ["agent:e2e-test", "test-data"],
            body="This task should end up blocked.",
        ),
    }

    print(f"\n[E2E SETUP] Created {len(issues)} test issues: {issues}")

    yield issues

    # Cleanup at session end
    print(f"\n[E2E TEARDOWN] Cleaning up test issues...")
    cleanup_test_issues(repo_name)


@pytest.fixture(scope="session")
def e2e_orchestrator(
    e2e_session_config: Config,
    e2e_project_root: Path,
    filter_label: str,
) -> Generator["OrchestratorProcess", None, None]:
    """Single orchestrator instance for all e2e tests.

    Starts once at session start, stops at session end.
    Tests create their own issues via test_issue_factory or inflight_create.
    """
    proc = OrchestratorProcess(e2e_session_config, e2e_project_root)
    proc.start(max_issues=5, extra_args=["--label", filter_label])

    # Wait for orchestrator to be ready
    time.sleep(2)

    if not proc.is_running():
        stdout, stderr = proc.stop()
        raise RuntimeError(
            f"Orchestrator failed to start.\nstdout: {stdout}\nstderr: {stderr}"
        )

    print(f"\n[E2E] Orchestrator running (pid={proc.process.pid})")

    yield proc

    print(f"\n[E2E TEARDOWN] Stopping orchestrator...")
    proc.stop()


def trigger_refresh(port: int = 8080, timeout: int = 5) -> bool:
    """Trigger orchestrator to refresh issues immediately.

    Returns True if refresh was requested successfully.
    """
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/api/refresh",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def inflight_create(
    repo: str,
    title: str,
    labels: list[str],
    body: str = "Created mid-test.",
    port: int = 8080,
) -> IssueKey:
    """Create an issue while orchestrator is running.

    Args:
        repo: GitHub repo in owner/repo format
        title: Issue title
        labels: Labels to apply
        body: Issue body
        port: Dashboard port for refresh API

    Returns:
        IssueKey for the created issue
    """
    issue_number = create_issue(repo, title, labels, body)
    trigger_refresh(port)
    return GitHubIssueKey(repo=repo, external_id=str(issue_number))


def inflight_update(
    issue: IssueKey,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    port: int = 8080,
) -> None:
    """Update an issue while orchestrator is running.

    Args:
        issue: The issue to update
        add_labels: Labels to add
        remove_labels: Labels to remove
        port: Dashboard port for refresh API
    """
    issue_number = int(issue.stable_id())
    update_issue(issue.scope(), issue_number, add_labels, remove_labels)
    trigger_refresh(port)


def inflight_close(
    issue: IssueKey,
    comment: str | None = None,
    port: int = 8080,
) -> None:
    """Close an issue while orchestrator is running.

    Args:
        issue: The issue to close
        comment: Optional comment when closing
        port: Dashboard port for refresh API
    """
    issue_number = int(issue.stable_id())
    close_issue(issue.scope(), issue_number, comment)
    trigger_refresh(port)


@pytest.fixture
def test_label(request) -> str:
    """Generate unique label from test name for isolation."""
    return f"e2e:{request.node.name}"


@pytest.fixture(scope="session")
def filter_label() -> str:
    """Configurable filter label for parallel test runs.

    Set E2E_FILTER env var to run parallel test sessions:
        E2E_FILTER=run-a pytest tests/e2e/
        E2E_FILTER=run-b pytest tests/e2e/  # parallel, no interference
    """
    return os.environ.get("E2E_FILTER", "test-data")


@pytest.fixture
def test_issue_factory(repo_name: str, test_label: str, filter_label: str):
    """Factory for creating test-scoped issues.

    Cleans up issues from previous failed runs of this test,
    then provides a factory to create fresh issues.
    """
    # Cleanup stale issues from this specific test
    cleanup_issues_by_label(repo_name, test_label)

    def create(title: str, extra_labels: list[str] | None = None) -> IssueKey:
        """Create an issue scoped to this test."""
        labels = [filter_label, "agent:e2e-test", test_label]
        if extra_labels:
            labels.extend(extra_labels)
        issue_num = create_issue(repo_name, title, labels)
        return GitHubIssueKey(repo=repo_name, external_id=str(issue_num))

    return create


@pytest.fixture
def e2e_config(e2e_project_root: Path, tmp_path: Path, repo_name: str) -> Config:
    """Create e2e test config with e2e-test agent."""
    from issue_orchestrator.config import ValidationConfig, ValidationGateConfig

    config = Config()
    config.repo = repo_name
    config.repo_root = e2e_project_root
    config.ui_mode = "tmux"
    config.max_concurrent_sessions = 1
    config.filter_label = "test-data"

    # Configure e2e-test agent
    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "prompts" / "e2e-test.md",
            worktree_base=tmp_path / "worktrees",
            timeout_minutes=3,
            model="sonnet",
        )
    }

    # Short timeouts for tests
    config.session_timeout_minutes = 3

    # Lightweight validation for e2e tests - typecheck only (not full test suite)
    # This prevents the 10+ minute publish_gate from blocking e2e tests
    config.validation = ValidationConfig(
        agent_gate=ValidationGateConfig(
            cmd="make typecheck",
            timeout_seconds=120,
        ),
        publish_gate=ValidationGateConfig(
            cmd="make typecheck",  # Fast - just typecheck, not full tests
            timeout_seconds=120,
        ),
    )

    return config


@pytest.fixture
def test_issues(repo_name: str) -> Generator[list[int], None, None]:
    """Create test issues, yield issue numbers, then cleanup.

    Creates issues with 'agent:e2e-test' and 'test-data' labels.
    Cleans up any stale test issues first to avoid interference.
    """
    # Clean up stale test issues before creating new ones
    cleanup_test_issues(repo_name)

    # Create test issues for e2e
    issue_numbers = create_test_issues(repo_name, ["agent:e2e-test"])

    yield issue_numbers

    # Cleanup: close all test issues
    cleanup_test_issues(repo_name)


def _create_test_issue(repo_name: str, index: int = 0) -> dict:
    """Create a single test issue and return its details.

    Delegates to the canonical create_issue() function.
    """
    title = f"[E2E-TEST] Automated test issue {index}"
    issue_number = create_issue(
        repo=repo_name,
        title=title,
        labels=["agent:e2e-test", "test-data"],
        body=f"This is automated test issue {index} for e2e testing.\n\nExpected: Agent completes quickly.",
    )
    return {
        "number": issue_number,
        "url": f"https://github.com/{repo_name}/issues/{issue_number}",
        "title": title,
    }


def _cleanup_test_issue(repo_name: str, issue_number: int) -> None:
    """Close a test issue."""
    subprocess.run(
        ["gh", "issue", "close", str(issue_number),
         "--repo", repo_name,
         "--comment", "Closed by e2e test cleanup."],
        capture_output=True
    )


@pytest.fixture
def single_test_issue(repo_name: str) -> Generator[dict, None, None]:
    """Create a single test issue and return its details.

    Cleans up any stale test issues first to avoid interference.
    """
    # Clean up stale test issues before creating new one
    cleanup_test_issues(repo_name)
    issue_data = _create_test_issue(repo_name, index=0)
    yield issue_data
    _cleanup_test_issue(repo_name, issue_data["number"])


@pytest.fixture
def concurrent_test_run(repo_name: str, request) -> Generator[dict, None, None]:
    """Create multiple issues with a unique label for concurrent processing.

    Use with: @pytest.mark.parametrize("concurrent_test_run", [3], indirect=True)
    to create 3 issues, or pass count via request.param.

    Returns dict with:
        - label: unique label for this test run
        - issues: list of issue dicts
    """
    import uuid
    count = getattr(request, "param", 3)  # Default to 3 issues

    # Unique label for this test run
    run_id = str(uuid.uuid4())[:8]
    run_label = f"e2e-run-{run_id}"

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

    yield {
        "label": run_label,
        "issues": issues,
        "run_id": run_id,
    }

    # Cleanup: close all issues and delete the label
    for issue in issues:
        _cleanup_test_issue(repo_name, issue["number"])

    subprocess.run(
        ["gh", "label", "delete", run_label, "--repo", repo_name, "--yes"],
        capture_output=True
    )


class OrchestratorProcess:
    """Wrapper for orchestrator subprocess with IPC support."""

    def __init__(self, config: Config, project_root: Path):
        self.config = config
        self.project_root = project_root
        self.process: subprocess.Popen | None = None
        self.ipc_socket_path: Path | None = None
        self._output_lines: list[str] = []
        self._log_thread: "threading.Thread | None" = None
        self._stop_logging = False

    def _log_reader(self) -> None:
        """Background thread to read and print orchestrator output."""
        import select
        import sys

        if self.process is None:
            return

        while not self._stop_logging and self.process.poll() is None:
            # Use select to check for available output
            if self.process.stderr:
                readable, _, _ = select.select([self.process.stderr], [], [], 0.5)
                if readable:
                    line = self.process.stderr.readline()
                    if line:
                        text = line.decode('utf-8', errors='replace').rstrip()
                        self._output_lines.append(text)
                        # Print orchestrator events with prefix
                        if any(kw in text for kw in ['[EVENT]', 'Session', 'Issue', 'PR', 'Review', 'launch', 'complet', 'start', 'ERROR', 'WARN']):
                            print(f"  [ORCH] {text}", file=sys.stderr, flush=True)

    def start(self, max_issues: int = 1, extra_args: list[str] | None = None) -> None:
        """Start the orchestrator process."""
        import sys
        import threading

        # Use sys.executable to find the venv's issue-orchestrator
        venv_bin = Path(sys.executable).parent / "issue-orchestrator"

        # Allow UI mode override via env var for interactive debugging
        ui_mode = os.environ.get("E2E_UI_MODE", "tmux")

        cmd = [
            str(venv_bin), "start",
            "--label", "test-data",
            "--max-issues", str(max_issues),
            "--ui-mode", ui_mode,
        ]

        # Add dashboard flags based on UI mode
        if ui_mode == "web":
            cmd.extend(["--port", os.environ.get("E2E_WEB_PORT", "8080")])
            print(f"  [E2E] Web UI available at http://localhost:{os.environ.get('E2E_WEB_PORT', '8080')}", flush=True)
        else:
            cmd.append("--no-dashboard")  # Don't start TUI in tests

        if extra_args:
            cmd.extend(extra_args)

        # Set up environment with fast publish_gate for e2e tests
        # agent_gate is already fast (just typecheck) so no override needed
        # publish_gate normally runs full test suite (10+ min) - override to fast echo
        # This makes validation actually RUN (creates records) but fast
        env = os.environ.copy()
        env["ORCHESTRATOR_PUBLISH_GATE_CMD"] = "echo 'e2e publish gate validation'"
        env["ORCHESTRATOR_PUBLISH_GATE_TIMEOUT"] = "30"
        # Enable verbose logging and ensure unbuffered output
        env["ORCHESTRATOR_LOG_LEVEL"] = "INFO"
        env["PYTHONUNBUFFERED"] = "1"
        # Enable event logging to stderr (works with --no-dashboard)
        env["ORCHESTRATOR_LOG_TO_STDERR"] = "1"

        print(f"  [E2E] Starting orchestrator: {' '.join(cmd)}", flush=True)

        self.process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Start background log reader
        self._stop_logging = False
        self._log_thread = threading.Thread(target=self._log_reader, daemon=True)
        self._log_thread.start()

        # Give it time to start
        time.sleep(3)
        print(f"  [E2E] Orchestrator started (pid={self.process.pid})", flush=True)

    def stop(self) -> tuple[str, str]:
        """Stop the orchestrator and return stdout/stderr."""
        if self.process is None:
            return "", ""

        print(f"  [E2E] Stopping orchestrator (pid={self.process.pid})...", flush=True)

        # Stop the log reader thread
        self._stop_logging = True

        # Send SIGTERM for graceful shutdown
        self.process.send_signal(signal.SIGTERM)

        try:
            stdout, stderr = self.process.communicate(timeout=5)
            self._cleanup_tmux_sessions()
            print(f"  [E2E] Orchestrator stopped gracefully", flush=True)
            return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
        except subprocess.TimeoutExpired:
            print(f"  [E2E] Sending second SIGTERM...", flush=True)
            # Send second SIGTERM to trigger force-kill of child sessions
            self.process.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = self.process.communicate(timeout=5)
                self._cleanup_tmux_sessions()
                print(f"  [E2E] Orchestrator stopped after second SIGTERM", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
            except subprocess.TimeoutExpired:
                # Last resort - kill the process
                print(f"  [E2E] Force killing orchestrator...", flush=True)
                self.process.kill()
                stdout, stderr = self.process.communicate()
                self._cleanup_tmux_sessions()
                print(f"  [E2E] Orchestrator killed", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""

    def _cleanup_tmux_sessions(self) -> None:
        """Clean up any tmux windows created by e2e tests.

        E2E test windows have names like '#123 [E2E-TEST]...'
        We kill these to prevent zombie accumulation.
        """
        try:
            # Get list of windows in orchestrator session
            result = subprocess.run(
                ["tmux", "list-windows", "-t", "orchestrator", "-F", "#{window_index}:#{window_name}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return  # No session or error

            # Kill windows that look like e2e test windows (contain E2E-TEST)
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if "E2E-TEST" in line or "E2E-" in line:
                    window_index = line.split(":")[0]
                    subprocess.run(
                        ["tmux", "kill-window", "-t", f"orchestrator:{window_index}"],
                        capture_output=True,
                    )
        except Exception:
            pass  # Best effort cleanup

    def is_running(self) -> bool:
        """Check if process is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None


@pytest.fixture
def orchestrator_process(e2e_config: Config, e2e_project_root: Path) -> Generator[OrchestratorProcess, None, None]:
    """Create orchestrator process wrapper."""
    proc = OrchestratorProcess(e2e_config, e2e_project_root)
    yield proc
    # Ensure cleanup
    if proc.is_running():
        proc.stop()


def wait_for_issue_label(
    repo: str,
    issue_number: int,
    label: str,
    timeout: int = 120,
    orchestrator: "OrchestratorProcess | None" = None,
) -> bool:
    """Wait for an issue to have a specific label.

    If orchestrator is provided, fails fast if the process crashes.
    """
    def check_label():
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number),
             "--repo", repo,
             "--json", "labels"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            labels = [l["name"] for l in data.get("labels", [])]
            if label in labels:
                return True
        return None

    return wait_with_process_check(
        check_label,
        timeout=timeout,
        orchestrator=orchestrator,
        description=f"label '{label}' on issue #{issue_number}",
    ) is not None


def wait_for_pr_created(
    repo: str,
    issue_number: int,
    timeout: int = 120,
    orchestrator: "OrchestratorProcess | None" = None,
) -> dict | None:
    """Wait for a PR to be created for an issue.

    If orchestrator is provided, fails fast if the process crashes.
    """
    def check_pr():
        # Search for PRs by head branch starting with issue number
        # (PRs don't have the test-data label, issues do)
        result = subprocess.run(
            ["gh", "pr", "list",
             "--repo", repo,
             "--json", "number,title,url,headRefName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            for pr in prs:
                # Check if PR branch starts with issue number (format: {issue_number}-{slug})
                head_ref = pr.get("headRefName", "")
                if head_ref.startswith(f"{issue_number}-"):
                    return pr
        return None

    return wait_with_process_check(
        check_pr,
        timeout=timeout,
        orchestrator=orchestrator,
        description=f"PR for issue #{issue_number}",
    )


def get_issue_comments(repo: str, issue_number: int) -> list[dict]:
    """Get comments on an issue."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo,
         "--json", "comments"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return data.get("comments", [])
    return []
