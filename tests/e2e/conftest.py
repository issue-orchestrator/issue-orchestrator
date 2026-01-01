"""E2E test fixtures for live testing.

These fixtures create real GitHub issues and run the orchestrator.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
import threading
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, AsyncGenerator, Callable, TypeVar
import socket

import pytest

from issue_orchestrator.config import find_config_file
from issue_orchestrator.execution.github_http import GitHubHttpClient, GitHubHttpConfig, resolve_github_token
from issue_orchestrator.testing.asyncdsl import (
    OrchestratorWatcher,
    SSEEventStream,
    HTTPSnapshotProvider,
    HTTPReplayProvider,
    WatcherConfig,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pytest Configuration - Fail fast for e2e tests
# ---------------------------------------------------------------------------

def pytest_configure(config):
    """Configure pytest for e2e tests - fail fast by default."""
    # Only apply if we're in the e2e test directory
    if any("e2e" in str(arg) for arg in config.args):
        # Set maxfail=1 if not explicitly overridden
        if config.option.maxfail == 0:  # 0 means unlimited
            config.option.maxfail = 1
            logger.info("[E2E] Fail-fast enabled (maxfail=1)")


def _find_free_port() -> int:
    """Find an available localhost port for the control API."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_github_client_cache: dict[str, GitHubHttpClient] = {}


def _github_client(repo: str) -> GitHubHttpClient:
    client = _github_client_cache.get(repo)
    if client is None:
        token = resolve_github_token(configured_token=None)
        client = GitHubHttpClient(GitHubHttpConfig(repo=repo, token=token))
        _github_client_cache[repo] = client
    return client


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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default label for e2e test data - used to identify and clean up test artifacts
DEFAULT_E2E_FILTER_LABEL = "test-data"
E2E_TEST_LABEL_PREFIX = "e2e:"
_CLEANUP_CMD_TIMEOUT_SECONDS: int | None = None


def e2e_label(logical: str) -> str:
    """Apply the e2e label prefix to a logical test label."""
    if logical.startswith(E2E_TEST_LABEL_PREFIX):
        return logical
    return f"{E2E_TEST_LABEL_PREFIX}{logical}"


def _cleanup_command_timeout_seconds() -> int:
    global _CLEANUP_CMD_TIMEOUT_SECONDS
    if _CLEANUP_CMD_TIMEOUT_SECONDS is not None:
        return _CLEANUP_CMD_TIMEOUT_SECONDS

    default_timeout = 120
    timeout = default_timeout
    config_path = Path.cwd() / ".issue-orchestrator.e2e.yaml"
    if not config_path.exists():
        config_path = find_config_file(Path.cwd())
    if config_path and config_path.exists():
        try:
            with open(config_path) as handle:
                data = yaml.safe_load(handle) or {}
            e2e_config = data.get("e2e", {}) if isinstance(data, dict) else {}
            raw_timeout = e2e_config.get("cleanup_command_timeout_seconds", default_timeout)
            timeout = int(raw_timeout)
        except Exception:
            timeout = default_timeout

    if timeout < 1:
        timeout = 1
    _CLEANUP_CMD_TIMEOUT_SECONDS = timeout
    return timeout

# Persistent log directory - survives test cancellation
E2E_LOG_DIR = Path("/tmp/e2e-orchestrator-logs")
E2E_LOG_DIR.mkdir(exist_ok=True)
E2E_PROGRESS_LOG = E2E_LOG_DIR / "pytest-progress.log"
E2E_CURRENT_TEST = E2E_LOG_DIR / "pytest-current-test.txt"
E2E_SNAPSHOT_LOG = E2E_LOG_DIR / "pytest-abort-snapshot.log"


