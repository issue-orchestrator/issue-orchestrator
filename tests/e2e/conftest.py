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
from typing import Generator, AsyncGenerator

import pytest

from issue_orchestrator.config import Config, AgentConfig
from issue_orchestrator.test_data import create_test_issues, cleanup_test_issues


def is_gh_authenticated() -> bool:
    """Check if gh CLI is authenticated."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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

    return config


@pytest.fixture
def test_issues(repo_name: str) -> Generator[list[str], None, None]:
    """Create test issues, yield URLs, then cleanup.

    Creates issues with 'agent:e2e-test' and 'test-data' labels.
    """
    # Create just one test issue for e2e
    urls = create_test_issues(repo_name, ["agent:e2e-test"])

    yield urls

    # Cleanup: close all test issues
    cleanup_test_issues(repo_name)


@pytest.fixture
def single_test_issue(repo_name: str) -> Generator[dict, None, None]:
    """Create a single test issue and return its details."""
    # Create labels if they don't exist
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

    # Create the issue (returns URL)
    result = subprocess.run(
        ["gh", "issue", "create",
         "--repo", repo_name,
         "--title", "[E2E-TEST] Automated test issue",
         "--body", "This is an automated test issue for e2e testing.\n\nExpected: Agent completes quickly.",
         "--label", "agent:e2e-test",
         "--label", "test-data"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        pytest.skip(f"Failed to create test issue: {result.stderr}")

    # Output is the issue URL like https://github.com/owner/repo/issues/123
    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    issue_data = {
        "number": issue_number,
        "url": issue_url,
        "title": "[E2E-TEST] Automated test issue",
    }

    yield issue_data

    # Cleanup: close the issue
    subprocess.run(
        ["gh", "issue", "close", str(issue_data["number"]),
         "--repo", repo_name,
         "--comment", "Closed by e2e test cleanup."],
        capture_output=True
    )


class OrchestratorProcess:
    """Wrapper for orchestrator subprocess with IPC support."""

    def __init__(self, config: Config, project_root: Path):
        self.config = config
        self.project_root = project_root
        self.process: subprocess.Popen | None = None
        self.ipc_socket_path: Path | None = None

    def start(self, max_issues: int = 1, extra_args: list[str] | None = None) -> None:
        """Start the orchestrator process."""
        cmd = [
            "issue-orchestrator", "start",
            "--label", "test-data",
            "--max-issues", str(max_issues),
            "--ui-mode", "tmux",
            "--no-dashboard",  # Don't start TUI in tests
        ]
        if extra_args:
            cmd.extend(extra_args)

        self.process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Give it time to start
        time.sleep(3)

    def stop(self) -> tuple[str, str]:
        """Stop the orchestrator and return stdout/stderr."""
        if self.process is None:
            return "", ""

        # Send SIGTERM for graceful shutdown
        self.process.send_signal(signal.SIGTERM)

        try:
            stdout, stderr = self.process.communicate(timeout=10)
            return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
        except subprocess.TimeoutExpired:
            self.process.kill()
            stdout, stderr = self.process.communicate()
            return stdout.decode() if stdout else "", stderr.decode() if stderr else ""

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


def wait_for_issue_label(repo: str, issue_number: int, label: str, timeout: int = 120) -> bool:
    """Wait for an issue to have a specific label."""
    start = time.time()
    while time.time() - start < timeout:
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
        time.sleep(5)
    return False


def wait_for_pr_created(repo: str, issue_number: int, timeout: int = 120) -> dict | None:
    """Wait for a PR to be created for an issue."""
    start = time.time()
    while time.time() - start < timeout:
        # Look for PRs mentioning the issue
        result = subprocess.run(
            ["gh", "pr", "list",
             "--repo", repo,
             "--label", "test-data",
             "--json", "number,title,url,headRefName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            for pr in prs:
                # Check if PR branch contains issue number
                if str(issue_number) in pr.get("headRefName", ""):
                    return pr
        time.sleep(5)
    return None


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
