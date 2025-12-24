"""E2E test fixtures for live testing.

These fixtures create real GitHub issues and run the orchestrator.
"""

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Generator, AsyncGenerator, Callable, TypeVar

import pytest

from issue_orchestrator.config import Config, AgentConfig
from issue_orchestrator.test_data import create_test_issues, cleanup_test_issues


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
def test_issues(repo_name: str) -> Generator[list[str], None, None]:
    """Create test issues, yield URLs, then cleanup.

    Creates issues with 'agent:e2e-test' and 'test-data' labels.
    Cleans up any stale test issues first to avoid interference.
    """
    # Clean up stale test issues before creating new ones
    cleanup_test_issues(repo_name)

    # Create just one test issue for e2e
    urls = create_test_issues(repo_name, ["agent:e2e-test"])

    yield urls

    # Cleanup: close all test issues
    cleanup_test_issues(repo_name)


def _ensure_test_labels(repo_name: str) -> None:
    """Create test labels if they don't exist."""
    subprocess.run(
        ["gh", "label", "create", "agent:e2e-test", "--repo", repo_name, "--force",
         "--description", "E2E test agent"],
        capture_output=True
    )
    subprocess.run(
        ["gh", "label", "create", "test-data", "--repo", repo_name, "--force",
         "--description", "Test data for e2e tests"],
        capture_output=True
    )


def _create_test_issue(repo_name: str, index: int = 0) -> dict:
    """Create a single test issue and return its details."""
    result = subprocess.run(
        ["gh", "issue", "create",
         "--repo", repo_name,
         "--title", f"[E2E-TEST] Automated test issue {index}",
         "--body", f"This is automated test issue {index} for e2e testing.\n\nExpected: Agent completes quickly.",
         "--label", "agent:e2e-test",
         "--label", "test-data"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to create test issue: {result.stderr}")

    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    return {
        "number": issue_number,
        "url": issue_url,
        "title": f"[E2E-TEST] Automated test issue {index}",
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
    _ensure_test_labels(repo_name)
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

    # Create the unique label
    subprocess.run(
        ["gh", "label", "create", run_label, "--repo", repo_name, "--force",
         "--description", f"E2E test run {run_id}"],
        capture_output=True
    )
    _ensure_test_labels(repo_name)

    issues = []
    for i in range(count):
        result = subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo_name,
             "--title", f"[E2E-TEST] Concurrent test issue {i}",
             "--body", f"Test issue {i} for concurrent e2e run {run_id}.\n\nExpected: Agent completes quickly.",
             "--label", "agent:e2e-test",
             "--label", run_label],  # Use unique run label instead of test-data
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create test issue: {result.stderr}")

        issue_url = result.stdout.strip()
        issue_number = int(issue_url.split("/")[-1])
        issues.append({
            "number": issue_number,
            "url": issue_url,
            "title": f"[E2E-TEST] Concurrent test issue {i}",
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
        cmd = [
            str(venv_bin), "start",
            "--label", "test-data",
            "--max-issues", str(max_issues),
            "--ui-mode", "tmux",
            "--no-dashboard",  # Don't start TUI in tests
        ]
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