def _write_progress(event: str, nodeid: str = "", extra: str = "") -> None:
    """Persist pytest progress so aborted runs still have breadcrumbs."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} {event}"
    if nodeid:
        line += f" {nodeid}"
    if extra:
        line += f" {extra}"
    try:
        with E2E_PROGRESS_LOG.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _keep_artifacts() -> bool:
    """Return True if e2e cleanup should be skipped."""
    return os.environ.get("E2E_KEEP_ARTIFACTS") == "1"


def _env_token_name() -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return "GITHUB_TOKEN"
    if os.environ.get("GH_TOKEN"):
        return "GH_TOKEN"
    return None


def _keep_remote_artifacts() -> bool:
    """Return True if remote cleanup (PRs/branches/issues) should be skipped."""
    return os.environ.get("E2E_KEEP_REMOTE_ARTIFACTS") == "1"


def _tail_lines(path: Path, limit: int = 200) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-limit:]


def _find_recent_worktrees(limit: int = 3) -> list[Path]:
    """Find recent e2e worktrees for snapshotting."""
    candidates: list[Path] = []
    tmp_root = Path("/tmp/e2e-worktrees")
    if tmp_root.exists():
        candidates.extend([p for p in tmp_root.iterdir() if p.is_dir()])

    # pytest tmp worktrees live under /private/var/folders/*/*/T/pytest-of-*/.../worktrees/*
    tmp_parent = Path("/private/var/folders")
    if tmp_parent.exists():
        for pytest_dir in tmp_parent.glob("*/*/T/pytest-of-*"):
            for worktree_root in pytest_dir.glob("**/worktrees"):
                try:
                    for worktree in worktree_root.iterdir():
                        if worktree.is_dir():
                            candidates.append(worktree)
                except OSError:
                    continue

    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[:limit]


def _claude_project_dir_for(worktree: Path) -> Path:
    escaped = "-" + str(worktree).lstrip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / escaped


def _snapshot_logs(reason: str) -> None:
    """Persist tail snapshots of the latest logs for aborted/failed sessions."""
    try:
        latest_orch = max(E2E_LOG_DIR.glob("orchestrator-*.log"), key=lambda p: p.stat().st_mtime, default=None)
        latest_e2e = max(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        latest_orch = None
        latest_e2e = None

    try:
        with E2E_SNAPSHOT_LOG.open("a") as handle:
            handle.write("=" * 60 + "\n")
            handle.write(f"SNAPSHOT reason={reason}\n")
            if latest_e2e:
                handle.write(f"[E2E] {latest_e2e}\n")
                for line in _tail_lines(latest_e2e):
                    handle.write(line + "\n")
            if latest_orch:
                handle.write(f"[ORCH] {latest_orch}\n")
                for line in _tail_lines(latest_orch):
                    handle.write(line + "\n")
            # Tmux context can explain stuck sessions
            try:
                handle.write("[TMUX] list-windows\n")
                tmux_list = subprocess.run(
                    ["tmux", "list-windows", "-t", "orchestrator"],
                    capture_output=True,
                    text=True,
                )
                if tmux_list.returncode == 0:
                    handle.write(tmux_list.stdout.strip() + "\n")
                else:
                    handle.write(f"(tmux list-windows failed: {tmux_list.stderr.strip()})\n")
            except OSError:
                handle.write("(tmux not available)\n")

            # Capture recent pane output for each window to aid debugging
            try:
                tmux_windows = []
                if tmux_list.returncode == 0:
                    for line in tmux_list.stdout.splitlines():
                        window_id = line.split(":")[0].strip()
                        if window_id:
                            tmux_windows.append(window_id)
                for window_id in tmux_windows:
                    handle.write(f"[TMUX] capture-pane window={window_id}\n")
                    cap = subprocess.run(
                        ["tmux", "capture-pane", "-t", f"orchestrator:{window_id}", "-p", "-S", "-200"],
                        capture_output=True,
                        text=True,
                    )
                    if cap.returncode == 0:
                        handle.write(cap.stdout.strip() + "\n")
                    else:
                        handle.write(f"(capture-pane failed: {cap.stderr.strip()})\n")
            except OSError:
                handle.write("(tmux capture-pane not available)\n")

            # Snapshot recent worktree artifacts
            for worktree in _find_recent_worktrees():
                handle.write(f"[WORKTREE] {worktree}\n")
                session_dir = worktree / ".issue-orchestrator"
                if session_dir.exists():
                    for identity in session_dir.glob("session-identity-*.json"):
                        handle.write(f"[IDENTITY] {identity}\n")
                        for line in _tail_lines(identity):
                            handle.write(line + "\n")
                    for completion in session_dir.glob("completion-*.json"):
                        handle.write(f"[COMPLETION] {completion}\n")
                        for line in _tail_lines(completion):
                            handle.write(line + "\n")
                    session_log = session_dir / "session.log"
                    if session_log.exists():
                        handle.write(f"[SESSION_LOG] {session_log}\n")
                        for line in _tail_lines(session_log):
                            handle.write(line + "\n")

                claude_dir = _claude_project_dir_for(worktree)
                handle.write(f"[CLAUDE] {claude_dir}\n")
                if claude_dir.exists():
                    jsonl_files = sorted(claude_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if jsonl_files:
                        recent = jsonl_files[0]
                        handle.write(f"[CLAUDE_JSONL] {recent}\n")
                        for line in _tail_lines(recent):
                            handle.write(line + "\n")
    except OSError:
        pass


def pytest_sessionstart(session):
    _write_progress("SESSION_START")


def pytest_sessionfinish(session, exitstatus):
    _write_progress("SESSION_FINISH", extra=f"exitstatus={exitstatus}")
    if exitstatus != 0:
        _snapshot_logs(f"exitstatus={exitstatus}")


def pytest_runtest_logstart(nodeid, location):
    try:
        E2E_CURRENT_TEST.write_text(nodeid)
    except OSError:
        pass
    _write_progress("TEST_START", nodeid=nodeid)


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    status = "PASSED"
    if report.failed:
        status = "FAILED"
    elif report.skipped:
        status = "SKIPPED"
    _write_progress("TEST_END", nodeid=report.nodeid, extra=f"status={status}")


# ---------------------------------------------------------------------------
# Timing Infrastructure
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@dataclass
class Phase:
    """Track timing for a phase within a test."""
    name: str
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start if self.end else time.time() - self.start


@dataclass
class TestTiming:
    """Track timing for a single test, including phases."""
    name: str
    start: float = 0.0
    end: float = 0.0
    phases: list[Phase] = field(default_factory=list)
    status: str = "running"  # running, passed, failed

    @property
    def duration(self) -> float:
        return self.end - self.start if self.end else time.time() - self.start

    def start_phase(self, name: str) -> Phase:
        phase = Phase(name=name, start=time.time())
        self.phases.append(phase)
        return phase

    def end_phase(self, phase: Phase) -> None:
        phase.end = time.time()

    def print_phases(self, indent: str = "    ") -> None:
        """Print phase breakdown for this test."""
        if not self.phases:
            return
        for p in self.phases:
            pct = (p.duration / self.duration * 100) if self.duration > 0 else 0
            print(f"{indent}├─ {p.name}: {p.duration:.1f}s ({pct:.0f}%)")


@dataclass
class E2ETimingStats:
    """Track timing for the entire e2e test session."""
    session_start: float = field(default_factory=time.time)
    test_timings: list[TestTiming] = field(default_factory=list)
    current_test: TestTiming | None = None
    detailed: bool = True  # Always show phase breakdown by default

    def start_test(self, name: str) -> None:
        self.current_test = TestTiming(name=name, start=time.time())

    def end_test(self, status: str = "passed") -> TestTiming | None:
        if self.current_test:
            self.current_test.end = time.time()
            self.current_test.status = status
            self.test_timings.append(self.current_test)
            result = self.current_test
            self.current_test = None
            return result
        return None

    @contextmanager
    def phase(self, name: str):
        """Context manager to track a phase within the current test.

        Usage:
            with e2e_timing_stats.phase("Creating issue"):
                issue = create_issue(...)
        """
        if self.current_test:
            p = self.current_test.start_phase(name)
            try:
                yield
            finally:
                self.current_test.end_phase(p)
        else:
            yield  # No-op if not in a test

    @property
    def total_duration(self) -> float:
        return time.time() - self.session_start

    def print_summary(self, detailed: bool | None = None) -> None:
        """Print timing summary. Use detailed=True for phase breakdown."""
        show_detailed = detailed if detailed is not None else self.detailed
        print("\n" + "=" * 70)
        print("E2E TEST TIMING SUMMARY")
        print("=" * 70)
        for t in self.test_timings:
            if t.status == "passed":
                status = "✓" if t.duration < 120 else "⚠"
            else:
                status = "✗"
            print(f"  {status} {t.name}: {t.duration:.1f}s [{t.status}]")
            if show_detailed and t.phases:
                t.print_phases()
        print("-" * 70)
        passed = sum(1 for t in self.test_timings if t.status == "passed")
        failed = sum(1 for t in self.test_timings if t.status == "failed")
        print(f"  TOTAL: {self.total_duration:.1f}s ({self.total_duration/60:.1f} min)")
        print(f"  Tests: {len(self.test_timings)} ({passed} passed, {failed} failed)")
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

    # Determine pass/fail status
    status = "passed"
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        status = "failed"

    timing = e2e_timing_stats.end_test(status)
    if timing:
        print(f"⏱️  [{test_name}] Completed in {timing.duration:.1f}s [{status}]")
        if timing.phases:
            timing.print_phases(indent="    ")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test result on the node for access in fixtures."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def pytest_sessionfinish(session, exitstatus):
    """Print timing summary at end of test session."""
    global _timing_stats
    if _timing_stats and _timing_stats.test_timings:
        _timing_stats.print_summary()
    # Always print log directory for debugging (even on interrupt)
    print(f"\n  [E2E] Log files directory: {E2E_LOG_DIR}", flush=True)
    log_files = sorted(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if log_files:
        print(f"  [E2E] Latest log: {log_files[0]}", flush=True)

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


# Error patterns that indicate immediate failure (no point waiting)
FATAL_ERROR_PATTERNS = [
    "Traceback (most recent call last):",
    "FATAL:",
    "panic:",
    "Can't trigger event",  # State machine errors
    "RecursionError:",
    "MemoryError:",
    "session.failed",
    "session.start_failed",
    "ended without completion",
    "Terminated without completion record",
    "Timed out without completion record",
    "No completion record found",
]


def wait_with_process_check(
    condition_fn: Callable[[], T | None],
    timeout: int,
    orchestrator: "OrchestratorProcess | None" = None,
    interval: int = 2,  # Reduced from 5s for faster feedback
    description: str = "condition",
    show_progress: bool = True,
) -> T | None:
    """Wait for a condition with orchestrator health checks and progress.

    Args:
        condition_fn: Function that returns truthy value when condition is met, None otherwise
        timeout: Maximum time to wait in seconds
        orchestrator: If provided, fails fast if process crashes or logs errors
        interval: Polling interval in seconds
        description: Description for error messages and progress
        show_progress: If True, print progress every 10 seconds

    Returns:
        The truthy return value from condition_fn, or None on timeout

    Raises:
        RuntimeError: If orchestrator process crashes or logs fatal errors
    """
    start = time.time()
    last_progress = start
    last_snapshot = start
    poll_count = 0

    while time.time() - start < timeout:
        elapsed = time.time() - start
        poll_count += 1

        # Fast failure detection: check if orchestrator crashed
        if orchestrator is not None:
            if not orchestrator.is_running():
                stdout, stderr = orchestrator.stop()
                raise RuntimeError(
                    f"Orchestrator crashed while waiting for {description}.\n"
                    f"Log file: {orchestrator.log_path}\n"
                    f"stdout tail: {stdout[-1000:] if stdout else '(empty)'}\n"
                    f"stderr tail: {stderr[-1000:] if stderr else '(empty)'}"
                )

            # Check for fatal errors in recent log output
            recent_logs = "\n".join(orchestrator._output_lines[-20:])
            for pattern in FATAL_ERROR_PATTERNS:
                if pattern in recent_logs:
                    raise RuntimeError(
                        f"Fatal error detected while waiting for {description}:\n"
                        f"Pattern: {pattern}\n"
                        f"Log file: {orchestrator.log_path}\n"
                        f"Recent output:\n{recent_logs[-500:]}"
                    )
            if orchestrator.last_log_age_seconds() > 20 and (time.time() - last_snapshot) > 30:
                _snapshot_logs(f"stall={description}")
                last_snapshot = time.time()

        result = condition_fn()
        if result:
            return result

        # Show progress every 10 seconds
        if show_progress and time.time() - last_progress >= 10:
            remaining = timeout - elapsed
            print(f"    ... waiting for {description} ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)", flush=True)
            last_progress = time.time()

        time.sleep(interval)

    # Timeout - provide helpful debug info
    if orchestrator and orchestrator.log_path:
        print(f"    [TIMEOUT] {description} after {timeout}s. Check: {orchestrator.log_path}", flush=True)
    return None


def is_gh_authenticated() -> bool:
    """Check if GitHub auth is available."""
    try:
        resolve_github_token(configured_token=None)
        return True
    except Exception:
        return False


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
    try:
        data = _github_client(get_test_repo()).get_rate_limit_snapshot()
        if data is None:
            return {"remaining": -1, "limit": -1, "reset_at": "unknown"}
        core = data.to_payload().get("core", {})
        remaining = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_timestamp = core.get("reset", 0)

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
    except Exception:
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


def is_github_connection_error(error_message: str) -> bool:
    """Check if an error message indicates a GitHub connectivity problem."""
    indicators = [
        "error connecting to api.github.com",
        "could not resolve host",
        "network is unreachable",
        "connection timed out",
        "connect: connection refused",
    ]
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in indicators)


def is_claude_available() -> bool:
    """Check if claude CLI is available."""
    import shutil
    return shutil.which("claude") is not None


def is_github_reachable() -> bool:
    """Check that GitHub API is reachable for live e2e tests."""
    try:
        snapshot = _github_client(get_test_repo()).get_rate_limit_snapshot()
        return snapshot is not None
    except Exception:
        return False


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
        not is_github_reachable(),
        reason="GitHub API not reachable"
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
        capture_output=True,
    )

    # Fallback: explicitly scan and terminate matching processes
    try:
        ps_result = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
        )
    except PermissionError as exc:
        logger.info("[E2E CLEANUP] Skipping ps scan (permission denied): %s", exc)
        ps_result = None

    if ps_result and ps_result.returncode == 0:
        for line in ps_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_str, cmd = parts
            if "issue-orchestrator" in cmd and " start " in f" {cmd} ":
                try:
                    subprocess.run(["kill", pid_str], capture_output=True)
                    logger.info("[E2E CLEANUP] Killed stale orchestrator pid=%s", pid_str)
                except Exception:
                    pass
    yield


def _cleanup_local_worktrees():
    """Clean up local e2e worktrees."""
    worktree_base = Path("/tmp/e2e-worktrees")
    if worktree_base.exists():
        import shutil
        count = 0
        for item in worktree_base.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                    count += 1
                except Exception as e:
                    logger.warning("Failed to remove worktree %s: %s", item, e)
        if count > 0:
            logger.info("[E2E CLEANUP] Removed %d local worktrees", count)
    return 0


def _cleanup_tmux_sessions():
    """Clean up tmux sessions from previous e2e runs."""
    # Kill orchestrator tmux session if it exists
    result = subprocess.run(
        ["tmux", "kill-session", "-t", "orchestrator"],
        capture_output=True
    )
    if result.returncode == 0:
        logger.info("[E2E CLEANUP] Killed stale orchestrator tmux session")


def _run_cleanup_step(name: str, fn, timeout_s: int) -> int:
    """Run a cleanup step with a hard wall-clock timeout."""
    start = time.monotonic()
    result: dict[str, int] = {}

    def _runner() -> None:
        try:
            result["value"] = int(fn())
        except Exception:
            result["value"] = 0

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        logger.warning("[E2E CLEANUP] %s timed out after %ds; skipping", name, timeout_s)
        return 0
    elapsed = time.monotonic() - start
    logger.info("[E2E CLEANUP] %s completed in %.1fs", name, elapsed)
    return result.get("value", 0)


def _verify_cleanup_items(
    name: str,
    items: list,
    check_fn,
    retries: int = 1,
    retry_delay_s: float = 2.0,
) -> int:
    """Verify cleanup items, retrying once to allow eventual consistency."""
    remaining = list(items)
    for attempt in range(retries + 1):
        if not remaining:
            return 0
        still = []
        for item in remaining:
            if check_fn(item):
                continue
            still.append(item)
        if not still:
            return 0
        remaining = still
        if attempt < retries:
            logger.info(
                "[E2E CLEANUP] %s verify pending=%d; retrying in %.1fs",
                name,
                len(remaining),
                retry_delay_s,
            )
            time.sleep(retry_delay_s)
    logger.warning("[E2E CLEANUP] %s verify incomplete; remaining=%d", name, len(remaining))
    return len(remaining)


def _cleanup_remote_branches(repo: str) -> int:
    """Clean up remote branches matching e2e patterns (orphaned from crashed runs)."""
    # Patterns for e2e test branches: numeric prefix (issue number) + e2e keywords
    e2e_patterns = ["e2e-", "-e2e-", "-test-"]
    branches_deleted = 0
    branches_attempted: list[str] = []
    deadline = time.monotonic() + 30

    # 1. Delete branches for ALL e2e PRs (including closed) - prevents "PR already exists" error
    # GitHub keeps closed PRs and won't let you create a new PR for the same branch
    try:
        prs = _github_client(repo).list_prs(state="all", limit=100)
        for pr in prs:
            branch = (pr.get("head") or {}).get("ref", "")
            if any(pattern in branch.lower() for pattern in e2e_patterns):
                if time.monotonic() > deadline:
                    logger.warning("[E2E CLEANUP] Branch cleanup time budget exceeded; stopping early")
                    return branches_deleted
                try:
                    _github_client(repo).delete_branch(branch)
                    branches_attempted.append(branch)
                    logger.info("[E2E CLEANUP] Deleted branch for PR #%d: %s", pr.get("number"), branch)
                    branches_deleted += 1
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed deleting branch for PR #%s: %s", pr.get("number"), branch)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list PRs for branch cleanup: %s", exc)

    # 2. Also check for orphan branches not associated with any PR
    try:
        for branch in _github_client(repo).list_branches():
            if any(pattern in branch.lower() for pattern in e2e_patterns):
                logger.info("[E2E CLEANUP] Deleting orphan branch: %s", branch)
                if time.monotonic() > deadline:
                    logger.warning("[E2E CLEANUP] Branch cleanup time budget exceeded; stopping early")
                    return branches_deleted
                try:
                    _github_client(repo).delete_branch(branch)
                    branches_attempted.append(branch)
                    branches_deleted += 1
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed deleting orphan branch: %s", branch)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list remote branches: %s", exc)

    def _branch_gone(branch: str) -> bool:
        try:
            return not _github_client(repo).branch_exists(branch)
        except Exception:
            return True

    _verify_cleanup_items(
        "Branch cleanup",
        branches_attempted,
        _branch_gone,
        retries=1,
        retry_delay_s=3.0,
    )
    return branches_deleted


def _cleanup_prs(repo: str) -> int:
    """Clean up PRs with test labels or e2e branch patterns.

    Also handles closed PRs whose branches weren't deleted.
    """
    labels_to_cleanup = [DEFAULT_E2E_FILTER_LABEL, "needs-code-review", "code-reviewed"]
    e2e_branch_patterns = ["e2e-", "-test-", "-concurrent-"]
    closed_prs: list[dict] = []
    closed_pr_nums: set[int] = set()
    branches_attempted: list[str] = []

    # First, clean up OPEN PRs with specific labels
    for label in labels_to_cleanup:
        try:
            items = _github_client(repo).get_prs_with_label(label, state="open")
            for item in items:
                pr_num = item.get("number")
                if not pr_num or pr_num in closed_pr_nums:
                    continue
                pr = _github_client(repo).get_pr(pr_num) or {}
                title = pr.get("title", "")
                logger.info("[E2E CLEANUP] Closing PR #%d: %s (label: %s)", pr_num, title, label)
                _github_client(repo).close_pr(pr_num)
                branch = (pr.get("head") or {}).get("ref", "")
                if branch:
                    try:
                        _github_client(repo).delete_branch(branch)
                    except Exception:
                        pass
                    branches_attempted.append(branch)
                closed_prs.append(pr)
                closed_pr_nums.add(pr_num)
        except Exception as exc:
            logger.warning("[E2E CLEANUP] Failed listing PRs for label '%s': %s", label, exc)

    # Second, clean up OPEN PRs with e2e branch patterns
    try:
        prs = _github_client(repo).list_prs(state="open", limit=100)
        for pr in prs:
            pr_num = pr.get("number")
            branch = (pr.get("head") or {}).get("ref", "")
            if not pr_num or pr_num in closed_pr_nums:
                continue
            if any(pattern in branch.lower() for pattern in e2e_branch_patterns):
                logger.info("[E2E CLEANUP] Closing PR #%d: %s (branch pattern)", pr_num, pr.get("title", ""))
                _github_client(repo).close_pr(pr_num)
                if branch:
                    try:
                        _github_client(repo).delete_branch(branch)
                    except Exception:
                        pass
                    branches_attempted.append(branch)
                closed_prs.append(pr)
                closed_pr_nums.add(pr_num)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list open PRs: %s", exc)

    # Third, clean up branches from CLOSED/MERGED PRs that match e2e patterns
    # This handles cases where PRs were closed but branches weren't deleted
    try:
        prs = _github_client(repo).list_prs(state="all", limit=100)
        for pr in prs:
            if str(pr.get("state", "")).lower() in ("closed", "merged"):
                branch = (pr.get("head") or {}).get("ref", "")
                if any(pattern in branch.lower() for pattern in e2e_branch_patterns):
                    try:
                        _github_client(repo).delete_branch(branch)
                        branches_attempted.append(branch)
                        logger.info(
                            "[E2E CLEANUP] Deleted orphan branch: %s (from closed PR #%d)",
                            branch,
                            pr.get("number"),
                        )
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list PRs for branch cleanup: %s", exc)

    pr_numbers = list(closed_pr_nums)

    def _pr_closed(pr_number: int) -> bool:
        try:
            pr = _github_client(repo).get_pr(pr_number)
            if not pr:
                return True
            state = str(pr.get("state", "")).upper()
            return state in {"CLOSED", "MERGED"}
        except Exception:
            return True

    def _branch_gone(branch: str) -> bool:
        try:
            return not _github_client(repo).branch_exists(branch)
        except Exception:
            return True

    _verify_cleanup_items(
        "PR cleanup",
        pr_numbers,
        _pr_closed,
        retries=1,
        retry_delay_s=3.0,
    )
    _verify_cleanup_items(
        "PR branch cleanup",
        branches_attempted,
        _branch_gone,
        retries=1,
        retry_delay_s=3.0,
    )

    return len(closed_prs)


def _ensure_pr_label(repo: str, label: str) -> None:
    """Ensure a PR label exists (noop if already created)."""
    try:
        _github_client(repo).create_label(label, force=True)
    except Exception:
        logger.warning("[E2E CLEANUP] Failed ensuring label: %s", label)


def _ensure_required_pr_labels(repo: str) -> None:
    """Ensure required PR labels exist for e2e workflows."""
    labels = [
        "needs-code-review",
        "code-reviewed",
        "needs-rework",
        "rework-cycle-1",
        "rework-cycle-2",
        "triage-reviewed",
        "agent:triage-investigator",
        "agent:script-review",
        "agent:script-completes",
        "agent:e2e-test",
    ]
    for label in labels:
        _ensure_pr_label(repo, label)


def _cleanup_issues(repo: str) -> int:
    """Close test issues with test-data label."""
    try:
        issues = _github_client(repo).list_issues(labels=[DEFAULT_E2E_FILTER_LABEL], state="open", limit=100)
    except Exception:
        return 0
    closed_issues: list[int] = []
    for issue in issues:
        logger.info("[E2E CLEANUP] Closing issue #%d: %s", issue['number'], issue.get('title', ''))
        try:
            _github_client(repo).update_issue_state(issue["number"], "closed")
            closed_issues.append(issue["number"])
        except Exception:
            logger.warning("[E2E CLEANUP] Timeout closing issue #%d", issue["number"])

    def _issue_closed(issue_number: int) -> bool:
        try:
            issue = _github_client(repo).get_issue(issue_number)
            if not issue:
                return True
            return str(issue.get("state", "")).upper() == "CLOSED"
        except Exception:
            return True

    _verify_cleanup_items(
        "Issue cleanup",
        closed_issues,
        _issue_closed,
        retries=1,
        retry_delay_s=3.0,
    )

    return len(issues)


@pytest.fixture(scope="session", autouse=True)
def e2e_reconciliation_at_session_start():
    """Comprehensive e2e test reconciliation - clean slate before running tests.

    This runs once at the beginning of the e2e test session and cleans up ALL
    artifacts from previous (possibly crashed) test runs:

    1. Local worktrees in /tmp/e2e-worktrees/
    2. Stale tmux sessions
    3. Remote branches matching e2e patterns (orphaned from crashes)
    4. Open PRs with test labels or e2e branch patterns
    5. Open issues with test-data label

    This ensures deterministic test runs regardless of previous state.
    """
    repo = get_test_repo()
    logger.info("=" * 60)
    logger.info("[E2E RECONCILIATION] Cleaning up artifacts from previous runs...")
    logger.info("=" * 60)

    if _keep_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping local cleanup (E2E_KEEP_ARTIFACTS=1)")
    else:
        # 1. Local cleanup
        _cleanup_local_worktrees()
        _cleanup_tmux_sessions()

    if _keep_remote_artifacts():
        logger.info("[E2E RECONCILIATION] Skipping remote cleanup (E2E_KEEP_REMOTE_ARTIFACTS=1)")
        prs_closed = 0
        branches_deleted = 0
        issues_closed = 0
        _ensure_required_pr_labels(repo)
    else:
    # 2. Remote cleanup (order matters: PRs first, then orphan branches)
        prs_closed = _run_cleanup_step("PR cleanup", lambda: _cleanup_prs(repo), timeout_s=120)
        branches_deleted = _run_cleanup_step("Branch cleanup", lambda: _cleanup_remote_branches(repo), timeout_s=120)
        issues_closed = _run_cleanup_step("Issue cleanup", lambda: _cleanup_issues(repo), timeout_s=120)
        _ensure_required_pr_labels(repo)

    # Summary
    logger.info("=" * 60)
    logger.info("[E2E RECONCILIATION] Summary: PRs=%d, Branches=%d, Issues=%d",
                prs_closed, branches_deleted, issues_closed)
    logger.info("=" * 60)
    try:
        cache_path.write_text(json.dumps({"last_run": now}))
    except Exception:
        pass

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
    config.github_token_env = _env_token_name()
    config.queue_refresh_seconds = 600
    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    config.control_api_port = int(env_port) if env_port else _find_free_port()
    config.e2e_pr_labels = ["test-data"]
    config.code_review_agent = "agent:script-review"
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.max_rework_cycles = 2
    config.triage_review_agent = None
    config.triage_review_threshold = 2
    config.gh_audit_enabled = True
    config.gh_audit_events = True
    config.gh_audit_file = str(E2E_LOG_DIR / "gh-audit-{pid}.json")

    # Configure e2e-test agent (scripted for deterministic e2e)
    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "scripts" / "complete-immediately.sh",
            worktree_base=e2e_session_tmp / "worktrees",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            permission_mode="bypassPermissions",
        )
        ,
        "agent:script-completes": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "scripts" / "complete-immediately.sh",
            worktree_base=e2e_session_tmp / "worktrees",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            permission_mode="bypassPermissions",
        ),
        "agent:script-review": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "scripts" / "review-decider.sh",
            worktree_base=e2e_session_tmp / "worktrees",
            timeout_minutes=3,
            model="sonnet",
            command="PR_NUMBER={pr_number} bash {prompt}",
            meta_agent="claude-code",
            permission_mode="bypassPermissions",
        ),
        "agent:triage-investigator": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "scripts" / "complete-immediately.sh",
            worktree_base=e2e_session_tmp / "worktrees",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
            permission_mode="bypassPermissions",
        ),
    }

    config.session_timeout_minutes = 3

    # Default review timeout: code agent + review agent timeouts (seconds)
    from tests.e2e.flows import review_timeout_from_config
    os.environ["E2E_REVIEW_TIMEOUT_S"] = str(review_timeout_from_config(config))

    # Fast validation for e2e
    config.validation = ValidationConfig(
        agent_gate=ValidationGateConfig(cmd="make typecheck", timeout_seconds=120),
        publish_gate=ValidationGateConfig(cmd="make typecheck", timeout_seconds=120),
    )
    if _keep_artifacts():
        config.cleanup.with_triage.close_ai_session_tabs = False
        config.cleanup.with_triage.remove_worktrees = False
        config.cleanup.without_triage.close_ai_session_tabs = False
        config.cleanup.without_triage.remove_worktrees = False
    os.environ["E2E_CONTROL_API_PORT"] = str(config.control_api_port)

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
    max_issues = int(os.environ.get("E2E_MAX_ISSUES", "50"))
    proc.start(max_issues=max_issues, extra_args=["--label", filter_label])

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


@pytest.fixture
async def orchestrator_watcher(
    e2e_orchestrator: "OrchestratorProcess",
) -> AsyncGenerator[OrchestratorWatcher, None]:
    """Async watcher wired to the control API SSE stream."""
    from issue_orchestrator.config import Config

    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    port = int(env_port) if env_port is not None else Config().control_api_port
    if port <= 0:
        pytest.skip("Control API disabled; async watcher unavailable")

    stream = SSEEventStream(f"http://localhost:{port}/api/events")
    await stream.start()
    snapshot_provider = HTTPSnapshotProvider(f"http://localhost:{port}/api/snapshot")
    replay_provider = HTTPReplayProvider(f"http://localhost:{port}/api/events_since")
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


def trigger_refresh(port: int | None = None, timeout: int = 5) -> bool:
    """Trigger orchestrator to refresh issues immediately via control API.

    Args:
        port: Control API port (defaults to Config.control_api_port)
        timeout: Request timeout in seconds

    Returns True if refresh was requested successfully.
    """
    import urllib.request
    import urllib.error
    import time as _time
    from issue_orchestrator.config import Config

    # Retry a few times with backoff - the control API might still be starting
    max_retries = 5
    if port is None:
        env_port = os.environ.get("E2E_CONTROL_API_PORT")
        port = int(env_port) if env_port is not None else Config().control_api_port
    if port <= 0:
        logger.info("[E2E] Control API disabled; relying on queue refresh")
        return False
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/api/refresh",
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    logger.info("[E2E] Refresh triggered successfully")
                    return True
                return False
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries - 1:
                wait = 1 * (attempt + 1)  # 1s, 2s, 3s, 4s backoff
                logger.info("[E2E] Refresh attempt %d failed (%s), retrying in %ds...",
                           attempt + 1, e, wait)
                _time.sleep(wait)
            else:
                logger.warning("[E2E] Failed to trigger refresh after %d attempts: %s",
                              max_retries, e)
                return False
    return False


class _InflightRefreshTracker:
    def __init__(self) -> None:
        self._pending: set[str] = set()

    def reset(self) -> None:
        self._pending.clear()

    def register(self, issue_key: str) -> None:
        self._pending.add(issue_key)

    def ensure_refreshed(self, port: int | None) -> None:
        if not self._pending:
            return
        pending = set(self._pending)
        self._pending.clear()
        logger.info("[E2E] Triggering refresh for %d inflight issue(s)", len(pending))
        if not trigger_refresh(port):
            self._pending.update(pending)


_inflight_refresh_tracker = _InflightRefreshTracker()


def register_inflight_issue(issue: IssueKey) -> None:
    """Record an inflight issue that requires a refresh when waiting."""
    _inflight_refresh_tracker.register(issue.stable_id())


def ensure_inflight_refresh(port: int | None) -> None:
    """Trigger a single refresh if inflight issues are pending."""
    _inflight_refresh_tracker.ensure_refreshed(port)


@pytest.fixture(autouse=True)
def e2e_inflight_refresh_guard() -> None:
    """Reset refresh tracking per test so inflight issues don't leak."""
    _inflight_refresh_tracker.reset()
    yield


def _control_api_port() -> int | None:
    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    if env_port is not None:
        return int(env_port)
    return None


def _fetch_gh_audit_report(port: int | None) -> dict | None:
    if port is None or port <= 0:
        return None
    import urllib.request
    import urllib.error
    import time as _time
    import threading

    for attempt in range(3):
        try:
            payload: dict | None = None
            error: Exception | None = None

            def _fetch() -> None:
                nonlocal payload, error
                try:
                    req = urllib.request.Request(
                        f"http://localhost:{port}/api/gh_audit_report",
                        data=b"{}",
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    error = exc

            thread = threading.Thread(target=_fetch, daemon=True)
            thread.start()
            thread.join(timeout=6)
            if thread.is_alive():
                logger.info("[E2E] GH audit report fetch timed out (hard)")
                if attempt == 2:
                    return None
                _time.sleep(1 + attempt)
                continue
            if error:
                raise error
            if payload is None:
                raise RuntimeError("Empty GH audit payload")
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            logger.info("[E2E] GH audit report fetch failed: %s", exc)
            if attempt == 2:
                return None
            _time.sleep(1 + attempt)

    path = payload.get("path") if isinstance(payload, dict) else None
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        logger.info("[E2E] GH audit report read failed: %s", exc)
        return None


def _usage_units_from_report(report: dict) -> int:
    return int(report.get("usage_units", 0))


def _calls_from_report(report: dict) -> int:
    return int(report.get("total_calls", 0))


def _scope_usage(report: dict, scope: str) -> int:
    totals = report.get("by_scope_totals") or {}
    entry = totals.get(scope) or {}
    return int(entry.get("usage_units", 0))


def _scope_calls(report: dict, scope: str) -> int:
    totals = report.get("by_scope_totals") or {}
    entry = totals.get(scope) or {}
    return int(entry.get("calls", 0))


def _delta_counts(before: dict | None, after: dict | None, key: str) -> dict[str, int]:
    before_map = (before or {}).get(key) or {}
    after_map = (after or {}).get(key) or {}
    deltas: dict[str, int] = {}
    for name, count in after_map.items():
        try:
            after_count = int(count)
        except (TypeError, ValueError):
            after_count = 0
        try:
            before_count = int(before_map.get(name, 0))
        except (TypeError, ValueError):
            before_count = 0
        delta = after_count - before_count
        if delta:
            deltas[str(name)] = delta
    return deltas


def _log_top_deltas(label: str, deltas: dict[str, int], limit: int = 5) -> None:
    if not deltas:
        return
    top = sorted(deltas.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    logger.info("[E2E] GH activity %s: %s", label, top)


@pytest.fixture(autouse=True)
def e2e_gh_activity_guard(request) -> None:
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
    port = _control_api_port()
    before = _fetch_gh_audit_report(port)
    yield
    after = _fetch_gh_audit_report(port)
    if not before or not after:
        return

    delta_usage = _usage_units_from_report(after) - _usage_units_from_report(before)
    delta_calls = _calls_from_report(after) - _calls_from_report(before)
    delta_startup_usage = _scope_usage(after, "startup") - _scope_usage(before, "startup")
    delta_periodic_usage = _scope_usage(after, "periodic") - _scope_usage(before, "periodic")
    delta_startup_calls = _scope_calls(after, "startup") - _scope_calls(before, "startup")
    delta_periodic_calls = _scope_calls(after, "periodic") - _scope_calls(before, "periodic")
    charged_usage = delta_usage - delta_startup_usage - delta_periodic_usage
    charged_calls = delta_calls - delta_startup_calls - delta_periodic_calls

    max_test_usage = marker.kwargs.get("test_gh_activity_limit")
    max_system_usage = marker.kwargs.get("system_gh_activity_limit")

    system_usage = delta_startup_usage + delta_periodic_usage
    logger.info(
        "[E2E] GH activity usage: test=%d system=%d total=%d",
        charged_usage,
        system_usage,
        delta_usage,
    )
    _log_top_deltas("by_issue", _delta_counts(before, after, "by_issue"))
    _log_top_deltas("by_caller", _delta_counts(before, after, "by_caller"))
    _log_top_deltas("by_command", _delta_counts(before, after, "by_command"))
    if max_test_usage is not None and charged_usage > int(max_test_usage):
        pytest.fail(f"Test GH activity exceeded limit: {charged_usage} > {max_test_usage}")
    if max_system_usage is not None and system_usage > int(max_system_usage):
        pytest.fail(f"System GH activity exceeded limit: {system_usage} > {max_system_usage}")

    snapshot = after.get("last_rate_limit") or {}
    core = snapshot.get("core") or {}
    remaining = core.get("remaining")
    if isinstance(remaining, int) and remaining <= 0:
        logger.warning("[E2E] GitHub rate limit exceeded (core.remaining <= 0)")
async def wait_for_issue_seen(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
) -> None:
    """Wait for an issue to appear in snapshots (resyncing as needed)."""
    deadline = time.monotonic() + timeout_s
    last_resync = 0.0
    min_resync_interval_s = 600.0
    while time.monotonic() < deadline:
        if issue_key in watcher.view.issues:
            return
        now = time.monotonic()
        if now - last_resync >= min_resync_interval_s:
            await watcher.resync_snapshot()
            last_resync = now
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    raise TimeoutError(f"Timed out waiting for issue {issue_key} to appear in snapshot")


async def wait_for_session_started(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
) -> None:
    """Wait until a session starts (in-progress label or PR created)."""
    deadline = time.monotonic() + timeout_s
    last_resync = 0.0
    min_resync_interval_s = 600.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_resync >= min_resync_interval_s:
            await watcher.resync_snapshot()
            last_resync = now
        issue_view = watcher.view.issues.get(issue_key)
        if issue_view:
            if "in-progress" in issue_view.labels or issue_view.pr.number is not None:
                return
        await asyncio.sleep(1)
    raise TimeoutError(f"Timed out waiting for session start or PR for {issue_key}")


async def wait_for_issue_label_snapshot(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    label: str,
    timeout_s: float,
) -> None:
    """Wait for a label to appear on an issue (resyncing as needed)."""
    deadline = time.monotonic() + timeout_s
    last_resync = 0.0
    min_resync_interval_s = 600.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_resync >= min_resync_interval_s:
            await watcher.resync_snapshot()
            last_resync = now
        issue_view = watcher.view.issues.get(issue_key)
        if issue_view and label in issue_view.labels:
            return
        await asyncio.sleep(1)
    raise TimeoutError(f"Timed out waiting for label {label} on {issue_key}")


def inflight_create(
    repo: str,
    title: str,
    labels: list[str],
    body: str = "Created mid-test.",
) -> IssueKey:
    """Create an issue while orchestrator is running.

    Note: Caller should call trigger_refresh() after creating all issues
    to notify the orchestrator to pick them up.

    Args:
        repo: GitHub repo in owner/repo format
        title: Issue title
        labels: Labels to apply
        body: Issue body

    Returns:
        IssueKey for the created issue
    """
    issue_number = create_issue(repo, title, labels, body)
    return GitHubIssueKey(repo=repo, external_id=str(issue_number))


def inflight_update(
    issue: IssueKey,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    port: int | None = None,
) -> None:
    """Update an issue while orchestrator is running.

    Args:
        issue: The issue to update
        add_labels: Labels to add
        remove_labels: Labels to remove
        port: Control API port for refresh (defaults to Config.control_api_port)
    """
    issue_number = int(issue.stable_id())
    update_issue(issue.scope(), issue_number, add_labels, remove_labels)
    trigger_refresh(port)


def inflight_close(
    issue: IssueKey,
    comment: str | None = None,
    port: int | None = None,
) -> None:
    """Close an issue while orchestrator is running.

    Args:
        issue: The issue to close
        comment: Optional comment when closing
        port: Control API port for refresh (defaults to Config.control_api_port)
    """
    issue_number = int(issue.stable_id())
    close_issue(issue.scope(), issue_number, comment)
    trigger_refresh(port)


@pytest.fixture
def test_label(request) -> str:
    """Generate unique label from test name for isolation."""
    return request.node.name


@pytest.fixture(scope="session")
def filter_label() -> str:
    """Configurable filter label for parallel test runs.

    Set E2E_FILTER env var to run parallel test sessions:
        E2E_FILTER=run-a pytest tests/e2e/
        E2E_FILTER=run-b pytest tests/e2e/  # parallel, no interference
    """
    return os.environ.get("E2E_FILTER", DEFAULT_E2E_FILTER_LABEL)


@pytest.fixture
def test_issue_factory(repo_name: str, test_label: str, filter_label: str):
    """Factory for creating test-scoped issues.

    Cleans up issues from previous failed runs of this test,
    then provides a factory to create fresh issues.
    """
    # Cleanup stale issues from this specific test
    cleanup_issues_by_label(repo_name, e2e_label(test_label))

    def create(title: str, extra_labels: list[str] | None = None) -> IssueKey:
        """Create an issue scoped to this test."""
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
def e2e_config(e2e_project_root: Path, tmp_path: Path, repo_name: str) -> Config:
    """Create e2e test config with e2e-test agent."""
    from issue_orchestrator.config import ValidationConfig, ValidationGateConfig

    config = Config()
    config.repo = repo_name
    config.repo_root = e2e_project_root
    config.ui_mode = "tmux"
    config.max_concurrent_sessions = 1
    config.filter_label = "test-data"
    config.github_token_env = _env_token_name()

    # Configure e2e-test agent (scripted for deterministic e2e)
    config.agents = {
        "agent:e2e-test": AgentConfig(
            prompt_path=e2e_project_root / "examples" / "scripts" / "complete-immediately.sh",
            worktree_base=tmp_path / "worktrees",
            timeout_minutes=3,
            model="sonnet",
            command="bash {prompt}",
            meta_agent="claude-code",
        )
    }

    # Short timeouts for tests
    config.session_timeout_minutes = 3

    # Default review timeout: code agent + review agent timeouts (seconds)
    from tests.e2e.flows import review_timeout_from_config
    os.environ["E2E_REVIEW_TIMEOUT_S"] = str(review_timeout_from_config(config))

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
    if _keep_artifacts():
        config.cleanup.with_triage.close_ai_session_tabs = False
        config.cleanup.with_triage.remove_worktrees = False
        config.cleanup.without_triage.close_ai_session_tabs = False
        config.cleanup.without_triage.remove_worktrees = False

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
    try:
        _github_client(repo_name).add_comment(issue_number, "Closed by e2e test cleanup.")
        _github_client(repo_name).update_issue_state(issue_number, "closed")
    except Exception:
        pass


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

    try:
        _github_client(repo_name).delete_label(run_label)
    except Exception:
        pass


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
        self._log_file: Path | None = None
        self._orchestrator_log_file: Path | None = None
        self._log_handle: "open | None" = None
        self._config_path: Path | None = None
        self._last_log_time: float | None = None

    def _write_e2e_config(self) -> Path:
        """Write an ephemeral config file so the CLI uses the e2e config."""
        config_dir = Path("/tmp/e2e-orchestrator-configs")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"issue-orchestrator.e2e.{os.getpid()}.yaml"
        data = {
            "repo": self.config.repo,
            "repo_root": str(self.config.repo_root),
            "filter_label": self.config.filter_label,
            "github_token_env": self.config.github_token_env,
            "ui_mode": self.config.ui_mode,
            "web_port": self.config.web_port,
            "control_api_port": self.config.control_api_port,
            "queue_refresh_seconds": self.config.queue_refresh_seconds,
            "e2e_pr_labels": self.config.e2e_pr_labels,
            "gh_write_verify_timeout_seconds": self.config.gh_write_verify_timeout_seconds,
            "gh_write_verify_initial_delay_ms": self.config.gh_write_verify_initial_delay_ms,
            "gh_write_verify_max_delay_ms": self.config.gh_write_verify_max_delay_ms,
            "gh_write_verify_backoff": self.config.gh_write_verify_backoff,
            "gh_write_verify_jitter_ms": self.config.gh_write_verify_jitter_ms,
            "gh_audit_enabled": self.config.gh_audit_enabled,
            "gh_audit_events": self.config.gh_audit_events,
            "gh_audit_file": self.config.gh_audit_file,
            "concurrency": {
                "max_concurrent_sessions": self.config.max_concurrent_sessions,
                "session_timeout_minutes": self.config.session_timeout_minutes,
            },
            "agents": {
                label: {
                    "prompt": str(cfg.prompt_path),
                    "worktree_base": str(cfg.worktree_base),
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "permission_mode": cfg.permission_mode,
                    "command": cfg.command,
                    "meta_agent": cfg.meta_agent,
                    "repo_root": str(self.config.repo_root),
                }
                for label, cfg in self.config.agents.items()
            },
            "validation": {
                "agent_gate": {
                    "cmd": self.config.validation.agent_gate.cmd,
                    "timeout_seconds": self.config.validation.agent_gate.timeout_seconds,
                },
                "publish_gate": {
                    "cmd": self.config.validation.publish_gate.cmd,
                    "timeout_seconds": self.config.validation.publish_gate.timeout_seconds,
                },
            },
            "validation_policy": {
                "publish_requires": self.config.validation_policy.publish_requires,
                "agent_runs": self.config.validation_policy.agent_runs,
            },
            "review": {
                "code_review_agent": self.config.code_review_agent,
                "code_review_label": self.config.code_review_label,
                "code_reviewed_label": self.config.code_reviewed_label,
                "max_rework_cycles": self.config.max_rework_cycles,
                "triage_review_agent": self.config.triage_review_agent,
                "triage_review_label": self.config.triage_review_label,
                "triage_reviewed_label": self.config.triage_reviewed_label,
                "triage_review_threshold": self.config.triage_review_threshold,
                "triage_review_on_failure": self.config.triage_review_on_failure,
            },
            "cleanup": {
                "with_triage": {
                    "close_ai_session_tabs": self.config.cleanup.with_triage.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.with_triage.remove_worktrees,
                },
                "without_triage": {
                    "wait_for_code_review": self.config.cleanup.without_triage.wait_for_code_review,
                    "close_ai_session_tabs": self.config.cleanup.without_triage.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.without_triage.remove_worktrees,
                },
            },
            "dangerous": {
                "allow_unsupported_agents": self.config.dangerous.allow_unsupported_agents,
            },
        }
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))
        self._config_path = config_path
        return config_path

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
                    try:
                        line = self.process.stderr.readline()
                    except ValueError:
                        break
                    if line:
                        text = line.decode('utf-8', errors='replace').rstrip()
                        self._output_lines.append(text)
                        self._last_log_time = time.time()
                        # Always write to persistent log file
                        if self._log_handle:
                            self._log_handle.write(f"{text}\n")
                            self._log_handle.flush()
                        # Print orchestrator events with prefix (filtered for readability)
                        if any(kw in text for kw in ['[EVENT]', 'Session', 'Issue', 'PR', 'Review', 'launch', 'complet', 'start', 'ERROR', 'WARN', 'failed', 'timeout']):
                            print(f"  [ORCH] {text}", file=sys.stderr, flush=True)

    def start(self, max_issues: int = 1, extra_args: list[str] | None = None) -> None:
        """Start the orchestrator process."""
        import sys
        import threading
        from datetime import datetime

        # Create persistent log file (survives Ctrl+C)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_file = E2E_LOG_DIR / f"e2e-{timestamp}.log"
        orchestrator_log_file = E2E_LOG_DIR / f"orchestrator-{timestamp}.log"
        self._orchestrator_log_file = orchestrator_log_file
        self._log_handle = open(self._log_file, "w")

        # Clean up old log files (keep last 10)
        log_files = sorted(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_log in log_files[10:]:
            old_log.unlink()

        # Print debug paths upfront for troubleshooting
        worktree_dir = Path("/tmp/e2e-worktrees")  # E2E worktree location
        claude_logs = Path.home() / ".claude" / "logs"
        print(f"\n  {'='*60}", flush=True)
        print(f"  [E2E DEBUG PATHS]", flush=True)
        print(f"    Orchestrator log: {self._log_file}", flush=True)
        print(f"    Orchestrator file: {orchestrator_log_file}", flush=True)
        print(f"    Worktrees:        {worktree_dir}", flush=True)
        print(f"    Claude logs:      {claude_logs}", flush=True)
        print(f"    Keep artifacts:   {_keep_artifacts()}", flush=True)
        print(f"    Keep remote:      {_keep_remote_artifacts()}", flush=True)
        if os.environ.get("E2E_CLAUDE_ARGS"):
            print(f"    E2E_CLAUDE_ARGS:  {os.environ.get('E2E_CLAUDE_ARGS')}", flush=True)
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            print(f"    E2E_PROMPT_MODE:  {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}", flush=True)
        print(f"  {'='*60}\n", flush=True)

        # Write header to log file
        self._log_handle.write(f"E2E Test Run: {timestamp}\n")
        self._log_handle.write(f"Orchestrator file: {orchestrator_log_file}\n")
        self._log_handle.write(f"Worktrees: {worktree_dir}\n")
        self._log_handle.write(f"Claude logs: {claude_logs}\n")
        self._log_handle.write(f"Keep artifacts: {_keep_artifacts()}\n")
        self._log_handle.write(f"Keep remote: {_keep_remote_artifacts()}\n")
        self._log_handle.write(f"E2E_KEEP_ARTIFACTS: {os.environ.get('E2E_KEEP_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_KEEP_REMOTE_ARTIFACTS: {os.environ.get('E2E_KEEP_REMOTE_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_CONTROL_API_PORT: {os.environ.get('E2E_CONTROL_API_PORT', '')}\n")
        if os.environ.get("E2E_CLAUDE_ARGS"):
            self._log_handle.write(f"E2E_CLAUDE_ARGS: {os.environ.get('E2E_CLAUDE_ARGS')}\n")
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            self._log_handle.write(f"E2E_CLAUDE_PROMPT_MODE: {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}\n")
        self._log_handle.write("=" * 60 + "\n\n")
        self._log_handle.flush()

        # Prefer project .venv (has e2e deps like fastapi); fall back to pytest venv
        preferred_bin = self.project_root / ".venv" / "bin" / "issue-orchestrator"
        venv_bin = preferred_bin if preferred_bin.exists() else Path(sys.executable).parent / "issue-orchestrator"

        # Allow UI mode override via env var for interactive debugging
        ui_mode = os.environ.get("E2E_UI_MODE", "tmux")

        config_path = self._write_e2e_config()
        cmd = [
            str(venv_bin), "--config", str(config_path), "start",
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
        env["ORCHESTRATOR_LOG_LEVEL"] = "DEBUG"
        env["ORCHESTRATOR_LOG_FILE"] = str(orchestrator_log_file)
        env["PYTHONUNBUFFERED"] = "1"
        # Enable event logging to stderr (works with --no-dashboard)
        env["ORCHESTRATOR_LOG_TO_STDERR"] = "1"
        if os.environ.get("E2E_CLAUDE_ARGS"):
            env["ORCHESTRATOR_CLAUDE_ARGS"] = os.environ["E2E_CLAUDE_ARGS"]
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            env["ORCHESTRATOR_CLAUDE_PROMPT_MODE"] = os.environ["E2E_CLAUDE_PROMPT_MODE"]
        env["ORCHESTRATOR_WORKTREE_PER_SESSION"] = os.environ.get("E2E_WORKTREE_PER_SESSION", "1")
        env["ORCHESTRATOR_DISABLE_WORKTREE_REUSE"] = os.environ.get("E2E_DISABLE_WORKTREE_REUSE", "1")

        print(f"  [E2E] Starting orchestrator: {' '.join(cmd)}", flush=True)
        self._log_handle.write(f"Command: {' '.join(cmd)}\n\n")
        self._log_handle.flush()

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
            self._cleanup_log_tailers()
            self._close_log_file()
            print(f"  [E2E] Orchestrator stopped gracefully", flush=True)
            return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
        except subprocess.TimeoutExpired:
            print(f"  [E2E] Sending second SIGTERM...", flush=True)
            # Send second SIGTERM to trigger force-kill of child sessions
            self.process.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = self.process.communicate(timeout=5)
                self._cleanup_tmux_sessions()
                self._cleanup_log_tailers()
                self._close_log_file()
                print(f"  [E2E] Orchestrator stopped after second SIGTERM", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
            except subprocess.TimeoutExpired:
                # Last resort - kill the process
                print(f"  [E2E] Force killing orchestrator...", flush=True)
                self.process.kill()
                stdout, stderr = self.process.communicate()
                self._cleanup_tmux_sessions()
                self._cleanup_log_tailers()
                self._close_log_file()
                print(f"  [E2E] Orchestrator killed", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""

    @property
    def log_path(self) -> Path | None:
        """Get the path to the persistent log file."""
        return self._log_file

    def orchestrator_log_path(self) -> Path | None:
        """Get the path to the orchestrator log file."""
        return self._orchestrator_log_file

    def last_log_age_seconds(self) -> float:
        """Return seconds since last orchestrator stderr log line."""
        if not self._last_log_time:
            return 0.0
        return time.time() - self._last_log_time

    def _close_log_file(self) -> None:
        """Close log file and print location for debugging."""
        if self._log_handle:
            self._log_handle.write(f"\n{'='*60}\nOrchestrator stopped at {time.strftime('%H:%M:%S')}\n")
            self._log_handle.close()
            self._log_handle = None
        if self._log_file:
            print(f"  [E2E] Full log saved to: {self._log_file}", flush=True)
        if self._config_path and self._config_path.exists():
            try:
                self._config_path.unlink()
            except OSError:
                pass

    def _cleanup_tmux_sessions(self) -> None:
        """Clean up any tmux windows created by e2e tests.

        E2E test windows have names like '#123 [E2E-TEST]...'
        We kill these to prevent zombie accumulation.
        """
        if _keep_artifacts():
            return
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

    def _cleanup_log_tailers(self) -> None:
        """Stop lingering session.log tail processes from tmux pipe-pane."""
        if _keep_artifacts():
            return
        try:
            result = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return
        for line in result.stdout.splitlines():
            if "cat >>" not in line or ".issue-orchestrator/session.log" not in line:
                continue
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue

    def is_running(self) -> bool:
        """Check if process is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def request_refresh(self) -> bool:
        """Request the orchestrator to refresh issues on next tick via control API.

        Returns True if request succeeded.
        """
        return trigger_refresh()


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
        labels = _github_client(repo).get_issue_labels(issue_number)
        # Fail fast if the session already failed/blocked.
        if any(lbl in labels for lbl in ("blocked-failed", "blocked-needs-human", "failed")):
            raise RuntimeError(
                f"Issue #{issue_number} entered failure state: {sorted(labels)}"
            )
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
        prs = _github_client(repo).list_prs(state="open", limit=100)
        for pr in prs:
            head_ref = (pr.get("head") or {}).get("ref", "")
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
    return _github_client(repo).get_issue_comments(issue_number)
